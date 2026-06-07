"""Tests for v3 actions — enum, context, executor."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from foreman.reconciler.actions import Action, ActionContext, execute_action
from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.host import PRMergeability
from foreman.reconciler.state import IssueState, ProjectSnapshot, PRState


def _snapshot() -> ProjectSnapshot:
    return ProjectSnapshot(
        project="foreman",
        owner="jeffrichley",
        repo="foreman",
        issues=(
            IssueState(
                number=143,
                title="t",
                labels=("foreman:planning",),
                assignees=(),
                body="",
                updated_at=datetime(2026, 6, 3, tzinfo=UTC),
            ),
        ),
        prs=(),
        fetched_at=datetime(2026, 6, 3, tzinfo=UTC),
    )


def test_action_enum_covers_spec_catalog() -> None:
    expected = {
        "NOOP",
        "SURFACE_HELP",
        "DISPATCH_PLANNER",
        # foreman#165 replaces MERGE_SPEC_PR / MERGE_IMPL_PR with a pair
        # of label-advance actions plus a pair of state-machine-driven
        # attempt_merge actions. The actual host.merge_pr call lives
        # inside ATTEMPT_MERGE_* when the PR's mergeStateStatus is CLEAN.
        "ADVANCE_LABEL_TO_MERGING_PLAN",
        "ADVANCE_LABEL_TO_MERGING_IMPL",
        "ATTEMPT_MERGE_PLAN",
        "ATTEMPT_MERGE_IMPL",
        # foreman#171 — auto-transition the queue label so the daemon
        # doesn't stall silently on ``foreman:plan``-only tickets.
        "ADVANCE_LABEL_TO_PLANNING",
        "ADVANCE_LABEL_TO_PLAN_APPROVED",
        "DISPATCH_WORKER",
        # Per-target Reviewer + Fixer dispatch (Stage-2 action split).
        "DISPATCH_REVIEWER_SPEC",
        "DISPATCH_REVIEWER_IMPL",
        "DISPATCH_FIXER_SPEC",
        "DISPATCH_FIXER_IMPL",
        "ADVANCE_LABEL_TO_DONE",
    }
    assert {a.name for a in Action} == expected


def test_action_context_exposes_ticket_id(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    issue = snap.issues[0]
    ctx = ActionContext(snapshot=snap, issue=issue, pr=None, log=log)
    assert ctx.ticket_id == "jeffrichley/foreman#143"


def test_action_context_carries_pr_when_present(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    issue = snap.issues[0]
    pr = PRState(
        number=144,
        head_ref="spec-143",
        mergeable="MERGEABLE",
        ci_status="SUCCESS",
        body="Implements #143",
        linked_issue_numbers=(143,),
        is_merged=False,
    )
    ctx = ActionContext(snapshot=snap, issue=issue, pr=pr, log=log)
    assert ctx.pr is pr


def test_action_context_carries_merge_flags(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    issue = snap.issues[0]
    ctx = ActionContext(
        snapshot=snap,
        issue=issue,
        pr=None,
        log=log,
        auto_merge_spec=True,
        auto_merge_impl=False,
    )
    assert ctx.auto_merge_spec is True
    assert ctx.auto_merge_impl is False


def test_action_context_merge_flags_default() -> None:
    """Defaults must match global ReconcilerConfig defaults so existing
    callers (tests, fixtures) that omit the flags don't change behavior:
    spec auto-merges, impl does not.
    """
    import inspect

    sig = inspect.signature(ActionContext)
    assert sig.parameters["auto_merge_spec"].default is True
    assert sig.parameters["auto_merge_impl"].default is False


# --- Executor tests ---


@dataclass
class _FakeHost:
    """Recording test double — captures every call without doing real GH work."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def add_label(self, *, owner: str, repo: str, issue: int, label: str) -> None:
        self.calls.append(("add_label", {"owner": owner, "repo": repo, "issue": issue, "label": label}))

    def remove_label(self, *, owner: str, repo: str, issue: int, label: str) -> None:
        self.calls.append(("remove_label", {"owner": owner, "repo": repo, "issue": issue, "label": label}))

    def post_comment(self, *, owner: str, repo: str, issue: int, body: str) -> None:
        self.calls.append(("post_comment", {"owner": owner, "repo": repo, "issue": issue, "body": body}))

    def merge_pr(
        self, *, owner: str, repo: str, pr_number: int, mechanism: str
    ) -> None:
        self.calls.append(
            (
                "merge_pr",
                {
                    "owner": owner,
                    "repo": repo,
                    "pr_number": pr_number,
                    "mechanism": mechanism,
                },
            )
        )

    # When non-None, dispatch_role returns this value as the "PID". Set
    # to None to simulate the cap-skip path (host's concurrency cap was
    # full at dispatch time).
    dispatch_role_return: int | None = 12345

    # foreman#165: per-test override of the mergeability snapshot the
    # host returns from get_pr_mergeability. Tests inject the GitHub
    # state they want ``_handle_attempt_merge`` to branch on; the
    # default is CLEAN so any test that doesn't care still merges.
    dispatch_mergeability_return: PRMergeability = field(
        default_factory=lambda: PRMergeability(
            state="CLEAN",
            failing_required_check_count=0,
            pending_required_check_count=0,
        )
    )

    def get_pr_mergeability(
        self, *, owner: str, repo: str, pr_number: int
    ) -> PRMergeability:
        self.calls.append(
            (
                "get_pr_mergeability",
                {"owner": owner, "repo": repo, "pr_number": pr_number},
            )
        )
        return self.dispatch_mergeability_return

    def update_branch(
        self, *, owner: str, repo: str, pr_number: int
    ) -> None:
        self.calls.append(
            (
                "update_branch",
                {"owner": owner, "repo": repo, "pr_number": pr_number},
            )
        )

    def dispatch_role(
        self,
        *,
        role: str,
        target: str | None,
        owner: str,
        repo: str,
        issue: int,
        pr_number: int | None,
        start_log_id: int,
        project: str,
    ) -> int | None:
        self.calls.append(
            (
                "dispatch_role",
                {
                    "role": role,
                    "target": target,
                    "owner": owner,
                    "repo": repo,
                    "issue": issue,
                    "pr_number": pr_number,
                    "start_log_id": start_log_id,
                    "project": project,
                },
            )
        )
        return self.dispatch_role_return


