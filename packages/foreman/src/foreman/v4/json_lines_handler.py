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


class JsonLinesHandler(logging.Handler):
    def __init__(self, *, filename: str, encoding: str = "utf-8") -> None:
        super().__init__()
        self._stream = open(filename, "a", encoding=encoding)

    def emit(self, record: logging.LogRecord) -> None:
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
            self._stream.write(json.dumps(payload, default=str) + "\n")
            self._stream.flush()
        except Exception:
            # Mirror stdlib logging idiom: handlers must never raise back
            # into the application. self.handleError(record) writes to
            # stderr by default.
            self.handleError(record)

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            super().close()
