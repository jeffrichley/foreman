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
                "--target",
                "spec_pr",
                "--config",
                str(config_file),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "needs_fix" in result.output
    assert "1 findings" in result.output
    assert "confidence=medium" in result.output
    mock_run.assert_called_once()


def test_cli_review_target_flag_optional(tmp_path: Path) -> None:
    """``foreman review`` accepts ``--target`` but does not require it; legacy
    callers (and pre-Stage-2 dispatchers) still work without the flag."""
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

    fake_result = ReviewerRunResult(
        llm_output=ReviewerOutput(
            outcome="clean",
            review_comment="clean",
            findings=[],
            confidence="high",
        ),
        final_labels=["foreman:plan-approved"],
    )

    runner = CliRunner()
    with patch("foreman.cli.run_reviewer", new=AsyncMock(return_value=fake_result)):
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
                "--issue-url",
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
    # ``--target`` defaults to ``spec_pr`` — the CLI plumbs that through.
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs.get("target") == "spec_pr"


def test_cli_fix_with_target_impl_pr_plumbs_through(tmp_path: Path) -> None:
    """CRITICAL #4 wire-up: ``foreman fix --target impl_pr`` forwards
    ``target='impl_pr'`` to ``run_fixer``. Pre-rescue the flag did not
    exist and v3 dispatches landed on the spec-side default."""
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
            fix_comment="fixed",
            commits_made=[],
            addressed_findings=[],
            unaddressed_findings=[],
            confidence="medium",
        ),
        attempt=1,
        final_labels=["foreman:impl-review"],
    )

    runner = CliRunner()
    with patch("foreman.cli.run_fixer", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = runner.invoke(
            cli,
            [
                "fix",
                "--issue-url",
                "https://github.com/jeffrichley/voice/issues/42",
                "--pr-url",
                "https://github.com/jeffrichley/voice/pull/77",
                "--project",
                "voice",
                "--target",
                "impl_pr",
                "--config",
                str(config_file),
            ],
        )

    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs.get("target") == "impl_pr"
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
    daemon stop / status agree with daemon start on the same path.

    Override must beat BOTH the legacy daemon.lock_path AND the v3
    reconciler.lock_path so operators can redirect to a temp file in
    smoke-tests without editing config.
    """
    from foreman.cli import _resolve_lock_path
    from foreman.config import DaemonConfig, ReconcilerConfig

    env_lock = tmp_path / "env.lock"
    v3_lock = tmp_path / "v3.lock"
    legacy_lock = tmp_path / "legacy.lock"

    cfg = type(
        "FakeConfig",
        (),
        {
            "daemon": DaemonConfig(lock_path=str(legacy_lock)),
            "reconciler": ReconcilerConfig(lock_path=str(v3_lock)),
        },
    )()
    monkeypatch.setenv("FOREMAN_LOCK_PATH", str(env_lock))

    assert _resolve_lock_path(cfg) == env_lock


def test_resolve_lock_path_returns_v3_reconciler_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """foreman#118: v3 is the canonical runtime. When the operator's config
    does NOT explicitly set ``[daemon]\\nlock_path`` (the usual case for any
    v3 config), ``_resolve_lock_path`` must return ``config.reconciler.lock_path``
    so that ``daemon stop`` / ``status`` address the same file ``daemon
    v3-start`` acquired. Without this fix operators saw 'No daemon lock file
    at ...daemon.lock' even though the v3 daemon was running and holding
    ``reconciler.lock``."""
    from foreman.cli import _resolve_lock_path
    from foreman.config import DaemonConfig, ReconcilerConfig

    monkeypatch.delenv("FOREMAN_LOCK_PATH", raising=False)

    v3_lock = tmp_path / "reconciler.lock"

    cfg = type(
        "FakeConfig",
        (),
        {
            # DaemonConfig() with no explicit lock_path — model_fields_set
            # will not contain "lock_path", so the helper falls through to
            # the v3 reconciler section.
            "daemon": DaemonConfig(),
            "reconciler": ReconcilerConfig(lock_path=str(v3_lock)),
        },
    )()

    assert _resolve_lock_path(cfg) == v3_lock


def test_resolve_lock_path_honors_legacy_daemon_lock_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backwards-compat shim: when a legacy v2 config has explicitly set
    ``[daemon]\\nlock_path``, honor it. Without the shim, pre-v3 config files
    silently start writing to the v3 path on upgrade — a foot-gun for anyone
    who pinned the v2 lock to a non-default location."""
    from foreman.cli import _resolve_lock_path
    from foreman.config import DaemonConfig, ReconcilerConfig

    monkeypatch.delenv("FOREMAN_LOCK_PATH", raising=False)

    legacy_lock = tmp_path / "legacy-daemon.lock"
    v3_lock = tmp_path / "reconciler.lock"

    cfg = type(
        "FakeConfig",
        (),
        {
            # Explicit lock_path means model_fields_set will include it.
            "daemon": DaemonConfig(lock_path=str(legacy_lock)),
            "reconciler": ReconcilerConfig(lock_path=str(v3_lock)),
        },
    )()

    assert _resolve_lock_path(cfg) == legacy_lock


def test_resolve_lock_path_falls_back_to_default_without_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a config (e.g., bare operator invocation), the resolved lock
    path is the v3 canonical default — ``~/.foreman/reconciler.lock``."""
    from foreman.cli import _resolve_lock_path

    monkeypatch.delenv("FOREMAN_LOCK_PATH", raising=False)

    assert _resolve_lock_path(None) == Path("~/.foreman/reconciler.lock").expanduser()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows daemon_stop intentionally skips os.kill(SIGTERM) — there it "
        "maps to TerminateProcess (a hard kill that delivers no signal), so "
        "the daemon's graceful-shutdown handlers never run. Windows uses "
        "the sentinel-file path only; that path is covered by "
        "test_daemon_stop_writes_shutdown_sentinel below."
    ),
)
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
    # Redirect sentinel write into tmp_path so the test doesn't pollute
    # ~/.foreman/shutdown-requested on the dev's box.
    monkeypatch.setenv(
        "FOREMAN_SHUTDOWN_SENTINEL_PATH", str(tmp_path / "shutdown-requested")
    )

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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows daemon_stop skips os.kill(SIGTERM) and the grace-period "
        "polling that follows it (TerminateProcess can't run the daemon's "
        "cleanup path); shutdown is sentinel-only on Windows."
    ),
)
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
    monkeypatch.setenv(
        "FOREMAN_SHUTDOWN_SENTINEL_PATH", str(tmp_path / "shutdown-requested")
    )

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
    monkeypatch.setenv(
        "FOREMAN_SHUTDOWN_SENTINEL_PATH", str(tmp_path / "shutdown-requested")
    )

    result = CliRunner().invoke(cli, ["daemon", "stop"])

    assert result.exit_code == 0
    assert str(lock_path) in result.output
    assert "foreman" in result.output
    # One of the platform discovery commands must appear:
    assert ("tasklist" in result.output) or ("ps aux" in result.output)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows daemon_stop skips os.kill entirely; stale-PID detection "
        "relies on os.kill(pid, SIGTERM) raising ProcessLookupError, which "
        "the Windows code path doesn't reach. Stale lock files on Windows "
        "are detected lazily by the next `daemon start`."
    ),
)
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
    monkeypatch.setenv(
        "FOREMAN_SHUTDOWN_SENTINEL_PATH", str(tmp_path / "shutdown-requested")
    )

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
    monkeypatch.setenv(
        "FOREMAN_SHUTDOWN_SENTINEL_PATH", str(tmp_path / "shutdown-requested")
    )

    kill_called = []
    monkeypatch.setattr(
        "foreman.cli.os.kill",
        lambda pid, sig: kill_called.append((pid, sig)),
    )

    result = CliRunner().invoke(cli, ["daemon", "stop"])

    assert result.exit_code == 0, result.output
    assert "unreadable" in result.output.lower()
    assert kill_called == []  # never tried to signal an unknown PID


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows daemon_stop skips os.kill(SIGTERM); SIGTERM-assertion "
        "tests don't apply there. The cross-platform sentinel write is "
        "still exercised in test_daemon_stop_writes_shutdown_sentinel."
    ),
)
def test_daemon_stop_works_without_config_file(
    tmp_path: Path, monkeypatch
) -> None:
    """Operator without a config file can still stop the daemon —
    falls back to the default lock path via FOREMAN_LOCK_PATH or
    ~/.foreman/reconciler.lock (v3 canonical)."""
    monkeypatch.delenv("FOREMAN_CONFIG", raising=False)

    default_lock_path = tmp_path / "default.lock"
    default_lock_path.write_text(str(os.getpid()))
    monkeypatch.setenv("FOREMAN_LOCK_PATH", str(default_lock_path))
    monkeypatch.setenv(
        "FOREMAN_SHUTDOWN_SENTINEL_PATH", str(tmp_path / "shutdown-requested")
    )

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


# --- Sentinel-file shutdown mechanism (cross-platform graceful stop) ---
#
# Pass-2 adversarial review HIGH: ``os.kill(pid, SIGTERM)`` on Windows
# maps to ``TerminateProcess`` — a hard kill that delivers no signal,
# so the daemon's SIGTERM handler never runs and the graceful-shutdown
# promise is broken. The sentinel file is the cross-platform IPC
# primitive that actually works everywhere: ``daemon stop`` writes it,
# the reconciler polls it each tick. POSIX still gets the SIGTERM as a
# faster signal; Windows relies on the sentinel alone.


def test_daemon_stop_writes_shutdown_sentinel(
    tmp_path: Path, monkeypatch
) -> None:
    """daemon_stop writes the sentinel file when a daemon is running.

    The cross-platform contract: when the lock file exists (i.e., there's
    a live daemon to receive the signal), the sentinel write happens
    before any SIGTERM logic, so a crashing SIGTERM-send never silently
    disarms the v3 reconciler's poll-based shutdown path. This is the
    ONLY way the daemon receives a graceful-shutdown request on Windows.

    See the no-daemon-running tests below for the opposite case — when
    there's no lock file, daemon_stop must NOT write a sentinel, because
    a stale sentinel would silently kill the next `daemon v3-start`.
    """
    sentinel_path = tmp_path / "shutdown-requested"
    lock_path = tmp_path / "d.lock"
    lock_path.write_text(str(os.getpid()))

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f'[daemon]\nlock_path = "{lock_path.as_posix()}"\n'
        f'[reconciler]\nshutdown_sentinel_path = "{sentinel_path.as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))
    # No-op os.kill so the test doesn't actually signal anything; we only
    # care about the sentinel-write happening on the lock-exists path.
    monkeypatch.setattr("foreman.cli.os.kill", lambda pid, sig: None)
    monkeypatch.setattr("foreman.cli._STOP_GRACE_SECONDS", 0.0)

    result = CliRunner().invoke(cli, ["daemon", "stop"])

    assert result.exit_code == 0, result.output
    assert sentinel_path.exists(), "sentinel file must be written by daemon_stop"
    assert "requested by foreman daemon stop" in sentinel_path.read_text(
        encoding="utf-8"
    )
    assert "shutdown requested via sentinel" in result.output


def test_daemon_stop_does_not_write_sentinel_when_no_daemon_running(
    tmp_path: Path, monkeypatch
) -> None:
    """daemon_stop must NOT leave a sentinel on disk when there's no
    daemon to receive it. A stale sentinel would be polled by the
    next `daemon v3-start`'s first tick and trigger an immediate
    shutdown — silent failure mode that's hard to diagnose."""
    sentinel_path = tmp_path / "shutdown-requested"
    lock_path = tmp_path / "missing.lock"  # never created

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f'[daemon]\nlock_path = "{lock_path.as_posix()}"\n'
        f'[reconciler]\nshutdown_sentinel_path = "{sentinel_path.as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    result = CliRunner().invoke(cli, ["daemon", "stop"])

    assert result.exit_code == 0, result.output
    assert not sentinel_path.exists(), (
        "sentinel must NOT be written when no daemon is running — "
        "would kill the next daemon start"
    )
    # The no-daemon path should still echo the actionable message.
    assert "No daemon lock file" in result.output


def test_daemon_stop_does_not_write_sentinel_without_config_or_lock(
    tmp_path: Path, monkeypatch
) -> None:
    """Even without a config file (FOREMAN_CONFIG unset), the lock-gate
    check still applies — no lock means no daemon means no sentinel.
    Otherwise an operator running `daemon stop` on a fresh box would
    plant a sentinel that mysteriously kills their first daemon start."""
    monkeypatch.delenv("FOREMAN_CONFIG", raising=False)
    sentinel_path = tmp_path / "shutdown-requested"
    monkeypatch.setenv("FOREMAN_SHUTDOWN_SENTINEL_PATH", str(sentinel_path))

    # Point lock to a nonexistent path so we return at the no-lock gate.
    monkeypatch.setenv("FOREMAN_LOCK_PATH", str(tmp_path / "missing.lock"))

    result = CliRunner().invoke(cli, ["daemon", "stop"])

    assert result.exit_code == 0, result.output
    assert not sentinel_path.exists()


def test_resolve_shutdown_sentinel_path_honors_env_override(
    tmp_path: Path, monkeypatch
) -> None:
    """FOREMAN_SHUTDOWN_SENTINEL_PATH overrides config + the
    hardcoded default, mirroring FOREMAN_LOCK_PATH's resolution order
    so tests + operators can redirect without editing the config file."""
    from foreman.cli import _resolve_shutdown_sentinel_path
    from foreman.config import ReconcilerConfig

    env_sentinel = tmp_path / "env.sentinel"
    config_sentinel = tmp_path / "config.sentinel"

    cfg = type(
        "FakeConfig",
        (),
        {"reconciler": ReconcilerConfig(shutdown_sentinel_path=str(config_sentinel))},
    )()
    monkeypatch.setenv("FOREMAN_SHUTDOWN_SENTINEL_PATH", str(env_sentinel))

    assert _resolve_shutdown_sentinel_path(cfg) == env_sentinel


def test_resolve_shutdown_sentinel_path_falls_back_to_default_without_config(
    monkeypatch,
) -> None:
    from foreman.cli import _resolve_shutdown_sentinel_path

    monkeypatch.delenv("FOREMAN_SHUTDOWN_SENTINEL_PATH", raising=False)

    assert (
        _resolve_shutdown_sentinel_path(None)
        == Path("~/.foreman/shutdown-requested").expanduser()
    )


# --- daemon reload (foreman#100): sentinel-only IPC ---


def test_daemon_reload_writes_sentinel_when_lock_present(
    tmp_path: Path, monkeypatch
) -> None:
    """daemon_reload writes the reload sentinel when a daemon is running.

    Cross-platform contract: when the lock file exists with a parseable
    PID, the sentinel write happens unconditionally — the v3 reconciler
    polls the file at the top of its next tick.
    """
    sentinel_path = tmp_path / "reload-requested"
    lock_path = tmp_path / "d.lock"
    lock_path.write_text(str(os.getpid()))

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f'[daemon]\nlock_path = "{lock_path.as_posix()}"\n'
        f'[reconciler]\nreload_sentinel_path = "{sentinel_path.as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))
    monkeypatch.setenv("FOREMAN_LOCK_PATH", str(lock_path))
    monkeypatch.setenv("FOREMAN_RELOAD_SENTINEL_PATH", str(sentinel_path))
    # Defensive: ensure the shutdown-sentinel default is also redirected
    # so an accidental write doesn't pollute ~/.foreman/.
    monkeypatch.setenv(
        "FOREMAN_SHUTDOWN_SENTINEL_PATH", str(tmp_path / "shutdown-requested")
    )

    result = CliRunner().invoke(cli, ["daemon", "reload"])

    assert result.exit_code == 0, result.output
    assert sentinel_path.exists(), "sentinel file must be written by daemon_reload"
    assert "requested by foreman daemon reload" in sentinel_path.read_text(
        encoding="utf-8"
    )
    assert "reload requested via sentinel" in result.output


def test_daemon_reload_refuses_when_no_lock_file(
    tmp_path: Path, monkeypatch
) -> None:
    """daemon_reload must NOT leave a sentinel on disk when there's no
    daemon to receive it. A stale sentinel would be polled by the
    next `daemon v3-start`'s first tick and trigger a confusing
    config_reload audit row for an already-fresh config — silent
    failure mode that's hard to diagnose."""
    sentinel_path = tmp_path / "reload-requested"
    lock_path = tmp_path / "missing.lock"  # never created

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f'[daemon]\nlock_path = "{lock_path.as_posix()}"\n'
        f'[reconciler]\nreload_sentinel_path = "{sentinel_path.as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))
    monkeypatch.setenv("FOREMAN_LOCK_PATH", str(lock_path))
    monkeypatch.setenv("FOREMAN_RELOAD_SENTINEL_PATH", str(sentinel_path))
    monkeypatch.setenv(
        "FOREMAN_SHUTDOWN_SENTINEL_PATH", str(tmp_path / "shutdown-requested")
    )

    result = CliRunner().invoke(cli, ["daemon", "reload"])

    assert result.exit_code == 0, result.output
    assert not sentinel_path.exists(), (
        "sentinel must NOT be written when no daemon is running — "
        "would fire on the next daemon start"
    )
    # The no-daemon path must echo the resolved lock path so the operator
    # knows where to look.
    assert "No daemon lock file" in result.output
    assert str(lock_path) in result.output


def test_daemon_reload_refuses_when_lock_pid_unreadable(
    tmp_path: Path, monkeypatch
) -> None:
    """When the lock file exists but its content is unparseable,
    `reload` reports clearly without writing the sentinel."""
    sentinel_path = tmp_path / "reload-requested"
    lock_path = tmp_path / "d.lock"
    lock_path.write_text("not a pid")

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f'[daemon]\nlock_path = "{lock_path.as_posix()}"\n'
        f'[reconciler]\nreload_sentinel_path = "{sentinel_path.as_posix()}"\n'
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))
    monkeypatch.setenv("FOREMAN_LOCK_PATH", str(lock_path))
    monkeypatch.setenv("FOREMAN_RELOAD_SENTINEL_PATH", str(sentinel_path))
    monkeypatch.setenv(
        "FOREMAN_SHUTDOWN_SENTINEL_PATH", str(tmp_path / "shutdown-requested")
    )

    result = CliRunner().invoke(cli, ["daemon", "reload"])

    assert result.exit_code == 0, result.output
    assert not sentinel_path.exists(), (
        "sentinel must NOT be written when the lock PID is unparseable"
    )
    assert "unreadable" in result.output.lower()
    assert str(lock_path) in result.output


