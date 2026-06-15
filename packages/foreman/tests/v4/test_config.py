"""V4Config — TOML-loaded settings with MergeQueue default."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from foreman.v4.config import ProjectConfig, load_config


def test_defaults_set_merge_mechanism_to_queue(tmp_path: Path):
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
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.role_timeout_seconds == 1200
