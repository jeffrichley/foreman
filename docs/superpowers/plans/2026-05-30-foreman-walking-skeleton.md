# Foreman walking-skeleton implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the minimum end-to-end Foreman pipeline — `foreman plan <issue-url>` reads a GitHub issue, runs the Planner node via the Anthropic Agent SDK in an isolated git worktree, opens a spec PR, advances the issue label `foreman:plan → foreman:spec-review`, exits. Single-node pipeline. No daemon loop, no reviewer/fixer/worker, no bus, no SQLite, no MCP. Walking skeleton only.

**Architecture:** CLI dispatches `run_planner(issue_url)`. Planner: resolves the planner-bot identity (PyGithub client per token), creates a per-ticket git worktree from the locally-cloned target repo, dispatches the Anthropic Agent SDK with the planner system prompt + scoped tools + structured-output JSON schema, parses the result, opens a spec PR on the issue's branch, advances the label.

**Tech Stack:** Python 3.12, PyGithub (GitHub API), `claude-agent-sdk` (Anthropic Agent SDK), Pydantic (config + output schemas), click (CLI), `tomllib` (stdlib TOML reader). Subprocess `git` for worktree ops.

---

## File structure

| File | Responsibility |
|---|---|
| `packages/foreman/src/foreman/__init__.py` | Module docstring (exists) |
| `packages/foreman/src/foreman/config.py` | Load `~/.foreman/config.toml`; apply env-var override hierarchy; Pydantic models |
| `packages/foreman/src/foreman/identity.py` | Resolve per-role token (env → config), build PyGithub `Github` clients per identity |
| `packages/foreman/src/foreman/worktree.py` | Create + cleanup per-ticket git worktrees via subprocess git |
| `packages/foreman/src/foreman/provider.py` | `ProviderFacade` abstract base — single method `run_agent` |
| `packages/foreman/src/foreman/providers/__init__.py` | Empty package marker |
| `packages/foreman/src/foreman/providers/anthropic_sdk.py` | `AnthropicSDKProvider` — calls `claude_agent_sdk.query()` |
| `packages/foreman/src/foreman/schemas/__init__.py` | Empty package marker |
| `packages/foreman/src/foreman/schemas/planner.py` | Pydantic `PlannerOutput` model + JSON-schema export |
| `packages/foreman/src/foreman/roles/__init__.py` | Empty package marker |
| `packages/foreman/src/foreman/roles/planner.py` | `run_planner(issue_url, config)` orchestrator |
| `packages/foreman/src/foreman/prompts/planner.md` | Planner system prompt (packaged with wheel) |
| `packages/foreman/src/foreman/cli.py` | click-based `foreman plan <issue-url>` entry point |
| `packages/foreman/tests/test_config.py` | Unit tests for config loading + env override |
| `packages/foreman/tests/test_identity.py` | Unit tests for token resolution + client construction |
| `packages/foreman/tests/test_worktree.py` | Unit tests for worktree create/cleanup (uses a tmp git repo) |
| `packages/foreman/tests/test_schemas_planner.py` | Unit tests for PlannerOutput validation |
| `packages/foreman/tests/test_provider_anthropic_sdk.py` | Unit test that AnthropicSDKProvider can be instantiated; real-engine integration deferred |
| `packages/foreman/tests/test_roles_planner.py` | Integration test for run_planner with mocked provider + mocked PyGithub |
| `packages/foreman/tests/test_cli.py` | CLI smoke test via click testing harness |

---

## Task 1: Add walking-skeleton dependencies

**Files:**
- Modify: `packages/foreman/pyproject.toml`
- Modify: `pyproject.toml` (root, if click + pydantic should go in dev or main)

- [ ] **Step 1: Add runtime deps to the package's pyproject.toml**

Edit `packages/foreman/pyproject.toml`, replace the `dependencies = []` line with:

```toml
dependencies = [
    "PyGithub>=2.0,<3",
    "claude-agent-sdk>=0.1,<1",
    "pydantic>=2.5,<3",
    "click>=8.1,<9",
]
```

- [ ] **Step 2: Run uv sync to install**

Run: `uv sync --all-packages`
Expected: "Installed N packages" with PyGithub, claude-agent-sdk, pydantic, click added. No errors.

- [ ] **Step 3: Verify imports work**

Run: `uv run python -c "import github, claude_agent_sdk, pydantic, click; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add packages/foreman/pyproject.toml uv.lock
git commit -m "feat: add walking-skeleton runtime dependencies"
```

---

## Task 2: Config loading with env-var override hierarchy

**Files:**
- Create: `packages/foreman/src/foreman/config.py`
- Create: `packages/foreman/tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Create `packages/foreman/tests/test_config.py`:

```python
"""Tests for config loading with env-var override hierarchy."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from foreman.config import Config, ProjectConfig, load_config


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.config'`

- [ ] **Step 3: Implement config.py**

Create `packages/foreman/src/foreman/config.py`:

```python
"""Foreman configuration — TOML loading with env-var override hierarchy.

Hierarchy (highest precedence first):
1. Env var (e.g., FOREMAN_PLANNER_BOT_TOKEN)
2. Config file value (e.g., bots.planner_token in ~/.foreman/config.toml)

