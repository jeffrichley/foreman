"""Shared user-prompt section formatters for spec-side roles (foreman#328).

The Planner, Reviewer-on-spec, and Fixer-on-spec all need to splice the
originating issue's **comments** into their user prompts (the Planner
additionally splices **labels**). Pulling the format into one module
keeps the prompt-section shape consistent across roles and gives the
"impl-side roles must stay byte-identical" regression tests a single
seam to verify.

SRP — one tiny module, three pure formatters, no I/O. Empty inputs
return ``""`` so role composers can splice unconditionally without an
empty-header check.
"""

from __future__ import annotations

from foreman.git_host import CommentRef


def format_comments_section(comments: list[CommentRef]) -> str:
    """Render an issue's comments as a ``## Comments`` markdown section.

    Returns ``""`` when ``comments`` is empty so callers can concatenate
    unconditionally without producing an empty header.

    Each comment renders as::

        ### @{author_login} ({iso8601_timestamp})
        {body}

    Comments are emitted in the order they're passed in — callers are
    responsible for chronological ordering (the provider returns them
    oldest-first; the role-side filter preserves that order).
    """
    if not comments:
        return ""
    blocks: list[str] = ["## Comments", ""]
    for c in comments:
        blocks.append(f"### @{c.author_login} ({c.posted_at.isoformat()})")
        blocks.append(c.body)
        blocks.append("")
    return "\n".join(blocks) + "\n"


def format_labels_section(labels: set[str]) -> str:
    """Render an issue's labels as a ``## Labels`` markdown section.

    Returns ``""`` when ``labels`` is empty. Otherwise emits::

        ## Labels
        {label1}, {label2}

    Labels are sorted alphabetically for determinism — the LLM doesn't
    need stable order, but tests benefit.
    """
    if not labels:
        return ""
    joined = ", ".join(sorted(labels))
    return f"## Labels\n{joined}\n\n"


def filter_bot_self_comments(
    comments: list[CommentRef], exclude_logins: set[str]
) -> list[CommentRef]:
    """Drop comments whose ``author_login`` is in ``exclude_logins``.

    Returns a new list (the input is not mutated) and preserves the input
    order. Used by the spec-side role dispatchers to strip foreman
    role-bot self-comments from the originating issue's comment stream
    before feeding them into the LLM's user prompt — without this, the
    bots' own previous postings would feed back into the next run and
    skew the LLM's understanding of the operator's intent.
    """
    return [c for c in comments if c.author_login not in exclude_logins]
