"""Pytest fixtures shared across the foreman test suite."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _strip_git_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove inherited ``GIT_*`` env vars before each test.

    Why: git sets ``GIT_DIR``, ``GIT_WORK_TREE``, ``GIT_INDEX_FILE``,
    ``GIT_PREFIX``, etc. in the environment when invoking hooks
    (pre-push, pre-commit, ...). Child ``subprocess.run(["git", ...],
    cwd=tmp_path)`` calls inherit these. Git honors ``GIT_DIR`` over
    ``cwd`` — so ``git init`` inside an isolated ``tmp_path`` clone
    silently reinitializes the OUTER repo whose hook is firing, and
    every subsequent commit/branch/push from the test fixture operates
    on the wrong repository.

    Surfaced 2026-06-01 (foreman#19) when foreman's own pre-push hook
    fired ``just check`` while pushing the walking-skeleton branch:
    119 tests failed in hook context, all 358 passed in a plain shell.
    Test fixture commits ("seed" by "Test User") leaked onto the real
    foreman branch.

    The stripping is per-test via ``monkeypatch`` so it auto-reverts;
    no global mutation persists across the suite.
    """
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key, raising=False)
