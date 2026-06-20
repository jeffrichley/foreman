# Spec: port pytest hardening stack from agent_core (issue #336)

## Goal
Port agent_core PR #192's 7-plugin pytest hardening stack (pytest-randomly,
pytest-cov, diff-cover, pytest-clarity, pytest-timeout, pytest-xdist,
hypothesis) into foreman so the same class of fixture-pollution / hang /
silent-test-deletion bugs that motivated agent_core's stack are caught
locally (`just check`) and on CI for foreman. Tracks
[foreman#336](https://github.com/jeffrichley/foreman/issues/336).

Port the agent_core pattern; do not reinvent. The Worker reads agent_core
PR #192 (commits `2679c9a`, `7bb79c2`, `4cbde70`, `16a6257`, `ec15803`,
`7fbbbbd`, `ef4e9d3`, `2e9b06c`, `18f1360`, `9055d0a`) as the canonical
reference and adapts file paths / coverage thresholds to foreman's actual
measurements.

## Acceptance criteria
- [ ] `pyproject.toml`'s `[dependency-groups].dev` block lists all 7 new
  test-only deps with the minimum-version pins from the issue body
  (`pytest-randomly>=3.15`, `pytest-cov>=5.0`, `diff-cover>=9.0`,
  `pytest-clarity>=1.0`, `pytest-timeout>=2.3`, `pytest-xdist>=3.6`,
  `hypothesis>=6.100`). The block is alphabetically grouped where the
  existing block already is — do not reorder unrelated entries.
- [ ] `pyproject.toml`'s `[tool.pytest.ini_options].addopts` is extended
  with the new flags: `--cov`, `--cov-branch`, `--cov-report=term-missing`,
  `--cov-report=xml:coverage.xml`, `--cov-fail-under=<MEASURED_BASELINE>`,
  `-n auto`, `--dist=loadscope`. The existing `--import-mode=importlib`
  flag is preserved (do not remove it). `<MEASURED_BASELINE>` is set
  empirically per sub-request 4 below — NOT copied from agent_core's
  number.
- [ ] `pyproject.toml` gains `[tool.pytest.ini_options].timeout = 60` and
  `timeout_method = "thread"`. (`thread` not `signal` — `signal` does not
  work with `pytest-xdist`'s worker processes per `pytest-timeout` docs.)
- [ ] `pyproject.toml` gains a `[tool.coverage.run]` block configured with
  `source = ["packages/foreman/src"]`, `branch = true`, and an `omit`
  list covering at minimum `*/.venv/*`, `*/__pycache__/*`,
  `*/tests/*`. Add `packages/foreman/src/foreman/v4/__main__.py` and any
  other no-cover `__main__` shims only if they're actually present in
  the tree at port time (do not invent entries).
- [ ] `pyproject.toml` gains a `[tool.coverage.report]` block with
  `show_missing = true` and `skip_covered = false`. (Keeps the
  term-missing report informative when iterating locally.)
- [ ] `just check` runs the full suite with random order, parallel
  execution (`-n auto`), 60s per-test timeout, and coverage gate,
  exiting zero on the current tree. The `test` recipe in `justfile` is
  unchanged — all wiring lives in `[tool.pytest.ini_options]`.
- [ ] `.github/workflows/ci.yml` gains a NEW step after the existing
  `just check` step that runs:
  `uv run --no-sync diff-cover coverage.xml --compare-branch=origin/${{ github.base_ref }} --fail-under=80`
  gated on `if: github.event_name == 'pull_request'`. The step's name is
  explicit, e.g. `- name: Patch coverage gate (diff-cover)`. The
  `uv cache prune --ci` step remains last.
- [ ] `CLAUDE.md` gains a new top-level section `## Running tests` that
  documents: (a) `just check` runs the full gate (random order, parallel,
  cov gate, 60s timeout); (b) `--no-cov` to skip cov locally for tight
  loops; (c) `--randomly-seed=<N>` to reproduce a specific order;
  (d) `-p no:randomly` to disable randomization entirely; (e) the cov
  gate floor is `<MEASURED_BASELINE>` and is ratcheted in follow-up
  tickets, not in this PR.
- [ ] No production-code dependencies added. All 7 deps are in
  `[dependency-groups].dev` (the existing block where `pytest`,
  `ruff`, `mypy` live). The `[project].dependencies` lists in each
  member package under `packages/*/pyproject.toml` are NOT touched.
- [ ] `uv.lock` is regenerated and committed (the lock-check gate
  added in foreman#337 will reject a drifted lockfile, so the impl
  PR must commit the regenerated lock).
- [ ] `just check` exits zero on the impl PR's branch.
- [ ] The diff-cover step exits zero on the impl PR (impl PR's own
  patch — the 7-plugin wiring — has trivial patch coverage because
  it's config; diff-cover's `--fail-under=80` should still be
  satisfied. If not, the Worker records the exact value and
  documents the reason in the PR description.)
- [ ] `new_failures_count == 0` on the impl PR per the Worker's
  pre-merge gate.

## Approach

The defect class is structural and well-attested in foreman's own
history: foreman#19 was a fixture-pollution bug where ordering masked
a test that leaked state; the v3→v4 substrate cutover (Phase 9) deleted
11 test files with no coverage gate to flag the regression; the
stability-sprint sleepers were bugs that static lint+typecheck didn't
catch but dynamic test-execution would have if randomized / timed /
covered. agent_core PR #192 is a same-workspace-shape (uv + just + ruff
+ mypy + pytest, Python 3.12, Linux-only CI) port target that already
solved these for that repo. Re-derive the same wiring here; do not
re-design the stack.