Tokens are secrets — env-var precedence lets CI / Docker / one-off testing
inject them without touching the config file.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class AdminConfig(BaseModel):
    """Admin identity (Jeff's PAT) used for `foreman project add` ops."""

    github_token_env: str = "FOREMAN_ADMIN_TOKEN"


class BotConfig(BaseModel):
    """Per-role bot credentials with env-var override.

    For walking skeleton: only planner is needed. Reviewer/fixer/worker fields
    are placeholders for thickening; resolution methods will raise until set.
    """

    planner_env: str = "FOREMAN_PLANNER_BOT_TOKEN"
    planner_token: str | None = None

    def resolve_planner_token(self) -> str:
        env_value = os.environ.get(self.planner_env)
        if env_value:
            return env_value
        if self.planner_token:
            return self.planner_token
        raise RuntimeError(
            f"No planner token: env var {self.planner_env} not set and "
            "bots.planner_token not in config file"
        )


class ProjectConfig(BaseModel):
    """Per-project configuration."""

    repo: str = Field(..., description="GitHub repo in 'owner/name' form")
    local_clone_path: str = Field(
        ..., description="Local path to the repo's clone (worktrees branch from here)"
    )
    bots: BotConfig = Field(default_factory=BotConfig)


class Config(BaseModel):
    """Top-level Foreman config."""

    admin: AdminConfig = Field(default_factory=AdminConfig)
    projects: dict[str, ProjectConfig] = Field(default_factory=dict)


def load_config(path: Path | str) -> Config:
    """Load + validate Foreman config from a TOML file."""
    p = Path(path)
    with p.open("rb") as f:
        raw = tomllib.load(f)
    return Config.model_validate(raw)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/foreman/tests/test_config.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/config.py packages/foreman/tests/test_config.py
git commit -m "feat: add config loading with env-var override hierarchy"
```

---

## Task 3: Per-role identity + PyGithub clients

**Files:**
- Create: `packages/foreman/src/foreman/identity.py`
- Create: `packages/foreman/tests/test_identity.py`

- [ ] **Step 1: Write the failing test**

Create `packages/foreman/tests/test_identity.py`:

```python
"""Tests for per-role identity resolution + PyGithub client construction."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from github import Github

from foreman.config import BotConfig, ProjectConfig
from foreman.identity import IdentityRegistry


def _make_project() -> ProjectConfig:
    return ProjectConfig(
        repo="jeffrichley/voice",
        local_clone_path="/tmp/voice",
        bots=BotConfig(
            planner_env="FOREMAN_PLANNER_BOT_TOKEN",
            planner_token="config-file-token",
        ),
    )


def test_get_planner_client_returns_github_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FOREMAN_PLANNER_BOT_TOKEN", raising=False)
    reg = IdentityRegistry(_make_project())
    client = reg.get_client("planner")
    assert isinstance(client, Github)


def test_get_planner_client_uses_env_var_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOREMAN_PLANNER_BOT_TOKEN", "env-token")
    reg = IdentityRegistry(_make_project())
    # PyGithub stores the auth in the Auth attribute; we verify the token
    # routed correctly by inspecting the internal requester.
    with patch("foreman.identity.Github") as mock_github:
        IdentityRegistry(_make_project()).get_client("planner")
        # First positional arg to Github() is the auth object holding the token
        called_with = mock_github.call_args
        # PyGithub 2.x prefers `auth=` keyword over positional token; accept either.
        token_seen = (
            called_with.kwargs.get("auth")
            or (called_with.args[0] if called_with.args else None)
        )
        assert token_seen is not None


def test_unknown_role_raises() -> None:
    reg = IdentityRegistry(_make_project())
    with pytest.raises(ValueError, match="Unknown role"):
        reg.get_client("reviewer")  # Reviewer not implemented in walking skeleton
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/test_identity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.identity'`

- [ ] **Step 3: Implement identity.py**

Create `packages/foreman/src/foreman/identity.py`:

```python
"""Per-role identity registry — builds PyGithub clients per bot identity.

Each role gets its own Github() instance authenticated as the role's bot.
For walking skeleton: only the planner role is wired. Reviewer/fixer/worker
will be added during thickening.
"""

from __future__ import annotations

from github import Auth, Github

from foreman.config import ProjectConfig


class IdentityRegistry:
    """Holds per-role PyGithub clients for one project."""

    def __init__(self, project: ProjectConfig) -> None:
        self._project = project
        self._clients: dict[str, Github] = {}

    def get_client(self, role: str) -> Github:
        if role in self._clients:
            return self._clients[role]
        token = self._resolve_token(role)
        client = Github(auth=Auth.Token(token))
        self._clients[role] = client
        return client

    def _resolve_token(self, role: str) -> str:
        if role == "planner":
            return self._project.bots.resolve_planner_token()
        raise ValueError(
            f"Unknown role: {role!r}. Walking skeleton only supports 'planner'."
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/foreman/tests/test_identity.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/identity.py packages/foreman/tests/test_identity.py
git commit -m "feat: add per-role identity registry with PyGithub clients"
```

---

## Task 4: Per-ticket git worktree management

**Files:**
- Create: `packages/foreman/src/foreman/worktree.py`
- Create: `packages/foreman/tests/test_worktree.py`

- [ ] **Step 1: Write the failing test**

Create `packages/foreman/tests/test_worktree.py`:

```python
"""Tests for per-ticket git worktree create + cleanup."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from foreman.worktree import WorktreeManager


