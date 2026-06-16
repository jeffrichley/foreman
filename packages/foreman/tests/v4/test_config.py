"""V4Config — TOML-loaded settings."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from foreman.v4.config import ProjectConfig, load_config

# Shared `[apps.*]` + `[orchestrator]` blocks used by every existing
# test. As of Task 8.3 `apps` is REQUIRED on V4Config; Task 8.4 makes
# `orchestrator` REQUIRED too (pivoted from env-var PAT to App
# installation credentials — same shape as the per-role apps).
# Concatenating this helper into each test's TOML keeps the existing
# assertions focused on what they're actually testing (projects, etc.)
# instead of rewriting every TOML string inline.
_APPS_TOML = (
    '[apps.planner]\n'
    'app_id = 12345\n'
    'private_key_path = "/tmp/fake-planner.pem"\n'
    '[apps.reviewer]\n'
    'app_id = 12346\n'
    'private_key_path = "/tmp/fake-reviewer.pem"\n'
    '[apps.fixer]\n'
    'app_id = 12347\n'
    'private_key_path = "/tmp/fake-fixer.pem"\n'
    '[apps.worker]\n'
    'app_id = 12348\n'
    'private_key_path = "/tmp/fake-worker.pem"\n'
    '[orchestrator]\n'
    'app_id = 12349\n'
    'private_key_path = "/tmp/fake-orchestrator.pem"\n'
)


def test_daemon_defaults(tmp_path: Path):
    """Phase 8d.19 dropped merge_mechanism — daemon now uses direct
    pr.merge() for both spec and impl PRs, same on every project. The
    other daemon-level defaults stay."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        + _APPS_TOML +
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.tick_seconds > 0
    assert config.max_in_flight > 0


def test_projects_round_trip(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        + _APPS_TOML +
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
        '[[projects]]\n'
        'name = "foreman"\n'
        'repo = "jeffrichley/foreman"\n'
        'local_clone_path = "/tmp/foreman"\n'
    )
    config = load_config(config_path)
    assert len(config.projects) == 2
    assert config.projects[0].name == "voice"
    assert config.projects[1].name == "foreman"


def test_missing_project_name_raises(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        + _APPS_TOML +
        '[[projects]]\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_project_config_default_trigger_label():
    p = ProjectConfig(
        name="voice", repo="jeffrichley/voice",
        local_clone_path="/tmp/voice",
    )
    assert p.trigger_label == "foreman:plan"


def test_role_timeout_seconds_default_600(tmp_path: Path):
    """Phase 5 carryover: SubprocessRoleDispatcher.timeout_seconds was hardcoded
    600s. V4Config now exposes it; Phase 7.5 will thread it through to the
    dispatcher constructor."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        + _APPS_TOML +
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.role_timeout_seconds == 600


def test_role_timeout_seconds_override(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        'role_timeout_seconds = 1200\n'
        + _APPS_TOML +
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.role_timeout_seconds == 1200


def test_max_state_attempts_default_3(tmp_path: Path):
    """Phase 8c.2 retry cap defaults to 3 — matches max_fix_attempts /
    max_impl_attempts shape and is the smallest cap that still allows
    one retry after a transient failure."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        + _APPS_TOML +
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.max_state_attempts == 3


def test_max_state_attempts_override(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        'max_state_attempts = 5\n'
        + _APPS_TOML +
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.max_state_attempts == 5


def test_max_state_attempts_zero_raises(tmp_path: Path):
    """ge=1 — a value of 0 would dump every ticket to NeedsHelp on
    first entry, which is never a valid configuration."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        'max_state_attempts = 0\n'
        + _APPS_TOML +
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    with pytest.raises(ValidationError):
        load_config(config_path)


# ---------------------------------------------------------------------------
# Task 8.3: [apps] + [orchestrator] sections
# ---------------------------------------------------------------------------


def test_missing_apps_block_raises(tmp_path: Path):
    """Task 8.3: ``[apps]`` is REQUIRED. A config without any app
    credentials cannot construct an IdentityRegistry, so the daemon
    refuses to start at load time. The error message should mention
    ``apps`` so the operator sees the gap immediately."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    with pytest.raises(ValidationError) as exc_info:
        load_config(config_path)
    assert "apps" in str(exc_info.value)


def test_missing_role_app_raises(tmp_path: Path):
    """Task 8.3: each of the four roles needs its own credentials.
    Dropping a single role (worker, here) still raises — partial
    identity wiring is worse than none, because the daemon would crash
    halfway through processing a ticket."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        '[apps.planner]\n'
        'app_id = 12345\n'
        'private_key_path = "/tmp/fake-planner.pem"\n'
        '[apps.reviewer]\n'
        'app_id = 12346\n'
        'private_key_path = "/tmp/fake-reviewer.pem"\n'
        '[apps.fixer]\n'
        'app_id = 12347\n'
        'private_key_path = "/tmp/fake-fixer.pem"\n'
        # No [apps.worker].
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    with pytest.raises(ValidationError) as exc_info:
        load_config(config_path)
    assert "worker" in str(exc_info.value)


def test_apps_orchestrator_round_trip(tmp_path: Path):
    """Task 8.4: full valid config round-trips through Pydantic. Each
    role's ``app_id`` + ``private_key_path`` is reachable from the
    loaded V4Config, and the ``[orchestrator]`` block carries its own
    App installation credentials (no env-var PAT)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        '[apps.planner]\n'
        'app_id = 12345\n'
        'private_key_path = "/tmp/fake-planner.pem"\n'
        '[apps.reviewer]\n'
        'app_id = 12346\n'
        'private_key_path = "/tmp/fake-reviewer.pem"\n'
        '[apps.fixer]\n'
        'app_id = 12347\n'
        'private_key_path = "/tmp/fake-fixer.pem"\n'
        '[apps.worker]\n'
        'app_id = 12348\n'
        'private_key_path = "/tmp/fake-worker.pem"\n'
        '[orchestrator]\n'
        'app_id = 99999\n'
        'private_key_path = "/tmp/fake-orchestrator.pem"\n'
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.apps.planner.app_id == 12345
    assert config.apps.planner.private_key_path == "/tmp/fake-planner.pem"
    assert config.apps.reviewer.app_id == 12346
    assert config.apps.reviewer.private_key_path == "/tmp/fake-reviewer.pem"
    assert config.apps.fixer.app_id == 12347
    assert config.apps.fixer.private_key_path == "/tmp/fake-fixer.pem"
    assert config.apps.worker.app_id == 12348
    assert config.apps.worker.private_key_path == "/tmp/fake-worker.pem"
    assert config.orchestrator.app_id == 99999
    assert config.orchestrator.private_key_path == "/tmp/fake-orchestrator.pem"


# ---------------------------------------------------------------------------
# Task 8b.2: ProjectConfig grows per-project fields the role CLIs need
# ---------------------------------------------------------------------------


def test_project_minimal_still_parses(tmp_path: Path):
    """Phase 8b.2: ProjectConfig keeps minimal-required shape.

    A TOML with only the historically-required fields (name/repo/
    local_clone_path) still loads, and all 4 new per-project fields take
    their sensible defaults. This proves existing v4 configs continue
    to work unchanged — operators do not have to touch their config to
    pick up the role-CLI rewire.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        + _APPS_TOML +
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    proj = config.projects[0]
    assert proj.check_command is None
    assert proj.dev_base_branch is None
    assert proj.max_fix_attempts == 3
    assert proj.max_impl_attempts == 3


def test_project_check_command_override(tmp_path: Path):
    """Phase 8b.2: per-project ``check_command`` flows through.

    Worker uses this verification command before claiming done;
    projects that don't run ``just check`` set it here.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        + _APPS_TOML +
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
        'check_command = "pytest"\n'
    )
    config = load_config(config_path)
    assert config.projects[0].check_command == "pytest"


def test_project_dev_base_branch_override(tmp_path: Path):
    """Phase 8b.2: per-project ``dev_base_branch`` flows through.

    Used for walking-skeleton phases where the active dev line lives
    on a feature branch rather than ``main``.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        + _APPS_TOML +
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
        'dev_base_branch = "develop"\n'
    )
    config = load_config(config_path)
    assert config.projects[0].dev_base_branch == "develop"


def test_project_max_fix_attempts_override(tmp_path: Path):
    """Phase 8b.2: per-project ``max_fix_attempts`` flows through.

    Fixer's preflight cap before NeedsHelp escalation.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        + _APPS_TOML +
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
        'max_fix_attempts = 5\n'
    )
    config = load_config(config_path)
    assert config.projects[0].max_fix_attempts == 5


