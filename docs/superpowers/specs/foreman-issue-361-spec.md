# Spec: classify Anthropic transient failures + exponential-backoff retry without burning state cap (issue #361)

## Goal
Distinguish Anthropic-side transient failures (5xx / 429 / connection blip
/ network timeout) from genuine role failures, retry them on an exponential-
backoff schedule WITHOUT consuming the runaway-defense `max_state_attempts`
cap, and only escalate to `NeedsHelp` once the backoff schedule exhausts.
The change preserves the existing cap semantics for real role failures
(low-confidence Planner, Reviewer churn) while making the loop tolerant of
short Anthropic outages. Tracks
[foreman#361](https://github.com/jeffrichley/foreman/issues/361).

## Acceptance criteria
- [ ] `OutcomeKind` in `packages/foreman/src/foreman/v4/outcome.py` gains
  one new variant: `TRANSIENT_PROVIDER_ERROR = "transient_provider_error"`.
  The class docstring lists it alongside the existing five values, and the
  per-role outcome-kind matrix in
  `docs/superpowers/specs/foreman-v1-architectural-spec.md` is updated to
  note that every role-dispatch state may emit it.
- [ ] `ProviderTransientError(ProviderError)` is added in
  `packages/foreman/src/foreman/providers/exceptions.py` with a docstring
  that says: "Anthropic-side transient transport failure — 5xx, 429,
  connection refused, transport-level timeout. Re-raised at the provider
  boundary; roles must surface it as
  `Outcome(kind=TRANSIENT_PROVIDER_ERROR)`, not `ERROR`." Re-exported from
  `packages/foreman/src/foreman/providers/__init__.py` (`__all__`) so role
  runners import it via `from foreman.providers import
  ProviderTransientError`.
- [ ] `_translate_sdk_exception` in
  `packages/foreman/src/foreman/providers/anthropic_sdk.py` classifies the
  following as `ProviderTransientError` BEFORE the existing fallthrough to
  `ProviderUnknownError`:
  * Any exception whose `str(exc)` matches the
    `_SDK_CLAUDE_CODE_PREFIX` envelope AND contains any of the substrings
    `"429"`, `"rate_limit"`, `"rate limit"`, `"overloaded"`, `"503"`,
    `"502"`, `"504"`, `"500"`, `"connection refused"`,
    `"connection reset"`, `"connection aborted"`,
    `"timeout"` (case-insensitive).
  * Best-effort `isinstance` check against `anthropic.APIConnectionError`,
    `anthropic.RateLimitError`, `anthropic.APITimeoutError`, and
    `anthropic.InternalServerError`. Import is guarded by
    `try: import anthropic` at module load; if `anthropic` isn't
    importable (test stub environments) the isinstance branch is a no-op
    and only the string-pattern path is active.
  * `asyncio.TimeoutError` keeps mapping to `ProviderTimeoutError`
    (existing behavior); the new transient classifier is a **second**
    branch placed AFTER the timeout branch and BEFORE the auth-prefix
    branch — order is documented in the function docstring.
- [ ] All four role CLI entry points in `packages/foreman/src/foreman/roles/`
  (`planner.py:run_planner_cli`, `reviewer.py:run_reviewer_cli`,
  `worker.py:run_worker_cli`, `fixer.py:run_fixer_cli`) catch
  `ProviderTransientError` in a NEW `except` arm placed BEFORE the
  catch-all `except Exception`. The arm emits
  `Outcome(kind=OutcomeKind.TRANSIENT_PROVIDER_ERROR,
  confidence=OutcomeConfidence.HIGH,
  summary=f"provider transient failure: {exc}"[:500],
  details={"provider_status": str(exc), "exception_class":
  type(exc.__cause__).__name__ if exc.__cause__ else
  type(exc).__name__})` and returns exit code `0` (the outcome carries
  the signal; non-zero would trip `RoleSubprocessError` in the dispatcher
  and lose the discriminator).
- [ ] `BACKOFF_SCHEDULE_SECONDS = (30, 120, 600, 1800)` is defined as a
  module-level constant in a new file
  `packages/foreman/src/foreman/v4/backoff.py` along with a pure helper
  `next_retry_delay(attempt: int) -> int | None` that returns
  `BACKOFF_SCHEDULE_SECONDS[attempt]` for `attempt in [0, 3]` and
  `None` for `attempt >= 4`. Module docstring documents that one
  "attempt" equals one observed `TRANSIENT_PROVIDER_ERROR` outcome on
  the same ticket without an intervening non-transient outcome.
- [ ] `tickets` table in
  `packages/foreman/src/foreman/v4/schema.sql` gains a nullable
  `next_action_at TEXT` column (ISO 8601 UTC string, semantics: "Poller
  must not enqueue this ticket until at least this wall-clock time").
- [ ] `SqliteTicketRepository.__init__` in
  `packages/foreman/src/foreman/v4/sqlite_repository.py` runs a one-shot
  additive migration AFTER `executescript`: query
  `PRAGMA table_info(tickets)`; if `next_action_at` is missing, execute
  `ALTER TABLE tickets ADD COLUMN next_action_at TEXT`. This mirrors the
  pattern intended for the `depends_on` column add and keeps existing
  on-disk DBs forward-compatible.
- [ ] `TicketRecord` in `packages/foreman/src/foreman/v4/records.py`
  gains `next_action_at: dt.datetime | None = None` (defaulted so the
  many test fixtures that build `TicketRecord` directly without the
  new field stay green).
- [ ] `TicketRepository` Protocol in
  `packages/foreman/src/foreman/v4/repository.py` grows two methods:
  ```python
  def set_next_action_at(self, ticket_id: int, *, when: dt.datetime) -> None: ...
  def clear_next_action_at(self, ticket_id: int) -> None: ...
  ```
  with matching implementations in both `InMemoryTicketRepository` and
  `SqliteTicketRepository`. Both impls also include `next_action_at` in
  the result of `_ticket_row_to_record` / the in-memory record
  construction so reads round-trip.
- [ ] `_ticket_row_to_record` reads `next_action_at` from the row via
  `_from_iso(row["next_action_at"])`. `set_next_action_at` issues
  `UPDATE tickets SET next_action_at = ?, updated_at = ? WHERE id = ?`;
  `clear_next_action_at` issues
  `UPDATE tickets SET next_action_at = NULL, updated_at = ? WHERE id = ?`.
  Both commit immediately and acquire `self._lock`.
- [ ] `Poller._enqueue_open_tickets` in
  `packages/foreman/src/foreman/v4/poller.py` skips tickets whose
  `next_action_at` is not None AND `> self._clock()`. Tickets whose
  `next_action_at <= now` are enqueued normally (no separate
  clear-on-poll — clearing happens at successful execution; see below).
- [ ] `RoleDispatchState.next_state` in
  `packages/foreman/src/foreman/v4/states/role_dispatch.py` becomes a
  Template Method that intercepts `TRANSIENT_PROVIDER_ERROR` before
  delegating to `next_state_for`. When the outcome kind is
  `TRANSIENT_PROVIDER_ERROR`:
  1. Count consecutive `TRANSIENT_PROVIDER_ERROR` outcomes on this
     ticket via a new repo helper
     `count_consecutive_transient_provider_errors(ticket_id) -> int`
     (same shape as `count_consecutive_same_state`).
  2. If `next_retry_delay(consecutive)` is not None: call
     `ctx.repo.set_next_action_at(ctx.ticket.id,
     when=ctx.clock() + dt.timedelta(seconds=delay))`, publish a
     `TransientProviderErrorEvent` via `ctx.bus`, and return a fresh
     instance of `self.__class__()` (re-enter the same state, no
     cap-burn).
  3. If `next_retry_delay(consecutive)` returns None: the backoff is
     exhausted. Return `NeedsHelpState()` with no extra side effects —
     `ExecuteCompletedEvent`'s outcome already carries the
     `provider_status` details and the state-machine's existing
     terminal-landing plumbing emits the label.
- [ ] `count_consecutive_transient_provider_errors` is added to the
  `TicketRepository` Protocol and both impls. The implementation walks
  `state_instances` for `ticket_id` ORDER BY `sequence DESC` and counts
  rows whose `outcome_kind == TRANSIENT_PROVIDER_ERROR.value`. Rows
  whose `failure_phase == "can_run"` are skipped (same precedent as
  `count_consecutive_same_state`); any other outcome kind breaks the
  run (resets the count). A consecutive sequence interrupted by a
  CLEAN / NEEDS_FIX / NEEDS_HELP / BLOCKED / ERROR outcome restarts
  from zero — so a successful retry mid-backoff resets the budget.
- [ ] `count_consecutive_same_state` in both repository impls is
  extended to skip rows whose `outcome_kind ==
  TRANSIENT_PROVIDER_ERROR.value` — the same precedent as the
  Phase 8d.18 BLOCKED skip on lines 388–397 of
  `sqlite_repository.py` and lines 102–109 of `repository.py`. The
  inline comment for the new skip explicitly names "foreman#361 —
  transient-provider self-loops are not runaway-defense signal" so the
  intent is grep-able.
- [ ] On any non-transient outcome from a role-dispatch state's
  successful execute (CLEAN / NEEDS_FIX / etc.), the
  `RoleDispatchState.next_state` template ALSO calls
  `ctx.repo.clear_next_action_at(ctx.ticket.id)` — defense-in-depth so
  a stale suspension never outlives a successful retry. (The transient
  branch sets a fresh `next_action_at`; the non-transient branch
  clears it.)
- [ ] `TransientProviderErrorEvent` is added in
  `packages/foreman/src/foreman/v4/events.py` carrying the standard
  `Event` envelope fields plus `attempt: int`,
  `next_retry_at: dt.datetime | None` (None when the schedule has
  exhausted and the state is escalating to NeedsHelp), and
  `provider_status: str` (verbatim from
  `outcome.details["provider_status"]`).
- [ ] `StructuredLogObserver._EVENT_NAMES` in
  `packages/foreman/src/foreman/v4/observers/structured_log.py` gets
  one new entry:
  `TransientProviderErrorEvent: ("transient_provider_error", logging.WARNING)`.
  The `__call__` body already serializes any `dataclass.fields(event)`
  it walks; verify the new fields surface in the JSON payload.
- [ ] `LabelObservabilityObserver` is NOT modified — the state label
  on the GitHub issue stays at `foreman:state-planning` (or whichever
  role-dispatch state is suspended), which truthfully reflects the
  in-flight state. The suspension surfaces via `cmd_show` / `cmd_ps`
  output and the structured-log event.
- [ ] `cmd_retry` in
  `packages/foreman/src/foreman/v4/cli/mutations.py` calls
  `repo.clear_next_action_at(ticket_id)` BEFORE the existing
  `qm.enqueue(...)` so an operator-forced retry bypasses any active
  suspension. Output line gains a parenthetical
  `"(cleared next_action_at)"` when a suspension was active.
- [ ] `cmd_show` in `packages/foreman/src/foreman/v4/cli/queue.py`
  (and any matching `cmd_ps`) renders `next_action_at` when non-null
  as e.g. `suspended until 2026-06-20T14:25:00Z (provider-throttled,
  attempt 2/4)`. The "attempt N/4" hint comes from a fresh call to
  `count_consecutive_transient_provider_errors(ticket_id)`.
- [ ] New test file
  `packages/foreman/tests/providers/test_translate_sdk_transient.py`:
  one parametrized test that drives `_translate_sdk_exception` with
  representative strings from each pattern (`"... 429 Too Many ..."`,
  `"... 503 ..."`, `"connection refused"`, etc.) and asserts the
  return type is `ProviderTransientError`. A second parametrized test
  drives benign-looking strings (`"Claude Code returned an error
  result: success"`, plain `"some random failure"`) and asserts the
  return type is NOT `ProviderTransientError`.
- [ ] New test file
  `packages/foreman/tests/roles/test_planner_transient_outcome.py`:
  patches `_run_planner_for_v4` to raise `ProviderTransientError`,
  calls `run_planner_cli`, asserts the emitted FOREMAN_OUTCOME JSON
  parses to `kind="transient_provider_error"` with `details`
  populated AND the exit code is `0`. Repeat the test shape for the
  three other role CLIs in their existing test modules.
- [ ] New test file
  `packages/foreman/tests/v4/states/test_role_dispatch_transient.py`
  (one module, parametrized over Planning + Implementing + ImplFix as
  representatives of the role-dispatch family): seed a fake repo with
  a ticket in `Planning`. Drive three transient outcomes in a row;
  assert (a) `next_action_at` advances per the backoff schedule,
  (b) `count_consecutive_same_state` returns 0 (cap not burned),
  (c) `TransientProviderErrorEvent` fires with the right `attempt`
  and `next_retry_at`. Then drive a CLEAN outcome and assert
  `next_action_at` is cleared and the state advances to SpecReview
  (the normal Planning → SpecReview transition).
- [ ] New test
  `test_role_dispatch_transient_exhausts_to_needs_help` in the same
  file: drive 4 transient outcomes in a row; assert the 4th
  transition returns `NeedsHelpState()` and emits
  `TransientProviderErrorEvent(next_retry_at=None)`.
- [ ] New test
  `test_poller_skips_suspended_ticket` in
  `packages/foreman/tests/v4/test_poller.py`: seed a ticket with
  `next_action_at` 5 minutes in the future; call `Poller.tick()`;
  assert `qm.enqueue` was NOT called for that ticket. Then advance
  the clock past `next_action_at` and re-tick; assert the ticket is
  enqueued.
- [ ] New test `test_retry_clears_next_action_at` in
  `packages/foreman/tests/v4/cli/test_mutations.py`: invoke
  `cmd_retry` against a ticket with non-null `next_action_at`;
  assert `next_action_at` is None afterwards and the WorkItem was
  enqueued.
- [ ] `docs/RUNBOOK.md` gains a new section between "Daily operations"
  and "Recovery: daemon won't start" titled "Provider transient
  failures and backoff suspension" documenting (a) what
  `next_action_at` in `foreman show <id>` means, (b) the 30s / 2m /
  10m / 30m schedule, (c) how to verify "this is Anthropic, not us"
  by checking the `transient_provider_error` lines in the daemon's
  structured log, (d) `foreman retry <id>` as the operator override,
  and (e) when escalation to NeedsHelp legitimately means "Anthropic
  has been out for at least 40 minutes — page Anthropic status or
  switch to manual mode".

## Approach
**Pattern naming (Decision 4 — calibrated lens).** Two patterns
apply, plus one Google principle:

1. **Strategy refinement on Outcome.kind** — adding
   `TRANSIENT_PROVIDER_ERROR` extends the existing dispatch-on-kind
   strategy already used by every `RoleDispatchState.next_state_for`
   and by the Phase 8d.18 retry-cap exemption in
   `count_consecutive_same_state`. We are not inventing a new
   mechanism; we are widening the existing one to cover a new failure
   class.
2. **Template Method on `RoleDispatchState.next_state`** — the
   transient branch is a cross-cutting concern that applies
   identically to all six role-dispatch states (Planning, SpecReview,
   SpecFix, Implementing, ImplReview, ImplFix). Concentrating it in
   the base class avoids the alternative of six near-identical
   `next_state_for` updates that drift over time.
3. **"Make the right thing easy" (Google SRE)** — the backoff
   schedule lives in one module (`backoff.py`); transient-failure
   classification lives in one function (`_translate_sdk_exception`);
   the retry-cap exemption lives in one helper
   (`count_consecutive_same_state`). A future operator who wants to
   tune the schedule, tighten the classifier, or audit the exemption
   has exactly one place to look in each case.

**Why a new `OutcomeKind` variant vs. an `Outcome.details` flag.**
The state machine's retry-cap exemption (`count_consecutive_same_state`)
already discriminates on `outcome_kind`. The role-dispatch state's
routing (`next_state_for`) already discriminates on `outcome_kind`.
Adding a sibling enum value reuses both discriminators; using a
`details["provider_status"]` flag would require new conditionals in
both call sites AND would weaken the SQL-level filter
(`WHERE outcome_kind = 'transient_provider_error'`) that powers cheap
analytics. Precedent: Phase 8d.18's BLOCKED exemption uses exactly
this shape.

**Why a new `next_action_at` column vs. an in-memory schedule.** The
daemon restarts (Docker container rolls, host reboots, hot reload).
An in-memory schedule loses every pending suspension across a
restart, which is precisely the wrong behavior — a daemon that
restarts mid-Anthropic-outage would resume every suspended ticket
immediately, hammer the API, and trip its own cap. Persistence is
load-bearing. The schema migration is additive (nullable column with
an `ALTER TABLE ADD COLUMN` guarded by `PRAGMA table_info`), matching
the precedent for how `depends_on` was added later.

**Why exit code 0 from the role CLI on transient.** The
`SubprocessRoleDispatcher` treats any non-zero exit + missing
FOREMAN_OUTCOME marker as `RoleSubprocessError` — which would erase
our carefully-classified discriminator. By emitting the outcome and
returning 0, the dispatcher's verify hook parses the
`TRANSIENT_PROVIDER_ERROR` outcome cleanly and the state machine's
routing fires. The exit code is not the signal; the outcome JSON is.

**Why the Poller filters rather than the QueueManager.** The
QueueManager dedups by `WorkItem`; it doesn't know about wall-clock
time. The Poller already iterates open tickets and decides whether
to enqueue. Adding the `next_action_at > now` filter there is one
line at the natural decision point and preserves the
QueueManager's pure-set-semantics invariant.

**Granular pattern detection in `_translate_sdk_exception`.** The
Anthropic SDK exposes the transport through Claude Code as a
subprocess, so most transport errors surface as
`Exception("Claude Code returned an error result: ...")` strings.
String-pattern matching against the standard HTTP status numbers and
the small set of common transport-layer phrases is the load-bearing
detection path. The optional `isinstance(exc, anthropic.*)` branch
catches the rare case where a raw SDK exception leaks past the CLI
boundary (some `claude_agent_sdk` paths re-raise the underlying
`anthropic` exception directly); `try: import anthropic` guards
make this best-effort so test environments without `anthropic`
installed don't break import.

## Sub-requests (topologically sorted)
1. Add `OutcomeKind.TRANSIENT_PROVIDER_ERROR` in
   `packages/foreman/src/foreman/v4/outcome.py`. Update the per-role
   matrix in the architectural spec.
2. Add `ProviderTransientError` in
   `packages/foreman/src/foreman/providers/exceptions.py`; re-export
   from `providers/__init__.py.__all__`.
3. Extend `_translate_sdk_exception` in
   `packages/foreman/src/foreman/providers/anthropic_sdk.py` with the
   transient-classification branch (string patterns + best-effort
   `isinstance(anthropic.*)`).
4. Add new `backoff.py` module with `BACKOFF_SCHEDULE_SECONDS` +
   `next_retry_delay(attempt)`.
5. Add nullable `next_action_at TEXT` to `tickets` in `schema.sql`.
6. Add the additive `ALTER TABLE` migration in
   `SqliteTicketRepository.__init__`.
7. Add `next_action_at: dt.datetime | None = None` to `TicketRecord`.
8. Add `set_next_action_at` / `clear_next_action_at` /
   `count_consecutive_transient_provider_errors` to the Protocol and
   both repository impls. Round-trip `next_action_at` through
   `_ticket_row_to_record`.
9. Extend `count_consecutive_same_state` in both repository impls
   to skip `TRANSIENT_PROVIDER_ERROR` rows (precedent: BLOCKED skip).
10. Modify `Poller._enqueue_open_tickets` to filter
    suspended tickets.
11. Add `TransientProviderErrorEvent` in `events.py`; wire it into
    `StructuredLogObserver._EVENT_NAMES`.
12. Modify `RoleDispatchState.next_state` (in
    `states/role_dispatch.py`) to intercept
    `TRANSIENT_PROVIDER_ERROR` (schedule next attempt OR escalate to
    NeedsHelp) AND clear `next_action_at` on every other outcome
    kind.
13. Add the transient catch arm to each of the four role CLIs:
    `planner.py:run_planner_cli`, `reviewer.py:run_reviewer_cli`,
    `worker.py:run_worker_cli`, `fixer.py:run_fixer_cli`.
14. Extend `cmd_retry` (`v4/cli/mutations.py`) to call
    `clear_next_action_at` first; extend `cmd_show` to display
    suspensions.
15. Write the unit/integration tests enumerated in Acceptance.
16. Add the RUNBOOK section.

## File-level changes
- `packages/foreman/src/foreman/v4/outcome.py` — add
  `TRANSIENT_PROVIDER_ERROR` to `OutcomeKind`.
- `packages/foreman/src/foreman/providers/exceptions.py` — add
  `ProviderTransientError`.
- `packages/foreman/src/foreman/providers/__init__.py` — export
  `ProviderTransientError` in `__all__`.
- `packages/foreman/src/foreman/providers/anthropic_sdk.py` —
  extend `_translate_sdk_exception` classifier.
- `packages/foreman/src/foreman/v4/backoff.py` — NEW: the
  backoff schedule constant + helper.
- `packages/foreman/src/foreman/v4/schema.sql` — add
  `next_action_at` column to `tickets`.
- `packages/foreman/src/foreman/v4/sqlite_repository.py` —
  additive `ALTER TABLE` migration in `__init__`; new repository
  methods; extend `count_consecutive_same_state`; round-trip
  `next_action_at`.
- `packages/foreman/src/foreman/v4/repository.py` — Protocol
  additions; mirror methods in `InMemoryTicketRepository`; extend
  in-memory `count_consecutive_same_state`.
- `packages/foreman/src/foreman/v4/records.py` — add
  `next_action_at` to `TicketRecord`.
- `packages/foreman/src/foreman/v4/poller.py` —
  suspension filter in `_enqueue_open_tickets`.
- `packages/foreman/src/foreman/v4/events.py` — add
  `TransientProviderErrorEvent`.
- `packages/foreman/src/foreman/v4/observers/structured_log.py` —
  wire the new event class.
- `packages/foreman/src/foreman/v4/states/role_dispatch.py` —
  Template Method extension on `next_state`.
- `packages/foreman/src/foreman/roles/planner.py`,
  `reviewer.py`, `worker.py`, `fixer.py` — transient catch arm
  in each role's `run_*_cli`.
- `packages/foreman/src/foreman/v4/cli/mutations.py` —
  extend `cmd_retry`.
- `packages/foreman/src/foreman/v4/cli/queue.py` — display
  `next_action_at` in `cmd_show` / `cmd_ps`.
- `packages/foreman/tests/providers/test_translate_sdk_transient.py`
  — NEW classifier tests.
- `packages/foreman/tests/roles/test_planner_transient_outcome.py`
  — NEW role-CLI outcome test (and analogous additions to the
  existing reviewer/worker/fixer CLI test modules).
- `packages/foreman/tests/v4/states/test_role_dispatch_transient.py`
  — NEW state-machine routing + backoff + escalation tests.
- `packages/foreman/tests/v4/test_poller.py` — append
  `test_poller_skips_suspended_ticket`.
- `packages/foreman/tests/v4/cli/test_mutations.py` — append
  `test_retry_clears_next_action_at`.
- `packages/foreman/tests/v4/test_schema.py` — extend
  `test_tickets_table_columns` to include `next_action_at`.
- `docs/RUNBOOK.md` — new "Provider transient failures and
  backoff suspension" section.

## Alternatives considered
1. **Reuse `OutcomeKind.BLOCKED` + a `details["provider_status"]`
   marker.** Rejected: BLOCKED currently means "legitimate async
   poll in flight" (CI verdict pending, merge-queue pending);
   overloading it would conflate two semantically different things
   and weaken both the state-machine routing (a BLOCKED outcome
   would need a sub-discriminator to decide whether to schedule a
   backoff or just re-tick immediately) and the analytics value
   (`WHERE outcome_kind = 'blocked'` no longer means one thing).
2. **In-memory backoff schedule + WorkItem requeue with a `not-before`
   timestamp.** Rejected: loses suspensions across daemon restarts.
   A daemon restart during an Anthropic outage would resume every
   suspended ticket immediately, hammer the API, and trip the
   cap — the exact failure this ticket is trying to prevent.
3. **Per-role retry inside the role subprocess (sleep + retry in
   `run_planner_cli`).** Rejected: holds the subprocess open for up
   to 40 minutes per ticket, exhausts the worker pool's
   `max_in_flight` slots (currently 1 — see V4Config), and breaks
   the dispatcher's `role_timeout_seconds` budget. State-machine-
   level suspension correctly releases the worker slot back to the
   pool during the backoff window.
4. **Add a new top-level state (`Suspended` / `Throttled`) that
   tickets transition to.** Rejected: would create a new state to
   serialize in labels, new transitions to test, and would
   complicate the "what state is the ticket REALLY in?" question
   when an operator runs `foreman show`. The minimum useful
   surface is "the ticket is still in Planning, but its
   `next_action_at` is in the future." Suspension is a property of
   the ticket row, not a state.
5. **Do nothing — let `max_state_attempts = 3` absorb short
   outages.** Rejected: explicitly named in the issue as the
   current failure mode. A 30-second Anthropic blip during a busy
   hour burns the cap on multiple tickets within seconds; this is
   not outage tolerance.

## Open questions
None. The acceptance criteria, file paths, classifier pattern set,
schedule values, and routing rules are all directly traceable to the
issue body or to in-repo precedents (Phase 8d.18 BLOCKED exemption,
foreman#357 spec for the `StateContext` extension pattern). One
consideration left for the implementing PR's reviewer: whether the
string-pattern set in the transient classifier needs tightening
once we have empirical examples from a real outage. The classifier
is designed to be easy to extend — one new substring per row —
and the test file is parametrized so adding cases stays cheap.

## Out of scope
- Multi-provider fallback (drop down to a different LLM on
  Anthropic outage). Explicitly out of scope per the issue body —
  separate epic.
- Capacity/quota planning, API-key rotation, or budget alerting.
  Different concern.
- Long-term outage handling beyond the 4-attempt / ~40-minute
  schedule. Past that point, NeedsHelp is the correct answer —
  the human operator needs to know Anthropic has been out for
  >40 minutes and decide whether to wait, switch to manual mode,
  or open a status-page ticket.
- Refactoring `Outcome` into a discriminated-union shape. The
  freeform `details` bag is sufficient to carry the new
  `provider_status` field; the Outcome v1 tightening (per Phase
  8d.17 comment in `outcome.py`) remains a stretch goal under
  foreman#315.
- Dashboard surfacing of `transient_provider_error` events. Lives
  with the foreman#352 dashboard epic; this ticket lays the
  observability groundwork (the event + structured-log line) but
  does NOT add UI.
- Auto-retry inside the provider adapter itself (a retry loop
  inside `AnthropicSDKProvider.run_agent`). The state-machine
  layer is the right place: it releases the worker slot during
  the backoff, persists across restarts, and unifies the retry
  policy across every role.
