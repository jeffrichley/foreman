"""Shared helper for posting operator-visible escalation comments.

Single source of truth for the marker-fenced GitHub issue comment that
foreman role bots post when self-escalating (Planner / Reviewer /
Fixer / Worker), when the sustained-BLOCKED observer fires, and when
the terminal-landing observer fires.

Rationale (foreman#367): when the autonomous loop hits a state
operators care about — `NeedsHelp` escalation, `Failed` terminal,
`Blocked` for a long-running async reason, Reviewer rejection — the
role subprocess used to emit a structured outcome and exit. The state
machine acted on the outcome. But nothing landed on the GitHub issue's
comment stream as operator-visible context. This module is the
single-source-of-truth helper that all four role cores plus the two
new observers (`SustainedBlockedObserver`, `TerminalLandingObserver`)
call to post the comment.

Marker shape and dedup
----------------------
Each posted comment is wrapped in matching begin/end markers:

    <!-- foreman:escalation:begin ticket=<repo>#<N>:source=<source>:key=<key> -->
    ... rendered body ...
    <!-- foreman:escalation:end -->

Idempotency keys ride inside the begin marker as
``ticket=<id>:source=<source>:key=<key>``. The dedup check is a plain
substring scan over the issue's existing comments
(:func:`already_posted_for_key`). Precedent: the Reviewer's
``FINDINGS_BEGIN_MARKER`` / ``FINDINGS_END_MARKER`` handshake in
:mod:`foreman.roles.reviewer`.

POST-failure semantics
----------------------
:func:`post_escalation_comment` catches every
``host.post_issue_comment`` failure (GitHub 5xx, rate limit, network
drop) and returns ``False`` rather than re-raising. The role-core call
sites and both observers treat ``False`` as a non-fatal comment-post
failure and proceed with their normal success-path telemetry write.
This is the regression guard against foreman#235: a transient GitHub
5xx on the comment post MUST NOT skip the JSONL telemetry write or
kill the role subprocess.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field

from foreman.git_host import CommentRef, GitHostProvider

logger = logging.getLogger(__name__)


# Marker constants. The begin marker carries the dedup key as a
# substring; the existence check is a literal substring scan.
ESCALATION_MARKER_BEGIN = "<!-- foreman:escalation:begin -->"
ESCALATION_MARKER_END = "<!-- foreman:escalation:end -->"


class EscalationComment(BaseModel):
    """LLM-populated structured payload that drives the escalation comment.

    Nested under each role's structured-output schema (PlannerOutput /
    ReviewerOutput / FixerOutput / WorkerOutput) as an optional field.
    Required-iff the role's confidence/outcome gate fires; each schema's
    ``model_validator`` enforces the requirement so a slip surfaces at
    schema-validation time rather than as a missing GitHub comment.
    """

    why: str = Field(
        ...,
        description=(
            "Why I escalated / why my confidence is low. Multi-sentence "
            "reasoning. Be specific; cite file:line if relevant."
        ),
    )
    what_tried: str = Field(
        ...,
        description=(
            "What I attempted before escalating. Brief bullets in prose "
            "or a short paragraph."
        ),
    )
    what_would_unblock: str = Field(
        ...,
        description=(
            "What an operator (or another role) would need to do for the "
            "loop to make progress."
        ),
    )
    extra_context: str | None = Field(
        default=None,
        description=(
            "Optional additional context. When present, rendered as its "
            "own block beneath the three required sections."
        ),
    )


# Forbidden tokens — GitHub's nine auto-close keywords. A standalone
# token of any of these followed by a ``#N`` reference auto-closes the
# referenced issue when the body is merged. Our comments are NOT merged
# — they're posted directly — but a defense-in-depth assertion guards
# against the template ever including them.
_FORBIDDEN_CLOSING_KEYWORDS = (
    "close", "closes", "closed",
    "fix", "fixes", "fixed",
    "resolve", "resolves", "resolved",
)


def _begin_marker(*, ticket_ref: str, source: str, key: str) -> str:
    """Render the begin marker carrying the dedup key as substring."""
    return (
        f"<!-- foreman:escalation:begin "
        f"ticket={ticket_ref}:source={source}:key={key} -->"
    )


def _matches_source_and_key(comment_body: str, *, source: str, key: str) -> bool:
    """Substring scan: does ``comment_body`` carry our begin marker for ``(source, key)``?"""
    needle = f"source={source}:key={key}"
    if needle not in comment_body:
        return False
    # Guard: the substring must be inside a begin marker, not stray text.
    return "<!-- foreman:escalation:begin" in comment_body


def already_posted_for_key(
    comments: list[CommentRef], *, source: str, key: str,
) -> bool:
    """Return True iff at least one comment carries our begin marker for ``(source, key)``.

    Pure scan — performs no I/O. ``comments`` is the issue's existing
    comment list (e.g., fetched via ``host.get_issue_comments``).
    """
    for c in comments:
        if _matches_source_and_key(c.body, source=source, key=key):
            return True
    return False


def any_recent_marker_with_source_prefix(
    comments: list[CommentRef], *, source_prefix: str, since: dt.datetime,
) -> bool:
    """Return True iff any comment carries a begin marker whose ``source=``
    starts with ``source_prefix`` AND whose ``posted_at >= since``.

    Used by :class:`TerminalLandingObserver` for the 5-minute
    "recent role-comment suppress" check. The substring scan is a
    literal lookup for ``source=<source_prefix>`` inside the begin
    marker; the timestamp check uses :class:`CommentRef.posted_at`
    directly (no parsing of the marker timestamp).

    Independent of :func:`already_posted_for_key` (different shape:
    prefix scan + timestamp filter vs. exact substring scan) but lives
    in the same module so the single-source-of-truth invariant holds.
    """
    needle = f"source={source_prefix}"
    for c in comments:
        if "<!-- foreman:escalation:begin" not in c.body:
            continue
        if needle not in c.body:
            continue
        if c.posted_at >= since:
            return True
    return False


def build_escalation_comment_body(
    *,
    role: str,
    outcome_label: str,
    summary: str,
    at: dt.datetime,
    payload: EscalationComment | None,
    fallback_reason: str | None = None,
    ticket_ref: str,
    source: str,
    key: str,
) -> str:
    """Pure function: return the Markdown body wrapped in begin/end markers.

    When ``payload is None`` OR ``fallback_reason is not None``, the
    body explicitly names that the role-side prompt did NOT populate
    the structured field and falls back to ``summary`` as the only
    available signal.

    The body skeleton matches the issue's prose template:

        **[role] · [outcome_label] · [iso timestamp]**

        > <summary>

        ## Why
        <payload.why OR fallback prose>

        ## What I tried
        <payload.what_tried OR fallback bullet>

        ## What would unblock this
        <payload.what_would_unblock OR fallback line>

        <payload.extra_context block — omitted when None>

        ---
        *Auto-posted by foreman-<role>-bot. ...*

    Forbidden: any standalone ``Closes #N`` / ``Fixes #N`` /
    ``Resolves #N`` keyword forms. The template is controlled, so no
    runtime sanitizer is needed; a unit test asserts the rendered
    template contains none of the nine closing keywords as standalone
    tokens.
    """
    iso = at.isoformat()

    if payload is None:
        # Fallback shape. Name that the role-side prompt did NOT
        # populate the structured field; summary is the only available
        # signal.
        why = (
            "(role-side prompt did not populate; fallback) — "
            f"{fallback_reason or 'no payload available'}"
        )
        what_tried = (
            "(role-side prompt did not populate; fallback) — see daemon "
            "log for full role output if available."
        )
        what_would_unblock = (
            "(role-side prompt did not populate; fallback) — operator "
            "should review the role's structured outcome and apply the "
            "`foreman:retry` label once unblocked."
        )
        extra_context_block = ""
    else:
        why = payload.why
        what_tried = payload.what_tried
        what_would_unblock = payload.what_would_unblock
        # If the helper-caller asked for an explicit fallback reason
        # while still passing a payload, prepend it to the why block
        # so the audit log captures the reason for the override.
        if fallback_reason:
            why = (
                f"(escalation fallback) — {fallback_reason}\n\n{why}"
            )
        extra_context_block = (
            f"\n{payload.extra_context}\n" if payload.extra_context else ""
        )

    begin = _begin_marker(ticket_ref=ticket_ref, source=source, key=key)
    return (
        f"{begin}\n"
        f"**[{role}] · [{outcome_label}] · [{iso}]**\n\n"
        f"> {summary}\n\n"
        f"## Why\n{why}\n\n"
        f"## What I tried\n{what_tried}\n\n"
        f"## What would unblock this\n{what_would_unblock}\n"
        f"{extra_context_block}\n"
        "---\n"
        f"*Auto-posted by foreman-{role}-bot. Do not edit; apply the "
        "`foreman:retry` label on the issue to re-dispatch.*\n"
        f"{ESCALATION_MARKER_END}"
    )


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def post_escalation_comment(
    *,
    host: GitHostProvider,
    repo_slug: str,
    issue_number: int,
    role: str,
    outcome_label: str,
    summary: str,
    payload: EscalationComment | None,
    source: str,
    key: str,
    fallback_reason: str | None = None,
    clock: Callable[[], dt.datetime] = _utcnow,
) -> bool:
    """Fetch comments, short-circuit on dedup, render body, post.

    Returns True iff the helper actually posted a fresh comment.
    Returns False on dedup hit (already posted) OR on post failure
    (caught + logged via ``logger.exception``).

    POST-failure semantics (load-bearing for the "non-fatal comment
    post" claim in role-core wiring): ``host.post_issue_comment``
    failures (any ``Exception`` subclass — GitHub 5xx, rate limit,
    network drop) are caught inside the helper and logged via
    ``logger.exception``; the helper returns ``False`` rather than
    re-raising. Callers treat ``False`` as non-fatal and proceed.

    A failure on ``host.get_issue_comments`` (fetch fails) logs and
    PROCEEDS to post a fresh comment — duplicate is preferable to
    missing under the "issue is the answer" invariant.
    """
    # Build the ticket reference used inside the begin marker.
    ticket_ref = f"{repo_slug}#{issue_number}"

    # Fetch existing comments; on fetch failure, log and proceed.
    try:
        comments = host.get_issue_comments(repo_slug, issue_number)
    except Exception:
        logger.exception(
            "post_escalation_comment: get_issue_comments failed for "
            "%s#%d (source=%s, key=%s); proceeding to post (duplicate "
            "preferable to missing)",
            repo_slug, issue_number, source, key,
        )
        comments = []

    if already_posted_for_key(comments, source=source, key=key):
        logger.debug(
            "post_escalation_comment: dedup hit for source=%s key=%s; "
            "skipping post",
            source, key,
        )
        return False

    body = build_escalation_comment_body(
        role=role,
        outcome_label=outcome_label,
        summary=summary,
        at=clock(),
        payload=payload,
        fallback_reason=fallback_reason,
        ticket_ref=ticket_ref,
        source=source,
        key=key,
    )
    try:
        host.post_issue_comment(repo_slug, issue_number, body)
    except Exception:
        logger.exception(
            "post_escalation_comment: post_issue_comment failed for "
            "%s#%d (source=%s, key=%s); returning False",
            repo_slug, issue_number, source, key,
        )
        return False
    return True


# Subprocess-failure-signal extraction. Used by
# :class:`TerminalLandingObserver` to render exit code + log tail
# inline in the comment body when the failure path is a subprocess
# crash / TIMEOUT / retry-cap trip.

_EXIT_CODE_RE = re.compile(r"exit(?:ed| code:) (\d+)")


def extract_subprocess_failure_signals(
    *, failure_reason: str | None, log_path: Path | None,
) -> tuple[int | None, str | None]:
    """Return ``(exit_code, log_tail)`` for the inline subprocess block.

    ``exit_code`` is extracted via regex over ``failure_reason``:
    matches both ``"... exited <N> ..."`` (RoleSubprocessError prose)
    AND ``"--- exit code: <N> ---"`` (the on-disk footer written by
    :mod:`foreman.v4.subprocess_dispatcher`). Returns ``None`` when
    neither pattern matches (TIMEOUT path, generic retry-cap
    failure_reason).

    ``log_tail`` is the last 500 bytes of ``log_path`` decoded with
    ``errors="replace"``. When the file is missing, unreadable, or
    empty, returns ``None`` and emits a ``logger.warning``. Read
    failures MUST NOT raise.
    """
    exit_code: int | None = None
    if failure_reason:
        m = _EXIT_CODE_RE.search(failure_reason)
        if m is not None:
            try:
                exit_code = int(m.group(1))
            except ValueError:
                exit_code = None

    log_tail: str | None = None
    if log_path is not None:
        try:
            raw = log_path.read_bytes()
            if raw:
                log_tail = raw[-500:].decode("utf-8", errors="replace")
        except OSError:
            logger.warning(
                "extract_subprocess_failure_signals: failed to read "
                "log file %s; log_tail will be omitted",
                log_path,
            )
            log_tail = None

    return exit_code, log_tail


__all__ = [
    "ESCALATION_MARKER_BEGIN",
    "ESCALATION_MARKER_END",
    "EscalationComment",
    "already_posted_for_key",
    "any_recent_marker_with_source_prefix",
    "build_escalation_comment_body",
    "extract_subprocess_failure_signals",
    "post_escalation_comment",
]
