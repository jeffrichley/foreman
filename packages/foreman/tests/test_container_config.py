"""Regression tests for ``docker/foreman/config.toml.container``.

The container config TOML is the runtime contract between
``docker-compose.yml`` (which mounts the ``foreman-state`` named volume
at ``/foreman/state/``) and the v3 reconciler (which sqlite-persists
the execution log used by ``has_unterminated()`` /
``count_completed()``). When ``reconciler.db_path`` is left at its
default ``~/.foreman/reconciler.sqlite``, the file lands on the
container's ephemeral root filesystem and is lost on every restart —
which silently re-dispatches in-flight roles (issue #139).

This test pins the invariant: the literal container TOML on disk
overrides ``reconciler.db_path`` to a path under ``/foreman/state/``.
If a future edit drops the override, this test fails before the
container ever ships.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from foreman.config import Config


def _container_config_path() -> Path:
    """Path to ``docker/foreman/config.toml.container`` relative to repo root.

    Walks parents from ``__file__``:
    ``packages/foreman/tests/test_container_config.py`` -> repo root is
    ``parents[3]``.
    """
    return Path(__file__).parents[3] / "docker" / "foreman" / "config.toml.container"


def test_container_config_pins_reconciler_db_path_to_named_volume() -> None:
    """``reconciler.db_path`` resolves under ``/foreman/state/``.

    The ``foreman-state`` Docker named volume is mounted at
    ``/foreman/state/`` (``docker-compose.yml``). The reconciler sqlite
    MUST live there so the execution log survives ``docker compose down``
    / restarts. Default ``~/.foreman/reconciler.sqlite`` resolves to
    ``/root/.foreman/reconciler.sqlite`` inside the container — on the
    ephemeral root fs — and loses state. See issue #139.
    """
    container_toml = _container_config_path()
    assert container_toml.is_file(), (
        f"expected container config TOML at {container_toml}; "
        "test path-resolution logic is stale"
    )

    with container_toml.open("rb") as fh:
        raw = tomllib.load(fh)

    cfg = Config.model_validate(raw)

    assert cfg.reconciler.db_path == "/foreman/state/reconciler.sqlite", (
        "container config must pin reconciler.db_path to the "
        "foreman-state named volume; default ~/.foreman/... lands on "
        "the container's ephemeral root fs and loses execution-log "
        "state on every restart (issue #139)"
    )
