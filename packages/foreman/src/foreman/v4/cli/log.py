"""log — recent + filtered JSON-lines view of foreman.v4.transitions.

Polls the file for --tail; no platform-specific watcher magic. The N most
recent lines are shown by default; --ticket / --state filter inline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer


def _default_log_path() -> Path:
    """Resolve the default log path at call-time (avoids B008)."""
    return Path.home() / ".foreman/v4/transitions.jsonl"


def cmd_log(
    ctx: typer.Context,
    log_path: str | None = typer.Option(
        None,
        "--log-path",
        help="Path to the JSON-lines transition log",
    ),
    limit: int = typer.Option(50, "--limit"),
    ticket: int | None = typer.Option(None, "--ticket"),
    state: str | None = typer.Option(None, "--state"),
    tail: bool = typer.Option(False, "--tail", help="Follow the log (rich.Live)"),
) -> None:
    """Print the transitions log as JSON lines, optionally filtered and/or followed.

    Reads ``--limit`` most recent rows from the JSON-lines transition
    log (default ``~/.foreman/v4/transitions.jsonl``), filtering by
    ``--ticket`` / ``--state`` when given. With ``--tail``, hands off to
    a polling ``rich.Live`` follower instead of printing once and
    returning.
    """
    resolved = Path(log_path) if log_path is not None else _default_log_path()
    if tail:
        _tail(resolved, ticket=ticket, state=state)
        return
    rows = _read_last(resolved, limit, ticket=ticket, state=state)
    for row in rows:
        typer.echo(json.dumps(row))


def _read_last(
    path: Path,
    limit: int,
    *,
    ticket: int | None,
    state: str | None,
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    matched: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if ticket is not None and row.get("ticket_id") != ticket:
                continue
            if state is not None and row.get("state") != state:
                continue
            matched.append(row)
    return matched[-limit:]


def _tail(
    path: Path,
    *,
    ticket: int | None,
    state: str | None,
) -> None:
    """Polling-based follow. Cheap on small logs; not optimized for high-volume."""
    import time

    from rich.console import Console
    from rich.live import Live
    from rich.text import Text

    console = Console()
    seen_size = 0
    with Live(Text(""), console=console, refresh_per_second=4) as live:
        try:
            while True:
                if path.exists():
                    current_size = path.stat().st_size
                    if current_size != seen_size:
                        new_rows = _read_last(
                            path,
                            limit=20,
                            ticket=ticket,
                            state=state,
                        )
                        text = Text("\n".join(json.dumps(r) for r in new_rows))
                        live.update(text)
                        seen_size = current_size
                time.sleep(0.5)
        except KeyboardInterrupt:
            return
