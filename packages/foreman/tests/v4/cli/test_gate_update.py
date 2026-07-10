"""gate-update — watchtower pre-update lifecycle hook.

Three exit paths:
  - busy (≥1 open ticket): exit 75 (EX_TEMPFAIL)
  - idle (0 open tickets): exit 0
  - error (repo raises):   exit 0 + WARNING to stderr
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from typer.testing import CliRunner

from foreman.v4.cli import app
from foreman.v4.cli.context import build_cli_context
from foreman.v4.records import TicketRecord
from foreman.v4.repository import InMemoryTicketRepository


class _RaisingRepo:
    """Minimal fake that raises on list_open_tickets()."""

    def list_open_tickets(self) -> list[TicketRecord]:
        raise RuntimeError("DB is down")

    # TicketRepository is a Protocol — we only need to satisfy the call
    # sites used by cmd_gate_update, which is just list_open_tickets().
    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)


def _busy_repo() -> InMemoryTicketRepository:
    repo = InMemoryTicketRepository()
    now = dt.datetime(2026, 6, 26, 12, 0, 0)
    repo.create_ticket(project="p", issue_number=1, now=now)
    # Ticket starts in "Queued" (non-terminal) — board is busy
    return repo


def _idle_repo() -> InMemoryTicketRepository:
    repo = InMemoryTicketRepository()
    now = dt.datetime(2026, 6, 26, 12, 0, 0)
    t = repo.create_ticket(project="p", issue_number=1, now=now)
    repo.set_ticket_state(t.id, "Done", now=now)
    return repo


def test_gate_update_busy_exits_75() -> None:
    """When the board has ≥1 open ticket, gate-update must exit 75 (EX_TEMPFAIL)."""
    runner = CliRunner()
    result = runner.invoke(app, ["gate-update"], obj=build_cli_context(repo=_busy_repo()))
    assert result.exit_code == 75


def test_gate_update_idle_exits_0() -> None:
    """When the board is empty (all tickets terminal), gate-update must exit 0."""
    runner = CliRunner()
    result = runner.invoke(app, ["gate-update"], obj=build_cli_context(repo=_idle_repo()))
    assert result.exit_code == 0


def test_gate_update_error_exits_0_and_warns() -> None:
    """When list_open_tickets() raises, gate-update must exit 0 (fail-open) and
    emit a line containing 'WARNING' to stderr."""
    # CliRunner (typer.testing) mixes stderr into output by default.
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["gate-update"],
        obj=build_cli_context(repo=_RaisingRepo()),  # type: ignore[arg-type]
    )
    assert result.exit_code == 0
    assert "WARNING" in result.output
