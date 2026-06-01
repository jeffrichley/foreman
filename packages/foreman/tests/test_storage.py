"""Tests for the SQLite lifecycle storage layer."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
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


def test_upsert_pipeline_creates_then_updates(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    pipeline_id = storage.upsert_pipeline(
        project="voice", issue_number=42, current_state="foreman:plan", started_at=now
    )
    assert pipeline_id > 0

    same_id = storage.upsert_pipeline(
        project="voice", issue_number=42, current_state="foreman:spec-review", started_at=now
    )
    assert same_id == pipeline_id

    row = storage.get_pipeline("voice", 42)
    assert row is not None
    assert row["current_state"] == "foreman:spec-review"


def test_get_pipeline_returns_none_when_absent(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    assert storage.get_pipeline("voice", 999) is None


def test_labels_seen_upsert_and_read(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    storage.upsert_labels_seen("voice", 42, ["foreman:plan"], now)
    assert storage.get_labels_seen("voice", 42) == ["foreman:plan"]

    storage.upsert_labels_seen("voice", 42, ["foreman:spec-review"], now)
    assert storage.get_labels_seen("voice", 42) == ["foreman:spec-review"]


def test_labels_seen_returns_none_when_absent(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    assert storage.get_labels_seen("voice", 42) is None


def test_iter_pipelines_in_flight_yields_non_terminal(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    storage.upsert_pipeline("voice", 1, "foreman:planning", now)
    storage.upsert_pipeline("voice", 2, "foreman:plan", now)
    storage.mark_pipeline_terminated("voice", 2, now)

    in_flight = list(storage.iter_pipelines_in_flight())
    states = [(row["project"], row["issue_number"]) for row in in_flight]
    assert ("voice", 1) in states
    assert ("voice", 2) not in states


def test_record_node_run_persists_role_and_outcome(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    pipeline_id = storage.upsert_pipeline("voice", 42, "foreman:plan", now)

    run_id = storage.record_node_run_start(
        pipeline_id=pipeline_id, role="planner", identity="foreman-planner-bot", at=now
    )
    later = datetime(2026, 6, 1, 12, 5, 0, tzinfo=timezone.utc)
    storage.record_node_run_finish(
        run_id=run_id,
        at=later,
        outcome="success",
        structured_output={"pr_number": 18},
    )

    with storage.connect() as conn:
        row = conn.execute("SELECT * FROM node_runs WHERE id = ?", (run_id,)).fetchone()
    assert row["role"] == "planner"
    assert row["outcome"] == "success"
    assert json.loads(row["structured_output_json"]) == {"pr_number": 18}


def test_record_transition_persists_label_diff(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    pipeline_id = storage.upsert_pipeline("voice", 42, "foreman:plan", now)

    storage.record_transition(
        pipeline_id=pipeline_id,
        at=now,
        from_labels=["foreman:plan"],
        to_labels=["foreman:spec-review"],
        actor="planner",
    )

    with storage.connect() as conn:
        row = conn.execute(
            "SELECT * FROM transitions WHERE pipeline_id = ?", (pipeline_id,)
        ).fetchone()
    assert row["actor"] == "planner"
    assert json.loads(row["from_labels_json"]) == ["foreman:plan"]
    assert json.loads(row["to_labels_json"]) == ["foreman:spec-review"]


def test_record_failure_persists_reason_and_traceback(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    pipeline_id = storage.upsert_pipeline("voice", 42, "foreman:plan", now)

    storage.record_failure(
        pipeline_id=pipeline_id,
        at=now,
        role="planner",
        reason="RuntimeError: missing token",
        traceback="Traceback (most recent call last):\n  ...\n",
    )

    with storage.connect() as conn:
        row = conn.execute(
            "SELECT * FROM failures WHERE pipeline_id = ?", (pipeline_id,)
        ).fetchone()
    assert row["reason"].startswith("RuntimeError")
    assert "Traceback" in row["traceback"]
