"""gate-update — watchtower pre-update lifecycle hook.

Exit paths:
  - busy (≥1 in-flight role job):    exit 75 (EX_TEMPFAIL)
  - idle (no running job):           exit 0
  - error (repo raises):             exit 0 + WARNING to stderr

A NeedsHelp/Queued ticket is NOT busy — it's parked, not running (regression
lock below: a lingering NeedsHelp ticket must not defer deploys forever).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from typer.testing import CliRunner

from foreman.v4.cli import app
from foreman.v4.cli.context import build_cli_context
from foreman.v4.records import StateInstanceRecord
from foreman.v4.repository import InMemoryTicketRepository


class _RaisingRepo:
    """Minimal fake that raises on acquire_drain_lease() to simulate DB-unreachable.

    An explicit ``acquire_drain_lease`` method is required so that
    ``test_gate_update_error_exits_0_and_warns`` exercises "DB unreachable
    at lease acquisition" rather than "method not found via __getattr__".
    """

    def acquire_drain_lease(self, *, now: dt.datetime, ttl_seconds: int = 300) -> None:
        raise RuntimeError("DB is down")

    def list_in_flight_state_instances(self) -> list[StateInstanceRecord]:
        raise RuntimeError("DB is down")

    # TicketRepository is a Protocol — we only need to satisfy the call sites
    # cmd_gate_update uses.
    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)


def _busy_repo() -> InMemoryTicketRepository:
    """A repo with a role job actually in flight (an open state-instance)."""
    repo = InMemoryTicketRepository()
    now = dt.datetime(2026, 6, 26, 12, 0, 0)
    t = repo.create_ticket(project="p", issue_number=1, now=now)
    repo.set_ticket_state(t.id, "Implementing", now=now)
    repo.open_state_instance(ticket_id=t.id, state_name="Implementing", sequence=1, now=now)
    return repo


def _idle_repo() -> InMemoryTicketRepository:
    repo = InMemoryTicketRepository()
    now = dt.datetime(2026, 6, 26, 12, 0, 0)
    t = repo.create_ticket(project="p", issue_number=1, now=now)
    repo.set_ticket_state(t.id, "Done", now=now)
    return repo


def _parked_repo() -> InMemoryTicketRepository:
    """A repo whose only ticket is parked in NeedsHelp — NOT a running job."""
    repo = InMemoryTicketRepository()
    now = dt.datetime(2026, 6, 26, 12, 0, 0)
    t = repo.create_ticket(project="p", issue_number=1, now=now)
    repo.set_ticket_state(t.id, "NeedsHelp", now=now)
    return repo


def test_gate_update_busy_exits_75() -> None:
    """A role job in flight must defer the update (exit 75)."""
    runner = CliRunner()
    result = runner.invoke(app, ["gate-update"], obj=build_cli_context(repo=_busy_repo()))
    assert result.exit_code == 75


def test_gate_update_idle_exits_0() -> None:
    """No running job → allow the update (exit 0)."""
    runner = CliRunner()
    result = runner.invoke(app, ["gate-update"], obj=build_cli_context(repo=_idle_repo()))
    assert result.exit_code == 0


def test_gate_update_parked_needs_help_ticket_is_not_busy() -> None:
    """A parked NeedsHelp ticket must NOT defer the update.

    Regression lock: gate-update used to block on any non-terminal ticket, so a
    single lingering NeedsHelp ticket deferred every auto-deploy forever
    (silently disabling Watchtower). A NeedsHelp ticket is awaiting a human, not
    a running job a redeploy would kill.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["gate-update"], obj=build_cli_context(repo=_parked_repo()))
    assert result.exit_code == 0


def test_gate_update_error_exits_0_and_warns() -> None:
    """When acquire_drain_lease() raises (DB unreachable), gate-update must exit 0
    (fail-open) and emit a line containing 'WARNING' to stderr."""
    # CliRunner (typer.testing) mixes stderr into output by default.
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["gate-update"],
        obj=build_cli_context(repo=_RaisingRepo()),  # type: ignore[arg-type]
    )
    assert result.exit_code == 0
    assert "WARNING" in result.output


def test_gate_update_idle_leaves_lease_active() -> None:
    """With no in-flight work, gate-update exits 0 and leaves the drain lease set.

    The lease must remain active so no new state-instance can be opened in
    the window between gate-update returning and SIGTERM arriving.
    """
    now = dt.datetime(2026, 6, 26, 12, 0, 0)
    repo = _idle_repo()
    runner = CliRunner()
    result = runner.invoke(app, ["gate-update"], obj=build_cli_context(repo=repo))
    assert result.exit_code == 0
    assert repo.is_drain_lease_active(now=now)


def test_gate_update_busy_releases_lease() -> None:
    """With in-flight work, gate-update exits 75 and releases the drain lease.

    The lease must NOT remain set after a deferred poll — a busy deferral
    that leaves the lease active would silently block dispatch until the
    TTL expires.
    """
    now = dt.datetime(2026, 6, 26, 12, 0, 0)
    repo = _busy_repo()
    runner = CliRunner()
    result = runner.invoke(app, ["gate-update"], obj=build_cli_context(repo=repo))
    assert result.exit_code == 75
    assert not repo.is_drain_lease_active(now=now)
