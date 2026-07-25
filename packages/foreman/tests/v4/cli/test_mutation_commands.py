"""hold/resume/retry/skip/drop/set-state — operator mutations."""

from __future__ import annotations

import datetime as dt

import pytest
from typer.testing import CliRunner

from foreman.v4.cli import app
from foreman.v4.cli.context import build_cli_context
from foreman.v4.queue_manager import QueueManager
from foreman.v4.repository import InMemoryTicketRepository, TicketNotFoundError
from foreman.v4.work import WorkItem


def _make(state: str = "Planning") -> tuple[InMemoryTicketRepository, int]:
    repo = InMemoryTicketRepository()
    t = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.set_ticket_state(t.id, state, now=dt.datetime(2026, 6, 13))
    return repo, t.id


def _make_with_history(*states: str) -> tuple[InMemoryTicketRepository, int]:
    """Build a ticket with a real state_instances trail.

    Seeds one ``state_instances`` row per name (in order) and parks the
    ticket at the last one — mirroring how the daemon records a ticket
    that walked Queued→...→NeedsHelp. ``retry`` resolves the resume
    target from this trail.
    """
    repo = InMemoryTicketRepository()
    t = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    for seq, name in enumerate(states):
        repo.open_state_instance(
            ticket_id=t.id,
            state_name=name,
            sequence=seq,
            now=dt.datetime(2026, 6, 13),
        )
    repo.set_ticket_state(t.id, states[-1], now=dt.datetime(2026, 6, 13))
    return repo, t.id


def test_hold_sets_held_columns():
    repo, tid = _make()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["hold", str(tid), "--reason", "vacation", "--by", "jeff"],
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
        app,
        ["retry", str(tid)],
        obj=build_cli_context(repo=repo, qm=qm),
    )
    assert result.exit_code == 0
    assert qm.dequeue() == WorkItem(ticket_id=tid, state_name="Planning", project="p")


def test_retry_clears_next_action_at():
    """foreman#361: an operator-forced retry MUST bypass any active
    transient-provider-error suspension. ``cmd_retry`` calls
    ``clear_next_action_at`` before enqueuing so the Poller doesn't
    skip the just-requeued WorkItem until the suspension window
    elapses.
    """
    repo, tid = _make()
    repo.set_next_action_at(tid, when=dt.datetime(2026, 6, 13, 12, 30, tzinfo=dt.UTC))
    qm = QueueManager(repo=repo, max_in_flight=4)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["retry", str(tid)],
        obj=build_cli_context(repo=repo, qm=qm),
    )
    assert result.exit_code == 0
    assert repo.get_ticket(tid).next_action_at is None
    assert qm.dequeue() == WorkItem(ticket_id=tid, state_name="Planning", project="p")


def test_retry_needshelp_resumes_escalated_role_state():
    """foreman#414: retry on a NeedsHelp ticket re-dispatches the role
    that escalated (the most recent non-terminal state), not a no-op
    re-enqueue of the terminal NeedsHelp state.
    """
    repo, tid = _make_with_history(
        "Queued",
        "Planning",
        "Implementing",
        "NeedsHelp",
    )
    qm = QueueManager(repo=repo, max_in_flight=4)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["retry", str(tid)],
        obj=build_cli_context(repo=repo, qm=qm),
    )
    assert result.exit_code == 0, result.output
    # Ticket moved off the terminal state back to the escalated role-state.
    assert repo.get_ticket(tid).current_state == "Implementing"
    # And a WorkItem for that role-state was enqueued (daemon re-runs Worker).
    assert qm.dequeue() == WorkItem(ticket_id=tid, state_name="Implementing", project="p")


def test_retry_needshelp_message_names_resumed_state():
    """The success message reflects the resumed role-state, not NeedsHelp."""
    repo, tid = _make_with_history("Planning", "Implementing", "NeedsHelp")
    qm = QueueManager(repo=repo, max_in_flight=4)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["retry", str(tid)],
        obj=build_cli_context(repo=repo, qm=qm),
    )
    assert result.exit_code == 0
    assert "NeedsHelp -> Implementing" in result.output


