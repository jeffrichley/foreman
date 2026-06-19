"""WorkItem — the v4 queue item shape."""
from __future__ import annotations

import pytest

from foreman.v4.work import WorkItem


def test_work_item_carries_ticket_and_state_name():
    item = WorkItem(ticket_id=1, state_name="Planning")
    assert item.ticket_id == 1
    assert item.state_name == "Planning"


def test_work_item_is_hashable_for_dedup():
    a = WorkItem(ticket_id=1, state_name="Planning")
    b = WorkItem(ticket_id=1, state_name="Planning")
    assert a == b
    assert hash(a) == hash(b)


def test_work_item_is_immutable():
    item = WorkItem(ticket_id=1, state_name="Planning")
    with pytest.raises(AttributeError):
        item.ticket_id = 2  # type: ignore[misc]
