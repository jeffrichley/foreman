"""FakeRoleDispatcher — canned-stdout for testing concrete states."""
from __future__ import annotations

import pytest

from foreman.v4.role_dispatcher import FakeRoleDispatcher, RoleNotConfiguredError


def test_returns_canned_stdout_for_configured_role():
    dispatcher = FakeRoleDispatcher(
        responses={
            ("planner", "p", 1): "log line\nFOREMAN_OUTCOME:{\"kind\":\"clean\",\"confidence\":\"high\",\"summary\":\"ok\"}\n",
        }
    )
    out = dispatcher.dispatch(role="planner", project="p", issue_number=1, ticket_id=1)
    assert "FOREMAN_OUTCOME:" in out


def test_unconfigured_role_raises():
    dispatcher = FakeRoleDispatcher(responses={})
    with pytest.raises(RoleNotConfiguredError) as exc:
        dispatcher.dispatch(role="planner", project="p", issue_number=1, ticket_id=1)
    assert "planner" in str(exc.value)


def test_dispatch_records_invocation_for_assertion():
    dispatcher = FakeRoleDispatcher(
        responses={
            ("planner", "p", 1): 'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}',
        }
    )
    dispatcher.dispatch(role="planner", project="p", issue_number=1, ticket_id=99)
    assert dispatcher.calls == [("planner", "p", 1, 99)]
