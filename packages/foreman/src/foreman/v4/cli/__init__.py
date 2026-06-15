"""Foreman v4 CLI — typer app.

Command groups land in sibling files (ps.py, show.py, etc.); each
registers itself with this top-level ``app``. The console script
entry point is foreman.v4.cli:main (set in Task 6.6), which imports +
invokes this app.
"""

from __future__ import annotations

import typer

from foreman.v4.cli.daemon import (
    cmd_daemon_reload,
    cmd_daemon_start,
    cmd_daemon_status,
    cmd_daemon_stop,
)
from foreman.v4.cli.log import cmd_log
from foreman.v4.cli.mutations import (
    cmd_drop,
    cmd_hold,
    cmd_resume,
    cmd_retry,
    cmd_set_state,
    cmd_skip,
)
from foreman.v4.cli.ps import cmd_ps
from foreman.v4.cli.queue import cmd_queue
from foreman.v4.cli.show import cmd_show

__version__ = "0.4.0"

app = typer.Typer(
    name="foreman",
    help="Foreman v4 — autonomous-loop coordinator",
    no_args_is_help=True,
)


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", help="Print version and exit",
    ),
) -> None:
    if version:
        typer.echo(f"foreman {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


# Query + log + mutation commands wired. Daemon lifecycle commands land in
# the daemon_app sub-typer below.
app.command("ps")(cmd_ps)
app.command("show")(cmd_show)
app.command("queue")(cmd_queue)
app.command("log")(cmd_log)
app.command("hold")(cmd_hold)
app.command("resume")(cmd_resume)
app.command("retry")(cmd_retry)
app.command("skip")(cmd_skip)
app.command("drop")(cmd_drop)
app.command("set-state")(cmd_set_state)


daemon_app = typer.Typer(name="daemon", help="Daemon lifecycle")
app.add_typer(daemon_app)

daemon_app.command("start")(cmd_daemon_start)
daemon_app.command("stop")(cmd_daemon_stop)
daemon_app.command("reload")(cmd_daemon_reload)
daemon_app.command("status")(cmd_daemon_status)


def main() -> None:
    """Console-script entry point. Phase 6 Task 6.6 swaps pyproject.toml
    to point at this."""
    app()
