# Spec: make `_MAX_FIX_ATTEMPTS` and `_MAX_IMPL_ATTEMPTS` configurable per-project (issue #14)

## Goal

Replace the hardcoded module-level constants `_MAX_FIX_ATTEMPTS = 3` (in
`roles/fixer.py`) and `_MAX_IMPL_ATTEMPTS = 3` (in `roles/worker.py`) with
per-project configuration fields so operators can tune retry budgets to
match each project's CI cost profile. Default remains `3` for both, so
existing configs are unaffected. Issue:
[#14](https://github.com/jeffrichley/foreman/issues/14).

## Acceptance criteria

- `ProjectConfig` (in `packages/foreman/src/foreman/config.py`) exposes
  `max_fix_attempts: int` and `max_impl_attempts: int` Pydantic fields, both
  defaulting to `3`.
- Both fields are loadable from a project's TOML config block in
  `~/.foreman/config.toml` under the project key (e.g.
  `[projects."jeffrichley/voice"]`); missing keys fall back to the default
  `3`.
- `roles/fixer.py` references `project_config.max_fix_attempts` at the
  max-gate check; the module-level `_MAX_FIX_ATTEMPTS` constant is removed
  (no shadow constant left behind).
- `roles/worker.py` references `project_config.max_impl_attempts` at the
  max-gate check; the module-level `_MAX_IMPL_ATTEMPTS` constant is removed.
- The label-provisioning code path used by project onboarding (see
  Open Questions on naming: `foreman init` vs `foreman project add`)
  creates `foreman:fix-attempt-1..N` labels where `N = max_fix_attempts`
  and `foreman:impl-attempt-1..M` where `M = max_impl_attempts`.
- A field validator on `ProjectConfig` rejects values `< 1` with a clear
  Pydantic error (zero attempts is meaningless; negative values are nonsense).
- Unit tests cover: (a) default-value behavior unchanged, (b) custom values
  round-trip through TOML loading, (c) Fixer/Worker honor the configured
  max, (d) label generation matches the configured maxes, (e) invalid
  values rejected at config-load time.
- `just check` passes (ruff + mypy + pytest).

## Approach

The change is mechanical once the prerequisite modules exist: lift two
hardcoded ints into a Pydantic config schema, replumb the two call sites,
and update label provisioning to use the same numbers. The interesting
work is preserving the contract precisely — defaults must stay at `3` so
this lands as a no-op for any project that didn't opt in, and the
configured maxes must drive label generation so the label state machine
never gets out of sync with the runtime gate.

`ProjectConfig` is the per-project sub-model inside the TOML config
described in §2.3 and §6.1 of the architectural spec. Adding two integer
fields with sensible defaults matches the Pydantic conventions already
implied by `config.py`'s described role ("TOML config schema + loading").
The field validator ensures `>= 1`; the docstring on each field cites the
issue and the rationale (fast-CI vs slow-CI projects).

Fixer and Worker each currently (per the issue) hold a module-level
constant plus a helper `_count_fix_attempts` / `_count_impl_attempts` that
counts prior attempts from GitHub state. The minimal change is: at the
point where the helper's return value is compared against the constant,
read the constant from `project_config` instead. The Fixer/Worker entry
points already receive a project-config object (they need it to identify
which repo's labels to query), so no new plumbing is needed at the call
boundary.

Label provisioning is where the architectural-spec gap surfaces. The
architectural spec's §2.2 state machine lists `foreman:spec-fix` and
`foreman:impl-fix` but not the per-attempt counter labels
(`foreman:fix-attempt-N`, `foreman:impl-attempt-N`) implied by the issue
and by the existence of `_count_fix_attempts`. This spec treats those
counter labels as a real design element that the onboarding command
must provision in the configured count. The Worker should NOT invent a
new label-creation pathway — it should reuse the same label-creation
helper the onboarding command uses, parameterized by the configured
maxes.

This is opt-in by construction: existing configs without
`max_fix_attempts` / `max_impl_attempts` keys see no behavior change.

## Sub-requests (topologically sorted)

1. In `packages/foreman/src/foreman/config.py`, add `max_fix_attempts: int = 3`
   and `max_impl_attempts: int = 3` to the `ProjectConfig` Pydantic model,
   with a field validator rejecting values `< 1`. Include docstrings
   referencing this issue and the fast-CI vs slow-CI rationale.
2. In `packages/foreman/src/foreman/roles/fixer.py`, remove the
   module-level `_MAX_FIX_ATTEMPTS = 3` constant. Replace the comparison
   site (the max-gate check guarded by `_count_fix_attempts`) with a read
   of `project_config.max_fix_attempts`.
3. In `packages/foreman/src/foreman/roles/worker.py`, remove the
   module-level `_MAX_IMPL_ATTEMPTS = 3` constant. Replace the comparison
   site (the max-gate check guarded by `_count_impl_attempts`) with a read
   of `project_config.max_impl_attempts`.
4. In the onboarding command's label-provisioning step (see Open Questions
   for the command name), generate `foreman:fix-attempt-1..N` labels where
   `N = project_config.max_fix_attempts` and `foreman:impl-attempt-1..M`
   labels where `M = project_config.max_impl_attempts`. Reuse a single
   `_provision_attempt_labels` helper rather than duplicating the loop.
5. Add unit tests in `packages/foreman/tests/`:
   - `test_config.py`: defaults are `3`; custom values round-trip from
     TOML; values `< 1` raise validation error.
   - `test_fixer.py`: at attempt count == max, Fixer halts; at attempt
     count == max - 1, Fixer proceeds. Parameterize over `max=3` and
     `max=5` to prove the field is consulted, not a constant.
   - `test_worker.py`: same shape as `test_fixer.py` but for
     `max_impl_attempts`.
   - `test_onboarding_labels.py` (or extend existing): with
     `max_fix_attempts=5`, exactly 5 `foreman:fix-attempt-N` labels are
     created.
6. Run `just check` and fix any lint/typecheck/test failures.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/config.py` | Add two fields + validator to `ProjectConfig`. |
| `packages/foreman/src/foreman/roles/fixer.py` | Remove `_MAX_FIX_ATTEMPTS`; read from `project_config`. |
| `packages/foreman/src/foreman/roles/worker.py` | Remove `_MAX_IMPL_ATTEMPTS`; read from `project_config`. |
| `packages/foreman/src/foreman/cli.py` (or wherever the onboarding command's label-creation lives) | Parameterize attempt-label generation by configured maxes. |
| `packages/foreman/tests/test_config.py` | New: config field defaults, round-trip, validation. |
| `packages/foreman/tests/test_fixer.py` | New: max-gate honors configured value. |
| `packages/foreman/tests/test_worker.py` | New: max-gate honors configured value. |
| `packages/foreman/tests/test_onboarding_labels.py` | New (or extend): attempt-label count matches config. |

## Alternatives considered

- **Environment-variable override only** (`FOREMAN_MAX_FIX_ATTEMPTS`):
  rejected — the architectural spec §2.3 reserves env vars for credentials,
  and per-project tuning belongs in per-project config, not per-process env.
- **Single shared `max_attempts` field for both Fixer and Worker**:
  rejected — the issue and the architectural spec already separate fix vs
  impl counters; collapsing them loses tuning granularity (impl is usually
  the expensive one).
- **Per-issue label override** (`foreman:max-attempts:5`): the issue
  explicitly lists this as out of scope for this ticket and a separate
  lower-priority feature; deferred.
- **Hardcode a higher default like `5` instead of making it configurable**:
  rejected — the issue's whole point is that "right" varies per project
  (voice ~10s CI vs chrona ~3min). One default cannot fit both.
- **Do nothing**: rejected — operators already feel the pain on slow-CI
  projects burning compute on doomed retries; the fix is small.

## Open questions

1. **`foreman init` vs `foreman project add` naming.** The issue says
   "foreman init", but the architectural spec §5.2 calls the onboarding
   command `foreman project add`. The spec assumes these are the same
   command and the worker should target whichever name has shipped at
   implementation time. Reviewer should confirm whether `foreman init`
   is a rename (and update the architectural spec) or a separate command.
2. **`foreman:fix-attempt-N` labels are not in the architectural spec's
   §2.2 state machine.** The issue implies they exist and that
   `_count_fix_attempts` counts them. This spec accepts the implication
   and treats them as a real labeling convention, but they are not
   formally locked anywhere. Reviewer should confirm the label-naming
   convention (`foreman:fix-attempt-N` vs `foreman:fix-attempt:N` vs
   another shape) — there is no precedent in the architectural spec's
   existing label list.
3. **Prerequisite modules do not exist yet.** As of this spec, the
   `foreman` package contains only `__init__.py`. The referenced
   `roles/fixer.py`, `roles/worker.py`, `config.py`, and onboarding
   command are all unbuilt. This spec is a forward-looking contract; the
   Worker can only execute it after the walking skeleton (per
   architectural spec §7) has produced those modules. If the Worker
   reaches this ticket before those exist, it should fail back to human
   rather than create them from scratch.

## Out of scope

- Per-issue label-based overrides (e.g. `foreman:max-attempts:5` on a
  specific issue). The issue explicitly defers this.
- Separating spec-fix vs impl-fix vs review-fix into distinct counters
  beyond what already exists. The two-counter design is preserved as-is.
- Changing the default value from `3` for either field.
- Reworking the attempt-counting logic itself (`_count_fix_attempts` /
  `_count_impl_attempts`); only the constant the count is compared
  against changes.
- Adding telemetry or metrics around attempt usage. Useful but separate.
- Building `foreman init` / `foreman project add` itself — this spec
  assumes that command already exists and extends its label-creation step.
