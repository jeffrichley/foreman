"""Tests for the daemon's JSON-lines logging setup."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from foreman.logging_setup import configure_daemon_logging


def test_configure_daemon_logging_writes_json_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "daemon.log"
    configure_daemon_logging(log_path=log_path, level="INFO", console=False)

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
    configure_daemon_logging(log_path=log_path, level="WARNING", console=False)

    logger = logging.getLogger("foreman.daemon.test_level")
    logger.info("info message")
    logger.warning("warning message")

    for handler in logging.getLogger("foreman").handlers:
        handler.flush()

    lines = [json.loads(line) for line in log_path.read_text().strip().splitlines()]
    messages = [r["message"] for r in lines]
    assert "info message" not in messages
    assert "warning message" in messages


def test_configure_daemon_logging_renders_exc_info(tmp_path: Path) -> None:
    log_path = tmp_path / "daemon.log"
    configure_daemon_logging(log_path=log_path, level="INFO", console=False)

    logger = logging.getLogger("foreman.daemon.test_exc")
    try:
        raise ValueError("bad creds")
    except ValueError:
        logger.exception("poll_project failed", extra={"project": "foreman"})

    for handler in logging.getLogger("foreman").handlers:
        handler.flush()

    line = log_path.read_text().strip().splitlines()[-1]
    record = json.loads(line)
    assert record["message"] == "poll_project failed"
    assert record["level"] == "ERROR"
    assert record["project"] == "foreman"
    assert record["exception"]["type"] == "ValueError"
    assert record["exception"]["message"] == "bad creds"
    assert "Traceback" in record["exception"]["traceback"]
    assert "ValueError: bad creds" in record["exception"]["traceback"]


def test_configure_daemon_logging_renders_stack_info(tmp_path: Path) -> None:
    log_path = tmp_path / "daemon.log"
    configure_daemon_logging(log_path=log_path, level="INFO", console=False)

    logger = logging.getLogger("foreman.daemon.test_stack")
    logger.info("hello", stack_info=True)

    for handler in logging.getLogger("foreman").handlers:
        handler.flush()

    line = log_path.read_text().strip().splitlines()[-1]
    record = json.loads(line)
    assert isinstance(record["stack_info"], str)
    assert record["stack_info"] != ""
    assert "Stack (most recent call last)" in record["stack_info"]


def test_configure_daemon_logging_omits_exception_key_when_absent(tmp_path: Path) -> None:
    log_path = tmp_path / "daemon.log"
    configure_daemon_logging(log_path=log_path, level="INFO", console=False)

    logger = logging.getLogger("foreman.daemon.test_plain")
    logger.info("hello", extra={"ticket": 1})

    for handler in logging.getLogger("foreman").handlers:
        handler.flush()

    line = log_path.read_text().strip().splitlines()[-1]
    record = json.loads(line)
    assert "exception" not in record
    assert "stack_info" not in record
