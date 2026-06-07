"""Tests for the daemon's JSON-lines logging setup."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

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


def test_configure_daemon_logging_preserves_third_party_handlers(tmp_path: Path) -> None:
    """foreman#131: a CLI invocation that calls configure_daemon_logging
    must not strip handlers it doesn't own (e.g., pytest's
    LogCaptureHandler installed for ``caplog`` capture). Without this
    guarantee, any test that runs the daemon CLI breaks caplog capture
    for every downstream test in the same pytest session.
    """
    foreman_logger = logging.getLogger("foreman")
    # Simulate a third-party handler that was installed before
    # configure_daemon_logging fires (e.g., pytest's LogCaptureHandler
    # attaches to the ``foreman`` logger via propagation).
    third_party = logging.NullHandler()
    third_party.name = "third-party-sentinel"
    foreman_logger.addHandler(third_party)

    try:
        configure_daemon_logging(log_path=tmp_path / "daemon.log", level="INFO", console=False)
        names = [h.name for h in foreman_logger.handlers]
        assert "third-party-sentinel" in names, (
            "configure_daemon_logging stripped a third-party handler; "
            "must only remove handlers it installed itself"
        )
        # And our handler is still there.
        assert any(
            isinstance(h, logging.FileHandler) for h in foreman_logger.handlers
        ), "configure_daemon_logging did not install its file handler"
    finally:
        # Restore the foreman logger to a clean state so this test
        # doesn't pollute siblings.
        foreman_logger.handlers.clear()


def test_configure_daemon_logging_idempotent_does_not_accumulate_own_handlers(
    tmp_path: Path,
) -> None:
    """Re-calling configure_daemon_logging must replace its own handlers,
    not accumulate them. (Regression guard for the foreman#131 fix: the
    new ours-only filter must still strip ours on re-entry.)
    """
    foreman_logger = logging.getLogger("foreman")
    foreman_logger.handlers.clear()
    try:
        configure_daemon_logging(log_path=tmp_path / "daemon.log", level="INFO", console=False)
        configure_daemon_logging(log_path=tmp_path / "daemon.log", level="INFO", console=False)
        file_handlers = [h for h in foreman_logger.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) == 1, (
            f"expected exactly 1 FileHandler after re-entry, got {len(file_handlers)}"
        )
    finally:
        foreman_logger.handlers.clear()


# foreman#138 (rescue from PR #207): mirror daemon log to stdout as JSON
# for docker logs. These two tests guard the stdout-mirror behavior that
# `if console:` enables when configure_daemon_logging is called with
# console=True. The orphaned PR #207 was the original carrier of these.
def test_configure_daemon_logging_mirrors_to_stdout_as_json_when_console_true(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_path = tmp_path / "daemon.log"
    configure_daemon_logging(log_path=log_path, level="INFO", console=True)

    logger = logging.getLogger("foreman.daemon.test_stdout_mirror")
    logger.info("stdout-mirror", extra={"ticket": 138, "project": "foreman"})

    for handler in logging.getLogger("foreman").handlers:
        handler.flush()

    captured_out = capsys.readouterr().out
    line = captured_out.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["message"] == "stdout-mirror"
    assert record["level"] == "INFO"
    assert record["ticket"] == 138
    assert record["project"] == "foreman"
    assert "timestamp" in record


def test_configure_daemon_logging_disk_and_stdout_payloads_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_path = tmp_path / "daemon.log"
    configure_daemon_logging(log_path=log_path, level="INFO", console=True)

    logger = logging.getLogger("foreman.daemon.test_payload_match")
    logger.info("payload-match", extra={"ticket": 138, "phase": "dual-emit"})

    for handler in logging.getLogger("foreman").handlers:
        handler.flush()

    disk_line = log_path.read_text().strip().splitlines()[-1]
    stdout_line = capsys.readouterr().out.strip().splitlines()[-1]
    disk_record = json.loads(disk_line)
    stdout_record = json.loads(stdout_line)
    assert disk_record == stdout_record
