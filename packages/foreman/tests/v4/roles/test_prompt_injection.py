"""v4 end-to-end coverage of the foreman#328 comment+label injection path.

This file is the v4 successor to the v3-shape end-to-end assertions that
lived in ``tests/test_roles_planner.py`` / ``tests/test_roles_reviewer.py``
/ ``tests/test_roles_fixer.py`` before the v4 substrate cutover (PR #333)
deleted the v3 orchestration entry points. Those tests pinned PR #331's
spec-side prompt-injection feature (foreman#328) end-to-end through
``run_planner`` / ``run_reviewer`` / ``run_fixer``; on v4 the equivalent
boundary is ``_run_<role>_core`` so the assertions live here.

Three spec-side integration tests prove the prompt picks up issue
comments + labels (Planner) or comments alone (Reviewer-on-spec,
Fixer-on-spec). Three impl-side regression tests prove the impl-side
roles (Reviewer-on-impl, Fixer-on-impl, Worker) never reach the
comment-fetch path — the v4 successor to the call-counter assertions
PR #331 used in ``test_roles_worker.py`` on main before #333 displaced
them.

References:

- Issue: foreman#335 (this ticket)
- Feature: foreman#328 (PR #331 added the spec-side injection)
- Substrate cutover: PR #333 (renamed ``run_<role>`` to
  ``_run_<role>_core`` and replaced the orchestration entry point with
  the v4 state machine)
- Helper unit coverage: ``tests/test_roles_prompt_helpers.py`` still
  covers ``format_comments_section`` / ``format_labels_section`` /
  ``filter_bot_self_comments`` in isolation
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from foreman.git_host import CommentRef, IssueRef, PRRef
from foreman.provider import UsageInfo
from foreman.roles.fixer import _run_fixer_core
from foreman.roles.planner import _run_planner_core
from foreman.roles.reviewer import _run_reviewer_core
from foreman.roles.worker import _run_worker_core
from foreman.schemas.fixer import FixerOutput
from foreman.schemas.planner import PlannerOutput
from foreman.schemas.reviewer import ReviewerOutput
from foreman.schemas.worker import CommitMade, WorkerOutput
from foreman.v4.config import (
    AppCredentials,
    AppsConfig,
    OperatorConfig,
    OperatorIdentity,
    OrchestratorConfig,
    ProjectConfig,
    StorageConfig,
    V4Config,
)
from foreman.worktree import ImplWorktreeResult

# ---------------------------------------------------------------------
# Shared scaffolding — copied (per spec sub-request 2) from
# ``tests/v4/roles/test_worker_core.py::_build_v4_config``. Inlining
# keeps the test file self-contained; the worker file's helper is the
# canonical reference if the V4Config shape changes.
# ---------------------------------------------------------------------


def _build_v4_config(*, project_repo: str, local_clone_path: str) -> V4Config:
    """Build a minimal V4Config with one project + four App identities.

    The App credentials are placeholders — the tests patch
    ``build_role_resources`` so the registry / fetch_app_metadata path
    is never exercised, but the V4Config validator still requires every
    field to be present and well-typed.
    """
    placeholder_app = AppCredentials(app_id=1, private_key_path="/dev/null")
    return V4Config(
        storage=StorageConfig(engine="postgres", dsn="postgresql://test/test"),
        log_dir="/tmp/foreman-test-logs",
        apps=AppsConfig(
            planner=placeholder_app,
            reviewer=placeholder_app,
            fixer=placeholder_app,
            worker=placeholder_app,
        ),
        orchestrator=OrchestratorConfig(app_id=1, private_key_path="/dev/null"),
        operator=OperatorConfig(
            supervisor=OperatorIdentity(name="Test Supervisor", email="sup@example.com"),
            signer=OperatorIdentity(name="Test Signer", email="sign@example.com"),
        ),
        projects=[
            ProjectConfig(
                name="p",
                repo=project_repo,
                local_clone_path=local_clone_path,
            ),
        ],
    )


# Canonical set of foreman role-bot logins ``identity_registry.get_role_bot_logins()``
# returns in production. The Planner test's seeded bot comment uses
# ``foreman-planner[bot]`` so ``filter_bot_self_comments`` has a match
# to drop.
_ALL_ROLE_BOT_LOGINS = {
    "foreman-planner[bot]",
    "foreman-reviewer[bot]",
    "foreman-fixer[bot]",
    "foreman-worker[bot]",
}


def _seed_comment_refs(
    entries: list[tuple[str, datetime, str]],
) -> list[CommentRef]:
    """Build a list of ``CommentRef`` objects for the Planner code path.

    Planner reads comments through ``host.get_issue_comments`` (a
    GitHostProvider abstraction) which already returns the
    :class:`~foreman.git_host.CommentRef` wire shape.
    """
    return [
        CommentRef(author_login=login, posted_at=ts, body=body) for (login, ts, body) in entries
    ]


def _seed_fake_issue_comments(
    entries: list[tuple[str, datetime, str]],
) -> list[SimpleNamespace]:
    """Build a list of PyGithub-shaped issue comments.

    Reviewer + Fixer iterate ``issue.get_comments()`` directly on the
    PyGithub-returned Issue object (``reviewer.py:551`` /
    ``fixer.py:624``), then translate each one into a ``CommentRef``
    using ``c.user.login`` / ``c.created_at`` / ``c.body``. The
    :class:`SimpleNamespace` shape mirrors PyGithub's
    ``IssueComment`` access surface — nested ``user.login`` attribute
    and flat ``created_at`` / ``body``.
    """
    return [
        SimpleNamespace(
            user=SimpleNamespace(login=login),
            created_at=ts,
            body=body,
        )
        for (login, ts, body) in entries
    ]


# ---------------------------------------------------------------------
# Planner — _run_planner_core injection tests
# ---------------------------------------------------------------------


def _build_planner_output() -> PlannerOutput:
    """Synthetic Planner LLM output. The Planner's PR-open path needs
    a ``spec_doc_content`` / ``pr_title`` / ``pr_body`` / ``summary``
    for the commit + PR open call. Confidence stays at ``high`` so the
    Planner's downstream confidence-based NEEDS_HELP path is irrelevant
    to this test.
    """
    return PlannerOutput(
        spec_doc_content="# Spec\nseed content\n",
        pr_title="docs(spec): seed",
        pr_body="seed body",
        summary="seed summary",
        considered_alternatives=[],
        confidence="high",
    )


def _build_planner_pr_ref() -> PRRef:
    """Fake ``PRRef`` for ``host.open_pull_request`` return.

    Planner constructs a ``PlannerRunResult(pr=...)`` from this object,
    and Pydantic enforces the dataclass shape — a MagicMock fails
    validation. Use the real frozen dataclass.
    """
    return PRRef(
        number=1234,
        url="https://github.com/testowner/myrepo/pull/1234",
        title="docs(spec): seed",
        body="seed body",
        branch="foreman/issue-335",
        base_branch="main",
        repo_slug="testowner/myrepo",
    )


@pytest.mark.asyncio
async def test_planner_injects_filtered_comments_and_labels(
    tmp_path: Path,
) -> None:
    """foreman#335 / PR #331 e2e pin (Planner-side): the issue's comment
    + label streams reach the user prompt through ``_run_planner_core``,
    with foreman role-bot self-comments filtered out.

    Seeds two comments — one bot self-comment that must be filtered,
    one human comment that must reach the prompt — plus two labels that
    must render in the ``## Labels`` block. Captures the
    ``user_prompt`` kwarg from the patched ``provider.run_agent`` and
    asserts on the rendered markdown.

    Mirrors the v3-shape integration that lived in
    ``tests/test_roles_planner.py`` before PR #333 displaced it. The v3
    shape drove through ``run_planner``; the v4 boundary is
    ``_run_planner_core``.
    """
    cfg = _build_v4_config(
        project_repo="testowner/myrepo",
        local_clone_path=str(tmp_path / "clone"),
    )
    Path(tmp_path / "clone").mkdir(parents=True, exist_ok=True)
    worktrees_root = tmp_path / "worktrees"
    wt_path = worktrees_root / "myrepo" / "issue-335"
    wt_path.mkdir(parents=True, exist_ok=True)

    issue_ref = IssueRef(
        number=335,
        title="test: v4 state-machine coverage",
        body="See PR #331.",
        labels=["foreman:state-planning", "priority:high"],
        repo_slug="testowner/myrepo",
    )
    mock_host = MagicMock()
    mock_host.get_issue.return_value = issue_ref
    mock_host.get_default_branch.return_value = "main"
    mock_host.get_issue_comments.return_value = _seed_comment_refs(
        [
            (
                "foreman-planner[bot]",
                datetime(2026, 6, 17, 20, 0, tzinfo=UTC),
                "bot self-comment that must be filtered",
            ),
            (
                "alice",
                datetime(2026, 6, 17, 21, 0, tzinfo=UTC),
                "Also handle case X.",
            ),
        ]
    )
    mock_host.open_pull_request.return_value = _build_planner_pr_ref()

    identity_registry = MagicMock()
    identity_registry.get_role_bot_logins.return_value = _ALL_ROLE_BOT_LOGINS

    mock_wt_mgr = MagicMock()
    mock_wt_mgr.create.return_value = wt_path

    mock_provider = MagicMock()
    mock_provider.run_agent = AsyncMock(return_value=(_build_planner_output(), UsageInfo()))

    with (
        patch("foreman.roles.planner.WorktreeManager", return_value=mock_wt_mgr),
        patch(
            "foreman.roles.planner.build_role_resources",
            return_value=(mock_host, "fake-token", MagicMock()),
        ),
        patch("foreman.roles.planner.load_project_instructions", return_value=None),
        patch(
            "foreman.roles.planner.log_planner_run",
            return_value=Path("/tmp/fake-stats.jsonl"),
        ),
    ):
        await _run_planner_core(
            issue_url="https://github.com/testowner/myrepo/issues/335",
            config=cfg,
            project_name="p",
            worktrees_root=worktrees_root,
            provider=mock_provider,
            identity_registry=identity_registry,
        )

    user_prompt = mock_provider.run_agent.call_args.kwargs["user_prompt"]
    # Human comment routed in.
    assert "### @alice" in user_prompt
    # Bot self-comment filtered out by ``filter_bot_self_comments``.
    assert "### @foreman-planner[bot]" not in user_prompt
    # Labels rendered as the spec describes: ``## Labels`` followed by a
    # newline, then alphabetically-sorted labels joined by ", ".
    assert "## Labels\nforeman:state-planning, priority:high" in user_prompt


@pytest.mark.asyncio
async def test_planner_emits_no_comments_section_when_issue_has_none(
    tmp_path: Path,
) -> None:
    """When the issue has zero comments AND zero labels, neither section
    renders — the helper-level contract (empty input → empty string)
    is preserved end-to-end through ``_run_planner_core``.

    Counter-test to ``test_planner_injects_filtered_comments_and_labels``:
    proves the sections are conditional on non-empty inputs rather than
    accidentally always-emitted.
    """
    cfg = _build_v4_config(
        project_repo="testowner/myrepo",
        local_clone_path=str(tmp_path / "clone"),
    )
    Path(tmp_path / "clone").mkdir(parents=True, exist_ok=True)
    worktrees_root = tmp_path / "worktrees"
    wt_path = worktrees_root / "myrepo" / "issue-335"
    wt_path.mkdir(parents=True, exist_ok=True)

    issue_ref = IssueRef(
        number=335,
        title="t",
        body="b",
        labels=[],
        repo_slug="testowner/myrepo",
    )
    mock_host = MagicMock()
    mock_host.get_issue.return_value = issue_ref
    mock_host.get_default_branch.return_value = "main"
    mock_host.get_issue_comments.return_value = []
    mock_host.open_pull_request.return_value = _build_planner_pr_ref()

    identity_registry = MagicMock()
    identity_registry.get_role_bot_logins.return_value = _ALL_ROLE_BOT_LOGINS

    mock_wt_mgr = MagicMock()
    mock_wt_mgr.create.return_value = wt_path

    mock_provider = MagicMock()
    mock_provider.run_agent = AsyncMock(return_value=(_build_planner_output(), UsageInfo()))

    with (
        patch("foreman.roles.planner.WorktreeManager", return_value=mock_wt_mgr),
        patch(
            "foreman.roles.planner.build_role_resources",
            return_value=(mock_host, "fake-token", MagicMock()),
        ),
        patch("foreman.roles.planner.load_project_instructions", return_value=None),
        patch(
            "foreman.roles.planner.log_planner_run",
            return_value=Path("/tmp/fake-stats.jsonl"),
        ),
    ):
        await _run_planner_core(
            issue_url="https://github.com/testowner/myrepo/issues/335",
            config=cfg,
            project_name="p",
            worktrees_root=worktrees_root,
            provider=mock_provider,
            identity_registry=identity_registry,
        )

    user_prompt = mock_provider.run_agent.call_args.kwargs["user_prompt"]
    assert "## Comments" not in user_prompt
    assert "## Labels" not in user_prompt


# ---------------------------------------------------------------------
# Reviewer-on-spec / Reviewer-on-impl — _run_reviewer_core
# ---------------------------------------------------------------------


def _build_reviewer_output() -> ReviewerOutput:
    """Synthetic Reviewer LLM output. ``outcome="clean"`` + no findings
    is the simplest contract-satisfying shape; the model_validator
    rejects ``clean`` with findings and ``needs_fix`` without findings,
    so this branch lets the run reach the post-review path cleanly.
    """
    return ReviewerOutput(
        outcome="clean",
        review_comment="looks good",
        findings=[],
        confidence="high",
    )


def _build_reviewer_mock_pr(*, head_ref: str) -> MagicMock:
    """Fake PyGithub PR object with the attributes the Reviewer reads.

    ``head_ref`` selects whether the parsed target is ``spec_pr``
    (``foreman/issue-<N>``) or ``impl_pr`` (``foreman/impl-<N>``).
    """
    pr = MagicMock()
    pr.head.ref = head_ref
    pr.head.sha = "deadbeef"
    pr.base.ref = "main"
    pr.title = "test PR"
    pr.body = "test PR body"
    return pr


def _build_reviewer_mocks(
    *,
    head_ref: str,
    seeded_comments: list[SimpleNamespace],
    issue_labels: list[MagicMock] | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Build the PyGithub mock graph for the Reviewer.

    Returns ``(mock_repo, mock_pr, fake_issue)`` so individual tests can
    assert on ``fake_issue.get_comments.call_count``. The Reviewer reads
    ``repo.get_pull(n)`` → ``pr`` and ``repo.get_issue(n)`` → ``issue``;
    seeding both with explicit fakes makes the comment-fetch assertion
    cleanly observable.
    """
    mock_pr = _build_reviewer_mock_pr(head_ref=head_ref)
    fake_issue = MagicMock()
    fake_issue.title = "test issue"
    fake_issue.body = "test issue body"
    fake_issue.labels = issue_labels if issue_labels is not None else []
    fake_issue.get_comments = MagicMock(return_value=seeded_comments)
    mock_repo = MagicMock()
    mock_repo.get_pull.return_value = mock_pr
    mock_repo.get_issue.return_value = fake_issue
    return mock_repo, mock_pr, fake_issue


