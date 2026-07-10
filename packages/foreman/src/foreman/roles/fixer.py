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

from foreman.branches import impl_branch, spec_branch
from foreman.git_host import CommentRef
from foreman.instructions import load_project_instructions
from foreman.provider import ProviderFacade, UsageInfo
from foreman.providers import ProviderError, ProviderTransientError
from foreman.roles import (
    build_role_resources,
    emit_transient_provider_outcome,
    handle_unhandled_role_exception,
)
from foreman.roles._escalation_comment import post_escalation_comment
from foreman.roles._prompt_helpers import (
    filter_bot_self_comments,
    format_comments_section,
)
from foreman.roles.reviewer import FINDINGS_BEGIN_MARKER, FINDINGS_END_MARKER
from foreman.schemas.fixer import FixerOutput, FixerRunResult
from foreman.schemas.reviewer import Finding
from foreman.stats import log_fixer_run
from foreman.v4.config import V4Config, resolve_operator
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
    comments: list[CommentRef],
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

    ``comments`` is the originating issue's comment stream (foreman#328),
    used only by the spec-side Fixer. ``run_fixer`` passes ``[]`` for
    impl-side fixes to keep the impl prompt byte-identical to pre-#328
    behavior. Empty list → no ``## Comments`` section.
    """
    instructions_section = (
        f"## Project-specific instructions\n\n{instructions}\n\n" if instructions else ""
    )
    comments_section = format_comments_section(comments)
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
        f"{comments_section}"
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


def _find_role_pr(
    repo: Repository,
    owner: str,
    branch: str,
    *,
    target: Literal["spec_pr", "impl_pr"],
) -> PullRequest:
    """Locate the open PR whose head branch matches ``branch``.

    The Planner names spec branches ``foreman/issue-<N>`` and the
    Worker names impl branches ``foreman/impl-<N>``. We query open
    PRs by head (``owner:branch``) and take the first match.

    ``target`` selects the error-wording shape so callers reaching
    for the impl PR don't get spec-flavored boilerplate (the
    algokit#21 dogfood bug: the legacy lookup always raised "spec PR"
    even when the Fixer was invoked for the impl side).

    Raises if no open PR is found — the Fixer is only meant to act on
    PRs the Reviewer flagged, so an absent PR is an upstream-state
    error worth surfacing loudly.
    """
    head_qualifier = f"{owner}:{branch}"
    pulls = list(repo.get_pulls(state="open", head=head_qualifier))
    if not pulls:
        if target == "impl_pr":
            opener = "Worker-opened impl PR"
            pr_label = "impl PR"
        else:
            opener = "Planner-opened spec PR"
            pr_label = "spec PR"
        raise RuntimeError(
            f"No open PR found for branch {branch!r} in {repo.full_name!r}. "
            f"The Fixer expects the {opener} to still be open "
            f"for the issue it's fixing ({pr_label})."
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


async def _run_fixer_core(
    *,
    issue_url: str,
    config: V4Config,
    project_name: str,
    worktrees_root: Path,
    provider: ProviderFacade,
    identity_registry: V4IdentityRegistry,
    target: str = "spec_pr",
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
        target: ``"spec_pr"`` (default) or ``"impl_pr"``. Selects which
            PR shape to fix — drives the Fixer prompt composition
            (:func:`_load_fixer_prompt`), which branch to locate
            (:func:`_find_role_pr`), and the error wording used when
            no matching open PR is found.

    Returns:
        A :class:`~foreman.schemas.fixer.FixerRunResult` bundling the
        Fixer LLM's :class:`~foreman.schemas.fixer.FixerOutput` and the
        ``attempt`` counter (the run-number stamped on the issue at
        entry). The CLI surfaces both for the one-line summary.

    Raises:
        ValueError: Issue URL malformed or repo mismatch.
        RuntimeError: No open PR (spec or impl, per ``target``) /
            no Reviewer review found.
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
        project = next((p for p in config.projects if p.name == project_name), None)
        if project is None:
            known = [p.name for p in config.projects]
            raise ValueError(
                f"project {project_name!r} not found in V4Config. Known projects: {known}"
            )
        expected_repo_slug = project.repo
        actual_repo_slug = f"{owner}/{repo_name}"
        _setup_repo_slug = actual_repo_slug
        if expected_repo_slug != actual_repo_slug:
            raise ValueError(
                f"Issue URL repo {actual_repo_slug!r} does not match project "
                f"{project_name!r} configured repo {expected_repo_slug!r}"
            )

        # Issue #347: the Fixer now owns the post-LLM ``host.push_branch``
        # call so the runtime ``_ensure_provenance_trailers`` amend lands
        # on the REMOTE branch the DCO gate from PR #346 checks.
        # Mirrors the Worker's foreman#222 pattern at ``worker.py:1032``.
        host, fixer_token, fixer_client = build_role_resources(
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
    # ``_find_role_pr``, ``_latest_reviewer_review_comment``,
    # ``provider.run_agent``, and ``pr.create_issue_comment`` are all
    # inside the wrap; pre-#239 any of those raising silently dropped
    # the run's cost telemetry because the success-path
    # ``log_fixer_run`` call below never executed. Mirrors the
    # foreman#235 / PR #236 pattern for Planner.
    start_time = time.monotonic()
    usage: UsageInfo | None = None
    pr_number: int | None = None

    def _on_failure(exc: BaseException) -> None:
        """Shared cleanup body for ``ProviderError`` + ``Exception`` arms (foreman#266 — type-narrowing split).

        Closes over
        ``start_time`` / ``usage`` / ``pr_number`` /
        ``actual_repo_slug`` / ``issue_number`` / ``attempt`` /
        ``project_name`` / ``issue``. The bare ``raise`` that
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
        # foreman#229: runaway-burn defense. Post the traceback as a
        # comment on the originating issue. Under v4 the state machine
        # owns the NeedsHelp transition + ``foreman:state-needs-help``
        # label write via :class:`LabelObservabilityObserver`; the
        # role-side ``foreman:needs-help`` write was dropped in Phase
        # 8d.7.
        handle_unhandled_role_exception(
            role="fixer",
            issue_number=issue_number,
            exc=exc,
            post_comment=lambda body: issue.create_comment(body),
        )

    try:
        # Resolve the role's PR from the issue's branch convention.
        # ``target`` selects which branch shape to look up: the Planner-
        # opened spec branch (``foreman/issue-<N>``) for spec-side fixes,
        # or the Worker-opened impl branch (``foreman/impl-<N>``) for
        # impl-side fixes. Pre-Phase 8d.21 this hardcoded the spec
        # branch and crashed the algokit#21 dogfood at ImplFix because
        # the impl PR was never found.
        target_literal = cast(Literal["spec_pr", "impl_pr"], target)
        if target_literal == "impl_pr":
            branch = impl_branch(issue_number)
        else:
            branch = spec_branch(issue_number)
        pr = _find_role_pr(repo, owner=owner, branch=branch, target=target_literal)
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
        # Phase 8d.23: pass ``target`` so the impl-side Fixer's worktree
        # checks out ``foreman/impl-<N>`` (the Worker's branch) instead of
        # ``foreman/issue-<N>`` (the Planner's spec branch). Without this,
        # Phase 8d.21's PR-lookup fix found the right impl PR but the
        # Fixer then edited + committed against the spec branch — the
        # algokit#21 dogfood surfaced this on 2026-06-16. The
        # ``target='spec_pr'`` path is unchanged from before.
        wt_path = wt_mgr.attach(
            clone_path=Path(project.local_clone_path),
            repo_slug=repo_name,
            ticket_id=issue_number,
            repo_url=f"https://github.com/{project.repo}.git",
            target=target_literal,
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
            comments=comments,
        )

        # Issue #347: resolve the operator identities once. The env
        # injection feeds the LLM's ``<provenance_trailers>`` prompt
        # section (primary defense); the post-LLM
        # ``_ensure_provenance_trailers`` helper run + ``host.push_branch``
        # below are the runtime backstop that ensures every Fixer commit
        # lands on the REMOTE with BOTH ``Supervised-by:`` and
        # ``Signed-off-by:`` trailers — regardless of which target
        # (spec_pr / impl_pr) is in flight.
        operator = resolve_operator(project, config)
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
            allowed_tools=FIXER_ALLOWED_TOOLS,
            output_model=FixerOutput,
            cwd=wt_path,
            session_id=_session_id,
            resume=_resume,
            env={
                **os.environ,
                "GH_TOKEN": fixer_token,
                # Issue #347: identical env-var quadruple as the Worker
                # (see ``worker.py``'s ``provider.run_agent`` call site).
                "FOREMAN_OPERATOR_SUPERVISOR_NAME": operator.supervisor.name,
                "FOREMAN_OPERATOR_SUPERVISOR_EMAIL": operator.supervisor.email,
                "FOREMAN_OPERATOR_SIGNER_NAME": operator.signer.name,
                "FOREMAN_OPERATOR_SIGNER_EMAIL": operator.signer.email,
            },
        )
        # foreman#239: hoist ``usage`` to the outer scope IMMEDIATELY
        # so a failure in any later step (create_issue_comment) still
        # records the per-call token cost in the fixer_failed row.
        usage = run_usage

        # Issue #347 runtime defense: amend HEAD if EITHER trailer is
        # missing, then deterministically push from Python so the
        # amended commit lands on the remote where the DCO gate
        # checks. The prompt-side ``<provenance_trailers>`` section in
        # ``fixer.md`` / ``fixer_impl.md`` is the primary defense. Both
        # Fixer targets follow this shape — the per-target ``branch``
        # was resolved above at the ``branch = …`` lines.
        from foreman.roles.worker import _ensure_provenance_trailers

        _ensure_provenance_trailers(
            worktree_path=wt_path,
            operator=operator,
            commits_made_count=len(llm_output.commits_made),
            role_token=fixer_token,
        )
        # Push only when the LLM actually committed; if commits_made is
        # empty the branch is in whatever state the previous push left
        # it (or hasn't been touched), and ``host.push_branch`` would
        # be a wasted call.
        if llm_output.commits_made:
            host.push_branch(worktree_path=wt_path, branch=branch)
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

        # foreman#367: post the operator-visible escalation comment
        # BEFORE log_fixer_run so a comment-post failure is visible in
        # the daemon log without preventing the JSONL row.
        if llm_output.outcome == "incomplete" or llm_output.confidence == "low":
            state_instance_id = os.environ.get(
                "FOREMAN_STATE_INSTANCE_ID",
                "unknown",
            )
            fallback_reason = None
            if llm_output.escalation_comment is None:
                fallback_reason = (
                    "fixer LLM produced outcome/confidence triggering "
                    "escalation but did not populate escalation_comment"
                )
            post_escalation_comment(
                host=host,
                repo_slug=actual_repo_slug,
                issue_number=issue_number,
                role="fixer",
                outcome_label=llm_output.outcome,
                summary=llm_output.fix_comment[:500] or llm_output.outcome,
                payload=llm_output.escalation_comment,
                fallback_reason=fallback_reason,
                source=f"role:fixer-{target}",
                key=(f"state-instance-{state_instance_id}-pr-{pr_number}-{llm_output.outcome}"),
            )

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

        return FixerRunResult(
            llm_output=llm_output,
            attempt=attempt,
            final_labels=final_labels,
        )
    except ProviderError as exc:
        # foreman#361: transient failures are retried by the state
        # machine with backoff; suppress the runaway-burn issue
        # comment so a 40-min outage does not carpet the issue with
        # redundant tracebacks.
        if isinstance(exc, ProviderTransientError):
            raise
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


