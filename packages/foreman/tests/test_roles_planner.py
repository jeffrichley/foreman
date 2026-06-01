"""Integration test for ``run_planner`` with a fake GitHostProvider + fake
ProviderFacade.

A real-engine integration test (against actual Anthropic API + real GitHub)
is gated behind the ``real_engine`` pytest marker and lives separately. This
test verifies the orchestration wiring: issue parsing, worktree creation,
identity configuration, LLM dispatch, commit/push/PR, label advancement.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from foreman.config import AppsConfig, Config, ProjectConfig
from foreman.git_host import GitHostProvider, IssueRef, PRRef
from foreman.roles.planner import (
    PLANNER_ALLOWED_TOOLS,
    parse_issue_url,
    run_planner,
)
from foreman.schemas.planner import PlannerOutput

# ----------------------------------------------------------------------
# parse_issue_url
# ----------------------------------------------------------------------


def test_parse_issue_url_extracts_owner_repo_number() -> None:
    owner, repo, number = parse_issue_url("https://github.com/jeffrichley/voice/issues/42")
    assert owner == "jeffrichley"
    assert repo == "voice"
    assert number == 42


def test_parse_issue_url_rejects_non_issue_url() -> None:
    with pytest.raises(ValueError, match="Not a GitHub issue URL"):
        parse_issue_url("https://github.com/jeffrichley/voice/pull/42")


# ----------------------------------------------------------------------
# Planner LLM tool surface
# ----------------------------------------------------------------------


def test_planner_allowed_tools_is_read_only() -> None:
    """Post-refactor: the Planner LLM never writes files or shells out.

    Foreman core does the writes via :class:`~foreman.git_host.GitHostProvider`.
    Pinning this list prevents accidental Bash/Write reintroduction.
    """
    assert set(PLANNER_ALLOWED_TOOLS) == {"Read", "Glob", "Grep"}


# ----------------------------------------------------------------------
# Test scaffolding
# ----------------------------------------------------------------------


class _FakeHostProvider(GitHostProvider):
    """In-memory host provider that records every call for assertions."""

    def __init__(self) -> None:
        self.issue_to_return = IssueRef(
            number=42,
            title="SSML",
            body="Add SSML support to madrigal.",
            labels=["foreman:plan"],
            repo_slug="jeffrichley/voice",
        )
        self.default_branch = "main"
        self.committed_files: dict[str, str] | None = None
        self.commit_message: str | None = None
        self.pushed_branch: str | None = None
        self.configure_calls: list[Path] = []
        self.label_calls: list[tuple[str, int, list[str], list[str]]] = []
        self.pr_to_return = PRRef(
            number=99,
            url="https://github.com/jeffrichley/voice/pull/99",
            title="spec: SSML",
            body="body",
            branch="foreman/issue-42",
            base_branch="main",
            repo_slug="jeffrichley/voice",
        )

    def get_issue(self, repo_slug: str, issue_number: int) -> IssueRef:
        return self.issue_to_return

    def get_default_branch(self, repo_slug: str) -> str:
        return self.default_branch

    def configure_worktree_identity(self, worktree_path: Path) -> None:
        self.configure_calls.append(worktree_path)

    def commit_files_to_worktree(
        self, worktree_path: Path, files: dict[str, str], message: str
    ) -> str:
        self.committed_files = dict(files)
        self.commit_message = message
        return "deadbeef" * 5

    def push_branch(self, worktree_path: Path, branch: str) -> None:
        self.pushed_branch = branch

    def open_pull_request(
        self, repo_slug: str, title: str, body: str, base: str, head: str
    ) -> PRRef:
        return PRRef(
            number=self.pr_to_return.number,
            url=self.pr_to_return.url,
            title=title,
            body=body,
            branch=head,
            base_branch=base,
            repo_slug=repo_slug,
        )

    def update_issue_labels(
        self, repo_slug: str, issue_number: int, add: list[str], remove: list[str]
    ) -> None:
        self.label_calls.append((repo_slug, issue_number, list(add), list(remove)))


def _seed_clone(clone: Path, *, origin_path: Path | None = None) -> None:
    """Init a minimal git repo at ``clone``.

    If ``origin_path`` is provided, also wire a bare upstream at that path
    as ``origin`` and push ``main`` so ``origin/main`` and
    ``refs/remotes/origin/HEAD`` are resolvable. ``WorktreeManager.create``
    bases new branches on ``origin/<default-branch>`` rather than local
    HEAD, so any test that calls ``create`` (via ``run_planner``) needs
    an origin set up.
    """
    clone.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=clone, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=clone, check=True, capture_output=True
    )
    (clone / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=clone, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=clone, check=True, capture_output=True)
    if origin_path is not None:
        origin_path.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--bare", "-b", "main"],
            cwd=origin_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(origin_path)],
            cwd=clone,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=clone,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "remote", "set-head", "origin", "main"],
            cwd=clone,
            check=True,
            capture_output=True,
        )


def _make_config(clone: Path) -> Config:
    return Config(
        projects={
            "voice": ProjectConfig(
                repo="jeffrichley/voice",
                local_clone_path=str(clone),
                apps=AppsConfig(
                    planner_app_id_env="FOREMAN_PLANNER_APP_ID",
                    planner_private_key_path="/tmp/planner.pem",
                ),
            )
        }
    )


def _make_llm_output(**overrides: Any) -> PlannerOutput:
    """Build a ``PlannerOutput`` instance for use as the fake provider's return.

    Post-refactor the provider returns Pydantic instances (not dicts), so
    test doubles must mirror that contract.
    """
    base: dict[str, Any] = {
        "spec_doc_content": "# Spec: SSML support (issue #42)\n\n## Goal\nx\n",
        "pr_title": "spec: add SSML support",
        "pr_body": "Adds the spec for SSML support. See spec doc.",
        "summary": "Drafted SSML support spec",
        "considered_alternatives": ["raw-string approach", "external library"],
        "confidence": "high",
    }
    base.update(overrides)
    return PlannerOutput.model_validate(base)


# ----------------------------------------------------------------------
# run_planner end-to-end
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_planner_dispatches_and_advances_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    _seed_clone(clone, origin_path=tmp_path / "origin.git")
    monkeypatch.setenv("FOREMAN_PLANNER_APP_ID", "123456")

    cfg = _make_config(clone)

    fake_host = _FakeHostProvider()
    fake_registry = MagicMock()
    fake_registry.get_host_provider.return_value = fake_host

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_make_llm_output())

    result = await run_planner(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=fake_registry,
    )

    # LLM was dispatched once with read-only tools
    fake_provider.run_agent.assert_called_once()
    call_kwargs = fake_provider.run_agent.call_args.kwargs
    assert call_kwargs["allowed_tools"] == ["Read", "Glob", "Grep"]
    # Pydantic-first contract: dispatcher passes the model class, not a schema dict
    assert call_kwargs["output_model"] is PlannerOutput
    assert "output_schema" not in call_kwargs
    # No env injection (decoupled from gh CLI)
    assert "env" not in call_kwargs or call_kwargs["env"] is None

    # Host operations all happened, in order
    assert fake_host.configure_calls, "configure_worktree_identity must be called"
    assert fake_host.committed_files == {
        "docs/superpowers/specs/foreman-issue-42-spec.md": (
            "# Spec: SSML support (issue #42)\n\n## Goal\nx\n"
        )
    }
    assert fake_host.commit_message == "spec: add SSML support"
    assert fake_host.pushed_branch == "foreman/issue-42"

    # Label advanced
    assert fake_host.label_calls == [
        ("jeffrichley/voice", 42, ["foreman:spec-review"], ["foreman:plan"])
    ]

    # PlannerRunResult populated end-to-end
    assert result.pr.number == 99
    assert result.pr.url == "https://github.com/jeffrichley/voice/pull/99"
    assert result.pr.branch == "foreman/issue-42"
    assert result.llm_output.confidence == "high"
    assert result.llm_output.summary == "Drafted SSML support spec"


@pytest.mark.asyncio
async def test_run_planner_does_not_inject_env_into_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-refactor: the planner-bot token does NOT flow into the agent
    subprocess. The LLM never calls ``gh``; commits and pushes happen in
    Foreman core via :class:`~foreman.git_host.GitHostProvider`.

    This test pins the *removal* of the env-injection hack so we don't
    accidentally reintroduce it.
    """
    clone = tmp_path / "clone"
    _seed_clone(clone, origin_path=tmp_path / "origin.git")
    monkeypatch.setenv("GH_TOKEN", "parent-pat-do-not-use")
    monkeypatch.setenv("FOREMAN_PLANNER_APP_ID", "123456")

    cfg = _make_config(clone)
    fake_host = _FakeHostProvider()
    fake_registry = MagicMock()
    fake_registry.get_host_provider.return_value = fake_host

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_make_llm_output())

    await run_planner(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=fake_registry,
    )

    call_kwargs = fake_provider.run_agent.call_args.kwargs
    # The env kwarg is no longer supplied by run_planner. The provider's
    # `env` parameter remains supported for future use, but we must not be
    # passing it here.
    assert call_kwargs.get("env") is None
    # And the registry must NOT have been asked for the raw token —
    # that path only existed for the GH_TOKEN injection.
    fake_registry.get_token.assert_not_called()


