"""Tests for V3GitHubHost — wraps v2 REST + adds subprocess dispatch_role."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.v3_host import (
    V3GitHubHost,
    _build_role_subprocess_env,
    _default_subprocess_runner,
)


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
        project="foreman",
    )

    assert pid == 98765
    assert len(captured_argv) == 1
    argv = captured_argv[0]
    # CLI subcommand for planner role is "plan"
    assert "plan" in argv
    # `foreman plan` takes positional ISSUE_URL — no --issue-url flag.
    assert "--issue-url" not in argv
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
            project="foreman",
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
            project="foreman",
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
            project="foreman",
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
            project="foreman",
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
        project="foreman",
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
        project="foreman",
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


def test_dispatch_role_planner_uses_positional_issue_url(tmp_path: Path) -> None:
    """`foreman plan` CLI takes positional ISSUE_URL — no --issue-url flag.

    Regression test for the first autonomous-dogfood failure after PR #113/#114
    merged: Planner subprocesses exited returncode 2 (Click usage error)
    because v3_host built argv with --issue-url, which `foreman plan` does
    not accept.
    """
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
        ticket_id="jeffrichley/foreman#100",
        project="foreman",
        rule_name="dispatch_planner",
        action="dispatch_planner",
        outcome="running",
        details={},
    )
    host.dispatch_role(
        role="planner",
        target=None,
        owner="jeffrichley",
        repo="foreman",
        issue=100,
        pr_number=None,
        start_log_id=start_id,
        project="foreman",
    )

    assert captured, "runner not called"
    argv = captured[0]
    assert "plan" in argv
    # The bug we're fixing: --issue-url is not a valid flag for `foreman plan`.
    assert "--issue-url" not in argv
    # The URL must be present, positional.
    url = "https://github.com/jeffrichley/foreman/issues/100"
    assert url in argv
    # Verify positional: the URL is not immediately preceded by an unknown
    # option flag (only "plan" subcommand or "--project" value can precede).
    url_idx = argv.index(url)
    if url_idx > 0:
        prev = argv[url_idx - 1]
        assert prev == "plan" or prev == "--project" or not prev.startswith("--"), (
            f"URL preceded by flag {prev!r} — must be positional"
        )
    # Planner is not target-ambiguous — no --target should be emitted.
    assert "--target" not in argv
    # `foreman plan` does not accept --pr-url either.
    assert "--pr-url" not in argv


def test_dispatch_role_worker_uses_positional_issue_url(tmp_path: Path) -> None:
    """`foreman implement` CLI takes positional ISSUE_URL — no --issue-url flag.

    Same shape bug as the Planner: v3_host built argv with --issue-url, which
    `foreman implement` does not accept. Worker also opens its own PR, so it
    must not receive --pr-url either.
    """
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
        ticket_id="jeffrichley/foreman#101",
        project="foreman",
        rule_name="dispatch_worker",
        action="dispatch_worker",
        outcome="running",
        details={},
    )
    host.dispatch_role(
        role="worker",
        target=None,
        owner="jeffrichley",
        repo="foreman",
        issue=101,
        pr_number=None,
        start_log_id=start_id,
        project="foreman",
    )

    assert captured, "runner not called"
    argv = captured[0]
    # CLI subcommand for worker role is "implement"
    assert "implement" in argv
    # The bug we're fixing: --issue-url is not a valid flag for `foreman implement`.
    assert "--issue-url" not in argv
    # The URL must be present, positional.
    url = "https://github.com/jeffrichley/foreman/issues/101"
    assert url in argv
    url_idx = argv.index(url)
    if url_idx > 0:
        prev = argv[url_idx - 1]
        assert prev == "implement" or prev == "--project" or not prev.startswith("--"), (
            f"URL preceded by flag {prev!r} — must be positional"
        )
    # Worker is not target-ambiguous — no --target should be emitted.
    assert "--target" not in argv
    # `foreman implement` does not accept --pr-url either.
    assert "--pr-url" not in argv


# ----------------------------------------------------------------------
# foreman#119 — per-dispatch subprocess output capture
# ----------------------------------------------------------------------


def _read_termination_details(db_path: Path, action: str) -> dict[str, Any]:
    """Pull the `details` JSON off the most recent terminated row for
    ``action`` from the execution log so the test can assert on
    log_path. The exec_log table stores details as a JSON string."""
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT details FROM execution_log "
            "WHERE action = ? AND outcome != 'running' "
            "ORDER BY id DESC LIMIT 1",
            (action,),
        ).fetchone()
    assert row is not None, f"no terminated row for action={action!r}"
    return json.loads(row[0])  # type: ignore[no-any-return]


def test_dispatch_role_records_log_path_in_details_when_log_dir_set(
    tmp_path: Path,
) -> None:
    """foreman#119: when ``log_dir`` is configured at construction, every
    dispatch_role termination row carries a ``log_path`` entry in
    ``details`` so post-mortem is ``cat <path>`` without grepping the
    daemon log to find which file to read."""
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    log_dir = tmp_path / "dispatch-logs"
    captured_log_paths: list[Path] = []

    class _FakeProc:
        def __init__(self, pid: int, log_path: Path) -> None:
            self.pid = pid
            self.log_path = log_path

        async def wait(self) -> int:
            return 0

    def runner(argv: list[str], *, log_path: Path | None = None) -> _FakeProc:
        assert log_path is not None, "host should pass log_path when log_dir set"
        captured_log_paths.append(log_path)
        return _FakeProc(pid=4242, log_path=log_path)

    host = V3GitHubHost(
        v2_host=_FakeV2Host(),
        log=log,
        subprocess_runner=runner,
        log_dir=log_dir,
    )

    start_id = log.write_action(
        ticket_id="foreman/owner/repo#119",
        project="foreman",
        rule_name="dispatch_planner",
        action="dispatch_planner",
        outcome="running",
        details={},
    )

    async def run() -> None:
        host.dispatch_role(
            role="planner",
            target=None,
            owner="owner",
            repo="repo",
            issue=119,
            pr_number=None,
            start_log_id=start_id,
            project="foreman",
        )
        for _ in range(100):
            if log.count_completed("dispatch_planner", "foreman/owner/repo#119") > 0:
                return
            await asyncio.sleep(0.01)

    asyncio.run(run())

    assert len(captured_log_paths) == 1
    expected_log_path = captured_log_paths[0]
    # Path lives under <log_dir>/<role>/ — issue and role both surface
    # in the path components so operators can ls the role's directory.
    assert expected_log_path.parent == log_dir / "planner"
    assert expected_log_path.name.startswith("119__")
    assert expected_log_path.suffix == ".log"

    details = _read_termination_details(tmp_path / "log.sqlite", "dispatch_planner")
    assert details["log_path"] == str(expected_log_path)
    assert details["returncode"] == 0
    assert details["role"] == "planner"


def test_dispatch_role_omits_log_path_when_log_dir_not_set(tmp_path: Path) -> None:
    """When ``log_dir`` is ``None`` (the test default), the termination
    row's ``details`` must NOT carry a ``log_path`` key — the existing
    no-capture path is preserved verbatim."""
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    captured_kwargs: list[dict[str, Any]] = []

    class _FakeProc:
        pid = 4242

        async def wait(self) -> int:
            return 0

    def runner(argv: list[str], **kwargs: Any) -> _FakeProc:
        captured_kwargs.append(kwargs)
        return _FakeProc()

    host = V3GitHubHost(
        v2_host=_FakeV2Host(),
        log=log,
        subprocess_runner=runner,
        # NB: no log_dir — default None
    )

    start_id = log.write_action(
        ticket_id="foreman/owner/repo#119",
        project="foreman",
        rule_name="dispatch_planner",
        action="dispatch_planner",
        outcome="running",
        details={},
    )

    async def run() -> None:
        host.dispatch_role(
            role="planner",
            target=None,
            owner="owner",
            repo="repo",
            issue=119,
            pr_number=None,
            start_log_id=start_id,
            project="foreman",
        )
        for _ in range(100):
            if log.count_completed("dispatch_planner", "foreman/owner/repo#119") > 0:
                return
            await asyncio.sleep(0.01)

    asyncio.run(run())

    # Runner was called WITHOUT log_path kwarg (only positional argv).
    assert captured_kwargs == [{}]
    details = _read_termination_details(tmp_path / "log.sqlite", "dispatch_planner")
    assert "log_path" not in details
    assert details["returncode"] == 0


def test_default_runner_captures_subprocess_output_to_log_path(
    tmp_path: Path,
) -> None:
    """End-to-end: ``_default_subprocess_runner`` with a real subprocess
    writes the child's stdout AND stderr into the log file, interleaved
    like a terminal. The file is created if missing (parent dirs too)
    and the parent-side handle is closed after ``wait()`` so no FD leaks.
    """
    log_path = tmp_path / "nested" / "dir" / "out.log"
    argv = [
        sys.executable,
        "-c",
        "import sys; print('stdout-line'); print('stderr-line', file=sys.stderr); sys.exit(7)",
    ]

    proc = _default_subprocess_runner(argv, log_path=log_path)

    async def wait() -> int:
        return await proc.wait()

    returncode = asyncio.run(wait())

    assert returncode == 7
    assert log_path.exists()
    contents = log_path.read_text(encoding="utf-8", errors="replace")
    assert "stdout-line" in contents
    assert "stderr-line" in contents
    # Parent-side handle should be closed after wait() (no second handle
    # held; opening for write should succeed even on Windows which would
    # refuse if the file were still locked).
    log_path.unlink()  # would raise PermissionError if any handle stuck


def test_default_runner_without_log_path_uses_devnull(tmp_path: Path) -> None:
    """No-capture path: ``log_path=None`` (default) preserves the
    existing DEVNULL behavior — no file is created, subprocess output
    is dropped."""
    argv = [sys.executable, "-c", "print('would-go-to-devnull')"]
    proc = _default_subprocess_runner(argv)

    returncode = asyncio.run(proc.wait())
    assert returncode == 0
    # No log file written — nothing in tmp_path.
    assert not any(tmp_path.iterdir())


def test_dispatch_role_argv_does_not_use_uv_run(tmp_path: Path) -> None:
    """The container-runtime daemon dispatches via PATH-resolved `foreman`,
    NOT `uv run foreman`. The `uv run` form re-syncs the editable install
    on every invocation, which fails on Windows when the daemon's own
    `foreman.exe` is file-locked (the failure mode this whole design
    eliminates)."""
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    captured_argv: list[list[str]] = []

    class _FakeProc:
        pid = 4242

        async def wait(self) -> int:
            return 0

    def runner(argv: list[str], **kwargs: Any) -> _FakeProc:
        captured_argv.append(argv)
        return _FakeProc()

    host = V3GitHubHost(v2_host=_FakeV2Host(), log=log, subprocess_runner=runner)
    start_id = log.write_action(
        ticket_id="foreman/owner/repo#143",
        project="foreman",
        rule_name="dispatch_planner",
        action="dispatch_planner",
        outcome="running",
        details={},
    )
    host.dispatch_role(
        role="planner",
        target=None,
        owner="owner",
        repo="repo",
        issue=143,
        pr_number=None,
        start_log_id=start_id,
        project="foreman",
    )
    assert len(captured_argv) == 1
    argv = captured_argv[0]
    assert argv[0] == "foreman", f"expected argv[0]='foreman', got {argv[0]!r}"
    assert "uv" not in argv[:2], (
        f"`uv run` wrapper still in argv — this re-introduces the Windows "
        f"file-lock bug the Docker runtime exists to eliminate. argv={argv}"
    )


def test_foreman_log_dir_env_var_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """FOREMAN_LOG_DIR overrides the default host fallback."""
    monkeypatch.setenv("FOREMAN_LOG_DIR", "/custom/path/foreman/logs")
    from foreman.reconciler.v3_host import resolve_log_dir
    assert resolve_log_dir() == Path("/custom/path/foreman/logs")


def test_foreman_log_dir_falls_back_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """When FOREMAN_LOG_DIR is unset, fall back to ~/.foreman/logs.
    Keeps `foreman daemon v3-start` invokable on the host for ad-hoc
    debug without containerization."""
    monkeypatch.delenv("FOREMAN_LOG_DIR", raising=False)
    from foreman.reconciler.v3_host import resolve_log_dir
    assert resolve_log_dir() == Path.home() / ".foreman" / "logs"


def test_foreman_state_dir_env_var_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """FOREMAN_STATE_DIR overrides the default host fallback. Used in the
    container to point the daemon at the foreman-state named volume so
    SQLite + sentinels + v3-daemon.log survive `docker compose down`."""
    monkeypatch.setenv("FOREMAN_STATE_DIR", "/foreman/state")
    from foreman.reconciler.v3_host import resolve_state_dir
    assert resolve_state_dir() == Path("/foreman/state")


def test_foreman_state_dir_falls_back_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """When FOREMAN_STATE_DIR is unset, fall back to ~/.foreman (the host
    directory holding config.toml, daemon.lock, etc.)."""
    monkeypatch.delenv("FOREMAN_STATE_DIR", raising=False)
    from foreman.reconciler.v3_host import resolve_state_dir
    assert resolve_state_dir() == Path.home() / ".foreman"


# ---------------------------------------------------------------------------
# Role-subprocess env-scrub tests
# ---------------------------------------------------------------------------
#
# These guard the daemon-side leg of the 2026-06-06 fix that closed the
# autonomous-loop runtime-shutdown bug. The runtime daemon detected a
# /root/.foreman/shutdown-requested sentinel it never wrote because a role
# subprocess's pytest-or-CLI invocation had inherited the daemon's env and
# resolved its own sentinel-write path to the prod location. The scrub
# rewrites those env paths so role subprocesses can never reach the
# daemon's polled state — even when the role tool stack is unaware of
# the constraint. See _build_role_subprocess_env for the full rationale.


def test_build_role_subprocess_env_rewrites_sentinel_and_lock_paths() -> None:
    """The three load-bearing env vars are rewritten to noop paths so a
    role subprocess that calls ``foreman daemon stop`` never reaches the
    daemon's polled sentinel or interrogates the daemon's lock.
    """
    base = {"FOREMAN_CONFIG_PATH": "/etc/foreman/config.toml", "HOME": "/root"}
    env = _build_role_subprocess_env(base)
    assert env["FOREMAN_SHUTDOWN_SENTINEL_PATH"].endswith("noop-shutdown-sentinel")
    assert env["FOREMAN_RELOAD_SENTINEL_PATH"].endswith("noop-reload-sentinel")
    assert env["FOREMAN_LOCK_PATH"].endswith("noop-lock")
    # The role-noop paths must NOT collide with the prod paths a
    # daemon container would use.
    assert "/root/.foreman" not in env["FOREMAN_SHUTDOWN_SENTINEL_PATH"]
    assert "/foreman/state" not in env["FOREMAN_LOCK_PATH"]


def test_build_role_subprocess_env_drops_state_and_log_dir() -> None:
    """The daemon's state and log dir env vars are NOT inherited by the
    role subprocess. Inheriting them would let a role's pytest session
    resolve config-default sentinel paths to the prod state dir.
    """
    base = {
        "FOREMAN_STATE_DIR": "/foreman/state",
        "FOREMAN_LOG_DIR": "/foreman/logs",
        "FOREMAN_CONFIG_PATH": "/etc/foreman/config.toml",
        "HOME": "/root",
    }
    env = _build_role_subprocess_env(base)
    assert "FOREMAN_STATE_DIR" not in env
    assert "FOREMAN_LOG_DIR" not in env


def test_build_role_subprocess_env_preserves_config_path_and_home() -> None:
    """FOREMAN_CONFIG_PATH stays — the role needs project config.
    HOME stays — claude_agent_sdk needs ``~/.claude/.credentials.json``.
    Both are load-bearing for the role's legitimate work; scrubbing
    them would break dispatch.
    """
    base = {
        "FOREMAN_CONFIG_PATH": "/etc/foreman/config.toml",
        "HOME": "/root",
        "PATH": "/usr/local/bin:/usr/bin",
        "GH_TOKEN": "ghp_test_token_value",
    }
    env = _build_role_subprocess_env(base)
    assert env["FOREMAN_CONFIG_PATH"] == "/etc/foreman/config.toml"
    assert env["HOME"] == "/root"
    # Other inherited env (PATH, credentials) pass through untouched.
    assert env["PATH"] == "/usr/local/bin:/usr/bin"
    assert env["GH_TOKEN"] == "ghp_test_token_value"


def test_build_role_subprocess_env_does_not_mutate_base_env() -> None:
    """The helper must return a new dict, not mutate the caller's env.
    A subtle aliasing bug here would mean the daemon's own os.environ
    gets the noop sentinel paths applied — defeating the daemon's
    ability to poll its real sentinel.
    """
    base = {
        "FOREMAN_CONFIG_PATH": "/etc/foreman/config.toml",
        "FOREMAN_STATE_DIR": "/foreman/state",
        "HOME": "/root",
    }
    base_snapshot = dict(base)
    _build_role_subprocess_env(base)
    assert base == base_snapshot, (
        "Helper mutated its base_env argument — the daemon's own env "
        "would be corrupted if base_env is os.environ."
    )
