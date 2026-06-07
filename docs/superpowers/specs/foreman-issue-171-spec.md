# Spec: add `foreman:plan` → `foreman:planning` auto-transition rule (issue #171)

## Goal

A ticket labeled only `foreman:plan` currently stalls silently — the
daemon's observer never even sees it (the label is absent from
`packages/foreman/src/foreman/reconciler/observer.py::_QUERY`'s
`filterBy.labels` list), so no rule fires and no surface-help comment
appears. This spec restores `foreman:plan` as the documented "queue for
planning" entry label and adds a forward-progress rule that transitions
`foreman:plan` → `foreman:planning` on truly fresh tickets, after which
the existing `dispatch_planner` rule fires on the next poll. Closes the
visibility gap reported in issue #171.

## Acceptance criteria

- A new `Action.ADVANCE_LABEL_TO_PLANNING` enum value exists in
  `packages/foreman/src/foreman/reconciler/actions.py`.
- `execute_action` handles the new action by calling
  `host.remove_label(..., "foreman:plan")` then
  `host.add_label(..., "foreman:planning")`. Termination row is
  `outcome="success"` on the synchronous path (mirrors the existing
  `ADVANCE_LABEL_TO_PLAN_APPROVED` handler at lines 387–399).
- A new rule named `advance_label_to_planning` is registered in
  `packages/foreman/src/foreman/reconciler/rules.py` in the
  `FORWARD_PROGRESS` tier at precedence `95` (immediately before
  `dispatch_planner` at 100). The predicate `_plan_label_only` fires iff
  `"foreman:plan" in ctx.issue.labels` AND none of these labels are
  present: `foreman:planning`, `foreman:plan-approved`,
  `foreman:merging-plan`, `foreman:spec-fix`, `foreman:impl-review`,
  `foreman:impl-approved`, `foreman:merging-impl`, `foreman:impl-fix`,
  `foreman:done`, `foreman:failed`.
- `"foreman:plan"` is added to
  `packages/foreman/src/foreman/reconciler/observer.py::_QUERY`'s
  `filterBy.labels` list. Without this the daemon never receives the
  issue in its snapshot and the new rule cannot fire.
- `"foreman:plan"` is added back to `_FOREMAN_LABELS` in
  `packages/foreman/src/foreman/init.py` as the first entry (the v3
  documented entry queue label) with color `"0E8A16"` and description
  `"Foreman: queue for planning (auto-transitions to foreman:planning)"`.
- README.md is updated so the line "foreman watches a GitHub repo for
  issues labeled `foreman:plan`" remains accurate; add one sentence
  explaining that the daemon auto-transitions the label to
  `foreman:planning` on the next poll.
- New test
  `test_advance_label_to_planning_fires_on_plan_only_ticket` in
  `packages/foreman/tests/reconciler/test_rules.py` asserts that a
  ticket labeled `("foreman:plan",)` with no PR returns
  `Action.ADVANCE_LABEL_TO_PLANNING` via `evaluate(ctx, rules=RULES)`.
- New test
  `test_advance_label_to_planning_blocked_by_hold` asserts that a
  ticket labeled `("foreman:plan", "foreman:hold")` returns
  `Action.NOOP` (safety tier preempts forward-progress).
