"""Tests for the `foreman daemon v3-start` Click subcommand."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from foreman.cli import cli


def _write_v3_config(
    tmp_path: Path,
    *,
    lock_path: Path,
    db_path: Path,
    sentinel_path: Path,
    reload_sentinel_path: Path | None = None,
) -> Path:
    """Write a minimal v3-start config TOML pointing at tmp_path locations.

    Centralizes the as_posix() escaping so individual tests stay readable.
    ``reload_sentinel_path`` is optional so existing tests stay unchanged;
    when provided, the field is written into the ``[reconciler]`` block.
    """
    config_path = tmp_path / "config.toml"
    body = (
        "[reconciler]\n"
        f'db_path = "{db_path.as_posix()}"\n'
        f'lock_path = "{lock_path.as_posix()}"\n'
        f'shutdown_sentinel_path = "{sentinel_path.as_posix()}"\n'
    )
    if reload_sentinel_path is not None:
        body += f'reload_sentinel_path = "{reload_sentinel_path.as_posix()}"\n'
    config_path.write_text(body)
    return config_path


def test_v3_start_help_lists_dry_run_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["daemon", "v3-start", "--help"])
    assert result.exit_code == 0
    assert "--dry-run" in result.output
    assert "reconciler" in result.output.lower()


def test_v3_start_short_circuits_without_runtime_setup(monkeypatch, tmp_path) -> None:
    # Sanity: the command is wired and the entry point function is callable.
    # Full runtime (real GH client + bus) is integration-tested elsewhere.
    # All paths point under tmp_path so the test never touches the real
    # ~/.foreman/reconciler.lock (a live daemon's lock would make this fail
    # with "daemon already running" on a developer's box).
    runner = CliRunner()
    config_path = _write_v3_config(
        tmp_path,
        lock_path=tmp_path / "reconciler.lock",
        db_path=tmp_path / "reconciler.sqlite",
        sentinel_path=tmp_path / "shutdown-requested",
    )
    monkeypatch.setenv("FOREMAN_CONFIG_PATH", str(config_path))
    result = runner.invoke(cli, ["daemon", "v3-start", "--max-ticks", "0", "--dry-run"])
    # --max-ticks 0 means "wire everything, run zero ticks, exit". Either
    # exit 0 (clean) or a controlled stub-not-implemented exit; never crash
    # uncaught.
    assert result.exit_code in (0, 2), result.output


def test_v3_start_removes_stale_sentinel_at_startup(monkeypatch, tmp_path) -> None:
    """If a stale shutdown sentinel was left on disk by a prior
    `daemon stop` (e.g., the bug where stop wrote the sentinel
    unconditionally even with no daemon running), a fresh
    `daemon v3-start` must clean it up before the reconciler's first
    tick — otherwise the tick poller would consume it and trigger
    an immediate graceful-shutdown.

    Uses --max-ticks 0 to wire everything and exit before the loop
    starts; the sentinel cleanup runs after lock acquisition but
    before the reconciler is constructed, so the cleanup path
    still fires."""
    sentinel_path = tmp_path / "shutdown-requested"
    sentinel_path.write_text("stale from prior stop\n", encoding="utf-8")
    assert sentinel_path.exists()  # pre-condition

    lock_path = tmp_path / "reconciler.lock"
    db_path = tmp_path / "reconciler.sqlite"
    config_path = _write_v3_config(
        tmp_path,
        lock_path=lock_path,
        db_path=db_path,
        sentinel_path=sentinel_path,
    )
    monkeypatch.setenv("FOREMAN_CONFIG_PATH", str(config_path))

    runner = CliRunner()
    result = runner.invoke(
        cli, ["daemon", "v3-start", "--max-ticks", "0", "--dry-run"]
    )

    assert result.exit_code in (0, 2), result.output
    assert not sentinel_path.exists(), (
        "v3-start must remove a stale sentinel left over from a prior "
        "`daemon stop` before the reconciler's first poll, otherwise the "
        "new daemon would shut down on its first tick"
    )


def test_v3_start_removes_stale_reload_sentinel_at_startup(
    monkeypatch, tmp_path
) -> None:
    """If a stale reload sentinel was left on disk by a prior `daemon
    reload` (or by some prior edge case), a fresh `daemon v3-start`
    must clean it up before the reconciler's first tick — otherwise
    the tick poller would consume it and log a confusing
    config_reload row for an already-fresh config.

    Parallel to ``test_v3_start_removes_stale_sentinel_at_startup``;
    uses --max-ticks 0 to wire everything and exit before the loop
    starts."""
    reload_sentinel_path = tmp_path / "reload-requested"
    reload_sentinel_path.write_text(
        "stale from prior reload\n", encoding="utf-8"
    )
    assert reload_sentinel_path.exists()  # pre-condition

    lock_path = tmp_path / "reconciler.lock"
    db_path = tmp_path / "reconciler.sqlite"
    shutdown_sentinel_path = tmp_path / "shutdown-requested"
    config_path = _write_v3_config(
        tmp_path,
        lock_path=lock_path,
        db_path=db_path,
        sentinel_path=shutdown_sentinel_path,
        reload_sentinel_path=reload_sentinel_path,
    )
    monkeypatch.setenv("FOREMAN_CONFIG_PATH", str(config_path))

    runner = CliRunner()
    result = runner.invoke(
        cli, ["daemon", "v3-start", "--max-ticks", "0", "--dry-run"]
    )

    assert result.exit_code in (0, 2), result.output
    assert not reload_sentinel_path.exists(), (
        "v3-start must remove a stale reload sentinel left over from a "
        "prior `daemon reload` before the reconciler's first poll, "
        "otherwise the new daemon would log a confusing config_reload "
        "row on its first tick"
    )
