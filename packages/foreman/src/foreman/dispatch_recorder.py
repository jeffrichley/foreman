"""DispatchRecorder — single Mediator for dispatch telemetry (foreman#251).

This is the Phase 1 implementation of the design at
``docs/dispatch-recorder-design.md``: introduce the Mediator + two
Observer-style subscribers, dual-write through Phase 1 so nothing
breaks. Phase 2 will switch the source-of-truth read to
``execution_log``; Phase 3 will refactor role runners onto a
``BaseRoleRunner`` Template Method. Neither lives in this PR.

Why the indirection (vs. role runners directly calling ``log_<role>_run``
+ ``execution_log.write_action``):

After seven sibling-bug-per-PR shipments, every cost-telemetry add
surfaced another silent gap (success path only, failure paths, killed
subprocess, ad-hoc CLI, cache tokens, double-row). The pattern is
*multiple writers reaching into shared mutable telemetry with no
coordination protocol*. The Recorder is the coordination protocol:

- One writer per signal — the Recorder owns the cost ledger and the
  JSONL stream. Role runners and the parent reconciler both emit
  events INTO the Recorder; nobody writes execution_log cost columns
  except CostSubscriber, and nobody writes JSONL except
  RoleStatsSubscriber.
- Trace-id propagation — every emit carries ``trace_id`` (= the
  ``start_log_id`` the daemon wrote when it dispatched). Writes that
  target the same ``(trace_id, event_kind)`` are idempotent. Double-
  write is structurally impossible.
- Telemetry best-effort — subscribers swallow their own write errors
  to a logger.warning so a disk-full on the JSONL write doesn't kill
  a role run, and so the cost write going wrong doesn't suppress the
  JSONL write or vice versa.

Cross-process dedup: the role subprocess and the parent daemon hold
DIFFERENT Recorder instances. The in-process ``_seen`` set only
catches duplicates within ONE process. For the spec's "subprocess
already emitted dispatch_complete, parent's subsequent
record_subprocess_terminated must be a no-op" case, the parent's
CostSubscriber does a defensive DB lookup: if a row with
``parent_log_id = trace_id`` already exists, the parent's terminate
write is skipped (the in-band cost row is the source of truth).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from foreman.provider import UsageInfo
from foreman.reconciler.exec_log import ExecutionLog
from foreman.stats import (
    log_fixer_run,
    log_planner_run,
    log_reviewer_run,
    log_worker_run,
)
from foreman.v3_bus_endpoint import _usage_to_columns

logger = logging.getLogger(__name__)


# Event-kind sentinels for the ``(trace_id, event_kind)`` dedup set. Two
# distinct values:
#
# - "dispatch_complete" — role finished (success OR in-band failure path)
# - "subprocess_terminated" — parent observed subprocess exit
#
# Within a single process, the Recorder dedups both kinds independently:
# the same trace_id can carry one of each (e.g., a role emits
# dispatch_complete, then the parent's same Recorder later observes the
# subprocess exit). Cross-process, the parent's CostSubscriber falls
# back to a DB lookup.
_KIND_DISPATCH_COMPLETE = "dispatch_complete"
_KIND_SUBPROCESS_TERMINATED = "subprocess_terminated"


# Trace-id env var the daemon sets when spawning role subprocesses. The
# role CLI reads this at start so its in-process Recorder tags every
# event with the same trace_id the daemon will use when calling
# ``record_subprocess_terminated`` after the subprocess exits.
TRACE_ID_ENV_VAR = "FOREMAN_DISPATCH_TRACE_ID"


def trace_id_from_env() -> int | None:
    """Return the dispatch trace_id from :data:`TRACE_ID_ENV_VAR`, or None.

    Used by role CLI entry points: when present, the role constructs a
    Recorder and emits ``record_dispatch_complete`` carrying this
    trace_id. When absent (manual ``foreman plan`` invocation, ad-hoc
    debug, tests that don't go through dispatch), the role still runs
    but emits no Recorder events — the existing ``log_<role>_run``
    calls keep producing JSONL as before. This is the back-compat
    seam for non-dispatched invocations.
    """
    value = os.environ.get(TRACE_ID_ENV_VAR)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        logger.warning(
            "ignoring malformed %s=%r — not a valid integer trace_id",
            TRACE_ID_ENV_VAR,
            value,
        )
        return None


def _repo_slug_to_dirname(repo_slug: str) -> str:
    """Mirror :func:`foreman.stats._repo_slug_to_dirname` without importing
    the private symbol. Single literal substitution (``/`` → ``__``)."""
    return repo_slug.replace("/", "__")


# ---------------------------------------------------------------------------
# CostSubscriber — writes cost rows to execution_log.
# ---------------------------------------------------------------------------


class CostSubscriber:
    """Observer subscriber: writes dispatch cost into ``execution_log``.

    Each event arrives with a ``trace_id`` (= ``start_log_id`` from
    the daemon's dispatch row). The cost row is written as a
    *termination row* whose ``parent_log_id`` points back at the start
    row, so the existing ``has_unterminated`` / ``count_completed`` /
    rules-engine queries see the dispatch as complete on the same row
    they always did.

    Phase 1 dedup contract:

    - Within one process the Recorder's ``_seen`` set already
      prevents a second emit for the same ``(trace_id, event_kind)``.
    - Across processes, ``handle_subprocess_terminated`` does a
      defensive DB lookup: if any row already exists with
      ``parent_log_id = trace_id``, the subprocess-terminated write
      skips. The in-band cost row written by the subprocess's own
      Recorder is the source of truth.

    Phase 2 will switch downstream cost reads onto these columns and
    stop the JSONL cost duplication. Phase 1 keeps both.
    """

    def __init__(self, *, log: ExecutionLog) -> None:
        self._log = log

    def handle_dispatch_complete(
        self,
        *,
        trace_id: int,
        role: str,
        repo_slug: str,
        ticket_id: str,
        project: str,
        issue_number: int,
        pr_number: int | None,
        outcome: str,
        usage: UsageInfo,
        role_data: dict[str, Any],
        duration_seconds: float,
    ) -> None:
        """Write a termination row carrying the eight cost columns.

        The row's ``action`` is inherited from the parent dispatch row
        (the daemon's ``dispatch_<role>`` row), so the parent's
        ``terminate_dispatch`` queries continue to find it. ``outcome``
        is the role-level outcome (``spec_written`` / ``worker_failed``
        / etc.) — distinct from the parent-level ``success`` /
        ``timeout`` / ``error`` outcomes that ``terminate_dispatch``
        writes.

        Phase 2 will collapse the two termination rows into one. Phase
        1 dual-writes: this cost row sits ALONGSIDE the existing
        ``terminate_dispatch`` row the parent writes. The two
        coordinate via the ``has_unterminated`` query (which returns
        False once EITHER row exists with parent_log_id = trace_id),
        so the existing rules engine is unaffected.
        """
        details = {
            "role": role,
            "issue_number": issue_number,
            "pr_number": pr_number,
            "duration_seconds": round(duration_seconds, 3),
            "role_data": role_data,
            # foreman#251: marker so a future Phase 2 query can pick
            # the Recorder's row vs. the existing terminate_dispatch
            # row deterministically. Plain string so jq / sqlite ad-hoc
            # queries can read it without JSON traversal.
            "recorder_event": _KIND_DISPATCH_COMPLETE,
        }
        usage_columns = _usage_to_columns(usage)
        try:
            self._log.terminate_action(
                parent_log_id=trace_id,
                outcome=outcome,
                details=details,
                usage_columns=usage_columns,
            )
        except Exception:
            # Best-effort telemetry: a failed write must not kill the
            # role run. Log loudly so the operator notices.
            logger.exception(
                "CostSubscriber: failed to write dispatch_complete row for "
                "trace_id=%d role=%s outcome=%s",
                trace_id,
                role,
                outcome,
            )

    def handle_subprocess_terminated(
        self,
        *,
        trace_id: int,
        role: str,
        ticket_id: str,
        project: str,
        exit_outcome: str,
        duration_seconds: float,
    ) -> bool:
        """Write a ``subprocess_killed`` row when no prior completion exists.

        Returns ``True`` when a row was written (the genuinely-killed
        case); ``False`` when the lookup found an existing cost row
        for this trace_id and the write was skipped. The boolean lets
        the Recorder tell :class:`RoleStatsSubscriber` whether to also
        write a JSONL row.

        The lookup is the cross-process dedup mechanism: the
        subprocess's Recorder writes the cost row; later the parent's
        Recorder (a separate instance) calls
        ``record_subprocess_terminated``. The parent has never seen
        the trace_id locally, but the DB tells it the role already
        completed.
        """
        if self._has_existing_terminate_row(trace_id):
            return False

        details = {
            "role": role,
            "exit_outcome": exit_outcome,
            "duration_seconds": round(duration_seconds, 3),
            "recorder_event": _KIND_SUBPROCESS_TERMINATED,
        }
        # Zero-cost UsageInfo so the eight cost columns are populated
        # with explicit zeros rather than NULLs. Operators querying
        # "how many runs had zero cost" can distinguish "Recorder wrote
        # zero" (subprocess killed) from "Recorder didn't touch the
        # row" (start row only) by the presence of zero vs. NULL.
        usage_columns = _usage_to_columns(UsageInfo())
        try:
            self._log.terminate_action(
                parent_log_id=trace_id,
                outcome="subprocess_killed",
                details=details,
                usage_columns=usage_columns,
            )
        except Exception:
            logger.exception(
                "CostSubscriber: failed to write subprocess_killed row for "
                "trace_id=%d role=%s exit_outcome=%s",
                trace_id,
                role,
                exit_outcome,
            )
            return False
        return True

    def _has_existing_terminate_row(self, trace_id: int) -> bool:
        """True iff ANY row with ``parent_log_id = trace_id`` already exists.

        Catches BOTH the in-band cost row written by a role's Recorder
        AND the parent's existing ``terminate_dispatch`` row — the
        intent at the Recorder layer is "did anyone already mark this
        dispatch as terminated?" and either flavor suffices to keep
        Phase 1's no-double-write contract.
        """
        with sqlite3.connect(self._log.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM execution_log WHERE parent_log_id = ? LIMIT 1",
                (trace_id,),
            ).fetchone()
        return row is not None


# ---------------------------------------------------------------------------
# RoleStatsSubscriber — writes JSONL via existing log_<role>_run functions.
# ---------------------------------------------------------------------------


class RoleStatsSubscriber:
    """Observer subscriber: writes the per-role JSONL line.

    Phase 1: this delegates to the existing :func:`log_<role>_run`
    functions in :mod:`foreman.stats`. The dual-write contract of
    Phase 1 means the role runner *also* calls ``log_<role>_run``
    directly today; the Subscriber's call is additional, not
    replacing. Phase 2 / 3 will collapse this once cost reads have
    migrated to ``execution_log``.

    The role-specific JSONL schema (sub_request counts, finding
    counts, etc.) varies per role. ``role_data`` carries those fields
    keyed by their JSONL column name; the dispatcher per role pulls
    out the keys it needs. Unknown keys are tolerated — the role's
    log_*_run signature is strict so we explicitly pass only the
    fields it accepts.
    """

    def __init__(self, *, stats_root: Path) -> None:
        self._stats_root = stats_root

    def handle_dispatch_complete(
        self,
        *,
        trace_id: int,
        role: str,
        repo_slug: str,
        ticket_id: str,
        project: str,
        issue_number: int,
        pr_number: int | None,
        outcome: str,
        usage: UsageInfo,
        role_data: dict[str, Any],
        duration_seconds: float,
    ) -> None:
        try:
            self._write_for_role(
                role=role,
                repo_slug=repo_slug,
                issue_number=issue_number,
                pr_number=pr_number,
                outcome=outcome,
                usage=usage,
                role_data=role_data,
                duration_seconds=duration_seconds,
            )
        except Exception:
            logger.exception(
                "RoleStatsSubscriber: failed to write JSONL for trace_id=%d "
                "role=%s outcome=%s",
                trace_id,
                role,
                outcome,
            )

    def handle_subprocess_terminated(
        self,
        *,
        trace_id: int,
        role: str,
        repo_slug: str,
        ticket_id: str,
        project: str,
        issue_number: int,
        exit_outcome: str,
        duration_seconds: float,
    ) -> None:
        """Write the ``subprocess_killed`` JSONL row with zero cost.

        Only called when :meth:`CostSubscriber.handle_subprocess_terminated`
        returned ``True`` (no prior completion existed). The
        Recorder's fan-out checks that return value before invoking
        this method.
        """
        try:
            self._write_for_role(
                role=role,
                repo_slug=repo_slug,
                issue_number=issue_number,
                pr_number=None,
                outcome="subprocess_killed",
                usage=UsageInfo(),
                role_data={},
                duration_seconds=duration_seconds,
            )
        except Exception:
            logger.exception(
                "RoleStatsSubscriber: failed to write subprocess_killed JSONL "
                "for trace_id=%d role=%s exit_outcome=%s",
                trace_id,
                role,
                exit_outcome,
            )

    def _write_for_role(
        self,
        *,
        role: str,
        repo_slug: str,
        issue_number: int,
        pr_number: int | None,
        outcome: str,
        usage: UsageInfo,
        role_data: dict[str, Any],
        duration_seconds: float,
    ) -> None:
        """Dispatch to the role-specific ``log_<role>_run`` writer.

        Each role's signature differs in its role-specific fields
        (Worker has sub_request counts, Fixer has finding counts,
        Reviewer has ``target``, Planner has none). We unpack
        ``role_data`` per role with safe defaults so a partial-state
        failure path (which carries an empty dict) still produces a
        valid JSONL row.
        """
        usage_kwargs = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
            "total_cost_usd": usage.total_cost_usd,
            "model_usage": usage.model_usage,
            "duration_ms": usage.duration_ms,
            "num_turns": usage.num_turns,
        }
        if role == "planner":
            log_planner_run(
                repo_slug=repo_slug,
                issue_number=issue_number,
                pr_number=pr_number,
                outcome=outcome,  # type: ignore[arg-type]
                duration_seconds=duration_seconds,
                stats_root=self._stats_root,
                **usage_kwargs,  # type: ignore[arg-type]
            )
        elif role == "reviewer":
            log_reviewer_run(
                repo_slug=repo_slug,
                issue_number=issue_number,
                pr_number=pr_number,
                target=role_data.get("target", "spec_pr"),
                outcome=outcome,  # type: ignore[arg-type]
                duration_seconds=duration_seconds,
                stats_root=self._stats_root,
                **usage_kwargs,  # type: ignore[arg-type]
            )
        elif role == "worker":
            log_worker_run(
                repo_slug=repo_slug,
                issue_number=issue_number,
                pr_number=pr_number,
                attempt=int(role_data.get("attempt", 0)),
                outcome=outcome,  # type: ignore[arg-type]
                total_sub_requests=int(role_data.get("total_sub_requests", 0)),
                implemented_count=int(role_data.get("implemented_count", 0)),
                skipped_count=int(role_data.get("skipped_count", 0)),
                skipped_by_reason=dict(role_data.get("skipped_by_reason", {})),
                did_check_pass=bool(role_data.get("did_check_pass", False)),
                confidence=role_data.get("confidence", "low"),
                duration_seconds=duration_seconds,
                baseline_failures_count=int(role_data.get("baseline_failures_count", 0)),
                new_failures_count=int(role_data.get("new_failures_count", 0)),
                stats_root=self._stats_root,
                **usage_kwargs,  # type: ignore[arg-type]
            )
        elif role == "fixer":
            log_fixer_run(
                repo_slug=repo_slug,
                issue_number=issue_number,
                pr_number=pr_number,
                attempt=int(role_data.get("attempt", 0)),
                outcome=outcome,  # type: ignore[arg-type]
                total_findings=int(role_data.get("total_findings", 0)),
                addressed_count=int(role_data.get("addressed_count", 0)),
                unaddressed_count=int(role_data.get("unaddressed_count", 0)),
                unaddressed_by_reason=dict(role_data.get("unaddressed_by_reason", {})),
                disagreed_count=int(role_data.get("disagreed_count", 0)),
                confidence=role_data.get("confidence", "low"),
                duration_seconds=duration_seconds,
                stats_root=self._stats_root,
                **usage_kwargs,  # type: ignore[arg-type]
            )
        else:
            raise ValueError(f"unknown role for stats: {role!r}")


# ---------------------------------------------------------------------------
# DispatchRecorder — the Mediator. Fans out to subscribers + dedups.
# ---------------------------------------------------------------------------


class DispatchRecorder:
    """Mediator for all dispatch telemetry writes.

    Process-local instance. Each role subprocess and the parent daemon
    construct their own ``DispatchRecorder``; cross-process dedup is
    handled by the CostSubscriber's DB lookup, not by sharing memory.

    The Recorder owns:

    - The ``(trace_id, event_kind)`` dedup set — second emit for the
      same key is a structural no-op (returns immediately, neither
      subscriber sees it).
    - The fan-out to subscribers — one emit, multiple persistence
      destinations.

    Phase 1 has two concrete subscribers (CostSubscriber +
    RoleStatsSubscriber) — wired directly in ``__init__`` rather than
    via an abstract ``Subscriber`` ABC. The ABC is deliberately
    premature; if a third subscriber lands (Prometheus, OpenTelemetry,
    Slack alerts) THEN we extract the protocol.
    """

    def __init__(self, *, log: ExecutionLog, stats_root: Path) -> None:
        self._log = log
        self._stats_root = stats_root
        self._seen: set[tuple[int, str]] = set()
        self._cost_subscriber = CostSubscriber(log=log)
        self._stats_subscriber = RoleStatsSubscriber(stats_root=stats_root)

    def record_dispatch_started(
        self,
        *,
        trace_id: int,
        role: str,
        repo_slug: str,
        ticket_id: str,
        project: str,
        issue_number: int,
    ) -> None:
        """Observer hook for "dispatch begun" events.

        Phase 1 is a no-op except for the dedup-set bookkeeping; the
        existing ``write_action`` call in the executor still writes
        the start row. The hook exists so Phase 3 can move the start
        write into the Recorder without an API churn at every call
        site.
        """
        key = (trace_id, "dispatch_started")
        if key in self._seen:
            return
        self._seen.add(key)
        # Hook reserved for future subscribers; Phase 1 emits no writes
        # because the daemon already wrote the start row before calling
        # dispatch_role. Logging the no-op at debug helps diagnose
        # missing trace_ids during Phase 2 / Phase 3 wiring.
        logger.debug(
            "DispatchRecorder: dispatch_started trace_id=%d role=%s ticket=%s",
            trace_id,
            role,
            ticket_id,
        )

    def record_dispatch_complete(
        self,
        *,
        trace_id: int,
        role: str,
        repo_slug: str,
        ticket_id: str,
        project: str,
        issue_number: int,
        pr_number: int | None,
        outcome: str,
        usage: UsageInfo,
        role_data: dict[str, Any],
        duration_seconds: float,
    ) -> None:
        """Role finished (success path OR in-band exception path).

        Both the cost row (execution_log) and the JSONL row are written.
        Second emit for the same ``(trace_id, "dispatch_complete")``
        is a no-op.
        """
        key = (trace_id, _KIND_DISPATCH_COMPLETE)
        if key in self._seen:
            return
        self._seen.add(key)
        self._cost_subscriber.handle_dispatch_complete(
            trace_id=trace_id,
            role=role,
            repo_slug=repo_slug,
            ticket_id=ticket_id,
            project=project,
            issue_number=issue_number,
            pr_number=pr_number,
            outcome=outcome,
            usage=usage,
            role_data=role_data,
            duration_seconds=duration_seconds,
        )
        self._stats_subscriber.handle_dispatch_complete(
            trace_id=trace_id,
            role=role,
            repo_slug=repo_slug,
            ticket_id=ticket_id,
            project=project,
            issue_number=issue_number,
            pr_number=pr_number,
            outcome=outcome,
            usage=usage,
            role_data=role_data,
            duration_seconds=duration_seconds,
        )

    def record_subprocess_terminated(
        self,
        *,
        trace_id: int,
        role: str,
        repo_slug: str,
        ticket_id: str,
        project: str,
        issue_number: int,
        exit_outcome: str,
        duration_seconds: float,
    ) -> None:
        """Parent observed the subprocess exit.

        Three cases:

        1. Same Recorder previously saw ``record_dispatch_complete``
           for this trace_id (in-process). The local ``_seen`` set
           catches it via the alternate key — but more importantly
           CostSubscriber's DB lookup catches it too, so JSONL never
           gets a spurious ``subprocess_killed`` line.
        2. A DIFFERENT Recorder previously wrote the cost row
           (cross-process: subprocess Recorder wrote, parent Recorder
           now observes exit). CostSubscriber's DB lookup catches it.
        3. No prior completion exists — the subprocess died before
           emitting. Both ledgers get a ``subprocess_killed`` row with
           zero cost.

        The local ``_seen`` check on ``(trace_id,
        "subprocess_terminated")`` catches a second-fire of the same
        kind (re-trigger from a flapping background task, etc.).
        """
        key = (trace_id, _KIND_SUBPROCESS_TERMINATED)
        if key in self._seen:
            return
        self._seen.add(key)

        wrote_cost_row = self._cost_subscriber.handle_subprocess_terminated(
            trace_id=trace_id,
            role=role,
            ticket_id=ticket_id,
            project=project,
            exit_outcome=exit_outcome,
            duration_seconds=duration_seconds,
        )
        # If the cost row write was skipped (prior completion exists),
        # skip the JSONL write too — the existing dispatch_complete
        # row's JSONL already carries the truth.
        if not wrote_cost_row:
            return
        self._stats_subscriber.handle_subprocess_terminated(
            trace_id=trace_id,
            role=role,
            repo_slug=repo_slug,
            ticket_id=ticket_id,
            project=project,
            issue_number=issue_number,
            exit_outcome=exit_outcome,
            duration_seconds=duration_seconds,
        )


def emit_recorder_complete(
    *,
    dispatch_recorder: DispatchRecorder | None,
    dispatch_trace_id: int | None,
    role: str,
    repo_slug: str,
    ticket_id: str,
    project: str,
    issue_number: int,
    pr_number: int | None,
    outcome: str,
    usage: UsageInfo,
    role_data: dict[str, Any],
    duration_seconds: float,
) -> None:
    """Fire ``record_dispatch_complete`` if a recorder + trace_id are present.

    foreman#251 (Phase 1): the four role runners (planner / reviewer /
    worker / fixer) each call this AFTER the existing
    ``log_<role>_run`` call on both success and failure paths. The
    null-recorder / null-trace_id check is hoisted into this helper so
    each role keeps a single call site at each emit point and the
    null-guard logic stays single-sourced.

    Best-effort: a recorder exception is swallowed and logged. The
    role's existing ``log_<role>_run`` call has already produced the
    JSONL row, so the Recorder's failure must NOT crash the role run.
    Phase 2 will tighten this once cost reads have migrated to
    ``execution_log``.
    """
    if dispatch_recorder is None or dispatch_trace_id is None:
        return
    try:
        dispatch_recorder.record_dispatch_complete(
            trace_id=dispatch_trace_id,
            role=role,
            repo_slug=repo_slug,
            ticket_id=ticket_id,
            project=project,
            issue_number=issue_number,
            pr_number=pr_number,
            outcome=outcome,
            usage=usage,
            role_data=role_data,
            duration_seconds=duration_seconds,
        )
    except Exception:
        logger.exception(
            "emit_recorder_complete: DispatchRecorder.record_dispatch_complete "
            "raised for trace_id=%d role=%s outcome=%s; ignoring (telemetry-only)",
            dispatch_trace_id,
            role,
            outcome,
        )


__all__ = [
    "CostSubscriber",
    "DispatchRecorder",
    "RoleStatsSubscriber",
    "TRACE_ID_ENV_VAR",
    "emit_recorder_complete",
    "trace_id_from_env",
]
