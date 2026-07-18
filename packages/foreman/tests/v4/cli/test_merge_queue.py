"""merge-queue — read-only inspection of each project's merge_queue (foreman#550)."""

from __future__ import annotations

import datetime as dt
import json

from typer.testing import CliRunner

from foreman.v4.cli import app
from foreman.v4.cli.context import build_cli_context
from foreman.v4.repository import InMemoryTicketRepository


def _seed_two_entries(repo: InMemoryTicketRepository) -> None:
    now = dt.datetime(2026, 6, 13, 12, 0, 0)
    ticket_a = repo.create_ticket(project="p", issue_number=1, now=now)
    ticket_b = repo.create_ticket(project="p", issue_number=2, now=now)
    head = repo.enqueue_merge(
        project="p",
        ticket_id=ticket_a.id,
        pr_number=101,
        kind="impl",
        now=now,
    )
    repo.enqueue_merge(
        project="p",
        ticket_id=ticket_b.id,
        pr_number=102,
        kind="spec",
        now=now + dt.timedelta(seconds=1),
    )
    repo.mark_merge_active(head.id)
    repo.increment_merge_attempts(head.id)


def test_merge_queue_lists_entries_for_project_as_table() -> None:
    repo = InMemoryTicketRepository()
    _seed_two_entries(repo)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["merge-queue", "--project", "p"],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code == 0, result.output
    assert "101" in result.output
    assert "102" in result.output
    assert "merging" in result.output
    assert "queued" in result.output
    assert "1/3" in result.output


def test_merge_queue_json_format_carries_position_and_detail() -> None:
    repo = InMemoryTicketRepository()
    _seed_two_entries(repo)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["merge-queue", "--project", "p", "--format", "json"],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert len(rows) == 2

    head, tail = rows
    assert head["pos"] == 1
    assert head["project"] == "p"
    assert head["pr"] == 101
    assert head["kind"] == "impl"
    assert head["status"] == "merging"
    assert head["attempts"] == "1/3"
    assert head["detail"] == "merging · attempt 1/3"

    assert tail["pos"] == 2
    assert tail["pr"] == 102
    assert tail["kind"] == "spec"
    assert tail["status"] == "queued"
    assert tail["attempts"] == "0/3"
    assert tail["detail"] != head["detail"]


def test_merge_queue_without_project_shows_all_repos() -> None:
    repo = InMemoryTicketRepository()
    now = dt.datetime(2026, 6, 13, 12, 0, 0)
    ticket_p = repo.create_ticket(project="p", issue_number=1, now=now)
    ticket_q = repo.create_ticket(project="q", issue_number=1, now=now)
    repo.enqueue_merge(project="p", ticket_id=ticket_p.id, pr_number=1, kind="impl", now=now)
    repo.enqueue_merge(project="q", ticket_id=ticket_q.id, pr_number=2, kind="impl", now=now)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["merge-queue", "--format", "json"],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    projects = {r["project"] for r in rows}
    assert projects == {"p", "q"}
    # Each project's own FIFO restarts position numbering at 1.
    assert all(r["pos"] == 1 for r in rows)


def test_merge_queue_empty_project_renders_no_rows() -> None:
    repo = InMemoryTicketRepository()
    repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13, 12, 0, 0))
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["merge-queue", "--project", "p", "--format", "json"],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []
