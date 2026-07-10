"""LabelObservabilityObserver — granular add/remove per state transition.

Phase 8c.4 switched the observer from ``write_labels(set)`` (which
replaced the entire label set via PyGithub's ``set_labels`` and
stripped the trigger label) to ``add_labels`` / ``remove_labels``
across StateEntered / StateExited events. These tests pin the
preservation behavior — especially that ``foreman:plan`` is never in
any writer call, which is the bug that wedged the 2026-06-15 dogfood.
"""
from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.events import (
    ExecuteStartedEvent,
    StateEnteredEvent,
    StateExitedEvent,
)
from foreman.v4.observers.label_observability import LabelObservabilityObserver
from foreman.v4.repository import InMemoryTicketRepository


class _RecordingWriter:
    """Records every add_labels / remove_labels call for assertion.

    The Protocol expects keyword-only args; we record them in a
    discriminated tuple so a single assertion can cover ordering AND
    op-kind across a multi-transition sequence."""

    def __init__(self) -> None:
        self.add_calls: list[tuple[str, int, set[str]]] = []
        self.remove_calls: list[tuple[str, int, set[str]]] = []
        # Ordered (op, project, issue_number, labels) for sequence
        # assertions — lets test_observer_does_not_touch_trigger_label
        # scan every call across both ops in one pass.
        self.all_calls: list[tuple[str, str, int, set[str]]] = []

    def add_labels(
        self, *, project: str, issue_number: int, labels: set[str]
    ) -> None:
        self.add_calls.append((project, issue_number, set(labels)))
        self.all_calls.append(("add", project, issue_number, set(labels)))

    def remove_labels(
        self, *, project: str, issue_number: int, labels: set[str]
    ) -> None:
        self.remove_calls.append((project, issue_number, set(labels)))
        self.all_calls.append(("remove", project, issue_number, set(labels)))


_T0 = dt.datetime(2026, 6, 15)


def _make_repo_and_ticket(state_name: str = "Planning"):
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="foreman", issue_number=42, now=_T0)
    repo.set_ticket_state(ticket.id, state_name, now=_T0)
    return repo, repo.get_ticket(ticket.id)


def test_observer_adds_state_label_on_state_entered() -> None:
    """StateEntered triggers add_labels with the new state's label.

    Uses ``SpecReview`` (a mid-lifecycle, non-first state) so the
    trigger-label removal from Phase 8d.8 is out of scope and the
    assertion stays focused on the add-side behavior. The first-state
    trigger-label cases are covered separately by
    ``test_observer_removes_trigger_label_on_first_state_transition`` and
    ``test_observer_does_not_remove_trigger_label_on_later_transitions``.
    """
    repo, ticket = _make_repo_and_ticket("SpecReview")
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)
    obs(
        StateEnteredEvent(
            ticket_id=ticket.id,
            instance_id=99,
            state_name="SpecReview",
            sequence=1,
            at=_T0,
        )
    )
    assert writer.add_calls == [("foreman", 42, {"foreman:state-specreview"})]
    assert writer.remove_calls == []


def test_observer_removes_state_label_on_state_exited() -> None:
    """StateExited triggers remove_labels with the exited state's label."""
    repo, ticket = _make_repo_and_ticket("Planning")
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)
    obs(
        StateExitedEvent(
            ticket_id=ticket.id,
            instance_id=99,
            state_name="Planning",
            sequence=1,
            at=_T0,
            outcome=None,
        )
    )
    assert writer.remove_calls == [("foreman", 42, {"foreman:state-planning"})]
    assert writer.add_calls == []


def test_state_label_lowercases_state_name() -> None:
    """SpecReview → foreman:state-specreview (lowercase). Same shape on
    both add and remove paths."""
    repo, ticket = _make_repo_and_ticket("SpecReview")
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)
    obs(
        StateEnteredEvent(
            ticket_id=ticket.id,
            instance_id=99,
            state_name="SpecReview",
            sequence=1,
            at=_T0,
        )
    )
    obs(
        StateExitedEvent(
            ticket_id=ticket.id,
            instance_id=99,
            state_name="SpecReview",
            sequence=1,
            at=_T0,
            outcome=None,
        )
    )
    assert writer.add_calls[0][2] == {"foreman:state-specreview"}
    assert writer.remove_calls[0][2] == {"foreman:state-specreview"}


