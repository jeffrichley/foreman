# Spec: auto-rerun flaky failed required-checks before blocking merge (issue #537)

## Goal

When a required CI check reports `FAILED` in the merge coordinator's tick loop, foreman should rerun the failed checks once (the same way it handles `TIMED_OUT_OR_CANCELLED` today) before treating the PR as blocked. A check that keeps failing after `MAX_ATTEMPTS = 3` coordinator-driven reruns escalates to `NeedsHelp` with the failure named. This prevents flaky required-check failures (ASGI teardown errors, subprocess timeouts mis-classified by GitHub as `FAILED` rather than `TIMED_OUT_OR_CANCELLED`) from stalling the pipeline indefinitely. See issue [#537](https://github.com/jeffrichley/foreman/issues/537).

## Acceptance criteria

- On the first coordinator tick where `required_check_state` returns `FAILED`, `attempt_merge` calls `ctx.git.rerun_failed_checks(...)` and returns `BLOCKED` with `details={RERUN_DETAIL_KEY: True}` — the same marker used by `TIMED_OUT_OR_CANCELLED` reruns.
- The ticket stays in `MergeQueued` (not routed to `ImplFix` / `SpecFix`) on that tick.
- `MergeCoordinator._handle_blocked` counts the rerun as a "real action" (because `RERUN_DETAIL_KEY` is present) and increments `entry.attempts`.
- After `MergeCoordinator.MAX_ATTEMPTS = 3` consecutive ticks where the check is still `FAILED` (and thus reruns are still issued), the coordinator escalates to `NeedsHelp` and dequeues the entry — a genuinely-failing check is not looped forever.
- If the rerun succeeds (check becomes `PASSED`, PR becomes mergeable), the PR merges normally and the ticket routes to `Done` (impl) or `Implementing` (spec) as before.
- Two existing tests in `packages/foreman/tests/v4/test_merge_coordinator.py` that asserted `FAILED → ImplFix/SpecFix` on the first tick are updated to reflect the new behaviour (first tick → BLOCKED + rerun).
- `just check` exits zero.

## Approach

**Pattern (Decision 4):** No GoF pattern fits — this is a straightforward if-branch replacement, extending one path in an existing classifier to reuse an already-designed mechanism. The Google engineering principle is **"make the right thing easy"**: `TIMED_OUT_OR_CANCELLED` already has a bounded-rerun path (`_rerun_or_escalate`) that does exactly what flaky `FAILED` checks need. Routing `FAILED` through the same mechanism rather than building a parallel one is DRY and exploits the existing test coverage.

### Root cause

In `packages/foreman/src/foreman/v4/states/merge_helper.py`, the `attempt_merge` function's classifier (foreman#317) routes `RequiredCheckState.FAILED` immediately to `NEEDS_FIX`:

```python
if check == RequiredCheckState.FAILED:
    return Outcome(
        kind=OutcomeKind.NEEDS_FIX,
        ...
        details={"fix_reason": "ci_failed"},
    )
```

The `MergeCoordinator._failure_state` method maps `NEEDS_FIX` to `"ImplFix"` / `"SpecFix"`, which dispatches the Worker to fix code that may not be broken — the failure was a CI infrastructure flake.

`TIMED_OUT_OR_CANCELLED` already has the right behavior:

```python
if check == RequiredCheckState.TIMED_OUT_OR_CANCELLED:
    return _rerun_or_escalate(ctx, pr_number)
```

`_rerun_or_escalate` issues one rerun and returns `BLOCKED` with `RERUN_DETAIL_KEY: True`. The coordinator's `MAX_ATTEMPTS = 3` bound (tracked on the `merge_queue` entry's `attempts` field) caps reruns. For coordinator-driven merges, `_prior_rerun_count` always returns 0 (the synthetic `MergeQueued` `StateContext` never gets `outcome_kind` written — see `merge_coordinator._ctx_for` docstring), so the coordinator's `MAX_ATTEMPTS = 3` is the effective rerun cap in production (not `MAX_CHECK_RERUNS = 1` in merge_helper, which applies to non-coordinator callers). This is consistent across both `TIMED_OUT_OR_CANCELLED` and the new `FAILED` path.