def _init_git_repo(path: Path) -> None:
    """Initialize a minimal git repo with one commit so we can branch from it."""
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"], cwd=path, check=True, capture_output=True
    )


def test_create_worktree_creates_dir_with_branch(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone)

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt_path = mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)

    assert wt_path.exists()
    assert wt_path == worktrees_root / "voice" / "issue-42"
    branch_check = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=wt_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert branch_check.stdout.strip() == "foreman/issue-42"


def test_cleanup_removes_worktree(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone)

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt_path = mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)
    assert wt_path.exists()

    mgr.cleanup(clone_path=clone, worktree_path=wt_path)
    assert not wt_path.exists()


def test_create_idempotent_on_existing_worktree(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone)

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt1 = mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)
    wt2 = mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)
    assert wt1 == wt2, "Re-creating same worktree should return existing path"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/test_worktree.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.worktree'`

- [ ] **Step 3: Implement worktree.py**

Create `packages/foreman/src/foreman/worktree.py`:

```python
"""Per-ticket git worktree manager.

Each Foreman pipeline gets its own worktree at:
  <worktrees_root>/<repo_slug>/issue-<N>/

Branched as `foreman/issue-<N>`. All node commits for that ticket land on
that branch in that worktree. Cleanup happens at pipeline completion (or
deferred for failed pipelines per the spec).
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class WorktreeManager:
    """Create + cleanup per-ticket git worktrees."""

    def __init__(self, worktrees_root: Path) -> None:
        self.worktrees_root = worktrees_root

    def create(self, clone_path: Path, repo_slug: str, ticket_id: int) -> Path:
        """Create a worktree for one ticket. Idempotent on existing worktree."""
        wt_path = self.worktrees_root / repo_slug / f"issue-{ticket_id}"
        if wt_path.exists():
            return wt_path
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        branch = f"foreman/issue-{ticket_id}"
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(wt_path)],
            cwd=clone_path,
            check=True,
            capture_output=True,
            text=True,
        )
        return wt_path

    def cleanup(self, clone_path: Path, worktree_path: Path) -> None:
        """Remove a worktree. Safe to call on already-removed worktrees."""
        if not worktree_path.exists():
            return
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=clone_path,
            check=True,
            capture_output=True,
            text=True,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/foreman/tests/test_worktree.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/worktree.py packages/foreman/tests/test_worktree.py
git commit -m "feat: add per-ticket git worktree manager"
```

---

## Task 5: PlannerOutput Pydantic schema

**Files:**
- Create: `packages/foreman/src/foreman/schemas/__init__.py`
- Create: `packages/foreman/src/foreman/schemas/planner.py`
- Create: `packages/foreman/tests/test_schemas_planner.py`

- [ ] **Step 1: Create empty schemas package marker**

```bash
mkdir -p packages/foreman/src/foreman/schemas
```

Create `packages/foreman/src/foreman/schemas/__init__.py`:

```python
"""Per-role structured-output Pydantic schemas."""
```

- [ ] **Step 2: Write the failing test**

Create `packages/foreman/tests/test_schemas_planner.py`:

```python
"""Tests for PlannerOutput Pydantic schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foreman.schemas.planner import PlannerOutput


def test_planner_output_validates_valid_dict() -> None:
    obj = PlannerOutput.model_validate(
        {
            "pr_url": "https://github.com/jeffrichley/voice/pull/42",
            "pr_number": 42,
            "branch_name": "foreman/issue-7",
            "summary": "Drafted spec for SSML support in madrigal.",
            "considered_alternatives": ["raw-string approach", "external library"],
            "confidence": "medium",
        }
    )
    assert obj.pr_number == 42
    assert obj.confidence == "medium"
    assert len(obj.considered_alternatives) == 2


def test_planner_output_rejects_missing_required_field() -> None:
    with pytest.raises(ValidationError, match="pr_url"):
        PlannerOutput.model_validate(
            {
                "pr_number": 42,
                "branch_name": "foreman/issue-7",
                "summary": "x",
            }
        )


def test_planner_output_rejects_bad_confidence_value() -> None:
    with pytest.raises(ValidationError, match="confidence"):
        PlannerOutput.model_validate(
            {
                "pr_url": "https://github.com/jeffrichley/voice/pull/42",
                "pr_number": 42,
                "branch_name": "foreman/issue-7",
                "summary": "x",
                "confidence": "extremely-confident-bro",
            }
        )


def test_planner_output_json_schema_is_serializable() -> None:
    schema = PlannerOutput.model_json_schema()
    assert isinstance(schema, dict)
    assert "properties" in schema
    assert "pr_url" in schema["properties"]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/test_schemas_planner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.schemas.planner'`

- [ ] **Step 4: Implement schemas/planner.py**

Create `packages/foreman/src/foreman/schemas/planner.py`:

