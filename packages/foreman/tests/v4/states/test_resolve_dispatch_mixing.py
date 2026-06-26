"""Adversarial cross-identity session-mixing proofs (LOAD-BEARING SAFETY).

A *wrong* resume is catastrophic: it would load an agent with another role's,
ticket's, or target's entire Claude context, or revive a session that already
finished. The decision tests in ``test_resolve_dispatch.py`` prove the happy
path and the basic walls; THIS module is the explicit, legible artifact that
proves mixing is IMPOSSIBLE — every adversarial cross-identity construction
falls back to a FRESH run (``resume is False``).

Each scenario builds, through the real repository write methods, a prior
*interrupted* same-run instance whose stored ``session_id`` belongs to
DIFFERENT work, then asserts the current dispatch refuses to resume it. The
wall that trips in cases 1-3 is verify-or-fresh: the freshly-derived id for the
current work can never equal an id derived for a different role / ticket /
target, so the stored id never matches. Case 4 proves a COMPLETED session is
never resumed even on a perfect id match. Case 5 pins the determinism wall that
makes 1-3 true by construction.

Two of the four independent anti-mixing walls are exercised here directly:

1. **Scoped id by construction** (``derive_session_id``) — case 5's matrix.
2. **Verify-or-fresh** (``resolve_dispatch``) — cases 1-4.

The THIRD wall — the **cwd wall** — is NOT a unit-test concern: Claude stores
transcripts under a per-``(ticket, target)`` worktree directory, and the
dispatcher ALWAYS passes an explicit ``session_id`` (never relies on Claude's
"latest session in cwd"). That wall is enforced structurally by the worktree
layout plus the always-passed id, so it is covered by the worktree/dispatch
integration surface rather than by these pure-decision tests.
"""
from __future__ import annotations

import datetime as dt
import itertools

from foreman.v4.outcome import OutcomeKind
from foreman.v4.records import StateInstanceRecord
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.session_ids import derive_session_id
from foreman.v4.state import StateContext
from foreman.v4.states.resolve_dispatch import resolve_dispatch

_T0 = dt.datetime(2026, 6, 25, 12, 0, 0)


def _at(seconds: int) -> dt.datetime:
    return _T0 + dt.timedelta(seconds=seconds)


def _ctx_for(
    repo: InMemoryTicketRepository,
    *,
    ticket_id: int,
    instance: StateInstanceRecord,
) -> StateContext:
    ticket = repo.get_ticket(ticket_id)
    return StateContext(
        ticket=ticket,
        instance=instance,
        repo=repo,
        clock=lambda: _at(0),
    )


def _open(
    repo: InMemoryTicketRepository,
    *,
    ticket_id: int,
    state_name: str,
    sequence: int,
) -> StateInstanceRecord:
    return repo.open_state_instance(
        ticket_id=ticket_id,
        state_name=state_name,
        sequence=sequence,
        now=_at(sequence),
    )


def _make_repo_and_ticket(issue_number: int = 1) -> tuple[InMemoryTicketRepository, int]:
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=issue_number, now=_T0)
    return repo, ticket.id


def _interrupted_prior_with_stored_id(
    repo: InMemoryTicketRepository,
    *,
    ticket_id: int,
    state_name: str,
    sequence: int,
    stored_session_id: str,
) -> StateInstanceRecord:
    """Open + start + stamp a stored session_id, then crash-close (interrupted).

    Mirrors production's crash shape: ``mark_execute_started`` + ``set_session_id``
    happened, but ``mark_execute_completed`` never did — so the row reads as
    interrupted (the only resumable kind). The stored id is supplied by the
    caller so each test can plant a DIFFERENT-work id.
    """
    prior = _open(repo, ticket_id=ticket_id, state_name=state_name, sequence=sequence)
    repo.mark_execute_started(prior.id, now=_at(sequence))
    repo.set_session_id(prior.id, stored_session_id)
    repo.record_failure(
        prior.id, now=_at(sequence), failure_phase="crash_recovery",
        failure_reason="x",
    )
    repo.close_state_instance(prior.id, now=_at(sequence))
    return prior


# --------------------------------------------------------------------------
# 1. WRONG ROLE.
#    A Reviewer dispatch must NEVER resume a Fixer's interrupted session.
# --------------------------------------------------------------------------
def test_wrong_role_never_resumes_other_roles_session():
    """A reviewer-spec dispatch must never resume a fixer-spec interrupted session."""
    repo, ticket_id = _make_repo_and_ticket()
    run_key = "1"  # single consecutive same-state run starting at sequence 1
    target = "spec"

    # The interrupted prior stored an id derived for a DIFFERENT role (fixer-spec).
    stored = derive_session_id(ticket_id, "fixer-spec", target, run_key)
    _interrupted_prior_with_stored_id(
        repo, ticket_id=ticket_id, state_name="SpecReview", sequence=1,
        stored_session_id=stored,
    )

    # Current dispatch resolves as the reviewer-spec role.
    current = _open(repo, ticket_id=ticket_id, state_name="SpecReview", sequence=2)
    ctx = _ctx_for(repo, ticket_id=ticket_id, instance=current)

    plan = resolve_dispatch(ctx, role="reviewer-spec", target=target)

    expected_sid = derive_session_id(ticket_id, "reviewer-spec", target, run_key)
    assert plan.resume is False
    assert plan.session_id == expected_sid
    # Sanity: the stored fixer id is genuinely a different id (the wall has teeth).
    assert stored != expected_sid


