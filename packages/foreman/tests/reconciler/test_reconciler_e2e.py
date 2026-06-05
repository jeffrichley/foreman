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
    head_ref: str | None = None,
) -> dict[str, Any]:
    # Default head_ref is v3 spec-shaped (``foreman/issue-<linked-issue>``) so
    # rule predicates with head-ref filters (adversarial review 4c) still match
    # for spec-PR cases by default. Tests exercising impl-PR rules must pass
    # ``head_ref="foreman/impl-<N>"`` explicitly.
    if head_ref is None:
        head_ref = f"foreman/issue-{closes[0]}" if closes else f"foreman/issue-{number}"
    return {
        "number": number,
        "headRefName": head_ref,
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
            [_pr_payload(number=144, closes=[143], ci="FAILURE", head_ref="foreman/impl-143")],
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


# --- Sentinel-file graceful shutdown ---
#
# The reconciler polls a configured sentinel file at the end of each tick.
# When present, it triggers graceful shutdown (sets the internal stop event)
# AND deletes the file so the next ``daemon start`` doesn't immediately
# shut down. This is the cross-platform shutdown path — on Windows it's the
# only mechanism that works because ``os.kill(pid, SIGTERM)`` there maps to
# ``TerminateProcess`` (a hard kill that delivers no signal).


@pytest.mark.asyncio
async def test_tick_consumes_shutdown_sentinel(tmp_path: Path) -> None:
    """When the sentinel file exists at tick end, the reconciler deletes
    it and sets the stop event so the next ``run`` loop iteration exits.
    """
    sentinel = tmp_path / "shutdown-requested"
    sentinel.write_text("requested by test", encoding="utf-8")

    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    # Empty snapshot — no issues to reconcile; the sentinel check runs
    # at the end of the per-tick loop body regardless.
    gh = _StubGHClient(_gh_with([], []))
    host = _StubHost()

    reconciler = Reconciler(
        projects=(
            ReconcilerProject(name="foreman", owner="jeffrichley", repo="foreman"),
        ),
        log=log,
        gh=gh,
        host=host,
        dry_run=False,
        shutdown_sentinel_path=sentinel,
    )

    assert sentinel.exists()  # pre-check
    assert not reconciler._stop_event.is_set()

    await reconciler.tick()

    assert not sentinel.exists(), "sentinel must be consumed (deleted) on detection"
    assert reconciler._stop_event.is_set(), "stop event must fire after sentinel detection"


@pytest.mark.asyncio
async def test_tick_without_sentinel_does_not_shut_down(tmp_path: Path) -> None:
    """When no sentinel exists, the reconciler keeps running — sentinel
    polling must not introduce a regression that exits the daemon on
    every tick when the sentinel-path knob is configured but the file
    isn't present."""
    sentinel = tmp_path / "shutdown-requested"  # never created
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    gh = _StubGHClient(_gh_with([], []))
    host = _StubHost()

    reconciler = Reconciler(
        projects=(
            ReconcilerProject(name="foreman", owner="jeffrichley", repo="foreman"),
        ),
        log=log,
        gh=gh,
        host=host,
        dry_run=False,
        shutdown_sentinel_path=sentinel,
    )

    await reconciler.tick()

    assert not reconciler._stop_event.is_set()


@pytest.mark.asyncio
async def test_tick_without_sentinel_path_configured_works(tmp_path: Path) -> None:
    """When ``shutdown_sentinel_path=None`` (default in tests that don't
    care about it), tick must not crash and the stop event must not fire.
    """
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    gh = _StubGHClient(_gh_with([], []))
    host = _StubHost()

    reconciler = Reconciler(
        projects=(
            ReconcilerProject(name="foreman", owner="jeffrichley", repo="foreman"),
        ),
        log=log,
        gh=gh,
        host=host,
        dry_run=False,
        # shutdown_sentinel_path explicitly omitted → defaults to None.
    )

    await reconciler.tick()
    assert not reconciler._stop_event.is_set()


# --- Sentinel-file config reload (foreman#100) ---
#
# The reconciler polls a configured reload sentinel file at the TOP of
# each tick (before the per-project loop). When present, it consumes
# the sentinel, calls the reload callback to obtain a fresh project
# tuple, diffs against current, and updates ``self.projects``. Newly-
# added projects are reconciled in the SAME tick. The callback raising
# (e.g., TOML parse error during operator edit) is non-fatal — the
# daemon keeps running with the prior project set and the operator's
# next reload command is the canonical retry.


@pytest.mark.asyncio
async def test_reconciler_reload_adds_project_on_next_tick(tmp_path: Path) -> None:
    """A reload that adds a project: ``self.projects`` carries the
    addition AND the failure counter is initialized for it AND the
    sentinel is consumed."""
    sentinel = tmp_path / "reload-requested"
    sentinel.write_text("requested by test", encoding="utf-8")

    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    gh = _StubGHClient(_gh_with([], []))
    host = _StubHost()

    initial = (
        ReconcilerProject(name="foreman", owner="jeffrichley", repo="foreman"),
    )
    expanded = (
        ReconcilerProject(name="foreman", owner="jeffrichley", repo="foreman"),
        ReconcilerProject(name="voice", owner="jeffrichley", repo="voice"),
    )

    def _callback() -> tuple[ReconcilerProject, ...]:
        return expanded

    reconciler = Reconciler(
        projects=initial,
        log=log,
        gh=gh,
        host=host,
        dry_run=False,
        reload_callback=_callback,
        reload_sentinel_path=sentinel,
    )

    assert sentinel.exists()
    await reconciler.tick()

    assert not sentinel.exists(), "sentinel must be consumed after reload"
    names = sorted(p.name for p in reconciler.projects)
    assert names == ["foreman", "voice"]
    assert "voice" in reconciler._consecutive_failures
    assert reconciler._consecutive_failures["voice"] == 0
    assert "foreman" in reconciler._consecutive_failures


@pytest.mark.asyncio
async def test_reconciler_reload_removes_project_and_cleans_failures(
    tmp_path: Path,
) -> None:
    """A reload that removes a project: the removed project is dropped
    from ``self.projects`` AND its failure counter is deleted."""
    sentinel = tmp_path / "reload-requested"
    sentinel.write_text("requested by test", encoding="utf-8")

    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    gh = _StubGHClient(_gh_with([], []))
    host = _StubHost()

    initial = (
        ReconcilerProject(name="surviving", owner="o", repo="surviving"),
        ReconcilerProject(name="going-away", owner="o", repo="going-away"),
    )

    def _callback() -> tuple[ReconcilerProject, ...]:
        return (ReconcilerProject(name="surviving", owner="o", repo="surviving"),)

    reconciler = Reconciler(
        projects=initial,
        log=log,
        gh=gh,
        host=host,
        dry_run=False,
        reload_callback=_callback,
        reload_sentinel_path=sentinel,
    )
    # Seed a failure counter on the project that's about to be removed —
    # the reload must clean it up to avoid leaking stale state.
    reconciler._consecutive_failures["going-away"] = 2

    await reconciler.tick()

    names = [p.name for p in reconciler.projects]
    assert names == ["surviving"]
    assert "going-away" not in reconciler._consecutive_failures
    assert "surviving" in reconciler._consecutive_failures


@pytest.mark.asyncio
async def test_reconciler_reload_idempotent_when_unchanged_logs_info(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A reload that returns the same project tuple: logs INFO
    "config reload: no changes" AND writes no exec_log row."""
    import logging as _logging

    sentinel = tmp_path / "reload-requested"
    sentinel.write_text("requested by test", encoding="utf-8")

    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    gh = _StubGHClient(_gh_with([], []))
    host = _StubHost()

    initial = (
        ReconcilerProject(name="foreman", owner="jeffrichley", repo="foreman"),
    )

    def _callback() -> tuple[ReconcilerProject, ...]:
        # Same name, owner, repo, and resolved flags — no change.
        return (
            ReconcilerProject(name="foreman", owner="jeffrichley", repo="foreman"),
        )

    reconciler = Reconciler(
        projects=initial,
        log=log,
        gh=gh,
        host=host,
        dry_run=False,
        reload_callback=_callback,
        reload_sentinel_path=sentinel,
    )

    with caplog.at_level(_logging.INFO, logger="foreman.reconciler.daemon"):
        await reconciler.tick()

    assert any(
        "config reload: no changes" in record.getMessage() for record in caplog.records
    )
    # No exec_log row should have been written for the no-change case.
    import sqlite3

    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM execution_log WHERE action='config_reload'"
        ).fetchone()
    assert rows[0] == 0


@pytest.mark.asyncio
async def test_reconciler_reload_failure_keeps_running_and_logs(
    tmp_path: Path,
) -> None:
    """A reload callback that raises (e.g., TOML parse error) must NOT
    crash the daemon. The sentinel is consumed, an exec_log row is
    written with outcome='failed', and ``self.projects`` is unchanged."""
    sentinel = tmp_path / "reload-requested"
    sentinel.write_text("requested by test", encoding="utf-8")

    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    gh = _StubGHClient(_gh_with([], []))
    host = _StubHost()

    initial = (
        ReconcilerProject(name="foreman", owner="jeffrichley", repo="foreman"),
    )

    def _broken_callback() -> tuple[ReconcilerProject, ...]:
        raise ValueError("config.toml parse error: unexpected token at line 12")

    reconciler = Reconciler(
        projects=initial,
        log=log,
        gh=gh,
        host=host,
        dry_run=False,
        reload_callback=_broken_callback,
        reload_sentinel_path=sentinel,
    )

    await reconciler.tick()

    # Sentinel must be consumed regardless of success/failure.
    assert not sentinel.exists()
    # Projects unchanged.
    assert reconciler.projects == initial
    # Daemon NOT stopped.
    assert not reconciler._stop_event.is_set()
    # exec_log row written with outcome='failed' and the error details.
    import sqlite3

    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        rows = conn.execute(
            "SELECT outcome, details FROM execution_log WHERE action='config_reload'"
        ).fetchall()
    assert len(rows) == 1
    outcome, details_json = rows[0]
    assert outcome == "failed"
    import json as _json

    details = _json.loads(details_json)
    assert details["error_class"] == "ValueError"
    assert "parse error" in details["error_message"]


@pytest.mark.asyncio
async def test_reconciler_reload_inert_when_no_callback(tmp_path: Path) -> None:
    """When ``reload_callback=None`` (test default), the reload mechanism
    is inert: ``tick()`` runs cleanly and no AttributeError surfaces
    even if a sentinel-shaped file exists on disk where the path WOULD
    point. Construct without either kwarg so the path-only-no-callback
    case is also exercised — the present-check short-circuits on
    ``self._reload_callback is None``."""
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    gh = _StubGHClient(_gh_with([], []))
    host = _StubHost()

    reconciler = Reconciler(
        projects=(
            ReconcilerProject(name="foreman", owner="jeffrichley", repo="foreman"),
        ),
        log=log,
        gh=gh,
        host=host,
        dry_run=False,
        # reload_callback + reload_sentinel_path both omitted → inert.
    )

    await reconciler.tick()  # Must not raise
    assert not reconciler._stop_event.is_set()