def test_ignores_non_lifecycle_events() -> None:
    """Observer only acts on StateEnteredEvent + StateExitedEvent.
    Other events (ExecuteStarted, ExecuteCompleted, StateFailed) are
    silently ignored — they don't change which state-progress label
    should be on the issue."""
    repo, ticket = _make_repo_and_ticket()
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)
    obs(
        ExecuteStartedEvent(
            ticket_id=ticket.id,
            instance_id=99,
            state_name="Planning",
            sequence=1,
            at=_T0,
        )
    )
    assert writer.add_calls == []
    assert writer.remove_calls == []


def test_observer_does_not_touch_trigger_label() -> None:
    """The defining test for Phase 8c.4.

    Simulates a Planning → SpecReview transition sequence
    (StateExited Planning, StateEntered SpecReview). Asserts that
    ``foreman:plan`` appears in NO add_labels and NO remove_labels
    call. This is the trigger-label preservation that the morning's
    dogfood wedged 40min waiting on — if the daemon crashed after
    Phase 8.1's set_labels replaced the entire label set, the Poller
    couldn't re-find the ticket because foreman:plan was gone."""
    repo, ticket = _make_repo_and_ticket("SpecReview")
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)

    # Full transition: exit Planning, enter SpecReview.
    obs(
        StateExitedEvent(
            ticket_id=ticket.id,
            instance_id=98,
            state_name="Planning",
            sequence=1,
            at=_T0,
            outcome=None,
        )
    )
    obs(
        StateEnteredEvent(
            ticket_id=ticket.id,
            instance_id=99,
            state_name="SpecReview",
            sequence=2,
            at=_T0,
        )
    )

    # Every single call's label set is inspected — the trigger label
    # must NEVER appear in any of them, on either op.
    for op, _, _, labels in writer.all_calls:
        assert "foreman:plan" not in labels, (
            f"foreman:plan leaked into {op}_labels call: {labels}"
        )

    # Sanity: the observer DID do its job (added new, removed old).
    assert writer.add_calls == [("foreman", 42, {"foreman:state-specreview"})]
    assert writer.remove_calls == [("foreman", 42, {"foreman:state-planning"})]


def test_observer_removes_trigger_label_on_first_state_transition() -> None:
    """Phase 8d.8 — observer removes ``foreman:plan`` when the ticket enters
    its first state.

    The Poller adopts a ticket once it sees ``foreman:plan`` and enqueues
    ``WorkItem(state_name="Queued")``. The first ``StateEnteredEvent`` the
    observer sees is therefore ``Queued``. Since 8d.3 stripped the Planner's
    manual trigger-label removal (per "roles don't touch labels"), the
    trigger label is moved into the observer. The observer must, on the
    first-state ``StateEnteredEvent``, call ``remove_labels`` with
    ``foreman:plan`` so the operator-applied trigger is cleared exactly
    once at the start of the lifecycle.
    """
    repo, ticket = _make_repo_and_ticket("Queued")
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)
    obs(
        StateEnteredEvent(
            ticket_id=ticket.id,
            instance_id=99,
            state_name="Queued",
            sequence=1,
            at=_T0,
        )
    )
    # Exactly one remove_labels call, carrying foreman:plan.
    trigger_removals = [
        labels for _project, _issue, labels in writer.remove_calls
        if "foreman:plan" in labels
    ]
    assert len(trigger_removals) == 1, (
        f"expected one remove_labels call with foreman:plan, got: "
        f"{writer.remove_calls!r}"
    )
    assert trigger_removals[0] == {"foreman:plan"}


def test_observer_does_not_remove_trigger_label_on_later_transitions() -> None:
    """Phase 8d.8 — observer only removes ``foreman:plan`` on the first
    state, not on every subsequent ``StateEnteredEvent``.

    On entry to ``SpecReview`` (not a first state), the observer still
    calls ``add_labels`` for the new state's progress label, but no
    ``remove_labels`` call should carry ``foreman:plan`` — only the
    operator-applied trigger removal on the FIRST state is the observer's
    job. Note: the observer still emits ``remove_labels`` separately when
    handling the paired ``StateExitedEvent`` to drop the prior state's
    progress label; this test is scoped to the ``StateEnteredEvent`` for a
    non-first state and asserts ``foreman:plan`` does not appear in any
    labels set.
    """
    repo, ticket = _make_repo_and_ticket("SpecReview")
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)
    obs(
        StateEnteredEvent(
            ticket_id=ticket.id,
            instance_id=99,
            state_name="SpecReview",
            sequence=1,
            at=_T0,
        )
    )
    for _project, _issue, labels in writer.remove_calls:
        assert "foreman:plan" not in labels, (
            f"foreman:plan leaked into remove_labels on a later "
            f"transition: {labels!r}"
        )


