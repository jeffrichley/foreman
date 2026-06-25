"""Planner ``_run_planner_core`` end-to-end seam tests.

These tests exercise :func:`foreman.roles.planner._run_planner_core` with
every external collaborator (WorktreeManager, ProviderFacade, GitHub
host, PyGithub client/repo) replaced by an explicit mock — so the test
pins the Planner's CONTROL FLOW rather than any one helper's behavior.
Mirrors ``tests/v4/roles/test_worker_core.py``'s mock-graph style.

Stage 1b (foreman crash-recovery): a daemon crash mid-Planning
re-dispatches the Planner subprocess. Before the idempotency guard, the
re-run unconditionally committed the spec doc, pushed the branch, and
called ``host.open_pull_request`` — which GitHub answers with 422 ("A
pull request already exists ...") because the prior (crashed) attempt
already opened the spec PR. The 422 crashed the subprocess and wedged
the ticket to Failed. The fix mirrors the Worker's existing impl-PR
idempotency (issue #342): probe for an open spec PR on
``foreman/issue-<N>`` and ADOPT it (skip commit/push/create) instead of
re-creating.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from foreman.git_host import PRRef
from foreman.provider import UsageInfo
from foreman.roles.planner import _run_planner_core
from foreman.schemas.planner import PlannerOutput
from foreman.v4.config import (
    AppCredentials,
    AppsConfig,
    OperatorConfig,
    OperatorIdentity,
    OrchestratorConfig,
    ProjectConfig,
    V4Config,
)


def _build_v4_config(*, project_repo: str, local_clone_path: str) -> V4Config:
    """Minimal V4Config with one project + four App identities.

    The App credentials are placeholders — the test patches
    ``build_role_resources`` so the registry / fetch_app_metadata path
    is never exercised, but the V4Config validator still requires every
    field present and well-typed.
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


def _fake_planner_output() -> PlannerOutput:
    return PlannerOutput(
        spec_doc_content="# Spec\nfresh spec doc from the re-run\n",
        pr_title="feat(test): fake spec",
        pr_body="fake spec PR body",
        summary="spec PR opened",
        confidence="high",
    )


def _build_planner_scaffold(
    tmp_path: Path,
    *,
    existing_spec_pr: object | None,
    issue_number: int = 555,
) -> tuple[V4Config, MagicMock, MagicMock, MagicMock, MagicMock, MagicMock, Path]:
    """Build the mock graph + V4Config shared by the Planner seam tests.

    Returns ``(cfg, identity_registry, mock_wt_mgr, mock_repo,
    mock_host, mock_provider, worktrees_root)``. ``existing_spec_pr`` is
    the value ``repo.get_pulls(state="open", head="...")`` returns for
    the spec-branch query (single-element list with this mock, or empty
    list for the first-run/create path).
    """
    worktrees_root = tmp_path / "worktrees"
    wt_path = worktrees_root / "myrepo" / f"issue-{issue_number}"
    wt_path.mkdir(parents=True, exist_ok=True)

    cfg = _build_v4_config(
        project_repo="testowner/myrepo",
        local_clone_path=str(tmp_path / "clone"),
    )
    Path(tmp_path / "clone").mkdir(parents=True, exist_ok=True)

    identity_registry = MagicMock()

    mock_wt_mgr = MagicMock()
    mock_wt_mgr.create.return_value = wt_path

    spec_branch_name = f"foreman/issue-{issue_number}"

    def _get_pulls_side_effect(*, state: str, head: str):
        assert state == "open"
        if head == f"testowner:{spec_branch_name}":
            return [existing_spec_pr] if existing_spec_pr is not None else []
        return []

    mock_repo = MagicMock()
    mock_repo.get_pulls.side_effect = _get_pulls_side_effect

    mock_client = MagicMock()
    mock_client.get_repo.return_value = mock_repo

    # GitHostProvider stub. ``open_pull_request`` returns a PRRef-shaped
    # object only on the create path; the adopt path must NOT call it.
    mock_host = MagicMock()
    mock_issue = MagicMock()
    mock_issue.number = issue_number
    mock_issue.title = "test issue"
    mock_issue.body = "test body"
    mock_issue.labels = []
    mock_host.get_issue.return_value = mock_issue
    mock_host.get_default_branch.return_value = "main"
    mock_host.get_issue_comments.return_value = []
    # ``host.open_pull_request`` returns a real PRRef in production; the
    # adopt path must NOT reach this, but the create path asserts on it.
    mock_host.open_pull_request.return_value = PRRef(
        number=9000 + issue_number,
        url=f"https://github.com/testowner/myrepo/pull/{9000 + issue_number}",
        title="feat(test): fake spec",
        body="fake spec PR body",
        branch=spec_branch_name,
        base_branch="main",
        repo_slug="testowner/myrepo",
    )

    mock_provider = MagicMock()
    mock_provider.run_agent = AsyncMock(
        return_value=(_fake_planner_output(), UsageInfo())
    )

    # The mock client/host are injected via build_role_resources at call time.
    mock_host._injected_client = mock_client  # test wiring marker

    return (
        cfg,
        identity_registry,
        mock_wt_mgr,
        mock_repo,
        mock_host,
        mock_provider,
        worktrees_root,
    )


