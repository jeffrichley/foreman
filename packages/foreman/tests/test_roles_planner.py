"""Integration test for ``run_planner`` with a fake GitHostProvider + fake
ProviderFacade.

A real-engine integration test (against actual Anthropic API + real GitHub)
is gated behind the ``real_engine`` pytest marker and lives separately. This
test verifies the orchestration wiring: issue parsing, worktree creation,
identity configuration, LLM dispatch, commit/push/PR, label advancement.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from foreman.config import AppsConfig, Config, ProjectConfig
from foreman.git_host import GitHostProvider, IssueRef, PRRef
from foreman.prompts import load_role_prompt
from foreman.provider import UsageInfo
from foreman.roles.planner import (
    PLANNER_ALLOWED_TOOLS,
    _strip_auto_close_keywords,
    parse_issue_url,
    run_planner,
)
from foreman.schemas.planner import PlannerOutput


def _with_usage(output: Any) -> tuple[Any, UsageInfo]:
    """Wrap a role-output with a default zeroed UsageInfo.

    foreman#227: ``ProviderFacade.run_agent`` now returns
    ``tuple[T, UsageInfo]`` so role runners can persist per-call token
    usage. Test mocks return the tuple shape via this helper.
    """
    return output, UsageInfo()

# ----------------------------------------------------------------------
# _strip_auto_close_keywords
#
# foreman#63: the Planner must not produce spec PR bodies that auto-close
# the originating issue at spec-merge time. This helper is the runtime
# defense-in-depth (the prompt teaches the LLM; this strips noncompliant
# output before it reaches GitHub). The table below pins all nine
# GitHub closing-keyword forms, their casing variants, the colon
# separator, the ``owner/repo#N`` cross-repo form, multi-issue lines,
# and negative cases (bare ``#N`` references, substring-of-other-word).
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # All nine keyword forms with default casing
        ("Closes #42", "#42"),
        ("closes #42", "#42"),
        ("CLOSED #42", "#42"),
        ("Fixes #42", "#42"),
        ("fix #42", "#42"),
        ("FIXED #42", "#42"),
        ("Resolves #42", "#42"),
        ("resolve #42", "#42"),
        ("RESOLVED #42", "#42"),
        # Colon separator
        ("Closes: #42", "#42"),
        # Cross-repo owner/repo#N form
        ("Closes jeffrichley/foreman#42", "jeffrichley/foreman#42"),
        # Multi-issue line — both keywords stripped, both refs preserved
        ("closes #42, fixes #43", "#42, #43"),
        # Negative: bare reference survives untouched
        ("See #42 for context.", "See #42 for context."),
        # Negative: substring-of-other-word survives (word boundary)
        ("foreclosed by #42", "foreclosed by #42"),
        # Negative: empty body is a no-op
        ("", ""),
    ],
)
def test_strip_auto_close_keywords_table(body: str, expected: str) -> None:
    assert _strip_auto_close_keywords(body) == expected


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


def test_planner_prompt_forbids_auto_close_keywords() -> None:
    """The Planner system prompt MUST explicitly forbid GitHub
    auto-close keywords in pr_body. Without this guardrail the LLM
    writes ``Closes #N``, which auto-closes the originating issue at
    spec-merge time — short-circuiting the loop's close-out gate
    that lives in ``daemon_runners.merge_impl_pr`` (foreman#63).
    """
    prompt = load_role_prompt("planner")
    assert "pr_body_guardrails" in prompt
    # All three keyword families named, so the LLM can generalize:
    assert "Closes" in prompt
    assert "Fixes" in prompt
    assert "Resolves" in prompt
    # Rationale cites the daemon's authoritative close path:
    assert "merge_impl_pr" in prompt


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
        self.label_calls: list[tuple[str, int, list[str], list[str]]] = []
        # foreman#63: capture the body kwarg that arrives at
        # ``open_pull_request`` so the auto-close-strip integration test
        # can assert what GitHub would have seen.
        self.last_open_pr_body: str | None = None
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
        self.last_open_pr_body = body
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
    # Planner role-token (Stage 3e follow-up): WorktreeManager threads
    # this into git subprocess GH_TOKEN. MagicMock default returns a
    # Mock object that subprocess rejects with "environment can only
    # contain strings", so pin it to a string.
    fake_registry.get_planner_token.return_value = "fake-planner-token"

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_make_llm_output()))

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

    # Host operations all happened, in order. Note: foreman#53 removed
    # the configure_worktree_identity call from Planner — bot identity is
    # now injected per-commit via env vars inside commit_files_to_worktree.
    assert fake_host.committed_files == {
        "docs/superpowers/specs/foreman-issue-42-spec.md": (
            "# Spec: SSML support (issue #42)\n\n## Goal\nx\n"
        )
    }
    assert fake_host.commit_message == "spec: add SSML support"
    assert fake_host.pushed_branch == "foreman/issue-42"

    # v3 label vocabulary: Planner makes ZERO label mutations. The issue
    # stays at ``foreman:planning`` so v3's reconciler can fire
    # ``dispatch_reviewer_spec`` on the open spec PR. The legacy
    # ``foreman:plan`` cleanup that used to live here was removed
    # because PyGithub 404s when remove_from_labels is called on a
    # label that isn't present — and fresh v3 tickets never carry
    # ``foreman:plan``.
    assert fake_host.label_calls == []

    # PlannerRunResult populated end-to-end
    assert result.pr.number == 99
    assert result.pr.url == "https://github.com/jeffrichley/voice/pull/99"
    assert result.pr.branch == "foreman/issue-42"
    assert result.llm_output.confidence == "high"
    assert result.llm_output.summary == "Drafted SSML support spec"


@pytest.mark.asyncio
async def test_run_planner_does_not_attempt_legacy_plan_label_removal_on_fresh_v3_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: v3 tickets carry only ``foreman:planning`` at hatch
    time. A prior revision attempted a best-effort ``remove=["foreman:plan"]``
    cleanup after opening the spec PR; PyGithub's
    ``issue.remove_from_labels`` raises ``GithubException(404)`` when the
    label isn't on the issue, so every fresh v3 ticket crashed the
    Planner subprocess immediately after the PR opened.

    The fix removes the cleanup entirely. This test pins the contract:
    on a fresh v3 ticket, the Planner must NOT attempt to remove
    ``foreman:plan`` (or any label) via ``host.update_issue_labels``.
    """
    clone = tmp_path / "clone"
    _seed_clone(clone, origin_path=tmp_path / "origin.git")
    monkeypatch.setenv("FOREMAN_PLANNER_APP_ID", "123456")

    cfg = _make_config(clone)
    fake_host = _FakeHostProvider()
    # Fresh v3 ticket: only the planning entry label, no legacy
    # ``foreman:plan``. This is the exact shape every ``foreman init``
    # ticket has after PR #111.
    fake_host.issue_to_return = IssueRef(
        number=42,
        title="SSML",
        body="Add SSML support.",
        labels=["foreman:planning"],
        repo_slug="jeffrichley/voice",
    )
    fake_registry = MagicMock()
    fake_registry.get_host_provider.return_value = fake_host
    # Planner role-token (Stage 3e follow-up): WorktreeManager threads
    # this into git subprocess GH_TOKEN. MagicMock default returns a
    # Mock object that subprocess rejects with "environment can only
    # contain strings", so pin it to a string.
    fake_registry.get_planner_token.return_value = "fake-planner-token"

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_make_llm_output()))

    result = await run_planner(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=fake_registry,
    )

    # The Planner must not have touched labels at all on a fresh
    # v3 ticket. If a future refactor reintroduces label mutation
    # here, this assertion + the per-call ``"foreman:plan" not in remove``
    # check below must both hold so the 404-crash regression cannot
    # silently come back.
    assert fake_host.label_calls == []
    for _repo, _num, _add, remove in fake_host.label_calls:
        assert "foreman:plan" not in remove

    # The issue stays at ``foreman:planning`` so the v3 reconciler can
    # fire ``dispatch_reviewer_spec`` on the open spec PR next tick.
    assert result.final_labels == ["foreman:planning"]


