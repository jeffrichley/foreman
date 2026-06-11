# Spec: derive `_NON_FAILURE_OUTCOMES` from a typed `Outcome` enum with member metadata (issue #258)

## Goal

Replace the hand-maintained `_NON_FAILURE_OUTCOMES: tuple[str, ...]` (plus the two sibling
constants `_OUTCOME_RUNNING` and `_OUTCOME_ERRORED_RECOVERY`) at the top of
`packages/foreman/src/foreman/reconciler/exec_log.py` with a typed `Outcome` enum-with-metadata
that catalogs every outcome string written to the `execution_log` table and forces each member
to declare its classification (NON_FAILURE / FAILURE / NEUTRAL) at the point of definition.
Adding a new outcome anywhere in the codebase becomes a single-line operation that Python's
enum machinery rejects at module load if the contributor omits the classification — making
"forgot to update `_NON_FAILURE_OUTCOMES`" structurally impossible. Type-only change; runtime
values and SQL bind shapes are byte-identical. Tracks issue
[#258](https://github.com/jeffrichley/foreman/issues/258).

## Acceptance criteria

- A new module at `packages/foreman/src/foreman/reconciler/outcomes.py` exists and exports:
  - `OutcomeClass` — an `enum.Enum` (or `enum.StrEnum`) with exactly three members:
    `NON_FAILURE`, `FAILURE`, `NEUTRAL`.
  - `Outcome` — a `enum.StrEnum` subclass (Python 3.12, available since 3.11) whose members
    each carry both a string value (the SQL-bind value) and a `classification: OutcomeClass`
    attribute, attached via a custom `__new__(cls, value: str, classification: OutcomeClass)`.
  - `NON_FAILURE_OUTCOMES: frozenset[str]` — derived at module load by filtering
    `Outcome` members on `m.classification is OutcomeClass.NON_FAILURE`.
  - `FAILURE_OUTCOMES: frozenset[str]` — derived the same way on `OutcomeClass.FAILURE`.
  - `NEUTRAL_OUTCOMES: frozenset[str]` — derived the same way on `OutcomeClass.NEUTRAL`.
  - `__all__` lists exactly those five names.
- The `Outcome` enum contains **exactly these 12 members** with the exact value strings
  and classifications below (the enumeration matches every outcome string currently written
  to `execution_log` via `write_action` or `terminate_action` anywhere under
  `packages/foreman/src/foreman/`):

  | Member name | Value string | Classification |
  | --- | --- | --- |
  | `SUCCESS` | `"success"` | `NON_FAILURE` |
  | `DRY_RUN` | `"dry_run"` | `NON_FAILURE` |
  | `SKIPPED_CAPACITY` | `"skipped_capacity"` | `NON_FAILURE` |
  | `ERROR` | `"error"` | `FAILURE` |
  | `TIMEOUT` | `"timeout"` | `FAILURE` |
  | `SUBPROCESS_KILLED` | `"subprocess_killed"` | `FAILURE` |
  | `ERRORED_RECOVERY` | `"errored:recovery"` | `FAILURE` |
  | `RUNNING` | `"running"` | `NEUTRAL` |
  | `RESET` | `"reset"` | `NEUTRAL` |
  | `ALERT` | `"alert"` | `NEUTRAL` |
  | `FAILED` | `"failed"` | `NEUTRAL` |
  | `EXECUTED` | `"executed"` | `NEUTRAL` |

- `NON_FAILURE_OUTCOMES` equals `frozenset({"success", "dry_run", "skipped_capacity"})` —
  byte-for-byte the same set the existing
  `_NON_FAILURE_OUTCOMES: tuple[str, ...] = ("success", "dry_run", "skipped_capacity")`
  carries at `packages/foreman/src/foreman/reconciler/exec_log.py:31`.
- `packages/foreman/src/foreman/reconciler/exec_log.py`:
  - The module-level constant `_NON_FAILURE_OUTCOMES` (currently at line 31) is **removed**.
  - A new import at the top of the module adds
    `from foreman.reconciler.outcomes import NON_FAILURE_OUTCOMES, Outcome`.
  - The two SQL bind sites that iterate the tuple (currently at lines 477-478 and 572-573)
    iterate `NON_FAILURE_OUTCOMES` instead. The placeholder shape (`",".join("?" for _ in
    NON_FAILURE_OUTCOMES)`) and the `*NON_FAILURE_OUTCOMES` parameter splat continue to work
    because `frozenset` is iterable in both contexts. The SQL strings around them are
    unchanged.
  - `_OUTCOME_RUNNING = "running"` (currently at line 21) is **removed**. Every reference
    in this file (lines 86, 318, 612 — `WHERE outcome = '{_OUTCOME_RUNNING}'`,
    `(ticket_id, action, _OUTCOME_RUNNING)`, `(_OUTCOME_RUNNING,)`) is replaced with
    `Outcome.RUNNING.value` (or `Outcome.RUNNING` itself in the parameterized bind sites,
    since `StrEnum` members compare equal to their string values when sqlite binds them).
    The partial-index DDL at line 86 (which uses f-string interpolation, not a bind
    parameter) MUST use `Outcome.RUNNING.value` to interpolate the bare string `'running'`
    — interpolating an enum member's `repr()` would write `'Outcome.RUNNING'` into the
    DDL and silently break the index.
  - `_OUTCOME_ERRORED_RECOVERY = "errored:recovery"` (currently at line 22) is **removed**.
    The two references (line 618 in `recover_orphaned`'s `outcome=_OUTCOME_ERRORED_RECOVERY`
    arg and any docstring mention) are replaced with `Outcome.ERRORED_RECOVERY.value`.
  - All other module-level constants — `_RATE_LIMIT_RESET_ACTION_PREFIX`,
    `_RATE_LIMIT_RESET_OUTCOME`, `CURRENT_SCHEMA_VERSION`, `_COST_COLUMNS`, `_SCHEMA` —
    are **left untouched**. They are not `_NON_FAILURE_OUTCOMES` / `_OUTCOME_RUNNING` /
    `_OUTCOME_ERRORED_RECOVERY` and the issue body does not call them out for replacement.
  - Internal docstring references to `:data:`_NON_FAILURE_OUTCOMES`` (currently at lines
    408 and 429 of `count_recent_failures`'s docstring) are updated to point at the new
    `NON_FAILURE_OUTCOMES` frozenset (e.g.,
    `:data:`foreman.reconciler.outcomes.NON_FAILURE_OUTCOMES``).
- **Red positive test first** (`packages/foreman/tests/reconciler/test_outcomes.py`):
  - `test_outcome_enum_contains_all_known_outcomes` — asserts that
    `{m.value for m in Outcome}` equals the exact set of 12 string values in the table
    above. Pins the inventory so a future contributor who adds an outcome string somewhere
    without adding it to the enum trips this test.
  - `test_non_failure_outcomes_matches_existing_tuple_contents` — asserts that
    `NON_FAILURE_OUTCOMES == frozenset({"success", "dry_run", "skipped_capacity"})`.
    Byte-for-byte compatibility with the old tuple's contents (the spec body's "must
    match byte-for-byte" requirement).
  - `test_failure_outcomes_classification` — asserts
    `FAILURE_OUTCOMES == frozenset({"error", "timeout", "subprocess_killed",
    "errored:recovery"})`.
  - `test_neutral_outcomes_classification` — asserts
    `NEUTRAL_OUTCOMES == frozenset({"running", "reset", "alert", "failed", "executed"})`.
  - `test_outcome_member_str_equality` — asserts `Outcome.RUNNING == "running"`
    (sanity-check `StrEnum` behavior so the SQL bind sites keep working without
    `.value` access at the call site).
  - `test_outcome_classifications_partition_membership` — asserts every `Outcome` member's
    classification is one of the three buckets AND that
    `NON_FAILURE_OUTCOMES | FAILURE_OUTCOMES | NEUTRAL_OUTCOMES`
    covers `{m.value for m in Outcome}` with no overlap (i.e., the three frozensets are a
    partition). This pins the design contract that adding a new bucket also requires
    classifying every existing member into it explicitly.
  - These tests MUST be added as the first commit on the branch and MUST be red against
    the spec's parent commit (the `outcomes` module doesn't exist yet — `ImportError`).
    The Worker confirms red against the parent commit and reports the red signal in the
    commit message of the test commit (per
    [[superpowers:test-driven-development]] and `CLAUDE.md`'s test-first convention).
- **Negative test** (same file): `test_outcome_member_without_classification_raises_type_error` —
  pins the constraint that adding a new enum member without a `(value, classification)`
  tuple is impossible at module load. Construct a synthetic `enum.StrEnum` subclass with
  the same `__new__(cls, value, classification)` pattern as `Outcome` and a member
  defined as a bare string (`BAD = "bad"` instead of `BAD = ("bad", OutcomeClass.NEUTRAL)`),
  inside a `with pytest.raises(TypeError):` block. (The synthetic-subclass approach is
  used rather than asserting against `Outcome` itself because Python evaluates the
  real enum's class body at import time — by the time a test runs, an `Outcome` with
  a broken member would have raised `ImportError` from `pytest` collection, not
  `TypeError` from the test body.) The synthetic enum's `__new__` body matches
  `Outcome.__new__`'s shape, so the test pins the design contract: "any enum that adopts
  this pattern raises `TypeError` for a member without classification" — which is
  exactly what the real `Outcome` does.
- `_OUTCOME_RUNNING` and `_OUTCOME_ERRORED_RECOVERY` and `_NON_FAILURE_OUTCOMES` no longer
  appear anywhere in `packages/foreman/src/foreman/`:
  `grep -rE "_OUTCOME_RUNNING|_OUTCOME_ERRORED_RECOVERY|_NON_FAILURE_OUTCOMES" packages/foreman/src/foreman/`
  returns 0 results.
- Docstring or comment references to `_NON_FAILURE_OUTCOMES` (e.g., in
  `packages/foreman/tests/reconciler/test_rate_limit.py:223`) are updated to refer to
  `NON_FAILURE_OUTCOMES` (or to `Outcome.<MEMBER>.classification`) so a future reader who
  greps for the symbol finds the new name. The test logic at that site (asserting that
  `errored:recovery` IS counted as a failure for the rate-limit predicate) is unchanged —
  it remains a contract test against the predicate, not against the enum's classification.
- `just check` (lint + mypy + pytest) exits 0. `mypy` typechecks the new module — the
  custom `__new__` requires class-variable annotations (per the issue body's
  "implementation sketch"), and the project's strict-mypy settings will reject an enum
  subclass whose member-attribute access goes through `Any`.
- `new_failures_count == 0` against the Worker's pre-push gate. No previously-passing test
  goes from green to red; the existing `test_count_recent_failures_counts_errored_recovery_outcomes`
  in `packages/foreman/tests/reconciler/test_rate_limit.py:215-242` (which pins that
  `errored:recovery` IS counted as a failure) MUST continue to pass — that test is a
  ground-truth contract test the refactor must not break.

## Approach

The fix is structural. Today's `_NON_FAILURE_OUTCOMES` tuple is one of three discrete
module-level constants near the top of `exec_log.py` that name outcome strings: the tuple
itself (line 31), `_OUTCOME_RUNNING` (line 21), and `_OUTCOME_ERRORED_RECOVERY` (line 22).
All three serve the same role — they're the canonical-name source for outcome string
literals used in SQL queries and writer calls. The contract they encode — "this is every
outcome string the system cares about, with its classification" — is implicit, distributed
across three lines, and impossible for the type system to enforce. The fix is to make that
contract explicit and singular.

**The enum-with-metadata pattern.** Python's `enum.StrEnum` (3.11+, available in this
project per `pyproject.toml:58` = Python 3.12) supports a custom `__new__` that takes
additional positional args beyond the value. The standard pattern is:

```python
class OutcomeClass(Enum):
    NON_FAILURE = "non_failure"
    FAILURE = "failure"
    NEUTRAL = "neutral"


class Outcome(StrEnum):
    """Every outcome string written to ``execution_log``, classified.

    Adding a new outcome ... [docstring per spec template].
    """

    # Class-variable annotation so mypy knows .classification is real.
    classification: OutcomeClass

    def __new__(cls, value: str, classification: OutcomeClass) -> "Outcome":
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.classification = classification
        return obj

    SUCCESS = ("success", OutcomeClass.NON_FAILURE)
    DRY_RUN = ("dry_run", OutcomeClass.NON_FAILURE)
    # ... etc.
```

The Python enum machinery passes the right-hand-side tuple as `*args` to `__new__`. A
member declared as `BAD = "bad"` (bare string) passes a single positional arg, missing
`classification` — Python's call-site validation raises `TypeError: __new__() missing 1
required positional argument: 'classification'` at class-creation time, before the module
finishes loading. That is the "impossible to forget classification" property the issue
explicitly asks for.

The derived frozensets fall out of a generator expression at module load:

```python
NON_FAILURE_OUTCOMES: frozenset[str] = frozenset(
    m.value for m in Outcome if m.classification is OutcomeClass.NON_FAILURE
)
```

Same shape for `FAILURE_OUTCOMES` and `NEUTRAL_OUTCOMES`. No drift possible: change a
member's classification, and the frozenset memberships move with it.

**Module location: `packages/foreman/src/foreman/reconciler/outcomes.py`.** The issue body
suggests `packages/foreman/src/foreman/outcomes.py` (top-level) but leaves the Planner
discretion. Co-locating with `exec_log.py` under `reconciler/` is the better fit because:

- The outcomes ARE reconciler-domain concepts. The `execution_log` table is the
  reconciler's append-only ledger (`exec_log.py:1-8` opening docstring). Outcomes are
  values bound into that table — they belong with the table.
- Every write site that uses these strings already imports from
  `foreman.reconciler.*` (actions.py is in the same package; v3_host.py is in the same
  package; daemon.py is in the same package). The two non-reconciler write sites —
  `foreman.dispatch_recorder` and `foreman.v3_bus_endpoint` — already import
  `foreman.reconciler.exec_log`, so adding another `foreman.reconciler.outcomes` import
  doesn't expand their package coupling.
- The stats.py JSONL outcome Literals are a different vocabulary (see "Out of scope"
  below). Putting `Outcome` at the top level of `foreman/` would invite future drift
  pressure to merge the two namespaces — which the issue body and stats.py docstrings
  explicitly defer. Keeping `Outcome` in the reconciler subpackage signals scope clearly.

**The 12-outcome inventory.** A grep across `packages/foreman/src/foreman/` for the
`outcome="<literal>"` and `outcome='<literal>'` pattern at every `ExecutionLog.write_action`
and `ExecutionLog.terminate_action` call site enumerates these strings: `success`, `error`,
`dry_run`, `running`, `skipped_capacity`, `timeout`, `subprocess_killed`,
`errored:recovery`, `reset`, `alert`, `failed`, `executed`. That's 12. The Worker MUST
re-run this grep at implementation time and cross-reference against the table above; any
mismatch (a new outcome string introduced after this spec lands, or one this spec missed)
is a Worker decision-point — add it to the enum with a classification, document the
classification choice in the commit message, and proceed.

**Classification choices and why each one falls out.** The three buckets map directly to
how `count_recent_failures` and `recent_failure_details` treat the outcome:

- `NON_FAILURE` — explicitly excluded from the failure-counter via `outcome NOT IN
  (NON_FAILURE_OUTCOMES)`. These three (success, dry_run, skipped_capacity) ARE the
  current tuple contents. The behavior is preserved byte-for-byte.
- `FAILURE` — the outcomes that DO count toward the rate-limit window when they appear on
  a dispatch_* action's terminator row. `error` is the generic exception terminator
  (actions.py:739, v3_host.py:1008). `timeout` is the subprocess-hung path (v3_host.py:987).
  `subprocess_killed` is the Recorder's exit-tracker terminator (dispatch_recorder.py:255).
  `errored:recovery` is the daemon-restart orphan-recovery path (exec_log.py:618), pinned
  as a failure by `test_count_recent_failures_counts_errored_recovery_outcomes` and the
  intentional "over-count is safer than under-count" semantics in
  `count_recent_failures`'s docstring.
- `NEUTRAL` — sentinel/marker rows that the rate-limit predicate's existing structural
  filters already exclude. `running` is filtered by `parent_log_id IS NOT NULL` (start
  rows have null parent). `reset` is filtered by the `action = ?` clause (the reset
  sentinel's action is `rate_limit_reset:<orig_action>`, not the orig_action itself).
  `alert` / `failed` / `executed` are the observer-failure-alert and config-reload
  admin-action one-shot rows (daemon.py:210, 343, 385) — they're never on `dispatch_*`
  actions, so the rate-limit predicate's action-filter excludes them.

**Why not replace the inline string literals at every write site?** A truer "single source
of truth" would replace every `outcome="success"` / `outcome="error"` / etc. at the 21
write sites in `actions.py`, `v3_host.py`, `dispatch_recorder.py`, and `daemon.py` with
`Outcome.SUCCESS.value` / `Outcome.ERROR.value`. That would let mypy enforce "no new
outcome string can be written without going through the enum." But the issue body's
sub-request §1 and §2 explicitly bound the scope to `exec_log.py`'s three constants. Going
further is a larger refactor that touches many test files (test setup uses string
literals like `outcome="error"` directly in `_record_failure`) and would expand the diff
significantly. The `test_outcome_enum_contains_all_known_outcomes` test is the structural
floor for "future contributor adds a new outcome string" — it's a guard test, not a
type-system enforcement, but it catches drift at PR-review time. Replacing inline write
sites with `Outcome.<MEMBER>.value` references is a follow-up ticket (see "Out of scope").

**Why `StrEnum` and not `IntEnum` or bare `Enum`.** SQL bind sites pass outcome values as
positional `?` parameters; sqlite's adapter converts `str` subclasses transparently.
`StrEnum` members compare equal to their string values (`Outcome.RUNNING == "running"` is
`True`), which means existing call sites like `(ticket_id, action, Outcome.RUNNING)`
work without a `.value` accessor. The partial-index DDL string at exec_log.py:84-87 is
the one case where `.value` IS required: it interpolates via f-string, and an enum
member's `repr()` would write `Outcome.RUNNING` into the DDL string rather than
`running`. That site MUST be `Outcome.RUNNING.value`.

**Sequencing against foreman#256.** The issue body says this ticket "should land AFTER
foreman#256 (dead `*_failed` Literal cleanup)" to reduce enumeration scope. That sequencing
concern is **misframed** — foreman#256 touches stats.py JSONL outcome Literals
(`worker_failed`, `fixer_failed`), which are a **different vocabulary** from
execution_log's outcome strings. The `Outcome` enum in this spec enumerates only the
execution_log outcomes, so what foreman#256 does to stats.py has zero effect on this
ticket's enumeration. Both tickets can land in any order. (The Planner is recording this
explicitly because the issue body's sequencing claim is wrong; the Worker should not
block on foreman#256 landing first.)

## Sub-requests (topologically sorted)

1. **Create new module** `packages/foreman/src/foreman/reconciler/outcomes.py` with
   `OutcomeClass` enum (NON_FAILURE / FAILURE / NEUTRAL), `Outcome(StrEnum)` with the 12
   members and their classifications per the table above, custom `__new__` that attaches
   `classification` to each member, and the three derived `frozenset[str]` exports
   (`NON_FAILURE_OUTCOMES`, `FAILURE_OUTCOMES`, `NEUTRAL_OUTCOMES`). `__all__` lists
   exactly those five names. Module docstring follows the project's "what + why" style
   (see `exec_log.py:1-8` for the precedent).
2. **Add red positive + negative tests** at
   `packages/foreman/tests/reconciler/test_outcomes.py` per the acceptance-criteria list.
   This commit lands BEFORE the consumer migration so the red signal is documented per
   [[superpowers:test-driven-development]]. The Worker confirms the test is red against
   the spec's parent commit and notes the red signal in the commit message.
3. **Migrate `exec_log.py` to consume the new module.** In one commit: remove the local
   `_NON_FAILURE_OUTCOMES` tuple (line 31) and the `_OUTCOME_RUNNING` / `_OUTCOME_ERRORED_RECOVERY`
   constants (lines 21-22); add the `from foreman.reconciler.outcomes import
   NON_FAILURE_OUTCOMES, Outcome` import at the top; replace every reference in this
   file (lines 86, 318, 477-478, 572-573, 612, 618) per the acceptance-criteria
   list. Run `just check` and confirm 0 failures.
4. **Update docstring + comment references** to point at the new symbols. Specifically:
   `packages/foreman/src/foreman/reconciler/exec_log.py:408,429` (docstring refs to
   `_NON_FAILURE_OUTCOMES`) and
   `packages/foreman/tests/reconciler/test_rate_limit.py:223` (comment ref). The test
   logic itself is not changed; only the symbol name in the comment.
5. **Run the full quality gate.** `just check` exits 0. `new_failures_count == 0` against
   the pre-push gate.

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/src/foreman/reconciler/outcomes.py` | **NEW.** `OutcomeClass` enum, `Outcome` StrEnum with 12 members and classification metadata, three derived `frozenset[str]` exports. |
| `packages/foreman/src/foreman/reconciler/exec_log.py` | Remove `_NON_FAILURE_OUTCOMES` tuple at line 31, `_OUTCOME_RUNNING` at line 21, `_OUTCOME_ERRORED_RECOVERY` at line 22. Add `from foreman.reconciler.outcomes import NON_FAILURE_OUTCOMES, Outcome`. Replace 6 in-file references (lines 86 DDL f-string, 318 SQL bind, 477-478 SQL bind, 572-573 SQL bind, 612 SQL bind, 618 terminate-action call). Update docstring refs at lines 408 and 429. |
| `packages/foreman/tests/reconciler/test_outcomes.py` | **NEW.** Six positive tests (enum inventory, three classification-frozenset checks, str-equality check, partition-membership check) and one negative test (member-without-classification raises TypeError via synthetic enum subclass). |
| `packages/foreman/tests/reconciler/test_rate_limit.py` | Update the comment at line 223 (currently `\`errored:recovery\` to \`_NON_FAILURE_OUTCOMES\``) to reference `NON_FAILURE_OUTCOMES` (or `Outcome.ERRORED_RECOVERY.classification`). Test body unchanged. |

## Alternatives considered

- **Dict-based registry**: a separate dict mapping outcome → classification, kept in sync
  with the existing outcome strings via a guard test. Ruled out per the issue body's
  explicit framing: the dict shape requires a guard test asserting every outcome has a
  classification entry, but a contributor can pass the test today by adding both entries
  and break it tomorrow by adding only one. The enum-with-metadata shape eliminates the
  second structure entirely — Python's enum machinery enforces classification at the
  constructor call site, raising `TypeError` at module load if a contributor adds a
  member without it. Jeff's design ask 2026-06-10 was emphatic that this dict-based
  shape did NOT meet the "impossible to forget classification" bar.
- **Top-level module location** (`packages/foreman/src/foreman/outcomes.py`): the issue
  body suggests this path. Ruled out in favor of `packages/foreman/src/foreman/reconciler/outcomes.py`
  because the enum's scope is execution_log-specific (a reconciler-domain concept), and
  every existing consumer already imports from `foreman.reconciler.*`. The top-level
  path would invite scope drift pressure to merge the stats.py JSONL outcome Literals
  into the same enum, which is explicitly out of scope (see below).
- **Also replace inline `outcome="..."` string literals at every write site** (21 sites
  across actions.py, v3_host.py, dispatch_recorder.py, daemon.py) with
  `Outcome.<MEMBER>.value` references. Ruled out for scope per the issue body's
  sub-request §1/§2 boundary. A future ticket can take this on; the
  `test_outcome_enum_contains_all_known_outcomes` test is the structural net in the
  meantime — if a contributor adds a new outcome string at a write site without adding
  it to the enum, the test goes red.
- **Defer to foreman#256 first**: the issue body asserts a sequencing dependency. Ruled
  out because foreman#256 touches stats.py JSONL outcome Literals (a different vocabulary
  from execution_log outcomes), so its landing has zero effect on this ticket's
  enumeration scope. Both tickets can land in any order.

