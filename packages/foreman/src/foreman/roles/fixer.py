"""Fixer role dispatcher.

The Fixer LLM consumes the Reviewer's findings on a spec or impl PR,
applies addressable edits, commits + pushes, and returns a
:class:`~foreman.schemas.fixer.FixerOutput`. Foreman core then:

  1. Posts ``fix_comment`` as a PR **comment** (not a review) via
     :meth:`PullRequest.create_issue_comment` — the Fixer is not
     re-reviewing.
  2. Emits FOREMAN_OUTCOME so the v4 state machine can advance the
     ticket.
  3. Appends a JSONL line to ``~/.foreman/stats/<repo>/fixer.jsonl``
     for lifecycle stats (proto for foreman#11).
  4. Returns :class:`~foreman.schemas.fixer.FixerOutput` to the caller.

Under v4, ``LabelObservabilityObserver`` owns every ``foreman:*`` label
write off state-machine transitions; the Fixer itself no longer reads
or writes labels (the v4 state machine's retry cap, foreman#8c.2, owns
attempt counting now). SQLite is the source of truth — labels are
write-only observability.

The Fixer's tool surface includes Edit + Write (it edits the spec doc
or impl code) plus Read / Grep / Glob / Bash. Bash is needed so the LLM
can ``git add`` + ``git commit`` + ``git push`` directly — Foreman
core's host abstraction is read+post-only here; the commit machinery
lives in the LLM's hands inside the worktree, because the LLM is the one
deciding which edits to bundle into which commit per the prompt's
discipline.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Literal, cast

from github.Issue import Issue
from github.PullRequest import PullRequest
from github.Repository import Repository
from pydantic import ValidationError

from foreman.branches import spec_branch
from foreman.dispatch_recorder import DispatchRecorder, emit_recorder_complete
from foreman.instructions import load_project_instructions
from foreman.provider import ProviderFacade, UsageInfo
from foreman.providers import ProviderError
from foreman.roles import (
    TERMINAL_BLOCKING_LABEL,
    build_role_resources,
    handle_unhandled_role_exception,
)
from foreman.roles.reviewer import FINDINGS_BEGIN_MARKER, FINDINGS_END_MARKER
from foreman.schemas.fixer import FixerOutput, FixerRunResult
from foreman.schemas.reviewer import Finding
from foreman.stats import log_fixer_run
from foreman.v4.config import V4Config
from foreman.v4.identity import V4IdentityRegistry
from foreman.worktree import WorktreeManager

_log = logging.getLogger(__name__)

# Match the marker-fenced JSON block embedded by the Reviewer
# (see ``foreman.roles.reviewer._build_findings_block``). The Reviewer wraps
# the JSON in a ``<details>`` fold for readability, so the markers may have
# arbitrary HTML between them and the fenced block — we anchor on the fence
# itself rather than expecting a fixed layout.
_FINDINGS_BLOCK_RE = re.compile(
    re.escape(FINDINGS_BEGIN_MARKER)
    + r".*?```json\s*\n(?P<json>.*?)\n```.*?"
    + re.escape(FINDINGS_END_MARKER),
    flags=re.DOTALL,
)

_ISSUE_URL_RE = re.compile(
    r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)"
)

# Tool capabilities matrix for the Fixer. Edit + Write so it can modify
# the spec doc; Bash so it can stage / commit / push from inside the
# worktree. Read / Grep / Glob for verification before committing. The
# matrix is wider than Reviewer's by design — Fixer is the only role in
# the walking skeleton that mutates the worktree directly.
FIXER_ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Bash", "Edit", "Write"]

# foreman#79: per-target prompt-composition routing for the Fixer. The
# role accepts a ``target`` kwarg (added by foreman#41 via DaemonRunners)
# that distinguishes spec-PR fixes from impl-PR fixes. Each target gets
# its own prompt composition.
_FIXER_SUPERPOWERS_BY_TARGET: dict[str, list[str]] = {
    # Spec-side: today's discipline — receiving review feedback.
    "spec_pr": ["receiving-code-review"],
    # Impl-side: same feedback-reception discipline PLUS the Worker's
    # what-counts-as-done (verification-before-completion) and TDD
    # discipline (test-driven-development). Impl fixes change code, so
    # the test-first + verify-before-commit patterns apply.
    "impl_pr": [
        "receiving-code-review",
        "verification-before-completion",
        "test-driven-development",
    ],
}


def parse_issue_url(url: str) -> tuple[str, str, int]:
    """Extract ``(owner, repo, issue_number)`` from a GitHub issue URL.

    The Fixer is triggered by a label on the ISSUE, not the PR — the
    spec PR is derived from the issue's ``foreman/issue-<N>`` branch.
    """
    m = _ISSUE_URL_RE.match(url.strip())
    if not m:
        raise ValueError(f"Not a GitHub issue URL: {url!r}")
    return m["owner"], m["repo"], int(m["number"])


def _load_fixer_prompt(target: Literal["spec_pr", "impl_pr"] = "spec_pr") -> str:
    """Load the Fixer system prompt for the given ``target``.

    ``target="spec_pr"`` (default for back-compat) loads ``fixer.md``
    composed with ``receiving-code-review``. ``target="impl_pr"``
    loads ``fixer_impl.md`` composed with the impl-side superpowers
    list (adds ``verification-before-completion`` +
    ``test-driven-development``). See ``_FIXER_SUPERPOWERS_BY_TARGET``
    for the exact composition.

    Unknown target values fall back to the spec composition — the
    role's precondition gate is the right place to surface
    wrong-target errors, not this loader.
    """
    from foreman.prompts import compose_role_prompt

    superpowers = _FIXER_SUPERPOWERS_BY_TARGET.get(target, _FIXER_SUPERPOWERS_BY_TARGET["spec_pr"])
    safe_target = target if target in _FIXER_SUPERPOWERS_BY_TARGET else "spec_pr"
    return compose_role_prompt(
        role="fixer",
        superpowers=superpowers,
        target=safe_target,
    )


def _extract_findings_from_review_comment(body: str) -> list[Finding]:
    """Recover the Reviewer's structured findings from a posted review body.

    The Reviewer embeds the findings as a marker-fenced JSON block
    (see :func:`foreman.roles.reviewer._build_findings_block`). This is the
    only path the structured list takes across the role boundary in v1 —
    the in-memory ``ReviewerOutput.findings`` list is process-local and
    does not survive into the Fixer's run.

    Returns ``[]`` on:
      - missing markers (review body predates this feature, or block
        stripped by some intermediary)
      - missing fenced JSON inside markers (malformed block)
      - JSON that fails to parse
      - JSON entries that fail :class:`Finding` validation

    A warning is logged in each malformed case so operators can diagnose,
    but we never raise — the Fixer falls back to its existing
    "no structured findings" behavior. Empty list is a valid wire shape
    (Reviewer always emits the block, even on clean outcomes).
    """
    if not body:
        return []
    m = _FINDINGS_BLOCK_RE.search(body)
    if m is None:
        return []
    raw = m.group("json").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log.warning("Failed to parse Reviewer findings JSON block: %s; raw=%r", exc, raw)
        return []
    if not isinstance(payload, list):
        _log.warning("Reviewer findings JSON block was not a list: type=%s", type(payload).__name__)
        return []
    findings: list[Finding] = []
    for entry in payload:
        try:
            findings.append(Finding.model_validate(entry))
        except ValidationError as exc:
            _log.warning("Skipping invalid finding entry %r: %s", entry, exc)
            continue
    return findings


def _render_findings_markdown(findings: list[Finding]) -> str:
    """Render the Reviewer's findings as markdown for the LLM's reading order."""
    if not findings:
        return "_No findings carried forward._\n"
    # Group by severity in the order the prompt's per-finding loop walks.
    by_sev: dict[str, list[Finding]] = {"critical": [], "important": [], "minor": []}
    for f in findings:
        by_sev[f.severity].append(f)
    parts: list[str] = []
    for sev in ("critical", "important", "minor"):
        bucket = by_sev[sev]
        if not bucket:
            continue
        parts.append(f"### {sev.capitalize()}\n")
        for f in bucket:
            parts.append(
                f"- **target**: {f.target}\n  - **issue**: {f.issue}\n  - **needed**: {f.needed}\n"
            )
        parts.append("")
    return "\n".join(parts)


def _render_findings_json(findings: list[Finding]) -> str:
    """Render the Reviewer's findings as JSON for unambiguous targeting."""
    payload = [
        {
            "severity": f.severity,
            "target": f.target,
            "issue": f.issue,
            "needed": f.needed,
        }
        for f in findings
    ]
    return json.dumps(payload, indent=2)


