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
    _parse_review_branch,
    parse_pr_url,
    run_reviewer,
)
from foreman.schemas.reviewer import Finding, ReviewerOutput, ReviewerRunResult

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
# _parse_review_branch
# ----------------------------------------------------------------------


def test_parse_review_branch_parses_spec_branch() -> None:
    assert _parse_review_branch("foreman/issue-42") == (42, "spec_pr")


def test_parse_review_branch_parses_impl_branch() -> None:
    assert _parse_review_branch("foreman/impl-42") == (42, "impl_pr")


def test_parse_review_branch_rejects_unrelated_branch() -> None:
    with pytest.raises(ValueError, match="foreman/issue-<N>.*foreman/impl-<N>"):
        _parse_review_branch("feature/some-thing")


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
    def __init__(self, *, number: int, title: str, body: str, labels: list[str]) -> None:
        self.number = number
        self.title = title
        self.body = body
        self.labels = [_FakeLabel(label) for label in labels]
        self.removed: list[str] = []
        self.added: list[str] = []
        # ``set_labels_calls`` records each atomic ``issue.set_labels(...)``
        # invocation so tests can assert on the atomicity primitive directly.
        # Each entry is the sorted label tuple passed to that call.
        self.set_labels_calls: list[tuple[str, ...]] = []

    def remove_from_labels(self, label: str) -> None:
        self.removed.append(label)
        self.labels = [lbl for lbl in self.labels if lbl.name != label]

    def add_to_labels(self, label: str) -> None:
        self.added.append(label)
        if not any(lbl.name == label for lbl in self.labels):
            self.labels.append(_FakeLabel(label))

    def set_labels(self, *labels: str) -> None:
        # Production code (adversarial review MEDIUM #12) uses
        # ``set_labels`` for atomic transitions. To keep the existing
        # ``removed`` / ``added`` assertion shape valid for migrated
        # tests, derive both lists from the diff against the current
        # label set, then replace the label set in one shot.
        current = {lbl.name for lbl in self.labels}
        target = set(labels)
        for removed in sorted(current - target):
            self.removed.append(removed)
        for added in sorted(target - current):
            self.added.append(added)
        self.labels = [_FakeLabel(name) for name in labels]
        self.set_labels_calls.append(tuple(labels))


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
    # Return to main so the branch is purely an existing local ref for attach.
    subprocess.run(["git", "checkout", "main"], cwd=clone, check=True, capture_output=True)
    return head_sha


def _seed_clone_with_impl_branch(clone: Path, issue_number: int) -> str:
    """Init a minimal git repo with both a spec branch AND a Worker-style
    ``foreman/impl-<N>`` branch stacked on the spec branch.

    Returns the head SHA on the impl branch so callers can verify the
    attach picks it up. Leaves the clone checked out on ``main``.
    """
    _seed_clone_with_spec_branch(clone, issue_number)
    spec_branch = f"foreman/issue-{issue_number}"
    impl_branch = f"foreman/impl-{issue_number}"
    subprocess.run(
        ["git", "checkout", spec_branch], cwd=clone, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "checkout", "-b", impl_branch], cwd=clone, check=True, capture_output=True
    )
    src_dir = clone / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "foo.py").write_text("# impl stub\n")
    subprocess.run(["git", "add", "."], cwd=clone, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "impl: stub"], cwd=clone, check=True, capture_output=True
    )
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
    labels = labels if labels is not None else ["foreman:planning"]
    pr = _FakePR(
        number=77,
        title="spec: SSML",
        body="Adds SSML spec. Closes #" + str(issue_number) + ".",
        head_ref=f"foreman/issue-{issue_number}",
        head_sha=head_sha,
        base_ref="main",
    )
    issue = _FakeIssue(number=issue_number, title="SSML", body="Add SSML support.", labels=labels)
    repo = _FakeRepo(pr=pr, issue=issue)
    return repo, pr, issue