def test_execute_noop_does_nothing(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    ctx = ActionContext(snapshot=snap, issue=snap.issues[0], pr=None, log=log)
    host = _FakeHost()

    execute_action(Action.NOOP, ctx, host=host, rule_name="x", dry_run=False)

    assert host.calls == []
    # Noop is not logged either.
    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        rows = conn.execute("SELECT COUNT(*) FROM execution_log").fetchone()[0]
    assert rows == 0


def test_execute_dispatch_skipped_capacity_terminates_running_row(tmp_path: Path) -> None:
    """When host.dispatch_role returns None (cap-skip), executor terminates
    the start row with outcome ``skipped_capacity`` and does NOT raise.

    Regression for the 2026-06-06 cap-cascade incident: pre-fix,
    dispatch_role raised RuntimeError when full, which the executor's
    except-clause caught and wrote as outcome=error. That error termination
    polluted attempt-counting AND ultimately led to tickets being marked
    foreman:failed via downstream rule evaluation. The fix changes
    dispatch_role's contract from raise→return-None and the executor
    handles the None path explicitly as a no-burn skip.
    """
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    ctx = ActionContext(snapshot=snap, issue=snap.issues[0], pr=None, log=log)
    host = _FakeHost()
    # Simulate the cap-skip path: dispatch_role returns None.
    host.dispatch_role_return = None

    # No raise — cap-skip is routine, not exceptional.
    execute_action(
        Action.DISPATCH_PLANNER,
        ctx,
        host=host,
        rule_name="dispatch_planner",
        dry_run=False,
    )

    # The host's dispatch_role WAS called (we attempted to dispatch).
    assert len(host.calls) == 1
    assert host.calls[0][0] == "dispatch_role"

    # The start row was terminated with skipped_capacity — NOT with success
    # (which would double-count) and NOT with error (which polluted the log
    # and corrupted attempt-counting in the original bug).
    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        rows = conn.execute(
            "SELECT outcome, details FROM execution_log "
            "WHERE action='dispatch_planner' AND outcome != 'running'"
        ).fetchall()
    assert len(rows) == 1, (
        f"Expected exactly one termination row, got {len(rows)}. "
        "Either the cap-skip path didn't terminate, or it wrote multiple "
        "rows."
    )
    outcome, details = rows[0]
    assert outcome == "skipped_capacity", (
        f"Expected outcome=skipped_capacity, got {outcome!r}. If this "
        "shows 'error', the cap-cascade fix regressed and the original "
        "2026-06-06 incident is back."
    )
    assert "concurrency cap reached" in details

    # has_unterminated must be False — we DID terminate the row, just
    # with skipped_capacity not success.
    assert log.has_unterminated("dispatch_planner", ctx.ticket_id) is False


def test_execute_dispatch_planner_writes_running_and_calls_host(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    ctx = ActionContext(snapshot=snap, issue=snap.issues[0], pr=None, log=log)
    host = _FakeHost()

    execute_action(Action.DISPATCH_PLANNER, ctx, host=host, rule_name="dispatch_planner", dry_run=False)

    assert len(host.calls) == 1
    call_name, call_kwargs = host.calls[0]
    assert call_name == "dispatch_role"
    assert call_kwargs["role"] == "planner"
    # Planner is not target-ambiguous — executor must pass target=None.
    assert call_kwargs["target"] is None
    assert call_kwargs["owner"] == "jeffrichley"
    assert call_kwargs["repo"] == "foreman"
    assert call_kwargs["issue"] == 143
    assert call_kwargs["pr_number"] is None
    # The executor must pass the start_log_id it just wrote so the host can
    # terminate that exact row when the subprocess exits.
    assert isinstance(call_kwargs["start_log_id"], int) and call_kwargs["start_log_id"] > 0
    assert log.has_unterminated("dispatch_planner", ctx.ticket_id) is True


def test_dispatch_role_uses_snapshot_project_not_host_default(tmp_path: Path) -> None:
    """Executor must pass ``ctx.snapshot.project`` to ``host.dispatch_role``
    so each dispatch uses the right project name regardless of host
    construction order.

    Regression for the v3 dogfood bug (2026-06-04): pre-fix, V3GitHubHost was
    constructed once with ``projects[0].name`` baked into ``self._project_name``
    and reused for all projects. Every dispatched subprocess got
    ``--project <first_registered>`` regardless of which project's ticket
    fired the rule. The fix moves ``project`` to a per-call kwarg on
    ``dispatch_role`` and has the executor pass ``ctx.snapshot.project``.
    """
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    # Snapshot belongs to project "foreman" — the executor must plumb that
    # exact value through to dispatch_role, not whatever the host might
    # internally default to.
    snap = _snapshot()  # project="foreman"
    ctx = ActionContext(snapshot=snap, issue=snap.issues[0], pr=None, log=log)

    captured_dispatch_kwargs: dict[str, Any] = {}

    class _ProjectCapturingHost:
        """Test double that captures the ``project`` kwarg specifically — and
        defaults that kwarg to ``"voice"`` (the wrong project) if the executor
        forgets to pass it, so the assertion fails loudly in either bug shape.
        """

        def add_label(self, **k: Any) -> None: ...
        def remove_label(self, **k: Any) -> None: ...
        def post_comment(self, **k: Any) -> None: ...
        def merge_pr(self, **k: Any) -> None: ...

        def dispatch_role(
            self,
            *,
            role: str,
            target: str | None,
            owner: str,
            repo: str,
            issue: int,
            pr_number: int | None,
            start_log_id: int,
            project: str,
        ) -> int:
            captured_dispatch_kwargs["project"] = project
            return 42

    execute_action(
        Action.DISPATCH_PLANNER,
        ctx,
        host=_ProjectCapturingHost(),
        rule_name="dispatch_planner",
        dry_run=False,
    )

    assert captured_dispatch_kwargs["project"] == "foreman"


def test_execute_advance_label_to_planning_swaps_labels(tmp_path: Path) -> None:
    """foreman#171: handler removes ``foreman:plan`` and adds
    ``foreman:planning`` so ``dispatch_planner`` fires on the next poll."""
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    ctx = ActionContext(snapshot=snap, issue=snap.issues[0], pr=None, log=log)
    host = _FakeHost()

    execute_action(
        Action.ADVANCE_LABEL_TO_PLANNING,
        ctx,
        host=host,
        rule_name="advance_label_to_planning",
        dry_run=False,
    )

    # remove_label("foreman:plan") + add_label("foreman:planning")
    label_calls = [(c[0], c[1]["label"]) for c in host.calls]
    assert ("remove_label", "foreman:plan") in label_calls
    assert ("add_label", "foreman:planning") in label_calls
    # Termination row written; no orphan ``running`` row.
    assert log.has_unterminated("advance_label_to_planning", ctx.ticket_id) is False


def test_execute_advance_label_writes_running_then_success(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    ctx = ActionContext(snapshot=snap, issue=snap.issues[0], pr=None, log=log)
    host = _FakeHost()

    execute_action(
        Action.ADVANCE_LABEL_TO_PLAN_APPROVED,
        ctx,
        host=host,
        rule_name="advance_label_to_plan_approved",
        dry_run=False,
    )

    call_names = [c[0] for c in host.calls]
    assert "remove_label" in call_names
    assert "add_label" in call_names
    # Both running + success rows present, advance is complete.
    assert log.has_unterminated("advance_label_to_plan_approved", ctx.ticket_id) is False


def test_dry_run_does_not_call_host_but_logs_dry_run_outcome(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    ctx = ActionContext(snapshot=snap, issue=snap.issues[0], pr=None, log=log)
    host = _FakeHost()

    execute_action(Action.DISPATCH_PLANNER, ctx, host=host, rule_name="dispatch_planner", dry_run=True)

    assert host.calls == []
    import sqlite3
    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        outcome = conn.execute("SELECT outcome FROM execution_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert outcome == "dry_run"


def test_execute_dispatch_reviewer_spec_passes_target_spec_pr(tmp_path: Path) -> None:
    """Each per-target dispatch action must plumb the right ``target`` string
    into ``host.dispatch_role``. CRITICAL #4: pre-rescue the executor passed
    no target; the role then defaulted to ``spec_pr`` even on impl-fix
    dispatches, raising on the entry-label check."""
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    ctx = ActionContext(snapshot=snap, issue=snap.issues[0], pr=None, log=log)
    host = _FakeHost()

    execute_action(
        Action.DISPATCH_REVIEWER_SPEC,
        ctx,
        host=host,
        rule_name="dispatch_reviewer_spec",
        dry_run=False,
    )

    assert len(host.calls) == 1
    _, kwargs = host.calls[0]
    assert kwargs["role"] == "reviewer"
    assert kwargs["target"] == "spec_pr"


def test_execute_dispatch_reviewer_impl_passes_target_impl_pr(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    ctx = ActionContext(snapshot=snap, issue=snap.issues[0], pr=None, log=log)
    host = _FakeHost()

    execute_action(
        Action.DISPATCH_REVIEWER_IMPL,
        ctx,
        host=host,
        rule_name="dispatch_reviewer_impl",
        dry_run=False,
    )

    _, kwargs = host.calls[0]
    assert kwargs["role"] == "reviewer"
    assert kwargs["target"] == "impl_pr"


def test_execute_dispatch_fixer_spec_passes_target_spec_pr(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    ctx = ActionContext(snapshot=snap, issue=snap.issues[0], pr=None, log=log)
    host = _FakeHost()

    execute_action(
        Action.DISPATCH_FIXER_SPEC,
        ctx,
        host=host,
        rule_name="dispatch_fixer_spec",
        dry_run=False,
    )

    _, kwargs = host.calls[0]
    assert kwargs["role"] == "fixer"
    assert kwargs["target"] == "spec_pr"


def test_execute_dispatch_fixer_impl_passes_target_impl_pr(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    ctx = ActionContext(snapshot=snap, issue=snap.issues[0], pr=None, log=log)
    host = _FakeHost()

    execute_action(
        Action.DISPATCH_FIXER_IMPL,
        ctx,
        host=host,
        rule_name="dispatch_fixer_impl",
        dry_run=False,
    )

    _, kwargs = host.calls[0]
    assert kwargs["role"] == "fixer"
    assert kwargs["target"] == "impl_pr"


def test_execute_action_handles_host_exception_and_logs_error(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    ctx = ActionContext(snapshot=snap, issue=snap.issues[0], pr=None, log=log)

    class _BoomHost(_FakeHost):
        def dispatch_role(self, **kwargs):  # type: ignore[override]
            raise RuntimeError("boom")

    host = _BoomHost()

    execute_action(Action.DISPATCH_PLANNER, ctx, host=host, rule_name="dispatch_planner", dry_run=False)

    # error termination wrote — has_unterminated must return False after error.
    assert log.has_unterminated("dispatch_planner", ctx.ticket_id) is False
    import sqlite3
    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        outcome = conn.execute(
            "SELECT outcome FROM execution_log ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert outcome == "error"


# ---------------------------------------------------------------------------
# foreman#165 — attempt_merge state-machine branches
# ---------------------------------------------------------------------------


def _attempt_merge_ctx(
    tmp_path: Path,
    *,
    head_ref: str = "foreman/issue-143",
    pr_number: int = 144,
) -> ActionContext:
    """Build an ActionContext with a PR and the merging-plan / merging-impl
    label already applied — i.e., the state at which the attempt_merge_*
    rule fires."""
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    issue = snap.issues[0]
    pr = PRState(
        number=pr_number,
        head_ref=head_ref,
        mergeable="MERGEABLE",
        ci_status="SUCCESS",
        body="Implements #143",
        linked_issue_numbers=(143,),
        is_merged=False,
    )
    return ActionContext(snapshot=snap, issue=issue, pr=pr, log=log)


def test_attempt_merge_plan_clean_calls_host_merge_pr(tmp_path: Path) -> None:
    """CLEAN → call host.merge_pr with the configured merge_mechanism.
    No update_branch, no needs-help. Pins the spec's state-machine table
    'all gates green → merge' row.

    foreman#190: also asserts the post-merge ``remove_label`` call that
    clears ``foreman:merging-plan`` so the downstream phase predicates
    (``_advance_label_to_merging_impl_when_eligible``) see a clean
    label set. Ordering matters — ``remove_label`` MUST appear after
    ``merge_pr`` so a failed merge does not strip the label and force
    the retry rule to re-add it on every poll.
    """
    ctx = _attempt_merge_ctx(tmp_path)
    host = _FakeHost()
    host.dispatch_mergeability_return = PRMergeability(
        state="CLEAN",
        failing_required_check_count=0,
        pending_required_check_count=0,
    )

    execute_action(
        Action.ATTEMPT_MERGE_PLAN,
        ctx,
        host=host,
        rule_name="attempt_merge_plan",
        dry_run=False,
    )

    call_names = [c[0] for c in host.calls]
    assert "get_pr_mergeability" in call_names
    assert "merge_pr" in call_names
    assert "update_branch" not in call_names
    # The merge_pr was called on the focal PR with the ctx's mechanism.
    merge_call = next(c for c in host.calls if c[0] == "merge_pr")
    assert merge_call[1]["pr_number"] == 144
    assert merge_call[1]["mechanism"] == "direct"
    # foreman#190: the merging-plan label must be removed AFTER merge_pr,
    # not before — strict ordering protects the retry-on-failure contract.
    merge_idx = next(
        i for i, c in enumerate(host.calls) if c[0] == "merge_pr"
    )
    remove_idx = next(
        (
            i
            for i, c in enumerate(host.calls)
            if c[0] == "remove_label" and c[1]["label"] == "foreman:merging-plan"
        ),
        None,
    )
    assert remove_idx is not None, (
        "expected remove_label(foreman:merging-plan) in host.calls after merge"
    )
    assert remove_idx > merge_idx, (
        "remove_label must execute AFTER merge_pr — otherwise a failed "
        "merge would strip the label and force the retry rule to re-add it"
    )


def test_attempt_merge_plan_behind_calls_host_update_branch_and_does_not_merge(
    tmp_path: Path,
) -> None:
    """BEHIND → host.update_branch + NO merge. The next poll re-reads
    mergeability after GitHub's server-side rebase recomputes."""
    ctx = _attempt_merge_ctx(tmp_path)
    host = _FakeHost()
    host.dispatch_mergeability_return = PRMergeability(
        state="BEHIND",
        failing_required_check_count=0,
        pending_required_check_count=0,
    )

    execute_action(
        Action.ATTEMPT_MERGE_PLAN,
        ctx,
        host=host,
        rule_name="attempt_merge_plan",
        dry_run=False,
    )

    call_names = [c[0] for c in host.calls]
    assert "update_branch" in call_names
    assert "merge_pr" not in call_names
    update_call = next(c for c in host.calls if c[0] == "update_branch")
    assert update_call[1]["pr_number"] == 144


def test_attempt_merge_plan_blocked_with_pending_required_checks_is_noop(
    tmp_path: Path,
) -> None:
    """BLOCKED + pending required checks → no-op. The handler reads
    mergeability, sees CI still running, and returns. Next poll will
    re-evaluate."""
    ctx = _attempt_merge_ctx(tmp_path)
    host = _FakeHost()
    host.dispatch_mergeability_return = PRMergeability(
        state="BLOCKED",
        failing_required_check_count=0,
        pending_required_check_count=2,
    )

    execute_action(
        Action.ATTEMPT_MERGE_PLAN,
        ctx,
        host=host,
        rule_name="attempt_merge_plan",
        dry_run=False,
    )

    call_names = [c[0] for c in host.calls]
    assert "get_pr_mergeability" in call_names
    assert "merge_pr" not in call_names
    assert "update_branch" not in call_names
    # No needs-help — pending CI is the normal wait path, not an error.
    assert "add_label" not in call_names
    assert "post_comment" not in call_names


def test_attempt_merge_plan_blocked_with_failing_required_check_surfaces_needs_help(
    tmp_path: Path,
) -> None:
    """BLOCKED + at least one required check failed → needs-help label +
    surface comment. Operators must intervene; the autonomous loop won't
    retry."""
    ctx = _attempt_merge_ctx(tmp_path)
    host = _FakeHost()
    host.dispatch_mergeability_return = PRMergeability(
        state="BLOCKED",
        failing_required_check_count=1,
        pending_required_check_count=0,
    )

    execute_action(
        Action.ATTEMPT_MERGE_PLAN,
        ctx,
        host=host,
        rule_name="attempt_merge_plan",
        dry_run=False,
    )

    # The handler added foreman:needs-help and posted a surfacing comment.
    add_label_calls = [c for c in host.calls if c[0] == "add_label"]
    assert any(
        c[1]["label"] == "foreman:needs-help" for c in add_label_calls
    )
    assert any(c[0] == "post_comment" for c in host.calls)
    # No merge, no rebase.
    assert not any(c[0] == "merge_pr" for c in host.calls)
    assert not any(c[0] == "update_branch" for c in host.calls)


def test_attempt_merge_plan_unstable_surfaces_needs_help(tmp_path: Path) -> None:
    """UNSTABLE → needs-help (conservative default per the issue body's
    'anti-patterns to watch' note). Non-required red is a human-attention
    case in the default config."""
    ctx = _attempt_merge_ctx(tmp_path)
    host = _FakeHost()
    host.dispatch_mergeability_return = PRMergeability(
        state="UNSTABLE",
        failing_required_check_count=0,
        pending_required_check_count=0,
    )

    execute_action(
        Action.ATTEMPT_MERGE_PLAN,
        ctx,
        host=host,
        rule_name="attempt_merge_plan",
        dry_run=False,
    )

    assert any(
        c[0] == "add_label" and c[1]["label"] == "foreman:needs-help"
        for c in host.calls
    )
    assert any(c[0] == "post_comment" for c in host.calls)


def test_attempt_merge_plan_dirty_surfaces_needs_help(tmp_path: Path) -> None:
    """DIRTY → needs-help. A real git conflict can't be resolved by the
    bot; human intervention required."""
    ctx = _attempt_merge_ctx(tmp_path)
    host = _FakeHost()
    host.dispatch_mergeability_return = PRMergeability(
        state="DIRTY",
        failing_required_check_count=0,
        pending_required_check_count=0,
    )

    execute_action(
        Action.ATTEMPT_MERGE_PLAN,
        ctx,
        host=host,
        rule_name="attempt_merge_plan",
        dry_run=False,
    )

    assert any(
        c[0] == "add_label" and c[1]["label"] == "foreman:needs-help"
        for c in host.calls
    )


def test_attempt_merge_plan_unknown_is_noop(tmp_path: Path) -> None:
    """UNKNOWN → no-op. GitHub hasn't computed mergeability yet; the next
    poll re-evaluates. Issue body's 'anti-patterns to watch' explicitly
    says DON'T short-circuit UNKNOWN even though it costs ~30s of dwell."""
    ctx = _attempt_merge_ctx(tmp_path)
    host = _FakeHost()
    host.dispatch_mergeability_return = PRMergeability(
        state="UNKNOWN",
        failing_required_check_count=0,
        pending_required_check_count=0,
    )

    execute_action(
        Action.ATTEMPT_MERGE_PLAN,
        ctx,
        host=host,
        rule_name="attempt_merge_plan",
        dry_run=False,
    )

    # Only the get_pr_mergeability read — no other host calls.
    assert [c[0] for c in host.calls] == ["get_pr_mergeability"]


def test_attempt_merge_impl_clean_calls_host_merge_pr_on_impl_pr(
    tmp_path: Path,
) -> None:
    """target=impl mirror: ATTEMPT_MERGE_IMPL calls host.merge_pr with the
    impl PR's number (not the spec PR's). One mirror test is enough — the
    remaining branches share the body via target-parameterized helper.

    foreman#190: also asserts the post-merge ``remove_label`` call that
    clears ``foreman:merging-impl`` so subsequent ticks see a clean
    label set (the bug-fixed predicate path).
    """
    ctx = _attempt_merge_ctx(
        tmp_path, head_ref="foreman/impl-143", pr_number=200
    )
    host = _FakeHost()
    host.dispatch_mergeability_return = PRMergeability(
        state="CLEAN",
        failing_required_check_count=0,
        pending_required_check_count=0,
    )

    execute_action(
        Action.ATTEMPT_MERGE_IMPL,
        ctx,
        host=host,
        rule_name="attempt_merge_impl",
        dry_run=False,
    )

    merge_calls = [c for c in host.calls if c[0] == "merge_pr"]
    assert len(merge_calls) == 1
    # The impl PR's number — NOT the spec PR's. Defends against a
    # target-mixup regression where the handler reads pr_number from the
    # wrong source.
    assert merge_calls[0][1]["pr_number"] == 200
    # foreman#190: post-merge cleanup of the merging-impl label, in
    # that order — see test_attempt_merge_plan_clean_calls_host_merge_pr
    # for the ordering rationale.
    merge_idx = next(
        i for i, c in enumerate(host.calls) if c[0] == "merge_pr"
    )
    remove_idx = next(
        (
            i
            for i, c in enumerate(host.calls)
            if c[0] == "remove_label" and c[1]["label"] == "foreman:merging-impl"
        ),
        None,
    )
    assert remove_idx is not None, (
        "expected remove_label(foreman:merging-impl) in host.calls after merge"
    )
    assert remove_idx > merge_idx


def _attempt_merge_ctx_with_merging_label(
    tmp_path: Path,
    *,
    head_ref: str,
    pr_number: int,
    merging_label: str,
) -> ActionContext:
    """Like ``_attempt_merge_ctx`` but additionally seeds the issue's
    labels with the corresponding ``foreman:merging-*`` label.

    The production rule predicate guarantees the label is present when
    the action fires (see ``_attempt_merge_plan_eligible`` /
    ``_attempt_merge_impl_eligible`` in rules.py), so the tests for the
    foreman#190 cleanup branch need that same precondition or they would
    test an impossible state. The helper constructs a fresh ``IssueState``
    rather than mutating the frozen dataclass returned by ``_snapshot``.
    """
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    base_snap = _snapshot()
    base_issue = base_snap.issues[0]
    issue = IssueState(
        number=base_issue.number,
        title=base_issue.title,
        labels=tuple(list(base_issue.labels) + [merging_label]),
        assignees=base_issue.assignees,
        body=base_issue.body,
        updated_at=base_issue.updated_at,
    )
    snap = ProjectSnapshot(
        project=base_snap.project,
        owner=base_snap.owner,
        repo=base_snap.repo,
        issues=(issue,),
        prs=base_snap.prs,
        fetched_at=base_snap.fetched_at,
    )
    pr = PRState(
        number=pr_number,
        head_ref=head_ref,
        mergeable="MERGEABLE",
        ci_status="SUCCESS",
        body="Implements #143",
        linked_issue_numbers=(143,),
        is_merged=False,
    )
    return ActionContext(snapshot=snap, issue=issue, pr=pr, log=log)


def test_attempt_merge_plan_clean_removes_merging_plan_label_after_merge(
    tmp_path: Path,
) -> None:
    """foreman#190 — the bug fix's primary unit test.

    Pre-fix, the CLEAN branch of ``_handle_attempt_merge`` called
    ``host.merge_pr`` and returned without touching the
    ``foreman:merging-plan`` label. That stale label then blocked
    ``_advance_label_to_merging_impl_when_eligible`` from firing once
    the Worker produced an impl PR and the Reviewer approved it
    (the predicate requires ``"foreman:merging-plan" not in labels``).

    Post-fix, the CLEAN branch emits a ``remove_label`` for the
    corresponding ``foreman:merging-*`` label so downstream phase
    predicates see a clean post-merge label set.
    """
    ctx = _attempt_merge_ctx_with_merging_label(
        tmp_path,
        head_ref="foreman/issue-143",
        pr_number=144,
        merging_label="foreman:merging-plan",
    )
    host = _FakeHost()
    host.dispatch_mergeability_return = PRMergeability(
        state="CLEAN",
        failing_required_check_count=0,
        pending_required_check_count=0,
    )

    execute_action(
        Action.ATTEMPT_MERGE_PLAN,
        ctx,
        host=host,
        rule_name="attempt_merge_plan",
        dry_run=False,
    )

    # The precise call shape: same owner / repo as the snapshot, same
    # issue number as the focal issue, label exactly ``foreman:merging-plan``.
    expected = (
        "remove_label",
        {
            "owner": "jeffrichley",
            "repo": "foreman",
            "issue": 143,
            "label": "foreman:merging-plan",
        },
    )
    assert expected in host.calls, (
        f"expected {expected!r} in host.calls; got {host.calls!r}"
    )


def test_attempt_merge_impl_clean_removes_merging_impl_label_after_merge(
    tmp_path: Path,
) -> None:
    """Impl-side mirror of the foreman#190 fix. Same shape, different
    target label (``foreman:merging-impl``) and impl-shaped head_ref."""
    ctx = _attempt_merge_ctx_with_merging_label(
        tmp_path,
        head_ref="foreman/impl-143",
        pr_number=200,
        merging_label="foreman:merging-impl",
    )
    host = _FakeHost()
    host.dispatch_mergeability_return = PRMergeability(
        state="CLEAN",
        failing_required_check_count=0,
        pending_required_check_count=0,
    )

    execute_action(
        Action.ATTEMPT_MERGE_IMPL,
        ctx,
        host=host,
        rule_name="attempt_merge_impl",
        dry_run=False,
    )

    expected = (
        "remove_label",
        {
            "owner": "jeffrichley",
            "repo": "foreman",
            "issue": 143,
            "label": "foreman:merging-impl",
        },
    )
    assert expected in host.calls, (
        f"expected {expected!r} in host.calls; got {host.calls!r}"
    )


@pytest.mark.parametrize(
    "mergeability",
    [
        PRMergeability(
            state="BEHIND",
            failing_required_check_count=0,
            pending_required_check_count=0,
        ),
        PRMergeability(
            state="BLOCKED",
            failing_required_check_count=0,
            pending_required_check_count=2,
        ),
        PRMergeability(
            state="BLOCKED",
            failing_required_check_count=1,
            pending_required_check_count=0,
        ),
        PRMergeability(
            state="UNSTABLE",
            failing_required_check_count=0,
            pending_required_check_count=0,
        ),
        PRMergeability(
            state="DIRTY",
            failing_required_check_count=0,
            pending_required_check_count=0,
        ),
        PRMergeability(
            state="UNKNOWN",
            failing_required_check_count=0,
            pending_required_check_count=0,
        ),
    ],
    ids=[
        "BEHIND",
        "BLOCKED_pending",
        "BLOCKED_failing",
        "UNSTABLE",
        "DIRTY",
        "UNKNOWN",
    ],
)
def test_attempt_merge_plan_non_clean_paths_do_not_remove_merging_label(
    tmp_path: Path, mergeability: PRMergeability
) -> None:
    """foreman#190 negative assertion: non-CLEAN branches must NOT emit
    the post-merge ``remove_label`` for ``foreman:merging-plan``. If a
    future refactor moves the cleanup outside the CLEAN branch (e.g.,
    "always remove the label at the end"), the retry-on-next-poll
    contract breaks — BEHIND, BLOCKED+pending, UNSTABLE, UNKNOWN all
    require the label to stay so the rule re-fires on the next tick.
    """
    ctx = _attempt_merge_ctx_with_merging_label(
        tmp_path,
        head_ref="foreman/issue-143",
        pr_number=144,
        merging_label="foreman:merging-plan",
    )
    host = _FakeHost()
    host.dispatch_mergeability_return = mergeability

    execute_action(
        Action.ATTEMPT_MERGE_PLAN,
        ctx,
        host=host,
        rule_name="attempt_merge_plan",
        dry_run=False,
    )

    assert not any(
        c[0] == "remove_label" and c[1]["label"] == "foreman:merging-plan"
        for c in host.calls
    ), (
        f"non-CLEAN path ({mergeability.state}) must NOT remove "
        f"foreman:merging-plan; got host.calls={host.calls!r}"
    )


def test_attempt_merge_plan_clean_does_not_remove_label_when_merge_pr_raises(
    tmp_path: Path,
) -> None:
    """foreman#190 — failure path: if ``host.merge_pr`` raises, the
    cleanup must NOT run so the label stays on the issue and the next
    poll re-enters ``_handle_attempt_merge`` to retry the merge.

    Mirrors ``test_execute_action_handles_host_exception_and_logs_error``
    in that the executor catches the exception and writes an ``error``
    termination row; the handler never reaches the ``remove_label`` line
    because control returns through the except branch.
    """
    ctx = _attempt_merge_ctx_with_merging_label(
        tmp_path,
        head_ref="foreman/issue-143",
        pr_number=144,
        merging_label="foreman:merging-plan",
    )

    class _MergePrRaisesHost(_FakeHost):
        def merge_pr(self, **kwargs: Any) -> None:  # type: ignore[override]
            # Record the attempt for the assertion below, then raise.
            self.calls.append(("merge_pr", kwargs))
            raise RuntimeError("boom")

    host = _MergePrRaisesHost()
    host.dispatch_mergeability_return = PRMergeability(
        state="CLEAN",
        failing_required_check_count=0,
        pending_required_check_count=0,
    )

    execute_action(
        Action.ATTEMPT_MERGE_PLAN,
        ctx,
        host=host,
        rule_name="attempt_merge_plan",
        dry_run=False,
    )

    # The failed merge_pr attempt was recorded.
    assert any(c[0] == "merge_pr" for c in host.calls)
    # The cleanup did NOT run — the label stays on the issue so the
    # next poll re-enters the handler for a retry.
    assert not any(
        c[0] == "remove_label" and c[1]["label"] == "foreman:merging-plan"
        for c in host.calls
    ), (
        "remove_label must NOT run when merge_pr raises — otherwise the "
        "merging-plan label is gone and the retry rule cannot re-fire"
    )
    # And the executor wrote an ``error`` termination row.
    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        outcome = conn.execute(
            "SELECT outcome FROM execution_log ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert outcome == "error"


def test_attempt_merge_plan_dry_run_skips_host(tmp_path: Path) -> None:
    """dry_run=True writes a single ``dry_run`` outcome row and makes no
    host calls — same shape as every other action's dry-run path."""
    ctx = _attempt_merge_ctx(tmp_path)
    host = _FakeHost()
    # Even though the host would CLEAN-merge in the real branch, dry-run
    # must short-circuit before reading mergeability.
    host.dispatch_mergeability_return = PRMergeability(
        state="CLEAN",
        failing_required_check_count=0,
        pending_required_check_count=0,
    )

    execute_action(
        Action.ATTEMPT_MERGE_PLAN,
        ctx,
        host=host,
        rule_name="attempt_merge_plan",
        dry_run=True,
    )

    # No host calls at all on the dry-run path.
    assert host.calls == []
    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        outcome = conn.execute(
            "SELECT outcome FROM execution_log ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert outcome == "dry_run"
