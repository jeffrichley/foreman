"""Reviewer role dispatcher.

The Reviewer LLM reads an already-open spec or impl PR and returns a
:class:`~foreman.schemas.reviewer.ReviewerOutput`. Foreman core then:

  1. Posts the ``review_comment`` as a PR review (``event="COMMENT"``)
  2. Emits FOREMAN_OUTCOME so the v4 state machine can advance the ticket
  3. Returns :class:`~foreman.schemas.reviewer.ReviewerOutput` to the caller
     for display / persistence

The Reviewer derives the issue number and the review target (spec PR vs
impl PR) from the PR's head branch:
- ``foreman/issue-<N>`` → spec PR review
- ``foreman/impl-<N>``  → impl PR review

Under v4, ``LabelObservabilityObserver`` owns every ``foreman:*`` label
write off state-machine transitions; the Reviewer itself no longer reads
or writes labels. SQLite is the source of truth — labels are write-only
observability.

The Reviewer LLM is read-only on the filesystem (Read / Glob / Grep) plus
Bash for shell-level recon (e.g., ``gh pr view`` if it needs more context).
All host mutations (review post) happen in core via the PyGithub client.
This mirrors the Planner's "LLM is host-agnostic; core is deterministic"
split.
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

from github.Issue import Issue
from github.Repository import Repository

from foreman.git_host import CommentRef
from foreman.instructions import load_project_instructions
from foreman.provider import ProviderFacade, UsageInfo
from foreman.providers import ProviderError, ProviderTransientError
from foreman.roles import (
    build_role_resources,
    emit_transient_provider_outcome,
    handle_unhandled_role_exception,
    role_identity,
)
from foreman.roles._escalation_comment import post_escalation_comment
from foreman.roles._prompt_helpers import (
    filter_bot_self_comments,
    format_comments_section,
)
from foreman.schemas.reviewer import Finding, ReviewerOutput, ReviewerRunResult
from foreman.stats import log_reviewer_run
from foreman.v4.config import V4Config
from foreman.v4.identity import V4IdentityRegistry
from foreman.worktree import WorktreeManager

_LOG = logging.getLogger(__name__)

_PR_URL_RE = re.compile(
    r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)
_BRANCH_ISSUE_RE = re.compile(r"^foreman/issue-(?P<number>\d+)$")
_BRANCH_IMPL_RE = re.compile(r"^foreman/impl-(?P<number>\d+)$")


# Tool capabilities matrix for the Reviewer. Read-only on the filesystem;
# Bash is allowed for read-only recon (e.g., ``gh pr view``, ``git log``).
# Pinning this here prevents accidental ``Edit`` / ``Write`` reintroduction.
REVIEWER_ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Bash"]

# foreman#78: per-target prompt-composition routing for the Reviewer.
# The role accepts a ``target`` kwarg that distinguishes spec PRs
# (``foreman/issue-<N>``) from impl PRs (``foreman/impl-<N>``). Each
# target gets its own prompt composition. The mapping is intentionally
# explicit rather than computed — adding a new target later
# (``docs_pr``, ``release_pr``) requires updating the mapping
# deliberately, not silently falling back to spec behavior.
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
    comments: list[CommentRef],
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

    ``comments`` is the originating issue's comment stream (foreman#328),
    used only by the spec-side Reviewer. ``run_reviewer`` passes ``[]``
    for impl-side reviews to keep the impl prompt byte-identical to
    pre-#328 behavior. Empty list → no ``## Comments`` section.
    """
    instructions_section = (
        f"## Project-specific instructions\n\n{instructions}\n\n" if instructions else ""
    )
    comments_section = format_comments_section(comments)
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
        f"{comments_section}"
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
        "reviewer._get_pr_diff: base ref origin/%s missing for head %s; fell back to origin/%s",
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


