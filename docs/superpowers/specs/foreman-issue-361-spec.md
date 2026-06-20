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
  `docs/superpowers/specs/2026-06-13-foreman-v4-substrate-redesign-design.md`
  (section `Per-role kind matrix`, currently at line 215 of that file —
  the matrix does NOT live in `foreman-v1-architectural-spec.md`; that
  earlier path was wrong) is updated by appending
  `· TRANSIENT_PROVIDER_ERROR` to EACH of the six rows (Planner,
  Reviewer-on-spec, Fixer-on-spec, Worker, Reviewer-on-impl,
  Fixer-on-impl) since every role-dispatch state may emit it. The
  `outcome.py` docstring already cites that design doc as
  `"Outcome JSON — role-side reporting contract"`, so the matrix path
  is also the natural one to reference from the new docstring line.
- [ ] `ProviderTransientError(ProviderError)` is added in
  `packages/foreman/src/foreman/providers/exceptions.py` with a docstring
  that says: "Anthropic-side transient transport failure — 5xx, 429,
  connection refused, transport-level timeout. Re-raised at the provider
  boundary; roles must surface it as
  `Outcome(kind=TRANSIENT_PROVIDER_ERROR)`, not `ERROR`." Re-exported from
  `packages/foreman/src/foreman/providers/__init__.py` (`__all__`) so role
  runners import it via `from foreman.providers import
  ProviderTransientError`.
- [ ] **Transient classifier helper (single source of truth).** Add a
  module-level helper
  `_is_transient_sdk_error(exc: BaseException) -> bool` in
  `packages/foreman/src/foreman/providers/anthropic_sdk.py` placed
  adjacent to (and BEFORE) `_translate_sdk_exception` near line 145
  so both `_translate_sdk_exception` AND the auth-retry guards in
  `run_agent` can call it. The substrings list lives in one
  module-level `_TRANSIENT_PATTERNS: tuple[str, ...]` tuple so
  future operators tuning the classifier have exactly one place
  to edit (Approach: "make the right thing easy"). The helper
  returns True iff EITHER:
  * `isinstance(exc, Exception)` AND `str(exc)` starts with
    `_SDK_AUTH_ERROR_PREFIX` (defined at `anthropic_sdk.py:63`
    as `"Claude Code returned an error result"`) AND
    `str(exc).lower()` contains any of `_TRANSIENT_PATTERNS`,
    whose contents are: `"429"`, `"rate_limit"`, `"rate limit"`,
    `"overloaded"`, `"503"`, `"502"`, `"504"`, `"500"`,
    `"connection refused"`, `"connection reset"`,
    `"connection aborted"`, `"timeout"` (the prefix check stays
    case-sensitive; only the substring search lowercases the
    haystack). Note: the constant name `_SDK_AUTH_ERROR_PREFIX`
    is now misleading because it covers both auth and transport
    failures, but renaming it is explicitly out of scope here —
    the existing auth-retry guards at lines 167 / 272 / 299
    reference it by name and a rename would balloon the diff
    without semantic gain. Leave the name alone and document the
    dual role in `_is_transient_sdk_error`'s docstring.
  * OR best-effort `isinstance` check against
    `anthropic.APIConnectionError`, `anthropic.RateLimitError`,
    `anthropic.APITimeoutError`, or
    `anthropic.InternalServerError`. Import is guarded by
    `try: import anthropic` at module load; if `anthropic` isn't
    importable (test stub environments) the isinstance branch is
    a no-op and only the string-pattern path is active.
- [ ] `_translate_sdk_exception` in
  `packages/foreman/src/foreman/providers/anthropic_sdk.py` classifies
  exceptions matching `_is_transient_sdk_error(exc)` as
  `ProviderTransientError(str(exc))` as a NEW branch placed
  AFTER the existing `asyncio.TimeoutError` branch and BEFORE the
  existing auth-prefix branch. `asyncio.TimeoutError` keeps mapping
  to `ProviderTimeoutError` (existing behavior). The final branch
  order in `_translate_sdk_exception` becomes (documented verbatim
  in the function docstring):
  1. `asyncio.TimeoutError` → `ProviderTimeoutError` (existing).
  2. `_is_transient_sdk_error(exc)` → `ProviderTransientError`
     (NEW — covers the case where a transient slips past the
     `run_agent`-level pre-check, e.g. an `_iterate_query`-internal
     raise that re-enters translation without going through the
     auth-retry block).
  3. `str(exc).startswith(_SDK_AUTH_ERROR_PREFIX)` →
     `ProviderAuthError` (existing).
  4. Default → `ProviderUnknownError` (existing).
- [ ] **CRITICAL — auth-retry control flow short-circuits transients
  upstream (foreman#361 Reviewer-identified gap).**
  `AnthropicSDKProvider.run_agent` in `anthropic_sdk.py` has two
  prefix-match decision points (lines 272 and 299) that currently
  send every exception starting with `_SDK_AUTH_ERROR_PREFIX` down
  the credential-refresh / auth-retry path. Without the edits
  below, a Claude-Code-wrapped 5xx/429
  (`"Claude Code returned an error result: 503 ..."`) matches the
  prefix and never reaches `_translate_sdk_exception`, so the
  role's `except ProviderTransientError` arm never fires for the
  common scenario described in the issue. Concrete edits:
  1. **At `anthropic_sdk.py:272`** (the outer `except Exception
     as e:` arm in `run_agent`, right after the recovery-chain
     short-circuit at line 269 AND BEFORE the existing
     `if not str(e).startswith(_SDK_AUTH_ERROR_PREFIX):` branch
     at 272), insert:
     ```python
     # foreman#361: classify transient transport failures BEFORE
     # the auth-retry decision. A Claude-Code-wrapped 5xx/429 also
     # starts with _SDK_AUTH_ERROR_PREFIX, so without this guard
     # the auth-retry block at 274 would swallow it, refresh
     # credentials, re-run the query (burning wall-clock + budget
     # while Anthropic is already failing), and ultimately
     # mis-classify as ProviderAuthError.
     if _is_transient_sdk_error(e):
         raise ProviderTransientError(str(e)) from e
     ```
     so prefix-matching transients skip the credential-refresh
     dance entirely.
  2. **At `anthropic_sdk.py:299`** (the inner `except Exception
     as retry_exc:` arm after the auth-retry, right after the
     recovery-chain short-circuit at 296 AND BEFORE the existing
     `if str(retry_exc).startswith(_SDK_AUTH_ERROR_PREFIX):`
     branch at 299), insert the same shape:
     ```python
     # foreman#361: see line 272 comment. The post-retry path
     # also needs to classify transients BEFORE raising the
     # "auth failed twice" ProviderAuthError.
     if _is_transient_sdk_error(retry_exc):
         raise ProviderTransientError(str(retry_exc)) from retry_exc
     ```
     so a post-retry transient still surfaces as
     `ProviderTransientError`, not as the misleading "auth failed
     twice" `ProviderAuthError`.
  3. The existing comment block at `anthropic_sdk.py:231–252`
     (the one that explains the recovery-chain → auth-retry →
     translator ordering) is rewritten to insert the transient
     pre-check between "recovery chain" and "auth-retry". The
     new documented order is **recovery chain → transient
     pre-check (new) → auth-retry → translator**. The comment
     additionally explains *why* the transient pre-check sits
     BEFORE the auth-retry rather than after: a Claude-Code-wrapped
     503 looks identical to an auth error at the prefix level, and
     the auth-retry would (a) needlessly refresh credentials,
     (b) re-run the entire query — burning wall-clock + Anthropic
     budget while the API is already transient-failing, (c)
     ultimately raise `ProviderAuthError` and mis-route the failure
     to NeedsHelp instead of the backoff scheduler. Pre-checking
     transients keeps the auth-retry path meaning exactly what it
     says: "this exception is plausibly an auth issue, retry once
     after refreshing creds".
- [ ] **Regression tests at the `run_agent` integration boundary**
  (NOT just `_translate_sdk_exception` in isolation). Append two
  tests to the new file
  `packages/foreman/tests/providers/test_translate_sdk_transient.py`
  (the same NEW file the unit tests are specced into):
  * `test_run_agent_classifies_wrapped_transient_as_transient`:
    drive a fake SDK whose `query()` raises
    `Exception("Claude Code returned an error result: 503 Service
    Unavailable")` from the first `_iterate_query` call. Assert
    `AnthropicSDKProvider().run_agent(...)` raises
    `ProviderTransientError` (NOT `ProviderAuthError`, NOT
    `ProviderUnknownError`) AND that no credential-refresh
    attempt was made (patch `_maybe_refresh_container_creds` to a
    `MagicMock`; assert `mock.assert_not_called()`).
  * `test_run_agent_classifies_wrapped_429_post_retry_as_transient`:
    drive a fake SDK whose first call raises a non-transient
    prefix-matching exception (forcing the auth-retry path) and
    whose retry call raises a prefix-matching 429
    (`"Claude Code returned an error result: 429 ..."`). Assert
    `run_agent` raises `ProviderTransientError` rather than the
    "auth failed twice" `ProviderAuthError`.
  Both tests exercise the auth-retry control-flow short-circuit at
  lines 272 and 299, not just `_translate_sdk_exception` in
  isolation, and would FAIL against the unfixed control flow.
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
- [ ] **Worker inner-arm split (foreman#361 critical fix —
  Reviewer-identified gap).** The Worker's `_run_worker_core` has an
  inner `except ProviderError as exc:` arm at
  `packages/foreman/src/foreman/roles/worker.py:1051` that swallows
  `ProviderError`, synthesizes an `incomplete`-shaped `WorkerOutput`,
  and falls through to post-check verification — i.e. it does NOT
  re-raise. So without an explicit split, the `except
  ProviderTransientError` arm added to `run_worker_cli` literally
  cannot fire for the Worker. The fix: edit the inner arm at
  `worker.py:1051` so the FIRST line of its body is
  `if isinstance(exc, ProviderTransientError): raise` — re-raising
  the transient subclass past the swallow so the outer
  `except ProviderError` arm at `worker.py:1302` (and from there
  `run_worker_cli`'s `except ProviderTransientError` arm) sees it.
  Non-transient `ProviderError` keeps the existing
  `WorkerOutput(outcome='incomplete', ...)` path. Add an inline
  comment naming this finding so the intent is grep-able. The
  outer `except ProviderError` arm at `worker.py:1302` is verified
  to re-raise (no need to re-edit it), but the spec's role-CLI
  acceptance criterion above already covers `run_worker_cli`
  catching `ProviderTransientError` ahead of `except Exception`.
- [ ] **Suppress redundant issue comments on transient retries
  (foreman#361 important fix — Reviewer-identified gap).** The
  Planner / Reviewer / Fixer inner `except ProviderError as exc:`
  arms at `planner.py:435`, `reviewer.py:636`, `fixer.py:748` call
  `_on_failure(exc)` before re-raising; `_on_failure` invokes
  `handle_unhandled_role_exception` (defined in
  `packages/foreman/src/foreman/roles/__init__.py:120`) which posts
  a traceback comment on the originating GitHub issue
  (foreman#229 runaway-burn defense). With the 4-attempt /
  ~40-minute backoff schedule added by this spec, a single
  Anthropic outage would post up to 4 redundant tracebacks per
  ticket per outage. Decision (option (a) from the Reviewer's
  three choices): the inner handler short-circuits on
  `ProviderTransientError` BEFORE calling `_on_failure`. Concrete
  edit: change each inner arm body to
  ```python
  except ProviderError as exc:
      if isinstance(exc, ProviderTransientError):
          # foreman#361: transient failures are retried by the
          # state machine with backoff; suppress the
          # runaway-burn issue comment so a 40-min outage does
          # not carpet the issue with redundant tracebacks.
          raise
      _on_failure(exc)
      raise
  ```
  Applies identically at `planner.py:435`, `reviewer.py:636`,
  `fixer.py:748`. The Worker's inner arm at `worker.py:1051` does
  NOT call `_on_failure` (it synthesizes a `WorkerOutput` and falls
  through), but with the inner-arm split above the
  `ProviderTransientError` subclass re-raises past 1051 and is
  caught by the OUTER `except ProviderError` at
  `worker.py:1302`, which DOES call `_on_failure` (`worker.py:883`
  → `handle_unhandled_role_exception` → issue comment). So the
  outer arm at `worker.py:1302` needs the SAME short-circuit:
  ```python
  except ProviderError as exc:
      if isinstance(exc, ProviderTransientError):
          # foreman#361: see inner-arm comment above.
          raise
      _on_failure(exc)
      raise
  ```
  `handle_unhandled_role_exception` itself is NOT modified —
  keeping the existing v3-era behavior intact for genuine role
  crashes, where the comment is still the right surface.
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
  additive migration AFTER the existing `executescript` + WAL pragma
  block (lines 92–104), and BEFORE assigning `self._conn`. The exact
  shape:
  ```python
  cols = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
  if "next_action_at" not in cols:
      conn.execute("ALTER TABLE tickets ADD COLUMN next_action_at TEXT")
      conn.commit()
  ```
  Rationale: SQLite's `CREATE TABLE IF NOT EXISTS` (used by
  `executescript`) is a no-op when the table already exists, so a
  pre-existing on-disk DB created against the old schema would NEVER
  get the new column from the inline declaration alone — the
  `ALTER TABLE` is load-bearing for forward compatibility. There is
  NO existing additive-migration precedent in the repo (the
  `depends_on` column was added by inline schema declaration BEFORE
  any production DB existed at `schema.sql:20` with `NOT NULL DEFAULT
  '[]'`, no `ALTER TABLE` shim was ever written). This ticket
  introduces the pattern; future column adds should mirror this
  shape.
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
- [ ] **CRITICAL — widen `TicketState.next_state` to accept `ctx`
  (Option A from the Reviewer's two viable shapes).** The transient-
  retry plumbing needs `ctx.repo` / `ctx.bus` / `ctx.clock` in scope
  at routing time, but the existing abstract signature at
  `packages/foreman/src/foreman/v4/state.py:190-192` is
  `next_state(self, outcome: Outcome) -> TicketState | None` — no
  `ctx` parameter — and `state.py:313` invokes it as
  `self.next_state(outcome)`. Concrete edits:
  1. **`packages/foreman/src/foreman/v4/state.py`** — change the
     abstract signature to
     `next_state(self, ctx: StateContext, outcome: Outcome) -> TicketState | None`
     and update the call site at `state.py:313` from
     `next_ = self.next_state(outcome)` to
     `next_ = self.next_state(ctx, outcome)`. The docstring on the
     abstract method gains one sentence: "The `ctx` parameter
     mirrors the other lifecycle hooks (`enter` / `execute` /
     `verify` / `exit`) and is load-bearing for the
     `RoleDispatchState` transient-retry intercept (foreman#361)."
  2. **`packages/foreman/src/foreman/v4/states/role_dispatch.py`** —
     update the delegating override at `role_dispatch.py:37` to the
     new signature
     `next_state(self, ctx: StateContext, outcome: Outcome) -> TicketState | None`.
     The body becomes the Template Method described below.
     `next_state_for(self, outcome)` on subclasses stays unchanged —
     the new signature only widens the OUTER routing method, not the
     per-state inner branching, so the six concrete
     role-dispatch subclasses (`planning.py:14`, `spec_review.py`,
     `spec_fix.py:13`, `implementing.py:20`, `impl_review.py`,
     `impl_fix.py:13`) need NO source edit at all — they keep
     overriding the unchanged `next_state_for`.
  3. **`packages/foreman/src/foreman/v4/states/queued.py`** —
     update the concrete `next_state` override at `queued.py:19` to
     the new `(self, ctx, outcome)` signature. Body unchanged.
  4. **`packages/foreman/src/foreman/v4/states/merging.py`** —
     update the concrete `next_state` override at `merging.py:169`
     to the new `(self, ctx, outcome)` signature. Body unchanged.
  5. **`packages/foreman/src/foreman/v4/states/terminal.py`** —
     update the concrete `next_state` override on `_TerminalState`
     at `terminal.py:26` to the new `(self, ctx, outcome)`
     signature. Body unchanged (still `return None`). The four
     concrete terminal subclasses (`DoneState`, `FailedState`,
     `NeedsHelpState`, `CompletedState`) inherit from
     `_TerminalState` so they pick up the new signature for free.
  6. **Tests** — every test fixture that subclasses `TicketState`
     and defines its own `next_state` must have the signature
     widened to `(self, ctx, outcome)`. The bodies do not change.
     Concretely (one-line signature edit each):
     `packages/foreman/tests/v4/states/test_role_dispatch.py`
     (the `_Demo` fixture; it overrides `next_state_for` so this
     file only ripples if it ALSO defines `next_state` — verify
     during the edit and skip if not),
     `packages/foreman/tests/v4/test_transition.py` (4 fixtures
     at lines 44, 57, 92, 452),
     `packages/foreman/tests/v4/test_transition_events.py`
     (6 fixtures at lines 31, 44, 116, 207, 332, 428 — note that
     two are typed as `def next_state(self, outcome):` without
     annotations; they still need the `ctx` parameter inserted),
     `packages/foreman/tests/v4/test_state_abc.py` (1 fixture at
     line 23),
     `packages/foreman/tests/v4/test_fanout_integration.py`
     (2 fixtures at lines 27, 40).
  Why Option A over Option B (override `transition()` on
  `RoleDispatchState`): symmetry with the existing lifecycle hooks
  (`enter` / `execute` / `verify` / `exit` all take `ctx` first),
  no duplication of the ~100-line `transition()` orchestration in
  the role-dispatch base class, and the per-file diff is purely
  mechanical — every non-base call site just gains one parameter.
- [ ] `RoleDispatchState.next_state` in
  `packages/foreman/src/foreman/v4/states/role_dispatch.py` becomes a
  Template Method that intercepts `TRANSIENT_PROVIDER_ERROR` before
  delegating to `next_state_for`. With the widened signature from the
  previous bullet, `ctx` is in scope. When the outcome kind is
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
  Non-transient outcomes fall through to `return self.next_state_for(outcome)`
  unchanged — `next_state_for`'s signature stays narrow so concrete
  subclasses are not touched.
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
  ADDITIONALLY (Reviewer-identified gap): the `__call__` body does NOT
  walk `dataclass.fields(event)` — it hard-codes per-event-type field
  emission via explicit `isinstance` branches at lines 47–54 (the
  existing `ExecuteCompletedEvent` and `StateFailedEvent` arms). So
  add a NEW `isinstance(event, TransientProviderErrorEvent)` branch
  after the `StateFailedEvent` branch that writes:
  ```python
  payload["attempt"] = event.attempt
  payload["next_retry_at"] = (
      event.next_retry_at.isoformat() if event.next_retry_at else None
  )
  payload["provider_status"] = event.provider_status
  ```
  Without this branch the three new fields would be silently dropped
  from the JSONL output and the runbook-promised
  `transient_provider_error` log lines would be missing their
  attempt / backoff / cause-string detail. Also add
  `TransientProviderErrorEvent` to the top-of-file `from
  foreman.v4.events import (...)` block alongside the other
  imported event types.
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
- [ ] `cmd_show` in `packages/foreman/src/foreman/v4/cli/show.py`
  (function defined at `show.py:20`) renders `next_action_at` when
  non-null as a tree child off the ticket header, e.g. `[yellow]
  suspended until 2026-06-20T14:25:00Z (provider-throttled,
  attempt 2/4)[/yellow]`. The "attempt N/4" hint comes from a
  fresh call to
  `count_consecutive_transient_provider_errors(ticket_id)`. The
  `cmd_show` body builds a `rich.Tree` (it does not go through
  the Formatter Strategy — see file docstring); add the suspension
  line as another `tree.add(...)` before the per-instance loop.
- [ ] `cmd_ps` in `packages/foreman/src/foreman/v4/cli/ps.py`
  (function defined at `ps.py:16`) gains a `next_action_at` column
  in its row dict (formatted as ISO 8601 or empty string when
  None) so the suspension surfaces alongside `state` / `updated`
  in the Formatter Strategy output. Update the table/json/yaml
  formatter callers transparently — the dict shape is the
  contract.
- [ ] New test file
  `packages/foreman/tests/providers/test_translate_sdk_transient.py`:
  one parametrized test that drives `_translate_sdk_exception` with
  representative strings from each pattern (`"... 429 Too Many ..."`,
  `"... 503 ..."`, `"connection refused"`, etc.) and asserts the
  return type is `ProviderTransientError`. A second parametrized test
  drives benign-looking strings (`"Claude Code returned an error
  result: success"`, plain `"some random failure"`) and asserts the
  return type is NOT `ProviderTransientError`.
- [ ] **New test file (Planner)**
  `packages/foreman/tests/v4/roles/test_planner_transient_outcome.py`
  (note: alongside the existing
  `tests/v4/roles/test_planner_outcome.py` — the
  `packages/foreman/tests/v4/roles/` directory is the established
  location for role-CLI tests; `packages/foreman/tests/roles/`
  does NOT exist and must not be created). Patches
  `_run_planner_for_v4` to raise `ProviderTransientError`, calls
  `run_planner_cli`, asserts the emitted FOREMAN_OUTCOME JSON
  parses to `kind="transient_provider_error"` with `details`
  populated AND the exit code is `0`.
- [ ] **Additions to existing role-CLI test modules** for
  Reviewer and Fixer transient cases — append a
  `test_<role>_transient_outcome` function (same shape as the
  Planner test) into the existing
  `packages/foreman/tests/v4/roles/test_reviewer_outcome.py` and
  `packages/foreman/tests/v4/roles/test_fixer_outcome.py`. These
  files already exercise the OutcomeKind round-trip for each
  role's normal outcome paths; the new transient cases sit
  naturally alongside them.
- [ ] **New test file (Worker) that EXERCISES the inner-arm split**:
  add `test_worker_transient_outcome` in a new file
  `packages/foreman/tests/v4/roles/test_worker_transient_outcome.py`
  (alongside the existing `test_worker_outcome.py` and
  `test_worker_core.py`). Patch `provider.run_agent` to raise
  `ProviderTransientError` from inside `_run_worker_core`; assert
  (a) the inner arm at `worker.py:1051` re-raises (the synthesized
  `WorkerOutput(outcome='incomplete')` path is NOT taken),
  (b) the outer `run_worker_cli` `except ProviderTransientError`
  arm fires, (c) the FOREMAN_OUTCOME JSON parses to
  `kind="transient_provider_error"` with `details` populated AND
  the exit code is `0`. A second test
  `test_worker_non_transient_provider_error_still_swallowed`
  in the same file patches `provider.run_agent` to raise a
  non-transient `ProviderError`; asserts the existing
  `incomplete`-shaped `WorkerOutput` path is preserved
  (regression guard for the documented foreman#266 inner-arm
  behavior).
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
- [ ] New test `test_retry_clears_next_action_at` appended to
  `packages/foreman/tests/v4/cli/test_mutation_commands.py` (note:
  the file is named `test_mutation_commands.py`, NOT
  `test_mutations.py` — the latter does not exist in the
  `tests/v4/cli/` directory): invoke `cmd_retry` against a ticket
  with non-null `next_action_at`; assert `next_action_at` is None
  afterwards and the WorkItem was enqueued.
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
load-bearing. The schema migration is additive (nullable column
declared in `schema.sql` and gated by a `PRAGMA table_info`-driven
`ALTER TABLE ADD COLUMN` in `SqliteTicketRepository.__init__` —
needed because `CREATE TABLE IF NOT EXISTS` is a no-op against a
pre-existing on-disk DB and would NOT pick up an inline column
addition alone). No prior additive-migration precedent exists in
the repo; this ticket introduces the pattern and future column adds
should mirror its shape.

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

**Granular pattern detection via `_is_transient_sdk_error`.** The
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
installed don't break import. The classifier is extracted into a
module-level `_is_transient_sdk_error(exc)` helper so it has ONE
implementation and `_translate_sdk_exception` + the auth-retry
guards in `run_agent` both call the same function — no drift risk
between the two sites.

**Why the auth-retry block calls the classifier directly (control-
flow short-circuit).** A subtle but load-bearing point the Reviewer
caught: `run_agent` has TWO prefix-match decision points (lines 272
and 299) that intercept exceptions whose `str()` starts with
`_SDK_AUTH_ERROR_PREFIX` *before* `_translate_sdk_exception` is
ever called. Without an upstream short-circuit, a Claude-Code-
wrapped 5xx/429 — which DOES start with that prefix — would be
routed into the credential-refresh / auth-retry dance instead of
classified as transient. The auth-retry would (a) needlessly burn
a credential refresh, (b) re-run the query while Anthropic is
already failing, (c) ultimately raise `ProviderAuthError` and
mis-route the failure to NeedsHelp instead of the backoff
scheduler. The fix is one `_is_transient_sdk_error(e)` check at
each of the two decision points, raising `ProviderTransientError`
directly when matched. The classifier still lives inside
`_translate_sdk_exception` as a defense-in-depth (covers
`_iterate_query`-internal raises that bypass the outer
`except Exception`), but the operational path for the common case
in the issue runs through the `run_agent`-level short-circuits.
Documented control-flow order in the rewritten
`anthropic_sdk.py:231–252` comment block: **recovery chain →
transient pre-check (new) → auth-retry → translator**.

## Sub-requests (topologically sorted)
1. Add `OutcomeKind.TRANSIENT_PROVIDER_ERROR` in
   `packages/foreman/src/foreman/v4/outcome.py`. Update the per-role
   matrix in the architectural spec.
2. Add `ProviderTransientError` in
   `packages/foreman/src/foreman/providers/exceptions.py`; re-export
   from `providers/__init__.py.__all__`.
3. In `packages/foreman/src/foreman/providers/anthropic_sdk.py`:
   (a) add the `_is_transient_sdk_error(exc)` helper +
   `_TRANSIENT_PATTERNS` tuple; (b) extend `_translate_sdk_exception`
   to call the helper as a NEW branch between the timeout branch and
   the auth-prefix branch; (c) **CRITICAL — short-circuit transients
   upstream of the auth-retry** at lines 272 and 299 of `run_agent`
   so prefix-matching transient errors never enter the credential-
   refresh path (without this the transient mechanism is dead code
   for Claude-Code-wrapped 5xx/429, per Reviewer-identified gap);
   (d) update the `anthropic_sdk.py:231–252` comment block to
   document the new control-flow order **recovery chain → transient
   pre-check → auth-retry → translator**.
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
12. Widen `TicketState.next_state` to take `ctx` as first parameter
    (Option A — symmetric with `enter`/`execute`/`verify`/`exit`).
    This ripples through `state.py` (abstract def + call site at line
    313), `states/role_dispatch.py` (delegating override),
    `states/queued.py` / `states/merging.py` /
    `states/terminal.py` (signature widening, body unchanged), and
    the five test files that define `next_state` fixtures. Then
    modify `RoleDispatchState.next_state` to intercept
    `TRANSIENT_PROVIDER_ERROR` (schedule next attempt OR escalate to
    NeedsHelp) AND clear `next_action_at` on every other outcome
    kind, using the now-in-scope `ctx`. Concrete-state subclasses'
    `next_state_for` is NOT touched.
13. Add the transient catch arm to each of the four role CLIs:
    `planner.py:run_planner_cli`, `reviewer.py:run_reviewer_cli`,
    `worker.py:run_worker_cli`, `fixer.py:run_fixer_cli`.
    Additionally split the Worker's inner `except ProviderError`
    arm at `worker.py:1051` so `ProviderTransientError` re-raises
    past the swallow (see the Worker inner-arm acceptance
    criterion above); also short-circuit Planner / Reviewer /
    Fixer inner arms (`planner.py:435`, `reviewer.py:636`,
    `fixer.py:748`) so `ProviderTransientError` bypasses
    `_on_failure` (see the issue-comment-suppression criterion
    below).
14. Extend `cmd_retry` (`v4/cli/mutations.py`) to call
    `clear_next_action_at` first; extend `cmd_show`
    (`v4/cli/show.py`) and `cmd_ps` (`v4/cli/ps.py`) to display
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
  add `_is_transient_sdk_error` helper +
  `_TRANSIENT_PATTERNS` tuple; extend `_translate_sdk_exception`
  with a new transient branch (between timeout and auth-prefix);
  **short-circuit transients in `run_agent`'s auth-retry block
  at lines 272 and 299** (CRITICAL — without this the
  prefix-matching path swallows wrapped 5xx/429); rewrite the
  comment block at lines 231–252 to document the new
  control-flow order **recovery chain → transient pre-check →
  auth-retry → translator**.
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
- `packages/foreman/src/foreman/v4/state.py` — widen the abstract
  `next_state` signature to `(self, ctx: StateContext, outcome: Outcome)`
  and update the call site at line 313 (`self.next_state(ctx, outcome)`).
  See the dedicated CRITICAL acceptance bullet above for the rationale
  (Option A — symmetry with the other ctx-first lifecycle hooks).
- `packages/foreman/src/foreman/v4/states/role_dispatch.py` —
  Template Method extension on `next_state` (now `(self, ctx, outcome)`);
  delegates non-transient outcomes to the unchanged `next_state_for`.
- `packages/foreman/src/foreman/v4/states/queued.py` — widen
  `next_state` override at line 19 to `(self, ctx, outcome)`. Body
  unchanged.
- `packages/foreman/src/foreman/v4/states/merging.py` — widen
  `next_state` override at line 169 to `(self, ctx, outcome)`. Body
  unchanged.
- `packages/foreman/src/foreman/v4/states/terminal.py` — widen
  `_TerminalState.next_state` at line 26 to `(self, ctx, outcome)`.
  All four concrete terminal subclasses inherit the new signature.
- `packages/foreman/tests/v4/states/test_role_dispatch.py`,
  `packages/foreman/tests/v4/test_transition.py`,
  `packages/foreman/tests/v4/test_transition_events.py`,
  `packages/foreman/tests/v4/test_state_abc.py`,
  `packages/foreman/tests/v4/test_fanout_integration.py` — widen
  the fixture `next_state` signatures to match the abstract
  method's new shape; bodies do not change. See the CRITICAL
  acceptance bullet for the per-file line numbers.
- `packages/foreman/src/foreman/roles/planner.py`,
  `reviewer.py`, `worker.py`, `fixer.py` — transient catch arm
  in each role's `run_*_cli`.
- `packages/foreman/src/foreman/v4/cli/mutations.py` —
  extend `cmd_retry`.
- `packages/foreman/src/foreman/v4/cli/show.py` — display
  `next_action_at` in `cmd_show` (rendered as a tree child).
- `packages/foreman/src/foreman/v4/cli/ps.py` — add a
  `next_action_at` column to the `cmd_ps` row dict so the
  suspension surfaces in table / json / yaml output.
- `packages/foreman/tests/providers/test_translate_sdk_transient.py`
  — NEW: `_translate_sdk_exception` unit tests AND
  `run_agent`-level integration tests for the auth-retry
  control-flow short-circuit (see the regression-test
  acceptance bullet above).
- `packages/foreman/tests/v4/roles/test_planner_transient_outcome.py`
  — NEW role-CLI outcome test for the Planner. (Note: the actual
  role-CLI test directory is `packages/foreman/tests/v4/roles/`,
  NOT `packages/foreman/tests/roles/` — the latter does not exist.)
- `packages/foreman/tests/v4/roles/test_reviewer_outcome.py` —
  EXTEND with `test_reviewer_transient_outcome` (existing file;
  do NOT create a new module).
- `packages/foreman/tests/v4/roles/test_fixer_outcome.py` — EXTEND
  with `test_fixer_transient_outcome` (existing file; do NOT
  create a new module).
- `packages/foreman/tests/v4/roles/test_worker_transient_outcome.py`
  — NEW: hosts both `test_worker_transient_outcome` (exercises
  the inner-arm split at `worker.py:1051`) and
  `test_worker_non_transient_provider_error_still_swallowed`
  (regression guard for the existing foreman#266 inner-arm
  behavior). Sits alongside the existing
  `test_worker_outcome.py` and `test_worker_core.py`.
- `packages/foreman/tests/v4/states/test_role_dispatch_transient.py`
  — NEW state-machine routing + backoff + escalation tests.
- `packages/foreman/tests/v4/test_poller.py` — append
  `test_poller_skips_suspended_ticket`.
- `packages/foreman/tests/v4/cli/test_mutation_commands.py` —
  append `test_retry_clears_next_action_at`. (Note: file is
  `test_mutation_commands.py`, NOT `test_mutations.py`.)
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
