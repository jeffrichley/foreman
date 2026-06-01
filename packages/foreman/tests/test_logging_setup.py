"""Tests for the daemon's JSON-lines logging setup."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from foreman.logging_setup import configure_daemon_logging


def test_configure_daemon_logging_writes_json_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "daemon.log"
    configure_daemon_logging(log_path=log_path, level="INFO")

    logger = logging.getLogger("foreman.daemon.test")
    logger.info("hello", extra={"ticket": 42, "project": "voice"})

    for handler in logging.getLogger("foreman").handlers:
        handler.flush()

    line = log_path.read_text().strip().splitlines()[-1]
    record = json.loads(line)
    assert record["message"] == "hello"
    assert record["level"] == "INFO"
    assert record["ticket"] == 42
    assert record["project"] == "voice"
    assert "timestamp" in record


def test_configure_daemon_logging_respects_level(tmp_path: Path) -> None:
    log_path = tmp_path / "daemon.log"
    configure_daemon_logging(log_path=log_path, level="WARNING")

    logger = logging.getLogger("foreman.daemon.test_level")
    logger.info("info message")
    logger.warning("warning message")

    for handler in logging.getLogger("foreman").handlers:
        handler.flush()

    lines = [json.loads(line) for line in log_path.read_text().strip().splitlines()]
    messages = [r["message"] for r in lines]
    assert "info message" not in messages
    assert "warning message" in messages
