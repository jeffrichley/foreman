"""Per-role dispatch modules. Each role: build context, run agent, parse output, act.

This package also exposes the shared defensive-exception helper used by all
four role runners — see :func:`handle_unhandled_role_exception`.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from typing import Any

_log = logging.getLogger(__name__)

# foreman#229: runaway-burn defense — when an unhandled exception escapes a
# role runner's body, the in-flight ticket label was NOT being transitioned.
# The dispatcher's next poll then re-dispatched the SAME role on the SAME
# ticket, producing a runaway (foreman#227: 171 dispatches in 2h52m). This
# helper is the uniform "post traceback + transition to terminal blocking
# label" surface every role runner calls from its outermost ``except``.
TERMINAL_BLOCKING_LABEL = "foreman:needs-help"

# Cap the traceback shown in the GitHub comment so a runaway provider that
# fills its own stack with megabytes of context doesn't post a multi-MB
# comment (GitHub's hard limit is 65k chars; we want to stay well under and
# leave room for the prose preamble). 4000 chars OR 50 lines, whichever is
# smaller — same shape the spec specified.
_TRACEBACK_CHAR_LIMIT = 4000
_TRACEBACK_LINE_LIMIT = 50

# Truncation marker copy. Names the daemon's per-dispatch subprocess log
# (foreman#119: ``<log_dir>/<role>/<issue>__<iso-timestamp>.log``) so an
# operator reading the truncated comment knows exactly where to find the
# full traceback. The path itself is configurable via FOREMAN_LOG_DIR
# (compose sets ``/foreman/logs``; host default is ``~/.foreman/logs``),
# so the marker names the convention rather than a hard-coded path.
_TRUNCATION_MARKER = (
    "... (truncated; full traceback in the foreman daemon's per-dispatch "
    "subprocess log — `<log_dir>/<role>/<issue>__<timestamp>.log`, where "
    "`<log_dir>` is `$FOREMAN_LOG_DIR` or `~/.foreman/logs` by default)"
)


def _format_traceback(exc: BaseException) -> str:
    """Return a truncated traceback string for inclusion in the comment body.

    Truncated to at most :data:`_TRACEBACK_CHAR_LIMIT` characters OR
    :data:`_TRACEBACK_LINE_LIMIT` lines, whichever comes first. The
    truncation is signalled with a ``... (truncated)`` marker so a human
    reading the comment knows the tail was elided.
    """
    raw = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    # Line-based truncation first (preserves frame boundaries).
    lines = raw.splitlines()
    if len(lines) > _TRACEBACK_LINE_LIMIT:
        lines = lines[:_TRACEBACK_LINE_LIMIT] + [_TRUNCATION_MARKER]
    raw = "\n".join(lines)
    # Char-based truncation second (catches the long-single-frame case).
    if len(raw) > _TRACEBACK_CHAR_LIMIT:
        raw = raw[:_TRACEBACK_CHAR_LIMIT] + f"\n{_TRUNCATION_MARKER}"
    return raw


# Cap the inline ``**Exception:** type: msg`` line. The full message is
# in the traceback block below; the headline only needs to be readable on
# the issue's preview pane (a single GitHub list row).
_INLINE_MSG_LIMIT = 200


def _truncate_inline(msg: str, limit: int = _INLINE_MSG_LIMIT) -> str:
    if len(msg) <= limit:
        return msg
    # Short form for the inline ``**Exception:**`` line — keeps the
    # headline scannable. The full marker (with log path) lives on the
    # truncated traceback fence below.
    return msg[:limit] + "... (truncated)"


def build_exception_comment(role: str, exc: BaseException) -> str:
    """Compose the markdown comment body posted on the ticket.

    The body names the role that crashed, the exception type + (truncated)
    message, and the truncated traceback inside a fenced code block.
    Operators reading the issue see exactly what blew up without spelunking
    daemon logs. The inline message is capped at ~200 chars so a runaway
    provider that built a 10MB exception string doesn't post a 10MB GitHub
    comment; the full content (also truncated) is available in the
    traceback fence below.
    """
    tb = _format_traceback(exc)
    inline_msg = _truncate_inline(str(exc))
    return (
        f"foreman {role} runner crashed with an unhandled exception. "
        f"The ticket has been transitioned to `{TERMINAL_BLOCKING_LABEL}` to "
        "stop the daemon from re-dispatching on every poll (foreman#227, "
        "foreman#229). Remove the label to resume the autonomous flow once "
        "the underlying cause is addressed.\n\n"
        f"**Exception:** `{type(exc).__name__}: {inline_msg}`\n\n"
        "<details>\n"
        "<summary>Traceback</summary>\n\n"
        f"```\n{tb}\n```\n\n"
        "</details>"
    )


def handle_unhandled_role_exception(
    *,
    role: str,
    issue_number: int,
    exc: BaseException,
    post_comment: Callable[[str], Any],
    set_needs_help_label: Callable[[], Any],
) -> None:
    """Surface an unhandled role-runner exception to the ticket.

    Posts a comment carrying the exception type + message + truncated
    traceback, then transitions the originating ticket to
    :data:`TERMINAL_BLOCKING_LABEL` so the daemon stops re-dispatching
    on every poll (the runaway-burn pattern foreman#227 surfaced and
    foreman#229 fixes).

    Both side effects are wrapped in their own try/except so a GitHub
    5xx during the comment-post does NOT mask the failure to transition
    the label, and vice-versa. The original exception still propagates
    via the caller's bare ``raise`` — this helper never swallows.

    Args:
        role: ``"planner"`` / ``"reviewer"`` / ``"worker"`` / ``"fixer"``,
            used only in the human-facing comment prose.
        issue_number: For diagnostic logs when the helper's own writes
            fail. The label-transition callable already targets the
            right issue.
        exc: The exception that escaped the role's body. The caller is
            expected to ``raise`` after this helper returns so the
            dispatcher's error handling still sees it.
        post_comment: Closure that posts ``body`` as a GitHub comment on
            the originating ticket. Each role wires its own (the four
            roles use different PyGithub / GitHostProvider surfaces).
        set_needs_help_label: Closure that atomically transitions the
            originating ticket to :data:`TERMINAL_BLOCKING_LABEL`. Same
            rationale as ``post_comment``.
    """
    body = build_exception_comment(role=role, exc=exc)
    try:
        post_comment(body)
    except Exception:
        _log.exception(
            "foreman#229 %s exception-handler: post_comment failed for "
            "issue=%d; label transition will still be attempted",
            role,
            issue_number,
        )
    try:
        set_needs_help_label()
    except Exception:
        _log.exception(
            "foreman#229 %s exception-handler: set_needs_help_label "
            "failed for issue=%d; ticket will remain on the in-flight "
            "label and the daemon may re-dispatch",
            role,
            issue_number,
        )
