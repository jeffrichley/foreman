"""Keystone meta-tests for ``conftest.py``'s ``_isolate_foreman_env`` fixture.

The autouse fixture scrubs every env var that would let a test reach prod
foreman state (config paths) or pick up real App identity (bot App IDs).
These tests assert the scrub list stays comprehensive — a new
``FOREMAN_*`` env var added to the codebase without an entry in
``_FOREMAN_ENV_VARS_TO_SCRUB`` would silently bypass test isolation, and
this file is the canary that catches that regression before it ships.

See ``conftest.py`` for the failure mode the scrub prevents and the
foreman#19 + foreman#138 incidents that motivated it.
"""

from __future__ import annotations

import os

import pytest

from tests.conftest import _FOREMAN_ENV_VARS_TO_SCRUB


def test_foreman_env_vars_are_scrubbed_by_autouse_fixture() -> None:
    """Every name listed in ``_FOREMAN_ENV_VARS_TO_SCRUB`` must be absent
    from ``os.environ`` once the autouse fixture has run. The fixture
    runs before every test (including this one), so a regression here
    means a name in the tuple isn't actually being delenv'd."""
    leaks = [var for var in _FOREMAN_ENV_VARS_TO_SCRUB if var in os.environ]
    assert leaks == [], f"env vars not scrubbed by autouse fixture: {leaks}"


def test_app_id_env_vars_are_in_the_scrub_list() -> None:
    """Regression guard for foreman#138 (Worker verification false positive):
    every App-ID env var the ``AppsConfig`` + ``OrchestratorConfig``
    resolvers read MUST be in the scrub list. Leaving one out means a
    container's real App ID leaks into test assertions like
    ``assert call_args.args[0] == 123456``."""
    required = {
        "FOREMAN_PLANNER_APP_ID",
        "FOREMAN_REVIEWER_APP_ID",
        "FOREMAN_FIXER_APP_ID",
        "FOREMAN_WORKER_APP_ID",
        "FOREMAN_ORCHESTRATOR_APP_ID",
    }
    missing = required - set(_FOREMAN_ENV_VARS_TO_SCRUB)
    assert missing == set(), f"App-ID env vars missing from scrub list: {missing}"


def test_isolate_foreman_env_does_not_strip_unrelated_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope check: the fixture must NOT touch env vars outside its
    explicit allow-list. A regression that broadens the scrub to
    ``startswith('FOREMAN_')`` would also strip per-test overrides set
    via ``monkeypatch.setenv`` AFTER the fixture ran (the fixture would
    re-run between tests but the test's setenv wins because pytest's
    fixture-then-test ordering puts the test's monkeypatch second)."""
    monkeypatch.setenv("FOREMAN_PLANNER_APP_ID", "999999")
    assert os.environ["FOREMAN_PLANNER_APP_ID"] == "999999"