def _build_user_prompt(
    *,
    issue_title: str,
    issue_body: str,
    pr_title: str,
    pr_body: str,
    spec_doc_content: str | None,
    review_comment: str,
    findings: list[Finding],
    attempt: int,
    max_fix_attempts: int,
    instructions: str | None,
) -> str:
    """Compose the per-run user prompt.

    The Fixer needs the issue (ground truth), the spec PR's artifact
    (spec doc + PR body), the Reviewer's review_comment (context), and
    the structured findings (the contract). Findings are rendered BOTH
    as markdown (for reading order) AND as JSON (for unambiguous
    targeting) — the LLM uses the JSON when emitting the structured
    output, the markdown when reading.

    ``instructions`` is the verbatim contents of the project's
    ``.foreman/INSTRUCTIONS.md`` (or ``None`` when absent). When present
    the section is emitted near the top so project-specific conventions
    (commit-message rules, branch conventions, etc.) frame the fix.
    When ``None`` the section is omitted entirely.
    """
    instructions_section = (
        f"## Project-specific instructions\n\n{instructions}\n\n" if instructions else ""
    )
    spec_section = (
        f"## Spec doc (currently committed in this PR)\n```markdown\n{spec_doc_content}\n```\n\n"
        if spec_doc_content
        else (
            "## Spec doc\nNot inlined — read it from the worktree using "
            "the Read tool at the path the PR body references.\n\n"
        )
    )
    return (
        f"You are fixing Reviewer findings on spec PR. This is fix attempt "
        f"#{attempt} of a maximum of {max_fix_attempts}.\n\n"
        f"{instructions_section}"
        f"## Originating issue\nTitle: {issue_title}\n\n{issue_body}\n\n"
        f"## PR title\n{pr_title}\n\n"
        f"## PR body\n{pr_body}\n\n"
        f"{spec_section}"
        f"## Reviewer's review_comment (prose context)\n{review_comment}\n\n"
        f"## Reviewer's structured findings (markdown for reading)\n"
        f"{_render_findings_markdown(findings)}\n"
        f"## Reviewer's structured findings (JSON, authoritative for targeting)\n"
        f"```json\n{_render_findings_json(findings)}\n```\n\n"
        "Follow the steps in your system prompt. Apply edits in severity "
        "order, verify critical+important edits before committing, "
        "commit+push from the worktree, then return your structured "
        "FixerOutput."
    )


