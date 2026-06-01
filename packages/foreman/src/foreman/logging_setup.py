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
    - **RichHandler** writing pretty colored output to stderr — for humans
      watching the foreground daemon. Suppressed when ``console=False``
      (e.g., in tests that import this function but don't want terminal
      output).

    Idempotent — re-calling replaces all existing handlers on the
    'foreman' logger.
    """
    log_path = Path(log_path).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    foreman_logger = logging.getLogger("foreman")
    foreman_logger.handlers.clear()

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(_JsonLinesFormatter())
    foreman_logger.addHandler(file_handler)

    if console:
        # Imported lazily so tests that don't exercise this branch don't
        # need rich installed at import time.
        from rich.console import Console
        from rich.logging import RichHandler

        rich_handler = RichHandler(
            console=Console(stderr=True),
            show_time=True,
            show_path=False,
            rich_tracebacks=True,
            markup=False,
        )
        rich_handler.setFormatter(logging.Formatter("%(message)s"))
        foreman_logger.addHandler(rich_handler)

    foreman_logger.setLevel(level.upper())
    foreman_logger.propagate = False
