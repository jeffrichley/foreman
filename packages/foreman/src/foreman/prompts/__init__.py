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

# Preamble injected at the very top of every composed role prompt. The
# vendored superpowers SKILL.md files were written for interactive
# Claude Code, which has a Skill tool, a working directory it owns, and
# the ability to invoke other skills dynamically. The Foreman role LLMs
# have none of those affordances — they run via the claude-agent-sdk
# against a per-ticket worktree. Without this adapter, lines like
# "REQUIRED SUB-SKILL: Use superpowers:executing-plans" or
# "Save plans to: docs/superpowers/plans/..." would either cause the
# LLM to look for a Skill tool that doesn't exist, or send it to a
# directory that's wrong for the role's actual contract.
#
# The preamble is short on purpose: a few terse rules at the top frame
# how the LLM should interpret every cross-reference it's about to see.
_ADAPTER_PREAMBLE = """\
# Foreman role adapter (read first)

You are an SDK-driven Foreman role agent — not interactive Claude
Code. The discipline patterns below were originally written for
interactive Claude Code; this preamble tells you how to interpret
them in your environment.

## Tools you have

- File ops: `Read`, `Glob`, `Grep`
- Mutation (some roles only): `Edit`, `Write`, `Bash`
- That is the entire tool surface.

## Things the discipline patterns mention that you DO NOT have

- **No `Skill` tool.** Cross-references that say "REQUIRED SUB-SKILL:
  Use superpowers:X" or "invoke the X skill" are informational only.
  The relevant skill content has been inlined for you here (or, if it
  isn't, the role contract below covers it). Do NOT try to call a
  Skill tool. Do NOT halt waiting for one. Apply the discipline
  directly from this prompt.
- **No `TodoWrite` / `TaskCreate` tool.** When a skill says "create
  todos" or "track in TodoWrite", treat that as "decompose into a
  numbered checklist in your structured output". Your output schema
  is the audit trail.
- **No working directory ownership.** Skills reference paths like
  `docs/superpowers/plans/`, `~/.config/superpowers/worktrees/`, etc.
  Those are interactive-Claude-Code defaults. The Foreman role
  contract BELOW names the EXACT paths to use (e.g., the Planner
  writes specs to `docs/superpowers/specs/foreman-issue-<N>-spec.md`,
  not `docs/superpowers/plans/`). Always defer to the role contract
  for paths and filenames.
- **No conversation with a human.** Skills sometimes say "ask the
  user" or "present options". You cannot ask. You must decide,
  document the decision in your structured output, and proceed. If
  genuine ambiguity blocks you, the role contract has a specific
  outcome label (`spec_invalid`, `needs-help`, etc.) — use that
  instead of stalling.

## Order of authority

When the inlined discipline conflicts with the role contract below,
**the role contract wins.** The discipline is the *how*; the role
contract is the *what and where*. Foreman's label vocabulary, branch
conventions, structured output schema, and identity model are not
negotiable.
"""


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


def load_role_prompt(role: str, *, target: str | None = None) -> str:
    """Read the Foreman-specific role prompt by role name.

    When ``target == "impl_pr"`` AND a target-specific prompt file
    exists at ``<role>_impl.md``, return that. Otherwise fall back
    to the canonical ``<role>.md``.

    The mapping from ``(role, target)`` to filename is a fact about
    the prompt directory layout — it lives in this loader, not in
    each role's call site. Roles that don't have a target-specific
    variant (Planner, Worker today) automatically fall back; no
    branching at the role layer.

    Unrecognized target values (including the documented ``"spec_pr"``)
    fall back to ``<role>.md`` without raising. Wrong-target errors are
    the role's precondition gate to surface, not the loader's — the
    loader's job is to resolve a path, full stop.
    """
    if target == "impl_pr":
        impl_path = resources.files(_PROMPTS_ROOT).joinpath(f"{role}_impl.md")
        if impl_path.is_file():
            return impl_path.read_text(encoding="utf-8")
    return (
        resources.files(_PROMPTS_ROOT)
        .joinpath(f"{role}.md")
        .read_text(encoding="utf-8")
    )


def _wrap_skill(name: str) -> str:
    """Wrap one vendored SKILL.md with a short header that names it as a
    superpowers skill and tells the LLM to treat downstream
    "use superpowers:<name>" references as "apply what's in this block."

    This closes the dangling cross-references the upstream content
    contains. The vendored ``writing-plans`` skill, for example, points
    callers at ``superpowers:executing-plans`` and
    ``superpowers:subagent-driven-development`` — the first IS vendored
    elsewhere in the same composed prompt, the second is NOT. With this
    wrapper:

    - For vendored siblings: the LLM searches the composed prompt for
      the named block and applies that discipline directly.
    - For un-vendored references: the top-level adapter preamble already
      told the LLM that those are informational; this wrapper just
      reinforces that the loaded skill IS what it appears to be (no need
      to look for a Skill tool that doesn't exist).
    """
    header = (
        f"## superpowers:{name} (inlined)\n\n"
        f"This entire block IS the superpowers ``{name}`` skill. When any\n"
        f"other section says ``Use superpowers:{name}`` or "
        f"``REQUIRED SUB-SKILL: superpowers:{name}``, that instruction\n"
        f"means **apply the discipline below — it is already loaded.**\n"
        f"Do not look for a separate skill file to read; do not call a\n"
        f"Skill tool. The text below is the discipline.\n"
    )
    return f"{header}\n{load_superpowers_skill(name)}"


def compose_role_prompt(
    *,
    role: str,
    superpowers: list[str],
    target: str | None = None,
) -> str:
    """Compose a role's full system prompt with three layers:

    1. **Adapter preamble** (:data:`_ADAPTER_PREAMBLE`) — read first,
       tells the LLM how to interpret cross-references and missing
       tools in the layers that follow.
    2. **Vendored superpowers skills**, each wrapped with a header
       (:func:`_wrap_skill`) that explicitly names the block as that
       skill so downstream references resolve to the in-prompt content
       instead of stalling on a missing Skill tool.
    3. **Foreman role contract** — the role's labels, branch
       conventions, structured output schema. ``target`` is forwarded
       to :func:`load_role_prompt` so target-specific role prompts
       (e.g. ``reviewer_impl.md``) are loaded for the impl-side
       variants — see foreman#78 / foreman#79.

    ``superpowers`` is the ordered list of vendored skill names to inline
    between the preamble and the role contract. Order matters — the role
    LLM reads top-to-bottom, so put the most foundational skill first
    (e.g. ``test-driven-development`` before ``executing-plans``).

    Layers are separated by ``---`` so the LLM sees clear section
    boundaries.
    """
    parts: list[str] = [_ADAPTER_PREAMBLE]
    parts.extend(_wrap_skill(name) for name in superpowers)
    parts.append(load_role_prompt(role, target=target))
    return "\n\n---\n\n".join(parts)
