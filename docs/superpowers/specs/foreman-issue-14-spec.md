# Spec: Make `_MAX_FIX_ATTEMPTS` and `_MAX_IMPL_ATTEMPTS` configurable per-project (issue #14)

## Goal

Lift the two hardcoded retry budgets — `_MAX_FIX_ATTEMPTS = 3` in
`packages/foreman/src/foreman/roles/fixer.py:94` and
`_MAX_IMPL_ATTEMPTS = 3` in `packages/foreman/src/foreman/roles/worker.py:113`
— into `ProjectConfig` as two new optional integer fields (default `3`, so
existing configs are unaffected). Plumb the resolved values through the
Fixer and Worker run paths and through `foreman init`'s label-generation
step so the right number of `foreman:fix-attempt-N` / `foreman:impl-attempt-N`
labels exist on the repo before the gate ever fires.

Tracks issue [#14](https://github.com/jeffrichley/foreman/issues/14).

## Acceptance criteria

- `ProjectConfig` in `packages/foreman/src/foreman/config.py` exposes two
  new fields:
  - `max_fix_attempts: int = 3` (validated `ge=1`)
  - `max_impl_attempts: int = 3` (validated `ge=1`)
  Both round-trip through `load_config()` from TOML when set and resolve to
  `3` when omitted — verified by new tests in
  `packages/foreman/tests/test_config.py`.
- The Fixer's max-attempt gate at `roles/fixer.py:404`, the
  failed-escalation check at `roles/fixer.py:495`, and the
  `_build_user_prompt`'s "maximum of {N}" interpolation at
  `roles/fixer.py:249` all read the configured value rather than the
  module constant.
- The Worker's max-attempt gate at `roles/worker.py:471`, the
  failed-escalation check at `roles/worker.py:667`, and the
  `_build_user_prompt`'s "maximum of {N}" interpolation at
  `roles/worker.py:333` all read the configured value rather than the
  module constant.
- The module-level constants `_MAX_FIX_ATTEMPTS` and `_MAX_IMPL_ATTEMPTS`
  are removed — the `Field(default=3)` on `ProjectConfig` is now the
  single source of truth for the default. The default-of-3 is also
  asserted by a config test, so a future accidental edit of that default
  fails loudly.
- `foreman init` accepts `--max-fix-attempts` and `--max-impl-attempts`
  click options (both default `3`), threads them through `InitConfig` to
  `run_init`, and:
  - Creates `foreman:fix-attempt-1..N` and `foreman:impl-attempt-1..M`
    labels on the target repo (instead of the hardcoded 1..3 currently in
    `init.py:101-106`), where `N == max_fix_attempts` and
    `M == max_impl_attempts`.
  - Emits `max_fix_attempts` / `max_impl_attempts` lines into the written
    `[projects.<name>]` block ONLY when the value differs from the default
    of `3` (same "omit when default" pattern `_format_project_block`
    already uses for `check_command` at `init.py:441`).
- New test in `packages/foreman/tests/test_init.py` confirms that
  `--max-fix-attempts=5 --max-impl-attempts=4` causes init to create
  `foreman:fix-attempt-1..5` and `foreman:impl-attempt-1..4` on the repo
  AND to write those two lines into the project block.
- New tests in `packages/foreman/tests/test_roles_fixer.py` and
  `packages/foreman/tests/test_roles_worker.py` cover the per-project
  override path: with `max_fix_attempts=5`, an issue carrying
  `foreman:fix-attempt-3` still dispatches (attempt 4); with
  `max_fix_attempts=2`, an issue carrying `foreman:fix-attempt-2` is
  refused before LLM dispatch. Symmetric coverage for the Worker.
- Existing tests in `test_roles_fixer.py`, `test_roles_worker.py`,
  `test_config.py`, `test_init.py`, `test_cli.py` continue to pass under
  `just check`. No behavior change for any caller that does not set the
  new fields — `3 → 3` round-trip is the default.
- `packages/foreman/src/foreman/templates/instructions.md.template`
  documents the two new knobs in a new "Retry budgets" section so
  operators can discover them when reading their own
  `.foreman/INSTRUCTIONS.md`.

## Approach

The two constants are read in three places each (gate, failed-escalation,
user-prompt interpolation) and nowhere else, per the grep at
`_MAX_FIX_ATTEMPTS|_MAX_IMPL_ATTEMPTS`. Both `run_fixer` and `run_worker`
already receive `config: Config, project_name: str` and already pull
`project = config.projects[project_name]` to read other per-project fields
(`check_command`, `repo`, `local_clone_path`). So the wiring is mechanical:
add the field on `ProjectConfig`, read it once at the top of each `run_*`,
pass the resolved int down into `_build_user_prompt` as a kwarg, and use
it in the gate + escalation checks in place of the module constant.

The reason we **remove** rather than keep the module constants is to avoid
having two facts about the default that can drift. Today's constants are
`= 3`; tomorrow's `ProjectConfig.max_fix_attempts: int = Field(default=3, ge=1)`
is `= 3`. Two defaults is one too many. A single pydantic Field default is
the source of truth, and a config test pins it.

For `foreman init`, the label-generation step at `init.py:78-107`
currently lists `foreman:fix-attempt-1..3` and `foreman:impl-attempt-1..3`
as static tuples. We factor those out of `_FOREMAN_LABELS` into a small
helper `_build_foreman_labels(max_fix_attempts, max_impl_attempts)` that
returns the full list with the attempt rows generated to match. The 12
non-attempt labels stay verbatim; only the 6 attempt rows become
parametric. `_ensure_labels` then takes the result of that helper rather
than reading the module-level constant.

`foreman init` gains two click options. Both default to `3` and use
`show_default=True` (matches the `--check-command` option's shape at
`cli.py:212-218`). They flow into `InitConfig` (frozen dataclass — add
two new fields), then into `run_init`, which passes them into both
`_build_foreman_labels(...)` and `_format_project_block(...)`. The block
formatter only emits the line when the value differs from the default
(same pattern as `check_command` today at `init.py:441-442`), so existing
config blocks aren't padded with default lines.

We deliberately do NOT add a separate "label sync" surface for projects
that bump the value post-init. Operators have two existing paths: re-run
`foreman init --force --max-fix-attempts=N`, or create the missing labels
by hand. The first re-uses the idempotent label step (`_ensure_labels`
already skips existing names and tolerates 422 race-with-self). The
second is one `gh label create` per missing label. Either is cheap, and
not adding a new CLI surface keeps this ticket atomic.

The Fixer's and Worker's system prompts (`prompts/fixer.md`,
`prompts/worker.md`) do not hardcode "3" anywhere — only the user prompt
does, via the constant interpolation. So the prompts themselves need no
changes; only the user-prompt builder strings.

Why this fits the repo:

- Pattern parity with `ProjectConfig.check_command`, `dev_base_branch`,
  `auto_merge_spec`, `auto_merge_impl` — all are optional per-project
  knobs with sensible defaults that the role orchestrators read at run
  start. Tests for those (`test_config.py:446-619`, `test_config.py:571-598`)
  are the template for the new round-trip tests.
- Pattern parity with init's existing "omit default values" block formatter
  (`init.py:441-442`) — non-default `max_*` lines are emitted, defaults
  are silent.
- Pattern parity with init's idempotent label step — `_ensure_labels`
  already handles "label exists" cleanly, so re-running init with a higher
  N is safe.

## Sub-requests (topologically sorted)

1. **`packages/foreman/src/foreman/config.py`**: Add
   `max_fix_attempts: int = Field(default=3, ge=1, description=...)` and
   `max_impl_attempts: int = Field(default=3, ge=1, description=...)` to
   `ProjectConfig` (insert after `auto_merge_impl` at line 240). Field
   descriptions should mention that these are the retry budgets for the
   Fixer's spec-fix cycle and the Worker's impl cycle respectively, and
   that `foreman init` uses them to decide how many `foreman:*-attempt-N`
   labels to create.

2. **`packages/foreman/tests/test_config.py`**: Add three new tests
   alongside the existing `check_command` / `auto_merge_*` round-trip
   tests (around line 598):
   - `test_max_attempts_default_to_three`: confirms an omitted block reads
     `3` for both fields.
   - `test_max_attempts_read_from_config_file`: confirms TOML values flow
     through (`max_fix_attempts = 5`, `max_impl_attempts = 4`).
   - `test_max_attempts_reject_zero`: confirms `max_fix_attempts = 0`
     raises `ValidationError` (validated `ge=1`).

3. **`packages/foreman/src/foreman/roles/fixer.py`**: Remove the
   `_MAX_FIX_ATTEMPTS = 3` constant at line 94. In `run_fixer` (around
   line 402), resolve `max_fix_attempts = project.max_fix_attempts` after
   the `project` lookup; replace the gate at line 404 (`if attempt >
   _MAX_FIX_ATTEMPTS`) and the failed-escalation at line 495 (`if attempt
   == _MAX_FIX_ATTEMPTS`) with the resolved value. Add
   `max_fix_attempts: int` as a required kwarg to `_build_user_prompt`
   (line 209-264) and replace the interpolation at line 249 with the
   passed-in value. Update the module docstring at lines 31-35 to say
   "max N attempts (per-project configurable via
   `ProjectConfig.max_fix_attempts`, default 3)" rather than the literal
   "Max 3".

