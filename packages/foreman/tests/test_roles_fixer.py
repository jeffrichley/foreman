"""Integration test for ``run_fixer`` with a fake PyGithub client + fake
ProviderFacade.

Verifies orchestration wiring: issue URL parsing, entry-condition label
pre-flight, max-3-attempts gate, attempt-counter increment, worktree
attach (no new branch), LLM dispatch with Pydantic-first contract +
env-injection, fix_comment posted as PR comment (NOT review), issue-label
advancement based on outcome, stats JSONL line emission.

A real-engine integration test (against actual Anthropic API + real
GitHub) is gated behind ``real_engine`` and lives separately.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from foreman.config import AppsConfig, Config, ProjectConfig
from foreman.roles.fixer import (
    FIXER_ALLOWED_TOOLS,
    _count_fix_attempts,
    _extract_findings_from_review_comment,
    parse_issue_url,
    run_fixer,
)
from foreman.roles.reviewer import FINDINGS_BEGIN_MARKER, FINDINGS_END_MARKER
from foreman.schemas.fixer import (
    AddressedFinding,
    CommitMade,
    FixerOutput,
    UnaddressedFinding,
)

# ----------------------------------------------------------------------
# parse_issue_url
# ----------------------------------------------------------------------


def test_parse_issue_url_extracts_owner_repo_number() -> None:
    owner, repo, number = parse_issue_url("https://github.com/owner/repo/issues/42")
    assert owner == "owner"
    assert repo == "repo"
    assert number == 42


def test_parse_issue_url_rejects_pr_url() -> None:
    with pytest.raises(ValueError, match="Not a GitHub issue URL"):
        parse_issue_url("https://github.com/owner/repo/pull/42")


# ----------------------------------------------------------------------
# _count_fix_attempts
# ----------------------------------------------------------------------


def test_count_fix_attempts_zero_with_no_labels() -> None:
    assert _count_fix_attempts(set()) == 0


def test_count_fix_attempts_zero_with_only_unrelated_labels() -> None:
    assert _count_fix_attempts({"foreman:spec-fix", "bug", "P1"}) == 0


def test_count_fix_attempts_returns_max_existing() -> None:
    assert _count_fix_attempts({"foreman:fix-attempt-1", "foreman:fix-attempt-2"}) == 2


def test_count_fix_attempts_handles_single_attempt() -> None:
    assert _count_fix_attempts({"foreman:fix-attempt-1"}) == 1


def test_count_fix_attempts_skips_partial_matches() -> None:
    # `foreman:fix-attempt-` with no digit, and unrelated labels, should not count.
    assert _count_fix_attempts({"foreman:fix-attempt-x", "foreman:fix-attempt"}) == 0


# ----------------------------------------------------------------------
# Tool surface
# ----------------------------------------------------------------------


def test_fixer_allowed_tools_includes_edit_and_write() -> None:
    """Fixer LLM mutates the spec doc — Edit + Write are required.
    Bash for git stage/commit/push from inside the worktree."""
    assert set(FIXER_ALLOWED_TOOLS) == {"Read", "Grep", "Glob", "Bash", "Edit", "Write"}


# ----------------------------------------------------------------------
# Fake PyGithub surface
# ----------------------------------------------------------------------


class _FakeLabel:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeRef:
    def __init__(self, ref: str, sha: str) -> None:
        self.ref = ref
        self.sha = sha


class _FakeReview:
    def __init__(self, body: str) -> None:
        self.body = body


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
        reviews: list[_FakeReview] | None = None,
    ) -> None:
        self.number = number
        self.title = title
        self.body = body
        self.head = _FakeRef(head_ref, head_sha)
        self.base = _FakeRef(base_ref, "basesha")
        self._reviews = (
            reviews if reviews is not None else [_FakeReview("needs_fix — see findings.")]
        )
        self.issue_comments_posted: list[str] = []
        self.reviews_posted: list[tuple[str, str]] = []

    def get_reviews(self) -> list[_FakeReview]:
        return list(self._reviews)

    def create_issue_comment(self, body: str) -> None:
        self.issue_comments_posted.append(body)

    def create_review(self, body: str, event: str) -> None:
        # Pinned NOT to be called by Fixer — if it ever is, this records it
        # so the test can fail.
        self.reviews_posted.append((body, event))


class _FakeIssue:
    def __init__(self, *, number: int, title: str, body: str, labels: list[str]) -> None:
        self.number = number
        self.title = title
        self.body = body
        self.labels = [_FakeLabel(name) for name in labels]
        self.removed: list[str] = []
        self.added: list[str] = []

    def remove_from_labels(self, label: str) -> None:
        self.removed.append(label)
        self.labels = [lbl for lbl in self.labels if lbl.name != label]

    def add_to_labels(self, label: str) -> None:
        self.added.append(label)
        # Reflect in labels too so subsequent reads see it.
        self.labels.append(_FakeLabel(label))


class _FakeRepo:
    def __init__(self, *, pr: _FakePR, issue: _FakeIssue) -> None:
        self._pr = pr
        self._issue = issue
        self.full_name = "jeffrichley/voice"
        self.get_pulls_calls: list[dict[str, Any]] = []
        self.get_issue_calls: list[int] = []

    def get_pulls(
        self, state: str | None = None, head: str | None = None, **kwargs: Any
    ) -> list[_FakePR]:
        self.get_pulls_calls.append({"state": state, "head": head, **kwargs})
        return [self._pr]

    def get_issue(self, number: int) -> _FakeIssue:
        self.get_issue_calls.append(number)
        return self._issue


class _FakeFixerClient:
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
    """Init a minimal git repo with a Planner-style ``foreman/issue-N`` branch."""
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
    subprocess.run(
        ["git", "remote", "add", "origin", str(clone)],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "fetch", "origin"], cwd=clone, check=False, capture_output=True)
    branch = f"foreman/issue-{issue_number}"
    subprocess.run(["git", "checkout", "-b", branch], cwd=clone, check=True, capture_output=True)
    spec_dir = clone / "docs" / "superpowers" / "specs"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / f"foreman-issue-{issue_number}-spec.md").write_text(
        f"# Spec for issue #{issue_number}\n"
    )
    subprocess.run(["git", "add", "."], cwd=clone, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "spec doc"], cwd=clone, check=True, capture_output=True)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
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
                    fixer_app_id_env="FOREMAN_FIXER_APP_ID",
                    fixer_private_key_path="/tmp/fixer.pem",
                ),
            )
        }
    )


def _fixed_output() -> FixerOutput:
    return FixerOutput(
        outcome="fixed",
        fix_comment="fixed — addressed AC bullet 3.",
        commits_made=[
            CommitMade(
                sha="a" * 40,
                summary="fix: address Reviewer findings on spec 42",
                findings_addressed=["Acceptance criteria bullet 3"],
            ),
        ],
        addressed_findings=[
            AddressedFinding(
                target="Acceptance criteria bullet 3",
                summary="Replaced 'improve' with concrete verb.",
            ),
        ],
        unaddressed_findings=[],
        confidence="high",
    )


def _incomplete_output() -> FixerOutput:
    return FixerOutput(
        outcome="incomplete",
        fix_comment="incomplete — critical finding requires git surgery.",
        commits_made=[],
        addressed_findings=[],
        unaddressed_findings=[
            UnaddressedFinding(
                target=".github/workflows/release.yml",
                severity="critical",
                reason="requires_git_surgery",
                rationale="Drift file outside spec doc; v1 does not attempt git surgery.",
            ),
        ],
        confidence="medium",
    )


def _make_fake_repo(
    *,
    issue_number: int,
    head_sha: str,
    labels: list[str] | None = None,
    reviews: list[_FakeReview] | None = None,
) -> tuple[_FakeRepo, _FakePR, _FakeIssue]:
    labels = labels if labels is not None else ["foreman:spec-fix"]
    pr = _FakePR(
        number=77,
        title="spec: SSML",
        body=f"Adds SSML spec. Closes #{issue_number}.",
        head_ref=f"foreman/issue-{issue_number}",
        head_sha=head_sha,
        base_ref="main",
        reviews=reviews,
    )
    issue = _FakeIssue(number=issue_number, title="SSML", body="Add SSML support.", labels=labels)
    repo = _FakeRepo(pr=pr, issue=issue)
    return repo, pr, issue


def _make_registry(client: _FakeFixerClient, token: str = "ghs_fixer_token") -> Any:
    reg = MagicMock()
    reg.get_fixer_client.return_value = client
    reg.get_fixer_token.return_value = token
    return reg


# ----------------------------------------------------------------------
# run_fixer end-to-end — fixed outcome
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_fixer_fixed_outcome_advances_back_to_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, pr, issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_fixed_output())

    result = await run_fixer(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    # LLM dispatched once
    fake_provider.run_agent.assert_called_once()
    call_kwargs = fake_provider.run_agent.call_args.kwargs

    # Tool surface includes Edit + Write
    assert call_kwargs["allowed_tools"] == ["Read", "Grep", "Glob", "Bash", "Edit", "Write"]

    # Pydantic-first contract — model class, not a schema dict
    assert call_kwargs["output_model"] is FixerOutput
    assert "output_schema" not in call_kwargs

    # Env injected with fixer-bot's GH_TOKEN; parent env merged in
    env = call_kwargs["env"]
    assert env is not None
    assert env["GH_TOKEN"] == "ghs_fixer_token"

    # fix_comment posted as PR COMMENT, NOT review
    assert pr.issue_comments_posted == ["fixed — addressed AC bullet 3."]
    assert pr.reviews_posted == []

    # First-attempt label stamped at entry; v3 spec-side outcome advances
    # to ``foreman:planning`` (the planning umbrella) so the reconciler
    # re-fires ``dispatch_reviewer_spec`` on the updated PR head.
    assert "foreman:fix-attempt-1" in issue.added
    assert "foreman:planning" in issue.added
    # Per-episode counter reset: on outcome=fixed, the fix-attempt-N label
    # is also dropped so the next fix-episode (if any) gets a fresh budget.
    assert "foreman:spec-fix" in issue.removed
    assert "foreman:fix-attempt-1" in issue.removed

    # Return type
    assert result.attempt == 1
    assert result.llm_output.outcome == "fixed"


# ----------------------------------------------------------------------
# run_fixer end-to-end — incomplete outcome
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_fixer_incomplete_outcome_keeps_spec_fix_adds_needs_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, pr, issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_incomplete_output())

    result = await run_fixer(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    # PR comment posted (not review)
    assert len(pr.issue_comments_posted) == 1
    assert "incomplete" in pr.issue_comments_posted[0]
    assert pr.reviews_posted == []

    # spec-fix kept; needs-help added; first-attempt stamped
    assert "foreman:fix-attempt-1" in issue.added
    assert "foreman:needs-help" in issue.added
    assert "foreman:spec-fix" not in issue.removed

    # NOT the final attempt, so failed should NOT be added
    assert "foreman:failed" not in issue.added

    assert result.attempt == 1
    assert result.llm_output.outcome == "incomplete"


@pytest.mark.asyncio
async def test_run_fixer_incomplete_at_attempt_3_also_adds_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the third (last) attempt, an incomplete outcome escalates to
    ``foreman:failed``."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    # 2 prior attempts already on the issue
    repo, _pr, issue = _make_fake_repo(
        issue_number=42,
        head_sha=head_sha,
        labels=[
            "foreman:spec-fix",
            "foreman:fix-attempt-1",
            "foreman:fix-attempt-2",
        ],
    )
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_incomplete_output())

    result = await run_fixer(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    assert "foreman:fix-attempt-3" in issue.added
    assert "foreman:needs-help" in issue.added
    assert "foreman:failed" in issue.added
    assert result.attempt == 3


# ----------------------------------------------------------------------
# Attempt counter increments correctly
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_fixer_attempt_counter_increments_with_existing_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two existing fix-attempt-* labels → new attempt is 3."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _pr, issue = _make_fake_repo(
        issue_number=42,
        head_sha=head_sha,
        labels=[
            "foreman:spec-fix",
            "foreman:fix-attempt-1",
            "foreman:fix-attempt-2",
        ],
    )
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_fixed_output())

    result = await run_fixer(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    assert result.attempt == 3
    assert "foreman:fix-attempt-3" in issue.added


# ----------------------------------------------------------------------
# Pre-flight gates
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_fixer_missing_spec_fix_label_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, pr, issue = _make_fake_repo(issue_number=42, head_sha=head_sha, labels=["random-label"])
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_fixed_output())

    with pytest.raises(RuntimeError, match="foreman:spec-fix"):
        await run_fixer(
            issue_url="https://github.com/jeffrichley/voice/issues/42",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=registry,
        )

    # No LLM, no comment, no label mutation, no PR fetch
    fake_provider.run_agent.assert_not_called()
    assert pr.issue_comments_posted == []
    assert issue.removed == []
    assert issue.added == []


@pytest.mark.asyncio
async def test_run_fixer_max_attempts_gate_raises_before_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 fix-attempt-* labels already present → attempt 4 would be next →
    refuse with clear RuntimeError."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, pr, issue = _make_fake_repo(
        issue_number=42,
        head_sha=head_sha,
        labels=[
            "foreman:spec-fix",
            "foreman:fix-attempt-1",
            "foreman:fix-attempt-2",
            "foreman:fix-attempt-3",
        ],
    )
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_fixed_output())

    with pytest.raises(RuntimeError, match="max 3 fix-attempts"):
        await run_fixer(
            issue_url="https://github.com/jeffrichley/voice/issues/42",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=registry,
        )

    # No LLM dispatch, no new labels added (no fix-attempt-4 stamped)
    fake_provider.run_agent.assert_not_called()
    assert "foreman:fix-attempt-4" not in issue.added
    assert pr.issue_comments_posted == []


@pytest.mark.asyncio
async def test_run_fixer_honors_project_max_fix_attempts_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``ProjectConfig.max_fix_attempts`` overrides the default, the
    gate fires at the overridden value, not at the historical 3. Pinning
    this empirically because otherwise the configurable feature could
    silently regress to ``hardcoded 3`` and the default-only tests would
    never notice (the symptom this whole ticket exists to prevent).
    """
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    # Tighten the cap to 1 — first attempt is allowed; second must raise.
    cfg.projects["voice"] = cfg.projects["voice"].model_copy(
        update={"max_fix_attempts": 1}
    )
    repo, pr, issue = _make_fake_repo(
        issue_number=42,
        head_sha=head_sha,
        labels=["foreman:spec-fix", "foreman:fix-attempt-1"],
    )
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_fixed_output())

    with pytest.raises(RuntimeError, match="max 1 fix-attempts"):
        await run_fixer(
            issue_url="https://github.com/jeffrichley/voice/issues/42",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=registry,
        )

    fake_provider.run_agent.assert_not_called()
    assert "foreman:fix-attempt-2" not in issue.added