def test_project_max_impl_attempts_override(tmp_path: Path):
    """Phase 8b.2: per-project ``max_impl_attempts`` flows through.

    Worker's preflight cap before NeedsHelp escalation. Audit during
    8b.2 implementation found Worker reads this field on v3
    ProjectConfig (worker.py:702), so v4 must grow it too even though
    the plan's starting field list omitted it.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        + _APPS_TOML +
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
        'max_impl_attempts = 5\n'
    )
    config = load_config(config_path)
    assert config.projects[0].max_impl_attempts == 5


def test_project_attempt_caps_reject_zero(tmp_path: Path):
    """Phase 8b.2 ge=1 constraint: matches v3 ProjectConfig validation.

    A value of 0 (or negative) would silently skip the role's retry
    loop and dump every ticket to NeedsHelp on first dispatch — a
    confusing config-corruption mode. Reject at load time so the
    daemon refuses to start instead.
    """
    for field, bad in (("max_fix_attempts", 0), ("max_impl_attempts", 0)):
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[daemon]\n'
            'db_path = "/tmp/foreman.db"\n'
            'log_dir = "/tmp/foreman-logs"\n'
            + _APPS_TOML
            + '[[projects]]\n'
            'name = "voice"\n'
            'repo = "jeffrichley/voice"\n'
            'local_clone_path = "/tmp/voice"\n'
            f'{field} = {bad}\n'
        )
        with pytest.raises(ValidationError):
            load_config(config_path)


def test_missing_orchestrator_raises(tmp_path: Path):
    """Task 8.4: ``[orchestrator]`` is REQUIRED. The pivot from env-var
    PAT to App installation tokens means the daemon literally needs an
    ``app_id`` + ``private_key_path`` to mint orchestrator-level
    tokens — no default makes sense. A missing block raises
    ValidationError at load time, same as the per-role [apps.*] blocks."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        '[apps.planner]\n'
        'app_id = 12345\n'
        'private_key_path = "/tmp/fake-planner.pem"\n'
        '[apps.reviewer]\n'
        'app_id = 12346\n'
        'private_key_path = "/tmp/fake-reviewer.pem"\n'
        '[apps.fixer]\n'
        'app_id = 12347\n'
        'private_key_path = "/tmp/fake-fixer.pem"\n'
        '[apps.worker]\n'
        'app_id = 12348\n'
        'private_key_path = "/tmp/fake-worker.pem"\n'
        # No [orchestrator] block.
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    with pytest.raises(ValidationError) as exc_info:
        load_config(config_path)
    assert "orchestrator" in str(exc_info.value)
