"""Integration test for ``run_worker`` with a fake PyGithub client + fake
ProviderFacade + monkey-patched ``_run_check_command``.

Verifies orchestration wiring: issue URL parsing, entry-condition label
pre-flight, max-3-attempts gate, attempt-counter increment, worktree
create_impl (not create or attach), baseline preflight runs, post-Worker
verification runs, outcome-override-on-new-failures, label transitions
per outcome (all 3 outcomes), impl PR creation iff implemented,
spec_invalid posts to spec PR (not issue), stats JSONL emission, SDK
errors surfacing as incomplete (not crashing).

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
from foreman.roles.worker import (
    WORKER_ALLOWED_TOOLS,
    _count_impl_attempts,
    _resolve_check_command,
    parse_issue_url,
    run_worker,
)
from foreman.schemas.worker import (
    CommitMade,
    ImplementedSubRequest,
    SkippedSubRequest,
    WorkerOutput,
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
# _count_impl_attempts
# ----------------------------------------------------------------------


def test_count_impl_attempts_zero_with_no_labels() -> None:
    assert _count_impl_attempts(set()) == 0


def test_count_impl_attempts_zero_with_only_unrelated_labels() -> None:
    assert _count_impl_attempts({"foreman:spec-ready", "bug"}) == 0


def test_count_impl_attempts_returns_max_existing() -> None:
    assert _count_impl_attempts({"foreman:impl-attempt-1", "foreman:impl-attempt-2"}) == 2


def test_count_impl_attempts_skips_partial_matches() -> None:
    assert _count_impl_attempts({"foreman:impl-attempt-x", "foreman:impl-attempt"}) == 0


# ----------------------------------------------------------------------
# _resolve_check_command — D2 default
# ----------------------------------------------------------------------


def test_resolve_check_command_returns_just_check_when_none() -> None:
    assert _resolve_check_command(None) == "just check"


def test_resolve_check_command_returns_just_check_when_empty_string() -> None:
    """Empty string is treated as 'not configured', same as None — projects
    that explicitly want a different command set a non-empty value."""
    assert _resolve_check_command("") == "just check"


def test_resolve_check_command_uses_project_override() -> None:
    assert _resolve_check_command("make test") == "make test"


# ----------------------------------------------------------------------
# Tool surface
# ----------------------------------------------------------------------


def test_worker_allowed_tools_includes_edit_write_and_bash() -> None:
    """Worker LLM mutates code — Edit + Write are required. Bash for
    check_command + git ops from inside the worktree."""
    assert set(WORKER_ALLOWED_TOOLS) == {
        "Read",
        "Grep",
        "Glob",
        "Bash",
        "Edit",
        "Write",
    }


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


class _FakeImplPR:
    """Stand-in for the PR object returned by repo.create_pull(...)."""

    def __init__(self, *, number: int, html_url: str) -> None:
        self.number = number
        self.html_url = html_url


class _FakeSpecPR:
    """Stand-in for the spec PR the Worker may read + comment on."""

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
        self.issue_comments_posted: list[str] = []

    def create_issue_comment(self, body: str) -> None:
        self.issue_comments_posted.append(body)


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
        self.labels.append(_FakeLabel(label))


class _FakeRepo:
    def __init__(
        self,
        *,
        spec_pr: _FakeSpecPR | None,
        issue: _FakeIssue,
        impl_pr_to_return: _FakeImplPR | None = None,
    ) -> None:
        self._spec_pr = spec_pr
        self._issue = issue
        self._impl_pr_to_return = impl_pr_to_return or _FakeImplPR(
            number=101,
            html_url="https://github.com/jeffrichley/voice/pull/101",
        )
        self.full_name = "jeffrichley/voice"
        self.get_pulls_calls: list[dict[str, Any]] = []
        self.get_issue_calls: list[int] = []
        self.create_pull_calls: list[dict[str, Any]] = []

    def get_pulls(
        self, state: str | None = None, head: str | None = None, **kwargs: Any
    ) -> list[_FakeSpecPR]:
        self.get_pulls_calls.append({"state": state, "head": head, **kwargs})
        return [self._spec_pr] if self._spec_pr is not None else []

    def get_issue(self, number: int) -> _FakeIssue:
        self.get_issue_calls.append(number)
        return self._issue

    def create_pull(
        self, *, title: str, body: str, base: str, head: str, **kwargs: Any
    ) -> _FakeImplPR:
        self.create_pull_calls.append(
            {"title": title, "body": body, "base": base, "head": head, **kwargs}
        )
        return self._impl_pr_to_return


class _FakeWorkerClient:
    def __init__(self, *, repo: _FakeRepo) -> None:
        self._repo = repo
        self.get_repo_calls: list[str] = []

    def get_repo(self, slug: str) -> _FakeRepo:
        self.get_repo_calls.append(slug)
        return self._repo


# ----------------------------------------------------------------------
# Seed helpers — set up a clone + worktree the way Planner / Reviewer / Fixer would
# ----------------------------------------------------------------------


def _seed_clone_with_spec_branch(clone: Path, issue_number: int) -> str:
    """Init a minimal git repo with a Planner-style ``foreman/issue-N`` branch.

    Returns the spec branch HEAD SHA. Pushes ``foreman/issue-N`` to a bare
    origin so ``WorktreeManager.create_impl`` can resolve
    ``origin/foreman/issue-N``.
    """
    clone.mkdir()
    origin = clone.parent / "origin.git"
    origin.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", "-b", "main"],
        cwd=origin,
        check=True,
        capture_output=True,
    )
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
        ["git", "remote", "add", "origin", str(origin)],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "push", "origin", "main"], cwd=clone, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "set-head", "origin", "main"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    branch = f"foreman/issue-{issue_number}"
    subprocess.run(["git", "checkout", "-b", branch], cwd=clone, check=True, capture_output=True)
    spec_dir = clone / "docs" / "superpowers" / "specs"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / f"foreman-issue-{issue_number}-spec.md").write_text(
        f"# Spec for issue #{issue_number}\n\n## Sub-requests\n1. Add X.\n2. Add Y.\n"
    )
    subprocess.run(["git", "add", "."], cwd=clone, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "spec doc"], cwd=clone, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", branch], cwd=clone, check=True, capture_output=True)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "checkout", "main"], cwd=clone, check=True, capture_output=True)
    return head_sha


def _make_config(clone: Path, *, check_command: str | None = None) -> Config:
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
                    worker_app_id_env="FOREMAN_WORKER_APP_ID",
                    worker_private_key_path="/tmp/worker.pem",
                ),
                check_command=check_command,
            )
        }
    )


def _implemented_output() -> WorkerOutput:
    return WorkerOutput(
        outcome="implemented",
        work_comment="implemented all sub-requests; check passed.",
        pr_title="feat(foo): add X and Y per spec",
        pr_body=(
            "Implements #42.\n\n"
            "Spec: docs/superpowers/specs/foreman-issue-42-spec.md\n"
            "Spec PR: #77\n\n"
            "## What was implemented\n"
            "- Sub-request 1: added X.\n"
            "- Sub-request 2: added Y.\n"
        ),
        commits_made=[
            CommitMade(
                sha="a" * 40,
                summary="feat(foo): add X and Y per spec",
                files_changed=["packages/foo/src/foo/x.py"],
            ),
        ],
        implemented_sub_requests=[
            ImplementedSubRequest(
                spec_reference="Sub-request 1",
                summary="Added X function.",
                files_touched=["packages/foo/src/foo/x.py"],
                tests_added=["test_x_returns_y"],
            ),
            ImplementedSubRequest(
                spec_reference="Sub-request 2",
                summary="Added Y function.",
                files_touched=["packages/foo/src/foo/x.py"],
                tests_added=["test_y_returns_z"],
            ),
        ],
        skipped_sub_requests=[],
        did_check_pass=True,
        confidence="high",
    )


def _incomplete_output() -> WorkerOutput:
    return WorkerOutput(
        outcome="incomplete",
        work_comment="incomplete — check failed on test_y.",
        commits_made=[],
        implemented_sub_requests=[
            ImplementedSubRequest(
                spec_reference="Sub-request 1",
                summary="Added X function.",
                files_touched=["packages/foo/src/foo/x.py"],
                tests_added=[],
            ),
        ],
        skipped_sub_requests=[
            SkippedSubRequest(
                spec_reference="Sub-request 2",
                reason="spec_unclear",
                rationale="The spec did not name a concrete return type for Y.",
            ),
        ],
        did_check_pass=False,
        check_output_summary="test_y_returns_z failed",
        confidence="medium",
    )


def _spec_invalid_output() -> WorkerOutput:
    return WorkerOutput(
        outcome="spec_invalid",
        work_comment="spec_invalid — issue and spec contradict.",
        spec_invalid_reason=(
            "Issue line 7 says 'must use JSON' but the spec's sub-request 2 "
            "says 'must use XML'. The contradiction is unresolvable without "
            "human input."
        ),
        commits_made=[],
        implemented_sub_requests=[],
        skipped_sub_requests=[],
        did_check_pass=True,
        confidence="high",
    )


def _make_fake_repo(
    *,
    issue_number: int,
    head_sha: str,
    labels: list[str] | None = None,
    include_spec_pr: bool = True,
) -> tuple[_FakeRepo, _FakeSpecPR | None, _FakeIssue]:
    labels = labels if labels is not None else ["foreman:spec-ready"]
    spec_pr: _FakeSpecPR | None = None
    if include_spec_pr:
        spec_pr = _FakeSpecPR(
            number=77,
            title="spec: SSML",
            body=f"Adds SSML spec. Closes #{issue_number}.",
            head_ref=f"foreman/issue-{issue_number}",
            head_sha=head_sha,
            base_ref="main",
        )
    issue = _FakeIssue(number=issue_number, title="SSML", body="Add SSML support.", labels=labels)
    repo = _FakeRepo(spec_pr=spec_pr, issue=issue)
    return repo, spec_pr, issue


def _make_registry(client: _FakeWorkerClient, token: str = "ghs_worker_token") -> Any:
    reg = MagicMock()
    reg.get_worker_client.return_value = client
    reg.get_worker_token.return_value = token
    return reg


def _make_passing_check_command(
    monkeypatch: pytest.MonkeyPatch, *, baseline: set[str] | None = None
) -> dict[str, list[Any]]:
    """Patch ``_run_check_command`` to return zero failures both times.

    Returns a ``calls`` dict the test can inspect to confirm the
    orchestrator ran ``check_command`` twice (baseline + post-Worker).
    """
    calls: dict[str, list[Any]] = {"calls": []}
    baseline_set = baseline if baseline is not None else set()

    def fake(check_command: str, cwd: Path) -> tuple[int, set[str], str]:
        calls["calls"].append({"check_command": check_command, "cwd": cwd})
        return 0, set(baseline_set), ""

    monkeypatch.setattr("foreman.roles.worker._run_check_command", fake)
    return calls


def _make_check_command_pair(
    monkeypatch: pytest.MonkeyPatch,
    *,
    baseline: set[str],
    post: set[str],
    post_rc: int = 0,
) -> dict[str, list[Any]]:
    """Patch ``_run_check_command`` to return ``baseline`` on call 1 and
    ``post`` on call 2 — simulating the brief's D4 baseline + post pair."""
    calls: dict[str, list[Any]] = {"calls": []}
    rc_sequence = [0, post_rc]
    failures_sequence = [baseline, post]

    def fake(check_command: str, cwd: Path) -> tuple[int, set[str], str]:
        idx = len(calls["calls"])
        calls["calls"].append({"check_command": check_command, "cwd": cwd})
        if idx < len(rc_sequence):
            return rc_sequence[idx], set(failures_sequence[idx]), ""
        # Defensive: more calls than expected — return passing tail.
        return 0, set(), ""

    monkeypatch.setattr("foreman.roles.worker._run_check_command", fake)
    return calls


