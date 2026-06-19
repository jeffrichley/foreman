"""JsonLinesHandler — one JSON object per log record."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from foreman.v4.json_lines_handler import JsonLinesHandler


def test_emit_writes_one_json_per_record(tmp_path: Path):
    log_path = tmp_path / "transitions.jsonl"
    handler = JsonLinesHandler(filename=str(log_path))
    log = logging.getLogger("test_jsonl")
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    try:
        log.info(json.dumps({"event": "state_entered", "ticket_id": 1}))
        log.info(json.dumps({"event": "execute_completed", "ticket_id": 1}))
    finally:
        log.removeHandler(handler)
        handler.close()

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "state_entered"
    assert json.loads(lines[1])["event"] == "execute_completed"


def test_emit_wraps_non_json_messages_safely(tmp_path: Path):
    """If a logger emits a plain string, the handler still produces valid JSON."""
    log_path = tmp_path / "transitions.jsonl"
    handler = JsonLinesHandler(filename=str(log_path))
    log = logging.getLogger("test_jsonl_safe")
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    try:
        log.warning("plain string warning")
    finally:
        log.removeHandler(handler)
        handler.close()

    line = log_path.read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    assert payload["level"] == "WARNING"
    assert payload["message"] == "plain string warning"


def test_emit_includes_timestamp(tmp_path: Path):
    log_path = tmp_path / "transitions.jsonl"
    handler = JsonLinesHandler(filename=str(log_path))
    log = logging.getLogger("test_jsonl_ts")
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    try:
        log.info('{"event": "x"}')
    finally:
        log.removeHandler(handler)
        handler.close()
    payload = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert "ts" in payload