### Fix

Two small changes in `merge_helper.py`:

1. **Parameterize `_rerun_or_escalate`** with a `cause: str` keyword argument (default `"timed out/cancelled"`). Use `cause` in the BLOCKED summary and the NEEDS_HELP escalation summary so the per-branch human-readable message is accurate. Backward-compatible: the existing `TIMED_OUT_OR_CANCELLED` callsite passes no argument and gets the same text as before.

2. **Replace the `FAILED` branch** in `attempt_merge`: instead of returning `NEEDS_FIX` immediately, call `_rerun_or_escalate(ctx, pr_number, cause="failed")`.

After this change, the `FAILED` path escalates to `NEEDS_HELP` (not `NEEDS_FIX`) if the check keeps failing after all retries — intentional, because after N reruns a human should decide whether it's a persistent CI infrastructure problem or a real code failure. The issue's acceptance criteria explicitly says "surfaces to NeedsHelp".

### Existing test breakage and required updates

`test_ci_failed_routes_to_impl_fix_and_dequeues` and `test_ci_failed_spec_routes_to_spec_fix` in `packages/foreman/tests/v4/test_merge_coordinator.py` assert the OLD `FAILED → ImplFix/SpecFix` behavior on the first tick. Both must be updated to assert the NEW behavior: first tick → ticket stays `MergeQueued`, `rerun_failed_checks_calls` records one call, `entry.attempts == 1`.

## Sub-requests (topologically sorted)

1. **Modify `_rerun_or_escalate` in `packages/foreman/src/foreman/v4/states/merge_helper.py`** to accept a `cause: str = "timed out/cancelled"` keyword parameter and use it in both summary strings (the BLOCKED rerun summary and the NEEDS_HELP escalation summary).

   Current signature:
   ```python
   def _rerun_or_escalate(ctx: StateContext, pr_number: int) -> Outcome:
   ```

   New signature and body:
   ```python
   def _rerun_or_escalate(ctx: StateContext, pr_number: int, *, cause: str = "timed out/cancelled") -> Outcome:
       """Re-run a PR's failed/timed-out required checks once, then escalate.

       foreman#317 Decision T: originally for TIMED_OUT_OR_CANCELLED.
       foreman#537: extended to FAILED (same bounded-rerun logic, different cause label).
       Re-runs once (bounded by MAX_CHECK_RERUNS when called from a TicketState context;
       by MergeCoordinator.MAX_ATTEMPTS in the coordinator context — see _ctx_for docstring).
       """
       prior = _prior_rerun_count(ctx)
       if prior >= MAX_CHECK_RERUNS:
           return Outcome(
               kind=OutcomeKind.NEEDS_HELP,
               confidence=OutcomeConfidence.HIGH,
               summary=f"required check {cause} after {MAX_CHECK_RERUNS} re-run — escalating",
               artifacts=OutcomeArtifacts(pr_number=pr_number),
           )
       assert ctx.git is not None
       ctx.git.rerun_failed_checks(project=ctx.ticket.project, pr_number=pr_number)
       return Outcome(
           kind=OutcomeKind.BLOCKED,
           confidence=OutcomeConfidence.HIGH,
           summary=f"required check {cause} — re-running once",
           artifacts=OutcomeArtifacts(pr_number=pr_number),
           details={RERUN_DETAIL_KEY: True},
       )
   ```

2. **Replace the `FAILED` branch in `attempt_merge`** (same file) from the immediate `NEEDS_FIX` return to `_rerun_or_escalate`:

   Current:
   ```python
   if check == RequiredCheckState.FAILED:
       return Outcome(
           kind=OutcomeKind.NEEDS_FIX,
           confidence=OutcomeConfidence.HIGH,
           summary="required CI check failed — routing to ImplFix",
           artifacts=OutcomeArtifacts(pr_number=pr_number),
           details={"fix_reason": "ci_failed"},
       )
   ```

   New:
   ```python
   if check == RequiredCheckState.FAILED:
       return _rerun_or_escalate(ctx, pr_number, cause="failed")
   ```

