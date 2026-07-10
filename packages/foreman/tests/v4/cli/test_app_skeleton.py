"""Typer app skeleton — root invocation prints help, --version returns string."""

from __future__ import annotations

from typer.testing import CliRunner

from foreman.v4.cli import app


def test_app_help_lists_command_groups():
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    # Each group's primary command should appear in --help:
    for cmd in ("ps", "show", "log", "queue", "hold", "resume", "daemon"):
        assert cmd in result.output


def test_app_version_prints_version_string():
    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "foreman" in result.output.lower()
