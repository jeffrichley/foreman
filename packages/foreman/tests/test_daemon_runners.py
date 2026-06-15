"""Tests for DaemonRunners — wraps role functions for the daemon."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from foreman.config import AdminConfig, AppsConfig, Config, DaemonConfig, ProjectConfig
from foreman.daemon_runners import DaemonRunners
from foreman.dispatcher import Ticket


def _ticket(issue: int = 42, labels: set[str] | None = None) -> Ticket:
    return Ticket(
        project_name="voice",
        issue_number=issue,
        labels=frozenset(labels or {"foreman:plan"}),
        last_transition_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


def _config(tmp_path: Path) -> Config:
    # Phase 8d.2: the v3 daemon-runners path now adapts v3 Config →
    # V4Config + V4IdentityRegistry on every dispatch. The adapter calls
    # ``apps.resolve_<role>_app_id()`` for all four roles, which raises
    # ``RuntimeError`` when both the env var and config value are unset.
    # Populate per-role credentials with stub values so the conversion
    # succeeds in tests that never actually mint tokens.
    return Config(
        admin=AdminConfig(),
        daemon=DaemonConfig(sqlite_path=str(tmp_path / "f.sqlite")),
        projects={
            "voice": ProjectConfig(
                repo="jeffrichley/voice",
                local_clone_path=str(tmp_path / "voice"),
                apps=AppsConfig(
                    planner_app_id=1001,
                    planner_private_key_path="/tmp/planner.pem",
                    reviewer_app_id=1002,
                    reviewer_private_key_path="/tmp/reviewer.pem",
                    fixer_app_id=1003,
                    fixer_private_key_path="/tmp/fixer.pem",
                    worker_app_id=1004,
                    worker_private_key_path="/tmp/worker.pem",
                ),
            )
        },
    )


def _make_role_result(
    *, final_labels: list[str], dump: dict | None = None
) -> MagicMock:
    """Build a fake ``*RunResult`` carrying ``final_labels`` + a stub
    ``llm_output`` whose ``model_dump`` returns ``dump`` (default ``{}``).

    The role-mock returns this object; ``DaemonRunners`` reads
    ``result.final_labels`` (the authoritative post-transition set, per
    foreman#91) and ``result.llm_output.model_dump()`` (for the audit
    row). No round-trip through ``host.get_issue_labels``.
    """
    llm_output = MagicMock()
    llm_output.model_dump = MagicMock(return_value=dump if dump is not None else {})
    result = MagicMock()
    result.final_labels = list(final_labels)
    result.llm_output = llm_output
    return result


@pytest.fixture
def host() -> MagicMock:
    """Fake GitHubDaemonHost with the methods the runners use."""
    m = MagicMock()
    # NOTE: ``get_issue_labels`` is intentionally a MagicMock so tests
    # can assert it is NOT called by the runners — foreman#91 moved the
    # post-run label source from a host re-read to role-authoritative
    # ``final_labels``. The method stays on the protocol because
    # reconciliation + the poller still legitimately read labels via
    # other code paths.
    m.get_issue_labels = MagicMock(return_value=[])
    m.find_pr_for_branch = MagicMock(return_value=18)
    m.merge_pull_request = MagicMock()
    m.add_issue_label = MagicMock()
    m.remove_issue_label = MagicMock()
    m.close_issue = MagicMock()
    # Defaults that make the retarget branch in merge_impl_pr a no-op so
    # existing tests don't trip it. Tests that want to exercise the
    # retarget logic override these.
    m.get_pr_base_ref = MagicMock(return_value="main")
    m.is_pr_merged_for_branch = MagicMock(return_value=False)
    m.retarget_pr_base = MagicMock()
    m.get_default_branch = MagicMock(return_value="main")
    return m


@pytest.mark.asyncio
async def test_run_planner_calls_role_with_issue_url(tmp_path: Path, host: MagicMock) -> None:
    config = _config(tmp_path)
    mock_role = AsyncMock(
        return_value=_make_role_result(
            final_labels=["foreman:spec-review"],
            dump={"pr": 18},
        )
    )

    runners = DaemonRunners(
        host=host,
        worktrees_root=tmp_path / "worktrees",
        _planner=mock_role,
    )

    result = await runners.run_planner(ticket=_ticket(), config=config)

    mock_role.assert_awaited_once()
    kwargs = mock_role.await_args.kwargs
    assert kwargs["issue_url"] == "https://github.com/jeffrichley/voice/issues/42"
    assert kwargs["project_name"] == "voice"
    # foreman#91: the new label set comes from the role's ``final_labels``
    # field, not from a host re-read. The host's ``get_issue_labels`` is
    # never consulted on this path.
    assert "foreman:spec-review" in result.new_labels
    host.get_issue_labels.assert_not_called()


@pytest.mark.asyncio
async def test_run_reviewer_with_spec_target_calls_role_with_spec_pr_url(
    tmp_path: Path, host: MagicMock
) -> None:
    config = _config(tmp_path)
    mock_role = AsyncMock(
        return_value=_make_role_result(final_labels=["foreman:spec-ready"])
    )
    host.find_pr_for_branch.return_value = 99

    runners = DaemonRunners(
        host=host,
        worktrees_root=tmp_path / "worktrees",
        _reviewer=mock_role,
    )

    await runners.run_reviewer(ticket=_ticket(), config=config, target="spec_pr")

    kwargs = mock_role.await_args.kwargs
    assert kwargs["pr_url"] == "https://github.com/jeffrichley/voice/pull/99"
    # spec_pr target → looks up PR for branch foreman/issue-42
    host.find_pr_for_branch.assert_called_once_with("jeffrichley/voice", "foreman/issue-42")


@pytest.mark.asyncio
async def test_run_reviewer_with_impl_target_uses_impl_branch(
    tmp_path: Path, host: MagicMock
) -> None:
    config = _config(tmp_path)
    mock_role = AsyncMock(
        return_value=_make_role_result(final_labels=["foreman:ready-for-merge"])
    )
    host.find_pr_for_branch.return_value = 100

    runners = DaemonRunners(
        host=host,
        worktrees_root=tmp_path / "worktrees",
        _reviewer=mock_role,
    )

    await runners.run_reviewer(ticket=_ticket(), config=config, target="impl_pr")

    # impl_pr target → looks up PR for branch foreman/impl-42
    host.find_pr_for_branch.assert_called_once_with(
        "jeffrichley/voice", "foreman/impl-42"
    )


@pytest.mark.asyncio
async def test_run_reviewer_raises_when_no_pr_for_branch(
    tmp_path: Path, host: MagicMock
) -> None:
    config = _config(tmp_path)
    mock_role = AsyncMock()
    host.find_pr_for_branch.return_value = None

    runners = DaemonRunners(
        host=host,
        worktrees_root=tmp_path / "worktrees",
        _reviewer=mock_role,
    )

    with pytest.raises(RuntimeError, match="No open PR found for branch"):
        await runners.run_reviewer(ticket=_ticket(), config=config, target="spec_pr")
    # Role NOT called because we couldn't find the PR.
    mock_role.assert_not_called()


@pytest.mark.asyncio
async def test_run_reviewer_uses_role_final_labels_not_host_reread(
    tmp_path: Path, host: MagicMock
) -> None:
    """foreman#91: ``DaemonRunners.run_reviewer`` must populate
    ``new_labels`` from the role's ``final_labels`` field, not via a
    fresh ``host.get_issue_labels`` GET that races GitHub's eventual-
    consistency window.

    Setup: stub ``host.get_issue_labels`` to return a STALE label set
    (the bug scenario — the GET happened just after the role's write
    and returned the OLD labels). Stub the reviewer role to return a
    wrapper whose ``final_labels`` is the FRESH set the role actually
    wrote. The runner must trust the role's value, not the host's.
    """
    config = _config(tmp_path)
    host.find_pr_for_branch.return_value = 99
    # STALE — the bug scenario.
    host.get_issue_labels.return_value = ["foreman:impl-review"]
    # FRESH — the role-authoritative truth.
    mock_role = AsyncMock(
        return_value=_make_role_result(final_labels=["foreman:ready-for-merge"])
    )

    runners = DaemonRunners(
        host=host,
        worktrees_root=tmp_path / "worktrees",
        _reviewer=mock_role,
    )

    result = await runners.run_reviewer(
        ticket=_ticket(labels={"foreman:impl-review"}),
        config=config,
        target="impl_pr",
    )

    assert result.new_labels == frozenset({"foreman:ready-for-merge"})
    # The post-mutation host re-read MUST be gone — that's the whole fix.
    host.get_issue_labels.assert_not_called()


@pytest.mark.asyncio
async def test_run_fixer_forwards_target_to_role_function(
    tmp_path: Path, host: MagicMock
) -> None:
    """foreman#79: ``DaemonRunners.run_fixer`` must forward ``target``
    to the role function. Without this, the role defaults to
    ``target='spec_pr'``, the precondition gate rejects
    ``foreman:impl-fix``-labeled issues, and the autonomous
    Fixer-on-impl flow stalls forever."""
    config = _config(tmp_path)
    mock_role = AsyncMock(
        return_value=_make_role_result(final_labels=["foreman:impl-review"])
    )

    runners = DaemonRunners(
        host=host,
        worktrees_root=tmp_path / "worktrees",
        _fixer=mock_role,
    )

    await runners.run_fixer(ticket=_ticket(), config=config, target="impl_pr")

    kwargs = mock_role.await_args.kwargs
    assert kwargs["target"] == "impl_pr"
    assert kwargs["issue_url"] == "https://github.com/jeffrichley/voice/issues/42"


@pytest.mark.asyncio
async def test_merge_spec_pr_merges_and_sets_implementing_ready(
    tmp_path: Path, host: MagicMock
) -> None:
    config = _config(tmp_path)
    host.find_pr_for_branch.return_value = 18

    runners = DaemonRunners(host=host, worktrees_root=tmp_path / "worktrees")

    result = await runners.merge_spec_pr(
        ticket=_ticket(labels={"foreman:spec-ready"}), config=config
    )

    host.find_pr_for_branch.assert_called_once_with("jeffrichley/voice", "foreman/issue-42")
    host.merge_pull_request.assert_called_once_with("jeffrichley/voice", 18)
    host.add_issue_label.assert_called_once_with(
        "jeffrichley/voice", 42, "foreman:implementing-ready"
    )
    # Remove any prior spec-state labels that linger.
    host.remove_issue_label.assert_any_call("jeffrichley/voice", 42, "foreman:spec-ready")
    assert "foreman:implementing-ready" in result.new_labels


@pytest.mark.asyncio
async def test_merge_spec_pr_uses_deterministic_labels_not_host_reread(
    tmp_path: Path, host: MagicMock
) -> None:
    """foreman#91: ``DaemonRunners.merge_spec_pr`` computes
    ``new_labels`` deterministically from ``ticket.labels`` + the merge
    transitions (drop ``foreman:spec-ready``, add
    ``foreman:implementing-ready``). The post-mutation host re-read is
    gone.
    """
    config = _config(tmp_path)
    host.find_pr_for_branch.return_value = 18
    # STALE — would have been read by the old code path. Assert below
    # that this value is NOT what populates ``new_labels``.
    host.get_issue_labels.return_value = ["foreman:spec-ready"]

    runners = DaemonRunners(host=host, worktrees_root=tmp_path / "worktrees")

    result = await runners.merge_spec_pr(
        ticket=_ticket(labels={"foreman:spec-ready"}), config=config
    )

    assert result.new_labels == frozenset({"foreman:implementing-ready"})
    host.get_issue_labels.assert_not_called()


@pytest.mark.asyncio
async def test_merge_impl_pr_merges_and_closes_issue(
    tmp_path: Path, host: MagicMock
) -> None:
    config = _config(tmp_path)
    host.find_pr_for_branch.return_value = 25

    runners = DaemonRunners(host=host, worktrees_root=tmp_path / "worktrees")

    result = await runners.merge_impl_pr(
        ticket=_ticket(labels={"foreman:ready-for-merge"}), config=config
    )

    host.find_pr_for_branch.assert_called_once_with(
        "jeffrichley/voice", "foreman/impl-42"
    )
    host.merge_pull_request.assert_called_once_with("jeffrichley/voice", 25)
    host.close_issue.assert_called_once_with("jeffrichley/voice", 42)
    assert result.new_labels == frozenset()


@pytest.mark.asyncio
async def test_merge_impl_pr_uses_deterministic_labels_not_host_reread(
    tmp_path: Path, host: MagicMock
) -> None:
    """foreman#91 symmetric: ``merge_impl_pr`` computes ``new_labels``
    deterministically from ``ticket.labels`` minus
    ``foreman:ready-for-merge``. The issue is closed, so the resulting
    set is empty — ``next_action`` will return ``None`` and the queue
    parks the ticket (correct terminal state).
    """
    config = _config(tmp_path)
    host.find_pr_for_branch.return_value = 25
    host.get_issue_labels.return_value = ["foreman:ready-for-merge"]

    runners = DaemonRunners(host=host, worktrees_root=tmp_path / "worktrees")

    result = await runners.merge_impl_pr(
        ticket=_ticket(labels={"foreman:ready-for-merge"}), config=config
    )

    assert result.new_labels == frozenset()
    host.get_issue_labels.assert_not_called()


@pytest.mark.asyncio
async def test_merge_spec_pr_raises_when_no_pr_found(
    tmp_path: Path, host: MagicMock
) -> None:
    config = _config(tmp_path)
    host.find_pr_for_branch.return_value = None

    runners = DaemonRunners(host=host, worktrees_root=tmp_path / "worktrees")

    with pytest.raises(RuntimeError, match="No open spec PR"):
        await runners.merge_spec_pr(ticket=_ticket(), config=config)
    host.merge_pull_request.assert_not_called()


@pytest.mark.asyncio
async def test_merge_impl_pr_retargets_base_when_spec_pr_merged(
    tmp_path: Path, host: MagicMock
) -> None:
    """The retarget step (issue #62): when the impl PR's base is still the
    spec branch AND the spec PR has already merged, retarget the impl PR
    to the default branch BEFORE calling merge — otherwise GitHub would
    squash impl commits onto the about-to-be-deleted spec branch.
    """
    config = _config(tmp_path)
    host.find_pr_for_branch.return_value = 25
    # Impl PR is still pointing at the spec branch (the Worker creates it
    # this way per the stacked-PR pattern).
    host.get_pr_base_ref.return_value = "foreman/issue-42"
    # And the spec PR has already merged.
    host.is_pr_merged_for_branch.return_value = True
    host.get_default_branch.return_value = "main"

    runners = DaemonRunners(host=host, worktrees_root=tmp_path / "worktrees")

    await runners.merge_impl_pr(ticket=_ticket(), config=config)

    host.retarget_pr_base.assert_called_once_with("jeffrichley/voice", 25, "main")
    host.merge_pull_request.assert_called_once_with("jeffrichley/voice", 25)
    # Ordering — retarget BEFORE merge.
    call_names = [c[0] for c in host.method_calls]
    assert call_names.index("retarget_pr_base") < call_names.index(
        "merge_pull_request"
    )


@pytest.mark.asyncio
async def test_merge_impl_pr_skips_retarget_when_spec_pr_still_open(
    tmp_path: Path, host: MagicMock
) -> None:
    """When the spec PR has NOT yet merged, we must NOT retarget — landing
    impl changes on main that depend on un-landed spec changes would
    break the build. Merge proceeds against the spec branch (the
    stacked-PR pattern's "spec hasn't landed yet" case).
    """
    config = _config(tmp_path)
    host.find_pr_for_branch.return_value = 25
    host.get_pr_base_ref.return_value = "foreman/issue-42"
    # Spec PR is still open (not yet merged).
    host.is_pr_merged_for_branch.return_value = False

    runners = DaemonRunners(host=host, worktrees_root=tmp_path / "worktrees")

    await runners.merge_impl_pr(ticket=_ticket(), config=config)

    host.retarget_pr_base.assert_not_called()
    # We still call merge_pull_request — Foreman does not short-circuit;
    # the impl commits land on the spec branch, which preserves them.
    host.merge_pull_request.assert_called_once_with("jeffrichley/voice", 25)


@pytest.mark.asyncio
async def test_merge_impl_pr_skips_retarget_when_base_already_default(
    tmp_path: Path, host: MagicMock
) -> None:
    """Idempotency: if an operator (or a prior crashed run) already
    retargeted the impl PR to main, we skip the retarget and merge
    directly. The daemon may re-enqueue this action on crash, so this
    path matters.
    """
    config = _config(tmp_path)
    host.find_pr_for_branch.return_value = 25
    # Already on the default branch.
    host.get_pr_base_ref.return_value = "main"
    # is_pr_merged_for_branch should not even be consulted, but make it
    # True just to be sure we're skipping based on base.ref, not on the
    # spec PR's merge state.
    host.is_pr_merged_for_branch.return_value = True

    runners = DaemonRunners(host=host, worktrees_root=tmp_path / "worktrees")

    await runners.merge_impl_pr(ticket=_ticket(), config=config)

    host.retarget_pr_base.assert_not_called()
    host.merge_pull_request.assert_called_once_with("jeffrichley/voice", 25)
