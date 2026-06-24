"""TerminalLandingObserver — posts a comment when a ticket lands on
``NeedsHelp`` / ``Failed``.

Subscribes to :class:`StateEnteredEvent`. On entry to a state whose
name is in ``{"NeedsHelp", "Failed"}``:

1. Fetch the issue's existing comments.
2. Build dedup key ``ticket-<ticket_id>-instance-<instance_id>``.
3. Apply belt-and-suspenders dedup layering:
   a. Same-key dedup (belt): if a same-key terminal-landing comment
      already exists, short-circuit.
   b. Recent-in-role suppress (suspenders): if any ``source=role:``
      marker was posted within the last 5 minutes, short-circuit.
      This catches the happy-path case where the in-role escalation
      comment has just posted.
4. Look up the prior ``state_instances`` row (``[-2]`` from the
   landing row) and render the failure context. On the subprocess
   crash / TIMEOUT / retry-cap-trip path, the comment additionally
   carries an inline exit-code + log-tail block in
   ``extra_context``.

POST-failure semantics: same as
:class:`SustainedBlockedObserver` — comment-post failure delegated
to :func:`post_escalation_comment`'s catch + return-``False``
contract, plus a top-level ``try/except`` swallow in ``__call__``.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Callable
from pathlib import Path

from foreman.roles._escalation_comment import (
    EscalationComment,
    already_posted_for_key,
    any_recent_marker_with_source_prefix,
    build_escalation_comment_body,
    extract_subprocess_failure_signals,
)
from foreman.v4.events import Event, StateEnteredEvent
from foreman.v4.git_provider import GitProvider
from foreman.v4.repository import TicketRepository
from foreman.v4.subprocess_dispatcher import _base_role

logger = logging.getLogger(__name__)


# 5-minute heuristic for the "recent in-role comment" suppress. Wide
# enough to absorb daemon-tick skew between the role-core post and the
# ``StateEnteredEvent("NeedsHelp")`` emission; narrow enough that a
# stale ``role:`` marker from a prior dispatch on the same ticket does
# NOT suppress a fresh terminal landing.
TERMINAL_LANDING_ROLE_COMMENT_WINDOW: dt.timedelta = dt.timedelta(minutes=5)

_TERMINAL_STATES: frozenset[str] = frozenset({"NeedsHelp", "Failed"})

# Map from a state name (the role-dispatch state's name) to the
# subprocess-dispatcher role key. Used to compute the on-disk role log
# directory. The two columns pin to:
#   - keys: ``state_name`` strings declared in
#     ``foreman.v4.states.*``
#   - values: keys of ``foreman.v4.subprocess_dispatcher._ROLE_TO_INVOCATION``
# A drift in either source is caught at test-collection time by a
# regression test (see test_terminal_landing_observer.py).
_STATE_NAME_TO_ROLE: dict[str, str] = {
    "Planning":     "planner",
    "SpecReview":   "reviewer-spec",
    "SpecFix":      "fixer-spec",
    "Implementing": "worker",
    "ImplReview":   "reviewer-impl",
    "ImplFix":      "fixer-impl",
}

# Used by retry-cap walkback: the EARLIEST same-state crash row whose
# failure_reason carries an ``exited <N>`` substring is the actual
# crash row (the row whose ``failure_reason`` is ``"state X failed N
# consecutive times"`` is the cap-trip row, NOT the crash row).
_EXITED_RE = re.compile(r"exited \d+")


class TerminalLandingObserver:
    """Posts an operator-visible comment on terminal landing."""

    def __init__(
        self,
        *,
        repo: TicketRepository,
        git: GitProvider,
        log_dir: Path,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
        role_comment_window: dt.timedelta = TERMINAL_LANDING_ROLE_COMMENT_WINDOW,
    ) -> None:
        self._repo = repo
        self._git = git
        self._log_dir = log_dir
        self._clock = clock
        self._role_comment_window = role_comment_window

    def __call__(self, event: Event) -> None:
        try:
            self._handle(event)
        except Exception:
            logger.exception(
                "TerminalLandingObserver: handler raised; swallowing so "
                "EventBus dispatch is not interrupted"
            )

    def _handle(self, event: Event) -> None:
        if not isinstance(event, StateEnteredEvent):
            return
        if event.state_name not in _TERMINAL_STATES:
            return

        ticket_id = event.ticket_id
        ticket = self._repo.get_ticket(ticket_id)

        # Fetch existing comments for both dedup checks.
        try:
            comments = self._git.get_issue_comments(
                project=ticket.project, issue_number=ticket.issue_number,
            )
        except Exception:
            logger.exception(
                "TerminalLandingObserver: get_issue_comments failed for "
                "%s#%d; proceeding without dedup (duplicate preferable "
                "to missing)",
                ticket.project, ticket.issue_number,
            )
            comments = []

        instance_id = event.instance_id
        key = f"ticket-{ticket_id}-instance-{instance_id}"

        # Belt: same-key dedup.
        if already_posted_for_key(
            comments, source="terminal-landing", key=key
        ):
            return
        # Suspenders: recent-in-role suppress.
        since = self._clock() - self._role_comment_window
        if any_recent_marker_with_source_prefix(
            comments, source_prefix="role:", since=since
        ):
            return

        # Walk back from [-2] to find the prior (cause) row. The
        # landing row is [-1] (just-fired); [-2] is the row that
        # produced the failure.
        history = self._repo.list_state_instances_for_ticket(ticket_id)
        prior = history[-2] if len(history) >= 2 else None

        failure_phase = prior.failure_phase if prior else None
        failure_reason = prior.failure_reason if prior else None
        prior_state_name = prior.state_name if prior else event.state_name
        summary_from_payload = ""
        if prior and prior.outcome_payload:
            raw = prior.outcome_payload.get("summary")
            if isinstance(raw, str):
                summary_from_payload = raw

        # Retry-cap-trip walkback: when failure_reason is the generic
        # "state X failed N consecutive times" message, walk backward
        # through prior same-state rows to find the EARLIEST row whose
        # failure_phase == "execute" AND failure_reason matches the
        # ``exited \d+`` regex. Use that crash row's failure_reason
        # for the exit-code extraction.
        if (
            prior is not None
            and failure_reason
            and "failed" in failure_reason
            and "consecutive times" in failure_reason
        ):
            for older in reversed(history[:-1]):
                if older.state_name != prior_state_name:
                    continue
                if older.failure_phase != "execute":
                    continue
                if older.failure_reason and _EXITED_RE.search(older.failure_reason):
                    failure_reason = older.failure_reason
                    break

        # Determine the role and log path. The role is derived from the
        # prior state's name; the log file is the most-recently-modified
        # file under <log_dir>/<role-base>/ for this ticket.
        role_name = _STATE_NAME_TO_ROLE.get(prior_state_name)
        log_path: Path | None = None
        log_dir_for_role: Path | None = None
        if role_name is not None:
            log_dir_for_role = self._log_dir / _base_role(role_name)
            try:
                candidates = list(
                    log_dir_for_role.glob(f"{ticket_id}__*.log")
                )
            except OSError:
                candidates = []
            if candidates:
                candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                log_path = candidates[0]
            else:
                logger.warning(
                    "TerminalLandingObserver: no log file found for "
                    "ticket %d under %s",
                    ticket_id, log_dir_for_role,
                )

        # Render the extra_context block when the prior row carries a
        # subprocess-crash failure signature. The block is intentionally
        # rendered ONLY on the subprocess-crash / TIMEOUT / retry-cap
        # paths — the in-role self-report path is suppressed by the
        # 5-minute heuristic earlier in this method.
        exit_code, log_tail = extract_subprocess_failure_signals(
            failure_reason=failure_reason, log_path=log_path,
        )
        extra_context: str | None = None
        if exit_code is not None or log_tail is not None or log_path is not None:
            exit_code_str = (
                str(exit_code) if exit_code is not None
                else "(unknown — TIMEOUT or generic cap-trip; see log)"
            )
            log_path_str = (
                str(log_path) if log_path is not None
                else f"(log file not found under {log_dir_for_role})"
                if log_dir_for_role is not None
                else "(log path could not be resolved)"
            )
            log_tail_block = (
                log_tail if log_tail is not None
                else "(log file missing or unreadable)"
            )
            extra_context = (
                "## Subprocess signals\n"
                f"- exit code: {exit_code_str}\n"
                f"- log path: {log_path_str}\n\n"
                "<details>\n"
                "<summary>last 500 chars of role log</summary>\n\n"
                "```\n"
                f"{log_tail_block}\n"
                "```\n\n"
                "</details>"
            )

        # Build the structured payload from the prior row's failure
        # signature.
        summary = (
            summary_from_payload
            or failure_reason
            or f"state landed on {event.state_name}"
        )
        why = (
            f"State `{prior_state_name}` failed; the state machine "
            f"transitioned to `{event.state_name}`."
        )
        if failure_phase:
            why += f"\n\nFailure phase: `{failure_phase}`."
        if failure_reason:
            why += f"\n\nFailure reason: `{failure_reason[:500]}`."
        what_tried = (
            "Per the state machine's failure handling, the dispatched "
            "role subprocess was retried up to the per-state cap before "
            "landing here."
        )
        what_would_unblock = (
            "Review the linked role log for the underlying failure "
            "signature. When ready, apply the `foreman:retry` label "
            "on the issue to re-dispatch the ticket from the prior "
            "state."
        )
        payload = EscalationComment(
            why=why,
            what_tried=what_tried,
            what_would_unblock=what_would_unblock,
            extra_context=extra_context,
        )

        # Render and post. Inline render-then-post (rather than
        # calling post_escalation_comment) is used here because we
        # already fetched comments for the two dedup checks above —
        # avoiding the redundant fetch keeps the observer cheap on
        # high-frequency landings.
        body = build_escalation_comment_body(
            role=role_name or "daemon",
            outcome_label=event.state_name,
            summary=summary,
            at=self._clock(),
            payload=payload,
            ticket_ref=f"{ticket.project}#{ticket.issue_number}",
            source="terminal-landing",
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
                "TerminalLandingObserver: post_issue_comment failed for "
                "%s#%d; swallowing (the helper-helper contract treats a "
                "post failure as non-fatal)",
                ticket.project, ticket.issue_number,
            )
