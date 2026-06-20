"""Exponential-backoff schedule for transient provider failures (foreman#361).

One module-level constant + one pure helper. The state machine's
``RoleDispatchState`` Template Method override calls
:func:`next_retry_delay` after observing a ``TRANSIENT_PROVIDER_ERROR``
outcome to decide whether to suspend the ticket (``int`` delay returned)
or escalate to ``NeedsHelp`` (``None`` returned).

Schedule semantics
------------------

One "attempt" equals one observed ``TRANSIENT_PROVIDER_ERROR`` outcome
on the same ticket without an intervening non-transient outcome. The
count of prior consecutive transient outcomes feeds directly into
:func:`next_retry_delay`:

* attempt 0 (first transient ever observed on the ticket) → 30s
* attempt 1 (second consecutive transient) → 2m
* attempt 2 (third consecutive transient) → 10m
* attempt 3 (fourth consecutive transient) → 30m
* attempt 4 or more → ``None`` (the schedule has exhausted; the state
  machine should route the ticket to ``NeedsHelp``)

A successful retry mid-backoff (any non-transient outcome) resets the
counter to zero — the next transient on the ticket starts at 30s again.
This is enforced by the ``count_consecutive_*`` walk on the repository
side, not here; this module is purely a stateless schedule lookup.
"""

from __future__ import annotations

#: The backoff schedule. Indexed by ``attempt`` (number of PRIOR
#: consecutive transient-provider-error outcomes already observed on
#: the ticket). Values are in seconds: 30s, 2m, 10m, 30m.
BACKOFF_SCHEDULE_SECONDS: tuple[int, ...] = (30, 120, 600, 1800)


def next_retry_delay(attempt: int) -> int | None:
    """Return the backoff delay (in seconds) for the given ``attempt``.

    ``attempt`` is the number of consecutive ``TRANSIENT_PROVIDER_ERROR``
    outcomes already observed on the ticket. See the module docstring
    for the full semantics.

    Returns ``None`` once the schedule has exhausted (4 attempts
    observed; the next one is the escalation). Callers route the
    ticket to ``NeedsHelp`` on ``None``.
    """
    if attempt < 0:
        # Defensive — a negative attempt shouldn't reach this helper
        # under normal use, but we should not silently translate it
        # to a 30s delay either.
        raise ValueError(f"attempt must be >= 0, got {attempt!r}")
    if attempt >= len(BACKOFF_SCHEDULE_SECONDS):
        return None
    return BACKOFF_SCHEDULE_SECONDS[attempt]
