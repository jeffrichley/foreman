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

import os
import re
import subprocess
from importlib import resources
from pathlib import Path

from github import Github
from github.Repository import Repository

from foreman.config import Config
from foreman.identity import IdentityRegistry
from foreman.provider import ProviderFacade
from foreman.schemas.reviewer import ReviewerOutput
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
    """Load the reviewer system prompt from packaged resources."""
    return (
        resources.files("foreman.prompts").joinpath("reviewer.md").read_text(encoding="utf-8")
    )


def _build_user_prompt(
    *,
    issue_title: str,
    issue_body: str,
    pr_title: str,
    pr_body: str,
    spec_doc_content: str | None,
    pr_diff: str,
) -> str:
    """Compose the per-run user prompt.

    The Reviewer needs the issue (ground truth) and the spec PR's artifact
    (the spec doc + PR body) plus the actual diff so it can verify file-level
    claims. The spec doc may be embedded directly when available; otherwise
    the Reviewer reads it from the worktree via its Read tool.
    """
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
        RuntimeError: PR is missing the ``foreman:spec-review`` label —
            we refuse to advance PRs that were not queued for review.
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

    pr_labels = {label.name for label in pr.labels}
    if _LABEL_IN_REVIEW not in pr_labels:
        raise RuntimeError(
            f"PR #{pr_number} does not carry the {_LABEL_IN_REVIEW!r} label "
            "(labels: " + ", ".join(sorted(pr_labels) or ["<none>"]) + "). "
            "The Reviewer only acts on PRs queued via the Planner."
        )

    head_branch = pr.head.ref
    head_sha = pr.head.sha
    base_branch = pr.base.ref
    issue_number = _issue_number_from_branch(head_branch)

    issue = repo.get_issue(issue_number)
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

    system_prompt = _load_reviewer_prompt()
    user_prompt = _build_user_prompt(
        issue_title=issue_title,
        issue_body=issue_body,
        pr_title=pr.title or "",
        pr_body=pr.body or "",
        spec_doc_content=spec_doc_content,
        pr_diff=pr_diff,
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
    pr.create_review(body=llm_output.review_comment, event="COMMENT")

    # Advance the originating ISSUE's label (not the PR's) — same pattern
    # the Planner uses.
    if llm_output.outcome == "clean":
        add_label = _LABEL_SPEC_READY
    else:
        add_label = _LABEL_SPEC_FIX

    issue.remove_from_labels(_LABEL_IN_REVIEW)
    issue.add_to_labels(add_label)

    return llm_output
