"""Integration test for ``run_reviewer`` with a fake PyGithub client + fake
ProviderFacade.

Verifies orchestration wiring: PR URL parsing, label pre-flight, branch →
issue derivation, worktree attach (no new branch), LLM dispatch with the
Pydantic-first contract + env-injection, PR-review post, issue-label
advancement based on outcome. A real-engine integration test (against
actual Anthropic API + real GitHub) is gated behind ``real_engine`` and
lives separately.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from foreman.config import AppsConfig, Config, ProjectConfig
from foreman.roles.reviewer import (
    FINDINGS_BEGIN_MARKER,
    FINDINGS_END_MARKER,
    REVIEWER_ALLOWED_TOOLS,
    _issue_number_from_branch,
    parse_pr_url,
    run_reviewer,
)
from foreman.schemas.reviewer import Finding, ReviewerOutput

# ----------------------------------------------------------------------
# parse_pr_url
# ----------------------------------------------------------------------


def test_parse_pr_url_extracts_owner_repo_number() -> None:
    owner, repo, number = parse_pr_url("https://github.com/owner/repo/pull/42")
    assert owner == "owner"
    assert repo == "repo"
    assert number == 42


def test_parse_pr_url_rejects_issue_url() -> None:
    with pytest.raises(ValueError, match="Not a GitHub PR URL"):
        parse_pr_url("https://github.com/owner/repo/issues/42")


# ----------------------------------------------------------------------
# _issue_number_from_branch
# ----------------------------------------------------------------------


def test_issue_number_from_branch_parses_foreman_branch() -> None:
    assert _issue_number_from_branch("foreman/issue-42") == 42


def test_issue_number_from_branch_rejects_unrelated_branch() -> None:
    with pytest.raises(ValueError, match="not a Foreman spec branch"):
        _issue_number_from_branch("feature/some-thing")


# ----------------------------------------------------------------------
# Tool surface
# ----------------------------------------------------------------------


def test_reviewer_allowed_tools_is_read_only_plus_bash() -> None:
    """Reviewer LLM never writes files. Read/Glob/Grep for recon + Bash for
    shell-level recon (``gh pr view``, ``git log``). Pinning this list
    prevents accidental Edit/Write reintroduction."""
    assert set(REVIEWER_ALLOWED_TOOLS) == {"Read", "Grep", "Glob", "Bash"}


# ----------------------------------------------------------------------
# Test scaffolding — fake GitHub objects mimicking PyGithub surface
# ----------------------------------------------------------------------


class _FakeLabel:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeRef:
    def __init__(self, ref: str, sha: str) -> None:
        self.ref = ref
        self.sha = sha


class _FakePR:
    def __init__(
        self,
        *,
        number: int,
        title: str,
        body: str,
        head_ref: str,
        head_sha: str,
        base_ref: str,
    ) -> None:
        self.number = number
        self.title = title
        self.body = body
        self.head = _FakeRef(head_ref, head_sha)
        self.base = _FakeRef(base_ref, "basesha")
        self.reviews_posted: list[tuple[str, str]] = []

    def create_review(self, body: str, event: str) -> None:
        self.reviews_posted.append((body, event))


class _FakeIssue:
    def __init__(
        self, *, number: int, title: str, body: str, labels: list[str]
    ) -> None:
        self.number = number
        self.title = title
        self.body = body
        self.labels = [_FakeLabel(label) for label in labels]
        self.removed: list[str] = []
        self.added: list[str] = []

    def remove_from_labels(self, label: str) -> None:
        self.removed.append(label)

    def add_to_labels(self, label: str) -> None:
        self.added.append(label)


class _FakeRepo:
    def __init__(self, *, pr: _FakePR, issue: _FakeIssue) -> None:
        self._pr = pr
        self._issue = issue
        self.get_pull_calls: list[int] = []
        self.get_issue_calls: list[int] = []

    def get_pull(self, number: int) -> _FakePR:
        self.get_pull_calls.append(number)
        return self._pr

    def get_issue(self, number: int) -> _FakeIssue:
        self.get_issue_calls.append(number)
        return self._issue


class _FakeReviewerClient:
    """Subset of PyGithub's ``Github`` surface used by the Reviewer."""

    def __init__(self, *, repo: _FakeRepo) -> None:
        self._repo = repo
        self.get_repo_calls: list[str] = []

    def get_repo(self, slug: str) -> _FakeRepo:
        self.get_repo_calls.append(slug)
        return self._repo


