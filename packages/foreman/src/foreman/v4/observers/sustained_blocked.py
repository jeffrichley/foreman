"""SustainedBlockedObserver — posts a comment when a ticket has been
BLOCKED for ≥15 minutes on the same async signal.

Subscribes to :class:`ExecuteCompletedEvent`. On a ``BLOCKED`` outcome:

1. Compute a stable per-cause blocked-reason signal from
   ``outcome.summary`` (truncated to 80 chars).
2. Walk the ticket's ``state_instances`` in reverse; collect the
   contiguous suffix whose ``outcome_kind == OutcomeKind.BLOCKED`` AND
   whose blocked-reason-signal matches step 1. Take the
   ``execute_completed_at`` of the EARLIEST row in that suffix as
   ``first_blocked_at``.
3. If ``event.at - first_blocked_at > 15 minutes``, post a comment
   via :func:`post_escalation_comment` with
   ``source="sustained-blocked"`` and a per-(ticket, reason-hash)
   key so the issue stays clean across a 4-hour CI wait.

POST-failure semantics: comment-post failure is delegated to
:func:`post_escalation_comment`, which catches and returns ``False``.
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

from foreman.git_host import GitHostProvider
from foreman.roles._escalation_comment import (
    EscalationComment,
    post_escalation_comment,
)
from foreman.v4.events import Event, ExecuteCompletedEvent
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
    - ``host_for_project``: ``Callable[[str], GitHostProvider | None]``
      returning the per-project GitHostProvider. Returning ``None``
      means the project does not have an orchestrator-token-backed
      host configured; the observer treats this as a no-op (logged).
    - ``threshold``: override for tests (defaults to
      :data:`SUSTAINED_BLOCKED_THRESHOLD`).
    - ``clock``: optional override (defaults to UTC now).
    """

    def __init__(
        self,
        *,
        repo: TicketRepository,
        host_for_project: Callable[[str], GitHostProvider | None],
        threshold: dt.timedelta = SUSTAINED_BLOCKED_THRESHOLD,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self._repo = repo
        self._host_for_project = host_for_project
        self._threshold = threshold
        self._clock = clock

    def __call__(self, event: Event) -> None:
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

        # Threshold crossed. Resolve the host and attempt to post.
        ticket = self._repo.get_ticket(ticket_id)
        host = self._host_for_project(ticket.project)
        if host is None:
            logger.warning(
                "SustainedBlockedObserver: no host configured for "
                "project=%s; skipping comment for ticket=%d",
                ticket.project, ticket_id,
            )
            return

        # Look up the project's repo slug. The TicketRecord carries
        # ``project`` (a name), not the slug; we cannot derive the
        # slug from the observer's surface today (no config dep).
        # ``host.get_issue_comments`` and ``host.post_issue_comment``
        # both require ``repo_slug``. We obtain it via a lookup
        # injected on the host adapter (PyGithub providers carry it
        # implicitly). Use the project name as a best-effort proxy
        # when the host is the v3-shape GitHostProvider whose methods
        # accept the slug directly.
        #
        # Concrete contract: callers wire ``host_for_project`` to
        # return a GitHostProvider whose ``get_issue_comments`` /
        # ``post_issue_comment`` methods accept the slug as the first
        # arg. The repo_slug is the project's configured repo
        # (``owner/name``) — the caller resolves this before passing.
        # The shim wired in bootstrap.py threads the lookup through.
        repo_slug = self._resolve_repo_slug(ticket.project)
        if repo_slug is None:
            logger.warning(
                "SustainedBlockedObserver: cannot resolve repo_slug for "
                "project=%s; skipping comment for ticket=%d",
                ticket.project, ticket_id,
            )
            return

        reason_hash = _reason_hash(signal)
        key = (
            f"ticket-{ticket_id}-state-{event.state_name}-"
            f"reason-{reason_hash}"
        )
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
                "merge-queue verdict, etc.) and apply the "
                "`foreman:retry` label once it has converged, OR "
                "triage the BLOCKED cause if it is genuinely stuck."
            ),
        )
        post_escalation_comment(
            host=host,
            repo_slug=repo_slug,
            issue_number=ticket.issue_number,
            role="daemon",
            outcome_label="BLOCKED",
            summary=f"BLOCKED for ≥{self._threshold}: {signal}",
            payload=payload,
            source="sustained-blocked",
            key=key,
        )

    # Repo-slug lookup is injected via a side channel — the
    # ``host_for_project`` callable can carry its own slug-resolver if
    # constructed by ``bootstrap_cli_context``. To keep the observer
    # surface minimal, we accept an OPTIONAL inner attribute on the
    # callable. Production wires it directly. Tests may override by
    # subclassing or by passing a callable that exposes ``repo_slug``.
    def _resolve_repo_slug(self, project: str) -> str | None:
        """Resolve a project name to its ``owner/name`` slug.

        The default implementation looks for a ``repo_slug_for`` method
        on the ``host_for_project`` callable (set by
        ``bootstrap_cli_context``); when absent, returns the project
        name unchanged (legacy fallback that lets tests pass slugs
        directly as project names).
        """
        resolver = getattr(self._host_for_project, "repo_slug_for", None)
        if callable(resolver):
            slug = resolver(project)
            return slug if isinstance(slug, str) and slug else None
        return project
