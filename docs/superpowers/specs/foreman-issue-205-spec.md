# Spec: 24h has_recent boundary tests for lagging advance-label rules (issue #205)

## Goal

Add four boundary tests that pin the 24-hour `has_recent` guard on
`advance_label_to_plan_approved_lagging` and `advance_label_to_done`.
These rules suppress when a successful row was written within the last
24h and re-fire after the window expires; the boundary is currently
asserted only at "row just written" (NOOP) and "no row at all" (rule
fires). The middle of that range — exactly at 24h − 1s vs. 24h + 1s — is
unverified, and a regression in the `has_recent` string-timestamp
comparison would silently break either eager-suppression (operator
ping-pong returns) or re-fire (tickets stall after spec/impl PR merge).
Closes the §7 item 4 drift entry in `docs/architecture/v3-reconciler.md`.
See issue [#205](https://github.com/jeffrichley/foreman/issues/205).

## Acceptance criteria

- Four new tests exist in
  `packages/foreman/tests/reconciler/test_rules.py` with these exact
  function names:
  - `test_advance_label_to_plan_approved_lagging_re_fires_after_24h_window`
  - `test_advance_label_to_plan_approved_lagging_suppressed_within_24h_window`
  - `test_advance_label_to_done_re_fires_after_24h_window`
  - `test_advance_label_to_done_suppressed_within_24h_window`
- `grep -c 'def test_advance_label_to_.*_24h_window' packages/foreman/tests/reconciler/test_rules.py`
  returns `4`.
- The two `*_re_fires_after_24h_window` tests assert the resulting
  action is `Action.ADVANCE_LABEL_TO_PLAN_APPROVED` and
  `Action.ADVANCE_LABEL_TO_DONE` respectively. Each is implemented via
  the public `evaluate(ctx, rules=RULES)` entry point — not by calling
  the predicate function in isolation — so the test exercises the full
  rule + `has_recent` integration as required by the issue body.
- The two `*_suppressed_within_24h_window` tests assert the resulting
  action is `Action.NOOP`, mirroring the existing
  `test_advance_label_to_plan_approved_idempotent` "just now"
  assertion shape (test_rules.py:519-535).
- The 24h boundary deltas used by the tests are exactly
  `timedelta(seconds=24 * 3600 + 1)` (re-fires case) and
  `timedelta(hours=23, minutes=59, seconds=59)` (suppressed case),
  matching the bullet list in the issue body's "What this ticket does".
- The tests do NOT call `time.sleep`, do NOT import `freezegun`, and do
  NOT monkeypatch `datetime.now`. Determinism comes from seeding the
  `execution_log.ts` column explicitly with a UTC timestamp computed
  relative to `datetime.now(UTC)`. (See Approach §2 for the helper
  shape; the recommended seeding strategy is the one called out in the
  issue body under "Recommended approach".)
- The §7 item 4 drift entry in `docs/architecture/v3-reconciler.md`
  is removed and the surviving items are renumbered consecutively.
- All four new tests pass; every existing test in
  `packages/foreman/tests/reconciler/test_rules.py` still passes;
  every other previously-passing test still passes.
- `just check` exits 0.
- The new tests can be run as a focused selection:
  `uv run pytest packages/foreman/tests/reconciler/test_rules.py -k 'advance_label_to.*24h' -v`
  reports 4 passed.

## Approach

### 1. Why the existing tests don't cover this

`packages/foreman/src/foreman/reconciler/rules.py:322-337` defines
`_spec_pr_merged_label_lagging`, which the
`advance_label_to_plan_approved_lagging` rule uses as its predicate.
Lines 333-336 short-circuit on
`ctx.log.has_recent("advance_label_to_plan_approved", ctx.ticket_id,
within_seconds=3600 * 24)`. The impl-side analog lives at
`rules.py:439-452` (`_impl_pr_merged_label_lagging`).

`has_recent` itself (exec_log.py:149-167) builds a cutoff via
`datetime.now(UTC) - timedelta(seconds=within_seconds)`, formats it as
`"%Y-%m-%d %H:%M:%S"`, and runs `SELECT 1 FROM execution_log WHERE
ticket_id = ? AND action = ? AND ts > ?`. The `ts` column is
`TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP`, so SQLite stores
timestamps as `YYYY-MM-DD HH:MM:SS` text strings and the comparison is
string-lexicographic — correct only because the format is
fixed-width and lexicographically equivalent to chronological order.

Existing coverage:
- `test_advance_label_to_plan_approved_when_spec_pr_merged`
  (test_rules.py:508-516) — rule FIRES when no row exists.
- `test_advance_label_to_plan_approved_idempotent`
  (test_rules.py:519-535) — rule is suppressed when a row was just
  written via `write_action` (which uses `CURRENT_TIMESTAMP`).
- Same shape for the impl side at lines 600-607.

What's missing: a test where the seeded row's `ts` lands at a known
delta from `datetime.now(UTC)`. Without that, a regression that
flipped the comparator (e.g., `ts >=` → `ts >`) or broke the format
contract (e.g., serializing with `T` separator or a `Z` suffix)
would not be caught.

### 2. Test-helper shape

The existing `write_action` method (exec_log.py:68-91) does not
accept a `ts` argument — it relies on SQLite's `CURRENT_TIMESTAMP`
default. The cleanest path is a private test-only helper local to
`test_rules.py` that issues a raw `INSERT` through a `sqlite3`
connection, matching the precedent in `test_exec_log.py` (lines
73, 108).

Helper signature (private to this test module, single call site
shared by all four tests via the parameterized cases):

```python
import sqlite3
from datetime import UTC, datetime, timedelta

def _seed_advance_label_row_at(
    log: ExecutionLog,
    *,
    ticket_id: str,
    action: str,
    seconds_ago: int,
) -> None:
    """Insert a `success`-outcome row into the exec_log with ts set
    exactly ``seconds_ago`` seconds before ``datetime.now(UTC)``.

    Bypasses ``ExecutionLog.write_action`` because that method takes
    its ``ts`` from SQLite's ``CURRENT_TIMESTAMP`` default; these
    boundary tests need an explicit delta. The `ts` format matches
    the one ``has_recent`` builds its cutoff in (exec_log.py:153-157)
    so SQLite's lexicographic comparison sees the row at the intended
    offset.
    """
    ts = (datetime.now(UTC) - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with sqlite3.connect(log.db_path) as conn:
        conn.execute(
            """
            INSERT INTO execution_log
                (ts, ticket_id, project, rule_name, action, outcome, details)
            VALUES (?, ?, 'foreman', ?, ?, 'success', '{}')
            """,
            (ts, ticket_id, f"{action}_rule", action),
        )
```

Notes:
- `rule_name` is set to `f"{action}_rule"` only to satisfy the schema's
  TEXT-nullable column with a non-null value that won't mislead future
  log readers; `has_recent` doesn't read `rule_name`.
- `details` is the literal JSON string `'{}'` to mirror
  `json.dumps({}, sort_keys=True)`.
- The `with sqlite3.connect(...) as conn:` block ends with an implicit
  commit, matching `ExecutionLog._connect`'s pattern.
- No transactional separation needed across the four tests — each test
  gets a fresh `tmp_path` and a fresh log.

### 3. Tests

Each test reuses the existing `_ctx_with` / `_issue` / `_pr` helpers in
test_rules.py. Two test fixtures already cover the shapes we need:

- `_issue(labels=("foreman:planning",))` + `_pr(is_merged=True)`
  (spec head_ref default) — matches the spec-side lagging rule.
- `_issue(labels=("foreman:impl-approved",))` +
  `_pr(is_merged=True, head_ref="foreman/impl-143")` — matches the
  impl-side lagging rule.

Both are exactly the configurations already used by the existing
"rule fires" and "idempotent" tests.

Each new test follows the same shape:
1. Build `ctx` via `_ctx_with`.
2. Call `_seed_advance_label_row_at(ctx.log, ticket_id=ctx.ticket_id,
   action=<action_name>, seconds_ago=<boundary>)`.
3. Assert `evaluate(ctx, rules=RULES) is <expected_action>`.

The four cases:

| Test name | seconds_ago | action seeded | expected `evaluate` result |
|---|---|---|---|
| `test_advance_label_to_plan_approved_lagging_re_fires_after_24h_window` | `24 * 3600 + 1` | `"advance_label_to_plan_approved"` | `Action.ADVANCE_LABEL_TO_PLAN_APPROVED` |
| `test_advance_label_to_plan_approved_lagging_suppressed_within_24h_window` | `23 * 3600 + 59 * 60 + 59` | `"advance_label_to_plan_approved"` | `Action.NOOP` |
| `test_advance_label_to_done_re_fires_after_24h_window` | `24 * 3600 + 1` | `"advance_label_to_done"` | `Action.ADVANCE_LABEL_TO_DONE` |
| `test_advance_label_to_done_suppressed_within_24h_window` | `23 * 3600 + 59 * 60 + 59` | `"advance_label_to_done"` | `Action.NOOP` |

The re-fire cases produce a deterministic non-NOOP action because the
rest of the catalog's predicates don't match on a `planning` (or
`impl-approved`) + merged-PR shape — only the lagging rule matches —
so the asserted action is the lagging rule's `then`.

The suppressed cases produce NOOP because: no safety rule fires (no
needs-help, no conflict, PR is MERGEABLE), the lagging rule is
suppressed by `has_recent` returning True, and no other
forward-progress rule's predicate is satisfied on a `planning + PR
is_merged=True` (or `impl-approved + PR is_merged=True`) shape. This
matches the assertion in
`test_advance_label_to_plan_approved_idempotent`.

### 4. Doc-drift cleanup

`docs/architecture/v3-reconciler.md` §7 currently lists item 4 as:

> **Lagging-label rules (`advance_label_to_plan_approved_lagging`,
> `advance_label_to_done`) have a 24h `has_recent` guard** but no
> test for multi-tick re-fire scenarios. If a manual label revert
> happened, would the rule re-fire? Probably yes; unverified.

Once these four tests land, that entry is resolved. The spec
requires deleting it and renumbering items 5, 6, 7 down to 4, 5, 6 so
the list stays consecutive. No other text in the doc references item
numbers in §7, so the renumber is local. (Verified by grep:
`grep -n '§7' docs/architecture/v3-reconciler.md` returns the section
header only; no cross-reference broken by the renumber.)

### 5. Why explicit `ts` over `freezegun` or `monkeypatch`

The issue body's "Recommended approach" picks the explicit-`ts`
strategy. Three reasons make it the right call here, beyond following
the spec:

1. **No new dev dependency.** `freezegun` is not in this repo's test
   deps (`pyproject.toml`); adding it for two assertions is excessive.
2. **Fewer monkeys, fewer escapes.** Monkeypatching
   `foreman.reconciler.exec_log.datetime` would work but adds a
   fragile coupling on the module-level import shape — if the module
   later imports `datetime` differently (e.g., a `from datetime
   import UTC` rename) the patch silently stops applying.
3. **Tests stay parallel-safe.** The explicit-`ts` row is purely
   per-test SQLite state. `freezegun` and module-level monkeypatches
   sometimes leak across xdist workers in subtle ways; pytest-tmp_path
   isolation is bulletproof here.

## Sub-requests (topologically sorted)

1. In `packages/foreman/tests/reconciler/test_rules.py`, add the
   imports needed by the new helper at the top of the file (if not
   already present): `import sqlite3` and `from datetime import
   timedelta` (the file already imports `datetime` and `UTC` at
   test_rules.py:6).
2. In the same file, add the `_seed_advance_label_row_at` private
   helper exactly as written in Approach §2, placed below the
   existing `_ctx_with` helper (so all per-test helpers live in one
   block).
3. Add `test_advance_label_to_plan_approved_lagging_re_fires_after_24h_window`
   in the "Forward-progress rule cases" section, immediately AFTER
   the existing `test_advance_label_to_plan_approved_idempotent`
   (test_rules.py:519-535). Body follows the row 1 column of the
   table in Approach §3.
4. Add `test_advance_label_to_plan_approved_lagging_suppressed_within_24h_window`
   immediately after the test from sub-request 3. Body follows row 2.
5. Add `test_advance_label_to_done_re_fires_after_24h_window`
   immediately after the existing `test_advance_label_to_done_when_impl_pr_merged`
   (test_rules.py:600-607). Body follows row 3.
6. Add `test_advance_label_to_done_suppressed_within_24h_window`
   immediately after the test from sub-request 5. Body follows row 4.
7. In `docs/architecture/v3-reconciler.md`, delete §7 item 4
   verbatim, then renumber the surviving §7 items 5, 6, 7 down to
   4, 5, 6. No other doc changes.
8. Run the verification commands from the `## Verification` section
   below; record their outputs in the impl PR body so the Reviewer
   can cross-check.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/tests/reconciler/test_rules.py` | Add `import sqlite3`, `from datetime import timedelta`. Add `_seed_advance_label_row_at` helper. Add four new tests (names per acceptance criteria). No edits to existing tests. |
| `docs/architecture/v3-reconciler.md` | Delete §7 item 4; renumber items 5-7 down to 4-6. |

No expected changes to:
- `packages/foreman/src/foreman/reconciler/rules.py` (predicate code
  is correct).
- `packages/foreman/src/foreman/reconciler/exec_log.py` (`has_recent`
  is correct).
- `pyproject.toml` (no new dev dependency).
- Any other test file or doc.

## Verification

Before opening the impl PR, the Worker MUST run AND record the output
of these commands in the PR body so the Reviewer can cross-check:

1. `grep -c 'def test_advance_label_to_.*_24h_window' packages/foreman/tests/reconciler/test_rules.py`
   — expected: `4`.
2. `uv run pytest packages/foreman/tests/reconciler/test_rules.py -k 'advance_label_to.*24h' -v`
   — expected: 4 passed, 0 failed.
3. `uv run pytest packages/foreman/tests/reconciler/test_rules.py -v`
   — expected: every test passes, including the new four.
4. `just check` — expected exit code 0. Capture the tail of pytest
   output showing test count + pass/fail summary.
5. `grep -n 'multi-tick re-fire' docs/architecture/v3-reconciler.md`
   — expected: 0 matches (the §7 item 4 phrase removed).
6. Sanity-check that no `freezegun` import was added:
   `grep -rn freezegun packages/foreman/` — expected: 0 matches.

## Alternatives considered

- **Use `freezegun` to advance the clock instead of seeding an
  explicit `ts`.** Rejected: would add a new dev dependency for two
  assertions, and the issue body's Out-of-scope rule explicitly forbids
  introducing `freezegun` if it isn't already in the test deps. The
  explicit-`ts` approach is the recommended one in the issue body.

- **Monkeypatch `foreman.reconciler.exec_log.datetime.now`.** Rejected:
  fragile against module-level import renames, doesn't isolate cleanly
  across xdist workers, and the issue body lists it as "fall back" —
  the explicit-`ts` row is the primary recommendation.

- **Parameterize the four assertions onto two `@pytest.mark.parametrize`
  test functions (one per rule, two boundary cases each).** Rejected:
  would produce only TWO `def test_...` statements, breaking the
  acceptance-criteria grep check (`grep -c 'def
  test_advance_label_to_.*_24h_window'` must return 4). The issue's
  test names are individually enumerated; the right interpretation
  is four distinct top-level test functions.

- **Add a `write_action_at(ts=...)` method to `ExecutionLog` instead
  of a test-only helper.** Rejected: the production code has no use
  for it (all production `write_action` calls take `CURRENT_TIMESTAMP`),
  so the addition would carry test-coupled API surface into the
  module. A private helper inside `test_rules.py` localizes the
  test-time-injection concern to the tests that need it.

- **Wait until the 24h window is hit in production before adding
  these tests (i.e., let real telemetry catch the drift).** Rejected:
  the §7 item 4 drift entry exists precisely because the failure
  mode is silent (a ticket stalls forever or operator ping-pong
  appears); the unit-test cost is cheap and the production telemetry
  cost is "operator manually unsticks a ticket and files a bug
  weeks later".

## Open questions

(none — the implementation is fully specified by the issue body, the
codebase precedents are clear, and the only design call — explicit
`ts` vs. `freezegun` — is explicitly decided in the issue body's
"Recommended approach")

## Out of scope

- Do not modify the 24h value in either `_spec_pr_merged_label_lagging`
  (rules.py:334) or `_impl_pr_merged_label_lagging` (rules.py:449).
  The window is correct.
- Do not modify `ExecutionLog.has_recent` (exec_log.py:149-167) or its
  string-based UTC comparison. The implementation is correct; this
  PR is testing its integration with the lagging rules, not refactoring
  the implementation.
- Do not add boundary tests for the 1-hour `surface_help` rate-limit
  in `_safety_with_rate_limit` (rules.py:135-145). That rule is
  already pinned by `test_surface_help_rate_limited_within_one_hour`
  (test_rules.py:201-217); per the issue's Out-of-scope section,
  the 1h variant is "sufficient" and does not need a boundary test
  for this PR.
- Do not add an operator-override CLI flag to bypass the 24h guard.
  Explicitly out of scope per the issue body.
- Do not introduce `freezegun` as a new dependency. Out of scope per
  the issue body.
- Do not refactor `_seed_advance_label_row_at` into a shared
  conftest fixture or helper module — it is private to
  `test_rules.py` and not consumed elsewhere. A future test that
  needs the same helper can promote it then.
- Do not rename the existing `test_advance_label_to_plan_approved_idempotent`
  / `test_advance_label_to_done_when_impl_pr_merged` tests. The new
  tests COMPLEMENT them (the issue body says so explicitly under
  "Related"); they do not replace them.
- Do not edit any other entry in `docs/architecture/v3-reconciler.md`
  §7. Only item 4 is in scope; the surrounding items are out of scope
  (renumbering them down by one to keep the list consecutive is
  mechanical and does not count as an edit to their content).
