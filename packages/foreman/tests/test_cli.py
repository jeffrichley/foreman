"""CLI smoke tests via click's testing harness."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
from foreman.schemas.reviewer import Finding, ReviewerOutput
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

    fake_result = ReviewerOutput(
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
    )

    runner = CliRunner()
    with patch(
        "foreman.cli.run_reviewer", new=AsyncMock(return_value=fake_result)
    ) as mock_run:
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
    )

    runner = CliRunner()
    with patch(
        "foreman.cli.run_fixer", new=AsyncMock(return_value=fake_result)
    ) as mock_run:
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
    )

    runner = CliRunner()
    with patch(
        "foreman.cli.run_worker", new=AsyncMock(return_value=fake_result)
    ) as mock_run:
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
        "[admin]\ngithub_token_env = \"X\"\n"
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
        f"[admin]\ngithub_token_env = \"X\"\n"
        f"[daemon]\nsqlite_path = \"{(tmp_path / 'f.sqlite').as_posix()}\"\n"
        f"log_path = \"{(tmp_path / 'd.log').as_posix()}\"\n"
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    from click.testing import CliRunner
    from foreman.cli import cli

    result = CliRunner().invoke(cli, ["daemon", "start", "--max-iterations", "1"])
    assert result.exit_code == 0
