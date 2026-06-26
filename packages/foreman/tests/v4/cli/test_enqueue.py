"""enqueue — direct ticket ingest bypassing the Poller.

The Poller scans GitHub for `foreman:plan` labels every ``tick_seconds``
and creates ``Queued`` ticket rows. ``foreman enqueue`` shortcuts that
loop for dogfood + recovery: insert the row directly, let the next
worker tick pick it up. No GitHub round-trip.
"""
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from foreman.v4.cli import app
from foreman.v4.cli.context import build_cli_context
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
from foreman.v4.git_provider import FakeGitProvider
from foreman.v4.repository import InMemoryTicketRepository


def _make_config(tmp_path: Path, project_names: tuple[str, ...] = ("algokit",)) -> V4Config:
    """Build the minimum V4Config the enqueue command needs.

    Only ``projects`` is read; the rest is fake-padding so V4Config validates.
    """
    fake_creds = AppCredentials(app_id=1, private_key_path="/tmp/fake.pem")
    return V4Config(
        storage=StorageConfig(engine="postgres", dsn="postgresql://test/test"),
        log_dir=str(tmp_path / "logs"),
        apps=AppsConfig(
            planner=fake_creds,
            reviewer=fake_creds,
            fixer=fake_creds,
            worker=fake_creds,
        ),
        orchestrator=OrchestratorConfig(
            app_id=2, private_key_path="/tmp/fake-orch.pem",
        ),
        operator=OperatorConfig(
            supervisor=OperatorIdentity(name="Test Sup", email="sup@example.com"),
            signer=OperatorIdentity(name="Test Sign", email="sign@example.com"),
        ),
        projects=[
            ProjectConfig(
                name=name, repo=f"o/{name}",
                local_clone_path=str(tmp_path / name),
            )
            for name in project_names
        ],
    )


def test_enqueue_creates_queued_ticket_in_sqlite(tmp_path: Path) -> None:
    """Happy path: existing project + fresh issue number -> Queued row.

    Asserts no GitProvider methods are called — the whole point is to
    skip the GitHub round-trip. The FakeGitProvider's mutable state
    should be untouched: empty PRs, empty merge queue, empty labels.
    """
    repo = InMemoryTicketRepository()
    git = FakeGitProvider()
    config = _make_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["enqueue", "--project", "algokit", "--issue-number", "21"],
        obj=build_cli_context(repo=repo, git=git, config=config),
    )

    assert result.exit_code == 0, result.output
    tickets = repo.list_all_tickets()
    assert len(tickets) == 1
    assert tickets[0].project == "algokit"
    assert tickets[0].issue_number == 21
    assert tickets[0].current_state == "Queued"
    # New ticket id printed to stdout.
    assert str(tickets[0].id) in result.output

    # GitProvider was not touched (no PRs, no labels, no merges).
    assert git._prs == {}
    assert git.merge_pr_calls == set()
    assert git._labeled_issues == {}
    assert git._issue_labels == {}


def test_enqueue_rejects_duplicate(tmp_path: Path) -> None:
    """Idempotency: second call with same (project, issue) errors loudly.

    The error must name the existing ticket's id + state so the operator
    can investigate (resume? drop? leave alone?) without a separate
    `foreman ps` round-trip.
    """
    repo = InMemoryTicketRepository()
    config = _make_config(tmp_path)
    runner = CliRunner()

    first = runner.invoke(
        app,
        ["enqueue", "--project", "algokit", "--issue-number", "21"],
        obj=build_cli_context(repo=repo, config=config),
    )
    assert first.exit_code == 0, first.output
    existing = repo.get_ticket_by_issue(project="algokit", issue_number=21)

    second = runner.invoke(
        app,
        ["enqueue", "--project", "algokit", "--issue-number", "21"],
        obj=build_cli_context(repo=repo, config=config),
    )
    assert second.exit_code != 0
    # CliRunner in click 8.2+ merges stderr into ``output``.
    assert str(existing.id) in second.output
    assert existing.current_state in second.output
    # No silent duplicate.
    assert len(repo.list_all_tickets()) == 1


def test_enqueue_rejects_unknown_project(tmp_path: Path) -> None:
    """Unknown project name -> non-zero exit + list of configured projects.

    Operator most-likely typo'd; the message should let them fix it
    without re-reading the config TOML.
    """
    repo = InMemoryTicketRepository()
    config = _make_config(tmp_path, project_names=("algokit", "voice"))
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["enqueue", "--project", "not-a-real-project", "--issue-number", "5"],
        obj=build_cli_context(repo=repo, config=config),
    )

    assert result.exit_code != 0
    # CliRunner in click 8.2+ merges stderr into ``output``.
    assert "algokit" in result.output
    assert "voice" in result.output
    # Nothing got inserted.
    assert repo.list_all_tickets() == []
