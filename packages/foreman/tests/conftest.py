"""Pytest fixtures shared across the foreman test suite."""

from __future__ import annotations

import os
from pathlib import Path

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


# Sibling FOREMAN_*/HOME scrub — see `_isolate_foreman_env` below.
# Listed at module level so the keystone self-test can assert against
# the same authoritative tuple instead of duplicating the names.
_FOREMAN_ENV_VARS_TO_SCRUB = (
    "FOREMAN_CONFIG_PATH",
    "FOREMAN_CONFIG",
    "FOREMAN_STATE_DIR",
    "FOREMAN_LOG_DIR",
    "FOREMAN_SHUTDOWN_SENTINEL_PATH",
    "FOREMAN_RELOAD_SENTINEL_PATH",
    "FOREMAN_LOCK_PATH",
    # App-identity env vars — the resolver in config.py honors env var
    # over the config-file literal, so leaking these from the container
    # process (docker-compose `env_file: .env`) flips test assertions
    # against the REAL App IDs. Surfaced 2026-06-07 in the autonomous-
    # loop's first end-to-end dogfood: foreman#138's Worker
    # verification-check ran the full foreman test suite inside the
    # daemon container, and `test_mint_invoked_with_resolved_app_credentials`
    # failed with `assert 3922445 == 123456` — the 3922445 is the real
    # planner App ID picked up from the container env, the 123456 is
    # the test's literal. Tests that genuinely need a specific App ID
    # value still set it explicitly via ``monkeypatch.setenv`` inside
    # the test body (this fixture runs first, so the test's setenv wins).
    "FOREMAN_PLANNER_APP_ID",
    "FOREMAN_REVIEWER_APP_ID",
    "FOREMAN_FIXER_APP_ID",
    "FOREMAN_WORKER_APP_ID",
    "FOREMAN_ORCHESTRATOR_APP_ID",
    "FOREMAN_ADMIN_TOKEN",
)


@pytest.fixture(autouse=True)
def _isolate_foreman_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """Scrub every env var that lets a test reach prod foreman state.

    Why: when foreman runs in a container, the daemon process exports
    ``FOREMAN_CONFIG_PATH=/etc/foreman/config.toml``,
    ``FOREMAN_STATE_DIR=/foreman/state``, ``FOREMAN_LOG_DIR=/foreman/logs``,
    and ``HOME=/root``. Role subprocesses (Planner, Worker, Reviewer,
    Fixer) inherit those — and so does any ``pytest`` invocation the
    role kicks off inside its worktree. A test that calls ``foreman
    daemon stop`` via Click or ``subprocess.run`` then resolves config
    + lock paths to the LIVE daemon's files, writes a real shutdown
    sentinel at ``/root/.foreman/shutdown-requested``, and the live
    daemon polls it on its next tick and shuts itself down.

    Surfaced 2026-06-06 in the autonomous-loop's reliability sweep: a
    role subprocess's pytest run wrote ``/root/.foreman/shutdown-requested``
    via the inherited prod env, the daemon detected the sentinel 30s
    later and gracefully shut down, the 8-ticket sweep died on the
    first poll cycle with cap-cascade noise as the visible symptom.

    The fix is defense in depth: the daemon scrubs these vars when
    dispatching role subprocesses (closes the prod path), AND this
    fixture scrubs them inside the test suite (closes every other
    path — local dev, CI, future tools, pytest invoked from inside a
    role's worktree). Either alone is incomplete; both make the bug
    structurally impossible.

    HOME is also rewritten to a fresh tmp dir so any code path that
    falls back to ``Path("~/.foreman/...").expanduser()`` lands in
    the sandbox, not on prod paths. Tests that genuinely need a
    specific env value: set it explicitly via ``monkeypatch.setenv``
    inside the test body — this fixture runs first, so the test's
    setenv wins.

    Returns the fake HOME path so tests can introspect it if needed.
    """
    fake_home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(fake_home))
    # USERPROFILE is HOME on Windows — scrub it the same way so
    # cross-platform tests stay consistent.
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    for var in _FOREMAN_ENV_VARS_TO_SCRUB:
        monkeypatch.delenv(var, raising=False)
    return fake_home


@pytest.fixture(autouse=True)
def _restore_foreman_logger_state():
    """Snapshot the ``foreman`` logger's handlers + level + propagate flag
    before each test and restore after.

    Why: ``configure_daemon_logging`` mutates the ``foreman`` logger
    (replaces handlers, sets ``propagate=False``, sets level). When a
    test that exercises the daemon CLI runs in the same pytest session
    as a downstream test using ``caplog``, the persistent
    ``propagate=False`` breaks caplog capture — records emitted under
    the ``foreman.*`` hierarchy walk up to ``foreman``, hit
    ``propagate=False``, and never reach the root logger where pytest's
    ``LogCaptureHandler`` is attached.

    Surfaced 2026-06-07 (foreman#131): the test
    ``test_reconciler_reload_idempotent_when_unchanged_logs_info`` passes
    in isolation and in the full sweep, but fails under the subset
    ``test_cli.py + reconciler/`` because a test_cli case mutates
    ``foreman.propagate``.

    The snapshot-and-restore makes the mutation test-local instead of
    session-persistent. Idempotent: tests that don't touch
    ``configure_daemon_logging`` see no change.
    """
    import logging as _logging

    foreman_logger = _logging.getLogger("foreman")
    saved_handlers = list(foreman_logger.handlers)
    saved_level = foreman_logger.level
    saved_propagate = foreman_logger.propagate

    try:
        yield
    finally:
        foreman_logger.handlers[:] = saved_handlers
        foreman_logger.level = saved_level
        foreman_logger.propagate = saved_propagate
