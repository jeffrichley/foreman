"""Pure-function state machine for the Foreman daemon.

Given a ticket's labels (and its project's config), returns the next action
the daemon should take — or None if the ticket is parked (hold, failed, or
awaiting human action).

No I/O. No side effects. No time dependence. The single source of truth for
"what should happen next" — every drift between intent and behavior lives
here, not scattered across the daemon.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ActionKind(Enum):
    """One of the eight things the daemon can do to a ticket."""

    RUN_PLANNER = "run_planner"
    RUN_REVIEWER_SPEC = "run_reviewer_spec"
    RUN_FIXER_SPEC = "run_fixer_spec"
    MERGE_SPEC_PR = "merge_spec_pr"
    RUN_WORKER = "run_worker"
    RUN_REVIEWER_IMPL = "run_reviewer_impl"
    RUN_FIXER_IMPL = "run_fixer_impl"
    MERGE_IMPL_PR = "merge_impl_pr"


@dataclass(frozen=True)
class Action:
    """An action returned by ``next_action`` for the worker to dispatch."""

    kind: ActionKind


@dataclass(frozen=True)
class Ticket:
    """A snapshot of an issue's state at one point in time.

    Frozen + hashable so it can be a dict key in the queue's dedup map.
    """

    project_name: str
    issue_number: int
    labels: frozenset[str]
    last_transition_at: datetime


_STAGE_ORDER: dict[ActionKind, int] = {
    ActionKind.RUN_PLANNER: 1,
    ActionKind.RUN_REVIEWER_SPEC: 2,
    ActionKind.RUN_FIXER_SPEC: 3,
    ActionKind.MERGE_SPEC_PR: 4,
    ActionKind.RUN_WORKER: 5,
    ActionKind.RUN_REVIEWER_IMPL: 6,
    ActionKind.RUN_FIXER_IMPL: 7,
    ActionKind.MERGE_IMPL_PR: 8,
}


def stage_index(kind: ActionKind) -> int:
    """Return the pipeline-progression index of an action.

    Higher = further along. Used as the primary sort key in queue dequeue
    so further-along tickets win — driving "first ticket merges fastest"
    behavior (see spec §2).
    """
    return _STAGE_ORDER[kind]


# Labels that, when present, halt the daemon from taking new actions on the
# ticket. ``foreman:hold`` is operator-toggled; ``foreman:failed`` is added
# by the daemon on crash / role error and only cleared by an operator.
#
# Forward-compat (v2): is_blocked() will additionally check for
# ``foreman:blocked-by:#N`` labels pointing at non-terminal tickets.
_BLOCKING_LABELS = frozenset({"foreman:hold", "foreman:failed"})


def is_blocked(ticket: Ticket) -> bool:
    """Return True if the ticket should be skipped by the worker.

    A blocked ticket stays in the queue but ``next_action`` returns None,
    so the worker moves on to the next item without dispatching anything.
    """
    return bool(ticket.labels & _BLOCKING_LABELS)
