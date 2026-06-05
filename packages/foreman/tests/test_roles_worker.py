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
from github.GithubException import GithubException

from foreman.config import AppsConfig, Config, ProjectConfig
from foreman.roles.worker import (
    WORKER_ALLOWED_TOOLS,
    _count_impl_attempts,
    _create_pull_with_base_fallback,
    _is_invalid_base_422,
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
    assert _count_impl_attempts({"foreman:plan-approved", "bug"}) == 0


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
# _is_invalid_base_422 + _create_pull_with_base_fallback (foreman#122)
# ----------------------------------------------------------------------


def _invalid_base_422_exception() -> GithubException:
    """Construct the exact GithubException shape PyGithub raises when
    GitHub rejects ``create_pull`` because the ``base`` ref is gone.
    Matches the live 422 payload caught on foreman#100."""
    return GithubException(
        status=422,
        data={
            "message": "Validation Failed",
            "errors": [
                {"resource": "PullRequest", "field": "base", "code": "invalid"}
            ],
            "documentation_url": "https://docs.github.com/rest/pulls/pulls#create-a-pull-request",
        },
        headers={},
    )


def test_is_invalid_base_422_matches_real_shape() -> None:
    assert _is_invalid_base_422(_invalid_base_422_exception()) is True


def test_is_invalid_base_422_rejects_non_422_status() -> None:
    exc = GithubException(status=500, data={"errors": []}, headers={})
    assert _is_invalid_base_422(exc) is False


def test_is_invalid_base_422_rejects_422_without_base_invalid() -> None:
    exc = GithubException(
        status=422,
        data={"errors": [{"resource": "PullRequest", "field": "head", "code": "invalid"}]},
        headers={},
    )
    assert _is_invalid_base_422(exc) is False


class _StubRepoForFallback:
    """Minimal stand-in for ``github.Repository.Repository`` exposing
    just what ``_create_pull_with_base_fallback`` touches:
    ``default_branch`` + ``create_pull(...)``. Records call args so the
    test can assert which ``base`` was used."""

    def __init__(
        self,
        *,
        default_branch: str = "main",
        first_call_exception: Exception | None = None,
    ) -> None:
        self.default_branch = default_branch
        self._first_call_exception = first_call_exception
        self.create_pull_calls: list[dict[str, Any]] = []

    def create_pull(self, *, title: str, body: str, base: str, head: str) -> Any:
        self.create_pull_calls.append(
            {"title": title, "body": body, "base": base, "head": head}
        )
        if len(self.create_pull_calls) == 1 and self._first_call_exception is not None:
            raise self._first_call_exception
        pr = MagicMock()
        pr.html_url = f"https://github.com/owner/repo/pull/{len(self.create_pull_calls)}"
        return pr


def test_create_pull_with_base_fallback_passes_through_on_success() -> None:
    """No exception path: a single create_pull call against the
    requested base; no fallback ever fires."""
    repo = _StubRepoForFallback(default_branch="main")
    pr = _create_pull_with_base_fallback(
        repo,  # type: ignore[arg-type]
        title="feat: x",
        body="body",
        base="foreman/issue-100",
        head="foreman/impl-100",
    )
    assert pr.html_url == "https://github.com/owner/repo/pull/1"
    assert repo.create_pull_calls == [
        {
            "title": "feat: x",
            "body": "body",
            "base": "foreman/issue-100",
            "head": "foreman/impl-100",
        }
    ]


def test_create_pull_with_base_fallback_retries_on_invalid_base_422() -> None:
    """foreman#122: when the spec branch was deleted on origin (e.g.,
    auto-delete after spec PR merge), GitHub rejects create_pull with
    422 base=invalid. The wrapper recovers by retrying against the
    repo's default branch instead of crashing the run."""
    repo = _StubRepoForFallback(
        default_branch="main",
        first_call_exception=_invalid_base_422_exception(),
    )
    pr = _create_pull_with_base_fallback(
        repo,  # type: ignore[arg-type]
        title="feat: x",
        body="body",
        base="foreman/issue-100",
        head="foreman/impl-100",
    )
    assert pr.html_url == "https://github.com/owner/repo/pull/2"
    assert len(repo.create_pull_calls) == 2
    assert repo.create_pull_calls[0]["base"] == "foreman/issue-100"
    assert repo.create_pull_calls[1]["base"] == "main"
    # Other args unchanged across the retry.
    assert repo.create_pull_calls[1]["head"] == "foreman/impl-100"
    assert repo.create_pull_calls[1]["title"] == "feat: x"


def test_create_pull_with_base_fallback_does_not_loop_when_base_is_default() -> None:
    """If ``base`` is already ``default_branch`` and we still get
    422 base=invalid, the wrapper re-raises rather than retrying with
    the same value. Whatever GitHub didn't like is not the
    deleted-spec-branch case the fallback is meant to recover."""
    repo = _StubRepoForFallback(
        default_branch="main",
        first_call_exception=_invalid_base_422_exception(),
    )
    with pytest.raises(GithubException):
        _create_pull_with_base_fallback(
            repo,  # type: ignore[arg-type]
            title="feat: x",
            body="body",
            base="main",
            head="foreman/impl-100",
        )
    assert len(repo.create_pull_calls) == 1


def test_create_pull_with_base_fallback_re_raises_unrelated_github_exception() -> None:
    """Non-422 GithubException (e.g., 500 server error) is not the
    deleted-spec-branch case — re-raise without a retry."""
    repo = _StubRepoForFallback(
        default_branch="main",
        first_call_exception=GithubException(
            status=500, data={"message": "server error"}, headers={}
        ),
    )
    with pytest.raises(GithubException):
        _create_pull_with_base_fallback(
            repo,  # type: ignore[arg-type]
            title="feat: x",
            body="body",
            base="foreman/issue-100",
            head="foreman/impl-100",
        )
    assert len(repo.create_pull_calls) == 1


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
    labels = labels if labels is not None else ["foreman:plan-approved"]
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
    The ``role_token`` kwarg is captured per-call so tests can verify
    the worker bot's token reaches the check_command subprocess (HIGH
    #10 — without this, ``gh`` / ``git`` invoked from check_command
    inherits the daemon's parent ``GH_TOKEN``).
    """
    calls: dict[str, list[Any]] = {"calls": []}
    baseline_set = baseline if baseline is not None else set()

    def fake(
        check_command: str, cwd: Path, role_token: str | None = None
    ) -> tuple[int, set[str], str]:
        calls["calls"].append(
            {"check_command": check_command, "cwd": cwd, "role_token": role_token}
        )
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
    ``post`` on call 2 — simulating the brief's D4 baseline + post pair.

    Mirrors the real signature including the new ``role_token`` kwarg
    (HIGH #10) so the fake refuses argument shapes the real wouldn't.
    """
    calls: dict[str, list[Any]] = {"calls": []}
    rc_sequence = [0, post_rc]
    failures_sequence = [baseline, post]

    def fake(
        check_command: str, cwd: Path, role_token: str | None = None
    ) -> tuple[int, set[str], str]:
        idx = len(calls["calls"])
        calls["calls"].append(
            {"check_command": check_command, "cwd": cwd, "role_token": role_token}
        )
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

    # v3 label transitions: plan-approved cleared at dispatch, impl-review
    # added on implemented; per-episode reset drops impl-attempt-1.
    # v3 has no in-flight ``foreman:implementing`` label.
    assert "foreman:impl-attempt-1" in issue.added
    assert "foreman:implementing" not in issue.added
    assert "foreman:impl-review" in issue.added
    assert "foreman:plan-approved" in issue.removed  # cleared at dispatch
    assert "foreman:impl-attempt-1" in issue.removed  # per-episode reset

    # WorkerRunResult populated
    assert result.attempt == 1
    assert result.llm_output.outcome == "implemented"
    assert result.pr_url == "https://github.com/jeffrichley/voice/pull/101"
    assert result.final_did_check_pass is True


@pytest.mark.asyncio
async def test_run_worker_uses_atomic_set_labels_for_dispatch_and_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adversarial review MEDIUM #12: every Worker label transition (the
    pre-LLM attempt-stamp + entry-clear, and the post-LLM outcome
    transition) must land via a single ``issue.set_labels(...)`` call so
    a subprocess crash mid-transition cannot leave the issue with no
    ``foreman:*`` entry/outcome label — which would silently drop it
    out of the v3 observer's GraphQL ``filterBy.labels`` filter.
    """
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _spec_pr, issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())
    _make_check_command_pair(monkeypatch, baseline=set(), post=set(), post_rc=0)

    await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    # Two atomic set_labels calls: one before LLM dispatch (attempt-stamp
    # + entry-clear), one after LLM (outcome transition). Both must
    # carry the FULL final label set — never a partial diff.
    assert len(issue.set_labels_calls) == 2

    # First call: ``foreman:plan-approved`` cleared, ``foreman:impl-attempt-1``
    # stamped, atomically.
    pre_dispatch = set(issue.set_labels_calls[0])
    assert "foreman:plan-approved" not in pre_dispatch
    assert "foreman:impl-attempt-1" in pre_dispatch

    # Second call: outcome transition — ``foreman:impl-review`` added,
    # per-episode reset drops ``foreman:impl-attempt-1``.
    post_dispatch = set(issue.set_labels_calls[1])
    assert "foreman:impl-review" in post_dispatch
    assert "foreman:impl-attempt-1" not in post_dispatch


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
            "foreman:plan-approved",
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

    # v3 labels: plan-approved removed at dispatch (and idempotently
    # re-removed on spec_invalid); spec-fix + needs-help added. No
    # in-flight ``implementing`` label in v3.
    assert "foreman:plan-approved" in issue.removed
    assert "foreman:spec-fix" in issue.added
    assert "foreman:needs-help" in issue.added
    assert "foreman:implementing" not in issue.added
    assert "foreman:implementing" not in issue.removed

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
            "foreman:plan-approved",
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
async def test_run_worker_missing_plan_approved_label_raises(
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

    with pytest.raises(RuntimeError, match=r"foreman:plan-approved"):
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
async def test_run_worker_accepts_plan_approved_entry_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``foreman:plan-approved`` is the v3 post-Reviewer-signoff entry
    label the reconciler sets to queue the Worker. The role MUST accept
    it as the sole valid entry condition (v2's ``foreman:spec-ready`` /
    ``foreman:implementing-ready`` labels were removed in v3)."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _spec_pr, issue = _make_fake_repo(
        issue_number=42,
        head_sha=head_sha,
        labels=["foreman:plan-approved"],
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

    # LLM dispatched, attempt label stamped; entry label cleared at dispatch.
    fake_provider.run_agent.assert_called_once()
    assert "foreman:impl-attempt-1" in issue.added
    # v3 has no in-flight ``implementing`` label.
    assert "foreman:implementing" not in issue.added
    assert "foreman:plan-approved" in issue.removed


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
            "foreman:plan-approved",
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
        labels=["foreman:plan-approved", "foreman:impl-attempt-1"],
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
    """Real precedence test: parent process has ``GH_TOKEN`` set to a
    "leaking daemon" value; the worker-bot's token must win.

    Without setting parent ``GH_TOKEN``, the test would pass regardless
    of the production merge order — placebo per HIGH #9 adversarial
    review.
    """
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))
    monkeypatch.setenv("MY_SENTINEL_VAR", "sentinel-worker")
    # HIGH #9: load-bearing — exercises the role-wins precedence.
    monkeypatch.setenv("GH_TOKEN", "ghs_PARENT_SHOULD_NOT_WIN_worker")

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
    # Role token wins over the daemon's inherited GH_TOKEN.
    assert env["GH_TOKEN"] == "ghs_specific_worker_token"
    assert env["MY_SENTINEL_VAR"] == "sentinel-worker"


@pytest.mark.asyncio
async def test_run_worker_check_command_receives_worker_token_not_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HIGH #10: ``_run_check_command`` must be invoked with the worker
    bot's ``role_token`` so any ``git`` / ``gh`` the check command runs
    authenticates as the worker, not the daemon.

    Before the fix, ``_run_check_command`` had no ``role_token`` param
    and the underlying ``filtered_subprocess_env()`` inherited whatever
    ``GH_TOKEN`` the parent had (the daemon's identity).
    """
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))
    monkeypatch.setenv("GH_TOKEN", "ghs_PARENT_LEAKING_DAEMON")

    cfg = _make_config(clone)
    repo, _spec_pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client, token="ghs_specific_worker_token")
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

    # Both baseline + post-Worker check_command invocations carried the
    # worker bot's token, not the leaking parent.
    assert len(calls["calls"]) == 2, "expected baseline + post-Worker check runs"
    for call in calls["calls"]:
        assert call["role_token"] == "ghs_specific_worker_token", (
            "check_command run did not receive the worker bot's role_token; "
            "would inherit daemon's GH_TOKEN at runtime"
        )


