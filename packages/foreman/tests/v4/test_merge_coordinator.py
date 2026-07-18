"""MergeCoordinator — drains each project's merge_queue, one head entry per tick.

foreman#550 Task 4. These tests drive ``MergeCoordinator.tick()`` directly
against an ``InMemoryTicketRepository`` + ``FakeGitProvider`` (and, where the
criterion needs it, a real ``QueueManager``), covering:

* the routing table (CLEAN -> post-merge state, NEEDS_FIX -> *Fix,
  NEEDS_HELP -> NeedsHelp, BLOCKED -> stays queued) for both spec and impl
  entries,
* FIFO (only the head entry advances per tick),
* the foreman#546 poison-PR attempts bound and its "only a REAL heal/rerun
  action counts" nuance,
* ``close_originating_issue`` firing on an impl merge and NOT on a spec
  merge,
* the QueueManager heap-zombie eviction when a ticket leaves MergeQueued,
* terminal-landing journal/event synthesis for the two terminals a merge
  can land on (Done, NeedsHelp).
"""

from __future__ import annotations

import datetime as dt

from foreman.v4.event_bus import EventBus
from foreman.v4.events import Event, StateEnteredEvent, StateExitedEvent
from foreman.v4.git_provider import FakeGitProvider, PRState, RequiredCheckState
from foreman.v4.merge_coordinator import MergeCoordinator
from foreman.v4.queue_manager import QueueManager
from foreman.v4.records import TicketRecord
from foreman.v4.repository import InMemoryTicketRepository, MergeQueueEntry
from foreman.v4.state import StateContext
from foreman.v4.states.merging import MergingState
from foreman.v4.work import WorkItem

_NOW = dt.datetime(2026, 7, 17, tzinfo=dt.UTC)


def _clock() -> dt.datetime:
    return _NOW


def _seed_ticket_in_merge_queue(
    repo: InMemoryTicketRepository,
    git: FakeGitProvider,
    *,
    project: str = "p",
    issue_number: int,
    pr: int,
    kind: str,
    mergeable: bool = False,
    ci_passing: bool = False,
    mergeable_state: str = "blocked",
    ci: RequiredCheckState | None = None,
) -> TicketRecord:
    """Create a ticket parked in MergeQueued with one merge_queue entry for it."""
    ticket = repo.create_ticket(project=project, issue_number=issue_number, now=_NOW)
    repo.set_ticket_state(ticket.id, "MergeQueued", now=_NOW)
    repo.enqueue_merge(project=project, ticket_id=ticket.id, pr_number=pr, kind=kind, now=_NOW)
    git.set_pr_state(
        project=project,
        pr_number=pr,
        state=PRState(
            merged=False,
            mergeable=mergeable,
            ci_passing=ci_passing,
            base_ref="main",
            mergeable_state=mergeable_state,
        ),
    )
    if ci is not None:
        git.seed_check_state(project, pr, ci)
    return ticket


def _fake() -> tuple[InMemoryTicketRepository, FakeGitProvider]:
    return InMemoryTicketRepository(), FakeGitProvider()


def _coordinator(
    repo: InMemoryTicketRepository,
    git: FakeGitProvider,
    *,
    project: str = "p",
    qm: QueueManager | None = None,
    bus: EventBus | None = None,
) -> MergeCoordinator:
    return MergeCoordinator(
        repo=repo, git=git, projects=lambda: [project], clock=_clock, qm=qm, bus=bus
    )


# ---------------------------------------------------------------------------
# Routing table
# ---------------------------------------------------------------------------


def test_coordinator_merges_head_and_routes_impl_to_done():
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="impl", mergeable=True, ci_passing=True
    )
    _coordinator(repo, git).tick()
    assert repo.get_ticket(ticket.id).current_state == "Done"
    assert repo.head_merge_entry("p") is None
    assert ("p", 10) in git.merge_pr_calls


def test_spec_merge_routes_to_implementing():
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="spec", mergeable=True, ci_passing=True
    )
    _coordinator(repo, git).tick()
    assert repo.get_ticket(ticket.id).current_state == "Implementing"
    assert repo.head_merge_entry("p") is None


def test_ci_failed_routes_to_impl_fix_and_dequeues():
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo,
        git,
        issue_number=1,
        pr=10,
        kind="impl",
        mergeable_state="blocked",
        ci=RequiredCheckState.FAILED,
    )
    _coordinator(repo, git).tick()
    assert repo.get_ticket(ticket.id).current_state == "ImplFix"
    assert repo.head_merge_entry("p") is None


def test_ci_failed_spec_routes_to_spec_fix():
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo,
        git,
        issue_number=1,
        pr=10,
        kind="spec",
        mergeable_state="blocked",
        ci=RequiredCheckState.FAILED,
    )
    _coordinator(repo, git).tick()
    assert repo.get_ticket(ticket.id).current_state == "SpecFix"


