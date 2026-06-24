"""STATE_REGISTRY — name → factory mapping for state revival from SQLite.

The Poller and CLI both need to instantiate the right concrete state class
from a stored ``current_state`` string. This is the only place that mapping
lives; updating it is a single edit when new states are added.
"""

from __future__ import annotations

from collections.abc import Callable

from foreman.v4.state import TicketState
from foreman.v4.states.impl_approved import ImplApprovedState
from foreman.v4.states.impl_fix import ImplFixState
from foreman.v4.states.impl_review import ImplReviewState
from foreman.v4.states.implementing import ImplementingState
from foreman.v4.states.merging import MergingState
from foreman.v4.states.planning import PlanningState
from foreman.v4.states.queued import QueuedState
from foreman.v4.states.spec_fix import SpecFixState
from foreman.v4.states.spec_merging import SpecMerging
from foreman.v4.states.spec_review import SpecReviewState
from foreman.v4.states.terminal import DoneState, FailedState, NeedsHelpState

STATE_REGISTRY: dict[str, Callable[[], TicketState]] = {
    "Queued": QueuedState,
    "Planning": PlanningState,
    "SpecReview": SpecReviewState,
    "SpecFix": SpecFixState,
    "SpecMerging": SpecMerging,
    "Implementing": ImplementingState,
    "ImplReview": ImplReviewState,
    "ImplFix": ImplFixState,
    "ImplApproved": ImplApprovedState,
    "Merging": MergingState,
    "Done": DoneState,
    "Failed": FailedState,
    "NeedsHelp": NeedsHelpState,
}


def build_state(name: str) -> TicketState:
    """Return a fresh instance of the named state.

    Raises KeyError if the name is unknown — that's a schema-evolution
    invariant violation (someone added a state without updating the registry).
    """
    return STATE_REGISTRY[name]()
