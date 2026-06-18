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


def test_discover_collects_full_state_when_everything_present():
    from foreman.v4.cli.mutations import ResetPlan, _discover
    from foreman.v4.git_provider import FakeGitProvider, PRState

    repo = SqliteTicketRepository.in_memory()
    ticket = repo.create_ticket(
        project="agent_core", issue_number=180, now=dt.datetime(2026, 6, 17),
    )
    git = FakeGitProvider()
    git.seed_issue_labels(
        project="agent_core", issue_number=180,
        labels={"foreman:state-failed", "foreman:plan", "bug"},
    )
    git.seed_branch(project="agent_core", branch_name="foreman/issue-180")
    git.seed_branch(project="agent_core", branch_name="foreman/impl-180")
    git.set_pr_state(
        project="agent_core", pr_number=19,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    git.set_pr_head_branch(
        project="agent_core", pr_number=19, branch_name="foreman/issue-180",
    )
    git.set_pr_state(
        project="agent_core", pr_number=21,
        state=PRState(merged=False, mergeable=False, ci_passing=False),
    )
    git.set_pr_head_branch(
        project="agent_core", pr_number=21, branch_name="foreman/impl-180",
    )
    plan = _discover(
        git=git, repo=repo,
        project="agent_core", issue_number=180,
        keep_pr=False, keep_worktree=False, retrigger=True,
    )
    assert isinstance(plan, ResetPlan)
    assert plan.spec_pr == 19
    assert plan.impl_pr == 21
    assert plan.delete_branches == [
        "foreman/issue-180", "foreman/impl-180",
    ]
    assert plan.prune_worktrees is True
    assert plan.strip_labels == {"foreman:state-failed", "foreman:plan"}
    assert plan.delete_ticket_id == ticket.id
    assert plan.apply_plan_label is True


def test_discover_no_row_no_branches_no_prs_minimal_plan():
    from foreman.v4.cli.mutations import _discover
    from foreman.v4.git_provider import FakeGitProvider

    repo = SqliteTicketRepository.in_memory()
    git = FakeGitProvider()
    # No labels, no branches, no PRs, no ticket row.
    plan = _discover(
        git=git, repo=repo,
        project="agent_core", issue_number=999,
        keep_pr=False, keep_worktree=False, retrigger=True,
    )
    assert plan.spec_pr is None
    assert plan.impl_pr is None
    assert plan.strip_labels == set()
    assert plan.delete_ticket_id is None
    assert plan.apply_plan_label is True


def test_discover_keep_pr_skips_pr_lookup():
    from foreman.v4.cli.mutations import _discover
    from foreman.v4.git_provider import FakeGitProvider, PRState

    repo = SqliteTicketRepository.in_memory()
    git = FakeGitProvider()
    git.set_pr_state(
        project="agent_core", pr_number=19,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    git.set_pr_head_branch(
        project="agent_core", pr_number=19, branch_name="foreman/issue-180",
    )
    plan = _discover(
        git=git, repo=repo,
        project="agent_core", issue_number=180,
        keep_pr=True, keep_worktree=False, retrigger=True,
    )
    assert plan.spec_pr is None
    assert plan.impl_pr is None
