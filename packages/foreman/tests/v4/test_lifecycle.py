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
    bus.subscribe(EventArchiveObserver(repo=repo))
    bus.subscribe(StructuredLogObserver())

    # Drive the ticket. MergingState now calls pr.merge() directly
    # when GitHub reports the PR mergeable + CI green — no MergeQueue
    # polling. The seeded PRState satisfies the merge gate immediately.
    final = _run_until_terminal(repo, ticket.id, dispatcher=dispatcher, git=git, bus=bus)
    assert final.current_state == "Done"

    # PR #42 ended merged. foreman#416: the spec-PR merge moved out of
    # SpecReviewState.verify() into the dedicated SpecMerging state, and
    # MergingState merges the impl PR — both via the same merge_pr call.
    assert git.get_pr_state(project="p", pr_number=42).merged is True
    assert ("p", 42) in git.merge_pr_calls

    # Journal records every state transition in order. foreman#416 inserts
    # SpecMerging between SpecReview and Implementing.
    rows = repo._conn.execute(
        "SELECT state_name, outcome_kind, next_state FROM state_instances "
        "WHERE ticket_id = ? ORDER BY sequence",
        (ticket.id,),
    ).fetchall()
    state_order = [r["state_name"] for r in rows]
    assert state_order == [
        "Queued", "Planning", "SpecReview", "SpecMerging", "Implementing",
        "ImplReview", "Merging", "Done",
    ]

    # Events archived for each transition:
    event_rows = repo._conn.execute(
        "SELECT DISTINCT state_name FROM events ORDER BY id"
    ).fetchall()
    archived_states = [r["state_name"] for r in event_rows]
    assert set(archived_states) >= {
        "Queued", "Planning", "SpecReview", "SpecMerging", "Implementing",
        "ImplReview", "Merging",
    }


def test_behind_spec_pr_heals_then_reaches_implementing_and_done():
    """foreman#416 end-to-end: a spec PR that starts BEHIND self-heals.

    SpecMerging issues update_branch (BLOCKED → self-loop), and on the next
    poll the PR is no longer behind, so it merges and the ticket advances
    to Implementing → … → Done. This is the exact case that used to 405 in
    SpecReviewState.verify() and escalate to NeedsHelp (agent_core#190).
    """
    repo = SqliteTicketRepository.in_memory()
    ticket = repo.create_ticket(project="p", issue_number=3, now=dt.datetime(2026, 6, 13))

    # A git wrapper that starts PR #42 BEHIND and flips it to clean the
    # instant update_branch is called — modelling GitHub's "Update branch"
    # rebasing the PR onto the advanced base.
    class _HealingGit(FakeGitProvider):
        def update_branch(self, *, project, pr_number):
            super().update_branch(project=project, pr_number=pr_number)
            cur = self.get_pr_state(project=project, pr_number=pr_number)
            self.set_pr_state(
                project=project, pr_number=pr_number,
                state=PRState(
                    merged=cur.merged, mergeable=True, ci_passing=True,
                    base_ref=cur.base_ref, mergeable_state="clean",
                ),
            )

    git = _HealingGit()
    git.set_pr_state(
        project="p", pr_number=42,
        state=PRState(
            merged=False, mergeable=False, ci_passing=True,
            base_ref="main", mergeable_state="behind",
        ),
    )
    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 3):        _canned("clean", pr_number=42),
        ("reviewer-spec", "p", 3):  _canned("clean", pr_number=42),
        ("worker", "p", 3):         _canned("clean", pr_number=42),
        ("reviewer-impl", "p", 3):  _canned("clean", pr_number=42),
    })

    final = _run_until_terminal(
        repo, ticket.id, dispatcher=dispatcher, git=git, bus=EventBus(),
    )
    assert final.current_state == "Done"
    # The heal happened exactly once before the spec PR merged.
    assert git.update_branch_calls == [("p", 42)]
    assert ("p", 42) in git.merge_pr_calls

    state_order = [
        r["state_name"]
        for r in repo._conn.execute(
            "SELECT state_name FROM state_instances WHERE ticket_id = ? "
            "ORDER BY sequence",
            (ticket.id,),
        ).fetchall()
    ]
    # SpecMerging appears twice: once heals (BLOCKED self-loop), once merges.
    assert state_order.count("SpecMerging") == 2
    assert "Implementing" in state_order