## Open questions

(none — the inventory of 12 outcome strings is grep-derived from the codebase as it
stands at the spec-branch parent commit; the classification choices map directly to
existing rate-limit-predicate behavior; module location is a defensible Planner judgment
call.)

## Out of scope

- **Rewriting the per-role `Literal` unions in `packages/foreman/src/foreman/stats.py`**
  (`outcome: Literal["spec_written", "spec_failed", "exception"]`, etc.) to derive from
  the `Outcome` enum. Per the issue body's explicit "Out of scope" bullet. The JSONL
  outcome vocabulary is a different namespace from execution_log's outcome vocabulary
  (the four Literal sets contain `spec_written`, `clean`, `needs_fix`, `implemented`,
  `fixed`, `exception`, etc. — none of which appear in execution_log). Whether to
  unify them or keep them separate is a separate design question; this spec keeps
  them separate.
- **Replacing the inline `outcome="..."` string literals** at the 21 write sites in
  `actions.py`, `v3_host.py`, `dispatch_recorder.py`, and `daemon.py` with
  `Outcome.<MEMBER>.value` references. Follow-up ticket if/when the
  `test_outcome_enum_contains_all_known_outcomes` guard test catches drift.
- **Migrating the test helpers** (e.g., `_record_failure(log, outcome="error")` in
  `packages/foreman/tests/reconciler/test_rate_limit.py`) to use enum references. Tests
  are documentation of intent; their string literals stay readable as-is.
- **Adding new outcome values** for new failure modes (separate tickets).
- **Migrating historical JSONL data or the existing `execution_log.sqlite`**: no migration
  needed; runtime values are byte-identical and the type system enforces the contract
  only at the Python layer.
- **Renaming any existing outcome string** (e.g., `errored:recovery` → `errored_recovery`,
  `subprocess_killed` → `killed`). Existing strings are persisted on disk in production
  databases; renaming requires a separate migration design.
- **The `_RATE_LIMIT_RESET_ACTION_PREFIX` and `_RATE_LIMIT_RESET_OUTCOME` constants** at
  `exec_log.py:42-43`. `_RATE_LIMIT_RESET_OUTCOME` is the string `"reset"` — the same
  value as the new `Outcome.RESET.value`. Technically the Worker could replace the
  module-level constant with `Outcome.RESET.value`; not doing so is an explicit scope
  choice (the issue body only calls out `_NON_FAILURE_OUTCOMES`, `_OUTCOME_RUNNING`,
  and `_OUTCOME_ERRORED_RECOVERY`). Worker may NOT change `_RATE_LIMIT_RESET_OUTCOME`
  in this ticket.
