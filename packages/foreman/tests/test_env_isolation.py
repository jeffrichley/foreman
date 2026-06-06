"""Keystone tests for the ``_isolate_foreman_env`` conftest fixture.

These tests don't exercise foreman code — they exercise the safety net
the rest of the suite depends on. If any of these break, the suite has
lost its prod-state isolation guarantee and a single misbehaving test
can write the real ``~/.foreman/shutdown-requested`` (the bug that
surfaced 2026-06-06 in the autonomous-loop sweep).

The fixture is autouse + session-tmp-backed; tests just assert the
post-conditions hold.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Mirrors the tuple in ``conftest.py``. Kept inline here on purpose:
# this test asserts WHAT must be absent regardless of where the
# fixture is defined, so the duplication is a feature — if the
# fixture's list ever drifts from this one, one of them is wrong and
# the test will catch it.
_EXPECTED_SCRUBBED_VARS = (
    "FOREMAN_CONFIG_PATH",
    "FOREMAN_CONFIG",
    "FOREMAN_STATE_DIR",
    "FOREMAN_LOG_DIR",
    "FOREMAN_SHUTDOWN_SENTINEL_PATH",
    "FOREMAN_RELOAD_SENTINEL_PATH",
    "FOREMAN_LOCK_PATH",
)


def test_isolate_foreman_env_scrubs_every_named_var() -> None:
    """Every var the fixture promises to scrub is gone from os.environ.

    If a new env var gets added to the FOREMAN_* family, this test
    must be updated alongside the conftest fixture. The two lists
    living separately is intentional: it catches drift.
    """
    for var in _EXPECTED_SCRUBBED_VARS:
        assert var not in os.environ, (
            f"{var} leaked through the env-isolation fixture. "
            "Either the fixture failed to scrub it, or a test ran "
            "setenv on it without monkeypatch (which is the only way "
            "the value would survive between tests)."
        )


def test_isolate_foreman_env_rewrites_home_to_sandbox(tmp_path: Path) -> None:
    """HOME points to a fixture-owned tmp dir, not the real /root or /home.

    This is the load-bearing assertion. Any code that calls
    ``Path("~/.foreman/...").expanduser()`` resolves through HOME.
    If HOME is the prod ``/root`` inside the daemon container, the
    expansion lands on the live daemon's state.
    """
    home = os.environ["HOME"]
    # The fake home is a real pytest tmp dir — must exist + must not
    # be the container's prod root.
    assert Path(home).exists(), f"HOME points at {home!r} which does not exist"
    assert home != "/root", (
        "HOME is the container's prod root — the fixture did not "
        "rewrite it. A test that expands ``~/.foreman/...`` will "
        "hit the live daemon's state."
    )
    # Sanity: ``~`` expansion now lands inside the sandbox, not on
    # a prod-shaped path.
    expanded = Path("~/.foreman/shutdown-requested").expanduser()
    assert str(expanded).startswith(home), (
        f"Path('~/.foreman/...').expanduser() resolved to {expanded!r} "
        f"which is OUTSIDE the fixture's sandbox HOME {home!r}. "
        "The fixture's HOME rewrite is broken."
    )


def test_isolate_foreman_env_lets_tests_override_locally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tests that need a specific FOREMAN_* value can set it locally.

    The fixture runs FIRST (autouse), then the test's own
    monkeypatch.setenv runs and wins. After the test, both reverts
    cleanly. This is the contract that makes the autouse fixture
    non-invasive: opt-out is monkeypatch in the test.
    """
    custom_config = tmp_path / "test-config.toml"
    custom_config.write_text("# test override\n", encoding="utf-8")
    monkeypatch.setenv("FOREMAN_CONFIG_PATH", str(custom_config))

    assert os.environ["FOREMAN_CONFIG_PATH"] == str(custom_config), (
        "Test's monkeypatch.setenv did not override the fixture's "
        "delenv. The fixture is ordering wrong or running after the "
        "test setup."
    )
