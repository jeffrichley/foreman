"""Tests for the daemon composition — poller + worker async tasks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from foreman.config import AdminConfig, AppsConfig, Config, DaemonConfig, ProjectConfig
from foreman.daemon import Daemon
from foreman.dispatcher import Action, Ticket
from foreman.worker import RoleResult


@dataclass
class _FakeIssue:
    number: int
    labels: list[str]
    updated_at: datetime


@dataclass
class _FakeHost:
    issues_by_query: dict[str, list[_FakeIssue]] = field(default_factory=dict)

    def search_foreman_labeled_issues(self, repo: str) -> list[_FakeIssue]:
        return list(self.issues_by_query.get(repo, []))


@dataclass
class _FakeRoleDispatcher:
    calls: list[tuple[Ticket, Action]] = field(default_factory=list)

    async def dispatch(self, *, ticket: Ticket, action: Action) -> RoleResult:
        self.calls.append((ticket, action))
        return RoleResult(
            new_labels=frozenset({"foreman:spec-ready"}),
            structured_output={"pr_number": 1},
            outcome="success",
        )


def _config(tmp_path: Path) -> Config:
    return Config(
        admin=AdminConfig(),
        daemon=DaemonConfig(
            poll_interval_seconds=5,
            max_concurrent_workers=1,
            sqlite_path=str(tmp_path / "f.sqlite"),
            log_path=str(tmp_path / "daemon.log"),
        ),
        projects={
            "voice": ProjectConfig(
                repo="jeffrichley/voice",
                local_clone_path=str(tmp_path / "voice"),
                apps=AppsConfig(),
            )
        },
    )


@pytest.mark.asyncio
async def test_daemon_polls_and_dispatches_one_ticket(tmp_path: Path) -> None:
    config = _config(tmp_path)
    host = _FakeHost(
        issues_by_query={
            "jeffrichley/voice": [
                _FakeIssue(
                    number=42,
                    labels=["foreman:plan"],
                    updated_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                )
            ]
        }
    )
    dispatcher = _FakeRoleDispatcher()

    daemon = Daemon(config=config, host=host, role_dispatcher=dispatcher)
    await daemon.start()
    # Give the daemon enough wallclock to: do a startup poll, enqueue, run iteration.
    await asyncio.sleep(0.3)
    await daemon.shutdown()

    assert len(dispatcher.calls) == 1
    assert dispatcher.calls[0][0].issue_number == 42


@pytest.mark.asyncio
async def test_daemon_shutdown_is_idempotent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    daemon = Daemon(config=config, host=_FakeHost(), role_dispatcher=_FakeRoleDispatcher())
    await daemon.start()
    await daemon.shutdown()
    await daemon.shutdown()
