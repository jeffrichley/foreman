"""Tests for the daemon's in-memory ticket queue."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foreman.config import AppsConfig, ProjectConfig
from foreman.dispatcher import Ticket
from foreman.queue import DaemonQueue


def _ticket(
    project: str = "voice",
    issue: int = 42,
    labels: set[str] | None = None,
    at: datetime | None = None,
) -> Ticket:
    return Ticket(
        project_name=project,
        issue_number=issue,
        labels=frozenset(labels or {"foreman:plan"}),
        last_transition_at=at or datetime(2026, 6, 1, tzinfo=timezone.utc),
    )


def test_enqueue_then_len_one() -> None:
    q = DaemonQueue()
    q.enqueue(_ticket())
    assert len(q) == 1


def test_enqueue_same_ticket_twice_stays_at_one() -> None:
    q = DaemonQueue()
    q.enqueue(_ticket(issue=42, labels={"foreman:plan"}))
    q.enqueue(_ticket(issue=42, labels={"foreman:plan"}))
    assert len(q) == 1


def test_enqueue_overwrites_with_fresh_labels() -> None:
    q = DaemonQueue()
    q.enqueue(_ticket(issue=42, labels={"foreman:plan"}))
    q.enqueue(_ticket(issue=42, labels={"foreman:spec-review"}))
    snapshot = q.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0].labels == frozenset({"foreman:spec-review"})


def test_enqueue_distinct_projects_or_issues_are_separate() -> None:
    q = DaemonQueue()
    q.enqueue(_ticket(project="voice", issue=42))
    q.enqueue(_ticket(project="voice", issue=43))
    q.enqueue(_ticket(project="chrona", issue=42))
    assert len(q) == 3