@pytest.mark.asyncio
async def test_run_planner_strips_auto_close_keywords_from_pr_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """foreman#63: ``run_planner`` must strip GitHub auto-close keywords
    (``Closes`` / ``Fixes`` / ``Resolves`` + ``#N``) from the LLM's
    ``pr_body`` before passing it to ``host.open_pull_request``.

    Issue closure routes exclusively through
    ``daemon_runners.merge_impl_pr`` → ``close_issue``; a spec PR body
    containing ``Closes #N`` would let GitHub close the originating issue
    at spec-merge time, short-circuiting the loop's close-out gate.

    Audit-log preservation: ``PlannerRunResult.llm_output.pr_body`` must
    still carry the LLM's original text — the scrub is per-call, not a
    mutation on the model.
    """
    clone = tmp_path / "clone"
    _seed_clone(clone, origin_path=tmp_path / "origin.git")
    monkeypatch.setenv("FOREMAN_PLANNER_APP_ID", "123456")

    cfg = _make_config(clone)
    fake_host = _FakeHostProvider()
    fake_registry = MagicMock()
    fake_registry.get_host_provider.return_value = fake_host
    # Planner role-token (Stage 3e follow-up): WorktreeManager threads
    # this into git subprocess GH_TOKEN. MagicMock default returns a
    # Mock object that subprocess rejects with "environment can only
    # contain strings", so pin it to a string.
    fake_registry.get_planner_token.return_value = "fake-planner-token"

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(
        return_value=_with_usage(
            _make_llm_output(
                pr_body="Drafted SSML support spec. Closes #42. See spec doc."
            )
        )
    )

    result = await run_planner(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=fake_registry,
    )

    # The body that reached open_pull_request must NOT contain any of the
    # nine auto-close keywords followed by an issue reference.
    assert fake_host.last_open_pr_body is not None
    assert not re.search(
        r"(?i)\b(close[sd]?|fix(?:es|ed)?|resolve[sd]?)\b\s*:?\s+"
        r"(?:[\w.-]+/[\w.-]+)?#\d+",
        fake_host.last_open_pr_body,
    ), f"Auto-close keyword leaked into PR body: {fake_host.last_open_pr_body!r}"
    # The bare issue reference must survive — humans still want to see it.
    assert "#42" in fake_host.last_open_pr_body
    # The audit-log copy preserves what the LLM actually produced.
    assert "Closes #42" in result.llm_output.pr_body


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
    # Planner role-token (Stage 3e follow-up): WorktreeManager threads
    # this into git subprocess GH_TOKEN. MagicMock default returns a
    # Mock object that subprocess rejects with "environment can only
    # contain strings", so pin it to a string.
    fake_registry.get_planner_token.return_value = "fake-planner-token"

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_make_llm_output()))

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
        "# Foreman instructions for voice\n\n## PR title rules\nUse `feat(scope): ...` only.\n"
    )
    (foreman_dir / "INSTRUCTIONS.md").write_text(instructions_text, encoding="utf-8")

    cfg = _make_config(clone)
    fake_host = _FakeHostProvider()
    fake_registry = MagicMock()
    fake_registry.get_host_provider.return_value = fake_host
    # Planner role-token (Stage 3e follow-up): WorktreeManager threads
    # this into git subprocess GH_TOKEN. MagicMock default returns a
    # Mock object that subprocess rejects with "environment can only
    # contain strings", so pin it to a string.
    fake_registry.get_planner_token.return_value = "fake-planner-token"
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_make_llm_output()))

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
    # Planner role-token (Stage 3e follow-up): WorktreeManager threads
    # this into git subprocess GH_TOKEN. MagicMock default returns a
    # Mock object that subprocess rejects with "environment can only
    # contain strings", so pin it to a string.
    fake_registry.get_planner_token.return_value = "fake-planner-token"
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_make_llm_output()))

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
    # Planner role-token (Stage 3e follow-up): WorktreeManager threads
    # this into git subprocess GH_TOKEN. MagicMock default returns a
    # Mock object that subprocess rejects with "environment can only
    # contain strings", so pin it to a string.
    fake_registry.get_planner_token.return_value = "fake-planner-token"
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_make_llm_output()))

    with pytest.raises(ValueError, match="does not match project"):
        await run_planner(
            issue_url="https://github.com/someone-else/other-repo/issues/1",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=fake_registry,
        )