def test_dirty_routes_to_impl_fix():
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="impl", mergeable_state="dirty"
    )
    _coordinator(repo, git).tick()
    assert repo.get_ticket(ticket.id).current_state == "ImplFix"
    assert repo.head_merge_entry("p") is None


def test_needs_help_outcome_routes_to_needs_help():
    """draft -> NEEDS_HELP per the foreman#317 routing table, regardless of kind."""
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="impl", mergeable_state="draft"
    )
    _coordinator(repo, git).tick()
    assert repo.get_ticket(ticket.id).current_state == "NeedsHelp"
    assert repo.head_merge_entry("p") is None


def test_pending_stays_queued_no_dequeue():
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo,
        git,
        issue_number=1,
        pr=10,
        kind="impl",
        mergeable_state="blocked",
        ci=RequiredCheckState.PENDING,
    )
    _coordinator(repo, git).tick()
    assert repo.get_ticket(ticket.id).current_state == "MergeQueued"
    entry = repo.head_merge_entry("p")
    assert entry is not None
    assert entry.status == "merging"  # marked active on the first tick
    assert entry.attempts == 0  # a plain CI-pending poll does not advance the bound


def test_fifo_only_head_processed():
    repo, git = _fake()
    first = _seed_ticket_in_merge_queue(
        repo,
        git,
        issue_number=1,
        pr=10,
        kind="impl",
        mergeable_state="blocked",
        ci=RequiredCheckState.PENDING,
    )
    second = _seed_ticket_in_merge_queue(
        repo, git, issue_number=2, pr=11, kind="impl", mergeable=True, ci_passing=True
    )
    _coordinator(repo, git).tick()
    # The head (first-enqueued) entry is the only one attempt_merge touched
    # this tick, even though the second entry's PR is trivially mergeable.
    assert repo.get_ticket(first.id).current_state == "MergeQueued"
    assert repo.get_ticket(second.id).current_state == "MergeQueued"
    assert ("p", 11) not in git.merge_pr_calls
    entry = repo.head_merge_entry("p")
    assert entry is not None and entry.ticket_id == first.id


# ---------------------------------------------------------------------------
# foreman#546 poison-PR attempts bound
# ---------------------------------------------------------------------------


def test_poison_pr_bounded_to_needs_help_after_3():
    """A perpetually-behind PR (the plain FakeGitProvider never auto-clears
    "behind" on update_branch) heals every tick without ever resolving —
    exactly ``MAX_ATTEMPTS`` real heal cycles before escalating."""
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="impl", mergeable_state="behind"
    )
    coordinator = _coordinator(repo, git)
    coordinator.tick()
    assert repo.get_ticket(ticket.id).current_state == "MergeQueued"
    coordinator.tick()
    assert repo.get_ticket(ticket.id).current_state == "MergeQueued"
    coordinator.tick()
    assert repo.get_ticket(ticket.id).current_state == "NeedsHelp"
    assert repo.head_merge_entry("p") is None
    assert git.update_branch_calls == [("p", 10), ("p", 10), ("p", 10)]


def test_plain_ci_pending_polls_do_not_count_toward_bound():
    """REGRESSION guard mirroring foreman#317's marker discipline: a PR
    that legitimately polls BLOCKED while CI runs must NOT trip the
    coordinator's attempts bound, however many ticks it takes."""
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo,
        git,
        issue_number=1,
        pr=10,
        kind="impl",
        mergeable_state="blocked",
        ci=RequiredCheckState.PENDING,
    )
    coordinator = _coordinator(repo, git)
    for _ in range(MergeCoordinator.MAX_ATTEMPTS + 5):
        coordinator.tick()
    assert repo.get_ticket(ticket.id).current_state == "MergeQueued"
    entry = repo.head_merge_entry("p")
    assert entry is not None
    assert entry.attempts == 0


def test_mixed_heal_then_ci_pending_only_heal_counts():
    """A PR that heals once then settles into legitimate CI-pending polling
    must not accumulate attempts from the CI-pending cycles."""
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="impl", mergeable_state="behind"
    )
    coordinator = _coordinator(repo, git)
    coordinator.tick()  # heal #1 -> attempts=1
    entry = repo.head_merge_entry("p")
    assert entry is not None and entry.attempts == 1
    # Flip the PR to a plain CI-pending state -- no further heals apply.
    git.set_pr_state(
        project="p",
        pr_number=10,
        state=PRState(
            merged=False,
            mergeable=False,
            ci_passing=False,
            base_ref="main",
            mergeable_state="blocked",
        ),
    )
    for _ in range(5):
        coordinator.tick()
    entry = repo.head_merge_entry("p")
    assert entry is not None
    assert entry.attempts == 1  # unchanged by the 5 CI-pending polls
    assert repo.get_ticket(ticket.id).current_state == "MergeQueued"