@pytest.mark.asyncio
async def test_run_fixer_rejects_url_pointing_at_wrong_project(
    tmp_path: Path,
) -> None:
    clone = tmp_path / "clone"
    _seed_clone_with_spec_branch(clone, issue_number=42)
    cfg = _make_config(clone)
    repo, _pr, _issue = _make_fake_repo(issue_number=42, head_sha="x" * 40)
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_fixed_output())

    with pytest.raises(ValueError, match="does not match project"):
        await run_fixer(
            issue_url="https://github.com/someone-else/other-repo/issues/1",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=registry,
        )


# ----------------------------------------------------------------------
# Worktree reuse
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_fixer_attaches_existing_branch_does_not_create_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Fixer attaches to the Planner's existing branch — it must NOT
    pass ``-b`` to ``git worktree add``."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_fixed_output())

    await run_fixer(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

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

    rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert rev == head_sha


# ----------------------------------------------------------------------
# fix_comment posted as PR comment, NOT review
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_fixer_uses_create_issue_comment_not_create_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Fixer posts an *issue comment* on the PR — it is NOT a review.
    Reviews come from Reviewers. Pinned via mock distinguishing the two."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_fixed_output())

    await run_fixer(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    assert len(pr.issue_comments_posted) == 1
    assert pr.issue_comments_posted[0] == "fixed — addressed AC bullet 3."
    # The key assertion — create_review was NOT invoked.
    assert pr.reviews_posted == []


# ----------------------------------------------------------------------
# Env injection — fixer-bot GH_TOKEN + parent env merged
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_fixer_passes_env_with_fixer_token_and_parent_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))
    monkeypatch.setenv("MY_SENTINEL_VAR", "sentinel-fixer")

    cfg = _make_config(clone)
    repo, _pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client, token="ghs_specific_fixer_token")
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_fixed_output())

    await run_fixer(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    env = fake_provider.run_agent.call_args.kwargs["env"]
    assert env["GH_TOKEN"] == "ghs_specific_fixer_token"
    assert env["MY_SENTINEL_VAR"] == "sentinel-fixer"


# ----------------------------------------------------------------------
# Stats JSONL line emission
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_fixer_writes_stats_jsonl_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    stats_root = tmp_path / "stats"
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(stats_root))

    cfg = _make_config(clone)
    repo, _pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_fixed_output())

    await run_fixer(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    stats_file = stats_root / "jeffrichley__voice" / "fixer.jsonl"
    assert stats_file.exists()
    line = stats_file.read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    assert payload["issue_number"] == 42
    assert payload["pr_number"] == 77
    assert payload["attempt"] == 1
    assert payload["outcome"] == "fixed"
    assert payload["addressed_count"] == 1
    assert payload["unaddressed_count"] == 0
    assert payload["confidence"] == "high"
    assert payload["duration_seconds"] >= 0


@pytest.mark.asyncio
async def test_run_fixer_stats_disagreed_count_tracks_needed_remediation_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``disagreed_count`` must equal the number of unaddressed findings
    with reason == ``needed_remediation_wrong``."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    stats_root = tmp_path / "stats"
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(stats_root))

    cfg = _make_config(clone)
    repo, _pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)

    mixed_output = FixerOutput(
        outcome="incomplete",
        fix_comment="mixed.",
        commits_made=[],
        addressed_findings=[],
        unaddressed_findings=[
            UnaddressedFinding(
                target="AC 1",
                severity="important",
                reason="needed_remediation_wrong",
                rationale="Reviewer's needed contradicts issue line 5: 'must use SSML'.",
            ),
            UnaddressedFinding(
                target="AC 2",
                severity="important",
                reason="needed_remediation_wrong",
                rationale="Spec section A already says the opposite; applying would contradict.",
            ),
            UnaddressedFinding(
                target="AC 3",
                severity="minor",
                reason="needs_info",
                rationale="issue doesn't specify the throughput target",
            ),
        ],
        confidence="medium",
    )

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=mixed_output)

    await run_fixer(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    stats_file = stats_root / "jeffrichley__voice" / "fixer.jsonl"
    payload = json.loads(stats_file.read_text(encoding="utf-8").strip())
    assert payload["disagreed_count"] == 2
    assert payload["unaddressed_by_reason"] == {
        "needed_remediation_wrong": 2,
        "needs_info": 1,
    }


# ----------------------------------------------------------------------
# Spec PR resolution + Reviewer review prerequisite
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_fixer_raises_when_no_open_spec_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If no open PR exists for the issue's spec branch, refuse to run."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)

    # Patch get_pulls to return empty list — no open PR
    def _empty_pulls(**_: Any) -> list[_FakePR]:
        return []

    repo.get_pulls = _empty_pulls  # type: ignore[assignment]
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_fixed_output())

    with pytest.raises(RuntimeError, match="No open PR"):
        await run_fixer(
            issue_url="https://github.com/jeffrichley/voice/issues/42",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=registry,
        )