# ----------------------------------------------------------------------
# Seed helpers — set up a clone + worktree the way Planner would
# ----------------------------------------------------------------------


def _seed_clone_with_spec_branch(clone: Path, issue_number: int) -> str:
    """Init a minimal git repo with a Planner-style ``foreman/issue-N`` branch.

    Returns the head SHA on the spec branch.
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
    # Set up an origin so the Reviewer's ``git fetch origin <base>`` is a no-op
    # rather than a hard failure.
    subprocess.run(
        ["git", "remote", "add", "origin", str(clone)],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "fetch", "origin"], cwd=clone, check=False, capture_output=True)
    # Branch + spec doc commit, mirroring what Planner produces.
    branch = f"foreman/issue-{issue_number}"
    subprocess.run(
        ["git", "checkout", "-b", branch], cwd=clone, check=True, capture_output=True
    )
    spec_dir = clone / "docs" / "superpowers" / "specs"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / f"foreman-issue-{issue_number}-spec.md").write_text(
        f"# Spec for issue #{issue_number}\n"
    )
    subprocess.run(["git", "add", "."], cwd=clone, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "spec doc"], cwd=clone, check=True, capture_output=True
    )
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # Return to main so the branch is purely an existing local ref for attach.
    subprocess.run(["git", "checkout", "main"], cwd=clone, check=True, capture_output=True)
    return head_sha


def _make_config(clone: Path) -> Config:
    return Config(
        projects={
            "voice": ProjectConfig(
                repo="jeffrichley/voice",
                local_clone_path=str(clone),
                apps=AppsConfig(
                    planner_app_id_env="FOREMAN_PLANNER_APP_ID",
                    planner_private_key_path="/tmp/planner.pem",
                    reviewer_app_id_env="FOREMAN_REVIEWER_APP_ID",
                    reviewer_private_key_path="/tmp/reviewer.pem",
                ),
            )
        }
    )


def _make_clean_output() -> ReviewerOutput:
    return ReviewerOutput(
        outcome="clean",
        review_comment="Clean — traced ACs 1-4 against the spec.",
        findings=[],
        confidence="high",
    )


def _make_needs_fix_output() -> ReviewerOutput:
    return ReviewerOutput(
        outcome="needs_fix",
        review_comment="needs_fix — sub-request 2 references missing file.",
        findings=[
            Finding(
                severity="critical",
                target="packages/foo/src/foo/missing.py",
                issue="File referenced by spec does not exist.",
                needed="Either create the file or remove the reference.",
            )
        ],
        confidence="medium",
    )


def _make_fake_repo(
    *,
    issue_number: int,
    head_sha: str,
    labels: list[str] | None = None,
) -> tuple[_FakeRepo, _FakePR, _FakeIssue]:
    labels = labels if labels is not None else ["foreman:spec-review"]
    pr = _FakePR(
        number=77,
        title="spec: SSML",
        body="Adds SSML spec. Closes #" + str(issue_number) + ".",
        head_ref=f"foreman/issue-{issue_number}",
        head_sha=head_sha,
        base_ref="main",
    )
    issue = _FakeIssue(
        number=issue_number, title="SSML", body="Add SSML support.", labels=labels
    )
    repo = _FakeRepo(pr=pr, issue=issue)
    return repo, pr, issue


def _make_registry(client: _FakeReviewerClient, token: str = "ghs_reviewer_token") -> Any:
    reg = MagicMock()
    reg.get_reviewer_client.return_value = client
    reg.get_reviewer_token.return_value = token
    return reg


# ----------------------------------------------------------------------
# run_reviewer end-to-end
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_reviewer_clean_outcome_advances_to_spec_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_REVIEWER_APP_ID", "123456")

    cfg = _make_config(clone)
    repo, pr, issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeReviewerClient(repo=repo)
    registry = _make_registry(client)

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_make_clean_output())

    result = await run_reviewer(
        pr_url="https://github.com/jeffrichley/voice/pull/77",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    # LLM dispatched once
    fake_provider.run_agent.assert_called_once()
    call_kwargs = fake_provider.run_agent.call_args.kwargs

    # Tool surface — read-only + Bash
    assert call_kwargs["allowed_tools"] == ["Read", "Grep", "Glob", "Bash"]

    # Pydantic-first contract — model class, not a schema dict
    assert call_kwargs["output_model"] is ReviewerOutput
    assert "output_schema" not in call_kwargs

    # Env injected with reviewer-bot's GH_TOKEN (parity with what was asked
    # in the brief). Parent env should still be present so PATH etc. work.
    env = call_kwargs["env"]
    assert env is not None
    assert env["GH_TOKEN"] == "ghs_reviewer_token"
    assert "PATH" in env or len(env) > 1  # parent env merged in

    # PR review posted, NOT approved. The Reviewer's prose is preserved
    # verbatim at the start, then enriched with the marker-fenced findings
    # block (always emitted, even for clean outcomes — the empty-list shape
    # is part of the contract).
    assert len(pr.reviews_posted) == 1
    body, event = pr.reviews_posted[0]
    assert event == "COMMENT"
    assert body.startswith("Clean — traced ACs 1-4 against the spec.")
    assert FINDINGS_BEGIN_MARKER in body
    assert FINDINGS_END_MARKER in body

    # Issue label advanced: spec-review → spec-ready
    assert issue.removed == ["foreman:spec-review"]
    assert issue.added == ["foreman:spec-ready"]

    # PR's labels NOT touched (label transition is on the issue)
    # (The fake PR's `add_to_labels` / `remove_from_labels` don't exist;
    # this passes implicitly — leaving an assertion-by-omission note.)

    # ReviewerOutput returned
    assert isinstance(result, ReviewerOutput)
    assert result.outcome == "clean"
    assert result.confidence == "high"


@pytest.mark.asyncio
async def test_run_reviewer_embeds_structured_findings_json_in_review_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The posted review body must carry the structured findings as a
    marker-fenced JSON block so the Fixer can recover them from what GitHub
    stored. Without this, the Fixer's "every edit traces to a structured
    finding" rule produces zero actions (the in-memory ``findings`` list
    never crosses the role boundary). Round-trip the JSON to confirm
    severity/target/issue/needed survive verbatim.
    """
    import json
    import re

    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_REVIEWER_APP_ID", "123456")

    cfg = _make_config(clone)
    repo, pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeReviewerClient(repo=repo)
    registry = _make_registry(client)

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_make_needs_fix_output())

    await run_reviewer(
        pr_url="https://github.com/jeffrichley/voice/pull/77",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    body, _event = pr.reviews_posted[0]
    assert FINDINGS_BEGIN_MARKER in body
    assert FINDINGS_END_MARKER in body

    # Pull the fenced JSON out of the marker-delimited region.
    between = body.split(FINDINGS_BEGIN_MARKER, 1)[1].split(FINDINGS_END_MARKER, 1)[0]
    m = re.search(r"```json\n(.*?)\n```", between, flags=re.DOTALL)
    assert m is not None, f"no fenced JSON block found between markers: {between!r}"
    parsed = json.loads(m.group(1))

    expected = _make_needs_fix_output().findings
    assert len(parsed) == len(expected)
    for got, want in zip(parsed, expected, strict=True):
        assert got["severity"] == want.severity
        assert got["target"] == want.target
        assert got["issue"] == want.issue
        assert got["needed"] == want.needed


@pytest.mark.asyncio
async def test_run_reviewer_embeds_empty_findings_list_when_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The block is always emitted — empty list (``[]``) on clean outcomes —
    so the Fixer's extractor sees a single predictable shape. Pinning this
    keeps the wire format unconditional.
    """
    import json
    import re

    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_REVIEWER_APP_ID", "123456")

    cfg = _make_config(clone)
    repo, pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeReviewerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_make_clean_output())

    await run_reviewer(
        pr_url="https://github.com/jeffrichley/voice/pull/77",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    body, _event = pr.reviews_posted[0]
    between = body.split(FINDINGS_BEGIN_MARKER, 1)[1].split(FINDINGS_END_MARKER, 1)[0]
    m = re.search(r"```json\n(.*?)\n```", between, flags=re.DOTALL)
    assert m is not None
    assert json.loads(m.group(1)) == []


@pytest.mark.asyncio
async def test_run_reviewer_needs_fix_outcome_advances_to_spec_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_REVIEWER_APP_ID", "123456")

    cfg = _make_config(clone)
    repo, pr, issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeReviewerClient(repo=repo)
    registry = _make_registry(client)

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_make_needs_fix_output())

    result = await run_reviewer(
        pr_url="https://github.com/jeffrichley/voice/pull/77",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    # PR review posted with the needs_fix prose
    assert len(pr.reviews_posted) == 1
    body, event = pr.reviews_posted[0]
    assert "needs_fix" in body
    assert event == "COMMENT"

    # Issue label advanced: spec-review → spec-fix
    assert issue.removed == ["foreman:spec-review"]
    assert issue.added == ["foreman:spec-fix"]

    assert result.outcome == "needs_fix"
    assert len(result.findings) == 1


@pytest.mark.asyncio
async def test_run_reviewer_reuses_existing_branch_does_not_create_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Reviewer attaches to the Planner's existing branch — it must NOT
    pass ``-b`` to ``git worktree add`` (which would try to create a new
    branch from HEAD)."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_REVIEWER_APP_ID", "123456")

    cfg = _make_config(clone)
    repo, _pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeReviewerClient(repo=repo)
    registry = _make_registry(client)

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_make_clean_output())

    await run_reviewer(
        pr_url="https://github.com/jeffrichley/voice/pull/77",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    # Worktree exists at the expected path and is checked out on the
    # spec branch (proves attach, not create-with-new-branch).
    wt_path = tmp_path / "worktrees" / "voice" / "issue-42"
    assert wt_path.exists()
    current_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=wt_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert current_branch == "foreman/issue-42"

    # And the branch existed BEFORE attach (it points at the seeded spec
    # commit, not a fresh main-branch commit) — proves no `-b` was passed.
    rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert rev == head_sha


@pytest.mark.asyncio
async def test_run_reviewer_missing_spec_review_label_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-flight guard: source issue without ``foreman:spec-review`` is
    not advanced — we refuse to act on issues not queued by the Planner."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_REVIEWER_APP_ID", "123456")

    cfg = _make_config(clone)
    # Issue has a random unrelated label, NOT foreman:spec-review
    repo, pr, issue = _make_fake_repo(
        issue_number=42, head_sha=head_sha, labels=["random-label"]
    )
    client = _FakeReviewerClient(repo=repo)
    registry = _make_registry(client)

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_make_clean_output())

    with pytest.raises(RuntimeError, match="foreman:spec-review"):
        await run_reviewer(
            pr_url="https://github.com/jeffrichley/voice/pull/77",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=registry,
        )

    # No LLM dispatch, no review post, no label mutation
    fake_provider.run_agent.assert_not_called()
    assert pr.reviews_posted == []
    assert issue.removed == []
    assert issue.added == []


@pytest.mark.asyncio
async def test_run_reviewer_rejects_url_pointing_at_wrong_project(
    tmp_path: Path,
) -> None:
    clone = tmp_path / "clone"
    _seed_clone_with_spec_branch(clone, issue_number=42)
    cfg = _make_config(clone)
    repo, _pr, _issue = _make_fake_repo(issue_number=42, head_sha="x" * 40)
    client = _FakeReviewerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_make_clean_output())

    with pytest.raises(ValueError, match="does not match project"):
        await run_reviewer(
            pr_url="https://github.com/someone-else/other-repo/pull/1",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=registry,
        )


@pytest.mark.asyncio
async def test_run_reviewer_passes_env_with_reviewer_token_and_parent_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer-bot's installation token flows into the agent
    subprocess via ``env={"GH_TOKEN": reviewer_token, **os.environ}`` so any
    ``gh`` calls the LLM makes act as the reviewer bot, not the parent.

    Parity with the brief; pin this so we don't accidentally drop env-injection
    when the Planner's removal pattern is mirrored."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_REVIEWER_APP_ID", "123456")
    monkeypatch.setenv("MY_SENTINEL_VAR", "sentinel")

    cfg = _make_config(clone)
    repo, _pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeReviewerClient(repo=repo)
    registry = _make_registry(client, token="ghs_specific_reviewer_token")

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_make_clean_output())

    await run_reviewer(
        pr_url="https://github.com/jeffrichley/voice/pull/77",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    env = fake_provider.run_agent.call_args.kwargs["env"]
    assert env["GH_TOKEN"] == "ghs_specific_reviewer_token"
    assert env["MY_SENTINEL_VAR"] == "sentinel"  # parent env merged in