def test_retry_failed_also_resumes_role_state():
    """Failed is the other operator-recovery terminal (foreman drop);
    retry resumes the escalated role-state there too.
    """
    repo, tid = _make_with_history("Planning", "SpecReview", "Failed")
    qm = QueueManager(repo=repo, max_in_flight=4)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["retry", str(tid)],
        obj=build_cli_context(repo=repo, qm=qm),
    )
    assert result.exit_code == 0, result.output
    assert repo.get_ticket(tid).current_state == "SpecReview"
    assert qm.dequeue() == WorkItem(ticket_id=tid, state_name="SpecReview", project="p")


def test_retry_done_refuses():
    """Done is a happy terminal — retry has nothing to re-dispatch."""
    repo, tid = _make_with_history("Planning", "Implementing", "Done")
    qm = QueueManager(repo=repo, max_in_flight=4)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["retry", str(tid)],
        obj=build_cli_context(repo=repo, qm=qm),
    )
    assert result.exit_code != 0
    assert "nothing to retry" in result.output.lower()


def test_retry_needshelp_without_role_history_errors():
    """NeedsHelp with no prior non-terminal instance can't resolve a
    resume target — refuse with a set-state hint rather than no-op.
    """
    repo, tid = _make_with_history("NeedsHelp")
    qm = QueueManager(repo=repo, max_in_flight=4)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["retry", str(tid)],
        obj=build_cli_context(repo=repo, qm=qm),
    )
    assert result.exit_code != 0
    assert "set-state" in result.output.lower()


def test_set_state_changes_current_state():
    repo, tid = _make()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["set-state", str(tid), "SpecReview"],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code == 0
    assert repo.get_ticket(tid).current_state == "SpecReview"


def test_set_state_unknown_state_errors():
    repo, tid = _make()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["set-state", str(tid), "NotAState"],
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
        app,
        ["skip", str(tid), "ImplReview"],
        obj=build_cli_context(repo=repo),
    )
    assert repo.get_ticket(tid).current_state == "ImplReview"


def test_set_state_refuses_merge_queued():
    """foreman#576: `set-state <id> MergeQueued` is refused — it would orphan the ticket.

    ``MergeQueued`` requires the ``enqueue_for_merge`` side-effect (a
    merge_queue row) that only ``Merging``/``SpecMerging`` perform. A bare
    state set leaves the ticket parked with no queue row, so the
    MergeCoordinator never processes it. Refuse and redirect to ``Merging``.
    """
    repo, tid = _make()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["set-state", str(tid), "MergeQueued"],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code != 0
    assert repo.get_ticket(tid).current_state == "Planning"  # unchanged
    assert "Merging" in result.output  # redirect hint


def test_skip_refuses_merge_queued():
    """foreman#576: `skip <id> MergeQueued` is refused for the same reason as set-state."""
    repo, tid = _make()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["skip", str(tid), "MergeQueued"],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code != 0
    assert repo.get_ticket(tid).current_state == "Planning"  # unchanged
    assert "Merging" in result.output


