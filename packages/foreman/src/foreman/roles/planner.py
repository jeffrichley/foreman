"""Planner role dispatcher.

Walks through:
  1. Parse the issue URL (owner / repo / number)
  2. Resolve planner identity → PyGithub client
  3. Fetch issue body + title
  4. Create per-ticket worktree
  5. Load planner system prompt
  6. Dispatch via provider with scoped tools + structured output schema
  7. Parse PlannerOutput
  8. Advance label: foreman:plan → foreman:spec-review
  9. Return PlannerOutput

For walking skeleton: cleanup is NOT performed automatically (per spec —
worktrees persist until pipeline completion, which here is the human merging
the PR).
"""

from __future__ import annotations

import os
import re
from importlib import resources
from pathlib import Path

from foreman.config import Config
from foreman.identity import IdentityRegistry
from foreman.provider import ProviderFacade
from foreman.schemas.planner import PlannerOutput
from foreman.worktree import WorktreeManager

_ISSUE_URL_RE = re.compile(
    r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)"
)

# Tool capabilities matrix for Planner (from architectural spec §4.1)
PLANNER_ALLOWED_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]


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


def _build_user_prompt(*, issue_title: str, issue_body: str, issue_number: int) -> str:
    return (
        f"You are processing GitHub issue #{issue_number}.\n\n"
        f"## Title\n{issue_title}\n\n"
        f"## Body\n{issue_body}\n\n"
        f"Follow the steps in your system prompt. Return your structured output "
        f"when done."
    )


async def run_planner(
    *,
    issue_url: str,
    config: Config,
    project_name: str,
    worktrees_root: Path,
    provider: ProviderFacade,
) -> PlannerOutput:
    """Run the Planner role end-to-end on one issue."""
    owner, repo_name, issue_number = parse_issue_url(issue_url)
    project = config.projects[project_name]
    expected_repo_slug = project.repo  # e.g. "jeffrichley/voice"
    actual_repo_slug = f"{owner}/{repo_name}"
    if expected_repo_slug != actual_repo_slug:
        raise ValueError(
            f"Issue URL repo {actual_repo_slug!r} does not match project "
            f"{project_name!r} configured repo {expected_repo_slug!r}"
        )

    identity = IdentityRegistry(project)
    gh = identity.get_client("planner")
    repo = gh.get_repo(actual_repo_slug)
    issue = repo.get_issue(issue_number)

    # Resolve planner-bot token for injection into the agent subprocess so
    # the agent's `gh` CLI calls (e.g., `gh pr create`) run as the bot,
    # not as whatever GH_TOKEN the parent foreman process inherited.
    # Without this, the spec PR would open under Jeff's identity, not
    # @foreman-planner-bot — violating the bot-attribution success criterion.
    planner_token = identity.get_token("planner")
    agent_env = {**os.environ, "GH_TOKEN": planner_token}

    wt_mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt_path = wt_mgr.create(
        clone_path=Path(project.local_clone_path),
        repo_slug=repo_name,
        ticket_id=issue_number,
    )

    system_prompt = _load_planner_prompt()
    user_prompt = _build_user_prompt(
        issue_title=issue.title,
        issue_body=issue.body or "",
        issue_number=issue_number,
    )

    raw = await provider.run_agent(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        allowed_tools=PLANNER_ALLOWED_TOOLS,
        output_schema=PlannerOutput.model_json_schema(),
        cwd=wt_path,
        env=agent_env,
    )
    output = PlannerOutput.model_validate(raw)

    # Advance label: foreman:plan → foreman:spec-review
    issue.remove_from_labels("foreman:plan")
    issue.add_to_labels("foreman:spec-review")

    return output
