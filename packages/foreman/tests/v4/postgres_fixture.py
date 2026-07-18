"""Session-scoped Postgres testcontainer + per-test reset.

The container starts once per pytest session (image pull + boot is
~5s). Each test gets an empty schema via TRUNCATE ... RESTART IDENTITY
CASCADE in the per-test fixture, which is far cheaper than recreating
the container.

Skips the whole module if Docker is unavailable (e.g. a dev box with
no daemon), so the suite degrades gracefully instead of erroring.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

# Windows dev boxes: testcontainers' Ryuk reaper sidecar fails its port-
# mapping lookup under Docker Desktop, erroring every Postgres test. The
# fixture's `with PostgresContainer(...)` context manager already cleans up
# the container deterministically, so disabling Ryuk here is safe. Scoped to
# Windows only — Linux CI keeps Ryuk (guaranteed cleanup even on hard crash).
if sys.platform == "win32":
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

_SCHEMA = Path(__file__).parents[2] / "src" / "foreman" / "v4" / "postgres_schema.sql"

try:  # pragma: no cover - import guard
    from testcontainers.postgres import PostgresContainer

    _HAVE_TESTCONTAINERS = True
except Exception:  # pragma: no cover
    _HAVE_TESTCONTAINERS = False


def _docker_daemon_reachable() -> bool:  # pragma: no cover - env probe
    """True only if a Docker daemon actually answers — not merely that the
    ``testcontainers``/``docker`` libraries import.

    foreman#419: the old guard checked import-ability (``_HAVE_DOCKER``),
    which is ALWAYS true in the daemon container (the libs are installed) —
    but the container has no Docker socket. So ``PostgresContainer(...)``
    ran anyway and raised ``FileNotFoundError`` on ``/var/run/docker.sock``,
    turning every Postgres test into an ERROR instead of the graceful SKIP
    this module promises. Pinging the daemon distinguishes "no Docker here"
    (skip) from a real test failure (don't mask it).
    """
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    if not _HAVE_TESTCONTAINERS:
        pytest.skip("testcontainers not installed")
    if not _docker_daemon_reachable():
        pytest.skip("docker daemon not reachable (e.g. in-container, no socket)")
    with PostgresContainer("postgres:16-alpine") as pg:
        # testcontainers default URL uses the psycopg2 driver scheme;
        # normalize to a plain libpq DSN psycopg v3 accepts.
        url = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(_SCHEMA.read_text(encoding="utf-8"))
        yield url


@pytest.fixture()
def clean_postgres_dsn(postgres_dsn: str) -> Iterator[str]:
    with psycopg.connect(postgres_dsn, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE tickets, state_instances, events, merge_queue RESTART IDENTITY CASCADE"
        )
    yield postgres_dsn