- New test
  `test_advance_label_to_planning_skipped_when_planning_label_present`
  asserts that a ticket labeled `("foreman:plan", "foreman:planning")`
  returns `Action.DISPATCH_PLANNER` (the existing rule still wins; new
  rule's predicate is False because `foreman:planning` is present).
- New test
  `test_advance_label_to_planning_skipped_when_plan_approved_present`
  asserts that a ticket labeled `("foreman:plan", "foreman:plan-approved")`
  does not return `Action.ADVANCE_LABEL_TO_PLANNING` (predicate excludes
  any later phase label).
- New test in `packages/foreman/tests/reconciler/test_actions.py`
  asserts that executing `Action.ADVANCE_LABEL_TO_PLANNING` against a
  fake host calls `host.remove_label` with `"foreman:plan"` then
  `host.add_label` with `"foreman:planning"` (mirror existing
  `ADVANCE_LABEL_TO_PLAN_APPROVED` test if one exists, otherwise model
  it on the `Action.ADVANCE_LABEL_TO_DONE` branch).
- `test_action_enum_covers_spec_catalog` in
  `packages/foreman/tests/reconciler/test_actions.py` updated to add
  `"ADVANCE_LABEL_TO_PLANNING"` to the expected enum-name set.
- New test
  `test_observer_query_includes_plan_entry_label` in
  `packages/foreman/tests/reconciler/test_observer.py` asserts
  `"foreman:plan"` is in `_QUERY` (mirrors the existing
  `test_observer_query_includes_spec_fix_label`).
- `packages/foreman/tests/test_init.py` updated so any existing
  assertion that walks `_FOREMAN_LABELS` continues to pass; specifically
  the `test_run_init_creates_all_v3_labels_on_empty_repo` arm gains an
  expectation for `"foreman:plan"`. The summary-style test that asserts
  `"foreman:plan and run" not in summary` continues to pass (the
  next-steps copy still points at `foreman:planning`; the new label is a
  silent entry-queue marker, not a recommended operator action).
- `just check` exits zero on the resulting branch.

## Approach

The bug has two layers and both need a fix:

1. **Observer-side visibility.** `observer.py::_QUERY` filters issues
   server-side via the GraphQL `filterBy.labels` argument. Any label
   absent from that allow-list is invisible to the daemon — the issue
   simply never appears in `ProjectSnapshot.issues`, regardless of what
   any rule predicate would say. `foreman:plan` is not in the current
   list (verified at observer.py:55–67), which is the bedrock cause of
   the silent stall. Adding `"foreman:plan"` to that list is the
   smallest fix that makes the issue visible to the rule engine.
   `test_observer_query_includes_hold_and_failed_labels` already
   documents this contract — the new label needs a parallel assertion.

2. **Rule-side advance.** The v3 reconciler keys every existing rule
   off `foreman:planning` (line 75, 121, 222, 240, 271 in rules.py).
   Adding a `_plan_label_only` predicate + corresponding Rule entry
   in the `FORWARD_PROGRESS` tier at precedence `95` puts the new
   label-advance one step ahead of `dispatch_planner` (precedence 100)
   in the catalog. Because the predicate excludes every other foreman-
   phase label, the rule is only eligible on truly fresh tickets —
   exactly the issue author's "low precedence so it only fires on
   truly-fresh tickets" requirement. The safety tier (precedence 5–70)
   preempts the entire forward-progress tier already, so adversarial
   combinations like `foreman:plan + foreman:hold` are inert by
   construction.

The new `Action.ADVANCE_LABEL_TO_PLANNING` value and its handler in
`execute_action` mirror the existing `ADVANCE_LABEL_TO_PLAN_APPROVED`
shape exactly (actions.py:387–399): two host calls, synchronous
termination row, error-catch already in place at line 425+. No new
host primitive is needed — `remove_label` and `add_label` are already
in the `ReconcilerHost` Protocol and used by sibling label-advance
handlers.

On the `init.py` side, `foreman:plan` was removed from `_FOREMAN_LABELS`
in PR #111 (per the comment at planner.py:241), on the theory that
operators would directly use `foreman:planning`. The empirical lesson
from issue #170 + the issue body of #171 is that this theory was wrong:
the README, the walking-skeleton plan doc, and the legacy v2 dispatcher
all still reference `foreman:plan`, so the label keeps showing up on
real tickets. Restoring it to the catalog — with the description
clarifying that it auto-transitions — is the durable fix; the
auto-transition rule then handles whichever path the label arrives by
(operator typo, doc following, legacy carryover).

The README sentence at line 7 stays factually correct (the daemon
*does* watch for `foreman:plan`) and gains one sentence explaining the
auto-transition; no broader doc refactor is in scope.

`dispatcher.py:99`'s `"foreman:plan": ActionKind.RUN_PLANNER` mapping is
legacy v1/v2 dead code on the v3 daemon path and is intentionally
out of scope (see "Out of scope" below).

## Sub-requests (topologically sorted)

1. Add `ADVANCE_LABEL_TO_PLANNING = "advance_label_to_planning"` to the
   `Action` enum in
   `packages/foreman/src/foreman/reconciler/actions.py` (insert
   immediately above `DISPATCH_PLANNER` so the enum body reads in
   pipeline order).
2. In the same file, extend the `execute_action` if/elif chain with a
   branch matching the `ADVANCE_LABEL_TO_PLAN_APPROVED` body shape:
   `host.remove_label(... "foreman:plan")` then
   `host.add_label(... "foreman:planning")`. No `_DISPATCH_ROLE_FOR_ACTION`
   change (the new action is not a role dispatch).
3. In `packages/foreman/src/foreman/reconciler/rules.py`, add a
   module-level `_plan_label_only` predicate. It returns `True` iff
   `"foreman:plan" in ctx.issue.labels` and the issue carries none of
   the labels enumerated in Acceptance criteria item 3.
4. In the same file, append a new `Rule(name="advance_label_to_planning",
   tier=PrecedenceTier.FORWARD_PROGRESS, precedence=95,
   when=_plan_label_only, then=Action.ADVANCE_LABEL_TO_PLANNING)` to
   `_PROGRESS_RULES`, ordered before `dispatch_planner` (precedence 100).
5. In
   `packages/foreman/src/foreman/reconciler/observer.py::_QUERY`, add
   `"foreman:plan"` to the `filterBy.labels` list (e.g., as the first
   entry, mirroring the natural pipeline order).
6. In `packages/foreman/src/foreman/init.py`, prepend
   `("foreman:plan", "0E8A16", "Foreman: queue for planning (auto-transitions to foreman:planning)")`
   as the first tuple of `_FOREMAN_LABELS`.
7. Update README.md line 7 area to add a parenthetical or a follow-up
   sentence noting that the daemon auto-advances the label to
   `foreman:planning` on the next poll.
8. Add the four new rule tests in
   `packages/foreman/tests/reconciler/test_rules.py` (model on the
   existing `test_dispatch_planner_fires_on_planning_no_pr` and
   `test_needs_help_label_fires_surface_help` helpers — they already
   import `RULES`, `evaluate`, `_issue`, `_ctx_with`).
9. Add the action-handler test in
   `packages/foreman/tests/reconciler/test_actions.py` and extend
   `test_action_enum_covers_spec_catalog`'s expected set.
10. Add the observer test in
    `packages/foreman/tests/reconciler/test_observer.py` (model on
    `test_observer_query_includes_spec_fix_label`).
11. Update `packages/foreman/tests/test_init.py` so the `_FOREMAN_LABELS`
    walk continues to pass with the new entry present. Confirm
    `test_run_init_v3_label_summary_calls_out_planning_entry` (or
    whatever name corresponds to lines 663–667) still passes — its
    string assertions are about `foreman:planning` in the summary copy,
    not about `foreman:plan` absence from the catalog.
12. Run `just check`; fix any incidental failures before opening the PR.

## File-level changes

- `packages/foreman/src/foreman/reconciler/actions.py` — new
  `Action.ADVANCE_LABEL_TO_PLANNING` enum value + matching branch in
  `execute_action`.
- `packages/foreman/src/foreman/reconciler/rules.py` — new
  `_plan_label_only` predicate and `advance_label_to_planning` Rule
  appended to `_PROGRESS_RULES` at precedence 95.
- `packages/foreman/src/foreman/reconciler/observer.py` — add
  `"foreman:plan"` to `_QUERY`'s `filterBy.labels`.
- `packages/foreman/src/foreman/init.py` — prepend the
  `foreman:plan` tuple to `_FOREMAN_LABELS`.
- `README.md` — one-sentence clarification of the auto-transition.
- `packages/foreman/tests/reconciler/test_rules.py` — four new rule
  tests covering fires, hold-preempts, planning-coexists,
  plan-approved-coexists.
- `packages/foreman/tests/reconciler/test_actions.py` — handler test +
  enum-coverage update.
- `packages/foreman/tests/reconciler/test_observer.py` — new
  `test_observer_query_includes_plan_entry_label`.
- `packages/foreman/tests/test_init.py` — extend the label-catalog
  expectation to include `"foreman:plan"`.

## Alternatives considered

- **Option B from the issue body — drop `foreman:plan` entirely and
  document `foreman:planning` as the operator-facing entry label.**
  Ruled out because the documentation (README, walking-skeleton spec,
  legacy dispatcher) all use `foreman:plan`, and at least one path is
  cited by operator-facing copy. Renaming the entry label is a bigger
  doc + UX churn than restoring the auto-transition. The auto-
  transition also has the property that any future legacy carryover
  path (a v2 ticket reopened, a CLI command from older docs) heals
  itself rather than stalling.
- **Make `foreman:planning` the only entry label but emit a
  `surface_help` comment when a `foreman:plan`-only ticket is seen.**
  Ruled out: it requires the operator to manually swap the label, which
  is the exact friction the auto-transition removes. It also requires
  the observer fix anyway (otherwise the help comment never fires) — so
  the operator cost goes up without saving any code.
- **Put the new rule at safety-tier precedence (e.g., 90).** Ruled out
  because the label-advance is a forward-progress action, not a safety
  net; semantically it belongs in the forward-progress tier. The
  practical behavior is identical (safety still preempts via the
  hold/needs-help rules), so there's no compatibility benefit to
  moving the new rule into the safety tier.

## Open questions

(none — the predicate shape is determined by the existing rule catalog,
the action shape is determined by the existing ADVANCE_LABEL_*
templates, and the observer query change is mechanical.)

## Out of scope

- Removing or modernizing the v1/v2 `dispatcher.py:99`
  `"foreman:plan": ActionKind.RUN_PLANNER` mapping. That module is
  legacy code on the v3 daemon path and its cleanup belongs in a
  separate sweep ticket.
- Broader README rewrites beyond the one-sentence auto-transition note.
- Adding rate-limit / idempotence gates to the new rule. The handler is
  one synchronous label swap; once `foreman:planning` is set, the
  predicate flips False on the next poll, so re-fire is structurally
  impossible. No `count_completed` gate is needed.
- A migration path for in-flight tickets that already carry the legacy
  `foreman:plan` label alongside a later phase label (e.g.,
  `plan + plan-approved`). The predicate refuses to fire in those cases
  and the later-phase rules continue to drive the ticket forward; an
  operator can manually remove `foreman:plan` when convenient.
- Changes to the Planner role's label-cleanup behavior (`planner.py`'s
  commentary at 236–244 about not touching `foreman:plan` stays as is —
  the auto-transition now handles label removal before the Planner
  even runs).
