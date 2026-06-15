"""configure_logging — one call at daemon startup wires both handlers.

RichHandler renders human-readable colored output to stderr; JsonLinesHandler
appends machine-readable records to <log_dir>/transitions.jsonl. The transitions
logger is the v4 default sink; StructuredLogObserver (Phase 2) writes to it.

Idempotent so callers can re-invoke after a daemon reload without doubling
up handlers.
"""

from __future__ import annotations

import logging
from pathlib import Path

from rich.logging import RichHandler

from foreman.v4.json_lines_handler import JsonLinesHandler

_V4_LOGGER_NAMES = (
    "foreman.v4",
    "foreman.v4.transitions",
    "foreman.v4.event_bus",
)


def configure_logging(*, log_dir: Path, level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = log_dir / "transitions.jsonl"

    for name in _V4_LOGGER_NAMES:
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = False

        existing_types = {type(h).__name__ for h in logger.handlers}
        if "RichHandler" not in existing_types:
            rich = RichHandler(
                show_time=True,
                show_level=True,
                show_path=False,
                rich_tracebacks=True,
            )
            rich.setLevel(level)
            logger.addHandler(rich)
        if "JsonLinesHandler" not in existing_types:
            jsonl = JsonLinesHandler(filename=str(jsonl_path))
            jsonl.setLevel(level)
            logger.addHandler(jsonl)


def reset_logging() -> None:
    """Tear down handlers — test helper, not for production use."""
    for name in _V4_LOGGER_NAMES:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
