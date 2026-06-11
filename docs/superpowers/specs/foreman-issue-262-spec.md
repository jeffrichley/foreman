# Spec: bump `claude-agent-sdk` past 0.2.87 to pick up upstream PR #918 (issue #262)

## Goal

Tighten the loose `claude-agent-sdk>=0.1,<1` dependency pin in
`packages/foreman/pyproject.toml` (line 10) to a lower bound that
includes upstream fix
[claude-agent-sdk-python#918](https://github.com/anthropics/claude-agent-sdk-python/pull/918),
regenerate the workspace lockfile so `uv sync` picks up the bumped
version, and use the existing xfail tripwire test
`test_sdk_receive_messages_does_not_raise_on_success_subtype`
(`packages/foreman/tests/test_provider_anthropic_sdk.py:825-882`) as
the green-light signal that the fix landed. See issue
[#262](https://github.com/jeffrichley/foreman/issues/262) and root-cause
ticket #230. This is a dependency-only change: no foreman source code
under `packages/foreman/src/` is modified.

## Acceptance criteria

- The Worker has visited
  `https://github.com/anthropics/claude-agent-sdk-python/releases` (or
  the `gh release list -R anthropics/claude-agent-sdk-python` equivalent),
  identified the FIRST tagged release that contains the merged
  [PR #918](https://github.com/anthropics/claude-agent-sdk-python/pull/918)
  (per the issue body, this is expected to be somewhere in the v0.2.88 →
  v0.2.96 range; the exact tag must be verified at impl time, not
  copied from the issue), and recorded that version number in the impl
  PR body as the chosen lower bound. Call this version `V_FIRST_FIXED`
  in the rest of this spec.
- `packages/foreman/pyproject.toml` line 10 reads exactly:
  `"claude-agent-sdk>=<V_FIRST_FIXED>,<1",`
  where `<V_FIRST_FIXED>` is the verified release tag (no leading `v`
  prefix — PEP 440 / uv specifier form). The `<1` upper bound stays as
  is; bumping past a major is explicitly out of scope.
- The repo-root `uv.lock` is regenerated via `uv lock --upgrade-package
  claude-agent-sdk` (NOT a full `uv lock --upgrade` — single-package
  upgrade keeps the blast radius narrow). Post-regeneration:
  - `grep -nE '^name = "claude-agent-sdk"$' uv.lock -A2` shows
    `version = "<V_FIRST_FIXED>"` (or a later 0.2.x patch the resolver
    chose within the new lower bound).
  - `grep -nE 'claude-agent-sdk.*specifier' uv.lock` shows the updated
    `specifier = ">=<V_FIRST_FIXED>,<1"`.
  - No other `[[package]]` blocks in `uv.lock` had their `version` line
    changed (eyeball-verify via `git diff uv.lock` — the diff should be
    confined to the `claude-agent-sdk` block + its `metadata` /
    `specifier` mention).
- The tripwire test at
  `packages/foreman/tests/test_provider_anthropic_sdk.py:835`
  (`test_sdk_receive_messages_does_not_raise_on_success_subtype`)
  flips from XFAIL to XPASS after the bump. Verification step:
  `uv run --no-sync pytest
   packages/foreman/tests/test_provider_anthropic_sdk.py::test_sdk_receive_messages_does_not_raise_on_success_subtype
   -v` reports `XPASS` (not `XFAIL`, not `FAILED`, not `PASSED` —
  specifically XPASS because the `@pytest.mark.xfail(strict=False,
  ...)` marker is intentionally retained per the issue's out-of-scope
  list). The impl PR body MUST quote the relevant line of pytest
  output proving the XPASS as the cited "tripwire fired" evidence.
- `just check` exits zero on the post-bump worktree — that is, lint
  (`ruff check packages/foreman`), typecheck (`mypy
  packages/foreman/src`), and the full pytest suite all pass, including
  the now-XPASSing tripwire test. No new failures introduced
  (`new_failures_count == 0`).
- The impl PR body cites both upstream PR
  [#918](https://github.com/anthropics/claude-agent-sdk-python/pull/918)
  and the chosen first-fixed release tag, AND links foreman issue #230
  as the root-cause ticket this bump retires. The PR body MUST NOT
  contain a GitHub closing-keyword reference to #230 or #262 (per
  foreman#63 — Foreman's daemon owns issue closure; PR bodies route
  around `Closes #N`). Reference plainly: "addresses #262", "retires
  root cause from #230".
- The xfail marker block at
  `packages/foreman/tests/test_provider_anthropic_sdk.py:825-833` is
  UNCHANGED by this PR. Removing or flipping `strict=False → True` is
  explicitly out of scope (see "Out of scope" below); the issue's
  scoping note keeps the marker as the long-tail signal.
- `packages/foreman/src/foreman/providers/anthropic_sdk.py` is NOT
  modified by this PR. The auth-retry guard at lines 145-183 and the
  `_SDK_AUTH_ERROR_PREFIX` pattern at lines 45-55 are belt-and-suspenders
  defenses; the issue body says they stay. Likewise the rate-limit
  config at `packages/foreman/src/foreman/config.py:109-145` is not
  modified.

## Approach

The change is a 2-file dependency bump (`pyproject.toml` + `uv.lock`)
gated by an existing xfail tripwire test. Foreman's CI does not run the
upstream SDK's own test suite; instead, foreman pinned an in-repo
contract test in PR #255 commit 1 that calls the REAL
`claude_agent_sdk.query()` against a hand-rolled fake `Transport` (see
`_SuccessAsErrorTransport` at
`packages/foreman/tests/test_provider_anthropic_sdk.py:765-822`) so the
real `receive_messages` code path is exercised. That test is currently
XFAIL because the in-container SDK is 0.2.87, which still has the bug
where a `{"type": "error", "error": "success"}` raw protocol envelope
raises a generic `Exception("success")` from `receive_messages` even
though the payload was logically a successful completion. Upstream
PR #918 fixes the classification; once the bump lands, the same fake
transport exercises the fixed code path and the test stops raising —
pytest reports XPASS automatically.

**Single-package lockfile upgrade.** The right uv invocation here is
`uv lock --upgrade-package claude-agent-sdk` (not `uv lock --upgrade`,
not `uv sync --upgrade`, and not deleting and regenerating the whole
lockfile). Single-package upgrade re-resolves only the named package
plus any of its sub-deps the new version requires; everything else in
the lockfile stays pinned at its current resolution. Spec rationale:
this is a bug-fix bump, not a "refresh all deps" sweep, and the
project's quality gate runs against the lockfile-pinned versions of
every other library. Drifting unrelated pins would expand the
post-merge surface and undercut the "narrow, surgical fix" intent the
issue calls out.

**Version cutoff verification.** The issue body names upstream PR #918
and reports the SDK was at v0.2.96 at filing time, but does NOT pin the
exact release tag in which the fix first landed (the issue says
">= 0.2.88, but verify"). The Worker MUST resolve this against the
upstream release notes / commit history before opening the impl PR.
Two correct ways:

1. `gh release list -R anthropics/claude-agent-sdk-python --limit 20`
   → identify the lowest tag whose release notes reference PR #918.
2. `gh api repos/anthropics/claude-agent-sdk-python/pulls/918 --jq
   '.merge_commit_sha'` then `gh api
   repos/anthropics/claude-agent-sdk-python/commits/<sha>/tags` (or the
   web-UI equivalent) → identify the first tag containing that commit.

Either is fine; document the chosen tag in the PR body. If the upstream
PR is NOT yet present in any tagged release at impl time, exit the role
with `outcome="incomplete"` and label `foreman:needs-help`; do NOT pin
to an unreleased SHA.

**House-style conventions followed.** The impl PR uses
`fix(deps): bump claude-agent-sdk past 0.2.87 to <V_FIRST_FIXED> for
upstream receive_messages fix` as the conventional-commit title —
`fix(deps):` matches both the allowed types in CLAUDE.md and the
issue-title shape. The Planner spec PR (this PR) uses `docs(spec):` per
the project-mandated Planner-PR scope.

**No production source touched.** This is the discipline the
"`packages/foreman/src/` is NOT modified" acceptance criterion enforces.
The bug is in the vendored SDK; the cure is in the lockfile. Touching
foreman source here would conflate this PR with unrelated cleanups and
violate the issue's stated narrow scope.

## Sub-requests (topologically sorted)

1. **Confirm the tripwire is currently XFAIL on this branch (pre-bump).**
   Run on the `foreman/impl-262` worktree, before any edit:
   ```bash
   uv run --no-sync pytest \
     packages/foreman/tests/test_provider_anthropic_sdk.py::test_sdk_receive_messages_does_not_raise_on_success_subtype \
     -v
   ```
   Expected: report shows `XFAIL` for this test name. Record the raw
   pytest output line in the PR body as the "red baseline" evidence
   (this is the TDD red-first discipline adapted to an upstream-bug
   shape).

2. **Resolve `V_FIRST_FIXED`.** Run:
   ```bash
   gh release list -R anthropics/claude-agent-sdk-python --limit 20
   gh api repos/anthropics/claude-agent-sdk-python/pulls/918 \
     --jq '{merged_at, merge_commit_sha, base: .base.ref}'
   ```
   Cross-reference the merge commit against the release tags to
   identify the LOWEST released tag containing the fix. Strip any
   leading `v` (PEP 440 / uv specifier form is bare-numeric). Record
   the chosen tag in a one-line comment to keep in the PR body draft.

3. **Edit `packages/foreman/pyproject.toml`.** Line 10 changes from
   `    "claude-agent-sdk>=0.1,<1",` to
   `    "claude-agent-sdk>=<V_FIRST_FIXED>,<1",`. No other edits to
   this file. Sample (assuming `V_FIRST_FIXED = 0.2.88` — the Worker
   verifies the actual value in step 2):
   ```toml
   "claude-agent-sdk>=0.2.88,<1",
   ```

4. **Regenerate the lockfile, single-package upgrade only:**
   ```bash
   uv lock --upgrade-package claude-agent-sdk
   ```
   Expected output: one line like
   `Updated claude-agent-sdk v0.2.87 -> v<resolved>`. If the resolver
   reports updates to any other package, STOP and investigate — the
   single-package flag should keep blast radius minimal; cross-package
   bumps are out of scope for this PR.

5. **Eyeball the lockfile diff:**
   ```bash
   git diff -- uv.lock | head -120
   ```
   Expected: changes confined to the `[[package]] name = "claude-agent-sdk"`
   block (version + sdist + wheels hashes) and the workspace
   `metadata.requires-dist` block where the foreman manifest's
   specifier is mirrored (`grep -n 'claude-agent-sdk' uv.lock` should
   still show 3 hits, with the specifier line now reading
   `specifier = ">=<V_FIRST_FIXED>,<1"`). If foreign blocks show diffs
   on `version` lines, undo the lockfile change and rerun step 4 with
   `--upgrade-package claude-agent-sdk` only (no other names).

6. **Run the tripwire test to confirm it flipped:**
   ```bash
   uv run --no-sync pytest \
     packages/foreman/tests/test_provider_anthropic_sdk.py::test_sdk_receive_messages_does_not_raise_on_success_subtype \
     -v
   ```
   Expected: pytest reports `XPASS` for this test. If it still reports
   `XFAIL`, the chosen `V_FIRST_FIXED` does NOT actually contain the
   fix — go back to step 2 with a higher tag.

7. **Run the targeted provider-test module:**
   ```bash
   uv run --no-sync pytest packages/foreman/tests/test_provider_anthropic_sdk.py -v
   ```
   Expected: every test in this file passes (or XPASSes — the tripwire);
   none XFAIL→FAIL or PASSED→FAIL. The other tests in this file mock
   the SDK's `query()` at the top level and are insensitive to the
   bump, so any new failure here is a real regression to investigate
   before pushing.

8. **Run the full quality gate:** `just check`. Expected: exit 0. The
   typecheck step (`mypy packages/foreman/src`) does not consult
   `uv.lock`, so the bump cannot affect mypy output; the lint step
   reads only foreman source, also unaffected; the test step reflects
   the resolver's new pin via the in-container SDK. If any test other
   than the tripwire flips state, STOP — investigate before bundling
   into the PR.

9. **Stage and commit:**
   ```bash
   git add packages/foreman/pyproject.toml uv.lock
   git commit -m "fix(deps): bump claude-agent-sdk past 0.2.87 to <V_FIRST_FIXED> for upstream receive_messages fix"
   ```
   Exactly two files staged. If `git status` shows any other modified
   files in the staged set, unstage them — the discipline is "this PR
   bumps the SDK and nothing else."

10. **Draft the impl PR body.** Required elements (no GitHub
    closing-keywords for #262 or #230 — see foreman#63):
    - Cite `claude_agent_sdk_python#918` and the chosen `V_FIRST_FIXED`
      tag with permalinks.
    - Quote the XFAIL → XPASS pytest line as evidence the tripwire
      fired.
    - Link issue #262 as "addresses #262" and #230 as "retires the
      root cause documented in #230"; do NOT write `Closes #262` or
      `Fixes #230`. Foreman's daemon performs the close-out via
      `merge_impl_pr` after the impl PR merges.
    - One-line acknowledgment that the operator-level smoke test
      (container rebuild + benign ticket through the autonomous loop)
      is post-merge work, not part of this PR (see "Out of scope").

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/pyproject.toml` | Line 10 only: `"claude-agent-sdk>=0.1,<1",` → `"claude-agent-sdk>=<V_FIRST_FIXED>,<1",` where `<V_FIRST_FIXED>` is the verified first release tag containing upstream PR #918. No other edits. |
| `uv.lock` | Regenerated via `uv lock --upgrade-package claude-agent-sdk`. Changes confined to the `[[package]] name = "claude-agent-sdk"` block (version + sdist URL + wheel URLs + hashes) and the workspace `metadata.requires-dist` `specifier = ...` line for that package. No other `[[package]]` blocks' `version` lines should change. |

No expected changes to (sanity-checked):

- `packages/foreman/src/foreman/providers/anthropic_sdk.py` — the
  auth-retry wrapper at lines 145-183 and `_SDK_AUTH_ERROR_PREFIX` at
  lines 45-55 are belt-and-suspenders defenses that the issue body
  explicitly says to keep.
- `packages/foreman/src/foreman/config.py` — the
  `rate_limit_max_consecutive_failures` / `rate_limit_window_seconds`
  fields from PR #255 commit 3 stay as the cascade defense; issue body
  out-of-scope item.
- `packages/foreman/tests/test_provider_anthropic_sdk.py` — the xfail
  marker block (lines 825-833) stays as written; the issue's
  out-of-scope list explicitly keeps the marker as a long-tail signal.
- `Dockerfile` — the deps layer (`uv export` + `uv pip install -r`) at
  lines 64-67 already reads the bumped lockfile when the daemon image
  is rebuilt; no Dockerfile edits required for the bump to take effect
  on next `scripts/build-docker.sh`.
- All other foreman source / test files.

## Verification

Before opening the impl PR, the Worker MUST run and record:

1. **Red baseline** (pre-edit, on the impl worktree): the tripwire test
   reports XFAIL. Record the pytest output line.
2. **Single-file pin diff**: `git diff --stat packages/foreman/pyproject.toml`
   shows exactly one insertion + one deletion on line 10.
3. **Targeted lockfile diff**: `git diff -- uv.lock` shows changes
   confined to the `claude-agent-sdk` block and its workspace
   `specifier` mention. Eyeball-verify no other `[[package]]` blocks
   had their `version` changed.
4. **Green tripwire**: targeted pytest run for
   `test_sdk_receive_messages_does_not_raise_on_success_subtype`
   reports XPASS. Record the output line.
5. **Module pass**: `pytest packages/foreman/tests/test_provider_anthropic_sdk.py`
   exits 0 with no new failures.
6. **Full gate**: `just check` exits 0. mypy / ruff / pytest all green.
7. **Scope check**: `git status` shows exactly two modified files
   (`packages/foreman/pyproject.toml`, `uv.lock`).

## Alternatives considered

- **Bump to a strict-equals pin (e.g., `==0.2.96`).** Rejected — the
  project's existing dependency style is range-bounded (`>=A,<B`), the
  upper bound `<1` already guards against unintentional major bumps,
  and a strict-equals pin would force a deps PR on every upstream
  patch release (including unrelated patch fixes that don't touch
  foreman's surface). The range form keeps maintenance light while
  still excluding the known-buggy 0.2.87.

- **Bump the upper bound to `<2` along with the lower bound.** Rejected
  — the existing `<1` bound is an intentional guardrail against
  unreviewed SDK majors; the issue's out-of-scope list explicitly
  protects it ("If 1.0 ships, the `<1` upper bound is intentional and
  a separate decision"). Bundling an upper-bound widening into a
  narrow bug-fix bump expands the blast radius unnecessarily.

- **Run `uv lock --upgrade` (full lockfile refresh) instead of
  `--upgrade-package claude-agent-sdk`.** Rejected — full refresh
  would re-resolve every dependency in the workspace, which means
  every PR-merge-time `just check` runs against potentially-drifted
  versions of unrelated libraries. The "narrow, surgical" framing in
  the issue body explicitly cuts that off; single-package upgrade is
  the discipline.

- **Remove the `@pytest.mark.xfail(strict=False, ...)` marker so the
  test becomes a normal regression guard post-bump.** Rejected — the
  issue's out-of-scope list explicitly says "The xfail contract test
  itself ... leave it in place as a long-tail regression guard against
  the SDK reintroducing the bug." We respect the issue author's
  explicit scoping decision here. (See "Open questions" — there is a
  real technical question about whether the marker actually functions
  as a regression guard once XPASS is observed, but that's a follow-up
  ticket, not this PR.)

- **Bundle the container rebuild + operator smoke test into this PR's
  acceptance.** Rejected — the Worker subprocess runs inside the daemon
  container; it cannot rebuild the image hosting it nor restart the
  daemon. Container rebuild + smoke is an operator-level step. The PR
  body acknowledges this so the reviewer understands what the PR is
  and isn't claiming.

- **Vendor the patch directly into `packages/foreman/src/foreman/providers/anthropic_sdk.py`
  (monkey-patch `receive_messages` instead of bumping the SDK).**
  Rejected — would couple foreman to SDK internals beyond the public
  `query()` / `ResultMessage` surface, expanding the maintenance
  burden every time the SDK refactors internally. The bump is the
  cheaper fix.

- **Do nothing — rely on PR #255's defenses indefinitely.** Rejected
  per the issue body's "Why this matters even though PR #255's defenses
  landed" section: the defenses prevent the cascade but allow individual
  successful runs to be recorded as `outcome="exception"` (silent work
  loss). The bump removes that residual cost.

## Open questions

- **Does leaving `strict=False` on the xfail marker actually function
  as a long-tail regression guard?** Pytest semantics: with
  `strict=False`, an XFAIL → XPASS transition produces a visible
  XPASS report row but does NOT fail CI; a later XPASS → XFAIL
  transition (SDK regression reintroduces the bug) silently goes back
  to XFAIL and ALSO does not fail CI. The issue's out-of-scope
  framing presumes the marker keeps the test a regression guard
  post-bump, but the marker's `strict=False` form means a future
  regression would be invisible. The right follow-up (separate
  ticket, NOT this PR) is probably to remove the xfail marker
  entirely after one observed XPASS, so a regression FAILs the suite
  loudly. Flagging here so the Reviewer knows the discrepancy is
  noticed and intentionally deferred; this PR respects the issue's
  explicit out-of-scope decision. Confidence: medium — the bump
  itself is unambiguous, the marker-handling follow-up is the only
  real uncertainty.

- **Exact `V_FIRST_FIXED` tag.** The issue body states the latest
  upstream tag at filing time was v0.2.96 and the fix is "somewhere
  >= 0.2.88, but verify". The Worker resolves this against the
  upstream release notes at impl time (step 2 of Sub-requests). Spec
  is parametric in `V_FIRST_FIXED`; the Worker fills it in.

## Out of scope

- **Bumping any other dependency** in `pyproject.toml` or `uv.lock`.
  Single-package upgrade is the discipline; refreshing other pins is a
  separate concern.
- **Removing or modifying the auth-retry wrapper** at
  `packages/foreman/src/foreman/providers/anthropic_sdk.py:145-183`
  (PR #255 commit 2 / #229). Belt-and-suspenders defense; per issue
  body explicitly stays.
- **Removing or modifying the per-ticket consecutive-failure
  rate-limit** in `packages/foreman/src/foreman/config.py:109-145` (PR
  #255 commit 3 / #228). Same reason.
- **Removing or modifying the xfail marker** on
  `test_sdk_receive_messages_does_not_raise_on_success_subtype`
  (`packages/foreman/tests/test_provider_anthropic_sdk.py:825-833`).
  Per issue out-of-scope. See "Open questions" for the reasoning
  follow-up.
- **Bumping the `<1` upper bound to `<2`** or otherwise widening major
  acceptance. Intentional guardrail per issue out-of-scope.
- **Rebuilding the daemon container, restarting the daemon, and
  smoke-running a benign ticket through the autonomous loop** (issue
  body Step 3). The Worker subprocess runs inside the daemon container
  it would need to rebuild; this is operator-level work post-merge,
  not Worker-level work pre-PR.
- **Closing issue #230 in this PR's body.** Per foreman#63, issue
  closure is owned by the Foreman daemon's `merge_impl_pr` action;
  PR bodies that contain GitHub closing-keyword references would
  short-circuit the gate. The Worker references #230 plainly
  ("retires the root cause documented in #230") and the daemon's
  reconciler handles closure after merge.
- **Editing `packages/foreman/src/`** for any reason. This is a
  dependency-only PR.
- **Documenting the SDK release-notes process** for future bumps. If
  useful, file separately.
- **Updating `Dockerfile`.** The deps layer already consumes the
  bumped lockfile at next `scripts/build-docker.sh`; no Dockerfile
  change is required for the bump to take effect post-merge.
