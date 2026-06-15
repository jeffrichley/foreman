"""hold/resume/retry/skip/drop/set-state/enqueue — operator mutations.

Each command resolves the ticket via repo + applies the change. retry
enqueues a WorkItem (needs the QueueManager from ctx); enqueue inserts
a new ticket row at state ``Queued`` (bypassing the Poller's GitHub
label scan); the rest are repository-only.
"""

from __future__ import annotations

import datetime as dt
import os

import typer

from foreman.v4.repository import (
    TicketAlreadyExistsError,
    TicketNotFoundError,
)
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


def cmd_enqueue(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project name from V4Config"),
    issue_number: int = typer.Option(
        ..., "--issue-number", min=1, help="GitHub issue number",
    ),
) -> None:
    """Insert a ticket directly into SQLite at state ``Queued``.

    Bypasses the Poller's GitHub label scan. Useful for dogfood +
    recovery scenarios where round-tripping through ``gh issue edit``
    + waiting for the next Poller tick is friction. The next worker
    poll picks the row up like any other Queued ticket.

    No GitHub API calls are made; this is a pure SQLite mutation.
    """
    repo = ctx.obj.repo
    config = ctx.obj.config

    # Unknown-project check has to happen before the create call —
    # without a V4Config we can't validate, so refuse rather than
    # silently allowing typos.
    if config is None:
        typer.echo(
            "enqueue requires a V4Config (cannot validate --project)",
            err=True,
        )
        raise typer.Exit(code=1)

    known = [p.name for p in config.projects]
    if project not in known:
        typer.echo(
            f"unknown project: {project!r}. "
            f"Configured projects: {known}",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        ticket = repo.create_ticket(
            project=project,
            issue_number=issue_number,
            now=dt.datetime.now(dt.UTC),
        )
    except TicketAlreadyExistsError:
        existing = repo.get_ticket_by_issue(
            project=project, issue_number=issue_number,
        )
        typer.echo(
            f"ticket already exists for {project}#{issue_number}: "
            f"id={existing.id} state={existing.current_state}",
            err=True,
        )
        raise typer.Exit(code=1) from None

    typer.echo(str(ticket.id))