@pytest.mark.asyncio
async def test_run_worker_git_subprocess_uses_worker_token_not_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HIGH #10: every direct ``subprocess.run`` from the worker module
    must carry the worker bot's token in ``env``.

    Covers the ``_read_spec_doc_from_branch`` git-show fallback. We
    force the fallback path by removing the on-disk spec file from the
    impl worktree post-creation (via a Path.exists patch keyed on the
    spec-doc filename), then intercept the worker module's subprocess
    namespace and assert every git call's env carries the worker token
    rather than the leaking parent ``GH_TOKEN``.
    """
    import subprocess as _real_subprocess
    import types

    from foreman.roles import worker as worker_mod

    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))
    monkeypatch.setenv("GH_TOKEN", "ghs_PARENT_LEAKING_DAEMON")

    cfg = _make_config(clone)
    repo, _spec_pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client, token="ghs_specific_worker_token")
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())
    _make_passing_check_command(monkeypatch)

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

    # Substitute a stand-in module on the worker module so we only
    # intercept what the worker module itself calls (not
    # WorktreeManager's subprocess.run from the same process).
    fake_subprocess = types.SimpleNamespace(run=capturing_run)
    monkeypatch.setattr(worker_mod, "subprocess", fake_subprocess)

    # Force the git-show fallback in _read_spec_doc_from_branch by
    # making the on-disk spec-doc path report as non-existent. We patch
    # Path.exists in a targeted way: only False for paths ending in the
    # spec-doc filename, real Path.exists everywhere else.
    real_exists = Path.exists
    spec_filename = "foreman-issue-42-spec.md"

    def selective_exists(self: Path) -> bool:
        if self.name == spec_filename:
            return False
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", selective_exists)

    await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    git_calls = [
        c for c in captured if isinstance(c["cmd"], list) and c["cmd"] and c["cmd"][0] == "git"
    ]
    assert git_calls, "expected at least one git subprocess.run from the worker module"
    for c in git_calls:
        assert c["has_env"], f"git call missing env=: {c['cmd']}"
        assert c["env_gh_token"] == "ghs_specific_worker_token", (
            f"git call leaked parent GH_TOKEN: cmd={c['cmd']} "
            f"GH_TOKEN={c['env_gh_token']!r}"
        )


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


# ----------------------------------------------------------------------
# Issue #48 — impl PR opens against the base reported by create_impl
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_opens_impl_pr_with_base_from_create_impl_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``create_impl`` returns an :class:`ImplWorktreeResult` whose
    ``base_branch`` is the repo default (issue #48 fallback path — spec
    branch was deleted between Reviewer and Worker, spec doc landed on
    default), the Worker MUST open the impl PR with ``base=<default>``,
    not with a hard-coded ``foreman/issue-<N>``. Locks the Worker's
    contract on the new dataclass."""
    from foreman.worktree import ImplWorktreeResult

    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    # Pre-create a fake worktree directory with the spec doc so the
    # Worker's on-disk spec read finds something. The dir does NOT
    # have to be a real git worktree because we monkeypatch both
    # ``create_impl`` and ``_run_check_command``.
    fake_worktree = tmp_path / "fake-impl-wt"
    spec_dir = fake_worktree / "docs" / "superpowers" / "specs"
    spec_dir.mkdir(parents=True)
    (spec_dir / "foreman-issue-42-spec.md").write_text(
        "# Spec for issue #42\n\n## Sub-requests\n1. Add X.\n"
    )

    def fake_create_impl(
        self: Any,
        *,
        clone_path: Path,
        repo_slug: str,
        ticket_id: int,
        repo_url: str | None = None,
    ) -> ImplWorktreeResult:
        return ImplWorktreeResult(path=fake_worktree, base_branch="main")

    monkeypatch.setattr(
        "foreman.roles.worker.WorktreeManager.create_impl", fake_create_impl
    )

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

    assert len(repo.create_pull_calls) == 1
    create_call = repo.create_pull_calls[0]
    assert create_call["base"] == "main", (
        f"Worker must use wt_result.base_branch as the impl PR's base "
        f"(here: 'main' from the fallback path); got base={create_call['base']!r}"
    )
    assert create_call["head"] == "foreman/impl-42"


