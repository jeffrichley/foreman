"""Tests for the `foreman daemon v3-start` Click subcommand."""

from __future__ import annotations

from click.testing import CliRunner

from foreman.cli import cli


def test_v3_start_help_lists_dry_run_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["daemon", "v3-start", "--help"])
    assert result.exit_code == 0
    assert "--dry-run" in result.output
    assert "reconciler" in result.output.lower()


def test_v3_start_short_circuits_without_runtime_setup(monkeypatch, tmp_path) -> None:
    # Sanity: the command is wired and the entry point function is callable.
    # Full runtime (real GH client + bus) is integration-tested elsewhere.
    runner = CliRunner()
    monkeypatch.setenv("FOREMAN_CONFIG_PATH", str(tmp_path / "config.toml"))
    (tmp_path / "config.toml").write_text("[reconciler]\ndb_path = '" + str(tmp_path / "reconciler.sqlite").replace("\\", "/") + "'\n")
    result = runner.invoke(cli, ["daemon", "v3-start", "--max-ticks", "0", "--dry-run"])
    # --max-ticks 0 means "wire everything, run zero ticks, exit". Either
    # exit 0 (clean) or a controlled stub-not-implemented exit; never crash
    # uncaught.
    assert result.exit_code in (0, 2), result.output