# ---------------------------------------------------------------------------
# close_originating_issue (foreman#443/#550) — impl only, never spec
# ---------------------------------------------------------------------------


def test_close_originating_issue_fires_on_impl_merge():
    repo, git = _fake()
    _seed_ticket_in_merge_queue(
        repo, git, issue_number=7, pr=10, kind="impl", mergeable=True, ci_passing=True
    )
    _coordinator(repo, git).tick()
    assert ("p", 7) in git.closed_issues


def test_close_originating_issue_does_not_fire_on_spec_merge():
    repo, git = _fake()
    _seed_ticket_in_merge_queue(
        repo, git, issue_number=7, pr=10, kind="spec", mergeable=True, ci_passing=True
    )
    _coordinator(repo, git).tick()
    assert ("p", 7) not in git.closed_issues


def test_close_originating_issue_fires_on_already_merged_impl_pr():
    """on_merge_success fires on BOTH attempt_merge success branches — the
    already-merged short-circuit too, mirroring the old MergingState."""
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=7, pr=10, kind="impl", mergeable=True, ci_passing=True
    )
    # Externally merged before the coordinator ever looks at it.
    git.merge_pr(project="p", pr_number=10)
    git.merge_pr_calls.clear()  # isolate: assert the coordinator itself didn't re-merge
    _coordinator(repo, git).tick()
    assert ("p", 7) in git.closed_issues
    assert ("p", 10) not in git.merge_pr_calls
    assert repo.get_ticket(ticket.id).current_state == "Done"


# ---------------------------------------------------------------------------
# QueueManager heap-zombie eviction
# ---------------------------------------------------------------------------


def test_evicts_stale_merge_queued_workitem_on_drain():
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="impl", mergeable=True, ci_passing=True
    )
    qm = QueueManager(repo=repo, max_in_flight=4)
    zombie = WorkItem(ticket_id=ticket.id, state_name="MergeQueued", project="p")
    qm.enqueue(zombie)
    assert qm.queue_depth() == 1

    _coordinator(repo, git, qm=qm).tick()

    assert repo.get_ticket(ticket.id).current_state == "Done"
    # The stale MergeQueued WorkItem is gone -- not just filtered, GONE.
    assert qm.queue_depth() == 0
    assert qm.dequeue() is None


def test_evicts_stale_workitem_on_needs_fix_route_too():
    """Eviction is not CLEAN-only -- every routing branch that leaves
    MergeQueued must clean up its zombie WorkItem."""
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="impl", mergeable_state="dirty"
    )
    qm = QueueManager(repo=repo, max_in_flight=4)
    qm.enqueue(WorkItem(ticket_id=ticket.id, state_name="MergeQueued", project="p"))

    _coordinator(repo, git, qm=qm).tick()

    assert repo.get_ticket(ticket.id).current_state == "ImplFix"
    assert qm.queue_depth() == 0


def test_does_not_evict_when_qm_not_supplied():
    """qm is optional -- routing logic must not blow up without one."""
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="impl", mergeable=True, ci_passing=True
    )
    _coordinator(repo, git, qm=None).tick()
    assert repo.get_ticket(ticket.id).current_state == "Done"


def test_still_queued_entry_is_not_evicted():
    """A BLOCKED (still-queued) tick must NOT touch the QueueManager at all
    -- the ticket hasn't left MergeQueued."""
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo,
        git,
        issue_number=1,
        pr=10,
        kind="impl",
        mergeable_state="blocked",
        ci=RequiredCheckState.PENDING,
    )
    qm = QueueManager(repo=repo, max_in_flight=4)
    qm.enqueue(WorkItem(ticket_id=ticket.id, state_name="MergeQueued", project="p"))

    _coordinator(repo, git, qm=qm).tick()

    assert repo.get_ticket(ticket.id).current_state == "MergeQueued"
    assert qm.queue_depth() == 1


# ---------------------------------------------------------------------------
# Terminal-landing journal + event synthesis (Done / NeedsHelp)
# ---------------------------------------------------------------------------


def test_done_landing_is_journaled():
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="impl", mergeable=True, ci_passing=True
    )
    _coordinator(repo, git).tick()
    rows = repo.list_state_instances_for_ticket(ticket.id)
    assert [r.state_name for r in rows] == ["Done"]
    assert rows[0].exited_at is not None  # closed, not left dangling in-flight


def test_needs_help_landing_is_journaled():
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="impl", mergeable_state="draft"
    )
    _coordinator(repo, git).tick()
    rows = repo.list_state_instances_for_ticket(ticket.id)
    assert [r.state_name for r in rows] == ["NeedsHelp"]


