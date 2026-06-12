"""Reviewer role dispatcher.

The Reviewer LLM reads an already-open spec PR (the Planner's output) and
returns a :class:`~foreman.schemas.reviewer.ReviewerOutput`. Foreman core
then:

  1. Posts the ``review_comment`` as a PR review (``event="COMMENT"``)
  2. Advances the **issue's** label deterministically:
     - ``clean``     → ``foreman:planning`` → ``foreman:plan-approved``
     - ``needs_fix`` → ``foreman:planning`` → ``foreman:spec-fix``
  3. Returns :class:`~foreman.schemas.reviewer.ReviewerOutput` to the caller
     for display / persistence

The label transition is on the originating ISSUE, not the PR — same
pattern the Planner uses. The Reviewer derives the issue number and
the review target (spec PR vs impl PR) from the PR's head branch:
- ``foreman/issue-<N>`` → spec PR (label ``foreman:planning``)
- ``foreman/impl-<N>``  → impl PR (label ``foreman:impl-review``)

Pre-flight guard: if the source issue does not carry the
target-appropriate review label, the orchestrator raises before
doing any work — we will not silently advance a PR whose source
issue was not queued for review.

The Reviewer LLM is read-only on the filesystem (Read / Glob / Grep) plus
Bash for shell-level recon (e.g., ``gh pr view`` if it needs more context).
All host mutations (review post, label advance) happen in core via the
PyGithub client. This mirrors the Planner's "LLM is host-agnostic; core is
deterministic" split.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Literal

from github import Github
from github.Issue import Issue
from github.Repository import Repository

from foreman.config import Config
from foreman.dispatch_recorder import DispatchRecorder, emit_recorder_complete
from foreman.identity import IdentityRegistry
from foreman.instructions import load_project_instructions
from foreman.provider import ProviderFacade, UsageInfo
from foreman.providers import ProviderError
from foreman.roles import TERMINAL_BLOCKING_LABEL, handle_unhandled_role_exception
from foreman.schemas.reviewer import Finding, ReviewerOutput, ReviewerRunResult
from foreman.stats import log_reviewer_run
from foreman.worktree import WorktreeManager

_LOG = logging.getLogger(__name__)

_PR_URL_RE = re.compile(
    r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)
_BRANCH_ISSUE_RE = re.compile(r"^foreman/issue-(?P<number>\d+)$")
_BRANCH_IMPL_RE = re.compile(r"^foreman/impl-(?P<number>\d+)$")


class _ReviewerPreflightRefusal(RuntimeError):
    """Reviewer refused to proceed because the source issue lacks the
    expected entry label (``foreman:planning`` for spec PRs,
    ``foreman:impl-review`` for impl PRs).

    Subclass of :class:`RuntimeError` so existing callers' ``except
    RuntimeError`` clauses and ``pytest.raises(RuntimeError, match=...)``
    test patterns continue to work unchanged. The distinguishing
    purpose is to tell the runaway-burn defense (#229 helper invocation)
    to SKIP firing: a label-mismatch is operator intent (the label was
    removed deliberately or never set), not a "broken system" runaway
    signal. Firing the helper would override the operator's removal by
    re-adding ``foreman:needs-help``.
    """


# Tool capabilities matrix for the Reviewer. Read-only on the filesystem;
# Bash is allowed for read-only recon (e.g., ``gh pr view``, ``git log``).
# Pinning this here prevents accidental ``Edit`` / ``Write`` reintroduction.
REVIEWER_ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Bash"]

# Labels the Reviewer touches on the originating issue. The spec-PR labels
# and impl-PR labels form parallel triples; ``run_reviewer`` picks one
# triple based on the PR's head-branch shape.
_LABEL_SPEC_REVIEW = "foreman:planning"
_LABEL_SPEC_READY = "foreman:plan-approved"
_LABEL_SPEC_FIX = "foreman:spec-fix"
_LABEL_IMPL_REVIEW = "foreman:impl-review"
_LABEL_READY_FOR_MERGE = "foreman:impl-approved"
_LABEL_IMPL_FIX = "foreman:impl-fix"

# foreman#78: per-target routing for the Reviewer. The role accepts
# a ``target`` kwarg (added by foreman#41) that distinguishes spec
# PRs (``foreman/issue-<N>``) from impl PRs (``foreman/impl-<N>``).
# Each target gets its own entry-label precondition and its own
# prompt composition. The mappings are intentionally explicit
# rather than computed — adding a new target later (``docs_pr``,
# ``release_pr``) requires updating the mappings deliberately, not
# silently falling back to spec behavior.
_REVIEWER_ENTRY_LABEL_BY_TARGET: dict[str, str] = {
    "spec_pr": _LABEL_SPEC_REVIEW,
    "impl_pr": _LABEL_IMPL_REVIEW,
}

_REVIEWER_SUPERPOWERS_BY_TARGET: dict[str, list[str]] = {
    # Spec-side: today's discipline — empirical code review.
    "spec_pr": ["requesting-code-review"],
    # Impl-side: same code-review discipline PLUS what-counts-as-done
    # (verification-before-completion) and TDD-shape checking
    # (test-driven-development). The impl Reviewer judges whether the
    # Worker did right, so it needs the Worker's discipline patterns
    # to know what right looks like.
    "impl_pr": [
        "requesting-code-review",
        "verification-before-completion",
        "test-driven-development",
    ],
}

# Machine-parseable markers embedded in the posted review body so the Fixer
# can recover the Reviewer's structured findings from what GitHub stores.
# GitHub renders Markdown but preserves HTML comments verbatim in the source,
# so the markers are invisible to humans (when the review is rendered) yet
# deterministically locatable when the Fixer fetches the raw body. The
# ``<details>`` wrapper keeps the JSON visually folded for humans who DO
# expand the source. Both Reviewer (writer) and Fixer (reader) must agree on
# these strings — they are the wire format between the roles.
FINDINGS_BEGIN_MARKER = "<!-- foreman:findings:begin -->"
FINDINGS_END_MARKER = "<!-- foreman:findings:end -->"


def _build_findings_block(findings: list[Finding]) -> str:
    """Build the HTML-comment-fenced findings block appended to the review body.

    Always emits the markers + a JSON list (``[]`` when ``findings`` is empty)
    so the Fixer's extractor has a single, predictable shape to look for. The
    ``<details>`` fold keeps the prose readable on GitHub; the markers make
    parsing deterministic.
    """
    payload = [
        {
            "severity": f.severity,
            "target": f.target,
            "issue": f.issue,
            "needed": f.needed,
        }
        for f in findings
    ]
    json_text = json.dumps(payload, indent=2)
    return (
        f"{FINDINGS_BEGIN_MARKER}\n"
        "<details>\n"
        "<summary>Structured findings (for Fixer)</summary>\n\n"
        f"```json\n{json_text}\n```\n\n"
        "</details>\n"
        f"{FINDINGS_END_MARKER}"
    )


def parse_pr_url(url: str) -> tuple[str, str, int]:
    """Extract ``(owner, repo, pr_number)`` from a GitHub PR URL."""
    m = _PR_URL_RE.match(url.strip())
    if not m:
        raise ValueError(f"Not a GitHub PR URL: {url!r}")
    return m["owner"], m["repo"], int(m["number"])


def _parse_review_branch(branch: str) -> tuple[int, Literal["spec_pr", "impl_pr"]]:
    """Derive ``(issue_number, target)`` from a Reviewer-eligible head branch.

    Two valid shapes:
    - ``foreman/issue-<N>`` → spec PR review (``target="spec_pr"``)
    - ``foreman/impl-<N>``  → impl PR review (``target="impl_pr"``)

    Raises ``ValueError`` on any other shape — the Reviewer only acts
    on PRs produced by the Planner or the Worker, both of which use
    the conventions above.
    """
    m = _BRANCH_ISSUE_RE.match(branch)
    if m is not None:
        return int(m["number"]), "spec_pr"
    m = _BRANCH_IMPL_RE.match(branch)
    if m is not None:
        return int(m["number"]), "impl_pr"
    raise ValueError(
        f"PR head branch {branch!r} is not a Foreman review branch "
        "(expected 'foreman/issue-<N>' for spec PRs or "
        "'foreman/impl-<N>' for impl PRs)."
    )


def _load_reviewer_prompt(target: Literal["spec_pr", "impl_pr"] = "spec_pr") -> str:
    """Load the Reviewer system prompt for the given ``target``.

    ``target="spec_pr"`` (default for back-compat with existing call
    sites) loads ``reviewer.md`` composed with
    ``requesting-code-review``. ``target="impl_pr"`` loads
    ``reviewer_impl.md`` composed with the impl-side superpowers list
    (adds ``verification-before-completion`` + ``test-driven-development``).
    See ``_REVIEWER_SUPERPOWERS_BY_TARGET`` for the exact composition.

    Unknown target values fall back to the spec composition — the
    role's precondition gate is the right place to surface
    wrong-target errors, not this loader.

    Composed via :func:`foreman.prompts.compose_role_prompt` so the
    adapter preamble + per-skill wrappers from PR #43 are applied
    consistently with the other roles.
    """
    from foreman.prompts import compose_role_prompt

    superpowers = _REVIEWER_SUPERPOWERS_BY_TARGET.get(
        target, _REVIEWER_SUPERPOWERS_BY_TARGET["spec_pr"]
    )
    # Map unknown target to "spec_pr" for the prompt loader call too,
    # so the file resolution stays consistent with the superpowers list.
    safe_target = target if target in _REVIEWER_SUPERPOWERS_BY_TARGET else "spec_pr"
    return compose_role_prompt(
        role="reviewer",
        superpowers=superpowers,
        target=safe_target,
    )


def _build_user_prompt(
    *,
    issue_title: str,
    issue_body: str,
    pr_title: str,
    pr_body: str,
    spec_doc_content: str | None,
    pr_diff: str,
    instructions: str | None,
) -> str:
    """Compose the per-run user prompt.

    The Reviewer needs the issue (ground truth) and the spec PR's artifact
    (the spec doc + PR body) plus the actual diff so it can verify file-level
    claims. The spec doc may be embedded directly when available; otherwise
    the Reviewer reads it from the worktree via its Read tool.

    ``instructions`` is the verbatim contents of the project's
    ``.foreman/INSTRUCTIONS.md`` (or ``None`` when absent). When present
    the section is emitted near the top so project-specific conventions
    (PR title rules, branch conventions, etc.) frame the review. When
    ``None`` the section is omitted entirely.
    """
    instructions_section = (
        f"## Project-specific instructions\n\n{instructions}\n\n" if instructions else ""
    )
    spec_section = (
        f"## Spec doc (committed in this PR)\n{spec_doc_content}\n\n"
        if spec_doc_content
        else (
            "## Spec doc\nNot inlined — read it from the worktree at the "
            "path the PR body references.\n\n"
        )
    )
    return (
        "You are reviewing an open spec PR produced by the Planner.\n\n"
        f"{instructions_section}"
        f"## Originating issue\nTitle: {issue_title}\n\n{issue_body}\n\n"
        f"## PR title\n{pr_title}\n\n"
        f"## PR body\n{pr_body}\n\n"
        f"{spec_section}"
        f"## PR diff (head vs base)\n```\n{pr_diff}\n```\n\n"
        "Follow the steps in your system prompt. Return your structured "
        "output when done."
    )


def _get_pr_diff(worktree_path: Path, base_branch: str, head_sha: str, *, role_token: str) -> str:
    """Return the unified diff for the PR's head against its base branch.

    Uses ``git diff`` in the worktree rather than the GitHub Files API so
    we don't pay round-trips for large PRs and so the diff matches whatever
    the worktree has checked out (which the LLM will read from with Read /
    Grep / Glob).

    ``role_token`` is the reviewer bot's installation token. We inject it
    into ``GH_TOKEN`` for both git invocations so any credential-helper
    (e.g., ``gh auth git-credential``) configured in the worktree
    authenticates as the reviewer bot rather than inheriting whatever
    ``GH_TOKEN`` the daemon's parent process had set (CI runner or dev
    shell). Without this, identity attribution leaks (HIGH #10).

    foreman#294: if ``origin/<base_branch>`` does not exist on origin
    (auto-delete-on-merge, operator pruned, future cleanup feature),
    the diff falls back to ``origin/<default-branch>...<head_sha>``.
    The fetch step routes through
    :func:`foreman.worktree.fetch_origin_branch` so the existing
    foreman#122 prune-stale-ref self-heal fires at fetch time. A
    WARNING log identifies the recovery so an operator running
    ``docker compose logs daemon`` can pin on the message prefix.
    """
    from foreman._env_filter import filtered_subprocess_env
    from foreman.worktree import fetch_origin_branch, resolve_default_branch

    role_env = filtered_subprocess_env(role_token=role_token)

    # Refresh the base ref. Routes through the shared self-heal: if the
    # base branch was deleted on origin, the local stale ref is pruned
    # here (foreman#122).
    fetch_origin_branch(worktree_path, base_branch, role_token=role_token)

    result = subprocess.run(
        ["git", "diff", f"origin/{base_branch}...{head_sha}"],
        cwd=worktree_path,
        check=False,
        capture_output=True,
        text=True,
        env=role_env,
    )
    if result.returncode == 0:
        return result.stdout

    stderr_lower = (result.stderr or "").lower()
    missing_ref = (
        "bad revision" in stderr_lower
        or "unknown revision" in stderr_lower
        or "ambiguous argument" in stderr_lower
    )
    if not missing_ref:
        # Other failure (e.g., real corruption). Surface the original
        # error to the existing _on_failure path.
        result.check_returncode()  # raises CalledProcessError

    default_branch = resolve_default_branch(worktree_path, role_token=role_token)
    fetch_origin_branch(worktree_path, default_branch, role_token=role_token)
    _LOG.warning(
        "reviewer._get_pr_diff: base ref origin/%s missing for head %s; "
        "fell back to origin/%s",
        base_branch,
        head_sha,
        default_branch,
    )
    fallback = subprocess.run(
        ["git", "diff", f"origin/{default_branch}...{head_sha}"],
        cwd=worktree_path,
        check=True,
        capture_output=True,
        text=True,
        env=role_env,
    )
    return fallback.stdout


def _read_spec_doc(worktree_path: Path, issue_number: int) -> str | None:
    """Best-effort read of the Planner's spec doc from the worktree.

    The Planner commits at a deterministic path
    (``docs/superpowers/specs/foreman-issue-<N>-spec.md``). Reading it
    eagerly here lets us inline it into the user prompt instead of forcing
    the LLM to ``Read`` it as a tool call. Returns ``None`` if the file is
    missing — the LLM can still find it via its tools.
    """
    spec_path = (
        worktree_path / "docs" / "superpowers" / "specs" / f"foreman-issue-{issue_number}-spec.md"
    )
    if not spec_path.exists():
        return None
    try:
        return spec_path.read_text(encoding="utf-8")
    except OSError:
        return None


async def run_reviewer(
    *,
    pr_url: str,
    config: Config,
    project_name: str,
    worktrees_root: Path,
    provider: ProviderFacade,
    identity_registry: IdentityRegistry | None = None,
    dispatch_recorder: DispatchRecorder | None = None,
    dispatch_trace_id: int | None = None,
) -> ReviewerRunResult:
    """Run the Reviewer role end-to-end on one spec PR.

    Args:
        pr_url: Full GitHub PR URL
            (``https://github.com/owner/repo/pull/N``).
        config: Loaded foreman config.
        project_name: Key into ``config.projects``.
        worktrees_root: Root directory under which per-ticket worktrees live.
        provider: Agent provider facade (e.g., AnthropicSDKProvider).
        identity_registry: Optional pre-built registry; defaults to a fresh
            :class:`~foreman.identity.IdentityRegistry` for the project.
            Tests inject a fake registry to bypass real App auth.

    Returns:
        A :class:`~foreman.schemas.reviewer.ReviewerRunResult` bundling
        the LLM's :class:`~foreman.schemas.reviewer.ReviewerOutput` and
        the deterministic post-transition label set
        (``final_labels``). The CLI surfaces ``outcome`` / ``findings``
        / ``confidence`` for human inspection by reading
        ``result.llm_output``.

    Raises:
        ValueError: PR URL malformed, repo mismatch, or PR head branch is
            not a Foreman review branch (``foreman/issue-<N>`` or
            ``foreman/impl-<N>``).
        RuntimeError: Source issue is missing the target-appropriate
            review label (``foreman:planning`` for spec PRs,
            ``foreman:impl-review`` for impl PRs) — we refuse to advance
            PRs whose source issue was not queued for review.
    """
    # foreman#237: stamp ``start_time`` BEFORE the body wrap and
    # initialize ``usage`` to ``None`` so the except branch below can
    # log partial state regardless of where in the pipeline a failure
    # surfaces. ``WorktreeManager.attach`` / ``attach_impl``,
    # ``_get_pr_diff``, ``provider.run_agent``, ``pr.create_review``,
    # ``issue.update`` / ``set_labels`` are all inside the wrap; pre-#237
    # any of those raising silently dropped the run's cost telemetry
    # because the success-path ``log_reviewer_run`` call below never
    # executed (same shape as the Planner bug fixed in foreman#235 /
    # PR #236).
    #
    # Post-adversarial-review: extend the wrap to ALSO cover URL parse,
    # project lookup, identity setup, ``repo.get_pull`` /
    # ``repo.get_issue``, and the in-review-label refusal check. Any of
    # those raising used to crash the role subprocess WITHOUT
    # transitioning the in-flight label; the dispatcher then
    # re-dispatched until #228's rate-limit caught the loop at N=3.
    # Now the FIRST such failure fires the helper (assuming the issue
    # was resolvable — see the None guards in the except branch).
    start_time = time.monotonic()
    usage: UsageInfo | None = None
    actual_repo_slug: str | None = None
    pr_number: int | None = None
    issue_number: int | None = None
    target: Literal["spec_pr", "impl_pr"] = "spec_pr"  # Default until _parse_review_branch resolves
    issue: Issue | None = None

    def _on_failure(exc: BaseException) -> None:
        """Shared cleanup body for the ``ProviderError`` + ``Exception``
        catch arms (foreman#266 — type-narrowing split). Closes over
        ``start_time`` / ``usage`` / ``pr_number`` /
        ``actual_repo_slug`` / ``issue_number`` / ``target`` /
        ``issue`` / ``project_name`` / ``dispatch_recorder`` /
        ``dispatch_trace_id``. The bare ``raise`` that re-propagates
        the original exception lives in each ``except`` arm after
        calling this helper.
        """
        # foreman#237 + foreman#229: capture cost telemetry AND defend
        # against the runaway-burn pattern.
        duration_seconds = time.monotonic() - start_time
        if actual_repo_slug is not None and issue_number is not None:
            try:
                log_reviewer_run(
                    repo_slug=actual_repo_slug,
                    issue_number=issue_number,
                    pr_number=pr_number,
                    target=target,
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
                # Best-effort telemetry — swallow so the daemon
                # dispatcher sees the ORIGINAL exception, not whatever
                # the stats writer raised.
                pass
            # foreman#251 (Phase 1): mirror the failure-path dual-write.
            emit_recorder_complete(
                dispatch_recorder=dispatch_recorder,
                dispatch_trace_id=dispatch_trace_id,
                role="reviewer",
                repo_slug=actual_repo_slug,
                ticket_id=f"{actual_repo_slug}#{issue_number}",
                project=project_name,
                issue_number=issue_number,
                pr_number=pr_number,
                outcome="exception",
                usage=usage if usage is not None else UsageInfo(),
                role_data={"target": target},
                duration_seconds=duration_seconds,
            )
        # foreman#229: runaway-burn defense. Skip for
        # ``_ReviewerPreflightRefusal`` — that's intentional.
        if (
            not isinstance(exc, _ReviewerPreflightRefusal)
            and issue is not None
            and issue_number is not None
        ):
            bound_issue = issue
            handle_unhandled_role_exception(
                role="reviewer",
                issue_number=issue_number,
                exc=exc,
                post_comment=lambda body: bound_issue.create_comment(body),
                set_needs_help_label=lambda: bound_issue.add_to_labels(TERMINAL_BLOCKING_LABEL),
            )

    try:
        owner, repo_name, pr_number = parse_pr_url(pr_url)
        project = config.projects[project_name]
        expected_repo_slug = project.repo
        actual_repo_slug = f"{owner}/{repo_name}"
        if expected_repo_slug != actual_repo_slug:
            raise ValueError(
                f"PR URL repo {actual_repo_slug!r} does not match project "
                f"{project_name!r} configured repo {expected_repo_slug!r}"
            )

        registry = identity_registry if identity_registry is not None else IdentityRegistry(project)
        reviewer_client: Github = registry.get_reviewer_client()
        reviewer_token: str = registry.get_reviewer_token()

        repo: Repository = reviewer_client.get_repo(actual_repo_slug)
        pr = repo.get_pull(pr_number)

        head_branch = pr.head.ref
        head_sha = pr.head.sha
        base_branch = pr.base.ref
        issue_number, target = _parse_review_branch(head_branch)

        in_review_label = _REVIEWER_ENTRY_LABEL_BY_TARGET[target]
        if target == "impl_pr":
            clean_label = _LABEL_READY_FOR_MERGE
            fix_label = _LABEL_IMPL_FIX
        else:
            clean_label = _LABEL_SPEC_READY
            fix_label = _LABEL_SPEC_FIX

        issue = repo.get_issue(issue_number)
        issue_labels = {label.name for label in issue.labels}
        if in_review_label not in issue_labels:
            # Graceful refusal, NOT a runaway-burn signal. The operator
            # may have removed the label deliberately (to abort an
            # in-flight review, or because the ticket was retargeted).
            # Use the marker subclass so the except branch below skips
            # the helper and lets the original intent survive.
            raise _ReviewerPreflightRefusal(
                f"Issue #{issue_number} (source of PR #{pr_number}) does not carry "
                f"the {in_review_label!r} label (labels: "
                + ", ".join(sorted(issue_labels) or ["<none>"])
                + "). The Reviewer only acts on issues queued via the Planner."
            )

        issue_title = issue.title or ""
        issue_body = issue.body or ""

        # WorktreeManager's git subprocesses (fetch / worktree add) must
        # authenticate as the reviewer bot — without the explicit token they
        # inherit the daemon's parent ``GH_TOKEN`` (CI runner, dev shell)
        # and attribute identity to the daemon's identity on private repos.
        # Same anti-leak motivation as :func:`_get_pr_diff` above; the
        # ``role_token`` parameter is plumbed all the way down through the
        # module-level git helpers.
        wt_mgr = WorktreeManager(worktrees_root=worktrees_root, role_token=reviewer_token)
        if target == "impl_pr":
            wt_path = wt_mgr.attach_impl(
                clone_path=Path(project.local_clone_path),
                repo_slug=repo_name,
                ticket_id=issue_number,
                repo_url=f"https://github.com/{project.repo}.git",
            )
        else:
            wt_path = wt_mgr.attach(
                clone_path=Path(project.local_clone_path),
                repo_slug=repo_name,
                ticket_id=issue_number,
                repo_url=f"https://github.com/{project.repo}.git",
            )

        pr_diff = _get_pr_diff(
            wt_path, base_branch=base_branch, head_sha=head_sha, role_token=reviewer_token
        )
        spec_doc_content = _read_spec_doc(wt_path, issue_number)
        instructions = load_project_instructions(Path(project.local_clone_path))

        system_prompt = _load_reviewer_prompt(target=target)
        user_prompt = _build_user_prompt(
            issue_title=issue_title,
            issue_body=issue_body,
            pr_title=pr.title or "",
            pr_body=pr.body or "",
            spec_doc_content=spec_doc_content,
            pr_diff=pr_diff,
            instructions=instructions,
        )

        llm_output, run_usage = await provider.run_agent(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            allowed_tools=REVIEWER_ALLOWED_TOOLS,
            output_model=ReviewerOutput,
            cwd=wt_path,
            env={**os.environ, "GH_TOKEN": reviewer_token},
        )
        # foreman#237: hoist ``usage`` to the outer scope IMMEDIATELY
        # so a failure in any later step (review post, label
        # transition) still records the per-call token cost in the
        # review_failed row.
        usage = run_usage

        # Post the review comment as the reviewer bot. ``event="COMMENT"``
        # (not ``"APPROVE"``) — the bot doesn't have write access on the head
        # branch and approval is a human decision in v1 anyway.
        #
        # We append a marker-fenced JSON block carrying the structured findings
        # to the review body so the Fixer can recover them by parsing what
        # GitHub actually stored. Without this, the Fixer's "every edit must
        # trace to a structured finding" rule produces 0 actions: the structured
        # ``findings`` list lives only in this in-memory ``ReviewerOutput``, and
        # by the time the Fixer runs (in a separate process / later) all it has
        # is the posted review prose. ``llm_output`` is treated as immutable —
        # we build a fresh ``enriched_body`` string instead of mutating it.
        findings_block = _build_findings_block(llm_output.findings)
        enriched_body = f"{llm_output.review_comment}\n\n{findings_block}"
        pr.create_review(body=enriched_body, event="COMMENT")

        # Advance the originating ISSUE's label (not the PR's) — same pattern
        # the Planner uses.
        if llm_output.outcome == "clean":
            add_label = clean_label
        else:
            add_label = fix_label

        # Atomic label transition (foreman#? — adversarial review MEDIUM #12):
        # use ``issue.set_labels(...)`` (single PUT /issues/{N}/labels) instead
        # of sequential ``remove_from_labels`` + ``add_to_labels``. A subprocess
        # crash between the two PyGithub calls leaves the issue with neither
        # the entry label nor the outcome label — it then falls out of the v3
        # observer's GraphQL ``filterBy.labels`` filter and the reconciler
        # never sees it again (silent stall). ``set_labels`` replaces the full
        # label set in one API call, so the transition is either fully applied
        # or not applied at all.
        #
        # Namespace-scoped merge (Pass 2 HIGH): ``set_labels`` REPLACES the
        # full label set on GitHub, so we must preserve every label the role
        # is not actively touching. Re-read labels NOW (not from the pre-LLM
        # snapshot) to minimize the race window for operator-added labels —
        # the LLM call took minutes, but the re-read → API call window is
        # only the round-trip (~hundreds of ms). The role declares the
        # foreman labels it is removing (``removed_foreman``) and adding
        # (``added_foreman``); everything else — non-foreman labels like
        # ``priority:high`` AND foreman labels the role isn't touching like
        # ``foreman:hold`` — passes through.
        #
        # Pass 3 CRITICAL: PyGithub's ``Issue.labels`` is a cached property
        # — ``_completeIfNotSet(self._labels)`` only fetches on FIRST access
        # (see ``.venv/Lib/site-packages/github/Issue.py:266`` +
        # ``GithubObject.py:618``). Subsequent reads return the same snapshot
        # taken at the top of ``run_reviewer`` — operator-added labels during
        # the LLM call would be silently dropped. ``issue.update()`` issues a
        # conditional GET and re-stores the attributes (see
        # ``GithubObject.py:638``), invalidating the cache so the next
        # ``issue.labels`` access reflects the real remote state.
        removed_foreman = {in_review_label}
        added_foreman = {add_label}
        issue.update()
        current_label_names = {label.name for label in issue.labels}
        final_labels = sorted((current_label_names - removed_foreman) | added_foreman)
        issue.set_labels(*final_labels)

        duration_seconds = time.monotonic() - start_time

        # foreman#227: append the per-call token usage + cost to the
        # Reviewer JSONL stats file. New file under
        # ``~/.foreman/stats/<owner>__<repo>/reviewer.jsonl`` — previously
        # the Reviewer had no audit log; the envelope captures target
        # (spec_pr vs impl_pr) + outcome (clean vs needs_fix) + the new
        # usage fields.
        # foreman#237: the ``review_failed`` mirror lands in the
        # ``except`` branch below; reaching this line means the LLM
        # returned and the host transition succeeded — by definition
        # the outcome is the LLM's own ``clean`` / ``needs_fix``.
        log_reviewer_run(
            repo_slug=actual_repo_slug,
            issue_number=issue_number,
            pr_number=pr_number,
            target=target,
            outcome=llm_output.outcome,
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
        # foreman#251 (Phase 1): dual-write through the Recorder.
        # ``target`` lands in ``role_data`` because it's reviewer-
        # specific JSONL — :class:`RoleStatsSubscriber` reads it back
        # when fanning out to ``log_reviewer_run``.
        emit_recorder_complete(
            dispatch_recorder=dispatch_recorder,
            dispatch_trace_id=dispatch_trace_id,
            role="reviewer",
            repo_slug=actual_repo_slug,
            ticket_id=f"{actual_repo_slug}#{issue_number}",
            project=project_name,
            issue_number=issue_number,
            pr_number=pr_number,
            outcome=llm_output.outcome,
            usage=usage,
            role_data={"target": target},
            duration_seconds=duration_seconds,
        )

        # foreman#91: ``final_labels`` is the authoritative post-transition
        # set, computed in-process from the pre-mutation snapshot + the role's
        # known transitions — not via a post-mutation host re-read (which
        # raced its own write and produced stale-snapshot dispatches at the
        # next worker iteration).
        return ReviewerRunResult(llm_output=llm_output, final_labels=final_labels)
    except ProviderError as exc:
        # foreman#266: typed catch for the documented provider-boundary
        # failure mode. Same body as the ``except Exception`` arm
        # below — the change is structural (type narrowing + boundary
        # documentation), not semantic.
        _on_failure(exc)
        raise
    except Exception as exc:
        # PR #255 commit 2 defensive handler — belt-and-suspenders for
        # non-provider failures (worktree ops, host I/O, GitHub 5xx).
        _on_failure(exc)
        raise