@pytest.mark.asyncio
async def test_reviewer_on_spec_injects_filtered_comments(
    tmp_path: Path,
) -> None:
    """foreman#335 / PR #331 e2e pin (Reviewer-on-spec side): when the
    PR head branch is ``foreman/issue-<N>``, the Reviewer's user prompt
    picks up the originating issue's comments via the PyGithub
    ``issue.get_comments()`` path (NOT via ``host.get_issue_comments``,
    which Planner uses — see ``reviewer.py:551`` for the production
    site).
    """
    cfg = _build_v4_config(
        project_repo="testowner/myrepo",
        local_clone_path=str(tmp_path / "clone"),
    )
    Path(tmp_path / "clone").mkdir(parents=True, exist_ok=True)
    worktrees_root = tmp_path / "worktrees"
    wt_path = worktrees_root / "myrepo" / "issue-335"
    wt_path.mkdir(parents=True, exist_ok=True)

    seeded = _seed_fake_issue_comments(
        [
            (
                "alice",
                datetime(2026, 6, 17, 21, 0, tzinfo=UTC),
                "Please also cover edge case X.",
            ),
        ]
    )
    mock_repo, mock_pr, fake_issue = _build_reviewer_mocks(
        head_ref="foreman/issue-335",
        seeded_comments=seeded,
    )
    mock_client = MagicMock()
    mock_client.get_repo.return_value = mock_repo

    mock_host = MagicMock()

    identity_registry = MagicMock()
    identity_registry.get_role_bot_logins.return_value = _ALL_ROLE_BOT_LOGINS

    mock_wt_mgr = MagicMock()
    mock_wt_mgr.attach.return_value = wt_path
    mock_wt_mgr.attach_impl.return_value = wt_path

    mock_provider = MagicMock()
    mock_provider.run_agent = AsyncMock(return_value=(_build_reviewer_output(), UsageInfo()))

    with (
        patch("foreman.roles.reviewer.WorktreeManager", return_value=mock_wt_mgr),
        patch(
            "foreman.roles.reviewer.build_role_resources",
            return_value=(mock_host, "fake-token", mock_client),
        ),
        patch("foreman.roles.reviewer._get_pr_diff", return_value=""),
        patch("foreman.roles.reviewer._read_spec_doc", return_value="# Spec\n"),
        patch("foreman.roles.reviewer.load_project_instructions", return_value=None),
        patch(
            "foreman.roles.reviewer.log_reviewer_run",
            return_value=Path("/tmp/fake-stats.jsonl"),
        ),
    ):
        await _run_reviewer_core(
            pr_url="https://github.com/testowner/myrepo/pull/1234",
            config=cfg,
            project_name="p",
            worktrees_root=worktrees_root,
            provider=mock_provider,
            identity_registry=identity_registry,
        )

    user_prompt = mock_provider.run_agent.call_args.kwargs["user_prompt"]
    assert "## Comments" in user_prompt
    assert "### @alice" in user_prompt
    # Reviewer never gets a ``## Labels`` section — labels are
    # Planner-only per ``reviewer._build_user_prompt``.
    assert "## Labels" not in user_prompt
    # Confirm the comment-fetch path WAS taken on spec_pr — the
    # counter-test below pins the impl_pr branch's zero-count.
    assert fake_issue.get_comments.call_count == 1
    # And the spec-side review was actually posted (no exception
    # short-circuit before then).
    mock_pr.create_review.assert_called_once()


