# Spec: ImplApproved auto-finalizes on human merge (issue #443)

## Goal

Turn `ImplApprovedState` from a dead-end terminal into a **polling wait-then-finalize** state: each poll checks whether the human has merged the impl PR, and when detected, closes the originating issue and transitions to Done. The human still pulls the merge trigger (preserving the trust boundary from #418); foreman adds the auto-close bookkeeping that was missing.

See issue [#443](https://github.com/anthropics/foreman/issues/443).

## Acceptance criteria

- `ImplApproved` is removed from `_TERMINAL_STATE_NAMES` (in `state.py`) and from the Poller's `_TERMINAL_STATES` (in `poller.py`); the Poller re-enqueues it each tick.
- When the impl PR is `merged=True`, `ImplApprovedState.execute()` calls `close_originating_issue(ctx)` and returns `OutcomeKind.CLEAN`; `next_state()` returns `DoneState()`.
- When the impl PR is open (`merged=False, closed=False`), `execute()` returns `OutcomeKind.BLOCKED`; `next_state()` returns `ImplApprovedState()` (self-loop). The Poller re-enqueues on the next tick.
- When the impl PR is closed-without-merge (`merged=False, closed=True`), `execute()` returns `OutcomeKind.NEEDS_HELP` with a human-readable reason; `next_state()` returns `NeedsHelpState()`.
- N consecutive BLOCKED polls from `ImplApprovedState` do NOT trip the runaway cap (BLOCKED rows are already skipped by `count_consecutive_same_state`; a test asserts this explicitly for the ImplApproved case).
- `close_issue` is called from a single shared helper `close_originating_issue(ctx)` in `states/merge_helper.py`, used by both `MergingState` and `ImplApprovedState`.
- `PRState` gains a `closed: bool = False` field; `PyGithubGitProvider.get_pr_state` populates it from `pr.state == "closed"`; `FakeGitProvider.close_pr` marks the stored `PRState` as `closed=True` (so test code calling `close_pr` on the fake automatically reflects a closed-without-merge PR).
- Unit tests for all branches: merged→Done+close, open→BLOCKED (no close), closed-unmerged→NeedsHelp (no close), and already-closed-issue→idempotent `close_issue` call.
- `just check` exits zero; `new_failures_count == 0`.

## Approach

**Pattern:** State Pattern (GoF) — `ImplApprovedState` transitions from a parked terminal into an active polling state. The Template Method (`TicketState.transition()`) already handles BLOCKED self-loops correctly; no changes to the orchestration layer are needed. The `close_originating_issue` helper extraction is a straight DRY refactor — no pattern applies beyond "extract shared call into one function."

**Detecting closed-without-merge:** `PRState` currently lacks a `closed` field. GitHub exposes this through `pr.state == "closed"` when the PR is closed (whether or not it was merged). A `closed: bool = False` field (default preserves all existing test fixtures) is the narrowest change: `PyGithubGitProvider.get_pr_state` sets it from the live API; `FakeGitProvider.close_pr` mutates the stored `PRState` to `closed=True` so the fake reflects the real invariant. Note that a merged PR has `merged=True` AND `state == "closed"` on GitHub — `ImplApprovedState.execute()` must therefore check `merged` **first** (the `state.merged` check wins over the `state.closed` check).

**Runaway-cap exemption:** BLOCKED outcomes are already excluded from `count_consecutive_same_state` (see `repository.py:457–459`). Returning `OutcomeKind.BLOCKED` from the "PR still open" branch is therefore automatically exempt — no new predicate or skip-set change is required. The Worker adds a test that asserts N polls don't escalate to prove this invariant for ImplApproved specifically.

**`_TERMINAL_STATE_NAMES` removal:** After removing `ImplApproved` from `_TERMINAL_STATE_NAMES` in `state.py`, the `transition()` Template Method no longer calls `_enter_terminal` inline when advancing to `ImplApproved`. Instead, the Poller re-enqueues the ticket, the WorkerPool opens a new `state_instances` row, and `transition()` publishes `StateEnteredEvent` naturally on the next dispatch. The label-observability label is set on the first real dispatch — a one-tick delay, not a regression, since labels were set by `_enter_terminal` immediately before (the label set is idempotent).

**`mutations.py` / `cmd_retry`:** After the removal, `ImplApproved` no longer appears in `_TERMINAL_STATE_NAMES`, so `cmd_retry` won't hit the "terminal → can it retry?" branch for ImplApproved tickets. This is correct: a ticket polling at ImplApproved can simply be re-enqueued directly without special terminal handling. The `_RETRYABLE_TERMINALS` set is unchanged (`ImplApproved` was never in it). Update stale comments in `mutations.py`.

**Lifecycle test `test_impl_approved_operator_resume_to_done`:** This test (in `test_lifecycle.py`) verifies the old operator-resume path (park → set-state → Merging → Done). After our change, the operator no longer needs to manually set-state to Merging; foreman detects the human merge automatically. The test must be rewritten to exercise the new auto-detect flow: seed the PR as open (BLOCKED poll), then seed it as merged (CLEAN poll → Done + issue closed). The local `_TERMINAL_STATES` helper in `test_lifecycle.py` must also drop `"ImplApproved"`.

## Sub-requests (topologically sorted)

1. Add `closed: bool = False` to `PRState` in `git_provider.py`; update `FakeGitProvider.close_pr` to set `closed=True` on the stored `PRState`; update `PyGithubGitProvider.get_pr_state` to populate `closed = pr.state == "closed"`.
2. Extract `close_originating_issue(ctx: StateContext) -> None` free function into `states/merge_helper.py`; update `MergingState.execute`'s `on_merge_success` callback to call it instead of `git.close_issue(...)` directly.
3. Remove `"ImplApproved"` from `_TERMINAL_STATE_NAMES` in `state.py` and from `_TERMINAL_STATES` in `poller.py`; update the accompanying comments.
4. Rewrite `ImplApprovedState` in `states/impl_approved.py` as a polling-wait-then-finalize state with three branches (merged → CLEAN + close + Done; open → BLOCKED + self; closed-unmerged → NEEDS_HELP).
5. Update `mutations.py` stale comments (the `_TERMINAL_STATE_NAMES` import comment and the `_RETRYABLE_TERMINALS` comment that mentions ImplApproved as not-retryable).
6. Rewrite `tests/v4/states/test_impl_approved.py` with unit tests for all four branches (merged → Done + close, open → BLOCKED, closed-unmerged → NeedsHelp, already-closed → idempotent) plus the runaway-cap exemption test.
7. Update `tests/v4/test_lifecycle.py`: remove `"ImplApproved"` from the local `_TERMINAL_STATES` helper; rewrite `test_impl_approved_operator_resume_to_done` to exercise the auto-detect flow.

## File-level changes

| File | Change |
|------|--------|
| `packages/foreman/src/foreman/v4/git_provider.py` | Add `closed: bool = False` to `PRState`; update `FakeGitProvider.close_pr` to mutate stored `PRState` to `closed=True` |
| `packages/foreman/src/foreman/v4/pygithub_git_provider.py` | Populate `closed = pr.state == "closed"` in `get_pr_state` |
| `packages/foreman/src/foreman/v4/states/merge_helper.py` | Add `close_originating_issue(ctx: StateContext) -> None` free function |
| `packages/foreman/src/foreman/v4/states/merging.py` | Update `on_merge_success` to call `close_originating_issue(ctx)` |
| `packages/foreman/src/foreman/v4/states/impl_approved.py` | Full rewrite: polling state with merged/open/closed-unmerged branches |
| `packages/foreman/src/foreman/v4/state.py` | Remove `"ImplApproved"` from `_TERMINAL_STATE_NAMES`; update comment |
| `packages/foreman/src/foreman/v4/poller.py` | Remove `"ImplApproved"` from `_TERMINAL_STATES`; update comment |
| `packages/foreman/src/foreman/v4/cli/mutations.py` | Update two stale comments that mention `ImplApproved` as terminal/non-retryable |
| `packages/foreman/tests/v4/states/test_impl_approved.py` | Full rewrite with new behavior tests |
| `packages/foreman/tests/v4/test_lifecycle.py` | Drop `"ImplApproved"` from local `_TERMINAL_STATES`; rewrite `test_impl_approved_operator_resume_to_done` |

## Alternatives considered

1. **Do nothing; document the manual operator path** (`foreman set-state <id> Merging`): Rejected — this is exactly the manual bookkeeping foreman is supposed to eliminate. Observed live on ticket 408 / issue 413.
2. **Add a new `AwaitingMerge` state** instead of repurposing `ImplApproved`: Rejected — ImplApproved already carries the correct semantic ("impl approved, awaiting human merge"). A new state adds schema complexity (new registry entry, new label, migration concern) for no behavioral benefit.
3. **Route `auto_merge_impl=False` through `MergingState` with a "no-merge" flag**: Rejected — this conflates the merge-gate decision (shall we call `merge_pr`?) with the merge-detection loop (has the human merged it?). `ImplApproved` is the correct semantic boundary between the two concerns.

## Open questions

None. The approach is fully determined by the existing patterns in the codebase: BLOCKED self-loops, the runaway-cap skip for BLOCKED rows, and the `attempt_merge` hook injection pattern for `close_issue`.

## Out of scope

- Changing the `auto_merge_impl` default (stays `False` — the human-merge gate from #418 is intentional).
- The `MergingState` (`auto_merge_impl=True`) merge path — unchanged except the `on_merge_success` refactor delegates to the shared helper.
- Hoisting the runaway-exempt skip-set into a single `is_runaway_exempt()` predicate (mentioned as a "while we're here" arch note; deferred — BLOCKED already gives the right semantics automatically, and refactoring the three distinct sets requires touching Postgres + Poller + Repository under separate test coverage).
- Granular `mergeable_state` handling in `ImplApprovedState` (CI-failed, dirty, etc.) — `ImplApprovedState` doesn't attempt a merge, so mergeable_state is irrelevant; only `merged` and `closed` matter.
