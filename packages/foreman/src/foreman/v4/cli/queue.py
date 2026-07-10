"""queue — report QueueManager depth + in-flight.

Reads QM's two public counters and shapes them as a single-row table for
the Formatter Strategy. Exits 1 when no QM is configured (running outside
the daemon context, e.g. against a plain repo file) — that's the operator
asking a question that has no answer rather than an answer of "0/0".
"""

from __future__ import annotations

import typer

from foreman.v4.cli.formatters import get_formatter


def cmd_queue(
    ctx: typer.Context,
    format: str = typer.Option(
        "table",
        "--format",
        help="table | json | yaml",
    ),
) -> None:
    """Report queue depth and in-flight count."""
    qm = ctx.obj.qm
    if qm is None:
        typer.echo("queue manager not configured", err=True)
        raise typer.Exit(code=1)
    rows = [
        {
            "in_flight": qm.in_flight_count(),
            "queued": qm.queue_depth(),
        }
    ]
    typer.echo(get_formatter(format).format(rows), nl=False)
