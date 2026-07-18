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
from foreman.v4.events import StateEnteredEvent
from foreman.v4.git_provider import FakeGitProvider, PRState, RequiredCheckState
from foreman.v4.merge_coordinator import MergeCoordinator
from foreman.v4.queue_manager import QueueManager
from foreman.v4.records import TicketRecord
from foreman.v4.repository import InMemoryTicketRepository, MergeQueueEntry
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