@pytest.mark.asyncio
async def test_run_planner_embeds_project_instructions_in_user_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``.foreman/INSTRUCTIONS.md`` exists in the clone, its content
    is embedded verbatim in the LLM's user_prompt under a project-specific
    instructions section header. This is how project conventions (PR
    title rules, branch conventions) reach the Planner."""
    clone = tmp_path / "clone"
    _seed_clone(clone, origin_path=tmp_path / "origin.git")
    monkeypatch.setenv("FOREMAN_PLANNER_APP_ID", "123456")

    # Drop the instructions file into the clone — same path the
    # ``foreman init`` command would write it.
    foreman_dir = clone / ".foreman"
    foreman_dir.mkdir()
    instructions_text = (
        "# Foreman instructions for voice\n\n"
        "## PR title rules\nUse `feat(scope): ...` only.\n"
    )
    (foreman_dir / "INSTRUCTIONS.md").write_text(instructions_text, encoding="utf-8")

    cfg = _make_config(clone)
    fake_host = _FakeHostProvider()
    fake_registry = MagicMock()
    fake_registry.get_host_provider.return_value = fake_host
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_make_llm_output())

    await run_planner(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=fake_registry,
    )

    user_prompt = fake_provider.run_agent.call_args.kwargs["user_prompt"]
    # Section header + content both present.
    assert "## Project-specific instructions" in user_prompt
    assert "Use `feat(scope): ...` only." in user_prompt
    # And the distinctive instructions title survived verbatim.
    assert "# Foreman instructions for voice" in user_prompt


@pytest.mark.asyncio
async def test_run_planner_omits_instructions_section_when_file_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``.foreman/INSTRUCTIONS.md`` is absent, the project-specific
    instructions section header MUST NOT appear in the user_prompt —
    empty headers would be a distracting no-op the LLM would have to
    mentally skip."""
    clone = tmp_path / "clone"
    _seed_clone(clone, origin_path=tmp_path / "origin.git")
    monkeypatch.setenv("FOREMAN_PLANNER_APP_ID", "123456")

    # Note: no `.foreman/INSTRUCTIONS.md` written.

    cfg = _make_config(clone)
    fake_host = _FakeHostProvider()
    fake_registry = MagicMock()
    fake_registry.get_host_provider.return_value = fake_host
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_make_llm_output())

    await run_planner(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=fake_registry,
    )

    user_prompt = fake_provider.run_agent.call_args.kwargs["user_prompt"]
    assert "## Project-specific instructions" not in user_prompt


@pytest.mark.asyncio
async def test_run_planner_rejects_url_pointing_at_wrong_project(
    tmp_path: Path,
) -> None:
    clone = tmp_path / "clone"
    _seed_clone(clone)
    cfg = _make_config(clone)
    fake_host = _FakeHostProvider()
    fake_registry = MagicMock()
    fake_registry.get_host_provider.return_value = fake_host
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_make_llm_output())

    with pytest.raises(ValueError, match="does not match project"):
        await run_planner(
            issue_url="https://github.com/someone-else/other-repo/issues/1",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=fake_registry,
        )