# ----------------------------------------------------------------------
# run_worker end-to-end — implemented outcome
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_worker_implemented_opens_impl_pr_and_advances_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, spec_pr, issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())
    check_calls = _make_passing_check_command(monkeypatch)

    result = await run_worker(
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

    # Pydantic-first contract
    assert call_kwargs["output_model"] is WorkerOutput
    assert "output_schema" not in call_kwargs

    # Env injected with worker GH_TOKEN; parent env merged in
    env = call_kwargs["env"]
    assert env is not None
    assert env["GH_TOKEN"] == "ghs_worker_token"

    # check_command ran twice — baseline + post (D4)
    assert len(check_calls["calls"]) == 2
    assert all(c["check_command"] == "just check" for c in check_calls["calls"])

    # Impl PR opened on the stacked base
    assert len(repo.create_pull_calls) == 1
    create_call = repo.create_pull_calls[0]
    assert create_call["base"] == "foreman/issue-42"
    assert create_call["head"] == "foreman/impl-42"
    assert create_call["title"] == "feat(foo): add X and Y per spec"

    # spec_invalid post NOT triggered
    assert spec_pr is not None
    assert spec_pr.issue_comments_posted == []

    # Label transitions: implementing → impl-review; per-episode reset
    assert "foreman:impl-attempt-1" in issue.added
    assert "foreman:implementing" in issue.added
    assert "foreman:impl-review" in issue.added
    assert "foreman:spec-ready" in issue.removed  # cleared at dispatch
    assert "foreman:implementing" in issue.removed  # cleared on implemented
    assert "foreman:impl-attempt-1" in issue.removed  # per-episode reset

    # WorkerRunResult populated
    assert result.attempt == 1
    assert result.llm_output.outcome == "implemented"
    assert result.pr_url == "https://github.com/jeffrichley/voice/pull/101"
    assert result.final_did_check_pass is True


# ----------------------------------------------------------------------
# run_worker end-to-end — incomplete outcome
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_worker_incomplete_keeps_implementing_adds_needs_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _spec_pr, issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_incomplete_output())
    # Post-Worker check fails (rc=1 + 1 new failure) — consistent with Worker's claim
    _make_check_command_pair(
        monkeypatch,
        baseline=set(),
        post={"tests/test_y.py::test_y_returns_z"},
        post_rc=1,
    )

    result = await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    # NO impl PR opened
    assert repo.create_pull_calls == []

    # Labels: needs-help added; failed NOT yet (only attempt 1)
    assert "foreman:impl-attempt-1" in issue.added
    assert "foreman:needs-help" in issue.added
    assert "foreman:failed" not in issue.added

    assert result.attempt == 1
    assert result.llm_output.outcome == "incomplete"
    assert result.pr_url is None
    assert result.final_did_check_pass is False


