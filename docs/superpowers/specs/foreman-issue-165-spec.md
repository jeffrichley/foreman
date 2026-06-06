# Spec: add merging-plan/merging-impl states + `attempt_merge` action (issue #165)

## Goal

Replace the current "merge once, fail if CI is not green at that exact
instant" path with a state-machine-driven merge phase. Today
`MERGE_SPEC_PR` / `MERGE_IMPL_PR` actions call `host.merge_pr` directly
and rely on the strict `MERGEABLE + SUCCESS` predicate; the moment CI
flips green between polls, the next poll merges, but back-to-back PRs in
the same repo stall because each merge must arrive in a single "all gates
green at this exact instant" window.

This spec introduces:

- Two new issue labels — `foreman:merging-plan`, `foreman:merging-impl`
  — that represent "pipeline is attempting to merge the spec / impl PR."
- A new pair of actions, `ATTEMPT_MERGE_PLAN` / `ATTEMPT_MERGE_IMPL`,
  that drive GitHub's per-PR `mergeStateStatus` machine
  (`CLEAN` / `BEHIND` / `BLOCKED` / `UNSTABLE` / `DIRTY` / `UNKNOWN`)
  inside the action. Behind → server-side rebase. Blocked +
  running → wait. Blocked + failure → needs-help. Clean → merge.
- Two new host methods, `get_pr_mergeability` and `update_branch`,
  that read the live PR state and call GitHub's
  `updatePullRequestBranch` GraphQL mutation.

Tracks [#165](https://github.com/jeffrichley/foreman/issues/165). Related:
foreman#161 (the `merge_mechanism` config knob is preserved and continues
to gate direct vs. queue), foreman#163 (queue plumbing remains dormant),
foreman v3 rescue Stage 2 target-split pattern
(`DISPATCH_REVIEWER_SPEC` / `DISPATCH_REVIEWER_IMPL`).

## Acceptance criteria

1. **Two new labels added to the canonical catalog in
   `packages/foreman/src/foreman/init.py`**:
   - `foreman:merging-plan` — color `FBCA04`, description
     `"Foreman: attempting to merge the spec PR"`.
   - `foreman:merging-impl` — color `FBCA04`, description
     `"Foreman: attempting to merge the impl PR"`.
   - Inserted into `_FOREMAN_LABELS` in the natural pipeline order:
     `merging-plan` between `plan-approved` and `spec-fix`;
     `merging-impl` between `impl-approved` and `impl-fix`.
   - `foreman init` on a fresh repo creates both labels (idempotent;
     existing-labels behavior unchanged).
2. **`packages/foreman/tests/test_init.py::test_run_init_creates_all_v3_labels_on_empty_repo`
   updated** to assert the new vocabulary set includes
   `foreman:merging-plan` and `foreman:merging-impl`. The total label
   count assertion (`len(result.labels_created) == len(expected_names)`)
   continues to pass because both arms read from
   `_FOREMAN_LABELS`.
3. **Observer GraphQL filter updated** in
   `packages/foreman/src/foreman/reconciler/observer.py::_QUERY` to add
   `"foreman:merging-plan"` and `"foreman:merging-impl"` to the
   `filterBy.labels` list so issues parked in those states are still
   surfaced in the poll snapshot. No change to the PR fragment.
4. **Four new `Action` enum values added** in
   `packages/foreman/src/foreman/reconciler/actions.py::Action`:
   - `ADVANCE_LABEL_TO_MERGING_PLAN = "advance_label_to_merging_plan"`
   - `ADVANCE_LABEL_TO_MERGING_IMPL = "advance_label_to_merging_impl"`
   - `ATTEMPT_MERGE_PLAN = "attempt_merge_plan"`
   - `ATTEMPT_MERGE_IMPL = "attempt_merge_impl"`
   - The existing `MERGE_SPEC_PR` / `MERGE_IMPL_PR` enum values are
     **removed**: in the new flow the actual `host.merge_pr` call lives
     inside the `ATTEMPT_MERGE_*` handler when the PR's `mergeStateStatus`
     is `CLEAN`. The `_DISPATCH_ROLE_FOR_ACTION` map is unchanged
     (none of the new actions dispatch a role).
   - `test_action_enum_covers_spec_catalog` in
     `packages/foreman/tests/reconciler/test_actions.py` updated to
     match the new set.
