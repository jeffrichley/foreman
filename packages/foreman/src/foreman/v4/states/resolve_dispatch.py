"""resolve_dispatch — the fresh-vs-verified-resume healer (SAFETY-CRITICAL).

Stage 2 of the crash-recovery resume arm makes a safe re-run *cheap* by
continuing the role's interrupted Claude session instead of restarting it.
This module is the pure DECISION function — analogous to ``attempt_merge`` for
the merge states — that decides, at the dispatch step, whether a role-dispatch
should run FRESH or RESUME.

Cardinal rule
-------------
A *wrong* resume is catastrophic: it loads an agent with the wrong role's or
ticket's entire context. So we **resume ONLY on an exact, verified identity
match of an interrupted same-work attempt; every other path → fresh.** Fresh is
always safe — Stage-1 idempotency (reconciliation + healer guard) makes a redo
harmless. There is deliberately NO positional / "latest session" / "best guess"
path: when in doubt, fresh.

The four independent walls (per the design's "Anti-mixing safeguards"):

1. **Scoped id by construction** — ``derive_session_id(ticket, role, target,
   run_key)`` cannot compute another role's / ticket's / target's id.
2. **Verify-or-fresh (this module)** — resume only on an exact match of an
   *interrupted* same-work attempt within the *current consecutive-same-state
   run*; any mismatch → fresh.
3. **cwd wall** — Claude stores transcripts under a per-(ticket,target)
   worktree directory, and an explicit ``session_id`` is always passed.

Routing by ``execute_started_at`` (the crash-timing split):

* prior with ``execute_started_at IS NULL`` → role never started → fresh
  (zero side-effect risk);
* prior with ``execute_started_at`` set but never completed → interrupted →
  resume *iff* the stored id matches and the resume bound is not exhausted.

"Interrupted" vs "completed"
----------------------------
The journal marks completion via ``mark_execute_completed``, which sets BOTH
``execute_completed_at`` and ``outcome_kind`` together (atomically). So a row
is *completed* iff ``execute_completed_at is not None``; an *interrupted*
attempt is one that ``mark_execute_started`` touched (``execute_started_at is
not None``) but ``mark_execute_completed`` never reached
(``execute_completed_at is None``). ``record_failure`` / ``close_state_instance``
(written by the crash-recovery reconciliation pass) do NOT set
``execute_completed_at``, so a crash-closed orphan still reads as interrupted —
which is exactly the row we may resume.

Unit of resumability = the consecutive-same-state run
-----------------------------------------------------
NOT the raw ``state_instance_id`` (which changes on each retry: orphan row X is
closed, new row Y opened). The ``run_key`` is the sequence number of the FIRST
instance in the trailing contiguous block of same-state rows (computed with the
SAME skip/break window as ``count_consecutive_same_state``). It is stable across
retries within a run and changes when the state changes — so a *completed*
Planning session can never bleed into a later SpecReview run.
"""
from __future__ import annotations

from dataclasses import dataclass

from foreman.v4.outcome import OutcomeKind
from foreman.v4.records import (
    FAILURE_PHASE_CRASH_RECOVERY,
    StateInstanceRecord,
)
from foreman.v4.session_ids import derive_session_id
from foreman.v4.state import StateContext

#: Maximum number of resume attempts before falling back to fresh. Mirrors the
#: ``MAX_HEAL_ACTIONS`` discipline: a poison session must never loop forever.
#: Bound = 1 (per Jeff's decision) — we resume at most ONCE per run. The signal
#: for "a resume was already tried" is the count of interrupted prior attempts
#: in the run; once two or more interrupted priors exist, the first re-run
#: already had its resume chance, so we stop and go fresh.
_MAX_INTERRUPTED_PRIORS_FOR_RESUME = 1


@dataclass(frozen=True)
class DispatchPlan:
    """The dispatch decision.

    ``session_id`` is ALWAYS set — it names the session even on a fresh run, so
    the run can be resumed if *it* later crashes. ``resume`` gates whether the
    dispatcher passes ``--resume`` to continue an interrupted prior session.
    """

    session_id: str
    resume: bool


def _is_skipped(inst: StateInstanceRecord) -> bool:
    """Rows that ``count_consecutive_same_state`` neither counts nor breaks on.

    Must stay in lockstep with ``count_consecutive_same_state`` so this module's
    run window is identical to the one the runaway-defense cap uses.
    """
    if inst.failure_phase == "can_run":
        return True
    if inst.failure_phase == FAILURE_PHASE_CRASH_RECOVERY:
        return True
    if inst.outcome_kind == OutcomeKind.BLOCKED:
        return True
    if inst.outcome_kind == OutcomeKind.TRANSIENT_PROVIDER_ERROR:
        return True
    return False