def test_resolve_reload_sentinel_path_honors_env_override(
    tmp_path: Path, monkeypatch
) -> None:
    """FOREMAN_RELOAD_SENTINEL_PATH overrides config + the hardcoded
    default, mirroring FOREMAN_SHUTDOWN_SENTINEL_PATH's resolution order
    so tests + operators can redirect without editing the config file."""
    from foreman.cli import _resolve_reload_sentinel_path
    from foreman.config import ReconcilerConfig

    env_sentinel = tmp_path / "env.sentinel"
    config_sentinel = tmp_path / "config.sentinel"

    cfg = type(
        "FakeConfig",
        (),
        {"reconciler": ReconcilerConfig(reload_sentinel_path=str(config_sentinel))},
    )()
    monkeypatch.setenv("FOREMAN_RELOAD_SENTINEL_PATH", str(env_sentinel))

    assert _resolve_reload_sentinel_path(cfg) == env_sentinel


def test_resolve_reload_sentinel_path_falls_back_to_default_without_config(
    monkeypatch,
) -> None:
    from foreman.cli import _resolve_reload_sentinel_path

    monkeypatch.delenv("FOREMAN_RELOAD_SENTINEL_PATH", raising=False)

    assert (
        _resolve_reload_sentinel_path(None)
        == Path("~/.foreman/reload-requested").expanduser()
    )


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


