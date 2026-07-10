"""``foreman doctor`` — deployment-health probes (image-freshness, …).

Covers the four exit shapes for the image-fresh check (foreman#363):
OK, STALE, SKIPPED, and the two WARN branches (subprocess failure +
timeout). Each test stubs ``subprocess.run`` at the doctor module's
import site so the real network call never fires.
"""

from __future__ import annotations

import subprocess

from typer.testing import CliRunner

from foreman.v4.cli import app


def _make_completed(
    returncode: int,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    """Build a CompletedProcess[str] the doctor's stub layer expects."""
    return subprocess.CompletedProcess(
        args=["git", "ls-remote"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_doctor_image_fresh_ok_when_shas_match(monkeypatch) -> None:
    """IMAGE_SHA matches ``origin/main`` (first 7 chars) → exit 0 + OK."""
    monkeypatch.setenv("IMAGE_SHA", "abc1234")

    def fake_run(*args, **kwargs):
        return _make_completed(
            0,
            stdout="abc1234deadbeefcafefeed00112233445566778\trefs/heads/main\n",
        )

    monkeypatch.setattr("foreman.v4.cli.doctor.subprocess.run", fake_run)

    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "image-fresh: OK" in result.output


def test_doctor_image_fresh_stale_when_shas_differ(monkeypatch) -> None:
    """IMAGE_SHA differs from ``origin/main`` → exit 1 + STALE message."""
    monkeypatch.setenv("IMAGE_SHA", "abc1234")

    def fake_run(*args, **kwargs):
        return _make_completed(
            0,
            stdout="ffffffffdeadbeefcafefeed00112233445566778\trefs/heads/main\n",
        )

    monkeypatch.setattr("foreman.v4.cli.doctor.subprocess.run", fake_run)

    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "image-fresh: STALE" in result.output
    assert "just rebuild-daemon" in result.output


def test_doctor_image_fresh_skipped_when_env_unset(monkeypatch) -> None:
    """IMAGE_SHA absent → exit 0 + SKIPPED (host-direct invocation shape)."""
    monkeypatch.delenv("IMAGE_SHA", raising=False)

    # The subprocess.run stub should never fire on this code path —
    # install a guard that raises if it does, so a regression that
    # forgets the SKIPPED short-circuit shows up loudly.
    def fake_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called when IMAGE_SHA is unset")

    monkeypatch.setattr("foreman.v4.cli.doctor.subprocess.run", fake_run)

    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "image-fresh: SKIPPED" in result.output


def test_doctor_image_fresh_warn_on_subprocess_failure(monkeypatch) -> None:
    """``git ls-remote`` returncode != 0 → exit 0 + WARN (not stale)."""
    monkeypatch.setenv("IMAGE_SHA", "abc1234")

    def fake_run(*args, **kwargs):
        return _make_completed(
            128,
            stdout="",
            stderr="fatal: unable to access 'https://github.com/...': Could not resolve host: github.com\n",
        )

    monkeypatch.setattr("foreman.v4.cli.doctor.subprocess.run", fake_run)

    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "image-fresh: WARN" in result.output


def test_doctor_image_fresh_warn_on_subprocess_timeout(monkeypatch) -> None:
    """``git ls-remote`` timing out → exit 0 + WARN, daemon survives."""
    monkeypatch.setenv("IMAGE_SHA", "abc1234")

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["git", "ls-remote"], timeout=10)

    monkeypatch.setattr("foreman.v4.cli.doctor.subprocess.run", fake_run)

    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "image-fresh: WARN" in result.output
