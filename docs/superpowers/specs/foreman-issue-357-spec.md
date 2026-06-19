# Spec: MergingState refuses to merge when impl PR base diverges from the configured dev base branch (issue #357)

## Goal
Add a defense-in-depth guard to `MergingState` that refuses to call
`host.merge_pr` when the impl PR's `base.ref` does not match the
project's configured base branch. The originating Worker-side bug was
already fixed by [foreman#341](https://github.com/jeffrichley/foreman/issues/341)
/ PR #339, but it recurred as [foreman#347](https://github.com/jeffrichley/foreman/issues/347)
on 2026-06-19 due to a stale container shipping the pre-#341 Worker code.
A `MergingState` pre-merge assertion catches both root causes (logic
bug AND stale binary) and any future regression in the same family by
gating the merge on a direct read of the GitHub PR's actual `base.ref`.
Tracks [foreman#357](https://github.com/jeffrichley/foreman/issues/357).

## Acceptance criteria
- [ ] `PRState` in `packages/foreman/src/foreman/v4/git_provider.py` grows
  a `base_ref: str` field (default `""` so existing test fixtures and
  any unrelated PRState constructors stay compatible).
- [ ] `PyGithubGitProvider.get_pr_state` in
  `packages/foreman/src/foreman/v4/pygithub_git_provider.py` populates
  `base_ref=pr.base.ref` from the PyGithub PR object — the field is
  always non-empty in production.
- [ ] `FakeGitProvider.set_pr_state` accepts the new field through the
  `PRState` argument; `FakeGitProvider.get_pr_state` returns it
  unchanged. No new Fake-specific helper is needed — the field rides on
  the existing `PRState` dataclass.
- [ ] `StateContext` in `packages/foreman/src/foreman/v4/state.py` grows
  a `project_configs: dict[str, ProjectConfig]` field (default
  `field(default_factory=dict)` so headless tests that don't seed it
  keep working). The field maps `ProjectConfig.name` →
  `ProjectConfig`. Import `ProjectConfig` under a `TYPE_CHECKING` guard
  to avoid a runtime import cycle with `foreman.v4.config`.
- [ ] `WorkerPool` in `packages/foreman/src/foreman/v4/worker_pool.py`
  accepts `project_configs: dict[str, ProjectConfig]` (default
  `None` → empty dict) at construction and threads it into every
  `StateContext` built by `_run_transition`.
- [ ] `Daemon` in `packages/foreman/src/foreman/v4/daemon.py` accepts
  `project_configs: dict[str, ProjectConfig]` at construction and
  forwards it to `WorkerPool`. Defaults to `None` → empty dict so
  existing direct-`Daemon` constructions in tests still work.
- [ ] `bootstrap_cli_context` in
  `packages/foreman/src/foreman/v4/bootstrap.py` builds the
  `project_configs` map from `config.projects` (keyed by
  `project_config.name`) and passes it to `Daemon`. Same loop already
  builds `per_project_providers`; the new map is built alongside.
- [ ] `MergingState.execute` in
  `packages/foreman/src/foreman/v4/states/merging.py` reads the PR's
  `base_ref` from `get_pr_state` and compares it (case-insensitive)
  against the expected base BEFORE either the already-merged short-
  circuit or the `merge_pr` call. The expected base is resolved as
  `project_configs[ctx.ticket.project].dev_base_branch or
  DEFAULT_DEV_BASE_BRANCH` where `DEFAULT_DEV_BASE_BRANCH = "main"` is
  a module-level constant in `merging.py` documenting the fallback.
- [ ] On mismatch (or on empty `base_ref` — meaning we could not read
  the base): `MergingState.execute` returns
  `Outcome(kind=OutcomeKind.NEEDS_HELP, confidence=HIGH,
  summary=...)` with `details={"actual_base": ..., "expected_base":
  ..., "pr_number": ...}` and the existing `artifacts.pr_number`
  populated. `host.merge_pr` is NOT called and `host.close_issue` is
  NOT called on this branch.
- [ ] On match: `MergingState.execute` proceeds through the existing
  CLEAN / BLOCKED branches unchanged. The guard is evaluated BEFORE
  the already-merged short-circuit so that an externally-merged PR
  with the wrong base also surfaces as `NEEDS_HELP` (defense-in-depth
  also applies to the rare "operator click-merged through the UI"
  case).
- [ ] `MergingState.next_state` explicitly handles
  `OutcomeKind.NEEDS_HELP` → `NeedsHelpState` (the current
  defensive fall-through already routes it correctly, but make it
  explicit so the new branch's intent is grep-able).
- [ ] When `ctx.project_configs` is empty OR does not contain
  `ctx.ticket.project` (legacy tests; misconfigured production):
  emit a structured log warning ("MergingState: no project_config for
  project=<X>; skipping base-ref guard") and proceed without the
  guard. This keeps the change additive — old tests pass, production
  paths populate the map via `bootstrap_cli_context`.
- [ ] New test
  `test_merging_state_refuses_to_merge_when_pr_base_diverges_from_dev_base_branch`
  in `packages/foreman/tests/v4/states/test_merging.py`: seeds
  `project_configs={"p": ProjectConfig(name="p", repo="o/p",
  local_clone_path="/x", dev_base_branch="main")}`, sets PR
  `base_ref="foreman/issue-1"`, asserts the next state is
  `NeedsHelp`, asserts `("p", 99) not in git.merge_pr_calls`,
  asserts `("p", 1) not in git.closed_issues`, and asserts the
  recorded outcome's `details` carries
  `actual_base="foreman/issue-1"`, `expected_base="main"`,
  `pr_number=99`.
- [ ] New test
  `test_merging_state_merges_when_pr_base_matches_dev_base_branch`:
  seeds the same project_configs with `dev_base_branch="main"`, sets
  PR `base_ref="main"`, asserts next state is `Done`, asserts
  `("p", 99) in git.merge_pr_calls`, asserts `("p", 1) in
  git.closed_issues`.
- [ ] New test
  `test_merging_state_base_ref_comparison_is_case_insensitive`:
  seeds `dev_base_branch="Main"`, PR `base_ref="main"` → must
  pass (Done). Mirror case: `dev_base_branch="main"`, PR
  `base_ref="MAIN"` → must also pass (Done).
- [ ] New test
  `test_merging_state_falls_back_to_main_when_dev_base_branch_unset`:
  seeds `dev_base_branch=None`, PR `base_ref="main"` → Done.
  Same fixture with PR `base_ref="foreman/issue-1"` → NeedsHelp.
- [ ] New test
  `test_merging_state_refuses_when_base_ref_empty`: seeds
  `dev_base_branch="main"`, PR `base_ref=""` (the "couldn't read it"
  shape) → NeedsHelp.
- [ ] New test
  `test_merging_state_skips_guard_when_project_config_missing`:
  passes `project_configs={}` (empty dict — the legacy test shape),
  PR `base_ref="anything"` → Done. Asserts a warning was logged via
  `caplog` to document the operator-visible signal.
- [ ] Existing tests in `packages/foreman/tests/v4/states/test_merging.py`
  that exercise CLEAN / BLOCKED paths are updated to seed `base_ref`
  on `PRState` to a value matching the seeded
  `project.dev_base_branch` so they continue to pass through the new
  guard. The `_ctx_with_pr` helper is the single point of change.
- [ ] Existing tests that construct `PRState` outside `test_merging.py`
  (`packages/foreman/tests/v4/test_git_provider_fake.py`,
  `packages/foreman/tests/v4/test_pygithub_git_provider.py`,
  `packages/foreman/tests/v4/test_phase4_e2e.py`,
  `packages/foreman/tests/v4/test_phase7_e2e.py`,
  `packages/foreman/tests/v4/test_lifecycle.py`,
  `packages/foreman/tests/v4/_repository_contract.py`,
  `packages/foreman/tests/test_labels.py`) compile unchanged because
  the new field defaults to `""`. They are NOT updated.
- [ ] `PyGithubGitProvider` test in
  `packages/foreman/tests/v4/test_pygithub_git_provider.py` gains an
  assertion that `state.base_ref == "main"` against a PyGithub fake
  whose `pr.base.ref` is mocked to `"main"`. The current assertion
  shape (`assert state == PRState(merged=False, mergeable=True,
  ci_passing=True)`) is updated to either include `base_ref="main"`
  in the expected `PRState` OR replaced with per-field assertions
  including `base_ref`.
- [ ] `just check` exits zero from the worktree root;
  `new_failures_count == 0` per the Worker's pre-push gate.

## Approach

The defect is not in `MergingState` itself — it's the absence of a
verification step between "Worker emitted a PR" and "we merge it into
the substrate." Two independent root causes have already produced the
same wrong-base merge symptom in production (foreman#341 was a Worker
logic bug fixed in PR #345; foreman#347 was a stale-container redeploy
that resurfaced the same logic bug). The right architectural answer is
not "fix the Worker harder" but "stop trusting the Worker blindly at
the last gate before the substrate moves." That gate is `MergingState`.

The guard is structurally cheap: one new field on `PRState` (already
returned by `get_pr_state`), one config lookup, one case-insensitive
string compare. No new `GitProvider` Protocol methods — `base_ref`
extends the existing `PRState` return shape, satisfying the issue's
explicit "Do NOT add new Protocol methods" constraint. On mismatch the
state returns `Outcome.NEEDS_HELP`, which `next_state` already routes
to `NeedsHelpState` via the defensive fall-through (made explicit by
this spec for grep-ability). Recovery shape is operator-driven: human
inspects the diagnostic in the `details` bag, retargets or abandons
the PR, and resumes through `foreman resume` — no auto-correct,
matching the issue's out-of-scope.

Project configs are not currently visible inside the state machine.
The Worker reads `project.dev_base_branch` via `V4Config` from
`bootstrap_cli_context`, but `StateContext` carries only
`ticket.project: str` (the project name). The smallest plumbing that
makes the guard work is a `project_configs: dict[str, ProjectConfig]`
on `StateContext`, populated by `WorkerPool._run_transition` from a
map handed up by `Daemon` from `bootstrap_cli_context`. This is the
same shape `per_project_providers` already uses in `bootstrap.py:59`
— config-resolution moves to startup, runtime gets a flat dict
keyed by project name. The default everywhere is `{}` so existing
tests and headless transitions stay green; production paths fill the
map and benefit from the guard.

The `dev_base_branch is None` fallback is `"main"`, declared as
`DEFAULT_DEV_BASE_BRANCH` at the top of `merging.py`. Rationale: the
Worker resolves `None` to the repo's actual default branch via
`_resolve_default_branch` (`worktree.py:614`), which requires running
`git symbolic-ref` against a clone. `MergingState` has no clone to
probe; replicating the resolution would require a new Protocol method
(which the issue forbids) or threading the resolved default branch
into config at startup (substrate change out of proportion to the
bug). The `"main"` fallback matches the de-facto default on every
project this orchestrator currently runs against, and the doc on the
constant tells operators with non-`main` defaults to set
`dev_base_branch` explicitly. This is the documented shape, not a
silent assumption.

**Pattern naming (per `CLAUDE.md` Decision 4):** No GoF pattern fits.
The Google engineering principle is **"defense in depth"** /
fail-stop at the last load-bearing gate before durable state changes.
The bug class is "upstream produces wrong artifact; downstream
trusts blindly"; the fix is "downstream verifies the one invariant
that protects the substrate." This is the same shape as
`SpecReviewState.verify`'s
`raise ValueError("...no pr_number in artifacts")` defensive check at
`spec_review.py:25-27` — a small assertion at the substrate-write
boundary, written precisely because the failure mode of NOT having
it is silent corruption rather than a loud error.

## Sub-requests (topologically sorted)

1. **Extend `PRState`.** In
   `packages/foreman/src/foreman/v4/git_provider.py`, add
   `base_ref: str = ""` to `PRState` (already
   `@dataclass(frozen=True, slots=True)` — dataclass default-arg
   syntax works directly). Update the module docstring to mention
   the field briefly. Update the `GitProvider` Protocol comment block
   describing `PRState` if any exists (none today, no change needed).

2. **Populate `base_ref` in production.** In
   `packages/foreman/src/foreman/v4/pygithub_git_provider.py`,
   `PyGithubGitProvider.get_pr_state` (around line 164), set
   `base_ref=pr.base.ref` in the returned `PRState`. PyGithub's
   `pr.base` is a `GitRef` object with a `.ref` string attribute
   (verified by the file's own usage pattern).

3. **Populate `base_ref` in the fake.** The default `""` on
   `PRState` means `FakeGitProvider.set_pr_state` already accepts
   the new shape via the existing argument. Confirm no test in
   `tests/v4/test_git_provider_fake.py` asserts equality against a
   `PRState` with implicit positional-arg ordering (`PRState(False,
   True, True)`-style) that would shift on the new field. If any
   exists, refactor to keyword args.

4. **Plumb `project_configs` into `StateContext`.** In
   `packages/foreman/src/foreman/v4/state.py`, import
   `ProjectConfig` under `TYPE_CHECKING` (alongside the existing
   `GitProvider` import), and add `project_configs: dict[str,
   ProjectConfig] = field(default_factory=dict)` to the
   `@dataclass(frozen=True)` `StateContext`. The frozen-default
   pattern (`field(default_factory=dict)`) is already in use on
   `TicketRecord.depends_on` in `records.py:29` — follow that
   shape.

5. **Plumb the map through `WorkerPool`.** In
   `packages/foreman/src/foreman/v4/worker_pool.py`, add
   `project_configs: dict[str, ProjectConfig] | None = None` to
   `WorkerPool.__init__` and store it as `self._project_configs =
   project_configs or {}`. Thread it into the `StateContext(...)`
   construction in `_run_transition` (around line 121).

6. **Plumb the map through `Daemon`.** In
   `packages/foreman/src/foreman/v4/daemon.py`, add
   `project_configs: dict[str, ProjectConfig] | None = None` to
   `Daemon.__init__` and forward it to the `WorkerPool(...)`
   construction at line 76. Default `None` keeps test-only
   `Daemon(...)` constructions valid.

7. **Build the map at bootstrap.** In
   `packages/foreman/src/foreman/v4/bootstrap.py`, inside the
   existing `for project_config in config.projects:` loop (around
   line 60), build
   `project_configs: dict[str, ProjectConfig] = {pc.name: pc for
   pc in config.projects}` (one-line dict comp outside the loop is
   simpler — choose whichever reads cleaner). Pass it to the
   `Daemon(...)` constructor at line 111.

8. **Add the constant and the guard.** In
   `packages/foreman/src/foreman/v4/states/merging.py`:
   - Add module-level `DEFAULT_DEV_BASE_BRANCH = "main"` with a
     docstring explaining the fallback for when
     `ProjectConfig.dev_base_branch` is `None`. Cite that
     non-`main` projects must set the config explicitly.
   - In `MergingState.execute`, BEFORE the `if state.merged:`
     short-circuit, add:
     ```python
     project_config = ctx.project_configs.get(ctx.ticket.project)
     if project_config is not None:
         expected_base = (
             project_config.dev_base_branch or DEFAULT_DEV_BASE_BRANCH
         )
         actual_base = state.base_ref
         if (
             not actual_base
             or actual_base.casefold() != expected_base.casefold()
         ):
             return Outcome(
                 kind=OutcomeKind.NEEDS_HELP,
                 confidence=OutcomeConfidence.HIGH,
                 summary=(
                     f"impl PR base {actual_base!r} does not match "
                     f"configured dev_base_branch {expected_base!r}; "
                     f"refusing to merge"
                 ),
                 artifacts=OutcomeArtifacts(pr_number=pr_number),
                 details={
                     "actual_base": actual_base,
                     "expected_base": expected_base,
                     "pr_number": pr_number,
                     "ticket_issue_number": ctx.ticket.issue_number,
                 },
             )
     else:
         logger.warning(
             "MergingState: no project_config for project=%s; "
             "skipping base-ref guard for ticket=%d pr=%d",
             ctx.ticket.project, ctx.ticket.id, pr_number,
         )
     ```
     Add the `logger = logging.getLogger(__name__)` declaration at
     module scope (with the `import logging` import added to the
     existing future-import block).
   - Update `MergingState.next_state` to add an explicit
     `if outcome.kind == OutcomeKind.NEEDS_HELP: return NeedsHelpState()`
     branch BEFORE the defensive fall-through. The fall-through
     stays as a fail-safe for unexpected outcome kinds.

9. **Update the existing `test_merging.py` fixtures.** In
   `packages/foreman/tests/v4/states/test_merging.py`, change
   `_ctx_with_pr` to:
   - Accept an optional `base_ref: str = "main"` parameter.
   - Construct `PRState(..., base_ref=base_ref)` instead of the
     current 3-arg form.
   - Build `project_configs={"p": ProjectConfig(name="p",
     repo="o/p", local_clone_path="/tmp/p", dev_base_branch="main")}`
     and thread it into the `StateContext(...)` construction.
   - Import `ProjectConfig` from `foreman.v4.config`.

10. **Add the new MergingState guard tests.** In the same file, add
    the five new tests listed in the Acceptance criteria. Each
    constructs a `StateContext` via `_ctx_with_pr(...)` with the
    appropriate `base_ref` + project-config overrides:
    - `test_merging_state_refuses_to_merge_when_pr_base_diverges_from_dev_base_branch`
    - `test_merging_state_merges_when_pr_base_matches_dev_base_branch`
    - `test_merging_state_base_ref_comparison_is_case_insensitive`
      (two sub-cases, one test function with two PRState fixtures
      or two parametrize entries)
    - `test_merging_state_falls_back_to_main_when_dev_base_branch_unset`
      (two sub-cases — happy + refusal)
    - `test_merging_state_refuses_when_base_ref_empty`
    - `test_merging_state_skips_guard_when_project_config_missing`
      (uses `caplog` to assert the warning fires)

11. **Update the PyGithub provider test.** In
    `packages/foreman/tests/v4/test_pygithub_git_provider.py`,
    extend the existing `get_pr_state` test (around line 45) so the
    mocked PyGithub PR's `base.ref` is set to `"main"` and the
    expected `PRState` includes `base_ref="main"`.

12. **Run the quality gate.** From the worktree root, run
    `just check`. Confirm exit zero and `new_failures_count == 0`.
    The Worker's pre-push gate is the contract here per
    `CLAUDE.md`.

## File-level changes
| File | Change |
|------|--------|
| `packages/foreman/src/foreman/v4/git_provider.py` | Add `base_ref: str = ""` field to `PRState`. |
| `packages/foreman/src/foreman/v4/pygithub_git_provider.py` | Populate `base_ref=pr.base.ref` in `PyGithubGitProvider.get_pr_state`. |
| `packages/foreman/src/foreman/v4/state.py` | Add `project_configs: dict[str, ProjectConfig]` field (default empty) to `StateContext`; import `ProjectConfig` under `TYPE_CHECKING`. |
| `packages/foreman/src/foreman/v4/worker_pool.py` | Accept `project_configs` kwarg; thread into every `StateContext`. |
| `packages/foreman/src/foreman/v4/daemon.py` | Accept `project_configs` kwarg; forward to `WorkerPool`. |
| `packages/foreman/src/foreman/v4/bootstrap.py` | Build `{pc.name: pc for pc in config.projects}` map and pass to `Daemon`. |
| `packages/foreman/src/foreman/v4/states/merging.py` | Add `DEFAULT_DEV_BASE_BRANCH = "main"`; add pre-merge base-ref guard; explicit `NEEDS_HELP` routing in `next_state`; add `logging` import + module logger. |
| `packages/foreman/tests/v4/states/test_merging.py` | Update `_ctx_with_pr` to seed `base_ref` + `project_configs`; add 5+ new tests covering the guard. |
| `packages/foreman/tests/v4/test_pygithub_git_provider.py` | Extend `get_pr_state` test to assert `base_ref="main"`. |

No changes to: `packages/foreman/src/foreman/v4/config.py` (the
`ProjectConfig.dev_base_branch` field is already the right shape and
is consumed read-only); `packages/foreman/src/foreman/roles/worker.py`
(the Worker fix from #341 is the upstream half — this spec is the
downstream half and does not replace it);
`packages/foreman/src/foreman/v4/states/spec_review.py` (per the
issue's Out of scope — spec PRs are not in this guard's scope).

## Alternatives considered

- **Keep relying solely on Worker correctness; do not add a guard.**
  Rejected. The bug has already recurred twice from two different
  root causes (foreman#341 logic bug; foreman#347 stale binary). The
  base-branch invariant is small enough that asserting it at the
  substrate-write boundary is strictly cheaper than the recovery cost
  of even one more recurrence. This is exactly the shape Decision 4
  of the architecture-stability plan calls out as "make the right
  thing easy" — refusing to merge into the wrong base is the right
  thing; today the daemon makes the wrong thing easy.
- **Add a new Protocol method
  `GitProvider.get_pr_base_ref(project, pr_number)` instead of
  extending `PRState`.** Rejected. The issue explicitly says "Do NOT
  add new Protocol methods." Extending the existing `PRState`
  return shape is the smaller cut and keeps the `get_pr_state` call
  the single round-trip per `MergingState.execute` (no extra
  network hop).
- **Refuse to merge whenever `dev_base_branch` is `None`, instead
  of falling back to `"main"`.** Rejected. Every project this
  orchestrator runs against today uses `main` as its default and
  many do not set `dev_base_branch` (it's optional, and the Worker
  resolves `None` via origin's default branch). A strict refusal
  on `None` would break every current production path and force
  operators to set the config defensively just to keep merging
  working. The documented `"main"` fallback with a clear warning
  for non-`main` repos is the operationally safer default.
- **Resolve `dev_base_branch` to the real default branch at config
  load time (so `MergingState` always has a concrete expected base
  string).** Rejected. Resolution requires either a clone (the
  Worker's path via `_resolve_default_branch`) or a new
  `GitProvider.get_default_branch` method — both substrate
  changes out of proportion to the bug. The fallback constant
  documented in `merging.py` is the simpler, smaller path.
- **Put the guard in `SpecReviewState` instead of `MergingState`.**
  Rejected. The spec PR's blast radius is smaller (a wrong-base
  spec PR is recoverable by retargeting; a wrong-base impl PR
  silently moves substrate state forward). The issue's Out of scope
  also excludes the spec-PR extension. Adding it would add API
  surface without proportionate benefit.
- **Auto-retarget the impl PR (call `pr.edit(base=expected)`) on
  detection.** Rejected. Out of scope per the issue body. Refusal
  + escalation to a human is the documented recovery shape.
- **Plumb the full `V4Config` into `StateContext` instead of just
  the `project_configs` map.** Rejected. The whole config has many
  fields the state machine has no business touching (orchestrator
  app credentials, operator identities). The narrower map is the
  minimum API surface that solves the problem; future widening can
  happen on the day a state actually needs it.

## Open questions
- None. The plumbing shape mirrors the existing
  `per_project_providers` map in `bootstrap.py:59`; the fallback
  constant is documented; the test plan covers the four
  behavior axes (refusal, happy path, case-insensitivity, fallback);
  the issue's Out of scope is explicit.

## Out of scope
- Changing the Worker's base-branch logic. The Worker fix shipped
  in foreman#341 / PR #345 stays; this spec is the layered
  downstream defense, not a replacement.
- Auto-retarget / auto-correct logic in `MergingState`. Refusal
  with escalation to `NeedsHelp` is the entire recovery shape.
- Extending the guard to `SpecReviewState`. Spec PRs are outside
  the issue's stated scope.
- Adding any new `GitProvider` Protocol method. The new field on
  `PRState` is the only substrate change.
- Backfilling the guard against PRs already merged into the wrong
  base. The guard is purely forward-looking; existing recovery PRs
  (#345, #355) have already brought past wrong-base impls onto
  `main`.
- Telemetry beyond the structured warning logged when
  `project_config` is missing. Operator-facing observability for
  the refusal branch rides on the existing `Outcome.details` bag
  + `LabelObservabilityObserver`'s `foreman:state-needshelp`
  stamp — no new observer needed.
- Updating the issue templates or operator docs. The recovery
  instructions are issue-specific (which PR to retarget); a
  generic doc update is overscope for a defensive guard.
- Resolving the issue body's reference to a non-existent
  `Outcome.diagnostic_detail` field (the freeform bag is
  `Outcome.details`, introduced in Phase 8d.17 per
  `outcome.py:56-69`). This spec uses `details` as that is the
  actual field name; no schema change.
- Reconciling the issue's source-file pointers
  (`packages/foreman/src/foreman/v4/git_host.py`,
  `.../git_hosts/github.py`) with the actual filenames
  (`git_provider.py`, `pygithub_git_provider.py`). This spec
  uses the actual filenames throughout.
