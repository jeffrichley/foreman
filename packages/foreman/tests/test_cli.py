"""CLI smoke tests via click's testing harness."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import UTC
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from foreman.cli import cli
from foreman.git_host import PRRef
from foreman.init import BotVerification, InitResult
from foreman.schemas.fixer import (
    AddressedFinding,
    FixerOutput,
    FixerRunResult,
    UnaddressedFinding,
)
from foreman.schemas.planner import PlannerOutput, PlannerRunResult
from foreman.schemas.reviewer import Finding, ReviewerOutput, ReviewerRunResult
from foreman.schemas.worker import (
    ImplementedSubRequest,
    SkippedSubRequest,
    WorkerOutput,
    WorkerRunResult,
)


def test_cli_plan_invokes_run_planner(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
planner_app_id_env = "FOREMAN_PLANNER_APP_ID"
planner_app_id = 123456
planner_private_key_path = "/tmp/planner.pem"
"""
    )

    fake_result = PlannerRunResult(
        llm_output=PlannerOutput(
            spec_doc_content="# Spec",
            pr_title="spec: x",
            pr_body="body",
            summary="ok",
            considered_alternatives=[],
            confidence="medium",
        ),
        pr=PRRef(
            number=99,
            url="https://github.com/jeffrichley/voice/pull/99",
            title="spec: x",
            body="body",
            branch="foreman/issue-42",
            base_branch="main",
            repo_slug="jeffrichley/voice",
        ),
        final_labels=["foreman:spec-review"],
    )

    runner = CliRunner()
    with patch("foreman.cli.run_planner", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = runner.invoke(
            cli,
            [
                "plan",
                "https://github.com/jeffrichley/voice/issues/42",
                "--project",
                "voice",
                "--config",
                str(config_file),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "PR #99" in result.output or "pull/99" in result.output
    assert "foreman/issue-42" in result.output
    mock_run.assert_called_once()


def test_cli_help_lists_plan_subcommand() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "plan" in result.output


def test_cli_help_lists_review_subcommand() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "review" in result.output


def test_cli_review_invokes_run_reviewer(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
planner_app_id_env = "FOREMAN_PLANNER_APP_ID"
planner_app_id = 123456
planner_private_key_path = "/tmp/planner.pem"
reviewer_app_id_env = "FOREMAN_REVIEWER_APP_ID"
reviewer_app_id = 654321
reviewer_private_key_path = "/tmp/reviewer.pem"
"""
    )

    fake_result = ReviewerRunResult(
        llm_output=ReviewerOutput(
            outcome="needs_fix",
            review_comment="needs_fix — see findings.",
            findings=[
                Finding(
                    severity="important",
                    target="Acceptance criteria bullet 3",
                    issue="Uses 'improve' which is not testable.",
                    needed="Replace with a concrete verb.",
                )
            ],
            confidence="medium",
        ),
        final_labels=["foreman:spec-fix"],
    )

    runner = CliRunner()
    with patch("foreman.cli.run_reviewer", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = runner.invoke(
            cli,
            [
                "review",
                "https://github.com/jeffrichley/voice/pull/77",
                "--project",
                "voice",
                "--config",
                str(config_file),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "needs_fix" in result.output
    assert "1 findings" in result.output
    assert "confidence=medium" in result.output
    mock_run.assert_called_once()


def test_cli_help_lists_fix_subcommand() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "fix" in result.output


def test_cli_fix_invokes_run_fixer(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
planner_app_id_env = "FOREMAN_PLANNER_APP_ID"
planner_app_id = 123456
planner_private_key_path = "/tmp/planner.pem"
fixer_app_id_env = "FOREMAN_FIXER_APP_ID"
fixer_app_id = 777777
fixer_private_key_path = "/tmp/fixer.pem"
"""
    )

    fake_result = FixerRunResult(
        llm_output=FixerOutput(
            outcome="fixed",
            fix_comment="fixed — addressed 2 findings.",
            commits_made=[],
            addressed_findings=[
                AddressedFinding(target="AC bullet 3", summary="x"),
                AddressedFinding(target="AC bullet 5", summary="y"),
            ],
            unaddressed_findings=[
                UnaddressedFinding(
                    target="minor-typo-bullet",
                    severity="minor",
                    reason="needs_info",
                    rationale="judgment call, leaving for human",
                ),
            ],
            confidence="high",
        ),
        attempt=2,
        final_labels=["foreman:spec-review"],
    )

    runner = CliRunner()
    with patch("foreman.cli.run_fixer", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = runner.invoke(
            cli,
            [
                "fix",
                "https://github.com/jeffrichley/voice/issues/42",
                "--project",
                "voice",
                "--config",
                str(config_file),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "fixed" in result.output
    assert "2/3 attempt" in result.output
    assert "2 fixed" in result.output
    assert "1 unaddressed" in result.output
    mock_run.assert_called_once()


def test_cli_help_lists_implement_subcommand() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "implement" in result.output


def test_cli_implement_invokes_run_worker(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
planner_app_id_env = "FOREMAN_PLANNER_APP_ID"
planner_app_id = 123456
planner_private_key_path = "/tmp/planner.pem"
worker_app_id_env = "FOREMAN_WORKER_APP_ID"
worker_app_id = 444444
worker_private_key_path = "/tmp/worker.pem"
"""
    )

    fake_result = WorkerRunResult(
        llm_output=WorkerOutput(
            outcome="implemented",
            work_comment="implemented all sub-requests.",
            pr_title="feat(foo): add X",
            pr_body="Implements #42.",
            commits_made=[],
            implemented_sub_requests=[
                ImplementedSubRequest(spec_reference="Sub-request 1", summary="x"),
                ImplementedSubRequest(spec_reference="Sub-request 2", summary="y"),
            ],
            skipped_sub_requests=[
                SkippedSubRequest(
                    spec_reference="Sub-request 3",
                    reason="out_of_scope",
                    rationale="issue did not request this",
                ),
            ],
            did_check_pass=True,
            confidence="high",
        ),
        attempt=1,
        pr_url="https://github.com/jeffrichley/voice/pull/101",
        final_did_check_pass=True,
        final_labels=["foreman:impl-review"],
    )

    runner = CliRunner()
    with patch("foreman.cli.run_worker", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = runner.invoke(
            cli,
            [
                "implement",
                "https://github.com/jeffrichley/voice/issues/42",
                "--project",
                "voice",
                "--config",
                str(config_file),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "implemented" in result.output
    assert "1/3 attempt" in result.output
    assert "2 implemented" in result.output
    assert "1 skipped" in result.output
    assert "did_check_pass=True" in result.output
    assert "PR=https://github.com/jeffrichley/voice/pull/101" in result.output
    mock_run.assert_called_once()


def test_cli_help_lists_init_subcommand() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "init" in result.output


def test_cli_init_invokes_run_init(tmp_path: Path, monkeypatch) -> None:
    """The ``foreman init`` CLI surface delegates to ``foreman.init.run_init``.

    We mock the underlying orchestrator so the CLI test stays a pure
    surface check: arg threading + summary echo.
    """
    monkeypatch.setenv("FOREMAN_ADMIN_TOKEN", "ghp_fake_admin_token")
    clone = tmp_path / "clone"
    clone.mkdir()
    config_file = tmp_path / "config.toml"

    fake_summary = "OK Foreman initialized for jeffrichley/foreman\n  ..."
    fake_result = InitResult(
        repo="jeffrichley/foreman",
        name="foreman",
        clone_path=clone,
        config_path=config_file,
        instructions_path=clone / ".foreman" / "INSTRUCTIONS.md",
        instructions_written=True,
        labels_created=["foreman:plan"],
        labels_existing=[],
        bot_verifications=[
            BotVerification(role="planner", ok=True, detail="OK"),
        ],
        summary=fake_summary,
    )

    runner = CliRunner()
    with (
        patch("foreman.cli.run_init", return_value=fake_result) as mock_run,
        patch("foreman.cli.Github", return_value=MagicMock()),
    ):
        result = runner.invoke(
            cli,
            [
                "init",
                "jeffrichley/foreman",
                "--name",
                "foreman",
                "--clone-path",
                str(clone),
                "--config",
                str(config_file),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "OK Foreman initialized" in result.output
    mock_run.assert_called_once()


def test_cli_init_requires_admin_token(tmp_path: Path, monkeypatch) -> None:
    """No admin token → ClickException explaining what's needed."""
    monkeypatch.delenv("FOREMAN_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    clone = tmp_path / "clone"
    clone.mkdir()

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "init",
            "jeffrichley/foreman",
            "--name",
            "foreman",
            "--clone-path",
            str(clone),
            "--config",
            str(tmp_path / "config.toml"),
        ],
    )

    assert result.exit_code != 0
    assert "admin GitHub token" in result.output


def test_cli_init_defaults_name_from_repo_tail(tmp_path: Path, monkeypatch) -> None:
    """When ``--name`` is omitted, the repo's tail is used."""
    monkeypatch.setenv("FOREMAN_ADMIN_TOKEN", "ghp_fake")
    clone = tmp_path / "clone"
    clone.mkdir()

    captured: dict[str, str] = {}

    def fake_run_init(init_config, *, admin_client):  # type: ignore[no-untyped-def]
        captured["name"] = init_config.name
        return InitResult(
            repo=init_config.repo,
            name=init_config.name,
            clone_path=init_config.clone_path,
            config_path=init_config.config_path,
            instructions_path=init_config.clone_path / ".foreman" / "INSTRUCTIONS.md",
            instructions_written=True,
            labels_created=[],
            labels_existing=[],
            bot_verifications=[],
            summary="OK",
        )

    runner = CliRunner()
    with (
        patch("foreman.cli.run_init", side_effect=fake_run_init),
        patch("foreman.cli.Github", return_value=MagicMock()),
    ):
        result = runner.invoke(
            cli,
            [
                "init",
                "jeffrichley/some-new-repo",
                "--clone-path",
                str(clone),
                "--config",
                str(tmp_path / "config.toml"),
            ],
        )

    assert result.exit_code == 0, result.output
    assert captured["name"] == "some-new-repo"


def test_daemon_status_when_not_running(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f'[daemon]\nlock_path = "{(tmp_path / "d.lock").as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    from click.testing import CliRunner

    from foreman.cli import cli

    result = CliRunner().invoke(cli, ["daemon", "status"])
    assert result.exit_code == 0
    assert "not running" in result.output.lower()


def test_daemon_start_foreground_runs_and_exits_clean(tmp_path: Path, monkeypatch) -> None:
    """Foreground daemon start respects --max-iterations test mode."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f"[daemon]\n"
        f'sqlite_path = "{(tmp_path / "f.sqlite").as_posix()}"\n'
        f'log_path = "{(tmp_path / "d.log").as_posix()}"\n'
        f'lock_path = "{(tmp_path / "d.lock").as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    from click.testing import CliRunner

    from foreman.cli import cli

    result = CliRunner().invoke(cli, ["daemon", "start", "--max-iterations", "1"])
    assert result.exit_code == 0


def test_daemon_start_refuses_when_lock_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """daemon_start exits non-zero when the lock is held by another
    process — same-process variant exercises the cli.py error-
    translation path without needing subprocess."""
    from foreman.daemon_lock import DaemonLock

    lock_path = tmp_path / "d.lock"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f"[daemon]\n"
        f'lock_path = "{lock_path.as_posix()}"\n'
        f'sqlite_path = "{(tmp_path / "f.sqlite").as_posix()}"\n'
        f'log_path = "{(tmp_path / "d.log").as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    # Hold the lock outside of a ``with`` block so the test owns
    # release timing.
    holder = DaemonLock(lock_path).__enter__()
    try:
        result = CliRunner().invoke(
            cli, ["daemon", "start", "--max-iterations", "1"]
        )
        assert result.exit_code != 0
        assert "already running" in result.output
        assert str(os.getpid()) in result.output
    finally:
        holder.__exit__(None, None, None)


def test_daemon_start_acquires_lock_and_releases_on_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """daemon_start holds the lock during the run and releases it on
    exit — verified by a fresh acquisition after the command returns."""
    from foreman.daemon_lock import DaemonLock

    lock_path = tmp_path / "d.lock"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f"[daemon]\n"
        f'lock_path = "{lock_path.as_posix()}"\n'
        f'sqlite_path = "{(tmp_path / "f.sqlite").as_posix()}"\n'
        f'log_path = "{(tmp_path / "d.log").as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    captured: dict[str, str | None] = {}

    async def spy(*, config, max_iterations):  # type: ignore[no-untyped-def]
        captured["mid_run_pid"] = (
            lock_path.read_text(encoding="ascii").strip()
            if lock_path.exists()
            else None
        )

    monkeypatch.setattr("foreman.cli._daemon_run", spy)

    result = CliRunner().invoke(
        cli, ["daemon", "start", "--max-iterations", "1"]
    )
    assert result.exit_code == 0, result.output
    assert captured["mid_run_pid"] == str(os.getpid())

    # Fresh acquisition must succeed (lock was released on exit).
    with DaemonLock(lock_path):
        pass  # If this raises, the previous run leaked the lock.


def test_daemon_start_honors_foreman_lock_path_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FOREMAN_LOCK_PATH env var overrides config.daemon.lock_path
    (issue #88 acceptance criterion #4)."""
    config_lock = tmp_path / "from_config.lock"
    env_lock = tmp_path / "from_env.lock"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f"[daemon]\n"
        f'lock_path = "{config_lock.as_posix()}"\n'
        f'sqlite_path = "{(tmp_path / "f.sqlite").as_posix()}"\n'
        f'log_path = "{(tmp_path / "d.log").as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    captured: dict[str, bool] = {}

    async def spy(*, config, max_iterations):  # type: ignore[no-untyped-def]
        captured["env_lock_exists"] = env_lock.exists()
        captured["config_lock_exists"] = config_lock.exists()

    monkeypatch.setattr("foreman.cli._daemon_run", spy)

    # Case 1: env var set → env path wins.
    monkeypatch.setenv("FOREMAN_LOCK_PATH", str(env_lock))
    result = CliRunner().invoke(
        cli, ["daemon", "start", "--max-iterations", "1"]
    )
    assert result.exit_code == 0, result.output
    assert captured["env_lock_exists"] is True
    assert captured["config_lock_exists"] is False

    # Case 2: env var empty → falls back to config.
    captured.clear()
    env_lock.unlink(missing_ok=True)
    monkeypatch.setenv("FOREMAN_LOCK_PATH", "")
    result = CliRunner().invoke(
        cli, ["daemon", "start", "--max-iterations", "1"]
    )
    assert result.exit_code == 0, result.output
    assert captured["config_lock_exists"] is True
    assert captured["env_lock_exists"] is False


def test_daemon_start_handles_unreadable_lock_content_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the lock holder hasn't written a valid PID yet, the error
    message falls back to 'pid: unknown' instead of crashing."""
    from foreman.daemon_lock import _format_already_running_message

    lock_path = tmp_path / "d.lock"
    # Pre-create the lock file with garbage content.
    lock_path.write_text("not a pid")

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f"[daemon]\n"
        f'lock_path = "{lock_path.as_posix()}"\n'
        f'sqlite_path = "{(tmp_path / "f.sqlite").as_posix()}"\n'
        f'log_path = "{(tmp_path / "d.log").as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    # Hold the lock at the OS level WITHOUT going through DaemonLock
    # (which would replace the garbage content with a valid PID). We
    # use the same low-level pattern DaemonLock uses internally so the
    # second start sees both "lock held" AND "unparseable content".
    fd = os.open(lock_path, os.O_RDWR)
    try:
        if sys.platform == "win32":
            import msvcrt

            # Mirror DaemonLock's lock-at-offset-1024 strategy so we
            # don't accidentally let the second start "see through"
            # our lock by locking a different byte.
            os.lseek(fd, 1024, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            os.lseek(fd, 0, os.SEEK_SET)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Re-truncate + re-write garbage to guarantee the second
        # start's _read_holder_pid sees unparseable content.
        os.ftruncate(fd, 0)
        os.write(fd, b"not a pid")
        os.fsync(fd)

        result = CliRunner().invoke(
            cli, ["daemon", "start", "--max-iterations", "1"]
        )
        assert result.exit_code != 0
        # Assert the single canonical "(pid: unknown)" form by
        # computing the expected message from the helper, which also
        # exercises the helper itself.
        assert _format_already_running_message(None) in result.output
    finally:
        os.close(fd)


def test_daemon_start_refuses_second_instance(tmp_path: Path) -> None:
    """End-to-end: two `foreman daemon start` invocations in parallel
    — the second exits non-zero with a clear message naming the
    first's PID (foreman#88 issue-body acceptance)."""
    lock_path = tmp_path / "d.lock"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f"[daemon]\n"
        f'lock_path = "{lock_path.as_posix()}"\n'
        f'sqlite_path = "{(tmp_path / "f.sqlite").as_posix()}"\n'
        f'log_path = "{(tmp_path / "d.log").as_posix()}"\n'
    )
    env = {**os.environ, "FOREMAN_CONFIG": str(config_path)}

    proc = subprocess.Popen(
        [sys.executable, "-m", "foreman.cli", "daemon", "start"],
        env=env,
    )
    try:
        # Poll until the first daemon has acquired the lock AND
        # written a numeric PID into the file. We don't assert the
        # written PID equals ``proc.pid`` because on Windows
        # ``sys.executable`` is sometimes a launcher exe whose pid
        # differs from the inner Python interpreter's pid (the latter
        # is what gets written to the lock file). What we care about
        # is "the second start saw the holder's PID" — so we capture
        # the file content and assert THAT is in the error message.
        deadline = time.monotonic() + 30
        holder_pid_in_file: str | None = None
        while time.monotonic() < deadline:
            if lock_path.exists():
                content = lock_path.read_text(encoding="ascii").strip()
                if content and content.isdigit():
                    holder_pid_in_file = content
                    break
            time.sleep(0.1)
        else:
            raise AssertionError(
                "First daemon did not write lock-file PID within 30s"
            )
        assert holder_pid_in_file is not None

        # Now try to start a second daemon with the same config.
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "foreman.cli",
                "daemon",
                "start",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        combined = (result.stdout or "") + (result.stderr or "")
        assert result.returncode != 0, (
            f"Second daemon start should exit non-zero. output: {combined}"
        )
        assert "already running" in combined, combined
        assert holder_pid_in_file in combined, combined

        # Lock file content is unchanged after the second call.
        assert (
            lock_path.read_text(encoding="ascii").strip() == holder_pid_in_file
        )
    finally:
        # Cross-platform teardown: terminate the first daemon. On
        # Windows, this is TerminateProcess (hard kill) — the OS
        # releases the lock at process death regardless.
        if proc.poll() is None:
            if sys.platform == "win32":
                proc.kill()
            else:
                proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def test_ps_shows_active_tickets(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f'[daemon]\nsqlite_path = "{(tmp_path / "f.sqlite").as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    from datetime import datetime

    from foreman.storage import Storage

    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    storage.upsert_pipeline("voice", 42, "foreman:spec-review", datetime(2026, 6, 1, tzinfo=UTC))

    from click.testing import CliRunner

    from foreman.cli import cli

    result = CliRunner().invoke(cli, ["ps"])
    assert result.exit_code == 0
    assert "voice" in result.output
    assert "42" in result.output
    assert "spec-review" in result.output


def test_pipeline_detail_shows_node_runs(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f'[daemon]\nsqlite_path = "{(tmp_path / "f.sqlite").as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    from datetime import datetime

    from foreman.storage import Storage

    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    now = datetime(2026, 6, 1, tzinfo=UTC)
    pid = storage.upsert_pipeline("voice", 42, "foreman:plan", now)
    rid = storage.record_node_run_start(
        pipeline_id=pid, role="planner", identity="foreman-planner-bot", at=now
    )
    storage.record_node_run_finish(
        run_id=rid, at=now, outcome="success", structured_output={"pr_number": 1}
    )

    from click.testing import CliRunner

    from foreman.cli import cli

    result = CliRunner().invoke(cli, ["pipeline-detail", "voice", "42"])
    assert result.exit_code == 0
    assert "planner" in result.output
    assert "success" in result.output


def test_worktree_clean_removes_directory(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f'[daemon]\nsqlite_path = "{(tmp_path / "f.sqlite").as_posix()}"\n'
        f"[projects.voice]\n"
        f'repo = "jeffrichley/voice"\n'
        f'local_clone_path = "{(tmp_path / "voice").as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))
    worktree = tmp_path / "worktrees" / "voice" / "issue-42"
    worktree.mkdir(parents=True)
    (worktree / "marker.txt").write_text("present")

    monkeypatch.setenv("FOREMAN_WORKTREES_ROOT", str(tmp_path / "worktrees"))

    from click.testing import CliRunner

    from foreman.cli import cli

    result = CliRunner().invoke(cli, ["worktree", "clean", "voice", "42"])
    assert result.exit_code == 0
    assert not worktree.exists()


# --- daemon lock-file lifecycle (foreman#72 + foreman#88) ---
#
# The daemon lock file at ``~/.foreman/daemon.lock`` serves as both
# the OS exclusive mutex (foreman#88) AND the operator-visible PID
# carrier for ``daemon stop`` / ``daemon status`` (foreman#72).
# DaemonLock writes ``str(os.getpid())`` to the file on acquisition.


def test_read_lock_file_pid_returns_none_for_missing_file(
    tmp_path: Path,
) -> None:
    from foreman.cli import _read_lock_file_pid

    assert _read_lock_file_pid(tmp_path / "missing.lock") is None


def test_read_lock_file_pid_returns_none_for_corrupt_file(
    tmp_path: Path,
) -> None:
    from foreman.cli import _read_lock_file_pid

    lock_path = tmp_path / "d.lock"
    lock_path.write_text("not a pid")

    assert _read_lock_file_pid(lock_path) is None


def test_read_lock_file_pid_parses_valid_content(tmp_path: Path) -> None:
    from foreman.cli import _read_lock_file_pid

    lock_path = tmp_path / "d.lock"
    lock_path.write_text(f"{os.getpid()}\n")

    assert _read_lock_file_pid(lock_path) == os.getpid()


def test_resolve_lock_path_honors_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FOREMAN_LOCK_PATH overrides the config-provided lock_path so
    daemon stop / status agree with daemon start on the same path."""
    from foreman.cli import _resolve_lock_path
    from foreman.config import DaemonConfig

    env_lock = tmp_path / "env.lock"
    config_lock = tmp_path / "config.lock"

    cfg = type(
        "FakeConfig",
        (),
        {"daemon": DaemonConfig(lock_path=str(config_lock))},
    )()
    monkeypatch.setenv("FOREMAN_LOCK_PATH", str(env_lock))

    assert _resolve_lock_path(cfg) == env_lock


def test_resolve_lock_path_falls_back_to_default_without_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from foreman.cli import _resolve_lock_path

    monkeypatch.delenv("FOREMAN_LOCK_PATH", raising=False)

    assert _resolve_lock_path(None) == Path("~/.foreman/daemon.lock").expanduser()


def test_daemon_stop_reads_lock_file_pid_and_sends_sigterm(
    tmp_path: Path, monkeypatch
) -> None:
    """daemon_stop must read the lock file's PID and send SIGTERM."""
    lock_path = tmp_path / "d.lock"
    lock_path.write_text(str(os.getpid()))

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f'[daemon]\nlock_path = "{lock_path.as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    kill_calls: list[tuple[int, int]] = []

    def _fake_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))
        # On the post-SIGTERM poll, simulate the daemon process
        # having died — raising ProcessLookupError when `stop`
        # probes liveness via os.kill(pid, 0).
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr("foreman.cli.os.kill", _fake_kill)
    monkeypatch.setattr("foreman.cli._STOP_POLL_INTERVAL_SECONDS", 0.01)

    result = CliRunner().invoke(cli, ["daemon", "stop"])

    assert result.exit_code == 0, result.output
    assert (os.getpid(), signal.SIGTERM) in kill_calls
    assert "Daemon stopped cleanly." in result.output


def test_daemon_stop_reports_when_daemon_does_not_exit(
    tmp_path: Path, monkeypatch
) -> None:
    """When the polled process refuses to die within the grace period,
    `stop` reports it. We don't remove the lock file ourselves — the
    OS will release the lock when the process eventually dies."""
    lock_path = tmp_path / "d.lock"
    lock_path.write_text(str(os.getpid()))

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f'[daemon]\nlock_path = "{lock_path.as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    # SIGTERM is a no-op AND liveness probes succeed (process refuses
    # to die during the grace period).
    monkeypatch.setattr("foreman.cli.os.kill", lambda pid, sig: None)
    monkeypatch.setattr("foreman.cli._STOP_GRACE_SECONDS", 0.05)
    monkeypatch.setattr("foreman.cli._STOP_POLL_INTERVAL_SECONDS", 0.01)

    result = CliRunner().invoke(cli, ["daemon", "stop"])

    assert result.exit_code == 0, result.output
    assert "did not exit" in result.output
    # We do NOT unlink the lock file — that's the daemon process's
    # contract via its OS lock, not ours.
    assert lock_path.exists()


def test_daemon_stop_with_missing_lock_file_gives_actionable_message(
    tmp_path: Path, monkeypatch
) -> None:
    """daemon_stop's missing-lock-file message must name a discovery
    command so the operator can find a stray daemon process."""
    lock_path = tmp_path / "d.lock"  # never created
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f'[daemon]\nlock_path = "{lock_path.as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    result = CliRunner().invoke(cli, ["daemon", "stop"])

    assert result.exit_code == 0
    assert str(lock_path) in result.output
    assert "foreman" in result.output
    # One of the platform discovery commands must appear:
    assert ("tasklist" in result.output) or ("ps aux" in result.output)


def test_daemon_stop_with_dead_pid_reports_stale(
    tmp_path: Path, monkeypatch
) -> None:
    """When the lock file's PID is dead, `stop` reports the stale
    state. The file is left in place — the OS lock is already free
    (the dead daemon's fd is gone), so the next `daemon start` will
    succeed and overwrite the file content."""
    lock_path = tmp_path / "d.lock"
    lock_path.write_text("999999999")

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f'[daemon]\nlock_path = "{lock_path.as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    def _fake_kill(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr("foreman.cli.os.kill", _fake_kill)

    result = CliRunner().invoke(cli, ["daemon", "stop"])

    assert result.exit_code == 0, result.output
    assert "not running" in result.output
    assert "stale" in result.output


def test_daemon_stop_with_unreadable_lock_content_reports(
    tmp_path: Path, monkeypatch
) -> None:
    """When the lock file exists but its content is unparseable,
    `stop` reports clearly without attempting to send a signal."""
    lock_path = tmp_path / "d.lock"
    lock_path.write_text("not a pid")

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f'[daemon]\nlock_path = "{lock_path.as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    kill_called = []
    monkeypatch.setattr(
        "foreman.cli.os.kill",
        lambda pid, sig: kill_called.append((pid, sig)),
    )

    result = CliRunner().invoke(cli, ["daemon", "stop"])

    assert result.exit_code == 0, result.output
    assert "unreadable" in result.output.lower()
    assert kill_called == []  # never tried to signal an unknown PID


def test_daemon_stop_works_without_config_file(
    tmp_path: Path, monkeypatch
) -> None:
    """Operator without a config file can still stop the daemon —
    falls back to the default lock path via FOREMAN_LOCK_PATH or
    ~/.foreman/daemon.lock."""
    monkeypatch.delenv("FOREMAN_CONFIG", raising=False)

    default_lock_path = tmp_path / "default.lock"
    default_lock_path.write_text(str(os.getpid()))
    monkeypatch.setenv("FOREMAN_LOCK_PATH", str(default_lock_path))

    kill_calls: list[tuple[int, int]] = []

    def _fake_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr("foreman.cli.os.kill", _fake_kill)
    monkeypatch.setattr("foreman.cli._STOP_POLL_INTERVAL_SECONDS", 0.01)

    result = CliRunner().invoke(cli, ["daemon", "stop"])

    assert result.exit_code == 0, result.output
    assert (os.getpid(), signal.SIGTERM) in kill_calls
    assert "Daemon stopped cleanly." in result.output


def test_daemon_status_reports_running_when_lock_pid_alive(
    tmp_path: Path, monkeypatch
) -> None:
    """daemon_status reads the lock file's PID and probes liveness."""
    lock_path = tmp_path / "d.lock"
    lock_path.write_text(str(os.getpid()))

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f'[daemon]\nlock_path = "{lock_path.as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    result = CliRunner().invoke(cli, ["daemon", "status"])

    assert result.exit_code == 0, result.output
    assert "running" in result.output.lower()
    assert str(os.getpid()) in result.output


def test_daemon_status_reports_stale_when_lock_pid_dead(
    tmp_path: Path, monkeypatch
) -> None:
    lock_path = tmp_path / "d.lock"
    lock_path.write_text("999999999")

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f'[daemon]\nlock_path = "{lock_path.as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    def _fake_kill(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr("foreman.cli.os.kill", _fake_kill)

    result = CliRunner().invoke(cli, ["daemon", "status"])

    assert result.exit_code == 0, result.output
    assert "stale" in result.output.lower()


# TODO(foreman#98): re-enable on CI Windows once subprocess hang is root-caused.
@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "CI Windows Server 2025 hangs in pytest when this test runs, "
        "even though the same test passes on local Windows 11 with the "
        "same Python / uv / cmd.exe shell. The lock-file-as-PID contract "
        "is otherwise covered by the unit-level CliRunner tests above. "
        "Root cause + Windows-safe restoration tracked in foreman#98."
    ),
)
def test_daemon_subprocess_writes_pid_and_releases_lock_on_exit(
    tmp_path: Path,
) -> None:
    """End-to-end: spawn `foreman daemon start --max-iterations 1` as
    a real subprocess; assert it writes its PID to the lock file
    during the run AND releases the OS lock when it exits naturally.

    We use --max-iterations to avoid coupling this test to the
    daemon's SIGTERM-shutdown timing (a daemon-internal concern,
    separately tested via mocked CliRunner tests). What we're
    verifying here is the foreman#72 + foreman#88 contract: the
    lock file is the single source of truth for the daemon's PID,
    and the OS lock is freed when the daemon process ends —
    however it ends.
    """
    import subprocess

    lock_path = tmp_path / "d.lock"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f"[daemon]\n"
        f'lock_path = "{lock_path.as_posix()}"\n'
        f'sqlite_path = "{(tmp_path / "f.sqlite").as_posix()}"\n'
        f'log_path = "{(tmp_path / "d.log").as_posix()}"\n'
    )
    env = {**os.environ, "FOREMAN_CONFIG": str(config_path)}

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "foreman.cli",
            "daemon",
            "start",
            "--max-iterations",
            "1",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout

    # The lock file remains on disk (no unlink in the daemon's exit
    # path — the OS lock is freed by fd close, not file removal).
    # Its content is the daemon's PID, now stale.
    assert lock_path.exists()
    content = lock_path.read_text(encoding="ascii").strip()
    assert content.isdigit(), f"lock file content not a PID: {content!r}"

    # The architectural contract: after the daemon exits, the OS
    # lock is free. Verify by re-acquiring DaemonLock against the
    # same path from the test process. If this raises, the daemon
    # leaked the lock.
    from foreman.daemon_lock import DaemonLock

    with DaemonLock(lock_path):
        pass
