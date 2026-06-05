"""Tests for config loading with env-var override hierarchy (App-based auth)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from foreman.config import Config, ProjectConfig, ReconcilerConfig, load_config


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


def test_missing_reviewer_app_id_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_missing_fixer_app_id_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_missing_worker_app_id_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    config_path.write_text('[admin]\ngithub_token_env = "FOREMAN_ADMIN_TOKEN"\n')
    cfg = load_config(config_path)
    assert cfg.daemon.poll_interval_seconds == 30
    assert cfg.daemon.max_concurrent_workers == 1
    assert cfg.daemon.log_level == "INFO"
    assert cfg.daemon.log_path == "~/.foreman/daemon.log"
    assert cfg.daemon.sqlite_path == "~/.foreman/foreman.sqlite"
    assert cfg.daemon.role_dispatch_timeout_seconds is None


def test_daemon_config_rejects_max_workers_above_one(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[admin]\ngithub_token_env = "FOREMAN_ADMIN_TOKEN"\n[daemon]\nmax_concurrent_workers = 4\n'
    )
    with pytest.raises(ValidationError, match="max_concurrent_workers"):
        load_config(config_path)


def test_daemon_config_rejects_timeout_below_floor(tmp_path: Path) -> None:
    """When an operator opts into a role timeout, reject values below 30s —
    a cap smaller than the average role startup wedges all roles immediately,
    which is a worse failure mode than the hang we're guarding against. The
    default remains None (no cap)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[admin]\ngithub_token_env = "FOREMAN_ADMIN_TOKEN"\n'
        "[daemon]\nrole_dispatch_timeout_seconds = 10\n"
    )
    with pytest.raises(ValidationError, match="role_dispatch_timeout_seconds"):
        load_config(config_path)


def test_daemon_config_accepts_explicit_timeout(tmp_path: Path) -> None:
    """Operators opt into a wall-clock cap by setting a positive value. Pin
    the round-trip so we can see at a glance that the field still flows
    through TOML loading after switching its default to None."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[admin]\ngithub_token_env = "FOREMAN_ADMIN_TOKEN"\n'
        "[daemon]\nrole_dispatch_timeout_seconds = 1800\n"
    )
    cfg = load_config(config_path)
    assert cfg.daemon.role_dispatch_timeout_seconds == 1800


def test_project_config_auto_merge_defaults_to_none(tmp_path: Path) -> None:
    """When unset in TOML, per-project auto_merge_* read as ``None`` so the
    resolver can fall back to the global default (Task 4)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[admin]\ngithub_token_env = "FOREMAN_ADMIN_TOKEN"\n'
        "[projects.voice]\n"
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    cfg = load_config(config_path)
    project = cfg.projects["voice"]
    assert project.auto_merge_spec is None
    assert project.auto_merge_impl is None


def test_project_config_auto_merge_reads_true_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[admin]\ngithub_token_env = "FOREMAN_ADMIN_TOKEN"\n'
        "[projects.voice]\n"
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
        "auto_merge_spec = true\n"
        "auto_merge_impl = true\n"
    )
    cfg = load_config(config_path)
    project = cfg.projects["voice"]
    assert project.auto_merge_spec is True
    assert project.auto_merge_impl is True


# ----------------------------------------------------------------------
# ReconcilerConfig.auto_merge_spec / auto_merge_impl — global defaults
# with per-project override (Task 4 of v3 state-machine plan)
# ----------------------------------------------------------------------


def test_shutdown_sentinel_path_default() -> None:
    """The sentinel path defaults to ``~/.foreman/shutdown-requested``.

    Operators expect the v3 reconciler to look at that path without any
    extra config; ``daemon stop`` writes there by default. Drifting this
    default silently breaks the cross-platform graceful-shutdown contract.
    """
    cfg = ReconcilerConfig()
    assert cfg.shutdown_sentinel_path == "~/.foreman/shutdown-requested"


def test_shutdown_sentinel_path_override(tmp_path: Path) -> None:
    """The sentinel path is overridable via TOML for non-standard installs
    (multi-tenant boxes, per-instance daemons sharing the same home dir)."""
    config_file = tmp_path / "config.toml"
    custom_path = (tmp_path / "custom-shutdown").as_posix()
    config_file.write_text(
        f'[reconciler]\nshutdown_sentinel_path = "{custom_path}"\n'
    )
    cfg = load_config(config_file)
    assert cfg.reconciler.shutdown_sentinel_path == custom_path


