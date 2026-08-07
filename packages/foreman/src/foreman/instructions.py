"""Per-project instructions loader — ``.foreman/INSTRUCTIONS.md``.

Each project opting into Foreman can keep a ``.foreman/INSTRUCTIONS.md``
file at the root of its local clone. The file carries project-specific
guidance (PR title rules, branch conventions, project-specific notes,
quirks) that the four role bots embed in their per-run user prompt.

Missing instructions are not an error — most projects start without one
and accumulate guidance as they grow. The role dispatchers call this
loader unconditionally and pass ``None`` straight through to their
``_build_user_prompt`` helpers, which simply omit the section.

*Missing* and *unreadable* are different events, though, and only the
first is normal. A file that is on disk but cannot be read is logged at
warning level rather than being folded into the silent path — treating
the two identically is what allowed issue #586 to run undetected for its
whole lifetime, with every role silently unconfigured while the repo
asserted its instructions were being consumed.

Why a separate module: keeps the role orchestrators thin and lets every
role share one canonical implementation. Future work (validation,
versioning, schema-driven sections) lands here without touching any of
the four role files.
"""

from __future__ import annotations

import logging
from pathlib import Path

_log = logging.getLogger(__name__)

# Canonical relative path inside the project clone. Pinned as a module
# constant so the ``foreman init`` template writer and the loader stay
# in lockstep without either having to reach into the other.
INSTRUCTIONS_RELPATH = Path(".foreman") / "INSTRUCTIONS.md"


def load_project_instructions(clone_path: Path) -> str | None:
    """Load ``.foreman/INSTRUCTIONS.md`` from the role's checked-out worktree.

    Args:
        clone_path: Path to a **checked-out worktree**, not the bare mirror
            base (``project.local_clone_path``). Bare mirrors hold git
            objects only — no file is ever present on disk there — so
            passing a bare mirror path always returns ``None`` even when
            ``.foreman/INSTRUCTIONS.md`` is committed. Use the ``wt_path``
            value returned by ``WorktreeManager.create``,
            ``WorktreeManager.attach``, or ``WorktreeManager.attach_impl``
            as the argument. The instructions file is read relative to this
            path.

    Returns:
        The file's contents as a UTF-8 string when present, or ``None``
        when the file is absent or cannot be read.

        The two ``None`` cases are NOT the same event, and only one of
        them is normal:

        * **Absent** — no ``.foreman/INSTRUCTIONS.md``. Expected; most
          projects start this way. Silent, and the dispatcher simply
          omits the section.
        * **Unreachable** — the file is on disk but the read failed.
          Logged at warning level with the path and the underlying
          error. Still returns ``None`` so a role is never blocked by
          it, but it never again passes for "this project has no
          instructions" (issue #586).
    """
    instructions_path = clone_path / INSTRUCTIONS_RELPATH
    if not instructions_path.exists():
        return None
    try:
        return instructions_path.read_text(encoding="utf-8")
    except OSError as exc:
        # The file EXISTS — .exists() just said so — and we could not read
        # it. Returning a bare None here made that byte-identical to "this
        # project never wrote instructions", which is what let #586 run for
        # its entire lifetime with every role silently unconfigured. Roles
        # still proceed; the difference is that this one leaves a trace.
        _log.warning(
            "project instructions present but unreadable at %s (%s: %s) — "
            "roles will run WITHOUT project-specific instructions",
            instructions_path,
            type(exc).__name__,
            exc,
        )
        return None