# ----------------------------------------------------------------------
# dev_base_branch wiring (Foreman issue #16)
#
# When ``ProjectConfig.dev_base_branch`` is set, ``run_planner`` must
# thread that value to ``WorktreeManager.create``. When unset, it must
# pass ``None`` so behavior matches the pre-#16 contract.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_planner_threads_dev_base_branch_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``ProjectConfig.dev_base_branch`` is set, the value reaches
    ``WorktreeManager.create`` as the ``dev_base_branch`` kwarg.

    We stub ``WorktreeManager.create`` because the wiring is what's under
    test here — the real branch-creation path is covered by
    ``test_create_with_dev_base_branch_uses_alt_branch`` in test_worktree.py.
    """
    clone = tmp_path / "clone"
    _seed_clone(clone, origin_path=tmp_path / "origin.git")
    monkeypatch.setenv("FOREMAN_PLANNER_APP_ID", "123456")

    cfg = Config(
        projects={
            "voice": ProjectConfig(
                repo="jeffrichley/voice",
                local_clone_path=str(clone),
                dev_base_branch="feat/walking-skeleton",
                apps=AppsConfig(
                    planner_app_id_env="FOREMAN_PLANNER_APP_ID",
                    planner_private_key_path="/tmp/planner.pem",
                ),
            )
        }
    )

    fake_host = _FakeHostProvider()
    fake_registry = MagicMock()
    fake_registry.get_host_provider.return_value = fake_host
    # Planner role-token (Stage 3e follow-up): WorktreeManager threads
    # this into git subprocess GH_TOKEN. MagicMock default returns a
    # Mock object that subprocess rejects with "environment can only
    # contain strings", so pin it to a string.
    fake_registry.get_planner_token.return_value = "fake-planner-token"
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_make_llm_output()))

    # Stub create — pre-make the worktree dir so identity-config + later
    # commit/push ops can still run against a real path. We're testing
    # the wiring, not the git operation.
    captured: dict[str, Any] = {}
    wt_path = tmp_path / "worktrees" / "voice" / "issue-42"
    wt_path.mkdir(parents=True, exist_ok=True)

    def stub_create(self: Any, **kwargs: Any) -> Path:
        captured.update(kwargs)
        return wt_path

    monkeypatch.setattr("foreman.worktree.WorktreeManager.create", stub_create)

    await run_planner(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=fake_registry,
    )

    assert captured.get("dev_base_branch") == "feat/walking-skeleton", (
        "run_planner must forward project.dev_base_branch to "
        f"WorktreeManager.create; got {captured.get('dev_base_branch')!r}"
    )


@pytest.mark.asyncio
async def test_run_planner_passes_none_when_dev_base_branch_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``ProjectConfig.dev_base_branch`` is None (the default), the
    Planner passes ``None`` through so ``WorktreeManager.create`` falls
    back to its existing default-branch resolution.
    """
    clone = tmp_path / "clone"
    _seed_clone(clone, origin_path=tmp_path / "origin.git")
    monkeypatch.setenv("FOREMAN_PLANNER_APP_ID", "123456")

    cfg = _make_config(clone)  # _make_config omits dev_base_branch → None
    assert cfg.projects["voice"].dev_base_branch is None, (
        "test fixture assumption: _make_config produces dev_base_branch=None"
    )

    fake_host = _FakeHostProvider()
    fake_registry = MagicMock()
    fake_registry.get_host_provider.return_value = fake_host
    # Planner role-token (Stage 3e follow-up): WorktreeManager threads
    # this into git subprocess GH_TOKEN. MagicMock default returns a
    # Mock object that subprocess rejects with "environment can only
    # contain strings", so pin it to a string.
    fake_registry.get_planner_token.return_value = "fake-planner-token"
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_make_llm_output()))

    captured: dict[str, Any] = {}
    wt_path = tmp_path / "worktrees" / "voice" / "issue-42"
    wt_path.mkdir(parents=True, exist_ok=True)

    def stub_create(self: Any, **kwargs: Any) -> Path:
        captured.update(kwargs)
        return wt_path

    monkeypatch.setattr("foreman.worktree.WorktreeManager.create", stub_create)

    await run_planner(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=fake_registry,
    )

    # Must be passed explicitly (key present) and equal to None — not just
    # omitted. Omission would still default to None today but would silently
    # break a future refactor that makes the kwarg required.
    assert "dev_base_branch" in captured, (
        "run_planner must pass dev_base_branch as a kwarg even when None"
    )
    assert captured["dev_base_branch"] is None