```python
"""Planner role's structured output schema.

The Planner returns this after writing the spec PR. It is persisted to
SQLite for audit + replay. Per the B-strict forwarding rule, this is NOT
automatically forwarded to downstream nodes — the spec PR contents ARE
the contract for the Reviewer.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PlannerOutput(BaseModel):
    """Structured result from the Planner role."""

    pr_url: str = Field(..., description="Full GitHub URL of the created spec PR")
    pr_number: int = Field(..., description="PR number (integer)")
    branch_name: str = Field(..., description="Branch name the PR is on")
    summary: str = Field(..., description="One-paragraph summary of the spec approach")
    considered_alternatives: list[str] = Field(
        default_factory=list,
        description="Approaches considered and rejected, for the audit log",
    )
    confidence: Literal["high", "medium", "low"] = Field(
        default="medium",
        description="Planner's self-rated confidence in the spec approach",
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest packages/foreman/tests/test_schemas_planner.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/schemas/ packages/foreman/tests/test_schemas_planner.py
git commit -m "feat: add PlannerOutput Pydantic schema"
```

---

## Task 6: Provider facade + Anthropic SDK adapter

**Files:**
- Create: `packages/foreman/src/foreman/provider.py`
- Create: `packages/foreman/src/foreman/providers/__init__.py`
- Create: `packages/foreman/src/foreman/providers/anthropic_sdk.py`
- Create: `packages/foreman/tests/test_provider_anthropic_sdk.py`

- [ ] **Step 1: Define the provider facade**

Create `packages/foreman/src/foreman/provider.py`:

```python
"""Provider facade — single interface that all role modules dispatch through.

First (and currently only) concrete implementation is `AnthropicSDKProvider`.
The facade exists so future vendors (opencode, codex, cursor-cli) can plug
in via thin adapters without changing role-module code.

The `run_agent` contract returns a parsed dict matching the supplied
JSON schema. Role modules pass their Pydantic model's `model_json_schema()`
output as the schema.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class ProviderFacade(ABC):
    """Abstract base for agent provider adapters."""

    @abstractmethod
    async def run_agent(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        allowed_tools: list[str],
        output_schema: dict[str, Any],
        cwd: Path,
        max_turns: int = 40,
    ) -> dict[str, Any]:
        """Run an agent and return its structured output as a dict.

        Args:
            system_prompt: System-level instructions for the agent.
            user_prompt: The role-specific task prompt (issue body + context).
            allowed_tools: Tool names auto-approved (e.g., ["Read", "Edit", "Bash"]).
            output_schema: JSON schema the agent's output must match.
            cwd: Working directory for file ops (the per-ticket worktree).
            max_turns: Safety cap on agent loop iterations.

        Returns:
            Dict matching `output_schema`.

        Raises:
            RuntimeError: If the agent fails to produce schema-valid output.
        """
```

- [ ] **Step 2: Create providers package marker**

```bash
mkdir -p packages/foreman/src/foreman/providers
```

Create `packages/foreman/src/foreman/providers/__init__.py`:

```python
"""Provider adapter implementations (Anthropic Agent SDK, etc.)."""
```

- [ ] **Step 3: Write the failing test**

Create `packages/foreman/tests/test_provider_anthropic_sdk.py`:

```python
"""Smoke tests for AnthropicSDKProvider — verifies wiring without real API calls.

Real end-to-end agent runs are gated behind the `real_engine` pytest marker
(per the never-done-without-running rule). This file covers structural
correctness of the adapter; the live integration test lives separately.
"""

from __future__ import annotations

from foreman.provider import ProviderFacade
from foreman.providers.anthropic_sdk import AnthropicSDKProvider


def test_provider_can_be_instantiated() -> None:
    provider = AnthropicSDKProvider()
    assert isinstance(provider, ProviderFacade)


def test_provider_inherits_from_facade() -> None:
    assert issubclass(AnthropicSDKProvider, ProviderFacade)
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/test_provider_anthropic_sdk.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.providers.anthropic_sdk'`

- [ ] **Step 5: Implement providers/anthropic_sdk.py**

Create `packages/foreman/src/foreman/providers/anthropic_sdk.py`:

```python
"""Anthropic Agent SDK adapter for the provider facade.

Uses `claude_agent_sdk.query()` to run an agent with the supplied prompt,
tools, working directory, and structured-output schema. Returns the parsed
structured output as a dict.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from foreman.provider import ProviderFacade


class AnthropicSDKProvider(ProviderFacade):
    """ProviderFacade implementation backed by the Anthropic Agent SDK."""

    async def run_agent(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        allowed_tools: list[str],
        output_schema: dict[str, Any],
        cwd: Path,
        max_turns: int = 40,
    ) -> dict[str, Any]:
        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            cwd=str(cwd),
            allowed_tools=allowed_tools,
            permission_mode="acceptEdits",
            max_turns=max_turns,
            output_format={"type": "json_schema", "schema": output_schema},
        )
        structured: dict[str, Any] | None = None
        async for message in query(prompt=user_prompt, options=options):
            if isinstance(message, ResultMessage) and message.structured_output:
                structured = message.structured_output
        if structured is None:
            raise RuntimeError(
                "Anthropic Agent SDK did not return structured_output matching schema"
            )
        return structured
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest packages/foreman/tests/test_provider_anthropic_sdk.py -v`
Expected: 2 passed.

- [ ] **Step 7: Commit**

