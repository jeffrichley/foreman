"""Shared subprocess-env filter for operations targeting a foreign worktree.

When Foreman runs ``git push`` or ``uv sync`` inside a *different* repo's
worktree (the per-ticket worktree at
``~/.foreman/worktrees/<repo>/issue-<N>/``), the target's pre-push hooks
inherit our parent process's environment. If ``VIRTUAL_ENV`` (or other
Python / uv env vars) point at Foreman's own venv, ``uv run --no-sync``
in the hook detects the mismatch with the target worktree, ignores the
env var, and creates a fresh empty ``.venv`` inside the target — then
mypy/pytest fail because that new venv has no deps installed.

Issue #10 surfaced this on 3 consecutive dogfood runs (2026-05-31).
Workaround was ``env -u VIRTUAL_ENV git push`` by hand.

The same blocklist is needed in *two* places:
- ``GitHubProvider._git`` — when pushing, configuring, or committing in
  the target worktree.
- ``WorktreeManager.create`` — when running ``uv sync --all-packages``
  in the freshly-created worktree to pre-populate its ``.venv`` (so the
  pre-push hook actually has deps to run against).

Both paths share this filter to keep the rationale and the blocklist in
one place.
"""

from __future__ import annotations

import os

# Env vars that would mis-direct any Python / uv tool running inside a
# DIFFERENT repo's worktree.
#
# UV_CACHE_DIR is intentionally NOT on the blocklist — cache sharing
# across repos is safe and desirable (faster syncs).
FOREIGN_WORKTREE_BLOCKED_ENV_VARS = frozenset(
    {
        # --- Python interpreter / module path ---
        "VIRTUAL_ENV",  # load-bearing — the one that triggered #10
        "PYTHONHOME",  # would mis-direct python interpreter selection
        "PYTHONPATH",  # would pull foreman modules into hook imports
        "CONDA_PREFIX",  # uv also detects this as an "active env"
        # --- uv project / interpreter selection ---
        # Sourced from https://docs.astral.sh/uv/reference/environment/
        "UV_PROJECT",  # equivalent to --project
        "UV_PROJECT_ENVIRONMENT",  # path to project venv dir
        "UV_WORKING_DIR",  # equivalent to --directory
        "UV_PYTHON",  # path to interpreter for all ops
        "UV_SYSTEM_PYTHON",  # use first python on PATH
        "UV_PYTHON_PREFERENCE",  # system vs managed
        "UV_PYTHON_SEARCH_PATH",  # PATH override for python discovery
        "UV_MANAGED_PYTHON",  # force use of uv-managed python
        "UV_NO_MANAGED_PYTHON",  # forbid uv-managed python
    }
)


def filtered_subprocess_env() -> dict[str, str]:
    """Return ``os.environ`` minus env vars that would mis-direct a foreign worktree.

    See :data:`FOREIGN_WORKTREE_BLOCKED_ENV_VARS` for rationale.

    Returns:
        A copy of the current process environment with the blocklist removed.
        Everything else (PATH, HOME, USERPROFILE, GIT_*, GH_TOKEN, LANG,
        UV_CACHE_DIR, ...) is preserved so the target repo's hooks and any
        ``uv sync`` we run inside the worktree behave the same as a human
        invocation would.
    """
    return {k: v for k, v in os.environ.items() if k not in FOREIGN_WORKTREE_BLOCKED_ENV_VARS}
