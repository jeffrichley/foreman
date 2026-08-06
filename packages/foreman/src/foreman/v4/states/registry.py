"""STATE_REGISTRY — name → factory mapping for state revival from the repository.

The Poller and CLI both need to instantiate the right concrete state class
from a stored ``current_state`` string. This is the only place that mapping
lives; updating it is a single edit when new states are added.
"""

from __future__ import annotations

from foreman.v4.state import TicketState
from foreman.v4.states.impl_approved import ImplApprovedState
from foreman.v4.states.impl_fix import ImplFixState
from foreman.v4.states.impl_review import ImplReviewState
from foreman.v4.states.implementing import ImplementingState
from foreman.v4.states.merge_queued import MergeQueuedState
from foreman.v4.states.merging import MergingState
from foreman.v4.states.planning import PlanningState
from foreman.v4.states.queued import QueuedState
from foreman.v4.states.spec_fix import SpecFixState
from foreman.v4.states.spec_merging import SpecMerging
from foreman.v4.states.spec_review import SpecReviewState
from foreman.v4.states.terminal import DoneState, FailedState, NeedsHelpState

# foreman#589: annotated as the concrete classes rather than bare
# ``Callable[[], TicketState]`` factories, so class-level declarations like
# ``dispatch_priority`` are visible to both the type checker and the
# import-time validator below. Every value here is (and must remain) a class.
STATE_REGISTRY: dict[str, type[TicketState]] = {
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
    # foreman#550: hand-off state after Merging/SpecMerging enqueue the PR;
    # coordinator-driven, excluded from QueueManager dequeue.
    "MergeQueued": MergeQueuedState,
    "Done": DoneState,
    "Failed": FailedState,
    "NeedsHelp": NeedsHelpState,
}


# foreman#589: every registered state must declare its own dispatch priority.
#
# The check is on ``cls.__dict__`` rather than ``getattr`` deliberately — an
# inherited value would mean the state got a priority nobody chose for it,
# which is the exact failure this replaces. ImplApproved was missing from the
# queue's old hand-maintained table and silently fell through to a sentinel
# 99, sorting behind freshly-Queued work; because the sentinel was a valid
# int, "nobody decided" was indistinguishable from "decided to run this last".
#
# Raising here makes the omission an import-time error: the daemon refuses to
# start rather than mis-ordering work at 3am.
def validate_dispatch_priorities(registry: dict[str, type[TicketState]]) -> None:
    """Raise if any state in ``registry`` does not declare its own priority.

    Args:
        registry: Mapping of state name to state class, shaped like
            :data:`STATE_REGISTRY`.

    Raises:
        TypeError: If any registered state omits ``dispatch_priority`` from
            its own class body. The message names every offender.
    """
    undeclared = sorted(
        name for name, cls in registry.items() if "dispatch_priority" not in vars(cls)
    )
    if undeclared:
        raise TypeError(
            f"states missing an explicit dispatch_priority: {undeclared}. "
            "Declare it in the class body (int = worker dispatch order, lower "
            "first; None = never worker-dispatched). See "
            "TicketState.dispatch_priority."
        )


validate_dispatch_priorities(STATE_REGISTRY)

#: name -> dispatch priority, derived from the registry. ``None`` means the
#: state is never worker-dispatched. This is the single source the
#: QueueManager keys its heap on — there is deliberately no second table.
STATE_DISPATCH_PRIORITY: dict[str, int | None] = {
    name: cls.dispatch_priority for name, cls in STATE_REGISTRY.items()
}


def build_state(name: str) -> TicketState:
    """Return a fresh instance of the named state.

    Raises KeyError if the name is unknown — that's a schema-evolution
    invariant violation (someone added a state without updating the registry).
    """
    return STATE_REGISTRY[name]()
