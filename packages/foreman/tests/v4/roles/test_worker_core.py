"""Worker ``_run_worker_core`` end-to-end seam tests.

These tests exercise :func:`foreman.roles.worker._run_worker_core` with
every external collaborator (WorktreeManager, ProviderFacade, GitHub
host, PyGithub repo, check_command runner) replaced by an explicit
mock — so the test pins the Worker's CONTROL FLOW rather than any one
helper's behavior.

foreman#341 added the first test here: a regression pin that the impl
PR's ``base`` is the project's ``dev_base_branch`` (default ``main``),
not the spec branch. Before #341, ``WorktreeManager.create_impl``
returned ``base_branch="foreman/issue-<N>"`` (stacked-PR design from
before v4 SpecReviewState merged the spec into main pre-Worker), and
``_run_worker_core`` faithfully forwarded that into
``repo.create_pull(base=...)``. The result was PR #339: the impl PR's
recorded base was the orphan spec branch, and merging through the UI
landed changes there instead of main. The fix retargets the worktree's
base to the dev base; this test pins ``create_pull(base="main")`` end-
to-end so a future regression of the same shape fails this test rather
than reaching production.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from foreman.provider import UsageInfo
from foreman.roles.worker import _run_worker_core
from foreman.schemas.worker import CommitMade, WorkerOutput
from foreman.v4.config import (
    AppCredentials,
    AppsConfig,
    OrchestratorConfig,
    ProjectConfig,
    V4Config,
)
from foreman.worktree import ImplWorktreeResult


def _build_v4_config(*, project_repo: str, local_clone_path: str) -> V4Config:
    """Build a minimal V4Config with one project + four App identities.

    The App credentials are placeholders — the test patches
    ``build_role_resources`` so the registry / fetch_app_metadata
    path is never exercised, but the V4Config validator still requires
    every field to be present and well-typed.
    """
    placeholder_app = AppCredentials(app_id=1, private_key_path="/dev/null")
    return V4Config(
        db_path="/tmp/foreman-test.db",
        log_dir="/tmp/foreman-test-logs",
        apps=AppsConfig(
            planner=placeholder_app,
            reviewer=placeholder_app,
            fixer=placeholder_app,
            worker=placeholder_app,
        ),
        orchestrator=OrchestratorConfig(app_id=1, private_key_path="/dev/null"),
        projects=[
            ProjectConfig(
                name="p",
                repo=project_repo,
                local_clone_path=local_clone_path,
            ),
        ],
    )


@pytest.mark.asyncio
async def test_worker_opens_impl_pr_with_base_main_not_spec_branch(
    tmp_path: Path,
) -> None:
    """foreman#341 regression pin: the impl PR's ``base`` kwarg must
    resolve to the project's dev base (default ``main``), never to the
    spec branch ``foreman/issue-<N>``.

    Pre-#341, the bug shape was:

    1. ``WorktreeManager.create_impl`` returned
       ``base_branch="foreman/issue-<N>"`` when the spec branch was
       on origin (the stacked-PR default).
    2. ``_run_worker_core`` passed that into ``create_pull(base=...)``.
    3. PR #339 was opened with ``base=foreman/issue-337``; merging it
       through the GitHub UI landed changes on the spec branch instead
       of main.

    After #341, ``create_impl`` reports the project's resolved
    ``dev_base_branch`` (or the clone's default branch when None) as
    the impl PR's base. This test mocks ``create_impl`` to return
    ``base_branch="main"`` and asserts the call propagates through to
    ``repo.create_pull(base="main")`` — i.e., the Worker forwards the
    WorktreeManager's decision, and the WorktreeManager's decision is
    now ``main``.

    The mock surface is wide because the goal is to pin the
    create_pull-arg semantics, not to exercise any one collaborator.
    Every seam between ``_run_worker_core`` and PyGithub is replaced.
    """
    # Set up a real worktree directory the function can `cwd=` into.
    # Nothing actually runs `git` against it — the check_command runner
    # is mocked — but the worktree path needs to exist so any defensive
    # checks (e.g., `Path.exists()` reads in the in-progress code path)
    # pass cleanly.
    worktrees_root = tmp_path / "worktrees"
    impl_wt_path = worktrees_root / "myrepo" / "impl-341"
    impl_wt_path.mkdir(parents=True, exist_ok=True)

    cfg = _build_v4_config(
        project_repo="testowner/myrepo",
        local_clone_path=str(tmp_path / "clone"),
    )
    Path(tmp_path / "clone").mkdir(parents=True, exist_ok=True)

    # Identity registry stub — `_run_worker_core` doesn't call it
    # directly because we patch `build_role_resources` further down.
    identity_registry = MagicMock()

    # Patched WorktreeManager: its `create_impl` returns
    # `ImplWorktreeResult(path=impl_wt_path, base_branch="main")`.
    # This is the foreman#341 contract under test — the result's
    # base_branch is "main", not "foreman/issue-341".
    mock_wt_mgr = MagicMock()
    mock_wt_mgr.create_impl.return_value = ImplWorktreeResult(
        path=impl_wt_path, base_branch="main"
    )

    # PyGithub Repository mock. `create_pull` returns a PR object with
    # an `html_url` so the Worker can record `pr_url` on its result.
    mock_pr = MagicMock()
    mock_pr.html_url = "https://github.com/testowner/myrepo/pull/9001"
    mock_pr.number = 9001
    mock_repo = MagicMock()
    mock_repo.create_pull.return_value = mock_pr
    # `_find_spec_pr` queries `repo.get_pulls(state="open", head=...)`;
    # return empty so the Worker treats spec_pr as None (the v4 normal
    # path post-SpecReviewState — the spec PR is already merged).
    mock_repo.get_pulls.return_value = []

    # Repository.get_issue → returns an issue with title/body.
    mock_issue = MagicMock()
    mock_issue.title = "test issue"
    mock_issue.body = "test body"
    mock_issue.labels = []
    mock_repo.get_issue.return_value = mock_issue

    # PyGithub client → repo.
    mock_client = MagicMock()
    mock_client.get_repo.return_value = mock_repo

    # GitHostProvider — only `push_branch` is called from `_run_worker_core`
    # (and inside `_verify_impl_branch_remote_state`, which we patch out
    # below so this doesn't matter).
    mock_host = MagicMock()

    # Provider facade — `run_agent` is async; return a synthesized
    # WorkerOutput with `outcome="implemented"` so the Worker takes
    # the create_pull path. The actual implementation work is faked.
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
        patch(
            "foreman.roles.worker.WorktreeManager",
            return_value=mock_wt_mgr,
        ),
        patch(
            "foreman.roles.worker.build_role_resources",
            return_value=(mock_host, "fake-token", mock_client),
        ),
        patch(
            "foreman.roles.worker._run_check_command",
            return_value=(0, set(), ""),
        ),
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
        patch(
            "foreman.roles.worker.load_project_instructions",
            return_value=None,
        ),
        patch(
            "foreman.roles.worker.log_worker_run",
            return_value=Path("/tmp/fake-stats.jsonl"),
        ),
    ):
        result = await _run_worker_core(
            issue_url="https://github.com/testowner/myrepo/issues/341",
            config=cfg,
            project_name="p",
            worktrees_root=worktrees_root,
            provider=mock_provider,
            identity_registry=identity_registry,
        )

    # Sanity: the run reached the create_pull path.
    assert result.pr_url == "https://github.com/testowner/myrepo/pull/9001"
    assert result.llm_output.outcome == "implemented"
    assert result.final_did_check_pass is True

    # foreman#341 regression pin: the impl PR's ``base`` kwarg MUST be
    # ``"main"``, NOT the spec branch ``"foreman/issue-341"``. This is
    # the contract violation PR #339 exposed.
    create_pull_kwargs = mock_repo.create_pull.call_args.kwargs
    assert create_pull_kwargs["base"] == "main", (
        f"foreman#341: impl PR base must be the dev base branch ('main'), "
        f"not the spec branch. create_pull received base="
        f"{create_pull_kwargs['base']!r}"
    )
    assert create_pull_kwargs["head"] == "foreman/impl-341", (
        f"impl PR head must be the impl branch; got "
        f"{create_pull_kwargs['head']!r}"
    )

    # Defense-in-depth: confirm the WorktreeManager was asked for the
    # impl worktree with ``dev_base_branch`` threaded through from the
    # project config. This pins sub-request 5 in the spec — the Worker
    # propagates ``project.dev_base_branch`` into the WorktreeManager,
    # so projects that override the dev base (e.g. ``develop``) reach
    # ``create_impl`` correctly.
    create_impl_kwargs = mock_wt_mgr.create_impl.call_args.kwargs
    assert "dev_base_branch" in create_impl_kwargs, (
        "Worker must pass dev_base_branch through to WorktreeManager.create_impl"
    )
    # ProjectConfig default for dev_base_branch is None when the TOML
    # didn't set it; the Worker propagates that None faithfully.
    assert create_impl_kwargs["dev_base_branch"] is None