import asyncio  # noqa: E402

from foreman.providers import make_provider  # noqa: E402
from foreman.v4.config import load_config as load_v4_config  # noqa: E402
from foreman.v4.config import load_projects as load_v4_projects  # noqa: E402
from foreman.v4.emit import emit_outcome  # noqa: E402
from foreman.v4.outcome import (  # noqa: E402
    Outcome,
    OutcomeArtifacts,
    OutcomeConfidence,
    OutcomeKind,
)

_DEFAULT_V4_CONFIG = Path.home() / ".foreman" / "v4" / "config.toml"
_DEFAULT_PROJECTS_PATH = Path.home() / ".foreman" / "projects.toml"

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
        details: dict[str, object] | None = None,
    ) -> None:
        self.pushed = pushed
        self.escalated = escalated
        self.pr_number = pr_number
        self.summary = summary
        # Phase 8d.17 / foreman#315: FixerOutput diagnostic detail
        # forwarded to Outcome.details on emit. Populated by
        # ``_run_fixer_for_v4`` from the LLM fields (fix_comment,
        # addressed/unaddressed breakdown, commits_made) that would
        # otherwise be dropped at the v3→v4 flatten point.
        self.details: dict[str, object] = details if details is not None else {}


def _run_fixer_for_v4(*, project: str, issue_number: int, target: str) -> _V4FixerResult:
    """Run the fixer end-to-end for a v4 caller.

    Wraps :func:`_run_fixer_core` (worktree attach → LLM dispatch →
    commit/push → PR comment post, formerly ``run_fixer``) and flattens
    its result into the v4 shape consumed by ``run_fixer_cli``. The v4
    state machine drives off the FOREMAN_OUTCOME emitted below, not off
    GitHub labels.

    ``target`` arrives in v4 vocabulary ("spec" / "impl"); the core takes
    the "spec_pr" / "impl_pr" form for prompt selection. Translation
    happens at the boundary.

    Escalation: a "soft" incomplete (LLM returned
    ``outcome="incomplete"``) maps to NEEDS_HELP — the v4 state
    machine's retry cap owns the budget; the role does not refuse on
    its own.
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

    # Patch the config's projects list so _run_fixer_core's lookup
    # (which reads config.projects) finds the project even when cfg was
    # loaded from a config.toml that has zero [[projects]] tables.
    if not cfg.projects:
        cfg = cfg.model_copy(update={"projects": all_projects})

    # Locate the open PR for this issue. The Fixer's legacy entry-
    # point takes an issue URL — v4's SubprocessRoleDispatcher only
    # knows the issue number + target. Resolve via the fixer App's
    # client (read-only get_pulls is in scope for the fixer identity).
    #
    # Phase 8d.21: the legacy ``run_fixer`` ALSO does a target-aware
    # PR lookup now (it used to hardcode the spec branch — see the
    # algokit#21 dogfood crash). The wrapper still does its own lookup
    # because (a) ``_V4FixerResult.pr_number`` is populated from
    # ``pr.number`` here and ``run_fixer``'s ``FixerRunResult`` does
    # not surface the PR number to callers, and (b) the wrapper's
    # lookup early-fails before ``make_provider`` + ``asyncio.run``
    # spin up, which is cheaper than letting ``run_fixer`` raise
    # mid-setup.
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
    core_result = asyncio.run(
        _run_fixer_core(
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
    llm = core_result.llm_output
    pushed = llm.outcome == "fixed"
    summary = (
        llm.fix_comment[:500]
        if llm.fix_comment
        else f"{llm.outcome} (attempt {core_result.attempt})"
    )
    # Phase 8d.17 / foreman#315: preserve FixerOutput diagnostic
    # detail by lifting onto Outcome.details. The fixer's full
    # ``fix_comment`` (the audit prose) plus the structured
    # addressed/unaddressed breakdown and the commits the Fixer made
    # ride forward without operators needing to pull the PR comment +
    # worktree. ``model_dump(mode="json")`` on the nested pydantic
    # records makes the dict JSON-safe end-to-end.
    details: dict[str, object] = {
        "outcome": llm.outcome,
        "fix_comment": llm.fix_comment,
        "confidence": llm.confidence,
        "attempt": core_result.attempt,
        "commits_made": [c.model_dump(mode="json") for c in llm.commits_made],
        "addressed_findings": [f.model_dump(mode="json") for f in llm.addressed_findings],
        "unaddressed_findings": [f.model_dump(mode="json") for f in llm.unaddressed_findings],
    }
    return _V4FixerResult(
        pushed=pushed,
        escalated=not pushed,
        pr_number=pr.number,
        summary=summary,
        details=details,
    )


def run_fixer_cli(*, project: str, issue_number: int, target: str) -> int:
    """v4 CLI entry-point. Emits FOREMAN_OUTCOME JSON; returns exit code.

    ``target`` is the v4 vocab ("spec" / "impl"). The
    SubprocessRoleDispatcher (Task 5.6) forks ``foreman fix-v4
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
        result = _run_fixer_for_v4(project=project, issue_number=issue_number, target=target)
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
                summary=f"fixer raised: {exc}"[:500],
            )
        )
        return 1

    # Phase 8d.17 / foreman#315: forward Fixer diagnostic detail onto
    # every emitted Outcome (CLEAN, NEEDS_HELP). The isinstance check
    # keeps the contract backward-compatible — older test doubles
    # (MagicMock) that don't set ``details`` explicitly produce a
    # non-dict attribute by default; we ignore it and emit an empty
    # bag rather than fail pydantic validation.
    raw_details = getattr(result, "details", None)
    details: dict[str, object] = raw_details if isinstance(raw_details, dict) else {}

    if getattr(result, "escalated", False):
        emit_outcome(
            Outcome(
                kind=OutcomeKind.NEEDS_HELP,
                confidence=OutcomeConfidence.HIGH,
                summary=getattr(result, "summary", None) or "fixer exhausted attempts",
                details=details,
            )
        )
        return 0

    emit_outcome(
        Outcome(
            kind=OutcomeKind.CLEAN,
            confidence=OutcomeConfidence.HIGH,
            summary=getattr(result, "summary", None) or "fix pushed",
            artifacts=OutcomeArtifacts(pr_number=getattr(result, "pr_number", None)),
            details=details,
        )
    )
    return 0
