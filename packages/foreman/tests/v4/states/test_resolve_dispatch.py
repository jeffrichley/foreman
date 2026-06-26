"""resolve_dispatch — the fresh-vs-verified-resume healer (SAFETY-CRITICAL).

These tests ARE the safety proof. The cardinal rule under test: resume ONLY
on an exact, verified identity match of an *interrupted* same-work attempt;
every other path must fall back to a FRESH run (fresh is always safe because
Stage-1 idempotency makes a redo harmless). There is no positional /
"latest session" / "best guess" path.

The records are built through the real repository write methods
(``open_state_instance`` / ``mark_execute_started`` / ``set_session_id`` /
``record_failure`` / ``close_state_instance`` / ``mark_execute_completed``)
so the journal rows are exactly the shape production produces.
"""
from __future__ import annotations

import datetime as dt

from foreman.v4.outcome import OutcomeKind
from foreman.v4.records import StateInstanceRecord
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.session_ids import derive_session_id
from foreman.v4.state import StateContext
from foreman.v4.states.resolve_dispatch import DispatchPlan, resolve_dispatch

_T0 = dt.datetime(2026, 6, 25, 12, 0, 0)
_ROLE = "planner"
_TARGET = None
_STATE = "Planning"


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


def _make_repo_and_ticket() -> tuple[InMemoryTicketRepository, int]:
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=_T0)
    return repo, ticket.id


# --------------------------------------------------------------------------
# 1. No prior same-state attempt → fresh, session_id = derived.
# --------------------------------------------------------------------------
def test_no_prior_attempt_is_fresh():
    repo, ticket_id = _make_repo_and_ticket()
    current = _open(repo, ticket_id=ticket_id, state_name=_STATE, sequence=1)
    ctx = _ctx_for(repo, ticket_id=ticket_id, instance=current)

    plan = resolve_dispatch(ctx, role=_ROLE, target=_TARGET)

    expected_sid = derive_session_id(ticket_id, _ROLE, _TARGET, "1")
    assert plan == DispatchPlan(session_id=expected_sid, resume=False)


# --------------------------------------------------------------------------
# 2. Prior same-state attempt that NEVER STARTED (execute_started_at NULL)
#    → fresh (zero side-effect risk; never resume a never-started run).
# --------------------------------------------------------------------------
def test_prior_never_started_is_fresh():
    repo, ticket_id = _make_repo_and_ticket()
    # prior row opened but never marked execute-started, then crash-closed
    prior = _open(repo, ticket_id=ticket_id, state_name=_STATE, sequence=1)
    repo.record_failure(
        prior.id, now=_at(1), failure_phase="crash_recovery", failure_reason="x"
    )
    repo.close_state_instance(prior.id, now=_at(1))

    current = _open(repo, ticket_id=ticket_id, state_name=_STATE, sequence=2)
    ctx = _ctx_for(repo, ticket_id=ticket_id, instance=current)

    plan = resolve_dispatch(ctx, role=_ROLE, target=_TARGET)

    # run_key is the first row of the consecutive Planning run = sequence 1.
    expected_sid = derive_session_id(ticket_id, _ROLE, _TARGET, "1")
    assert plan == DispatchPlan(session_id=expected_sid, resume=False)


# --------------------------------------------------------------------------
# 3. Prior INTERRUPTED attempt (started, never completed) with MATCHING
#    stored session_id → resume=True with that id.  The happy resume path.
# --------------------------------------------------------------------------
def test_prior_interrupted_matching_session_resumes():
    repo, ticket_id = _make_repo_and_ticket()
    run_key = "1"  # the run starts at sequence 1
    sid = derive_session_id(ticket_id, _ROLE, _TARGET, run_key)

    prior = _open(repo, ticket_id=ticket_id, state_name=_STATE, sequence=1)
    repo.mark_execute_started(prior.id, now=_at(1))
    repo.set_session_id(prior.id, sid)
    # crash: record_failure + close, but NO mark_execute_completed
    repo.record_failure(
        prior.id, now=_at(1), failure_phase="crash_recovery", failure_reason="x"
    )
    repo.close_state_instance(prior.id, now=_at(1))

    current = _open(repo, ticket_id=ticket_id, state_name=_STATE, sequence=2)
    ctx = _ctx_for(repo, ticket_id=ticket_id, instance=current)

    plan = resolve_dispatch(ctx, role=_ROLE, target=_TARGET)

    assert plan == DispatchPlan(session_id=sid, resume=True)


# --------------------------------------------------------------------------
# 4. Prior attempt that COMPLETED (has outcome_kind / execute_completed_at)
#    → fresh.  A completed session must never bleed into a re-run.
# --------------------------------------------------------------------------
def test_prior_completed_is_fresh():
    repo, ticket_id = _make_repo_and_ticket()
    run_key = "1"
    sid = derive_session_id(ticket_id, _ROLE, _TARGET, run_key)

    prior = _open(repo, ticket_id=ticket_id, state_name=_STATE, sequence=1)
    repo.mark_execute_started(prior.id, now=_at(1))
    repo.set_session_id(prior.id, sid)
    # completed: outcome recorded → execute_completed_at + outcome_kind set
    repo.mark_execute_completed(
        prior.id,
        now=_at(1),
        outcome_kind=OutcomeKind.CLEAN,
        outcome_payload={},
        next_state="SpecReview",
    )
    repo.close_state_instance(prior.id, now=_at(1))

    current = _open(repo, ticket_id=ticket_id, state_name=_STATE, sequence=2)
    ctx = _ctx_for(repo, ticket_id=ticket_id, instance=current)

    plan = resolve_dispatch(ctx, role=_ROLE, target=_TARGET)

    assert plan == DispatchPlan(session_id=sid, resume=False)


