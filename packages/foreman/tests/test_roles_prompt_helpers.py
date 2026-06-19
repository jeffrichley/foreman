"""Unit tests for the shared spec-side role prompt-section helpers
(foreman#328).

Pins the format-section + filter contracts in isolation so the role-side
integration tests can focus on wiring rather than re-deriving the exact
markdown shape on every assertion.
"""

from __future__ import annotations

from datetime import UTC, datetime

from foreman.git_host import CommentRef
from foreman.roles._prompt_helpers import (
    filter_bot_self_comments,
    format_comments_section,
    format_labels_section,
)

# ----------------------------------------------------------------------
# format_comments_section
# ----------------------------------------------------------------------


def test_format_comments_section_empty_returns_empty_string() -> None:
    assert format_comments_section([]) == ""


def test_format_comments_section_two_comments_renders_expected_blocks() -> None:
    older = CommentRef(
        author_login="alice",
        posted_at=datetime(2026, 6, 17, 20, 0, tzinfo=UTC),
        body="Also handle case X.",
    )
    newer = CommentRef(
        author_login="bob",
        posted_at=datetime(2026, 6, 17, 22, 15, tzinfo=UTC),
        body="Agreed, X is important.",
    )

    out = format_comments_section([older, newer])

    expected = (
        "## Comments\n"
        "\n"
        "### @alice (2026-06-17T20:00:00+00:00)\n"
        "Also handle case X.\n"
        "\n"
        "### @bob (2026-06-17T22:15:00+00:00)\n"
        "Agreed, X is important.\n"
        "\n"
    )
    assert out == expected


# ----------------------------------------------------------------------
# format_labels_section
# ----------------------------------------------------------------------


def test_format_labels_section_empty_returns_empty_string() -> None:
    assert format_labels_section(set()) == ""


def test_format_labels_section_two_labels_renders_sorted_joined() -> None:
    # Insertion order is not alphabetical — proves the helper sorts.
    out = format_labels_section({"priority:high", "foreman:planning"})
    assert out == "## Labels\nforeman:planning, priority:high\n\n"


# ----------------------------------------------------------------------
# filter_bot_self_comments
# ----------------------------------------------------------------------


def test_filter_bot_self_comments_drops_matching_logins() -> None:
    bot = CommentRef(
        author_login="foreman-planner[bot]",
        posted_at=datetime(2026, 6, 17, 20, 0, tzinfo=UTC),
        body="bot self-comment",
    )
    human = CommentRef(
        author_login="alice",
        posted_at=datetime(2026, 6, 17, 21, 0, tzinfo=UTC),
        body="human comment",
    )

    out = filter_bot_self_comments([bot, human], {"foreman-planner[bot]"})

    assert out == [human]


def test_filter_bot_self_comments_preserves_chronological_order() -> None:
    older = CommentRef(
        author_login="alice",
        posted_at=datetime(2026, 6, 17, 20, 0, tzinfo=UTC),
        body="first",
    )
    newer = CommentRef(
        author_login="bob",
        posted_at=datetime(2026, 6, 17, 22, 0, tzinfo=UTC),
        body="second",
    )

    out = filter_bot_self_comments([older, newer], set())

    assert out == [older, newer]


def test_filter_bot_self_comments_returns_new_list() -> None:
    """The helper must NOT mutate the input list — callers may keep a
    reference to the unfiltered comments for telemetry / audit."""
    human = CommentRef(
        author_login="alice",
        posted_at=datetime(2026, 6, 17, 20, 0, tzinfo=UTC),
        body="x",
    )
    original = [human]
    out = filter_bot_self_comments(original, set())

    assert out is not original
    assert out == original