def test_discover_collects_full_state_when_everything_present():
    from foreman.v4.cli.mutations import ResetPlan, _discover
    from foreman.v4.git_provider import FakeGitProvider, PRState

    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(
        project="agent_core",
        issue_number=180,
        now=dt.datetime(2026, 6, 17),
    )
    git = FakeGitProvider()
    git.seed_issue_labels(
        project="agent_core",
        issue_number=180,
        labels={"foreman:state-failed", "foreman:plan", "bug"},
    )
    git.seed_branch(project="agent_core", branch_name="foreman/issue-180")
    git.seed_branch(project="agent_core", branch_name="foreman/impl-180")
    git.set_pr_state(
        project="agent_core",
        pr_number=19,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    git.set_pr_head_branch(
        project="agent_core",
        pr_number=19,
        branch_name="foreman/issue-180",
    )
    git.set_pr_state(
        project="agent_core",
        pr_number=21,
        state=PRState(merged=False, mergeable=False, ci_passing=False),
    )
    git.set_pr_head_branch(
        project="agent_core",
        pr_number=21,
        branch_name="foreman/impl-180",
    )
    plan = _discover(
        git=git,
        repo=repo,
        project="agent_core",
        issue_number=180,
        keep_pr=False,
        keep_worktree=False,
        retrigger=True,
    )
    assert isinstance(plan, ResetPlan)
    assert plan.spec_pr == 19
    assert plan.impl_pr == 21
    assert plan.delete_branches == [
        "foreman/issue-180",
        "foreman/impl-180",
    ]
    assert plan.prune_worktrees is True
    assert plan.strip_labels == {"foreman:state-failed", "foreman:plan"}
    assert plan.delete_ticket_id == ticket.id
    assert plan.apply_plan_label is True


def test_discover_no_row_no_branches_no_prs_minimal_plan():
    from foreman.v4.cli.mutations import _discover
    from foreman.v4.git_provider import FakeGitProvider

    repo = InMemoryTicketRepository()
    git = FakeGitProvider()
    # No labels, no branches, no PRs, no ticket row.
    plan = _discover(
        git=git,
        repo=repo,
        project="agent_core",
        issue_number=999,
        keep_pr=False,
        keep_worktree=False,
        retrigger=True,
    )
    assert plan.spec_pr is None
    assert plan.impl_pr is None
    assert plan.strip_labels == set()
    assert plan.delete_ticket_id is None
    assert plan.apply_plan_label is True


def test_discover_keep_pr_skips_pr_lookup():
    from foreman.v4.cli.mutations import _discover
    from foreman.v4.git_provider import FakeGitProvider, PRState

    repo = InMemoryTicketRepository()
    git = FakeGitProvider()
    git.set_pr_state(
        project="agent_core",
        pr_number=19,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    git.set_pr_head_branch(
        project="agent_core",
        pr_number=19,
        branch_name="foreman/issue-180",
    )
    plan = _discover(
        git=git,
        repo=repo,
        project="agent_core",
        issue_number=180,
        keep_pr=True,
        keep_worktree=False,
        retrigger=True,
    )
    assert plan.spec_pr is None
    assert plan.impl_pr is None


def _full_reset_ctx(tmp_path):
    """Build a CliContext with all the deps cmd_reset reads."""
    from foreman.v4.config import (
        AppCredentials,
        AppsConfig,
        OperatorConfig,
        OperatorIdentity,
        OrchestratorConfig,
        ProjectConfig,
        StorageConfig,
        V4Config,
    )
    from foreman.v4.git_provider import FakeGitProvider, PRState

    repo = InMemoryTicketRepository()
    repo.create_ticket(
        project="agent_core",
        issue_number=180,
        now=dt.datetime(2026, 6, 17),
    )
    git = FakeGitProvider()
    git.seed_issue_labels(
        project="agent_core",
        issue_number=180,
        labels={"foreman:state-failed", "bug"},
    )
    git.seed_branch(project="agent_core", branch_name="foreman/issue-180")
    git.set_pr_state(
        project="agent_core",
        pr_number=19,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    git.set_pr_head_branch(
        project="agent_core",
        pr_number=19,
        branch_name="foreman/issue-180",
    )
    # Seed both worktrees.
    wt_root = tmp_path / "worktrees"
    (wt_root / "agent_core" / "issue-180").mkdir(parents=True)
    (wt_root / "agent_core" / "impl-180").mkdir(parents=True)
    # Seed a clone_path dir + V4Config that names it. The clone dir
    # doesn't need to be a real git repo — cmd_reset only reads the
    # path string off ProjectConfig and threads it into wt.prune,
    # which falls back to rmtree when ``git worktree remove`` rejects
    # (which it will, against a non-repo).
    clone_path = tmp_path / "clone"
    clone_path.mkdir()
    fake_creds = AppCredentials(app_id=1, private_key_path="/tmp/fake.pem")
    v4_config = V4Config(
        storage=StorageConfig(engine="postgres", dsn="postgresql://test/test"),
        log_dir=str(tmp_path / "logs"),
        apps=AppsConfig(
            planner=fake_creds,
            reviewer=fake_creds,
            fixer=fake_creds,
            worker=fake_creds,
        ),
        orchestrator=OrchestratorConfig(
            app_id=2,
            private_key_path="/tmp/fake-orch.pem",
        ),
        operator=OperatorConfig(
            supervisor=OperatorIdentity(name="Test Sup", email="sup@example.com"),
            signer=OperatorIdentity(name="Test Sign", email="sign@example.com"),
        ),
        projects=[
            ProjectConfig(
                name="agent_core",
                repo="org/agent_core",
                local_clone_path=str(clone_path),
                trigger_label="foreman:plan",
                check_command="just check",
            ),
        ],
    )
    ctx = build_cli_context(repo=repo, git=git, config=v4_config)
    return ctx, repo, git, wt_root


def test_reset_dry_run_prints_plan_no_mutations(tmp_path):
    ctx, repo, git, wt_root = _full_reset_ctx(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reset",
            "--project",
            "agent_core",
            "--issue-number",
            "180",
            "--dry-run",
            "--worktrees-root",
            str(wt_root),
        ],
        obj=ctx,
    )
    assert result.exit_code == 0
    # Plan should mention each action by name.
    assert "Close PR #19" in result.stdout
    assert "Delete remote branch foreman/issue-180" in result.stdout
    assert "Prune local worktree" in result.stdout
    assert "Delete ticket row" in result.stdout
    assert "Apply foreman:plan" in result.stdout
    # No mutations occurred.
    assert git.closed_prs == set()
    assert git.deleted_branches == set()
    assert (wt_root / "agent_core" / "issue-180").exists()
    # Ticket row still there.
    repo.get_ticket_by_issue(project="agent_core", issue_number=180)


def test_reset_yes_executes_full_plan(tmp_path):
    ctx, repo, git, wt_root = _full_reset_ctx(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reset",
            "--project",
            "agent_core",
            "--issue-number",
            "180",
            "--yes",
            "--worktrees-root",
            str(wt_root),
        ],
        obj=ctx,
    )
    assert result.exit_code == 0, result.stdout
    assert ("agent_core", 19) in git.closed_prs
    assert ("agent_core", "foreman/issue-180") in git.deleted_branches
    assert ("agent_core", "foreman/impl-180") in git.deleted_branches
    assert not (wt_root / "agent_core" / "issue-180").exists()
    assert not (wt_root / "agent_core" / "impl-180").exists()
    # Foreman labels stripped.
    remaining = git.get_issue_labels(project="agent_core", issue_number=180)
    assert "foreman:state-failed" not in remaining
    # foreman:plan re-applied.
    assert "foreman:plan" in remaining
    # Ticket row gone.
    with pytest.raises(TicketNotFoundError):
        repo.get_ticket_by_issue(project="agent_core", issue_number=180)


def test_reset_prompt_declined_aborts(tmp_path):
    """Default (no --yes) prompts; declining must abort with no mutations."""
    ctx, repo, git, wt_root = _full_reset_ctx(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reset",
            "--project",
            "agent_core",
            "--issue-number",
            "180",
            "--worktrees-root",
            str(wt_root),
        ],
        obj=ctx,
        input="n\n",
    )
    # Decline → exit with non-zero per typer.Abort convention.
    assert result.exit_code != 0
    assert git.closed_prs == set()
    assert (wt_root / "agent_core" / "issue-180").exists()