# ----------------------------------------------------------------------
# foreman#91 — final_labels is the authoritative post-transition set
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_planner_returns_authoritative_final_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """foreman#91 + v3: ``PlannerRunResult.final_labels`` is the sorted
    pre-mutation label set — computed in-process from the role's known
    transitions, not via a post-mutation host re-read. In v3 the Planner
    performs ZERO label mutations: the issue stays at ``foreman:planning``
    (the entry label) so the reconciler can fire ``dispatch_reviewer_spec``
    on the open spec PR. External labels and any legacy ``foreman:plan``
    label survive untouched — the daemon never crashed trying to remove
    a label that isn't there."""
    clone = tmp_path / "clone"
    _seed_clone(clone, origin_path=tmp_path / "origin.git")
    monkeypatch.setenv("FOREMAN_PLANNER_APP_ID", "123456")

    cfg = _make_config(clone)
    fake_host = _FakeHostProvider()
    # Pre-mutation snapshot the host returns includes the planning
    # umbrella label, a legacy ``foreman:plan`` entry, plus an external
    # label that must survive the transition unchanged.
    fake_host.issue_to_return = IssueRef(
        number=42,
        title="SSML",
        body="Add SSML support.",
        labels=["foreman:plan", "foreman:planning", "external-tag"],
        repo_slug="jeffrichley/voice",
    )
    fake_registry = MagicMock()
    fake_registry.get_host_provider.return_value = fake_host
    # Planner role-token (Stage 3e follow-up): WorktreeManager threads
    # this into git subprocess GH_TOKEN. MagicMock default returns a
    # Mock object that subprocess rejects with "environment can only
    # contain strings", so pin it to a string.
    fake_registry.get_planner_token.return_value = "fake-planner-token"

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_make_llm_output()))

    result = await run_planner(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=fake_registry,
    )

    # v3 deterministic: the Planner makes no label mutations, so the
    # final set equals the pre-mutation snapshot. Any v2-era cleanup of
    # ``foreman:plan`` is the operator's responsibility — not worth a
    # PyGithub 404 crash on the much-more-common fresh-v3-ticket path.
    assert result.final_labels == sorted(
        ["external-tag", "foreman:plan", "foreman:planning"]
    )