def test_terminal_landing_publishes_state_entered_event_when_bus_given():
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="impl", mergeable=True, ci_passing=True
    )
    bus = EventBus()
    events: list[StateEnteredEvent] = []
    bus.subscribe(lambda e: events.append(e) if isinstance(e, StateEnteredEvent) else None)
    _coordinator(repo, git, bus=bus).tick()
    done_events = [e for e in events if e.ticket_id == ticket.id and e.state_name == "Done"]
    assert len(done_events) == 1


def test_non_terminal_route_is_not_journaled_by_the_coordinator():
    """Implementing/*Fix are non-terminal -- the coordinator does not
    synthesize a journal row for them (the next normal dispatch does, via
    the WorkerPool's own open_state_instance -- out of this test's scope)."""
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="spec", mergeable=True, ci_passing=True
    )
    _coordinator(repo, git).tick()
    assert repo.list_state_instances_for_ticket(ticket.id) == []


# ---------------------------------------------------------------------------
# Per-project fault isolation (mirrors the daemon's poller isolation)
# ---------------------------------------------------------------------------


class _RaisingHeadEntryRepo(InMemoryTicketRepository):
    """A repo whose ``head_merge_entry`` raises for one designated project.

    Simulates a live GitHub API blip surfacing inside ``_tick_project`` --
    the real failure mode is ``attempt_merge`` raising, but raising from
    ``head_merge_entry`` exercises the identical code path (an unhandled
    exception escaping ``_tick_project``) without needing to fake GitHub
    I/O failures.
    """

    def __init__(self, *, raise_for_project: str) -> None:
        super().__init__()
        self._raise_for_project = raise_for_project

    def head_merge_entry(self, project: str) -> MergeQueueEntry | None:
        if project == self._raise_for_project:
            raise RuntimeError("simulated GitHub API blip")
        return super().head_merge_entry(project)


def test_reconcile_recovers_merging_entry_that_already_merged():
    """foreman#550 Task 5: a crash between the merge landing and the dequeue
    leaves the entry ``status="merging"``. Startup reconciliation re-fetches
    the PR, sees it merged, and reuses the Task-4 success path — post-merge
    routing, close-originating-issue, and dequeue — so the crash never loses
    a merge."""
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=7, pr=10, kind="impl", mergeable=True, ci_passing=True
    )
    entry = repo.head_merge_entry("p")
    assert entry is not None
    repo.mark_merge_active(entry.id)
    # The PR actually merged before the daemon died -- ground truth on GitHub.
    git.set_pr_state(
        project="p",
        pr_number=10,
        state=PRState(
            merged=True,
            mergeable=True,
            ci_passing=True,
            base_ref="main",
            mergeable_state="clean",
        ),
    )

    _coordinator(repo, git).reconcile_on_startup()

    assert repo.get_ticket(ticket.id).current_state == "Done"
    assert repo.head_merge_entry("p") is None
    assert ("p", 7) in git.closed_issues


def test_reconcile_spec_merge_routes_to_implementing():
    """The recovered post-merge routing must respect ``kind`` exactly like
    the normal tick path -- a spec merge lands in Implementing, not Done."""
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=7, pr=10, kind="spec", mergeable=True, ci_passing=True
    )
    entry = repo.head_merge_entry("p")
    assert entry is not None
    repo.mark_merge_active(entry.id)
    git.set_pr_state(
        project="p",
        pr_number=10,
        state=PRState(
            merged=True,
            mergeable=True,
            ci_passing=True,
            base_ref="main",
            mergeable_state="clean",
        ),
    )

    _coordinator(repo, git).reconcile_on_startup()

    assert repo.get_ticket(ticket.id).current_state == "Implementing"
    assert ("p", 7) not in git.closed_issues  # spec merges never close the issue


def test_reconcile_resets_merging_entry_that_did_not_merge():
    """A ``"merging"`` entry whose PR is NOT actually merged means the crash
    landed before the merge -- reset to ``"queued"`` so the next tick
    re-processes it at the head; the ticket itself is untouched."""
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo,
        git,
        issue_number=1,
        pr=10,
        kind="impl",
        mergeable_state="blocked",
        ci=RequiredCheckState.PENDING,
    )
    entry = repo.head_merge_entry("p")
    assert entry is not None
    repo.mark_merge_active(entry.id)

    _coordinator(repo, git).reconcile_on_startup()

    assert repo.get_ticket(ticket.id).current_state == "MergeQueued"  # unchanged
    reset_entry = repo.head_merge_entry("p")
    assert reset_entry is not None
    assert reset_entry.id == entry.id
    assert reset_entry.status == "queued"
    assert repo.list_active_merges() == []