# ----------------------------------------------------------------------
# foreman#91 — final_labels is the authoritative post-transition set
# ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output_factory", "starting_labels", "expected_final", "check_setup"),
    [
        # implemented: plan-approved cleared at dispatch, impl-review added,
        # impl-attempt-1 dropped (per-episode reset), needs-help dropped.
        # v3: no in-flight ``implementing`` label.
        ("implemented", ["foreman:plan-approved"], ["foreman:impl-review"], "pass"),
        # incomplete: plan-approved cleared at dispatch; impl-attempt-1
        # + needs-help added. v3: no ``implementing`` label.
        (
            "incomplete",
            ["foreman:plan-approved"],
            sorted(
                [
                    "foreman:impl-attempt-1",
                    "foreman:needs-help",
                ]
            ),
            "fail",
        ),
        # spec_invalid: plan-approved cleared, spec-fix + needs-help added,
        # impl-attempt-1 retained (no per-episode reset on invalid).
        (
            "spec_invalid",
            ["foreman:plan-approved"],
            sorted(
                [
                    "foreman:impl-attempt-1",
                    "foreman:spec-fix",
                    "foreman:needs-help",
                ]
            ),
            "pass",
        ),
    ],
)
async def test_run_worker_returns_authoritative_final_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_factory: str,
    starting_labels: list[str],
    expected_final: list[str],
    check_setup: str,
) -> None:
    """foreman#91: ``WorkerRunResult.final_labels`` is the deterministic
    post-transition set, computed in-process from the role's known
    mutations. Not a host re-read."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _spec_pr, _issue = _make_fake_repo(
        issue_number=42, head_sha=head_sha, labels=starting_labels
    )
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)

    output_map = {
        "implemented": _implemented_output,
        "incomplete": _incomplete_output,
        "spec_invalid": _spec_invalid_output,
    }
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=output_map[output_factory]())

    if check_setup == "pass":
        _make_passing_check_command(monkeypatch)
    else:
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

    assert result.final_labels == expected_final


@pytest.mark.asyncio
async def test_run_worker_preserves_non_foreman_labels_added_during_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass 2 HIGH: operator-added labels (``priority:high``,
    ``needs:design``) added DURING the LLM call must survive BOTH the
    pre-LLM dispatch transition AND the post-LLM outcome transition.
    ``set_labels`` REPLACES the full label set on GitHub — any label
    not present in the call's argument list is dropped.

    The Worker has TWO set_labels sites (pre-LLM attempt-stamp +
    entry-clear, and post-LLM outcome transition); both must re-read
    labels at the WRITE site to minimize the race window.

    Simulates the race by mutating the fake issue's labels via a
    side_effect on ``run_agent`` (the LLM call) — labels added during
    the LLM call must appear in the FINAL (post-LLM) set_labels call.
    Also asserts that a foreman label NOT under the Worker's control
    (``foreman:hold``) survives.
    """
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _spec_pr, issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)

    async def _mutate_then_return(*args: Any, **kwargs: Any) -> WorkerOutput:
        # Operator adds two non-foreman labels + one foreman label NOT
        # under the Worker's control during the LLM call. All three
        # must be preserved by the post-LLM outcome transition.
        # Mutate ``_remote_labels`` (simulated GitHub state) — NOT the
        # cached ``labels`` property — so the role's ``issue.update()``
        # at the WRITE site re-reads the operator changes from "GitHub",
        # matching PyGithub's real caching shape.
        issue._remote_labels.append("priority:high")
        issue._remote_labels.append("team:backend")
        issue._remote_labels.append("foreman:hold")
        return _implemented_output()

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(side_effect=_mutate_then_return)
    _make_check_command_pair(monkeypatch, baseline=set(), post=set(), post_rc=0)

    await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    # Two set_labels calls: pre-LLM dispatch + post-LLM outcome.
    assert len(issue.set_labels_calls) == 2

    # Post-LLM outcome call carries the FINAL set after the operator's
    # window — this is the call that must preserve operator labels.
    final = set(issue.set_labels_calls[-1])
    # Worker's verdict: impl-review added, attempt counter cleared.
    assert "foreman:impl-review" in final
    assert "foreman:impl-attempt-1" not in final
    # Operator-added labels preserved (the bug this test guards).
    assert "priority:high" in final
    assert "team:backend" in final
    # Foreman label NOT under the role's control also preserved.
    assert "foreman:hold" in final


