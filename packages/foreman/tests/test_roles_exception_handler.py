"""foreman#229: runaway-burn defense — defensive top-level exception
handler in every role runner.

When an exception escapes the role's body (in particular, an unhandled
provider exception that slipped past the typed
:class:`StructuredOutputRetryError` / :class:`StructuredOutputMissingError`
catches), the role used to keep the ticket on its entry label and the
dispatcher's next poll re-dispatched the SAME role on the SAME ticket
— the runaway burn (foreman#227 = 171 dispatches in 2h52m).

Under v4 (Phase 8d.7), the role-side ``foreman:needs-help`` label
write was dropped. The v4 state machine transitions to ``NeedsHelp``
when the role subprocess reports failure, and
:class:`LabelObservabilityObserver` writes the v4-namespaced
``foreman:state-needs-help`` label. The role-side helper now only
1. Posts a ticket comment carrying the exception type, message, and
   truncated traceback so the operator can diagnose without reading
   daemon logs.
2. Writes a ``outcome="exception"`` row to the per-role JSONL stats
   file for cost-attribution.

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

from foreman.git_host import GitHostProvider, IssueRef, PRRef
from foreman.roles import (
    build_exception_comment,
    handle_unhandled_role_exception,
)
from foreman.roles import fixer as _fixer_mod
from foreman.roles import planner as _planner_mod
from foreman.roles import reviewer as _reviewer_mod
from foreman.roles import worker as _worker_mod
from foreman.roles.fixer import run_fixer
from foreman.roles.planner import run_planner
from foreman.roles.reviewer import run_reviewer
from foreman.roles.worker import run_worker
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
    """Route ``build_role_resources`` through the fake registry's
    v3-shaped accessors in all 4 role modules.

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

    for mod in (_planner_mod, _reviewer_mod, _fixer_mod, _worker_mod):
        monkeypatch.setattr(mod, "build_role_resources", _fake_build)

# ----------------------------------------------------------------------
# Unit tests for the shared helper
# ----------------------------------------------------------------------


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


def test_handle_unhandled_role_exception_posts_comment() -> None:
    """The helper must post the diagnostic comment. The original
    exception is NOT raised by the helper itself; the caller does the
    bare ``raise``. Phase 8d.7 dropped the role-side label transition
    callback — v4's :class:`LabelObservabilityObserver` writes
    ``foreman:state-needs-help`` after the state machine transitions
    to ``NeedsHelp``."""
    post_comment_calls: list[str] = []

    def _post(body: str) -> None:
        post_comment_calls.append(body)

    try:
        raise RuntimeError("simulated SDK failure")
    except RuntimeError as exc:
        handle_unhandled_role_exception(
            role="planner",
            issue_number=42,
            exc=exc,
            post_comment=_post,
        )

    assert len(post_comment_calls) == 1
    assert "simulated SDK failure" in post_comment_calls[0]


def test_handle_unhandled_role_exception_swallows_post_comment_failure() -> None:
    """A GitHub 5xx during the comment post must not propagate out of
    the helper — the caller's ``raise`` still surfaces the original
    exception to the dispatcher, which is what drives v4's NeedsHelp
    transition. The comment is diagnostic; losing it doesn't lose the
    defense."""

    def _post_boom(body: str) -> None:
        raise RuntimeError("github API exploded")

    try:
        raise RuntimeError("simulated SDK failure")
    except RuntimeError as exc:
        # No exception escapes the helper.
        handle_unhandled_role_exception(
            role="planner",
            issue_number=42,
            exc=exc,
            post_comment=_post_boom,
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


def _make_v4_config(clone: Path) -> V4Config:
    """Shared V4Config fixture for the role exception-handler tests.

    All four roles read the same shape (apps + projects). Phase 8d.2
    collapsed the per-role variants of this builder into one since the
    v4 schema requires all four App credential slots — no per-role
    omission is valid.
    """
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


def _make_planner_config(clone: Path) -> V4Config:
    return _make_v4_config(clone)


@pytest.mark.asyncio
async def test_planner_unhandled_exception_posts_comment_transitions_label_and_logs_exception_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """foreman#229: when ``provider.run_agent`` raises an unhandled
    exception inside the Planner, ``run_planner`` must:

    1. Re-raise the original exception (so the daemon dispatcher still
       sees a non-zero exit; under v4 the role subprocess dies and the
       :class:`SubprocessRoleDispatcher` reports failure, which drives
       the state machine to ``NeedsHelp`` — that's the runaway-burn
       defense now, not a role-side label write).
    2. Post a comment on the originating issue carrying the exception
       type / message / traceback (so an operator can diagnose without
       reading daemon logs).
    3. Append a ``planner.jsonl`` row with ``outcome="exception"`` so
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

    # The host saw the diagnostic comment posted on the originating issue.
    assert fake_host.posted_comments, (
        "Planner exception handler must post a diagnostic comment on "
        "the originating issue so an operator can read the traceback "
        "without spelunking daemon logs"
    )

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


def _make_reviewer_config(clone: Path) -> V4Config:
    return _make_v4_config(clone)


@pytest.mark.asyncio
async def test_reviewer_unhandled_exception_posts_comment_transitions_label_and_logs_exception_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """foreman#229: when ``provider.run_agent`` raises an unhandled
    exception inside the Reviewer, ``run_reviewer`` must re-raise (the
    subprocess dies; v4 routes to ``NeedsHelp``) and log
    ``outcome="exception"`` to ``reviewer.jsonl``. Phase 8d.7 dropped
    the role-side ``foreman:needs-help`` write — the observer owns the
    v4-namespaced label now."""
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


def _make_worker_config(clone: Path) -> V4Config:
    return _make_v4_config(clone)


@pytest.mark.asyncio
async def test_worker_unhandled_exception_posts_comment_transitions_label_and_logs_exception_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """foreman#229: when an exception escapes the Worker body wrap
    (NOT the in-band ``provider.run_agent`` D5 recovery path — that one
    is synthesized to ``outcome=incomplete`` and is NOT a runaway-burn
    case), ``run_worker`` must:

    1. Re-raise the original exception (under v4 the subprocess dies
       and the state machine transitions to ``NeedsHelp``).
    2. Append a ``worker.jsonl`` row with ``outcome="exception"``.

    Phase 8d.7 dropped the role-side ``foreman:needs-help`` label
    write; :class:`LabelObservabilityObserver` writes
    ``foreman:state-needs-help`` after the NeedsHelp transition.
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


def _make_fixer_config(clone: Path) -> V4Config:
    return _make_v4_config(clone)


@pytest.mark.asyncio
async def test_fixer_unhandled_exception_posts_comment_transitions_label_and_logs_exception_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """foreman#229: when ``provider.run_agent`` raises an unhandled
    exception inside the Fixer, ``run_fixer`` must re-raise (the
    subprocess dies; v4 routes to ``NeedsHelp``) and log
    ``outcome="exception"`` to ``fixer.jsonl``. Phase 8d.7 dropped the
    role-side ``foreman:needs-help`` write — the observer owns the
    v4-namespaced label now."""
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

    # JSONL row written with outcome="exception".
    jsonl = stats_root / "jeffrichley__voice" / "fixer.jsonl"
    assert jsonl.exists()
    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    exception_rows = [r for r in rows if r["outcome"] == "exception"]
    assert exception_rows, (
        f"expected at least one outcome='exception' row in fixer.jsonl; "
        f"got rows={rows!r}"
    )
