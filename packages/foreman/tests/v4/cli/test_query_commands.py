"""ps, show, queue — query commands against an in-memory repository."""

from __future__ import annotations

import datetime as dt
import json

from typer.testing import CliRunner

from foreman.v4.cli import app
from foreman.v4.cli.context import build_cli_context
from foreman.v4.outcome import OutcomeKind
from foreman.v4.queue_manager import QueueManager
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.work import WorkItem


def _setup_repo_with_two_tickets() -> InMemoryTicketRepository:
    repo = InMemoryTicketRepository()
    now = dt.datetime(2026, 6, 13, 12, 0, 0)
    a = repo.create_ticket(project="p", issue_number=1, now=now)
    b = repo.create_ticket(project="p", issue_number=2, now=now)
    repo.set_ticket_state(a.id, "Planning", now=now)
    repo.set_ticket_state(b.id, "Done", now=now)
    return repo


def test_ps_lists_open_tickets_as_table():
    repo = _setup_repo_with_two_tickets()
    runner = CliRunner()
    result = runner.invoke(app, ["ps"], obj=build_cli_context(repo=repo))
    assert result.exit_code == 0
    # Only the non-terminal ticket shows by default
    assert "Planning" in result.output
    assert "Done" not in result.output


def test_ps_all_includes_terminal_tickets():
    repo = _setup_repo_with_two_tickets()
    runner = CliRunner()
    result = runner.invoke(app, ["ps", "--all"], obj=build_cli_context(repo=repo))
    assert "Planning" in result.output
    assert "Done" in result.output


def test_ps_format_json_emits_parseable_json():
    repo = _setup_repo_with_two_tickets()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["ps", "--format", "json"],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code == 0
    rows = json.loads(result.output)
    assert isinstance(rows, list)
    assert any(r["state"] == "Planning" for r in rows)


def test_show_renders_state_history_tree():
    repo = InMemoryTicketRepository()
    now = dt.datetime(2026, 6, 13, 12, 0, 0)
    ticket = repo.create_ticket(project="p", issue_number=1, now=now)
    inst1 = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="Queued",
        sequence=1,
        now=now,
    )
    repo.mark_execute_completed(
        inst1.id,
        now=now,
        outcome_kind=OutcomeKind.CLEAN,
        outcome_payload={"summary": "ok"},
        next_state="Planning",
    )
    repo.close_state_instance(inst1.id, now=now)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["show", str(ticket.id)],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code == 0
    assert "Queued" in result.output
    assert "clean" in result.output.lower()


def test_show_unknown_ticket_returns_nonzero():
    repo = InMemoryTicketRepository()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["show", "999"],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code != 0


def test_show_renders_tracked_dep_without_untracked_tag() -> None:
    """show: dep issue IS a tracked ticket → renders 'relies on #290' (no untracked tag)."""
    repo = InMemoryTicketRepository()
    now = dt.datetime(2026, 6, 13, 12, 0, 0)
    # ticket under test: project "p", issue 291
    ticket = repo.create_ticket(project="p", issue_number=291, now=now)
    # dep ticket 290 IS tracked
    repo.create_ticket(project="p", issue_number=290, now=now)
    repo.set_ticket_dependencies(ticket.id, deps=[290])
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["show", str(ticket.id)],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code == 0
    assert "relies on #290" in result.output
    assert "(untracked)" not in result.output


def test_show_renders_untracked_dep_with_untracked_tag() -> None:
    """show: dep issue is NOT a tracked ticket → renders 'relies on #290 (untracked)'."""
    repo = InMemoryTicketRepository()
    now = dt.datetime(2026, 6, 13, 12, 0, 0)
    ticket = repo.create_ticket(project="p", issue_number=291, now=now)
    # dep 290 is NOT tracked (no create_ticket for it)
    repo.set_ticket_dependencies(ticket.id, deps=[290])
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["show", str(ticket.id)],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code == 0
    assert "#290 (untracked)" in result.output


def test_show_no_deps_renders_no_relies_on_line() -> None:
    """show: no unmet deps → 'relies on' line is absent."""
    repo = InMemoryTicketRepository()
    now = dt.datetime(2026, 6, 13, 12, 0, 0)
    ticket = repo.create_ticket(project="p", issue_number=291, now=now)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["show", str(ticket.id)],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code == 0
    assert "relies on" not in result.output


def test_ps_blocked_by_column_tracked_dep() -> None:
    """ps: ticket with tracked dep → blocked_by column contains '#290' (no untracked tag)."""
    repo = InMemoryTicketRepository()
    now = dt.datetime(2026, 6, 13, 12, 0, 0)
    ticket = repo.create_ticket(project="p", issue_number=291, now=now)
    repo.create_ticket(project="p", issue_number=290, now=now)
    repo.set_ticket_dependencies(ticket.id, deps=[290])
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["ps", "--format", "json"],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code == 0
    rows = json.loads(result.output)
    blocked_rows = [r for r in rows if r["issue"] == 291]
    assert len(blocked_rows) == 1
    assert blocked_rows[0]["blocked_by"] == "#290"
    assert "(untracked)" not in blocked_rows[0]["blocked_by"]


def test_ps_blocked_by_column_untracked_dep() -> None:
    """ps: ticket with untracked dep → blocked_by column contains '#290 (untracked)'."""
    repo = InMemoryTicketRepository()
    now = dt.datetime(2026, 6, 13, 12, 0, 0)
    ticket = repo.create_ticket(project="p", issue_number=291, now=now)
    repo.set_ticket_dependencies(ticket.id, deps=[290])
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["ps", "--format", "json"],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code == 0
    rows = json.loads(result.output)
    blocked_rows = [r for r in rows if r["issue"] == 291]
    assert len(blocked_rows) == 1
    assert blocked_rows[0]["blocked_by"] == "#290 (untracked)"


def test_ps_blocked_by_column_empty_when_no_deps() -> None:
    """ps: ticket with no deps → blocked_by column is empty string."""
    repo = _setup_repo_with_two_tickets()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["ps", "--format", "json"],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code == 0
    rows = json.loads(result.output)
    for row in rows:
        assert row["blocked_by"] == ""


def test_queue_reports_depth_and_in_flight():
    repo = _setup_repo_with_two_tickets()
    qm = QueueManager(repo=repo, max_in_flight=4)
    qm.enqueue(WorkItem(ticket_id=1, state_name="Planning", project="p"))
    qm.dequeue()  # 1 in flight, 0 queued
    qm.enqueue(WorkItem(ticket_id=2, state_name="Done", project="p"))  # +1 queued
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["queue"],
        obj=build_cli_context(repo=repo, qm=qm),
    )
    assert result.exit_code == 0
    assert "in_flight" in result.output.lower() or "in flight" in result.output.lower()
    assert "1" in result.output