# --------------------------------------------------------------------------
# 2. WRONG TICKET.
#    Ticket 2's dispatch must NEVER resume a session stored for ticket 1.
# --------------------------------------------------------------------------
def test_wrong_ticket_never_resumes_other_tickets_session():
    """Ticket 2's dispatch must never resume a session derived for ticket 1."""
    repo, ticket_id = _make_repo_and_ticket(issue_number=2)
    run_key = "1"
    role = "planner"
    target = None

    other_ticket_id = ticket_id + 1000  # a ticket id that is definitively NOT ours
    stored = derive_session_id(other_ticket_id, role, target, run_key)
    _interrupted_prior_with_stored_id(
        repo, ticket_id=ticket_id, state_name="Planning", sequence=1,
        stored_session_id=stored,
    )

    current = _open(repo, ticket_id=ticket_id, state_name="Planning", sequence=2)
    ctx = _ctx_for(repo, ticket_id=ticket_id, instance=current)

    plan = resolve_dispatch(ctx, role=role, target=target)

    expected_sid = derive_session_id(ticket_id, role, target, run_key)
    assert plan.resume is False
    assert plan.session_id == expected_sid
    assert stored != expected_sid


# --------------------------------------------------------------------------
# 3. WRONG TARGET.
#    A spec dispatch must NEVER resume a session stored for the impl target.
# --------------------------------------------------------------------------
def test_wrong_target_never_resumes_other_targets_session():
    """A spec-target dispatch must never resume an impl-target interrupted session."""
    repo, ticket_id = _make_repo_and_ticket()
    run_key = "1"
    role = "reviewer-spec"

    # Prior stored an id derived for target="impl"; current resolves target="spec".
    stored = derive_session_id(ticket_id, role, "impl", run_key)
    _interrupted_prior_with_stored_id(
        repo, ticket_id=ticket_id, state_name="SpecReview", sequence=1,
        stored_session_id=stored,
    )

    current = _open(repo, ticket_id=ticket_id, state_name="SpecReview", sequence=2)
    ctx = _ctx_for(repo, ticket_id=ticket_id, instance=current)

    plan = resolve_dispatch(ctx, role=role, target="spec")

    expected_sid = derive_session_id(ticket_id, role, "spec", run_key)
    assert plan.resume is False
    assert plan.session_id == expected_sid
    assert stored != expected_sid


# --------------------------------------------------------------------------
# 4. COMPLETED SESSION (perfect id match).
#    A finished session must NEVER be resumed — guards against reviving a
#    Planning session that already completed.
# --------------------------------------------------------------------------
def test_completed_matching_session_never_resumes():
    """A completed session must never be resumed even on a perfect id match."""
    repo, ticket_id = _make_repo_and_ticket()
    run_key = "1"
    role = "planner"
    target = None
    sid = derive_session_id(ticket_id, role, target, run_key)

    # Prior carries the EXACT matching id, but it COMPLETED — id match must not
    # be enough; the completed gate has to refuse it.
    prior = _open(repo, ticket_id=ticket_id, state_name="Planning", sequence=1)
    repo.mark_execute_started(prior.id, now=_at(1))
    repo.set_session_id(prior.id, sid)
    repo.mark_execute_completed(
        prior.id,
        now=_at(1),
        outcome_kind=OutcomeKind.CLEAN,
        outcome_payload={},
        next_state="SpecReview",
    )
    repo.close_state_instance(prior.id, now=_at(1))

    current = _open(repo, ticket_id=ticket_id, state_name="Planning", sequence=2)
    ctx = _ctx_for(repo, ticket_id=ticket_id, instance=current)

    plan = resolve_dispatch(ctx, role=role, target=target)

    # session_id still names the run (so a fresh run is itself resumable), but
    # resume is refused despite the perfect id match.
    assert plan.resume is False
    assert plan.session_id == sid


# --------------------------------------------------------------------------
# 5. DETERMINISM WALL (unit-level).
#    No two distinct work identities may collide — this is what makes the
#    verify-or-fresh wall (cases 1-3) true BY CONSTRUCTION rather than by luck.
# --------------------------------------------------------------------------
def test_derive_session_id_no_cross_identity_collisions():
    """Distinct (ticket, role, target) identities must derive pairwise-distinct ids."""
    tickets = [1, 2]
    roles = ["planner", "reviewer-spec", "fixer-spec"]
    targets = [None, "spec", "impl"]
    run_key = "1"  # hold run_key fixed; we are pinning the work-identity scoping.

    ids: dict[str, tuple[int, str, str | None]] = {}
    collisions: list[tuple[tuple, tuple, str]] = []
    for ticket_id, role, target in itertools.product(tickets, roles, targets):
        sid = derive_session_id(ticket_id, role, target, run_key)
        if sid in ids:
            collisions.append(((ticket_id, role, target), ids[sid], sid))
        else:
            ids[sid] = (ticket_id, role, target)

    matrix_size = len(tickets) * len(roles) * len(targets)
    assert collisions == [], f"session-id collisions across distinct work: {collisions}"
    assert len(ids) == matrix_size  # every identity got a unique id
