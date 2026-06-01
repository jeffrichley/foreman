"""Tests for config loading with env-var override hierarchy (App-based auth)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from foreman.config import Config, load_config


def test_load_config_returns_config(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[admin]
github_token_env = "FOREMAN_ADMIN_TOKEN"

[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "e:/workspaces/ai/agents/voice"

[projects.voice.apps]
planner_app_id_env = "FOREMAN_PLANNER_APP_ID"
planner_app_id = 123456
planner_private_key_path = "/tmp/planner.pem"
"""
    )
    cfg = load_config(config_file)
    assert isinstance(cfg, Config)
    assert cfg.projects["voice"].repo == "jeffrichley/voice"
    assert cfg.projects["voice"].apps.planner_app_id == 123456
    assert cfg.projects["voice"].apps.planner_private_key_path == "/tmp/planner.pem"


def test_env_var_overrides_config_file_app_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
planner_app_id_env = "FOREMAN_PLANNER_APP_ID"
planner_app_id = 111111
planner_private_key_path = "/tmp/planner.pem"
"""
    )
    monkeypatch.setenv("FOREMAN_PLANNER_APP_ID", "999999")
    cfg = load_config(config_file)
    resolved = cfg.projects["voice"].apps.resolve_planner_app_id()
    assert resolved == 999999, "env var must win over config-file app id"


def test_config_file_app_id_used_when_env_var_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
planner_app_id_env = "FOREMAN_PLANNER_APP_ID"
planner_app_id = 222222
planner_private_key_path = "/tmp/planner.pem"
"""
    )
    monkeypatch.delenv("FOREMAN_PLANNER_APP_ID", raising=False)
    cfg = load_config(config_file)
    assert cfg.projects["voice"].apps.resolve_planner_app_id() == 222222


def test_missing_app_id_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
planner_app_id_env = "FOREMAN_PLANNER_APP_ID"
planner_private_key_path = "/tmp/planner.pem"
"""
    )
    monkeypatch.delenv("FOREMAN_PLANNER_APP_ID", raising=False)
    cfg = load_config(config_file)
    with pytest.raises(RuntimeError, match="planner app_id"):
        cfg.projects["voice"].apps.resolve_planner_app_id()


def test_missing_private_key_path_raises(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
planner_app_id = 123456
"""
    )
    cfg = load_config(config_file)
    with pytest.raises(RuntimeError, match="planner_private_key_path"):
        cfg.projects["voice"].apps.resolve_planner_private_key_path()


def test_resolve_planner_private_key_path_returns_path_object(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
planner_app_id = 123456
planner_private_key_path = "/tmp/planner.pem"
"""
    )
    cfg = load_config(config_file)
    result = cfg.projects["voice"].apps.resolve_planner_private_key_path()
    assert isinstance(result, Path)
    assert str(result).replace("\\", "/") == "/tmp/planner.pem"


# ----------------------------------------------------------------------
# Reviewer App fields — mirror the planner pair
# ----------------------------------------------------------------------


def test_load_config_reads_reviewer_app_fields(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
planner_app_id = 123456
planner_private_key_path = "/tmp/planner.pem"
reviewer_app_id_env = "FOREMAN_REVIEWER_APP_ID"
reviewer_app_id = 654321
reviewer_private_key_path = "/tmp/reviewer.pem"
"""
    )
    cfg = load_config(config_file)
    assert cfg.projects["voice"].apps.reviewer_app_id == 654321
    assert cfg.projects["voice"].apps.reviewer_private_key_path == "/tmp/reviewer.pem"


def test_reviewer_app_fields_optional_for_planner_only_configs(tmp_path: Path) -> None:
    """Existing configs that only define planner fields must still load — the
    reviewer fields are optional during the thickening transition."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
planner_app_id = 123456
planner_private_key_path = "/tmp/planner.pem"
"""
    )
    cfg = load_config(config_file)
    assert cfg.projects["voice"].apps.reviewer_app_id is None
    assert cfg.projects["voice"].apps.reviewer_private_key_path is None


def test_env_var_overrides_config_file_reviewer_app_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
reviewer_app_id_env = "FOREMAN_REVIEWER_APP_ID"
reviewer_app_id = 111111
reviewer_private_key_path = "/tmp/reviewer.pem"
"""
    )
    monkeypatch.setenv("FOREMAN_REVIEWER_APP_ID", "999999")
    cfg = load_config(config_file)
    resolved = cfg.projects["voice"].apps.resolve_reviewer_app_id()
    assert resolved == 999999


def test_missing_reviewer_app_id_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
reviewer_app_id_env = "FOREMAN_REVIEWER_APP_ID"
reviewer_private_key_path = "/tmp/reviewer.pem"
"""
    )
    monkeypatch.delenv("FOREMAN_REVIEWER_APP_ID", raising=False)
    cfg = load_config(config_file)
    with pytest.raises(RuntimeError, match="reviewer app_id"):
        cfg.projects["voice"].apps.resolve_reviewer_app_id()


