"""Fixer role dispatcher.

The Fixer LLM consumes the Reviewer's findings on a spec PR, applies
addressable edits to the spec doc, commits + pushes, and returns a
:class:`~foreman.schemas.fixer.FixerOutput`. Foreman core then:

  1. Posts ``fix_comment`` as a PR **comment** (not a review) via
     :meth:`PullRequest.create_issue_comment` — the Fixer is not
     re-reviewing.
  2. Advances the originating issue's label deterministically:
     - ``fixed``      → remove ``foreman:spec-fix``, add
       ``foreman:spec-review`` (back to Reviewer)
     - ``incomplete`` → keep ``foreman:spec-fix``, add
       ``foreman:needs-help``; if attempt == 3, ALSO add
       ``foreman:failed``
  3. Appends a JSONL line to ``~/.foreman/stats/<repo>/fixer.jsonl``
     for lifecycle stats (proto for foreman#11).
  4. Returns :class:`~foreman.schemas.fixer.FixerOutput` to the caller.

The fix-attempt counter is tracked via the issue's
``foreman:fix-attempt-N`` labels (set on entry, never removed for
audit). Max 3 attempts; the 4th raises before any LLM dispatch.

The Fixer's tool surface includes Edit + Write (it edits the spec
doc) plus Read / Grep / Glob / Bash. Bash is needed so the LLM can
``git add`` + ``git commit`` + ``git push`` directly — Foreman core's
host abstraction is read+post-only here; the commit machinery lives in
the LLM's hands inside the worktree, because the LLM is the one deciding
which edits to bundle into which commit per the prompt's discipline.

Pre-flight guards: if the issue is missing ``foreman:spec-fix`` we
refuse to run; if the issue already has 3 ``foreman:fix-attempt-*``
labels, we refuse to run with a clear "needs human intervention"
RuntimeError. Both raise before any LLM dispatch — cheap deterministic
checks.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from importlib import resources
from pathlib import Path

from github import Github
from github.PullRequest import PullRequest
from github.Repository import Repository
from pydantic import ValidationError

from foreman.config import Config
from foreman.identity import IdentityRegistry
from foreman.provider import ProviderFacade
from foreman.roles.reviewer import FINDINGS_BEGIN_MARKER, FINDINGS_END_MARKER
from foreman.schemas.fixer import FixerOutput, FixerRunResult
from foreman.schemas.reviewer import Finding
from foreman.stats import log_fixer_run
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
_FIX_ATTEMPT_RE = re.compile(r"^foreman:fix-attempt-(\d+)$")

# Tool capabilities matrix for the Fixer. Edit + Write so it can modify
# the spec doc; Bash so it can stage / commit / push from inside the
# worktree. Read / Grep / Glob for verification before committing. The
# matrix is wider than Reviewer's by design — Fixer is the only role in
# the walking skeleton that mutates the worktree directly.
FIXER_ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Bash", "Edit", "Write"]

# Labels the Fixer touches on the originating issue.
_LABEL_SPEC_FIX = "foreman:spec-fix"
_LABEL_SPEC_REVIEW = "foreman:spec-review"
_LABEL_NEEDS_HELP = "foreman:needs-help"
_LABEL_FAILED = "foreman:failed"
_MAX_FIX_ATTEMPTS = 3


def parse_issue_url(url: str) -> tuple[str, str, int]:
    """Extract ``(owner, repo, issue_number)`` from a GitHub issue URL.

    The Fixer is triggered by a label on the ISSUE, not the PR — the
    spec PR is derived from the issue's ``foreman/issue-<N>`` branch.
    """
    m = _ISSUE_URL_RE.match(url.strip())
    if not m:
        raise ValueError(f"Not a GitHub issue URL: {url!r}")
    return m["owner"], m["repo"], int(m["number"])


def _count_fix_attempts(label_names: set[str]) -> int:
    """Return the highest existing fix-attempt counter (0 if none).

    Reads existing ``foreman:fix-attempt-N`` labels and returns the
    largest N. The new attempt is ``_count_fix_attempts(...) + 1``.
    """
    attempts: list[int] = []
    for name in label_names:
        m = _FIX_ATTEMPT_RE.match(name)
        if m:
            attempts.append(int(m.group(1)))
    return max(attempts) if attempts else 0


def _load_fixer_prompt() -> str:
    """Load the fixer system prompt from packaged resources."""
    return resources.files("foreman.prompts").joinpath("fixer.md").read_text(encoding="utf-8")


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
        _log.warning(
            "Failed to parse Reviewer findings JSON block: %s; raw=%r", exc, raw
        )
        return []
    if not isinstance(payload, list):
        _log.warning(
            "Reviewer findings JSON block was not a list: type=%s", type(payload).__name__
        )
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
                f"- **target**: {f.target}\n"
                f"  - **issue**: {f.issue}\n"
                f"  - **needed**: {f.needed}\n"
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
) -> str:
    """Compose the per-run user prompt.

    The Fixer needs the issue (ground truth), the spec PR's artifact
    (spec doc + PR body), the Reviewer's review_comment (context), and
    the structured findings (the contract). Findings are rendered BOTH
    as markdown (for reading order) AND as JSON (for unambiguous
    targeting) — the LLM uses the JSON when emitting the structured
    output, the markdown when reading.
    """
    spec_section = (
        f"## Spec doc (currently committed in this PR)\n"
        f"```markdown\n{spec_doc_content}\n```\n\n"
        if spec_doc_content
        else (
            "## Spec doc\nNot inlined — read it from the worktree using "
            "the Read tool at the path the PR body references.\n\n"
        )
    )
    return (
        f"You are fixing Reviewer findings on spec PR. This is fix attempt "
        f"#{attempt} of a maximum of {_MAX_FIX_ATTEMPTS}.\n\n"
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
        worktree_path
        / "docs"
        / "superpowers"
        / "specs"
        / f"foreman-issue-{issue_number}-spec.md"
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


async def run_fixer(
    *,
    issue_url: str,
    config: Config,
    project_name: str,
    worktrees_root: Path,
    provider: ProviderFacade,
    identity_registry: IdentityRegistry | None = None,
) -> FixerRunResult:
    """Run the Fixer role end-to-end on one issue.

    Args:
        issue_url: Full GitHub ISSUE URL
            (``https://github.com/owner/repo/issues/N``) — NOT the spec
            PR URL. The Fixer is triggered by the
            ``foreman:spec-fix`` label on the issue and derives the PR
            from the issue's ``foreman/issue-<N>`` branch.
        config: Loaded foreman config.
        project_name: Key into ``config.projects``.
        worktrees_root: Root directory under which per-ticket worktrees
            live.
        provider: Agent provider facade (e.g., AnthropicSDKProvider).
        identity_registry: Optional pre-built registry; defaults to a
            fresh :class:`~foreman.identity.IdentityRegistry` for the
            project. Tests inject a fake registry to bypass real App
            auth.

    Returns:
        A :class:`~foreman.schemas.fixer.FixerRunResult` bundling the
        Fixer LLM's :class:`~foreman.schemas.fixer.FixerOutput` and the
        ``attempt`` counter (the run-number stamped on the issue at
        entry). The CLI surfaces both for the one-line summary.

    Raises:
        ValueError: Issue URL malformed or repo mismatch.
        RuntimeError: Issue missing ``foreman:spec-fix``, or max
            attempts (3) already reached, or no open spec PR / no
            Reviewer review found.
    """
    owner, repo_name, issue_number = parse_issue_url(issue_url)
    project = config.projects[project_name]
    expected_repo_slug = project.repo
    actual_repo_slug = f"{owner}/{repo_name}"
    if expected_repo_slug != actual_repo_slug:
        raise ValueError(
            f"Issue URL repo {actual_repo_slug!r} does not match project "
            f"{project_name!r} configured repo {expected_repo_slug!r}"
        )

    registry = identity_registry if identity_registry is not None else IdentityRegistry(project)
    fixer_client: Github = registry.get_fixer_client()
    fixer_token: str = registry.get_fixer_token()

    repo: Repository = fixer_client.get_repo(actual_repo_slug)
    issue = repo.get_issue(issue_number)
    issue_labels = {label.name for label in issue.labels}

    # Pre-flight: refuse to run without the entry-condition label.
    if _LABEL_SPEC_FIX not in issue_labels:
        raise RuntimeError(
            f"Issue #{issue_number} does not carry the {_LABEL_SPEC_FIX!r} "
            f"label (labels: " + ", ".join(sorted(issue_labels) or ["<none>"])
            + "). The Fixer only acts on issues queued by the Reviewer."
        )

    # Pre-flight: max-3-attempts gate. If 3 fix-attempt labels already
    # exist, refuse to run — this prevents infinite-loop drain and
    # forces human intervention via the foreman:failed escalation.
    previous_attempts = _count_fix_attempts(issue_labels)
    attempt = previous_attempts + 1
    if attempt > _MAX_FIX_ATTEMPTS:
        raise RuntimeError(
            f"Issue #{issue_number} has hit the max {_MAX_FIX_ATTEMPTS} "
            "fix-attempts; needs human intervention via foreman:failed. "
            f"Existing attempts: {previous_attempts}."
        )

    # Stamp the new attempt label IMMEDIATELY so it's visible even if
    # the LLM dispatch crashes mid-run. Audit-trail before audit-loss.
    attempt_label = f"foreman:fix-attempt-{attempt}"
    issue.add_to_labels(attempt_label)

    # Resolve the spec PR from the issue's branch convention.
    branch = f"foreman/issue-{issue_number}"
    pr = _find_spec_pr(repo, owner=owner, branch=branch)
    review_comment = _latest_reviewer_review_comment(pr)

    # Attach to the existing branch — the Planner created it; the
    # Fixer must not branch from main.
    wt_mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt_path = wt_mgr.attach(
        clone_path=Path(project.local_clone_path),
        repo_slug=repo_name,
        ticket_id=issue_number,
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

    system_prompt = _load_fixer_prompt()
    user_prompt = _build_user_prompt(
        issue_title=issue.title or "",
        issue_body=issue.body or "",
        pr_title=pr.title or "",
        pr_body=pr.body or "",
        spec_doc_content=spec_doc_content,
        review_comment=review_comment,
        findings=findings,
        attempt=attempt,
    )

    start_time = time.monotonic()
    llm_output = await provider.run_agent(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        allowed_tools=FIXER_ALLOWED_TOOLS,
        output_model=FixerOutput,
        cwd=wt_path,
        env={"GH_TOKEN": fixer_token, **os.environ},
    )
    duration_seconds = time.monotonic() - start_time

    # Post the fix summary as a PR COMMENT, not a review. The Fixer is
    # acting on the Reviewer's feedback; reviews come from Reviewers.
    pr.create_issue_comment(body=llm_output.fix_comment)

    # Advance the issue's label deterministically.
    if llm_output.outcome == "fixed":
        # Back to the Reviewer for a second pass. Per-episode counter
        # reset: clear all fix-attempt-N labels (and needs-help if
        # present) since this fix-episode closed cleanly. Each new
        # spec-fix → spec-review cycle gets a fresh 3-attempt budget.
        # Lifecycle stats JSONL preserves the cumulative audit trail;
        # labels reflect current-cycle state only.
        issue.remove_from_labels(_LABEL_SPEC_FIX)
        issue.add_to_labels(_LABEL_SPEC_REVIEW)
        all_known_labels = issue_labels | {attempt_label}
        for label_name in all_known_labels:
            if label_name.startswith("foreman:fix-attempt-") or label_name == _LABEL_NEEDS_HELP:
                try:
                    issue.remove_from_labels(label_name)
                except Exception:
                    pass  # label may already be absent on the GitHub side
    else:
        # incomplete: keep spec-fix so the human (or a later daemon
        # pass) can re-trigger; flag for help; if last attempt, also
        # add the failed escalation.
        issue.add_to_labels(_LABEL_NEEDS_HELP)
        if attempt == _MAX_FIX_ATTEMPTS:
            issue.add_to_labels(_LABEL_FAILED)

    # JSONL stats — write regardless of outcome.
    unaddressed_hist = _unaddressed_by_reason_histogram(llm_output)
    disagreed_count = unaddressed_hist.get("needed_remediation_wrong", 0)
    log_fixer_run(
        repo_slug=actual_repo_slug,
        issue_number=issue_number,
        pr_number=pr.number,
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
    )

    return FixerRunResult(llm_output=llm_output, attempt=attempt)
