"""Tests for V3GitHubHost — wraps v2 REST + adds subprocess dispatch_role."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.v3_host import V3GitHubHost


@dataclass
class _FakeV2Host:
    """Stands in for v2's GitHubDaemonHost — records calls. Matches real positional signatures."""

    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def add_issue_label(self, repo: str, issue_number: int, label: str) -> None:
        self.calls.append(("add_issue_label", (repo, issue_number, label)))

    def remove_issue_label(self, repo: str, issue_number: int, label: str) -> None:
        self.calls.append(("remove_issue_label", (repo, issue_number, label)))

    def post_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
        self.calls.append(("post_issue_comment", (repo, issue_number, body)))

    def merge_pull_request(self, repo: str, pr_number: int) -> None:
        self.calls.append(("merge_pull_request", (repo, pr_number)))


def test_add_label_delegates_to_v2_host(tmp_path: Path) -> None:
    v2 = _FakeV2Host()
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    host = V3GitHubHost(v2_host=v2, log=log, subprocess_runner=None)

    host.add_label(owner="jeffrichley", repo="foreman", issue=143, label="foreman:planning")

    assert v2.calls == [("add_issue_label", ("jeffrichley/foreman", 143, "foreman:planning"))]


def test_remove_label_delegates_to_v2_host(tmp_path: Path) -> None:
    v2 = _FakeV2Host()
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    host = V3GitHubHost(v2_host=v2, log=log, subprocess_runner=None)

    host.remove_label(owner="jeffrichley", repo="foreman", issue=143, label="foreman:planning")

    assert v2.calls == [("remove_issue_label", ("jeffrichley/foreman", 143, "foreman:planning"))]


def test_post_comment_delegates_to_v2_host(tmp_path: Path) -> None:
    v2 = _FakeV2Host()
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    host = V3GitHubHost(v2_host=v2, log=log, subprocess_runner=None)

    host.post_comment(owner="jeffrichley", repo="foreman", issue=143, body="hi")

    assert v2.calls == [("post_issue_comment", ("jeffrichley/foreman", 143, "hi"))]


def test_merge_pr_delegates_to_v2_host(tmp_path: Path) -> None:
    v2 = _FakeV2Host()
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    host = V3GitHubHost(v2_host=v2, log=log, subprocess_runner=None)

    host.merge_pr(owner="jeffrichley", repo="foreman", pr_number=144)

    assert v2.calls == [("merge_pull_request", ("jeffrichley/foreman", 144))]


def test_dispatch_role_spawns_subprocess_and_returns_pid(tmp_path: Path) -> None:
    v2 = _FakeV2Host()
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    captured_argv: list[list[str]] = []

    class _FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.returncode: int | None = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    def _runner(argv: list[str]) -> _FakeProcess:
        captured_argv.append(argv)
        return _FakeProcess(pid=98765)

    host = V3GitHubHost(v2_host=v2, log=log, subprocess_runner=_runner)
    # Executor writes the start row before calling dispatch_role; we mirror
    # that contract here so terminate_dispatch has a parent_log_id to point at.
    start_id = log.write_action(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        rule_name="dispatch_planner",
        action="dispatch_planner",
        outcome="running",
        details={},
    )

    pid = host.dispatch_role(
        role="planner",
        target=None,
        owner="jeffrichley",
        repo="foreman",
        issue=143,
        pr_number=None,
        start_log_id=start_id,
    )

    assert pid == 98765
    assert len(captured_argv) == 1
    argv = captured_argv[0]
    # CLI subcommand for planner role is "plan"
    assert "plan" in argv
    assert "--issue-url" in argv
    assert "https://github.com/jeffrichley/foreman/issues/143" in argv
    # Planner is not target-ambiguous — no --target should be emitted.
    assert "--target" not in argv


def test_dispatch_role_writes_termination_row_on_success(tmp_path: Path) -> None:
    """Production lifecycle: start row written by executor, dispatch_role
    returns pid, subprocess exits, termination row is written automatically by
    the host's background tracking task — no dictionary indirection required.
    """
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    class _FakeProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        async def wait(self) -> int:
            return 0

    def runner(argv: list[str]) -> _FakeProc:
        return _FakeProc(pid=12345)

    host = V3GitHubHost(
        v2_host=_FakeV2Host(),
        log=log,
        subprocess_runner=runner,
    )

    # Executor writes start row first.
    start_id = log.write_action(
        ticket_id="foreman/owner/repo#10",
        project="foreman",
        rule_name="dispatch_planner",
        action="dispatch_planner",
        outcome="running",
        details={"issue": 10, "pr": None},
    )

    import asyncio

    async def run() -> None:
        host.dispatch_role(
            role="planner",
            target=None,
            owner="owner",
            repo="repo",
            issue=10,
            pr_number=None,
            start_log_id=start_id,
        )
        # Yield until the background task finishes. Bounded poll so a
        # regression doesn't hang the suite indefinitely.
        for _ in range(100):
            if log.count_completed("dispatch_planner", "foreman/owner/repo#10") > 0:
                return
            await asyncio.sleep(0.01)

    asyncio.run(run())

    assert log.count_completed("dispatch_planner", "foreman/owner/repo#10") == 1


