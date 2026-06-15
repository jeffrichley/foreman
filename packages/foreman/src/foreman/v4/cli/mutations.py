"""hold/resume/retry/skip/drop/set-state — operator mutations.

Each command resolves the ticket via repo + applies the change. retry
enqueues a WorkItem (needs the QueueManager from ctx); the rest are
repository-only.
"""

from __future__ import annotations

import datetime as dt
import os

import typer

from foreman.v4.repository import TicketNotFoundError
from foreman.v4.states.registry import STATE_REGISTRY
from foreman.v4.work import WorkItem


def _resolve(ctx: typer.Context, ticket_id: int):
    repo = ctx.obj.repo
    try:
        ticket = repo.get_ticket(ticket_id)
    except TicketNotFoundError as exc:
        typer.echo(f"ticket {ticket_id} not found", err=True)
        raise typer.Exit(code=1) from exc
    return repo, ticket


def cmd_hold(
    ctx: typer.Context,
    ticket_id: int = typer.Argument(...),
    reason: str = typer.Option(..., "--reason"),
    by: str | None = typer.Option(None, "--by", help="Operator name (defaults to $USER)"),
) -> None:
    repo, _ = _resolve(ctx, ticket_id)
    repo.hold_ticket(
        ticket_id,
        held_by=by or os.environ.get("USER", "operator"),
        reason=reason,
        now=dt.datetime.now(dt.UTC),
    )
    typer.echo(f"ticket {ticket_id} held")


def cmd_resume(
    ctx: typer.Context,
    ticket_id: int = typer.Argument(...),
) -> None:
    repo, _ = _resolve(ctx, ticket_id)
    repo.resume_ticket(ticket_id, now=dt.datetime.now(dt.UTC))
    typer.echo(f"ticket {ticket_id} resumed")


def cmd_retry(
    ctx: typer.Context,
    ticket_id: int = typer.Argument(...),
) -> None:
    _, ticket = _resolve(ctx, ticket_id)
    qm = ctx.obj.qm
    if qm is None:
        typer.echo("retry requires a queue manager", err=True)
        raise typer.Exit(code=1)
    qm.enqueue(WorkItem(ticket_id=ticket_id, state_name=ticket.current_state))
    typer.echo(f"ticket {ticket_id} re-enqueued in {ticket.current_state}")


def cmd_set_state(
    ctx: typer.Context,
    ticket_id: int = typer.Argument(...),
    state: str = typer.Argument(...),
) -> None:
    repo, ticket = _resolve(ctx, ticket_id)
    if state not in STATE_REGISTRY:
        typer.echo(f"unknown state: {state}", err=True)
        raise typer.Exit(code=1)
    repo.set_ticket_state(ticket_id, state, now=dt.datetime.now(dt.UTC))
    typer.echo(f"ticket {ticket_id} moved {ticket.current_state} -> {state}")


def cmd_drop(
    ctx: typer.Context,
    ticket_id: int = typer.Argument(...),
) -> None:
    repo, _ = _resolve(ctx, ticket_id)
    repo.set_ticket_state(ticket_id, "Failed", now=dt.datetime.now(dt.UTC))
    typer.echo(f"ticket {ticket_id} dropped (-> Failed)")


def cmd_skip(
    ctx: typer.Context,
    ticket_id: int = typer.Argument(...),
    next_state: str = typer.Argument(...),
) -> None:
    repo, _ = _resolve(ctx, ticket_id)
    if next_state not in STATE_REGISTRY:
        typer.echo(f"unknown state: {next_state}", err=True)
        raise typer.Exit(code=1)
    repo.set_ticket_state(ticket_id, next_state, now=dt.datetime.now(dt.UTC))
    typer.echo(f"ticket {ticket_id} skipped to {next_state}")