3. **Update `test_ci_failed_routes_to_impl_fix_and_dequeues`** in `packages/foreman/tests/v4/test_merge_coordinator.py` to reflect the new first-tick behavior:

   ```python
   def test_ci_failed_reruns_and_stays_queued_on_first_tick():
       """foreman#537: a failed required check is re-run once before blocking merge.
       On the first tick the PR stays in MergeQueued (not routed to ImplFix)
       and rerun_failed_checks is called once."""
       repo, git = _fake()
       ticket = _seed_ticket_in_merge_queue(
           repo,
           git,
           issue_number=1,
           pr=10,
           kind="impl",
           mergeable_state="blocked",
           ci=RequiredCheckState.FAILED,
       )
       _coordinator(repo, git).tick()
       assert repo.get_ticket(ticket.id).current_state == "MergeQueued"
       assert git.rerun_failed_checks_calls == [("p", 10)]
       entry = repo.head_merge_entry("p")
       assert entry is not None
       assert entry.attempts == 1
   ```

4. **Update `test_ci_failed_spec_routes_to_spec_fix`** in `packages/foreman/tests/v4/test_merge_coordinator.py` with the same first-tick assertion change:

   ```python
   def test_ci_failed_spec_reruns_and_stays_queued_on_first_tick():
       """foreman#537: same bounded-rerun logic applies to spec PRs with FAILED checks."""
       repo, git = _fake()
       ticket = _seed_ticket_in_merge_queue(
           repo,
           git,
           issue_number=1,
           pr=10,
           kind="spec",
           mergeable_state="blocked",
           ci=RequiredCheckState.FAILED,
       )
       _coordinator(repo, git).tick()
       assert repo.get_ticket(ticket.id).current_state == "MergeQueued"
       assert git.rerun_failed_checks_calls == [("p", 10)]
   ```

5. **Add `test_failed_check_escalates_to_needs_help_after_max_attempts`** to `packages/foreman/tests/v4/test_merge_coordinator.py` — verifies the bound:

   ```python
   def test_failed_check_escalates_to_needs_help_after_max_attempts():
       """foreman#537: a required check that keeps failing for MAX_ATTEMPTS
       coordinator ticks escalates to NeedsHelp and dequeues — not looped forever."""
       repo, git = _fake()
       ticket = _seed_ticket_in_merge_queue(
           repo,
           git,
           issue_number=1,
           pr=10,
           kind="impl",
           mergeable_state="blocked",
           ci=RequiredCheckState.FAILED,
       )
       coordinator = _coordinator(repo, git)
       # Ticks 1 and 2: rerun, stay queued, attempts advance.
       coordinator.tick()
       assert repo.get_ticket(ticket.id).current_state == "MergeQueued"
       coordinator.tick()
       assert repo.get_ticket(ticket.id).current_state == "MergeQueued"
       # Tick 3: MAX_ATTEMPTS reached — escalate to NeedsHelp, dequeue.
       coordinator.tick()
       assert repo.get_ticket(ticket.id).current_state == "NeedsHelp"
       assert repo.head_merge_entry("p") is None
       assert len(git.rerun_failed_checks_calls) == MergeCoordinator.MAX_ATTEMPTS
   ```

