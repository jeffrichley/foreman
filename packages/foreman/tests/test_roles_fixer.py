"""Integration test for ``run_fixer`` with a fake PyGithub client + fake
ProviderFacade.

Verifies orchestration wiring: issue URL parsing, worktree attach (no
new branch), LLM dispatch with Pydantic-first contract + env-injection,
fix_comment posted as PR comment (NOT review), stats JSONL line
emission.

Under v4, the Fixer does not read or write labels —
``LabelObservabilityObserver`` owns every ``foreman:*`` write off
state-machine transitions; the v4 state machine's retry cap owns
attempt counting. This test file no longer asserts label state.

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

from foreman.provider import UsageInfo
from foreman.roles import fixer as _fixer_mod
from foreman.roles.fixer import (
    FIXER_ALLOWED_TOOLS,
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
from foreman.v4.config import (
    AppCredentials,
    AppsConfig,
    OrchestratorConfig,
    ProjectConfig,
    V4Config,
)


@pytest.fixture(autouse=True)
def _route_build_role_resources_through_fake_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route ``foreman.roles.fixer.build_role_resources`` through the
    fake registry's v3-shaped accessors instead of the v4 happy path
    (App-metadata HTTP fetch + Github(token) client construction).

    See ``test_roles_planner.py``'s identical fixture for the full
    rationale (Phase 8d.1 — port to V4IdentityRegistry).
    """

    def _fake_build(
        *,
        registry: Any,
        role: str,
        app_id: int,
        private_key_path: str,
    ) -> tuple[Any, str, Any]:
        client = getattr(registry, f"get_{role}_client")()
        token = getattr(registry, f"get_{role}_token")()
        host = registry.get_host_provider(role)
        return host, token, client

    monkeypatch.setattr(_fixer_mod, "build_role_resources", _fake_build)


def _with_usage(output: Any) -> tuple[Any, UsageInfo]:
    """Wrap a FixerOutput in ``(output, UsageInfo())`` for AsyncMock.

    foreman#227: provider returns a tuple now; mocks mirror that.
    """
    return output, UsageInfo()

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
    """Fake mirroring PyGithub's ``Issue`` caching semantics.

    PyGithub's ``Issue.labels`` is a property that fetches via
    ``_completeIfNotSet`` on FIRST access and caches the result; subsequent
    accesses return the same snapshot. ``issue.update()`` is the documented
    refresh primitive (conditional GET) that invalidates the cache so the
    next ``labels`` access re-fetches. ``set_labels`` mutates the remote
    state but does NOT invalidate the local cache.

    Without this mirroring, a plain mutable ``labels`` list would let the
    role's "re-read at WRITE site" code path appear to work in tests even
    though it silently uses a stale snapshot in production. Mirror the
    real lib's pickier surface here so the fakes refuse what the real lib
    would refuse.
    """

    def __init__(self, *, number: int, title: str, body: str, labels: list[str]) -> None:
        self.number = number
        self.title = title
        self.body = body
        # The "actual GitHub state" — tests mutate this to simulate
        # operator changes during the LLM call.
        self._remote_labels: list[str] = list(labels)
        # Lazy cache, mirrors PyGithub's ``_completeIfNotSet`` pattern.
        self._labels_cache: list[_FakeLabel] | None = None
        self.removed: list[str] = []
        self.added: list[str] = []
        # ``set_labels_calls`` records each atomic ``issue.set_labels(...)``
        # invocation so tests can assert on the atomicity primitive directly.
        # Each entry is the sorted label tuple passed to that call.
        self.set_labels_calls: list[tuple[str, ...]] = []
        # Audit trail for cache-busting assertions.
        self.update_calls: int = 0

    @property
    def labels(self) -> list[_FakeLabel]:
        # Mirrors PyGithub: cache populated on first access from "remote"
        # state, never auto-refreshed. ``update()`` is the only way to
        # invalidate.
        if self._labels_cache is None:
            self._labels_cache = [_FakeLabel(n) for n in self._remote_labels]
        return self._labels_cache

    def update(self) -> None:
        # PyGithub's ``update()`` issues a conditional GET and re-stores
        # attributes — effectively invalidating the cached label list.
        self._labels_cache = None
        self.update_calls += 1

    def remove_from_labels(self, label: str) -> None:
        self.removed.append(label)
        self._remote_labels = [n for n in self._remote_labels if n != label]

    def add_to_labels(self, label: str) -> None:
        self.added.append(label)
        if label not in self._remote_labels:
            self._remote_labels.append(label)

    def create_comment(self, body: str) -> None:
        # foreman#229: defensive exception-handler surface. PyGithub's
        # ``Issue.create_comment`` posts a comment on the issue; tests
        # that exercise the exception path read ``comments_posted``.
        if not hasattr(self, "comments_posted"):
            self.comments_posted = []  # type: ignore[attr-defined]
        self.comments_posted.append(body)

    def set_labels(self, *labels: str) -> None:
        # Production code (adversarial review MEDIUM #12) uses
        # ``set_labels`` for atomic transitions. To keep the existing
        # ``removed`` / ``added`` assertion shape valid for migrated
        # tests, derive both lists from the diff against the current
        # remote label set, then replace the remote state in one shot.
        #
        # Real PyGithub: ``set_labels`` PUTs the full list but does NOT
        # refresh the cached attribute. Mirror that — the cache is left
        # stale until the next ``update()``. Tests that read ``labels``
        # after ``set_labels`` must call ``update()`` first to see the
        # change (same discipline production code now follows).
        current = set(self._remote_labels)
        target = set(labels)
        for removed in sorted(current - target):
            self.removed.append(removed)
        for added in sorted(target - current):
            self.added.append(added)
        self._remote_labels = list(labels)
        self.set_labels_calls.append(tuple(labels))


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


