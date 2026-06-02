"""Reviewer role dispatcher.

The Reviewer LLM reads an already-open spec PR (the Planner's output) and
returns a :class:`~foreman.schemas.reviewer.ReviewerOutput`. Foreman core
then:

  1. Posts the ``review_comment`` as a PR review (``event="COMMENT"``)
  2. Advances the **issue's** label deterministically:
     - ``clean``     → ``foreman:spec-review`` → ``foreman:spec-ready``
     - ``needs_fix`` → ``foreman:spec-review`` → ``foreman:spec-fix``
  3. Returns :class:`~foreman.schemas.reviewer.ReviewerOutput` to the caller
     for display / persistence

The label transition is on the originating ISSUE, not the spec PR — same
pattern the Planner uses. The Reviewer derives the issue number from the
PR's head branch (the Planner names branches ``foreman/issue-<N>``).

Pre-flight guard: if the PR does not carry the ``foreman:spec-review``
label, the orchestrator raises before doing any work — we will not
silently advance a PR that was not queued for review.

The Reviewer LLM is read-only on the filesystem (Read / Glob / Grep) plus
Bash for shell-level recon (e.g., ``gh pr view`` if it needs more context).
All host mutations (review post, label advance) happen in core via the
PyGithub client. This mirrors the Planner's "LLM is host-agnostic; core is
deterministic" split.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from github import Github
from github.Repository import Repository

from foreman.config import Config
from foreman.identity import IdentityRegistry
from foreman.instructions import load_project_instructions
from foreman.provider import ProviderFacade
from foreman.schemas.reviewer import Finding, ReviewerOutput
from foreman.worktree import WorktreeManager

_PR_URL_RE = re.compile(
    r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)
_BRANCH_ISSUE_RE = re.compile(r"^foreman/issue-(?P<number>\d+)$")

# Tool capabilities matrix for the Reviewer. Read-only on the filesystem;
# Bash is allowed for read-only recon (e.g., ``gh pr view``, ``git log``).
# Pinning this here prevents accidental ``Edit`` / ``Write`` reintroduction.
REVIEWER_ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Bash"]

# Labels the Reviewer touches on the originating issue.
_LABEL_IN_REVIEW = "foreman:spec-review"
_LABEL_SPEC_READY = "foreman:spec-ready"
_LABEL_SPEC_FIX = "foreman:spec-fix"

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


def _issue_number_from_branch(branch: str) -> int:
    """Derive the originating issue number from a ``foreman/issue-<N>`` head.

    The Planner names spec-PR branches ``foreman/issue-<N>`` (see
    :mod:`foreman.roles.planner`). The Reviewer's PR URL doesn't carry the
    issue number, but the branch does — so we read it from there.
    """
    m = _BRANCH_ISSUE_RE.match(branch)
    if not m:
        raise ValueError(
            f"PR head branch {branch!r} is not a Foreman spec branch "
            "(expected 'foreman/issue-<N>'). The Reviewer only acts on "
            "Planner-produced spec PRs."
        )
    return int(m["number"])


def _load_reviewer_prompt() -> str:
    """Load the Reviewer system prompt: vendored ``requesting-code-review``
    followed by the Foreman-specific Reviewer contract.

    Inlining superpowers' code-review framing gives the SDK-driven
    Reviewer the same structure interactive Claude Code uses when asked
    to review a PR (clear severity tiers, file:line citations,
    actionable findings vs nits).
    """
    from foreman.prompts import compose_role_prompt

    return compose_role_prompt(
        role="reviewer", superpowers=["requesting-code-review"]
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


def _get_pr_diff(worktree_path: Path, base_branch: str, head_sha: str) -> str:
    """Return the unified diff for the PR's head against its base branch.

    Uses ``git diff`` in the worktree rather than the GitHub Files API so
    we don't pay round-trips for large PRs and so the diff matches whatever
    the worktree has checked out (which the LLM will read from with Read /
    Grep / Glob).
    """
    # Ensure we have the base ref locally — the PR's base is typically the
    # repo default (``main``), which the clone already tracks. Tolerate
    # fetch failure; the diff command below will surface a clearer error.
    subprocess.run(
        ["git", "fetch", "origin", base_branch],
        cwd=worktree_path,
        check=False,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        ["git", "diff", f"origin/{base_branch}...{head_sha}"],
        cwd=worktree_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


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
) -> ReviewerOutput:
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
        The :class:`~foreman.schemas.reviewer.ReviewerOutput` produced by
        the LLM. The CLI surfaces ``outcome`` / ``findings`` / ``confidence``
        for human inspection.

    Raises:
        ValueError: PR URL malformed, repo mismatch, or PR head branch is
            not a Foreman spec branch.
        RuntimeError: Source issue is missing the ``foreman:spec-review``
            label — we refuse to advance PRs whose source issue was not
            queued for review.
    """
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
    issue_number = _issue_number_from_branch(head_branch)

    issue = repo.get_issue(issue_number)
    issue_labels = {label.name for label in issue.labels}
    if _LABEL_IN_REVIEW not in issue_labels:
        raise RuntimeError(
            f"Issue #{issue_number} (source of PR #{pr_number}) does not carry "
            f"the {_LABEL_IN_REVIEW!r} label (labels: "
            + ", ".join(sorted(issue_labels) or ["<none>"])
            + "). The Reviewer only acts on issues queued via the Planner."
        )

    issue_title = issue.title or ""
    issue_body = issue.body or ""

    wt_mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt_path = wt_mgr.attach(
        clone_path=Path(project.local_clone_path),
        repo_slug=repo_name,
        ticket_id=issue_number,
    )

    pr_diff = _get_pr_diff(wt_path, base_branch=base_branch, head_sha=head_sha)
    spec_doc_content = _read_spec_doc(wt_path, issue_number)
    instructions = load_project_instructions(Path(project.local_clone_path))

    system_prompt = _load_reviewer_prompt()
    user_prompt = _build_user_prompt(
        issue_title=issue_title,
        issue_body=issue_body,
        pr_title=pr.title or "",
        pr_body=pr.body or "",
        spec_doc_content=spec_doc_content,
        pr_diff=pr_diff,
        instructions=instructions,
    )

    llm_output = await provider.run_agent(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        allowed_tools=REVIEWER_ALLOWED_TOOLS,
        output_model=ReviewerOutput,
        cwd=wt_path,
        env={"GH_TOKEN": reviewer_token, **os.environ},
    )

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
        add_label = _LABEL_SPEC_READY
    else:
        add_label = _LABEL_SPEC_FIX

    issue.remove_from_labels(_LABEL_IN_REVIEW)
    issue.add_to_labels(add_label)

    return llm_output
