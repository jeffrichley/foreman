"""Packaged role system prompts. Markdown files loaded via importlib.resources.

Each Foreman role composes its system prompt from two layers:

1. **Vendored superpowers skills** (``superpowers/<name>.md``) — the
   discipline patterns superpowers gives interactive Claude Code
   sessions. The Foreman roles are not interactive Claude Code, so the
   SDK we run them through doesn't load skills on its own. We inline the
   skill content so the role LLM sees the same patterns.

2. **Role-specific prompt** (``<role>.md``) — Foreman concerns: the
   label vocabulary, the GitHub App identity, the branch convention,
   the structured output shape. These don't belong upstream in
   superpowers and stay here.

The two layers are concatenated with a ``---`` separator. Superpowers
content comes first so the discipline frames the role's specific
instructions; the role file finishes with the operational contract.
"""

from __future__ import annotations

from importlib import resources

_PROMPTS_ROOT = "foreman.prompts"
_SUPERPOWERS_ROOT = "foreman.prompts.superpowers"


def load_superpowers_skill(name: str) -> str:
    """Read one vendored superpowers SKILL.md by basename (no extension).

    Raises ``FileNotFoundError`` if the skill isn't in the vendored set —
    that's the right signal during refresh: if a caller asks for a skill
    we didn't vendor, the role startup should fail loudly rather than
    silently dropping the discipline.
    """
    return (
        resources.files(_SUPERPOWERS_ROOT)
        .joinpath(f"{name}.md")
        .read_text(encoding="utf-8")
    )


def load_role_prompt(role: str) -> str:
    """Read the Foreman-specific role prompt by role name."""
    return (
        resources.files(_PROMPTS_ROOT)
        .joinpath(f"{role}.md")
        .read_text(encoding="utf-8")
    )


def compose_role_prompt(*, role: str, superpowers: list[str]) -> str:
    """Compose a role's full system prompt: vendored skills + role prompt.

    ``superpowers`` is the ordered list of vendored skill names to inline
    before the role-specific prompt. Order matters — the role LLM reads
    top-to-bottom, so put the most foundational skill first (e.g.
    ``test-driven-development`` before ``executing-plans``).
    """
    parts: list[str] = [load_superpowers_skill(name) for name in superpowers]
    parts.append(load_role_prompt(role))
    return "\n\n---\n\n".join(parts)