# --- foreman#246: ad-hoc CLI execution_log rows ---
#
# When a human runs `foreman plan / review / implement / fix` directly
# from the CLI without going through daemon dispatch, the role runner
# writes its JSONL row but `execution_log` got no row at all — leaving
# the SQL ledger blind to ad-hoc invocations. The CLI subcommands now
# write a start row tagged ``rule_name="manual-cli"`` with
# ``details.actor="manual-cli"`` (or ``"manual-cli-during-daemon"`` if
# the daemon's lock file is present), then a termination row on
# success / error. Implementer's call per the spec: proceed when the
# daemon is running, but distinguish the actor tag and warn — a hard
# refuse would block legitimate dev workflow.


def _write_basic_config(
    config_file: Path,
    *,
    tmp_path: Path,
    db_path: Path | None = None,
    extra: str = "",
) -> Path:
    """Build a minimal config TOML that points the v3 reconciler at a
    tmp-path db so the manual-cli logger doesn't pollute the dev box's
    ``~/.foreman/reconciler.sqlite``."""
    if db_path is None:
        db_path = tmp_path / "reconciler.sqlite"
    config_file.write_text(
        f"""
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
fixer_app_id_env = "FOREMAN_FIXER_APP_ID"
fixer_app_id = 777777
fixer_private_key_path = "/tmp/fixer.pem"
worker_app_id_env = "FOREMAN_WORKER_APP_ID"
worker_app_id = 444444
worker_private_key_path = "/tmp/worker.pem"

[reconciler]
db_path = "{db_path.as_posix()}"
{extra}
"""
    )
    return db_path