@pytest.mark.asyncio
async def test_run_fixer_raises_when_pr_has_no_reviews(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Fixer requires a Reviewer review to act on; no reviews → error."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha, reviews=[])
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_fixed_output())

    with pytest.raises(RuntimeError, match="no reviews"):
        await run_fixer(
            issue_url="https://github.com/jeffrichley/voice/issues/42",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=registry,
        )


# ----------------------------------------------------------------------
# _extract_findings_from_review_comment — unit tests
# ----------------------------------------------------------------------


def _build_review_body_with_findings(findings_json: str) -> str:
    """Compose a review body in the exact shape the Reviewer posts.

    The Reviewer wraps the fenced JSON in a ``<details>`` fold between the
    begin/end markers. Tests mirror that layout so extraction stays anchored
    to the real wire format, not a simplified stand-in.
    """
    return (
        "needs_fix — see findings below.\n\n"
        f"{FINDINGS_BEGIN_MARKER}\n"
        "<details>\n"
        "<summary>Structured findings (for Fixer)</summary>\n\n"
        f"```json\n{findings_json}\n```\n\n"
        "</details>\n"
        f"{FINDINGS_END_MARKER}"
    )


def test_extract_findings_happy_path_returns_finding_list() -> None:
    """Well-formed marker-fenced block → parsed list of ``Finding``."""
    payload = json.dumps(
        [
            {
                "severity": "critical",
                "target": "packages/foo/src/foo/missing.py",
                "issue": "Spec references a file that doesn't exist.",
                "needed": "Create the file or remove the reference.",
            },
            {
                "severity": "important",
                "target": "Acceptance criteria bullet 3",
                "issue": "Says 'improve' without a concrete verb.",
                "needed": "Replace 'improve' with 'returns within 200ms'.",
            },
        ]
    )
    body = _build_review_body_with_findings(payload)

    findings = _extract_findings_from_review_comment(body)

    assert len(findings) == 2
    assert findings[0].severity == "critical"
    assert findings[0].target == "packages/foo/src/foo/missing.py"
    assert findings[0].issue.startswith("Spec references")
    assert findings[0].needed.startswith("Create the file")
    assert findings[1].severity == "important"
    assert findings[1].target == "Acceptance criteria bullet 3"