@pytest.mark.asyncio
async def test_planner_adopts_existing_spec_pr_skips_create(tmp_path: Path) -> None:
    """Stage 1b: when an open spec PR already exists for
    ``foreman/issue-<N>`` (a prior crashed Planner dispatch opened it),
    the Planner MUST NOT call ``host.open_pull_request`` again — that is
    the source of the GitHub 422 "A pull request already exists" crash.

    The Planner adopts the existing PR: the CLEAN-outcome path's
    ``pr_number`` equals the existing PR's number, and neither
    ``commit_files_to_worktree`` / ``push_branch`` / ``open_pull_request``
    is invoked.
    """
    issue_number = 555
    existing_pr = MagicMock()
    existing_pr.number = 808
    existing_pr.html_url = "https://github.com/testowner/myrepo/pull/808"
    existing_pr.title = "feat(test): prior crashed-attempt spec"
    existing_pr.body = "spec PR body from the prior attempt"

    cfg, identity_registry, mock_wt_mgr, mock_repo, mock_host, mock_provider, worktrees_root = (
        _build_planner_scaffold(tmp_path, existing_spec_pr=existing_pr, issue_number=issue_number)
    )

    with (
        patch("foreman.roles.planner.WorktreeManager", return_value=mock_wt_mgr),
        patch(
            "foreman.roles.planner.build_role_resources",
            return_value=(mock_host, "fake-token", mock_host._injected_client),
        ),
        patch("foreman.roles.planner.load_project_instructions", return_value=None),
        patch("foreman.roles.planner._load_planner_prompt", return_value="system prompt"),
        patch(
            "foreman.roles.planner.log_planner_run",
            return_value=Path("/tmp/fake-stats.jsonl"),
        ),
    ):
        result = await _run_planner_core(
            issue_url=f"https://github.com/testowner/myrepo/issues/{issue_number}",
            config=cfg,
            project_name="p",
            worktrees_root=worktrees_root,
            provider=mock_provider,
            identity_registry=identity_registry,
        )

    # Core contract: the commit/push/create surface is fully bypassed when
    # the spec PR already exists — no duplicate, no 422.
    mock_host.open_pull_request.assert_not_called()
    mock_host.commit_files_to_worktree.assert_not_called()
    mock_host.push_branch.assert_not_called()

    # The adopted PR's number flows through to the CLEAN-outcome result.
    assert result.pr.number == 808
    assert result.pr.url == "https://github.com/testowner/myrepo/pull/808"
    assert result.llm_output.confidence == "high"


@pytest.mark.asyncio
async def test_planner_first_run_with_no_existing_pr_creates(tmp_path: Path) -> None:
    """Defensive: the no-existing-PR (first-run) path still commits the
    spec doc, pushes the branch, and opens the PR — the create path is
    unchanged when the probe returns no open spec PR.
    """
    issue_number = 556
    cfg, identity_registry, mock_wt_mgr, mock_repo, mock_host, mock_provider, worktrees_root = (
        _build_planner_scaffold(tmp_path, existing_spec_pr=None, issue_number=issue_number)
    )

    with (
        patch("foreman.roles.planner.WorktreeManager", return_value=mock_wt_mgr),
        patch(
            "foreman.roles.planner.build_role_resources",
            return_value=(mock_host, "fake-token", mock_host._injected_client),
        ),
        patch("foreman.roles.planner.load_project_instructions", return_value=None),
        patch("foreman.roles.planner._load_planner_prompt", return_value="system prompt"),
        patch(
            "foreman.roles.planner.log_planner_run",
            return_value=Path("/tmp/fake-stats.jsonl"),
        ),
    ):
        result = await _run_planner_core(
            issue_url=f"https://github.com/testowner/myrepo/issues/{issue_number}",
            config=cfg,
            project_name="p",
            worktrees_root=worktrees_root,
            provider=mock_provider,
            identity_registry=identity_registry,
        )

    mock_host.commit_files_to_worktree.assert_called_once()
    mock_host.push_branch.assert_called_once_with(
        worktree_path=mock_wt_mgr.create.return_value, branch=f"foreman/issue-{issue_number}"
    )
    mock_host.open_pull_request.assert_called_once()
    create_kwargs = mock_host.open_pull_request.call_args.kwargs
    assert create_kwargs["head"] == f"foreman/issue-{issue_number}"
    assert create_kwargs["base"] == "main"
    # Returned PR reflects the freshly-opened PR, not an adopted one.
    assert result.pr.number == 9000 + issue_number
