"""attempt_merge — shared merge-attempt logic for MergingState + SpecMerging.

foreman#416. Both merge states (impl PR in ``MergingState``, spec PR in
``SpecMerging``) share the same skeleton:

    get_pr_state
      merged                -> caller success (CLEAN)
      mergeable + ci        -> merge_pr -> caller success (CLEAN)
      else                  -> consult MERGE_HEALERS:
                                 RETRY/PROCEED -> BLOCKED (re-poll)
                                 ESCALATE      -> NEEDS_HELP
                                 no healer     -> BLOCKED (wait for CI)

Plus a heal-action bound: a perpetually-behind PR can't loop
``update_branch`` forever. After ``MAX_HEAL_ACTIONS`` prior BLOCKED heal
cycles on this state, the helper emits NEEDS_HELP instead of RETRY.

These tests drive ``attempt_merge`` directly with a FakeGitProvider and an
InMemoryTicketRepository, asserting outcome kind + side effects per branch.
"""
from __future__ import annotations

import datetime as dt

from foreman.v4.git_provider import FakeGitProvider, PRState
from foreman.v4.outcome import OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext
from foreman.v4.states.merge_helper import MAX_HEAL_ACTIONS, attempt_merge


def _ctx(
    *,
    git: FakeGitProvider,
    repo: InMemoryTicketRepository | None = None,
    state_name: str = "Merging",
    prior_blocked_heals: int = 0,
) -> StateContext:
    """Build a StateContext with the ticket parked in ``state_name`` and
    ``prior_blocked_heals`` prior BLOCKED instances of that state recorded
    in the journal (so the bound can count them)."""
    repo = repo or InMemoryTicketRepository()
    ticket = repo.create_ticket(
        project="p", issue_number=1, now=dt.datetime(2026, 6, 13),
    )
    repo.set_ticket_state(ticket.id, state_name, now=dt.datetime(2026, 6, 13))
    seq = 0
    for _ in range(prior_blocked_heals):
        seq += 1
        inst = repo.open_state_instance(
            ticket_id=ticket.id, state_name=state_name, sequence=seq,
            now=dt.datetime(2026, 6, 13),
        )
        repo.mark_execute_completed(
            inst.id, now=dt.datetime(2026, 6, 13),
            outcome_kind=OutcomeKind.BLOCKED,
            outcome_payload={"artifacts": {"pr_number": 99}},
            next_state=state_name,
        )
        repo.close_state_instance(inst.id, now=dt.datetime(2026, 6, 13))
    seq += 1
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name=state_name, sequence=seq,
        now=dt.datetime(2026, 6, 13),
    )
    return StateContext(
        ticket=repo.get_ticket(ticket.id), instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13), git=git,
    )


def _seed_pr(git: FakeGitProvider, **kwargs) -> None:
    defaults = dict(
        merged=False, mergeable=False, ci_passing=True,
        base_ref="main", mergeable_state="blocked",
    )
    defaults.update(kwargs)
    git.set_pr_state(project="p", pr_number=99, state=PRState(**defaults))


def test_merged_short_circuits_to_clean_without_merge_call():
    git = FakeGitProvider()
    _seed_pr(git, merged=True, mergeable=True, mergeable_state="clean")
    ctx = _ctx(git=git)
    calls: list[str] = []
    outcome = attempt_merge(
        ctx, pr_number=99, on_merge_success=lambda: calls.append("ok"),
    )
    assert outcome.kind == OutcomeKind.CLEAN
    assert ("p", 99) not in git.merge_pr_calls
    # on_merge_success fires on the merged branch too.
    assert calls == ["ok"]


def test_mergeable_and_ci_passing_merges_then_clean():
    git = FakeGitProvider()
    _seed_pr(git, mergeable=True, ci_passing=True, mergeable_state="clean")
    ctx = _ctx(git=git)
    calls: list[str] = []
    outcome = attempt_merge(
        ctx, pr_number=99, on_merge_success=lambda: calls.append("ok"),
    )
    assert outcome.kind == OutcomeKind.CLEAN
    assert ("p", 99) in git.merge_pr_calls
    assert git.get_pr_state(project="p", pr_number=99).merged is True
    assert calls == ["ok"]


def test_behind_pr_updates_branch_and_returns_blocked():
    git = FakeGitProvider()
    _seed_pr(git, mergeable=False, mergeable_state="behind")
    ctx = _ctx(git=git)
    calls: list[str] = []
    outcome = attempt_merge(
        ctx, pr_number=99, on_merge_success=lambda: calls.append("ok"),
    )
    assert outcome.kind == OutcomeKind.BLOCKED
    # The healer acted: update_branch once, no merge, no escalate, no success.
    assert git.update_branch_calls == [("p", 99)]
    assert ("p", 99) not in git.merge_pr_calls
    assert calls == []


def test_ci_pending_no_healer_returns_blocked_without_update_branch():
    git = FakeGitProvider()
    # ci_passing False, mergeable False, but mergeable_state is a plain
    # "blocked" (CI still running) — no healer applies.
    _seed_pr(git, mergeable=False, ci_passing=False, mergeable_state="blocked")
    ctx = _ctx(git=git)
    outcome = attempt_merge(ctx, pr_number=99, on_merge_success=lambda: None)
    assert outcome.kind == OutcomeKind.BLOCKED
    # No healer applied: update_branch NOT called (unchanged wait-for-CI).
    assert git.update_branch_calls == []
    assert ("p", 99) not in git.merge_pr_calls


def test_bound_exceeded_escalates_to_needs_help():
    """A perpetually-behind PR: after MAX_HEAL_ACTIONS prior BLOCKED heal
    cycles, the helper escalates to NEEDS_HELP instead of issuing yet
    another update_branch."""
    git = FakeGitProvider()
    _seed_pr(git, mergeable=False, mergeable_state="behind")
    ctx = _ctx(git=git, prior_blocked_heals=MAX_HEAL_ACTIONS)
    outcome = attempt_merge(ctx, pr_number=99, on_merge_success=lambda: None)
    assert outcome.kind == OutcomeKind.NEEDS_HELP
    # Bound tripped BEFORE acting — no further update_branch.
    assert git.update_branch_calls == []


def test_just_under_bound_still_heals():
    """One cycle below the bound, the healer still acts — the bound is a
    ceiling, not an off-by-one early stop."""
    git = FakeGitProvider()
    _seed_pr(git, mergeable=False, mergeable_state="behind")
    ctx = _ctx(git=git, prior_blocked_heals=MAX_HEAL_ACTIONS - 1)
    outcome = attempt_merge(ctx, pr_number=99, on_merge_success=lambda: None)
    assert outcome.kind == OutcomeKind.BLOCKED
    assert git.update_branch_calls == [("p", 99)]


def test_pre_merge_guard_short_circuits():
    """The impl caller passes a base-ref guard hook; when it returns an
    Outcome, attempt_merge returns it verbatim without touching the PR."""
    git = FakeGitProvider()
    _seed_pr(git, merged=True, mergeable=True, mergeable_state="clean")
    ctx = _ctx(git=git)
    from foreman.v4.outcome import Outcome, OutcomeConfidence

    guard_outcome = Outcome(
        kind=OutcomeKind.NEEDS_HELP, confidence=OutcomeConfidence.HIGH,
        summary="wrong base",
    )
    calls: list[str] = []
    outcome = attempt_merge(
        ctx, pr_number=99,
        on_merge_success=lambda: calls.append("ok"),
        pre_merge_guard=lambda _pr: guard_outcome,
    )
    assert outcome is guard_outcome
    assert ("p", 99) not in git.merge_pr_calls
    assert calls == []
