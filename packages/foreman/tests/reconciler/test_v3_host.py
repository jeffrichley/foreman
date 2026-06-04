"""Tests for V3GitHubHost — wraps v2 REST + adds subprocess dispatch_role."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.v3_host import V3GitHubHost


@dataclass
class _FakeV2Host:
    """Stands in for v2's GitHubDaemonHost — records calls."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def add_issue_label(self, *, owner: str, repo: str, issue_number: int, label: str) -> None:
        self.calls.append(("add_issue_label", {"owner": owner, "repo": repo, "issue_number": issue_number, "label": label}))

    def remove_issue_label(self, *, owner: str, repo: str, issue_number: int, label: str) -> None:
        self.calls.append(("remove_issue_label", {"owner": owner, "repo": repo, "issue_number": issue_number, "label": label}))

    def post_issue_comment(self, *, owner: str, repo: str, issue_number: int, body: str) -> None:
        self.calls.append(("post_issue_comment", {"owner": owner, "repo": repo, "issue_number": issue_number, "body": body}))

    def merge_pull_request(self, *, owner: str, repo: str, pr_number: int) -> None:
        self.calls.append(("merge_pull_request", {"owner": owner, "repo": repo, "pr_number": pr_number}))


def test_add_label_delegates_to_v2_host(tmp_path: Path) -> None:
    v2 = _FakeV2Host()
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    host = V3GitHubHost(v2_host=v2, log=log, subprocess_runner=None)

    host.add_label(owner="jeffrichley", repo="foreman", issue=143, label="foreman:planning")

    assert v2.calls == [
        ("add_issue_label", {"owner": "jeffrichley", "repo": "foreman", "issue_number": 143, "label": "foreman:planning"})
    ]


def test_remove_label_delegates_to_v2_host(tmp_path: Path) -> None:
    v2 = _FakeV2Host()
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    host = V3GitHubHost(v2_host=v2, log=log, subprocess_runner=None)

    host.remove_label(owner="jeffrichley", repo="foreman", issue=143, label="foreman:planning")

    assert v2.calls == [
        ("remove_issue_label", {"owner": "jeffrichley", "repo": "foreman", "issue_number": 143, "label": "foreman:planning"})
    ]


def test_post_comment_delegates_to_v2_host(tmp_path: Path) -> None:
    v2 = _FakeV2Host()
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    host = V3GitHubHost(v2_host=v2, log=log, subprocess_runner=None)

    host.post_comment(owner="jeffrichley", repo="foreman", issue=143, body="hi")

    assert v2.calls == [
        ("post_issue_comment", {"owner": "jeffrichley", "repo": "foreman", "issue_number": 143, "body": "hi"})
    ]


def test_merge_pr_delegates_to_v2_host(tmp_path: Path) -> None:
    v2 = _FakeV2Host()
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    host = V3GitHubHost(v2_host=v2, log=log, subprocess_runner=None)

    host.merge_pr(owner="jeffrichley", repo="foreman", pr_number=144)

    assert v2.calls == [
        ("merge_pull_request", {"owner": "jeffrichley", "repo": "foreman", "pr_number": 144})
    ]


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
    # Pre-create a start row so the background termination task has a parent_log_id to terminate.
    start_id = log.write_action(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        rule_name="dispatch_planner",
        action="dispatch_planner",
        outcome="running",
        details={},
    )
    host._pending_start_log_id_by_pid[98765] = start_id  # type: ignore[attr-defined]

    pid = host.dispatch_role(
        role="planner", owner="jeffrichley", repo="foreman", issue=143, pr_number=None
    )

    assert pid == 98765
    assert len(captured_argv) == 1
    argv = captured_argv[0]
    # CLI subcommand for planner role is "plan"
    assert "plan" in argv
    assert "--issue-url" in argv
    assert "https://github.com/jeffrichley/foreman/issues/143" in argv