def test_observer_stamps_terminal_state_label_on_state_entered() -> None:
    """Phase 8d.12 — observer stamps ``foreman:state-needshelp`` (or
    ``-done`` / ``-failed``) when transition() emits StateEnteredEvent
    for a terminal landing.

    This is the bug surfaced by algokit#21's dogfood: the ticket
    transitioned into NeedsHelp at the state-machine level, but no
    StateEnteredEvent fired for the terminal, so the observer never
    stamped the issue. The fix in state._enter_terminal synthesizes the
    event; this test pins the observer's response to it.
    """
    repo, ticket = _make_repo_and_ticket("NeedsHelp")
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)
    obs(
        StateEnteredEvent(
            ticket_id=ticket.id,
            instance_id=99,
            state_name="NeedsHelp",
            sequence=4,
            at=_T0,
        )
    )
    assert writer.add_calls == [
        ("foreman", 42, {"foreman:state-needshelp"}),
    ]


def test_done_entry_scrubs_sibling_terminal_labels() -> None:
    """StateEntered(Done) removes foreman:state-needshelp + foreman:state-failed.

    Regression for the class of residue found 2026-07-10: tickets that
    transited through NeedsHelp (and/or Failed) accumulated sibling terminal
    labels that were never stripped on the Done completion landing."""
    repo, ticket = _make_repo_and_ticket("Done")
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)
    obs(
        StateEnteredEvent(
            ticket_id=ticket.id,
            instance_id=99,
            state_name="Done",
            sequence=5,
            at=_T0,
        )
    )
    # Exactly one add_labels call for foreman:state-done.
    assert writer.add_calls == [("foreman", 42, {"foreman:state-done"})]
    # remove_labels called with the two sibling terminal labels.
    remove_label_sets = [labels for _proj, _issue, labels in writer.remove_calls]
    assert {"foreman:state-needshelp", "foreman:state-failed"} in remove_label_sets


def test_needshelp_terminal_does_not_scrub_siblings() -> None:
    """StateEntered(NeedsHelp) must NOT remove sibling terminal labels.

    NeedsHelp is a holding pen: the label must stay visible for humans and
    the state-sweep so operators can find and retry the ticket."""
    repo, ticket = _make_repo_and_ticket("NeedsHelp")
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)
    obs(
        StateEnteredEvent(
            ticket_id=ticket.id,
            instance_id=99,
            state_name="NeedsHelp",
            sequence=4,
            at=_T0,
        )
    )
    assert writer.add_calls == [("foreman", 42, {"foreman:state-needshelp"})]
    # No remove call for sibling terminal labels.
    sibling_scrub_calls = [
        labels for _proj, _issue, labels in writer.remove_calls
        if labels & {"foreman:state-needshelp", "foreman:state-failed"}
    ]
    assert sibling_scrub_calls == [], (
        f"NeedsHelp entry must not scrub sibling labels; got: {sibling_scrub_calls!r}"
    )


def test_failed_terminal_does_not_scrub_siblings() -> None:
    """StateEntered(Failed) must NOT remove sibling terminal labels.

    Failed is a terminal with human-actionable context; the label must stay
    visible for operators reviewing the issue."""
    repo, ticket = _make_repo_and_ticket("Failed")
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)
    obs(
        StateEnteredEvent(
            ticket_id=ticket.id,
            instance_id=99,
            state_name="Failed",
            sequence=4,
            at=_T0,
        )
    )
    assert writer.add_calls == [("foreman", 42, {"foreman:state-failed"})]
    sibling_scrub_calls = [
        labels for _proj, _issue, labels in writer.remove_calls
        if labels & {"foreman:state-needshelp", "foreman:state-failed"}
    ]
    assert sibling_scrub_calls == [], (
        f"Failed entry must not scrub sibling labels; got: {sibling_scrub_calls!r}"
    )


def test_writer_failure_propagates() -> None:
    """Label-write failure propagates — the EventBus owns the firewall,
    not the observer. We assert the exception class so EventBus's
    blanket except sees it."""

    class _BoomWriter:
        def add_labels(
            self, *, project: str, issue_number: int, labels: set[str]
        ) -> None:
            raise RuntimeError("network down")

        def remove_labels(
            self, *, project: str, issue_number: int, labels: set[str]
        ) -> None:
            raise RuntimeError("network down")

    repo, ticket = _make_repo_and_ticket()
    obs = LabelObservabilityObserver(writer=_BoomWriter(), repo=repo)
    with pytest.raises(RuntimeError):
        obs(
            StateEnteredEvent(
                ticket_id=ticket.id,
                instance_id=99,
                state_name="Planning",
                sequence=1,
                at=_T0,
            )
        )
