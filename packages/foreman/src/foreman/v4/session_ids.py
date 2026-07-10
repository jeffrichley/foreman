"""Deterministic Claude-session-id derivation for crash-recovery resume.

Stage 2 of the crash-recovery resume arm resumes an interrupted role's Claude
session. To resume, the session must be NAMED with a stable id, and that id
must be a pure function of the work identity plus the current run — so a crash
re-run of the *same* work recomputes the *same* id (and can therefore resume),
while any *different* work computes a *different* id (and therefore can't).

The id is bound to four inputs:

``ticket_id``, ``role``, ``target``
    The work identity. This binding is a **load-bearing anti-session-mixing
    guard**: because the id is derived from all three, a different role,
    ticket, or target produces a different id *by construction*. No role's
    dispatch can ever compute another role's session id, so a resume can never
    cross from (say) the planner's session into the fixer's, or from ticket 1's
    work into ticket 2's. This safety property is structural, not enforced by a
    runtime check.

``run_key``
    The consecutive-same-state run identity — which run *within* a given work
    identity this is. A later task in the resume arm supplies it (e.g. a
    monotonic counter of how many times the daemon has entered this state for
    this ticket/role). Folding it into the id means a fresh run of the same
    work gets a fresh session, while a crash *mid-run* recomputes the same
    ``run_key`` and so the same session id → resume.

Reproducibility constraint
--------------------------
The namespace UUID ``_NS`` is a hardcoded literal, NOT a value generated at
import or call time. ``uuid.uuid4()``/``uuid.uuid1()`` would produce a
different namespace in every process, which would break the entire point:
ids must be reproducible across processes (the crashed run and its re-run are
different processes). Keep ``_NS`` a constant baked into source.
"""

from __future__ import annotations

import uuid

# Fixed namespace for all session-id derivations. Generated once (uuid4) and
# hardcoded as a literal so derivation is reproducible across processes. Do NOT
# replace with a runtime uuid4()/uuid1() call — see module docstring.
_NS = uuid.UUID("d4110f98-5275-4dfb-8982-32298b276968")


def _norm_target(target: str | None) -> str:
    """Normalize ``target`` so ``None`` is a stable, distinct token.

    Maps ``None`` to the literal ``"none"`` (rather than crashing or producing
    an empty segment that could collide with a real empty target). Any other
    value passes through unchanged.
    """
    return "none" if target is None else target


def derive_session_id(
    ticket_id: int,
    role: str,
    target: str | None,
    run_key: str,
) -> str:
    """Derive the stable Claude-session id for one role run.

    Deterministic: identical args always return the same id (resume), and any
    change to ``ticket_id`` / ``role`` / ``target`` / ``run_key`` returns a
    different id (no cross-session mixing). See module docstring for the
    safety rationale.

    Returns the string form of a UUIDv5 over ``_NS`` and the
    ``"{ticket_id}:{role}:{target}:{run_key}"`` name.
    """
    name = f"{ticket_id}:{role}:{_norm_target(target)}:{run_key}"
    return str(uuid.uuid5(_NS, name))
