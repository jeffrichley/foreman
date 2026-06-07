# Spec: clear stale `foreman:merging-*` labels after successful attempt_merge (issue #190)

## Goal

Stop the silent stall where, after a spec PR merges, `foreman:merging-plan`
remains on the issue and blocks the next-phase rule
(`_advance_label_to_merging_impl_when_eligible`) from firing once the impl
PR is approved. The fix is small and local: after `_handle_attempt_merge`'s
`CLEAN` branch successfully calls `host.merge_pr`, the same handler removes
the corresponding `foreman:merging-<target>` label so downstream phase
predicates see a clean post-merge label set. Mirror the change for the
impl side. Addresses issue
[#190](https://github.com/jeffrichley/foreman/issues/190).

## Acceptance criteria

- After a successful `host.merge_pr(...)` call inside `_handle_attempt_merge`
  (`packages/foreman/src/foreman/reconciler/actions.py:168-175`), the handler
  also calls `host.remove_label(label=_MERGING_LABEL_FOR_TARGET[target])`
  on the focal issue. The lookup reuses the existing
  `_MERGING_LABEL_FOR_TARGET` map at `actions.py:119-122`; no new map or
  constant is introduced.
- `ATTEMPT_MERGE_PLAN` against a `CLEAN` PR results in EXACTLY these host
  calls in this order: `get_pr_mergeability`, `merge_pr`,
  `remove_label(label="foreman:merging-plan")`. `ATTEMPT_MERGE_IMPL`
  symmetrically results in: `get_pr_mergeability`, `merge_pr`,
  `remove_label(label="foreman:merging-impl")`.
- Non-CLEAN branches (`BEHIND`, `BLOCKED+pending`, `BLOCKED+failing`,
  `UNSTABLE`, `DIRTY`, `UNKNOWN`, `DRAFT`, `HAS_HOOKS`, unrecognized)
  do NOT call `host.remove_label` for the `foreman:merging-*` label.
  This preserves the retry-on-next-poll contract for transient
  non-mergeable states.
- If `host.merge_pr` raises, `host.remove_label` is NOT reached
  (control returns through the `execute_action` exception path that
  writes outcome=`error`). The `foreman:merging-*` label stays on the
  issue so the next poll re-enters `_handle_attempt_merge` and retries.
  This is identical to today's behavior on the merge-failure path.
- New unit test
  `test_attempt_merge_plan_clean_removes_merging_plan_label_after_merge`
  in `packages/foreman/tests/reconciler/test_actions.py`:
  starts an `ActionContext` whose `issue.labels` includes
  `"foreman:merging-plan"`, runs `Action.ATTEMPT_MERGE_PLAN` against the
  shared `_attempt_merge_ctx` helper with `dispatch_mergeability_return=CLEAN`,
  and asserts the `_FakeHost.calls` list contains a
  `("remove_label", {"label": "foreman:merging-plan", ...})` entry
  positioned AFTER the `merge_pr` entry. Order matters because if
  `merge_pr` fails the cleanup must not run.
- New unit test
  `test_attempt_merge_impl_clean_removes_merging_impl_label_after_merge`
  (impl-side mirror, same shape, asserting
  `"foreman:merging-impl"` and `head_ref="foreman/impl-143"`).
- New unit test
  `test_attempt_merge_plan_non_clean_paths_do_not_remove_merging_label`:
  parametrize across `("BEHIND", "BLOCKED" with pending=2, "BLOCKED" with
  failing=1, "UNSTABLE", "DIRTY", "UNKNOWN")`; for each, assert that no
  `("remove_label", {"label": "foreman:merging-plan", ...})` entry is
  present in `_FakeHost.calls`. Closes the regression gap explicitly so
  a future refactor that "always removes the label" is caught.
- New unit test
  `test_attempt_merge_plan_clean_does_not_remove_label_when_merge_pr_raises`:
  configure `_FakeHost.merge_pr` to raise (subclass of `_FakeHost`,
  override `merge_pr` to raise `RuntimeError("boom")`); assert
  `_FakeHost.calls` contains the failed `merge_pr` attempt but NO
  `remove_label` for `foreman:merging-plan`. Mirrors the existing
  `test_execute_action_handles_host_exception_and_logs_error` pattern.
- The existing tests
  `test_attempt_merge_plan_clean_calls_host_merge_pr` (test_actions.py:583)
  and `test_attempt_merge_impl_clean_calls_host_merge_pr_on_impl_pr`
  (test_actions.py:781) are updated to additionally assert the
  `remove_label` call is present after the existing `merge_pr` assertion.
  Their existing assertions (e.g., `assert "update_branch" not in
  call_names`) remain valid because removing a label is a labeling
  operation, not a branch-update.
- New end-to-end test
  `test_tick_full_lifecycle_does_not_stall_on_stale_merging_label`
  in `packages/foreman/tests/reconciler/test_reconciler_e2e.py`:
  drives the stub GH client + `_StubHost` through the sequence
  `plan-approved` → `merging-plan + merge_pr + remove_label` →
  (snapshot updated: spec PR merged, impl PR present, labels=
  `[impl-approved]`) → `merging-impl` → `merge_impl + remove_label` →
  `done`. Asserts that at NO tick does the host observe a label set
  that contains both `merging-plan` and `impl-approved` simultaneously
  (regression for issue #190's exact stuck-state shape). The test
  swaps the `_StubGHClient.response` between ticks via a helper to
  simulate the observer re-reading the GH state.
- The `_StubHost` in `test_reconciler_e2e.py` (`test_reconciler_e2e.py:27-45`)
  already records `remove_label` calls; no fixture change required.
- `just check` passes (lint + typecheck + tests).

## Approach

The bug is structural: foreman#165's spec explicitly documented (in its
"Why no in-action label cleanup" section and its "Out of scope" bullet
on "Cleaning up the stale `foreman:merging-plan` / `foreman:merging-impl`
labels after successful merge") an assertion that the stale label is
"inert (no downstream rule keys off its presence beyond the one rule
that originally added it)." That assertion is wrong. Two existing
predicates DO key off the absence of `merging-*` labels:

- `_advance_label_to_merging_plan_when_eligible`
  (`rules.py:280-301`) requires both `"foreman:merging-plan" not in labels`
  AND `"foreman:merging-impl" not in labels`. If `merging-plan` is
  stale from a prior phase, this rule cannot re-fire — which matters
  if the issue ever re-enters `plan-approved` (e.g., spec rewrite,
  reopened ticket).
- `_advance_label_to_merging_impl_when_eligible`
  (`rules.py:400-420`) requires the same exclusions. After a spec PR
  merges, `merging-plan` stays stale; when the Worker subsequently
  produces an impl PR and the Reviewer approves it, this rule's
  predicate sees `{impl-approved, merging-plan}` and returns False.
  The rule never fires. No safety rule preempts. The ticket parks
  silently — exactly the symptom dogfood caught on issues #138, #139,
  #170, #187.

The minimum fix is to remove the `merging-*` label as part of the same
action that did the merge. The `_handle_attempt_merge` helper already
has a per-target dispatch (`_MERGING_LABEL_FOR_TARGET[target]`) used
by the needs-help surfacing path, so the change is a 7-line addition
inside the `CLEAN` branch.

### Why the cleanup goes in the CLEAN branch, not the rule predicate

An alternative would be to relax the `merging-*` exclusion in the two
`_advance_label_to_merging_*_when_eligible` predicates so a stale label
doesn't block them. That fixes the bug at the predicate layer but
leaves the issue carrying inconsistent labels indefinitely (`foreman ps`
output is misleading; operators have to know the labels are stale). The
fix-at-source approach also keeps the merging-* labels true to their
documented meaning ("pipeline is currently attempting to merge"). After
the merge succeeds, the pipeline is no longer attempting — so the label
should come off.

### Why no defense-in-depth lagging cleanup rule

A "lagging cleanup" rule that detects `(PR merged) AND (merging-* label
still present)` would defend against two edge cases:

1. `host.merge_pr` succeeds but `host.remove_label` raises (network
   blip between two GH API calls).
2. A human merges the PR externally while the ticket is in
   `merging-*` state.

For case 1: the executor's exception path writes outcome=`error`, the
operator sees the error in `foreman ps`, and a manual retry plus
operator-side label sweep resolves it. The frequency is low; the
existing operator-escalation path catches it.

For case 2: human-driven merges during `merging-*` are unusual. The
existing `_spec_pr_merged_label_lagging` and `_impl_pr_merged_label_lagging`
rules already handle the post-merge label-state transitions for their
respective phases; only the new `merging-*` label is missed. A
follow-up issue can add the lagging cleanup if dogfood surfaces the
human-external-merge edge case in practice. The issue body explicitly
constrains scope to "the fix should make new tickets immune" — the
action-handler change accomplishes that for the autonomous path.

### Why this is not a label-state-machine redesign

The issue body's "Out of scope" section is explicit: "Adding a new
`foreman:plan-merged` label (decision for the design conversation).
The minimal fix is just 'remove the merging-* label after successful
merge' — anything richer is scope creep." This spec honors that
constraint. No new labels, no new rules, no new actions, no new
predicates.

### Existing tests as the safety net

`test_attempt_merge_plan_behind_calls_host_update_branch_and_does_not_merge`
(test_actions.py:613-639) and the other non-CLEAN branch tests already
exercise the BEHIND / BLOCKED / UNSTABLE / DIRTY / UNKNOWN paths and
assert specific host-call shapes. The new
`test_attempt_merge_plan_non_clean_paths_do_not_remove_merging_label`
test adds an explicit negative assertion to those paths so a future
refactor that erroneously moves the `remove_label` outside the `CLEAN`
branch is caught.

## Sub-requests (topologically sorted)

1. In `packages/foreman/src/foreman/reconciler/actions.py`, modify
   the `CLEAN` branch of `_handle_attempt_merge` (currently
   `actions.py:168-175`) to call
   `host.remove_label(owner=ctx.snapshot.owner, repo=ctx.snapshot.repo,
   issue=ctx.issue.number, label=_MERGING_LABEL_FOR_TARGET[target])`
   AFTER the existing `host.merge_pr(...)` call and BEFORE the early
   `return`. Add a short inline comment referencing issue #190 and
   pointing at the two downstream predicates the cleanup unblocks.
2. In `packages/foreman/tests/reconciler/test_actions.py`, update
   `test_attempt_merge_plan_clean_calls_host_merge_pr`
   (test_actions.py:583) to also assert
   `("remove_label", {..., "label": "foreman:merging-plan"})` is
   present in `host.calls` and appears AFTER the `merge_pr` entry
   (use `host.calls.index(...)` comparisons).
3. In the same file, update
   `test_attempt_merge_impl_clean_calls_host_merge_pr_on_impl_pr`
   (test_actions.py:781) symmetrically for
   `"foreman:merging-impl"`.
4. In the same file, add new test
   `test_attempt_merge_plan_clean_removes_merging_plan_label_after_merge`:
   builds an `ActionContext` whose `issue.labels` includes
   `"foreman:merging-plan"` (via a small `_with_label` helper or by
   constructing a fresh `IssueState`); runs the action; asserts the
   precise `("remove_label", {"owner": "jeffrichley", "repo":
   "foreman", "issue": 143, "label": "foreman:merging-plan"})`
   call entry is present.
5. In the same file, add new test
   `test_attempt_merge_impl_clean_removes_merging_impl_label_after_merge`
   (impl-side mirror; `head_ref="foreman/impl-143"`, `pr_number=200`,
   asserts `"foreman:merging-impl"` removal).
6. In the same file, add new parametrized test
   `test_attempt_merge_plan_non_clean_paths_do_not_remove_merging_label`
   with `@pytest.mark.parametrize` over the six non-CLEAN states
   (`BEHIND`, `BLOCKED`+pending=2, `BLOCKED`+failing=1, `UNSTABLE`,
   `DIRTY`, `UNKNOWN`); each parametrization configures
   `_FakeHost.dispatch_mergeability_return` accordingly and asserts
   `not any(c[0] == "remove_label" and c[1]["label"] ==
   "foreman:merging-plan" for c in host.calls)`.
7. In the same file, add new test
   `test_attempt_merge_plan_clean_does_not_remove_label_when_merge_pr_raises`:
   subclass `_FakeHost` with an overridden `merge_pr` that raises
   `RuntimeError("boom")`; run the action; assert no `remove_label`
   for `foreman:merging-plan` in `host.calls`; assert the executor
   wrote an `error` termination row (mirrors
   `test_execute_action_handles_host_exception_and_logs_error`).
8. In `packages/foreman/tests/reconciler/test_reconciler_e2e.py`,
   add new test
   `test_tick_full_lifecycle_does_not_stall_on_stale_merging_label`:
   drives the reconciler through three ticks with the `_StubGHClient.response`
   swapped between ticks to simulate observer state evolution. Asserts
   that after the spec-merge tick, the host called
   `remove_label(label="foreman:merging-plan")`; that the third tick
   (with `[impl-approved]` and an open impl PR) emits
   `ADVANCE_LABEL_TO_MERGING_IMPL` (i.e., the rule fires — the bug
   fixed). Uses `_pr_payload(..., head_ref="foreman/impl-143")` for
   the impl PR.
9. Run `just check`. Resolve any lint / typecheck drift (no new
   imports expected — `_MERGING_LABEL_FOR_TARGET` is already at module
   scope; `host.remove_label` is already in the host Protocol per
   the existing `ADVANCE_LABEL_TO_PLANNING` handler at
   `actions.py:400-405`).

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/src/foreman/reconciler/actions.py` | Add `host.remove_label(label=_MERGING_LABEL_FOR_TARGET[target])` inside the `CLEAN` branch of `_handle_attempt_merge`, after `host.merge_pr`, before the early return. Short inline comment referencing #190. |
| `packages/foreman/tests/reconciler/test_actions.py` | Update two existing CLEAN-branch tests to assert the new `remove_label` call + ordering; add four new tests (CLEAN-removes-plan, CLEAN-removes-impl, non-CLEAN-does-not-remove, raises-does-not-remove). |
| `packages/foreman/tests/reconciler/test_reconciler_e2e.py` | Add one new full-lifecycle test that exercises the plan-merge → impl-approved transition end-to-end and asserts no stuck-state. |

No changes are required to `rules.py` (the existing predicates work
correctly once the stale label is gone), `host.py` (the `remove_label`
method is already in the Protocol), `v3_host.py` (the implementation
is already there — used by `ADVANCE_LABEL_TO_PLANNING`,
`ADVANCE_LABEL_TO_PLAN_APPROVED`, `ADVANCE_LABEL_TO_DONE`),
`init.py` (no label catalog change), `observer.py` (no filter
change — the labels still need to be surfaced in the snapshot for
the rule-evaluation loop), or `daemon_runners.py` (v1 / v2 legacy
code path, not exercised by the v3 reconciler bug).

## Alternatives considered

- **Relax the `merging-*` exclusion in the two
  `_advance_label_to_merging_*_when_eligible` predicates.** Rejected:
  fixes the immediate stall but leaves the issue with stale labels
  indefinitely, misleading operators reading `foreman ps`. The labels
  are documented as "currently attempting to merge"; honoring that
  contract requires removal once the merge completes.
- **Add a separate `_merging_label_stale_after_merge` lagging rule
  that fires whenever a merged PR's issue still carries `merging-*`.**
  Rejected for scope: the issue body constrains the fix to "the
  minimum needed" and explicitly excludes the design-conversation-level
  alternatives. The lagging rule could be added later if
  `host.remove_label` reliability becomes a problem; the in-handler
  cleanup catches the autonomous-path bug at its source.
- **Add a new `foreman:plan-merged` / `foreman:impl-merged` phase
  label that explicitly represents "merge succeeded, phase
  transitioning."** Rejected: the issue body's "Out of scope"
  section names this exact alternative and rejects it as scope
  creep. The existing label vocabulary plus a clean removal is
  sufficient.
- **Do nothing in the action handler; rely on operators to sweep
  stale labels manually.** Rejected: dogfood has already shown this
  is the status quo and it does not scale — four tickets stuck in
  under 12 hours of multi-ticket runs. The cap-1 concurrency
  effectively amplifies any single stall into a full-queue halt.

## Open questions

None. The fix surface, test surface, and risk envelope are all
small and well-bounded. The "no defense-in-depth lagging rule"
decision is documented in the Approach section so a future
reviewer who disagrees has the reasoning to push back against.

## Out of scope

- Cleanup of the four currently-stuck tickets (#138, #139, #170,
  #187). The issue body explicitly carves this out — operator sweep
  or a one-off Wren run handles it.
- Adding a new `foreman:plan-merged` / `foreman:impl-merged` label
  for richer post-merge state semantics. Issue body Out-of-scope.
- Adding a `_merging_label_stale_after_merge` lagging cleanup rule
  for the human-external-merge or `remove_label`-fails edge cases.
  Defer to a follow-up issue if either edge case surfaces in dogfood.
- Refactoring `_handle_attempt_merge` into per-target subclasses or
  a Strategy pattern. The existing one-helper-with-`target` shape
  matches foreman#165's design and the cleanup change is two extra
  lines; restructuring would be over-engineering.
- Changing the `_MERGING_LABEL_FOR_TARGET` map's location or shape.
  It already lives at module scope (actions.py:119-122) for the
  `_surface_attempt_merge_needs_help` consumer; the new caller reuses
  it as-is.
- Adding GH API rate-limit guarding for the extra `remove_label`
  call. The handler already issues at least one host call
  (`merge_pr`) and the executor's logging captures rate-limit
  errors via the existing `error` outcome path; the marginal cost
  of one additional `remove_label` per merge is negligible.
