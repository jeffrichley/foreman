import datetime as dt

from foreman.v4.reconcile import reconcile_on_startup
from foreman.v4.repository import InMemoryTicketRepository


def test_reconcile_closes_orphans_as_crash_recovery():
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    repo = InMemoryTicketRepository()
    t = repo.create_ticket(project="p", issue_number=1, now=now)
    inst = repo.open_state_instance(
        ticket_id=t.id, state_name="Implementing", sequence=1, now=now,
    )  # left in-flight, as a crash would
    assert repo.list_in_flight_state_instances()  # precondition

    recovered = reconcile_on_startup(repo, clock=lambda: now)

    assert recovered == 1
    assert repo.list_in_flight_state_instances() == []          # closed
    closed = [i for i in repo.list_state_instances_for_ticket(t.id) if i.id == inst.id][0]
    assert closed.failure_phase == "crash_recovery"
    assert closed.exited_at is not None


def test_reconcile_noop_when_no_orphans():
    repo = InMemoryTicketRepository()
    assert reconcile_on_startup(repo, clock=lambda: dt.datetime(2026, 1, 1, tzinfo=dt.UTC)) == 0
