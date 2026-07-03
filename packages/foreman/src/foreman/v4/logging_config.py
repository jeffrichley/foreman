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
    """Attach a RichHandler + JsonLinesHandler pair to each v4 logger.

    Creates ``log_dir`` if it doesn't exist and points the JSON sink at
    ``log_dir/transitions.jsonl``. Idempotent: checks each logger's
    existing handler types before adding new ones, so repeated calls
    (e.g. after a daemon SIGHUP reload) never stack duplicate handlers.
    """
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
    """Tear down all v4 logger handlers.

    Used by the daemon's SIGHUP handler to drop existing
    Rich/JsonLines handlers before reconfigure, preventing
    handler-stacking on repeated reloads. Also used by tests
    that want a fresh logging surface.
    """
    for name in _V4_LOGGER_NAMES:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        # foreman#336: ``configure_logging`` sets ``propagate = False``
        # to keep v4 records out of the root logger's stream. Restore
        # the default so a follow-up ``configure_logging`` re-applies
        # the flag deliberately, and so test suites that share the
        # logger registry (caplog, pytest-randomly orderings) see a
        # genuine clean slate. Removing handlers without restoring
        # propagate leaves the logger silently muted from the root.
        logger.propagate = True
