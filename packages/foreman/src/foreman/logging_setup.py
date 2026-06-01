"""JSON-lines structured logging for the daemon.

One record per line, schema:
{"timestamp": "...", "level": "INFO", "logger": "...", "message": "...", **extra}

Use ``logger.info(msg, extra={...})`` to add structured fields. The
``extra`` dict's keys are merged into the JSON record at the top level.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
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
        return json.dumps(payload, default=str)


def configure_daemon_logging(*, log_path: Path | str, level: str) -> None:
    """Configure the 'foreman' logger to emit JSON lines to ``log_path``.

    Idempotent — re-calling replaces existing handlers on the 'foreman'
    logger.
    """
    log_path = Path(log_path).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    foreman_logger = logging.getLogger("foreman")
    foreman_logger.handlers.clear()

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(_JsonLinesFormatter())
    foreman_logger.addHandler(handler)
    foreman_logger.setLevel(level.upper())
    foreman_logger.propagate = False
