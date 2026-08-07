"""``foreman init <project>`` — V4Config-driven bootstrap.

These tests stub the v3 helpers (label creation, bot verification,
instructions template, clone validation) so the command exercise stays
local — no PyGithub round-trips, no real ``git`` subprocess. The point
is to prove the v4-config-load + project-lookup + helper-invocation
wiring, not to re-test the v3 helpers themselves (those have their
own coverage in ``tests/test_init.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from foreman.init import BotVerification
from foreman.v4.cli import app
from foreman.v4.config import (
    AppCredentials,
    AppsConfig,
    OperatorConfig,
    OperatorIdentity,
    OrchestratorConfig,
    ProjectConfig,
    StorageConfig,
    V4Config,
)


def _apps_config() -> AppsConfig:
    """Mirror ``tests/v4/test_bootstrap.py``'s helper: fake creds quadruple."""
    creds = AppCredentials(app_id=12345, private_key_path="/tmp/fake.pem")
    return AppsConfig(planner=creds, reviewer=creds, fixer=creds, worker=creds)


def _orchestrator_config() -> OrchestratorConfig:
    return OrchestratorConfig(
        app_id=99999,
        private_key_path="/tmp/fake-orch.pem",
    )


def _operator_config() -> OperatorConfig:
    return OperatorConfig(
        supervisor=OperatorIdentity(name="Test Sup", email="sup@example.com"),
        signer=OperatorIdentity(name="Test Sign", email="sign@example.com"),
    )


def _v4_config_toml(tmp_path: Path, projects: list[ProjectConfig]) -> Path:
    """Build a V4Config TOML file at ``tmp_path/config.toml``.

    The minimum required shape — daemon + apps + orchestrator.
    Since issue #477, [[projects]] tables are no longer read from config.toml
    at runtime (they come from FOREMAN_PROJECTS_PATH / projects.toml).
    The ``projects`` arg is kept for backward-compat of the call sites but
    the entries are NO LONGER written into the config.toml — call
    ``_projects_toml`` to write the companion projects file.
    """
    log_dir = tmp_path / "logs"
    toml = f"""\
[daemon]
log_dir = "{log_dir.as_posix()}"

[storage]
engine = "postgres"
dsn = "postgresql://test/test"

[apps.planner]
app_id = 12345
private_key_path = "/tmp/fake.pem"

[apps.reviewer]
app_id = 12345
private_key_path = "/tmp/fake.pem"

[apps.fixer]
app_id = 12345
private_key_path = "/tmp/fake.pem"

[apps.worker]
app_id = 12345
private_key_path = "/tmp/fake.pem"

[orchestrator]
app_id = 99999
private_key_path = "/tmp/fake-orch.pem"

[operator.supervisor]
name = "Test Supervisor"
email = "sup@example.com"

[operator.signer]
name = "Test Signer"
email = "sign@example.com"
"""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(toml, encoding="utf-8")
    return cfg_path


def _projects_toml(tmp_path: Path, projects: list[ProjectConfig]) -> Path:
    """Write a standalone projects.toml at ``tmp_path/projects.toml``.

    issue #477: cmd_init now loads the project list from
    ``$FOREMAN_PROJECTS_PATH`` (not from config.toml). Tests call this
    alongside ``_v4_config_toml`` and point ``FOREMAN_PROJECTS_PATH`` at
    the returned path.
    """
    project_blocks = "\n".join(
        f'[[projects]]\nname = "{p.name}"\nrepo = "{p.repo}"\n'
        f'local_clone_path = "{Path(p.local_clone_path).as_posix()}"\n'
        for p in projects
    )
    proj_path = tmp_path / "projects.toml"
    proj_path.write_text(project_blocks, encoding="utf-8")
    return proj_path


