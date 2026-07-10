"""Tests for the shared GitHub auto-close keyword sanitizer.

foreman#63 + Phase 8d.22: GitHub auto-closes the originating issue at
merge time when a merged PR's body — or the body of any commit on a
merged PR's branch — contains one of nine "closing keywords"
(``close``/``closes``/``closed``, ``fix``/``fixes``/``fixed``,
``resolve``/``resolves``/``resolved``) followed by ``#N`` or
``owner/repo#N``. Foreman routes issue closure exclusively through the
state machine's authoritative close path (impl-PR-merge →
``daemon_runners.close_issue``); any auto-close keyword in a Worker
commit body or a Planner PR body would short-circuit that gate.

The :mod:`foreman.auto_close` module is the single source of truth for
the regex + helper. Both :mod:`foreman.roles.planner` (PR body) and
:mod:`foreman.roles.worker` (commit body) import from here.
"""

from __future__ import annotations

import pytest

from foreman.auto_close import (
    AUTO_CLOSE_KEYWORDS_RE,
    contains_auto_close_keyword,
    strip_auto_close_keywords,
)


def test_strip_auto_close_keywords_basic_close() -> None:
    """``Closes #N`` → ``#N`` (preserve the bare reference)."""
    assert strip_auto_close_keywords("foo\n\nCloses #21") == "foo\n\n#21"


def test_strip_auto_close_keywords_case_insensitive() -> None:
    """All-caps and lowercase variants are both stripped."""
    assert strip_auto_close_keywords("FIXES #21") == "#21"
    assert strip_auto_close_keywords("closes #21") == "#21"


def test_strip_auto_close_keywords_idempotent() -> None:
    """Running the strip twice produces the same output as once."""
    once = strip_auto_close_keywords("Resolves #21 and Fixes #22")
    twice = strip_auto_close_keywords(once)
    assert once == twice


def test_strip_auto_close_keywords_owner_repo_form() -> None:
    """The cross-repo ``owner/repo#N`` form is preserved after the keyword strip."""
    assert strip_auto_close_keywords("resolves jeffrichley/algokit#21") == "jeffrichley/algokit#21"


def test_strip_auto_close_keywords_no_op_on_clean() -> None:
    """A clean body with a bare ``See #N`` reference is unchanged."""
    assert strip_auto_close_keywords("feat: add X\n\nSee #21") == "feat: add X\n\nSee #21"


def test_strip_auto_close_keywords_handles_subject_line() -> None:
    """Subject-line case (no preceding newline) must also be stripped.

    The Worker can emit a one-liner commit like
    ``fix(foo): closes #21`` which would otherwise slip past the
    regex if it were anchored to a body-only shape.
    """
    assert strip_auto_close_keywords("fix(foo): closes #21") == "fix(foo): #21"


def test_strip_auto_close_keywords_empty_body() -> None:
    """Empty body is a no-op."""
    assert strip_auto_close_keywords("") == ""


# ----------------------------------------------------------------------
# contains_auto_close_keyword — cheap pre-check for the runtime strip,
# so callers can skip subprocess work when the commit message is clean.
# ----------------------------------------------------------------------


def test_contains_auto_close_keyword_true_on_match() -> None:
    assert contains_auto_close_keyword("docs(readme): add section\n\nCloses #21")


def test_contains_auto_close_keyword_false_on_clean() -> None:
    assert not contains_auto_close_keyword("docs(readme): add section\n\nSee #21")


def test_contains_auto_close_keyword_false_on_empty() -> None:
    assert not contains_auto_close_keyword("")


# ----------------------------------------------------------------------
# Negative cases — word-boundary protection
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # Substring of another word — must NOT match (word boundary)
        ("foreclosed by #42", "foreclosed by #42"),
        # Bare reference — survives untouched
        ("See #42 for context.", "See #42 for context."),
    ],
)
def test_strip_auto_close_keywords_word_boundary_safety(body: str, expected: str) -> None:
    assert strip_auto_close_keywords(body) == expected


def test_AUTO_CLOSE_KEYWORDS_RE_is_compiled_regex() -> None:
    """Module exposes the compiled regex (planner / worker import it)."""
    import re

    assert isinstance(AUTO_CLOSE_KEYWORDS_RE, re.Pattern)
