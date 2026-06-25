"""Startup reconciliation for crash-orphaned in-flight state instances.

When the daemon dies mid-transition the Template Method's ``finally`` never
runs, leaving a ``state_instances`` row open (``exited_at IS NULL``) that no
process is executing. This pass — run ONCE at daemon startup, before the
WorkerPool starts a single thread (single-instance daemon, PID-locked) — finds
those orphans and closes each as ``crash_recovery``. That phase is exempt from
the runaway-cap counter, so a restart never escalates a healthy ticket.

Re-derives from the journal; carries no flags. The Poller re-enqueues the
ticket at its unchanged ``current_state`` on the first tick, as it already does.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

from foreman.v4.records import FAILURE_PHASE_CRASH_RECOVERY
from foreman.v4.repository import TicketRepository

logger = logging.getLogger(__name__)


def reconcile_on_startup(
    repo: TicketRepository, *, clock: Callable[[], dt.datetime]
) -> int:
    """Close every orphaned in-flight row as crash_recovery. Returns the count.

    Idempotent: a second run finds no in-flight rows (the first closed them),
    so re-invoking is a no-op.
    """
    orphans = repo.list_in_flight_state_instances()
    now = clock()
    for inst in orphans:
        repo.record_failure(
            inst.id, now=now,
            failure_phase=FAILURE_PHASE_CRASH_RECOVERY,
            failure_reason=(
                f"daemon restart: state {inst.state_name!r} was in-flight "
                f"(instance {inst.id}) when the previous process exited"
            ),
        )
        repo.close_state_instance(inst.id, now=now)
    if orphans:
        logger.warning(
            "crash recovery: closed %d orphaned in-flight state instance(s)",
            len(orphans),
        )
    return len(orphans)
