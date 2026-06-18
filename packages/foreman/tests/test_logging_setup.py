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


# foreman#323: harden the JSON-lines writer against the "silent stop"
# failure class described in the issue. The literal v4 source pointers
# do not exist in this repo (see the spec's Open questions); these
# three tests lock in the equivalent behavior of the stdlib
# FileHandler + _JsonLinesFormatter pipeline in logging_setup.py.
def test_logger_info_writes_to_disk_without_explicit_flush(tmp_path: Path) -> None:
    """foreman#323 regression guard: a single ``logger.info(...)`` call
    must be visible on disk WITHOUT the caller calling
    ``handler.flush()`` first. ``logging.StreamHandler.emit()`` (which
    ``FileHandler`` inherits) flushes after every write; this test
    pins that behavior so a future refactor that swaps in a buffered
    handler cannot silently regress the operator's ``tail -F`` UX.
    """
    foreman_logger = logging.getLogger("foreman")
    foreman_logger.handlers.clear()
    try:
        log_path = tmp_path / "daemon.log"
        configure_daemon_logging(log_path=log_path, level="INFO", console=False)

        logger = logging.getLogger("foreman.daemon.test_no_flush")
        logger.info("no-flush", extra={"ticket": 323})

        # DELIBERATELY do NOT call handler.flush(). The whole point of
        # this regression guard is that per-record flush already
        # happens inside FileHandler.emit().
        contents = log_path.read_text()
        assert contents != "", (
            "logger.info did not produce any disk write before an explicit "
            "flush — FileHandler.emit() is no longer flushing per-record"
        )
        line = contents.strip().splitlines()[-1]
        record = json.loads(line)
        assert record["message"] == "no-flush"
        assert record["ticket"] == 323
    finally:
        foreman_logger.handlers.clear()


def test_non_json_extra_does_not_disable_handler(tmp_path: Path) -> None:
    """foreman#323 regression guard: a non-JSON-serializable value
    passed via ``extra={...}`` must not silently disable the foreman
    logger's file handler. The offending record may be dropped or
    rendered via ``_JsonLinesFormatter``'s ``default=str`` fallback;
    the load-bearing assertion is that the NEXT record after the bad
    one still lands on disk and the handler is still attached.

    This is the in-codebase analog of the issue's path (b) — "an
    exception inside emit() triggered handleError() which silently
    swallowed and disabled the handler".
    """
    foreman_logger = logging.getLogger("foreman")
    foreman_logger.handlers.clear()
    try:
        log_path = tmp_path / "daemon.log"
        configure_daemon_logging(log_path=log_path, level="INFO", console=False)

        logger = logging.getLogger("foreman.daemon.test_non_json")
        # A set of bytes — neither set nor bytes is JSON-native. The
        # formatter's ``default=str`` should render this without
        # raising; even if it did raise, the handler must survive.
        logger.info("first", extra={"obj": {b"a", b"b"}})
        logger.info("second", extra={"ticket": 1})

        for handler in logging.getLogger("foreman").handlers:
            handler.flush()

        # Load-bearing: the file handler is still attached after the
        # exotic record.
        file_handlers = [
            h for h in foreman_logger.handlers if isinstance(h, logging.FileHandler)
        ]
        assert len(file_handlers) == 1, (
            f"non-JSON extra disabled or detached the file handler; "
            f"found {len(file_handlers)} FileHandler(s) on the foreman logger"
        )

        # Load-bearing: the NEXT record after the exotic one lands on
        # disk. (Whether the exotic record itself made it is not
        # asserted — default=str makes it likely but not contractual.)
        lines = log_path.read_text().strip().splitlines()
        last = json.loads(lines[-1])
        assert last["message"] == "second", (
            f"record after non-JSON extra did not land on disk; "
            f"last record was: {last!r}"
        )
        assert last["ticket"] == 1
    finally:
        foreman_logger.handlers.clear()


def test_re_entry_does_not_strand_writes(tmp_path: Path) -> None:
    """foreman#323 regression guard: re-entering
    ``configure_daemon_logging`` with the same ``log_path`` must not
    strand the file descriptor for prior writes. Records emitted
    before AND after re-entry must both appear in the log file.

    This is the in-codebase analog of the issue's SIGHUP-reset
    scenario. (This repo has no SIGHUP handler — see ``cli.py:1080``
    and ``docs/superpowers/specs/foreman-issue-100-spec.md`` — so
    re-entry of ``configure_daemon_logging`` is the closest analog.)
    """
    foreman_logger = logging.getLogger("foreman")
    foreman_logger.handlers.clear()
    try:
        log_path = tmp_path / "daemon.log"

        configure_daemon_logging(log_path=log_path, level="INFO", console=False)
        logger = logging.getLogger("foreman.daemon.test_reentry")
        logger.info("before-reentry", extra={"ticket": 323})

        # Re-entry: same log_path. Must replace the old handler
        # without losing the prior write or stranding the FD.
        configure_daemon_logging(log_path=log_path, level="INFO", console=False)
        logger.info("after-reentry", extra={"ticket": 323})

        for handler in logging.getLogger("foreman").handlers:
            handler.flush()

        records = [
            json.loads(line)
            for line in log_path.read_text().strip().splitlines()
        ]
        messages = [r["message"] for r in records]
        assert "before-reentry" in messages, (
            f"re-entry of configure_daemon_logging stranded the prior "
            f"FD; 'before-reentry' is missing. Got: {messages}"
        )
        assert "after-reentry" in messages, (
            f"writes after re-entry are not landing on disk. "
            f"Got: {messages}"
        )
    finally:
        foreman_logger.handlers.clear()
