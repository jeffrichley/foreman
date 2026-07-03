"""JsonLinesHandler — file sink that emits one JSON object per record.

Pair-with-RichHandler design: RichHandler renders human-readable colored
output to stdout; this handler persists structured records to disk for
grep, replay, and ad-hoc analysis. The transitions log written by
StructuredLogObserver (Phase 2) is the primary consumer.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

_STANDARD_RECORD_FIELDS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "message",
    "module",
    "msecs",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}

_FORMATTER = logging.Formatter()


class JsonLinesHandler(logging.Handler):
    """Logging handler that appends one JSON object per record to a file.

    Opens ``filename`` in append mode at construction and keeps the file
    handle open for the handler's lifetime. Pairs with a human-readable
    RichHandler on the same logger (see
    :func:`foreman.v4.logging_config.configure_logging`) so one log
    stream is simultaneously readable on a terminal and greppable/parsable
    on disk.
    """

    def __init__(self, *, filename: str, encoding: str = "utf-8") -> None:
        super().__init__()
        self._stream = open(filename, "a", encoding=encoding)

    def emit(self, record: logging.LogRecord) -> None:
        """Serialize ``record`` to one JSON line and append it to the file.

        Merges a JSON-encoded message (as produced by
        StructuredLogObserver) into the top-level payload rather than
        nesting it, folds in any non-standard extra record attributes,
        and attaches exception/stack info when present. Never raises back
        into the application: failures are routed through
        ``self.handleError`` per the stdlib handler contract.
        """
        try:
            payload: dict[str, Any] = {
                "ts": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
            }
            # If the log message is itself JSON (StructuredLogObserver),
            # merge its keys at top level so consumers don't dig into a
            # nested object.
            message = record.getMessage()
            try:
                parsed = json.loads(message)
                if isinstance(parsed, dict):
                    payload.update(parsed)
                else:
                    payload["message"] = message
            except (json.JSONDecodeError, TypeError):
                payload["message"] = message
            for key, value in record.__dict__.items():
                if key in _STANDARD_RECORD_FIELDS or key.startswith("_"):
                    continue
                payload[key] = value
            if record.exc_info:
                exc_type, exc_value, _exc_tb = record.exc_info
                payload["exception"] = {
                    "type": exc_type.__name__ if exc_type is not None else None,
                    "message": str(exc_value) if exc_value is not None else None,
                    "traceback": _FORMATTER.formatException(record.exc_info),
                }
            if record.stack_info:
                payload["stack_info"] = _FORMATTER.formatStack(record.stack_info)
            self._stream.write(json.dumps(payload, default=str) + "\n")
            self._stream.flush()
        except Exception:
            # Mirror stdlib logging idiom: handlers must never raise back
            # into the application. self.handleError(record) writes to
            # stderr by default.
            self.handleError(record)

    def close(self) -> None:
        """Close the underlying file stream, then chain to the base handler."""
        try:
            self._stream.close()
        finally:
            super().close()
