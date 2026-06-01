"""Tests for the SQLite lifecycle storage layer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from foreman.storage import Storage


def test_init_creates_schema_on_fresh_db(tmp_path: Path) -> None:
    db_path = tmp_path / "foreman.sqlite"
    storage = Storage(db_path)
    storage.init()

    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}

    assert "meta" in tables
    assert "pipelines" in tables
    assert "node_runs" in tables
    assert "transitions" in tables
    assert "failures" in tables
    assert "labels_seen" in tables


def test_init_records_schema_version(tmp_path: Path) -> None:
    db_path = tmp_path / "foreman.sqlite"
    storage = Storage(db_path)
    storage.init()

    with sqlite3.connect(db_path) as conn:
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]

    assert version == "1"


def test_init_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "foreman.sqlite"
    storage = Storage(db_path)
    storage.init()
    storage.init()  # second call should not raise

    with sqlite3.connect(db_path) as conn:
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]

    assert version == "1"
