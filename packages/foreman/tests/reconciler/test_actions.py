"""Tests for v3 actions — enum, context, executor."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
        # foreman#228 (runaway-burn defense): per-ticket-per-role
        # consecutive-failure rate-limit. Separate from SURFACE_HELP so
        # the executor can render a role-specific comment + write the
        # reset sentinel that lets human re-queue give a fresh window.
        "RATE_LIMIT_TRIP",
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
        # foreman#294: earlier-firing impl PR retarget so the Reviewer
        # on impl reads a stable base before any operator-driven or
        # auto-delete-on-merge prune of the spec branch can crash the
        # diff fetch.
        "RETARGET_IMPL_PR_TO_DEFAULT",
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

    # foreman#279 — retarget-guard test seams. Per-test override of
    # the PR's current base ref + the spec-PR-merged predicate +
    # the default branch. Defaults are tuned so existing
    # attempt_merge_* tests stay green: current base is "main" (so
    # the retarget conditional sees "already on default" and skips),
    # spec PR is reported merged (irrelevant when base is already
    # main), and the default branch is "main".
    pr_base_ref_for: dict[int, str] = field(default_factory=dict)
    spec_pr_merged_for_branch: dict[str, bool] = field(default_factory=dict)
    default_branch_name: str = "main"

    def retarget_pr_base(
        self, *, owner: str, repo: str, pr_number: int, new_base: str
    ) -> None:
        self.calls.append(
            (
                "retarget_pr_base",
                {
                    "owner": owner,
                    "repo": repo,
                    "pr_number": pr_number,
                    "new_base": new_base,
                },
            )
        )

    def get_pr_base_ref(
        self, *, owner: str, repo: str, pr_number: int
    ) -> str:
        self.calls.append(
            (
                "get_pr_base_ref",
                {"owner": owner, "repo": repo, "pr_number": pr_number},
            )
        )
        return self.pr_base_ref_for.get(pr_number, "main")

    def is_pr_merged_for_branch(
        self, *, owner: str, repo: str, branch: str
    ) -> bool:
        self.calls.append(
            (
                "is_pr_merged_for_branch",
                {"owner": owner, "repo": repo, "branch": branch},
            )
        )
        return self.spec_pr_merged_for_branch.get(branch, True)

    def get_default_branch(self, *, owner: str, repo: str) -> str:
        self.calls.append(
            (
                "get_default_branch",
                {"owner": owner, "repo": repo},
            )
        )
        return self.default_branch_name


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
    'all gates green → merge' row."""
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
    remaining branches share the body via target-parameterized helper."""
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


# ---------------------------------------------------------------------------
# foreman#279 — impl PR retarget guard (D9 from the 2026-06-11
# architecture stability plan). Without these tests, the autonomous
# loop silently orphans impl content onto the about-to-be-deleted
# spec branch — 15 historical PRs were merged into orphan branches
# this way before the empirical audit caught it. The v1/v2 daemon
# had this guard in daemon_runners.merge_impl_pr; the v3 reconciler
# migration did not port it. These tests pin the v3 port.
# ---------------------------------------------------------------------------


def test_attempt_merge_impl_retargets_base_to_main_when_spec_merged(
    tmp_path: Path,
) -> None:
    """foreman#279 — primary regression guard.

    Scenario: impl PR's base still points at ``foreman/issue-143``
    (the spec branch), the spec PR for issue 143 has merged. The
    handler MUST retarget the impl PR's base to the default branch
    BEFORE merging — otherwise the squash commit lands on the
    about-to-be-deleted spec branch and the content orphans.

    This is the test that would have caught the orphan-PR bug the
    moment it ran. If it ever fails on a future change, that change
    is reintroducing the bug.
    """
    ctx = _attempt_merge_ctx(
        tmp_path, head_ref="foreman/impl-143", pr_number=200
    )
    host = _FakeHost()
    # Impl PR's base is still the spec branch (the bug scenario).
    host.pr_base_ref_for = {200: "foreman/issue-143"}
    # Spec PR for issue 143 has merged.
    host.spec_pr_merged_for_branch = {"foreman/issue-143": True}
    host.default_branch_name = "main"
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

    call_names = [c[0] for c in host.calls]
    assert "retarget_pr_base" in call_names, (
        "Impl PR with base=spec-branch + spec PR merged must be retargeted. "
        "Without retarget, merge_pr produces an orphan commit on the spec "
        "branch — the v3 regression that orphaned 15 PRs before being "
        "diagnosed 2026-06-11."
    )
    # Retarget happens BEFORE merge (ordering matters — merging
    # before retarget produces the orphan we're guarding against).
    retarget_idx = call_names.index("retarget_pr_base")
    merge_idx = call_names.index("merge_pr")
    assert retarget_idx < merge_idx, (
        f"retarget_pr_base must come BEFORE merge_pr; "
        f"got order {call_names}"
    )
    # The retarget targeted the correct PR + new_base.
    retarget_call = next(
        c for c in host.calls if c[0] == "retarget_pr_base"
    )
    assert retarget_call[1]["pr_number"] == 200
    assert retarget_call[1]["new_base"] == "main"


def test_attempt_merge_impl_skips_retarget_when_base_is_already_default(
    tmp_path: Path,
) -> None:
    """Idempotency: if the impl PR's base is already the default
    branch (e.g., a previous handler invocation succeeded but the
    dispatcher re-enqueued after a crash), the retarget step must
    skip. We read current_base each tick so this is safe."""
    ctx = _attempt_merge_ctx(
        tmp_path, head_ref="foreman/impl-143", pr_number=200
    )
    host = _FakeHost()
    # Impl PR's base is already main — already retargeted from a
    # prior tick, or it never needed retargeting.
    host.pr_base_ref_for = {200: "main"}
    host.spec_pr_merged_for_branch = {"foreman/issue-143": True}
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

    call_names = [c[0] for c in host.calls]
    assert "retarget_pr_base" not in call_names, (
        "Idempotent: when base is already the default branch, no "
        "retarget call should fire."
    )
    # Merge still happens (this is the happy CLEAN path).
    assert "merge_pr" in call_names


def test_attempt_merge_impl_skips_retarget_when_spec_pr_not_merged(
    tmp_path: Path,
) -> None:
    """Safety: do NOT retarget the impl PR if the spec PR has not
    merged yet. Retargeting impl-on-main with a still-pending spec
    would merge impl changes that depend on unlanded spec changes —
    same bug shape from the opposite direction.

    In production this state typically doesn't reach
    ATTEMPT_MERGE_IMPL because the impl PR cannot CLEAN-merge while
    its base spec PR is open — but the guard is here as defense
    against any rule path that reaches the handler in an unexpected
    state.
    """
    ctx = _attempt_merge_ctx(
        tmp_path, head_ref="foreman/impl-143", pr_number=200
    )
    host = _FakeHost()
    host.pr_base_ref_for = {200: "foreman/issue-143"}
    # Spec PR for issue 143 is NOT merged yet.
    host.spec_pr_merged_for_branch = {"foreman/issue-143": False}
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

    call_names = [c[0] for c in host.calls]
    assert "retarget_pr_base" not in call_names, (
        "Safety: when the spec PR has not merged, the impl PR must "
        "NOT be retargeted — impl changes depend on unlanded spec "
        "changes."
    )


def test_attempt_merge_plan_does_not_invoke_retarget_path(
    tmp_path: Path,
) -> None:
    """Plan target is unaffected: ATTEMPT_MERGE_PLAN must not call
    any of the retarget-guard host methods. The plan PR's base IS
    main by construction (planner always opens spec PRs against the
    default branch); querying the base ref + spec-merged predicate
    on the plan path would be pointless work and noise."""
    ctx = _attempt_merge_ctx(tmp_path)  # default head_ref is spec-shape
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
    assert "retarget_pr_base" not in call_names
    assert "get_pr_base_ref" not in call_names
    assert "is_pr_merged_for_branch" not in call_names
    assert "get_default_branch" not in call_names


# ---------------------------------------------------------------------------
# foreman#294 — shared retarget helper + earlier-firing
# RETARGET_IMPL_PR_TO_DEFAULT action. The helper is the deduplication
# of the D9 retarget block (foreman#279); the new action fires at
# precedence 138 BEFORE Reviewer-on-impl so the Reviewer's
# ``_get_pr_diff`` never sees a stale stacked base.
# ---------------------------------------------------------------------------


def test_retarget_impl_pr_to_default_helper_idempotent(tmp_path: Path) -> None:
    """foreman#294: the shared helper is the single source of truth for
    the retarget guard. Three sub-cases pin the contract:

    1. base already on default branch -> no host mutation, returns False.
    2. base == spec branch AND spec PR merged -> exactly one
       ``retarget_pr_base`` call, returns True.
    3. base == spec branch BUT spec PR NOT merged -> no host mutation,
       returns False (safety guard).
    """
    from foreman.reconciler.actions import (
        _retarget_impl_pr_to_default_if_stacked,
    )

    # --- Sub-case 1: already on default branch ---
    ctx = _attempt_merge_ctx(
        tmp_path, head_ref="foreman/impl-143", pr_number=200
    )
    host = _FakeHost()
    host.pr_base_ref_for = {200: "main"}
    host.spec_pr_merged_for_branch = {"foreman/issue-143": True}

    result = _retarget_impl_pr_to_default_if_stacked(ctx, host)

    assert result is False, (
        "Helper must return False when base is already on default branch"
    )
    call_names = [c[0] for c in host.calls]
    assert "retarget_pr_base" not in call_names, (
        "Idempotent: no retarget call when base is already main"
    )

    # --- Sub-case 2: base == spec branch AND spec PR merged ---
    ctx2 = _attempt_merge_ctx(
        tmp_path / "case2", head_ref="foreman/impl-143", pr_number=200
    )
    host2 = _FakeHost()
    host2.pr_base_ref_for = {200: "foreman/issue-143"}
    host2.spec_pr_merged_for_branch = {"foreman/issue-143": True}
    host2.default_branch_name = "main"

    result2 = _retarget_impl_pr_to_default_if_stacked(ctx2, host2)

    assert result2 is True, "Helper must return True when retarget fires"
    retarget_calls = [c for c in host2.calls if c[0] == "retarget_pr_base"]
    assert len(retarget_calls) == 1, (
        f"Expected exactly one retarget_pr_base call, got "
        f"{len(retarget_calls)}: {host2.calls!r}"
    )
    assert retarget_calls[0][1]["pr_number"] == 200
    assert retarget_calls[0][1]["new_base"] == "main"

    # --- Sub-case 3: base == spec branch BUT spec PR NOT merged ---
    ctx3 = _attempt_merge_ctx(
        tmp_path / "case3", head_ref="foreman/impl-143", pr_number=200
    )
    host3 = _FakeHost()
    host3.pr_base_ref_for = {200: "foreman/issue-143"}
    host3.spec_pr_merged_for_branch = {"foreman/issue-143": False}

    result3 = _retarget_impl_pr_to_default_if_stacked(ctx3, host3)

    assert result3 is False, (
        "Safety guard: helper must return False when spec PR has not merged"
    )
    call_names3 = [c[0] for c in host3.calls]
    assert "retarget_pr_base" not in call_names3, (
        "Safety: NEVER retarget if the spec PR's content is still unlanded"
    )


def test_execute_retarget_action_calls_helper_and_logs(
    tmp_path: Path, caplog: Any
) -> None:
    """foreman#294: ``Action.RETARGET_IMPL_PR_TO_DEFAULT`` runs the
    shared helper through ``execute_action``. In the common path (impl
    PR's base is still the spec branch + spec PR merged), it issues
    exactly one ``retarget_pr_base`` host call and logs the True
    return at INFO so the audit trail is clear.
    """
    import logging as _logging

    ctx = _attempt_merge_ctx(
        tmp_path, head_ref="foreman/impl-143", pr_number=200
    )
    host = _FakeHost()
    host.pr_base_ref_for = {200: "foreman/issue-143"}
    host.spec_pr_merged_for_branch = {"foreman/issue-143": True}
    host.default_branch_name = "main"

    caplog.set_level(_logging.INFO, logger="foreman.reconciler.actions")

    execute_action(
        Action.RETARGET_IMPL_PR_TO_DEFAULT,
        ctx,
        host=host,
        rule_name="retarget_impl_pr_to_default",
        dry_run=False,
    )

    retarget_calls = [c for c in host.calls if c[0] == "retarget_pr_base"]
    assert len(retarget_calls) == 1
    assert retarget_calls[0][1]["pr_number"] == 200
    assert retarget_calls[0][1]["new_base"] == "main"

    # The action handler logs the bool return so the audit trail
    # surfaces whether this tick was the actual retarget or a no-op.
    info_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "foreman.reconciler.actions"
        and record.levelno == _logging.INFO
    ]
    assert any(
        "retarget_impl_pr_to_default: retargeted=True" in m for m in info_messages
    ), (
        "Expected an INFO log line documenting the retarget bool. "
        f"Got: {info_messages!r}"
    )


def test_handle_attempt_merge_impl_still_retargets_via_shared_helper(
    tmp_path: Path,
) -> None:
    """foreman#294 refactor guard: the D9 retarget regression
    (foreman#279) stays green after the inline block was extracted
    into the shared ``_retarget_impl_pr_to_default_if_stacked``
    helper. Same scenario as
    ``test_attempt_merge_impl_retargets_base_to_main_when_spec_merged``
    above; if this fails, the refactor changed behavior — fix the
    refactor.
    """
    ctx = _attempt_merge_ctx(
        tmp_path, head_ref="foreman/impl-143", pr_number=200
    )
    host = _FakeHost()
    host.pr_base_ref_for = {200: "foreman/issue-143"}
    host.spec_pr_merged_for_branch = {"foreman/issue-143": True}
    host.default_branch_name = "main"
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

    call_names = [c[0] for c in host.calls]
    assert "retarget_pr_base" in call_names
    retarget_idx = call_names.index("retarget_pr_base")
    merge_idx = call_names.index("merge_pr")
    assert retarget_idx < merge_idx
