"""hold/resume/retry/skip/drop/set-state/enqueue — operator mutations.

Each command resolves the ticket via repo + applies the change. retry
enqueues a WorkItem (needs the QueueManager from ctx); enqueue inserts
a new ticket row at state ``Queued`` (bypassing the Poller's GitHub
label scan); the rest are repository-only.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass

import typer

from foreman.v4.git_provider import GitProvider
from foreman.v4.repository import (
    TicketAlreadyExistsError,
    TicketNotFoundError,
)
from foreman.v4.states.registry import STATE_REGISTRY
from foreman.v4.work import WorkItem


@dataclass(frozen=True, slots=True)
class ResetPlan:
    """What ``foreman reset`` will do, decided in the discovery phase.

    Built read-only from current GitHub + SQLite + filesystem state by
    :func:`_discover`. Walked destructively by :func:`_execute`. Steps
    that are off — no PR matched, no row in SQLite, ``--keep-pr`` set —
    are encoded as ``None`` / empty / False so the renderer + executor
    can skip them uniformly.
    """
    project: str
    issue_number: int
    spec_pr: int | None
    impl_pr: int | None
    delete_branches: list[str]
    prune_worktrees: bool
    strip_labels: set[str]
    delete_ticket_id: int | None
    apply_plan_label: bool


def _discover(
    *,
    git: GitProvider,
    repo,
    project: str,
    issue_number: int,
    keep_pr: bool,
    keep_worktree: bool,
    retrigger: bool,
) -> ResetPlan:
    """Read-only scan of current state. No mutations."""
    if keep_pr:
        spec_pr = None
        impl_pr = None
    else:
        spec_pr = git.find_open_pr_by_head_branch(
            project=project, branch_name=f"foreman/issue-{issue_number}",
        )
        impl_pr = git.find_open_pr_by_head_branch(
            project=project, branch_name=f"foreman/impl-{issue_number}",
        )
    # Branches: always include both candidates. delete_branch is idempotent
    # on missing, so listing them unconditionally is fine.
    delete_branches = [
        f"foreman/issue-{issue_number}",
        f"foreman/impl-{issue_number}",
    ]
    labels_on_issue = git.get_issue_labels(
        project=project, issue_number=issue_number,
    )
    strip = {lbl for lbl in labels_on_issue if lbl.startswith("foreman:")}
    try:
        ticket = repo.get_ticket_by_issue(
            project=project, issue_number=issue_number,
        )
        delete_ticket_id = ticket.id
    except TicketNotFoundError:
        delete_ticket_id = None
    return ResetPlan(
        project=project,
        issue_number=issue_number,
        spec_pr=spec_pr,
        impl_pr=impl_pr,
        delete_branches=delete_branches,
        prune_worktrees=not keep_worktree,
        strip_labels=strip,
        delete_ticket_id=delete_ticket_id,
        apply_plan_label=retrigger,
    )


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
