"""Per-project instructions loader — ``.foreman/INSTRUCTIONS.md``.

Each project opting into Foreman can keep a ``.foreman/INSTRUCTIONS.md``
file at the root of its local clone. The file carries project-specific
guidance (PR title rules, branch conventions, project-specific notes,
quirks) that the four role bots embed in their per-run user prompt.

Missing instructions are not an error — most projects start without one
and accumulate guidance as they grow. The role dispatchers call this
loader unconditionally and pass ``None`` straight through to their
``_build_user_prompt`` helpers, which simply omit the section.

Why a separate module: keeps the role orchestrators thin and lets every
role share one canonical implementation. Future work (validation,
versioning, schema-driven sections) lands here without touching any of
the four role files.
"""

from __future__ import annotations

from pathlib import Path

# Canonical relative path inside the project clone. Pinned as a module
# constant so the ``foreman init`` template writer and the loader stay
# in lockstep without either having to reach into the other.
INSTRUCTIONS_RELPATH = Path(".foreman") / "INSTRUCTIONS.md"


def load_project_instructions(clone_path: Path) -> str | None:
    """Load ``.foreman/INSTRUCTIONS.md`` from the project's local clone.

    Args:
        clone_path: Path to the project's local clone (the directory the
            role dispatcher's worktree is branched from). The instructions
            file is read relative to this path.

    Returns:
        The file's contents as a UTF-8 string when present, or ``None``
        when the file (or its parent ``.foreman/`` directory) is absent
        or cannot be read. Instructions are optional, so a missing file
        is not an error — the role dispatcher simply omits the
        "Project-specific instructions" section from its user prompt.
    """
    instructions_path = clone_path / INSTRUCTIONS_RELPATH
    if not instructions_path.exists():
        return None
    try:
        return instructions_path.read_text(encoding="utf-8")
    except OSError:
        # Permissions glitch, race with deletion, encoding error — any
        # of these is a non-fatal "treat instructions as absent" signal.
        # The bots can still operate; they just don't get the project
        # nudge this run.
        return None