def test_reconcile_evicts_stale_workitem_on_recovered_merge():
    """Reconcile reuses the Task-4 ``_route`` helper -- QueueManager
    heap-zombie eviction must fire exactly as it does on a normal tick."""
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="impl", mergeable=True, ci_passing=True
    )
    entry = repo.head_merge_entry("p")
    assert entry is not None
    repo.mark_merge_active(entry.id)
    git.set_pr_state(
        project="p",
        pr_number=10,
        state=PRState(
            merged=True,
            mergeable=True,
            ci_passing=True,
            base_ref="main",
            mergeable_state="clean",
        ),
    )
    qm = QueueManager(repo=repo, max_in_flight=4)
    qm.enqueue(WorkItem(ticket_id=ticket.id, state_name="MergeQueued", project="p"))

    _coordinator(repo, git, qm=qm).reconcile_on_startup()

    assert repo.get_ticket(ticket.id).current_state == "Done"
    assert qm.queue_depth() == 0


def test_reconcile_covers_every_active_project():
    """``list_active_merges`` is cross-project -- reconcile must recover
    every ``"merging"`` entry it returns, not just the first project's."""
    repo, git = _fake()
    merged_ticket = _seed_ticket_in_merge_queue(
        repo, git, project="p1", issue_number=1, pr=10, kind="impl", mergeable=True, ci_passing=True
    )
    unmerged_ticket = _seed_ticket_in_merge_queue(
        repo,
        git,
        project="p2",
        issue_number=2,
        pr=20,
        kind="impl",
        mergeable_state="blocked",
        ci=RequiredCheckState.PENDING,
    )
    merged_entry = repo.head_merge_entry("p1")
    unmerged_entry = repo.head_merge_entry("p2")
    assert merged_entry is not None
    assert unmerged_entry is not None
    repo.mark_merge_active(merged_entry.id)
    repo.mark_merge_active(unmerged_entry.id)
    git.set_pr_state(
        project="p1",
        pr_number=10,
        state=PRState(
            merged=True,
            mergeable=True,
            ci_passing=True,
            base_ref="main",
            mergeable_state="clean",
        ),
    )

    # ``reconcile_on_startup`` reads ``list_active_merges()`` directly (it is
    # already cross-project), so the coordinator's ``projects`` callable --
    # which only matters for ``tick()`` -- is irrelevant here.
    _coordinator(repo, git).reconcile_on_startup()

    assert repo.get_ticket(merged_ticket.id).current_state == "Done"
    assert repo.head_merge_entry("p1") is None
    assert repo.get_ticket(unmerged_ticket.id).current_state == "MergeQueued"
    reset_entry = repo.head_merge_entry("p2")
    assert reset_entry is not None
    assert reset_entry.status == "queued"


def test_tick_isolates_a_failing_project_and_still_drains_the_rest():
    """One project's ``_tick_project`` raising must not starve the others.

    foreman I1: mirrors the daemon's per-poller fault isolation (see
    ``daemon.py`` ~369-376) at the ``MergeCoordinator.tick()`` level --
    the daemon's own ``try/except`` around ``coordinator.tick()`` only
    catches AFTER the internal ``for`` loop has already aborted, so it
    does not protect project 2 from project 1's failure.
    """
    repo = _RaisingHeadEntryRepo(raise_for_project="p1")
    git = FakeGitProvider()
    _seed_ticket_in_merge_queue(
        repo, git, project="p1", issue_number=1, pr=10, kind="impl", mergeable=True
    )
    second = _seed_ticket_in_merge_queue(
        repo, git, project="p2", issue_number=2, pr=20, kind="impl", mergeable=True, ci_passing=True
    )

    coordinator = MergeCoordinator(repo=repo, git=git, projects=lambda: ["p1", "p2"], clock=_clock)
    coordinator.tick()

    # p2 was still drained despite p1 raising.
    assert repo.get_ticket(second.id).current_state == "Done"
    assert repo.head_merge_entry("p2") is None
    assert ("p2", 20) in git.merge_pr_calls


# ---------------------------------------------------------------------------
# Per-entry fault isolation during startup reconciliation (foreman#550 Task 5)
# ---------------------------------------------------------------------------


class _RaisingGetPrStateGitProvider(FakeGitProvider):
    """A git provider whose ``get_pr_state`` raises for one designated PR.

    Simulates a live GitHub API blip surfacing inside ``_reconcile_entry`` --
    ``reconcile_on_startup`` re-fetches ground truth directly via
    ``get_pr_state`` (see its docstring), so raising there exercises the
    identical unhandled-exception code path without needing to fake other
    GitHub I/O. Mirrors ``_RaisingHeadEntryRepo`` above.
    """

    def __init__(self, *, raise_for: tuple[str, int]) -> None:
        super().__init__()
        self._raise_for = raise_for

    def get_pr_state(self, *, project: str, pr_number: int) -> PRState:
        if (project, pr_number) == self._raise_for:
            raise RuntimeError("simulated GitHub API blip")
        return super().get_pr_state(project=project, pr_number=pr_number)


