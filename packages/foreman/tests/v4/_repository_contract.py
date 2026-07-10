"""Shared TicketRepository contract suite.

Both InMemoryTicketRepository and PostgresTicketRepository must satisfy
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

    def test_list_open_tickets_excludes_done_and_failed(self, repo: TicketRepository) -> None:
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
        done = repo.open_state_instance(ticket_id=t.id, state_name="Queued", sequence=1, now=_now())
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

    def test_record_failure_writes_phase_and_reason(self, repo: TicketRepository) -> None:
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

    def test_set_session_id_round_trips(self, repo: TicketRepository) -> None:
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        inst = repo.open_state_instance(
            ticket_id=t.id, state_name="Implementing", sequence=1, now=_now()
        )
        assert inst.session_id is None
        repo.set_session_id(inst.id, "sess-abc")
        rows = repo.list_state_instances_for_ticket(t.id)
        matching = next(r for r in rows if r.id == inst.id)
        assert matching.session_id == "sess-abc"

    def test_latest_pr_number_for_ticket_returns_most_recent(self, repo: TicketRepository):
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        # First state has no PR
        i1 = repo.open_state_instance(
            ticket_id=t.id,
            state_name="Queued",
            sequence=1,
            now=_now(),
        )
        repo.mark_execute_completed(
            i1.id,
            now=_now(),
            outcome_kind=OutcomeKind.CLEAN,
            outcome_payload={"artifacts": {}},
            next_state="Planning",
        )
        repo.close_state_instance(i1.id, now=_now())
        # Second state records PR 42
        i2 = repo.open_state_instance(
            ticket_id=t.id,
            state_name="Planning",
            sequence=2,
            now=_now(),
        )
        repo.mark_execute_completed(
            i2.id,
            now=_now(),
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
            ticket_id=t.id,
            state_name="Queued",
            sequence=1,
            now=_now(),
        )
        repo.mark_execute_completed(
            i1.id,
            now=_now(),
            outcome_kind=OutcomeKind.CLEAN,
            outcome_payload={"artifacts": {"pr_number": 7}},
            next_state="Planning",
        )
        repo.close_state_instance(i1.id, now=_now())
        i2 = repo.open_state_instance(
            ticket_id=t.id,
            state_name="Planning",
            sequence=2,
            now=_now(),
        )
        repo.mark_execute_completed(
            i2.id,
            now=_now(),
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
            ticket_id=t.id,
            state_name="Queued",
            sequence=1,
            now=_now(),
        )
        assert repo.count_state_instances_for_ticket(t.id) == 1

    def test_count_consecutive_same_state_returns_zero_for_no_instances(
        self, repo: TicketRepository
    ):
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        assert repo.count_consecutive_same_state(ticket_id=t.id, state="SpecReview") == 0

    def test_count_consecutive_same_state_counts_run_correctly(self, repo: TicketRepository):
        """Walk back from latest sequence; stop at first non-match.

        History: SpecReview, SpecReview, SpecReview, Planning, SpecReview,
        SpecReview. Walking back from sequence=6 we see two SpecReviews,
        then Planning breaks the run. Only the trailing two count.
        """
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        for seq, name in enumerate(
            ["SpecReview", "SpecReview", "SpecReview", "Planning", "SpecReview", "SpecReview"],
            start=1,
        ):
            repo.open_state_instance(
                ticket_id=t.id,
                state_name=name,
                sequence=seq,
                now=_now(),
            )
        assert repo.count_consecutive_same_state(ticket_id=t.id, state="SpecReview") == 2
        # A different state name never matched the latest sequence.
        assert repo.count_consecutive_same_state(ticket_id=t.id, state="Planning") == 0

    def test_count_consecutive_same_state_full_history_match(self, repo: TicketRepository):
        """Every row matches → count equals total instances."""
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        for seq in range(1, 4):
            repo.open_state_instance(
                ticket_id=t.id,
                state_name="SpecReview",
                sequence=seq,
                now=_now(),
            )
        assert repo.count_consecutive_same_state(ticket_id=t.id, state="SpecReview") == 3

    def test_count_consecutive_same_state_skips_can_run_failures(self, repo: TicketRepository):
        """Phase 8d.15 (F4): can_run-failed rows mean "ticket was held,
        state never ran". They MUST NOT count toward the runaway-defense
        consecutive-failures cap (and must not break the run either) —
        otherwise holding a ticket for cap+ polls trips the cap on
        resume."""
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        # 3 can_run-failed rows (operator hold polled 3 times).
        for seq in range(1, 4):
            inst = repo.open_state_instance(
                ticket_id=t.id,
                state_name="SpecReview",
                sequence=seq,
                now=_now(),
            )
            repo.record_failure(
                inst.id,
                now=_now(),
                failure_phase="can_run",
                failure_reason="held",
            )
            repo.close_state_instance(inst.id, now=_now())
        # Cap-relevant count: 0 — operator's hold polls don't count.
        assert repo.count_consecutive_same_state(ticket_id=t.id, state="SpecReview") == 0

        # Add one real execute-failure on top.
        inst = repo.open_state_instance(
            ticket_id=t.id,
            state_name="SpecReview",
            sequence=4,
            now=_now(),
        )
        repo.record_failure(
            inst.id,
            now=_now(),
            failure_phase="execute",
            failure_reason="role crashed",
        )
        repo.close_state_instance(inst.id, now=_now())
        # Now count is 1 — the held-period rows are still skipped, but
        # the real failure counts.
        assert repo.count_consecutive_same_state(ticket_id=t.id, state="SpecReview") == 1

    def test_count_consecutive_same_state_skips_blocked_outcomes(self, repo: TicketRepository):
        """Phase 8d.18: rows whose ``outcome_kind == 'blocked'`` record a
        legitimate async-polling self-loop (e.g. MergingState polling a
        pending merge verdict; ImplementingState polling impl-PR CI).
        They MUST NOT count toward the runaway-defense consecutive-
        failures cap — the state RAN, emitted "still waiting", and asked
        to be re-tried. Without this skip, a few polling cycles trip the
        cap and escalate to NeedsHelp (defeating the polling intent).
        """
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        # 5 polling-loop rows: each ran execute() and emitted BLOCKED.
        for seq in range(1, 6):
            inst = repo.open_state_instance(
                ticket_id=t.id,
                state_name="Merging",
                sequence=seq,
                now=_now(),
            )
            repo.mark_execute_completed(
                inst.id,
                now=_now(),
                outcome_kind=OutcomeKind.BLOCKED,
                outcome_payload={"summary": "still polling"},
                next_state="Merging",
            )
            repo.close_state_instance(inst.id, now=_now())
        # All 5 are async-polling re-entries — none are runaway-defense
        # signal.
        assert repo.count_consecutive_same_state(ticket_id=t.id, state="Merging") == 0

    def test_count_consecutive_same_state_counts_after_one_execute_failure(
        self, repo: TicketRepository
    ):
        """Phase 8d.18: BLOCKED rows are skipped; a real execute-phase
        failure between them still counts. Sequence: 2 BLOCKED → 1 execute
        failure → 2 BLOCKED. Walking back, the BLOCKED rows are skipped
        (and don't break the run), the execute-failure row counts (=1),
        and the search continues past it — but the next non-BLOCKED row
        does not exist, so the final count is 1.
        """
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        # 2 BLOCKED polling rows.
        for seq in (1, 2):
            inst = repo.open_state_instance(
                ticket_id=t.id,
                state_name="Merging",
                sequence=seq,
                now=_now(),
            )
            repo.mark_execute_completed(
                inst.id,
                now=_now(),
                outcome_kind=OutcomeKind.BLOCKED,
                outcome_payload={"summary": "polling"},
                next_state="Merging",
            )
            repo.close_state_instance(inst.id, now=_now())
        # 1 real execute-phase failure.
        inst = repo.open_state_instance(
            ticket_id=t.id,
            state_name="Merging",
            sequence=3,
            now=_now(),
        )
        repo.record_failure(
            inst.id,
            now=_now(),
            failure_phase="execute",
            failure_reason="something blew up",
        )
        repo.close_state_instance(inst.id, now=_now())
        # 2 more BLOCKED polling rows after the failure.
        for seq in (4, 5):
            inst = repo.open_state_instance(
                ticket_id=t.id,
                state_name="Merging",
                sequence=seq,
                now=_now(),
            )
            repo.mark_execute_completed(
                inst.id,
                now=_now(),
                outcome_kind=OutcomeKind.BLOCKED,
                outcome_payload={"summary": "polling"},
                next_state="Merging",
            )
            repo.close_state_instance(inst.id, now=_now())
        # Only the real execute-failure row contributes — the BLOCKED
        # rows on both sides are skipped.
        assert repo.count_consecutive_same_state(ticket_id=t.id, state="Merging") == 1

    def test_crash_recovery_rows_are_exempt_from_consecutive_same_state(
        self, repo: TicketRepository
    ):
        """A reconciled crash-orphan has ``failure_phase='crash_recovery'``,
        ``outcome_kind=None``. A daemon restart is not a ticket failure, so
        these rows MUST NOT count toward the runaway-cap counter (and must
        not break the run). ``count_consecutive_transient_provider_errors``
        already skips ``outcome_kind is None``, so it covers crash-orphans
        with no change — assert that too.
        """
        now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        ticket = repo.create_ticket(project="p", issue_number=1, now=now)
        # Three crash-orphans on the same state, each closed by reconciliation:
        for seq in (1, 2, 3):
            inst = repo.open_state_instance(
                ticket_id=ticket.id,
                state_name="Implementing",
                sequence=seq,
                now=now,
            )
            repo.record_failure(
                inst.id,
                now=now,
                failure_phase="crash_recovery",
                failure_reason="daemon crash",
            )
            repo.close_state_instance(inst.id, now=now)
        # All three are crash_recovery → none count toward the cap.
        assert (
            repo.count_consecutive_same_state(
                ticket_id=ticket.id,
                state="Implementing",
            )
            == 0
        )
        # And the transient counter (already None-outcome-skipping) is also 0.
        assert repo.count_consecutive_transient_provider_errors(ticket.id) == 0

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

    def test_list_state_instances_for_ticket_returns_in_sequence_order(
        self, repo: TicketRepository
    ):
        now = _now()
        t = repo.create_ticket(project="p", issue_number=1, now=now)
        inst1 = repo.open_state_instance(
            ticket_id=t.id,
            state_name="Queued",
            sequence=1,
            now=now,
        )
        repo.mark_execute_completed(
            inst1.id,
            now=now,
            outcome_kind=OutcomeKind.CLEAN,
            outcome_payload={"summary": "ok"},
            next_state="Planning",
        )
        repo.close_state_instance(inst1.id, now=now)
        inst2 = repo.open_state_instance(
            ticket_id=t.id,
            state_name="Planning",
            sequence=2,
            now=now,
        )
        instances = repo.list_state_instances_for_ticket(t.id)
        assert [i.sequence for i in instances] == [1, 2]
        assert instances[0].id == inst1.id
        assert instances[1].id == inst2.id

    def test_list_state_instances_for_ticket_empty_for_no_journal(self, repo: TicketRepository):
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        assert repo.list_state_instances_for_ticket(t.id) == []

    def test_delete_ticket_removes_row(self, repo: TicketRepository) -> None:
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        repo.delete_ticket(t.id)
        with pytest.raises(TicketNotFoundError):
            repo.get_ticket(t.id)

    def test_delete_ticket_clears_by_issue_lookup(self, repo: TicketRepository) -> None:
        t = repo.create_ticket(project="p", issue_number=42, now=_now())
        repo.delete_ticket(t.id)
        with pytest.raises(TicketNotFoundError):
            repo.get_ticket_by_issue(project="p", issue_number=42)
        # After delete, the (project, issue_number) slot is free again.
        recreated = repo.create_ticket(project="p", issue_number=42, now=_now())
        assert recreated.issue_number == 42

    def test_delete_ticket_cascades_state_instances(
        self,
        repo: TicketRepository,
    ) -> None:
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        repo.open_state_instance(
            ticket_id=t.id,
            state_name="Planning",
            sequence=1,
            now=_now(),
        )
        repo.open_state_instance(
            ticket_id=t.id,
            state_name="SpecReview",
            sequence=2,
            now=_now(),
        )
        repo.delete_ticket(t.id)
        assert repo.list_state_instances_for_ticket(t.id) == []

    def test_delete_ticket_missing_raises(self, repo: TicketRepository) -> None:
        with pytest.raises(TicketNotFoundError):
            repo.delete_ticket(9999)

    def test_append_event_round_trips(self, repo: TicketRepository) -> None:
        now = dt.datetime(2026, 6, 21, 12, 0, tzinfo=dt.UTC)
        ticket = repo.create_ticket(project="p", issue_number=1, now=now)
        inst = repo.open_state_instance(
            ticket_id=ticket.id, state_name="Queued", sequence=0, now=now
        )
        repo.append_event(
            ticket_id=ticket.id,
            instance_id=inst.id,
            event_type="StateEntered",
            state_name="Queued",
            sequence=0,
            at=now,
            payload={"foo": "bar"},
        )
        events = repo.list_events_for_ticket(ticket.id)
        assert len(events) == 1
        assert events[0]["event_type"] == "StateEntered"
        assert events[0]["payload"] == {"foo": "bar"}