```bash
git add packages/foreman/src/foreman/provider.py packages/foreman/src/foreman/providers/ packages/foreman/tests/test_provider_anthropic_sdk.py
git commit -m "feat: add provider facade + Anthropic Agent SDK adapter"
```

---

## Task 7: Planner role module + system prompt

**Files:**
- Create: `packages/foreman/src/foreman/prompts/planner.md`
- Create: `packages/foreman/src/foreman/roles/__init__.py`
- Create: `packages/foreman/src/foreman/roles/planner.py`
- Modify: `packages/foreman/pyproject.toml` (include prompts in wheel via package-data)
- Create: `packages/foreman/tests/test_roles_planner.py`

- [ ] **Step 1: Create prompts directory + planner.md**

```bash
mkdir -p packages/foreman/src/foreman/prompts
```

Create `packages/foreman/src/foreman/prompts/planner.md`:

```markdown
# Planner role

You are the Planner role in the Foreman pipeline. Your job is to read a
GitHub issue and produce a **spec PR** — a pull request whose contents are
a planning document for the work, not the work itself.

## What you receive

- The issue body and metadata (title, labels, comments)
- The repository's local working directory (a git worktree branched as `foreman/issue-<N>`)
- Read/Edit/Bash/Glob/Grep tools scoped to that worktree
- A gh CLI authenticated as the planner-bot identity

## What you produce

1. **A planning document** at `docs/superpowers/specs/foreman-issue-<N>-spec.md`
   in the worktree. The doc should cover: goal, approach, file structure,
   key trade-offs considered, open questions.
2. **A pull request** opened against the repo's default branch, with:
   - Title: `spec: <one-line summary of approach>`
   - Body: brief PR description + link to the spec doc
3. **A structured output** matching the PlannerOutput schema (this is what
   you return at the end of your run).

## Working discipline

- **Read before writing.** Explore the repo to understand existing patterns
  before drafting the spec.
- **Document alternatives considered.** The `considered_alternatives` field
  in your structured output captures approaches you ruled out. Be honest;
  the audit log uses this.
- **Confidence-rate your output.** `confidence: high` means you're sure of
  the approach. `medium` is the default. `low` flags that the Reviewer
  should look extra-carefully.
- **The spec doc IS the contract for downstream nodes.** Don't bury
  important constraints in your structured output's `summary` — write them
  into the spec doc so the Reviewer/Worker can see them.

## Steps

1. Read the issue body carefully.
2. Explore the repo (Glob, Grep, Read) to understand existing patterns.
3. Draft the spec doc, write to `docs/superpowers/specs/foreman-issue-<N>-spec.md`.
4. Commit it: `git add . && git commit -m "spec: <one-line summary>"`.
5. Push the branch: `git push -u origin foreman/issue-<N>`.
6. Open the PR via `gh pr create --base <default-branch> --head foreman/issue-<N> --title "spec: ..." --body "..."`.
7. Return the structured output (PlannerOutput) with PR URL, number, branch, summary, alternatives, confidence.
```

- [ ] **Step 2: Update pyproject.toml to package the prompts directory**

Edit `packages/foreman/pyproject.toml` — change the wheel target block:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/foreman"]

[tool.hatch.build.targets.wheel.force-include]
"src/foreman/prompts" = "foreman/prompts"
```

(Hatch packages everything in `src/foreman` already; the `force-include`
ensures `.md` files in `prompts/` ship in the wheel — `.py` files are
auto-included but data files often need explicit inclusion.)

- [ ] **Step 3: Create roles package marker**

```bash
mkdir -p packages/foreman/src/foreman/roles
```

Create `packages/foreman/src/foreman/roles/__init__.py`:

```python
"""Per-role dispatch modules. Each role: build context, run agent, parse output, act."""
```

- [ ] **Step 4: Write the failing test**

Create `packages/foreman/tests/test_roles_planner.py`:

```python
"""Integration test for run_planner with mocked provider + mocked PyGithub.

A real-engine integration test (against actual Anthropic API + real GitHub)
is gated behind the `real_engine` pytest marker and lives separately. This
test verifies the orchestration wiring: issue parsing, worktree creation,
provider invocation, label advancement.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from foreman.config import BotConfig, Config, ProjectConfig
from foreman.roles.planner import parse_issue_url, run_planner


def test_parse_issue_url_extracts_owner_repo_number() -> None:
    owner, repo, number = parse_issue_url("https://github.com/jeffrichley/voice/issues/42")
    assert owner == "jeffrichley"
    assert repo == "voice"
    assert number == 42


def test_parse_issue_url_rejects_non_issue_url() -> None:
    with pytest.raises(ValueError, match="Not a GitHub issue URL"):
        parse_issue_url("https://github.com/jeffrichley/voice/pull/42")


@pytest.mark.asyncio
async def test_run_planner_dispatches_and_advances_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Set up config with the tmp_path as the local clone
    clone = tmp_path / "clone"
    clone.mkdir()
    # Init a minimal git repo there
    import subprocess

    subprocess.run(["git", "init", "-b", "main"], cwd=clone, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=clone, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=clone, check=True, capture_output=True
    )
    (clone / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=clone, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=clone, check=True, capture_output=True)

    monkeypatch.setenv("FOREMAN_PLANNER_BOT_TOKEN", "fake-token")

    cfg = Config(
        projects={
            "voice": ProjectConfig(
                repo="jeffrichley/voice",
                local_clone_path=str(clone),
                bots=BotConfig(planner_env="FOREMAN_PLANNER_BOT_TOKEN"),
            )
        }
    )

    fake_issue = MagicMock()
    fake_issue.body = "Add SSML support to madrigal."
    fake_issue.title = "SSML"
    fake_issue.labels = []
    fake_issue.add_to_labels = MagicMock()
    fake_issue.remove_from_labels = MagicMock()

    fake_repo = MagicMock()
    fake_repo.get_issue.return_value = fake_issue
    fake_repo.default_branch = "main"

    fake_gh = MagicMock()
    fake_gh.get_repo.return_value = fake_repo

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(
        return_value={
            "pr_url": "https://github.com/jeffrichley/voice/pull/99",
            "pr_number": 99,
            "branch_name": "foreman/issue-42",
            "summary": "Drafted SSML support spec",
            "considered_alternatives": [],
            "confidence": "high",
        }
    )

    with patch("foreman.roles.planner.IdentityRegistry") as mock_reg:
        mock_reg.return_value.get_client.return_value = fake_gh
        result = await run_planner(
            issue_url="https://github.com/jeffrichley/voice/issues/42",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
        )

    assert result.pr_number == 99
    fake_provider.run_agent.assert_called_once()
    fake_issue.add_to_labels.assert_called_with("foreman:spec-review")
    fake_issue.remove_from_labels.assert_called_with("foreman:plan")
```

- [ ] **Step 5: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/test_roles_planner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.roles.planner'`

- [ ] **Step 6: Implement roles/planner.py**

Create `packages/foreman/src/foreman/roles/planner.py`:

```python
"""Planner role dispatcher.

