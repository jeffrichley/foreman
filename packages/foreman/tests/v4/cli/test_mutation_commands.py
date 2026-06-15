"""hold/resume/retry/skip/drop/set-state — operator mutations."""
from __future__ import annotations

import datetime as dt

from typer.testing import CliRunner

from foreman.v4.cli import app
from foreman.v4.cli.context import build_cli_context
from foreman.v4.queue_manager import QueueManager
from foreman.v4.sqlite_repository import SqliteTicketRepository
from foreman.v4.work import WorkItem


def _make(state: str = "Planning") -> tuple[SqliteTicketRepository, int]:
    repo = SqliteTicketRepository.in_memory()
    t = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.set_ticket_state(t.id, state, now=dt.datetime(2026, 6, 13))
    return repo, t.id


def test_hold_sets_held_columns():
    repo, tid = _make()
    runner = CliRunner()
    result = runner.invoke(
        app, ["hold", str(tid), "--reason", "vacation", "--by", "jeff"],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code == 0
    assert repo.get_ticket(tid).is_held
    assert repo.get_ticket(tid).held_reason == "vacation"


def test_resume_clears_held_columns():
    repo, tid = _make()
    repo.hold_ticket(tid, held_by="jeff", reason="x", now=dt.datetime(2026, 6, 13))
    runner = CliRunner()
    result = runner.invoke(app, ["resume", str(tid)], obj=build_cli_context(repo=repo))
    assert result.exit_code == 0
    assert not repo.get_ticket(tid).is_held


def test_retry_enqueues_workitem_for_current_state():
    repo, tid = _make()
    qm = QueueManager(repo=repo, max_in_flight=4)
    runner = CliRunner()
    result = runner.invoke(
        app, ["retry", str(tid)],
        obj=build_cli_context(repo=repo, qm=qm),
    )
    assert result.exit_code == 0
    assert qm.dequeue() == WorkItem(ticket_id=tid, state_name="Planning")


def test_set_state_changes_current_state():
    repo, tid = _make()
    runner = CliRunner()
    result = runner.invoke(
        app, ["set-state", str(tid), "SpecReview"],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code == 0
    assert repo.get_ticket(tid).current_state == "SpecReview"


def test_set_state_unknown_state_errors():
    repo, tid = _make()
    runner = CliRunner()
    result = runner.invoke(
        app, ["set-state", str(tid), "NotAState"],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code != 0


def test_drop_sets_failed():
    repo, tid = _make()
    runner = CliRunner()
    runner.invoke(app, ["drop", str(tid)], obj=build_cli_context(repo=repo))
    assert repo.get_ticket(tid).current_state == "Failed"


def test_skip_targets_next_state():
    repo, tid = _make()
    runner = CliRunner()
    runner.invoke(
        app, ["skip", str(tid), "ImplReview"],
        obj=build_cli_context(repo=repo),
    )
    assert repo.get_ticket(tid).current_state == "ImplReview"
