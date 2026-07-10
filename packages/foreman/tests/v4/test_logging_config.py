"""configure_logging — installs RichHandler + JsonLinesHandler."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from foreman.v4.json_lines_handler import JsonLinesHandler
from foreman.v4.logging_config import configure_logging, reset_logging


@pytest.fixture(autouse=True)
def _reset():
    yield
    reset_logging()


def test_installs_both_handlers_on_foreman_v4_logger(tmp_path: Path):
    configure_logging(log_dir=tmp_path, level="INFO")
    log = logging.getLogger("foreman.v4.transitions")
    handler_types = {type(h).__name__ for h in log.handlers}
    assert "RichHandler" in handler_types
    assert "JsonLinesHandler" in handler_types


def test_jsonl_file_receives_writes(tmp_path: Path):
    configure_logging(log_dir=tmp_path, level="INFO")
    log = logging.getLogger("foreman.v4.transitions")
    log.info(json.dumps({"event": "test", "ticket_id": 42}))
    for handler in log.handlers:
        if isinstance(handler, JsonLinesHandler):
            handler.flush()
    line = (tmp_path / "transitions.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "test"
    assert payload["ticket_id"] == 42


def test_idempotent_does_not_add_duplicate_handlers(tmp_path: Path):
    configure_logging(log_dir=tmp_path, level="INFO")
    configure_logging(log_dir=tmp_path, level="INFO")
    log = logging.getLogger("foreman.v4.transitions")
    handler_types = [type(h).__name__ for h in log.handlers]
    # Each handler type appears exactly once
    assert handler_types.count("RichHandler") == 1
    assert handler_types.count("JsonLinesHandler") == 1


def test_level_is_honored(tmp_path: Path):
    configure_logging(log_dir=tmp_path, level="WARNING")
    log = logging.getLogger("foreman.v4.transitions")
    log.info("info-level should be filtered")
    log.warning("warning-level should pass")
    for handler in log.handlers:
        if isinstance(handler, JsonLinesHandler):
            handler.flush()
    content = (tmp_path / "transitions.jsonl").read_text(encoding="utf-8")
    assert "info-level" not in content
    assert "warning-level" in content