@pytest.mark.asyncio
async def test_run_worker_implemented_clears_stale_foreman_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass 2 MEDIUM: if an operator re-triggers a previously-failed ticket
    (e.g., by re-adding ``foreman:plan-approved`` to an issue that still
    carries ``foreman:failed`` from a prior exhausted-attempts episode) and
    the Worker succeeds this time, the stale ``foreman:failed`` label MUST
    be dropped by the per-episode reset — otherwise the dashboard keeps
    showing the issue as terminal-failed despite a clean implementation."""
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    # Issue carries the operator re-trigger label (plan-approved) AND the
    # stale failed marker from the prior episode. No prior impl-attempt
    # labels — the operator cleared them along with re-queueing.
    repo, _spec_pr, issue = _make_fake_repo(
        issue_number=42,
        head_sha=head_sha,
        labels=["foreman:plan-approved", "foreman:failed"],
    )
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())
    _make_check_command_pair(monkeypatch, baseline=set(), post=set(), post_rc=0)

    result = await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    # Final post-LLM set_labels call must not include the stale failed
    # marker — the per-episode reset on implemented drops it.
    final = set(issue.set_labels_calls[-1])
    assert "foreman:failed" not in final
    assert "foreman:impl-review" in final
    # And the returned final_labels matches the cleaned set.
    assert "foreman:failed" not in result.final_labels


@pytest.mark.asyncio
async def test_run_worker_calls_update_before_post_llm_write_site_label_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass 3 CRITICAL: Stage 4's "re-read labels at WRITE site" is
    illusory without ``issue.update()`` because PyGithub's
    ``Issue.labels`` is a cached property — the first access materializes
    the list, subsequent accesses return the same snapshot. Operator
    labels added during the minute-long LLM call would be silently
    dropped without an explicit cache refresh.

    The Worker has TWO write sites (pre-LLM attempt-stamp and post-LLM
    outcome). This test focuses on the post-LLM site (the high-stakes
    one — minutes of LLM time between the pre-flight read and the
    write). The operator's label arrives during the LLM call and must
    survive the outcome transition.
    """
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    cfg = _make_config(clone)
    repo, _spec_pr, issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)

    # Inject operator's label DURING the LLM call so it's in
    # ``_remote_labels`` by the time the role refreshes at the WRITE
    # site. If the role doesn't call ``issue.update()``, the cached
    # snapshot from the top of ``run_worker`` (which carried only
    # ``foreman:plan-approved``) is reused and the operator label is
    # silently dropped.
    async def _operator_adds_label_during_llm(*args: Any, **kwargs: Any) -> WorkerOutput:
        issue._remote_labels.append("priority:high")
        return _implemented_output()

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(side_effect=_operator_adds_label_during_llm)
    _make_check_command_pair(monkeypatch, baseline=set(), post=set(), post_rc=0)

    await run_worker(
        issue_url="https://github.com/jeffrichley/voice/issues/42",
        config=cfg,
        project_name="voice",
        worktrees_root=tmp_path / "worktrees",
        provider=fake_provider,
        identity_registry=registry,
    )

    # Assertion 1: ``update()`` was called at least twice — once before
    # each WRITE-site label read (pre-LLM dispatch + post-LLM outcome).
    # Without these calls, the cached snapshot would be reused and the
    # operator label would be silently dropped.
    assert issue.update_calls >= 2, (
        "Worker must call issue.update() before BOTH WRITE-site label reads "
        f"(pre-LLM dispatch + post-LLM outcome); observed {issue.update_calls}"
    )

    # Assertion 2: the operator's label survived the post-LLM
    # transition. Without ``issue.update()``, the cache would still hold
    # the pre-LLM snapshot and ``priority:high`` would be missing.
    assert len(issue.set_labels_calls) == 2
    final = set(issue.set_labels_calls[-1])
    assert "priority:high" in final
    # And the role's verdict still landed.
    assert "foreman:impl-review" in final
    assert "foreman:impl-attempt-1" not in final


