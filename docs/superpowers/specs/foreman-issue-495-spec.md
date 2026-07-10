# Spec: make codebase ruff-format-clean and add `format-check` gate (issue #495)

## Goal

Close the enforcement gap identified in [#495](https://github.com/jeffrichley/foreman/issues/495): `just check` (and CI) runs `ruff check` but not `ruff format --check`, so files can carry non-canonical formatting indefinitely. When a Worker or Fixer edits such a file and runs the formatter, the whole-file reformat inflates the PR diff with unrelated whitespace churn. The fix is atomic: canonicalize all Python files under `packages/foreman` with a one-shot `ruff format` sweep, then add `ruff format --check` to `just check` so the canonical state is enforced going forward.

## Acceptance criteria

- `uv run --no-sync ruff format --check packages/foreman` exits zero on `main` after this PR merges.
- `just check` includes a `format-check` step that runs `ruff format --check packages/foreman` (fails non-zero if any file is not format-canonical).
- `just check` exits zero end-to-end after both changes are applied.
- The `CONTRIBUTING.md` "What gates check what" table is updated to list `ruff format --check` separately from `ruff check`, with `just format-check` as the local run command.
- No production logic, type annotations, or test assertions are changed — the diff is formatting only (plus `justfile` and `CONTRIBUTING.md` changes).

## Approach

**Pattern (Decision 4):** No GoF pattern fits — this is a straightforward enforcement gap. The applicable Google principle is **"make the right thing easy"**: adding `ruff format --check` to `just check` makes the canonical format the enforced baseline so Workers and Fixers can never again silently accumulate format debt on files they edit. Pattern-fishing here would produce worse code than simply closing the gap.

**Why the gap exists.** `just check` (line 16 of `justfile`) chains `lock-check lint typecheck import-lint test`. The `fix` recipe (line 19–21) already runs both `ruff check --fix` and `ruff format packages/foreman` — demonstrating that the project uses `ruff format`. But `ruff format --check` (the read-only gate equivalent) was never wired into `check`. `ruff format` is behaviour-preserving, so its absence from the gate was a latent quality issue rather than a correctness bug; it surfaced when Worker PR #493 produced a +328/−174 diff for a ~20-line functional change.

**The two-step atomic fix.** These two changes must land together in one PR. Adding the gate before the sweep makes CI immediately red. Sweeping without adding the gate means format drift accumulates again on the next Worker PR.

1. **Format sweep:** run `uv run --no-sync ruff format packages/foreman`. This canonicalises every `.py` file under `packages/foreman/src` and `packages/foreman/tests` according to the existing `[tool.ruff.format]` config in `pyproject.toml` (lines 101–105). The config is already in place; only the enforcement was missing.
2. **Gate addition:** add a `format-check` recipe to `justfile` and append it to the `check` recipe. The recipe mirrors the `lint` recipe shape, replacing `ruff check` with `ruff format --check`.

**Scope of the format sweep.** `pyproject.toml`'s `[tool.ruff.format]` sets `skip-magic-trailing-comma = false` (the default), meaning function signatures and literals with a trailing comma are expanded to multi-line. This is what caused the churn in PR #493: files with trailing-comma-but-single-line signatures were expanded by the formatter when the Worker touched them. After the sweep, those signatures are already expanded, so a subsequent Worker PR that edits one line produces no unrelated reformatting noise.

**`CONTRIBUTING.md` update.** The "What gates check what" table (lines 36–42) currently says "`ruff` | lint + format | `just lint` / `just fix`". `just lint` only runs `ruff check` (lint, not format) and `just fix` applies format (not checks it). This is pre-existing inaccuracy; update the table to correctly describe the two distinct ruff steps after this change.

## Sub-requests (topologically sorted)

1. **Run the format sweep** on `packages/foreman`:

   ```bash
   uv run --no-sync ruff format packages/foreman
   ```

   Stage all changed `.py` files. This is a formatting-only commit; no logic changes.

2. **Add a `format-check` recipe to `justfile`** immediately after the existing `lint` recipe (after line 31):

   ```just
   # Format check (read-only; fails if any file is not ruff-format-canonical)
   format-check:
       uv run --no-sync ruff format --check packages/foreman
   ```

3. **Extend the `check` recipe** in `justfile` (line 16) to include `format-check` between `lint` and `typecheck`:

   Before:
   ```just
   check: lock-check lint typecheck import-lint test
   ```

   After:
   ```just
   check: lock-check lint format-check typecheck import-lint test
   ```

4. **Update `CONTRIBUTING.md`** "What gates check what" table (lines 36–42). Replace the single `ruff` row with two rows that accurately reflect the split:

   Before:
   ```
   | `ruff` | lint + format | `just lint` / `just fix` |
   ```

   After:
   ```
   | `ruff check` | lint (style, unused imports, etc.) | `just lint` / `just fix` |
   | `ruff format --check` | formatting (canonical whitespace / trailing commas) | `just format-check` / `just fix` |
   ```

5. **Run `just check`** and verify it exits zero. All existing tests, mypy, and import-lint must continue to pass unchanged; the new `format-check` step must also pass (the sweep in sub-request 1 ensures it will).

## File-level changes

| File | Change |
|---|---|
| All `.py` files under `packages/foreman/src/` and `packages/foreman/tests/` that are not already format-canonical | Reformatted by `ruff format packages/foreman` (formatting only, no logic changes). Exact file list determined at implementation time by the sweep. |
| `justfile` | Add `format-check` recipe after `lint`; add `format-check` to the `check` chain between `lint` and `typecheck`. |
| `CONTRIBUTING.md` | Update "What gates check what" table: split single `ruff` row into `ruff check` (lint) and `ruff format --check` (formatting) rows with correct local run commands. |

## Alternatives considered

1. **Scope the Worker's format pass to changed hunks only** (e.g., `ruff format --range`, or diffing before/after and reverting unrelated hunks). More complex and fragile — `ruff format` does not expose per-hunk formatting, and a git-diff-scoped approach requires careful scripting. Option 1 from the issue (make main canonical + enforce) largely obviates this: once main is canonical, a Worker editing one function produces no unrelated reformats. Rejected as unnecessary complexity once the canonical-baseline approach is implemented.

2. **Format sweep as a standalone chore PR, gate addition as a follow-up PR.** The issue mentions "dedicated chore PR" for the sweep. Two PRs is viable but riskier: if the sweep merges but the gate PR is delayed, Worker PRs in the window reintroduce format drift on newly-touched files (exactly the problem being fixed). A single atomic PR that does both is simpler and closes the gap with no window. Rejected in favour of the single-PR approach.

3. **Leave `just check` unchanged and rely on Workers running `just fix` before committing.** The root cause is the missing gate, not the Workers' behaviour. Without enforcement, format debt reaccumulates whenever a Worker (or a human contributor) forgets to run `just fix`. Rejected — enforcement is the fix.

## Open questions

None. The affected files, the `ruff format` command, and the `justfile` recipe shape are all clear from the codebase. The format sweep output (exact set of changed files) is determined by running the command; the Worker does not need to enumerate it in advance.

## Out of scope

- Changes to `ruff check` lint rules or the `[tool.ruff.lint]` config in `pyproject.toml`.
- Changes to `[tool.ruff.format]` config (the existing config is correct; the only thing missing was enforcement).
- Formatting files outside `packages/foreman` (e.g., scripts, Docker files — `ruff format` operates on Python files only).
- Any change to production logic, type annotations, or test assertions.
- Worker/Fixer role prompt changes (the gate addition is sufficient; the roles already run `just check` as their quality gate).
