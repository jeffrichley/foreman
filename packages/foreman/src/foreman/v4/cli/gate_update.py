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

**Drain lease protocol (foreman#599):**

The gate acquires a PostgreSQL-backed drain lease BEFORE re-checking for
in-flight work.  Acquiring the lease first is critical — if the check
preceded the acquisition, the dispatcher could open a new state-instance
in the gap between the check and the lease insert, reproducing the race.

Flow:
  1. ``repo.acquire_drain_lease(now=now, ttl_seconds=300)``
  2. ``in_flight = repo.list_in_flight_state_instances()``
  3. If any in-flight → release lease (best-effort) → exit 75 (defer).
  4. If idle → leave lease set → exit 0 (allow).

The ``open_state_instance`` repository method refuses to insert while the
lease is active, so no new state-instance can be started in the window
between the exit-0 and the SIGTERM that follows.

The lease TTL (300 s) acts as the fail-open safety valve: if the new
container fails to start and clear the lease, dispatch resumes
automatically within 5 minutes.  The ``_reconcile_startup`` call in the
new daemon's startup path releases the lease immediately after closing any
crash-orphaned rows.

See foreman#412 (original idle-gate), foreman#599 (drain lease), and
docs/RUNBOOK.md § "Watchtower idle-gate".
"""

from __future__ import annotations

import datetime as dt
import sys

import typer

_TTL_SECONDS = 300


def cmd_gate_update(ctx: typer.Context) -> None:
    """Exit 75 (defer) while a role job is in flight; exit 0 (allow) otherwise.

    "Busy" means a role subprocess is actually RUNNING — the only thing a
    redeploy (which kills the daemon and its --die-with-parent children) would
    destroy.  It must NOT mean "any non-terminal ticket exists": a ``NeedsHelp``
    ticket is parked awaiting a human, and a ``Queued`` ticket only awaits
    dispatch — neither is live work.  Gate on in-flight state-instances instead.

    The drain lease is acquired BEFORE re-checking in-flight work so any
    dispatch call to ``open_state_instance`` that arrives after the lease is
    set is blocked — even if the QueueManager already dequeued the item.

    Fail-open on any repository error (including lease acquisition failure)
    so a guard bug never wedges the deploy pipeline.
    """
    repo = ctx.obj.repo
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    try:
        repo.acquire_drain_lease(now=now, ttl_seconds=_TTL_SECONDS)
        in_flight = repo.list_in_flight_state_instances()
    except Exception as exc:
        typer.echo(
            f"WARNING: gate-update check failed ({exc!r}); allowing update (fail-open)",
            err=True,
        )
        sys.exit(0)
    if in_flight:
        # Board busy — release the lease so dispatch is not blocked until TTL.
        try:
            repo.release_drain_lease()
        except Exception:
            pass  # best-effort; a failed release must not turn defer into fail-open
        raise typer.Exit(code=75)
    # Board idle — leave the lease set so no new state-instance can open
    # in the window before SIGTERM arrives.  Fall through; typer exits 0.
