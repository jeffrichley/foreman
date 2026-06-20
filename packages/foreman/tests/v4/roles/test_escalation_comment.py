"""Unit tests for :mod:`foreman.roles._escalation_comment`."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from foreman.git_host import CommentRef
from foreman.roles._escalation_comment import (
    ESCALATION_MARKER_BEGIN,
    ESCALATION_MARKER_END,
    EscalationComment,
    already_posted_for_key,
    any_recent_marker_with_source_prefix,
    build_escalation_comment_body,
    extract_subprocess_failure_signals,
    post_escalation_comment,
)


def _comment(body: str, *, posted_at: dt.datetime | None = None) -> CommentRef:
    return CommentRef(
        author_login="foreman-worker-bot",
        posted_at=posted_at or dt.datetime(2026, 6, 20, tzinfo=dt.UTC),
        body=body,
    )


def _payload() -> EscalationComment:
    return EscalationComment(
        why="The spec is ambiguous on bullet 3.",
        what_tried="Re-read the spec twice; grepped the codebase.",
        what_would_unblock="Operator clarifies whether X or Y was intended.",
    )


# ---------------------------------------------------------------------------
# build_escalation_comment_body
# ---------------------------------------------------------------------------


def test_build_body_with_payload_renders_all_three_sections() -> None:
    body = build_escalation_comment_body(
        role="planner",
        outcome_label="NEEDS_HELP",
        summary="planner can't decide between X and Y",
        at=dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.UTC),
        payload=_payload(),
        ticket_ref="jeffrichley/foreman#367",
        source="role:planner",
        key="state-instance-42",
    )
    assert "## Why" in body
    assert "The spec is ambiguous" in body
    assert "## What I tried" in body
    assert "Re-read the spec twice" in body
    assert "## What would unblock this" in body
    assert "Operator clarifies" in body
    # Begin marker carries the dedup key as substring.
    assert "source=role:planner:key=state-instance-42" in body
    # End marker present.
    assert ESCALATION_MARKER_END in body
    # Auto-attribution signature present.
    assert "foreman-planner-bot" in body
    # Identification line.
    assert "**[planner] · [NEEDS_HELP]" in body


def test_build_body_with_extra_context_renders_block() -> None:
    payload = EscalationComment(
        why="x", what_tried="y", what_would_unblock="z",
        extra_context="## Subprocess signals\n- exit code: 137",
    )
    body = build_escalation_comment_body(
        role="worker",
        outcome_label="incomplete",
        summary="test",
        at=dt.datetime(2026, 6, 20, tzinfo=dt.UTC),
        payload=payload,
        ticket_ref="x/y#1",
        source="role:worker",
        key="k",
    )
    assert "## Subprocess signals" in body
    assert "exit code: 137" in body


def test_build_body_fallback_names_missing_field() -> None:
    body = build_escalation_comment_body(
        role="worker",
        outcome_label="incomplete",
        summary="worker could not finish",
        at=dt.datetime(2026, 6, 20, tzinfo=dt.UTC),
        payload=None,
        fallback_reason="worker LLM produced incomplete but did not populate escalation_comment",
        ticket_ref="x/y#1",
        source="role:worker",
        key="k1",
    )
    assert "role-side prompt did not populate" in body
    assert "fallback" in body
    assert "did not populate escalation_comment" in body


def test_body_contains_no_github_closing_keywords() -> None:
    """Regression guard mirror for foreman#63: the rendered template
    must contain no standalone auto-close keyword + ``#N`` tokens.

    The body intentionally mentions the literal label
    ``foreman:retry`` and the word ``role`` — neither is a closing
    keyword. We assert on the explicit nine keywords followed by a
    ``#N`` reference.
    """
    body = build_escalation_comment_body(
        role="planner",
        outcome_label="NEEDS_HELP",
        summary="s",
        at=dt.datetime(2026, 6, 20, tzinfo=dt.UTC),
        payload=_payload(),
        ticket_ref="x/y#42",
        source="role:planner",
        key="k",
    )
    import re

    pattern = re.compile(
        r"(?i)\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\b\s*:?\s+#\d+"
    )
    assert pattern.search(body) is None


# ---------------------------------------------------------------------------
# already_posted_for_key
# ---------------------------------------------------------------------------


def test_already_posted_for_key_matches_substring() -> None:
    comments = [
        _comment(
            f"{ESCALATION_MARKER_BEGIN}\n... source=role:planner:key=state-instance-1 ...\n"
            f"some body\n{ESCALATION_MARKER_END}"
        ),
    ]
    # The match relies on the substring being present alongside the
    # begin marker text.
    comments = [
        _comment(
            "<!-- foreman:escalation:begin ticket=x/y#1:source=role:planner:key=k1 -->\n"
            "body\n<!-- foreman:escalation:end -->"
        ),
    ]
    assert already_posted_for_key(
        comments, source="role:planner", key="k1"
    ) is True


def test_already_posted_for_key_misses_when_source_differs() -> None:
    comments = [
        _comment(
            "<!-- foreman:escalation:begin ticket=x/y#1:source=role:worker:key=k1 -->\n"
            "body\n<!-- foreman:escalation:end -->"
        ),
    ]
    assert already_posted_for_key(
        comments, source="role:planner", key="k1"
    ) is False


def test_already_posted_for_key_misses_when_key_differs() -> None:
    comments = [
        _comment(
            "<!-- foreman:escalation:begin ticket=x/y#1:source=role:planner:key=k1 -->\n"
            "body\n<!-- foreman:escalation:end -->"
        ),
    ]
    assert already_posted_for_key(
        comments, source="role:planner", key="k2"
    ) is False


def test_already_posted_for_key_empty_list() -> None:
    assert already_posted_for_key([], source="role:planner", key="k1") is False


# ---------------------------------------------------------------------------
# any_recent_marker_with_source_prefix
# ---------------------------------------------------------------------------


def _now() -> dt.datetime:
    return dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.UTC)


def test_any_recent_marker_with_source_prefix_matches_recent_prefix() -> None:
    posted_at = _now() - dt.timedelta(minutes=2)
    comments = [
        _comment(
            "<!-- foreman:escalation:begin ticket=x/y#1:source=role:planner:key=k1 -->\n"
            "body\n<!-- foreman:escalation:end -->",
            posted_at=posted_at,
        ),
    ]
    since = _now() - dt.timedelta(minutes=5)
    assert any_recent_marker_with_source_prefix(
        comments, source_prefix="role:", since=since,
    ) is True


def test_any_recent_marker_with_source_prefix_misses_stale_marker() -> None:
    posted_at = _now() - dt.timedelta(minutes=10)
    comments = [
        _comment(
            "<!-- foreman:escalation:begin ticket=x/y#1:source=role:planner:key=k1 -->\n"
            "body\n<!-- foreman:escalation:end -->",
            posted_at=posted_at,
        ),
    ]
    since = _now() - dt.timedelta(minutes=5)
    assert any_recent_marker_with_source_prefix(
        comments, source_prefix="role:", since=since,
    ) is False


def test_any_recent_marker_with_source_prefix_misses_wrong_prefix() -> None:
    posted_at = _now() - dt.timedelta(seconds=30)
    comments = [
        _comment(
            "<!-- foreman:escalation:begin ticket=x/y#1:source=terminal-landing:key=k1 -->\n"
            "body\n<!-- foreman:escalation:end -->",
            posted_at=posted_at,
        ),
    ]
    since = _now() - dt.timedelta(minutes=5)
    assert any_recent_marker_with_source_prefix(
        comments, source_prefix="role:", since=since,
    ) is False


# ---------------------------------------------------------------------------
# post_escalation_comment
# ---------------------------------------------------------------------------


def _host_mock(*, comments: list[CommentRef] | None = None) -> MagicMock:
    host = MagicMock()
    host.get_issue_comments.return_value = comments or []
    return host


def test_post_escalation_short_circuits_when_already_posted() -> None:
    existing = _comment(
        "<!-- foreman:escalation:begin ticket=x/y#1:source=role:planner:key=k1 -->\n"
        "body\n<!-- foreman:escalation:end -->"
    )
    host = _host_mock(comments=[existing])
    result = post_escalation_comment(
        host=host,
        repo_slug="x/y",
        issue_number=1,
        role="planner",
        outcome_label="NEEDS_HELP",
        summary="s",
        payload=_payload(),
        source="role:planner",
        key="k1",
    )
    assert result is False
    host.post_issue_comment.assert_not_called()


def test_post_escalation_proceeds_when_no_duplicate() -> None:
    host = _host_mock()
    result = post_escalation_comment(
        host=host,
        repo_slug="x/y",
        issue_number=1,
        role="planner",
        outcome_label="NEEDS_HELP",
        summary="s",
        payload=_payload(),
        source="role:planner",
        key="k1",
    )
    assert result is True
    host.post_issue_comment.assert_called_once()
    _, posted_args, _ = host.post_issue_comment.mock_calls[0]
    posted_body = posted_args[2]
    assert "source=role:planner:key=k1" in posted_body
    assert ESCALATION_MARKER_END in posted_body


def test_post_escalation_proceeds_on_get_comments_failure() -> None:
    host = MagicMock()
    host.get_issue_comments.side_effect = RuntimeError("github 5xx")
    result = post_escalation_comment(
        host=host,
        repo_slug="x/y",
        issue_number=1,
        role="planner",
        outcome_label="NEEDS_HELP",
        summary="s",
        payload=_payload(),
        source="role:planner",
        key="k1",
    )
    # Post still fires (duplicate preferable to missing).
    assert result is True
    host.post_issue_comment.assert_called_once()


def test_post_escalation_returns_false_on_post_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    host = _host_mock()
    host.post_issue_comment.side_effect = RuntimeError("github 5xx")
    with caplog.at_level(logging.ERROR, logger="foreman.roles._escalation_comment"):
        result = post_escalation_comment(
            host=host,
            repo_slug="x/y",
            issue_number=1,
            role="planner",
            outcome_label="NEEDS_HELP",
            summary="s",
            payload=_payload(),
            source="role:planner",
            key="k1",
        )
    assert result is False
    # Verify the helper logged the exception via logger.exception.
    assert any(
        "post_issue_comment failed" in r.message for r in caplog.records
    )


def test_post_escalation_renders_fallback_when_payload_is_none() -> None:
    host = _host_mock()
    result = post_escalation_comment(
        host=host,
        repo_slug="x/y",
        issue_number=1,
        role="worker",
        outcome_label="incomplete",
        summary="worker exited incomplete",
        payload=None,
        fallback_reason="worker LLM did not populate escalation_comment",
        source="role:worker",
        key="k1",
    )
    assert result is True
    posted_body = host.post_issue_comment.mock_calls[0].args[2]
    assert "role-side prompt did not populate" in posted_body
    assert "did not populate escalation_comment" in posted_body


# ---------------------------------------------------------------------------
# extract_subprocess_failure_signals
# ---------------------------------------------------------------------------


def test_extract_subprocess_failure_signals_parses_exit_code() -> None:
    exit_code, log_tail = extract_subprocess_failure_signals(
        failure_reason=(
            "RoleSubprocessError('role=worker exited 137 without "
            "emitting an outcome; see log at /tmp/x.log')"
        ),
        log_path=None,
    )
    assert exit_code == 137
    assert log_tail is None


def test_extract_subprocess_failure_signals_parses_on_disk_footer_form() -> None:
    exit_code, _ = extract_subprocess_failure_signals(
        failure_reason="--- exit code: 1 ---",
        log_path=None,
    )
    assert exit_code == 1


def test_extract_subprocess_failure_signals_returns_none_on_timeout() -> None:
    exit_code, _ = extract_subprocess_failure_signals(
        failure_reason=(
            "RoleSubprocessError('role=worker exceeded timeout 600s; "
            "see log at /tmp/x.log')"
        ),
        log_path=None,
    )
    assert exit_code is None


def test_extract_subprocess_failure_signals_handles_none_failure_reason() -> None:
    exit_code, _ = extract_subprocess_failure_signals(
        failure_reason=None, log_path=None,
    )
    assert exit_code is None


def test_extract_subprocess_failure_signals_reads_last_500_chars(
    tmp_path: Path,
) -> None:
    p = tmp_path / "role.log"
    body = "abcdefg" * 2000  # 14000 chars
    p.write_text(body, encoding="utf-8")
    _, log_tail = extract_subprocess_failure_signals(
        failure_reason=None, log_path=p,
    )
    assert log_tail is not None
    assert len(log_tail) == 500
    # Last 500 chars of "abcdefg" repeating is also "abcdefg" repeating.
    assert "abcdefg" in log_tail


def test_extract_subprocess_failure_signals_handles_missing_log(
    tmp_path: Path,
) -> None:
    nonexistent = tmp_path / "does-not-exist.log"
    exit_code, log_tail = extract_subprocess_failure_signals(
        failure_reason="exited 1", log_path=nonexistent,
    )
    assert exit_code == 1
    assert log_tail is None
