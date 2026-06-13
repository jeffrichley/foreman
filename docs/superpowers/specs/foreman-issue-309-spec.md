# Spec: adopt `import-linter` framework + one starter rule (R1: no test-pollution) — Decision 7 framework PR (issue #309)

## Goal

Add `import-linter` to foreman's dev toolchain as the boundary-enforcement layer Decision 7 of the 2026-06-11 architecture stability plan calls for, with **exactly one** justified starter rule (R1 — production code may not import test modules). This PR is the framework-PR-without-rules-it-doesn't-deserve: it makes the boundary-rule mechanism cheap to extend, lands one concrete rule whose justification is an already-fixed historical bug (foreman#19 test-fixture pollution), and explicitly defers the speculative R0/R2/R3 rules to the PRs that create their respective boundaries (#307 LabelManager, D2 RoleRunner ABC, D3/foreman#301 reachability sweep). See issue [#309](https://github.com/jeffrichley/foreman/issues/309) and the Decision 7 / Decision 8 rationale at `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md:883-907`.

## Acceptance criteria

- `import-linter>=2.0` is added to the `[dependency-groups].dev` array in the workspace-root `pyproject.toml`. Listed alphabetically next to the other dev tools (after `mypy` and before `pre-commit` is the natural alphabetic slot — `i` < `m` < `p` — so the actual final position is after `dev = [` and before `mypy` and `pre-commit`; the Worker picks the exact slot that matches the existing alphabetical order in this block, with `pytest` and `pytest-asyncio` already at the top per their own pin convention).
- A `[tool.importlinter]` block exists in the same workspace-root `pyproject.toml` (NOT a separate `.importlinter` file). Justification: every other quality tool in this repo (`[tool.ruff]`, `[tool.mypy]`, `[tool.vulture]`, `[tool.pytest.ini_options]`) lives in `pyproject.toml`. Consistency with house convention wins over the upstream INI default. The block contains:
  - `root_packages = ["foreman", "tests"]` — both packages must be roots of grimp's import graph so the forbidden contract can resolve `tests` as a real module. With only `foreman` as a root, grimp never visits `tests` and a `from tests import X` line in production code would slip past the rule.
  - `include_external_packages = false` (explicit, even though it is the default) — we are enforcing intra-codebase shape, not policing third-party-SDK leakage (that's Decision 8's Tier 1 audit, OUT of scope per "Out of scope" below).
- Exactly ONE `[[tool.importlinter.contracts]]` block in `pyproject.toml`, with this shape (exact field values are mandatory; the Worker may reword the `name` if it adds clarity, but must keep "R1" in the name for trace-to-decision):
  ```toml
  [[tool.importlinter.contracts]]
  name = "R1: production code does not import from tests"
  type = "forbidden"
  source_modules = ["foreman"]
  forbidden_modules = ["tests"]
  ```
  Rationale baked into the contract name: future operators reading `pyproject.toml` should be able to trace each rule back to its Decision-N source without grepping. "R1" is the trace handle.
- A `justfile` recipe named exactly `import-linter` (matches the issue body's wording, matches the typecheck/lint/test naming style):
  ```just
  # Import-graph boundary enforcement (Decision 7).
  # Config lives at [tool.importlinter] in workspace-root pyproject.toml.
  import-linter:
      uv run --no-sync lint-imports
  ```
  No path argument — `lint-imports` auto-discovers config from the workspace-root `pyproject.toml`. The `uv run --no-sync` prefix matches every other recipe in the file.
- `justfile`'s composite gate line at `justfile:16` changes from `check: lint typecheck test` to `check: lint typecheck import-linter test`. Order matters: `import-linter` runs AFTER `typecheck` (mypy is faster on a clean tree and surfaces more catastrophic shape errors first) and BEFORE `test` (test failures shouldn't mask boundary failures). The new order is `lint → typecheck → import-linter → test`.
- `.github/workflows/ci.yml` is **NOT** modified. CI's `check` step at `.github/workflows/ci.yml:37` already runs `just check`, which now transitively runs `lint-imports`. Per the issue body's "CI workflow inherits the new gate transparently" criterion, no per-step YAML changes are required. The Worker MUST verify CI is green on the PR before flipping the impl PR to ready-for-review (`gh pr checks` reads as `pass` on the `check` job).
- `lock` regeneration: after editing `pyproject.toml`'s `[dependency-groups].dev`, the Worker runs `uv lock` (or equivalent) so `uv.lock` reflects the new transitive set (notably `grimp`, which import-linter pulls in). The lockfile must be committed alongside the `pyproject.toml` change. The PR CI step `uv sync --locked --all-packages` will fail otherwise.
- **R1 baseline check (passes on current main):** before adding R1, the Worker runs `grep -rn '^from tests\|^import tests\|^from foreman\.tests\|^import foreman\.tests' packages/foreman/src` and confirms ZERO matches. (Pre-verified during spec drafting; the only matches in src today are inside markdown prompt files like `prompts/superpowers/test-driven-development.md` and `prompts/reviewer_impl.md`, neither of which is Python source — `import-linter` will not see them.) Post-config, `just import-linter` exits 0 on a clean tree.
- **R1 false-violation check (rule actually bites):** the Worker temporarily inserts `from tests.conftest import _FOREMAN_ENV_VARS_TO_SCRUB  # noqa: F401  TEMP R1 PROBE — DO NOT COMMIT` near the top of `packages/foreman/src/foreman/roles/worker.py` (the file the issue body names), runs `just import-linter`, and records in the impl PR body:
  1. The exit code is non-zero.
  2. The output names contract "R1" (or whatever the contract `name` was set to) AND names the violating module `foreman.roles.worker` AND the forbidden module `tests` or `tests.conftest`.
  3. The probe line is reverted (`git diff packages/foreman/src/foreman/roles/worker.py` returns empty) BEFORE any commit. Make this a single throwaway run; the probe lives in the working tree for ~10 seconds.
- A new section titled `## Import-graph boundaries (`import-linter`)` is added to `docs/RUNBOOK.md`, AFTER `## Pre-commit hooks` (line 146 anchor) and BEFORE `## What survives what` (line 190 anchor). Content covers exactly three things (no more):
  1. How to run locally: `just import-linter` (full gate) OR `uv run --no-sync lint-imports` (direct).
  2. How to interpret failure output: import-linter prints `Contracts: 1 broken / 0 kept`; the failure block names the contract, the source module that violated it, and the forbidden chain. Decode example: "If you see `foreman.roles.worker -> tests.conftest`, a production module gained a test-tree import — either move the helper into `foreman/*` proper or remove the import."
  3. Pointer to "how to add a rule": one line saying "see `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md` Decision 7 § How to add a rule." The full procedure lives in the plan, not in the RUNBOOK, so the procedure and its rationale stay co-located.
- A new subsection titled `#### How to add a rule (Decision 7 procedure)` is added to `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md`, inside Decision 7's section (currently spans lines 883–907), inserted AFTER the existing "Sequencing dependency" bullet at line 906 and BEFORE the "Next-step ticket" bullet at line 907. Content is a 5–8 line procedural list (NOT a full essay):
  1. A new rule is added only when a Decision in this plan (or a successor plan) names the boundary it enforces. Speculative rules are rejected at review time.
  2. The rule's `name` field MUST include the "R<N>" trace handle so future readers can map rule → decision.
  3. The rule's docstring/comment cites the Decision number AND the originating ticket (e.g. `# R0 — LabelManager owns label-writes. Decision 1 / foreman#307.`).
  4. Add the rule in the SAME PR that lands the code-level boundary, not a follow-up PR. Reason: the rule is meaningless until the code it constrains exists; landing them together prevents drift.
  5. If a future refactor needs to RELAX a rule, the PR description must justify the relaxation against the originating Decision — the rule does not get silently weakened.
  6. Last line of the subsection: "See `pyproject.toml` `[tool.importlinter]` for the current rule set."
- A new section titled `## Architectural boundaries` (or merged into an existing "Quality gate" section if the README already has one) is added to `README.md`, AFTER the existing "Working in this repo" section. Content is exactly 2–4 lines: a one-liner that foreman uses `import-linter` to enforce architectural boundaries, that `just check` runs it, and a pointer to RUNBOOK's "Import-graph boundaries" section for details. The README MUST NOT duplicate the full procedure — README's job is "this exists, find details here."
- `just check` exits 0 on the impl worktree: ruff clean, mypy clean, `lint-imports` clean (contract R1 kept), full pytest suite green. The pre-edit test count is recorded in the PR body and confirmed unchanged post-edit (this PR adds no production code and no tests; the count must be identical).
- The impl PR title uses `feat(ci):` conventional-commit prefix — `import-linter` is a new CI gate, not a refactor or docs-only change. The pr-title-lint workflow accepts `feat` per `CLAUDE.md:36`. Suggested title: `feat(ci): adopt import-linter with R1 no-test-pollution rule (D7)`. Subject MUST NOT start with an uppercase letter per `CLAUDE.md:35`. The PR body references issue #309 plainly — NO closing-keyword references (per foreman#63; impl-PR-merge closes the issue via the daemon's close-out path, not via GitHub auto-close).

## Approach

Per CLAUDE.md's Decision-4 calibrated lens: **no GoF pattern applies; the relevant Google principle is "make the right thing easy"** (SRP-adjacent but not SRP proper). The mechanic is to drop the cost of adding the NEXT boundary rule to ~zero — config in one file, recipe in one file, gate threaded once — so that decisions which create boundaries (Decision 5's provider merge, Decision 2's RoleRunner ABC, #307's LabelManager) can add their rule as a one-liner in the same PR. The framework-PR-without-rules-it-doesn't-deserve discipline IS the "make the right thing easy" application: keep the rule set scannable so adding a rule stays cheap; don't pre-populate with hypotheticals because that's what makes the rule set unreadable.