def test_reconciler_reload_sentinel_path_default() -> None:
    """The reload sentinel defaults to ``~/.foreman/reload-requested``.

    Operators expect the v3 reconciler to look at that path without any
    extra config; ``daemon reload`` writes there by default. Drifting
    this default silently breaks the reload contract — the daemon would
    wait forever and the operator would see no config-reload row.
    """
    cfg = ReconcilerConfig()
    assert cfg.reload_sentinel_path == "~/.foreman/reload-requested"


def test_reconciler_reload_sentinel_path_override(tmp_path: Path) -> None:
    """The reload sentinel path is overridable via TOML for non-standard
    installs (multi-tenant boxes, per-instance daemons sharing the same
    home dir)."""
    config_file = tmp_path / "config.toml"
    custom_path = (tmp_path / "custom-reload").as_posix()
    config_file.write_text(
        f'[reconciler]\nreload_sentinel_path = "{custom_path}"\n'
    )
    cfg = load_config(config_file)
    assert cfg.reconciler.reload_sentinel_path == custom_path


def test_global_auto_merge_defaults() -> None:
    """Global defaults: spec auto-merge on, impl auto-merge off.

    Rationale: spec PRs are cheap to revert (just an .md file under
    docs/superpowers/specs/) so the daemon merges them by default to keep
    the autonomous loop moving. Impl PRs ship real code, so the daemon
    parks at ``foreman:ready-for-merge`` for human review unless the
    project opts in.
    """
    cfg = ReconcilerConfig()
    assert cfg.auto_merge_spec is True
    assert cfg.auto_merge_impl is False


def test_project_auto_merge_inherits_global_when_unset() -> None:
    """A project with no override inherits the reconciler's defaults via
    the ``effective_auto_merge_*`` resolvers."""
    cfg = ReconcilerConfig(auto_merge_spec=True, auto_merge_impl=False)
    project = ProjectConfig(repo="foo/bar", local_clone_path="/tmp/foo")
    assert cfg.effective_auto_merge_spec(project) is True
    assert cfg.effective_auto_merge_impl(project) is False


def test_project_auto_merge_override_wins() -> None:
    """A per-project ``auto_merge_spec=False`` overrides the global True."""
    cfg = ReconcilerConfig(auto_merge_spec=True, auto_merge_impl=False)
    project = ProjectConfig(
        repo="foo/bar",
        local_clone_path="/tmp/foo",
        auto_merge_spec=False,
    )
    assert cfg.effective_auto_merge_spec(project) is False


def test_project_can_enable_impl_auto_merge() -> None:
    """A per-project ``auto_merge_impl=True`` overrides the global False."""
    cfg = ReconcilerConfig(auto_merge_spec=True, auto_merge_impl=False)
    project = ProjectConfig(
        repo="foo/bar",
        local_clone_path="/tmp/foo",
        auto_merge_impl=True,
    )
    assert cfg.effective_auto_merge_impl(project) is True


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


# ----------------------------------------------------------------------
# ProjectConfig.max_fix_attempts / max_impl_attempts — configurable
# retry budgets (Foreman issue #14)
# ----------------------------------------------------------------------


def test_max_attempts_default_to_three(tmp_path: Path) -> None:
    """An omitted block reads ``3`` for both fields — the Field default
    is the single source of truth for the legacy hardcoded behavior."""
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
    project = cfg.projects["voice"]
    assert project.max_fix_attempts == 3
    assert project.max_impl_attempts == 3


def test_max_attempts_read_from_config_file(tmp_path: Path) -> None:
    """When set in TOML, both values flow through verbatim."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"
max_fix_attempts = 5
max_impl_attempts = 4

[projects.voice.apps]
planner_app_id = 123456
planner_private_key_path = "/tmp/planner.pem"
"""
    )
    cfg = load_config(config_file)
    project = cfg.projects["voice"]
    assert project.max_fix_attempts == 5
    assert project.max_impl_attempts == 4


