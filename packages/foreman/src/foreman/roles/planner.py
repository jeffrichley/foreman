"""Planner role dispatcher.

The Planner LLM returns a :class:`~foreman.schemas.planner.PlannerOutput`
(spec content + PR metadata). Foreman core then performs all deterministic
host-platform operations through the
:class:`~foreman.git_host.GitHostProvider` abstraction:

  1. Parse the issue URL (owner / repo / number)
  2. Resolve the role's :class:`~foreman.git_host.GitHostProvider`
     (via :class:`~foreman.v4.identity.V4IdentityRegistry` +
     :func:`foreman.roles.build_role_resources`)
  3. Fetch :class:`~foreman.git_host.IssueRef` via ``host.get_issue``
  4. Create the per-ticket worktree (:class:`~foreman.worktree.WorktreeManager`)
  5. Build LLM context and dispatch with READ-ONLY tools
  6. Parse the ``PlannerOutput``
  7. Commit the spec doc (``host.commit_files_to_worktree``)
  8. Push the branch (``host.push_branch``)
  9. Open the spec PR (``host.open_pull_request``). The Planner makes
      no label mutations — under v4, ``LabelObservabilityObserver`` owns
      every ``foreman:*`` label write off state transitions.
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

from foreman.auto_close import (
    strip_auto_close_keywords as _strip_auto_close_keywords,
)
from foreman.branches import spec_branch
from foreman.git_host import CommentRef, GitHostProvider, IssueRef, PRRef
from foreman.instructions import load_project_instructions
from foreman.provider import ProviderFacade, UsageInfo
from foreman.providers import ProviderError, ProviderTransientError
from foreman.roles import (
    build_role_resources,
    emit_transient_provider_outcome,
    handle_unhandled_role_exception,
)
from foreman.roles._escalation_comment import post_escalation_comment
from foreman.roles._pr_lookup import find_open_pr_by_head_branch
from foreman.roles._prompt_helpers import (
    filter_bot_self_comments,
    format_comments_section,
    format_labels_section,
)
from foreman.schemas.planner import PlannerOutput, PlannerRunResult
from foreman.stats import log_planner_run
from foreman.v4.config import V4Config, resolve_operator
from foreman.v4.identity import V4IdentityRegistry
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
    """Load the Planner system prompt: vendored ``writing-plans`` followed by the Foreman-specific Planner contract.

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
# instead (which fires only after the Reviewer-on-impl approves). The
# regex + helper live in :mod:`foreman.auto_close` so the Worker (which
# scrubs commit bodies on the impl branch) can share the same pattern.
# The module-level alias above preserves the historical name for
# existing tests + call sites.