def _stub_helpers(monkeypatch, *, calls: dict[str, Any]) -> None:
    """Monkeypatch all v3 helpers cmd_init calls into recording stubs.

    ``calls`` is a shared dict the stubs populate so the test can
    assert call order + per-helper arguments. Helpers are patched at
    the v4 import site (``foreman.v4.cli.init``) — that's where the
    cmd reaches for them after the ``from foreman.init import ...``
    rebound the names to that module's namespace.
    """

    def fake_validate_clone(clone_path, expected_repo):
        calls["validate_clone"] = (clone_path, expected_repo)

    def fake_write_instructions(*, clone_path, repo_name, check_command):
        calls["write_instructions"] = {
            "clone_path": clone_path,
            "repo_name": repo_name,
            "check_command": check_command,
        }
        return clone_path / ".foreman" / "INSTRUCTIONS.md", True

    def fake_check_instructions(clone_path):
        calls["check_instructions"] = clone_path
        return None

    def fake_ensure_labels(*, client, repo_slug):
        calls["ensure_labels"] = {"client": client, "repo_slug": repo_slug}
        return (["foreman:plan"], ["foreman:done"])

    def fake_verify_bot(*, role, apps, repo_slug):
        calls.setdefault("verify_bot_roles", []).append(role)
        calls.setdefault("verify_bot_repo", repo_slug)
        return BotVerification(role=role, ok=True, detail="OK")

    class _FakeIdentity:
        def __init__(self, *, apps, orchestrator, installation_repo):
            calls["identity_installation_repo"] = installation_repo

        def get_role_token(self, role):
            calls["identity_role_token"] = role
            return "ghp_fake_token"

    class _FakeGithub:
        def __init__(self, *, auth=None):
            calls["github_auth_used"] = auth is not None

    monkeypatch.setattr(
        "foreman.v4.cli.init._validate_clone_path",
        fake_validate_clone,
    )
    monkeypatch.setattr(
        "foreman.v4.cli.init._write_instructions_template",
        fake_write_instructions,
    )
    monkeypatch.setattr(
        "foreman.v4.cli.init._check_instructions_committed",
        fake_check_instructions,
    )
    monkeypatch.setattr(
        "foreman.v4.cli.init._ensure_labels",
        fake_ensure_labels,
    )
    monkeypatch.setattr(
        "foreman.v4.cli.init._verify_bot_installation",
        fake_verify_bot,
    )
    monkeypatch.setattr(
        "foreman.v4.cli.init.V4IdentityRegistry",
        _FakeIdentity,
    )
    monkeypatch.setattr(
        "foreman.v4.cli.init.Github",
        _FakeGithub,
    )


def test_init_missing_config_file_raises(tmp_path: Path, monkeypatch):
    """No V4 config on disk → exit code 1 + clear message."""
    missing_path = tmp_path / "does-not-exist.toml"
    monkeypatch.setenv("FOREMAN_V4_CONFIG", str(missing_path))
    # CliRunner in click 8.2+ merges stderr into ``output`` by default
    # (the ``mix_stderr`` knob was removed). Both err/non-err echoes
    # land in ``result.output``.
    result = CliRunner().invoke(app, ["init", "algokit"])
    assert result.exit_code == 1
    assert "V4 config not found" in result.output
    assert str(missing_path) in result.output


def test_init_unknown_project_raises(tmp_path: Path, monkeypatch):
    """Project name not in projects.toml → exit code 1 + known-names list."""
    known_projects = [
        ProjectConfig(
            name="voice",
            repo="owner/voice",
            local_clone_path=str(tmp_path / "voice"),
        ),
        ProjectConfig(
            name="madrigal",
            repo="owner/madrigal",
            local_clone_path=str(tmp_path / "madrigal"),
        ),
    ]
    cfg_path = _v4_config_toml(tmp_path, projects=known_projects)
    proj_path = _projects_toml(tmp_path, projects=known_projects)
    monkeypatch.setenv("FOREMAN_V4_CONFIG", str(cfg_path))
    monkeypatch.setenv("FOREMAN_PROJECTS_PATH", str(proj_path))
    result = CliRunner().invoke(app, ["init", "algokit"])
    assert result.exit_code == 1
    assert "'algokit' not found" in result.output
    # Error names the known project list so operators see the mismatch
    # immediately rather than having to grep their own config.
    assert "voice" in result.output
    assert "madrigal" in result.output