def test_reset_keep_pr_skips_pr_close(tmp_path):
    ctx, repo, git, wt_root = _full_reset_ctx(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reset",
            "--project",
            "agent_core",
            "--issue-number",
            "180",
            "--yes",
            "--keep-pr",
            "--worktrees-root",
            str(wt_root),
        ],
        obj=ctx,
    )
    assert result.exit_code == 0
    assert git.closed_prs == set()  # PR untouched.
    assert ("agent_core", "foreman/issue-180") in git.deleted_branches  # but branch still went.


def test_reset_keep_worktree_skips_worktree_prune(tmp_path):
    ctx, repo, git, wt_root = _full_reset_ctx(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reset",
            "--project",
            "agent_core",
            "--issue-number",
            "180",
            "--yes",
            "--keep-worktree",
            "--worktrees-root",
            str(wt_root),
        ],
        obj=ctx,
    )
    assert result.exit_code == 0
    assert (wt_root / "agent_core" / "issue-180").exists()


def test_reset_no_retrigger_skips_apply_plan_label(tmp_path):
    ctx, repo, git, wt_root = _full_reset_ctx(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reset",
            "--project",
            "agent_core",
            "--issue-number",
            "180",
            "--yes",
            "--no-retrigger",
            "--worktrees-root",
            str(wt_root),
        ],
        obj=ctx,
    )
    assert result.exit_code == 0
    remaining = git.get_issue_labels(project="agent_core", issue_number=180)
    assert "foreman:plan" not in remaining


