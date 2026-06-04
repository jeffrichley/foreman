"""End-to-end integration tests for the v3 Reconciler.

Stubs the GH GraphQL client + ReconcilerHost; runs `tick()` against canned
project state; asserts the emitted actions + execution log rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from foreman.reconciler import ExecutionLog
from foreman.reconciler.daemon import Reconciler, ReconcilerProject


@dataclass
class _StubGHClient:
    response: dict[str, Any] = field(default_factory=dict)

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return self.response


@dataclass
class _StubHost:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def add_label(self, **kwargs) -> None:
        self.calls.append(("add_label", kwargs))

    def remove_label(self, **kwargs) -> None:
        self.calls.append(("remove_label", kwargs))

    def post_comment(self, **kwargs) -> None:
        self.calls.append(("post_comment", kwargs))

    def merge_pr(self, **kwargs) -> None:
        self.calls.append(("merge_pr", kwargs))

    def dispatch_role(self, **kwargs) -> int:
        self.calls.append(("dispatch_role", kwargs))
        return 12345


def _gh_with(
    issues: list[dict],
    prs: list[dict],
    merged_prs: list[dict] | None = None,
) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "issues": {"nodes": issues},
                "openPRs": {"nodes": prs},
                "recentMergedPRs": {"nodes": merged_prs or []},
            }
        }
    }


def _issue_payload(number: int, labels: list[str]) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"#{number}",
        "body": "",
        "state": "OPEN",
        "updatedAt": "2026-06-03T15:00:00Z",
        "labels": {"nodes": [{"name": label} for label in labels]},
        "assignees": {"nodes": []},
    }


def _pr_payload(
    *,
    number: int,
    closes: list[int],
    mergeable: str = "MERGEABLE",
    ci: str | None = "SUCCESS",
    merged: bool = False,
) -> dict[str, Any]:
    return {
        "number": number,
        "headRefName": f"branch-{number}",
        "body": "",
        "mergeable": mergeable,
        "merged": merged,
        "statusCheckRollup": {"state": ci} if ci else None,
        "closingIssuesReferences": {"nodes": [{"number": n} for n in closes]},
    }


@pytest.mark.asyncio
async def test_tick_emits_dispatch_planner_for_planning_with_no_pr(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    gh = _StubGHClient(_gh_with([_issue_payload(143, ["foreman:planning"])], []))
    host = _StubHost()
    reconciler = Reconciler(
        projects=(ReconcilerProject(name="foreman", owner="jeffrichley", repo="foreman"),),
        log=log,
        gh=gh,
        host=host,
        dry_run=False,
    )

    await reconciler.tick()

    role_calls = [c for c in host.calls if c[0] == "dispatch_role"]
    assert len(role_calls) == 1
    assert role_calls[0][1]["role"] == "planner"
    assert role_calls[0][1]["issue"] == 143
    assert log.has_unterminated("dispatch_planner", "jeffrichley/foreman#143")


@pytest.mark.asyncio
async def test_tick_emits_advance_label_for_stuck_today_ticket_143(tmp_path: Path) -> None:
    """The cutover proof point — v3 unsticks today's gum-up automatically."""
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    gh = _StubGHClient(
        _gh_with(
            [_issue_payload(143, ["foreman:planning"])],
            [_pr_payload(number=144, closes=[143], merged=True)],
        )
    )
    host = _StubHost()
    reconciler = Reconciler(
        projects=(ReconcilerProject(name="foreman", owner="jeffrichley", repo="foreman"),),
        log=log,
        gh=gh,
        host=host,
        dry_run=False,
    )

    await reconciler.tick()

    advance_calls = [c for c in host.calls if c[0] in ("remove_label", "add_label")]
    assert any(c[1].get("label") == "foreman:planning" for c in advance_calls if c[0] == "remove_label")
    assert any(c[1].get("label") == "foreman:plan-approved" for c in advance_calls if c[0] == "add_label")


@pytest.mark.asyncio
async def test_tick_safety_preempts_progress_when_ci_failed(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    gh = _StubGHClient(
        _gh_with(
            [_issue_payload(143, ["foreman:impl-review"])],
            [_pr_payload(number=144, closes=[143], ci="FAILURE")],
        )
    )
    host = _StubHost()
    reconciler = Reconciler(
        projects=(ReconcilerProject(name="foreman", owner="jeffrichley", repo="foreman"),),
        log=log,
        gh=gh,
        host=host,
        dry_run=False,
    )

    await reconciler.tick()

    # surface_help should have fired (add needs-help label + comment), and no
    # reviewer dispatch should have happened.
    assert any(c[0] == "add_label" and c[1].get("label") == "foreman:needs-help" for c in host.calls)
    assert all(c[0] != "dispatch_role" for c in host.calls)


@pytest.mark.asyncio
async def test_tick_dry_run_does_not_call_host(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    gh = _StubGHClient(_gh_with([_issue_payload(143, ["foreman:planning"])], []))
    host = _StubHost()
    reconciler = Reconciler(
        projects=(ReconcilerProject(name="foreman", owner="jeffrichley", repo="foreman"),),
        log=log,
        gh=gh,
        host=host,
        dry_run=True,
    )

    await reconciler.tick()

    assert host.calls == []
    # But the intended action was logged with outcome='dry_run'.
    import sqlite3
    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        outcome = conn.execute("SELECT outcome FROM execution_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert outcome == "dry_run"


@pytest.mark.asyncio
async def test_tick_observer_rate_limited_alerts_after_n_failures(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    class _ErrGH:
        def graphql(self, query, variables):
            raise RuntimeError("API rate limit exceeded")

    host = _StubHost()
    reconciler = Reconciler(
        projects=(ReconcilerProject(name="foreman", owner="jeffrichley", repo="foreman"),),
        log=log,
        gh=_ErrGH(),
        host=host,
        dry_run=False,
        alert_after_n_failures=3,
    )

    # Three consecutive failures should produce one observer_unreachable alert row.
    await reconciler.tick()
    await reconciler.tick()
    await reconciler.tick()

    import sqlite3
    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        rows = conn.execute(
            "SELECT action, outcome FROM execution_log WHERE action='observer_failure_alert'"
        ).fetchall()
    assert len(rows) == 1