def test_extract_findings_empty_list_is_valid() -> None:
    """Reviewer always emits the block — empty list on clean outcomes →
    extractor must return ``[]`` (not error)."""
    body = _build_review_body_with_findings("[]")
    assert _extract_findings_from_review_comment(body) == []


def test_extract_findings_missing_markers_returns_empty_list() -> None:
    """Old-format review (no markers) must not crash — return ``[]``."""
    body = "needs_fix — please address these issues.\n\nSome prose only."
    assert _extract_findings_from_review_comment(body) == []


def test_extract_findings_only_begin_marker_returns_empty() -> None:
    body = "needs_fix.\n\n" + FINDINGS_BEGIN_MARKER + "\n```json\n[]\n```\n"
    assert _extract_findings_from_review_comment(body) == []


def test_extract_findings_malformed_json_returns_empty_list() -> None:
    """Markers present but the inner JSON is broken → ``[]``, never raise."""
    body = _build_review_body_with_findings("[{not valid json")
    assert _extract_findings_from_review_comment(body) == []


def test_extract_findings_json_object_not_list_returns_empty() -> None:
    """Wrong shape (object, not array) → ``[]`` with a warning."""
    body = _build_review_body_with_findings('{"severity": "critical"}')
    assert _extract_findings_from_review_comment(body) == []