def test_missing_reviewer_private_key_path_raises(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
reviewer_app_id = 654321
"""
    )
    cfg = load_config(config_file)
    with pytest.raises(RuntimeError, match="reviewer_private_key_path"):
        cfg.projects["voice"].apps.resolve_reviewer_private_key_path()


# ----------------------------------------------------------------------
# Fixer App fields — mirror the planner / reviewer pair
# ----------------------------------------------------------------------


def test_load_config_reads_fixer_app_fields(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
planner_app_id = 123456
planner_private_key_path = "/tmp/planner.pem"
fixer_app_id_env = "FOREMAN_FIXER_APP_ID"
fixer_app_id = 777777
fixer_private_key_path = "/tmp/fixer.pem"
"""
    )
    cfg = load_config(config_file)
    assert cfg.projects["voice"].apps.fixer_app_id == 777777
    assert cfg.projects["voice"].apps.fixer_private_key_path == "/tmp/fixer.pem"


def test_fixer_app_fields_optional(tmp_path: Path) -> None:
    """Configs without fixer fields must still load — they're optional
    during the thickening transition."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
planner_app_id = 123456
planner_private_key_path = "/tmp/planner.pem"
"""
    )
    cfg = load_config(config_file)
    assert cfg.projects["voice"].apps.fixer_app_id is None
    assert cfg.projects["voice"].apps.fixer_private_key_path is None


def test_env_var_overrides_config_file_fixer_app_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
fixer_app_id_env = "FOREMAN_FIXER_APP_ID"
fixer_app_id = 111111
fixer_private_key_path = "/tmp/fixer.pem"
"""
    )
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "999999")
    cfg = load_config(config_file)
    assert cfg.projects["voice"].apps.resolve_fixer_app_id() == 999999


def test_missing_fixer_app_id_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
fixer_app_id_env = "FOREMAN_FIXER_APP_ID"
fixer_private_key_path = "/tmp/fixer.pem"
"""
    )
    monkeypatch.delenv("FOREMAN_FIXER_APP_ID", raising=False)
    cfg = load_config(config_file)
    with pytest.raises(RuntimeError, match="fixer app_id"):
        cfg.projects["voice"].apps.resolve_fixer_app_id()


def test_missing_fixer_private_key_path_raises(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
fixer_app_id = 777777
"""
    )
    cfg = load_config(config_file)
    with pytest.raises(RuntimeError, match="fixer_private_key_path"):
        cfg.projects["voice"].apps.resolve_fixer_private_key_path()


# ----------------------------------------------------------------------
# Worker App fields — mirror the planner / reviewer / fixer pair
# ----------------------------------------------------------------------


def test_load_config_reads_worker_app_fields(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
planner_app_id = 123456
planner_private_key_path = "/tmp/planner.pem"
worker_app_id_env = "FOREMAN_WORKER_APP_ID"
worker_app_id = 444444
worker_private_key_path = "/tmp/worker.pem"
"""
    )
    cfg = load_config(config_file)
    assert cfg.projects["voice"].apps.worker_app_id == 444444
    assert cfg.projects["voice"].apps.worker_private_key_path == "/tmp/worker.pem"


def test_worker_app_fields_optional(tmp_path: Path) -> None:
    """Configs without worker fields must still load — they're optional
    during the thickening transition."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
planner_app_id = 123456
planner_private_key_path = "/tmp/planner.pem"
"""
    )
    cfg = load_config(config_file)
    assert cfg.projects["voice"].apps.worker_app_id is None
    assert cfg.projects["voice"].apps.worker_private_key_path is None


def test_env_var_overrides_config_file_worker_app_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
worker_app_id_env = "FOREMAN_WORKER_APP_ID"
worker_app_id = 111111
worker_private_key_path = "/tmp/worker.pem"
"""
    )
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "999999")
    cfg = load_config(config_file)
    assert cfg.projects["voice"].apps.resolve_worker_app_id() == 999999


def test_missing_worker_app_id_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
worker_app_id_env = "FOREMAN_WORKER_APP_ID"
worker_private_key_path = "/tmp/worker.pem"
"""
    )
    monkeypatch.delenv("FOREMAN_WORKER_APP_ID", raising=False)
    cfg = load_config(config_file)
    with pytest.raises(RuntimeError, match="worker app_id"):
        cfg.projects["voice"].apps.resolve_worker_app_id()


