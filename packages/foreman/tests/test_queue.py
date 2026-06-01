"""Tests for the daemon's in-memory ticket queue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
        last_transition_at=at or datetime(2026, 6, 1, tzinfo=UTC),
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


def test_dequeue_returns_none_when_empty() -> None:
    q = DaemonQueue()
    project = ProjectConfig(repo="r", local_clone_path="/tmp", apps=AppsConfig())
    assert q.dequeue({"voice": project}) is None


def test_dequeue_returns_further_along_ticket_over_earlier() -> None:
    # Ticket A is at Planner stage; B is at Reviewer stage. B should win.
    q = DaemonQueue()
    base = datetime(2026, 6, 1, tzinfo=UTC)
    q.enqueue(_ticket(project="voice", issue=1, labels={"foreman:plan"}, at=base))
    q.enqueue(_ticket(project="voice", issue=2, labels={"foreman:spec-review"}, at=base))

    project = ProjectConfig(repo="r", local_clone_path="/tmp", apps=AppsConfig())
    next_t = q.dequeue({"voice": project})
    assert next_t is not None
    assert next_t.issue_number == 2  # the further-along ticket


def test_dequeue_fifo_within_same_stage() -> None:
    # Both at Planner stage; older transition timestamp wins.
    q = DaemonQueue()
    base = datetime(2026, 6, 1, tzinfo=UTC)
    q.enqueue(_ticket(project="voice", issue=1, labels={"foreman:plan"}, at=base))
    q.enqueue(
        _ticket(
            project="voice",
            issue=2,
            labels={"foreman:plan"},
            at=base + timedelta(minutes=5),
        )
    )

    project = ProjectConfig(repo="r", local_clone_path="/tmp", apps=AppsConfig())
    next_t = q.dequeue({"voice": project})
    assert next_t is not None
    assert next_t.issue_number == 1  # older transition wins (FIFO)


def test_dequeue_removes_ticket_from_queue() -> None:
    q = DaemonQueue()
    q.enqueue(_ticket())
    project = ProjectConfig(repo="r", local_clone_path="/tmp", apps=AppsConfig())
    q.dequeue({"voice": project})
    assert len(q) == 0


def test_dequeue_skips_parked_tickets_returns_none() -> None:
    q = DaemonQueue()
    q.enqueue(_ticket(labels={"foreman:plan", "foreman:hold"}))
    project = ProjectConfig(repo="r", local_clone_path="/tmp", apps=AppsConfig())
    # Parked tickets stay in the queue but dequeue returns None;
    # they leave only when their labels change (poller picks up the diff).
    assert q.dequeue({"voice": project}) is None
    assert len(q) == 1


def test_dequeue_skips_parked_and_returns_actionable_next() -> None:
    q = DaemonQueue()
    base = datetime(2026, 6, 1, tzinfo=UTC)
    q.enqueue(_ticket(project="voice", issue=1, labels={"foreman:plan", "foreman:hold"}, at=base))
    q.enqueue(_ticket(project="voice", issue=2, labels={"foreman:plan"}, at=base))

    project = ProjectConfig(repo="r", local_clone_path="/tmp", apps=AppsConfig())
    next_t = q.dequeue({"voice": project})
    assert next_t is not None
    assert next_t.issue_number == 2  # the unblocked one