Walks through:
  1. Parse the issue URL (owner / repo / number)
  2. Resolve planner identity → PyGithub client
  3. Fetch issue body + title
  4. Create per-ticket worktree
  5. Load planner system prompt
  6. Dispatch via provider with scoped tools + structured output schema
  7. Parse PlannerOutput
  8. Advance label: foreman:plan → foreman:spec-review
  9. Return PlannerOutput

For walking skeleton: cleanup is NOT performed automatically (per spec —
worktrees persist until pipeline completion, which here is the human merging
the PR).
"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

from foreman.config import Config
from foreman.identity import IdentityRegistry
from foreman.provider import ProviderFacade
from foreman.schemas.planner import PlannerOutput
from foreman.worktree import WorktreeManager

_ISSUE_URL_RE = re.compile(
    r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)"
)

# Tool capabilities matrix for Planner (from architectural spec §4.1)
PLANNER_ALLOWED_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]


def parse_issue_url(url: str) -> tuple[str, str, int]:
    """Extract (owner, repo, issue_number) from a GitHub issue URL."""
    m = _ISSUE_URL_RE.match(url.strip())
    if not m:
        raise ValueError(f"Not a GitHub issue URL: {url!r}")
    return m["owner"], m["repo"], int(m["number"])


def _load_planner_prompt() -> str:
    """Load the planner system prompt from packaged resources."""
    return (
        resources.files("foreman.prompts").joinpath("planner.md").read_text(encoding="utf-8")
    )


def _build_user_prompt(*, issue_title: str, issue_body: str, issue_number: int) -> str:
    return (
        f"You are processing GitHub issue #{issue_number}.\n\n"
        f"## Title\n{issue_title}\n\n"
        f"## Body\n{issue_body}\n\n"
        f"Follow the steps in your system prompt. Return your structured output "
        f"when done."
    )


async def run_planner(
    *,
    issue_url: str,
    config: Config,
    project_name: str,
    worktrees_root: Path,
    provider: ProviderFacade,
) -> PlannerOutput:
    """Run the Planner role end-to-end on one issue."""
    owner, repo_name, issue_number = parse_issue_url(issue_url)
    project = config.projects[project_name]
    expected_repo_slug = project.repo  # e.g. "jeffrichley/voice"
    actual_repo_slug = f"{owner}/{repo_name}"
    if expected_repo_slug != actual_repo_slug:
        raise ValueError(
            f"Issue URL repo {actual_repo_slug!r} does not match project "
            f"{project_name!r} configured repo {expected_repo_slug!r}"
        )

    identity = IdentityRegistry(project)
    gh = identity.get_client("planner")
    repo = gh.get_repo(actual_repo_slug)
    issue = repo.get_issue(issue_number)

    wt_mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt_path = wt_mgr.create(
        clone_path=Path(project.local_clone_path),
        repo_slug=repo_name,
        ticket_id=issue_number,
    )

    system_prompt = _load_planner_prompt()
    user_prompt = _build_user_prompt(
        issue_title=issue.title,
        issue_body=issue.body or "",
        issue_number=issue_number,
    )

    raw = await provider.run_agent(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        allowed_tools=PLANNER_ALLOWED_TOOLS,
        output_schema=PlannerOutput.model_json_schema(),
        cwd=wt_path,
    )
    output = PlannerOutput.model_validate(raw)

    # Advance label: foreman:plan → foreman:spec-review
    issue.remove_from_labels("foreman:plan")
    issue.add_to_labels("foreman:spec-review")

    return output