def test_reconcile_isolates_a_failing_entry_and_still_recovers_the_rest():
    """One entry's ``_reconcile_entry`` raising must not abort recovery of
    the others.

    The daemon calls ``_reconcile_startup`` BEFORE ``run_forever()``'s own
    ``try/except`` (see ``daemon.py`` ~422-423), so letting an exception
    escape ``reconcile_on_startup`` crashes daemon startup entirely -- a
    crash-loop under Docker restart-on-crash. Mirrors ``tick()``'s
    per-project isolation (``test_tick_isolates_a_failing_project_and_still_drains_the_rest``).
    """
    git = _RaisingGetPrStateGitProvider(raise_for=("p1", 10))
    repo = InMemoryTicketRepository()
    _seed_ticket_in_merge_queue(
        repo, git, project="p1", issue_number=1, pr=10, kind="impl", mergeable=True, ci_passing=True
    )
    second = _seed_ticket_in_merge_queue(
        repo, git, project="p2", issue_number=2, pr=20, kind="impl", mergeable=True, ci_passing=True
    )
    first_entry = repo.head_merge_entry("p1")
    second_entry = repo.head_merge_entry("p2")
    assert first_entry is not None
    assert second_entry is not None
    repo.mark_merge_active(first_entry.id)
    repo.mark_merge_active(second_entry.id)
    # p2's PR actually merged before the (simulated) crash.
    git.set_pr_state(
        project="p2",
        pr_number=20,
        state=PRState(
            merged=True,
            mergeable=True,
            ci_passing=True,
            base_ref="main",
            mergeable_state="clean",
        ),
    )

    coordinator = MergeCoordinator(repo=repo, git=git, projects=lambda: ["p1", "p2"], clock=_clock)
    count = coordinator.reconcile_on_startup()

    # p2 was still recovered despite p1 raising -- reconcile_on_startup
    # did not propagate p1's exception.
    assert repo.get_ticket(second.id).current_state == "Done"
    assert repo.head_merge_entry("p2") is None
    assert ("p2", 2) in git.closed_issues  # (project, issue_number), not pr_number
    assert count == 2


# ---------------------------------------------------------------------------
# MergeQueued label-stamp fix -- exit-side (StateExitedEvent on drain)
# ---------------------------------------------------------------------------
#
# foreman:state-merge-queued was never stamped: StateEnteredEvent(MergeQueued)
# never fired (fixed in test_transition_events.py, entry side) AND nothing
# ever fired StateExitedEvent(MergeQueued) when the coordinator routed a
# ticket out of MergeQueued, so even after the entry fix the label would
# linger forever alongside the ticket's real next-state label. These tests
# pin the exit half: every ``_route`` branch (terminal + non-terminal, normal
# tick + crash-recovery reconcile) must publish StateExitedEvent(MergeQueued).


def test_route_publishes_state_exited_event_for_merge_queued_on_clean_drain():
    """CLEAN impl merge -> Done (terminal). The exit event must precede
    the terminal's own StateEnteredEvent(Done) so there is no window
    where the issue shows both foreman:state-merge-queued and
    foreman:state-done."""
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="impl", mergeable=True, ci_passing=True
    )
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(events.append)
    _coordinator(repo, git, bus=bus).tick()

    exited = [
        e for e in events if isinstance(e, StateExitedEvent) and e.state_name == "MergeQueued"
    ]
    entered_done = [
        e for e in events if isinstance(e, StateEnteredEvent) and e.state_name == "Done"
    ]
    assert len(exited) == 1
    assert exited[0].ticket_id == ticket.id
    assert len(entered_done) == 1
    assert events.index(exited[0]) < events.index(entered_done[0])


def test_route_publishes_state_exited_event_for_merge_queued_on_impl_fix_drain():
    """NEEDS_FIX (dirty PR) -> ImplFix. Non-terminal drain must still
    remove the merge-queued label so it doesn't linger next to
    ImplFix's own label once the WorkerPool later picks the ticket up."""
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="impl", mergeable_state="dirty"
    )
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(events.append)
    _coordinator(repo, git, bus=bus).tick()

    exited = [
        e for e in events if isinstance(e, StateExitedEvent) and e.state_name == "MergeQueued"
    ]
    assert len(exited) == 1
    assert exited[0].ticket_id == ticket.id
    assert repo.get_ticket(ticket.id).current_state == "ImplFix"