Six structural moves, each independently verifiable:

1. **Dev dep**: add `import-linter>=2.0` to the workspace-root `pyproject.toml`'s `[dependency-groups].dev`. The version pin matches the issue body's spec; import-linter 2.x is the current stable line and uses `grimp` 3.x transitively (no need to pin grimp explicitly).
2. **Config block**: `[tool.importlinter]` with `root_packages = ["foreman", "tests"]` + one `[[tool.importlinter.contracts]]` block for R1. Both `foreman` AND `tests` must be root packages — without `tests` in `root_packages`, grimp's import graph never visits the `tests` modules, and a `from tests import X` line in src would be silently un-checked. This is the non-obvious gotcha that gets boundary-CI wrong on first attempts.
3. **Justfile recipe** (`import-linter`) + **gate insertion** (`check: lint typecheck import-linter test`). Order is `lint → typecheck → import-linter → test` so faster gates fail first; the test suite isn't bypassed if the boundary fails, but typecheck failures still surface first when both fail.
4. **`uv.lock` regen**: `pyproject.toml` change requires `uv lock` so the lockfile picks up the new transitive deps. Without this, CI's `uv sync --locked --all-packages` fails before it gets to running `just check`.
5. **Self-test** (R1 actually bites): the Worker injects a single throwaway `from tests.conftest import _FOREMAN_ENV_VARS_TO_SCRUB` line into `roles/worker.py`, observes failure with named contract + named import, then reverts BEFORE committing. The probe lives in the working tree for ~10 seconds. This is the only way to verify the rule fires; otherwise we're trusting import-linter to do the right thing on data it never saw. The probe target is `_FOREMAN_ENV_VARS_TO_SCRUB` specifically because it's a real symbol in `tests/conftest.py:41` — the import resolves successfully (so `mypy` and `ruff` wouldn't catch it; only `import-linter` would). If the Worker chooses a different symbol, it must be one that genuinely exists in `tests/conftest.py` or another tests-tree module so the import is real, not a fake error.
6. **Docs**: three doc surfaces, each scoped narrowly. RUNBOOK gets "how to run + how to read failures." The plan file's Decision 7 section gets the "how to add a rule" procedure (where the rule-source discipline already lives). README gets the 2-line surfacing pointer. No doc surface duplicates another's content — each is a single-purpose pointer.

**Choice of pyproject.toml over `.importlinter`:** every other quality tool in foreman lives in pyproject.toml (`[tool.ruff]`, `[tool.mypy]`, `[tool.vulture]`, `[tool.pytest.ini_options]`). Consistency with house convention beats import-linter's documented INI default. The `[tool.importlinter]` syntax is stable, supported, and documented at https://import-linter.readthedocs.io.

**Why a forbidden contract and not a layered contract:** R1 is a single-direction prohibition (production must not reach into tests). A `layered` contract would imply hierarchy (foreman is a layer "above" tests, tests is a layer "below"), which is misleading — tests imports foreman freely, but foreman never imports tests. `forbidden` is the structurally honest type.

**Why include `tests` as a root_package even though we never want production to reach it:** grimp's import-graph walker visits modules transitively from the declared roots. If `tests` is NOT a root, the contract's `forbidden_modules = ["tests"]` reference would fail to resolve (import-linter errors out with "module not found") OR worse, would silently pass because the graph never visits it. This is the single most common configuration error reported in the import-linter issue tracker. Both roots are explicit; the contract scopes the source side to `foreman` only.

**Anti-scope discipline (the load-bearing piece):** the issue body and Decision 7 BOTH name the "only when a decision creates the boundary" discipline as the rule-source guardrail. This spec respects it literally: ONE rule, R1, justified by an already-fixed-but-could-regress bug (foreman#19 test-fixture pollution at the pre-push hook). The Worker MUST NOT speculatively add R0 / R2 / R3 — even if they look easy and the LLM thinks "well, while I'm here." Per the issue body's Out of scope, those land with #307 / D2 / D3/foreman#301 respectively. The whole point of this PR is to make THEIR additions cheap, not to do their work for them.

## Sub-requests (topologically sorted)

1. **Add `import-linter>=2.0`** to the workspace-root `pyproject.toml`'s `[dependency-groups].dev` array. Match the existing alphabetical / category ordering in the block. Comment the entry with a single line: `# Import-graph boundary enforcement. Decision 7 of the 2026-06-11 architecture stability plan.`

2. **Run `uv lock`** to regenerate `uv.lock` with the new transitive dep set. `uv.lock` MUST be committed in the same commit as the `pyproject.toml` change so CI's `uv sync --locked --all-packages` step succeeds.

3. **Add the `[tool.importlinter]` block** at the END of the workspace-root `pyproject.toml`, after the `[tool.pytest.ini_options]` block. Two top-level fields: `root_packages = ["foreman", "tests"]` and `include_external_packages = false`. Then a single `[[tool.importlinter.contracts]]` block with the exact field shape named in the Acceptance criteria. Above the contract block, a 3-line `#`-style comment: `# R1: production code (foreman.*) must not import from the test tree.\n# Justification: foreman#19 (2026-06-01) — test-fixture pollution\n# at pre-push hook. Decision 7 of 2026-06-11 architecture stability plan.`

4. **Add the `import-linter` recipe** to `justfile`, AFTER the existing `typecheck:` recipe and BEFORE the existing `test:` recipe. Two lines: a 1-line `#` comment naming Decision 7, then the recipe body `uv run --no-sync lint-imports`.

5. **Edit `justfile:16`** — change the `check:` composite gate line from `check: lint typecheck test` to `check: lint typecheck import-linter test`. One line, one word inserted.

6. **Baseline-clean check**: from the worktree root, run `just import-linter`. Expected output: `Contracts: 1 kept / 0 broken`, exit code 0. If the run fails, STOP and investigate — there should be ZERO existing src→tests imports in the codebase (pre-verified during spec drafting). If a violation surfaces, escalate to `foreman:needs-help` rather than weakening the rule.

7. **R1-bites verification** (transient): edit `packages/foreman/src/foreman/roles/worker.py` to add the line `from tests.conftest import _FOREMAN_ENV_VARS_TO_SCRUB  # noqa: F401  TEMP R1 PROBE — DO NOT COMMIT` near the top of the file (after the existing `from __future__` line if present, or at the first import block otherwise). Run `just import-linter`. Capture the failure output. Record in the PR body draft:
   - exit code (must be non-zero)
   - the contract name as printed (must include "R1")
   - the violating chain (must include `foreman.roles.worker` and `tests.conftest`)
   Then `git checkout -- packages/foreman/src/foreman/roles/worker.py` to revert. Confirm `git diff packages/foreman/src/foreman/roles/worker.py` returns empty BEFORE running any further steps.

8. **Add the `## Import-graph boundaries (`import-linter`)` section** to `docs/RUNBOOK.md`. Insert AFTER the existing `## Pre-commit hooks (one-time setup per clone)` section (after its closing `---` separator at line 188) and BEFORE the `## What survives what` section at line 190. Content per the Acceptance criteria spec (run locally, interpret failures, pointer to the plan's "How to add a rule" subsection).

9. **Add the `#### How to add a rule (Decision 7 procedure)` subsection** to `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md`. Insert INSIDE Decision 7's section (lines 883–907), AFTER the `- **Sequencing dependency:**` bullet at line 906 and BEFORE the `- **Next-step ticket:**` bullet at line 907. Six numbered points per the Acceptance criteria spec.

10. **Add the `## Architectural boundaries` section** to `README.md`. Insert AFTER the existing `## Working in this repo` section (lines 39-42), at the end of the file. 2–4 lines of content per the Acceptance criteria spec — pointer-only, no procedure duplication.

11. **Run `just check`**: ruff clean, mypy clean, lint-imports clean (R1 kept), full pytest suite green. Record the pre-edit and post-edit test counts in the PR body draft; they MUST match (no tests added/deleted by this PR).

12. **Open the impl PR** with title `feat(ci): adopt import-linter with R1 no-test-pollution rule (D7)`. Body summary cites #309 plainly (NO closing-keyword shapes per foreman#63). Include the R1-bites verification artifacts from sub-request 7 in the PR body so the Reviewer can verify the rule actually fires without re-running the probe.

## File-level changes

| File | Change |
| --- | --- |
| `pyproject.toml` | Add `import-linter>=2.0` to `[dependency-groups].dev` (alphabetical slot). Add `[tool.importlinter]` block at file end with `root_packages = ["foreman", "tests"]` + `include_external_packages = false`. Add ONE `[[tool.importlinter.contracts]]` block for R1 (forbidden, source=foreman, forbidden=tests). |
| `uv.lock` | Regenerated by `uv lock` to pick up `import-linter` + transitive `grimp` deps. Committed alongside pyproject.toml. |
| `justfile` | Add `import-linter:` recipe (2 lines including comment). Change `check: lint typecheck test` to `check: lint typecheck import-linter test`. |
| `docs/RUNBOOK.md` | Add new `## Import-graph boundaries (`import-linter`)` section between the existing `## Pre-commit hooks` and `## What survives what` sections. ~15 lines. |
| `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md` | Insert `#### How to add a rule (Decision 7 procedure)` subsection inside Decision 7's section, between the "Sequencing dependency" and "Next-step ticket" bullets. ~10 lines. |
| `README.md` | Add 2–4 line `## Architectural boundaries` section at end of file. Pointer-only; no procedure. |

No expected changes to:

- `.github/workflows/ci.yml` — `just check` invocation at `.github/workflows/ci.yml:37` transparently inherits the new gate. Per the issue body's explicit criterion + the Acceptance criteria above.
- `.pre-commit-config.yaml` — per "Out of scope," pre-commit integration is explicitly deferred to a follow-up PR after local-dev ergonomics calibration.
- `packages/foreman/pyproject.toml` (the inner package) — `import-linter` is a workspace-level dev tool that lives in the workspace root, matching the placement of `ruff`, `mypy`, `vulture`, `pre-commit`. Inner-package pyproject stays untouched.
- `packages/foreman/src/foreman/**` — this PR adds no production code. The transient R1-bites probe in sub-request 7 is reverted before commit; the impl PR contains ZERO src/* edits.
- `packages/foreman/tests/**` — this PR adds no tests. The `tests/__init__.py` files (0 bytes, already present at `tests/`, `tests/providers/`, `tests/reconciler/`) are sufficient for grimp to treat them as packages. No fixture changes required.
- `vulture` / `ruff` / `mypy` config — `import-linter` operates at the import-graph level; the others operate at AST/type level. Per the issue body's "Out of scope," they coexist. No tool replacement.

## Alternatives considered

- **Land R1 as part of a multi-rule batch (R1 + R0 + R2 + R3 in one PR).** Tempting because the issue references all four rule shapes, the framework cost is paid once, and the "build the boundary CI" energy is fresh. Rejected because it directly violates Decision 7's load-bearing rule-set discipline: "only when a decision creates the boundary." R0 lives with #307 because the LabelManager code doesn't exist yet; R2 lives with the RoleRunner ABC PR (Decision 2) because there's no role-runner seam to enforce yet; R3 lives with the foreman#301 reachability sweep because there's no reachability tool emitting the dead-island module list yet. Pre-populating defeats the whole "small rule set, scannable at a glance" guardrail.

- **Use `.importlinter` (INI) instead of `[tool.importlinter]` in pyproject.toml.** Smaller diff (one new file instead of editing pyproject), matches import-linter's documented default, and is the shape used in import-linter's own examples. Rejected because every other quality tool in this repo lives in pyproject.toml (`[tool.ruff]`, `[tool.mypy]`, `[tool.vulture]`, `[tool.pytest.ini_options]`). Adding a sibling `.importlinter` file at the workspace root would be the ONLY top-level config file outside pyproject.toml — a smell that surfaces at every future "where does X config live?" question. House convention wins.

- **Skip the R1-bites verification probe (sub-request 7).** Smaller PR, no working-tree edits during impl. Rejected because the only way to verify the rule fires correctly is to make it fire. A passing-on-clean-tree `lint-imports` run proves only that NO existing code violates the rule — it does NOT prove the rule would catch a future violation. The probe is the difference between "the framework is installed" and "the framework demonstrably works." Cost is ~10 seconds of working-tree state.

- **Use a `layered` contract instead of `forbidden`.** A layered contract declaring `tests` as a layer "below" `foreman` would enforce the same one-directional prohibition. Rejected because `layered` carries hierarchy semantics — it implies tests is a lower layer that foreman is built atop. That's structurally wrong: tests imports foreman freely (which is correct) but tests is not "lower than" foreman in any architectural sense. `forbidden` is the structurally honest type and the import-linter docs explicitly call this out as the right choice for one-direction prohibitions.

- **Make `root_packages = ["foreman"]` only, and use a `forbidden_modules = ["foreman.tests"]` (or wildcard) trick.** The issue body itself uses the phrase "`foreman.tests`" which suggests this shape. Rejected after verification: there is no `foreman.tests` module — the test tree is at `packages/foreman/tests/`, NOT `packages/foreman/src/foreman/tests/`, so `tests` is its own top-level package importable as `tests.*`, not as `foreman.tests`. Importing `from foreman.tests import X` would fail with `ModuleNotFoundError` today; only `from tests.conftest import X` resolves. Setting `root_packages = ["foreman"]` would leave the `tests` package unvisited by grimp and the contract would either error at load time or silently no-op. Adding `tests` as a second root is the correct fix.

- **Add `import-linter` to the pre-commit hook in the same PR.** Issue body explicitly defers this to a follow-up, but the alternative is worth naming so future readers see it was considered. Rejected per the issue body's reasoning: pre-commit additions need separate calibration on local-dev ergonomics (Wren has historically been sensitive to slow pre-commit gates blocking flow). The `just check` integration is sufficient because the pre-push hook ALREADY runs `just check` (per `docs/RUNBOOK.md:153`), so `import-linter` runs at push time anyway. Pre-commit (at commit time) is the optional faster-feedback layer.

## Open questions

(None. The investigation closed every named question:

- `import-linter` version: pinned to `>=2.0` per the issue body. Verified that import-linter 2.x is the current stable line (https://import-linter.readthedocs.io references 2.x; pypi shows 2.0+ as the supported track).
- Config location: pyproject.toml per house convention. Verified all four other quality tools (`ruff`, `mypy`, `vulture`, `pytest`) live there.
- Both `foreman` AND `tests` must be root_packages for the contract to resolve. Verified by inspection of `tests/__init__.py` (exists, 0 bytes), `tests/providers/__init__.py` (exists, 0 bytes), `tests/reconciler/__init__.py` (exists, 0 bytes) — `tests` IS a real Python package today; grimp can walk it.
- R1 passes on current main. Verified: `grep -rn '^from tests\|^import tests\|^from foreman\.tests\|^import foreman\.tests' packages/foreman/src` returns ZERO Python-source matches. (The 3 matches surfaced by a broader grep are inside markdown prompt files, which `import-linter` does not parse.)
- The R1-bites probe target (`_FOREMAN_ENV_VARS_TO_SCRUB` in `tests/conftest.py:41`) is a real symbol. Verified by reading conftest.py during spec drafting.
- PR-title-lint accepts `feat(ci):` — verified per `CLAUDE.md:36` which lists `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert` as allowed types. `feat(ci)` is `feat` with `(ci)` scope, which the regex accepts.
- The issue body uses the phrase "`foreman.tests`" but the actual import path is `tests` — flagged in the Alternatives section above. The Worker uses `tests` (not `foreman.tests`) in the `forbidden_modules` field.)

## Out of scope

- **Speculative rules R0 (LabelManager), R2 (RoleRunner), R3 (dead-island).** Each lands with the PR that creates its respective boundary: R0 with #307, R2 with the RoleRunner ABC PR (Decision 2), R3 with the foreman#301 reachability sweep. Adding ANY of them in this PR violates Decision 7's "only when a decision creates the boundary" discipline and undoes the whole point of the framework PR.
- **Decision 8's Tier 1 third-party-SDK rules** (`anthropic`-in-adapters-only, `github`-in-identity/auth/git_hosts/daemon_host-only, etc.). Those are explicit Phase-2 work per Decision 8 (lines 909–937 of the plan); they land after the library-boundary audit produces the tier table. They are not in scope for the framework PR.
- **`vulture` / `ruff` / `mypy` replacement or merge.** `import-linter` is import-graph-level; the others are AST/type-level. Different layers; they coexist. Per issue body's Out of scope.
- **Direct integration with `tach` or `grimp`** beyond what `import-linter` pulls in transitively. `grimp` 3.x is a transitive dep of import-linter 2.x; no separate pin. Per issue body's Out of scope.
- **Adding `import-linter` to `.pre-commit-config.yaml`.** Deferred to a follow-up PR per the issue body's "Out of scope." Pre-commit additions need separate calibration on local-dev ergonomics.
- **Enforcing `import-linter` on `packages/foreman/tests`.** Per the issue body, tests legitimately reach across boundaries for fixture setup. The contract's `source_modules = ["foreman"]` scopes enforcement to src only; `tests` is in `root_packages` so the GRAPH includes it, but no contract constrains its own imports.
- **Operator-facing migration documentation / CHANGELOG copy beyond what `release-please` autogenerates.** The `feat(ci):` conventional-commit prefix gives release-please a CHANGELOG line; no separate doc-update PR.
- **A `vulture_whitelist.py`-style allowlist file for import-linter.** Not needed: import-linter doesn't false-positive on dynamic imports the way vulture does on dynamic attribute access. If a future rule genuinely needs an exception, import-linter's `ignore_imports` directive (per-contract) is the right surface; this PR doesn't need it.

## References

- foreman#309 — this ticket. Names the framework adoption + R1 scope.
- `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md:883-907` — Decision 7 source. The "How to add a rule" subsection lands inside this section.
- `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md:909-937` — Decision 8 source. Tier-1 library boundary audit is the planned successor work that will use this framework.
- foreman#307 — sibling ticket (LabelManager). Its R0 contract lands with #307's PR, not this one.
- foreman#301 — sibling ticket (D3 reachability sweep). Its R3 contract lands with #301's PR, not this one.
- foreman#19 — historical test-fixture pollution bug. Documented in `packages/foreman/tests/conftest.py:24`. The justification for R1: this bug shape must not regress.
- foreman#63 — issue close-out gating. Rationale for the no-closing-keyword constraint on the impl PR body.
- import-linter docs — https://import-linter.readthedocs.io — for contract types reference (forbidden, layered, independence).
- Source pointers used by this spec:
  - `pyproject.toml` `[dependency-groups].dev` (~lines 13-26) — where `import-linter>=2.0` is added.
  - `pyproject.toml` end-of-file — where `[tool.importlinter]` block lands, after `[tool.pytest.ini_options]`.
  - `justfile:14-20` — where the `check:` composite gate lives; line 16 changes word-for-word.
  - `justfile:24-35` — where the new `import-linter:` recipe is inserted, between `typecheck:` and `test:`.
  - `.github/workflows/ci.yml:37` — `just check` invocation; transparently inherits the new gate.
  - `docs/RUNBOOK.md:146-188` — Pre-commit hooks section; the new Import-graph boundaries section lands immediately after.
  - `docs/RUNBOOK.md:190` — What survives what section; the new section lands immediately before.
  - `README.md:39-42` — Working in this repo section; the new Architectural boundaries section lands immediately after.
  - `packages/foreman/tests/conftest.py:41` — `_FOREMAN_ENV_VARS_TO_SCRUB` symbol used as the R1-bites probe target.
  - `packages/foreman/src/foreman/roles/worker.py` — where the transient R1-bites probe is inserted and reverted in sub-request 7.