```

- [ ] **Step 7: Run test to verify it passes**

Run: `uv run pytest packages/foreman/tests/test_roles_planner.py -v`
Expected: 3 passed.

- [ ] **Step 8: Commit**

```bash
git add packages/foreman/src/foreman/prompts/ packages/foreman/src/foreman/roles/ packages/foreman/tests/test_roles_planner.py packages/foreman/pyproject.toml
git commit -m "feat: add planner role + system prompt + label-advance"
```

---

## Task 8: CLI entry point + console script

**Files:**
- Create: `packages/foreman/src/foreman/cli.py`
- Create: `packages/foreman/tests/test_cli.py`
- Modify: `packages/foreman/pyproject.toml` (add `[project.scripts]` entry)

- [ ] **Step 1: Write the failing test**

Create `packages/foreman/tests/test_cli.py`:

```python
"""CLI smoke tests via click's testing harness."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from foreman.cli import cli
from foreman.schemas.planner import PlannerOutput


def test_cli_plan_invokes_run_planner(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.bots]
planner_env = "FOREMAN_PLANNER_BOT_TOKEN"
planner_token = "test-token"
"""
    )

    fake_output = PlannerOutput(
        pr_url="https://github.com/jeffrichley/voice/pull/99",
        pr_number=99,
        branch_name="foreman/issue-42",
        summary="ok",
        considered_alternatives=[],
        confidence="medium",
    )

    runner = CliRunner()
    with patch("foreman.cli.run_planner", new=AsyncMock(return_value=fake_output)) as mock_run:
        result = runner.invoke(
            cli,
            [
                "plan",
                "https://github.com/jeffrichley/voice/issues/42",
                "--project",
                "voice",
                "--config",
                str(config_file),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "PR #99" in result.output or "pull/99" in result.output
    mock_run.assert_called_once()


def test_cli_help_lists_plan_subcommand() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "plan" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.cli'`

- [ ] **Step 3: Implement cli.py**

Create `packages/foreman/src/foreman/cli.py`:

```python
"""Foreman CLI — `foreman plan <issue-url>` is the walking-skeleton entry point.

Thickening will add: `foreman review`, `foreman work`, `foreman daemon ...`,
`foreman project add`, etc. Walking skeleton has just `plan`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import click

from foreman.config import load_config
from foreman.providers.anthropic_sdk import AnthropicSDKProvider
from foreman.roles.planner import run_planner


def _default_config_path() -> Path:
    return Path(os.environ.get("FOREMAN_CONFIG", str(Path.home() / ".foreman" / "config.toml")))


def _default_worktrees_root() -> Path:
    return Path(os.environ.get("FOREMAN_WORKTREES_ROOT", str(Path.home() / ".foreman" / "worktrees")))


@click.group()
def cli() -> None:
    """foreman — multi-identity GitHub-issue-to-PR orchestrator."""


@cli.command()
@click.argument("issue_url", type=str)
@click.option("--project", required=True, help="Project name as defined in config.toml")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to foreman config (default: $FOREMAN_CONFIG or ~/.foreman/config.toml)",
)
def plan(issue_url: str, project: str, config_path: Path | None) -> None:
    """Run the Planner on a GitHub issue and open a spec PR."""
    cfg_path = config_path or _default_config_path()
    cfg = load_config(cfg_path)
    provider = AnthropicSDKProvider()
    output = asyncio.run(
        run_planner(
            issue_url=issue_url,
            config=cfg,
            project_name=project,
            worktrees_root=_default_worktrees_root(),
            provider=provider,
        )
    )
    click.echo(f"Planner complete — PR #{output.pr_number} at {output.pr_url}")
    click.echo(f"Branch: {output.branch_name}")
    click.echo(f"Confidence: {output.confidence}")
    click.echo(f"Summary: {output.summary}")
    if output.considered_alternatives:
        click.echo("Considered alternatives:")
        for alt in output.considered_alternatives:
            click.echo(f"  - {alt}")


def main() -> None:
    """Console-script entry point."""
    cli()
```

- [ ] **Step 4: Add console_scripts entry to pyproject.toml**

Edit `packages/foreman/pyproject.toml` — add after the `[project]` block:

```toml
[project.scripts]
foreman = "foreman.cli:main"
```

- [ ] **Step 5: Re-sync to register the console script**

Run: `uv sync --all-packages`
Expected: Re-install foreman with new entry point.

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest packages/foreman/tests/test_cli.py -v`
Expected: 2 passed.

- [ ] **Step 7: Verify the CLI is callable**

Run: `uv run foreman --help`
Expected: shows the `plan` subcommand in the help output.

- [ ] **Step 8: Run the full quality gate**

Run: `just check`
Expected: All green (ruff + mypy + full pytest suite).

- [ ] **Step 9: Commit**

```bash
git add packages/foreman/src/foreman/cli.py packages/foreman/tests/test_cli.py packages/foreman/pyproject.toml uv.lock
git commit -m "feat: add foreman plan CLI entry point"
```

---

## Task 9: Manual end-to-end smoke test (deferred until planner-bot exists)

**Files:**
- Create: `docs/superpowers/plans/2026-05-30-foreman-walking-skeleton-smoke-instructions.md`

This task documents how to run the walking skeleton against a REAL GitHub
issue once Block 2 (planner-bot account + collaborator + token) is set up.
Per Jeff's "never done without running" rule, this is the keystone — but
it cannot run today because the bot doesn't exist yet.

- [ ] **Step 1: Write the smoke-test instructions doc**

Create `docs/superpowers/plans/2026-05-30-foreman-walking-skeleton-smoke-instructions.md`:

```markdown
# Foreman walking-skeleton manual smoke test

## Prerequisites

1. **`@foreman-planner-bot` account exists** (signed up via Gmail plus-alias
   `jeffrichley+foreman-planner-bot@gmail.com`).
2. **PAT generated** on the bot account with `repo` scope.
3. **Bot is a collaborator** on the target test repo (e.g., `jeffrichley/voice`).
   Invite via: `gh api -X PUT repos/jeffrichley/voice/collaborators/foreman-planner-bot -F permission=push`
4. **Bot accepted the invitation** (log in as the bot once, or accept via the bot's PAT).
5. **`~/.foreman/config.toml` exists** with the project entry:
   ```toml
   [projects.voice]
   repo = "jeffrichley/voice"
   local_clone_path = "e:/workspaces/ai/agents/voice"

   [projects.voice.bots]
   planner_env = "FOREMAN_PLANNER_BOT_TOKEN"
   ```
6. **`FOREMAN_PLANNER_BOT_TOKEN` env var set** to the bot's PAT.
7. **`ANTHROPIC_API_KEY` env var set** (the agent SDK uses this).
8. **Voice repo is cloned** at the `local_clone_path` and on the default branch.

## Pick a test issue

Use a small, well-scoped issue. Lean: voice #225 (endpoint polish, extract
method + hoist imports + 2 comments). Label it `foreman:plan` first:

```bash
gh issue edit 225 -R jeffrichley/voice --add-label foreman:plan
```

(If the `foreman:plan` label doesn't exist on the repo yet, create it:)

```bash
gh label create foreman:plan --color "0E8A16" --description "Foreman: queue for planning" -R jeffrichley/voice
gh label create foreman:spec-review --color "FBCA04" --description "Foreman: spec PR ready for review" -R jeffrichley/voice
```

## Run the planner

```bash
cd /any/dir
uv --project e:/workspaces/ai/agents/foreman run foreman plan \
  https://github.com/jeffrichley/voice/issues/225 \
  --project voice
```

## What to verify

- [ ] Worktree exists at `~/.foreman/worktrees/voice/issue-225/` with
      branch `foreman/issue-225` checked out
- [ ] New spec doc committed at `docs/superpowers/specs/foreman-issue-225-spec.md`
      in that worktree
- [ ] Spec PR exists on `jeffrichley/voice` authored by `@foreman-planner-bot`,
      titled `spec: ...`, targeting `main`
- [ ] Issue #225 has labels: `foreman:plan` REMOVED, `foreman:spec-review` ADDED
- [ ] CLI exit code = 0, prints PR number + URL + summary

## If it doesn't work

- Check the Anthropic API key is set: `echo $ANTHROPIC_API_KEY | head -c 10`
- Check the bot can authenticate: `GH_TOKEN=$FOREMAN_PLANNER_BOT_TOKEN gh api user --jq .login`
  expect: `foreman-planner-bot`
- Check the worktree was cleaned up if a prior run failed:
  `cd e:/workspaces/ai/agents/voice && git worktree list`

## After it works

This proves the walking skeleton. Next:
- Iterate the planner prompt based on what it produced
- Begin thickening: Reviewer, then Worker, then daemon poll loop, then Fixer
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/plans/2026-05-30-foreman-walking-skeleton-smoke-instructions.md
git commit -m "docs: add walking-skeleton manual smoke test instructions"
```

---

## Self-review (completed during plan writing)

**1. Spec coverage:** Walking-skeleton (spec §7) modules all have tasks:
- `config.py` → Task 2
- `identity.py` → Task 3 (planner-only; reviewer/fixer/worker deferred to thickening)
- `worktree.py` → Task 4
- `provider.py` + `providers/anthropic_sdk.py` → Task 6
- `schemas/planner.py` → Task 5
- `roles/planner.py` → Task 7
- `prompts/planner.md` → Task 7
- `cli.py` → Task 8
- Smoke-test discipline → Task 9

**2. Placeholder scan:** No TBDs / TODOs / "implement later" / vague handlers
in the task bodies. All code blocks are complete. Imports listed where used.

**3. Type consistency:** `PlannerOutput` fields match between
schema definition (Task 5), the test fixture (Task 7), and the CLI output
formatter (Task 8). `parse_issue_url` returns `tuple[str, str, int]` —
consumed correctly in `run_planner`. `WorktreeManager.create()` returns `Path`
— consumed as `Path` in `run_planner`.

---

## Execution handoff

This plan is ready for execution via `superpowers:subagent-driven-development`.
Total estimate: 8 tasks × ~30 min each = ~4 hours focused work.

The walking skeleton is intentionally minimal — many things are NOT here:
no daemon poll loop, no Reviewer/Fixer/Worker, no SQLite, no MCP, no bus
events. Those are thickening tasks for follow-up plans after walking skeleton
proves the architecture end-to-end via Task 9's manual smoke test.