# ----------------------------------------------------------------------
# foreman#235 — failure-path stats logging
#
# Before #235, ``log_planner_run`` was only called on the happy path. If
# anything after ``start_time`` raised (WorktreeManager.create,
# provider.run_agent, host.commit_files_to_worktree, host.push_branch,
# host.open_pull_request), the exception propagated up to the daemon
# dispatcher and NO JSONL row was written for the failed run. Cost
# telemetry for failed Planner runs vanished. #235 wraps the body in a
# try/except so the failure path also writes a row — with whatever
# partial ``usage`` / ``pr_number`` state was captured before the
# failure.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_planner_logs_spec_failed_with_partial_usage_when_open_pr_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """foreman#235: when ``host.open_pull_request`` raises after the LLM
    call has already returned, ``run_planner`` must:

    1. Re-raise the original exception unchanged so the daemon
       dispatcher's error handling stays in charge.
    2. Append a ``planner.jsonl`` row with ``outcome="spec_failed"``,
       ``pr_number=None`` (the PR never opened), and the
       ``input_tokens`` / ``output_tokens`` that the successful prior
       ``provider.run_agent`` call reported. Cost telemetry for failed
       runs must survive even when the failure happens mid-run.
    """
    import json as _json

    stats_root = tmp_path / "stats"
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(stats_root))
    clone = tmp_path / "clone"
    _seed_clone(clone, origin_path=tmp_path / "origin.git")
    monkeypatch.setenv("FOREMAN_PLANNER_APP_ID", "123456")

    cfg = _make_config(clone)
    fake_host = _FakeHostProvider()

    # Make ``open_pull_request`` fail. Everything before it
    # (worktree.create, provider.run_agent, commit, push) succeeds — so
    # ``usage`` from the LLM call should be captured and reach the
    # ``spec_failed`` row.
    class _OpenPRBoom(RuntimeError):
        pass

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise _OpenPRBoom("github API exploded")

    fake_host.open_pull_request = _boom  # type: ignore[method-assign]

    fake_registry = MagicMock()
    fake_registry.get_host_provider.return_value = fake_host
    fake_registry.get_planner_token.return_value = "fake-planner-token"

    # UsageInfo with non-zero token counts so we can prove the partial
    # capture actually carries the prior run_agent's values (vs the
    # safe-default zeros).
    partial_usage = UsageInfo(
        input_tokens=1234,
        output_tokens=567,
        total_cost_usd=0.012,
        duration_ms=4321,
        num_turns=3,
    )
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=(_make_llm_output(), partial_usage))

    with pytest.raises(_OpenPRBoom, match="github API exploded"):
        await run_planner(
            issue_url="https://github.com/jeffrichley/voice/issues/42",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=fake_registry,
        )

    # JSONL row landed for the failed run.
    jsonl = stats_root / "jeffrichley__voice" / "planner.jsonl"
    assert jsonl.exists(), (
        "spec_failed run must still append a row to planner.jsonl; #235 "
        "fixes the silent-disappearance bug where exceptions before the "
        "success-path log call skipped the write entirely"
    )
    rows = [_json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    assert len(rows) == 1, f"exactly one row expected, got {len(rows)}: {rows!r}"
    row = rows[0]

    # Outcome + pr_number reflect the failed-mid-run state.
    assert row["outcome"] == "spec_failed"
    assert row["pr_number"] is None
    assert row["role"] == "planner"
    assert row["issue_number"] == 42

    # Partial usage from the successful prior run_agent call survived.
    assert row["input_tokens"] == 1234
    assert row["output_tokens"] == 567
    assert row["total_cost_usd"] == 0.012
    assert row["duration_ms"] == 4321
    assert row["num_turns"] == 3
    # duration_seconds is non-negative wall-clock — we can't pin an exact
    # value but it must be present and a real float.
    assert isinstance(row["duration_seconds"], (int, float))
    assert row["duration_seconds"] >= 0.0


@pytest.mark.asyncio
async def test_run_planner_logs_spec_failed_with_safe_defaults_when_run_agent_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """foreman#235: when ``provider.run_agent`` raises before producing
    a ``UsageInfo``, the spec_failed row still lands — with the
    safe-default zeros for token / cost / duration fields (the spec's
    "fall back to safe defaults" branch). pr_number is also ``None``
    because the PR never opened.
    """
    import json as _json

    stats_root = tmp_path / "stats"
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(stats_root))
    clone = tmp_path / "clone"
    _seed_clone(clone, origin_path=tmp_path / "origin.git")
    monkeypatch.setenv("FOREMAN_PLANNER_APP_ID", "123456")

    cfg = _make_config(clone)
    fake_host = _FakeHostProvider()
    fake_registry = MagicMock()
    fake_registry.get_host_provider.return_value = fake_host
    fake_registry.get_planner_token.return_value = "fake-planner-token"

    class _RunAgentBoom(RuntimeError):
        pass

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(side_effect=_RunAgentBoom("LLM transport failed"))

    with pytest.raises(_RunAgentBoom, match="LLM transport failed"):
        await run_planner(
            issue_url="https://github.com/jeffrichley/voice/issues/42",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=fake_registry,
        )

    jsonl = stats_root / "jeffrichley__voice" / "planner.jsonl"
    assert jsonl.exists()
    rows = [_json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "spec_failed"
    assert row["pr_number"] is None
    # Safe-default zeros (no UsageInfo captured because run_agent raised).
    assert row["input_tokens"] == 0
    assert row["output_tokens"] == 0
    assert row["total_cost_usd"] is None
    assert row["model_usage"] is None
    assert row["duration_ms"] == 0
    assert row["num_turns"] == 0