def test_max_attempts_reject_zero(tmp_path: Path) -> None:
    """``max_fix_attempts = 0`` raises — ``ge=1`` enforces a positive budget."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"
max_fix_attempts = 0
"""
    )
    with pytest.raises(ValidationError, match="max_fix_attempts"):
        load_config(config_file)


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


def test_orchestrator_config_defaults_when_block_absent(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[admin]\ngithub_token_env = "FOREMAN_ADMIN_TOKEN"\n')
    cfg = load_config(config_file)
    assert cfg.orchestrator.app_id is None
    assert cfg.orchestrator.private_key_path is None
    assert cfg.orchestrator.app_id_env == "FOREMAN_ORCHESTRATOR_APP_ID"


def test_orchestrator_config_reads_from_toml(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[admin]\ngithub_token_env = "FOREMAN_ADMIN_TOKEN"\n'
        "[orchestrator]\n"
        "app_id = 3934489\n"
        'private_key_path = "/tmp/orchestrator.pem"\n'
    )
    cfg = load_config(config_file)
    assert cfg.orchestrator.app_id == 3934489
    assert cfg.orchestrator.private_key_path == "/tmp/orchestrator.pem"


def test_orchestrator_resolve_app_id_prefers_env(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[admin]\ngithub_token_env = "FOREMAN_ADMIN_TOKEN"\n'
        "[orchestrator]\napp_id = 100\n"
    )
    cfg = load_config(config_file)
    monkeypatch.setenv("FOREMAN_ORCHESTRATOR_APP_ID", "200")
    assert cfg.orchestrator.resolve_app_id() == 200


def test_orchestrator_resolve_app_id_raises_when_unset(
    tmp_path: Path, monkeypatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[admin]\ngithub_token_env = "X"\n')
    cfg = load_config(config_file)
    monkeypatch.delenv("FOREMAN_ORCHESTRATOR_APP_ID", raising=False)
    with pytest.raises(RuntimeError, match="orchestrator app_id"):
        cfg.orchestrator.resolve_app_id()


def test_orchestrator_resolve_private_key_path_returns_path(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[admin]\ngithub_token_env = "X"\n'
        "[orchestrator]\n"
        "app_id = 999\n"
        'private_key_path = "/tmp/orch.pem"\n'
    )
    cfg = load_config(config_file)
    assert cfg.orchestrator.resolve_private_key_path() == Path("/tmp/orch.pem")


def test_orchestrator_resolve_private_key_path_raises_when_unset(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[admin]\ngithub_token_env = "X"\n')
    cfg = load_config(config_file)
    with pytest.raises(RuntimeError, match="orchestrator.private_key_path"):
        cfg.orchestrator.resolve_private_key_path()


def test_foreman_config_path_env_var_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FOREMAN_CONFIG_PATH overrides the default host location."""
    custom = tmp_path / "custom-config.toml"
    custom.write_text(
        "[admin]\n"
        "github_token_env = \"FOREMAN_ADMIN_TOKEN\"\n"
    )
    monkeypatch.setenv("FOREMAN_CONFIG_PATH", str(custom))
    from foreman.config import resolve_config_path
    assert resolve_config_path() == custom


def test_foreman_config_path_falls_back_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """When FOREMAN_CONFIG_PATH is unset, fall back to ~/.foreman/config.toml."""
    monkeypatch.delenv("FOREMAN_CONFIG_PATH", raising=False)
    monkeypatch.delenv("FOREMAN_CONFIG", raising=False)
    from foreman.config import resolve_config_path
    assert resolve_config_path() == Path.home() / ".foreman" / "config.toml"


def test_foreman_config_path_honors_legacy_foreman_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Legacy FOREMAN_CONFIG env var is still honored as a fallback so
    existing operator workflows don't break. FOREMAN_CONFIG_PATH wins
    when both are set."""
    legacy = tmp_path / "legacy.toml"
    legacy.write_text("")
    monkeypatch.delenv("FOREMAN_CONFIG_PATH", raising=False)
    monkeypatch.setenv("FOREMAN_CONFIG", str(legacy))
    from foreman.config import resolve_config_path
    assert resolve_config_path() == legacy