def _read_spec_doc(worktree_path: Path, issue_number: int) -> str | None:
    """Best-effort read of the spec doc from the worktree.

    The Planner commits at
    ``docs/superpowers/specs/foreman-issue-<N>-spec.md``. Reading it
    eagerly lets us inline it into the user prompt. Returns ``None`` if
    the file is missing — the LLM can still find it via Read.
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


def _find_spec_pr(repo: Repository, owner: str, branch: str) -> PullRequest:
    """Locate the open spec PR whose head branch matches ``branch``.

    The Planner names spec branches ``foreman/issue-<N>``. We query
    open PRs by head (``owner:branch``) and take the first match.
    Raises if no open PR is found — the Fixer is only meant to act on
    PRs the Reviewer flagged, so an absent PR is an upstream-state
    error worth surfacing loudly.
    """
    head_qualifier = f"{owner}:{branch}"
    pulls = list(repo.get_pulls(state="open", head=head_qualifier))
    if not pulls:
        raise RuntimeError(
            f"No open PR found for branch {branch!r} in {repo.full_name!r}. "
            "The Fixer expects the Planner-opened spec PR to still be open "
            "for the issue it's fixing."
        )
    return pulls[0]


def _latest_reviewer_review_comment(pr: PullRequest) -> str:
    """Return the body of the most recent review on the PR.

    The Reviewer posts a review with ``event="COMMENT"``; we read the
    last one's body to give the Fixer the prose context the Reviewer
    addressed to it. If no reviews exist on the PR, we surface a
    RuntimeError — there's nothing for the Fixer to act on. The Fixer
    operates strictly downstream of the Reviewer.
    """
    reviews = list(pr.get_reviews())
    if not reviews:
        raise RuntimeError(
            f"PR #{pr.number} has no reviews. The Fixer requires a Reviewer "
            "review to act on; the upstream pipeline state is incomplete."
        )
    last = reviews[-1]
    return last.body or ""


def _unaddressed_by_reason_histogram(output: FixerOutput) -> dict[str, int]:
    """Compute the ``{reason: count}`` histogram for stats."""
    hist: dict[str, int] = {}
    for u in output.unaddressed_findings:
        hist[u.reason] = hist.get(u.reason, 0) + 1
    return hist


# v4-PHASE-8-KILL: legacy fixer entry-point used by the `fix` CLI command (cli.py); emits via label-writing tail. Replaced by run_fixer_cli (below) + fix-v4 CLI command. Remove this function and the legacy label-writing tail in Phase 8.
async def run_fixer(
    *,
    issue_url: str,
    config: V4Config,
    project_name: str,
    worktrees_root: Path,
    provider: ProviderFacade,
    identity_registry: V4IdentityRegistry,
    target: str = "spec_pr",
    dispatch_recorder: DispatchRecorder | None = None,
    dispatch_trace_id: int | None = None,
) -> FixerRunResult:
    """Run the Fixer role end-to-end on one issue.

    Args:
        issue_url: Full GitHub ISSUE URL
            (``https://github.com/owner/repo/issues/N``) — NOT the spec
            PR URL. The Fixer is triggered by the
            ``foreman:spec-fix`` label on the issue and derives the PR
            from the issue's ``foreman/issue-<N>`` branch.
        config: Loaded foreman v4 config.
        project_name: Selects which ``V4Config.projects`` entry to use.
        worktrees_root: Root directory under which per-ticket worktrees
            live.
        provider: Agent provider facade (e.g., AnthropicSDKProvider).
        identity_registry: Pre-built v4 registry. Tests may inject a
            ``MagicMock`` exposing the production
            ``get_role_token(role)`` shape — see
            :func:`foreman.roles.build_role_resources` for the test
            seam.

    Returns:
        A :class:`~foreman.schemas.fixer.FixerRunResult` bundling the
        Fixer LLM's :class:`~foreman.schemas.fixer.FixerOutput` and the
        ``attempt`` counter (the run-number stamped on the issue at
        entry). The CLI surfaces both for the one-line summary.

    Raises:
        ValueError: Issue URL malformed or repo mismatch.
        RuntimeError: No open spec PR / no Reviewer review found.
    """
    # Post-adversarial-review (#1): wrap the initial setup — URL parse,
    # project lookup, identity setup, ``repo.get_issue`` — in a
    # defensive try block so a transient failure fires the runaway-burn
    # helper on the FIRST failure instead of letting #228's rate-limit
    # catch it at N=3.
    _setup_issue_number: int | None = None
    _setup_repo_slug: str | None = None
    _setup_issue: Issue | None = None
    try:
        owner, repo_name, issue_number = parse_issue_url(issue_url)
        _setup_issue_number = issue_number
        project = next(
            (p for p in config.projects if p.name == project_name), None
        )
        if project is None:
            known = [p.name for p in config.projects]
            raise ValueError(
                f"project {project_name!r} not found in V4Config. "
                f"Known projects: {known}"
            )
        expected_repo_slug = project.repo
        actual_repo_slug = f"{owner}/{repo_name}"
        _setup_repo_slug = actual_repo_slug
        if expected_repo_slug != actual_repo_slug:
            raise ValueError(
                f"Issue URL repo {actual_repo_slug!r} does not match project "
                f"{project_name!r} configured repo {expected_repo_slug!r}"
            )

        _host, fixer_token, fixer_client = build_role_resources(
            registry=identity_registry,
            role="fixer",
            app_id=config.apps.fixer.app_id,
            private_key_path=config.apps.fixer.private_key_path,
        )

        repo: Repository = fixer_client.get_repo(actual_repo_slug)
        issue = repo.get_issue(issue_number)
        _setup_issue = issue
    except Exception as exc:
        if _setup_issue is not None and _setup_issue_number is not None:
            bound_issue = _setup_issue
            handle_unhandled_role_exception(
                role="fixer",
                issue_number=_setup_issue_number,
                exc=exc,
                post_comment=lambda body: bound_issue.create_comment(body),
                set_needs_help_label=lambda: bound_issue.add_to_labels(TERMINAL_BLOCKING_LABEL),
            )
        raise

    # Under v4, the state machine's retry cap (foreman#8c.2) owns
    # attempt counting; the role no longer reads or writes
    # ``foreman:fix-attempt-N`` labels. Each invocation IS attempt 1
    # from the role's vantage; the v4 state machine routes subsequent
    # attempts via its own retry budget. ``attempt`` stays in the
    # schema for stats compatibility.
    attempt = 1
    max_fix_attempts = project.max_fix_attempts

    # foreman#239: stamp ``start_time`` BEFORE the body wrap and
    # initialize ``usage`` / ``pr_number`` to ``None`` so the except
    # branch below can log partial state regardless of where in the
    # pipeline a failure surfaces. ``WorktreeManager.attach``,
    # ``_find_spec_pr``, ``_latest_reviewer_review_comment``,
    # ``provider.run_agent``, and ``pr.create_issue_comment`` are all
    # inside the wrap; pre-#239 any of those raising silently dropped
    # the run's cost telemetry because the success-path
    # ``log_fixer_run`` call below never executed. Mirrors the
    # foreman#235 / PR #236 pattern for Planner.
    start_time = time.monotonic()
    usage: UsageInfo | None = None
    pr_number: int | None = None

    def _on_failure(exc: BaseException) -> None:
        """Shared cleanup body for ``ProviderError`` + ``Exception``
        arms (foreman#266 — type-narrowing split). Closes over
        ``start_time`` / ``usage`` / ``pr_number`` /
        ``actual_repo_slug`` / ``issue_number`` / ``attempt`` /
        ``project_name`` / ``dispatch_recorder`` /
        ``dispatch_trace_id`` / ``issue``. The bare ``raise`` that
        re-propagates the original exception lives in each ``except``
        arm after calling this helper.
        """
        # foreman#239 + foreman#229: capture cost telemetry AND defend
        # against the runaway-burn pattern.
        duration_seconds = time.monotonic() - start_time
        try:
            log_fixer_run(
                repo_slug=actual_repo_slug,
                issue_number=issue_number,
                pr_number=pr_number,
                attempt=attempt,
                outcome="exception",
                total_findings=0,
                addressed_count=0,
                unaddressed_count=0,
                unaddressed_by_reason={},
                disagreed_count=0,
                confidence="low",
                duration_seconds=duration_seconds,
                input_tokens=usage.input_tokens if usage is not None else 0,
                output_tokens=usage.output_tokens if usage is not None else 0,
                cache_creation_input_tokens=(
                    usage.cache_creation_input_tokens if usage is not None else 0
                ),
                cache_read_input_tokens=(usage.cache_read_input_tokens if usage is not None else 0),
                total_cost_usd=usage.total_cost_usd if usage is not None else None,
                model_usage=usage.model_usage if usage is not None else None,
                duration_ms=usage.duration_ms if usage is not None else 0,
                num_turns=usage.num_turns if usage is not None else 0,
            )
        except Exception:
            # Best-effort telemetry — swallow so the daemon dispatcher
            # sees the ORIGINAL exception, not the stats writer's.
            pass
        # foreman#251 (Phase 1): mirror the failure-path dual-write.
        emit_recorder_complete(
            dispatch_recorder=dispatch_recorder,
            dispatch_trace_id=dispatch_trace_id,
            role="fixer",
            repo_slug=actual_repo_slug,
            ticket_id=f"{actual_repo_slug}#{issue_number}",
            project=project_name,
            issue_number=issue_number,
            pr_number=pr_number,
            outcome="exception",
            usage=usage if usage is not None else UsageInfo(),
            role_data={
                "attempt": attempt,
                "total_findings": 0,
                "addressed_count": 0,
                "unaddressed_count": 0,
                "unaddressed_by_reason": {},
                "disagreed_count": 0,
                "confidence": "low",
            },
            duration_seconds=duration_seconds,
        )
        # foreman#229: runaway-burn defense. Post the traceback as a
        # comment on the originating issue and transition it to
        # ``foreman:needs-help`` so the dispatcher's poll loop stops
        # re-dispatching.
        handle_unhandled_role_exception(
            role="fixer",
            issue_number=issue_number,
            exc=exc,
            post_comment=lambda body: issue.create_comment(body),
            set_needs_help_label=lambda: issue.add_to_labels(TERMINAL_BLOCKING_LABEL),
        )

    try:
        # Resolve the spec PR from the issue's branch convention.
        branch = spec_branch(issue_number)
        pr = _find_spec_pr(repo, owner=owner, branch=branch)
        # foreman#239: hoist ``pr_number`` to the outer scope right
        # after the PR is resolved so the fixer_failed row reports it
        # even when a later step (run_agent, create_issue_comment)
        # raises.
        pr_number = pr.number
        review_comment = _latest_reviewer_review_comment(pr)

        # Attach to the existing branch — the Planner created it; the
        # Fixer must not branch from main.
        # WorktreeManager's git subprocesses (fetch / worktree add) must
        # authenticate as the fixer bot — without the explicit token they
        # inherit the daemon's parent ``GH_TOKEN`` (CI runner, dev shell)
        # and attribute identity to the daemon's identity on private repos.
        # Same anti-leak motivation as the Stage 3e role-module subprocess
        # fix; WorktreeManager was scoped out at the time as a follow-up.
        wt_mgr = WorktreeManager(worktrees_root=worktrees_root, role_token=fixer_token)
        wt_path = wt_mgr.attach(
            clone_path=Path(project.local_clone_path),
            repo_slug=repo_name,
            ticket_id=issue_number,
            repo_url=f"https://github.com/{project.repo}.git",
        )

        # Recover the Reviewer's structured findings from the marker-fenced
        # JSON block the Reviewer embeds in its posted review body
        # (see ``foreman.roles.reviewer._build_findings_block``). This is the
        # only path the structured list takes across the role boundary in v1 —
        # the in-memory ``ReviewerOutput.findings`` is process-local. Without
        # this extraction the Fixer's "every edit must trace to a structured
        # finding" rule produces zero actions; the contract was broken before.
        # On malformed/missing block we return ``[]`` (logged warning) and the
        # LLM falls back to reading the prose ``review_comment`` — same behavior
        # as before this fix, just no longer the default.
        findings: list[Finding] = _extract_findings_from_review_comment(review_comment)

        spec_doc_content = _read_spec_doc(wt_path, issue_number)
        instructions = load_project_instructions(Path(project.local_clone_path))

        # The ``target`` kwarg is the v3-vocabulary "spec_pr" / "impl_pr"
        # form. Cast to satisfy mypy without widening
        # ``_load_fixer_prompt``'s signature. Unknown values fall back to
        # the spec composition inside the loader.
        system_prompt = _load_fixer_prompt(target=cast(Literal["spec_pr", "impl_pr"], target))
        user_prompt = _build_user_prompt(
            issue_title=issue.title or "",
            issue_body=issue.body or "",
            pr_title=pr.title or "",
            pr_body=pr.body or "",
            spec_doc_content=spec_doc_content,
            review_comment=review_comment,
            findings=findings,
            attempt=attempt,
            max_fix_attempts=max_fix_attempts,
            instructions=instructions,
        )

        llm_output, run_usage = await provider.run_agent(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            allowed_tools=FIXER_ALLOWED_TOOLS,
            output_model=FixerOutput,
            cwd=wt_path,
            env={**os.environ, "GH_TOKEN": fixer_token},
        )
        # foreman#239: hoist ``usage`` to the outer scope IMMEDIATELY
        # so a failure in any later step (create_issue_comment) still
        # records the per-call token cost in the fixer_failed row.
        usage = run_usage
        duration_seconds = time.monotonic() - start_time

        # Post the fix summary as a PR COMMENT, not a review. The Fixer is
        # acting on the Reviewer's feedback; reviews come from Reviewers.
        pr.create_issue_comment(body=llm_output.fix_comment)

        # The Fixer does not mutate labels. Under v4,
        # ``LabelObservabilityObserver`` owns every ``foreman:*`` write
        # off state-machine transitions; ``final_labels`` here is just
        # the post-call snapshot returned for the daemon's audit trail.
        final_labels = sorted({label.name for label in issue.labels})

        # JSONL stats — write regardless of outcome.
        unaddressed_hist = _unaddressed_by_reason_histogram(llm_output)
        disagreed_count = unaddressed_hist.get("needed_remediation_wrong", 0)
        log_fixer_run(
            repo_slug=actual_repo_slug,
            issue_number=issue_number,
            pr_number=pr_number,
            attempt=attempt,
            outcome=llm_output.outcome,
            total_findings=(
                len(llm_output.addressed_findings) + len(llm_output.unaddressed_findings)
            ),
            addressed_count=len(llm_output.addressed_findings),
            unaddressed_count=len(llm_output.unaddressed_findings),
            unaddressed_by_reason=unaddressed_hist,
            disagreed_count=disagreed_count,
            confidence=llm_output.confidence,
            duration_seconds=duration_seconds,
            # foreman#227: provider-reported usage from the ResultMessage.
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            # foreman#244: prompt-cache token counters from the SDK
            # (billed at 25% / 10% of regular input rate).
            cache_creation_input_tokens=usage.cache_creation_input_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
            total_cost_usd=usage.total_cost_usd,
            model_usage=usage.model_usage,
            duration_ms=usage.duration_ms,
            num_turns=usage.num_turns,
        )
        # foreman#251 (Phase 1): dual-write through the Recorder.
        emit_recorder_complete(
            dispatch_recorder=dispatch_recorder,
            dispatch_trace_id=dispatch_trace_id,
            role="fixer",
            repo_slug=actual_repo_slug,
            ticket_id=f"{actual_repo_slug}#{issue_number}",
            project=project_name,
            issue_number=issue_number,
            pr_number=pr_number,
            outcome=llm_output.outcome,
            usage=usage,
            role_data={
                "attempt": attempt,
                "total_findings": (
                    len(llm_output.addressed_findings) + len(llm_output.unaddressed_findings)
                ),
                "addressed_count": len(llm_output.addressed_findings),
                "unaddressed_count": len(llm_output.unaddressed_findings),
                "unaddressed_by_reason": unaddressed_hist,
                "disagreed_count": disagreed_count,
                "confidence": llm_output.confidence,
            },
            duration_seconds=duration_seconds,
        )

        return FixerRunResult(
            llm_output=llm_output,
            attempt=attempt,
            final_labels=final_labels,
        )
    except ProviderError as exc:
        # foreman#266: typed catch for the documented provider-boundary
        # failure mode. Same body as the ``except Exception`` arm
        # below — structural (type narrowing + boundary documentation),
        # not semantic.
        _on_failure(exc)
        raise
    except Exception as exc:
        # PR #255 commit 2 defensive handler — belt-and-suspenders for
        # non-provider failures (worktree ops, host I/O, GitHub 5xx).
        _on_failure(exc)
        raise


# ============================================================
# v4 emit path — additive alongside the legacy label-writing entry-point.
# The legacy code path (above) stays running through Phase 7. Phase 8
# deletes the legacy entry-point + everything tagged with v4-PHASE-8-KILL
# and removes this banner.
# ============================================================
import asyncio  # noqa: E402  (kept here so legacy import block above stays untouched)

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

# v4 RoleDispatcher uses "spec" / "impl"; the legacy Fixer internals
# (entry-label gate, prompt loader, target routing) speak "spec_pr" /
# "impl_pr". Translation lives only at the v4 boundary so legacy paths
# keep their existing vocabulary verbatim.
_V4_TARGET_TO_LEGACY: dict[str, Literal["spec_pr", "impl_pr"]] = {
    "spec": "spec_pr",
    "impl": "impl_pr",
}

# Head-branch shape per target — used to locate the open PR the v4
# Fixer is about to amend. The legacy Fixer takes an issue URL and
# derives the PR from the issue's branch convention; v4's
# SubprocessRoleDispatcher only knows the issue number + target.
_V4_TARGET_TO_BRANCH_PREFIX: dict[str, str] = {
    "spec": "foreman/issue-",
    "impl": "foreman/impl-",
}


class _V4FixerResult:
    """Flat-shape result for the v4 emit path.

    The legacy ``FixerRunResult`` nests outcome under
    ``llm_output.outcome`` ("fixed" / "incomplete") with an ``attempt``
    counter and full ``addressed_findings`` / ``unaddressed_findings``
    breakdown. The v4 emit path consumes the boolean ``pushed`` +
    ``escalated`` + a ``pr_number`` so ``run_fixer_cli`` can pick
    CLEAN vs NEEDS_HELP without re-interpreting legacy shape. The
    Fixer's outcome surface is intentionally narrower than the
    Reviewer's — findings do NOT propagate; the downstream v4 state
    machine routes the amended PR back to the Reviewer, which
    re-emits its own NEEDS_FIX/CLEAN verdict. Both this class and the
    helper that builds instances disappear in Phase 8.
    """

    def __init__(
        self,
        *,
        pushed: bool,
        escalated: bool,
        pr_number: int | None,
        summary: str,
    ) -> None:
        self.pushed = pushed
        self.escalated = escalated
        self.pr_number = pr_number
        self.summary = summary


def _run_fixer_for_v4(
    *, project: str, issue_number: int, target: str
) -> _V4FixerResult:
    """Run the fixer end-to-end for a v4 caller.

    Wraps the role's ``run_fixer`` (worktree attach → LLM dispatch →
    commit/push → PR comment post) and flattens its result into the
    v4 shape consumed by ``run_fixer_cli``. The v4 state machine
    drives off the FOREMAN_OUTCOME emitted below, not off GitHub
    labels — ``LabelObservabilityObserver`` owns ``foreman:*`` writes
    off state-machine transitions.

    ``target`` arrives in v4 vocabulary ("spec" / "impl"); the role's
    ``run_fixer`` takes the "spec_pr" / "impl_pr" form for prompt
    selection. Translation happens at the boundary.

    Escalation: a "soft" incomplete (LLM returned
    ``outcome="incomplete"``) maps to NEEDS_HELP — the v4 state
    machine's retry cap owns the budget; the role does not refuse on
    its own.
    """
    cfg_path = Path(os.environ.get("FOREMAN_V4_CONFIG", _DEFAULT_V4_CONFIG))
    cfg = load_v4_config(cfg_path)
    project_cfg = next((p for p in cfg.projects if p.name == project), None)
    if project_cfg is None:
        known = [p.name for p in cfg.projects]
        raise ValueError(
            f"project {project!r} not found in V4Config at {cfg_path}. "
            f"Known projects: {known}"
        )

    # Locate the open PR for this issue. The Fixer's legacy entry-
    # point takes an issue URL — v4's SubprocessRoleDispatcher only
    # knows the issue number + target. Resolve via the fixer App's
    # client (read-only get_pulls is in scope for the fixer identity).
    registry = V4IdentityRegistry(
        apps=cfg.apps,
        orchestrator=cfg.orchestrator,
        installation_repo=project_cfg.repo,
    )
    _host, _token, fixer_client = build_role_resources(
        registry=registry,
        role="fixer",
        app_id=cfg.apps.fixer.app_id,
        private_key_path=cfg.apps.fixer.private_key_path,
    )
    repo: Repository = fixer_client.get_repo(project_cfg.repo)
    owner = project_cfg.repo.split("/", 1)[0]
    branch_prefix = _V4_TARGET_TO_BRANCH_PREFIX[target]
    branch = f"{branch_prefix}{issue_number}"
    head_qualifier = f"{owner}:{branch}"
    pulls = list(repo.get_pulls(state="open", head=head_qualifier))
    if not pulls:
        raise RuntimeError(
            f"No open PR found for branch {branch!r} in {project_cfg.repo!r}. "
            f"The v4 Fixer expects the {target}-side PR to be open for "
            f"issue #{issue_number}."
        )
    pr = pulls[0]
    issue_url = f"https://github.com/{project_cfg.repo}/issues/{issue_number}"

    worktrees_root = Path(
        os.environ.get(
            "FOREMAN_WORKTREES_ROOT",
            str(Path.home() / ".foreman" / "worktrees"),
        )
    )
    provider = make_provider()
    legacy_target = _V4_TARGET_TO_LEGACY[target]
    legacy_result = asyncio.run(
        run_fixer(
            issue_url=issue_url,
            config=cfg,
            project_name=project,
            worktrees_root=worktrees_root,
            provider=provider,
            identity_registry=registry,
            target=legacy_target,
        )
    )

    # Flatten legacy → v4 shape. The legacy ``FixerOutput.outcome`` is
    # "fixed" or "incomplete"; the v4 surface needs a binary
    # ``pushed`` (CLEAN) vs ``escalated`` (NEEDS_HELP) — anything
    # short of "fixed" is treated as needing human help. Retry budget
    # lives in the v4 state machine (foreman#8c.2); the role no longer
    # caps attempts itself.
    llm = legacy_result.llm_output
    pushed = llm.outcome == "fixed"
    summary = (
        llm.fix_comment[:500]
        if llm.fix_comment
        else f"{llm.outcome} (attempt {legacy_result.attempt})"
    )
    return _V4FixerResult(
        pushed=pushed,
        escalated=not pushed,
        pr_number=pr.number,
        summary=summary,
    )


def run_fixer_cli(*, project: str, issue_number: int, target: str) -> int:
    """v4 CLI entry-point. Emits FOREMAN_OUTCOME JSON; returns exit code.

    ``target`` is the v4 vocab ("spec" / "impl"). The
    SubprocessRoleDispatcher (Task 5.6) forks ``foreman fix-v4
    --target spec|impl`` which calls this. Legacy ``fix`` command +
    label-writing tail in this module are tagged ``v4-PHASE-8-KILL``
    and deleted together in Phase 8.
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
        result = _run_fixer_for_v4(
            project=project, issue_number=issue_number, target=target
        )
    except Exception as exc:
        emit_outcome(
            Outcome(
                kind=OutcomeKind.ERROR,
                confidence=OutcomeConfidence.HIGH,
                summary=f"fixer raised: {exc}"[:500],
            )
        )
        return 1

    if getattr(result, "escalated", False):
        emit_outcome(
            Outcome(
                kind=OutcomeKind.NEEDS_HELP,
                confidence=OutcomeConfidence.HIGH,
                summary=getattr(result, "summary", None) or "fixer exhausted attempts",
            )
        )
        return 0

    emit_outcome(
        Outcome(
            kind=OutcomeKind.CLEAN,
            confidence=OutcomeConfidence.HIGH,
            summary=getattr(result, "summary", None) or "fix pushed",
            artifacts=OutcomeArtifacts(pr_number=getattr(result, "pr_number", None)),
        )
    )
    return 0