# --------------------------------------------------------------------------
# 5. Prior INTERRUPTED attempt but stored session_id does NOT match the
#    derived id → fresh.  Corruption-prevention: a stale/wrong id never
#    resumes (the verify-or-fresh wall).
# --------------------------------------------------------------------------
def test_prior_interrupted_mismatched_session_is_fresh():
    repo, ticket_id = _make_repo_and_ticket()

    prior = _open(repo, ticket_id=ticket_id, state_name=_STATE, sequence=1)
    repo.mark_execute_started(prior.id, now=_at(1))
    # store a WRONG id (e.g. some other work's session) — never matches.
    repo.set_session_id(prior.id, "deadbeef-0000-0000-0000-000000000000")
    repo.record_failure(
        prior.id, now=_at(1), failure_phase="crash_recovery", failure_reason="x"
    )
    repo.close_state_instance(prior.id, now=_at(1))

    current = _open(repo, ticket_id=ticket_id, state_name=_STATE, sequence=2)
    ctx = _ctx_for(repo, ticket_id=ticket_id, instance=current)

    plan = resolve_dispatch(ctx, role=_ROLE, target=_TARGET)

    expected_sid = derive_session_id(ticket_id, _ROLE, _TARGET, "1")
    assert plan == DispatchPlan(session_id=expected_sid, resume=False)


# --------------------------------------------------------------------------
# 6. State CHANGED between attempts (different state_name) → the prior is
#    NOT part of the current consecutive-same-state run → fresh.  This is the
#    rule that stops a completed Planning session from bleeding into SpecReview.
# --------------------------------------------------------------------------
def test_state_changed_breaks_run_is_fresh():
    repo, ticket_id = _make_repo_and_ticket()

    # A prior, interrupted Planning attempt — but it belongs to an EARLIER run.
    planning = _open(repo, ticket_id=ticket_id, state_name="Planning", sequence=1)
    repo.mark_execute_started(planning.id, now=_at(1))
    # store the id it would have for the Planning run — must NOT be picked up
    # by a SpecReview dispatch.
    repo.set_session_id(
        planning.id, derive_session_id(ticket_id, "planner", None, "1")
    )
    repo.record_failure(
        planning.id, now=_at(1), failure_phase="crash_recovery", failure_reason="x"
    )
    repo.close_state_instance(planning.id, now=_at(1))

    # Now the ticket is in SpecReview (different state) — current instance.
    current = _open(repo, ticket_id=ticket_id, state_name="SpecReview", sequence=2)
    ctx = _ctx_for(repo, ticket_id=ticket_id, instance=current)

    plan = resolve_dispatch(ctx, role="reviewer-spec", target="spec")

    # run_key for the SpecReview run = its own first sequence (2); no prior
    # same-state attempt in this run → fresh.
    expected_sid = derive_session_id(ticket_id, "reviewer-spec", "spec", "2")
    assert plan == DispatchPlan(session_id=expected_sid, resume=False)


# --------------------------------------------------------------------------
# 7. Resume bound: >= 2 interrupted priors in the run → fresh (don't
#    re-resume a poison session).  Mirrors MAX_HEAL_ACTIONS; bound = 1.
# --------------------------------------------------------------------------
def test_resume_bound_exhausted_is_fresh():
    repo, ticket_id = _make_repo_and_ticket()
    sid = derive_session_id(ticket_id, _ROLE, _TARGET, "1")

    # Two prior interrupted attempts in the SAME Planning run, both with the
    # matching session id (so only the bound stops the resume).
    for seq in (1, 2):
        prior = _open(repo, ticket_id=ticket_id, state_name=_STATE, sequence=seq)
        repo.mark_execute_started(prior.id, now=_at(seq))
        repo.set_session_id(prior.id, sid)
        repo.record_failure(
            prior.id, now=_at(seq), failure_phase="crash_recovery",
            failure_reason="x",
        )
        repo.close_state_instance(prior.id, now=_at(seq))

    current = _open(repo, ticket_id=ticket_id, state_name=_STATE, sequence=3)
    ctx = _ctx_for(repo, ticket_id=ticket_id, instance=current)

    plan = resolve_dispatch(ctx, role=_ROLE, target=_TARGET)

    # session_id still names the run, but resume is refused — poison-session bound.
    assert plan == DispatchPlan(session_id=sid, resume=False)


# --------------------------------------------------------------------------
# Bonus safety: exactly ONE matching interrupted prior still resumes
# (proves the bound is at >= 2, not >= 1 — the resume arm must work once).
# --------------------------------------------------------------------------
def test_single_interrupted_prior_still_resumes():
    repo, ticket_id = _make_repo_and_ticket()
    sid = derive_session_id(ticket_id, _ROLE, _TARGET, "1")

    prior = _open(repo, ticket_id=ticket_id, state_name=_STATE, sequence=1)
    repo.mark_execute_started(prior.id, now=_at(1))
    repo.set_session_id(prior.id, sid)
    repo.record_failure(
        prior.id, now=_at(1), failure_phase="crash_recovery", failure_reason="x"
    )
    repo.close_state_instance(prior.id, now=_at(1))

    current = _open(repo, ticket_id=ticket_id, state_name=_STATE, sequence=2)
    ctx = _ctx_for(repo, ticket_id=ticket_id, instance=current)

    plan = resolve_dispatch(ctx, role=_ROLE, target=_TARGET)

    assert plan == DispatchPlan(session_id=sid, resume=True)