def _make_config(clone: Path) -> V4Config:
    return V4Config(
        db_path="/tmp/v4.db",
        log_dir="/tmp/v4-logs",
        apps=AppsConfig(
            planner=AppCredentials(app_id=123456, private_key_path="/tmp/planner.pem"),
            reviewer=AppCredentials(app_id=123457, private_key_path="/tmp/reviewer.pem"),
            fixer=AppCredentials(app_id=123458, private_key_path="/tmp/fixer.pem"),
            worker=AppCredentials(app_id=123459, private_key_path="/tmp/worker.pem"),
        ),
        orchestrator=OrchestratorConfig(
            app_id=99999, private_key_path="/tmp/orch.pem",
        ),
        projects=[
            ProjectConfig(
                name="voice",
                repo="jeffrichley/voice",
                local_clone_path=str(clone),
            )
        ],
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
async def test_run_fixer_fixed_outcome_posts_comment_and_returns_output(
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
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_fixed_output()))

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

    # Under v4, the Fixer does NOT mutate labels —
    # ``LabelObservabilityObserver`` owns ``foreman:*`` writes off
    # state-machine transitions.
    assert issue.set_labels_calls == []
    assert issue.added == []
    assert issue.removed == []

    # Return type
    assert result.attempt == 1
    assert result.llm_output.outcome == "fixed"


