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


def find_cycles(edges: dict[int, list[int]]) -> list[list[int]]:
    """Return all cycles in a directed dependency graph over tracked tickets.

    Uses iterative DFS with three-colour marking (white/grey/black).  Each
    cycle is returned as a *sorted* list of the issue numbers it contains,
    and the outer list is sorted so output is deterministic regardless of
    dict iteration order.

    Args:
        edges: Mapping ``{issue_number: [blocked_by issue_numbers...]}``.
               Only issue numbers present as keys are considered tracked;
               edges pointing outside this set are ignored for cycle
               detection (an untracked dep can hold a ticket but cannot
               form a detectable cycle in this graph).

    Returns:
        A list of cycles, each cycle a sorted ``list[int]`` of issue
        numbers.  The outer list is sorted by the first element of each
        cycle.  Returns an empty list when no cycles exist.
    """
    # Three-colour DFS state.
    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[int, int] = {node: WHITE for node in edges}

    # Collect unique cycles as frozensets so duplicates (same set reached
    # from different entry points) are deduplicated before normalisation.
    cycle_sets: list[frozenset[int]] = []
    seen_cycle_sets: set[frozenset[int]] = set()

    tracked = set(edges)

    for start in sorted(edges):
        if colour[start] != WHITE:
            continue

        # Iterative DFS: stack holds (node, iterator-over-neighbours).
        path: list[int] = []
        path_set: set[int] = set()
        stack: list[tuple[int, int]] = [(start, 0)]  # (node, neighbour_index)
        colour[start] = GREY
        path.append(start)
        path_set.add(start)

        while stack:
            node, idx = stack[-1]
            neighbours = [n for n in edges[node] if n in tracked]

            if idx < len(neighbours):
                stack[-1] = (node, idx + 1)
                neighbour = neighbours[idx]

                if colour[neighbour] == GREY:
                    # Found a back-edge → cycle.
                    cycle_start_idx = path.index(neighbour)
                    cycle_nodes = frozenset(path[cycle_start_idx:])
                    if cycle_nodes not in seen_cycle_sets:
                        seen_cycle_sets.add(cycle_nodes)
                        cycle_sets.append(cycle_nodes)
                elif colour[neighbour] == WHITE:
                    colour[neighbour] = GREY
                    path.append(neighbour)
                    path_set.add(neighbour)
                    stack.append((neighbour, 0))
                # BLACK → already fully processed; skip.
            else:
                # All neighbours explored — pop and mark black.
                stack.pop()
                colour[node] = BLACK
                path.pop()
                path_set.discard(node)

    # Normalise: each cycle sorted, outer list sorted by first element.
    result = [sorted(c) for c in cycle_sets]
    result.sort()
    return result
