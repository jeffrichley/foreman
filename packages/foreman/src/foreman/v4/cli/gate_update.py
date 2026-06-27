"""gate-update — watchtower pre-update lifecycle hook.

Watchtower reads the ``com.centurylinklabs.watchtower.lifecycle.pre-update``
container label and executes its value as a shell command inside the daemon
container before stopping it for a redeploy.

Exit-code semantics (verified 2026-06-23 against official Watchtower docs):
  0   — board is idle; allow the update.
  75  — EX_TEMPFAIL; board is busy; defer the update to the next poll cycle.

Any other non-zero exit (including an uncaught crash → exit 1) also lets
Watchtower proceed, so the system is fail-open by design: a bug in this
hook must never permanently block deploys.

See foreman#412 and docs/RUNBOOK.md § "Watchtower idle-gate".
"""

from __future__ import annotations

import sys

import typer


def cmd_gate_update(ctx: typer.Context) -> None:
    """Exit 75 (defer) when the board is busy; exit 0 (allow) when idle.

    Fail-open on any repository error so a guard bug never wedges the
    deploy pipeline.
    """
    try:
        open_tickets = ctx.obj.repo.list_open_tickets()
    except Exception as exc:
        typer.echo(
            f"WARNING: gate-update check failed ({exc!r}); allowing update (fail-open)",
            err=True,
        )
        sys.exit(0)
    if open_tickets:
        raise typer.Exit(code=75)
    # Idle — fall through; typer exits 0.