@pytest.mark.asyncio
async def test_run_worker_incomplete_at_attempt_3_also_adds_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the third (last) attempt, an incomplete outcome escalates to
    ``foreman:failed`` — same pattern the Fixer uses."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _spec_pr, issue = _make_fake_repo(
        issue_number=42,
        head_sha=head_sha,
        labels=[
            "foreman:spec-ready",
            "foreman:impl-attempt-1",
            "foreman:impl-attempt-2",
        ],
    )
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_incomplete_output())
    _make_check_command_pair(
        monkeypatch,
        baseline=set(),
        post={"tests/test_y.py::test_y_returns_z"},
        post_rc=1,
    )

    result = await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    assert "foreman:impl-attempt-3" in issue.added
    assert "foreman:needs-help" in issue.added
    assert "foreman:failed" in issue.added
    assert result.attempt == 3


# ----------------------------------------------------------------------
# run_worker end-to-end — spec_invalid outcome
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_worker_spec_invalid_posts_comment_on_spec_pr_not_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D6: spec_invalid_reason posts as a comment on the SPEC PR, NOT
    the originating issue."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, spec_pr, issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_spec_invalid_output())
    _make_passing_check_command(monkeypatch)

    result = await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    # No impl PR opened
    assert repo.create_pull_calls == []

    # spec_invalid_reason posted on the SPEC PR
    assert spec_pr is not None
    assert len(spec_pr.issue_comments_posted) == 1
    assert "contradict" in spec_pr.issue_comments_posted[0]

    # Labels: spec-ready removed (twice — once at dispatch, once on outcome),
    # spec-fix + needs-help added, implementing removed
    assert "foreman:spec-ready" in issue.removed
    assert "foreman:spec-fix" in issue.added
    assert "foreman:needs-help" in issue.added
    assert "foreman:implementing" in issue.removed

    assert result.llm_output.outcome == "spec_invalid"
    assert result.pr_url is None


