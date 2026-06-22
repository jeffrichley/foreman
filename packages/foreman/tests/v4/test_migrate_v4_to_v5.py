import datetime as dt

from foreman.v4.cli.migrate import migrate_v4_to_v5
from foreman.v4.postgres_repository import PostgresTicketRepository
from foreman.v4.sqlite_repository import SqliteTicketRepository

from .postgres_fixture import clean_postgres_dsn, postgres_dsn  # noqa: F401  (fixture imports)


def test_migrate_ports_tickets_and_instances(tmp_path, clean_postgres_dsn):  # noqa: F811  (pytest fixture reuse)
    now = dt.datetime(2026, 6, 21, tzinfo=dt.UTC)
    src = SqliteTicketRepository.at_path(tmp_path / "state.db")
    t = src.create_ticket(project="p", issue_number=7, now=now)
    src.set_ticket_state(t.id, "NeedsHelp", now=now)
    src.open_state_instance(ticket_id=t.id, state_name="Queued", sequence=0, now=now)

    migrated = migrate_v4_to_v5(sqlite_path=tmp_path / "state.db", postgres_dsn=clean_postgres_dsn)
    assert migrated == 1

    dst = PostgresTicketRepository.from_dsn(clean_postgres_dsn, pool_min=1, pool_max=4)
    ported = dst.get_ticket_by_issue(project="p", issue_number=7)
    assert ported.current_state == "NeedsHelp"
    assert len(dst.list_state_instances_for_ticket(ported.id)) == 1


def test_migrate_is_idempotent(tmp_path, clean_postgres_dsn):  # noqa: F811  (pytest fixture reuse)
    now = dt.datetime(2026, 6, 21, tzinfo=dt.UTC)
    src = SqliteTicketRepository.at_path(tmp_path / "state.db")
    src.create_ticket(project="p", issue_number=7, now=now)
    migrate_v4_to_v5(sqlite_path=tmp_path / "state.db", postgres_dsn=clean_postgres_dsn)
    again = migrate_v4_to_v5(sqlite_path=tmp_path / "state.db", postgres_dsn=clean_postgres_dsn)
    assert again == 0
