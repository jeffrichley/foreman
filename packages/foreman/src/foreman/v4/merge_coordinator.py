"""MergeCoordinator — drains each project's merge_queue, one head entry per tick.

foreman#550 Task 4. ``MergingState``/``SpecMerging`` (Task 3) no longer merge
anything themselves — they enqueue the PR on ``ctx.repo``'s ``merge_queue``
and park the ticket in ``MergeQueued`` (excluded from ``QueueManager``
dispatch). Nothing drains that queue until now: this is the per-repo
serializer that does.

Routing table
-------------
``tick()`` processes every current project's HEAD ``merge_queue`` entry
(FIFO, one per project per tick) via ``merge_helper.attempt_merge`` — the
same skeleton + foreman#317 classifier ``MergingState``/``SpecMerging`` used
to call inline. On the resulting :class:`~foreman.v4.outcome.Outcome`:

CLEAN (merged)
    Route the ticket to its post-merge state — ``Implementing`` for a spec
    merge, ``Done`` for an impl merge — and dequeue the entry.
NEEDS_FIX
    Route to ``SpecFix`` (spec) / ``ImplFix`` (impl) and dequeue.
NEEDS_HELP
    Route to ``NeedsHelp`` and dequeue.
BLOCKED
    The legitimate wait-for-CI/heal-and-repoll case. The entry stays put —
    no dequeue, ticket stays in ``MergeQueued`` — UNLESS this cycle's
    ``attempt_merge`` call actually took a heal/update-branch/check-rerun
    action (see "Attempts bound" below), in which case the bound may trip
    and reroute to ``NeedsHelp``.

Attempts bound (foreman#546 poison-PR guard)
---------------------------------------------
``MAX_ATTEMPTS = 3``. A merge_queue entry's ``attempts`` counter
(``TicketRepository.increment_merge_attempts``) is incremented ONLY when
``attempt_merge`` reports it took a REAL action this tick — a healer acted
(``merge_helper.HEAL_ACTION_DETAIL_KEY`` in ``outcome.details``) or a
check-run re-run was issued (``merge_helper.RERUN_DETAIL_KEY``). A plain
"CI still running, nothing to do" BLOCKED poll carries neither marker and
must NOT count — mirrors the marker discipline
``merge_helper._prior_blocked_heal_count`` already established for
foreman#317's heal-loop bound, for the identical reason: PENDING checks can
legitimately poll BLOCKED for many ticks while CI runs, and counting those
would trip the bound on a perfectly healthy in-flight PR. After 3 real
(heal/rerun-acted) cycles without resolving, the entry escalates to
``NeedsHelp`` and is dequeued — a PR whose branch keeps churning or whose
checks keep timing out gets a human's attention instead of retrying forever.

close_originating_issue (foreman#443/#550)
-------------------------------------------
Task 3 correctly dropped the issue-close call from ``MergingState.execute()``
— the PR isn't merged yet at enqueue time. This coordinator re-wires it via
``attempt_merge``'s ``on_merge_success`` hook, exactly mirroring how
``MergingState`` used to wire it: fires ONLY for an impl-kind entry (on
both the "already merged" and "just merged" success branches — see
``merge_helper.attempt_merge``), never for a spec-kind entry.

Heap-zombie eviction (QueueManager)
-------------------------------------
``QueueManager.dequeue()`` filters out ``MergeQueued`` WorkItems rather than
dropping them (they must stay queued for a future ticket state change to
find). But nothing else ever removes that WorkItem, so once THIS coordinator
moves a ticket directly out of ``MergeQueued`` (bypassing the
WorkerPool/QueueManager entirely), the stale WorkItem the Poller enqueued
while the ticket sat parked would otherwise linger in the heap forever. Every
routing branch that leaves ``MergeQueued`` (CLEAN, NEEDS_FIX/NEEDS_HELP, and
the attempts-bound escalation) evicts it via
``QueueManager.evict_merge_queued`` when a ``qm`` was supplied — optional
because unit tests exercising routing logic alone don't need a QueueManager.

Terminal landings (Done / NeedsHelp)
---------------------------------------
Routing directly to a terminal state bypasses ``TicketState.transition()``,
which normally synthesizes the terminal's journal row + ``StateEnteredEvent``
via ``state._enter_terminal`` (see that function's docstring for why this
matters — without it, an issue never gets its ``foreman:state-done`` /
``foreman:state-needshelp`` label, the exact gap that wedged a 2026-06-15
dogfood). This coordinator calls the same helper on the same two terminals a
merge can land on, so labels + the state_instances journal stay complete.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

from foreman.v4.event_bus import EventBus
from foreman.v4.git_provider import GitProvider
from foreman.v4.outcome import Outcome, OutcomeKind
from foreman.v4.queue_manager import QueueManager
from foreman.v4.records import StateInstanceRecord
from foreman.v4.repository import MergeQueueEntry, TicketRepository
from foreman.v4.state import StateContext, TicketState, _enter_terminal
from foreman.v4.states.merge_helper import (
    HEAL_ACTION_DETAIL_KEY,
    RERUN_DETAIL_KEY,
    attempt_merge,
    close_originating_issue,
)
from foreman.v4.states.terminal import DoneState, NeedsHelpState

_log = logging.getLogger(__name__)

#: State names the coordinator can route directly to that require the same
#: terminal-landing journal synthesis ``state._enter_terminal`` performs for
#: the normal WorkerPool-driven path. Neither entry point ever routes to
#: ``Failed`` (that terminal is reserved for the runaway-defense cap /
#: worker-crash paths), so it is intentionally absent here.
_TERMINAL_LANDING_STATES: dict[str, type[TicketState]] = {
    "Done": DoneState,
    "NeedsHelp": NeedsHelpState,
}


def _took_real_action(outcome: Outcome) -> bool:
    """True if this BLOCKED outcome represents a heal/check-rerun action, not a plain CI poll.

    See the module docstring's "Attempts bound" section — only these two
    markers (set by ``merge_helper.attempt_merge`` when a healer acted or a
    timed-out check was re-run) count toward ``MergeCoordinator.MAX_ATTEMPTS``.
    """
    return bool(outcome.details.get(HEAL_ACTION_DETAIL_KEY)) or bool(
        outcome.details.get(RERUN_DETAIL_KEY)
    )


class MergeCoordinator:
    """Serializes merges per repo via the merge_queue. One active merge per project."""

    #: foreman#546 poison-PR bound — see the module docstring.
    MAX_ATTEMPTS = 3

    def __init__(
        self,
        *,
        repo: TicketRepository,
        git: GitProvider,
        projects: Callable[[], list[str]],
        clock: Callable[[], dt.datetime],
        qm: QueueManager | None = None,
        bus: EventBus | None = None,
    ) -> None:
        """Wire the coordinator's dependencies.

        Args:
            repo: The shared TicketRepository.
            git: The GitProvider merges are attempted against (a
                per-project-routing provider in production).
            projects: Zero-arg callable returning the CURRENT list of
                project names to drain, read fresh every ``tick()`` so a
                hot-reloaded project list is picked up without
                reconstructing the coordinator.
            clock: Zero-arg callable returning the current UTC time.
            qm: The daemon's shared QueueManager, used to evict a stale
                ``MergeQueued`` WorkItem whenever a ticket is routed away
                from that state. ``None`` (the default) skips eviction —
                fine for tests that only exercise routing logic.
            bus: The daemon's shared EventBus, used to publish
                ``StateEnteredEvent`` on a terminal landing (Done /
                NeedsHelp) so ``LabelObservabilityObserver`` and friends
                see it, exactly as the WorkerPool-driven path does.
                ``None`` (the default) skips publication.
        """
        self._repo = repo
        self._git = git
        self._projects = projects
        self._clock = clock
        self._qm = qm
        self._bus = bus

    def tick(self) -> None:
        """Process the head merge_queue entry of every current project, once each.

        Isolated per project: ``_tick_project`` runs live GitHub I/O via
        ``merge_helper.attempt_merge`` (``get_pr_state``/``merge_pr``/
        ``required_check_state``) and can raise on an API blip. Letting that
        propagate out of the loop would abort every remaining project's
        tick this cycle -- cross-project merge starvation. The daemon's
        outer ``try/except`` around ``coordinator.tick()`` (see
        ``daemon.py``) does NOT prevent this; it only catches after the
        loop has already aborted. Mirrors the poller isolation pattern
        (foreman I1, ``daemon.py`` ~369-376): log loudly (with traceback)
        and continue to the next project.
        """
        for project in self._projects():
            try:
                self._tick_project(project)
            except Exception:
                _log.exception(
                    "merge-coordinator tick failed for project %r; isolating and continuing",
                    project,
                )

    def _tick_project(self, project: str) -> None:
        entry = self._repo.head_merge_entry(project)
        if entry is None:
            return
        if entry.status != "merging":
            self._repo.mark_merge_active(entry.id)
        ctx = self._ctx_for(entry)
        outcome = attempt_merge(
            ctx,
            pr_number=entry.pr_number,
            on_merge_success=self._on_merge_success(ctx, entry),
            pre_merge_guard=None,
        )
        if outcome.kind == OutcomeKind.CLEAN:
            self._route(ctx, entry, self._post_merge_state(entry))
        elif outcome.kind == OutcomeKind.BLOCKED:
            self._handle_blocked(ctx, entry, outcome)
        else:
            # NEEDS_FIX / NEEDS_HELP (the only remaining OutcomeKinds
            # attempt_merge's merge skeleton ever returns).
            self._route(ctx, entry, self._failure_state(entry, outcome))

    def _handle_blocked(
        self,
        ctx: StateContext,
        entry: MergeQueueEntry,
        outcome: Outcome,
    ) -> None:
        """Advance the poison-PR bound only on a real heal/rerun cycle; escalate at the cap."""
        if not _took_real_action(outcome):
            # Plain CI-pending poll — the legitimate wait. No bound
            # movement, no dequeue; the entry stays head-of-queue for the
            # next tick to re-evaluate.
            return
        attempts = self._repo.increment_merge_attempts(entry.id)
        if attempts >= self.MAX_ATTEMPTS:
            self._route(ctx, entry, "NeedsHelp")

    def _route(self, ctx: StateContext, entry: MergeQueueEntry, state_name: str) -> None:
        """Move the ticket to ``state_name``, dequeue the entry, and clean up after it."""
        self._repo.set_ticket_state(entry.ticket_id, state_name, now=self._clock())
        self._repo.dequeue_merge(entry.id)
        if self._qm is not None:
            self._qm.evict_merge_queued(entry.ticket_id)
        terminal_cls = _TERMINAL_LANDING_STATES.get(state_name)
        if terminal_cls is not None:
            _enter_terminal(ctx, terminal_cls())

    def _ctx_for(self, entry: MergeQueueEntry) -> StateContext:
        """Build a StateContext for ``attempt_merge`` to run against.

        ``instance`` is a synthetic, never-persisted
        ``StateInstanceRecord`` — the coordinator does not journal a
        ``MergeQueued`` state_instance row (nothing ever has; see the
        ``MergeQueuedState`` module docstring), so there is no real row to
        attach. Its only consumer, ``merge_helper._prior_blocked_heal_count``
        / ``_prior_rerun_count``, reads it back via
        ``ctx.repo.list_state_instances_for_ticket`` filtered on
        ``state_name == ctx.instance.state_name`` — since no ``"MergeQueued"``
        row is ever persisted, that lookup always returns zero prior heals,
        which is correct: the coordinator's OWN ``MAX_ATTEMPTS`` bound (3,
        tracked on the merge_queue entry itself) supersedes
        ``merge_helper.MAX_HEAL_ACTIONS`` (5) for coordinator-driven merges
        anyway, since it is strictly tighter.
        """
        ticket = self._repo.get_ticket(entry.ticket_id)
        now = self._clock()
        instance = StateInstanceRecord(
            id=0,
            ticket_id=entry.ticket_id,
            state_name="MergeQueued",
            sequence=0,
            entered_at=now,
            execute_started_at=None,
            execute_completed_at=None,
            exited_at=None,
            outcome_kind=None,
            outcome_payload=None,
            next_state=None,
            failure_phase=None,
            failure_reason=None,
        )
        return StateContext(
            ticket=ticket,
            instance=instance,
            repo=self._repo,
            clock=self._clock,
            bus=self._bus,
            git=self._git,
        )

    def _on_merge_success(self, ctx: StateContext, entry: MergeQueueEntry) -> Callable[[], None]:
        """Close the originating issue for an impl merge only; no-op for a spec merge."""
        if entry.kind == "impl":
            return lambda: close_originating_issue(ctx)
        return lambda: None

    def _post_merge_state(self, entry: MergeQueueEntry) -> str:
        return "Implementing" if entry.kind == "spec" else "Done"

    def _failure_state(self, entry: MergeQueueEntry, outcome: Outcome) -> str:
        if outcome.kind == OutcomeKind.NEEDS_FIX:
            return "SpecFix" if entry.kind == "spec" else "ImplFix"
        return "NeedsHelp"
