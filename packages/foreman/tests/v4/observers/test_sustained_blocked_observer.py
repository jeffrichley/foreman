"""Unit tests for :class:`SustainedBlockedObserver`."""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest

from foreman.v4.events import ExecuteCompletedEvent
from foreman.v4.observers.sustained_blocked import (
    SUSTAINED_BLOCKED_THRESHOLD,
    SustainedBlockedObserver,
)
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.records import StateInstanceRecord, TicketRecord


def _record(
    *,
    seq: int,
    state_name: str = "Implementing",
    outcome_kind: OutcomeKind | None = None,
    outcome_payload: dict | None = None,
    execute_completed_at: dt.datetime | None = None,
) -> StateInstanceRecord:
    base = dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.UTC)
    return StateInstanceRecord(
        id=100 + seq,
        ticket_id=1,
        state_name=state_name,
        sequence=seq,
        entered_at=base,
        execute_started_at=base,
        execute_completed_at=execute_completed_at,
        exited_at=base,
        outcome_kind=outcome_kind,
        outcome_payload=outcome_payload,
        next_state=None,
        failure_phase=None,
        failure_reason=None,
    )


def _outcome(summary: str = "impl PR open, CI in flight") -> Outcome:
    return Outcome(
        kind=OutcomeKind.BLOCKED,
        confidence=OutcomeConfidence.HIGH,
        summary=summary,
    )


def _event(
    *, at: dt.datetime, outcome: Outcome, state_name: str = "Implementing",
) -> ExecuteCompletedEvent:
    return ExecuteCompletedEvent(
        at=at,
        ticket_id=1,
        instance_id=99,
        state_name=state_name,
        sequence=10,
        outcome=outcome,
        next_state=state_name,
    )


def _repo(history: list[StateInstanceRecord]) -> MagicMock:
    repo = MagicMock()
    repo.list_state_instances_for_ticket.return_value = history
    repo.get_ticket.return_value = TicketRecord(
        id=1, project="proj", issue_number=42,
        current_state="Implementing",
        created_at=dt.datetime(2026, 6, 20, tzinfo=dt.UTC),
        updated_at=dt.datetime(2026, 6, 20, tzinfo=dt.UTC),
        held_by=None, held_at=None, held_reason=None,
        depends_on=[],
    )
    return repo


def _host_factory(
    host: MagicMock | None, *, slug: str = "owner/repo",
) -> object:
    def resolver(project: str) -> str:
        return slug
    callable_obj = MagicMock(return_value=host)
    callable_obj.repo_slug_for = resolver
    return callable_obj


# ---------------------------------------------------------------------------


def test_first_blocked_event_below_threshold_posts_nothing() -> None:
    now = dt.datetime(2026, 6, 20, 12, 5, 0, tzinfo=dt.UTC)
    five_min_ago = now - dt.timedelta(minutes=5)
    history = [
        _record(
            seq=1,
            outcome_kind=OutcomeKind.BLOCKED,
            outcome_payload={"summary": "impl PR open, CI in flight"},
            execute_completed_at=five_min_ago,
        ),
    ]
    repo = _repo(history)
    host = MagicMock()
    host.get_issue_comments.return_value = []
    obs = SustainedBlockedObserver(
        repo=repo,
        host_for_project=_host_factory(host),
        clock=lambda: now,
    )
    obs(_event(at=now, outcome=_outcome()))
    host.post_issue_comment.assert_not_called()


def test_sustained_blocked_above_threshold_posts_once() -> None:
    now = dt.datetime(2026, 6, 20, 12, 16, 0, tzinfo=dt.UTC)
    sixteen_min_ago = now - dt.timedelta(minutes=16)
    # Simulate 32 BLOCKED events 30s apart: only the threshold cross
    # matters, but we structure the history to mirror the spec's
    # "32 ticks worth, not one comment per tick" guard.
    history: list[StateInstanceRecord] = []
    payload = {"summary": "impl PR open, CI in flight"}
    for i in range(32):
        history.append(_record(
            seq=i + 1,
            outcome_kind=OutcomeKind.BLOCKED,
            outcome_payload=payload,
            execute_completed_at=sixteen_min_ago + dt.timedelta(seconds=30 * i),
        ))
    repo = _repo(history)
    host = MagicMock()
    host.get_issue_comments.return_value = []
    obs = SustainedBlockedObserver(
        repo=repo,
        host_for_project=_host_factory(host),
        clock=lambda: now,
    )
    # Fire the latest event (cycle 32). The observer scans backward,
    # finds the contiguous BLOCKED suffix, and posts.
    obs(_event(at=now, outcome=_outcome()))
    assert host.post_issue_comment.call_count == 1