def _read_exec_log_rows(db_path: Path) -> list[dict]:
    """Return all execution_log rows as dicts, oldest first."""
    import json
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, ticket_id, project, rule_name, action, outcome, "
            "details, parent_log_id FROM execution_log ORDER BY id ASC"
        ).fetchall()
    out: list[dict] = []
    for row in rows:
        d = dict(row)
        d["details_parsed"] = json.loads(d["details"]) if d["details"] else {}
        out.append(d)
    return out


def _planner_fake_result() -> PlannerRunResult:
    return PlannerRunResult(
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


def _reviewer_fake_result() -> ReviewerRunResult:
    return ReviewerRunResult(
        llm_output=ReviewerOutput(
            outcome="clean",
            review_comment="clean",
            findings=[],
            confidence="high",
        ),
        final_labels=["foreman:plan-approved"],
    )


def _fixer_fake_result() -> FixerRunResult:
    return FixerRunResult(
        llm_output=FixerOutput(
            outcome="fixed",
            fix_comment="fixed",
            commits_made=[],
            addressed_findings=[],
            unaddressed_findings=[],
            confidence="high",
        ),
        attempt=1,
        final_labels=["foreman:spec-review"],
    )


def _worker_fake_result() -> WorkerRunResult:
    return WorkerRunResult(
        llm_output=WorkerOutput(
            outcome="implemented",
            work_comment="ok",
            pr_title="feat: x",
            pr_body="body",
            commits_made=[],
            implemented_sub_requests=[],
            skipped_sub_requests=[],
            did_check_pass=True,
            confidence="high",
        ),
        attempt=1,
        pr_url="https://github.com/jeffrichley/voice/pull/101",
        final_did_check_pass=True,
        final_labels=["foreman:impl-review"],
    )


def test_cli_plan_writes_execution_log_start_and_terminate_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`foreman plan` writes a start row (rule_name='manual-cli',
    outcome='running', details.actor='manual-cli') and a terminate row
    (outcome='success') on a successful role-runner return.

    Closes foreman#246's primary acceptance criterion for the Planner CLI.
    """
    config_file = tmp_path / "config.toml"
    db_path = _write_basic_config(config_file, tmp_path=tmp_path)
    # No daemon lock — the CLI is the only writer.
    monkeypatch.setenv("FOREMAN_LOCK_PATH", str(tmp_path / "missing.lock"))

    runner = CliRunner()
    with patch("foreman.cli.run_planner", new=AsyncMock(return_value=_planner_fake_result())):
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

    rows = _read_exec_log_rows(db_path)
    assert len(rows) == 2, rows

    start, term = rows
    assert start["rule_name"] == "manual-cli"
    assert start["action"] == "plan"
    assert start["outcome"] == "running"
    assert start["ticket_id"] == "jeffrichley/voice#42"
    assert start["project"] == "voice"
    assert start["details_parsed"]["actor"] == "manual-cli"
    assert start["parent_log_id"] is None

    assert term["rule_name"] == "manual-cli"
    assert term["action"] == "plan"
    assert term["outcome"] == "success"
    assert term["parent_log_id"] == start["id"]


def test_cli_review_writes_execution_log_start_and_terminate_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    db_path = _write_basic_config(config_file, tmp_path=tmp_path)
    monkeypatch.setenv("FOREMAN_LOCK_PATH", str(tmp_path / "missing.lock"))

    runner = CliRunner()
    with patch(
        "foreman.cli.run_reviewer", new=AsyncMock(return_value=_reviewer_fake_result())
    ):
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

    rows = _read_exec_log_rows(db_path)
    assert len(rows) == 2, rows

    start, term = rows
    assert start["rule_name"] == "manual-cli"
    assert start["action"] == "review"
    assert start["outcome"] == "running"
    # Reviewer takes a PR URL — the ticket_id captures the PR number,
    # which is the addressable artifact the manual run worked on.
    assert start["ticket_id"] == "jeffrichley/voice#77"
    assert start["project"] == "voice"
    assert start["details_parsed"]["actor"] == "manual-cli"

    assert term["outcome"] == "success"
    assert term["parent_log_id"] == start["id"]


def test_cli_implement_writes_execution_log_start_and_terminate_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    db_path = _write_basic_config(config_file, tmp_path=tmp_path)
    monkeypatch.setenv("FOREMAN_LOCK_PATH", str(tmp_path / "missing.lock"))

    runner = CliRunner()
    with patch(
        "foreman.cli.run_worker", new=AsyncMock(return_value=_worker_fake_result())
    ):
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

    rows = _read_exec_log_rows(db_path)
    assert len(rows) == 2, rows

    start, term = rows
    assert start["rule_name"] == "manual-cli"
    assert start["action"] == "implement"
    assert start["outcome"] == "running"
    assert start["ticket_id"] == "jeffrichley/voice#42"
    assert start["details_parsed"]["actor"] == "manual-cli"

    assert term["outcome"] == "success"
    assert term["parent_log_id"] == start["id"]


def test_cli_fix_writes_execution_log_start_and_terminate_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    db_path = _write_basic_config(config_file, tmp_path=tmp_path)
    monkeypatch.setenv("FOREMAN_LOCK_PATH", str(tmp_path / "missing.lock"))

    runner = CliRunner()
    with patch(
        "foreman.cli.run_fixer", new=AsyncMock(return_value=_fixer_fake_result())
    ):
        result = runner.invoke(
            cli,
            [
                "fix",
                "--issue-url",
                "https://github.com/jeffrichley/voice/issues/42",
                "--project",
                "voice",
                "--config",
                str(config_file),
            ],
        )

    assert result.exit_code == 0, result.output

    rows = _read_exec_log_rows(db_path)
    assert len(rows) == 2, rows

    start, term = rows
    assert start["rule_name"] == "manual-cli"
    assert start["action"] == "fix"
    assert start["outcome"] == "running"
    assert start["ticket_id"] == "jeffrichley/voice#42"
    assert start["details_parsed"]["actor"] == "manual-cli"

    assert term["outcome"] == "success"
    assert term["parent_log_id"] == start["id"]


def test_cli_plan_writes_terminate_row_on_exception_and_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the role runner raises, the CLI must write a terminate row
    with ``outcome='error'`` AND re-raise the original exception so the
    operator sees it and any wrapping shell exits non-zero.

    Pre-fix behavior: the JSONL stats logger captured the failure but
    ``execution_log`` got nothing, so cross-correlation queries missed
    every failed ad-hoc run.
    """
    config_file = tmp_path / "config.toml"
    db_path = _write_basic_config(config_file, tmp_path=tmp_path)
    monkeypatch.setenv("FOREMAN_LOCK_PATH", str(tmp_path / "missing.lock"))

    boom = RuntimeError("planner exploded")

    runner = CliRunner()
    with patch("foreman.cli.run_planner", new=AsyncMock(side_effect=boom)):
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

    # CliRunner captures the raised exception in result.exception and
    # propagates a non-zero exit code. The contract: the original
    # exception is re-raised unchanged (not swallowed, not wrapped).
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    assert str(result.exception) == "planner exploded"

    rows = _read_exec_log_rows(db_path)
    assert len(rows) == 2, rows

    start, term = rows
    assert start["outcome"] == "running"
    assert start["rule_name"] == "manual-cli"
    assert start["details_parsed"]["actor"] == "manual-cli"

    assert term["outcome"] == "error"
    assert term["parent_log_id"] == start["id"]
    assert "planner exploded" in term["details_parsed"].get("error", "")


def test_cli_plan_falls_back_gracefully_when_db_path_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the reconciler db path is unavailable (parent dir cannot be
    created), the CLI logs a warning, skips the execution_log write,
    and still runs the role to completion. The JSONL stats stream
    remains canonical for cost — execution_log is a supplemental
    cross-correlation surface.
    """
    config_file = tmp_path / "config.toml"
    # Point db_path at a location whose parent is a FILE — mkdir parents
    # will fail. This simulates a misconfigured deployment (or a fresh
    # operator who hasn't initialized foreman's state dir yet).
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    bad_db = blocker / "child" / "reconciler.sqlite"
    _write_basic_config(config_file, tmp_path=tmp_path, db_path=bad_db)
    monkeypatch.setenv("FOREMAN_LOCK_PATH", str(tmp_path / "missing.lock"))

    runner = CliRunner()
    with patch(
        "foreman.cli.run_planner", new=AsyncMock(return_value=_planner_fake_result())
    ) as mock_run:
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
    # The role runner ran despite the db being unreachable.
    mock_run.assert_called_once()
    # A warning surfaced — operators must know the row was skipped.
    assert "manual-cli" in result.output.lower() or "execution_log" in result.output.lower()
    # No db file was created at the bad path (the fallback didn't try
    # to write a sibling file or anything similarly clever).
    assert not bad_db.exists()


def test_cli_plan_tags_actor_when_daemon_lock_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the daemon's lock file is present, the manual CLI proceeds
    (per the implementer's call documented in the commit body) but tags
    ``actor="manual-cli-during-daemon"`` and emits a warning so the
    dual-writer scenario is auditable.
    """
    config_file = tmp_path / "config.toml"
    db_path = _write_basic_config(config_file, tmp_path=tmp_path)
    # Plant a lock file at the path the CLI resolves via _resolve_lock_path.
    daemon_lock = tmp_path / "reconciler.lock"
    daemon_lock.write_text(str(os.getpid()))
    monkeypatch.setenv("FOREMAN_LOCK_PATH", str(daemon_lock))

    runner = CliRunner()
    with patch(
        "foreman.cli.run_planner", new=AsyncMock(return_value=_planner_fake_result())
    ):
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

    rows = _read_exec_log_rows(db_path)
    assert len(rows) == 2, rows
    start, term = rows
    assert start["details_parsed"]["actor"] == "manual-cli-during-daemon"
    # The warning IS the audit surface — operators running manual CLI
    # against a live daemon need to see this in their terminal.
    assert "daemon" in result.output.lower()
    assert term["outcome"] == "success"
