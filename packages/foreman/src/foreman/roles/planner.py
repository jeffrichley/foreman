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
  11. Best-effort cleanup of the legacy ``foreman:plan`` entry label;
       the issue stays at ``foreman:planning`` so v3's reconciler
       (``dispatch_reviewer_spec``) can fire on ``foreman:planning`` +
       open spec PR (``host.update_issue_labels``)
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
from pathlib import Path

from foreman.branches import spec_branch
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
    """Load the Planner system prompt: vendored ``writing-plans`` followed
    by the Foreman-specific Planner contract.

    The discipline that makes superpowers' interactive Claude Code write
    rigorous, bite-sized plans is inlined here so the SDK-driven Planner
    role sees the same instructions. See
    :func:`foreman.prompts.compose_role_prompt` for the composition
    details.
    """
    from foreman.prompts import compose_role_prompt

    return compose_role_prompt(role="planner", superpowers=["writing-plans"])


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
        f"## Project-specific instructions\n\n{instructions}\n\n" if instructions else ""
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


# foreman#63: GitHub auto-closes the originating issue at merge time when
# a merged PR's body contains one of the nine "closing keywords" + a
# ``#N`` or ``owner/repo#N`` reference. The Planner produces spec PR
# bodies; issue closure must route through ``daemon_runners.merge_impl_pr``
# instead (which fires only after the Reviewer-on-impl approves). This
# regex matches the verb + optional ``:`` separator + whitespace, with a
# lookahead that preserves the bare issue reference so the body still
# reads cleanly after substitution.
_AUTO_CLOSE_KEYWORDS_RE = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\b\s*:?\s+"
    r"(?=(?:[\w.-]+/[\w.-]+)?#\d+)"
)


def _strip_auto_close_keywords(body: str) -> str:
    """Remove GitHub auto-close keyword+separator prefixes from ``body``.

    GitHub auto-closes the originating issue when a merged PR's body
    contains any of nine "closing keywords" (close/closes/closed,
    fix/fixes/fixed, resolve/resolves/resolved) followed by a ``#N`` or
    ``owner/repo#N`` reference. The Planner produces spec PR bodies;
    issue closure must route through ``daemon_runners.merge_impl_pr``
    (foreman#63). This helper strips the verb+separator while
    preserving the bare ``#N`` reference so the body still reads
    cleanly. The helper is idempotent and a no-op on bodies that
    contain no auto-close keywords.
    """
    return _AUTO_CLOSE_KEYWORDS_RE.sub("", body)


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

    branch = spec_branch(issue_number)
    host.commit_files_to_worktree(
        worktree_path=wt_path,
        files={_spec_doc_relpath(issue_number): llm_output.spec_doc_content},
        message=llm_output.pr_title,
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
    # v3 label vocabulary: Planner writes ZERO new labels. The issue
    # stays at ``foreman:planning`` (the entry label) after the spec PR
    # opens; v3's reconciler ``dispatch_reviewer_spec`` rule then fires
    # on ``foreman:planning`` + an open spec PR. We DO remove the legacy
    # ``foreman:plan`` entry label if a v2-era ticket still carries it,
    # so the issue doesn't end up with both ``foreman:plan`` and
    # ``foreman:planning`` simultaneously.
    host.update_issue_labels(
        repo_slug=actual_repo_slug,
        issue_number=issue_number,
        add=[],
        remove=["foreman:plan"],
    )

    # foreman#91: compute final_labels deterministically from the
    # pre-mutation set (``issue.labels`` is the snapshot taken by
    # ``host.get_issue`` above) + the role's known transitions.
    # v3: only the legacy ``foreman:plan`` is dropped; ``foreman:planning``
    # is preserved so the reconciler can fire on it next tick.
    final_labels = sorted(set(issue.labels) - {"foreman:plan"})

    return PlannerRunResult(llm_output=llm_output, pr=pr, final_labels=final_labels)
