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
  5. Configure the worktree git identity (``host.configure_worktree_identity``)
  6. Build LLM context and dispatch with READ-ONLY tools
  7. Parse the ``PlannerOutput``
  8. Commit the spec doc (``host.commit_files_to_worktree``)
  9. Push the branch (``host.push_branch``)
  10. Open the spec PR (``host.open_pull_request``)
  11. Advance the label: ``foreman:plan`` → ``foreman:spec-review``
       (``host.update_issue_labels``)
  12. Return :class:`~foreman.schemas.planner.PlannerRunResult`

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
from importlib import resources
from pathlib import Path

from foreman.config import Config
from foreman.git_host import GitHostProvider, IssueRef
from foreman.identity import IdentityRegistry
from foreman.instructions import load_project_instructions
from foreman.provider import ProviderFacade
from foreman.schemas.planner import PlannerOutput, PlannerRunResult
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
    """Load the planner system prompt from packaged resources."""
    return (
        resources.files("foreman.prompts").joinpath("planner.md").read_text(encoding="utf-8")
    )


def _build_user_prompt(*, issue: IssueRef, instructions: str | None) -> str:
    """Compose the per-run user prompt.

    ``instructions`` carries the verbatim contents of the project's
    ``.foreman/INSTRUCTIONS.md`` when present; the section is emitted
    near the top so project-specific conventions (PR title rules, branch
    conventions, etc.) frame everything that follows. When ``None`` the
    section is omitted entirely — empty headers would be a distracting
    no-op the LLM would have to mentally skip.
    """
    instructions_section = (
        f"## Project-specific instructions\n\n{instructions}\n\n"
        if instructions
        else ""
    )
    return (
        f"You are processing GitHub issue #{issue.number}.\n\n"
        f"{instructions_section}"
        f"## Title\n{issue.title}\n\n"
        f"## Body\n{issue.body}\n\n"
        f"Follow the steps in your system prompt. Return your structured "
        f"output when done."
    )


def _spec_doc_relpath(issue_number: int) -> str:
    return f"docs/superpowers/specs/foreman-issue-{issue_number}-spec.md"


async def run_planner(
    *,
    issue_url: str,
    config: Config,
    project_name: str,
    worktrees_root: Path,
    provider: ProviderFacade,
    identity_registry: IdentityRegistry | None = None,
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
    host: GitHostProvider = registry.get_host_provider("planner")

    issue = host.get_issue(actual_repo_slug, issue_number)
    default_branch = host.get_default_branch(actual_repo_slug)

    wt_mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt_path = wt_mgr.create(
        clone_path=Path(project.local_clone_path),
        repo_slug=repo_name,
        ticket_id=issue_number,
        dev_base_branch=project.dev_base_branch,
    )
    host.configure_worktree_identity(wt_path)

    instructions = load_project_instructions(Path(project.local_clone_path))

    system_prompt = _load_planner_prompt()
    user_prompt = _build_user_prompt(issue=issue, instructions=instructions)

    llm_output = await provider.run_agent(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        allowed_tools=PLANNER_ALLOWED_TOOLS,
        output_model=PlannerOutput,
        cwd=wt_path,
    )

    branch = f"foreman/issue-{issue_number}"
    host.commit_files_to_worktree(
        worktree_path=wt_path,
        files={_spec_doc_relpath(issue_number): llm_output.spec_doc_content},
        message=llm_output.pr_title,
    )
    host.push_branch(worktree_path=wt_path, branch=branch)
    pr = host.open_pull_request(
        repo_slug=actual_repo_slug,
        title=llm_output.pr_title,
        body=llm_output.pr_body,
        base=default_branch,
        head=branch,
    )
    host.update_issue_labels(
        repo_slug=actual_repo_slug,
        issue_number=issue_number,
        add=["foreman:spec-review"],
        remove=["foreman:plan"],
    )

    return PlannerRunResult(llm_output=llm_output, pr=pr)
