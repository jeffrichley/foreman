"""show — render state-instance journal for one ticket as a rich.Tree.

Unlike ps/queue, the natural shape here is a tree (ticket → instances →
optional failure children), not a flat row list, so this command doesn't go
through the Formatter Strategy. 404 → exit 1 so scripted callers can
distinguish "ticket missing" from "render succeeded with no journal."
"""

from __future__ import annotations

import io

import typer
from rich.console import Console
from rich.tree import Tree

from foreman.v4.repository import TicketNotFoundError


def cmd_show(
    ctx: typer.Context,
    ticket_id: int = typer.Argument(..., help="Ticket id"),
) -> None:
    """Render state history for one ticket."""
    repo = ctx.obj.repo
    try:
        ticket = repo.get_ticket(ticket_id)
    except TicketNotFoundError as exc:
        typer.echo(f"ticket {ticket_id} not found", err=True)
        raise typer.Exit(code=1) from exc

    instances = repo.list_state_instances_for_ticket(ticket_id)
    tree = Tree(
        f"[bold]Ticket {ticket.id}[/bold] "
        f"({ticket.project}#{ticket.issue_number}) "
        f"— {ticket.current_state}"
    )
    for inst in instances:
        outcome = inst.outcome_kind.value if inst.outcome_kind else "in-flight"
        next_state = inst.next_state or "—"
        node = tree.add(
            f"[cyan]{inst.state_name}[/cyan] #{inst.sequence} "
            f"→ {outcome} → {next_state}"
        )
        if inst.failure_reason:
            node.add(
                f"[red]failed @ {inst.failure_phase}: {inst.failure_reason}[/red]"
            )

    buffer = io.StringIO()
    # force_terminal=True so rich renders the tree shape even when stdout is
    # captured by typer's CliRunner (which is not a tty).
    console = Console(file=buffer, force_terminal=True, width=120)
    console.print(tree)
    typer.echo(buffer.getvalue(), nl=False)