async def _run_planner_core(
    *,
    issue_url: str,
    config: V4Config,
    project_name: str,
    worktrees_root: Path,
    provider: ProviderFacade,
    identity_registry: V4IdentityRegistry,
) -> PlannerRunResult:
    """Run the Planner role end-to-end on one issue.

    Args:
        issue_url: Full GitHub issue URL (``https://github.com/owner/repo/issues/N``).
        config: Loaded foreman v4 config.
        project_name: Selects which ``V4Config.projects`` entry to use.
        worktrees_root: Root directory under which per-ticket worktrees live.
        provider: Agent provider facade (e.g., AnthropicSDKProvider).
        identity_registry: Pre-built v4 registry. Tests may inject a
            ``MagicMock`` exposing the production
            ``get_role_token(role)`` shape — see
            :func:`foreman.roles.build_role_resources` for the test
            seam.

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
        """Shared cleanup body for both ``except ProviderError`` and ``except Exception`` arms (foreman#266 — type-narrowing split).

        Closes over ``start_time`` / ``usage`` / ``pr_number`` /
        ``actual_repo_slug`` / ``issue_number`` / ``host`` /
        ``project_name`` from the enclosing scope. The bare
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
        # foreman#229: runaway-burn defense. Post the traceback as a
        # comment on the originating issue so the operator sees the
        # crash without spelunking daemon logs. Under v4 the
        # NeedsHelp-state transition (and its
        # ``foreman:state-needs-help`` label) is owned by the state
        # machine + :class:`LabelObservabilityObserver`; the role no
        # longer writes the v3-namespaced ``foreman:needs-help`` itself
        # (Phase 8d.7).
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
            )

    try:
        owner, repo_name, issue_number = parse_issue_url(issue_url)
        project = next((p for p in config.projects if p.name == project_name), None)
        if project is None:
            known = [p.name for p in config.projects]
            raise ValueError(
                f"project {project_name!r} not found in V4Config. Known projects: {known}"
            )
        expected_repo_slug = project.repo  # e.g. "jeffrichley/voice"
        actual_repo_slug = f"{owner}/{repo_name}"
        if expected_repo_slug != actual_repo_slug:
            raise ValueError(
                f"Issue URL repo {actual_repo_slug!r} does not match project "
                f"{project_name!r} configured repo {expected_repo_slug!r}"
            )

        host, planner_token, planner_client = build_role_resources(
            registry=identity_registry,
            role="planner",
            app_id=config.apps.planner.app_id,
            private_key_path=config.apps.planner.private_key_path,
        )

        issue = host.get_issue(actual_repo_slug, issue_number)
        default_branch = host.get_default_branch(actual_repo_slug)

        # foreman#328: pull in the originating issue's comments + labels so
        # the Planner sees operator-supplied post-filing context. The
        # raw comment stream goes through ``filter_bot_self_comments`` so
        # the foreman role bots' own previous postings don't feed back
        # into subsequent Planner runs (the exact failure mode that
        # motivated this ticket — see agent_core#180).
        raw_comments = host.get_issue_comments(actual_repo_slug, issue_number)
        comments = filter_bot_self_comments(raw_comments, identity_registry.get_role_bot_logins())

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

        # Crash-recovery resume arm (inert here): the dispatcher exports
        # FOREMAN_SESSION_ID + FOREMAN_RESUME_SESSION_ID when a state wants
        # to resume an interrupted Claude session. Both unset under normal
        # operation, so ``session_id`` is None and ``resume`` is False.
        _session_id = os.environ.get("FOREMAN_SESSION_ID")
        _resume_id = os.environ.get("FOREMAN_RESUME_SESSION_ID")
        _resume = bool(_resume_id) and _resume_id == _session_id
        llm_output, run_usage = await provider.run_agent(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            allowed_tools=PLANNER_ALLOWED_TOOLS,
            output_model=PlannerOutput,
            cwd=wt_path,
            session_id=_session_id,
            resume=_resume,
        )
        # foreman#235: hoist ``usage`` to the outer scope IMMEDIATELY
        # so a failure in any later step (commit/push/open_pull_request)
        # still records the per-call token cost in the spec_failed row.
        usage = run_usage

        branch = spec_branch(issue_number)
        # Issue #347: layer the operator-identity trailers on top of the
        # bot's authorial attribution. Resolver returns a fresh
        # OperatorConfig with both identities populated — the top-level
        # block is required at config load, so this never raises.
        op = resolve_operator(project, config)
        provenance_trailers = [
            f"Supervised-by: {op.supervisor.name} <{op.supervisor.email}>",
            f"Signed-off-by: {op.signer.name} <{op.signer.email}>",
        ]
        # Stage 1b (foreman crash-recovery): probe for an already-open
        # spec PR on ``foreman/issue-<N>`` BEFORE the commit/push/create
        # sequence. A daemon crash mid-Planning re-dispatches the Planner
        # subprocess; if we naively re-run commit + push +
        # ``open_pull_request``, GitHub returns 422 ("A pull request
        # already exists ...") and the subprocess crashes, transitioning
        # the ticket to ``Failed``. The fix mirrors the Worker's existing
        # impl-PR idempotency (issue #342): when the helper returns a
        # non-``None`` PullRequest, adopt it and skip commit/push/create
        # entirely — the prior (crashed) attempt already committed the
        # spec doc onto the branch and opened the PR. We keep that prior
        # attempt's spec doc rather than re-pushing the re-run's freshly
        # generated doc (simplest; consistent with the Worker; avoids
        # mutating a PR that may already be entering review).
        repo = planner_client.get_repo(actual_repo_slug)
        existing_spec_pr = find_open_pr_by_head_branch(repo, owner=owner, branch=branch)
        if existing_spec_pr is not None:
            pr = PRRef(
                number=existing_spec_pr.number,
                url=existing_spec_pr.html_url,
                title=existing_spec_pr.title,
                body=existing_spec_pr.body or "",
                branch=branch,
                base_branch=default_branch,
                repo_slug=actual_repo_slug,
            )
        else:
            host.commit_files_to_worktree(
                worktree_path=wt_path,
                files={_spec_doc_relpath(issue_number): llm_output.spec_doc_content},
                message=llm_output.pr_title,
                provenance_trailers=provenance_trailers,
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
        # The Planner does not mutate labels. Under v4,
        # ``LabelObservabilityObserver`` owns every ``foreman:*`` write off
        # state transitions; ``final_labels`` here is just the pre-call
        # snapshot returned for the daemon's audit trail.
        final_labels = sorted(set(issue.labels))

        duration_seconds = time.monotonic() - start_time

        # foreman#367: post the operator-visible escalation comment
        # BEFORE log_planner_run so a comment-post failure is visible
        # in the daemon log without preventing the JSONL row. The
        # helper catches host.post_issue_comment failures and returns
        # False; we do not branch on the return.
        if llm_output.confidence == "low":
            import os as _os

            state_instance_id = _os.environ.get(
                "FOREMAN_STATE_INSTANCE_ID",
                "unknown",
            )
            fallback_reason = None
            if llm_output.escalation_comment is None:
                fallback_reason = (
                    "planner LLM produced confidence=low but did not populate escalation_comment"
                )
            post_escalation_comment(
                host=host,
                repo_slug=actual_repo_slug,
                issue_number=issue_number,
                role="planner",
                outcome_label="low confidence",
                summary=llm_output.summary[:500] or "low confidence",
                payload=llm_output.escalation_comment,
                fallback_reason=fallback_reason,
                source="role:planner",
                key=f"state-instance-{state_instance_id}",
            )

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
        return PlannerRunResult(llm_output=llm_output, pr=pr, final_labels=final_labels)
    except ProviderError as exc:
        # foreman#361: transient failures are retried by the state
        # machine with backoff; suppress the runaway-burn issue
        # comment so a 40-min outage does not carpet the issue with
        # redundant tracebacks. The state machine's
        # ``RoleDispatchState`` Template Method recognizes the
        # transient-provider outcome and reschedules with backoff.
        if isinstance(exc, ProviderTransientError):
            raise
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


import asyncio  # noqa: E402
import os  # noqa: E402

from foreman.providers import make_provider  # noqa: E402
from foreman.v4.config import load_config as load_v4_config  # noqa: E402
from foreman.v4.emit import emit_outcome  # noqa: E402
from foreman.v4.outcome import (  # noqa: E402
    Outcome,
    OutcomeArtifacts,
    OutcomeConfidence,
    OutcomeKind,
)

_DEFAULT_V4_CONFIG = Path.home() / ".foreman" / "v4" / "config.toml"


class _V4PlannerResult:
    """Flat-shape result for the v4 emit path.

    The legacy ``PlannerRunResult`` nests ``pr.url`` / ``pr.number`` under
    a ``PRRef``; the v4 emit path consumes a flat shape (``pr_url`` /
    ``pr_number`` / ``summary`` / ``confidence``) so ``run_planner_cli``
    can stay agnostic to whatever helper produced it. Both this class and
    the helper that builds instances disappear in Phase 8.
    """

    def __init__(
        self,
        *,
        pr_url: str | None,
        pr_number: int | None,
        summary: str,
        confidence: str,
    ) -> None:
        self.pr_url = pr_url
        self.pr_number = pr_number
        self.summary = summary
        self.confidence = confidence


def _run_planner_for_v4(*, project: str, issue_number: int) -> _V4PlannerResult:
    """Run the planner core and adapt its result for the v4 CLI entry point.

    Calls :func:`_run_planner_core` (the worktree → LLM → commit → push →
    open-PR sequence formerly known as ``run_planner``) and unpacks the
    pieces ``run_planner_cli`` reports via ``FOREMAN_OUTCOME``. The v4
    state machine drives off that outcome — not off GitHub labels.

    The legacy entry-point takes ``issue_url``; v4's SubprocessRoleDispatcher
    passes ``--issue-number``. Synthesize the URL from the project's
    configured repo so the v4 CLI stays positional-arg-free without
    requiring upstream callers to know about GitHub URL shape.
    """
    cfg_path = Path(os.environ.get("FOREMAN_V4_CONFIG", _DEFAULT_V4_CONFIG))
    cfg = load_v4_config(cfg_path)
    project_cfg = next((p for p in cfg.projects if p.name == project), None)
    if project_cfg is None:
        known = [p.name for p in cfg.projects]
        raise ValueError(
            f"project {project!r} not found in V4Config at {cfg_path}. Known projects: {known}"
        )
    issue_url = f"https://github.com/{project_cfg.repo}/issues/{issue_number}"

    worktrees_root = Path(
        os.environ.get(
            "FOREMAN_WORKTREES_ROOT",
            str(Path.home() / ".foreman" / "worktrees"),
        )
    )
    provider = make_provider()
    registry = V4IdentityRegistry(
        apps=cfg.apps,
        orchestrator=cfg.orchestrator,
        installation_repo=project_cfg.repo,
    )
    core_result = asyncio.run(
        _run_planner_core(
            issue_url=issue_url,
            config=cfg,
            project_name=project,
            worktrees_root=worktrees_root,
            provider=provider,
            identity_registry=registry,
        )
    )
    return _V4PlannerResult(
        pr_url=core_result.pr.url,
        pr_number=core_result.pr.number,
        summary=core_result.llm_output.summary,
        confidence=core_result.llm_output.confidence,
    )


def run_planner_cli(*, project: str, issue_number: int) -> int:
    """v4 CLI entry-point. Emits FOREMAN_OUTCOME JSON; returns exit code.

    The SubprocessRoleDispatcher (Task 5.6) forks ``foreman plan-v4`` which
    calls this.
    """
    if os.environ.get("FOREMAN_DRY_RUN") == "1":
        # Short-circuit for the Task 8.6 real-fork integration test. Emits
        # a canned CLEAN outcome without any provider / GitHub / worktree
        # work. The chain being exercised here is typer → role entry →
        # emit_outcome → parser → exit code — the role's actual work is
        # out of scope for this test path.
        emit_outcome(
            Outcome(
                kind=OutcomeKind.CLEAN,
                confidence=OutcomeConfidence.HIGH,
                summary="dry-run",
            )
        )
        return 0
    try:
        result = _run_planner_for_v4(project=project, issue_number=issue_number)
    except ProviderTransientError as exc:
        # foreman#361: classify Anthropic-side transient failures so
        # the state machine's RoleDispatchState Template Method can
        # schedule an exponential-backoff retry without burning the
        # max_state_attempts cap. Shared helper for the body — see
        # :func:`foreman.roles.emit_transient_provider_outcome`.
        return emit_transient_provider_outcome(exc)
    except Exception as exc:
        emit_outcome(
            Outcome(
                kind=OutcomeKind.ERROR,
                confidence=OutcomeConfidence.HIGH,
                summary=f"planner raised: {exc}"[:500],
            )
        )
        return 1

    if getattr(result, "confidence", "high") == "low":
        emit_outcome(
            Outcome(
                kind=OutcomeKind.NEEDS_HELP,
                confidence=OutcomeConfidence.LOW,
                summary=getattr(result, "summary", None) or "ticket under-specified",
            )
        )
        return 0

    emit_outcome(
        Outcome(
            kind=OutcomeKind.CLEAN,
            confidence=OutcomeConfidence.HIGH,
            summary=getattr(result, "summary", None) or "spec PR opened",
            artifacts=OutcomeArtifacts(
                pr_url=getattr(result, "pr_url", None),
                pr_number=getattr(result, "pr_number", None),
            ),
        )
    )
    return 0