@pytest.mark.asyncio
async def test_run_fixer_does_not_mutate_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under v4, the role no longer reads or writes ``foreman:*`` labels
    — ``LabelObservabilityObserver`` owns every label write off
    state-machine transitions. This test pins the "no label mutation"
    contract for BOTH success and failure outcomes so a regression that
    re-introduces a ``set_labels`` / ``add_to_labels`` call at the role
    boundary is caught immediately.
    """
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _pr, issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_fixed_output()))

    await run_fixer(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    assert issue.set_labels_calls == []
    assert issue.added == []
    assert issue.removed == []


# ----------------------------------------------------------------------
# run_fixer end-to-end — incomplete outcome
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_fixer_incomplete_outcome_posts_comment_and_returns_output(
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
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_incomplete_output()))

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

    # Under v4, the Fixer does NOT mutate labels.
    assert issue.set_labels_calls == []
    assert issue.added == []
    assert issue.removed == []

    assert result.attempt == 1
    assert result.llm_output.outcome == "incomplete"


# ----------------------------------------------------------------------
# Pre-flight gates
# ----------------------------------------------------------------------


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
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_fixed_output()))

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
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_fixed_output()))

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
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_fixed_output()))

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
    """Real precedence test: parent process has ``GH_TOKEN`` set to a
    "leaking daemon" value; the fixer-bot's token must win.

    Without setting parent ``GH_TOKEN``, the test would pass regardless
    of the production merge order — placebo per HIGH #9 adversarial
    review.
    """
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))
    monkeypatch.setenv("MY_SENTINEL_VAR", "sentinel-fixer")
    # HIGH #9: load-bearing — exercises the role-wins precedence.
    monkeypatch.setenv("GH_TOKEN", "ghs_PARENT_SHOULD_NOT_WIN_fixer")

    cfg = _make_config(clone)
    repo, _pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client, token="ghs_specific_fixer_token")
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_fixed_output()))

    await run_fixer(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    env = fake_provider.run_agent.call_args.kwargs["env"]
    # Role token wins over the daemon's inherited GH_TOKEN.
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
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_fixed_output()))

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
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(mixed_output))

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
async def test_run_fixer_locates_spec_pr_when_target_is_spec_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``target='spec_pr'`` (default) queries the spec branch
    ``foreman/issue-<N>`` head. Regression coverage so the
    target-aware lookup below doesn't accidentally rewire the
    default path."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_fixed_output()))

    await run_fixer(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
        target="spec_pr",
    )

    # PR lookup must have happened against the spec branch head.
    assert any(
        call["head"] == "jeffrichley:foreman/issue-42"
        for call in repo.get_pulls_calls
    ), (
        f"target='spec_pr' should query the spec branch "
        f"'jeffrichley:foreman/issue-42'; saw {repo.get_pulls_calls!r}"
    )


@pytest.mark.asyncio
async def test_run_fixer_locates_impl_pr_when_target_is_impl_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``target='impl_pr'`` queries the impl branch
    ``foreman/impl-<N>`` head. Before this fix, ``run_fixer``
    ignored ``target`` for the PR lookup and always used the spec
    branch — so the impl PR was never found and the role raised
    even when the impl PR existed."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    # The fake PR's head_ref reflects the impl branch — the locator
    # should query for it, find it, and proceed.
    impl_pr = _FakePR(
        number=77,
        title="impl: SSML",
        body="Implements SSML. Closes #42.",
        head_ref="foreman/impl-42",
        head_sha=head_sha,
        base_ref="foreman/issue-42",
    )
    issue = _FakeIssue(
        number=42,
        title="SSML",
        body="Add SSML support.",
        labels=["foreman:impl-fix"],
    )
    repo = _FakeRepo(pr=impl_pr, issue=issue)
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_fixed_output()))

    await run_fixer(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
        target="impl_pr",
    )

    # PR lookup must have happened against the impl branch head, NOT
    # the spec branch. This is the bug the algokit#21 dogfood
    # surfaced — the legacy ``run_fixer`` was hardcoded to
    # ``spec_branch(issue_number)`` regardless of ``target``.
    assert any(
        call["head"] == "jeffrichley:foreman/impl-42"
        for call in repo.get_pulls_calls
    ), (
        f"target='impl_pr' should query the impl branch "
        f"'jeffrichley:foreman/impl-42'; saw {repo.get_pulls_calls!r}"
    )
    assert not any(
        call["head"] == "jeffrichley:foreman/issue-42"
        for call in repo.get_pulls_calls
    ), (
        f"target='impl_pr' must NOT query the spec branch; "
        f"saw {repo.get_pulls_calls!r}"
    )


@pytest.mark.asyncio
async def test_run_fixer_raises_target_aware_error_when_impl_pr_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``target='impl_pr'`` and no open PR matches the impl
    branch, the raised ``RuntimeError`` must name the impl branch
    + impl-flavored wording — not the spec-flavored boilerplate."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)

    def _empty_pulls(**_: Any) -> list[_FakePR]:
        return []

    repo.get_pulls = _empty_pulls  # type: ignore[assignment]
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_fixed_output()))

    with pytest.raises(RuntimeError) as excinfo:
        await run_fixer(
            issue_url="https://github.com/jeffrichley/voice/issues/42",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=registry,
            target="impl_pr",
        )

    msg = str(excinfo.value)
    # Branch name in the error must be the impl branch.
    assert "foreman/impl-42" in msg, (
        f"impl-target error must name the impl branch; got {msg!r}"
    )
    assert "foreman/issue-42" not in msg, (
        f"impl-target error must NOT name the spec branch; got {msg!r}"
    )
    # Wording must reflect the impl side — the spec-flavored
    # "Planner-opened spec PR" string was load-bearing in the
    # original bug because Fixer surfaced spec-wording even when
    # invoked for the impl side.
    assert "spec PR" not in msg, (
        f"impl-target error must not say 'spec PR'; got {msg!r}"
    )
    assert "impl PR" in msg, (
        f"impl-target error must reference the impl PR; got {msg!r}"
    )


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
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_fixed_output()))

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
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_fixed_output()))

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
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_fixed_output()))

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
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_fixed_output()))

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
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_fixed_output()))

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
    fake_provider.run_agent = AsyncMock(return_value=_with_usage(_fixed_output()))

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
# Per-target prompt routing — foreman#79
#
# The Fixer's ``target`` kwarg drives the prompt loaded. Under v4 the
# role no longer reads labels for gating; the v4 state machine picks
# the target and the role just composes the right prompt for it.
# ----------------------------------------------------------------------


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
# foreman#239 — failure-path stats logging (mirrors foreman#235 for Planner)
#
# Before #239, ``log_fixer_run`` was only called on the success path —
# after ``pr.create_issue_comment``. If anything between
# ``provider.run_agent`` returning and the success log call raised
# (e.g., ``create_issue_comment`` crashed), the exception propagated up
# to the daemon and NO JSONL row was written for the failed Fixer run.
# Cost telemetry for failures vanished. #239 wraps the body in
# try/except so the failure path also writes a row tagged
# ``outcome="exception"`` with whatever partial ``usage`` was captured
# before the failure.
#
# Note: ``"incomplete"`` is the Fixer's SELF-REPORTED outcome (LLM said
# it didn't finish) — a different shape from an uncaught exception in
# the role runner. Keeping ``exception`` distinct lets cost-rollup
# queries answer "how many Fixer runs crashed vs how many the LLM
# gave up on?".
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_fixer_logs_exception_with_partial_usage_when_post_llm_step_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """foreman#239 + foreman#229: when a step AFTER
    ``provider.run_agent`` succeeds raises (here:
    ``pr.create_issue_comment``), ``run_fixer`` must:

    1. Re-raise the original exception unchanged so the daemon
       dispatcher's error handling stays in charge.
    2. Append a ``fixer.jsonl`` row with ``outcome="exception"`` and
       the ``input_tokens`` / ``output_tokens`` / ``total_cost_usd`` /
       ``duration_ms`` / ``num_turns`` from the successful prior
       ``provider.run_agent`` call. Cost telemetry for failed runs
       must survive even when the failure happens mid-run.
    """
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    stats_root = tmp_path / "stats"
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(stats_root))

    cfg = _make_config(clone)
    repo, pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)

    # Make ``create_issue_comment`` fail. Everything before it
    # (worktree.attach, provider.run_agent) succeeds — so ``usage`` from
    # the LLM call should be captured and reach the ``exception`` row.
    class _CommentBoom(RuntimeError):
        pass

    def _boom(body: str) -> None:
        raise _CommentBoom("github API exploded posting fix_comment")

    pr.create_issue_comment = _boom  # type: ignore[method-assign]

    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)

    # UsageInfo with non-zero token counts so we can prove the partial
    # capture actually carries the prior run_agent's values (vs the
    # safe-default zeros).
    partial_usage = UsageInfo(
        input_tokens=4321,
        output_tokens=765,
        total_cost_usd=0.034,
        duration_ms=8765,
        num_turns=5,
    )
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=(_fixed_output(), partial_usage))

    with pytest.raises(_CommentBoom, match="github API exploded"):
        await run_fixer(
            issue_url="https://github.com/jeffrichley/voice/issues/42",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=registry,
        )

    # JSONL row landed for the failed run.
    jsonl = stats_root / "jeffrichley__voice" / "fixer.jsonl"
    assert jsonl.exists(), (
        "exception run must still append a row to fixer.jsonl; #239 "
        "fixes the silent-disappearance bug where exceptions before the "
        "success-path log call skipped the write entirely"
    )
    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    assert len(rows) == 1, f"exactly one row expected, got {len(rows)}: {rows!r}"
    row = rows[0]

    # Outcome reflects the failed-mid-run state.
    assert row["outcome"] == "exception"
    assert row["role"] == "fixer"
    assert row["issue_number"] == 42
    # PR was resolved before the failure, so pr_number is the real PR.
    assert row["pr_number"] == 77
    # Attempt counter was stamped at entry — should reflect attempt 1.
    assert row["attempt"] == 1

    # Partial usage from the successful prior run_agent call survived.
    assert row["input_tokens"] == 4321
    assert row["output_tokens"] == 765
    assert row["total_cost_usd"] == 0.034
    assert row["duration_ms"] == 8765
    assert row["num_turns"] == 5
    # duration_seconds is non-negative wall-clock — we can't pin an exact
    # value but it must be present and a real float.
    assert isinstance(row["duration_seconds"], (int, float))
    assert row["duration_seconds"] >= 0.0

    # Safe defaults for role-specific fields (no FixerOutput consumed
    # because the failure landed before downstream histogram computation
    # was trusted).
    assert row["total_findings"] == 0
    assert row["addressed_count"] == 0
    assert row["unaddressed_count"] == 0
    assert row["unaddressed_by_reason"] == {}
    assert row["disagreed_count"] == 0
    assert row["confidence"] == "low"


@pytest.mark.asyncio
async def test_run_fixer_logs_exception_with_safe_defaults_when_run_agent_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """foreman#239 + foreman#229: when ``provider.run_agent`` raises
    before producing a ``UsageInfo``, the ``outcome="exception"`` row
    still lands — with the safe-default zeros for token / cost /
    duration fields (the spec's "fall back to safe defaults" branch).
    """
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")
    stats_root = tmp_path / "stats"
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(stats_root))

    cfg = _make_config(clone)
    repo, _pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeFixerClient(repo=repo)
    registry = _make_registry(client)

    class _RunAgentBoom(RuntimeError):
        pass

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(side_effect=_RunAgentBoom("LLM transport failed"))

    with pytest.raises(_RunAgentBoom, match="LLM transport failed"):
        await run_fixer(
            issue_url="https://github.com/jeffrichley/voice/issues/42",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=registry,
        )

    jsonl = stats_root / "jeffrichley__voice" / "fixer.jsonl"
    assert jsonl.exists()
    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "exception"
    assert row["role"] == "fixer"
    # Safe-default zeros (no UsageInfo captured because run_agent raised).
    assert row["input_tokens"] == 0
    assert row["output_tokens"] == 0
    assert row["total_cost_usd"] is None
    assert row["model_usage"] is None
    assert row["duration_ms"] == 0
    assert row["num_turns"] == 0
    # Role-specific safe defaults.
    assert row["total_findings"] == 0
    assert row["addressed_count"] == 0
    assert row["unaddressed_count"] == 0
    assert row["unaddressed_by_reason"] == {}
    assert row["disagreed_count"] == 0
    assert row["confidence"] == "low"
