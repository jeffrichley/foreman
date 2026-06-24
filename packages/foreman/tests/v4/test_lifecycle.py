"""End-to-end lifecycle: Queued -> ... -> Done with all-fake providers.

This is the Phase 3 completion check. The test scripts canned Outcomes for
each role-dispatch state and walks a ticket through the happy path,
asserting the journal looks right at the end.
"""
from __future__ import annotations

import datetime as dt

from foreman.v4.config import ProjectConfig
from foreman.v4.event_bus import EventBus
from foreman.v4.git_provider import FakeGitProvider, PRState
from foreman.v4.observers.event_archive import EventArchiveObserver
from foreman.v4.observers.structured_log import StructuredLogObserver
from foreman.v4.role_dispatcher import FakeRoleDispatcher
from foreman.v4.sqlite_repository import SqliteTicketRepository
from foreman.v4.state import StateContext
from foreman.v4.states.merging import MergingState
from foreman.v4.states.registry import build_state


def _auto_merge_configs() -> dict[str, ProjectConfig]:
    """foreman#418: the happy-path lifecycle test drives the ticket all
    the way to Done, which requires the impl PR to auto-merge. That now
    only happens when the project opts in to ``auto_merge_impl`` — so
    the lifecycle test threads a config map with the flag set. With the
    default (False), the ticket would correctly park at ImplApproved
    awaiting human merge (covered by the gating unit tests)."""
    return {
        "p": ProjectConfig(
            name="p",
            repo="o/p",
            local_clone_path="/tmp/p",
            auto_merge_impl=True,
        )
    }


def _canned(kind: str, *, pr_number: int | None = None) -> str:
    artifacts = f',"artifacts":{{"pr_number":{pr_number}}}' if pr_number else ""
    return (
        f'FOREMAN_OUTCOME:{{"kind":"{kind}","confidence":"high",'
        f'"summary":"test"{artifacts}}}'
    )


# foreman#418: ``ImplApproved`` is terminal-for-the-machine (the
# WorkerPool won't re-enqueue it) — the driver stops there too, exactly
# like the production loop, so a parked ticket can be resumed via the
# operator path in a separate driver run.
_TERMINAL_STATES = ("Done", "Failed", "NeedsHelp", "ImplApproved")


def _run_until_terminal(repo, ticket_id, *, dispatcher, git, bus, project_configs=None):
    """Drive the ticket one transition at a time until it reaches a terminal.

    Terminal landings (Done/Failed/NeedsHelp) are synthesized inline by
    ``state._enter_terminal`` — the transition that decides to advance
    to the terminal also creates the journal row + emits
    ``StateEnteredEvent`` for it. The WorkerPool never re-enqueues a
    terminally-parked ticket, and this loop mirrors that: it checks
    ticket state at the top of each iteration and returns as soon as
    the ticket lands on a terminal.
    """
    # Resume-safe: continue the sequence after any rows already in the
    # journal (a parked ticket resumed by the operator path re-enters this
    # driver with prior state_instances rows present).
    seq = repo.count_state_instances_for_ticket(ticket_id)
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
            project_configs=project_configs or {},
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
        state=PRState(merged=False, mergeable=True, ci_passing=True, base_ref="main"),
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
    final = _run_until_terminal(
        repo, ticket.id, dispatcher=dispatcher, git=git, bus=bus,
        project_configs=_auto_merge_configs(),
    )
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
        state=PRState(merged=False, mergeable=True, ci_passing=True, base_ref="main"),
    )

    final = _run_until_terminal(
        repo, ticket.id, dispatcher=_ScriptedDispatcher(), git=git, bus=EventBus(),
        project_configs=_auto_merge_configs(),
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


def test_impl_approved_operator_resume_to_done():
    """foreman#418 exit-path: a ticket parked at ImplApproved (the
    default, auto_merge_impl=False) can be resumed by an operator and
    driven all the way to Done.

    This proves the gate's core promise end-to-end: foreman parks the
    approved impl PR for human merge, the human moves the ticket to
    Merging via the real ``foreman set-state`` operator command, and the
    next driver pass merges the PR and lands on Done. Without this the
    parked state would be a dead end.

    Stage 1: drive Queued -> ... -> ImplApproved with NO auto_merge_impl
             opt-in (empty project_configs == default-safe park). Assert
             merge_pr was NOT called and the ticket is parked at
             ImplApproved.
    Stage 2: operator runs ``set-state <id> Merging`` (real CLI command).
    Stage 3: drive again — MergingState merges PR #42 (mergeable + CI
             green + base_ref main) -> Done. Assert merge_pr WAS called,
             the issue was closed, and the ticket reached Done.
    """
    from typer.testing import CliRunner

    from foreman.v4.cli import app
    from foreman.v4.cli.context import build_cli_context

    repo = SqliteTicketRepository.in_memory()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=42,
        state=PRState(merged=False, mergeable=True, ci_passing=True, base_ref="main"),
    )
    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 1):        _canned("clean", pr_number=42),
        ("reviewer-spec", "p", 1):  _canned("clean", pr_number=42),
        ("worker", "p", 1):         _canned("clean", pr_number=42),
        ("reviewer-impl", "p", 1):  _canned("clean", pr_number=42),
    })
    bus = EventBus()

    # Stage 1: park at ImplApproved. No auto_merge_impl opt-in => default
    # gate => the approved impl PR parks for human merge.
    parked = _run_until_terminal(
        repo, ticket.id, dispatcher=dispatcher, git=git, bus=bus,
        project_configs={},  # default-safe: no project => park
    )
    assert parked.current_state == "ImplApproved"
    # The whole point of the gate: foreman did NOT take the impl-merge
    # close-out path while parked. MergingState is the ONLY state that
    # closes the originating issue (after merging the impl PR), so the
    # issue staying OPEN proves the impl PR was not auto-merged here. (The
    # spec PR shares pr_number 42 with the impl PR in this test and was
    # merged by SpecReviewState.verify(), so merge_pr_calls is not a clean
    # impl-merge signal — closed_issues is.)
    assert git.closed_issues == set()

    # Stage 2: operator resumes the parked ticket via the real CLI command.
    runner = CliRunner()
    result = runner.invoke(
        app, ["set-state", str(ticket.id), "Merging"],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code == 0, result.output
    assert repo.get_ticket(ticket.id).current_state == "Merging"

    # Stage 3: drive the resumed ticket — MergingState merges and lands Done.
    final = _run_until_terminal(
        repo, ticket.id, dispatcher=dispatcher, git=git, bus=bus,
        project_configs={},
    )
    assert final.current_state == "Done"
    # Now the merge actually happened, driven by the operator resume.
    assert ("p", 42) in git.merge_pr_calls
    assert git.get_pr_state(project="p", pr_number=42).merged is True
    assert ("p", 1) in git.closed_issues
