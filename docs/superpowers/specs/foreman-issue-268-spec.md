# Spec: move reviewer budget cap to AFTER review — outcome-aware `needs_fix` counter (issue #268)

## Goal

The spec-side and impl-side Reviewer budget caps fire BEFORE the next
review runs (a count-based dispatch gate inside `_planning_pr_needs_review`
and `_impl_review_green` — note: the impl-side function in
`rules.py:437` is named `_impl_review_green`, NOT `_impl_pr_needs_review`
as the originating issue body uses), and the post-dispatch `attempts_exhausted`
safety rules conflate "Reviewer ran N times" with "Reviewer said
needs_fix N times". This spec moves the cap to AFTER each review's
outcome is known and counts only `needs_fix` verdicts toward the budget.
The hot-spawn-loop defense (the per-role rate-limit at
`_RATE_LIMIT_TARGETS`) stays untouched as the right gate for the
"Reviewer crashes in a tight window" failure mode it was designed for.
See issue [#268](https://github.com/jeffrichley/foreman/issues/268).

## Acceptance criteria

- `packages/foreman/src/foreman/reconciler/rules.py` line 365: the
  predicate clause
  `and ctx.log.count_completed("dispatch_reviewer_spec", ctx.ticket_id) < _MAX_REVIEWER_ATTEMPTS`
  is REMOVED from `_planning_pr_needs_review`. After the change, the
  spec-side Reviewer dispatch gate consists of: planning-label,
  `ctx.pr is not None`, `not ctx.pr.is_merged`, `head_ref` shape filter,
  and the `has_unterminated` check. Inline doc-comment (lines 353-358)
  is updated to explain the new "cap fires AFTER, not BEFORE" semantics
  and links to issue #268.
- `packages/foreman/src/foreman/reconciler/rules.py` line 453: the
  symmetric clause in `_impl_review_green`
  (`and ctx.log.count_completed("dispatch_reviewer_impl", ctx.ticket_id) < _MAX_REVIEWER_ATTEMPTS`)
  is REMOVED. Doc-comment updated symmetrically.
- `packages/foreman/src/foreman/reconciler/rules.py` line 80: the
  constant `_MAX_REVIEWER_ATTEMPTS = 3` is RENAMED to
  `_MAX_REVIEWER_FIX_VERDICTS = 3`. The value stays at 3. The constant
  is private and grep-confirmed to be referenced only inside
  `packages/foreman/src/foreman/reconciler/rules.py` (4 sites — the
  definition + 3 usages); no external module imports it.
- `packages/foreman/src/foreman/reconciler/rules.py:109-123`
  (`_reviewer_spec_attempts_exhausted`): the predicate switches from
  `ctx.log.count_completed("dispatch_reviewer_spec", ctx.ticket_id) >= _MAX_REVIEWER_ATTEMPTS`
  to
  `ctx.log.count_completed("dispatch_reviewer_spec", ctx.ticket_id, outcome="needs_fix") >= _MAX_REVIEWER_FIX_VERDICTS`.
  The doc-comment is rewritten to explain the new semantics (counts the
  Reviewer's needs_fix verdicts, NOT total dispatches) and links #268.
  The label gate (`foreman:planning`) stays.
- `packages/foreman/src/foreman/reconciler/rules.py:126-132`
  (`_reviewer_impl_attempts_exhausted`): symmetric change — predicate
  becomes
  `ctx.log.count_completed("dispatch_reviewer_impl", ctx.ticket_id, outcome="needs_fix") >= _MAX_REVIEWER_FIX_VERDICTS`.
  Doc-comment updated symmetrically. The label gate
  (`foreman:impl-review`) stays.
- `packages/foreman/src/foreman/reconciler/exec_log.py` is NOT modified.
  `ExecutionLog.count_completed` already accepts the optional
  `outcome: str | None` parameter (see lines 342-394 + the test at
  `packages/foreman/tests/reconciler/test_exec_log.py:280-333`); we
  call it with `outcome="needs_fix"`. No new helper is added — the
  call-site stays clean with the existing API.
- Red test 1 (spec-side, outcome-aware exhaustion):
  `test_reviewer_spec_attempts_exhausted_fires_only_on_three_needs_fix_verdicts`
  added to `packages/foreman/tests/reconciler/test_rules.py`. Builds a
  context with 3 completed `dispatch_reviewer_spec` rows whose
  termination outcomes are ALL `"needs_fix"`, then calls the predicate
  directly:
  `assert _reviewer_spec_attempts_exhausted(ctx) is True`. The test
  MUST NOT go through `evaluate(ctx, rules=RULES)` here — the
  `dispatch_reviewer_spec` rate-limit rule (precedence 44) runs BEFORE
  `reviewer_spec_attempts_exhausted` (precedence 65), and
  `_NON_FAILURE_OUTCOMES = ("success", "dry_run", "skipped_capacity")`
  (`exec_log.py:31-35`) classifies `needs_fix` as a failure, so a bare
  test fixture with 3 `needs_fix` rows would trip the rate-limit first
  and return `Action.RATE_LIMIT_TRIP`, not `Action.SURFACE_HELP`. In
  production the daemon's `success` termination row alongside each
  Recorder `needs_fix` row advances `fence_ts` (`v3_host.py:1023-1029`
  + `dispatch_recorder.py:181-200`), so the rate-limit stays silent
  and the higher-precedence exhaustion rule fires correctly; the
  direct-predicate-call pattern in the test isolates the property
  under test (the outcome filter) from that dual-write fixture
  scaffolding. Context labels: `labels=("foreman:planning",)`.
  Acceptable alternative pattern (Worker's choice): construct the
  fixture with BOTH the Recorder-shape `outcome="needs_fix"` row AND
  a daemon-shape `outcome="success"` row per dispatch (matching
  production), then assert
  `evaluate(ctx, rules=RULES) is Action.SURFACE_HELP`.
- Red test 2 (spec-side, clean verdicts don't burn budget):
  `test_reviewer_spec_attempts_exhausted_does_not_fire_when_one_clean_verdict_present`
  added to `packages/foreman/tests/reconciler/test_rules.py`. Builds
  a context with 3 completed `dispatch_reviewer_spec` rows whose
  termination outcomes are `["needs_fix", "needs_fix", "clean"]` —
  asserts `evaluate(ctx, rules=RULES)` is NOT
  `Action.SURFACE_HELP`. The `clean` verdict means the spec PR has
  moved past planning, so the test's label state must be consistent
  with that flow (`foreman:plan-approved`) or the test must directly
  call `_reviewer_spec_attempts_exhausted(ctx)` and assert False — the
  Worker may choose either pattern, but the asserted property is "fewer
  than 3 needs_fix verdicts → no exhaustion".
- Red test 3 (dispatch gate is decoupled from total dispatch count):
  `test_dispatch_reviewer_spec_fires_after_many_completed_dispatches_with_no_needs_fix_streak`
  added to `packages/foreman/tests/reconciler/test_rules.py`. Builds a
  context with 5 completed `dispatch_reviewer_spec` rows with mixed
  outcomes whose `needs_fix` count is STRICTLY LESS THAN 3 (e.g.,
  `["needs_fix", "success", "needs_fix", "success", "success"]` — 2
  needs_fix, 3 success), no unterminated dispatch, label state
  `foreman:planning`, PR is spec-shaped + unmerged. Asserts
  `evaluate(ctx, rules=RULES) is Action.DISPATCH_REVIEWER_SPEC`. The
  needs_fix count must be < 3 so the post-fix exhaustion rule does
  NOT fire (`count_completed(outcome="needs_fix") == 2 < 3`); the
  trailing `success` rows advance the rate-limit `fence_ts` past any
  needs_fix rows so the rate-limit stays silent. This fails today
  because the existing line-365 count gate blocks past 3 total
  dispatches; after the change it passes. Symmetric impl-side Test 6
  uses the same shape with `dispatch_reviewer_impl` /
  `foreman:impl-review` / `head_ref="foreman/impl-143"`.
- Symmetric impl-side red tests:
  `test_reviewer_impl_attempts_exhausted_fires_only_on_three_needs_fix_verdicts`,
  `test_reviewer_impl_attempts_exhausted_does_not_fire_when_one_clean_verdict_present`,
  `test_dispatch_reviewer_impl_fires_after_many_completed_dispatches_with_no_needs_fix_streak`
  — each mirrors its spec-side counterpart but uses
  `foreman:impl-review` label, `dispatch_reviewer_impl` action, and
  impl-shaped PR (`head_ref="foreman/impl-143"`). Test 4
  (`_fires_only_on_three_needs_fix_verdicts`) MUST follow the same
  rate-limit-avoidance pattern as Red test 1: call the predicate
  `_reviewer_impl_attempts_exhausted(ctx)` directly and assert `is
  True`, OR write a daemon-shape `outcome="success"` row alongside
  each Recorder-shape `outcome="needs_fix"` row to advance `fence_ts`
  so `evaluate(ctx, rules=RULES)` can return `Action.SURFACE_HELP`
  without `dispatch_reviewer_impl`'s precedence-45 rate-limit firing
  first. A bare 3-`needs_fix`-row fixture put through `evaluate`
  returns `Action.RATE_LIMIT_TRIP` (same reason as spec-side: the
  rate-limit at precedence 42+3=45 sits below
  `reviewer_impl_attempts_exhausted` at precedence 70, and `needs_fix`
  is not in `_NON_FAILURE_OUTCOMES`).
- Existing rate-limit test
  `test_dispatch_reviewer_spec_blocked_after_3_completed_attempts`
  (`packages/foreman/tests/reconciler/test_rules.py:933`) continues to
  pass UNCHANGED. It uses `outcome="error"` rows which (a) trip the
  rate-limit at precedence 42-47 (lower than the now-narrowed
  `reviewer_spec_attempts_exhausted` at 65) and (b) do NOT match the
  new `outcome="needs_fix"` filter — so RATE_LIMIT_TRIP still wins.
  Same for the impl-side variant at line 989.
- Existing test `test_dispatch_reviewer_spec_still_fires_under_budget`
  (`packages/foreman/tests/reconciler/test_rules.py:966`) is updated.
  Today it asserts that 2 error terminations + dispatch_reviewer_spec
  predicate returns the dispatch action — the property under test
  ("dispatch-side cap allows < N") no longer exists after the change.
  The Worker either (a) deletes the test (the property is gone) and
  relies on red test 3 above as the replacement, OR (b) renames + edits
  it to assert the new property: "completed-dispatch count is NOT a
  dispatch gate". Either approach is acceptable; the worker MUST NOT
  leave the test asserting a property that doesn't exist anymore.
- Existing invariant test
  `test_rules_are_sorted_by_precedence` at
  `packages/foreman/tests/reconciler/test_rules_precedence.py:48`
  continues to pass — no precedence values change; rule ordering is
  unchanged.
- Existing invariant test
  `test_forward_progress_tier_uses_precedence_at_or_above_100` at
  `packages/foreman/tests/reconciler/test_rules_precedence.py:25`
  continues to pass — no rules move tiers.
- Verify, during the implementation, that
  `("dispatch_reviewer_spec", "foreman:planning")` and
  `("dispatch_reviewer_impl", "foreman:impl-review")` are both already
  present in `_RATE_LIMIT_TARGETS` at
  `packages/foreman/src/foreman/reconciler/rules.py:150-157` (the
  Planner has already confirmed they are at lines 153-154; the Worker
  re-verifies during the rate-limit-retention check). The rate-limit
  rule for `dispatch_reviewer_spec` remains the hot-spawn-loop defense
  in lieu of the removed dispatch-side cap.
- `just check` exits 0 on the impl worktree (lint + typecheck + full
  pytest suite green, including the new tests).
- The impl PR body references issue #268 plainly — NO GitHub closing-
  keyword references (`Closes #268` / `Fixes #268` / `Resolves #268`),
  per foreman#63. Reference as "addresses #268" or "for issue #268".

## Approach

The bug is a placement error: a budget cap that conceptually answers
"how many times has the Reviewer told us to try again?" is implemented
as "how many times has the Reviewer process started?" The two diverge
exactly when the Fixer's pass succeeds and the Reviewer would have
returned `clean` on the next run — but the dispatch-side gate
preempts that next run, replaces the verdict with a counter, and
escalates to needs-help. Foreman#258 is the empirical case: two clean
Fixer passes, then needs-help at attempt-3's dispatch gate, with no
verdict from review #3 ever produced.

The fix has three localized edits in `rules.py`, all in a single
module, with no schema change and no new helper functions.

**Edit 1 — drop the dispatch-side count gate.** Lines 365 and 453.
After this edit, `_planning_pr_needs_review` and `_impl_review_green`
gate dispatch on label state + PR state + head-ref shape + no
in-flight Reviewer dispatch. There is no upper bound on how many times
Reviewer can dispatch against a single PR. The bound is the labeled-
state machine + the Fixer loop: each `needs_fix` moves the label to
`spec-fix`/`impl-fix`, where Fixer takes over; each Fixer success
moves it back to `planning`/`impl-review`, where Reviewer re-fires.
The loop terminates when (a) Reviewer says `clean` and the PR advances,
(b) Fixer's budget exhausts (separate cap at
`_fix_attempts_exhausted`/`_spec_fix_attempts_exhausted`), or (c) the
new outcome-aware Reviewer cap below trips.

**Edit 2 — make `_reviewer_*_attempts_exhausted` outcome-aware.**
Lines 109-123 and 126-132. The new predicate counts only termination
rows whose `outcome` is `"needs_fix"`. The Recorder writes that exact
string to the `execution_log` row's `outcome` column via
`CostSubscriber.handle_dispatch_complete` (see
`packages/foreman/src/foreman/dispatch_recorder.py:181-210` and
`packages/foreman/src/foreman/roles/reviewer.py:599-612`, where
`emit_recorder_complete(...outcome=llm_output.outcome...)` propagates
the Reviewer's `"clean"` / `"needs_fix"` verdict literally). So the
`count_completed(action="dispatch_reviewer_spec",
ticket_id, outcome="needs_fix")` query returns exactly the number of
`needs_fix` verdicts. `ExecutionLog.count_completed` already accepts
`outcome=` (see `exec_log.py:342-394` and the test guarding the new
contract at `tests/reconciler/test_exec_log.py:280-333`); we reuse
that API.

**Edit 3 — rename the constant.** Line 80. The new name
`_MAX_REVIEWER_FIX_VERDICTS = 3` accurately describes what is being
counted. The value stays at 3 (matches the existing budget). The
constant is private (single leading underscore, no `__all__` export),
and `grep -rn "_MAX_REVIEWER_ATTEMPTS" packages/foreman/` returns
hits only inside `rules.py` (definition + 3 usages). The rename is
safe.

**Why the rate-limit stays.** Plan B Stage 1+2 introduced the
dispatch-side budget cap as the structural defense against runaway
dispatches when a Reviewer subprocess crashed in a tight loop (LLM
provider down). After foreman#228 added the per-ticket-per-role
windowed-failure rate-limit (`_RATE_LIMIT_TARGETS` includes both
`dispatch_reviewer_spec` and `dispatch_reviewer_impl`), the rate-limit
is the right shape for that defense — it counts failures-in-window,
not total runs. A Reviewer that crashes 3 times in 30 minutes
trips the rate-limit (precedence 42-47, higher than
`reviewer_spec_attempts_exhausted` at 65) and the daemon writes a
reset sentinel. A Reviewer that returns 5 clean verdicts over an hour
does not trip the rate-limit. The two failure modes — "Reviewer keeps
crashing" vs "Reviewer keeps saying needs_fix" — get two different
defenses, which is exactly the shape the issue argues for.

**TDD discipline.** The new tests go red first against the current
predicates. Tests that exercise the outcome filter (Tests 1, 2, 4, 5)
use direct predicate calls — `assert
_reviewer_spec_attempts_exhausted(ctx) is …` / `_reviewer_impl_…` —
to bypass `evaluate`'s precedence walk. Going through `evaluate` on
needs_fix/error-only fixtures returns `Action.RATE_LIMIT_TRIP` first,
because the `dispatch_reviewer_spec` rate-limit at precedence 44
(impl at 45) sits below `reviewer_spec_attempts_exhausted` at 65
(impl at 70), and `_NON_FAILURE_OUTCOMES` (`exec_log.py:31-35`)
classifies any outcome other than `success` / `dry_run` /
`skipped_capacity` as a failure for the rate-limit's
`count_recent_failures`. The existing test at
`test_rules.py:933` already locks `Action.RATE_LIMIT_TRIP` on 3
error rows; tests that go through `evaluate` (Tests 3 and 6) use a
success-trailing outcome sequence so the rate-limit `fence_ts`
advances past any failure rows and rate-limit stays silent. Once the
predicate edits land, direct-predicate tests flip to the post-fix
assertion (`is True` / `is False` per the outcome filter), and the
`evaluate`-path tests advance to `Action.DISPATCH_REVIEWER_SPEC` (no
more line-365 count gate). The existing rate-limit + precedence
invariant tests are run in the same suite to confirm no regression.

**What this change does NOT do.** Out of scope per the issue body
(lifecycle event architecture, Worker/Fixer/Planner budget audits,
new label states, removing the rate-limit). The change is the smallest
correct fix at the wrong-place gate: three predicate edits and a
constant rename, plus six new tests (three spec-side, three impl-side)
and one existing test repurposed.

## Sub-requests (topologically sorted)

1. **Add the six red tests to
   `packages/foreman/tests/reconciler/test_rules.py`.** Three
   spec-side, three impl-side, matching the names in Acceptance
   criteria. Each test uses the existing `_ctx_with` / `_issue` / `_pr`
   helpers (defined at lines 109-150 of the same test file) and
   constructs execution_log state by calling `ctx.log.write_action`
   for the start row and `ctx.log.terminate_action(...,
   outcome="needs_fix"/"clean"/"success", details={})` for the
   termination row. The tests are placed in the same section as the
   existing reviewer attempt-budget tests (after line 989).

2. **Run the new tests; confirm they fail against current code.**
   The reds split into two categories based on which assertion path
   is exercised.

   **Tests 1 and 4 (direct-predicate path)** — using the pattern
   pinned in the Red test 1 acceptance criterion: `assert
   _reviewer_spec_attempts_exhausted(ctx) is True` (or the impl
   variant). Today's predicate is
   `count_completed(action, ticket_id) >= 3` (no outcome filter), so
   3 `needs_fix` rows + 0 other rows give `count_completed == 3` —
   predicate returns True today AND after the fix. Naturally green
   both ways. To make these GENUINE reds, the test fixture writes 3
   `outcome="needs_fix"` rows AND 1 `outcome="error"` row (4
   terminations total). Today: `count_completed == 4 >= 3` → True
   (green). After the fix:
   `count_completed(outcome="needs_fix") == 3 >= 3` → True (still
   green). To get a genuine red on Tests 1/4 themselves, INVERT the
   fixture and the assertion: write 3 `outcome="error"` rows + 0
   `outcome="needs_fix"` rows and assert `is FALSE`. Today's
   unfiltered predicate returns True (red); after fix's
   needs_fix-filtered predicate returns False (green). Worker MUST
   either (a) rename Tests 1 and 4 to
   `test_reviewer_spec_attempts_exhausted_does_not_fire_on_3_error_terminations`
   / `_impl_` counterpart with the inverted shape (3 error rows,
   assert `is False`), or (b) keep Tests 1 and 4 named as in the AC
   bullets (3 needs_fix rows, assert `is True`) AND add the inverted
   tests as genuine reds. Either approach exercises the new outcome
   filter directly. NOTE: Going through `evaluate(ctx, rules=RULES)`
   with 3 error rows asserts a property the rate-limit already
   enforces (at precedence 42-47, lower than exhaustion at 65-70) —
   the existing test at `test_rules.py:933` already locks
   `Action.RATE_LIMIT_TRIP` on 3 error rows. So the direct-predicate
   form is the ONLY assertion path that genuinely exercises the new
   outcome filter for the error-row case; do not duplicate the line
   933 property through `evaluate`.

   **Tests 2 and 5 (direct-predicate path).** Build a context with 3
   completed dispatches whose outcomes are `["needs_fix",
   "needs_fix", "clean"]` (mixed). Today's unfiltered predicate
   returns True (3 total terminations); after fix's
   needs_fix-filtered predicate returns False (only 2 `needs_fix`).
   Genuine reds via direct call: `assert
   _reviewer_spec_attempts_exhausted(ctx) is False` (and
   `_reviewer_impl_attempts_exhausted` symmetric). Using `evaluate`
   here would tangle the assertion with the rate-limit interaction
   too (2 needs_fix + 1 clean over the window → rate-limit asks
   "consecutive failures past the latest success fence" — `clean` is
   not a success, so `fence_ts` stays None, and the 2 needs_fix
   rows + the 1 clean row all read as failures; today
   `count_recent_failures = 3` → rate-limit also fires). Direct
   call avoids the entanglement.

   **Tests 3 and 6 (`evaluate` path).** Build a context with 5
   completed `dispatch_reviewer_spec` rows whose outcomes are
   `["needs_fix", "needs_fix", "success", "needs_fix", "success"]`,
   no unterminated dispatch, label state `foreman:planning`, PR is
   spec-shaped + unmerged. Today: the line-365 count gate sees
   `count_completed == 5 >= 3` → `_planning_pr_needs_review` returns
   False → no dispatch fires (whatever else `evaluate` returns is
   not `Action.DISPATCH_REVIEWER_SPEC`). After fix: the count gate
   is gone; `_planning_pr_needs_review` returns True. Rate-limit on
   `dispatch_reviewer_spec` reads: most recent success fence is the
   5th termination (`outcome="success"`), so the consecutive-failure
   window past the fence is empty → rate-limit silent. Exhaustion:
   `count_completed(outcome="needs_fix") == 3 >= 3` → exhaustion
   FIRES first (precedence 65 < dispatch tier). After fix's correct
   behavior, exhaustion still wins over dispatch on this fixture
   because the test is ALSO testing the new outcome-aware
   exhaustion. To get Tests 3 and 6 to assert
   `Action.DISPATCH_REVIEWER_SPEC` (not `SURFACE_HELP`) after the
   fix, the fixture's `needs_fix` count must be `< 3`. Adjust the
   outcome sequence to e.g.
   `["needs_fix", "success", "needs_fix", "success", "success"]`
   (2 needs_fix, 3 success — 5 total dispatches, none currently
   in-flight; exhaustion does NOT fire because
   `count_completed(outcome="needs_fix") == 2 < 3`; rate-limit
   silent because the most recent success advances the fence past
   any needs_fix); the only rule that matches at the FORWARD-PROGRESS
   tier is `_planning_pr_needs_review` → dispatch fires.
   Today's line-365 gate STILL blocks regardless of outcome mix
   (`count_completed == 5 >= 3`); after fix the gate is gone and
   dispatch fires. Genuine reds via `evaluate`.

   Confirm all genuine red tests fail on `just check`.

3. **Apply Edit 3 (constant rename) first.** Replace line 80
   `_MAX_REVIEWER_ATTEMPTS = 3` with
   `_MAX_REVIEWER_FIX_VERDICTS = 3`. Update the four usages at lines
   122, 131, 365, 453 to reference the new name. The rename is a pure
   `s/old/new/g` inside rules.py; no semantic change. Run tests after
   this edit to confirm green (no behavior change yet).

4. **Apply Edit 1 (drop dispatch-side gate).** Delete the
   `and ctx.log.count_completed(...) < _MAX_REVIEWER_FIX_VERDICTS`
   line from both `_planning_pr_needs_review` (formerly line 365)
   and `_impl_review_green` (formerly line 453). Update the
   inline doc-comment blocks at lines 353-358 and 444-446 (the
   "Budget cap..." paragraph) to reflect the new "cap fires AFTER,
   not BEFORE" design and link issue #268. Run tests after this
   edit. Tests 3 and 6 (the "dispatch fires after 5+ completions"
   reds) should now go green. Tests 2 and 5 may still be red
   because the attempts-exhausted rule still uses the unfiltered
   count.

5. **Apply Edit 2 (outcome-aware attempts_exhausted).** Replace the
   predicate body of `_reviewer_spec_attempts_exhausted` to pass
   `outcome="needs_fix"` into `count_completed`. Same for
   `_reviewer_impl_attempts_exhausted`. Update both doc-comments
   (lines 109-119 and 126-128) to explain the new semantics and link
   issue #268. Run tests. The genuine reds from sub-request 2 that
   target the new outcome filter should now go green:
   - Tests 1 and 4 in whichever shape the Worker chose in sub-request 2
     — either inverted (3 error rows, assert predicate is False) or
     positive (3 needs_fix rows, direct-predicate call asserts True;
     the predicate flips from True-by-coincidence to True-because-
     filter-matches, which is the semantic change being locked in).
   - Tests 2 and 5 (mixed-outcome `[needs_fix, needs_fix, clean]`,
     direct-predicate call asserts False).

6. **Update the legacy test
   `test_dispatch_reviewer_spec_still_fires_under_budget` at
   `packages/foreman/tests/reconciler/test_rules.py:966`.** The
   property it asserts ("2 completed dispatches is still under the
   dispatch-side cap of 3 → fire") describes a property that no
   longer exists after Edit 1 — there IS no dispatch-side cap. Two
   acceptable Worker outcomes:
   - **Delete the test.** Red test 3
     (`test_dispatch_reviewer_spec_fires_after_many_completed_dispatches_with_no_needs_fix_streak`)
     covers the replacement property: dispatch is not gated on total
     completion count. The legacy test is redundant.
   - **Rename + repurpose the test** to
     `test_dispatch_reviewer_spec_fires_when_some_completed_attempts_have_errored`
     and assert the same dispatch action result but with a docstring
     that frames the property as "dispatch is not gated on total
     completion count, only on no-in-flight + label-state".
   The Worker MUST choose one approach; leaving the test asserting
   the dead property is not acceptable.

7. **Apply the symmetric review of impl-side
   `test_dispatch_reviewer_impl_blocked_after_3_completed_attempts`
   at line 989.** The test uses `outcome="error"` rows and asserts
   `RATE_LIMIT_TRIP` — the rate-limit at precedence 42-47 wins. After
   the fix, `_reviewer_impl_attempts_exhausted` no longer fires on
   error terminations (filter requires `needs_fix`), but the
   rate-limit still trips at 3 errors-in-window. The test passes
   unchanged. CONFIRM by running it; do not modify it.

8. **Run the full quality gate:** `just check`. Expected: exit 0. New
   tests green. The legacy test resolved per sub-request 6. The
   rate-limit + precedence invariant tests green. `new_failures_count
   == 0` (per the issue's acceptance criterion and the project's
   conventional Worker contract).

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/src/foreman/reconciler/rules.py` | Rename `_MAX_REVIEWER_ATTEMPTS` → `_MAX_REVIEWER_FIX_VERDICTS` (line 80). Drop the `count_completed(...) < _MAX_REVIEWER_FIX_VERDICTS` clause from `_planning_pr_needs_review` (line 365) and `_impl_review_green` (line 453). Change `_reviewer_spec_attempts_exhausted` (lines 109-123) and `_reviewer_impl_attempts_exhausted` (lines 126-132) to call `count_completed(..., outcome="needs_fix")`. Update four doc-comment blocks to describe the new "cap fires AFTER, not BEFORE" design and link issue #268. No precedence values change; no rule additions or removals. |
| `packages/foreman/tests/reconciler/test_rules.py` | Add six new red tests (three spec-side, three impl-side) in the existing "Pass 3 HIGH: Reviewer attempt budget (spec + impl)" section after line 1010. Tests 1, 2, 4, 5 use the direct-predicate-call pattern (`assert _reviewer_spec_attempts_exhausted(ctx) is …` / `_reviewer_impl_attempts_exhausted`) to bypass `evaluate`'s precedence walk so the rate-limit at precedences 44/45 (lower than exhaustion at 65/70) does not preempt the assertion under test — see sub-request 2 for the per-test fixture shapes and the rate-limit interaction rationale, and Red test 1's acceptance bullet for the alternative dual-write pattern that lets a Worker use `evaluate` instead. Tests 3 and 6 use `evaluate` with the success-trailing outcome sequence (`[needs_fix, success, needs_fix, success, success]` — needs_fix count strictly < 3 so exhaustion does not fire, trailing success advances `fence_ts` so rate-limit stays silent). Resolve the legacy `test_dispatch_reviewer_spec_still_fires_under_budget` at line 966 per sub-request 6 (delete or rename + repurpose). No changes to the existing rate-limit tests at lines 933 + 989. |
| `packages/foreman/src/foreman/reconciler/exec_log.py` | NOT modified. `count_completed` already accepts `outcome: str \| None`. |
| `packages/foreman/src/foreman/dispatch_recorder.py` | NOT modified. The Recorder already writes the Reviewer's `needs_fix` / `clean` verdict to the `outcome` column via `CostSubscriber.handle_dispatch_complete`. |
| `packages/foreman/src/foreman/roles/reviewer.py` | NOT modified. `emit_recorder_complete` already propagates `llm_output.outcome` literally. |

No expected changes to:

- The four labeled-state-machine transitions (`foreman:planning` ⇄
  `foreman:spec-fix`, `foreman:impl-review` ⇄ `foreman:impl-fix`).
- The Worker / Fixer / Planner budget caps
  (`_fix_attempts_exhausted`, `_spec_fix_attempts_exhausted`,
  `_impl_attempts_exhausted`). Out of scope.
- The rate-limit (`_RATE_LIMIT_TARGETS`, `_build_rate_limit_rules`,
  `_make_rate_limit_predicate`). Out of scope; the rate-limit is the
  retained hot-spawn-loop defense.
- The reviewer's role runner (`roles/reviewer.py`). The propagation
  of `outcome` into the execution_log row already happens.

## Alternatives considered

- **Add a new helper `count_completed_with_outcome(action,
  ticket_id, *, outcome)` to `exec_log.py`.** Rejected — the existing
  `count_completed` already accepts `outcome=` (added in foreman#174 /
  PR for "skipped_capacity excluded by default"). Adding a parallel
  helper duplicates the API surface without a maintenance dividend.
  The issue body explicitly leaves the choice open ("Whichever keeps
  the call-sites cleanest"); reusing the existing kwarg keeps the call
  site one-liner.

- **Surface the Reviewer's verdict (clean/needs_fix) into a NEW
  dedicated column** (e.g., `role_verdict`) instead of reusing the
  existing `outcome` column. Rejected — the Recorder already writes
  the literal verdict string into `outcome` for the Reviewer's
  termination row (`packages/foreman/src/foreman/roles/reviewer.py:608`
  → `outcome=llm_output.outcome` is "clean" or "needs_fix"). Adding a
  new column requires a schema migration, a writer-side change, and
  a query change — all for a property already correctly stored. The
  existing schema is the right answer.

- **Keep the dispatch-side gate but switch it to outcome-aware too.**
  Rejected — the issue's named shape is "the stopping is after the
  reviewer not before". A dispatch-side outcome-aware gate would
  preserve the wrong placement: a Reviewer that hasn't run yet
  doesn't have a verdict yet, so checking the running verdict-count
  before the next run can't capture the next verdict. The only
  correct placement is AFTER each run, which is what the
  attempts_exhausted rule does.

- **Remove `_reviewer_*_attempts_exhausted` entirely and rely only on
  the rate-limit.** Rejected — the rate-limit answers "did Reviewer
  crash 3 times in window W?". A Reviewer that consistently returns
  `needs_fix` over many hours never trips the rate-limit (no crashes,
  no error outcomes). The outcome-aware exhaustion rule answers the
  "Fixer can't satisfy the Reviewer; escalate to human" case, which
  the rate-limit doesn't cover.

- **Make `_MAX_REVIEWER_FIX_VERDICTS` configurable via
  `packages/foreman/src/foreman/config.py`.** Rejected — out of scope.
  Existing budget caps
  (`_MAX_FIX_ATTEMPTS`, `_MAX_IMPL_ATTEMPTS`) are also private
  constants; configurability would be a separate, broader change.
  The issue does not request it.

- **Add a lifecycle-event abstraction (pre/post-dispatch hooks) to
  the reconciler.** Rejected per the issue's "What we explicitly do
  NOT do here" section. Larger design conversation; if it ends up
  being the right end-state, file separately. This ticket is the
  minimum localized fix at the wrong-place gate.

- **Land only the dispatch-side gate removal (Edit 1) without the
  outcome-aware exhaustion change (Edit 2).** Rejected — removing
  the dispatch gate alone leaves the cap counting "Reviewer ran N
  times" via the attempts-exhausted rule firing AFTER each run. The
  empirical case (3 runs, last 2 with verdicts addressed) would
  still escalate at run 3 even if Fixer's pass made run 3's verdict
  `clean`. The two edits work together; landing only one preserves
  the conceptual bug.

## Open questions

(None — the API surface is well-understood, the predicate change
is localized, and the test discipline is concrete.)

## Out of scope

- **Lifecycle-event architecture for the reconciler** (explicit
  pre/post-dispatch hooks, transition events). File separately if
  the team decides to redesign around it; this ticket assumes the
  existing snapshot-eval reconciler shape.
- **Worker / Fixer / Planner budget cap audits**
  (`_fix_attempts_exhausted`, `_spec_fix_attempts_exhausted`,
  `_impl_attempts_exhausted`). If any of these have a similar
  "wrong-place gate" bug, file a separate ticket; this spec changes
  only the Reviewer-side budget.
- **Removing the per-ticket-per-role rate-limit** at
  `packages/foreman/src/foreman/reconciler/rules.py:135-180`. The
  rate-limit is the retained hot-spawn-loop defense and stays.
- **Adjusting `_MAX_REVIEWER_FIX_VERDICTS` from 3 to a different
  value.** The issue says 3; keep 3.
- **Making the constant configurable** via TOML / env var. Existing
  budget constants are private — configurability would be a broader
  refactor across all four roles.
- **Reviewing the Recorder's dual-write behavior** (whether the
  daemon's `terminate_dispatch` row AND the Recorder's cost row
  both count toward `count_completed`). The outcome-aware filter
  inherently selects only the Recorder row (which carries the
  Reviewer's verdict) — so even if both rows exist per dispatch,
  the `needs_fix` filter still produces the correct count. No
  Recorder change is needed for this fix.
- **Adding new label states or pipeline phases.** Not needed.
- **Symmetric changes to spec-side Planner budget**
  (`_planning_no_pr` already uses `outcome="success"` correctly per
  foreman#174 — no work needed there).

## References

- foreman#268 — this ticket. The empirical case (foreman#258 + PR
  #259) where two clean Fixer passes were nevertheless escalated to
  needs-help by the attempt-3 dispatch gate.
- foreman#258 — the spec PR that surfaced the bug. Both Fixer
  passes ran cleanly; the system never got to see the would-have-
  been third Reviewer verdict.
- foreman#228 — the per-ticket-per-role consecutive-failure
  rate-limit. The retained hot-spawn-loop defense.
- foreman#174 — the `count_completed(outcome=...)` extension. The
  existing API this spec reuses.
- foreman v3 Plan B Stage 1+2 — the original budget caps that
  closed the crashed-Reviewer silent-stall (also addressed by
  foreman#228 more correctly).
- Jeff design ask 2026-06-10 PM: "If something went to a reviewer,
  it should always review. If the reviewer found issues too many
  times it should stop. The stopping is after the reviewer not
  before."
- Source code reference points:
  - `packages/foreman/src/foreman/reconciler/rules.py:80,109-123,126-132,338-366,437-454` — predicates and constants being edited.
  - `packages/foreman/src/foreman/reconciler/rules.py:150-157` — rate-limit targets table (verifies dispatch_reviewer_spec + dispatch_reviewer_impl coverage).
  - `packages/foreman/src/foreman/reconciler/exec_log.py:342-394` — `count_completed(outcome=)` API.
  - `packages/foreman/src/foreman/dispatch_recorder.py:149-210` — Recorder writes the verdict into the termination row's `outcome` column.
  - `packages/foreman/src/foreman/roles/reviewer.py:579-612` — Reviewer's `outcome=llm_output.outcome` propagation through `emit_recorder_complete`.
  - `packages/foreman/tests/reconciler/test_rules.py:933-1010` — existing reviewer-budget tests; new tests sit in the same section.
  - `packages/foreman/tests/reconciler/test_exec_log.py:280-333` — existing `count_completed(outcome=)` contract test.
  - `packages/foreman/tests/reconciler/test_rules_precedence.py:25,48` — invariant tests to keep green.
