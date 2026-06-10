"""Per-ticket per-role consecutive-failure rate-limit (foreman#228).

After N=3 consecutive same-role failures on the same ticket within a
W=30-minute window, the dispatcher must:

1. STOP re-dispatching the role on subsequent polls.
2. Transition the ticket to ``foreman:needs-help``.
3. Post a comment naming the trip + recent failure traces.
4. Reset the counter so the human can re-queue without immediately
   tripping again on the next failure.

The counter resets on EITHER same-role success OR ``foreman:needs-help``
removal (via the reset sentinel written at trip time). Per-ticket +
per-role isolation: ticket A failures don't affect ticket B; Planner
failures don't affect Worker dispatch.

These tests cover the contract end-to-end. They are the red-test floor
for the runaway-burn defense (yesterday foreman#227 spun 171 consecutive
Planner dispatches in 2h52m because no per-role-per-ticket cap existed).
Commit 1 (#230) and commit 2 (#229) reduce the surface area; this commit
is the structural floor.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from foreman.reconciler.actions import Action, ActionContext, execute_action
from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.host import PRMergeability
from foreman.reconciler.rules import evaluate
from foreman.reconciler.state import IssueState, ProjectSnapshot, PRState

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _issue(
    *,
    number: int = 143,
    labels: tuple[str, ...] = (),
) -> IssueState:
    return IssueState(
        number=number,
        title="t",
        labels=labels,
        assignees=(),
        body="",
        updated_at=datetime(2026, 6, 9, tzinfo=UTC),
    )


def _ctx(
    tmp_path: Path,
    issue: IssueState,
    pr: PRState | None = None,
    *,
    db_name: str = "log.sqlite",
) -> ActionContext:
    log = ExecutionLog(tmp_path / db_name)
    log.init()
    snap = ProjectSnapshot(
        project="foreman",
        owner="jeffrichley",
        repo="foreman",
        issues=(issue,),
        prs=(pr,) if pr else (),
        fetched_at=datetime(2026, 6, 9, tzinfo=UTC),
    )
    return ActionContext(snapshot=snap, issue=issue, pr=pr, log=log)


def _record_failure(
    log: ExecutionLog,
    *,
    ticket_id: str,
    action: str,
    outcome: str = "error",
    ts: datetime | None = None,
) -> None:
    """Append a complete failure pair (running + terminated)."""
    start_id = log.write_action(
        ticket_id=ticket_id,
        project="foreman",
        rule_name=action,
        action=action,
        outcome="running",
        details={},
    )
    log.terminate_action(parent_log_id=start_id, outcome=outcome, details={})
    if ts is not None:
        _backdate(log, start_id, ts)


def _record_success(
    log: ExecutionLog,
    *,
    ticket_id: str,
    action: str,
    ts: datetime | None = None,
) -> None:
    start_id = log.write_action(
        ticket_id=ticket_id,
        project="foreman",
        rule_name=action,
        action=action,
        outcome="running",
        details={},
    )
    log.terminate_action(parent_log_id=start_id, outcome="success", details={})
    if ts is not None:
        _backdate(log, start_id, ts)


def _backdate(log: ExecutionLog, parent_id: int, ts: datetime) -> None:
    """Rewrite ``ts`` on the start row + its termination row.

    SQLite's CURRENT_TIMESTAMP writes UTC as ``YYYY-MM-DD HH:MM:SS``
    (no ``T`` separator). The reader code in exec_log.py uses the same
    string-comparison shape; mirror it here.
    """
    ts_sql = ts.strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(log.db_path) as conn:
        conn.execute("UPDATE execution_log SET ts = ? WHERE id = ?", (ts_sql, parent_id))
        conn.execute(
            "UPDATE execution_log SET ts = ? WHERE parent_log_id = ?",
            (ts_sql, parent_id),
        )


# ---------------------------------------------------------------------------
# ExecutionLog query — recent-failure counter
# ---------------------------------------------------------------------------


def test_count_recent_failures_zero_for_fresh_ticket(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    assert (
        log.count_recent_failures(
            action="dispatch_planner",
            ticket_id="foo/bar#1",
            within_seconds=1800,
        )
        == 0
    )


def test_count_recent_failures_counts_errors_within_window(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    ticket = "foo/bar#1"
    for _ in range(3):
        _record_failure(log, ticket_id=ticket, action="dispatch_planner")
    assert (
        log.count_recent_failures(
            action="dispatch_planner",
            ticket_id=ticket,
            within_seconds=1800,
        )
        == 3
    )


def test_count_recent_failures_excludes_success(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    ticket = "foo/bar#1"
    _record_failure(log, ticket_id=ticket, action="dispatch_planner")
    _record_success(log, ticket_id=ticket, action="dispatch_planner")
    assert (
        log.count_recent_failures(
            action="dispatch_planner",
            ticket_id=ticket,
            within_seconds=1800,
        )
        == 0
    )


def test_count_recent_failures_resets_after_success(tmp_path: Path) -> None:
    """A successful run clears the counter — failures BEFORE the success
    must not count toward the next round."""
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    ticket = "foo/bar#1"
    # Backdate the prior failures + success so the latest failure's ts
    # is unambiguously after the success's ts. SQLite stores
    # CURRENT_TIMESTAMP at 1-second granularity, and tests can pack
    # multiple writes into the same second — production polls are
    # >=10s apart so this is a test-fixture concern only.
    now = datetime.now(UTC)
    _record_failure(
        log, ticket_id=ticket, action="dispatch_planner", ts=now - timedelta(minutes=10)
    )
    _record_failure(log, ticket_id=ticket, action="dispatch_planner", ts=now - timedelta(minutes=9))
    _record_success(log, ticket_id=ticket, action="dispatch_planner", ts=now - timedelta(minutes=5))
    _record_failure(log, ticket_id=ticket, action="dispatch_planner", ts=now - timedelta(minutes=1))
    # Only the single failure AFTER the success counts.
    assert (
        log.count_recent_failures(
            action="dispatch_planner",
            ticket_id=ticket,
            within_seconds=1800,
        )
        == 1
    )


def test_count_recent_failures_excludes_old_rows_outside_window(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    ticket = "foo/bar#1"
    old = datetime.now(UTC) - timedelta(minutes=45)
    for _ in range(3):
        _record_failure(log, ticket_id=ticket, action="dispatch_planner", ts=old)
    # 45 min ago > 30 min window — no rows count.
    assert (
        log.count_recent_failures(
            action="dispatch_planner",
            ticket_id=ticket,
            within_seconds=1800,
        )
        == 0
    )


def test_count_recent_failures_isolates_by_action(tmp_path: Path) -> None:
    """Failures on dispatch_worker must not count toward dispatch_planner."""
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    ticket = "foo/bar#1"
    for _ in range(3):
        _record_failure(log, ticket_id=ticket, action="dispatch_worker")
    assert (
        log.count_recent_failures(
            action="dispatch_planner",
            ticket_id=ticket,
            within_seconds=1800,
        )
        == 0
    )


def test_count_recent_failures_isolates_by_ticket(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    for _ in range(3):
        _record_failure(log, ticket_id="foo/bar#1", action="dispatch_planner")
    assert (
        log.count_recent_failures(
            action="dispatch_planner",
            ticket_id="foo/bar#2",
            within_seconds=1800,
        )
        == 0
    )


def test_count_recent_failures_resets_after_rate_limit_reset(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    ticket = "foo/bar#1"
    now = datetime.now(UTC)
    # Backdate the early failures so the reset + fresh-failure ts is
    # unambiguously after them. SQLite ts is 1-second granular.
    for i in range(3):
        _record_failure(
            log,
            ticket_id=ticket,
            action="dispatch_planner",
            ts=now - timedelta(minutes=10 - i),
        )
    log.write_rate_limit_reset(action="dispatch_planner", ticket_id=ticket)
    # Counter clears after the reset sentinel.
    assert (
        log.count_recent_failures(
            action="dispatch_planner",
            ticket_id=ticket,
            within_seconds=1800,
        )
        == 0
    )
    # And a fresh failure (1+ second after the reset) starts at 1.
    import time

    time.sleep(1.05)
    _record_failure(log, ticket_id=ticket, action="dispatch_planner")
    assert (
        log.count_recent_failures(
            action="dispatch_planner",
            ticket_id=ticket,
            within_seconds=1800,
        )
        == 1
    )


# ---------------------------------------------------------------------------
# Rule-layer behavior — the rate-limit safety rule
# ---------------------------------------------------------------------------


def test_rule_fires_after_n_consecutive_planner_failures(tmp_path: Path) -> None:
    """RED-TEST FLOOR. After 3 consecutive Planner failures on the same
    ticket, the next reconciliation must NOT re-dispatch — it must
    surface_help (the new ``rate_limit_consecutive_failures`` safety
    rule). Before this fix, ``dispatch_planner`` would keep re-firing on
    every poll cycle (yesterday's runaway: 171 in 2h52m).
    """
    from foreman.reconciler.rules import RULES

    ctx = _ctx(tmp_path, _issue(labels=("foreman:planning",)))
    for _ in range(3):
        _record_failure(ctx.log, ticket_id=ctx.ticket_id, action="dispatch_planner")
    assert evaluate(ctx, rules=RULES) is Action.RATE_LIMIT_TRIP


def test_rule_does_not_fire_under_threshold(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES

    ctx = _ctx(tmp_path, _issue(labels=("foreman:planning",)))
    for _ in range(2):  # N-1 failures
        _record_failure(ctx.log, ticket_id=ctx.ticket_id, action="dispatch_planner")
    # No trip; dispatch_planner is still the right action on the next tick.
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_PLANNER


def test_rule_resets_after_successful_same_role_dispatch(tmp_path: Path) -> None:
    """Same-role success clears the counter — the rate-limit rule must
    NOT trip when there are <N consecutive failures since the latest
    same-role success.

    We assert against the rate-limit predicate (the trip rule) rather
    than the downstream dispatch action because the dispatch action's
    own idempotence gate (success-based) is independent of the
    rate-limit and varies per role. The rate-limit rule's invariant is
    a single number: count_recent_failures(...) >= N → trip; <N → no
    trip. That's what this test pins.
    """
    from foreman.reconciler.rules import RULES

    ctx = _ctx(tmp_path, _issue(labels=("foreman:planning",)))
    now = datetime.now(UTC)
    _record_failure(
        ctx.log,
        ticket_id=ctx.ticket_id,
        action="dispatch_planner",
        ts=now - timedelta(minutes=10),
    )
    _record_failure(
        ctx.log,
        ticket_id=ctx.ticket_id,
        action="dispatch_planner",
        ts=now - timedelta(minutes=9),
    )
    _record_success(
        ctx.log,
        ticket_id=ctx.ticket_id,
        action="dispatch_planner",
        ts=now - timedelta(minutes=5),
    )
    _record_failure(
        ctx.log,
        ticket_id=ctx.ticket_id,
        action="dispatch_planner",
        ts=now - timedelta(minutes=2),
    )
    _record_failure(
        ctx.log,
        ticket_id=ctx.ticket_id,
        action="dispatch_planner",
        ts=now - timedelta(minutes=1),
    )
    # 2 failures since the success — under threshold. The rate-limit
    # rule must not fire RATE_LIMIT_TRIP.
    assert evaluate(ctx, rules=RULES) is not Action.RATE_LIMIT_TRIP


def test_rule_resets_after_needs_help_removal(tmp_path: Path) -> None:
    """After a trip writes a reset sentinel, the next dispatch starts
    fresh. This satisfies the contract that human re-queue (remove
    ``foreman:needs-help`` + re-add entry label) gives the role a clean
    window — even if W=30min hasn't elapsed."""
    from foreman.reconciler.rules import RULES

    ctx = _ctx(tmp_path, _issue(labels=("foreman:planning",)))
    for _ in range(3):
        _record_failure(ctx.log, ticket_id=ctx.ticket_id, action="dispatch_planner")
    # Simulate the trip: rate-limit rule writes the reset sentinel.
    ctx.log.write_rate_limit_reset(action="dispatch_planner", ticket_id=ctx.ticket_id)
    # Human investigates, removes foreman:needs-help, re-adds
    # foreman:planning — same label state we already have. Next failure
    # is now #1 of a new window.
    _record_failure(ctx.log, ticket_id=ctx.ticket_id, action="dispatch_planner")
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_PLANNER


def test_rule_per_role_isolation_planner_does_not_block_worker(
    tmp_path: Path,
) -> None:
    """3 Planner failures must NOT block a Worker dispatch on the same
    ticket. The two action keys are independent."""
    from foreman.reconciler.rules import RULES

    # Ticket has moved to plan-approved (Worker phase).
    ctx = _ctx(tmp_path, _issue(labels=("foreman:plan-approved",)))
    for _ in range(3):
        _record_failure(ctx.log, ticket_id=ctx.ticket_id, action="dispatch_planner")
    # Worker rule must still fire — Planner failures are scoped to its key.
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_WORKER


def test_rule_per_ticket_isolation_ticket_a_does_not_block_ticket_b(
    tmp_path: Path,
) -> None:
    """3 Planner failures on ticket A must NOT block dispatch on ticket B."""
    from foreman.reconciler.rules import RULES

    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    issue_b = _issue(number=200, labels=("foreman:planning",))
    snap = ProjectSnapshot(
        project="foreman",
        owner="jeffrichley",
        repo="foreman",
        issues=(issue_b,),
        prs=(),
        fetched_at=datetime(2026, 6, 9, tzinfo=UTC),
    )
    ctx_b = ActionContext(snapshot=snap, issue=issue_b, pr=None, log=log)
    # Pre-seed 3 failures against TICKET A.
    for _ in range(3):
        _record_failure(log, ticket_id="jeffrichley/foreman#143", action="dispatch_planner")
    # Ticket B must still dispatch.
    assert evaluate(ctx_b, rules=RULES) is Action.DISPATCH_PLANNER


def test_rule_window_is_configurable_via_daemon_config_defaults() -> None:
    """The N and W knobs live on ``DaemonConfig``. The rule reads the
    defaults (3, 1800) when no override is in flight; explicit overrides
    are covered by separate ActionContext tests."""
    from foreman.config import DaemonConfig

    cfg = DaemonConfig()
    assert cfg.rate_limit_max_consecutive_failures == 3
    assert cfg.rate_limit_window_seconds == 1800  # 30 min


def test_daemon_and_reconciler_config_defaults_match_for_rate_limit_knobs() -> None:
    """``DaemonConfig`` and ``ReconcilerConfig`` carry duplicate rate-limit
    knobs by design (the spec names ``DaemonConfig`` as the operator-
    facing surface; the v3 daemon CLI reads from ``ReconcilerConfig``).
    The duplication is documented; this test locks the defaults
    against silent drift — if one site changes default without the
    other, an operator's mental model breaks at the boundary."""
    from foreman.config import DaemonConfig, ReconcilerConfig

    daemon = DaemonConfig()
    reconciler = ReconcilerConfig()
    assert (
        daemon.rate_limit_max_consecutive_failures
        == reconciler.rate_limit_max_consecutive_failures
    )
    assert daemon.rate_limit_window_seconds == reconciler.rate_limit_window_seconds


def test_rule_n_is_configurable_via_action_context(tmp_path: Path) -> None:
    """An ActionContext with a custom N overrides the default (3)."""
    from foreman.reconciler.rules import RULES

    issue = _issue(labels=("foreman:planning",))
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = ProjectSnapshot(
        project="foreman",
        owner="jeffrichley",
        repo="foreman",
        issues=(issue,),
        prs=(),
        fetched_at=datetime(2026, 6, 9, tzinfo=UTC),
    )
    ctx = ActionContext(
        snapshot=snap,
        issue=issue,
        pr=None,
        log=log,
        rate_limit_max_consecutive_failures=2,
    )
    # 2 failures trips the lower N.
    for _ in range(2):
        _record_failure(log, ticket_id=ctx.ticket_id, action="dispatch_planner")
    assert evaluate(ctx, rules=RULES) is Action.RATE_LIMIT_TRIP


def test_rule_w_is_configurable_via_action_context(tmp_path: Path) -> None:
    """An ActionContext with a tight window excludes older failures."""
    from foreman.reconciler.rules import RULES

    issue = _issue(labels=("foreman:planning",))
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = ProjectSnapshot(
        project="foreman",
        owner="jeffrichley",
        repo="foreman",
        issues=(issue,),
        prs=(),
        fetched_at=datetime(2026, 6, 9, tzinfo=UTC),
    )
    ctx = ActionContext(
        snapshot=snap,
        issue=issue,
        pr=None,
        log=log,
        rate_limit_window_seconds=60,  # 1 minute
    )
    # 3 failures, all 5 minutes ago — outside a 60s window.
    old = datetime.now(UTC) - timedelta(minutes=5)
    for _ in range(3):
        _record_failure(log, ticket_id=ctx.ticket_id, action="dispatch_planner", ts=old)
    # No trip; dispatch is allowed.
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_PLANNER


def test_rule_only_fires_when_in_flight_label_present(tmp_path: Path) -> None:
    """The Planner rate-limit only fires for tickets carrying the
    Planner in-flight label (``foreman:planning``). A ticket past
    Planning shouldn't trip on stale planner failures."""
    from foreman.reconciler.rules import RULES

    ctx = _ctx(tmp_path, _issue(labels=("foreman:plan-approved",)))
    for _ in range(3):
        _record_failure(ctx.log, ticket_id=ctx.ticket_id, action="dispatch_planner")
    # Planner rate-limit must not fire; ticket is past Planner phase.
    assert evaluate(ctx, rules=RULES) is not Action.RATE_LIMIT_TRIP


def test_rule_fires_for_worker(tmp_path: Path) -> None:
    """Worker rate-limit symmetric to Planner."""
    from foreman.reconciler.rules import RULES

    ctx = _ctx(tmp_path, _issue(labels=("foreman:plan-approved",)))
    for _ in range(3):
        _record_failure(ctx.log, ticket_id=ctx.ticket_id, action="dispatch_worker")
    assert evaluate(ctx, rules=RULES) is Action.RATE_LIMIT_TRIP


# ---------------------------------------------------------------------------
# Action-executor behavior — RATE_LIMIT_TRIP side effects
# ---------------------------------------------------------------------------


@dataclass
class _FakeHost:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def add_label(self, *, owner: str, repo: str, issue: int, label: str) -> None:
        self.calls.append(
            ("add_label", {"owner": owner, "repo": repo, "issue": issue, "label": label})
        )

    def remove_label(self, *, owner: str, repo: str, issue: int, label: str) -> None:
        self.calls.append(
            (
                "remove_label",
                {"owner": owner, "repo": repo, "issue": issue, "label": label},
            )
        )

    def post_comment(self, *, owner: str, repo: str, issue: int, body: str) -> None:
        self.calls.append(
            ("post_comment", {"owner": owner, "repo": repo, "issue": issue, "body": body})
        )

    def merge_pr(self, *, owner: str, repo: str, pr_number: int, mechanism: str) -> None:
        raise AssertionError("merge_pr should not be called by RATE_LIMIT_TRIP")

    def get_pr_mergeability(self, *, owner: str, repo: str, pr_number: int) -> PRMergeability:
        raise AssertionError("get_pr_mergeability should not be called by RATE_LIMIT_TRIP")

    def update_branch(self, *, owner: str, repo: str, pr_number: int) -> None:
        raise AssertionError("update_branch should not be called by RATE_LIMIT_TRIP")

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
        raise AssertionError("dispatch_role should not be called by RATE_LIMIT_TRIP")


def test_executor_rate_limit_trip_transitions_to_needs_help(
    tmp_path: Path,
) -> None:
    """RATE_LIMIT_TRIP must: (a) remove the in-flight label, (b) add
    ``foreman:needs-help``, (c) post a comment, and (d) write a
    ``rate_limit_reset`` sentinel so the human's re-queue gives a fresh
    window."""
    issue = _issue(labels=("foreman:planning",))
    ctx = _ctx(tmp_path, issue)
    # Pre-seed 3 failures so the comment-builder has something to render.
    for _ in range(3):
        _record_failure(ctx.log, ticket_id=ctx.ticket_id, action="dispatch_planner")
    host = _FakeHost()
    execute_action(
        Action.RATE_LIMIT_TRIP,
        ctx,
        host=host,
        rule_name="rate_limit_consecutive_failures:dispatch_planner",
        dry_run=False,
    )
    call_kinds = [c[0] for c in host.calls]
    assert "add_label" in call_kinds, host.calls
    assert "remove_label" in call_kinds, host.calls
    assert "post_comment" in call_kinds, host.calls
    added = [c[1]["label"] for c in host.calls if c[0] == "add_label"]
    removed = [c[1]["label"] for c in host.calls if c[0] == "remove_label"]
    assert "foreman:needs-help" in added, host.calls
    assert "foreman:planning" in removed, host.calls
    # Reset sentinel written: count_recent_failures now reads 0.
    assert (
        ctx.log.count_recent_failures(
            action="dispatch_planner",
            ticket_id=ctx.ticket_id,
            within_seconds=1800,
        )
        == 0
    )


def test_executor_rate_limit_trip_dry_run_skips_host(tmp_path: Path) -> None:
    issue = _issue(labels=("foreman:planning",))
    ctx = _ctx(tmp_path, issue)
    host = _FakeHost()
    execute_action(
        Action.RATE_LIMIT_TRIP,
        ctx,
        host=host,
        rule_name="rate_limit_consecutive_failures:dispatch_planner",
        dry_run=True,
    )
    assert host.calls == []


def test_executor_rate_limit_trip_comment_names_role_and_count(
    tmp_path: Path,
) -> None:
    issue = _issue(labels=("foreman:planning",))
    ctx = _ctx(tmp_path, issue)
    for _ in range(3):
        _record_failure(ctx.log, ticket_id=ctx.ticket_id, action="dispatch_planner")
    host = _FakeHost()
    execute_action(
        Action.RATE_LIMIT_TRIP,
        ctx,
        host=host,
        rule_name="rate_limit_consecutive_failures:dispatch_planner",
        dry_run=False,
    )
    comment = next(c[1]["body"] for c in host.calls if c[0] == "post_comment")
    # Names the role + the trip explicitly.
    assert "planner" in comment.lower()
    # References needs-help so the operator knows the unblock signal.
    assert "foreman:needs-help" in comment
    # foreman#228 attribution lives in the comment for audit trail.
    assert "228" in comment or "rate-limit" in comment.lower()