6. **Add `test_failed_check_clears_after_rerun_and_merges`** to `packages/foreman/tests/v4/test_merge_coordinator.py` — regression guard for the happy path (a flaky check that passes on rerun):

   ```python
   def test_failed_check_clears_after_rerun_and_merges():
       """foreman#537: if the rerun succeeds, the PR merges normally on the next tick."""
       repo, git = _fake()
       ticket = _seed_ticket_in_merge_queue(
           repo,
           git,
           issue_number=1,
           pr=10,
           kind="impl",
           mergeable_state="blocked",
           ci=RequiredCheckState.FAILED,
       )
       coordinator = _coordinator(repo, git)
       # First tick: FAILED → rerun, stays queued.
       coordinator.tick()
       assert repo.get_ticket(ticket.id).current_state == "MergeQueued"
       assert git.rerun_failed_checks_calls == [("p", 10)]
       # Simulate the rerun clearing the failure: PR is now mergeable + CI green.
       git.set_pr_state(
           project="p",
           pr_number=10,
           state=PRState(
               merged=False,
               mergeable=True,
               ci_passing=True,
               base_ref="main",
               mergeable_state="clean",
           ),
       )
       git.seed_check_state("p", 10, RequiredCheckState.PASSED)
       # Second tick: PR merges, ticket routes to Done.
       coordinator.tick()
       assert repo.get_ticket(ticket.id).current_state == "Done"
       assert ("p", 10) in git.merge_pr_calls
       assert repo.head_merge_entry("p") is None
   ```

7. **Run `just check`** — all tests must pass, mypy exits zero.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/v4/states/merge_helper.py` | Add `cause: str = "timed out/cancelled"` parameter to `_rerun_or_escalate`; replace `FAILED → NEEDS_FIX` branch in `attempt_merge` with `_rerun_or_escalate(ctx, pr_number, cause="failed")` |
| `packages/foreman/tests/v4/test_merge_coordinator.py` | Rename/rewrite `test_ci_failed_routes_to_impl_fix_and_dequeues` → `test_ci_failed_reruns_and_stays_queued_on_first_tick`; rename/rewrite `test_ci_failed_spec_routes_to_spec_fix` → `test_ci_failed_spec_reruns_and_stays_queued_on_first_tick`; add `test_failed_check_escalates_to_needs_help_after_max_attempts`; add `test_failed_check_clears_after_rerun_and_merges` |

## Alternatives considered

- **Keep `FAILED → NEEDS_FIX` and add a separate "rerun-before-fix" gate upstream**: This would require adding state to the merge coordinator to distinguish "first FAILED encounter" from "FAILED after rerun" — a new counter field or a new merge queue status. Rejected because `RERUN_DETAIL_KEY` + `MAX_ATTEMPTS` already tracks exactly this; no new state is needed.
- **Use a different counter/key for FAILED reruns, independent of TIMED_OUT_OR_CANCELLED**: Would allow FAILED and TIMED_OUT_OR_CANCELLED to each get their own rerun budget. Rejected as over-engineering — a combined rerun budget of `MAX_ATTEMPTS = 3` is correct: a PR that bounced between TIMED_OUT_OR_CANCELLED and FAILED should not accumulate double the reruns. The shared `RERUN_DETAIL_KEY` correctly caps total reruns regardless of how GitHub classified each failure.
- **Route to NeedsHelp (not NeedsHelp) on the first FAILED occurrence, with no rerun**: Eliminates the rerun entirely — deterministic but fragile. The whole point of this ticket is to absorb the common case of a single flaky check. Rejected.
- **Do nothing; document that operators should manually `gh run rerun --failed`**: The observed impact (#412, #417) is that any flaky required check stalls the pipeline indefinitely. A manual escape hatch already exists; the ticket explicitly asks for automated resilience. Rejected.

## Open questions

None — the approach, exact callsite, and counter semantics are all verifiable from the codebase.

## Out of scope

- Changing `GitProvider.required_check_state` to return per-check details (names, run IDs). The current method returns a single `RequiredCheckState` enum; naming individual failing checks in the NeedsHelp summary would require a protocol change. Not required by the issue.
- Changing `MAX_ATTEMPTS` or `MAX_CHECK_RERUNS`. The existing bounds (3 coordinator-driven reruns, 1 TicketState-driven rerun) are correct and apply symmetrically to both `FAILED` and `TIMED_OUT_OR_CANCELLED`.
- The underlying flaky tests in agent_core (Track B #401) — that is a separate workstream.
- Changing routing for `ACTION_REQUIRED` (stays `NEEDS_HELP`), `dirty` (stays `NEEDS_FIX` / merge conflict), or `draft` (stays `NEEDS_HELP`).
