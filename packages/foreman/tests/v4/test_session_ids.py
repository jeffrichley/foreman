"""Tests for deterministic session_id derivation (foreman crash-recovery resume arm).

Pins the two load-bearing properties of ``derive_session_id``:

1. **Stable** — identical args always produce the same id, so a crash re-run
   of the same work computes the same session id and can resume.
2. **Distinct by construction** — changing any one of
   (ticket_id, role, target, run_key) yields a different id, so no role's
   dispatch can ever land on another role/ticket/target's session.
"""

from __future__ import annotations

import uuid

from foreman.v4.session_ids import derive_session_id


def test_returns_valid_uuid_string() -> None:
    result = derive_session_id(
        ticket_id=1,
        role="planner",
        target=None,
        run_key="seq-3",
    )
    assert isinstance(result, str)
    # Parses as a valid UUID (raises ValueError otherwise → test fails).
    uuid.UUID(result)


def test_stable_across_calls() -> None:
    a = derive_session_id(ticket_id=1, role="planner", target=None, run_key="seq-3")
    b = derive_session_id(ticket_id=1, role="planner", target=None, run_key="seq-3")
    assert a == b


def test_distinct_when_ticket_id_changes() -> None:
    base = derive_session_id(ticket_id=1, role="planner", target=None, run_key="seq-3")
    other = derive_session_id(ticket_id=2, role="planner", target=None, run_key="seq-3")
    assert base != other


def test_distinct_when_role_changes() -> None:
    base = derive_session_id(ticket_id=1, role="planner", target=None, run_key="seq-3")
    other = derive_session_id(ticket_id=1, role="fixer", target=None, run_key="seq-3")
    assert base != other


def test_distinct_when_target_changes() -> None:
    base = derive_session_id(ticket_id=1, role="planner", target=None, run_key="seq-3")
    other = derive_session_id(ticket_id=1, role="planner", target="spec", run_key="seq-3")
    assert base != other


def test_distinct_when_run_key_changes() -> None:
    base = derive_session_id(ticket_id=1, role="planner", target=None, run_key="seq-3")
    other = derive_session_id(ticket_id=1, role="planner", target=None, run_key="seq-4")
    assert base != other


def test_target_none_and_spec_differ() -> None:
    none_id = derive_session_id(ticket_id=1, role="planner", target=None, run_key="seq-3")
    spec_id = derive_session_id(ticket_id=1, role="planner", target="spec", run_key="seq-3")
    assert none_id != spec_id
