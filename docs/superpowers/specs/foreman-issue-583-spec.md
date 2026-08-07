# Spec: back off `ImplApproved` human-gated polling to a long interval (issue #583)

## Goal

Stop `ImplApprovedState` from re-enqueuing itself every ~36 seconds while waiting for a human to merge the impl PR. Instead, set `next_action_at` on each `BLOCKED` self-loop so the Poller defers the next poll by 5 minutes, reducing 2,160+ role instances per ticket to roughly 25 per day. See issue [#583](https://github.com/anthropics/foreman/issues/583).

## Acceptance criteria

- A module-level constant `HUMAN_POLL_INTERVAL_SECONDS: int = 300` exists in `packages/foreman/src/foreman/v4/states/impl_approved.py`.
- `ImplApprovedState.next_state()` calls `ctx.repo.set_next_action_at(ctx.ticket.id, when=ctx.clock() + dt.timedelta(seconds=HUMAN_POLL_INTERVAL_SECONDS))` before returning `ImplApprovedState()` on a `BLOCKED` outcome.
- `ImplApprovedState.next_state()` calls `ctx.repo.clear_next_action_at(ctx.ticket.id)` on `CLEAN` and `NEEDS_HELP` outcomes (defense in depth — clears any pending suspension when the state resolves).
- `impl_approved.py` adds `import datetime as dt` (it has none today).
- A test `test_impl_approved_blocked_sets_next_action_at` in `packages/foreman/tests/v4/states/test_impl_approved.py` verifies that after `next_state(ctx, blocked_outcome)` is called, `ctx.repo.get_ticket(ticket_id).next_action_at` equals `ctx.clock() + timedelta(seconds=HUMAN_POLL_INTERVAL_SECONDS)`.
- A test `test_impl_approved_clean_clears_next_action_at` verifies that after `next_state(ctx, clean_outcome)`, `next_action_at` is cleared to `None` when it was previously set.
- A test `test_impl_approved_needs_help_clears_next_action_at` verifies the same clearing on `NEEDS_HELP`.
- All existing `test_impl_approved_*.py` tests continue to pass unchanged.
- `just check` exits zero.

## Approach

**Pattern naming (per CLAUDE.md Decision 4):** No GoF pattern fits. The principle is "make the right thing easy": the `next_action_at` suspension mechanism already exists in `TicketRecord` / `TicketRepository.set_next_action_at` / `Poller._enqueue_open_tickets` — the backoff scheduler uses it for transient-provider-error retries (`RoleDispatchState._handle_transient`, `backoff.py`). The fix threads the same seam through `ImplApprovedState.next_state()`, which is the only change needed.

**Why `next_state()`, not `execute()`?** `execute()` does the git poll and produces the `Outcome`; it has no business scheduling the next retry — that is routing policy. `next_state()` is where all other suspension decisions live (see `RoleDispatchState._handle_transient`). Placing it here keeps the execute/route separation clean.

**The constant.** `HUMAN_POLL_INTERVAL_SECONDS = 300` (5 minutes) reduces the 2,160-instance observation to roughly 12 polls per hour instead of 100+, while remaining responsive enough to close the ticket within minutes of a human clicking merge. Placing the constant at module level makes it discoverable and overridable in integration tests without monkey-patching.

**Clearing on CLEAN/NEEDS_HELP.** `RoleDispatchState.next_state()` already calls `ctx.repo.clear_next_action_at()` defensively on every non-transient outcome (line 107 of `role_dispatch.py`). `ImplApprovedState` bypasses `RoleDispatchState` entirely (it inherits from `TicketState` directly), so it must call `clear_next_action_at` itself. Without this, a ticket that transitions from `BLOCKED → CLEAN` mid-suspension (human merged, next poll fell inside the 5-min window) would still have a stale `next_action_at` on its new `Done` row — harmless but misleading in `foreman ps`.

**Operator bypass already works.** `cmd_retry` in `mutations.py:524` already calls `repo.clear_next_action_at(ticket_id)` before re-enqueuing — the suspension is cleared whenever an operator forces a retry. No mutations.py changes are needed.

**`foreman ps` observability.** The `next_action_at` column is already surfaced in `ps.py:47` — operators can already see the suspension deadline. No CLI changes are needed.

**Secondary finding (transition count in monitoring) is out of scope.** The issue notes that state-comparison sweeps are blind to busy-wait livelocks because the state label never changes. That is a separate improvement; fixing the primary bug (reducing poll frequency by 8–10×) makes the secondary gap much less costly. A transition-count column in `foreman ps` would be the natural fix; it is deferred.

## Sub-requests (topologically sorted)

1. **`impl_approved.py`**: Add `import datetime as dt` at the top. Add `HUMAN_POLL_INTERVAL_SECONDS: int = 300` as a module-level constant. In `next_state()`, on `BLOCKED`, call `ctx.repo.set_next_action_at(ctx.ticket.id, when=ctx.clock() + dt.timedelta(seconds=HUMAN_POLL_INTERVAL_SECONDS))`. On `CLEAN` and `NEEDS_HELP`, call `ctx.repo.clear_next_action_at(ctx.ticket.id)`.

2. **`test_impl_approved.py`**: Add three new test functions covering (a) BLOCKED sets `next_action_at`, (b) CLEAN clears a pre-existing `next_action_at`, (c) NEEDS_HELP clears a pre-existing `next_action_at`. All three use the existing `_ctx()` fixture; tests (b) and (c) pre-seed `next_action_at` via `repo.set_next_action_at()` before calling `next_state()`.

## File-level changes

| File | Change |
|------|--------|
| `packages/foreman/src/foreman/v4/states/impl_approved.py` | Add `import datetime as dt`; add `HUMAN_POLL_INTERVAL_SECONDS = 300`; wire `set_next_action_at` on BLOCKED and `clear_next_action_at` on CLEAN/NEEDS_HELP in `next_state()`. |
| `packages/foreman/tests/v4/states/test_impl_approved.py` | Add three tests: `test_impl_approved_blocked_sets_next_action_at`, `test_impl_approved_clean_clears_next_action_at`, `test_impl_approved_needs_help_clears_next_action_at`. |

## Alternatives considered

- **Webhook-driven wake-up on PR merge.** The most efficient fix — no polling at all; the ticket wakes exactly when the merge webhook arrives. Ruled out: the webhook receiver would need to map the merged PR to its ticket and call `repo.clear_next_action_at` + re-enqueue, which requires new infra wiring not justified by this issue alone. The 5-minute interval is a safe, deployable intermediate step; a follow-up issue can add webhook wake-up on top.
- **Raise the global `tick_seconds`.** Increasing `V4Config.tick_seconds` (currently 36 s) would slow all polling — including machine-gated CI polling in `MergingState` and `ImplementingState`, which is intentionally fast. Ruled out: per-state suspension is the right granularity; the global tick knob is a blunt instrument.
- **Configurable per-project `human_poll_interval`.** Add a field to `ProjectConfig` so operators can tune the interval per project. Ruled out for now: the constant already achieves the fix; a config knob adds schema surface and TOML migration work not proportionate to a 5-minute default that suits the observed use-case. A follow-up can promote the constant to config if operators request it.

## Open questions

None. The approach is fully grounded in the existing codebase — `next_action_at`, `set_next_action_at`, `clear_next_action_at`, the Poller filter, `cmd_retry`'s existing bypass, and the precedent in `RoleDispatchState._handle_transient` were all verified by reading the source before writing this spec.

## Out of scope

- Webhook-driven wake-up on PR merge (see Alternatives).
- Adding a `transition_count` or `sequence` column to `foreman ps` (the secondary monitoring finding in the issue).
- Making the 5-minute interval configurable per project.
- Adding the same slow-poll behavior to any other state (only `ImplApproved` was reported as the busy-wait; other BLOCKED states like `MergingState` and `ImplementingState` are machine-gated and poll at the correct cadence).
