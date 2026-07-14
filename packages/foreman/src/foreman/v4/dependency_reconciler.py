"""Dependency reconciler — pure logic for computing unmet blocked_by deps.

A dep is "met" iff GitHub reports ``state_reason == "completed"`` for the
blocking issue.  Any other state (open, closed-not-planned, closed-reopened,
or never closed) leaves the dep unmet and blocking dispatch.

No repository writes; no side effects.  The caller (Task 4: Poller) is
responsible for persisting the result via ``repo.set_ticket_dependencies``.
"""

from __future__ import annotations

from foreman.v4.git_provider import GitProvider


def compute_unmet_dependencies(
    *,
    project: str,
    issue_number: int,
    provider: GitProvider,
) -> list[int]:
    """Return the subset of *blocked_by* whose dep issue is not closed-as-completed.

    Queries ``provider.read_blocked_by`` for the full blocker list, then
    filters to those where ``provider.get_issue_state_reason`` does NOT
    equal ``"completed"``.  Input order is preserved.

    Args:
        project: The foreman project name (e.g. ``"agent_core"``).
        issue_number: The GitHub issue number being evaluated.
        provider: A :class:`~foreman.v4.git_provider.GitProvider` instance.

    Returns:
        A list of issue numbers that are currently blocking dispatch —
        i.e. the blocked_by entries that have not been closed as completed.
    """
    blocked_by = provider.read_blocked_by(project=project, issue_number=issue_number)
    return [
        dep
        for dep in blocked_by
        if provider.get_issue_state_reason(project=project, issue_number=dep) != "completed"
    ]