4. **`packages/foreman/src/foreman/roles/worker.py`**: Mirror sub-request 3
   for the Worker. Remove `_MAX_IMPL_ATTEMPTS = 3` at line 113. Resolve
   `max_impl_attempts = project.max_impl_attempts` in `run_worker` (around
   line 469); replace the gate at line 471 and the failed-escalation at
   line 667. Thread `max_impl_attempts: int` into `_build_user_prompt`
   (line 267-350) and replace the interpolation at line 333. Update the
   module docstring at lines 38-51 accordingly.

5. **`packages/foreman/tests/test_roles_fixer.py`**: The unit-only tests
   for `_count_fix_attempts` (lines 62-80) need no change — they test
   parsing only. For the `run_fixer` integration tests around lines
   386-609, the existing Config-fixture construction path already builds
   a `ProjectConfig` that will now have `max_fix_attempts=3` by default —
   no test changes needed for the default-budget runs. Add two NEW tests:
   - `test_run_fixer_with_custom_max_fix_attempts_high`: configures
     `max_fix_attempts=5`, seeds the issue with `foreman:fix-attempt-3`,
     asserts dispatch proceeds (attempt becomes 4) and the new attempt
     label is added.
   - `test_run_fixer_with_custom_max_fix_attempts_low`: configures
     `max_fix_attempts=2`, seeds the issue with `foreman:fix-attempt-2`,
     asserts dispatch is refused with a RuntimeError mentioning "max 2"
     and no `foreman:fix-attempt-3` label is added.