# ----------------------------------------------------------------------
# D4: Post-Worker verification overrides implemented → incomplete on new failures
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_worker_new_failures_override_implemented_to_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the Worker claimed implemented but the orchestrator's post-Worker
    check_command found new failures (failures not in baseline), the
    orchestrator OVERRIDES outcome to incomplete and does NOT open the
    impl PR. This is the brief's D4 belt-and-suspenders ground truth."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _spec_pr, issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)

    # Worker LIES — claims implemented + did_check_pass=True
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())

    # Orchestrator sees new failures the Worker didn't disclose.
    _make_check_command_pair(
        monkeypatch,
        baseline={"tests/test_existing.py::test_unrelated"},
        post={
            "tests/test_existing.py::test_unrelated",  # baseline (Worker innocent)
            "tests/test_x.py::test_x_returns_y",  # NEW (Worker broke it)
        },
        post_rc=1,
    )

    result = await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    # NO impl PR opened — orchestrator's truth wins
    assert repo.create_pull_calls == []

    # final_did_check_pass reflects orchestrator's truth, not the Worker's lie
    assert result.final_did_check_pass is False

    # Worker's original llm_output.outcome was 'implemented' but
    # WorkerRunResult.pr_url is None — proving the orchestrator did not act
    # on the LLM's claim
    assert result.llm_output.outcome == "implemented"
    assert result.pr_url is None

    # Labels follow the OVERRIDDEN outcome (incomplete branch)
    assert "foreman:needs-help" in issue.added
    assert "foreman:impl-review" not in issue.added