**Pattern naming (per CLAUDE.md Decision 4):** No GoF pattern fits —
this is straightforward tooling configuration plus a CI gate. The
Google engineering principle that applies is "make the right thing
easy": `just check` is the single quality command surface (`justfile`
header lines 1-4: "Keep this as the source of truth for local + CI
check commands so developers and agents run the same gates in the
same order"). The 7-plugin wiring extends that surface so the dynamic
checks (random order, timeout, parallel, coverage) are part of "the
right thing" instead of being opt-in tooling each developer applies
ad-hoc.

**Why `[tool.pytest.ini_options].addopts` and not a separate justfile
recipe per plugin:** the existing justfile uses `uv run --no-sync
pytest` with no flags (line 52). Putting the plugin flags in
`addopts` means every invocation of `pytest` — `just test`, direct
`uv run pytest`, IDE test runners — picks them up uniformly. Putting
them in `justfile` would split the configuration across two surfaces
and let direct `pytest` invocations silently skip the gate.

**Why `timeout_method = "thread"` and not `"signal"`:** the
pytest-timeout README documents that `signal` (the default) does not
work under pytest-xdist's worker processes because the signal is
delivered to the controller, not the worker. `thread` works in both
serial and xdist modes. agent_core PR #192 uses `thread` for this
reason; copy that decision.

**Why measure the cov-fail-under threshold and not hardcode 85:**
the issue body explicitly says "set `--cov-fail-under` at whatever
the current baseline measures minus ~4% (don't ship a gate that's
red on day one)". The 85 in the issue's plugin table is agent_core's
number — foreman's source tree is smaller and the test surface
differs (108 test files, 1029 collected tests as of this spec). The
Worker runs `uv run --no-sync pytest --cov=packages/foreman/src
--cov-branch --no-header -q` against the current tree, records the
TOTAL percentage line, subtracts ~4 (rounded down), and uses that
as `<MEASURED_BASELINE>` in addopts. The PR description records the
measured value + the gate value.

**Why `diff-cover` is CI-only and PR-only:** the existing CI matrix
is Linux-only (Windows runner removed 2026-06-05 per docker-runtime
design; see `ci.yml:22-26`), so no OS gating is needed beyond `if:
github.event_name == 'pull_request'`. diff-cover compares `HEAD`
against the PR base — on a `push: branches: [main]` event the
comparison branch makes no sense, and on `merge_group` it's also a
no-op against the merge-queue ref. Gating on `pull_request` mirrors
the agent_core pattern exactly.

**Why omit `*/tests/*` from coverage.run:** tests pollute the
coverage number with their own per-test scaffolding. agent_core's
omit pattern excludes them; foreman should match. The `source =
["packages/foreman/src"]` setting already restricts measurement to
production code, but the omit-list is belt-and-suspenders so
`coverage report` doesn't mistakenly include a test that lives
elsewhere.

**Why no changes to ruff / mypy / import-linter / vulture wiring:**
the issue explicitly excludes these in "Out of scope" — the dynamic
checks are additive, not replacement. The 7 plugins coexist with
the existing static gate without conflict.

## Sub-requests (topologically sorted)

1. **Add 7 dev deps to `pyproject.toml`.** Edit
   `[dependency-groups].dev` to append the 7 entries from the issue
   body's plugin table with the listed minimum-version pins. Do not
   touch the existing entries; do not reorder. Run `uv sync` to
   pull the new deps into `.venv` and regenerate `uv.lock`.

