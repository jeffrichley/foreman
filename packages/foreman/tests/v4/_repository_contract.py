"""Shared TicketRepository contract suite.

Both InMemoryTicketRepository and SqliteTicketRepository must satisfy
every assertion here. Test files subclass ``RepositoryContract`` and bind
``factory`` to their concrete repo constructor; pytest collects the
contract tests against that binding.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

import pytest

from foreman.v4.outcome import OutcomeKind
from foreman.v4.repository import (
    StateInstanceNotFoundError,
    TicketAlreadyExistsError,
    TicketNotFoundError,
    TicketRepository,
)

RepoFactory = Callable[[], TicketRepository]


def _now() -> dt.datetime:
    return dt.datetime(2026, 6, 13, 12, 0, 0)


class RepositoryContract:
    """Mixin: subclass and override ``factory`` to bind a concrete repo."""

    factory: RepoFactory

    @pytest.fixture()
    def repo(self) -> TicketRepository:
        return self.factory()

    def test_create_ticket_returns_record_with_id(self, repo: TicketRepository) -> None:
        ticket = repo.create_ticket(project="foreman", issue_number=42, now=_now())
        assert ticket.id > 0
        assert ticket.project == "foreman"
        assert ticket.issue_number == 42
        assert ticket.current_state == "Queued"
        assert ticket.held_by is None

    def test_create_ticket_duplicate_raises(self, repo: TicketRepository) -> None:
        repo.create_ticket(project="foreman", issue_number=1, now=_now())
        with pytest.raises(TicketAlreadyExistsError):
            repo.create_ticket(project="foreman", issue_number=1, now=_now())

    def test_get_ticket_missing_raises(self, repo: TicketRepository) -> None:
        with pytest.raises(TicketNotFoundError):
            repo.get_ticket(999)

    def test_get_state_instance_missing_raises(self, repo: TicketRepository) -> None:
        with pytest.raises(StateInstanceNotFoundError):
            repo.get_state_instance(999)

    def test_get_ticket_by_issue(self, repo: TicketRepository) -> None:
        created = repo.create_ticket(project="foreman", issue_number=7, now=_now())
        fetched = repo.get_ticket_by_issue(project="foreman", issue_number=7)
        assert fetched == created

    def test_list_open_tickets_excludes_done_and_failed(
        self, repo: TicketRepository
    ) -> None:
        a = repo.create_ticket(project="p", issue_number=1, now=_now())
        b = repo.create_ticket(project="p", issue_number=2, now=_now())
        c = repo.create_ticket(project="p", issue_number=3, now=_now())
        repo.set_ticket_state(a.id, "Done", now=_now())
        repo.set_ticket_state(b.id, "Failed", now=_now())
        open_tickets = repo.list_open_tickets()
        assert {t.issue_number for t in open_tickets} == {c.issue_number}

    def test_set_ticket_state_updates(self, repo: TicketRepository) -> None:
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        repo.set_ticket_state(t.id, "Planning", now=dt.datetime(2026, 6, 13, 13))
        fetched = repo.get_ticket(t.id)
        assert fetched.current_state == "Planning"
        assert fetched.updated_at == dt.datetime(2026, 6, 13, 13)

    def test_hold_and_resume(self, repo: TicketRepository) -> None:
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        repo.hold_ticket(t.id, held_by="jeff", reason="vacation", now=_now())
        held = repo.get_ticket(t.id)
        assert held.is_held
        assert held.held_by == "jeff"
        assert held.held_reason == "vacation"
        repo.resume_ticket(t.id, now=_now())
        resumed = repo.get_ticket(t.id)
        assert not resumed.is_held
        assert resumed.held_by is None

    def test_open_state_instance(self, repo: TicketRepository) -> None:
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        instance = repo.open_state_instance(
            ticket_id=t.id, state_name="Planning", sequence=1, now=_now()
        )
        assert instance.id > 0
        assert instance.is_in_flight
        assert instance.entered_at == _now()

    def test_state_instance_lifecycle_timestamps(self, repo: TicketRepository) -> None:
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        inst = repo.open_state_instance(
            ticket_id=t.id, state_name="Planning", sequence=1, now=_now()
        )
        repo.mark_execute_started(inst.id, now=dt.datetime(2026, 6, 13, 12, 1))
        repo.mark_execute_completed(
            inst.id,
            now=dt.datetime(2026, 6, 13, 12, 5),
            outcome_kind=OutcomeKind.CLEAN,
            outcome_payload={"summary": "spec PR open"},
            next_state="SpecReview",
        )
        repo.close_state_instance(inst.id, now=dt.datetime(2026, 6, 13, 12, 6))
        closed = repo.get_state_instance(inst.id)
        assert closed.execute_started_at == dt.datetime(2026, 6, 13, 12, 1)
        assert closed.execute_completed_at == dt.datetime(2026, 6, 13, 12, 5)
        assert closed.exited_at == dt.datetime(2026, 6, 13, 12, 6)
        assert closed.outcome_kind == OutcomeKind.CLEAN
        assert closed.next_state == "SpecReview"
        assert closed.outcome_payload == {"summary": "spec PR open"}
        assert not closed.is_in_flight

    def test_list_in_flight_state_instances(self, repo: TicketRepository) -> None:
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        done = repo.open_state_instance(
            ticket_id=t.id, state_name="Queued", sequence=1, now=_now()
        )
        repo.mark_execute_started(done.id, now=_now())
        repo.mark_execute_completed(
            done.id,
            now=_now(),
            outcome_kind=OutcomeKind.CLEAN,
            outcome_payload={},
            next_state="Planning",
        )
        repo.close_state_instance(done.id, now=_now())
        in_flight = repo.open_state_instance(
            ticket_id=t.id, state_name="Planning", sequence=2, now=_now()
        )
        rows = repo.list_in_flight_state_instances()
        assert [r.id for r in rows] == [in_flight.id]

    def test_record_failure_writes_phase_and_reason(
        self, repo: TicketRepository
    ) -> None:
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        inst = repo.open_state_instance(
            ticket_id=t.id, state_name="Planning", sequence=1, now=_now()
        )
        repo.record_failure(
            inst.id,
            now=_now(),
            failure_phase="execute",
            failure_reason="subprocess timed out",
        )
        fetched = repo.get_state_instance(inst.id)
        assert fetched.failure_phase == "execute"
        assert fetched.failure_reason == "subprocess timed out"

    def test_latest_pr_number_for_ticket_returns_most_recent(self, repo: TicketRepository):
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        # First state has no PR
        i1 = repo.open_state_instance(
            ticket_id=t.id, state_name="Queued", sequence=1, now=_now(),
        )
        repo.mark_execute_completed(
            i1.id, now=_now(),
            outcome_kind=OutcomeKind.CLEAN,
            outcome_payload={"artifacts": {}},
            next_state="Planning",
        )
        repo.close_state_instance(i1.id, now=_now())
        # Second state records PR 42
        i2 = repo.open_state_instance(
            ticket_id=t.id, state_name="Planning", sequence=2, now=_now(),
        )
        repo.mark_execute_completed(
            i2.id, now=_now(),
            outcome_kind=OutcomeKind.CLEAN,
            outcome_payload={"artifacts": {"pr_number": 42}},
            next_state="SpecReview",
        )
        repo.close_state_instance(i2.id, now=_now())
        assert repo.latest_pr_number_for_ticket(t.id) == 42

    def test_latest_pr_number_returns_none_when_no_outcomes(self, repo: TicketRepository):
        t = repo.create_ticket(project="p", issue_number=2, now=_now())
        assert repo.latest_pr_number_for_ticket(t.id) is None

    def test_latest_pr_number_skips_outcomes_without_pr(self, repo: TicketRepository):
        t = repo.create_ticket(project="p", issue_number=3, now=_now())
        # Most recent outcome has no PR; earlier outcome had PR 7 — return 7.
        i1 = repo.open_state_instance(
            ticket_id=t.id, state_name="Queued", sequence=1, now=_now(),
        )
        repo.mark_execute_completed(
            i1.id, now=_now(),
            outcome_kind=OutcomeKind.CLEAN,
            outcome_payload={"artifacts": {"pr_number": 7}},
            next_state="Planning",
        )
        repo.close_state_instance(i1.id, now=_now())
        i2 = repo.open_state_instance(
            ticket_id=t.id, state_name="Planning", sequence=2, now=_now(),
        )
        repo.mark_execute_completed(
            i2.id, now=_now(),
            outcome_kind=OutcomeKind.CLEAN,
            outcome_payload={"artifacts": {}},
            next_state="SpecReview",
        )
        repo.close_state_instance(i2.id, now=_now())
        assert repo.latest_pr_number_for_ticket(t.id) == 7

    def test_count_state_instances_for_ticket(self, repo: TicketRepository):
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        assert repo.count_state_instances_for_ticket(t.id) == 0
        repo.open_state_instance(
            ticket_id=t.id, state_name="Queued", sequence=1, now=_now(),
        )
        assert repo.count_state_instances_for_ticket(t.id) == 1

    def test_count_consecutive_same_state_returns_zero_for_no_instances(
        self, repo: TicketRepository
    ):
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        assert repo.count_consecutive_same_state(
            ticket_id=t.id, state="SpecReview"
        ) == 0

    def test_count_consecutive_same_state_counts_run_correctly(
        self, repo: TicketRepository
    ):
        """Walk back from latest sequence; stop at first non-match.

        History: SpecReview, SpecReview, SpecReview, Planning, SpecReview,
        SpecReview. Walking back from sequence=6 we see two SpecReviews,
        then Planning breaks the run. Only the trailing two count.
        """
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        for seq, name in enumerate(
            ["SpecReview", "SpecReview", "SpecReview", "Planning",
             "SpecReview", "SpecReview"],
            start=1,
        ):
            repo.open_state_instance(
                ticket_id=t.id, state_name=name, sequence=seq, now=_now(),
            )
        assert repo.count_consecutive_same_state(
            ticket_id=t.id, state="SpecReview"
        ) == 2
        # A different state name never matched the latest sequence.
        assert repo.count_consecutive_same_state(
            ticket_id=t.id, state="Planning"
        ) == 0

    def test_count_consecutive_same_state_full_history_match(
        self, repo: TicketRepository
    ):
        """Every row matches → count equals total instances."""
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        for seq in range(1, 4):
            repo.open_state_instance(
                ticket_id=t.id, state_name="SpecReview", sequence=seq,
                now=_now(),
            )
        assert repo.count_consecutive_same_state(
            ticket_id=t.id, state="SpecReview"
        ) == 3

    def test_set_and_get_dependencies(self, repo: TicketRepository):
        a = repo.create_ticket(project="p", issue_number=1, now=_now())
        b = repo.create_ticket(project="p", issue_number=2, now=_now())
        c = repo.create_ticket(project="p", issue_number=3, now=_now())
        repo.set_ticket_dependencies(c.id, deps=[a.id, b.id])
        assert repo.get_ticket_dependencies(c.id) == [a.id, b.id]

    def test_dependencies_default_empty(self, repo: TicketRepository):
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        assert repo.get_ticket_dependencies(t.id) == []

    def test_list_unmet_dependencies_excludes_done_tickets(self, repo: TicketRepository):
        a = repo.create_ticket(project="p", issue_number=1, now=_now())
        b = repo.create_ticket(project="p", issue_number=2, now=_now())
        c = repo.create_ticket(project="p", issue_number=3, now=_now())
        repo.set_ticket_dependencies(c.id, deps=[a.id, b.id])
        # Both deps still in flight → both unmet
        assert sorted(repo.list_unmet_dependencies(c.id)) == sorted([a.id, b.id])
        # Move A to Done → only B unmet
        repo.set_ticket_state(a.id, "Done", now=_now())
        assert repo.list_unmet_dependencies(c.id) == [b.id]
        # Move B to Done → all met
        repo.set_ticket_state(b.id, "Done", now=_now())
        assert repo.list_unmet_dependencies(c.id) == []

    def test_list_unmet_does_not_count_failed_or_needs_help(self, repo: TicketRepository):
        """Only Done satisfies a dep — Failed/NeedsHelp stays blocked."""
        a = repo.create_ticket(project="p", issue_number=1, now=_now())
        b = repo.create_ticket(project="p", issue_number=2, now=_now())
        repo.set_ticket_dependencies(b.id, deps=[a.id])
        repo.set_ticket_state(a.id, "Failed", now=_now())
        assert repo.list_unmet_dependencies(b.id) == [a.id]

    def test_set_ticket_dependencies_raises_for_missing_ticket(self, repo: TicketRepository):
        with pytest.raises(TicketNotFoundError):
            repo.set_ticket_dependencies(9999, deps=[1, 2])

    def test_list_unmet_dependencies_raises_for_ghost_dependency(self, repo: TicketRepository):
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        repo.set_ticket_dependencies(t.id, deps=[9999])
        with pytest.raises(TicketNotFoundError):
            repo.list_unmet_dependencies(t.id)

    def test_list_all_tickets_includes_terminal(self, repo: TicketRepository):
        now = _now()
        open_t = repo.create_ticket(project="p", issue_number=1, now=now)
        done_t = repo.create_ticket(project="p", issue_number=2, now=now)
        repo.set_ticket_state(open_t.id, "Planning", now=now)
        repo.set_ticket_state(done_t.id, "Done", now=now)
        all_tickets = repo.list_all_tickets()
        ids = {t.id for t in all_tickets}
        assert open_t.id in ids
        assert done_t.id in ids

    def test_list_all_tickets_returns_empty_when_no_tickets(self, repo: TicketRepository):
        assert repo.list_all_tickets() == []

    def test_list_state_instances_for_ticket_returns_in_sequence_order(self, repo: TicketRepository):
        now = _now()
        t = repo.create_ticket(project="p", issue_number=1, now=now)
        inst1 = repo.open_state_instance(
            ticket_id=t.id, state_name="Queued", sequence=1, now=now,
        )
        repo.mark_execute_completed(
            inst1.id, now=now, outcome_kind=OutcomeKind.CLEAN,
            outcome_payload={"summary": "ok"}, next_state="Planning",
        )
        repo.close_state_instance(inst1.id, now=now)
        inst2 = repo.open_state_instance(
            ticket_id=t.id, state_name="Planning", sequence=2, now=now,
        )
        instances = repo.list_state_instances_for_ticket(t.id)
        assert [i.sequence for i in instances] == [1, 2]
        assert instances[0].id == inst1.id
        assert instances[1].id == inst2.id

    def test_list_state_instances_for_ticket_empty_for_no_journal(self, repo: TicketRepository):
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        assert repo.list_state_instances_for_ticket(t.id) == []
