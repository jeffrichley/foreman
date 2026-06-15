"""ps — list open tickets (or --all for terminal too).

Default omits Done/Failed so an operator scanning for "what's live" doesn't
have to grep past completed work. ``--all`` is the explicit ask for full
history. Output goes through the Formatter Strategy so the same row shape
serves table/json/yaml callers.
"""

from __future__ import annotations

import typer

from foreman.v4.cli.formatters import get_formatter


def cmd_ps(
    ctx: typer.Context,
    show_all: bool = typer.Option(
        False, "--all", help="Include terminal states (Done/Failed)",
    ),
    format: str = typer.Option(
        "table", "--format", help="table | json | yaml",
    ),
) -> None:
    """List tickets."""
    repo = ctx.obj.repo
    tickets = repo.list_all_tickets() if show_all else repo.list_open_tickets()
    rows = [
        {
            "id": t.id,
            "project": t.project,
            "issue": t.issue_number,
            "state": t.current_state,
            "held": "yes" if t.is_held else "",
            "updated": t.updated_at.isoformat(),
        }
        for t in tickets
    ]
    typer.echo(get_formatter(format).format(rows), nl=False)