6. **`packages/foreman/tests/test_roles_worker.py`**: Symmetric to
   sub-request 5 — two new tests for `max_impl_attempts={5,2}` mirroring
   the Fixer pair.

7. **`packages/foreman/src/foreman/init.py`**: Refactor `_FOREMAN_LABELS`
   (lines 78-107) — drop the 6 hardcoded `*-attempt-N` rows; keep the 12
   state/modifier rows verbatim as the new `_FOREMAN_STATIC_LABELS`. Add
   a new `_build_foreman_labels(max_fix_attempts: int, max_impl_attempts: int) -> list[tuple[str, str, str]]`
   helper that returns `_FOREMAN_STATIC_LABELS + [generated fix-attempt
   rows] + [generated impl-attempt rows]`, using the same color
   (`"BFD4F2"`) and the same description pattern (`f"Foreman: fix cycle
   attempt {n} of {max}"`). Update `_ensure_labels` (line 343) to accept
   `(max_fix_attempts, max_impl_attempts)` and call the helper internally.
   Update the summary's "labels total" count at line 551-553 to use the
   length of the built list, not the module constant.

8. **`packages/foreman/src/foreman/init.py`**: Add
   `max_fix_attempts: int = 3` and `max_impl_attempts: int = 3` to the
   `InitConfig` frozen dataclass (lines 118-145). Plumb them through
   `run_init` (lines 573-664) to both `_ensure_labels` and
   `_format_project_block`. Extend `_format_project_block` (lines
   426-443) to accept the two values and emit
   `max_fix_attempts = <N>` / `max_impl_attempts = <M>` lines ONLY when
   the value differs from 3 (mirror the existing `check_command` non-default
   gating). Add a module constant `_DEFAULT_MAX_ATTEMPTS = 3` near
   `_DEFAULT_CHECK_COMMAND` (line 67) for the comparison.