def test_init_calls_helpers_in_order(tmp_path: Path, monkeypatch):
    """Happy path: every v3 helper called once with the expected args."""
    project_cfg = ProjectConfig(
        name="algokit",
        repo="jeffrichley/algokit",
        local_clone_path=str(tmp_path / "algokit"),
    )
    cfg_path = _v4_config_toml(tmp_path, projects=[project_cfg])
    proj_path = _projects_toml(tmp_path, projects=[project_cfg])
    monkeypatch.setenv("FOREMAN_V4_CONFIG", str(cfg_path))
    monkeypatch.setenv("FOREMAN_PROJECTS_PATH", str(proj_path))

    calls: dict[str, Any] = {}
    _stub_helpers(monkeypatch, calls=calls)

    result = CliRunner().invoke(app, ["init", "algokit"])
    assert result.exit_code == 0, result.output

    # _validate_clone_path got the configured clone + repo.
    validate_clone = calls["validate_clone"]
    assert isinstance(validate_clone, tuple)
    assert validate_clone[0] == Path(project_cfg.local_clone_path)
    assert validate_clone[1] == "jeffrichley/algokit"

    # Instructions written with the bare repo name + default check command.
    write_call = calls["write_instructions"]
    assert isinstance(write_call, dict)
    assert write_call["repo_name"] == "algokit"
    assert write_call["check_command"] == "just check"
    assert write_call["clone_path"] == Path(project_cfg.local_clone_path)

    # Defensive git-status check ran on the clone.
    assert calls["check_instructions"] == Path(project_cfg.local_clone_path)

    # Label creation used the orchestrator-authed admin client + the
    # configured repo slug.
    ensure_call = calls["ensure_labels"]
    assert isinstance(ensure_call, dict)
    assert ensure_call["repo_slug"] == "jeffrichley/algokit"

    # All four roles verified, each against the configured repo.
    assert calls["verify_bot_roles"] == ["planner", "reviewer", "fixer", "worker"]
    assert calls["verify_bot_repo"] == "jeffrichley/algokit"

    # Identity registry pointed at the project's repo for installation
    # lookup + minted an orchestrator-role token for the admin client.
    assert calls["identity_installation_repo"] == "jeffrichley/algokit"
    assert calls["identity_role_token"] == "orchestrator"
    assert calls["github_auth_used"] is True

    # Summary surfaces project name + label counts + bot verifications.
    assert "algokit" in result.output
    assert "Labels:" in result.output
    assert "1 created" in result.output  # fake_ensure_labels: 1 created
    assert "1 existed" in result.output  # fake_ensure_labels: 1 existed
    assert "Bot verifications:" in result.output
    assert "planner" in result.output


def test_init_clone_validation_failure_exits_cleanly(tmp_path: Path, monkeypatch):
    """``_validate_clone_path`` raising ValueError → exit code 1 + msg."""
    projects = [
        ProjectConfig(
            name="algokit",
            repo="jeffrichley/algokit",
            local_clone_path=str(tmp_path / "algokit"),
        ),
    ]
    cfg_path = _v4_config_toml(tmp_path, projects=projects)
    proj_path = _projects_toml(tmp_path, projects=projects)
    monkeypatch.setenv("FOREMAN_V4_CONFIG", str(cfg_path))
    monkeypatch.setenv("FOREMAN_PROJECTS_PATH", str(proj_path))

    def fake_validate_clone(clone_path, expected_repo):
        raise ValueError(f"Clone path does not exist: {clone_path}")

    monkeypatch.setattr(
        "foreman.v4.cli.init._validate_clone_path",
        fake_validate_clone,
    )

    result = CliRunner().invoke(app, ["init", "algokit"])
    assert result.exit_code == 1
    assert "clone validation failed" in result.output
    assert "Clone path does not exist" in result.output