def test_extract_findings_skips_invalid_entries_keeps_valid_ones() -> None:
    """Per-entry validation: bad entries are skipped, good ones survive."""
    payload = json.dumps(
        [
            {
                "severity": "critical",
                "target": "valid",
                "issue": "ok",
                "needed": "fix",
            },
            # Missing required field 'needed'
            {"severity": "important", "target": "broken", "issue": "missing-needed"},
            # Invalid severity literal
            {
                "severity": "blocker",
                "target": "bad-severity",
                "issue": "x",
                "needed": "y",
            },
        ]
    )
    body = _build_review_body_with_findings(payload)
    findings = _extract_findings_from_review_comment(body)
    assert len(findings) == 1
    assert findings[0].target == "valid"


def test_extract_findings_handles_empty_body() -> None:
    assert _extract_findings_from_review_comment("") == []


# ----------------------------------------------------------------------
# run_fixer — extracted findings reach the LLM user prompt
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_fixer_passes_extracted_findings_into_user_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end contract: when the Reviewer's posted review body carries
    the marker-fenced JSON block, the Fixer recovers the findings and feeds
    them into the user prompt (both the markdown and JSON renders). Without
    this, the Fixer's "every edit must trace to a structured finding" rule
    produces zero actions — the bug this fix exists to close.
    """
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)

    findings_payload = json.dumps(
        [
            {
                "severity": "critical",
                "target": "packages/foo/src/foo/missing.py",
                "issue": "Spec references a file that doesn't exist.",
                "needed": "Create the file or remove the reference.",
            }
        ]
    )
    enriched_review_body = _build_review_body_with_findings(findings_payload)

    repo, _pr, _issue = _make_fake_repo(
        issue_number=42,
        head_sha=head_sha,
        reviews=[_FakeReview(enriched_review_body)],
    )
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_fixed_output())

    await run_fixer(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    fake_provider.run_agent.assert_called_once()
    user_prompt = fake_provider.run_agent.call_args.kwargs["user_prompt"]

    # The finding's distinctive target string must appear in BOTH the
    # markdown render (reading order) AND the JSON render (authoritative
    # targeting) sections — confirms the extracted findings flowed through
    # to the prompt the LLM actually sees.
    assert "packages/foo/src/foo/missing.py" in user_prompt
    assert "Spec references a file that doesn't exist." in user_prompt
    assert "Create the file or remove the reference." in user_prompt
    # And the "_No findings carried forward._" sentinel from the empty-list
    # branch of ``_render_findings_markdown`` must NOT be present — that
    # would prove the old broken-contract behavior.
    assert "_No findings carried forward._" not in user_prompt


@pytest.mark.asyncio
async def test_run_fixer_embeds_project_instructions_in_user_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``.foreman/INSTRUCTIONS.md`` exists in the clone, the Fixer's
    LLM sees the instructions content (verbatim) under a project-specific
    instructions section header so project conventions reach the fix."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    foreman_dir = clone / ".foreman"
    foreman_dir.mkdir()
    instructions_text = (
        "# Foreman instructions for voice\n\n"
        "## Branch naming\nKeep `foreman/issue-<N>` unchanged.\n"
    )
    (foreman_dir / "INSTRUCTIONS.md").write_text(instructions_text, encoding="utf-8")

    cfg = _make_config(clone)
    repo, _pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_fixed_output())

    await run_fixer(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    user_prompt = fake_provider.run_agent.call_args.kwargs["user_prompt"]
    assert "## Project-specific instructions" in user_prompt
    assert "Keep `foreman/issue-<N>` unchanged." in user_prompt
    assert "# Foreman instructions for voice" in user_prompt


@pytest.mark.asyncio
async def test_run_fixer_omits_instructions_section_when_file_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No instructions file → no project instructions section header."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_fixed_output())

    await run_fixer(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    user_prompt = fake_provider.run_agent.call_args.kwargs["user_prompt"]
    assert "## Project-specific instructions" not in user_prompt


@pytest.mark.asyncio
async def test_run_fixer_uses_empty_findings_when_review_body_lacks_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the Reviewer's review body has no marker-fenced block (older
    runs, third-party reviews, etc.), the Fixer must not crash — it falls
    back to an empty findings list and the LLM relies on the prose. Same
    behavior as before this fix, just no longer the default path.
    """
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _pr, _issue = _make_fake_repo(
        issue_number=42,
        head_sha=head_sha,
        reviews=[_FakeReview("needs_fix — prose only, no structured block.")],
    )
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_fixed_output())

    await run_fixer(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    user_prompt = fake_provider.run_agent.call_args.kwargs["user_prompt"]
    # The "no findings carried forward" sentinel proves the fallback was
    # taken (empty list → markdown renderer's empty branch).
    assert "_No findings carried forward._" in user_prompt
    # The prose context still flows through so the LLM has something to
    # read even when structured findings are absent.
    assert "needs_fix — prose only, no structured block." in user_prompt


# ----------------------------------------------------------------------
# Per-target prompt + precondition routing — foreman#79
#
# The Fixer's ``target`` kwarg now drives both the entry-label
# precondition and the prompt loaded. Today's bug (foreman#79) was
# that ``target="impl_pr"`` got plumbed through DaemonRunners but
# the role function rejected the issue because ``foreman:impl-fix``
# was not in its hardcoded acceptance set (only ``foreman:spec-fix``).
# These tests pin both halves of the routing.
# ----------------------------------------------------------------------


def test_fixer_entry_label_by_target_mapping_is_complete() -> None:
    """The mapping covers the two valid targets and nothing else."""
    from foreman.roles.fixer import _FIXER_ENTRY_LABEL_BY_TARGET

    assert _FIXER_ENTRY_LABEL_BY_TARGET == {
        "spec_pr": "foreman:spec-fix",
        "impl_pr": "foreman:impl-fix",
    }


def test_fixer_superpowers_by_target_mapping_is_complete() -> None:
    """Each target gets its own superpowers composition list. The
    impl variant adds verification-before-completion and
    test-driven-development."""
    from foreman.roles.fixer import _FIXER_SUPERPOWERS_BY_TARGET

    assert _FIXER_SUPERPOWERS_BY_TARGET == {
        "spec_pr": ["receiving-code-review"],
        "impl_pr": [
            "receiving-code-review",
            "verification-before-completion",
            "test-driven-development",
        ],
    }


def test_load_fixer_prompt_default_uses_spec_composition() -> None:
    """Zero-arg call returns the spec composition — back-compat for
    existing call sites and tests."""
    from foreman.prompts import compose_role_prompt
    from foreman.roles.fixer import _load_fixer_prompt

    actual = _load_fixer_prompt()
    expected = compose_role_prompt(
        role="fixer",
        superpowers=["receiving-code-review"],
        target="spec_pr",
    )
    assert actual == expected


def test_load_fixer_prompt_impl_target_loads_impl_composition() -> None:
    """``target="impl_pr"`` loads ``fixer_impl.md`` with the impl
    superpowers list. Without this, the Fixer reads spec-fix content
    while trying to fix impl-PR code."""
    from foreman.prompts import compose_role_prompt
    from foreman.roles.fixer import _load_fixer_prompt

    actual = _load_fixer_prompt(target="impl_pr")
    expected = compose_role_prompt(
        role="fixer",
        superpowers=[
            "receiving-code-review",
            "verification-before-completion",
            "test-driven-development",
        ],
        target="impl_pr",
    )
    assert actual == expected
    # Sanity: ensure the impl composition contains impl-file content.
    assert "implementation pull request" in actual.lower() or "impl-pr variant" in actual.lower()


# ----------------------------------------------------------------------
# foreman#91 — final_labels is the authoritative post-transition set
# ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output_factory", "starting_labels", "expected_final"),
    [
        # fixed outcome (spec target): spec-fix removed, planning added
        # (v3: reconciler re-fires on planning + open spec PR),
        # fix-attempt-1 cleared (per-episode reset).
        (
            _fixed_output,
            ["foreman:spec-fix"],
            ["foreman:planning"],
        ),
        # incomplete: spec-fix kept, fix-attempt-1 retained, needs-help added.
        (
            _incomplete_output,
            ["foreman:spec-fix"],
            sorted(
                [
                    "foreman:spec-fix",
                    "foreman:fix-attempt-1",
                    "foreman:needs-help",
                ]
            ),
        ),
    ],
)
async def test_run_fixer_returns_authoritative_final_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_factory: Any,
    starting_labels: list[str],
    expected_final: list[str],
) -> None:
    """foreman#91: ``FixerRunResult.final_labels`` is the deterministic
    post-transition set, computed in-process from the role's known
    mutations. Not a host re-read."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _pr, _issue = _make_fake_repo(
        issue_number=42, head_sha=head_sha, labels=starting_labels
    )
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=output_factory())

    result = await run_fixer(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    assert result.final_labels == expected_final