def test_route_publishes_state_exited_event_for_merge_queued_on_spec_implementing_drain():
    """CLEAN spec merge -> Implementing. Spec variant of the non-terminal
    drain -- same exit-event requirement as the impl variant."""
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="spec", mergeable=True, ci_passing=True
    )
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(events.append)
    _coordinator(repo, git, bus=bus).tick()

    exited = [
        e for e in events if isinstance(e, StateExitedEvent) and e.state_name == "MergeQueued"
    ]
    assert len(exited) == 1
    assert repo.get_ticket(ticket.id).current_state == "Implementing"


def test_route_publishes_state_exited_event_for_merge_queued_on_needs_help_drain():
    """NEEDS_HELP (draft PR) -> NeedsHelp (terminal). Exit event must
    precede the terminal's StateEnteredEvent(NeedsHelp), mirroring the
    Done case above."""
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="impl", mergeable_state="draft"
    )
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(events.append)
    _coordinator(repo, git, bus=bus).tick()

    exited = [
        e for e in events if isinstance(e, StateExitedEvent) and e.state_name == "MergeQueued"
    ]
    entered_needs_help = [
        e for e in events if isinstance(e, StateEnteredEvent) and e.state_name == "NeedsHelp"
    ]
    assert len(exited) == 1
    assert exited[0].ticket_id == ticket.id
    assert len(entered_needs_help) == 1
    assert events.index(exited[0]) < events.index(entered_needs_help[0])


def test_blocked_still_queued_does_not_publish_state_exited_for_merge_queued():
    """A BLOCKED (still-queued) tick must NOT fire StateExitedEvent(MergeQueued)
    -- the ticket hasn't left MergeQueued, so the label must stay put."""
    repo, git = _fake()
    _seed_ticket_in_merge_queue(
        repo,
        git,
        issue_number=1,
        pr=10,
        kind="impl",
        mergeable_state="blocked",
        ci=RequiredCheckState.PENDING,
    )
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(events.append)
    _coordinator(repo, git, bus=bus).tick()

    exited = [
        e for e in events if isinstance(e, StateExitedEvent) and e.state_name == "MergeQueued"
    ]
    assert exited == []


def test_route_closes_the_real_merge_queued_row_and_reuses_its_id():
    """Instance-handling: drives ``MergingState.transition()`` for REAL
    (unlike every other test in this file, which seeds a ticket directly
    into MergeQueued via ``_seed_ticket_in_merge_queue`` -- a shortcut
    that bypasses ``state.py`` entirely) so ``state._enter_merge_queued``
    opens a genuine, in-flight ``MergeQueued`` state_instance row. Then
    drains it via the coordinator and asserts ``_route`` finds THAT row,
    closes it, and the published ``StateExitedEvent`` carries its real
    ``instance_id``/``sequence`` -- not a synthetic placeholder."""
    repo, git = _fake()
    ticket = repo.create_ticket(project="p", issue_number=1, now=_NOW)
    repo.set_ticket_state(ticket.id, "Merging", now=_NOW)
    merging_instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Merging", sequence=1, now=_NOW
    )
    git.set_pr_state(
        project="p",
        pr_number=10,
        state=PRState(merged=False, mergeable=True, ci_passing=True, base_ref="main"),
    )
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(events.append)
    ctx = StateContext(
        ticket=repo.get_ticket(ticket.id),
        instance=merging_instance,
        repo=repo,
        clock=_clock,
        bus=bus,
        git=git,
    )
    merging_state = MergingState()
    merging_state._pr_number_for = lambda _ctx: 10  # type: ignore[method-assign]
    result = merging_state.transition(ctx)
    assert result is not None
    assert result.state_name == "MergeQueued"

    merge_queued_row = repo.list_state_instances_for_ticket(ticket.id)[-1]
    assert merge_queued_row.state_name == "MergeQueued"
    assert (
        merge_queued_row.is_in_flight
    )  # entry-side leaves it open (see test_transition_events.py)

    # Drain it -- the impl PR is mergeable + CI-passing, so one tick merges
    # it and routes to Done.
    _coordinator(repo, git, bus=bus).tick()

    closed_row = repo.get_state_instance(merge_queued_row.id)
    assert not closed_row.is_in_flight, "coordinator drain must close the real MergeQueued row"

    exited = [
        e for e in events if isinstance(e, StateExitedEvent) and e.state_name == "MergeQueued"
    ]
    assert len(exited) == 1
    assert exited[0].instance_id == merge_queued_row.id
    assert exited[0].sequence == merge_queued_row.sequence
    assert repo.get_ticket(ticket.id).current_state == "Done"


