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

from foreman.v4.git_provider import FakeGitProvider, PRState, RequiredCheckState
from foreman.v4.outcome import OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext
from foreman.v4.states.merge_helper import MAX_CHECK_RERUNS, MAX_HEAL_ACTIONS, attempt_merge


def _ctx(
    *,
    git: FakeGitProvider,
    repo: InMemoryTicketRepository | None = None,
    state_name: str = "Merging",
    prior_blocked_heals: int = 0,
    prior_ci_pending_blocks: int = 0,
    prior_check_reruns: int = 0,
) -> StateContext:
    """Build a StateContext with the ticket parked in ``state_name``.

    ``prior_blocked_heals`` seeds prior BLOCKED rows that carry the
    ``heal_action`` marker (a healer acted) — these count toward the heal
    bound. ``prior_ci_pending_blocks`` seeds plain wait-for-CI BLOCKED rows
    (no healer acted, no marker) — these must NOT count toward either
    bound. ``prior_check_reruns`` seeds BLOCKED rows carrying the
    ``reran_checks`` marker (foreman#317) — these count toward the
    check-rerun bound in ``_rerun_or_escalate``. All three journal as
    ``outcome_kind == BLOCKED``; only the marker distinguishes them, which
    is exactly the regression the bound-counting fix guards against.
    """
    repo = repo or InMemoryTicketRepository()
    ticket = repo.create_ticket(
        project="p",
        issue_number=1,
        now=dt.datetime(2026, 6, 13),
    )
    repo.set_ticket_state(ticket.id, state_name, now=dt.datetime(2026, 6, 13))
    seq = 0

    def _seed_blocked(*, details: dict | None) -> None:
        nonlocal seq
        seq += 1
        inst = repo.open_state_instance(
            ticket_id=ticket.id,
            state_name=state_name,
            sequence=seq,
            now=dt.datetime(2026, 6, 13),
        )
        payload: dict = {"artifacts": {"pr_number": 99}}
        if details is not None:
            payload["details"] = details
        repo.mark_execute_completed(
            inst.id,
            now=dt.datetime(2026, 6, 13),
            outcome_kind=OutcomeKind.BLOCKED,
            outcome_payload=payload,
            next_state=state_name,
        )
        repo.close_state_instance(inst.id, now=dt.datetime(2026, 6, 13))

    for _ in range(prior_blocked_heals):
        _seed_blocked(details={"heal_action": "behind-branch"})
    for _ in range(prior_ci_pending_blocks):
        _seed_blocked(details=None)
    for _ in range(prior_check_reruns):
        _seed_blocked(details={"reran_checks": True})
    seq += 1
    instance = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name=state_name,
        sequence=seq,
        now=dt.datetime(2026, 6, 13),
    )
    return StateContext(
        ticket=repo.get_ticket(ticket.id),
        instance=instance,
        repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        git=git,
    )


def _seed_pr(git: FakeGitProvider, **kwargs) -> None:
    defaults = dict(
        merged=False,
        mergeable=False,
        ci_passing=True,
        base_ref="main",
        mergeable_state="blocked",
    )
    defaults.update(kwargs)
    git.set_pr_state(project="p", pr_number=99, state=PRState(**defaults))