def test_init_uses_v4_config_apps_and_orchestrator():
    """V4Config + ProjectConfig shape sanity: the helpers cmd_init needs
    on the v4 side are present on the public V4Config schema.

    Catches drift if Phase 8b.2 (or a later refactor) restructures
    ``apps`` / ``orchestrator`` on V4Config in a way that breaks
    cmd_init's access pattern without anyone realizing the init
    surface depends on it.
    """
    config = V4Config(
        storage=StorageConfig(engine="postgres", dsn="postgresql://test/test"),
        log_dir="/tmp/logs",
        apps=_apps_config(),
        orchestrator=_orchestrator_config(),
        operator=_operator_config(),
        projects=[
            ProjectConfig(
                name="algokit",
                repo="jeffrichley/algokit",
                local_clone_path="/tmp/algokit",
            ),
        ],
    )
    # The exact attributes cmd_init reaches for on V4Config.
    assert config.apps.planner.app_id == 12345
    assert config.apps.planner.private_key_path == "/tmp/fake.pem"
    assert config.orchestrator.app_id == 99999
    assert config.projects[0].name == "algokit"
    assert config.projects[0].repo == "jeffrichley/algokit"
    assert config.projects[0].local_clone_path == "/tmp/algokit"


# ---------------------------------------------------------------------------
# issue #590: _ensure_webhook and the cmd_init webhook path tests (SR 11)
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock  # noqa: E402

from foreman.v4.cli.init import _ensure_webhook  # noqa: E402


def _v4_config_toml_with_inbound(tmp_path: Path, inbound_url: str | None = None) -> Path:
    """Build a V4Config TOML with optional [inbound] block."""
    log_dir = tmp_path / "logs"
    base = f"""\
[daemon]
log_dir = "{log_dir.as_posix()}"

[storage]
engine = "postgres"
dsn = "postgresql://test/test"

[apps.planner]
app_id = 12345
private_key_path = "/tmp/fake.pem"

[apps.reviewer]
app_id = 12345
private_key_path = "/tmp/fake.pem"

[apps.fixer]
app_id = 12345
private_key_path = "/tmp/fake.pem"

[apps.worker]
app_id = 12345
private_key_path = "/tmp/fake.pem"

[orchestrator]
app_id = 99999
private_key_path = "/tmp/fake-orch.pem"

[operator.supervisor]
name = "Test Supervisor"
email = "sup@example.com"

[operator.signer]
name = "Test Signer"
email = "sign@example.com"
"""
    if inbound_url is not None:
        base += f'\n[inbound]\nreceiver_url = "{inbound_url}"\n'
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(base, encoding="utf-8")
    return cfg_path


def _make_fake_hook(url: str, active: bool = True) -> MagicMock:
    h = MagicMock()
    h.active = active
    h.config = {"url": url}
    return h


def test_ensure_webhook_returns_existed_when_active_hook_exists() -> None:
    """_ensure_webhook returns (False, True) when an active matching hook already exists."""
    receiver = "https://my.funnel.example.com/webhook"
    client = MagicMock()
    client.get_repo.return_value.get_hooks.return_value = [_make_fake_hook(receiver, active=True)]

    created, existed = _ensure_webhook(client, "owner/repo", receiver)
    assert created is False
    assert existed is True
    # create_hook must NOT be called
    client.get_repo.return_value.create_hook.assert_not_called()


def test_ensure_webhook_creates_hook_when_absent() -> None:
    """_ensure_webhook returns (True, False) and calls create_hook when no matching hook exists."""
    receiver = "https://my.funnel.example.com/webhook"
    client = MagicMock()
    # Return a hook with a different URL — no match.
    client.get_repo.return_value.get_hooks.return_value = [
        _make_fake_hook("https://other.url/hook", active=True)
    ]
    client.get_repo.return_value.create_hook.return_value = MagicMock()

    created, existed = _ensure_webhook(client, "owner/repo", receiver)
    assert created is True
    assert existed is False
    client.get_repo.return_value.create_hook.assert_called_once()


