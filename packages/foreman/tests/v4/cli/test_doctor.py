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


# ---------------------------------------------------------------------------
# issue #590: _check_webhook_coverage tests
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock  # noqa: E402

from github import GithubException  # noqa: E402

from foreman.v4.cli.doctor import _check_webhook_coverage  # noqa: E402
from foreman.v4.config import (  # noqa: E402
    AppCredentials,
    AppsConfig,
    InboundConfig,
    OperatorConfig,
    OperatorIdentity,
    OrchestratorConfig,
    ProjectConfig,
    StorageConfig,
    V4Config,
)


def _make_config(inbound: InboundConfig | None = None) -> V4Config:
    """Build a minimal V4Config for webhook-coverage tests."""
    creds = AppCredentials(app_id=1, private_key_path="/dev/null")
    return V4Config(
        log_dir="/tmp/logs",
        apps=AppsConfig(planner=creds, reviewer=creds, fixer=creds, worker=creds),
        orchestrator=OrchestratorConfig(app_id=9, private_key_path="/dev/null"),
        operator=OperatorConfig(
            supervisor=OperatorIdentity(name="Sup", email="sup@example.com"),
            signer=OperatorIdentity(name="Sign", email="sign@example.com"),
        ),
        storage=StorageConfig(engine="postgres", dsn="postgresql://test/test"),
        inbound=inbound,
    )


def _make_project(name: str = "proj", repo: str = "owner/proj") -> ProjectConfig:
    return ProjectConfig(name=name, repo=repo, local_clone_path=f"/tmp/{name}")


def _fake_hook(url: str, active: bool = True) -> MagicMock:
    """Build a fake PyGithub hook object."""
    hook = MagicMock()
    hook.active = active
    hook.config = {"url": url}
    return hook


def test_check_webhook_coverage_skipped_when_inbound_none() -> None:
    """When config.inbound is None, returns 0 (SKIPPED) immediately — no github calls."""
    config = _make_config(inbound=None)
    projects = [_make_project()]
    called = []

    def factory():
        called.append(True)
        return MagicMock()

    exit_code = _check_webhook_coverage(config, projects, github_factory=factory)
    assert exit_code == 0
    assert not called, "github_factory should not be called when inbound is None"


def test_check_webhook_coverage_skipped_when_receiver_url_empty() -> None:
    """When receiver_url is the empty string, returns 0 (SKIPPED) — same as absent."""
    config = _make_config(inbound=InboundConfig(receiver_url=""))
    projects = [_make_project()]
    called = []

    def factory():
        called.append(True)
        return MagicMock()

    exit_code = _check_webhook_coverage(config, projects, github_factory=factory)
    assert exit_code == 0
    assert not called, "github_factory should not be called when receiver_url is empty"


def test_check_webhook_coverage_ok_when_all_projects_have_active_hook(
    capsys,
) -> None:
    """All projects have an active hook with matching URL → exit 0, OK per project."""
    receiver = "https://my.receiver.example.com/webhook"
    config = _make_config(inbound=InboundConfig(receiver_url=receiver))
    projects = [_make_project("alpha", "org/alpha"), _make_project("beta", "org/beta")]

    def factory():
        github = MagicMock()
        github.get_repo.return_value.get_hooks.return_value = [_fake_hook(receiver, active=True)]
        return github

    exit_code = _check_webhook_coverage(config, projects, github_factory=factory)
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "webhook-coverage" in captured.out
    assert "OK" in captured.out


def test_check_webhook_coverage_missing_when_no_matching_hook(capsys) -> None:
    """Project has no active hook with receiver URL → exit 1, MISSING line."""
    receiver = "https://my.receiver.example.com/webhook"
    config = _make_config(inbound=InboundConfig(receiver_url=receiver))
    projects = [_make_project("myproj", "org/myproj")]

    def factory():
        github = MagicMock()
        # Active hook but different URL
        github.get_repo.return_value.get_hooks.return_value = [
            _fake_hook("https://other.url.com/hook", active=True)
        ]
        return github

    exit_code = _check_webhook_coverage(config, projects, github_factory=factory)
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "MISSING" in captured.out
    assert "myproj" in captured.out or "org/myproj" in captured.out


def test_check_webhook_coverage_warn_on_github_exception(capsys) -> None:
    """GithubException during hook fetch → exit 0, WARN (not 1)."""
    receiver = "https://my.receiver.example.com/webhook"
    config = _make_config(inbound=InboundConfig(receiver_url=receiver))
    projects = [_make_project("myproj", "org/myproj")]

    def factory():
        github = MagicMock()
        github.get_repo.return_value.get_hooks.side_effect = GithubException(
            403, {"message": "Must have admin rights"}, None
        )
        return github

    exit_code = _check_webhook_coverage(config, projects, github_factory=factory)
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "WARN" in captured.out