@pytest.mark.asyncio
async def test_reviewer_on_impl_does_not_fetch_comments(
    tmp_path: Path,
) -> None:
    """foreman#335 regression pin (Reviewer-on-impl side): when the PR
    head branch is ``foreman/impl-<N>``, ``_run_reviewer_core`` MUST
    NOT call ``issue.get_comments()`` AND the user prompt MUST NOT
    contain a ``## Comments`` section.

    The seed deliberately populates ``fake_issue.get_comments`` with a
    non-empty list — if a future refactor accidentally calls it on the
    impl path, the populated comment would render and this test would
    fail on the negative-assertion line. An empty seed would also pass
    a leaky implementation (zero comments → empty section anyway).

    Mirrors the call-counter discipline PR #331 used in
    ``test_roles_worker.py`` on main before #333 displaced it.
    """
    cfg = _build_v4_config(
        project_repo="testowner/myrepo",
        local_clone_path=str(tmp_path / "clone"),
    )
    Path(tmp_path / "clone").mkdir(parents=True, exist_ok=True)
    worktrees_root = tmp_path / "worktrees"
    wt_path = worktrees_root / "myrepo" / "impl-335"
    wt_path.mkdir(parents=True, exist_ok=True)

    seeded_populated = _seed_fake_issue_comments(
        [
            (
                "alice",
                datetime(2026, 6, 17, 21, 0, tzinfo=UTC),
                "A populated comment that MUST NOT reach the impl prompt.",
            ),
        ]
    )
    mock_repo, mock_pr, fake_issue = _build_reviewer_mocks(
        head_ref="foreman/impl-335",
        seeded_comments=seeded_populated,
    )
    mock_client = MagicMock()
    mock_client.get_repo.return_value = mock_repo

    mock_host = MagicMock()

    identity_registry = MagicMock()
    identity_registry.get_role_bot_logins.return_value = _ALL_ROLE_BOT_LOGINS

    mock_wt_mgr = MagicMock()
    mock_wt_mgr.attach_impl.return_value = wt_path
    mock_wt_mgr.attach.return_value = wt_path

    mock_provider = MagicMock()
    mock_provider.run_agent = AsyncMock(return_value=(_build_reviewer_output(), UsageInfo()))

    with (
        patch("foreman.roles.reviewer.WorktreeManager", return_value=mock_wt_mgr),
        patch(
            "foreman.roles.reviewer.build_role_resources",
            return_value=(mock_host, "fake-token", mock_client),
        ),
        patch("foreman.roles.reviewer._get_pr_diff", return_value=""),
        patch("foreman.roles.reviewer._read_spec_doc", return_value="# Spec\n"),
        patch("foreman.roles.reviewer.load_project_instructions", return_value=None),
        patch(
            "foreman.roles.reviewer.log_reviewer_run",
            return_value=Path("/tmp/fake-stats.jsonl"),
        ),
    ):
        await _run_reviewer_core(
            pr_url="https://github.com/testowner/myrepo/pull/1234",
            config=cfg,
            project_name="p",
            worktrees_root=worktrees_root,
            provider=mock_provider,
            identity_registry=identity_registry,
        )

    user_prompt = mock_provider.run_agent.call_args.kwargs["user_prompt"]
    assert fake_issue.get_comments.call_count == 0
    assert "## Comments" not in user_prompt
    # Sanity: the impl-side review was still posted (the test is about
    # the comment-fetch surface, not the rest of the pipeline).
    mock_pr.create_review.assert_called_once()


