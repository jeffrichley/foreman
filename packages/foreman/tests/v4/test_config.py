"""V4Config — TOML-loaded settings with MergeQueue default."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from foreman.v4.config import ProjectConfig, load_config

# Shared `[apps.*]` block used by every existing test. As of Task 8.3
# `apps` is REQUIRED on V4Config — leaving it out raises ValidationError
# at load time, which is the intended production behavior (the daemon
# refuses to start without per-role identity wiring). Concatenating this
# helper into each test's TOML keeps the existing assertions focused on
# what they're actually testing (merge_mechanism, projects, etc.) instead
# of rewriting every TOML string inline.
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
)


def test_defaults_set_merge_mechanism_to_queue(tmp_path: Path):
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
    assert config.merge_mechanism == "queue"
    assert config.tick_seconds > 0
    assert config.max_in_flight > 0


def test_explicit_merge_mechanism_override(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        'merge_mechanism = "merge"\n'
        + _APPS_TOML +
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.merge_mechanism == "merge"


def test_invalid_merge_mechanism_raises(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        'merge_mechanism = "not-a-thing"\n'
        + _APPS_TOML +
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    with pytest.raises(ValidationError):
        load_config(config_path)


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
    """Task 8.3: full valid config round-trips through Pydantic. Each
    role's ``app_id`` + ``private_key_path`` is reachable from the
    loaded V4Config, and an explicit ``[orchestrator]`` block overrides
    the default env-var name."""
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
        'pat_env_var = "MY_CUSTOM_PAT_ENV"\n'
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
    assert config.orchestrator.pat_env_var == "MY_CUSTOM_PAT_ENV"


def test_orchestrator_default_when_omitted(tmp_path: Path):
    """Task 8.3: ``[orchestrator]`` is optional. Absent block means the
    default env-var name (``FOREMAN_ORCHESTRATOR_PAT``) applies."""
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
    assert config.orchestrator.pat_env_var == "FOREMAN_ORCHESTRATOR_PAT"
