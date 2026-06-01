# Spec: make Fixer / Worker max-attempt counters configurable per-project (issue #14)

## Goal
Promote the two hardcoded retry budgets (`_MAX_FIX_ATTEMPTS = 3` in
`packages/foreman/src/foreman/roles/fixer.py:94` and `_MAX_IMPL_ATTEMPTS = 3`
in `packages/foreman/src/foreman/roles/worker.py:113`) to per-project config
on `ProjectConfig`. Projects with fast CI can dial up the budget; projects
with slow CI can dial it down. Defaults preserve current behavior (`3` for
both) so existing configs see no change.

Issue: [#14 — Make `_MAX_FIX_ATTEMPTS` and `_MAX_IMPL_ATTEMPTS` configurable per-project](https://github.com/jeffrichley/foreman/issues/14).

## Acceptance criteria
- `ProjectConfig` exposes `max_fix_attempts: int = 3` and
  `max_impl_attempts: int = 3` fields (Pydantic, validated as positive
  ints). A TOML file with `max_fix_attempts = 5` round-trips through
  `load_config` and lands on the resolved `ProjectConfig` instance.
- `foreman.roles.fixer.run_fixer` uses `project.max_fix_attempts` for
  (a) the max-attempts pre-flight gate, (b) the "max … reached" error
  message, (c) the `_LABEL_FAILED` escalation predicate on the final
  attempt, and (d) the value rendered into the LLM user prompt's
  "fix attempt #N of a maximum of M" line.
- `foreman.roles.worker.run_worker` uses `project.max_impl_attempts` in
  the equivalent four places.
- The module-level `_MAX_FIX_ATTEMPTS` / `_MAX_IMPL_ATTEMPTS` constants
  are removed; the single source of truth for the default is the
  `ProjectConfig` field default.
- `foreman.init._FOREMAN_LABELS` becomes a function (or is generated
  from) `_foreman_labels(max_fix_attempts, max_impl_attempts)` that
  emits `foreman:fix-attempt-1..N` and `foreman:impl-attempt-1..M`
  labels sized to the configured maxima. Descriptions read
  `"Foreman: fix cycle attempt K of N"` (the trailing `N` reflects the
  configured max, not a hardcoded 3).
- `InitConfig` grows `max_fix_attempts: int` and `max_impl_attempts: int`
  fields (defaulting to 3). `run_init` threads them into label
  generation and into the written project block.
- `foreman.init._format_project_block` emits `max_fix_attempts = N` and
  `max_impl_attempts = M` lines only when the values differ from `3`
  (mirrors the existing `check_command` omit-when-default rule).
- The `foreman init` CLI command exposes `--max-fix-attempts` and
  `--max-impl-attempts` options (default 3, click `IntRange(min=1)`).
- The packaged `instructions.md.template` gains a short "Attempt
  budgets" section pointing operators at the two knobs.
- All four added behaviors are covered by tests in
  `packages/foreman/tests/test_config.py`,
  `packages/foreman/tests/test_roles_fixer.py`,
  `packages/foreman/tests/test_roles_worker.py`,
  `packages/foreman/tests/test_init.py`, and
  `packages/foreman/tests/test_cli.py`. `just check` passes after the
  changes.

## Approach
The change is small but touches five files because the two counters are
referenced symmetrically in Fixer + Worker + init's label seeding. The
critical move is making the project config the single source of truth and
deleting the module constants — anything else (per-role helpers, computed
properties) adds indirection the issue did not ask for.

**Config field shape.** Add the two fields to `ProjectConfig` in
`packages/foreman/src/foreman/config.py` immediately after the
`check_command` field (same "per-project tuning knob with sensible
default" tier). Use `Field(default=3, ge=1, description=...)`. `ge=1`
prevents the silly-but-possible `max_fix_attempts = 0` config that would
make every Fixer dispatch refuse to run with a confusing "max 0 reached"
message — fail fast at config load instead.

**Fixer rewiring.** In `run_fixer`, after the existing
`project = config.projects[project_name]` line (`fixer.py:386`), bind
`max_fix_attempts = project.max_fix_attempts` and pass it explicitly into
`_build_user_prompt`. The pre-flight gate at `fixer.py:416` reads from
the local, not the module constant. The "max N reached" RuntimeError
message and the `_LABEL_FAILED` predicate at `fixer.py:507` follow.
`_build_user_prompt` gains a `max_attempts: int` parameter so the
`"#{attempt} of a maximum of {_MAX_FIX_ATTEMPTS}"` interpolation reads
from the argument. Delete the `_MAX_FIX_ATTEMPTS` constant at
`fixer.py:94`.

**Worker rewiring.** Symmetric to the Fixer. In `run_worker`, bind
`max_impl_attempts = project.max_impl_attempts` after `project = ...`
(`worker.py:440`) and thread it through `_build_user_prompt`. Replace
the three downstream references (`worker.py:470`, `:472`, `:662`).
Delete the `_MAX_IMPL_ATTEMPTS` constant at `worker.py:113`.

**Label generation.** Convert `_FOREMAN_LABELS` in
`packages/foreman/src/foreman/init.py` from a module-level constant to
the result of `_foreman_labels(max_fix_attempts: int, max_impl_attempts:
int)`. The function returns the same `list[tuple[str, str, str]]`
shape: the 12 fixed state + modifier labels (unchanged), followed by
`foreman:fix-attempt-{k}` for `k in 1..max_fix_attempts`, then
`foreman:impl-attempt-{k}` for `k in 1..max_impl_attempts`.
Descriptions: `f"Foreman: fix cycle attempt {k} of {max_fix_attempts}"`.
`_ensure_labels` calls the function with the resolved maxima from the
`InitConfig`. Tests that import `_FOREMAN_LABELS` (e.g.
`test_init.py:21`) update to call `_foreman_labels(3, 3)` for the
default case.

**InitConfig + CLI.** Add `max_fix_attempts: int = 3` and
`max_impl_attempts: int = 3` to the `InitConfig` dataclass. Add the two
click options to the `init` command in `packages/foreman/src/foreman/cli.py`
mirroring `--check-command` (`cli.py:212-218`): `IntRange(min=1)`, default
`3`, `show_default=True`, with help text pointing at the issue's
motivating examples (fast CI = looser, slow CI = tighter). `_format_project_block`
gains the two parameters and emits each line only when the value
differs from `3`. Module-level `_DEFAULT_MAX_FIX_ATTEMPTS = 3` and
`_DEFAULT_MAX_IMPL_ATTEMPTS = 3` constants pin the "omit when default"
threshold, matching the existing `_DEFAULT_CHECK_COMMAND` pattern at
`init.py:67`.

**Template doc.** Append an "Attempt budgets" section to
`packages/foreman/src/foreman/templates/instructions.md.template`
describing the two knobs and pointing at the config-block syntax. Short
— this is operator-facing reference, not tutorial.

## Sub-requests (topologically sorted)
1. In `packages/foreman/src/foreman/config.py`, add
   `max_fix_attempts: int = Field(default=3, ge=1, description=...)` and
   `max_impl_attempts: int = Field(default=3, ge=1, description=...)` to
   `ProjectConfig`, immediately after the `check_command` field.
2. In `packages/foreman/src/foreman/roles/fixer.py`: delete
   `_MAX_FIX_ATTEMPTS` (line 94); add `max_attempts: int` parameter to
   `_build_user_prompt`; in `run_fixer`, bind
   `max_fix_attempts = project.max_fix_attempts` after `project = ...`
   (line 386) and use it in the pre-flight gate (line 416), the error
   message (line 418), the prompt call site, and the `_LABEL_FAILED`
   predicate (line 507).
3. In `packages/foreman/src/foreman/roles/worker.py`: delete
   `_MAX_IMPL_ATTEMPTS` (line 113); add `max_attempts: int` parameter to
   `_build_user_prompt`; in `run_worker`, bind
   `max_impl_attempts = project.max_impl_attempts` after `project = ...`
   (line 440) and use it in the three downstream references
   (lines 470, 472, 662) and the prompt call site.
4. In `packages/foreman/src/foreman/init.py`: convert `_FOREMAN_LABELS`
   to a function `_foreman_labels(max_fix_attempts: int,
   max_impl_attempts: int) -> list[tuple[str, str, str]]`; add
   `_DEFAULT_MAX_FIX_ATTEMPTS = 3` and `_DEFAULT_MAX_IMPL_ATTEMPTS = 3`
   module constants; add `max_fix_attempts: int = 3` and
   `max_impl_attempts: int = 3` fields to `InitConfig`; thread the
   values from `InitConfig` into `_ensure_labels` (which calls the new
   function) and into `_format_project_block`; update
   `_format_project_block` to emit each line only when non-default.
   Update the `f"{len(_FOREMAN_LABELS)} labels total"` line in
   `_format_summary` to use the resolved label count.
5. In `packages/foreman/src/foreman/cli.py`: add `--max-fix-attempts`
   and `--max-impl-attempts` click options to the `init` command
   (mirroring `--check-command`'s shape), thread them through
   `InitConfig` construction.
6. In `packages/foreman/src/foreman/templates/instructions.md.template`:
   add an "Attempt budgets" section after the "Quality gate" section
   describing the two knobs.
7. Add/update tests:
   - `test_config.py`: round-trip a config with non-default
     `max_fix_attempts` / `max_impl_attempts`; assert `ge=1` rejection
     of `max_fix_attempts = 0`.
   - `test_roles_fixer.py`: parametrize an existing max-gate test to
     show a project with `max_fix_attempts = 5` accepts a 4th attempt
     and rejects a 6th; assert the rendered prompt includes the
     configured max.
   - `test_roles_worker.py`: symmetric test for `max_impl_attempts`.
   - `test_init.py`: `_foreman_labels(5, 4)` returns 12 + 5 + 4 = 21
     labels with descriptions `"... of 5"` / `"... of 4"`; running
     `run_init` with `max_fix_attempts = 5` creates `fix-attempt-1..5`;
     `_format_project_block` omits both fields when both are 3 and
     emits them when non-default. Update existing
     `_format_project_block` and `run_init` call sites to pass the new
     args explicitly (defaults are fine; update tests that pin the
     exact label count).
   - `test_cli.py`: invoke `foreman init` with
     `--max-fix-attempts 5 --max-impl-attempts 4` and assert the
     written config contains both lines; invoke with the defaults and
     assert neither line is present.
8. Run `just check` (lint + typecheck + tests); fix anything red.

## File-level changes
| File | Change |
|---|---|
| `packages/foreman/src/foreman/config.py` | Add `max_fix_attempts` / `max_impl_attempts` fields to `ProjectConfig` with `default=3, ge=1`. |
| `packages/foreman/src/foreman/roles/fixer.py` | Delete `_MAX_FIX_ATTEMPTS`; thread `project.max_fix_attempts` through `run_fixer` (gate, error, prompt, `_LABEL_FAILED` predicate); add `max_attempts` parameter to `_build_user_prompt`. |
| `packages/foreman/src/foreman/roles/worker.py` | Delete `_MAX_IMPL_ATTEMPTS`; thread `project.max_impl_attempts` through `run_worker` (gate, error, prompt, `_LABEL_FAILED` predicate); add `max_attempts` parameter to `_build_user_prompt`. |
| `packages/foreman/src/foreman/init.py` | Convert `_FOREMAN_LABELS` to function `_foreman_labels(...)`; add `_DEFAULT_MAX_*_ATTEMPTS` constants; add `max_fix_attempts` / `max_impl_attempts` to `InitConfig`; thread into `_ensure_labels` and `_format_project_block`; update summary line. |
| `packages/foreman/src/foreman/cli.py` | Add `--max-fix-attempts` and `--max-impl-attempts` click options to `init`. |
| `packages/foreman/src/foreman/templates/instructions.md.template` | Add "Attempt budgets" section. |
| `packages/foreman/tests/test_config.py` | Round-trip + `ge=1` validation tests. |
| `packages/foreman/tests/test_roles_fixer.py` | Config-driven max-attempt behavior + prompt text. |
| `packages/foreman/tests/test_roles_worker.py` | Config-driven max-attempt behavior + prompt text. |
| `packages/foreman/tests/test_init.py` | Update `_FOREMAN_LABELS` import to call `_foreman_labels(3, 3)`; tests for dynamic label generation; `_format_project_block` omit-when-default + emit-when-set. |
| `packages/foreman/tests/test_cli.py` | `foreman init --max-fix-attempts ... --max-impl-attempts ...` writes the lines. |

## Alternatives considered
- **Add a single `max_attempts` field that applies to both Fixer and
  Worker.** Rejected: the issue's motivating example (voice ~10s vs.
  chrona ~3min) is about the Worker's `check_command` cost, not the
  Fixer's. Coupling the two prevents the operator from tuning them
  independently, and `ProjectConfig` already has them as conceptually
  separate counters.
- **Make the constants computed properties on `ProjectConfig`
  (`@property` returning `int`) instead of plain fields.** Rejected:
  adds no value over the Pydantic field default and breaks the TOML
  round-trip / `_format_project_block` symmetry with `check_command`.
- **Env-var overrides (`FOREMAN_MAX_FIX_ATTEMPTS`) mirroring the App-id
  hierarchy.** Rejected: out of scope. The App-id env hierarchy exists
  to inject secrets in CI / Docker; max-attempts is a project tuning
  knob, not a secret. Operators who want per-environment overrides can
  use a different config file via `FOREMAN_CONFIG`.
- **Keep `_FOREMAN_LABELS` static at the 3+3 ceiling and skip dynamic
  generation; let operators manually create extra attempt labels.**
  Rejected: the issue explicitly calls out generating enough labels to
  cover the configured max as part of `foreman init`. Static labels
  would surface as "missing label" errors on the 4th attempt, defeating
  the feature.

## Open questions
None — the issue's "Proposed approach" is concrete and the file shapes
to change are unambiguous after reading the four target modules. The
"3 of 3" description text on existing labels created by older `foreman
init` runs will stay "of 3" on already-onboarded projects; this is
intentional (init is idempotent and never overwrites existing label
descriptions) and the operator can run `foreman label sync` (foreman#11
reserved name) when that lands. Worth a one-line note in the PR body
but not a blocker.

## Out of scope
- Per-issue `foreman:max-attempts:N` label override — the issue
  explicitly defers this.
- Splitting fix-attempt vs. spec-fix vs. impl-fix vs. review into more
  than two counters — the issue explicitly defers this; the existing
  two-counter design already covers the user-visible distinction.
- Backfilling extra labels onto already-initialized repos. `foreman
  init` is idempotent on label creation; operators who raise their max
  must re-run `init` (which only creates missing labels, never updates
  descriptions on existing ones). A future `foreman label sync`
  command can take ownership of forced-update semantics.
- Touching `_count_fix_attempts` / `_count_impl_attempts`. They return
  the max existing counter regardless of the configured ceiling and
  need no change.
- Changing the wire shape of `FixerOutput` / `WorkerOutput` schemas —
  attempt counters live on the issue's labels (audit trail) and on the
  JSONL stats, not on the structured LLM output.