@pytest.mark.asyncio
async def test_run_worker_implemented_with_only_baseline_failures_trusts_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If post-Worker check still has baseline failures but NO new failures,
    the Worker is innocent — outcome stays implemented and the impl PR opens."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _spec_pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())
    _make_check_command_pair(
        monkeypatch,
        baseline={"tests/test_existing.py::test_pre_existing"},
        post={"tests/test_existing.py::test_pre_existing"},
        post_rc=1,
    )

    result = await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    # Impl PR opens — Worker is innocent of the pre-existing failure
    assert len(repo.create_pull_calls) == 1
    assert result.pr_url == "https://github.com/jeffrichley/voice/pull/101"
    # post_rc != 0 but no NEW failures → orchestrator-verified pass = False.
    # But the override rule only flips implemented→incomplete on NEW failures;
    # the implemented outcome holds.
    assert result.llm_output.outcome == "implemented"


# ----------------------------------------------------------------------
# Attempt counter increments correctly
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_worker_attempt_counter_increments_with_existing_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two existing impl-attempt labels → new attempt is 3."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _spec_pr, issue = _make_fake_repo(
        issue_number=42,
        head_sha=head_sha,
        labels=[
            "foreman:spec-ready",
            "foreman:impl-attempt-1",
            "foreman:impl-attempt-2",
        ],
    )
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())
    _make_passing_check_command(monkeypatch)

    result = await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    assert result.attempt == 3
    assert "foreman:impl-attempt-3" in issue.added


