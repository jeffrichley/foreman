"""Strategy pattern for CLI output formatting.

Each formatter consumes a list[dict] and returns a string. Concrete
strategies pluck different shapes from the same input. CLI's --format
flag picks the strategy at the top of each command.
"""

from __future__ import annotations

import io
import json
from typing import Any, Protocol

import yaml
from rich.console import Console
from rich.table import Table


class OutputFormatter(Protocol):
    """Structural type for a CLI output strategy: rows in, rendered text out."""

    def format(self, rows: list[dict[str, Any]]) -> str:
        """Render ``rows`` (already-shaped record dicts) as a display string."""
        ...


class JsonFormatter:
    """Renders rows as indented JSON, coercing non-serializable values with ``str``."""

    def format(self, rows: list[dict[str, Any]]) -> str:
        """Serialize ``rows`` to a pretty-printed JSON array."""
        return json.dumps(rows, default=str, indent=2)


class YamlFormatter:
    """Renders rows as a YAML document, preserving column order."""

    def format(self, rows: list[dict[str, Any]]) -> str:
        """Dump ``rows`` as YAML with keys kept in their original order."""
        return yaml.safe_dump(rows, sort_keys=False, default_flow_style=False)


class TableFormatter:
    """Renders rows as a Rich-drawn ASCII table for terminal display."""

    def format(self, rows: list[dict[str, Any]]) -> str:
        """Render ``rows`` as a bordered table, using the first row's keys as columns."""
        if not rows:
            return "(no rows)\n"
        buffer = io.StringIO()
        console = Console(file=buffer, force_terminal=True, width=120)
        table = Table(show_header=True, header_style="bold")
        for column in rows[0].keys():
            table.add_column(column)
        for row in rows:
            table.add_row(*(str(row.get(col, "")) for col in rows[0].keys()))
        console.print(table)
        return buffer.getvalue()


_FORMATTERS: dict[str, type[OutputFormatter]] = {
    "table": TableFormatter,
    "json": JsonFormatter,
    "yaml": YamlFormatter,
}


def get_formatter(name: str) -> OutputFormatter:
    """Look up the output-formatter strategy registered under ``name``.

    Raises:
        ValueError: if ``name`` isn't one of the registered ``--format``
            choices (table/json/yaml), so the CLI can surface a clean
            error instead of a raw ``KeyError``.
    """
    try:
        return _FORMATTERS[name]()
    except KeyError as exc:
        raise ValueError(f"unknown format: {name}") from exc