9. **`packages/foreman/src/foreman/cli.py`**: Add two new `--max-fix-attempts`
   and `--max-impl-attempts` click options to the `init` command (mirror
   the `--check-command` shape at lines 212-218), each
   `type=click.IntRange(min=1)`, `default=3`, `show_default=True`. Add
   them to the `init` function signature, and forward them into
   `InitConfig(...)` at lines 268-275.

10. **`packages/foreman/tests/test_init.py`**: Add tests covering the
    plumbing:
    - `test_init_creates_extra_attempt_labels_when_max_above_default`:
      runs init with `max_fix_attempts=5, max_impl_attempts=4`, asserts
      the fake repo got `create_label` calls for `foreman:fix-attempt-4`,
      `foreman:fix-attempt-5`, and `foreman:impl-attempt-4`.
    - `test_init_writes_max_attempt_lines_when_above_default`: calls
      `_format_project_block(... max_fix_attempts=5, max_impl_attempts=4)`
      and asserts both lines appear in the rendered TOML; calls again
      with `max_fix_attempts=3` and asserts neither line appears.
    - Update the existing `expected_names` list at line 453 (and any
      counterpart in `test_init.py`) that depends on `_FOREMAN_LABELS`
      having 18 entries — either pivot to assert against
      `_build_foreman_labels(3, 3)` or stay correct because the helper
      with default args returns exactly the same 18 names.

11. **`packages/foreman/tests/test_cli.py`**: Add (or extend an existing)
    click-runner test asserting that `foreman init` accepts
    `--max-fix-attempts 5 --max-impl-attempts 4` and forwards them into
    `InitConfig`. Mirror the existing `--check-command` test if present.