def test_cmd_init_webhook_skipped_when_inbound_none(tmp_path: Path, monkeypatch) -> None:
    """When config.inbound is None, the Webhook summary line reports skipped
    and no hook API is called."""
    project_cfg = ProjectConfig(
        name="algokit",
        repo="jeffrichley/algokit",
        local_clone_path=str(tmp_path / "algokit"),
    )
    cfg_path = _v4_config_toml_with_inbound(tmp_path, inbound_url=None)
    proj_path = _projects_toml(tmp_path, projects=[project_cfg])
    monkeypatch.setenv("FOREMAN_V4_CONFIG", str(cfg_path))
    monkeypatch.setenv("FOREMAN_PROJECTS_PATH", str(proj_path))

    hook_api_calls = []

    calls: dict[str, Any] = {}
    _stub_helpers(monkeypatch, calls=calls)

    # Patch _ensure_webhook to record whether it was called.
    def fake_ensure_webhook(client, repo_slug, receiver_url):
        hook_api_calls.append((repo_slug, receiver_url))
        return (False, True)

    monkeypatch.setattr("foreman.v4.cli.init._ensure_webhook", fake_ensure_webhook)

    result = CliRunner().invoke(app, ["init", "algokit"])
    assert result.exit_code == 0, result.output
    assert not hook_api_calls, "_ensure_webhook must not be called when inbound is None"
    assert "Webhook:" in result.output
    # Must contain a "skipped" or "no inbound" indicator
    assert "skipped" in result.output.lower() or "no inbound" in result.output.lower()


def test_cmd_init_webhook_existed_when_hook_already_present(tmp_path: Path, monkeypatch) -> None:
    """When config.inbound is set and a matching hook exists, summary shows 'existed'."""
    receiver = "https://my.funnel.example.com/webhook"
    project_cfg = ProjectConfig(
        name="algokit",
        repo="jeffrichley/algokit",
        local_clone_path=str(tmp_path / "algokit"),
    )
    cfg_path = _v4_config_toml_with_inbound(tmp_path, inbound_url=receiver)
    proj_path = _projects_toml(tmp_path, projects=[project_cfg])
    monkeypatch.setenv("FOREMAN_V4_CONFIG", str(cfg_path))
    monkeypatch.setenv("FOREMAN_PROJECTS_PATH", str(proj_path))

    calls: dict[str, Any] = {}
    _stub_helpers(monkeypatch, calls=calls)

    def fake_ensure_webhook(client, repo_slug, receiver_url):
        return (False, True)  # existed

    monkeypatch.setattr("foreman.v4.cli.init._ensure_webhook", fake_ensure_webhook)

    result = CliRunner().invoke(app, ["init", "algokit"])
    assert result.exit_code == 0, result.output
    assert "Webhook:" in result.output
    assert "existed" in result.output.lower()


def test_cmd_init_webhook_created_when_hook_absent(tmp_path: Path, monkeypatch) -> None:
    """When config.inbound is set and no matching hook exists, summary shows 'created'."""
    receiver = "https://my.funnel.example.com/webhook"
    project_cfg = ProjectConfig(
        name="algokit",
        repo="jeffrichley/algokit",
        local_clone_path=str(tmp_path / "algokit"),
    )
    cfg_path = _v4_config_toml_with_inbound(tmp_path, inbound_url=receiver)
    proj_path = _projects_toml(tmp_path, projects=[project_cfg])
    monkeypatch.setenv("FOREMAN_V4_CONFIG", str(cfg_path))
    monkeypatch.setenv("FOREMAN_PROJECTS_PATH", str(proj_path))

    calls: dict[str, Any] = {}
    _stub_helpers(monkeypatch, calls=calls)

    def fake_ensure_webhook(client, repo_slug, receiver_url):
        return (True, False)  # created

    monkeypatch.setattr("foreman.v4.cli.init._ensure_webhook", fake_ensure_webhook)

    result = CliRunner().invoke(app, ["init", "algokit"])
    assert result.exit_code == 0, result.output
    assert "Webhook:" in result.output
    assert "created" in result.output.lower()
