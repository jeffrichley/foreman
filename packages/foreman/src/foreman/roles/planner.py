"""Planner role dispatcher.

The Planner LLM returns a :class:`~foreman.schemas.planner.PlannerOutput`
(spec content + PR metadata). Foreman core then performs all deterministic
host-platform operations through the
:class:`~foreman.git_host.GitHostProvider` abstraction:

  1. Parse the issue URL (owner / repo / number)
  2. Resolve the role's :class:`~foreman.git_host.GitHostProvider`
     (via :class:`~foreman.identity.IdentityRegistry`)
  3. Fetch :class:`~foreman.git_host.IssueRef` via ``host.get_issue``
  4. Create the per-ticket worktree (:class:`~foreman.worktree.WorktreeManager`)
  5. Build LLM context and dispatch with READ-ONLY tools
  6. Parse the ``PlannerOutput``
  7. Commit the spec doc (``host.commit_files_to_worktree``)
  8. Push the branch (``host.push_branch``)
  9. Open the spec PR (``host.open_pull_request``). The issue stays at
      ``foreman:planning`` so v3's reconciler (``dispatch_reviewer_spec``)
      fires on ``foreman:planning`` + the open spec PR — no label mutation
      needed here.
  10. Return :class:`~foreman.schemas.planner.PlannerRunResult`

Decoupling rationale (Foreman issue #8 — the "Looper pattern"):
the LLM is non-deterministic and host-agnostic; deterministic operations
that the LLM might get wrong (or that would couple us to GitHub at the
prompt layer) live in core behind :class:`~foreman.git_host.GitHostProvider`.
A future GitLab provider drops in without prompt or role-dispatcher changes.

For walking skeleton: cleanup is NOT performed automatically (per spec —
worktrees persist until pipeline completion, which here is the human merging
the PR).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from foreman.branches import spec_branch
from foreman.config import Config
from foreman.dispatch_recorder import DispatchRecorder, emit_recorder_complete
from foreman.git_host import CommentRef, GitHostProvider, IssueRef
from foreman.identity import IdentityRegistry
from foreman.instructions import load_project_instructions
from foreman.provider import ProviderFacade, UsageInfo
from foreman.providers import ProviderError
from foreman.roles import TERMINAL_BLOCKING_LABEL, handle_unhandled_role_exception
from foreman.roles._prompt_helpers import (
    filter_bot_self_comments,
    format_comments_section,
    format_labels_section,
)
from foreman.schemas.planner import PlannerOutput, PlannerRunResult
from foreman.stats import log_planner_run
from foreman.worktree import WorktreeManager

_ISSUE_URL_RE = re.compile(
    r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)"
)

# Tool capabilities matrix for Planner (post-refactor — see spec §4.1).
# Planner LLM is now read-only: spec doc travels back via structured output,
# core performs the commit/push/PR/label operations through GitHostProvider.
PLANNER_ALLOWED_TOOLS = ["Read", "Glob", "Grep"]


def parse_issue_url(url: str) -> tuple[str, str, int]:
    """Extract (owner, repo, issue_number) from a GitHub issue URL."""
    m = _ISSUE_URL_RE.match(url.strip())
    if not m:
        raise ValueError(f"Not a GitHub issue URL: {url!r}")
    return m["owner"], m["repo"], int(m["number"])


def _load_planner_prompt() -> str:
    """Load the Planner system prompt: vendored ``writing-plans`` followed
    by the Foreman-specific Planner contract.

    The discipline that makes superpowers' interactive Claude Code write
    rigorous, bite-sized plans is inlined here so the SDK-driven Planner
    role sees the same instructions. See
    :func:`foreman.prompts.compose_role_prompt` for the composition
    details.
    """
    from foreman.prompts import compose_role_prompt

    return compose_role_prompt(role="planner", superpowers=["writing-plans"])


def _build_user_prompt(
    *,
    issue: IssueRef,
    instructions: str | None,
    comments: list[CommentRef],
    labels: set[str],
) -> str:
    """Compose the per-run user prompt.

    ``instructions`` carries the verbatim contents of the project's
    ``.foreman/INSTRUCTIONS.md`` when present; the section is emitted
    near the top so project-specific conventions (PR title rules, branch
    conventions, etc.) frame everything that follows. When ``None`` the
    section is omitted entirely — empty headers would be a distracting
    no-op the LLM would have to mentally skip.

    ``comments`` is the originating issue's comment stream (foreman#328),
    already filtered to exclude foreman role-bot self-comments and
    ordered chronologically (oldest first). Empty list → no ``## Comments``
    section is emitted.

    ``labels`` is the issue's label set (foreman#328) — informs the
    Planner whether to treat the issue as a bug / feat / epic. Empty
    set → no ``## Labels`` section is emitted.
    """
    instructions_section = (
        f"## Project-specific instructions\n\n{instructions}\n\n" if instructions else ""
    )
    comments_section = format_comments_section(comments)
    labels_section = format_labels_section(labels)
    return (
        f"You are processing GitHub issue #{issue.number}.\n\n"
        f"{instructions_section}"
        f"## Title\n{issue.title}\n\n"
        f"## Body\n{issue.body}\n\n"
        f"{comments_section}"
        f"{labels_section}"
        f"Follow the steps in your system prompt. Return your structured "
        f"output when done."
    )


def _spec_doc_relpath(issue_number: int) -> str:
    return f"docs/superpowers/specs/foreman-issue-{issue_number}-spec.md"


# foreman#63: GitHub auto-closes the originating issue at merge time when
# a merged PR's body contains one of the nine "closing keywords" + a
# ``#N`` or ``owner/repo#N`` reference. The Planner produces spec PR
# bodies; issue closure must route through ``daemon_runners.merge_impl_pr``
# instead (which fires only after the Reviewer-on-impl approves). This
# regex matches the verb + optional ``:`` separator + whitespace, with a
# lookahead that preserves the bare issue reference so the body still
# reads cleanly after substitution.
_AUTO_CLOSE_KEYWORDS_RE = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\b\s*:?\s+"
    r"(?=(?:[\w.-]+/[\w.-]+)?#\d+)"
)


def _strip_auto_close_keywords(body: str) -> str:
    """Remove GitHub auto-close keyword+separator prefixes from ``body``.

    GitHub auto-closes the originating issue when a merged PR's body
    contains any of nine "closing keywords" (close/closes/closed,
    fix/fixes/fixed, resolve/resolves/resolved) followed by a ``#N`` or
    ``owner/repo#N`` reference. The Planner produces spec PR bodies;
    issue closure must route through ``daemon_runners.merge_impl_pr``
    (foreman#63). This helper strips the verb+separator while
    preserving the bare ``#N`` reference so the body still reads
    cleanly. The helper is idempotent and a no-op on bodies that
    contain no auto-close keywords.
    """
    return _AUTO_CLOSE_KEYWORDS_RE.sub("", body)


async def run_planner(
    *,
    issue_url: str,
    config: Config,
    project_name: str,
    worktrees_root: Path,
    provider: ProviderFacade,
    identity_registry: IdentityRegistry | None = None,
    dispatch_recorder: DispatchRecorder | None = None,
    dispatch_trace_id: int | None = None,
) -> PlannerRunResult:
    """Run the Planner role end-to-end on one issue.

    Args:
        issue_url: Full GitHub issue URL (``https://github.com/owner/repo/issues/N``).
        config: Loaded foreman config.
        project_name: Key into ``config.projects``.
        worktrees_root: Root directory under which per-ticket worktrees live.
        provider: Agent provider facade (e.g., AnthropicSDKProvider).
        identity_registry: Optional pre-built registry; defaults to a fresh
            :class:`~foreman.identity.IdentityRegistry` for the project. Useful
            for tests that want to inject a fake host provider.

    Returns:
        :class:`~foreman.schemas.planner.PlannerRunResult` carrying both the
        LLM output and the opened PR's metadata.
    """
    # foreman#235: stamp ``start_time`` BEFORE the body wrap and
    # initialize ``usage`` / ``pr_number`` to ``None`` so the except
    # branch below can log partial state regardless of where in the
    # pipeline a failure surfaces. ``WorktreeManager.create``,
    # ``provider.run_agent``, ``host.commit_files_to_worktree``,
    # ``host.push_branch``, and ``host.open_pull_request`` are all
    # inside the wrap; pre-#235 any of those raising silently dropped
    # the run's cost telemetry because the success-path
    # ``log_planner_run`` call below never executed.
    #
    # Post-adversarial-review: extend the wrap to ALSO cover URL parse,
    # project lookup, identity setup, and host.get_issue / .get_default_branch
    # so a failure in any of those — a malformed URL, a wrong project
    # name, a stale auth token, a network blip on the get_issue HTTP
    # call — fires the runaway-burn defense helper too. Without the
    # extension, those pre-body exceptions would crash the role
    # subprocess without ever transitioning the in-flight label, and
    # the dispatcher would re-fire on the next poll until #228's
    # rate-limit caught the loop at N=3. Now the FIRST such failure
    # transitions the ticket and posts a comment.
    start_time = time.monotonic()
    usage: UsageInfo | None = None
    pr_number: int | None = None
    actual_repo_slug: str | None = None
    issue_number: int | None = None
    host: GitHostProvider | None = None

    def _on_failure(exc: BaseException) -> None:
        """Shared cleanup body for both ``except ProviderError`` and
        ``except Exception`` arms (foreman#266 — type-narrowing split).

        Closes over ``start_time`` / ``usage`` / ``pr_number`` /
        ``actual_repo_slug`` / ``issue_number`` / ``host`` /
        ``project_name`` / ``dispatch_recorder`` /
        ``dispatch_trace_id`` from the enclosing scope. The bare
        ``raise`` that re-propagates the original exception lives in
        each ``except`` arm AFTER calling this helper — keeps the
        re-raise inside its own arm where Python's exception context
        is alive.
        """
        # foreman#235 + foreman#229: capture cost telemetry AND defend
        # against the runaway-burn pattern.
        #
        # Pre-#235 any exception from worktree.create / provider.run_agent
        # / commit_files_to_worktree / push_branch / open_pull_request
        # propagated straight up and the JSONL row was never written —
        # failed runs vanished from ``planner.jsonl`` and cross-role
        # cost rollups under-counted by exactly the failure rate.
        #
        # Pre-#229 the issue's in-flight label (``foreman:planning``)
        # was NOT transitioned. The dispatcher's next poll then
        # re-dispatched the SAME role on the SAME ticket — the runaway
        # (foreman#227 = 171 dispatches in 2h52m). The defensive
        # ``handle_unhandled_role_exception`` call below transitions
        # the issue to ``foreman:needs-help`` so the dispatcher stops
        # re-dispatching until an operator removes the label.
        duration_seconds = time.monotonic() - start_time
        # Stats writes need ``actual_repo_slug`` + ``issue_number``. If
        # the exception happened during the URL parse itself those will
        # be None; skip the stats write rather than synthesize fake
        # values (under-counting is fixable; bogus rows mislead
        # downstream cost rollups).
        if actual_repo_slug is not None and issue_number is not None:
            try:
                log_planner_run(
                    repo_slug=actual_repo_slug,
                    issue_number=issue_number,
                    pr_number=pr_number,
                    outcome="exception",
                    duration_seconds=duration_seconds,
                    input_tokens=usage.input_tokens if usage is not None else 0,
                    output_tokens=usage.output_tokens if usage is not None else 0,
                    cache_creation_input_tokens=(
                        usage.cache_creation_input_tokens if usage is not None else 0
                    ),
                    cache_read_input_tokens=(
                        usage.cache_read_input_tokens if usage is not None else 0
                    ),
                    total_cost_usd=usage.total_cost_usd if usage is not None else None,
                    model_usage=usage.model_usage if usage is not None else None,
                    duration_ms=usage.duration_ms if usage is not None else 0,
                    num_turns=usage.num_turns if usage is not None else 0,
                )
            except Exception:
                # Best-effort telemetry — swallow and continue so the
                # daemon dispatcher sees the ORIGINAL exception, not
                # whatever the stats writer raised. Surfacing the stats
                # failure here would mask the actual Planner failure
                # that triggered this branch.
                pass
            # foreman#251 (Phase 1): mirror the dual-write on the failure
            # path. ``usage`` may be None (if ``provider.run_agent`` never
            # returned) — the helper fills zeros via a default UsageInfo
            # so the Recorder's cost columns stay populated with explicit
            # zeros rather than NULL, matching the existing JSONL shape.
            emit_recorder_complete(
                dispatch_recorder=dispatch_recorder,
                dispatch_trace_id=dispatch_trace_id,
                role="planner",
                repo_slug=actual_repo_slug,
                ticket_id=f"{actual_repo_slug}#{issue_number}",
                project=project_name,
                issue_number=issue_number,
                pr_number=pr_number,
                outcome="exception",
                usage=usage if usage is not None else UsageInfo(),
                role_data={},
                duration_seconds=duration_seconds,
            )
        # foreman#229: runaway-burn defense. Post the traceback as a
        # comment on the originating issue and transition it to
        # ``foreman:needs-help`` so the dispatcher's poll loop stops
        # re-dispatching the same role on the same ticket.
        if host is not None and actual_repo_slug is not None and issue_number is not None:
            bound_host = host
            bound_repo_slug = actual_repo_slug
            bound_issue_number = issue_number
            handle_unhandled_role_exception(
                role="planner",
                issue_number=bound_issue_number,
                exc=exc,
                post_comment=lambda body: bound_host.post_issue_comment(
                    bound_repo_slug, bound_issue_number, body
                ),
                set_needs_help_label=lambda: bound_host.update_issue_labels(
                    bound_repo_slug,
                    bound_issue_number,
                    add=[TERMINAL_BLOCKING_LABEL],
                    remove=[],
                ),
            )

    try:
        owner, repo_name, issue_number = parse_issue_url(issue_url)
        project = config.projects[project_name]
        expected_repo_slug = project.repo  # e.g. "jeffrichley/voice"
        actual_repo_slug = f"{owner}/{repo_name}"
        if expected_repo_slug != actual_repo_slug:
            raise ValueError(
                f"Issue URL repo {actual_repo_slug!r} does not match project "
                f"{project_name!r} configured repo {expected_repo_slug!r}"
            )

        registry = identity_registry if identity_registry is not None else IdentityRegistry(project)
        host = registry.get_host_provider("planner")
        planner_token: str = registry.get_planner_token()

        issue = host.get_issue(actual_repo_slug, issue_number)
        default_branch = host.get_default_branch(actual_repo_slug)

        # foreman#328: pull in the originating issue's comments + labels so
        # the Planner sees operator-supplied post-filing context. The
        # raw comment stream goes through ``filter_bot_self_comments`` so
        # the foreman role bots' own previous postings don't feed back
        # into subsequent Planner runs (the exact failure mode that
        # motivated this ticket — see agent_core#180).
        raw_comments = host.get_issue_comments(actual_repo_slug, issue_number)
        comments = filter_bot_self_comments(raw_comments, registry.get_role_bot_logins())

        # WorktreeManager's git subprocesses (fetch / worktree add) must
        # authenticate as the planner bot — without the explicit token they
        # inherit the daemon's parent ``GH_TOKEN`` (CI runner, dev shell)
        # and attribute identity to the daemon's identity on private repos.
        # Same anti-leak motivation as the Stage 3e role-module subprocess
        # fix; WorktreeManager was scoped out at the time as a follow-up.
        wt_mgr = WorktreeManager(worktrees_root=worktrees_root, role_token=planner_token)
        wt_path = wt_mgr.create(
            clone_path=Path(project.local_clone_path),
            repo_slug=repo_name,
            ticket_id=issue_number,
            dev_base_branch=project.dev_base_branch,
            repo_url=f"https://github.com/{project.repo}.git",
        )

        # foreman#53: bot identity is injected per-commit via env vars inside
        # ``host.commit_files_to_worktree``; the legacy
        # ``configure_worktree_identity`` call mutated ``.git/config`` which
        # worktrees share with the parent repo, leaking the bot identity
        # into subsequent human commits.

        instructions = load_project_instructions(Path(project.local_clone_path))

        system_prompt = _load_planner_prompt()
        user_prompt = _build_user_prompt(
            issue=issue,
            instructions=instructions,
            comments=comments,
            labels=set(issue.labels),
        )

        llm_output, run_usage = await provider.run_agent(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            allowed_tools=PLANNER_ALLOWED_TOOLS,
            output_model=PlannerOutput,
            cwd=wt_path,
        )
        # foreman#235: hoist ``usage`` to the outer scope IMMEDIATELY
        # so a failure in any later step (commit/push/open_pull_request)
        # still records the per-call token cost in the spec_failed row.
        usage = run_usage

        branch = spec_branch(issue_number)
        host.commit_files_to_worktree(
            worktree_path=wt_path,
            files={_spec_doc_relpath(issue_number): llm_output.spec_doc_content},
            message=llm_output.pr_title,
        )
        host.push_branch(worktree_path=wt_path, branch=branch)
        # foreman#63: strip GitHub auto-close keywords (Closes / Fixes /
        # Resolves + #N) from the PR body before opening. Issue closure
        # routes through daemon_runners.merge_impl_pr; an auto-close in the
        # spec PR's body would short-circuit that gate. Defense in depth —
        # the Planner prompt also forbids these keywords. The original
        # ``llm_output.pr_body`` is preserved on the audit-log copy.
        pr = host.open_pull_request(
            repo_slug=actual_repo_slug,
            title=llm_output.pr_title,
            body=_strip_auto_close_keywords(llm_output.pr_body),
            base=default_branch,
            head=branch,
        )
        # foreman#235: hoist ``pr_number`` to the outer scope right
        # after ``open_pull_request`` returns. If a later step raises
        # (today there are none, but defense in depth) the spec_failed
        # row still reports the PR number — non-zero signal that the
        # PR opened before whatever broke.
        pr_number = pr.number
        # v3 label vocabulary: Planner writes ZERO labels. The issue stays
        # at ``foreman:planning`` (the entry label) after the spec PR opens;
        # v3's reconciler ``dispatch_reviewer_spec`` rule then fires on
        # ``foreman:planning`` + an open spec PR. No host label mutation
        # happens here.
        #
        # Historical note: a prior revision attempted a best-effort cleanup
        # of the legacy ``foreman:plan`` entry label on v2-era tickets. That
        # cleanup raised PyGithub ``GithubException(404)`` on every fresh v3
        # ticket (which never carries ``foreman:plan``) because
        # ``issue.remove_from_labels`` 404s when the label is absent. Since
        # ``foreman init`` no longer creates ``foreman:plan`` (PR #111), the
        # cleanup is a back-compat tail that's no longer worth its crash
        # surface. v2-era tickets that still carry the label must be
        # cleaned up manually.

        # foreman#91: compute final_labels deterministically from the
        # pre-mutation set (``issue.labels`` is the snapshot taken by
        # ``host.get_issue`` above) + the role's known transitions.
        # v3: the Planner makes no label changes, so the final set is just
        # the pre-mutation set (sorted for stability).
        final_labels = sorted(set(issue.labels))

        duration_seconds = time.monotonic() - start_time

        # foreman#227: append the per-call token usage + cost to the
        # Planner JSONL stats file at
        # ``~/.foreman/stats/<owner>__<repo>/planner.jsonl``.
        # foreman#233: include the required ``outcome`` field. Reaching
        # this line means ``host.open_pull_request`` returned a ``pr`` —
        # by definition that's ``spec_written``. The ``spec_failed``
        # mirror lands in the ``except`` branch below (foreman#235).
        log_planner_run(
            repo_slug=actual_repo_slug,
            issue_number=issue_number,
            pr_number=pr_number,
            outcome="spec_written",
            duration_seconds=duration_seconds,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
            total_cost_usd=usage.total_cost_usd,
            model_usage=usage.model_usage,
            duration_ms=usage.duration_ms,
            num_turns=usage.num_turns,
        )
        # foreman#251 (Phase 1): dual-write via the DispatchRecorder.
        # When the daemon dispatched this run it constructed a
        # Recorder + propagated the trace_id via env; the CLI entry
        # point unpacks both and forwards them here. The existing
        # ``log_planner_run`` call above stays in place — Phase 2 will
        # collapse it.
        emit_recorder_complete(
            dispatch_recorder=dispatch_recorder,
            dispatch_trace_id=dispatch_trace_id,
            role="planner",
            repo_slug=actual_repo_slug,
            ticket_id=f"{actual_repo_slug}#{issue_number}",
            project=project_name,
            issue_number=issue_number,
            pr_number=pr_number,
            outcome="spec_written",
            usage=usage,
            role_data={},
            duration_seconds=duration_seconds,
        )

        return PlannerRunResult(llm_output=llm_output, pr=pr, final_labels=final_labels)
    except ProviderError as exc:
        # foreman#266: typed catch for the documented provider-boundary
        # failure mode. The body is identical to the
        # ``except Exception`` arm below — the change is structural
        # (type narrowing for readability + boundary documentation),
        # not semantic. The belt-and-suspenders ``except Exception``
        # arm stays in place for non-provider failures (worktree ops,
        # host I/O, etc.).
        _on_failure(exc)
        raise
    except Exception as exc:
        # PR #255 commit 2 defensive handler — belt-and-suspenders for
        # non-provider failures (worktree ops, host I/O, GitHub 5xx).
        # The handler body is shared with the typed ``except
        # ProviderError`` arm above (foreman#266) via ``_on_failure``.
        _on_failure(exc)
        raise