2. **Confirm the lock validates.** Run `just lock-check` (added in
   foreman#337). It must exit zero. If non-zero, the resolver
   produced a malformed lockfile and the Worker stops to investigate
   before touching anything else.

3. **Add the `[tool.coverage.run]` and `[tool.coverage.report]`
   blocks to `pyproject.toml`.** Per the acceptance criteria above.
   Place them after `[tool.pytest.ini_options]` (the file's
   existing convention is to group all `[tool.*]` blocks at the
   bottom; the spec's diff is additive, not rearranging).

4. **Measure the coverage baseline.** Run
   `uv run --no-sync pytest --cov=packages/foreman/src --cov-branch
   --no-header -q 2>&1 | tail -20`
   from the repo root. Record the TOTAL percentage line in the PR
   description. Compute `<MEASURED_BASELINE> = floor(TOTAL) - 4`
   and use that value in step 5. If TOTAL is below 50%, the Worker
   records the value and uses `max(measured - 4, 30)` as a sane
   floor (don't ship a gate at 0%, but don't shame-set it at 85
   either — the issue explicitly anticipates a lower baseline).

5. **Extend `[tool.pytest.ini_options].addopts`** with the new
   flags per the acceptance criteria, using `<MEASURED_BASELINE>`
   from step 4 in `--cov-fail-under`. Add the `timeout` and
   `timeout_method` keys to the same block.

6. **Run `just check` from a clean tree.** It must exit zero. The
   full suite now runs with random order + xdist + cov gate + 60s
   timeout. If any test fails ONLY under random order, the
   ordering bug is the test's fault (fixture pollution per
   foreman#19's lesson), and the Worker either fixes the offending
   test in this PR or files a follow-up ticket and uses
   `-p no:randomly` only as a temporary diagnostic — not as a
   permanent escape hatch. If any test hits the 60s timeout, same
   discipline: fix or file a follow-up; do not raise the global
   timeout.

7. **Add the diff-cover step to `.github/workflows/ci.yml`.** A new
   step after the existing `- run: just check` step, before the
   `- run: uv cache prune --ci` step. Body per the acceptance
   criteria. The step is gated on `if: github.event_name ==
   'pull_request'`.

8. **Update `CLAUDE.md`.** Add the `## Running tests` section per
   the acceptance criteria. Place it after the existing `## Working
   in this repo` section (logical adjacency — both describe the
   developer feedback loop). Document the actual `<MEASURED_BASELINE>`
   value, not the agent_core number.

9. **Self-verify the diff-cover gate at CI time.** When the impl PR
   opens, the new CI step runs against the PR's diff. The Worker
   confirms exit zero. If diff-cover fails on the wiring PR itself,
   the Worker records the exit value + the diff-cover report URL in
   the PR description and resolves before merge — either by adding
   a single sanity-check test, or by documenting why the patch
   coverage is unavoidably low (e.g. all changes are TOML, which
   diff-cover handles correctly by excluding non-Python files).

10. **`just check` from a clean tree, one more time.** Confirm exit
    zero with `new_failures_count == 0`. Commit, push, open the impl
    PR with the measured baseline value and the diff-cover result
    documented in the body.

## File-level changes

| File | Change |
|------|--------|
| `pyproject.toml` | Append 7 dev deps to `[dependency-groups].dev`. Extend `[tool.pytest.ini_options].addopts` with cov / xdist / report flags. Add `timeout` + `timeout_method` to `[tool.pytest.ini_options]`. Add new `[tool.coverage.run]` and `[tool.coverage.report]` blocks. |
| `uv.lock` | Regenerated by `uv sync` after the dep additions. Committed as-is. |
| `.github/workflows/ci.yml` | New step `- name: Patch coverage gate (diff-cover)` inserted between the existing `just check` step and the `uv cache prune --ci` step. Gated on `if: github.event_name == 'pull_request'`. |
| `CLAUDE.md` | New `## Running tests` section after the existing `## Working in this repo` section, documenting `--no-cov`, `--randomly-seed=<N>`, `-p no:randomly`, the cov gate floor, and the 60s per-test timeout. |

No other files change. The `justfile` is NOT modified — all
wiring lives in `pyproject.toml` so direct `pytest` invocations
pick it up uniformly. `.pre-commit-config.yaml` is NOT modified —
the existing `pre-push` stage entry `just check` automatically
picks up the new pytest flags via the composite gate. Member
package pyprojects under `packages/*/pyproject.toml` are NOT
touched (no production deps added).

## Alternatives considered

- **Wire the plugin flags into the `justfile` `test` recipe
  instead of `[tool.pytest.ini_options].addopts`.** Rejected — the
  justfile's `test` recipe is `uv run --no-sync pytest` with zero
  flags, and the convention captured in the file's header is "the
  single quality-command surface". Splitting the flags between
  `justfile` and `pyproject.toml` would let direct `pytest`
  invocations (IDE runners, ad-hoc local invocations) silently
  skip the gate. agent_core PR #192 puts the flags in `addopts`
  for the same reason; copy that decision.

- **Set `--cov-fail-under=85` literally (agent_core's number).**
  Rejected — the issue body explicitly says "set `--cov-fail-under`
  at whatever the current baseline measures minus ~4% (don't ship
  a gate that's red on day one)". foreman's source tree and test
  surface differ from agent_core's; the threshold MUST be measured,
  not copied. Documented in sub-request 4.

- **Skip pytest-xdist (run tests serially).** Rejected — xdist is
  one of the 7 explicitly-listed plugins in the issue body, and
  parallel execution is a load-bearing piece of the random-order
  discipline (random order + xdist together surface fixture
  ordering bugs that neither catches alone). The cost is the
  `timeout_method = "thread"` constraint and slightly more complex
  test isolation, both of which agent_core has already accepted.

- **Skip hypothesis (no property-test deps if we have no property
  tests today).** Rejected — the issue body explicitly lists it
  ("property-test deps for future tests"). Installing it now means
  the first property test added in a follow-up doesn't need a
  separate dep-add PR. Cost is one extra dev dep; payoff is
  removing a friction point from future testing work.

- **Add diff-cover as a separate CI workflow file
  (`.github/workflows/patch-coverage.yml`) instead of a step in
  `ci.yml`.** Rejected — diff-cover depends on `coverage.xml`
  produced by the pytest step in the same job. A separate workflow
  would have to either re-run the test suite (doubling CI time) or
  upload+download `coverage.xml` as an artifact (added
  complexity for no benefit). Adding a step to the existing job
  reuses the artifact in memory.

- **Gate diff-cover on `merge_group` AND `pull_request`.** Rejected
  — agent_core gates only on `pull_request`. The merge queue runs
  AFTER PR approval, by which point the diff-cover gate has
  already passed. Running it twice is wasted CI minutes.

- **Add `pytest-sugar` for nicer terminal output alongside
  pytest-clarity.** Rejected — out of scope per the issue's
  explicit 7-plugin list. The discipline is "port the agent_core
  stack", not "add every nice-to-have plugin we can think of".

- **Backfill tests to raise the coverage baseline before setting
  the gate.** Rejected — out of scope per the issue body:
  "Backfilling tests to lift coverage. Baseline is whatever
  measurement shows; ratchet up in follow-up tickets." The gate
  ships at the measured baseline; the ratchet is a separate
  workstream.

## Open questions

None. The issue body is the most specific issue brief in foreman's
recent history (it names exact source files in agent_core, exact
commit SHAs, exact version pins, exact CI shape, exact CLAUDE.md
section). The only thing that requires measurement at impl time is
the cov-fail-under threshold, and the spec resolves that with a
concrete procedure in sub-request 4. The diff-cover behavior on a
config-only PR (sub-request 9) is the one remaining unknown the
Worker resolves empirically — captured as part of the impl flow,
not deferred as an open question.

## Out of scope

- Backfilling production-code tests to lift the coverage baseline.
  Follow-up tickets ratchet the gate; this PR only ships the gate.
- Switching CI providers or restructuring the matrix beyond the
  one added diff-cover step.
- Changing foreman's existing ruff / mypy / import-linter / vulture
  / lock-check wiring.
- Adding pytest plugins beyond the 7 named in the issue's table.
- Removing the existing `--import-mode=importlib` flag from
  `addopts` (it's load-bearing for foreman's namespace packaging;
  preserve it).
- Modifying the `justfile` `test` recipe (wiring is in
  `pyproject.toml` per the rationale above).
- Modifying `.pre-commit-config.yaml` (the existing `pre-push`
  stage entry `just check` automatically inherits the new flags).
- Touching production-code dependencies in
  `packages/*/pyproject.toml`. All 7 deps are test-only.
- Adding a Python regression test that asserts the gate is wired
  (the gate IS the test — `just check` either runs the new flags
  or it doesn't).
- Pinning a maximum version on any of the 7 deps. Minimum-version
  pins from the issue body are sufficient; upper bounds are
  deferred to whichever future release breaks something.
- Configuring `[tool.coverage.html]` for HTML reports. Local
  developers who want HTML can pass `--cov-report=html` ad-hoc;
  the persistent config produces terminal + XML only.