def test_clean_outcome_resets_the_run() -> None:
    now = dt.datetime(2026, 6, 20, 12, 30, 0, tzinfo=dt.UTC)
    base = now - dt.timedelta(minutes=30)
    # BLOCKED → BLOCKED → CLEAN → BLOCKED. The CLEAN row breaks the
    # contiguous BLOCKED suffix, so the observer's scan stops at the
    # third row (CLEAN) and only considers the last BLOCKED row.
    history = [
        _record(
            seq=1,
            outcome_kind=OutcomeKind.BLOCKED,
            outcome_payload={"summary": "impl PR open, CI in flight"},
            execute_completed_at=base,
        ),
        _record(
            seq=2,
            outcome_kind=OutcomeKind.BLOCKED,
            outcome_payload={"summary": "impl PR open, CI in flight"},
            execute_completed_at=base + dt.timedelta(minutes=5),
        ),
        _record(
            seq=3,
            outcome_kind=OutcomeKind.CLEAN,
            outcome_payload={"summary": "ok"},
            execute_completed_at=base + dt.timedelta(minutes=10),
        ),
        _record(
            seq=4,
            outcome_kind=OutcomeKind.BLOCKED,
            outcome_payload={"summary": "impl PR open, CI in flight"},
            execute_completed_at=now - dt.timedelta(minutes=1),
        ),
    ]
    repo = _repo(history)
    host = MagicMock()
    host.get_issue_comments.return_value = []
    obs = SustainedBlockedObserver(
        repo=repo,
        host_for_project=_host_factory(host),
        clock=lambda: now,
    )
    obs(_event(at=now, outcome=_outcome()))
    # The most recent BLOCKED is 1 minute ago → below threshold → no
    # post.
    host.post_issue_comment.assert_not_called()


def test_distinct_reasons_each_get_one_comment() -> None:
    now = dt.datetime(2026, 6, 20, 13, 30, 0, tzinfo=dt.UTC)
    base = now - dt.timedelta(minutes=60)
    history = [
        _record(
            seq=1,
            outcome_kind=OutcomeKind.BLOCKED,
            outcome_payload={"summary": "impl PR open, CI in flight"},
            execute_completed_at=base,
        ),
        _record(
            seq=2,
            outcome_kind=OutcomeKind.BLOCKED,
            outcome_payload={"summary": "impl PR open, CI in flight"},
            execute_completed_at=base + dt.timedelta(minutes=16),
        ),
    ]
    repo = _repo(history)
    host = MagicMock()
    host.get_issue_comments.return_value = []
    obs = SustainedBlockedObserver(
        repo=repo,
        host_for_project=_host_factory(host),
        clock=lambda: now,
    )
    # First event: 16 min of CI-in-flight crosses threshold.
    first_event_at = base + dt.timedelta(minutes=16)
    obs(_event(at=first_event_at, outcome=_outcome()))
    assert host.post_issue_comment.call_count == 1
    # Now a different reason cycle: distinct summary signal forms a
    # new (ticket, reason) pair, gets its own comment. Second reason
    # spans from minute 20 to minute 40 → 20 min on the same signal.
    history2 = list(history) + [
        _record(
            seq=3,
            outcome_kind=OutcomeKind.BLOCKED,
            outcome_payload={"summary": "impl PR not yet mergeable"},
            execute_completed_at=base + dt.timedelta(minutes=20),
        ),
        _record(
            seq=4,
            outcome_kind=OutcomeKind.BLOCKED,
            outcome_payload={"summary": "impl PR not yet mergeable"},
            execute_completed_at=base + dt.timedelta(minutes=40),
        ),
    ]
    repo.list_state_instances_for_ticket.return_value = history2
    host2 = MagicMock()
    host2.get_issue_comments.return_value = []
    obs2 = SustainedBlockedObserver(
        repo=repo,
        host_for_project=_host_factory(host2),
        clock=lambda: now,
    )
    second_event_at = base + dt.timedelta(minutes=40)
    obs2(_event(at=second_event_at, outcome=_outcome("impl PR not yet mergeable")))
    # The reason hash differs → distinct dedup key → post fires.
    assert host2.post_issue_comment.call_count == 1


def test_handler_swallows_exceptions() -> None:
    repo = MagicMock()
    repo.list_state_instances_for_ticket.side_effect = RuntimeError("boom")
    obs = SustainedBlockedObserver(
        repo=repo,
        host_for_project=_host_factory(None),
    )
    # Should not raise; observer wraps handler.
    obs(_event(
        at=dt.datetime(2026, 6, 20, tzinfo=dt.UTC),
        outcome=_outcome(),
    ))


def test_non_blocked_event_is_ignored() -> None:
    repo = _repo([])
    host = MagicMock()
    obs = SustainedBlockedObserver(
        repo=repo,
        host_for_project=_host_factory(host),
    )
    obs(_event(
        at=dt.datetime(2026, 6, 20, tzinfo=dt.UTC),
        outcome=Outcome(
            kind=OutcomeKind.CLEAN,
            confidence=OutcomeConfidence.HIGH,
            summary="ok",
        ),
    ))
    host.get_issue_comments.assert_not_called()
    host.post_issue_comment.assert_not_called()


@pytest.mark.parametrize("threshold", [
    dt.timedelta(minutes=15),
    dt.timedelta(seconds=0.001),
])
def test_threshold_is_configurable(threshold: dt.timedelta) -> None:
    assert isinstance(SUSTAINED_BLOCKED_THRESHOLD, dt.timedelta)
    # Confirm the observer accepts the threshold override.
    repo = _repo([])
    obs = SustainedBlockedObserver(
        repo=repo,
        host_for_project=_host_factory(MagicMock()),
        threshold=threshold,
    )
    assert obs._threshold == threshold