async def _run_reviewer_core(
    *,
    pr_url: str,
    config: V4Config,
    project_name: str,
    worktrees_root: Path,
    provider: ProviderFacade,
    identity_registry: V4IdentityRegistry,
) -> ReviewerRunResult:
    """Run the Reviewer role end-to-end on one spec or impl PR.

    Args:
        pr_url: Full GitHub PR URL
            (``https://github.com/owner/repo/pull/N``).
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
        A :class:`~foreman.schemas.reviewer.ReviewerRunResult` bundling
        the LLM's :class:`~foreman.schemas.reviewer.ReviewerOutput` and
        the pre-call label snapshot for the daemon's audit trail
        (``final_labels``). Under v4 the Reviewer no longer mutates
        labels — ``LabelObservabilityObserver`` owns ``foreman:*``
        writes off state-machine transitions. The CLI surfaces
        ``outcome`` / ``findings`` / ``confidence`` for human inspection
        by reading ``result.llm_output``.

    Raises:
        ValueError: PR URL malformed, repo mismatch, or PR head branch is
            not a Foreman review branch (``foreman/issue-<N>`` or
            ``foreman/impl-<N>``).
    """
    # foreman#237: stamp ``start_time`` BEFORE the body wrap and
    # initialize ``usage`` to ``None`` so the except branch below can
    # log partial state regardless of where in the pipeline a failure
    # surfaces. ``WorktreeManager.attach`` / ``attach_impl``,
    # ``_get_pr_diff``, ``provider.run_agent``, ``pr.create_review`` are
    # all inside the wrap; pre-#237 any of those raising silently dropped
    # the run's cost telemetry because the success-path
    # ``log_reviewer_run`` call below never executed (same shape as the
    # Planner bug fixed in foreman#235 / PR #236).
    #
    # Post-adversarial-review: extend the wrap to ALSO cover URL parse,
    # project lookup, identity setup, and ``repo.get_pull`` /
    # ``repo.get_issue``. Any of those raising used to crash the role
    # subprocess; under v4 the runaway-burn defense fires the helper on
    # the FIRST such failure (assuming the issue was resolvable — see
    # the None guards in the except branch).
    start_time = time.monotonic()
    usage: UsageInfo | None = None
    actual_repo_slug: str | None = None
    pr_number: int | None = None
    issue_number: int | None = None
    target: Literal["spec_pr", "impl_pr"] = "spec_pr"  # Default until _parse_review_branch resolves
    issue: Issue | None = None

    def _on_failure(exc: BaseException) -> None:
        """Shared cleanup body for the ``ProviderError`` + ``Exception`` catch arms (foreman#266 — type-narrowing split).

        Closes over
        ``start_time`` / ``usage`` / ``pr_number`` /
        ``actual_repo_slug`` / ``issue_number`` / ``target`` /
        ``issue`` / ``project_name``. The bare ``raise`` that re-propagates
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
        # foreman#229: runaway-burn defense. Phase 8d.7 dropped the
        # role-side ``foreman:needs-help`` write — the v4 state machine
        # transitions to ``NeedsHelp`` when the role subprocess reports
        # failure, and :class:`LabelObservabilityObserver` writes
        # ``foreman:state-needs-help``. The role-side helper only posts
        # the diagnostic comment now.
        if issue is not None and issue_number is not None:
            bound_issue = issue
            handle_unhandled_role_exception(
                role="reviewer",
                issue_number=issue_number,
                exc=exc,
                post_comment=lambda body: bound_issue.create_comment(body),
            )

    try:
        owner, repo_name, pr_number = parse_pr_url(pr_url)
        project = next((p for p in config.projects if p.name == project_name), None)
        if project is None:
            known = [p.name for p in config.projects]
            raise ValueError(
                f"project {project_name!r} not found in V4Config. Known projects: {known}"
            )
        expected_repo_slug = project.repo
        actual_repo_slug = f"{owner}/{repo_name}"
        if expected_repo_slug != actual_repo_slug:
            raise ValueError(
                f"PR URL repo {actual_repo_slug!r} does not match project "
                f"{project_name!r} configured repo {expected_repo_slug!r}"
            )

        _host, reviewer_token, reviewer_client = build_role_resources(
            registry=identity_registry,
            role="reviewer",
            app_id=config.apps.reviewer.app_id,
        )

        repo: Repository = reviewer_client.get_repo(actual_repo_slug)
        pr = repo.get_pull(pr_number)

        head_branch = pr.head.ref
        head_sha = pr.head.sha
        base_branch = pr.base.ref
        issue_number, target = _parse_review_branch(head_branch)

        issue = repo.get_issue(issue_number)
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

        # foreman#328: comments belong only on the issue→spec contract.
        # Fetch them only for spec_pr; impl_pr passes ``[]`` so the
        # composed prompt stays byte-identical to pre-#328 behavior.
        # Branching at the FETCH site (not inside the composer) means
        # ``issue.get_comments()`` is never called on the impl path —
        # the regression test asserts on the call counter.
        comments: list[CommentRef]
        if target == "spec_pr":
            comments = [
                CommentRef(
                    author_login=c.user.login,
                    posted_at=c.created_at,
                    body=c.body or "",
                )
                for c in issue.get_comments()
            ]
            comments = filter_bot_self_comments(comments, identity_registry.get_role_bot_logins())
        else:
            comments = []

        system_prompt = _load_reviewer_prompt(target=target)
        user_prompt = _build_user_prompt(
            issue_title=issue_title,
            issue_body=issue_body,
            pr_title=pr.title or "",
            pr_body=pr.body or "",
            spec_doc_content=spec_doc_content,
            pr_diff=pr_diff,
            instructions=instructions,
            comments=comments,
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
            allowed_tools=REVIEWER_ALLOWED_TOOLS,
            output_model=ReviewerOutput,
            cwd=wt_path,
            env={**os.environ, "GH_TOKEN": reviewer_token},
            session_id=_session_id,
            resume=_resume,
        )
        # foreman#237: hoist ``usage`` to the outer scope IMMEDIATELY
        # so a failure in any later step (review post) still records the
        # per-call token cost in the review_failed row.
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

        # The Reviewer does not mutate labels. Under v4,
        # ``LabelObservabilityObserver`` owns every ``foreman:*`` write off
        # state transitions; ``final_labels`` here is just the pre-call
        # snapshot returned for the daemon's audit trail.
        final_labels = sorted({label.name for label in issue.labels})

        duration_seconds = time.monotonic() - start_time

        # foreman#367: post the operator-visible escalation comment
        # BEFORE log_reviewer_run so a comment-post failure is visible
        # in the daemon log without preventing the JSONL row.
        if llm_output.confidence == "low":
            _host_for_post, _, _ = build_role_resources(
                registry=identity_registry,
                role="reviewer",
                app_id=config.apps.reviewer.app_id,
            )
            state_instance_id = os.environ.get(
                "FOREMAN_STATE_INSTANCE_ID",
                "unknown",
            )
            fallback_reason = None
            if llm_output.escalation_comment is None:
                fallback_reason = (
                    "reviewer LLM produced confidence=low but did not populate escalation_comment"
                )
            post_escalation_comment(
                host=_host_for_post,
                repo_slug=actual_repo_slug,
                issue_number=issue_number,
                role="reviewer",
                outcome_label="low confidence",
                summary=llm_output.review_comment[:500] or "low confidence",
                payload=llm_output.escalation_comment,
                fallback_reason=fallback_reason,
                source=f"role:reviewer-{target}",
                key=(f"state-instance-{state_instance_id}-pr-{pr_number}-{llm_output.outcome}"),
            )

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
        # The Reviewer does not mutate labels under v4; ``final_labels``
        # is the pre-call snapshot returned for the daemon's audit
        # trail. ``LabelObservabilityObserver`` owns ``foreman:*`` writes
        # off state-machine transitions.
        return ReviewerRunResult(llm_output=llm_output, final_labels=final_labels)
    except ProviderError as exc:
        # foreman#361: transient failures are retried by the state
        # machine with backoff; suppress the runaway-burn issue
        # comment so a 40-min outage does not carpet the issue with
        # redundant tracebacks.
        if isinstance(exc, ProviderTransientError):
            raise
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


import asyncio  # noqa: E402

from foreman.providers import make_provider  # noqa: E402
from foreman.v4.config import load_config as load_v4_config  # noqa: E402
from foreman.v4.config import load_projects as load_v4_projects  # noqa: E402
from foreman.v4.emit import emit_outcome  # noqa: E402
from foreman.v4.outcome import Finding as V4Finding  # noqa: E402
from foreman.v4.outcome import (  # noqa: E402
    Outcome,
    OutcomeArtifacts,
    OutcomeConfidence,
    OutcomeKind,
)

_DEFAULT_V4_CONFIG = Path.home() / ".foreman" / "v4" / "config.toml"
_DEFAULT_PROJECTS_PATH = Path.home() / ".foreman" / "projects.toml"

# v4 RoleDispatcher uses "spec" / "impl"; the legacy Reviewer internals
# (branch parsing, label triples, prompt loader) speak "spec_pr" /
# "impl_pr". Translation lives only at the v4 boundary so legacy paths
# keep their existing vocabulary verbatim.
_V4_TARGET_TO_LEGACY: dict[str, Literal["spec_pr", "impl_pr"]] = {
    "spec": "spec_pr",
    "impl": "impl_pr",
}

# Head-branch shape per target — used to locate the open PR the v4
# Reviewer is about to review.
_V4_TARGET_TO_BRANCH_PREFIX: dict[str, str] = {
    "spec": "foreman/issue-",
    "impl": "foreman/impl-",
}


class _V4ReviewerResult:
    """Flat-shape result for the v4 emit path.

    The legacy ``ReviewerRunResult`` nests outcome under
    ``llm_output.outcome`` ("clean" / "needs_fix") with findings whose
    fields are ``severity`` / ``target`` / ``issue`` / ``needed``. The
    v4 emit path consumes the boolean ``approved`` + a flat
    ``findings`` list whose entries already carry v4-Finding fields
    (``severity`` / ``location`` / ``description``) so
    ``run_reviewer_cli`` can build :class:`OutcomeArtifacts` /
    :class:`Finding` without re-interpreting legacy shape. Both this
    class and the helper that builds instances disappear in Phase 8.
    """

    def __init__(
        self,
        *,
        approved: bool,
        pr_number: int | None,
        summary: str,
        findings: list[V4Finding],
        details: dict[str, object] | None = None,
    ) -> None:
        self.approved = approved
        self.pr_number = pr_number
        self.summary = summary
        self.findings = findings
        # Phase 8d.17 / foreman#315: ReviewerOutput diagnostic detail
        # forwarded to Outcome.details on emit. Populated by
        # ``_run_reviewer_for_v4`` from the LLM fields (pr_review_comment,
        # outcome, confidence) that would otherwise be dropped at the
        # v3→v4 flatten point.
        self.details: dict[str, object] = details if details is not None else {}


def _run_reviewer_for_v4(*, project: str, issue_number: int, target: str) -> _V4ReviewerResult:
    """Run the reviewer the same way the legacy ``review`` command does, but without label-writing on top.

    Calls :func:`_run_reviewer_core` (worktree → diff → LLM → review-post,
    formerly ``run_reviewer``) and unpacks the flat-shape result the v4
    state machine consumes via ``FOREMAN_OUTCOME``.

    ``target`` arrives in v4 vocabulary ("spec" / "impl"); the core infers
    spec vs impl from the PR's head branch on its own, so ``target`` is
    used only to locate the right open PR before handing off.
    """
    cfg_path = Path(os.environ.get("FOREMAN_V4_CONFIG", _DEFAULT_V4_CONFIG))
    cfg = load_v4_config(cfg_path)
    # issue #477: projects now live in the host-mounted projects file.
    # Only fall back to the projects file when cfg.projects is empty —
    # tests that mock load_v4_config to return a cfg with projects
    # already populated take the cfg.projects path.
    if cfg.projects:
        all_projects = cfg.projects
    else:
        projects_path = Path(os.environ.get("FOREMAN_PROJECTS_PATH", str(_DEFAULT_PROJECTS_PATH)))
        all_projects = load_v4_projects(projects_path) if projects_path.exists() else []
    project_cfg = next((p for p in all_projects if p.name == project), None)
    if project_cfg is None:
        known = [p.name for p in all_projects]
        raise ValueError(
            f"project {project!r} not found in V4Config at {cfg_path}. Known projects: {known}"
        )

    # Patch the config's projects list so _run_reviewer_core's lookup
    # (which reads config.projects) finds the project even when cfg was
    # loaded from a config.toml that has zero [[projects]] tables.
    if not cfg.projects:
        cfg = cfg.model_copy(update={"projects": all_projects})

    # Locate the open PR for this issue. The Reviewer's legacy entry-
    # point takes a PR URL — v4's SubprocessRoleDispatcher only knows
    # the issue number + target. Resolve via the reviewer App's client
    # (read-only get_pulls is in scope for the reviewer identity).
    registry = role_identity(cfg, installation_repo=project_cfg.repo)
    _host, _token, reviewer_client = build_role_resources(
        registry=registry,
        role="reviewer",
        app_id=cfg.apps.reviewer.app_id,
    )
    repo: Repository = reviewer_client.get_repo(project_cfg.repo)
    owner = project_cfg.repo.split("/", 1)[0]
    branch_prefix = _V4_TARGET_TO_BRANCH_PREFIX[target]
    branch = f"{branch_prefix}{issue_number}"
    head_qualifier = f"{owner}:{branch}"
    pulls = list(repo.get_pulls(state="open", head=head_qualifier))
    if not pulls:
        raise RuntimeError(
            f"No open PR found for branch {branch!r} in {project_cfg.repo!r}. "
            f"The v4 Reviewer expects the {target}-side PR to be open for "
            f"issue #{issue_number}."
        )
    pr = pulls[0]
    pr_url = pr.html_url

    worktrees_root = Path(
        os.environ.get(
            "FOREMAN_WORKTREES_ROOT",
            str(Path.home() / ".foreman" / "worktrees"),
        )
    )
    provider = make_provider()
    core_result = asyncio.run(
        _run_reviewer_core(
            pr_url=pr_url,
            config=cfg,
            project_name=project,
            worktrees_root=worktrees_root,
            provider=provider,
            identity_registry=registry,
        )
    )

    # Flatten legacy → v4 shape. The legacy ``Finding`` records
    # ``severity`` (kept verbatim — same severity literals as v4),
    # ``target`` + ``issue`` + ``needed``; v4's ``Finding`` wants
    # ``severity`` + ``location`` + ``description``. Map
    # ``target`` → ``location`` (both name "where in the artifact")
    # and join ``issue`` + ``needed`` into ``description`` so the
    # Fixer downstream still has both halves of the prose.
    llm = core_result.llm_output
    v4_findings: list[V4Finding] = [
        V4Finding(
            severity=f.severity,
            location=f.target or "general",
            description=f"{f.issue} — needed: {f.needed}",
        )
        for f in llm.findings
    ]
    # Phase 8d.17 / foreman#315: preserve ReviewerOutput diagnostic
    # detail by lifting onto Outcome.details. The reviewer's
    # full ``review_comment`` lives here (the Outcome.summary slot is
    # capped at 500 chars; reviewer prose often runs longer) along
    # with the raw outcome literal + confidence so operators can read
    # the full verdict without pulling the PR review.
    details: dict[str, object] = {
        "outcome": llm.outcome,
        "pr_review_comment": llm.review_comment,
        "confidence": llm.confidence,
    }
    return _V4ReviewerResult(
        approved=llm.outcome == "clean",
        pr_number=pr.number,
        summary=llm.review_comment[:500] if llm.review_comment else llm.outcome,
        findings=v4_findings,
        details=details,
    )


def run_reviewer_cli(*, project: str, issue_number: int, target: str) -> int:
    """v4 CLI entry-point. Emits FOREMAN_OUTCOME JSON; returns exit code.

    ``target`` is the v4 vocab ("spec" / "impl"). The
    SubprocessRoleDispatcher (Task 5.6) forks ``foreman review-v4
    --target spec|impl`` which calls this.
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
        result = _run_reviewer_for_v4(project=project, issue_number=issue_number, target=target)
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
                summary=f"reviewer raised: {exc}"[:500],
            )
        )
        return 1

    # Phase 8d.17 / foreman#315: forward Reviewer diagnostic detail
    # onto every emitted Outcome (CLEAN, NEEDS_FIX). The isinstance
    # check keeps the contract backward-compatible — older test
    # doubles (MagicMock) that don't set ``details`` explicitly
    # produce a non-dict attribute by default; we ignore it and emit
    # an empty bag rather than fail pydantic validation.
    raw_details = getattr(result, "details", None)
    details: dict[str, object] = raw_details if isinstance(raw_details, dict) else {}

    if getattr(result, "approved", False):
        emit_outcome(
            Outcome(
                kind=OutcomeKind.CLEAN,
                confidence=OutcomeConfidence.HIGH,
                summary=getattr(result, "summary", None) or "approved",
                artifacts=OutcomeArtifacts(pr_number=getattr(result, "pr_number", None)),
                details=details,
            )
        )
        return 0

    findings_raw = list(getattr(result, "findings", []) or [])
    # Findings already arrive in v4 shape from ``_run_reviewer_for_v4``
    # (or from a test double that builds them that way). Rebuild as
    # ``V4Finding`` instances so a MagicMock-based test stub still
    # round-trips through the Outcome model's validation.
    findings = [
        V4Finding(
            severity=f.severity,
            location=f.location,
            description=f.description,
        )
        for f in findings_raw
    ]
    emit_outcome(
        Outcome(
            kind=OutcomeKind.NEEDS_FIX,
            confidence=OutcomeConfidence.HIGH,
            summary=getattr(result, "summary", None) or f"{len(findings)} issues",
            artifacts=OutcomeArtifacts(pr_number=getattr(result, "pr_number", None)),
            findings=findings,
            details=details,
        )
    )
    return 0