def test_reset_idempotent_second_run_minimal(tmp_path):
    """Run reset twice in a row — the second run finds nothing to do."""
    ctx, repo, git, wt_root = _full_reset_ctx(tmp_path)
    runner = CliRunner()
    first = runner.invoke(
        app,
        [
            "reset",
            "--project",
            "agent_core",
            "--issue-number",
            "180",
            "--yes",
            "--worktrees-root",
            str(wt_root),
        ],
        obj=ctx,
    )
    assert first.exit_code == 0
    second = runner.invoke(
        app,
        [
            "reset",
            "--project",
            "agent_core",
            "--issue-number",
            "180",
            "--yes",
            "--worktrees-root",
            str(wt_root),
        ],
        obj=ctx,
    )
    assert second.exit_code == 0
    # Second run plan should ONLY contain the foreman:plan re-apply step
    # (everything else is already clean). The label re-apply is idempotent
    # at the GitProvider layer.
    assert "Apply foreman:plan" in second.stdout
    # Branches: delete_branch is idempotent, so both calls record again.
    # That's fine — test asserts the command succeeded twice.


def test_reset_no_row_still_cleans_debris(tmp_path):
    """The B payoff: row already gone, debris still cleaned."""
    ctx, repo, git, wt_root = _full_reset_ctx(tmp_path)
    # Manually delete the row to simulate "operator nuked it earlier."
    ticket = repo.get_ticket_by_issue(project="agent_core", issue_number=180)
    repo.delete_ticket(ticket.id)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reset",
            "--project",
            "agent_core",
            "--issue-number",
            "180",
            "--yes",
            "--worktrees-root",
            str(wt_root),
        ],
        obj=ctx,
    )
    assert result.exit_code == 0
    assert ("agent_core", "foreman/issue-180") in git.deleted_branches
    assert not (wt_root / "agent_core" / "issue-180").exists()
    # foreman:plan re-applied.
    assert "foreman:plan" in git.get_issue_labels(
        project="agent_core",
        issue_number=180,
    )


def test_reset_partial_failure_continues_and_exits_nonzero(tmp_path, monkeypatch):
    """One step fails → other steps still run, exit code is 1."""
    ctx, repo, git, wt_root = _full_reset_ctx(tmp_path)

    # Force close_pr to explode on PR 19 — every other step must still execute.
    original_close_pr = git.close_pr

    def boom_close_pr(*, project, pr_number):
        if pr_number == 19:
            raise RuntimeError("synthetic PR close failure")
        return original_close_pr(project=project, pr_number=pr_number)

    monkeypatch.setattr(git, "close_pr", boom_close_pr)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reset",
            "--project",
            "agent_core",
            "--issue-number",
            "180",
            "--yes",
            "--worktrees-root",
            str(wt_root),
        ],
        obj=ctx,
    )
    assert result.exit_code == 1
    assert "fail" in result.stdout.lower()
    # But the branch / worktree / row / label steps still ran.
    assert ("agent_core", "foreman/issue-180") in git.deleted_branches
    assert not (wt_root / "agent_core" / "issue-180").exists()


# ---------------------------------------------------------------------------
# foreman#409 — closed-issue guard on foreman reset
# ---------------------------------------------------------------------------


def test_reset_closed_issue_no_flag_exits_nonzero(tmp_path):
    """(a) closed + no --force-reopen → exit 1 with a message naming the issue
    is closed and that --force-reopen is required. Guard fires regardless of
    --dry-run; this test exercises the execute path (--yes).
    """
    ctx, repo, git, wt_root = _full_reset_ctx(tmp_path)
    git.seed_issue_state(project="agent_core", issue_number=180, state="closed")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reset",
            "--project",
            "agent_core",
            "--issue-number",
            "180",
            "--yes",
            "--worktrees-root",
            str(wt_root),
        ],
        obj=ctx,
    )
    assert result.exit_code == 1
    assert "closed" in result.output.lower()
    assert "force-reopen" in result.output.lower()