12. **`packages/foreman/src/foreman/templates/instructions.md.template`**:
    Add a new "Retry budgets" section between "Quality gate" (line 26-32)
    and "Active development branch" (line 34) describing
    `max_fix_attempts` and `max_impl_attempts`, their default of `3`, and
    the note that bumping them requires either re-running `foreman init
    --force` (to create the additional `foreman:*-attempt-N` labels) or
    creating those labels manually.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/config.py` | Add `max_fix_attempts: int = 3` and `max_impl_attempts: int = 3` to `ProjectConfig`, both `ge=1`. |
| `packages/foreman/src/foreman/roles/fixer.py` | Remove `_MAX_FIX_ATTEMPTS` constant; resolve from `project.max_fix_attempts`; thread into `_build_user_prompt` and the two gate checks. |
| `packages/foreman/src/foreman/roles/worker.py` | Mirror Fixer: remove `_MAX_IMPL_ATTEMPTS`; resolve from `project.max_impl_attempts`. |
| `packages/foreman/src/foreman/init.py` | Factor attempt-label rows out of `_FOREMAN_LABELS`; add `_build_foreman_labels(max_fix, max_impl)`; extend `InitConfig` + `run_init` + `_format_project_block` to accept and propagate the new values. |
| `packages/foreman/src/foreman/cli.py` | Add `--max-fix-attempts` / `--max-impl-attempts` click options to `init`, forward into `InitConfig`. |
| `packages/foreman/src/foreman/templates/instructions.md.template` | Document the new knobs in a new "Retry budgets" section. |
| `packages/foreman/tests/test_config.py` | Add 3 round-trip / validation tests for the new fields. |
| `packages/foreman/tests/test_roles_fixer.py` | Add 2 integration tests covering custom `max_fix_attempts` (high allows extra attempts; low gates earlier). |
| `packages/foreman/tests/test_roles_worker.py` | Symmetric: 2 integration tests for custom `max_impl_attempts`. |
| `packages/foreman/tests/test_init.py` | Add 2 tests covering extra-label generation and project-block emission for non-default values; reconcile any `_FOREMAN_LABELS`-length assertions. |
| `packages/foreman/tests/test_cli.py` | Add a click-runner test asserting the two new flags reach `InitConfig`. |

No new modules, no new dependencies. The work is entirely additive on
`ProjectConfig` + a contained refactor inside `init.py` + mechanical
constant-to-config replacement in the two role orchestrators.

## Alternatives considered

- **Use environment variables (`FOREMAN_MAX_FIX_ATTEMPTS`,
  `FOREMAN_MAX_IMPL_ATTEMPTS`).** Rejected: the issue explicitly asks for
  per-project configuration, not per-host. A daemon serving multiple
  projects (the explicit voice-vs-chrona example in the issue body) would
  collapse into a single value under env-var control.
- **Keep `_MAX_FIX_ATTEMPTS` / `_MAX_IMPL_ATTEMPTS` module constants as
  the default, have `ProjectConfig.max_fix_attempts: int | None = None`
  fall back to them.** Rejected: two facts about the default ("3" in the
  Field default vs "3" in the module constant) can drift; the next person
  changing the default in one place but not the other introduces a silent
  bug. Single source of truth via `Field(default=3)` is cleaner; the
  removed constants leave no dead code.
- **Add a per-issue label override (`foreman:max-attempts:5`).** Rejected
  by the issue's explicit "Out of scope" section.
- **Replace discrete `foreman:fix-attempt-N` labels with a single
  parameterized counter label (e.g., `foreman:fix-attempt-counter=4`).**
  Rejected: bigger surface change that breaks every in-flight ticket's
  audit trail and requires migrating the `_FIX_ATTEMPT_RE` parser, the
  per-episode reset logic in `roles/fixer.py:485` and
  `roles/worker.py:386`, and operator habits. The current discrete-label
  scheme works; this ticket should not also redesign it.
- **Do not change `foreman init` — let operators create extra labels
  manually after bumping the config.** Rejected: the issue explicitly
  asks for init awareness, and the cost of generating the right number
  of attempt labels in `_ensure_labels` is trivial (a list comprehension)
  while saving the operator the footgun where a max=5 config silently
  burns the budget at 3 because labels 4 + 5 don't exist on the repo.

## Open questions

(none — the issue is unambiguous, the surface is contained, the field
default preserves existing behavior, and the test approach mirrors
established patterns in `test_config.py` and `test_init.py`.)

## Out of scope

- Per-issue label override (`foreman:max-attempts:5` on a single ticket)
  — explicitly out of scope per the issue body.
- Different max budgets for spec-fix vs impl-fix vs review beyond the two
  counters this ticket adds — already implicit; the issue body confirms.
- A separate `foreman label sync` CLI surface to add missing attempt
  labels post-init when an operator bumps the value via direct config
  edit. Operators have two existing paths (re-run `foreman init --force`
  with the new flag values, or `gh label create` by hand); a dedicated
  sync command is a follow-up ticket if it's needed at all.
- Removing the `foreman:fix-attempt-N` discrete-label scheme in favor of
  a single counter label or a free-form integer label. Different change,
  different blast radius.
- Renaming or re-coloring any existing Foreman label. The 12 non-attempt
  labels are unchanged in name, color, and description.
- Auto-detecting "max" from CI duration / project size. The issue
  proposes operator-explicit configuration; auto-tuning is a separate
  feature.