def test_exit_merge_queued_falls_back_when_real_row_already_closed():
    """Minor finding from the 08d5aab review: an anticipated-but-untested
    branch in ``_exit_merge_queued``'s row scan.

    Unlike ``test_route_falls_back_to_synthetic_instance_when_no_real_row_was_opened``
    (empty ``list_state_instances_for_ticket`` -- no row was ever opened),
    this pins the case where a real row EXISTS (a non-empty list) but is
    already closed by the time ``_route`` runs -- e.g. a generic startup
    reconcile closed it as a crash orphan before this coordinator's own
    reconcile got to it. The scan's ``row.is_in_flight`` guard must skip
    that closed row rather than re-closing it, and fall through to the
    ``ctx.instance`` synthetic fallback so the exit event -- and the
    label removal it drives -- still fires."""
    repo, git = _fake()
    ticket = repo.create_ticket(project="p", issue_number=1, now=_NOW)
    repo.set_ticket_state(ticket.id, "Merging", now=_NOW)
    merging_instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Merging", sequence=1, now=_NOW
    )
    git.set_pr_state(
        project="p",
        pr_number=10,
        state=PRState(merged=False, mergeable=True, ci_passing=True, base_ref="main"),
    )
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(events.append)
    ctx = StateContext(
        ticket=repo.get_ticket(ticket.id),
        instance=merging_instance,
        repo=repo,
        clock=_clock,
        bus=bus,
        git=git,
    )
    merging_state = MergingState()
    merging_state._pr_number_for = lambda _ctx: 10  # type: ignore[method-assign]
    result = merging_state.transition(ctx)
    assert result is not None
    assert result.state_name == "MergeQueued"

    merge_queued_row = repo.list_state_instances_for_ticket(ticket.id)[-1]
    assert merge_queued_row.state_name == "MergeQueued"
    assert merge_queued_row.is_in_flight

    # Simulate a generic startup reconcile closing the row as a crash
    # orphan before this coordinator's own reconcile/tick runs.
    repo.close_state_instance(merge_queued_row.id, now=_NOW)
    events.clear()  # drop the entry-side StateEnteredEvent -- only exit matters here

    # Drain -- mergeable + CI-passing, so one tick merges and routes to Done.
    _coordinator(repo, git, bus=bus).tick()

    exited = [
        e for e in events if isinstance(e, StateExitedEvent) and e.state_name == "MergeQueued"
    ]
    assert len(exited) == 1, (
        "exit must still fire (and the label still get removed) even when the real row was "
        "already closed"
    )
    assert exited[0].instance_id != merge_queued_row.id, (
        "an already-closed row must not be reused or re-closed -- exit must fall back to the "
        "synthetic per-tick instance"
    )
    assert repo.get_ticket(ticket.id).current_state == "Done"

    # Closing it again must be a safe no-op: the row is exactly as closed
    # as it was before ``_route`` ran, not double-closed or errored.
    still_closed = repo.get_state_instance(merge_queued_row.id)
    assert not still_closed.is_in_flight


def test_route_falls_back_to_synthetic_instance_when_no_real_row_was_opened():
    """Every other test in this file (and any operator/CLI path that
    parks a ticket in MergeQueued without going through
    ``state._enter_merge_queued`` -- e.g. a generic crash-recovery pass
    that already closed the row) leaves no real journaled row for
    ``_route`` to find. The exit event must still fire (using a
    synthetic instance) rather than being skipped or raising -- label
    removal must not depend on a journal row existing."""
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="impl", mergeable=True, ci_passing=True
    )
    assert repo.list_state_instances_for_ticket(ticket.id) == []  # sanity: no real row

    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(events.append)
    _coordinator(repo, git, bus=bus).tick()

    exited = [
        e for e in events if isinstance(e, StateExitedEvent) and e.state_name == "MergeQueued"
    ]
    assert len(exited) == 1
    assert exited[0].ticket_id == ticket.id


def test_reconcile_entry_drain_publishes_state_exited_event_for_merge_queued():
    """Criterion 4: crash-recovery drain (``_reconcile_entry`` ->
    ``_route``) must behave exactly like a normal ``tick()`` drain for
    the merge-queued label -- it already shares ``_route``, so this pins
    that sharing actually covers the label-exit event too."""
    repo, git = _fake()
    ticket = _seed_ticket_in_merge_queue(
        repo, git, issue_number=7, pr=10, kind="impl", mergeable=True, ci_passing=True
    )
    entry = repo.head_merge_entry("p")
    assert entry is not None
    repo.mark_merge_active(entry.id)
    git.set_pr_state(
        project="p",
        pr_number=10,
        state=PRState(
            merged=True,
            mergeable=True,
            ci_passing=True,
            base_ref="main",
            mergeable_state="clean",
        ),
    )

    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(events.append)
    _coordinator(repo, git, bus=bus).reconcile_on_startup()

    exited = [
        e for e in events if isinstance(e, StateExitedEvent) and e.state_name == "MergeQueued"
    ]
    assert len(exited) == 1
    assert exited[0].ticket_id == ticket.id
    assert repo.get_ticket(ticket.id).current_state == "Done"
