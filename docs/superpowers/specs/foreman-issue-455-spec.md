# Spec: hoist runaway-exemption predicate into one shared function (issue #455)

## Goal

Eliminate the hand-duplication of the runaway-cap exemption predicate across
`InMemoryTicketRepository` and `PostgresTicketRepository` by extracting two
pure functions — `is_runaway_exempt` and `is_transient_error_exempt` — into
`outcome.py`. Both repository impls then call the shared predicates rather than
re-encoding the same skip-set inline. Behavior is unchanged; only the location
of the policy changes.

See issue [#455](https://github.com/anthropics/foreman/issues/455).

## Acceptance criteria

- A pure function `is_runaway_exempt(failure_phase: str | None, outcome_kind: OutcomeKind | None) -> bool` exists in `packages/foreman/src/foreman/v4/outcome.py`. It returns `True` for the four currently-exempt conditions: `failure_phase == "can_run"`, `failure_phase == FAILURE_PHASE_CRASH_RECOVERY`, `outcome_kind == OutcomeKind.BLOCKED`, and `outcome_kind == OutcomeKind.TRANSIENT_PROVIDER_ERROR`. It returns `False` for all other inputs.
- A pure function `is_transient_error_exempt(failure_phase: str | None, outcome_kind: OutcomeKind | None) -> bool` exists in `outcome.py`. It returns `True` for the two exempt conditions in `count_consecutive_transient_provider_errors`: `failure_phase == "can_run"` and `outcome_kind is None` (in-flight row). It returns `False` for all other inputs.
- `InMemoryTicketRepository.count_consecutive_same_state` in `repository.py` delegates its four skip checks to a single `if is_runaway_exempt(inst.failure_phase, inst.outcome_kind): continue` call. All inline `if … continue` branches for `can_run`, `crash_recovery`, `BLOCKED`, and `TRANSIENT_PROVIDER_ERROR` are removed.
- `InMemoryTicketRepository.count_consecutive_transient_provider_errors` in `repository.py` delegates its two skip checks to `if is_transient_error_exempt(inst.failure_phase, inst.outcome_kind): continue`. All inline `if … continue` branches for `can_run` and `outcome_kind is None` are removed.
- `PostgresTicketRepository.count_consecutive_same_state` and `count_consecutive_transient_provider_errors` in `postgres_repository.py` are updated identically.
- All existing repository-contract tests in `_repository_contract.py` (including crash-recovery, can_run, BLOCKED, and TRANSIENT_PROVIDER_ERROR skip tests) continue to pass against both impls without modification.
- New unit tests for `is_runaway_exempt` and `is_transient_error_exempt` are added (can live in a new `tests/v4/test_outcome.py` or be appended to an existing test file). Each predicate has at least one test per exempt case and one "not exempt" case.
- `just check` exits zero.

## Approach

**Pattern:** no GoF pattern fits — this is a straightforward Single Responsibility extraction. The policy of "which rows are runaway-exempt" is currently inline in four loops (two methods × two impls). Extracting it to a named pure function in `outcome.py` makes the policy visible, testable in isolation, and editable in one place.

**Why `outcome.py`?** `OutcomeKind` already lives there. `is_runaway_exempt` tests `outcome_kind` directly against `OutcomeKind` enum values, so it is a natural extension of the module rather than a new dependency. `FAILURE_PHASE_CRASH_RECOVERY` lives in `records.py`; a local import inside `outcome.py` would create a tiny circular-risk (records imports from outcome via `OutcomeKind`). To avoid any circularity, `is_runaway_exempt` accepts `failure_phase` as a plain `str | None` and compares it against the `"can_run"` and `"crash_recovery"` string literals directly — the same strings `FAILURE_PHASE_CRASH_RECOVERY` wraps. An alternative is a new `runaway.py` module; see Alternatives.

**Two distinct predicates, not one:** `count_consecutive_same_state` and `count_consecutive_transient_provider_errors` have different skip-sets:

- `count_consecutive_same_state` skips: `can_run`, `crash_recovery`, `BLOCKED`, `TRANSIENT_PROVIDER_ERROR`
- `count_consecutive_transient_provider_errors` skips: `can_run`, `outcome_kind is None`

These are parallel only in the `can_run` skip; they diverge after that. Forcing one function to serve both would require a mode flag — worse than two clear-purpose functions.

**No Protocol or import changes:** The `TicketRepository` Protocol's docstrings already describe the skip-set; they need no update. The two repository classes simply gain one `from foreman.v4.outcome import is_runaway_exempt, is_transient_error_exempt` import and replace their inline `if … continue` branches.

**Behavior is not changed:** The predicate functions codify the exact same conditions that are currently inline. The only observable effect is that adding a new exempt outcome-kind now requires editing `outcome.py` once, not both repository files.

## Sub-requests (topologically sorted)

1. Add `is_runaway_exempt(failure_phase: str | None, outcome_kind: OutcomeKind | None) -> bool` and `is_transient_error_exempt(failure_phase: str | None, outcome_kind: OutcomeKind | None) -> bool` as module-level pure functions at the bottom of `packages/foreman/src/foreman/v4/outcome.py`.
2. Update `InMemoryTicketRepository.count_consecutive_same_state` in `repository.py` to import and call `is_runaway_exempt`; remove the four inline skip branches.
3. Update `InMemoryTicketRepository.count_consecutive_transient_provider_errors` in `repository.py` to import and call `is_transient_error_exempt`; remove the two inline skip branches.
4. Update `PostgresTicketRepository.count_consecutive_same_state` in `postgres_repository.py` to import and call `is_runaway_exempt`; remove the four inline skip branches.
5. Update `PostgresTicketRepository.count_consecutive_transient_provider_errors` in `postgres_repository.py` to import and call `is_transient_error_exempt`; remove the two inline skip branches.
6. Append unit tests for `is_runaway_exempt` and `is_transient_error_exempt` to the existing `packages/foreman/tests/v4/test_outcome.py`.

## File-level changes

| File | Change |
|------|--------|
| `packages/foreman/src/foreman/v4/outcome.py` | Add `is_runaway_exempt` and `is_transient_error_exempt` pure functions |
| `packages/foreman/src/foreman/v4/repository.py` | Import the two predicates; replace inline skip branches in both counter methods |
| `packages/foreman/src/foreman/v4/postgres_repository.py` | Import the two predicates; replace inline skip branches in both counter methods |
| `packages/foreman/tests/v4/test_outcome.py` | Existing file: append unit tests for `is_runaway_exempt` and `is_transient_error_exempt` |

## Alternatives considered

1. **New `runaway.py` module instead of `outcome.py`**: Avoids any potential circular-import concern between `records.py` and `outcome.py`, and gives the predicate its own dedicated home. Rejected because the circularity risk is already avoided by accepting `failure_phase` as a plain `str | None` (no import of `records.FAILURE_PHASE_CRASH_RECOVERY` from within `outcome.py`), and adding a new module for two small pure functions adds unnecessary file fragmentation. `outcome.py` is the right home since both predicates dispatch on `OutcomeKind`.

2. **Do nothing; rely on the contract test suite as the single source of truth**: The contract tests do guard behavioral parity between both impls, but they test the full method's behavior, not the predicate's policy directly. A contributor adding a new exempt kind would still need to edit three or four places correctly, with no clear "here is the canonical policy" pointer. Rejected — the issue directly observed this as a pain point (the C1 crash-recovery fix required hand-editing each copy).

3. **Inline a shared `_is_exempt` helper inside each class instead of a module-level function**: Would reduce per-method duplication within each class but leave the two-impl duplication intact. Rejected — the goal is a single shared source across both `InMemoryTicketRepository` and `PostgresTicketRepository`, not just within each.

## Open questions

None. The skip-sets are fully documented in the existing code and contract tests; the target module and function signatures are unambiguous.

## Out of scope

- Changing any exempt condition (e.g. adding new exempt outcome-kinds). This is a pure location refactor; the predicate logic is unchanged.
- Updating the `TicketRepository` Protocol docstrings (they correctly describe the skip-set; no content change needed).
- Deduplicating the `max_state_attempts = 3` default that also appears in multiple places (the other I3 sub-item from the architecture review). That is a separate, independent change.
- Any changes to `count_state_instances_for_ticket`, `latest_pr_number_for_ticket`, or any other repository method.
