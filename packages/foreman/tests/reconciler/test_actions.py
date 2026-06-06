"""Tests for v3 actions — enum, context, executor."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from foreman.reconciler.actions import Action, ActionContext, execute_action
from foreman.reconciler.exec_log import ExecutionLog
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
        "MERGE_SPEC_PR",
        "ADVANCE_LABEL_TO_PLAN_APPROVED",
        "DISPATCH_WORKER",
        # Per-target Reviewer + Fixer dispatch (Stage-2 action split).
        "DISPATCH_REVIEWER_SPEC",
        "DISPATCH_REVIEWER_IMPL",
        "DISPATCH_FIXER_SPEC",
        "DISPATCH_FIXER_IMPL",
        "MERGE_IMPL_PR",
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

    def merge_pr(self, *, owner: str, repo: str, pr_number: int) -> None:
        self.calls.append(("merge_pr", {"owner": owner, "repo": repo, "pr_number": pr_number}))

    # When non-None, dispatch_role returns this value as the "PID". Set
    # to None to simulate the cap-skip path (host's concurrency cap was
    # full at dispatch time).
    dispatch_role_return: int | None = 12345

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
