"""Tests for config loading with env-var override hierarchy."""

from __future__ import annotations

from pathlib import Path

import pytest

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

[projects.voice.bots]
planner_env = "FOREMAN_PLANNER_BOT_TOKEN"
planner_token = "config-file-token"
"""
    )
    cfg = load_config(config_file)
    assert isinstance(cfg, Config)
    assert cfg.projects["voice"].repo == "jeffrichley/voice"
    assert cfg.projects["voice"].bots.planner_token == "config-file-token"


def test_env_var_overrides_config_file_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.bots]
planner_env = "FOREMAN_PLANNER_BOT_TOKEN"
planner_token = "config-file-token"
"""
    )
    monkeypatch.setenv("FOREMAN_PLANNER_BOT_TOKEN", "env-var-token")
    cfg = load_config(config_file)
    resolved = cfg.projects["voice"].bots.resolve_planner_token()
    assert resolved == "env-var-token", "env var must win over config-file token"


def test_config_file_token_used_when_env_var_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.bots]
planner_env = "FOREMAN_PLANNER_BOT_TOKEN"
planner_token = "config-file-token"
"""
    )
    monkeypatch.delenv("FOREMAN_PLANNER_BOT_TOKEN", raising=False)
    cfg = load_config(config_file)
    assert cfg.projects["voice"].bots.resolve_planner_token() == "config-file-token"


def test_missing_token_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.bots]
planner_env = "FOREMAN_PLANNER_BOT_TOKEN"
"""
    )
    monkeypatch.delenv("FOREMAN_PLANNER_BOT_TOKEN", raising=False)
    cfg = load_config(config_file)
    with pytest.raises(RuntimeError, match="planner token"):
        cfg.projects["voice"].bots.resolve_planner_token()
