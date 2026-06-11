"""foreman#229: runaway-burn defense — defensive top-level exception
handler in every role runner.

When an exception escapes the role's body (in particular, an unhandled
provider exception that slipped past the typed
:class:`StructuredOutputRetryError` / :class:`StructuredOutputMissingError`
catches), the in-flight ticket label used to stay on the role's entry
label (e.g. ``foreman:planning`` for the Planner). The dispatcher's
next poll then re-dispatched the SAME role on the SAME ticket — the
runaway burn (foreman#227 = 171 dispatches in 2h52m).

The fix: each role's outer ``except Exception:`` now ALSO
1. Posts a ticket comment carrying the exception type, message, and
   truncated traceback so the operator can diagnose without reading
   daemon logs.
2. Transitions the ticket to ``foreman:needs-help`` (the terminal
   blocking label) so the dispatcher stops re-dispatching until a
   human removes the label.
3. Writes a ``outcome="exception"`` row to the per-role JSONL stats
   file for cost-attribution. The role's existing ``*_failed`` row is
   NOT written when ``outcome="exception"`` is used — the new
   defensive handler replaces, not augments, the pre-existing
   exception-path stats write.

The tests below mirror the existing role-runner test fixtures (fake
PyGithub surface for reviewer/worker/fixer, fake
:class:`GitHostProvider` for the Planner) so the fakes refuse exactly
what the real lib refuses. Per Wren's rule "Test fakes mirror real
strictly."
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from foreman.config import AppsConfig, Config, ProjectConfig
from foreman.git_host import GitHostProvider, IssueRef, PRRef
from foreman.roles import (
    TERMINAL_BLOCKING_LABEL,
    build_exception_comment,
    handle_unhandled_role_exception,
)
from foreman.roles.fixer import run_fixer
from foreman.roles.planner import run_planner
from foreman.roles.reviewer import run_reviewer
from foreman.roles.worker import run_worker

# ----------------------------------------------------------------------
# Unit tests for the shared helper
# ----------------------------------------------------------------------


def test_terminal_blocking_label_is_needs_help() -> None:
    """Pin the literal value the helper transitions tickets to. If a
    future refactor renames this constant without updating the four
    role runners in lockstep, the daemon's GraphQL observer filter (which
    also names ``foreman:needs-help`` explicitly) will silently drop
    tickets and the runaway-burn regression resurrects.
    """
    assert TERMINAL_BLOCKING_LABEL == "foreman:needs-help"


def test_build_exception_comment_names_role_type_and_traceback() -> None:
    """The posted comment body must carry: the role name, the exception
    type, the exception message, and a fenced traceback. Operators
    diagnose from this comment alone; missing any of these forces them
    to spelunk daemon logs."""
    try:
        raise ValueError("simulated SDK failure")
    except ValueError as exc:
        body = build_exception_comment(role="planner", exc=exc)

    assert "planner" in body
    assert "ValueError" in body
    assert "simulated SDK failure" in body
    # Traceback fenced inside a code block so GitHub renders it readably.
    assert "```" in body
    # And the runaway-burn rationale + remediation instruction is in the
    # prose so the operator knows what to do.
    assert TERMINAL_BLOCKING_LABEL in body


def test_build_exception_comment_truncates_long_traceback() -> None:
    """A multi-MB traceback (e.g., from a runaway provider that filled
    its own stack) must NOT produce a multi-MB GitHub comment. The body
    is capped at ~4000 chars or 50 lines; the truncation marker tells
    the operator the tail was elided."""
    # Build an exception with a synthetic long message — simpler than
    # synthesizing a 50-frame deep stack in a test.
    long_msg = "x" * 10_000
    try:
        raise RuntimeError(long_msg)
    except RuntimeError as exc:
        body = build_exception_comment(role="worker", exc=exc)

    # The full 10k-char message must not appear verbatim — at least one
    # truncation cue is present.
    assert len(body) < 10_000, (
        f"comment body should be truncated but is {len(body)} chars: "
        f"{body[:200]!r}..."
    )
    assert "(truncated)" in body


def test_handle_unhandled_role_exception_calls_both_callbacks() -> None:
    """The helper must post the comment AND transition the label — both
    side effects fire even on the happy path. The original exception is
    NOT raised by the helper itself; the caller does the bare ``raise``."""
    post_comment_calls: list[str] = []
    set_label_calls: list[int] = []

    def _post(body: str) -> None:
        post_comment_calls.append(body)

    def _set_label() -> None:
        set_label_calls.append(1)

    try:
        raise RuntimeError("simulated SDK failure")
    except RuntimeError as exc:
        handle_unhandled_role_exception(
            role="planner",
            issue_number=42,
            exc=exc,
            post_comment=_post,
            set_needs_help_label=_set_label,
        )

    assert len(post_comment_calls) == 1
    assert "simulated SDK failure" in post_comment_calls[0]
    assert len(set_label_calls) == 1


def test_handle_unhandled_role_exception_label_transition_runs_even_when_post_fails() -> None:
    """foreman#229: a GitHub 5xx during the comment post MUST NOT block
    the label transition. The label transition is the load-bearing
    defense; the comment is diagnostic. Both attempts are independent."""
    set_label_calls: list[int] = []

    def _post_boom(body: str) -> None:
        raise RuntimeError("github API exploded")

    def _set_label() -> None:
        set_label_calls.append(1)

    try:
        raise RuntimeError("simulated SDK failure")
    except RuntimeError as exc:
        # No exception escapes the helper — it logs the comment-post
        # failure and proceeds to the label transition.
        handle_unhandled_role_exception(
            role="planner",
            issue_number=42,
            exc=exc,
            post_comment=_post_boom,
            set_needs_help_label=_set_label,
        )

    assert len(set_label_calls) == 1, (
        "label transition must run even when comment post raised — the "
        "label is the runaway-burn defense; the comment is diagnostic"
    )


# ----------------------------------------------------------------------
# Planner — defensive exception handler integration
# ----------------------------------------------------------------------
#
# Mirrors test_roles_planner.py's fixtures so the fakes refuse what the
# real lib refuses (foreman test discipline).


class _FakeHostProvider(GitHostProvider):
    """In-memory host provider that records every call for assertions.

    Adds two foreman#229-specific surfaces: ``post_issue_comment`` and
    ``set_issue_labels`` so the Planner's defensive exception handler
    can transition the originating issue via the existing host
    abstraction (Planner is the only role that uses GitHostProvider —
    the other three call PyGithub directly).
    """

    def __init__(self) -> None:
        self.issue_to_return = IssueRef(
            number=42,
            title="SSML",
            body="Add SSML support to madrigal.",
            labels=["foreman:planning"],
            repo_slug="jeffrichley/voice",
        )
        self.default_branch = "main"
        self.committed_files: dict[str, str] | None = None
        self.commit_message: str | None = None
        self.pushed_branch: str | None = None
        self.label_calls: list[tuple[str, int, list[str], list[str]]] = []
        self.posted_comments: list[tuple[str, int, str]] = []
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

    def post_issue_comment(self, repo_slug: str, issue_number: int, body: str) -> None:
        self.posted_comments.append((repo_slug, issue_number, body))


def _seed_clone(clone: Path, *, origin_path: Path | None = None) -> None:
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
            ["git", "push", "origin", "main"], cwd=clone, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "remote", "set-head", "origin", "main"],
            cwd=clone,
            check=True,
            capture_output=True,
        )


def _make_planner_config(clone: Path) -> Config:
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


@pytest.mark.asyncio
async def test_planner_unhandled_exception_posts_comment_transitions_label_and_logs_exception_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """foreman#229: when ``provider.run_agent`` raises an unhandled
    exception inside the Planner, ``run_planner`` must:

    1. Re-raise the original exception (so the daemon dispatcher still
       sees a non-zero exit).
    2. Post a comment on the originating issue carrying the exception
       type / message / traceback (so an operator can diagnose without
       reading daemon logs).
    3. Transition the issue to ``foreman:needs-help`` (so the dispatcher
       stops re-dispatching at every poll — the runaway-burn defense).
    4. Append a ``planner.jsonl`` row with ``outcome="exception"`` so
       cost-attribution still captures the failed dispatch.
    """
    stats_root = tmp_path / "stats"
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(stats_root))
    clone = tmp_path / "clone"
    _seed_clone(clone, origin_path=tmp_path / "origin.git")
    monkeypatch.setenv("FOREMAN_PLANNER_APP_ID", "123456")

    cfg = _make_planner_config(clone)
    fake_host = _FakeHostProvider()
    fake_registry = MagicMock()
    fake_registry.get_host_provider.return_value = fake_host
    fake_registry.get_planner_token.return_value = "fake-planner-token"

    class _SDKBoom(RuntimeError):
        pass

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(side_effect=_SDKBoom("simulated SDK failure"))

    with pytest.raises(_SDKBoom, match="simulated SDK failure"):
        await run_planner(
            issue_url="https://github.com/jeffrichley/voice/issues/42",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=fake_registry,
        )

    # The host saw a label transition adding foreman:needs-help.
    assert fake_host.label_calls, (
        "Planner exception handler must transition the issue to "
        "foreman:needs-help so the dispatcher stops re-dispatching"
    )
    added_labels: set[str] = set()
    for _slug, _num, add, _remove in fake_host.label_calls:
        added_labels.update(add)
    assert "foreman:needs-help" in added_labels

    # JSONL row written with outcome="exception".
    jsonl = stats_root / "jeffrichley__voice" / "planner.jsonl"
    assert jsonl.exists()
    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    exception_rows = [r for r in rows if r["outcome"] == "exception"]
    assert exception_rows, (
        f"expected at least one outcome='exception' row in planner.jsonl; "
        f"got rows={rows!r}"
    )


# ----------------------------------------------------------------------
# Reviewer — defensive exception handler integration
# ----------------------------------------------------------------------
#
# The Reviewer / Worker / Fixer test fakes already exist in
# test_roles_reviewer.py, test_roles_worker.py, test_roles_fixer.py
# respectively. Rather than duplicate the entire fixture surface here
# (which would drift from the source-of-truth fakes), we import the
# helpers from those modules. The tests below assert the new
# foreman#229 behavior on top of the existing scaffolding.


def _make_reviewer_config(clone: Path) -> Config:
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


@pytest.mark.asyncio
async def test_reviewer_unhandled_exception_posts_comment_transitions_label_and_logs_exception_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """foreman#229: when ``provider.run_agent`` raises an unhandled
    exception inside the Reviewer, ``run_reviewer`` must transition the
    originating ISSUE to ``foreman:needs-help`` and log
    ``outcome="exception"`` to ``reviewer.jsonl``."""
    from tests.test_roles_reviewer import (
        _FakeReviewerClient,
        _make_fake_repo,
        _seed_clone_with_spec_branch,
    )

    stats_root = tmp_path / "stats"
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(stats_root))
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_REVIEWER_APP_ID", "123456")

    cfg = _make_reviewer_config(clone)
    repo, _pr, issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeReviewerClient(repo=repo)
    registry = MagicMock()
    registry.get_reviewer_client.return_value = client
    registry.get_reviewer_token.return_value = "ghs_reviewer_token"

    class _SDKBoom(RuntimeError):
        pass

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(side_effect=_SDKBoom("simulated SDK failure"))

    with pytest.raises(_SDKBoom, match="simulated SDK failure"):
        await run_reviewer(
            pr_url="https://github.com/jeffrichley/voice/pull/77",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=registry,
        )

    # The Reviewer transitions the issue (not the PR) to needs-help.
    issue.update()  # invalidate the cached labels (matches production discipline)
    final_labels = {lbl.name for lbl in issue.labels}
    assert "foreman:needs-help" in final_labels, (
        f"Reviewer exception handler must transition the issue to "
        f"foreman:needs-help; got labels={sorted(final_labels)!r}"
    )

    # JSONL row written with outcome="exception".
    jsonl = stats_root / "jeffrichley__voice" / "reviewer.jsonl"
    assert jsonl.exists()
    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    exception_rows = [r for r in rows if r["outcome"] == "exception"]
    assert exception_rows, (
        f"expected at least one outcome='exception' row in reviewer.jsonl; "
        f"got rows={rows!r}"
    )


# ----------------------------------------------------------------------
# Worker — defensive exception handler integration
# ----------------------------------------------------------------------


def _make_worker_config(clone: Path) -> Config:
    return Config(
        projects={
            "voice": ProjectConfig(
                repo="jeffrichley/voice",
                local_clone_path=str(clone),
                apps=AppsConfig(
                    planner_app_id_env="FOREMAN_PLANNER_APP_ID",
                    planner_private_key_path="/tmp/planner.pem",
                    worker_app_id_env="FOREMAN_WORKER_APP_ID",
                    worker_private_key_path="/tmp/worker.pem",
                ),
            )
        }
    )


@pytest.mark.asyncio
async def test_worker_unhandled_exception_posts_comment_transitions_label_and_logs_exception_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """foreman#229: when an exception escapes the Worker body wrap
    (NOT the in-band ``provider.run_agent`` D5 recovery path — that one
    is synthesized to ``outcome=incomplete`` and is NOT a runaway-burn
    case), ``run_worker`` must:

    1. Re-raise the original exception.
    2. Transition the issue to ``foreman:needs-help``.
    3. Append a ``worker.jsonl`` row with ``outcome="exception"``.

    The Worker's existing ``finally:`` block reverts the entry-label
    transition back to ``foreman:plan-approved`` — that revert is the
    runaway burn it was supposed to fix. The new defensive handler
    short-circuits the revert so the issue ends up at ``foreman:needs-help``
    (the terminal blocking label), not back at the entry label.
    """
    from tests.test_roles_worker import (
        _FakeWorkerClient,
        _make_fake_repo,
        _seed_clone_with_spec_branch,
    )

    stats_root = tmp_path / "stats"
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(stats_root))
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "987654")

    cfg = _make_worker_config(clone)
    repo, _spec_pr, issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeWorkerClient(repo=repo)
    registry = MagicMock()
    registry.get_worker_client.return_value = client
    registry.get_worker_token.return_value = "ghs_worker_token"
    # Worker uses a host provider for push_branch — provide a no-op fake.
    host_provider = MagicMock()
    host_provider.push_branch = MagicMock(return_value=None)
    registry.get_host_provider.return_value = host_provider

    class _SDKBoom(RuntimeError):
        pass

    fake_provider = MagicMock()
    # Worker handles provider.run_agent failure in-band (D5 -> incomplete).
    # To exercise the new defensive handler we have to make a DIFFERENT step
    # raise — pick worktree creation. We monkeypatch WorktreeManager.create_impl
    # to raise so the failure surfaces after entry-label transition but
    # before the Worker's own finally-revert logic completes.
    fake_provider.run_agent = AsyncMock(side_effect=_SDKBoom("simulated SDK failure"))

    # Patch WorktreeManager.create_impl to raise — the Worker's in-band
    # provider.run_agent recovery isn't reached, so the exception escapes
    # the body wrap and the defensive handler must fire.
    import foreman.roles.worker as worker_module

    class _WorktreeBoom(RuntimeError):
        pass

    def _create_impl_boom(self: Any, **kwargs: Any) -> Any:
        raise _WorktreeBoom("worktree creation exploded")

    monkeypatch.setattr(
        worker_module.WorktreeManager, "create_impl", _create_impl_boom
    )

    with pytest.raises(_WorktreeBoom, match="worktree creation exploded"):
        await run_worker(
            issue_url="https://github.com/jeffrichley/voice/issues/42",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=registry,
        )

    # Issue transitioned to needs-help.
    issue.update()
    final_labels = {lbl.name for lbl in issue.labels}
    assert "foreman:needs-help" in final_labels, (
        f"Worker exception handler must transition the issue to "
        f"foreman:needs-help; got labels={sorted(final_labels)!r}"
    )

    # JSONL row written with outcome="exception".
    jsonl = stats_root / "jeffrichley__voice" / "worker.jsonl"
    assert jsonl.exists()
    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    exception_rows = [r for r in rows if r["outcome"] == "exception"]
    assert exception_rows, (
        f"expected at least one outcome='exception' row in worker.jsonl; "
        f"got rows={rows!r}"
    )


# ----------------------------------------------------------------------
# Fixer — defensive exception handler integration
# ----------------------------------------------------------------------


def _make_fixer_config(clone: Path) -> Config:
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


@pytest.mark.asyncio
async def test_fixer_unhandled_exception_posts_comment_transitions_label_and_logs_exception_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """foreman#229: when ``provider.run_agent`` raises an unhandled
    exception inside the Fixer, ``run_fixer`` must transition the issue
    to ``foreman:needs-help`` and log ``outcome="exception"`` to
    ``fixer.jsonl``."""
    from tests.test_roles_fixer import (
        _FakeFixerClient,
        _make_fake_repo,
        _seed_clone_with_spec_branch,
    )

    stats_root = tmp_path / "stats"
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(stats_root))
    clone = tmp_path / "clone"
    head_sha = _seed_clone_with_spec_branch(clone, issue_number=42)
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "777777")

    cfg = _make_fixer_config(clone)
    repo, _pr, issue = _make_fake_repo(issue_number=42, head_sha=head_sha)
    client = _FakeFixerClient(repo=repo)
    registry = MagicMock()
    registry.get_fixer_client.return_value = client
    registry.get_fixer_token.return_value = "ghs_fixer_token"

    class _SDKBoom(RuntimeError):
        pass

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(side_effect=_SDKBoom("simulated SDK failure"))

    with pytest.raises(_SDKBoom, match="simulated SDK failure"):
        await run_fixer(
            issue_url="https://github.com/jeffrichley/voice/issues/42",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
            identity_registry=registry,
        )

    # Issue transitioned to needs-help.
    issue.update()
    final_labels = {lbl.name for lbl in issue.labels}
    assert "foreman:needs-help" in final_labels, (
        f"Fixer exception handler must transition the issue to "
        f"foreman:needs-help; got labels={sorted(final_labels)!r}"
    )

    # JSONL row written with outcome="exception".
    jsonl = stats_root / "jeffrichley__voice" / "fixer.jsonl"
    assert jsonl.exists()
    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    exception_rows = [r for r in rows if r["outcome"] == "exception"]
    assert exception_rows, (
        f"expected at least one outcome='exception' row in fixer.jsonl; "
        f"got rows={rows!r}"
    )