def test_terminate_dispatch_writes_termination_row_synchronously(tmp_path: Path) -> None:
    """Tests that drive the host without a running asyncio loop can call
    terminate_dispatch directly to write the termination row.
    """
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    host = V3GitHubHost(
        v2_host=_FakeV2Host(),
        log=log,
        subprocess_runner=lambda argv: object(),  # type: ignore[arg-type, return-value]
    )

    start_id = log.write_action(
        ticket_id="foreman/owner/repo#10",
        project="foreman",
        rule_name="dispatch_reviewer_spec",
        action="dispatch_reviewer",
        outcome="running",
        details={"issue": 10, "pr": 99},
    )

    host.terminate_dispatch(
        start_log_id=start_id, outcome="success", details={"role": "reviewer"}
    )
    assert log.count_completed("dispatch_reviewer", "foreman/owner/repo#10") == 1


def test_concurrency_cap_refuses_dispatch_when_full(tmp_path: Path) -> None:
    """Once max_concurrent_dispatches is reached, further dispatch_role raises."""
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    class _FakeProc:
        pid = 42

        async def wait(self) -> int:
            return 0

    host = V3GitHubHost(
        v2_host=_FakeV2Host(),
        log=log,
        subprocess_runner=lambda _argv: _FakeProc(),
        max_concurrent_dispatches=1,
    )
    # Manually acquire the only slot to simulate "already full"
    host._dispatch_capacity.acquire(blocking=False)

    # Next dispatch_role should raise — caught by executor in production
    with pytest.raises(RuntimeError, match="concurrency cap reached"):
        host.dispatch_role(
            role="planner",
            target=None,
            owner="jeffrichley",
            repo="foreman",
            issue=1,
            pr_number=None,
            start_log_id=1,
        )


def test_dispatch_role_releases_slot_when_runner_raises(tmp_path: Path) -> None:
    """If subprocess_runner raises (uv missing, fork failure), the capacity
    slot must be released — otherwise repeated spawn failures permanently
    consume the cap until daemon restart."""
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    def raising_runner(_argv: list[str]) -> object:
        raise FileNotFoundError("uv: command not found")

    host = V3GitHubHost(
        v2_host=_FakeV2Host(),
        log=log,
        subprocess_runner=raising_runner,
        max_concurrent_dispatches=1,
    )

    # First call: runner raises → slot must be released.
    with pytest.raises(FileNotFoundError):
        host.dispatch_role(
            role="planner",
            target=None,
            owner="jeffrichley",
            repo="foreman",
            issue=1,
            pr_number=None,
            start_log_id=1,
        )

    # Second call: with slot properly released, would-be-cap-reached should NOT fire.
    # Runner still raises FileNotFoundError, not "concurrency cap reached".
    with pytest.raises(FileNotFoundError):
        host.dispatch_role(
            role="planner",
            target=None,
            owner="jeffrichley",
            repo="foreman",
            issue=2,
            pr_number=None,
            start_log_id=2,
        )


def test_dispatch_role_reviewer_uses_positional_pr_url(tmp_path: Path) -> None:
    """Reviewer's `foreman review` CLI takes positional pr_url, not --issue-url."""
    captured: list[list[str]] = []

    class _FakeProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        async def wait(self) -> int:
            return 0

    def runner(argv: list[str]) -> _FakeProc:
        captured.append(argv)
        return _FakeProc(pid=42)

    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    host = V3GitHubHost(
        v2_host=_FakeV2Host(),
        log=log,
        subprocess_runner=runner,
    )

    start_id = log.write_action(
        ticket_id="jeffrichley/foreman#63",
        project="foreman",
        rule_name="dispatch_reviewer_impl",
        action="dispatch_reviewer",
        outcome="running",
        details={},
    )
    host.dispatch_role(
        role="reviewer",
        target="impl_pr",
        owner="jeffrichley",
        repo="foreman",
        issue=63,
        pr_number=99,
        start_log_id=start_id,
    )

    assert captured, "runner not called"
    argv = captured[0]
    assert "review" in argv
    assert "--issue-url" not in argv
    assert "https://github.com/jeffrichley/foreman/pull/99" in argv
    # Stage-2 split: Reviewer dispatch now carries --target so the CLI
    # / observers see the dispatch shape explicitly.
    assert "--target" in argv
    assert "impl_pr" in argv


def test_dispatch_role_fixer_impl_passes_target_argv(tmp_path: Path) -> None:
    """CRITICAL #4 regression: ``foreman fix`` dispatched for the impl-fix
    flow must carry ``--target impl_pr`` so the role doesn't default to
    ``spec_pr`` and reject the issue on the entry-label check."""
    captured: list[list[str]] = []

    class _FakeProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        async def wait(self) -> int:
            return 0

    def runner(argv: list[str]) -> _FakeProc:
        captured.append(argv)
        return _FakeProc(pid=43)

    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    host = V3GitHubHost(
        v2_host=_FakeV2Host(),
        log=log,
        subprocess_runner=runner,
    )

    start_id = log.write_action(
        ticket_id="jeffrichley/foreman#63",
        project="foreman",
        rule_name="dispatch_fixer_impl",
        action="dispatch_fixer_impl",
        outcome="running",
        details={},
    )
    host.dispatch_role(
        role="fixer",
        target="impl_pr",
        owner="jeffrichley",
        repo="foreman",
        issue=63,
        pr_number=99,
        start_log_id=start_id,
    )

    assert captured, "runner not called"
    argv = captured[0]
    assert "fix" in argv
    assert "--issue-url" in argv
    assert "https://github.com/jeffrichley/foreman/issues/63" in argv
    assert "--pr-url" in argv
    assert "https://github.com/jeffrichley/foreman/pull/99" in argv
    assert "--target" in argv
    assert "impl_pr" in argv