# ----------------------------------------------------------------------
# Container first-run: Worker must pass repo_url so ensure_clone fires
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_worker_passes_repo_url_to_create_impl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the configured clone path is empty (container first boot,
    foreman-repos volume fresh), the Worker must pass ``repo_url`` to
    ``create_impl`` so that ``ensure_clone`` populates the clone before
    ``_resolve_default_branch`` runs subprocess in a missing directory.

    Symmetric with planner.py / fixer.py / reviewer.py, which all pass
    ``repo_url=f"https://github.com/{project.repo}.git"``. The bug we are
    locking against: the Worker was the only role that omitted the kwarg,
    so first-run Worker dispatch inside the container died with
    ``FileNotFoundError: '/foreman/repos/<project>'`` even after the
    ensure_clone plumbing existed.
    """
    from foreman.worktree import ImplWorktreeResult

    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    fake_worktree = tmp_path / "fake-impl-wt"
    spec_dir = fake_worktree / "docs" / "superpowers" / "specs"
    spec_dir.mkdir(parents=True)
    (spec_dir / "foreman-issue-42-spec.md").write_text(
        "# Spec for issue #42\n\n## Sub-requests\n1. Add X.\n"
    )

    captured_kwargs: dict[str, Any] = {}

    def fake_create_impl(
        self: Any,
        *,
        clone_path: Path,
        repo_slug: str,
        ticket_id: int,
        repo_url: str | None = None,
    ) -> ImplWorktreeResult:
        captured_kwargs["clone_path"] = clone_path
        captured_kwargs["repo_slug"] = repo_slug
        captured_kwargs["ticket_id"] = ticket_id
        captured_kwargs["repo_url"] = repo_url
        return ImplWorktreeResult(path=fake_worktree, base_branch="main")

    monkeypatch.setattr(
        "foreman.roles.worker.WorktreeManager.create_impl", fake_create_impl
    )

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

    assert captured_kwargs["repo_url"] == "https://github.com/jeffrichley/voice.git", (
        "Worker must pass repo_url=f'https://github.com/{project.repo}.git' "
        "to create_impl so ensure_clone can populate the clone on container "
        f"first boot; got repo_url={captured_kwargs.get('repo_url')!r}"
    )


# ----------------------------------------------------------------------
# Entry-label revert on Worker crash before outcome
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_worker_reverts_entry_labels_when_create_impl_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the Worker crashes after the entry-label transition has been
    written (``plan-approved`` cleared, ``impl-attempt-N`` added) but
    before the final outcome ``set_labels`` runs, the entry transition
    must be reverted so the v3 reconciler can re-dispatch via the
    normal ``_plan_approved_no_impl_pr`` rule.

    Without this, the issue is left with ``foreman:impl-attempt-N`` but
    without ``foreman:plan-approved`` — and no rule in the v3 catalog
    matches that state, so the ticket becomes a stuck zombie even though
    the execution log's ``count_completed`` is well below the safety
    cap.

    Symmetric verification: the original exception must still propagate
    so the parent v3_host can write the termination row via
    ``_track_subprocess_completion``.
    """
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "444444")
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "stats"))

    def crashing_create_impl(
        self: Any,
        *,
        clone_path: Path,
        repo_slug: str,
        ticket_id: int,
        repo_url: str | None = None,
    ) -> Any:
        raise FileNotFoundError(
            "[Errno 2] No such file or directory: "
            f"PosixPath('{clone_path}')"
        )

    monkeypatch.setattr(
        "foreman.roles.worker.WorktreeManager.create_impl", crashing_create_impl
    )

    cfg = _make_config(clone)
    repo, _spec_pr, _issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = _make_registry(client)
    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(return_value=_implemented_output())
    _make_passing_check_command(monkeypatch)

    issue = repo.get_issue(42)

    # Run and expect the original FileNotFoundError to propagate.
    with pytest.raises(FileNotFoundError):
        await run_worker(
            issue_url="https://github.com/jeffrichley/voice/issues/42",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=registry,
        )

    # Assertion 1: exactly two set_labels calls — entry transition, then
    # revert. The outcome set_labels at the end of run_worker never ran
    # (we crashed in create_impl before it).
    assert len(issue.set_labels_calls) == 2, (
        "Expected entry transition + revert (2 set_labels calls); got "
        f"{len(issue.set_labels_calls)}: {issue.set_labels_calls}"
    )

    # Assertion 2: entry transition removed plan-approved + added impl-attempt-1.
    entry_labels = set(issue.set_labels_calls[0])
    assert "foreman:impl-attempt-1" in entry_labels
    assert "foreman:plan-approved" not in entry_labels

    # Assertion 3: revert call restored plan-approved + dropped impl-attempt-1,
    # leaving the ticket in the exact state the reconciler can re-dispatch from.
    revert_labels = set(issue.set_labels_calls[1])
    assert "foreman:plan-approved" in revert_labels, (
        "Revert must restore foreman:plan-approved so the v3 reconciler's "
        f"_plan_approved_no_impl_pr rule can re-dispatch; got {revert_labels!r}"
    )
    assert "foreman:impl-attempt-1" not in revert_labels, (
        "Revert must drop the foreman:impl-attempt-N that the entry "
        f"transition just added; got {revert_labels!r}"
    )