def test_missing_worker_private_key_path_raises(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
worker_app_id = 444444
"""
    )
    cfg = load_config(config_file)
    with pytest.raises(RuntimeError, match="worker_private_key_path"):
        cfg.projects["voice"].apps.resolve_worker_private_key_path()


# ----------------------------------------------------------------------
# ProjectConfig.check_command — configurable, default None
# ----------------------------------------------------------------------


def test_check_command_default_is_none(tmp_path: Path) -> None:
    """Existing configs that omit ``check_command`` must still load and
    return ``None`` (the orchestrator resolves None → ``'just check'``).
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
planner_app_id = 123456
planner_private_key_path = "/tmp/planner.pem"
"""
    )
    cfg = load_config(config_file)
    assert cfg.projects["voice"].check_command is None


def test_check_command_reads_from_config_file(tmp_path: Path) -> None:
    """When ``check_command`` is set in TOML, it surfaces verbatim through
    the loaded config.
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"
check_command = "make test"

[projects.voice.apps]
planner_app_id = 123456
planner_private_key_path = "/tmp/planner.pem"
"""
    )
    cfg = load_config(config_file)
    assert cfg.projects["voice"].check_command == "make test"


# ----------------------------------------------------------------------
# ProjectConfig.dev_base_branch — alternate base branch for spec worktrees
# (Foreman issue #16)
#
# When a project's active development line lives on a feature branch (e.g.
# a walking-skeleton phase) rather than on ``main``, the Planner needs to
# branch new spec worktrees from ``origin/<that-branch>`` instead of
# ``origin/<default>``. ``dev_base_branch`` is the operator-explicit opt-in;
# auto-detection is deliberately out of scope for v1.
# ----------------------------------------------------------------------


def test_dev_base_branch_default_is_none(tmp_path: Path) -> None:
    """Existing configs that omit ``dev_base_branch`` must still load and
    return ``None`` (the orchestrator falls back to origin/<default>).
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
planner_app_id = 123456
planner_private_key_path = "/tmp/planner.pem"
"""
    )
    cfg = load_config(config_file)
    assert cfg.projects["voice"].dev_base_branch is None


def test_daemon_config_has_sane_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[admin]\ngithub_token_env = \"FOREMAN_ADMIN_TOKEN\"\n"
    )
    cfg = load_config(config_path)
    assert cfg.daemon.poll_interval_seconds == 30
    assert cfg.daemon.max_concurrent_workers == 1
    assert cfg.daemon.log_level == "INFO"
    assert cfg.daemon.log_path == "~/.foreman/daemon.log"
    assert cfg.daemon.sqlite_path == "~/.foreman/foreman.sqlite"


def test_daemon_config_rejects_max_workers_above_one(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[admin]\ngithub_token_env = \"FOREMAN_ADMIN_TOKEN\"\n"
        "[daemon]\nmax_concurrent_workers = 4\n"
    )
    with pytest.raises(ValidationError, match="max_concurrent_workers"):
        load_config(config_path)


def test_project_config_auto_merge_defaults_to_false(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[admin]\ngithub_token_env = \"FOREMAN_ADMIN_TOKEN\"\n"
        "[projects.voice]\n"
        "repo = \"jeffrichley/voice\"\n"
        "local_clone_path = \"/tmp/voice\"\n"
    )
    cfg = load_config(config_path)
    project = cfg.projects["voice"]
    assert project.auto_merge_spec is False
    assert project.auto_merge_impl is False


def test_project_config_auto_merge_reads_true_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[admin]\ngithub_token_env = \"FOREMAN_ADMIN_TOKEN\"\n"
        "[projects.voice]\n"
        "repo = \"jeffrichley/voice\"\n"
        "local_clone_path = \"/tmp/voice\"\n"
        "auto_merge_spec = true\n"
        "auto_merge_impl = true\n"
    )
    cfg = load_config(config_path)
    project = cfg.projects["voice"]
    assert project.auto_merge_spec is True
    assert project.auto_merge_impl is True


def test_dev_base_branch_reads_from_config_file(tmp_path: Path) -> None:
    """When ``dev_base_branch`` is set in TOML, it surfaces verbatim through
    the loaded config so the Planner can pass it to ``WorktreeManager.create``.
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.foreman]
repo = "jeffrichley/foreman"
local_clone_path = "/tmp/foreman"
dev_base_branch = "feat/walking-skeleton"

[projects.foreman.apps]
planner_app_id = 123456
planner_private_key_path = "/tmp/planner.pem"
"""
    )
    cfg = load_config(config_file)
    assert cfg.projects["foreman"].dev_base_branch == "feat/walking-skeleton"


def test_dev_base_branch_explicit_none_round_trips(tmp_path: Path) -> None:
    """Omitting ``dev_base_branch`` in TOML round-trips to ``None`` —
    same as the field not appearing at all. We pin this so a future
    refactor doesn't accidentally coerce missing keys to empty strings.
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"
check_command = "just check"

[projects.voice.apps]
planner_app_id = 123456
planner_private_key_path = "/tmp/planner.pem"
"""
    )
    cfg = load_config(config_file)
    assert cfg.projects["voice"].dev_base_branch is None
