"""GitHub auto-close keyword sanitizer.

Single source of truth for the regex + helper that strips GitHub's
"closing keywords" from prose that will become a merged-PR body or a
commit message on a merged-PR branch.

Foreman routes issue closure exclusively through the v4 state machine's
authoritative close path: an impl PR merges → ``MergingState`` advances
to ``Done`` → :mod:`foreman.daemon_runners` calls ``close_issue`` only
AFTER the Reviewer-on-impl approved. If a Planner spec-PR body OR a
Worker commit body on the impl branch contains
``close[sd]?``/``fix(es|ed)?``/``resolve[sd]?`` followed by ``#N`` or
``owner/repo#N``, GitHub auto-closes the originating issue at
spec-merge time (or impl-merge time, before the close-out gate had a
chance to evaluate the Reviewer's verdict). That short-circuits the
loop's close-out gate (foreman#63).

Two layers of defense use this module:

* :mod:`foreman.roles.planner` — strips the LLM's ``pr_body`` before
  passing it to :meth:`GitHostProvider.open_pull_request`.
* :mod:`foreman.roles.worker` — strips the Worker LLM's commit message
  on the impl branch via ``git commit --amend`` BEFORE Python pushes.
  Limited to the single-commit case; the prompt is the
  primary defense for multi-commit runs (the runtime defense emits a
  log warning and skips amending when ``commits_made`` has > 1 entries
  to avoid destructive rebases).

The regex below matches the verb + an optional ``:`` separator + at
least one whitespace character, with a lookahead that preserves the
bare ``#N`` (or ``owner/repo#N``) reference so the surrounding prose
still reads cleanly after substitution.

foreman#63 background: an `algokit#21` Worker dogfood produced a commit
``docs(readme): add Build & Serve the Docs Site Locally section`` whose
body contained ``Closes #21``. Reviewer-on-impl correctly flagged it;
Fixer couldn't address it (v1 doesn't do git history surgery); the loop
dead-ended at NeedsHelp. This module is the runtime backstop for that
class of slip.
"""

from __future__ import annotations

import re

# Compiled once at import time. ``(?i)`` makes the whole pattern
# case-insensitive so ``Closes``/``CLOSES``/``closes`` all match;
# ``\b`` enforces a word boundary so ``foreclosed by #42`` (substring
# of another word) does NOT match. The optional ``:`` covers
# ``Closes: #42``; the ``\s+`` requires at least one whitespace
# character between the verb and the reference. The lookahead
# ``(?=(?:[\w.-]+/[\w.-]+)?#\d+)`` preserves the bare reference (either
# plain ``#N`` or the cross-repo ``owner/repo#N`` form) without
# consuming it.
AUTO_CLOSE_KEYWORDS_RE: re.Pattern[str] = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\b\s*:?\s+"
    r"(?=(?:[\w.-]+/[\w.-]+)?#\d+)"
)


def strip_auto_close_keywords(body: str) -> str:
    """Remove GitHub auto-close keyword+separator prefixes from ``body``.

    Idempotent. A no-op on bodies that contain no auto-close keywords,
    so callers can apply it unconditionally without paying a
    substitution cost on the clean path.

    The bare issue reference (``#N`` or ``owner/repo#N``) is preserved
    so the prose still reads sensibly after the strip — e.g., ``Closes
    #42`` becomes ``#42``, and a sentence like ``Implements the spec.
    Resolves #42`` becomes ``Implements the spec. #42``.

    Args:
        body: The prose to sanitize. Typically a PR body or a commit
            message (subject + body joined by a blank line).

    Returns:
        The same prose with every auto-close-verb prefix removed.
    """
    return AUTO_CLOSE_KEYWORDS_RE.sub("", body)


def contains_auto_close_keyword(body: str) -> bool:
    """Return ``True`` iff ``body`` contains at least one auto-close keyword.

    Cheap pre-check for the runtime strip. Callers (the Worker) use this
    to short-circuit the expensive path (``git commit --amend`` shells
    out) when the commit message is already clean.

    Args:
        body: The prose to inspect.

    Returns:
        ``True`` when a match is present, ``False`` otherwise (including
        the empty-string case).
    """
    if not body:
        return False
    return AUTO_CLOSE_KEYWORDS_RE.search(body) is not None