def _current_run(
    instances: list[StateInstanceRecord],
    *,
    current: StateInstanceRecord,
    state_name: str,
) -> list[StateInstanceRecord]:
    """Return the trailing consecutive-same-state run up to ``current``.

    ``instances`` must be sorted ascending by sequence. We walk back from the
    current instance (inclusive) using the SAME window as
    ``count_consecutive_same_state``: a *skipped* row (``can_run`` /
    ``crash_recovery`` / ``BLOCKED`` / ``TRANSIENT_PROVIDER_ERROR``) is
    transparent — it neither extends nor breaks the run; the walk continues past
    it. Only a NON-skipped row whose ``state_name`` differs breaks the run.

    Crucially, a skipped row of the SAME ``state_name`` (e.g. a crash_recovery
    orphan from the dead process — the very row we may resume) STAYS in the run.
    That is what keeps ``run_key`` stable across the crash: the orphan stored its
    id with the run's first-sequence; the re-run must recompute the same
    first-sequence to match. Treating the crash orphan as a run-breaker would
    bump ``run_key`` (1 → 2) and the stored id could never match → resume would
    be silently impossible.

    Returned list is ascending-sequence and includes the current instance.
    """
    # Consider only rows at or before the current instance's sequence. The
    # current instance is the in-flight row the dispatch is running for.
    upto = [i for i in instances if i.sequence <= current.sequence]
    run: list[StateInstanceRecord] = []
    for inst in reversed(upto):  # newest → oldest
        if inst.state_name == state_name:
            # Same-state row — part of this run (whether skipped or not).
            run.append(inst)
            continue
        # Different state.
        if inst.id != current.id and _is_skipped(inst):
            # A skipped different-state row is transparent: walk past it
            # without extending or breaking the run (mirrors the cap window).
            continue
        # A non-skipped, different-state row is the run boundary.
        break
    run.reverse()  # back to ascending-sequence order
    return run


def resolve_dispatch(
    ctx: StateContext, *, role: str, target: str | None
) -> DispatchPlan:
    """Decide whether this role-dispatch runs FRESH or RESUMEs.

    ``role`` and ``target`` name the work identity together with the ticket id;
    the caller (the role-dispatch state) supplies them — ``role`` is the state's
    own ``role`` attr (e.g. ``"reviewer-spec"``) and ``target`` is the
    spec/impl distinction (or ``None``).

    Returns a :class:`DispatchPlan` whose ``session_id`` is always the id
    derived for the *current run*, and whose ``resume`` is ``True`` only when an
    interrupted, identity-matched, bound-respecting prior attempt exists.
    """
    repo = ctx.repo
    current = ctx.instance
    state_name = current.state_name

    instances = repo.list_state_instances_for_ticket(ctx.ticket.id)
    run = _current_run(instances, current=current, state_name=state_name)

    # run_key = sequence of the FIRST instance in the run. Stable across retries
    # within the run; changes when the state changes (a new run starts).
    run_key = str(run[0].sequence) if run else str(current.sequence)

    session_id = derive_session_id(ctx.ticket.id, role, target, run_key)

    # Prior attempts in THIS run, excluding the current (in-flight) instance.
    priors = [i for i in run if i.id != current.id]

    # --- Resume bound (poison-session guard). ---
    # Count interrupted prior attempts in the run (started but never completed).
    # If we've already had our one resume chance (>= 1 interrupted prior means a
    # re-run already happened; a second interrupted prior means that re-run also
    # crashed), stop resuming and go fresh — fresh is always safe.
    interrupted_priors = [
        i
        for i in priors
        if i.execute_started_at is not None and i.execute_completed_at is None
    ]
    if len(interrupted_priors) > _MAX_INTERRUPTED_PRIORS_FOR_RESUME:
        return DispatchPlan(session_id=session_id, resume=False)

    # --- Inspect the single MOST RECENT prior attempt in the run. ---
    # We do NOT skip past a non-interrupted newer prior to reach an older
    # interrupted one: a newer never-started/completed attempt means the run has
    # already moved past any resumable point, so the safe answer is fresh.
    if not priors:
        return DispatchPlan(session_id=session_id, resume=False)
    candidate = priors[-1]  # highest sequence (priors is ascending)

    # Routing by execute_started_at: never started → fresh (zero side effects).
    if candidate.execute_started_at is None:
        return DispatchPlan(session_id=session_id, resume=False)
    # Completed → its session is done; never resume into a finished session.
    if candidate.execute_completed_at is not None:
        return DispatchPlan(session_id=session_id, resume=False)

    # --- Verify-or-fresh wall: exact stored-id match required. ---
    if candidate.session_id != session_id:
        return DispatchPlan(session_id=session_id, resume=False)

    return DispatchPlan(session_id=session_id, resume=True)