# ----------------------------------------------------------------------
# Pre-flight gates
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_worker_missing_spec_ready_label_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _spec_pr, issue = _make_fake_repo(
        issue_number=42, head_sha=head_sha, labels=["random-label"]
    )
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())
    _make_passing_check_command(monkeypatch)

    with pytest.raises(
        RuntimeError,
        match=r"foreman:spec-ready.*foreman:implementing-ready",
    ):
        await run_worker(
            issue_url="https://github.com/jeffrichley/voice/issues/42",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=registry,
        )

    # No LLM, no PR, no label mutation
    fake_provider.run_agent.assert_not_called()
    assert repo.create_pull_calls == []
    assert issue.added == []
    assert issue.removed == []


@pytest.mark.asyncio
async def test_run_worker_accepts_implementing_ready_entry_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``foreman:implementing-ready`` is the daemon's post-spec-merge sentinel.
    When the orchestrator merges a spec PR via auto_merge_spec, it removes
    foreman:spec-ready and applies foreman:implementing-ready — so the
    Worker MUST accept implementing-ready as a valid entry condition,
    otherwise every daemon-driven impl attempt fails the pre-flight check
    (caught 2026-06-01 on foreman#30 dogfood)."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _spec_pr, issue = _make_fake_repo(
        issue_number=42,
        head_sha=head_sha,
        labels=["foreman:implementing-ready"],
    )
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())
    _make_passing_check_command(monkeypatch)

    await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    # LLM dispatched, attempt label stamped, in-flight label flipped on.
    fake_provider.run_agent.assert_called_once()
    assert "foreman:impl-attempt-1" in issue.added
    assert "foreman:implementing" in issue.added
    # Both possible entry labels are removed — idempotent on the absent one.
    assert "foreman:implementing-ready" in issue.removed


@pytest.mark.asyncio
async def test_run_worker_max_attempts_gate_raises_before_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 impl-attempt-* labels already present → attempt 4 would be next →
    refuse with clear RuntimeError BEFORE LLM dispatch."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _spec_pr, issue = _make_fake_repo(
        issue_number=42,
        head_sha=head_sha,
        labels=[
            "foreman:spec-ready",
            "foreman:impl-attempt-1",
            "foreman:impl-attempt-2",
            "foreman:impl-attempt-3",
        ],
    )
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())
    _make_passing_check_command(monkeypatch)

    with pytest.raises(RuntimeError, match="max 3 impl-attempts"):
        await run_worker(
            issue_url="https://github.com/jeffrichley/voice/issues/42",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=registry,
        )

    fake_provider.run_agent.assert_not_called()
    assert "foreman:impl-attempt-4" not in issue.added
    assert repo.create_pull_calls == []


@pytest.mark.asyncio
async def test_run_worker_honors_project_max_impl_attempts_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``ProjectConfig.max_impl_attempts`` overrides the default, the
    gate fires at the overridden value, not at the historical 3. Pinning
    this empirically because otherwise the configurable feature could
    silently regress to ``hardcoded 3`` and the default-only tests would
    never notice."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    cfg.projects["voice"] = cfg.projects["voice"].model_copy(
        update={"max_impl_attempts": 1}
    )
    repo, _spec_pr, issue = _make_fake_repo(
        issue_number=42,
        head_sha=head_sha,
        labels=["foreman:spec-ready", "foreman:impl-attempt-1"],
    )
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())
    _make_passing_check_command(monkeypatch)

    with pytest.raises(RuntimeError, match="max 1 impl-attempts"):
        await run_worker(
            issue_url="https://github.com/jeffrichley/voice/issues/42",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=registry,
        )

    fake_provider.run_agent.assert_not_called()
    assert "foreman:impl-attempt-2" not in issue.added
    assert repo.create_pull_calls == []