# ---------------------------------------------------------------------
# Fixer-on-spec / Fixer-on-impl — _run_fixer_core
# ---------------------------------------------------------------------


def _build_fixer_output() -> FixerOutput:
    """Synthetic Fixer LLM output. ``outcome="fixed"`` with no addressed
    findings + no commits made is the simplest shape that satisfies the
    schema and lets the test reach the assertion path.

    Empty ``commits_made`` means the Fixer's success-path
    ``host.push_branch`` call is skipped — fewer mock seams to maintain
    and the test still exercises the comment-fetch + prompt-compose
    path which is what we're pinning.
    """
    return FixerOutput(
        outcome="fixed",
        fix_comment="seed",
        commits_made=[],
        addressed_findings=[],
        unaddressed_findings=[],
        confidence="high",
    )


def _build_fixer_mocks(
    *,
    seeded_comments: list[SimpleNamespace],
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Build the PyGithub mock graph for the Fixer.

    The Fixer goes ``repo.get_issue(n)`` → ``issue`` for the comment
    fetch (when ``target='spec_pr'``); ``_find_role_pr`` is patched at
    the call site so the PR lookup never reaches ``repo.get_pulls``.
    """
    fake_issue = MagicMock()
    fake_issue.title = "test issue"
    fake_issue.body = "test issue body"
    fake_issue.labels = []
    fake_issue.get_comments = MagicMock(return_value=seeded_comments)

    mock_pr = MagicMock()
    mock_pr.number = 1234
    mock_pr.title = "test PR"
    mock_pr.body = "test PR body"

    mock_repo = MagicMock()
    mock_repo.get_issue.return_value = fake_issue
    return mock_repo, mock_pr, fake_issue


@pytest.mark.asyncio
async def test_fixer_on_spec_injects_filtered_comments(
    tmp_path: Path,
) -> None:
    """foreman#335 / PR #331 e2e pin (Fixer-on-spec side): when
    ``target='spec_pr'``, the Fixer's user prompt picks up the
    originating issue's comments via the PyGithub
    ``issue.get_comments()`` path (``fixer.py:624``).
    """
    cfg = _build_v4_config(
        project_repo="testowner/myrepo",
        local_clone_path=str(tmp_path / "clone"),
    )
    Path(tmp_path / "clone").mkdir(parents=True, exist_ok=True)
    worktrees_root = tmp_path / "worktrees"
    wt_path = worktrees_root / "myrepo" / "issue-335"
    wt_path.mkdir(parents=True, exist_ok=True)

    seeded = _seed_fake_issue_comments(
        [
            (
                "alice",
                datetime(2026, 6, 17, 21, 0, tzinfo=UTC),
                "Please also cover edge case X.",
            ),
        ]
    )
    mock_repo, mock_pr, fake_issue = _build_fixer_mocks(seeded_comments=seeded)
    mock_client = MagicMock()
    mock_client.get_repo.return_value = mock_repo

    mock_host = MagicMock()

    identity_registry = MagicMock()
    identity_registry.get_role_bot_logins.return_value = _ALL_ROLE_BOT_LOGINS

    mock_wt_mgr = MagicMock()
    mock_wt_mgr.attach.return_value = wt_path
    mock_wt_mgr.attach_impl.return_value = wt_path

    mock_provider = MagicMock()
    mock_provider.run_agent = AsyncMock(return_value=(_build_fixer_output(), UsageInfo()))

    with (
        patch("foreman.roles.fixer.WorktreeManager", return_value=mock_wt_mgr),
        patch(
            "foreman.roles.fixer.build_role_resources",
            return_value=(mock_host, "fake-token", mock_client),
        ),
        patch("foreman.roles.fixer._find_role_pr", return_value=mock_pr),
        patch(
            "foreman.roles.fixer._latest_reviewer_review_comment",
            return_value="reviewer prose",
        ),
        patch(
            "foreman.roles.fixer._extract_findings_from_review_comment",
            return_value=[],
        ),
        patch("foreman.roles.fixer._read_spec_doc", return_value="# Spec\n"),
        patch("foreman.roles.fixer.load_project_instructions", return_value=None),
        patch(
            "foreman.roles.fixer.log_fixer_run",
            return_value=Path("/tmp/fake-stats.jsonl"),
        ),
        # ``_ensure_provenance_trailers`` (worker module) is invoked
        # post-LLM by the Fixer to amend missing trailers; patch to a
        # no-op so the test doesn't try to run git in the fake worktree.
        patch(
            "foreman.roles.worker._ensure_provenance_trailers",
            return_value=None,
        ),
    ):
        await _run_fixer_core(
            issue_url="https://github.com/testowner/myrepo/issues/335",
            config=cfg,
            project_name="p",
            worktrees_root=worktrees_root,
            provider=mock_provider,
            identity_registry=identity_registry,
            target="spec_pr",
        )

    user_prompt = mock_provider.run_agent.call_args.kwargs["user_prompt"]
    assert "## Comments" in user_prompt
    assert "### @alice" in user_prompt
    # Fixer never gets a ``## Labels`` section — labels are
    # Planner-only per ``fixer._build_user_prompt``.
    assert "## Labels" not in user_prompt
    assert fake_issue.get_comments.call_count == 1


@pytest.mark.asyncio
async def test_fixer_on_impl_does_not_fetch_comments(
    tmp_path: Path,
) -> None:
    """foreman#335 regression pin (Fixer-on-impl side): when
    ``target='impl_pr'``, ``_run_fixer_core`` MUST NOT call
    ``issue.get_comments()`` AND the user prompt MUST NOT contain a
    ``## Comments`` section.

    Seeded with a populated comment so a leaky implementation that
    fetched-but-didn't-render would not slip past the negative
    assertion.
    """
    cfg = _build_v4_config(
        project_repo="testowner/myrepo",
        local_clone_path=str(tmp_path / "clone"),
    )
    Path(tmp_path / "clone").mkdir(parents=True, exist_ok=True)
    worktrees_root = tmp_path / "worktrees"
    wt_path = worktrees_root / "myrepo" / "impl-335"
    wt_path.mkdir(parents=True, exist_ok=True)

    seeded_populated = _seed_fake_issue_comments(
        [
            (
                "alice",
                datetime(2026, 6, 17, 21, 0, tzinfo=UTC),
                "A populated comment that MUST NOT reach the impl prompt.",
            ),
        ]
    )
    mock_repo, mock_pr, fake_issue = _build_fixer_mocks(seeded_comments=seeded_populated)
    mock_client = MagicMock()
    mock_client.get_repo.return_value = mock_repo

    mock_host = MagicMock()

    identity_registry = MagicMock()
    identity_registry.get_role_bot_logins.return_value = _ALL_ROLE_BOT_LOGINS

    mock_wt_mgr = MagicMock()
    mock_wt_mgr.attach.return_value = wt_path
    mock_wt_mgr.attach_impl.return_value = wt_path

    mock_provider = MagicMock()
    mock_provider.run_agent = AsyncMock(return_value=(_build_fixer_output(), UsageInfo()))

    with (
        patch("foreman.roles.fixer.WorktreeManager", return_value=mock_wt_mgr),
        patch(
            "foreman.roles.fixer.build_role_resources",
            return_value=(mock_host, "fake-token", mock_client),
        ),
        patch("foreman.roles.fixer._find_role_pr", return_value=mock_pr),
        patch(
            "foreman.roles.fixer._latest_reviewer_review_comment",
            return_value="reviewer prose",
        ),
        patch(
            "foreman.roles.fixer._extract_findings_from_review_comment",
            return_value=[],
        ),
        patch("foreman.roles.fixer._read_spec_doc", return_value="# Spec\n"),
        patch("foreman.roles.fixer.load_project_instructions", return_value=None),
        patch(
            "foreman.roles.fixer.log_fixer_run",
            return_value=Path("/tmp/fake-stats.jsonl"),
        ),
        patch(
            "foreman.roles.worker._ensure_provenance_trailers",
            return_value=None,
        ),
    ):
        await _run_fixer_core(
            issue_url="https://github.com/testowner/myrepo/issues/335",
            config=cfg,
            project_name="p",
            worktrees_root=worktrees_root,
            provider=mock_provider,
            identity_registry=identity_registry,
            target="impl_pr",
        )

    user_prompt = mock_provider.run_agent.call_args.kwargs["user_prompt"]
    assert fake_issue.get_comments.call_count == 0
    assert "## Comments" not in user_prompt


# ---------------------------------------------------------------------
# Worker — _run_worker_core never touches the helper module
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_does_not_fetch_comments(tmp_path: Path) -> None:
    """foreman#335 regression-by-construction pin: the Worker's
    ``_run_worker_core`` never imports ``foreman.roles._prompt_helpers``
    and never calls ``host.get_issue_comments``. This test makes the
    "Worker doesn't touch the comment-fetch surface" contract explicit
    so a future Worker change that accidentally adopts the helper fails
    this assertion at PR time.

    Uses the same first-run mock scaffold as
    ``test_worker_first_run_with_no_existing_pr_calls_push_and_create_pull``
    in ``test_worker_core.py``; the only added assertion is the
    ``assert_not_called`` on ``host.get_issue_comments``.
    """
    worktrees_root = tmp_path / "worktrees"
    impl_wt_path = worktrees_root / "myrepo" / "impl-335"
    impl_wt_path.mkdir(parents=True, exist_ok=True)
    Path(tmp_path / "clone").mkdir(parents=True, exist_ok=True)

    cfg = _build_v4_config(
        project_repo="testowner/myrepo",
        local_clone_path=str(tmp_path / "clone"),
    )

    identity_registry = MagicMock()

    mock_wt_mgr = MagicMock()
    mock_wt_mgr.create_impl.return_value = ImplWorktreeResult(path=impl_wt_path, base_branch="main")

    mock_pr = MagicMock()
    mock_pr.html_url = "https://github.com/testowner/myrepo/pull/9335"
    mock_pr.number = 9335
    mock_repo = MagicMock()
    mock_repo.default_branch = "main"
    mock_repo.create_pull.return_value = mock_pr
    mock_repo.get_pulls.return_value = []
    mock_issue = MagicMock()
    mock_issue.title = "test issue"
    mock_issue.body = "test body"
    mock_issue.labels = []
    mock_repo.get_issue.return_value = mock_issue

    mock_client = MagicMock()
    mock_client.get_repo.return_value = mock_repo

    mock_host = MagicMock()

    mock_provider = MagicMock()
    fake_worker_output = WorkerOutput(
        outcome="implemented",
        work_comment="implemented for test",
        pr_title="feat(test): fake impl",
        pr_body="fake impl PR body",
        commits_made=[
            CommitMade(
                sha="deadbeef",
                summary="feat(test): fake impl",
                files_changed=["packages/foo/bar.py"],
            ),
        ],
        implemented_sub_requests=[],
        skipped_sub_requests=[],
        did_check_pass=True,
        check_output_summary="",
        confidence="high",
    )
    mock_provider.run_agent = AsyncMock(return_value=(fake_worker_output, UsageInfo()))

    with (
        patch("foreman.roles.worker.WorktreeManager", return_value=mock_wt_mgr),
        patch(
            "foreman.roles.worker.build_role_resources",
            return_value=(mock_host, "fake-token", mock_client),
        ),
        patch("foreman.roles.worker._run_check_command", return_value=(0, set(), "")),
        patch(
            "foreman.roles.worker._read_spec_doc_from_branch",
            return_value="# Spec\nfake spec content\n",
        ),
        patch(
            "foreman.roles.worker._sanitize_head_commit_auto_close",
            return_value=False,
        ),
        patch(
            "foreman.roles.worker._verify_impl_branch_remote_state",
            return_value=None,
        ),
        patch("foreman.roles.worker.load_project_instructions", return_value=None),
        patch(
            "foreman.roles.worker.log_worker_run",
            return_value=Path("/tmp/fake-stats.jsonl"),
        ),
    ):
        await _run_worker_core(
            issue_url="https://github.com/testowner/myrepo/issues/335",
            config=cfg,
            project_name="p",
            worktrees_root=worktrees_root,
            provider=mock_provider,
            identity_registry=identity_registry,
        )

    # The contract pin: the Worker never reaches the comment-fetch
    # surface. ``_run_worker_core`` does not import
    # ``_prompt_helpers``, so this assertion is regression-by-
    # construction; the explicit ``assert_not_called`` makes the
    # contract observable in CI.
    mock_host.get_issue_comments.assert_not_called()


# ---------------------------------------------------------------------
# foreman#586 — load_project_instructions receives wt_path, not bare mirror
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planner_load_instructions_receives_wt_path(
    tmp_path: Path,
) -> None:
    """foreman#586 regression pin (Planner): ``load_project_instructions``
    must be called with the checked-out worktree path (``wt_path``),
    not ``Path(project.local_clone_path)`` (the bare mirror base).

    Bare mirrors have no working tree so the loader always returned
    ``None`` even when ``.foreman/INSTRUCTIONS.md`` was committed.
    """
    cfg = _build_v4_config(
        project_repo="testowner/myrepo",
        local_clone_path=str(tmp_path / "clone"),
    )
    Path(tmp_path / "clone").mkdir(parents=True, exist_ok=True)
    worktrees_root = tmp_path / "worktrees"
    wt_path = worktrees_root / "myrepo" / "issue-586"
    wt_path.mkdir(parents=True, exist_ok=True)

    issue_ref = IssueRef(
        number=586,
        title="test: wt_path instruction loading",
        body="See #586.",
        labels=[],
        repo_slug="testowner/myrepo",
    )
    mock_host = MagicMock()
    mock_host.get_issue.return_value = issue_ref
    mock_host.get_default_branch.return_value = "main"
    mock_host.get_issue_comments.return_value = []
    mock_host.open_pull_request.return_value = _build_planner_pr_ref()

    identity_registry = MagicMock()
    identity_registry.get_role_bot_logins.return_value = _ALL_ROLE_BOT_LOGINS

    mock_wt_mgr = MagicMock()
    mock_wt_mgr.create.return_value = wt_path

    mock_provider = MagicMock()
    mock_provider.run_agent = AsyncMock(return_value=(_build_planner_output(), UsageInfo()))

    mock_load = MagicMock(return_value=None)

    with (
        patch("foreman.roles.planner.WorktreeManager", return_value=mock_wt_mgr),
        patch(
            "foreman.roles.planner.build_role_resources",
            return_value=(mock_host, "fake-token", MagicMock()),
        ),
        patch("foreman.roles.planner.load_project_instructions", new=mock_load),
        patch(
            "foreman.roles.planner.log_planner_run",
            return_value=Path("/tmp/fake-stats.jsonl"),
        ),
    ):
        await _run_planner_core(
            issue_url="https://github.com/testowner/myrepo/issues/586",
            config=cfg,
            project_name="p",
            worktrees_root=worktrees_root,
            provider=mock_provider,
            identity_registry=identity_registry,
        )

    # foreman#586: the argument must be the worktree path, not the bare mirror.
    assert mock_load.call_args[0][0] == wt_path


@pytest.mark.asyncio
async def test_reviewer_on_spec_load_instructions_receives_wt_path(
    tmp_path: Path,
) -> None:
    """foreman#586 regression pin (Reviewer-on-spec): ``load_project_instructions``
    must be called with ``wt_path`` (the checked-out worktree), not
    ``Path(project.local_clone_path)`` (the bare mirror base).
    """
    cfg = _build_v4_config(
        project_repo="testowner/myrepo",
        local_clone_path=str(tmp_path / "clone"),
    )
    Path(tmp_path / "clone").mkdir(parents=True, exist_ok=True)
    worktrees_root = tmp_path / "worktrees"
    wt_path = worktrees_root / "myrepo" / "issue-586"
    wt_path.mkdir(parents=True, exist_ok=True)

    mock_repo, mock_pr, fake_issue = _build_reviewer_mocks(
        head_ref="foreman/issue-586",
        seeded_comments=[],
    )
    mock_client = MagicMock()
    mock_client.get_repo.return_value = mock_repo

    mock_host = MagicMock()
    identity_registry = MagicMock()
    identity_registry.get_role_bot_logins.return_value = _ALL_ROLE_BOT_LOGINS

    mock_wt_mgr = MagicMock()
    mock_wt_mgr.attach.return_value = wt_path
    mock_wt_mgr.attach_impl.return_value = wt_path

    mock_provider = MagicMock()
    mock_provider.run_agent = AsyncMock(return_value=(_build_reviewer_output(), UsageInfo()))

    mock_load = MagicMock(return_value=None)

    with (
        patch("foreman.roles.reviewer.WorktreeManager", return_value=mock_wt_mgr),
        patch(
            "foreman.roles.reviewer.build_role_resources",
            return_value=(mock_host, "fake-token", mock_client),
        ),
        patch("foreman.roles.reviewer._get_pr_diff", return_value=""),
        patch("foreman.roles.reviewer._read_spec_doc", return_value="# Spec\n"),
        patch("foreman.roles.reviewer.load_project_instructions", new=mock_load),
        patch(
            "foreman.roles.reviewer.log_reviewer_run",
            return_value=Path("/tmp/fake-stats.jsonl"),
        ),
    ):
        await _run_reviewer_core(
            pr_url="https://github.com/testowner/myrepo/pull/1234",
            config=cfg,
            project_name="p",
            worktrees_root=worktrees_root,
            provider=mock_provider,
            identity_registry=identity_registry,
        )

    # foreman#586: the argument must be the worktree path, not the bare mirror.
    assert mock_load.call_args[0][0] == wt_path


@pytest.mark.asyncio
async def test_fixer_on_spec_load_instructions_receives_wt_path(
    tmp_path: Path,
) -> None:
    """foreman#586 regression pin (Fixer-on-spec): ``load_project_instructions``
    must be called with ``wt_path`` (the checked-out worktree), not
    ``Path(project.local_clone_path)`` (the bare mirror base).
    """
    cfg = _build_v4_config(
        project_repo="testowner/myrepo",
        local_clone_path=str(tmp_path / "clone"),
    )
    Path(tmp_path / "clone").mkdir(parents=True, exist_ok=True)
    worktrees_root = tmp_path / "worktrees"
    wt_path = worktrees_root / "myrepo" / "issue-586"
    wt_path.mkdir(parents=True, exist_ok=True)

    mock_repo, mock_pr, fake_issue = _build_fixer_mocks(seeded_comments=[])
    mock_client = MagicMock()
    mock_client.get_repo.return_value = mock_repo

    mock_host = MagicMock()
    identity_registry = MagicMock()
    identity_registry.get_role_bot_logins.return_value = _ALL_ROLE_BOT_LOGINS

    mock_wt_mgr = MagicMock()
    mock_wt_mgr.attach.return_value = wt_path
    mock_wt_mgr.attach_impl.return_value = wt_path

    mock_provider = MagicMock()
    mock_provider.run_agent = AsyncMock(return_value=(_build_fixer_output(), UsageInfo()))

    mock_load = MagicMock(return_value=None)

    with (
        patch("foreman.roles.fixer.WorktreeManager", return_value=mock_wt_mgr),
        patch(
            "foreman.roles.fixer.build_role_resources",
            return_value=(mock_host, "fake-token", mock_client),
        ),
        patch("foreman.roles.fixer._find_role_pr", return_value=mock_pr),
        patch(
            "foreman.roles.fixer._latest_reviewer_review_comment",
            return_value="reviewer prose",
        ),
        patch(
            "foreman.roles.fixer._extract_findings_from_review_comment",
            return_value=[],
        ),
        patch("foreman.roles.fixer._read_spec_doc", return_value="# Spec\n"),
        patch("foreman.roles.fixer.load_project_instructions", new=mock_load),
        patch(
            "foreman.roles.fixer.log_fixer_run",
            return_value=Path("/tmp/fake-stats.jsonl"),
        ),
        patch(
            "foreman.roles.worker._ensure_provenance_trailers",
            return_value=None,
        ),
    ):
        await _run_fixer_core(
            issue_url="https://github.com/testowner/myrepo/issues/586",
            config=cfg,
            project_name="p",
            worktrees_root=worktrees_root,
            provider=mock_provider,
            identity_registry=identity_registry,
            target="spec_pr",
        )

    # foreman#586: the argument must be the worktree path, not the bare mirror.
    assert mock_load.call_args[0][0] == wt_path