def test_reset_closed_issue_force_reopen_reopens_and_applies_label(tmp_path):
    """(b) closed + --force-reopen → issue reopened AND foreman:plan label
    applied so the Poller picks it up on the next tick.
    """
    ctx, repo, git, wt_root = _full_reset_ctx(tmp_path)
    git.seed_issue_state(project="agent_core", issue_number=180, state="closed")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reset",
            "--project",
            "agent_core",
            "--issue-number",
            "180",
            "--yes",
            "--force-reopen",
            "--worktrees-root",
            str(wt_root),
        ],
        obj=ctx,
    )
    assert result.exit_code == 0, result.output
    assert git.get_issue_state(project="agent_core", issue_number=180) == "open"
    assert "foreman:plan" in git.get_issue_labels(project="agent_core", issue_number=180)


def test_reset_open_issue_unchanged_by_guard(tmp_path):
    """(c) open issue → guard does not fire, behaviour is unchanged across all
    flag combinations (tested with --yes, no --force-reopen).
    """
    ctx, repo, git, wt_root = _full_reset_ctx(tmp_path)
    # Default FakeGitProvider returns "open" for unseen issues — no seed needed.
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reset",
            "--project",
            "agent_core",
            "--issue-number",
            "180",
            "--yes",
            "--worktrees-root",
            str(wt_root),
        ],
        obj=ctx,
    )
    assert result.exit_code == 0, result.output
    assert "foreman:plan" in git.get_issue_labels(project="agent_core", issue_number=180)


def test_reset_closed_no_retrigger_no_guard_no_reopen(tmp_path):
    """(d) closed + --no-retrigger → guard does not fire (apply_plan_label is
    False), no reopen, no foreman:plan label applied.
    """
    ctx, repo, git, wt_root = _full_reset_ctx(tmp_path)
    git.seed_issue_state(project="agent_core", issue_number=180, state="closed")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reset",
            "--project",
            "agent_core",
            "--issue-number",
            "180",
            "--yes",
            "--no-retrigger",
            "--worktrees-root",
            str(wt_root),
        ],
        obj=ctx,
    )
    assert result.exit_code == 0, result.output
    # Issue state unchanged (still closed — reopen was not called).
    assert git.get_issue_state(project="agent_core", issue_number=180) == "closed"
    # No foreman:plan label was applied.
    assert "foreman:plan" not in git.get_issue_labels(project="agent_core", issue_number=180)


def test_reset_closed_dry_run_no_flag_exits_nonzero_no_mutations(tmp_path):
    """(e) closed + no --force-reopen + --dry-run → exit 1 + warning, guard fires
    BEFORE the dry-run early-return so no mutations occur.
    """
    ctx, repo, git, wt_root = _full_reset_ctx(tmp_path)
    git.seed_issue_state(project="agent_core", issue_number=180, state="closed")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reset",
            "--project",
            "agent_core",
            "--issue-number",
            "180",
            "--dry-run",
            "--worktrees-root",
            str(wt_root),
        ],
        obj=ctx,
    )
    assert result.exit_code == 1
    assert "closed" in result.output.lower()
    # No mutations despite dry-run — guard fires before any execution.
    assert git.closed_prs == set()
    assert git.deleted_branches == set()


def test_reset_closed_force_reopen_dry_run_shows_reopen_step_no_mutation(tmp_path):
    """(f) closed + --force-reopen + --dry-run → exit 0, 'Reopen issue' present
    in stdout plan output, issue state NOT flipped (reopen_issue not executed).
    """
    ctx, repo, git, wt_root = _full_reset_ctx(tmp_path)
    git.seed_issue_state(project="agent_core", issue_number=180, state="closed")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reset",
            "--project",
            "agent_core",
            "--issue-number",
            "180",
            "--dry-run",
            "--force-reopen",
            "--worktrees-root",
            str(wt_root),
        ],
        obj=ctx,
    )
    assert result.exit_code == 0, result.output
    assert "Reopen issue" in result.output
    # Dry-run: reopen_issue was NOT called — state is still "closed".
    assert git.get_issue_state(project="agent_core", issue_number=180) == "closed"
