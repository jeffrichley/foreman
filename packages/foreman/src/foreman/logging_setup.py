"""JSON-lines structured logging for the daemon.

One record per line, schema:
{"timestamp": "...", "level": "INFO", "logger": "...", "message": "...", **extra}

Use ``logger.info(msg, extra={...})`` to add structured fields. The
``extra`` dict's keys are merged into the JSON record at the top level.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# foreman#131: mark handlers we install so re-entry can remove ours
# without stripping third-party handlers (e.g., pytest's caplog
# LogCaptureHandler). Without this, calling ``configure_daemon_logging``
# from a test breaks caplog capture for every downstream test in the
# same pytest session.
_FOREMAN_OWNED_HANDLER = "_foreman_owned_handler"

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


class _JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_FIELDS or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            exc_type, exc_value, _exc_tb = record.exc_info
            payload["exception"] = {
                "type": exc_type.__name__ if exc_type is not None else None,
                "message": str(exc_value) if exc_value is not None else None,
                "traceback": self.formatException(record.exc_info),
            }
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str)


def configure_daemon_logging(
    *,
    log_path: Path | str,
    level: str,
    console: bool = True,
) -> None:
    """Configure the 'foreman' logger.

    Two handlers attached:
    - **FileHandler** writing JSON lines to ``log_path`` — machine-readable
      audit log, includes every ``extra={...}`` field at the top level.
    - **StreamHandler** writing JSON lines to ``sys.stdout`` —
      machine-readable mirror of the file payload so
      ``docker logs <container>`` and any log-aggregator that consumes
      container stdout see the same records, one per line. Suppressed
      when ``console=False`` (e.g., in tests that import this function
      but don't want terminal output) — FileHandler only.

    foreman#131 guarantee: re-calling this function replaces only the
    handlers it installed itself — third-party handlers (e.g., pytest's
    caplog ``LogCaptureHandler``) are preserved. Handlers we install
    carry the ``_FOREMAN_OWNED_HANDLER`` attribute; the cleanup filter
    only removes those, leaving everything else attached.
    """
    log_path = Path(log_path).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    foreman_logger = logging.getLogger("foreman")
    # foreman#131: only strip handlers WE installed. Preserves
    # third-party handlers across re-entry.
    foreman_logger.handlers[:] = [
        h
        for h in foreman_logger.handlers
        if not getattr(h, _FOREMAN_OWNED_HANDLER, False)
    ]

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(_JsonLinesFormatter())
    setattr(file_handler, _FOREMAN_OWNED_HANDLER, True)
    foreman_logger.addHandler(file_handler)

    if console:
        # foreman#138 (PR #207): mirror to stdout as JSON so
        # ``docker logs <container>`` sees the same payload as the file.
        stream_handler = logging.StreamHandler(stream=sys.stdout)
        stream_handler.setFormatter(_JsonLinesFormatter())
        setattr(stream_handler, _FOREMAN_OWNED_HANDLER, True)
        foreman_logger.addHandler(stream_handler)

    foreman_logger.setLevel(level.upper())
    foreman_logger.propagate = False