@pytest.mark.asyncio
async def test_run_worker_rejects_url_pointing_at_wrong_project(
    tmp_path: Path,
) -> None:
    clone = tmp_path / "clone"
    _seed_clone_with_spec_branch(clone, issue_number=42)
    cfg = _make_config(clone)
    repo, _spec_pr, _issue = _make_fake_repo(issue_number=42, head_sha="x" * 40)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())

    with pytest.raises(ValueError, match="does not match project"):
        await run_worker(
            issue_url="https://github.com/someone-else/other-repo/issues/1",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=registry,
        )


# ----------------------------------------------------------------------
# Worktree create_impl is used (not create or attach)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_worker_creates_impl_worktree_not_spec_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Worker must use ``create_impl`` (sibling impl-<N>/ worktree on
    foreman/impl-<N> branch), NOT ``create`` or ``attach``. Verified by
    inspecting the resulting worktree's branch + path."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _spec_pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())
    _make_passing_check_command(monkeypatch)

    await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    impl_wt = tmp_path / "worktrees" / "voice" / "impl-42"
    assert impl_wt.exists()
    # The spec-side worktree was NOT created — only impl
    spec_wt = tmp_path / "worktrees" / "voice" / "issue-42"
    assert not spec_wt.exists()

    current_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=impl_wt,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert current_branch == "foreman/impl-42"


# ----------------------------------------------------------------------
# Baseline preflight runs and is passed into user prompt
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_worker_baseline_preflight_runs_before_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The baseline preflight runs BEFORE the LLM is dispatched (so the
    list of failing tests can be passed into the user_prompt)."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _spec_pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())

    baseline_failures = {"tests/test_pre.py::test_already_failing"}
    _make_check_command_pair(
        monkeypatch,
        baseline=baseline_failures,
        post=baseline_failures,
        post_rc=1,
    )

    await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    user_prompt = fake_provider.run_agent.call_args.kwargs["user_prompt"]
    # Baseline failures must appear in the user prompt's "DO NOT FIX" section
    assert "Baseline failures" in user_prompt
    assert "tests/test_pre.py::test_already_failing" in user_prompt
    assert "DO NOT FIX" in user_prompt


@pytest.mark.asyncio
async def test_run_worker_clean_baseline_uses_none_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the baseline is clean, the prompt says so explicitly (rather
    than presenting a meaningless empty list)."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _spec_pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())
    _make_passing_check_command(monkeypatch)

    await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    user_prompt = fake_provider.run_agent.call_args.kwargs["user_prompt"]
    assert "None — the worktree's test suite is" in user_prompt


# ----------------------------------------------------------------------
# check_command D2 configurability
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_worker_uses_project_check_command_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``ProjectConfig.check_command`` is set, the Worker uses it (and
    surfaces it in the user prompt) rather than the default."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone, check_command="make test")
    repo, _spec_pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())
    calls = _make_passing_check_command(monkeypatch)

    await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    # Both baseline + post calls used the override
    assert all(c["check_command"] == "make test" for c in calls["calls"])

    # User prompt mentions the override
    user_prompt = fake_provider.run_agent.call_args.kwargs["user_prompt"]
    assert "make test" in user_prompt


# ----------------------------------------------------------------------
# Env injection — worker GH_TOKEN + parent env merged
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_worker_passes_env_with_worker_token_and_parent_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))
    monkeypatch.setenv("MY_SENTINEL_VAR", "sentinel-worker")

    cfg = _make_config(clone)
    repo, _spec_pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client, token="ghs_specific_worker_token")
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())
    _make_passing_check_command(monkeypatch)

    await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    env = fake_provider.run_agent.call_args.kwargs["env"]
    assert env["GH_TOKEN"] == "ghs_specific_worker_token"
    assert env["MY_SENTINEL_VAR"] == "sentinel-worker"