def test_merged_short_circuits_to_clean_without_merge_call():
    git = FakeGitProvider()
    _seed_pr(git, merged=True, mergeable=True, mergeable_state="clean")
    ctx = _ctx(git=git)
    calls: list[str] = []
    outcome = attempt_merge(
        ctx,
        pr_number=99,
        on_merge_success=lambda: calls.append("ok"),
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
        ctx,
        pr_number=99,
        on_merge_success=lambda: calls.append("ok"),
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
        ctx,
        pr_number=99,
        on_merge_success=lambda: calls.append("ok"),
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


def test_ci_pending_blocks_do_not_count_toward_heal_bound():
    """REGRESSION (review #416): plain wait-for-CI BLOCKED rows must NOT
    increment the heal bound. A BEHIND PR with MAX_HEAL_ACTIONS prior
    CI-pending blocks (zero real heals taken) must STILL heal — not
    escalate. MergingState is supposed to poll BLOCKED indefinitely while
    CI runs (Phase 8d.18); the bound only counts heal-ACTED cycles.
    """
    git = FakeGitProvider()
    _seed_pr(git, mergeable=False, mergeable_state="behind")
    ctx = _ctx(git=git, prior_ci_pending_blocks=MAX_HEAL_ACTIONS)
    outcome = attempt_merge(ctx, pr_number=99, on_merge_success=lambda: None)
    # Only CI-pending blocks preceded this — 0 real heals — so the healer
    # acts and we get BLOCKED, NOT NEEDS_HELP.
    assert outcome.kind == OutcomeKind.BLOCKED
    assert git.update_branch_calls == [("p", 99)]


def test_heal_acted_block_carries_heal_action_marker():
    """The heal-acted BLOCKED outcome tags details with the healer name so
    ``_prior_blocked_heal_count`` can distinguish it from CI-pending blocks
    when read back from the persisted journal."""
    git = FakeGitProvider()
    _seed_pr(git, mergeable=False, mergeable_state="behind")
    ctx = _ctx(git=git)
    outcome = attempt_merge(ctx, pr_number=99, on_merge_success=lambda: None)
    assert outcome.kind == OutcomeKind.BLOCKED
    assert outcome.details.get("heal_action") == "behind-branch"


def test_ci_pending_block_has_no_heal_action_marker():
    """Symmetric to the above: the plain wait-for-CI BLOCKED must NOT carry
    the marker, or it would wrongly count toward the bound."""
    git = FakeGitProvider()
    _seed_pr(git, mergeable=False, ci_passing=False, mergeable_state="blocked")
    ctx = _ctx(git=git)
    outcome = attempt_merge(ctx, pr_number=99, on_merge_success=lambda: None)
    assert outcome.kind == OutcomeKind.BLOCKED
    assert "heal_action" not in outcome.details


def test_mixed_sequence_heals_do_not_falsely_escalate():
    """Mixed: one prior heal-acted block + many CI-pending blocks. Only the
    one real heal counts, so a fresh BEHIND PR still heals rather than
    escalating on the combined BLOCKED-row total."""
    git = FakeGitProvider()
    _seed_pr(git, mergeable=False, mergeable_state="behind")
    ctx = _ctx(
        git=git,
        prior_blocked_heals=1,
        prior_ci_pending_blocks=MAX_HEAL_ACTIONS + 3,
    )
    outcome = attempt_merge(ctx, pr_number=99, on_merge_success=lambda: None)
    assert outcome.kind == OutcomeKind.BLOCKED
    assert git.update_branch_calls == [("p", 99)]


def test_healer_escalate_maps_to_needs_help():
    """Extension contract: a healer returning HealResult.ESCALATE maps to
    a NEEDS_HELP outcome. Uses a fake healer (no real healer escalates
    today, but future ones may)."""
    from foreman.v4.merge_healers import MERGE_HEALERS, HealResult

    class _EscalatingHealer:
        name = "escalate-test"

        def applies(self, pr):
            return pr.mergeable_state == "needs-human"

        def heal(self, ctx, *, project, pr_number, pr):
            return HealResult.ESCALATE

    git = FakeGitProvider()
    _seed_pr(git, mergeable=False, mergeable_state="needs-human")
    ctx = _ctx(git=git)
    MERGE_HEALERS.append(_EscalatingHealer())
    try:
        outcome = attempt_merge(ctx, pr_number=99, on_merge_success=lambda: None)
    finally:
        MERGE_HEALERS.pop()
    assert outcome.kind == OutcomeKind.NEEDS_HELP
    assert outcome.details.get("healer") == "escalate-test"


def test_healer_proceed_maps_to_blocked():
    """Extension contract: a healer returning HealResult.PROCEED maps to a
    BLOCKED outcome (re-evaluate next poll). It IS a heal action, so it
    carries the heal_action marker and counts toward the bound."""
    from foreman.v4.merge_healers import MERGE_HEALERS, HealResult

    class _ProceedHealer:
        name = "proceed-test"

        def applies(self, pr):
            return pr.mergeable_state == "now-ready"

        def heal(self, ctx, *, project, pr_number, pr):
            return HealResult.PROCEED

    git = FakeGitProvider()
    _seed_pr(git, mergeable=False, mergeable_state="now-ready")
    ctx = _ctx(git=git)
    MERGE_HEALERS.append(_ProceedHealer())
    try:
        outcome = attempt_merge(ctx, pr_number=99, on_merge_success=lambda: None)
    finally:
        MERGE_HEALERS.pop()
    assert outcome.kind == OutcomeKind.BLOCKED
    assert outcome.details.get("heal_action") == "proceed-test"


def test_pre_merge_guard_short_circuits():
    """The impl caller passes a base-ref guard hook; when it returns an
    Outcome, attempt_merge returns it verbatim without touching the PR."""
    git = FakeGitProvider()
    _seed_pr(git, merged=True, mergeable=True, mergeable_state="clean")
    ctx = _ctx(git=git)
    from foreman.v4.outcome import Outcome, OutcomeConfidence

    guard_outcome = Outcome(
        kind=OutcomeKind.NEEDS_HELP,
        confidence=OutcomeConfidence.HIGH,
        summary="wrong base",
    )
    calls: list[str] = []
    outcome = attempt_merge(
        ctx,
        pr_number=99,
        on_merge_success=lambda: calls.append("ok"),
        pre_merge_guard=lambda _pr: guard_outcome,
    )
    assert outcome is guard_outcome
    assert ("p", 99) not in git.merge_pr_calls
    assert calls == []


# ---------------------------------------------------------------------------
# The classifier (foreman#317): no healer applied -> route on ground truth
# instead of a blanket BLOCKED. See docs/superpowers/specs/foreman-issue-
# 317-spec.md "Routing table" for the (mergeable_state x
# RequiredCheckState) -> Outcome matrix these tests lock in.
# ---------------------------------------------------------------------------


def test_blocked_and_failed_reruns_once_then_blocked():
    """blocked + required FAILED -> BLOCKED + rerun (foreman#537: a failed
    required check is re-run once — same bounded path as TIMED_OUT_OR_CANCELLED —
    before escalating, so a single CI infrastructure flake does not
    immediately route the ticket to ImplFix). See
    ``test_failed_reruns_once_then_escalates_to_needs_fix`` below for what
    happens on the NEXT pass if it's still failing."""
    git = FakeGitProvider()
    _seed_pr(git, mergeable=False, ci_passing=False, mergeable_state="blocked")
    git.seed_check_state("p", 99, RequiredCheckState.FAILED)
    ctx = _ctx(git=git)
    outcome = attempt_merge(ctx, pr_number=99, on_merge_success=lambda: None)
    assert outcome.kind == OutcomeKind.BLOCKED
    assert outcome.details.get("reran_checks") is True
    assert git.rerun_failed_checks_calls == [("p", 99)]


def test_failed_reruns_once_then_escalates_to_needs_fix():
    """REGRESSION (foreman#554): after ``MAX_CHECK_RERUNS`` prior re-run
    cycles, a required check that is STILL FAILED must escalate to
    NEEDS_FIX with ``fix_reason=ci_failed`` (-> ImplFix auto-fixes the
    code), NOT NEEDS_HELP. #554 broke the #317/C1 fix by routing FAILED
    through the same rerun-then-escalate path as TIMED_OUT_OR_CANCELLED and
    hardcoding its escalation to NEEDS_HELP too — a genuine CI failure
    dead-ended waiting on a human instead of being auto-fixed. This test
    pins the restored behavior so the regression can't come back."""
    git = FakeGitProvider()
    _seed_pr(git, mergeable=False, ci_passing=False, mergeable_state="blocked")
    git.seed_check_state("p", 99, RequiredCheckState.FAILED)
    ctx = _ctx(git=git, prior_check_reruns=MAX_CHECK_RERUNS)
    outcome = attempt_merge(ctx, pr_number=99, on_merge_success=lambda: None)
    assert outcome.kind == OutcomeKind.NEEDS_FIX
    assert outcome.details.get("fix_reason") == "ci_failed"
    # Bound tripped BEFORE acting -- no further rerun issued.
    assert git.rerun_failed_checks_calls == []


def test_blocked_and_pending_stays_blocked():
    """blocked + required PENDING -> BLOCKED (the legitimate wait, not a
    failure)."""
    git = FakeGitProvider()
    _seed_pr(git, mergeable=False, ci_passing=False, mergeable_state="blocked")
    git.seed_check_state("p", 99, RequiredCheckState.PENDING)
    ctx = _ctx(git=git)
    outcome = attempt_merge(ctx, pr_number=99, on_merge_success=lambda: None)
    assert outcome.kind == OutcomeKind.BLOCKED
    assert "fix_reason" not in outcome.details


def test_passed_but_blocked_is_transient_wait():
    """blocked + required PASSED -> BLOCKED. GitHub hasn't recomputed
    ``mergeable_state`` yet even though the required checks already
    report green; still a wait, not a failure."""
    git = FakeGitProvider()
    _seed_pr(git, mergeable=False, ci_passing=False, mergeable_state="blocked")
    git.seed_check_state("p", 99, RequiredCheckState.PASSED)
    ctx = _ctx(git=git)
    outcome = attempt_merge(ctx, pr_number=99, on_merge_success=lambda: None)
    assert outcome.kind == OutcomeKind.BLOCKED


def test_dirty_routes_to_needs_fix_conflict():
    """dirty (textual merge conflict) -> NEEDS_FIX, regardless of check
    state — CI never even runs on a dirty PR (Decision D: only the Fixer
    can resolve a real conflict; update_branch/rebase just fails on it)."""
    git = FakeGitProvider()
    _seed_pr(git, mergeable=False, ci_passing=False, mergeable_state="dirty")
    ctx = _ctx(git=git)
    outcome = attempt_merge(ctx, pr_number=99, on_merge_success=lambda: None)
    assert outcome.kind == OutcomeKind.NEEDS_FIX
    assert outcome.details["fix_reason"] == "merge_conflict"


def test_draft_routes_to_needs_help():
    """draft -> NEEDS_HELP (spec routing table). A foreman impl PR being a
    draft is anomalous — no healer applies (only BehindBranchHealer, keyed
    on "behind"), so without this branch the classifier fell through to
    the check-state branches and could loop BLOCKED on this one
    pre-check-runs ``mergeable_state`` the ``dirty`` branch doesn't cover."""
    git = FakeGitProvider()
    _seed_pr(git, mergeable=False, ci_passing=False, mergeable_state="draft")
    ctx = _ctx(git=git)
    outcome = attempt_merge(ctx, pr_number=99, on_merge_success=lambda: None)
    assert outcome.kind == OutcomeKind.NEEDS_HELP


def test_action_required_routes_to_needs_help():
    """blocked + required ACTION_REQUIRED -> NEEDS_HELP (a human gate;
    ImplFix cannot act on it)."""
    git = FakeGitProvider()
    _seed_pr(git, mergeable=False, ci_passing=False, mergeable_state="blocked")
    git.seed_check_state("p", 99, RequiredCheckState.ACTION_REQUIRED)
    ctx = _ctx(git=git)
    outcome = attempt_merge(ctx, pr_number=99, on_merge_success=lambda: None)
    assert outcome.kind == OutcomeKind.NEEDS_HELP


def test_timed_out_reruns_once_then_needs_help():
    """blocked + required TIMED_OUT_OR_CANCELLED -> re-run once (Decision
    T: likely an infra flake), returning BLOCKED with the ``reran_checks``
    marker and recording the provider call."""
    git = FakeGitProvider()
    _seed_pr(git, mergeable=False, ci_passing=False, mergeable_state="blocked")
    git.seed_check_state("p", 99, RequiredCheckState.TIMED_OUT_OR_CANCELLED)
    ctx = _ctx(git=git)
    outcome = attempt_merge(ctx, pr_number=99, on_merge_success=lambda: None)
    assert outcome.kind == OutcomeKind.BLOCKED
    assert outcome.details.get("reran_checks") is True
    assert git.rerun_failed_checks_calls == [("p", 99)]


def test_timed_out_escalates_after_max_reruns():
    """After ``MAX_CHECK_RERUNS`` prior re-run cycles, a still timed-out/
    cancelled PR escalates to NEEDS_HELP instead of re-running again."""
    git = FakeGitProvider()
    _seed_pr(git, mergeable=False, ci_passing=False, mergeable_state="blocked")
    git.seed_check_state("p", 99, RequiredCheckState.TIMED_OUT_OR_CANCELLED)
    ctx = _ctx(git=git, prior_check_reruns=MAX_CHECK_RERUNS)
    outcome = attempt_merge(ctx, pr_number=99, on_merge_success=lambda: None)
    assert outcome.kind == OutcomeKind.NEEDS_HELP
    # Bound tripped BEFORE acting -- no further rerun issued.
    assert git.rerun_failed_checks_calls == []


# ---------------------------------------------------------------------------
# End-to-end regression (foreman#317 Task 4): the exact agent_core#390
# incident this issue closes — a blocked PR with a FAILED required check
# must reach ImplFix, not loop BLOCKED forever.
#
# foreman#550 (Task 3) removed the composed-path variant of this regression
# test that used to live here (``attempt_merge`` outcome fed straight into
# ``MergingState.next_state()``): MergingState no longer calls
# ``attempt_merge`` at all, and no longer special-cases NEEDS_FIX in its own
# ``next_state`` — the merge classifier's failure routing (dirty/CI-failed ->
# ImplFix, per #317) now belongs to the per-repo MergeCoordinator (a later
# foreman#550 task), which will call ``attempt_merge`` from its own tick loop
# and route failures itself, not through the TicketState.next_state()
# machinery. The underlying classifier behavior this test pinned — a
# blocked + FAILED-check PR reaches NEEDS_FIX, not an infinite BLOCKED loop —
# remains covered by the pair ``test_blocked_and_failed_reruns_once_then_blocked``
# (first pass: rerun once) + ``test_failed_reruns_once_then_escalates_to_needs_fix``
# (next pass: still failing -> NEEDS_FIX) above; only the now-obsolete
# ``MergingState``-composition assertion was removed.
# The equivalent "classifier failure reaches ImplFix" regression on the new
# composed path (MergeCoordinator + #317 routing) belongs to that later
# task's own tests (see ``test_merge_coordinator.py``'s
# ``test_ci_failed_reruns_and_stays_queued_on_first_tick`` /
# ``test_failed_check_escalates_to_impl_fix_after_max_attempts``).
# ---------------------------------------------------------------------------
