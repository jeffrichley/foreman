"""foreman migrate-v4-to-v5 — one-shot SQLite -> Postgres port.

Ports tickets + state_instances. Idempotent on (project, issue_number).
Does NOT port the events archive (history; large; low value). State-
instance integer ids are NOT preserved — Postgres assigns fresh
BIGSERIAL ids; sequence ordering within a ticket is preserved, which is
what the state machine relies on.
"""

from __future__ import annotations

from pathlib import Path

import typer

from foreman.v4.postgres_repository import PostgresTicketRepository
from foreman.v4.repository import TicketAlreadyExistsError
from foreman.v4.sqlite_repository import SqliteTicketRepository


def migrate_v4_to_v5(*, sqlite_path: Path, postgres_dsn: str) -> int:
    """Port tickets + state-instances. Returns the count of tickets ported."""
    src = SqliteTicketRepository.at_path(sqlite_path)
    dst = PostgresTicketRepository.from_dsn(postgres_dsn)
    ported = 0
    for ticket in src.list_all_tickets():
        try:
            new = dst.create_ticket(
                project=ticket.project,
                issue_number=ticket.issue_number,
                now=ticket.created_at,
            )
        except TicketAlreadyExistsError:
            continue  # idempotent skip
        if ticket.current_state != "Queued":
            dst.set_ticket_state(new.id, ticket.current_state, now=ticket.updated_at)
        if ticket.depends_on:
            dst.set_ticket_dependencies(new.id, deps=ticket.depends_on)
        for inst in src.list_state_instances_for_ticket(ticket.id):
            new_inst = dst.open_state_instance(
                ticket_id=new.id,
                state_name=inst.state_name,
                sequence=inst.sequence,
                now=inst.entered_at,
            )
            if inst.outcome_kind is not None and inst.next_state is not None:
                dst.mark_execute_completed(
                    new_inst.id,
                    now=inst.execute_completed_at or inst.entered_at,
                    outcome_kind=inst.outcome_kind,
                    outcome_payload=inst.outcome_payload or {},
                    next_state=inst.next_state,
                )
            if inst.exited_at is not None:
                dst.close_state_instance(new_inst.id, now=inst.exited_at)
        ported += 1
    return ported


def cmd_migrate_v4_to_v5(
    sqlite_path: Path = typer.Option(..., "--sqlite-path"),  # noqa: B008
    postgres_dsn: str = typer.Option(..., "--postgres-url"),
) -> None:
    """One-shot port of v4 SQLite tickets + state-instances into Postgres."""
    count = migrate_v4_to_v5(sqlite_path=sqlite_path, postgres_dsn=postgres_dsn)
    typer.echo(f"migrated {count} ticket(s) to postgres")
