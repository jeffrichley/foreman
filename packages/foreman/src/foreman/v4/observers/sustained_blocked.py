"""SustainedBlockedObserver — posts a comment when a ticket has been BLOCKED for ≥15 minutes on the same async signal.

Subscribes to :class:`ExecuteCompletedEvent`. On a ``BLOCKED`` outcome:

1. Compute a stable per-cause blocked-reason signal from
   ``outcome.summary`` (truncated to 80 chars).
2. Walk the ticket's ``state_instances`` in reverse; collect the
   contiguous suffix whose ``outcome_kind == OutcomeKind.BLOCKED`` AND
   whose blocked-reason-signal matches step 1. Take the
   ``execute_completed_at`` of the EARLIEST row in that suffix as
   ``first_blocked_at``.
3. If ``event.at - first_blocked_at > 15 minutes``, post a comment
   with ``source="sustained-blocked"`` and a per-(ticket, reason-hash)
   key so the issue stays clean across a 4-hour CI wait.

POST-failure semantics: comment-post failure is wrapped in a try/except.
The observer additionally wraps the entire ``__call__`` body in a
top-level ``try/except`` that logs and swallows — EventBus dispatch
must never raise out of this observer.

The 15-minute threshold is a module-level constant; the observer's
constructor accepts an override so tests can pass
``timedelta(seconds=0.001)`` without monkey-patching.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from collections.abc import Callable

from foreman.roles._escalation_comment import (
    EscalationComment,
    already_posted_for_key,
    build_escalation_comment_body,
)
from foreman.v4.events import Event, ExecuteCompletedEvent
from foreman.v4.git_provider import GitProvider
from foreman.v4.outcome import OutcomeKind
from foreman.v4.repository import TicketRepository

logger = logging.getLogger(__name__)


# Issue #367: 15 minutes is the threshold the issue body specifies.
# A ticket BLOCKED for less is "the daemon is polling normally"; ≥15
# minutes is "the operator probably wants to know."
SUSTAINED_BLOCKED_THRESHOLD: dt.timedelta = dt.timedelta(minutes=15)

# Truncate the summary used as the blocked-reason signal. Short enough
# to stay stable across poll ticks (BLOCKED emitters embed short
# canonical strings); long enough to disambiguate different causes.
_REASON_SIGNAL_MAX_CHARS = 80


def _reason_signal(summary: str) -> str:
    return summary[:_REASON_SIGNAL_MAX_CHARS]


def _reason_hash(signal: str) -> str:
    """Stable 8-char hash of the reason signal for dedup key construction."""
    return hashlib.sha1(signal.encode("utf-8")).hexdigest()[:8]


class SustainedBlockedObserver:
    """Posts one operator-visible comment per (ticket, blocked-reason).

    Constructed with:
    - ``repo``: :class:`TicketRepository` for ``list_state_instances_for_ticket``
      and ``get_ticket``.
    - ``git``: v4 :class:`GitProvider` — refresh-aware, routes by project
      name. Used for ``get_issue_comments`` (dedup) and ``post_issue_comment``.
    - ``threshold``: override for tests (defaults to
      :data:`SUSTAINED_BLOCKED_THRESHOLD`).
    - ``clock``: optional override (defaults to UTC now).
    """

    def __init__(
        self,
        *,
        repo: TicketRepository,
        git: GitProvider,
        threshold: dt.timedelta = SUSTAINED_BLOCKED_THRESHOLD,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self._repo = repo
        self._git = git
        self._threshold = threshold
        self._clock = clock

    def __call__(self, event: Event) -> None:
        """Handle the event, swallowing any exception so EventBus dispatch is never interrupted.

        Delegates to :meth:`_handle`; a raised exception there is logged
        and discarded rather than propagated.
        """
        try:
            self._handle(event)
        except Exception:
            logger.exception(
                "SustainedBlockedObserver: handler raised; swallowing so "
                "EventBus dispatch is not interrupted"
            )

    def _handle(self, event: Event) -> None:
        if not isinstance(event, ExecuteCompletedEvent):
            return
        if event.outcome.kind != OutcomeKind.BLOCKED:
            return

        ticket_id = event.ticket_id
        signal = _reason_signal(event.outcome.summary or "")
        if not signal:
            # Nothing to dedup on; cannot derive a stable key.
            return

        # Walk the ticket's state_instances in reverse to find the
        # contiguous BLOCKED suffix that matches our signal.
        history = self._repo.list_state_instances_for_ticket(ticket_id)
        if not history:
            return

        first_blocked_at: dt.datetime | None = None
        for inst in reversed(history):
            if inst.outcome_kind != OutcomeKind.BLOCKED:
                break
            inst_signal = ""
            if inst.outcome_payload:
                raw_summary = inst.outcome_payload.get("summary")
                if isinstance(raw_summary, str):
                    inst_signal = _reason_signal(raw_summary)
            if inst_signal != signal:
                break
            if inst.execute_completed_at is not None:
                first_blocked_at = inst.execute_completed_at

        if first_blocked_at is None:
            return
        if event.at - first_blocked_at <= self._threshold:
            return

        # Threshold crossed. Resolve the ticket and dedup.
        ticket = self._repo.get_ticket(ticket_id)

        reason_hash = _reason_hash(signal)
        key = (
            f"ticket-{ticket_id}-state-{event.state_name}-"
            f"reason-{reason_hash}"
        )

        # Fetch existing comments for dedup check.
        try:
            comments = self._git.get_issue_comments(
                project=ticket.project, issue_number=ticket.issue_number,
            )
        except Exception:
            logger.exception(
                "SustainedBlockedObserver: get_issue_comments failed for "
                "%s#%d; proceeding without dedup (duplicate preferable "
                "to missing)",
                ticket.project, ticket.issue_number,
            )
            comments = []

        if already_posted_for_key(comments, source="sustained-blocked", key=key):
            return

        payload = EscalationComment(
            why=(
                f"State {event.state_name} has been BLOCKED for ≥"
                f"{self._threshold} on the same async signal: "
                f"{signal!r}"
            ),
            what_tried=(
                "Polling the BLOCKED state on each daemon tick; the "
                "BLOCKED-exempt retry-cap is by-design and not "
                "consuming max_state_attempts."
            ),
            what_would_unblock=(
                "Check the external async signal (CI status, "
                "merge-queue verdict, etc.) and run "
                "`foreman retry <ticket_id>` once it has converged "
                "(find the id via `foreman ps`), OR "
                "triage the BLOCKED cause if it is genuinely stuck."
            ),
        )
        body = build_escalation_comment_body(
            role="daemon",
            outcome_label="BLOCKED",
            summary=f"BLOCKED for ≥{self._threshold}: {signal}",
            at=self._clock(),
            payload=payload,
            ticket_ref=f"{ticket.project}#{ticket.issue_number}",
            source="sustained-blocked",
            key=key,
        )
        try:
            self._git.post_issue_comment(
                project=ticket.project,
                issue_number=ticket.issue_number,
                body=body,
            )
        except Exception:
            logger.exception(
                "SustainedBlockedObserver: post_issue_comment failed for "
                "%s#%d; swallowing (non-fatal)",
                ticket.project, ticket.issue_number,
            )