5. **Two new methods added to the `ReconcilerHost` Protocol** in
   `packages/foreman/src/foreman/reconciler/host.py`:
   - `get_pr_mergeability(*, owner: str, repo: str, pr_number: int) -> PRMergeability`
     where `PRMergeability` is a new frozen dataclass living next to the
     Protocol with fields:
     - `state: str` — one of GitHub's `mergeStateStatus` enum values
       (`CLEAN`, `BEHIND`, `BLOCKED`, `UNSTABLE`, `DIRTY`, `UNKNOWN`,
       `DRAFT`, `HAS_HOOKS`). Returned uppercase as GitHub sends it.
     - `failing_required_check_count: int` — count of required
       check runs whose `conclusion` is in
       `{FAILURE, CANCELLED, TIMED_OUT, ACTION_REQUIRED, STALE}`.
     - `pending_required_check_count: int` — count of required
       check runs whose `conclusion` is `None` (still running) or
       whose `status` is not `COMPLETED`.
   - `update_branch(*, owner: str, repo: str, pr_number: int) -> None`
     — calls GitHub's `updatePullRequestBranch` GraphQL mutation
     (server-side rebase onto the base branch's head). Raises on
     GitHub errors so the executor's catch path can record an
     `error` termination row; the next poll re-evaluates.
6. **`V3GitHubHost` implements both new methods** in
   `packages/foreman/src/foreman/reconciler/v3_host.py`:
   - `get_pr_mergeability` issues one GraphQL query via the existing
     `_gh_queue_client` (the same authenticated GraphQL client already
     used by `_enqueue_pull_request`). Query body added at module
     scope, mirroring the `_PR_NODE_ID_QUERY` / `_ENQUEUE_PR_MUTATION`
     hoisting. The query MUST include `mergeStateStatus`,
     `commits(last: 1).nodes.commit.statusCheckRollup.contexts.nodes`
     (with the `CheckRun` inline fragment) and the inline
     `isRequired` field so the host can compute failing /
     pending required-check counts without a second round trip.
   - `update_branch` issues one GraphQL mutation
     (`updatePullRequestBranch`) using the same client. Mutation
     body added at module scope. Two-step shape (resolve PR node
     ID → mutation) matching `_enqueue_pull_request`.
   - When `_gh_queue_client` is `None` (test default for fixtures
     that never exercised queue or mergeability paths), both
     methods raise `RuntimeError` with the same shape as today's
     `merge_pr(mechanism="queue")` raise — a clear message naming
     the missing constructor kwarg.
7. **Four new rules added to
   `packages/foreman/src/foreman/reconciler/rules.py`** in the
   `_PROGRESS_RULES` tier, replacing today's `merge_spec_pr` and
   `merge_impl_pr` rules:
   - `advance_label_to_merging_plan` (precedence 115, replaces
     `merge_spec_pr`):
     - Predicate: issue has `foreman:plan-approved`, has neither
       `foreman:merging-plan` nor `foreman:merging-impl`, `ctx.pr`
       is non-`None`, not merged, head-ref starts with
       `foreman/issue-`, AND `ctx.auto_merge_spec` is True.
     - Action: `ADVANCE_LABEL_TO_MERGING_PLAN`.
   - `attempt_merge_plan` (precedence 118, NEW):
     - Predicate: issue has `foreman:merging-plan`, `ctx.pr` is
       non-`None`, not merged, head-ref starts with `foreman/issue-`.
     - Action: `ATTEMPT_MERGE_PLAN`.
   - `advance_label_to_merging_impl` (precedence 158, replaces
     `merge_impl_pr`):
     - Predicate: issue has `foreman:impl-approved`, has neither
       `foreman:merging-plan` nor `foreman:merging-impl`, `ctx.pr`
       is non-`None`, not merged, head-ref starts with
       `foreman/impl-`, AND `ctx.auto_merge_impl` is True.
     - Action: `ADVANCE_LABEL_TO_MERGING_IMPL`.
   - `attempt_merge_impl` (precedence 162, NEW):
     - Predicate: issue has `foreman:merging-impl`, `ctx.pr` is
       non-`None`, not merged, head-ref starts with `foreman/impl-`.
     - Action: `ATTEMPT_MERGE_IMPL`.
   - The existing `_plan_approved_pr_green_and_flag` /
     `_impl_approved_pr_green_and_flag` predicates and their two
     `Rule(...)` registrations are **deleted**. Their head-ref
     filter rationale (HIGH #11, the stacked-PR window) is
     preserved verbatim in the new predicates' docstrings.
   - The existing `_spec_pr_merged_label_lagging` and
     `_impl_pr_merged_label_lagging` rules are **unchanged**. They
     still fire when the PR is merged externally (human merges
     ahead of the daemon) or after a successful `ATTEMPT_MERGE_*`
     completes its `host.merge_pr` step — the lagging-label rule
     handles "PR merged + label not yet advanced" regardless of who
     merged the PR.
8. **`execute_action` in `actions.py` gains handlers for the four new
   action values**:
   - `ADVANCE_LABEL_TO_MERGING_PLAN` calls `host.add_label(label="foreman:merging-plan")`
     on the focal issue. Synchronous; terminates with `success`.
   - `ADVANCE_LABEL_TO_MERGING_IMPL` calls `host.add_label(label="foreman:merging-impl")`
     on the focal issue. Synchronous; terminates with `success`.
   - `ATTEMPT_MERGE_PLAN` calls a shared helper
     `_handle_attempt_merge(ctx, host, target="plan")`. The helper:
     1. Reads `host.get_pr_mergeability(owner, repo, pr_number)`.
     2. Switches on the returned `state`:
        - `CLEAN`: calls `host.merge_pr(..., mechanism=ctx.merge_mechanism)`.
          On return, the issue label catalog still carries
          `foreman:plan-approved` (added by Reviewer earlier) AND
          `foreman:merging-plan` (added by step 7's rule). The
          existing `dispatch_worker` rule then fires next tick
          because its predicate only requires `foreman:plan-approved`
          — the stale `foreman:merging-plan` label is inert (the
          `attempt_merge_plan` rule no longer fires because the
          spec PR is merged → `pr.is_merged` is True → predicate
          False). Operators reading `foreman ps` will see the
          stale label until the next polled merge of any spec PR
          touches the issue; cleanup is out of scope (anti-pattern
          guard: no cleanup-by-wallclock rule).
        - `BEHIND`: calls `host.update_branch(...)`. GitHub
          recomputes `mergeStateStatus` server-side; the next poll
          re-enters the rule and re-reads mergeability.
        - `BLOCKED` AND `pending_required_check_count > 0`: no-op.
          Required CI is still running; the next poll re-enters
          the rule and re-reads mergeability.
        - `BLOCKED` AND `pending_required_check_count == 0`
          (implies at least one required check has terminated
          non-`SUCCESS`): calls
          `host.add_label(label="foreman:needs-help")` and
          `host.post_comment(...)` with the same surfacing copy as
          the existing `Action.SURFACE_HELP` handler.
        - `UNSTABLE`: same needs-help branch as failing-BLOCKED.
          Conservative default per the issue body's "anti-patterns
          to watch" note.
        - `DIRTY`: same needs-help branch.
        - `UNKNOWN`: no-op. The next poll re-evaluates; GitHub
          will have computed the state by then.
        - `DRAFT` or `HAS_HOOKS`: no-op + log at INFO. These
          states should not occur on foreman PRs (no Draft PRs,
          no branch-protection hooks the bot can't pass) but
          documenting the no-op makes them explicit rather than
          AttributeError-raising falls through to needs-help.
   - `ATTEMPT_MERGE_IMPL` calls `_handle_attempt_merge(ctx, host, target="impl")`.
     Identical body; the `target` parameter is used only to scope
     the head-ref expectation and log attribution.
   - All four new handlers respect the existing `dry_run`
     short-circuit path at the top of `execute_action` (write a
     single `dry_run` outcome row, skip host calls).
   - All four new handlers respect the existing exception path
     (write `error` outcome row with the exception message; do not
     re-raise; one bad action must not crash the reconciler loop).
9. **Tests added to
   `packages/foreman/tests/reconciler/test_actions.py`** — one test
   per mergeable-state branch in `_handle_attempt_merge`:
   - `test_attempt_merge_plan_clean_calls_host_merge_pr`
   - `test_attempt_merge_plan_behind_calls_host_update_branch_and_does_not_merge`
   - `test_attempt_merge_plan_blocked_with_pending_required_checks_is_noop`
   - `test_attempt_merge_plan_blocked_with_failing_required_check_surfaces_needs_help`
   - `test_attempt_merge_plan_unstable_surfaces_needs_help`
   - `test_attempt_merge_plan_dirty_surfaces_needs_help`
   - `test_attempt_merge_plan_unknown_is_noop`
   - `test_attempt_merge_impl_clean_calls_host_merge_pr_on_impl_pr` —
     symmetric test that exercises `target="impl"` and asserts the
     `pr_number` passed to `host.merge_pr` is the impl PR's number,
     not the spec PR's. (One target-mirror test is enough; the
     remaining branches behave identically per the shared helper.)
   - `test_attempt_merge_plan_dry_run_skips_host` — dry-run path
     writes a single `dry_run` row and makes no host calls.
   - The existing `_FakeHost` in `test_actions.py` is extended
     with recording methods for `get_pr_mergeability(...)` and
     `update_branch(...)`. The fake's `get_pr_mergeability` returns
     a configurable `PRMergeability` (set per-test via a
     `dispatch_mergeability_return` field, mirroring today's
     `dispatch_role_return` configurability).
10. **Tests added to
    `packages/foreman/tests/reconciler/test_rules.py`** for the four
    new rule predicates:
    - `test_advance_label_to_merging_plan_fires_on_plan_approved_with_open_spec_pr_and_auto_merge_spec`
    - `test_advance_label_to_merging_plan_skipped_when_auto_merge_spec_false`
    - `test_advance_label_to_merging_plan_skipped_when_merging_label_already_present`
    - `test_attempt_merge_plan_fires_on_merging_plan_label_with_open_spec_pr`
    - `test_attempt_merge_plan_skipped_on_merged_spec_pr` (so the
      action doesn't re-fire after merge)
    - Symmetric four tests for the impl side.
11. **Tests added to
    `packages/foreman/tests/reconciler/test_v3_host.py`**:
    - `test_get_pr_mergeability_returns_state_from_graphql` — fake
      GraphQL client returns a synthetic `mergeStateStatus`
      response; assert the dataclass shape.
    - `test_get_pr_mergeability_computes_check_counts_from_rollup`
      — multi-check response; assert the failing / pending counts
      are computed correctly with `isRequired` filtering.
    - `test_update_branch_issues_graphql_mutation` — assert the
      mutation name + variables match GitHub's expected shape.
    - `test_get_pr_mergeability_without_client_raises_clear_error`
      — symmetric to today's
      `test_merge_pr_queue_without_client_raises_clear_error`.
    - `test_update_branch_without_client_raises_clear_error` —
      same shape.
12. **Tests added to
    `packages/foreman/tests/test_init.py`**:
    - The catalog set assertion in
      `test_run_init_creates_all_v3_labels_on_empty_repo` includes
      `"foreman:merging-plan"` and `"foreman:merging-impl"`.
13. **`just check` passes** (lint + typecheck + tests). New imports
    follow the existing patterns; no new dependencies.

## Approach

The bug surfaces a structural mismatch between how Foreman models a
merge and how GitHub actually computes mergeability. Foreman's current
model is one-shot: predicate fires when `MERGEABLE + SUCCESS`, action
calls `pr.merge()`, success or fail. GitHub's model is a small state
machine the API exposes via `mergeStateStatus`. The fix is to bring
Foreman's model in line — make "merging" a first-class phase, give it a
label so `foreman ps` shows it, and put the state-machine inside an
action that the reconciler re-enters every poll until the PR merges.

### Three things the issue body asks for

The issue body specifies:

1. **New labels** for visibility (`merging-plan`, `merging-impl`).
2. **A new action** that drives the `mergeStateStatus` machine.
3. **Two new host methods** (`get_pr_mergeability`, `update_branch`)
   to support the action.

The spec honors all three precisely. The label set, the state
mapping table, and the per-state handler decisions are taken directly
from the issue's mermaid diagram and "Per-state behavior" table.

### Why two `Action` enum values, not one

The issue says
`"attempt_merge(target: Literal['plan', 'impl']) — single action, target-aware"`.
Read literally as "one enum value with a parameter", that would conflict
with the foreman v3 rescue Stage 2 pattern documented in
`packages/foreman/src/foreman/reconciler/actions.py:32-46` —
`DISPATCH_REVIEWER_SPEC` / `DISPATCH_REVIEWER_IMPL` were split precisely
because `count_completed` idempotence gates need per-target action keys
in the execution log. `ATTEMPT_MERGE_PLAN` / `ATTEMPT_MERGE_IMPL` follow
the same shape: two enum values for log attribution + idempotence
counting, ONE shared helper function `_handle_attempt_merge(ctx, host,
target)` for the body. This preserves the issue's intent ("single,
target-aware") at the implementation layer while honoring the
documented project convention at the enum layer.

### Why a new host READ method (not the observer)

Today every GitHub read goes through the observer's bulk GraphQL query.
Adding `get_pr_mergeability` as a host method is a small departure: the
host now has READ methods, not just mutations. The departure is
justified by GitHub's lazy `mergeStateStatus` computation — the value
returned by a bulk PR query is often `UNKNOWN` because GitHub only
computes it on direct PR-lookup. Reading it at action-execution time
(when we KNOW we're about to act on this PR) forces the computation and
gives us the live state, not the snapshot from the start of the tick.
The existing `_enqueue_pull_request` method already issues GraphQL
reads (the node-ID lookup before the enqueue mutation), so the
precedent exists.

### Why no in-action label cleanup

When `ATTEMPT_MERGE_PLAN` succeeds (state=`CLEAN` → `host.merge_pr`
returns), the PR is merged and the `attempt_merge_plan` rule stops
firing because `ctx.pr.is_merged` becomes `True`. The
`foreman:merging-plan` label sits stale on the issue until a future
operator action clears it, but it is inert: the `dispatch_worker` rule
fires on `foreman:plan-approved` and does NOT exclude
`merging-plan`-bearing tickets. Adding an in-action label-removal step
adds a failure mode (what if the merge succeeded but the label-remove
HTTP call failed?) and adds a second host mutation per merge.
The simpler design — let the stale label sit until eventual cleanup —
matches the existing pattern (the `advance_label_to_plan_approved_lagging`
rule already handles "label out of sync with PR state" defensively).

### Why preserve `merge_mechanism` (and the queue plumbing)

The issue body is explicit: "The `merge_mechanism` config knob from
#161 stays. Default remains 'direct' which now means 'use attempt_merge.'"
That is the right call. When `_handle_attempt_merge` reaches the
`CLEAN` branch and calls `host.merge_pr(..., mechanism=ctx.merge_mechanism)`,
direct passes through to today's `pr.merge()` while queue routes
through the dormant `_enqueue_pull_request` path. Neither change here.
The new state machine sits BEFORE the merge call, not instead of it.

### Out-of-band PR closure remains handled by existing lagging rules

If a human closes the spec PR without merging while the issue is in
`merging-plan`, `ctx.pr` flips to `None` (or
`is_merged=False, state=CLOSED` in some snapshots). The
`attempt_merge_plan` rule's predicate requires `ctx.pr is not None` and
`not ctx.pr.is_merged`, so it stops firing. The existing
`needs_help_label` safety rule (precedence 10) catches anything that
escalates; the existing `_spec_pr_merged_label_lagging` rule catches
"PR merged externally + label not yet advanced." No new rules needed
for these paths.

## Sub-requests (topologically sorted)

1. In `packages/foreman/src/foreman/init.py`, add the two new label
   tuples to `_FOREMAN_LABELS` in the natural pipeline positions
   described in acceptance criterion #1.
2. In `packages/foreman/tests/test_init.py`, update
   `test_run_init_creates_all_v3_labels_on_empty_repo`'s expected
   vocabulary set to include the two new labels.
3. In `packages/foreman/src/foreman/reconciler/observer.py`, add
   `"foreman:merging-plan"` and `"foreman:merging-impl"` to the
   `filterBy.labels` list in `_QUERY`.
4. In `packages/foreman/src/foreman/reconciler/host.py`, define the
   new `PRMergeability` frozen dataclass and add the two new methods
   (`get_pr_mergeability`, `update_branch`) to the `ReconcilerHost`
   Protocol. Add Protocol docstrings naming the response shape and
   error semantics described in acceptance criterion #5.
5. In `packages/foreman/src/foreman/reconciler/v3_host.py`, hoist
   two new module-level GraphQL bodies (`_PR_MERGEABILITY_QUERY`,
   `_UPDATE_BRANCH_MUTATION`) next to the existing
   `_PR_NODE_ID_QUERY` / `_ENQUEUE_PR_MUTATION`. Implement
   `get_pr_mergeability` and `update_branch` on `V3GitHubHost` using
   the existing `_gh_queue_client`. Both methods raise the same
   shape of `RuntimeError` as today's `_enqueue_pull_request` when
   the client is unset.
6. In `packages/foreman/tests/reconciler/test_v3_host.py`, add the
   five new host tests listed in acceptance criterion #11. Reuse
   the existing `_FakeGraphQLClient` fixture pattern; add a small
   helper `_build_mergeability_response(state, pending=0, failing=0)`
   alongside the existing `_build_queue_responses` helper.
7. In `packages/foreman/src/foreman/reconciler/actions.py`:
   1. Remove `MERGE_SPEC_PR` and `MERGE_IMPL_PR` from the `Action`
      enum.
   2. Add `ADVANCE_LABEL_TO_MERGING_PLAN`, `ADVANCE_LABEL_TO_MERGING_IMPL`,
      `ATTEMPT_MERGE_PLAN`, `ATTEMPT_MERGE_IMPL`.
   3. Define the `_handle_attempt_merge(ctx, host, *, target: Literal["plan", "impl"])`
      helper at module scope.
   4. Update `execute_action` to delete the
      `Action.MERGE_SPEC_PR or Action.MERGE_IMPL_PR` branch and add
      branches for the four new action values, calling the shared
      helper for the two attempt-merge actions and
      `host.add_label` for the two label-advance actions.
8. In `packages/foreman/tests/reconciler/test_actions.py`:
   1. Update `test_action_enum_covers_spec_catalog` to the new set
      (drop `MERGE_SPEC_PR` + `MERGE_IMPL_PR`; add the four new).
   2. Update `_FakeHost` to record `get_pr_mergeability` and
      `update_branch` calls and to return a configurable
      `PRMergeability`.
   3. Add the new tests listed in acceptance criterion #9.
9. In `packages/foreman/src/foreman/reconciler/rules.py`:
   1. Remove `_plan_approved_pr_green_and_flag` and
      `_impl_approved_pr_green_and_flag` predicate functions and
      their two `Rule(...)` entries from `_PROGRESS_RULES`.
   2. Add the four new predicate functions described in acceptance
      criterion #7, preserving the head-ref filter and stacked-PR
      window rationale from the deleted predicates' docstrings.
   3. Add the four new `Rule(...)` entries to `_PROGRESS_RULES` at
      the precedence values listed in acceptance criterion #7.
10. In `packages/foreman/tests/reconciler/test_rules.py`, add the
    new rule-predicate tests listed in acceptance criterion #10.
11. Run `just check` and resolve any drift caught by lint / typecheck.
    Lint / mypy may flag the unused `MergeMechanism` import in
    `actions.py` (still needed — `_handle_attempt_merge` passes it
    through). The `MergeMechanism` import stays.

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/src/foreman/init.py` | Add `foreman:merging-plan` + `foreman:merging-impl` to `_FOREMAN_LABELS` in pipeline order. |
| `packages/foreman/src/foreman/reconciler/observer.py` | Add the two new labels to `_QUERY`'s `filterBy.labels` list. |
| `packages/foreman/src/foreman/reconciler/host.py` | Define `PRMergeability` dataclass; add `get_pr_mergeability` + `update_branch` to the Protocol. |
| `packages/foreman/src/foreman/reconciler/v3_host.py` | Implement both new methods via the existing GraphQL client; hoist two new GraphQL constants at module scope. |
| `packages/foreman/src/foreman/reconciler/actions.py` | Remove `MERGE_SPEC_PR` / `MERGE_IMPL_PR` enum values + handler branch; add four new enum values; add `_handle_attempt_merge` helper; add four new handler branches in `execute_action`. |
| `packages/foreman/src/foreman/reconciler/rules.py` | Remove the two existing `merge_*_pr` predicates + Rules; add four new predicates + Rules in `_PROGRESS_RULES`. |
| `packages/foreman/tests/test_init.py` | Update the v3-vocabulary assertion set. |
| `packages/foreman/tests/reconciler/test_v3_host.py` | Five new host tests (state read, check-count compute, mutation issuance, two error-shape tests). |
| `packages/foreman/tests/reconciler/test_actions.py` | Update `Action` enum coverage test; extend `_FakeHost`; eight new state-branch tests + one impl-target mirror test + one dry-run test. |
| `packages/foreman/tests/reconciler/test_rules.py` | Eight new rule-predicate tests (four per target). |

No changes are required to `packages/foreman/src/foreman/config.py`
(the `merge_mechanism` knob stays as-is), `packages/foreman/src/foreman/reconciler/state.py`
(no new fields on `PRState` — the live `mergeStateStatus` is read
through the host, not piggy-backed onto the snapshot), or
`packages/foreman/src/foreman/reconciler/daemon.py` (the existing
`ActionContext` plumbing already carries `merge_mechanism` and
`pr` through to the executor).

## Alternatives considered

- **Single `Action.ATTEMPT_MERGE` enum value with a `target` parameter
  carried out-of-band.** Rejected: the foreman v3 rescue Stage 2 split
  (`DISPATCH_REVIEWER_SPEC` / `DISPATCH_REVIEWER_IMPL`) was driven by
  exactly this problem — when both target shapes share an action key,
  `count_completed` idempotence gates flip permanently False once any
  target dispatches once (HIGH #7). Two enum values + one helper
  function honors the issue's intent at the implementation layer
  while protecting log attribution and idempotence at the audit-row
  layer.
- **Add `mergeStateStatus` to the observer's bulk PR fragment instead
  of a host READ method.** Rejected: GitHub computes
  `mergeStateStatus` lazily on direct PR lookup. A bulk PR-list query
  often returns `UNKNOWN` for every PR, so the rule would re-trigger
  `ATTEMPT_MERGE` on a stale `UNKNOWN` every tick. Reading it at
  action time forces GitHub to compute the live value and gives the
  state machine an accurate decision input.
- **Remove the `merging-*` label and let `attempt_merge` fire directly
  off `plan-approved` / `impl-approved`.** Rejected: the labels exist
  to give `foreman ps` operator visibility (and the issue body lists
  this as acceptance criterion 4: "`foreman ps` shows pipelines in
  merging-plan / merging-impl with no stalls"). Without the label
  there's no way for an operator to distinguish "approved, waiting
  for Worker" from "approved, attempting merge."
- **Add an in-action label-cleanup step that removes `merging-plan` /
  `merging-impl` after successful merge.** Rejected: adds a second
  host mutation per merge with a new failure mode (merge succeeded,
  label-remove failed → next poll re-fires attempt_merge on a merged
  PR, which `is_merged=True` filters out, but the label is now stale
  AND wrong). The simpler design — let the stale label sit until a
  future operator action clears it — has zero failure modes and the
  label is inert (no downstream rule keys off its presence beyond the
  one rule that originally added it).
- **Add a wallclock-timeout escalation inside `attempt_merge`.**
  Rejected outright by the issue body's "anti-patterns to watch"
  list. Stuck pipelines surface via `foreman ps` visibility, not
  elapsed-time thresholds; adding a timeout encourages operators to
  treat the daemon as autonomous when by design it expects human
  attention for genuinely stuck pipelines.

## Open questions

- The exact GraphQL fragment shape for reading required-check
  conclusions in one query is not perfectly settled. GitHub's
  `statusCheckRollup.contexts` returns a union of `CheckRun` and
  `StatusContext`; `isRequired` is exposed on both. The Worker
  should verify the field set against GitHub's current schema before
  finalizing `_PR_MERGEABILITY_QUERY` and may need a one-line
  schema-probe script (similar to the one used during foreman#158)
  if any field name has drifted. The spec gives the conceptual
  shape; the exact field names are confirmed at implementation time.

## Out of scope

- Adding a wallclock-timeout escalation to `attempt_merge`.
- Removing the dormant queue plumbing from foreman#163. The `queue`
  branch of `host.merge_pr` continues to exist; when
  `_handle_attempt_merge` hits the `CLEAN` branch and
  `ctx.merge_mechanism == "queue"`, the existing queue path runs
  unchanged.
- Changing the Reviewer's label-advance behavior. Reviewer still
  writes `foreman:plan-approved` on a clean spec review and
  `foreman:impl-approved` on a clean impl review; the new merging
  flow is wholly downstream of Reviewer.
- Cleaning up the stale `foreman:merging-plan` / `foreman:merging-impl`
  labels after successful merge. Operators can remove them manually
  if desired; the labels are inert and do not gate downstream rules.
- Refactoring `_handle_attempt_merge` into a shared base class /
  Strategy / per-target subclass. The two enum values + one helper
  function with a `target: Literal["plan", "impl"]` parameter matches
  the existing dispatch-action split pattern; adding more structure
  here would be over-engineering for a single shared body.
- Adding `mergeStateStatus` to `PRState` or the observer. The host
  READ method is the authoritative source for the new flow; adding
  the field redundantly would create two sources of truth for the
  same data.
