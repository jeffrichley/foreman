"""foreman migrate-v4-to-v5 — one-shot SQLite -> Postgres port.

Ports tickets + state_instances. Idempotent on (project, issue_number).
Does NOT port the events archive (history; large; low value). State-
instance integer ids are NOT preserved — Postgres assigns fresh
BIGSERIAL ids; sequence ordering within a ticket is preserved, which is
what the state machine relies on.

Not crash-atomic per ticket; if a run crashes mid-ticket, re-running
detects the partial (instance-count mismatch) and errors loudly — drop
the destination tables and re-run (the SQLite source is never mutated).
"""

from __future__ import annotations

from pathlib import Path

import typer

from foreman.v4.postgres_repository import PostgresTicketRepository
from foreman.v4.repository import TicketAlreadyExistsError
from foreman.v4.sqlite_repository import SqliteTicketRepository


class MigrationError(Exception):
    """Raised when a partial (half-migrated) ticket is detected on re-run."""


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
            # Ticket row already exists. This is either a normal idempotent
            # re-run (fully migrated) or a partial migration from a prior
            # crash (ticket row committed, but not all its instances). Verify
            # by comparing instance counts; fail loud on a mismatch rather
            # than silently leaving a half-populated journal.
            existing = dst.get_ticket_by_issue(
                project=ticket.project, issue_number=ticket.issue_number
            )
            dst_count = len(dst.list_state_instances_for_ticket(existing.id))
            src_count = len(src.list_state_instances_for_ticket(ticket.id))
            if dst_count != src_count:
                raise MigrationError(
                    f"ticket {ticket.project}#{ticket.issue_number} was partially "
                    f"migrated (dst has {dst_count} instances, src has {src_count}); "
                    "drop the destination tickets table and re-run, or manually repair"
                ) from None
            continue  # fully migrated — clean idempotent skip
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
            if inst.execute_started_at is not None:
                dst.mark_execute_started(new_inst.id, now=inst.execute_started_at)
            if inst.outcome_kind is not None and inst.next_state is not None:
                dst.mark_execute_completed(
                    new_inst.id,
                    now=inst.execute_completed_at or inst.entered_at,
                    outcome_kind=inst.outcome_kind,
                    outcome_payload=inst.outcome_payload or {},
                    next_state=inst.next_state,
                )
            if inst.failure_phase is not None:
                dst.record_failure(
                    new_inst.id,
                    now=inst.execute_completed_at or inst.entered_at,
                    failure_phase=inst.failure_phase,
                    failure_reason=inst.failure_reason or "",
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