def _make_fake_impl_repo(
    *,
    issue_number: int,
    head_sha: str,
    labels: list[str] | None = None,
) -> tuple[_FakeRepo, _FakePR, _FakeIssue]:
    """``_make_fake_repo`` sibling for impl-PR review tests — PR head on
    ``foreman/impl-<N>``, base on the spec branch, default issue label
    is ``foreman:impl-review``."""
    labels = labels if labels is not None else ["foreman:impl-review"]
    pr = _FakePR(
        number=99,
        title="feat(ssml): implement SSML",
        body="Implements #" + str(issue_number) + ".",
        head_ref=f"foreman/impl-{issue_number}",
        head_sha=head_sha,
        base_ref=f"foreman/issue-{issue_number}",
    )
    issue = _FakeIssue(number=issue_number, title="SSML", body="Add SSML support.", labels=labels)
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
async def test_run_reviewer_clean_outcome_advances_to_plan_approved(
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

    # Issue label advanced: planning → plan-approved
    assert issue.removed == ["foreman:planning"]
    assert issue.added == ["foreman:plan-approved"]

    # PR's labels NOT touched (label transition is on the issue)
    # (The fake PR's `add_to_labels` / `remove_from_labels` don't exist;
    # this passes implicitly — leaving an assertion-by-omission note.)

    # ReviewerRunResult returned (foreman#91 wrapper); the LLM output is
    # accessed through ``.llm_output``.
    assert isinstance(result, ReviewerRunResult)
    assert isinstance(result.llm_output, ReviewerOutput)
    assert result.llm_output.outcome == "clean"
    assert result.llm_output.confidence == "high"
    # foreman#91: final_labels is the authoritative post-transition set,
    # computed in-process by the role.
    assert result.final_labels == ["foreman:plan-approved"]


@pytest.mark.asyncio
async def test_run_reviewer_uses_atomic_set_labels_for_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adversarial review MEDIUM #12: the Reviewer must apply the label
    transition (entry → outcome) via a single ``issue.set_labels(...)``
    call (atomic PUT /issues/{N}/labels), NOT sequential
    ``remove_from_labels`` + ``add_to_labels``. The two-step pattern leaves
    a crash window where the issue carries neither the entry label nor
    the outcome label — it then falls out of the v3 observer's GraphQL
    ``filterBy.labels`` filter and silently stalls.
    """
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_REVIEWER_APP_ID", "123456")

    cfg = _make_config(clone)
    # Pre-existing unrelated label on the issue — it must survive the
    # transition. The set_labels call needs the FULL final label set,
    # not just the delta.
    repo, _pr, issue = _make_fake_repo(
        issue_number=42, head_sha=head_sha, labels=["foreman:planning", "area/infra"]
    )
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

    # Exactly one atomic set_labels call carrying the FULL final set —
    # entry label dropped, outcome label added, unrelated label preserved.
    assert len(issue.set_labels_calls) == 1
    assert issue.set_labels_calls[0] == ("area/infra", "foreman:plan-approved")


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

    # Issue label advanced: planning → spec-fix
    assert issue.removed == ["foreman:planning"]
    assert issue.added == ["foreman:spec-fix"]

    assert result.llm_output.outcome == "needs_fix"
    assert len(result.llm_output.findings) == 1
    assert result.final_labels == ["foreman:spec-fix"]


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
async def test_run_reviewer_missing_planning_label_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-flight guard: source issue without ``foreman:planning`` is
    not advanced — we refuse to act on issues not queued by the Planner."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_REVIEWER_APP_ID", "123456")

    cfg = _make_config(clone)
    # Issue has a random unrelated label, NOT foreman:planning
    repo, pr, issue = _make_fake_repo(issue_number=42, head_sha=head_sha, labels=["random-label"])
    client = _FakeReviewerClient(repo=repo)
    registry = _make_registry(client)

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_make_clean_output())

    with pytest.raises(RuntimeError, match="foreman:planning"):
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
async def test_run_reviewer_embeds_project_instructions_in_user_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``.foreman/INSTRUCTIONS.md`` exists in the clone, the
    Reviewer's LLM sees the instructions content (verbatim) under a
    project-specific instructions section header so it can use the
    project's conventions while reviewing."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_REVIEWER_APP_ID", "123456")

    foreman_dir = clone / ".foreman"
    foreman_dir.mkdir()
    instructions_text = (
        "# Foreman instructions for voice\n\n"
        "## PR title rules\nUse `docs(spec): ...` for spec PRs.\n"
    )
    (foreman_dir / "INSTRUCTIONS.md").write_text(instructions_text, encoding="utf-8")

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

    user_prompt = fake_provider.run_agent.call_args.kwargs["user_prompt"]
    assert "## Project-specific instructions" in user_prompt
    assert "Use `docs(spec): ...` for spec PRs." in user_prompt
    assert "# Foreman instructions for voice" in user_prompt


@pytest.mark.asyncio
async def test_run_reviewer_omits_instructions_section_when_file_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No instructions file → no project instructions section header."""
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

    user_prompt = fake_provider.run_agent.call_args.kwargs["user_prompt"]
    assert "## Project-specific instructions" not in user_prompt


@pytest.mark.asyncio
async def test_run_reviewer_passes_env_with_reviewer_token_and_parent_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer-bot's installation token flows into the agent
    subprocess via ``env={**os.environ, "GH_TOKEN": reviewer_token}`` so any
    ``gh`` calls the LLM makes act as the reviewer bot, not the parent.

    Critically, this exercises the precedence path: the parent process
    has ``GH_TOKEN`` set to a "leaking daemon" value, and we assert the
    reviewer-bot's token wins. Without this parent-env setup, the test
    would pass whether the production code merged dicts in the safe
    order (parent first, role last) or the leaky order (role first,
    parent last). Per HIGH #9 adversarial review."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_REVIEWER_APP_ID", "123456")
    monkeypatch.setenv("MY_SENTINEL_VAR", "sentinel")
    # HIGH #9: load-bearing — without this, the test never exercises
    # the precedence path it claims to test.
    monkeypatch.setenv("GH_TOKEN", "ghs_PARENT_SHOULD_NOT_WIN_reviewer")

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
    # Role token wins over the daemon's inherited GH_TOKEN.
    assert env["GH_TOKEN"] == "ghs_specific_reviewer_token"
    assert env["MY_SENTINEL_VAR"] == "sentinel"  # parent env merged in


@pytest.mark.asyncio
async def test_run_reviewer_git_subprocess_uses_reviewer_token_not_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HIGH #10: git operations issued directly from the Reviewer
    module must use the reviewer bot's token, not whatever
    ``GH_TOKEN`` the daemon process inherited.

    Before the fix, ``_get_pr_diff`` ran ``git fetch`` + ``git diff``
    with no ``env=`` kwarg, so any credential helper inherited the
    parent process's ``GH_TOKEN``. On CI / GitHub Actions / dev shells
    that token is the daemon's identity, not the reviewer bot's —
    network round-trips for fetch attribute to the wrong actor.

    We intercept ``subprocess.run`` on the reviewer module via a
    namespace-substituted module attribute (so we capture ONLY the
    reviewer's direct calls, not WorktreeManager's calls inside the
    same process). Then we assert every captured git call's ``env``
    carries the reviewer-bot token.
    """
    import subprocess as _real_subprocess
    import types

    from foreman.roles import reviewer as reviewer_mod

    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_REVIEWER_APP_ID", "123456")
    # Parent process has a "leaking" GH_TOKEN — the daemon's identity.
    monkeypatch.setenv("GH_TOKEN", "ghs_PARENT_LEAKING_DAEMON")

    cfg = _make_config(clone)
    repo, _pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeReviewerClient(repo=repo)
    registry = _make_registry(client, token="ghs_specific_reviewer_token")

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_make_clean_output())

    captured: list[dict[str, Any]] = []

    def capturing_run(*args: Any, **kwargs: Any) -> Any:
        cmd = args[0] if args else kwargs.get("args")
        env = kwargs.get("env")
        captured.append(
            {
                "cmd": list(cmd) if isinstance(cmd, list) else cmd,
                "env_gh_token": (env or {}).get("GH_TOKEN"),
                "has_env": env is not None,
            }
        )
        return _real_subprocess.run(*args, **kwargs)

    # Substitute a stand-in module so we only intercept what the
    # reviewer module calls — not WorktreeManager's subprocess.run in
    # the same process. This is the targeted interception.
    fake_subprocess = types.SimpleNamespace(run=capturing_run)
    monkeypatch.setattr(reviewer_mod, "subprocess", fake_subprocess)

    await run_reviewer(
        pr_url="https://github.com/jeffrichley/voice/pull/77",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    # Every git invocation from the reviewer module must have been
    # called with an explicit env carrying the reviewer-bot's token.
    git_calls = [
        c for c in captured if isinstance(c["cmd"], list) and c["cmd"] and c["cmd"][0] == "git"
    ]
    assert git_calls, "expected at least one git subprocess.run call from the reviewer module"
    for c in git_calls:
        assert c["has_env"], f"git call missing env=: {c['cmd']}"
        assert c["env_gh_token"] == "ghs_specific_reviewer_token", (
            f"git call leaked parent GH_TOKEN: cmd={c['cmd']} "
            f"GH_TOKEN={c['env_gh_token']!r}"
        )


# ----------------------------------------------------------------------
# run_reviewer end-to-end — impl PR path
#
# Mirrors the spec-PR tests above but exercises the
# ``foreman/impl-<N>`` branch shape and the impl-side label triple
# (``foreman:impl-review`` → ``foreman:impl-approved`` /
# ``foreman:impl-fix``). The daemon's ``RUN_REVIEWER_IMPL`` dispatch
# routes here once the Worker has opened the impl PR.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_reviewer_impl_clean_outcome_advances_to_impl_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_impl_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_REVIEWER_APP_ID", "123456")

    cfg = _make_config(clone)
    repo, pr, issue = _make_fake_impl_repo(issue_number=42, head_sha=head_sha)
    client = _FakeReviewerClient(repo=repo)
    registry = _make_registry(client)

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_make_clean_output())

    result = await run_reviewer(
        pr_url="https://github.com/jeffrichley/voice/pull/99",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    # Worktree path is the impl sibling, not the spec one
    wt_path = tmp_path / "worktrees" / "voice" / "impl-42"
    assert wt_path.exists()
    current_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=wt_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert current_branch == "foreman/impl-42"

    # Review posted as COMMENT
    assert len(pr.reviews_posted) == 1
    body, event = pr.reviews_posted[0]
    assert event == "COMMENT"
    assert body.startswith("Clean — traced ACs 1-4 against the spec.")

    # Issue label advanced: impl-review → impl-approved
    assert issue.removed == ["foreman:impl-review"]
    assert issue.added == ["foreman:impl-approved"]

    assert isinstance(result, ReviewerRunResult)
    assert isinstance(result.llm_output, ReviewerOutput)
    assert result.llm_output.outcome == "clean"
    assert result.final_labels == ["foreman:impl-approved"]


@pytest.mark.asyncio
async def test_run_reviewer_impl_needs_fix_outcome_advances_to_impl_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_impl_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_REVIEWER_APP_ID", "123456")

    cfg = _make_config(clone)
    repo, pr, issue = _make_fake_impl_repo(issue_number=42, head_sha=head_sha)
    client = _FakeReviewerClient(repo=repo)
    registry = _make_registry(client)

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_make_needs_fix_output())

    result = await run_reviewer(
        pr_url="https://github.com/jeffrichley/voice/pull/99",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    assert len(pr.reviews_posted) == 1
    body, event = pr.reviews_posted[0]
    assert "needs_fix" in body
    assert event == "COMMENT"

    # Issue label advanced: impl-review → impl-fix
    assert issue.removed == ["foreman:impl-review"]
    assert issue.added == ["foreman:impl-fix"]

    assert result.llm_output.outcome == "needs_fix"
    assert result.final_labels == ["foreman:impl-fix"]


@pytest.mark.asyncio
async def test_run_reviewer_missing_impl_review_label_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-flight guard for impl-PR path: source issue without
    ``foreman:impl-review`` is not advanced — same defense-in-depth as the
    spec-PR path."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_impl_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_REVIEWER_APP_ID", "123456")

    cfg = _make_config(clone)
    # Issue has a random unrelated label, NOT foreman:impl-review
    repo, pr, issue = _make_fake_impl_repo(
        issue_number=42, head_sha=head_sha, labels=["random-label"]
    )
    client = _FakeReviewerClient(repo=repo)
    registry = _make_registry(client)

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_make_clean_output())

    with pytest.raises(RuntimeError, match="foreman:impl-review"):
        await run_reviewer(
            pr_url="https://github.com/jeffrichley/voice/pull/99",
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


# ----------------------------------------------------------------------
# Per-target prompt routing — foreman#78
#
# The Reviewer's ``target`` kwarg now drives both the entry-label
# precondition AND the prompt loaded. Today's bug (foreman#78) was
# that ``target="impl_pr"`` correctly drove label transitions but
# silently kept the spec prompt — so impl PRs got reviewed as if
# they were spec PRs. These tests pin both halves of the routing.
# ----------------------------------------------------------------------


def test_reviewer_entry_label_by_target_mapping_is_complete() -> None:
    """The mapping covers the two valid targets and nothing else.
    A future target string (e.g. ``"docs_pr"``) must require an
    explicit addition to the mapping — not silently fall back to
    spec behavior."""
    from foreman.roles.reviewer import _REVIEWER_ENTRY_LABEL_BY_TARGET

    assert _REVIEWER_ENTRY_LABEL_BY_TARGET == {
        "spec_pr": "foreman:planning",
        "impl_pr": "foreman:impl-review",
    }


def test_reviewer_superpowers_by_target_mapping_is_complete() -> None:
    """Each target gets its own superpowers composition list. The
    impl variant adds verification-before-completion and
    test-driven-development (Worker's discipline patterns the
    impl-PR Reviewer needs to enforce)."""
    from foreman.roles.reviewer import _REVIEWER_SUPERPOWERS_BY_TARGET

    assert _REVIEWER_SUPERPOWERS_BY_TARGET == {
        "spec_pr": ["requesting-code-review"],
        "impl_pr": [
            "requesting-code-review",
            "verification-before-completion",
            "test-driven-development",
        ],
    }


def test_load_reviewer_prompt_default_uses_spec_composition() -> None:
    """The legacy zero-arg call MUST keep working — many tests and
    the spec-side dispatcher rely on it. It returns the spec
    composition."""
    from foreman.prompts import compose_role_prompt
    from foreman.roles.reviewer import _load_reviewer_prompt

    actual = _load_reviewer_prompt()
    expected = compose_role_prompt(
        role="reviewer",
        superpowers=["requesting-code-review"],
        target="spec_pr",
    )
    assert actual == expected


def test_load_reviewer_prompt_impl_target_loads_impl_composition() -> None:
    """``target="impl_pr"`` loads ``reviewer_impl.md`` with the
    impl superpowers list. Without this, the bug fix is incomplete —
    the role function reads spec content while claiming to do impl
    review."""
    from foreman.prompts import compose_role_prompt
    from foreman.roles.reviewer import _load_reviewer_prompt

    actual = _load_reviewer_prompt(target="impl_pr")
    expected = compose_role_prompt(
        role="reviewer",
        superpowers=[
            "requesting-code-review",
            "verification-before-completion",
            "test-driven-development",
        ],
        target="impl_pr",
    )
    assert actual == expected
    # Sanity: ensure the impl composition contains impl-file content.
    # If the loader silently fell back to reviewer.md, this would fail.
    assert "impl pull request" in actual.lower() or "impl-pr variant" in actual.lower()



# ----------------------------------------------------------------------
# foreman#91 — final_labels is the authoritative post-transition set
#
# The Reviewer must return ``final_labels`` matching the in-process
# transition (``(initial_labels - {entry_label}) | {add_label}``), so
# ``DaemonRunners.run_reviewer`` can populate the worker's
# ``RoleResult.new_labels`` without a post-mutation host re-read that
# would race GitHub's eventual-consistency window.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed_with_impl", "labels", "outcome", "expected_final"),
    [
        # spec_pr + clean → planning removed, plan-approved added
        (False, ["foreman:planning"], "clean", ["foreman:plan-approved"]),
        # spec_pr + needs_fix → planning removed, spec-fix added
        (False, ["foreman:planning"], "needs_fix", ["foreman:spec-fix"]),
        # impl_pr + clean → impl-review removed, impl-approved added
        (True, ["foreman:impl-review"], "clean", ["foreman:impl-approved"]),
        # impl_pr + needs_fix → impl-review removed, impl-fix added
        (True, ["foreman:impl-review"], "needs_fix", ["foreman:impl-fix"]),
    ],
)
async def test_run_reviewer_returns_authoritative_final_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_with_impl: bool,
    labels: list[str],
    outcome: str,
    expected_final: list[str],
) -> None:
    """foreman#91: ``ReviewerRunResult.final_labels`` is the sorted list
    of ``(initial_issue_labels - {entry_label}) | {add_label}`` for each
    branch, computed in-process from the role's known transitions —
    not from a post-mutation host re-read."""
    clone = tmp_path / "clone"
    if seed_with_impl:
        head_sha = _seed_clone_with_impl_branch(clone, issue_number=42)
        pr_url = "https://github.com/jeffrichley/voice/pull/99"
    else:
        head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
        pr_url = "https://github.com/jeffrichley/voice/pull/77"
    monkeypatch.setenv("FOREMAN_REVIEWER_APP_ID", "123456")

    cfg = _make_config(clone)
    if seed_with_impl:
        repo, _pr, _issue = _make_fake_impl_repo(
            issue_number=42, head_sha=head_sha, labels=labels
        )
    else:
        repo, _pr, _issue = _make_fake_repo(
            issue_number=42, head_sha=head_sha, labels=labels
        )
    client = _FakeReviewerClient(repo=repo)
    registry = _make_registry(client)

    llm_output = (
        _make_clean_output() if outcome == "clean" else _make_needs_fix_output()
    )
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=llm_output)

    result = await run_reviewer(
        pr_url=pr_url,
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    assert isinstance(result, ReviewerRunResult)
    assert result.final_labels == expected_final
