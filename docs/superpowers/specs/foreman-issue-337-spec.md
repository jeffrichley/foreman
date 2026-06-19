# Spec: add `uv.lock` validity check to `just check` (issue #337)

## Goal
Add a `lock-check` step to the `just check` composite gate so the same class
of malformed-`uv.lock` defect that broke PR #333's first CI run is caught
locally (via the pre-push hook → `just check` chain) before it reaches CI.
Tracks [foreman#337](https://github.com/jeffrichley/foreman/issues/337).

## Acceptance criteria
- [ ] A new `lock-check` recipe exists in `justfile` that validates
  `uv.lock` by invoking `uv lock --check` (no flags beyond `--check`).
  The recipe is one line, mirrors the existing `lint` / `typecheck` /
  `test` shape (single `uv` invocation, no shell glue), and is OS-agnostic
  (no `[unix]` / `[windows]` split needed — `uv lock --check` takes the
  same form on every platform that `uv` supports).
- [ ] The `check` recipe in `justfile` lists `lock-check` as its FIRST
  dependency, before `lint`. Final shape:
  `check: lock-check lint typecheck import-lint test`.
- [ ] `just lock-check` exits zero against the current `uv.lock` (proves
  the recipe is wired correctly and the current repo state is clean).
- [ ] `just check` exits zero against the current `uv.lock`.
- [ ] Manual repro recorded in the PR description: edit `uv.lock` to
  duplicate any `[[package]]` block (e.g. add a second identical block
  for `grimp`), run `just lock-check`, observe non-zero exit and an
  error message naming the offending package. Revert the edit before
  committing. This MUST be shown to land in the PR description as proof
  the gate catches the originating bug shape.
- [ ] `new_failures_count == 0` on the impl PR per the Worker's
  pre-merge gate.
- [ ] No changes to `.github/workflows/ci.yml` (CI already catches this
  via its existing `uv sync --locked --all-packages` step at
  `ci.yml:40`).
- [ ] No changes to `.pre-commit-config.yaml` (the existing `pre-push`
  stage entry `just check` automatically picks up the new step via the
  composite gate; the pre-commit framework does not need to be told
  about the new recipe).

## Approach
The defect is structural: `just check` invokes everything through
`uv run --no-sync` (see `justfile:20-29,38-46`), which means the existing
`.venv` is reused without validating `uv.lock`. CI is the first place a
`uv sync --locked` runs against the lockfile, so a malformed lockfile
slipped past every pre-push gate and only got caught after PR #333 opened.

The fix is to add one more step to the `check` chain — a recipe whose only
job is to ask `uv` itself "is this lockfile valid and up to date?". The
canonical command is `uv lock --check`, which (per `uv lock --help`) does
exactly that and returns non-zero on any failure to parse OR any drift
between `uv.lock` and `pyproject.toml`. Because parsing happens before
the up-to-date check, the duplicate-`[[package]]` shape from issue #337
fails the parse path and the gate rejects it.

**Why `uv lock --check` and not `uv sync --locked --all-packages --dry-run`:**
`uv lock --check` is purpose-built for the question we're asking ("is the
lockfile valid?"), runs without touching the resolver's full graph walk,
and is cheaper. `uv sync --locked --all-packages --dry-run` would also
catch the bug but does extra work (workspace resolution, distribution
selection) we don't need for a parse + drift check. The user's issue body
correctly framed this as an "either / or — verify exact uv subcommand"
choice; this spec resolves it in favor of the cheaper, exact-purpose tool.
The Worker's manual-repro acceptance criterion locks in that the chosen
command actually rejects the originating bug shape; if (against
expectation) `uv lock --check` does NOT catch it during repro, the Worker
falls back to `uv sync --locked --all-packages --dry-run` and notes the
substitution in the PR description.

**Why `lock-check` is FIRST in the chain:** fail-fast. The lockfile gate
runs in well under a second; lint, typecheck, import-lint, and the test
suite are all measurably slower. Catching a malformed lockfile before any
of those run shaves the failure-feedback loop and matches the discipline
captured in the existing recipe ordering (cheap gates first).

**Pattern naming (per `CLAUDE.md` Decision 4):** No GoF pattern fits —
this is straightforward defensive-gate addition to an existing composite
recipe. The Google engineering principle that applies is "make the right
thing easy": pre-push and CI should run the same gates in the same order,
and the existing `justfile` header literally says so
(`justfile:1-4`: "Keep this as the source of truth for local + CI check
commands so developers and agents run the same gates in the same order").
This spec restores that property for one gate that CI was already running
but local was not.

## Sub-requests (topologically sorted)
1. Add a new `lock-check` recipe to `justfile`, placed between the
   `fix` recipe and the `lint` recipe (preserves the
   "cheap gates near the top" ordering already implicit in the file).
   Body: `uv lock --check`. No `--no-sync`, no `uv run` wrapper — this
   is a direct `uv` subcommand, not a Python-package entry point.
2. Modify the `check` recipe in `justfile` from
   `check: lint typecheck import-lint test`
   to
   `check: lock-check lint typecheck import-lint test`.
3. Run `just lock-check` locally. Confirm exit zero against the
   current (clean) `uv.lock`.
4. Run `just check` locally. Confirm exit zero (proves the new step
   composes cleanly into the existing chain).
5. Manually reproduce the bug shape: copy any single `[[package]]`
   block in `uv.lock` and paste it immediately below itself (creating
   a duplicate-package entry). Run `just lock-check`. Confirm non-zero
   exit and an error message that names the duplicated package.
   Record the observed exit code and the first line of stderr in the
   impl PR description.
6. Revert the `uv.lock` edit (`git checkout -- uv.lock`) before
   staging the impl commit.
7. If sub-request 5 returns exit zero (i.e. `uv lock --check` does
   NOT catch the duplicate-package shape), substitute
   `uv sync --locked --all-packages --dry-run` for `uv lock --check`
   in the `lock-check` recipe and re-run sub-request 5. Note the
   substitution and rationale in the impl PR description.
8. Run `just check` from a clean working tree. Confirm exit zero and
   `new_failures_count == 0`.

## File-level changes
| File | Change |
|------|--------|
| `justfile` | Add a `lock-check` recipe (one-liner `uv lock --check`). Prepend `lock-check` to the `check` recipe's dependency list. |

No other files change. CI (`ci.yml`), pre-commit config (`.pre-commit-config.yaml`),
and `uv.lock` itself are explicitly untouched.

## Alternatives considered
- **Use `uv sync --locked --all-packages --dry-run` as the primary
  command.** Rejected as primary because it's heavier than needed (runs
  the full resolver against the workspace, not just the lockfile parser),
  while `uv lock --check` is the documented purpose-built check for
  this question. Kept as the documented fall-back in sub-request 7 if
  repro shows `--check` doesn't reject the duplicate-package shape.
- **Add the check via a pre-commit hook (commit-time) instead of
  pre-push.** Rejected — explicitly out of scope per the issue body:
  "commit-time is too early — devs sometimes commit broken lockfiles
  intentionally mid-merge". Pre-push is the correct gate.
- **Add the check via a standalone `.githooks/pre-push` script instead
  of the `justfile` chain.** Rejected — the repo's actual pre-push gate
  is driven by the `pre-commit` framework's `pre-push` stage (see
  `.pre-commit-config.yaml:25-36`, which calls `just check` and which
  explicitly notes "This replaces the older `.githooks/pre-push` shell
  script"). Wiring into `just check` keeps the single-source-of-truth
  property the `justfile` header promises.
- **Add the check to CI alongside the existing `uv sync --locked
  --all-packages` step.** Rejected — CI already catches this bug class
  (it's literally how PR #333's malformed lockfile was caught). The
  defect is "pre-push doesn't catch what CI catches"; the fix belongs
  in the pre-push chain, not in CI.
- **Auto-fix on detection (regenerate `uv.lock` via `uv lock` and stage
  the change).** Rejected — explicitly out of scope per the issue body:
  "The check should fail loud; resolution is a manual call." A malformed
  lockfile mid-merge often reflects an unresolved upstream-dependency
  conflict the developer needs to make a judgment call about; silently
  regenerating papers over that.
- **Add a regression test (Python `pytest`) that runs `uv lock --check`
  in a subprocess and asserts exit zero, instead of (or in addition to)
  the `justfile` recipe.** Rejected — the `justfile` gate is the
  load-bearing artifact (it's what pre-push runs), and a `pytest`
  shim that runs the same `uv` invocation buys no extra signal while
  adding test-suite latency and a redundant failure surface. The
  acceptance criterion "`just lock-check` exits zero against current
  `uv.lock`" is the test, run by `just check`.

## Open questions
- None. The user issue body already framed the implementation cleanly,
  the command choice is documented in `uv lock --help`, and the
  fall-back path is recorded in sub-request 7 if the primary command
  doesn't behave as expected at repro time. Confidence rationale below.

## Out of scope
- Pre-commit-stage coverage (the issue body excludes this).
- CI changes (the issue body excludes this; CI already catches it).
- Auto-fix on detection (the issue body excludes this).
- Validating `uv.lock` content beyond what `uv` itself validates
  (e.g. no custom TOML linter, no hand-rolled duplicate-package
  detector — defer to `uv`'s own parser).
- Refactoring the existing `check` chain or any other recipe in
  `justfile` — the only change to `check` is prepending one dependency.
- Touching `.pre-commit-config.yaml` — the existing `pre-push` stage
  entry `just check` already inherits the new step.
- Adding a separate Python `pytest` regression test for the gate
  (rejected in Alternatives considered).
- Adding the check to release-please / `release.yml` — those workflows
  are out of the issue's scope.
- Pinning the minimum `uv` version that supports `uv lock --check`.
  The flag has been stable since well before this repo's first commit
  (see `pyproject.toml` for the project's actual uv-version contract
  via `.python-version` + `uv` install instructions in `README.md`);
  pinning would be solving a problem that doesn't exist.
