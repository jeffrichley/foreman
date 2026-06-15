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
    def format(self, rows: list[dict[str, Any]]) -> str: ...


class JsonFormatter:
    def format(self, rows: list[dict[str, Any]]) -> str:
        return json.dumps(rows, default=str, indent=2)


class YamlFormatter:
    def format(self, rows: list[dict[str, Any]]) -> str:
        return yaml.safe_dump(rows, sort_keys=False, default_flow_style=False)


class TableFormatter:
    def format(self, rows: list[dict[str, Any]]) -> str:
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
    try:
        return _FORMATTERS[name]()
    except KeyError as exc:
        raise ValueError(f"unknown format: {name}") from exc
