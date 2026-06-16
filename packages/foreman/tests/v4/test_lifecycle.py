"""End-to-end lifecycle: Queued -> ... -> Done with all-fake providers.

This is the Phase 3 completion check. The test scripts canned Outcomes for
each role-dispatch state and walks a ticket through the happy path,
asserting the journal looks right at the end.
"""
from __future__ import annotations

import datetime as dt

from foreman.v4.event_bus import EventBus
from foreman.v4.git_provider import FakeGitProvider, PRState
from foreman.v4.observers.event_archive import EventArchiveObserver
from foreman.v4.observers.structured_log import StructuredLogObserver
from foreman.v4.role_dispatcher import FakeRoleDispatcher
from foreman.v4.sqlite_repository import SqliteTicketRepository
from foreman.v4.state import StateContext
from foreman.v4.states.merging import MergingState
from foreman.v4.states.registry import build_state


def _canned(kind: str, *, pr_number: int | None = None) -> str:
    artifacts = f',"artifacts":{{"pr_number":{pr_number}}}' if pr_number else ""
    return (
        f'FOREMAN_OUTCOME:{{"kind":"{kind}","confidence":"high",'
        f'"summary":"test"{artifacts}}}'
    )


_TERMINAL_STATES = ("Done", "Failed", "NeedsHelp")


def _run_until_terminal(repo, ticket_id, *, dispatcher, git, bus):
    """Drive the ticket one transition at a time until it reaches a terminal.

    Terminal landings (Done/Failed/NeedsHelp) are synthesized inline by
    ``state._enter_terminal`` — the transition that decides to advance
    to the terminal also creates the journal row + emits
    ``StateEnteredEvent`` for it. The WorkerPool never re-enqueues a
    terminally-parked ticket, and this loop mirrors that: it checks
    ticket state at the top of each iteration and returns as soon as
    the ticket lands on a terminal.
    """
    seq = 0
    while True:
        ticket = repo.get_ticket(ticket_id)
        if ticket.current_state in _TERMINAL_STATES:
            return ticket
        seq += 1
        state = build_state(ticket.current_state)
        instance = repo.open_state_instance(
            ticket_id=ticket.id, state_name=ticket.current_state,
            sequence=seq, now=dt.datetime(2026, 6, 13),
        )
        ctx = StateContext(
            ticket=ticket, instance=instance, repo=repo,
            clock=lambda seq=seq: dt.datetime(2026, 6, 13, 12, seq, 0),
            bus=bus, role_dispatcher=dispatcher, git=git,
        )
        # MergingState needs a pr_number; in real wiring it reads from the
        # ticket's most recent outcome_payload. For the lifecycle test we
        # monkey-patch that lookup.
        if isinstance(state, MergingState):
            state._pr_number_for = lambda _ctx: 42  # type: ignore[method-assign]
        state.transition(ctx)
        if seq > 25:
            raise AssertionError("did not converge; check canned outcomes")


def test_happy_path_queued_to_done():
    repo = SqliteTicketRepository.in_memory()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=42,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 1):        _canned("clean", pr_number=42),
        ("reviewer-spec", "p", 1):  _canned("clean", pr_number=42),
        ("worker", "p", 1):         _canned("clean", pr_number=42),
        ("reviewer-impl", "p", 1):  _canned("clean", pr_number=42),
    })
    bus = EventBus()
    bus.subscribe(EventArchiveObserver(conn=repo._conn))
    bus.subscribe(StructuredLogObserver())

    # Drive the ticket. MergingState now calls pr.merge() directly
    # when GitHub reports the PR mergeable + CI green — no MergeQueue
    # polling. The seeded PRState satisfies the merge gate immediately.
    final = _run_until_terminal(repo, ticket.id, dispatcher=dispatcher, git=git, bus=bus)
    assert final.current_state == "Done"

    # PR #42 ended merged. SpecReviewState.verify() merged the spec PR
    # AND MergingState merged the impl PR — both via the same merge_pr
    # call. Phase 8d.19 collapsed those two paths into one.
    assert git.get_pr_state(project="p", pr_number=42).merged is True
    assert ("p", 42) in git.merge_pr_calls

    # Journal records every state transition in order:
    rows = repo._conn.execute(
        "SELECT state_name, outcome_kind, next_state FROM state_instances "
        "WHERE ticket_id = ? ORDER BY sequence",
        (ticket.id,),
    ).fetchall()
    state_order = [r["state_name"] for r in rows]
    assert state_order == [
        "Queued", "Planning", "SpecReview", "Implementing",
        "ImplReview", "Merging", "Done",
    ]

    # Events archived for each transition:
    event_rows = repo._conn.execute(
        "SELECT DISTINCT state_name FROM events ORDER BY id"
    ).fetchall()
    archived_states = [r["state_name"] for r in event_rows]
    assert set(archived_states) >= {
        "Queued", "Planning", "SpecReview", "Implementing",
        "ImplReview", "Merging",
    }


def test_needs_fix_loop_spec_review_to_spec_fix_back():
    """When Reviewer rejects spec, we loop through SpecFix back to SpecReview."""
    repo = SqliteTicketRepository.in_memory()
    ticket = repo.create_ticket(project="p", issue_number=2, now=dt.datetime(2026, 6, 13))
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=7,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    # Reviewer rejects first, then Fixer fixes, then Reviewer accepts.
    # We cheat by mutating the canned response between iterations using a
    # mutable cell.
    review_calls = {"n": 0}

    class _ScriptedDispatcher:
        def dispatch(self, *, role, project, issue_number, ticket_id):
            if role == "planner":
                return _canned("clean", pr_number=7)
            if role == "reviewer-spec":
                review_calls["n"] += 1
                if review_calls["n"] == 1:
                    return _canned("needs_fix", pr_number=7)
                return _canned("clean", pr_number=7)
            if role == "fixer-spec":
                return _canned("clean", pr_number=7)
            if role == "worker":
                return _canned("clean", pr_number=7)
            if role == "reviewer-impl":
                return _canned("clean", pr_number=7)
            raise AssertionError(f"unexpected role {role}")

    # MergingState now calls pr.merge() directly when the PR is
    # mergeable + CI green; the seed PRState already satisfies that
    # gate. _run_until_terminal monkey-patches MergingState._pr_number_for
    # to 42, so register PR #42 in the fake git too.
    git.set_pr_state(
        project="p", pr_number=42,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )

    final = _run_until_terminal(
        repo, ticket.id, dispatcher=_ScriptedDispatcher(), git=git, bus=EventBus(),
    )
    assert final.current_state == "Done"

    state_order = [
        r["state_name"]
        for r in repo._conn.execute(
            "SELECT state_name FROM state_instances WHERE ticket_id = ? ORDER BY sequence",
            (ticket.id,),
        ).fetchall()
    ]
    assert "SpecFix" in state_order
    # SpecReview appears twice -- once rejecting, once accepting:
    assert state_order.count("SpecReview") == 2
