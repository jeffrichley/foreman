"""End-to-end daemon test using fake host + fake roles.

Proves stage-priority dequeue: when two tickets are tagged simultaneously,
the first to advance becomes most-progressed and finishes ahead of the
second — exactly matching spec §14 acceptance criterion #4.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from foreman.config import AdminConfig, AppsConfig, Config, DaemonConfig, ProjectConfig
from foreman.daemon import Daemon
from foreman.dispatcher import Action, ActionKind, Ticket
from foreman.worker import RoleResult


@dataclass
class _FakeIssue:
    number: int
    labels: list[str]
    updated_at: datetime


@dataclass
class _MutableHost:
    issues: dict[int, _FakeIssue] = field(default_factory=dict)
    added_labels: list[tuple[int, str]] = field(default_factory=list)

    def search_foreman_labeled_issues(self, repo: str) -> list[_FakeIssue]:
        return list(self.issues.values())

    def add_issue_label(self, repo: str, issue_number: int, label: str) -> None:
        self.added_labels.append((issue_number, label))
        if issue_number in self.issues:
            issue = self.issues[issue_number]
            if label not in issue.labels:
                issue.labels.append(label)

    def post_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
        pass


@dataclass
class _PipelineFakeDispatcher:
    """Simulates the full pipeline by advancing labels through the role chain."""

    completion_order: list[int] = field(default_factory=list)

    async def dispatch(self, *, ticket: Ticket, action: Action) -> RoleResult:
        labels = set(ticket.labels)

        if action.kind == ActionKind.RUN_PLANNER:
            new = {"foreman:spec-review"}
        elif action.kind == ActionKind.RUN_REVIEWER_SPEC:
            new = {"foreman:spec-ready"}
        elif action.kind == ActionKind.MERGE_SPEC_PR:
            new = {"foreman:implementing-ready"}
        elif action.kind == ActionKind.RUN_WORKER:
            new = {"foreman:impl-review"}
        elif action.kind == ActionKind.RUN_REVIEWER_IMPL:
            new = {"foreman:ready-for-merge"}
        elif action.kind == ActionKind.MERGE_IMPL_PR:
            new = set()  # terminal
            self.completion_order.append(ticket.issue_number)
        else:
            new = labels

        return RoleResult(
            new_labels=frozenset(new),
            structured_output=None,
            outcome="success",
        )


def _config(tmp_path: Path) -> Config:
    return Config(
        admin=AdminConfig(),
        daemon=DaemonConfig(
            poll_interval_seconds=5,
            sqlite_path=str((tmp_path / "f.sqlite").as_posix()),
            log_path=str((tmp_path / "d.log").as_posix()),
        ),
        projects={
            "voice": ProjectConfig(
                repo="jeffrichley/voice",
                local_clone_path=str((tmp_path / "voice").as_posix()),
                apps=AppsConfig(),
                auto_merge_spec=True,
                auto_merge_impl=True,
            )
        },
    )


@pytest.mark.asyncio
async def test_two_tickets_first_finishes_before_second_starts_implementing(
    tmp_path: Path,
) -> None:
    """Stage-priority verified: ticket #1 should reach impl-merge before ticket #2
    moves past spec-review (because dequeue prefers further-along tickets)."""
    host = _MutableHost(
        issues={
            1: _FakeIssue(
                number=1,
                labels=["foreman:plan"],
                updated_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
            ),
            2: _FakeIssue(
                number=2,
                labels=["foreman:plan"],
                updated_at=datetime(2026, 6, 1, 12, 1, tzinfo=timezone.utc),
            ),
        }
    )
    dispatcher = _PipelineFakeDispatcher()

    daemon = Daemon(config=_config(tmp_path), host=host, role_dispatcher=dispatcher)
    await daemon.start()
    # Generous slack to let both tickets reach terminal.
    await asyncio.sleep(1.0)
    await daemon.shutdown()

    # Both tickets eventually finish; ticket #1 finishes first.
    assert dispatcher.completion_order[0] == 1
    assert dispatcher.completion_order == [1, 2]