def test_spec_pr_heal_then_ci_pending_then_heal_reaches_done_not_needs_help():
    """foreman#416 regression e2e: a spec PR that heals, then sits in
    CI-pending BLOCKED for several polls, then goes behind again and heals
    a SECOND time must still reach Done — NOT escalate to NeedsHelp.

    The heal bound counts ONLY heal-acted cycles (2 here), not the
    interleaved CI-pending polls. Before the bound-counting fix, the
    combined BLOCKED-row total (2 heals + N CI-pending) would trip the
    bound and wrongly escalate. The scripted git below walks PR #42 through
    behind → (heal) clean-but-CI-pending → CI-pending×3 → behind → (heal)
    clean+green.
    """
    repo = SqliteTicketRepository.in_memory()
    ticket = repo.create_ticket(project="p", issue_number=4, now=dt.datetime(2026, 6, 13))

    # Scripted mergeable_state walk for PR #42. Each SpecMerging poll calls
    # get_pr_state once and consumes one script entry (the script advances
    # per poll). The last entry (clean+green) persists so the impl-phase
    # MergingState — which also reads PR 42 — sees a coherent state.
    class _ScriptedGit(FakeGitProvider):
        def __init__(self):
            super().__init__()
            # (mergeable, ci_passing, mergeable_state) per SpecMerging poll.
            self._script = [
                (False, True, "behind"),     # poll 1: heal #1
                (False, False, "blocked"),   # poll 2: CI pending
                (False, False, "blocked"),   # poll 3: CI pending
                (False, False, "blocked"),   # poll 4: CI pending
                (False, True, "behind"),     # poll 5: heal #2
                (True, True, "clean"),       # poll 6: merge
            ]
            self._idx = 0

        def get_pr_state(self, *, project, pr_number):
            cur = super().get_pr_state(project=project, pr_number=pr_number)
            # Once merged, freeze — never resurrect a merged PR (the impl
            # MergingState phase also reads PR 42 and must see merged=True).
            if pr_number == 42 and not cur.merged:
                # Consume the next script entry; clamp at the last so reads
                # after the script is exhausted stay on clean+green.
                entry = self._script[min(self._idx, len(self._script) - 1)]
                mergeable, ci, ms = entry
                self.set_pr_state(
                    project=project, pr_number=pr_number,
                    state=PRState(
                        merged=False, mergeable=mergeable, ci_passing=ci,
                        base_ref="main", mergeable_state=ms,
                    ),
                )
                if self._idx < len(self._script):
                    self._idx += 1
            return super().get_pr_state(project=project, pr_number=pr_number)

    git = _ScriptedGit()
    git.set_pr_state(
        project="p", pr_number=42,
        state=PRState(
            merged=False, mergeable=False, ci_passing=True,
            base_ref="main", mergeable_state="behind",
        ),
    )
    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 4):        _canned("clean", pr_number=42),
        ("reviewer-spec", "p", 4):  _canned("clean", pr_number=42),
        ("worker", "p", 4):         _canned("clean", pr_number=42),
        ("reviewer-impl", "p", 4):  _canned("clean", pr_number=42),
    })

    final = _run_until_terminal(
        repo, ticket.id, dispatcher=dispatcher, git=git, bus=EventBus(),
    )
    assert final.current_state == "Done"
    # Two genuine heals — and crucially NOT a NeedsHelp landing.
    assert git.update_branch_calls == [("p", 42), ("p", 42)]
    assert ("p", 42) in git.merge_pr_calls
    state_order = [
        r["state_name"]
        for r in repo._conn.execute(
            "SELECT state_name FROM state_instances WHERE ticket_id = ? "
            "ORDER BY sequence",
            (ticket.id,),
        ).fetchall()
    ]
    assert "NeedsHelp" not in state_order
    # SpecMerging polled 6 times (2 heals + 3 CI-pending + 1 merge).
    assert state_order.count("SpecMerging") == 6
    assert "Implementing" in state_order


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
        def dispatch(self, *, role, project, issue_number, ticket_id,
                     state_instance_id=None):
            del state_instance_id  # foreman#367: accepted but unused here
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