# ----------------------------------------------------------------------
# Stats JSONL line emission
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_worker_writes_stats_jsonl_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    stats_root = tmp_path / "stats"
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(stats_root))

    cfg = _make_config(clone)
    repo, _spec_pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())
    _make_passing_check_command(monkeypatch)

    await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    stats_file = stats_root / "jeffrichley__voice" / "worker.jsonl"
    assert stats_file.exists()
    line = stats_file.read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    assert payload["issue_number"] == 42
    assert payload["pr_number"] == 101
    assert payload["attempt"] == 1
    assert payload["outcome"] == "implemented"
    assert payload["implemented_count"] == 2
    assert payload["skipped_count"] == 0
    assert payload["did_check_pass"] is True
    assert payload["confidence"] == "high"
    assert payload["duration_seconds"] >= 0
    assert payload["baseline_failures_count"] == 0
    assert payload["new_failures_count"] == 0


@pytest.mark.asyncio
async def test_run_worker_stats_logs_overridden_outcome_not_worker_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stats line records the orchestrator-verified outcome, NOT the
    Worker's original lie. Audit log integrity depends on this."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    stats_root = tmp_path / "stats"
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(stats_root))

    cfg = _make_config(clone)
    repo, _spec_pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    # Worker lies (implemented + did_check_pass=True), orchestrator catches it
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())
    _make_check_command_pair(
        monkeypatch,
        baseline=set(),
        post={"tests/test_x.py::test_new_failure"},
        post_rc=1,
    )

    await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    stats_file = stats_root / "jeffrichley__voice" / "worker.jsonl"
    payload = json.loads(stats_file.read_text(encoding="utf-8").strip())
    # Truth, not Worker's claim
    assert payload["outcome"] == "incomplete"
    assert payload["did_check_pass"] is False
    assert payload["new_failures_count"] == 1
    # pr_number stays None because no PR was opened
    assert payload["pr_number"] is None


# ----------------------------------------------------------------------
# SDK errors surface as outcome=incomplete (D5)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_worker_embeds_project_instructions_in_user_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``.foreman/INSTRUCTIONS.md`` exists in the clone, the Worker's
    LLM sees the instructions content (verbatim) under a project-specific
    instructions section header so project conventions (PR title rules,
    code style, etc.) reach the implementation."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    foreman_dir = clone / ".foreman"
    foreman_dir.mkdir()
    instructions_text = (
        "# Foreman instructions for voice\n\n"
        "## PR title rules\nImpl PRs use `<type>(<scope>): ...`.\n"
    )
    (foreman_dir / "INSTRUCTIONS.md").write_text(instructions_text, encoding="utf-8")

    cfg = _make_config(clone)
    repo, _spec_pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())
    _make_passing_check_command(monkeypatch)

    await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    user_prompt = fake_provider.run_agent.call_args.kwargs["user_prompt"]
    assert "## Project-specific instructions" in user_prompt
    assert "Impl PRs use `<type>(<scope>): ...`." in user_prompt
    assert "# Foreman instructions for voice" in user_prompt


@pytest.mark.asyncio
async def test_run_worker_omits_instructions_section_when_file_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No instructions file → no project instructions section header."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _spec_pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())
    _make_passing_check_command(monkeypatch)

    await run_worker(
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
async def test_run_worker_sdk_error_surfaces_as_incomplete_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D5: when ``provider.run_agent`` raises (SDK timeout, network,
    validation), the orchestrator MUST NOT crash. The run completes with
    ``outcome: incomplete``, the error message lives in work_comment +
    check_output_summary, labels transition to needs-help, and the stats
    line is still written."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _spec_pr, issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(side_effect=TimeoutError("provider transport timed out"))
    _make_passing_check_command(monkeypatch)

    result = await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    assert result.llm_output.outcome == "incomplete"
    assert "TimeoutError" in result.llm_output.work_comment
    assert "provider transport timed out" in result.llm_output.work_comment
    # Incomplete branch: needs-help added, no impl PR opened
    assert "foreman:needs-help" in issue.added
    assert repo.create_pull_calls == []
