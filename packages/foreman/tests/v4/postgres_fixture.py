"""Session-scoped Postgres testcontainer + per-test reset.

The container starts once per pytest session (image pull + boot is
~5s). Each test gets an empty schema via TRUNCATE ... RESTART IDENTITY
CASCADE in the per-test fixture, which is far cheaper than recreating
the container.

Skips the whole module if Docker is unavailable (e.g. a dev box with
no daemon), so the suite degrades gracefully instead of erroring.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

_SCHEMA = Path(__file__).parents[2] / "src" / "foreman" / "v4" / "postgres_schema.sql"

try:  # pragma: no cover - import guard
    from testcontainers.postgres import PostgresContainer

    _HAVE_DOCKER = True
except Exception:  # pragma: no cover
    _HAVE_DOCKER = False


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    if not _HAVE_DOCKER:
        pytest.skip("testcontainers/docker not available")
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
        conn.execute("TRUNCATE tickets, state_instances, events RESTART IDENTITY CASCADE")
    yield postgres_dsn
