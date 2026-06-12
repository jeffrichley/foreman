# Spec: Reviewer-on-impl resilience to deleted spec branch (issue #294)

## Goal

`roles/reviewer.py:_get_pr_diff` shells out to `git diff origin/<base_branch>...<head_sha>` with `check=True`. When an impl PR's base ref (`foreman/issue-N`) has been deleted from origin between PR creation and Reviewer-on-impl dispatch, the command exits 128 with `fatal: bad revision`, raises `CalledProcessError`, and crashes the Reviewer subprocess. This spec ships two complementary layers that together close the failure mode: (Layer A) `_get_pr_diff` recovers from a missing base ref by refreshing the worktree's origin refs (re-using the existing rc=128 self-heal in `worktree._fetch_origin_branch`) and falling back to a diff against `origin/<default-branch>`; (Layer B) a new state-machine rule retargets the impl PR's base from the spec branch to the default branch BEFORE `dispatch_reviewer_impl` fires, so the structural fragility disappears at the source. See issue [#294](https://github.com/jeffrichley/foreman/issues/294). Same failure family as foreman#279 (D9 retarget guard) and foreman#122 (deleted-on-origin spec-branch self-heal in `WorktreeManager`).

## Acceptance criteria

- `packages/foreman/src/foreman/roles/reviewer.py:_get_pr_diff` no longer raises `CalledProcessError` when `origin/<base_branch>` does not exist on origin. Specifically:
  - The inline `subprocess.run(["git", "fetch", "origin", base_branch], check=False, ...)` at `reviewer.py:296-302` is replaced by a call to `foreman.worktree.fetch_origin_branch(worktree_path, base_branch, role_token=reviewer_token)`. This routes through the existing rc=128 / `couldn't find remote ref` prune-stale-ref self-heal at `worktree.py:691-715` (added by foreman#122) so the stale `refs/remotes/origin/<base>` ref is evicted at fetch time, not silently kept.
  - The `git diff origin/<base_branch>...<head_sha>` call switches to `check=False` and inspects the result. On rc=0 the existing behavior is preserved (return `result.stdout`).
  - On rc != 0 with stderr matching the missing-base-ref signatures (`bad revision`, `unknown revision or path not in the working tree`, OR the previous fetch having self-healed the ref), `_get_pr_diff` falls back to:
    1. Resolve the repo's default branch via a new public wrapper `foreman.worktree.resolve_default_branch(worktree_path, role_token=reviewer_token)` (which delegates to the existing private `_resolve_default_branch`).
    2. Refresh `origin/<default-branch>` via `foreman.worktree.fetch_origin_branch(worktree_path, default_branch, role_token=reviewer_token)`.
    3. Run `git diff origin/<default_branch>...<head_sha>` with `check=True`. If THIS fails, the original `CalledProcessError` is re-raised — the fallback exhausted; surfacing the error to the existing `_on_failure` path (which runs the foreman#229 helper) is the correct response.
  - Emit a structured WARNING via the Python logging module (NOT print-to-stderr) tagged with the original base ref, the head SHA, and the fallback ref so an operator running `docker compose logs daemon` sees the recovery firing. Log format: `reviewer._get_pr_diff: base ref origin/%s missing for head %s; fell back to origin/%s` — three placeholders (base_branch, head_sha, default_branch), matching `_get_pr_diff`'s existing signature so the refactor does NOT need to thread PR/issue numbers through to this function. Fixed message prefix so log scrapers can pin on it. (PR and issue numbers are already in the surrounding Reviewer dispatch log lines emitted from the daemon, so the operator-facing log surface keeps the join visible without changing `_get_pr_diff`'s signature — the reasoning that originally tempted the four-placeholder format.)

- `packages/foreman/src/foreman/worktree.py` gains two NEW public wrappers, both delegating to existing private helpers without altering their semantics:
  - `def fetch_origin_branch(clone_path: Path, branch: str, *, role_token: str | None = None) -> None:` — thin wrapper around `_fetch_origin_branch(clone_path, branch, role_token=role_token)`. Best-effort contract preserved verbatim. Docstring cross-references foreman#294 + foreman#122.
  - `def resolve_default_branch(clone_path: Path, *, role_token: str | None = None) -> str:` — thin wrapper around `_resolve_default_branch(clone_path, role_token=role_token)`. Returns the resolved default-branch name (falls back to `"main"` if `origin/HEAD` is missing — existing behavior). Docstring cross-references foreman#294.
  - The two existing private helpers (`_fetch_origin_branch`, `_resolve_default_branch`) keep their underscored names + signatures unchanged — all existing internal call sites (`worktree.py:251`, `:356`, `:745`) and tests (`test_worktree.py`) stay green.

- `packages/foreman/src/foreman/reconciler/actions.py` gains a new shared helper:
  - `def _retarget_impl_pr_to_default_if_stacked(ctx: ActionContext, host: ReconcilerHost) -> bool:` — extracts the existing D9 retarget block at `actions.py:465-496` verbatim into a reusable helper. Returns `True` if a retarget was performed, `False` otherwise (idempotent on re-fire). The two guard conditions (current base IS spec branch; spec PR has merged) are preserved.
  - `_handle_attempt_merge(ctx, host, target="impl")` is refactored to call this helper instead of inlining the retarget block. Existing behavior is preserved bit-for-bit; this is the deduplication move.

- `packages/foreman/src/foreman/reconciler/actions.py` gains a new `Action` enum value `RETARGET_IMPL_PR_TO_DEFAULT = "retarget_impl_pr_to_default"`. Placed alphabetically near the other impl-side actions; same shape as existing actions.

- `execute_action` in `actions.py` gains a new `elif action is Action.RETARGET_IMPL_PR_TO_DEFAULT:` branch that calls `_retarget_impl_pr_to_default_if_stacked(ctx, host)` and returns. The helper's `bool` return is logged at INFO level (`retarget_impl_pr_to_default: retargeted=%s for issue #%d`) so the audit trail shows whether the call was a no-op or an actual retarget.

- `packages/foreman/src/foreman/reconciler/rules.py` gains a new rule `retarget_impl_pr_to_default` at precedence 138 (slotted between `dispatch_worker` at 130 and `dispatch_reviewer_impl` at 140). Predicate `_retarget_impl_pr_eligible(ctx)`:
  - `ctx.pr is not None`
  - `not ctx.pr.is_merged`
  - `ctx.pr.head_ref.startswith("foreman/impl-")` — impl PR shape
  - `"foreman:impl-review" in ctx.issue.labels` — ticket has reached impl-review (this is the gate against pre-Worker premature fire)
  - **One-shot idempotence gate (REQUIRED to avoid deadlocking the autonomous loop):** `not ctx.log.has_unterminated("retarget_impl_pr_to_default", ctx.ticket_id) and ctx.log.count_completed("retarget_impl_pr_to_default", ctx.ticket_id) == 0`. Without this gate the rule keeps matching every tick (the other conditions are stable throughout the impl-review phase — only the Reviewer can flip the `foreman:impl-review` label, and the Reviewer never fires because precedence-138 wins over precedence-140 in `evaluate_with_rule`'s first-match-wins ordering at `rules.py:756-760`). The gate makes the rule fire AT MOST ONCE per ticket — exactly the shape Layer B needs, since the retarget is a single host call whose effect persists in GitHub state. This is the SAME gate pattern used by `dispatch_reviewer_impl` at `rules.py:482` and `dispatch_fixer_impl` at `rules.py:496-497`. (Different from the existing D9 retarget block at `attempt_merge_impl`, which fires inside an action handler — not via a top-level rule — and so doesn't need a rule-level gate.)

- Existing rule `dispatch_reviewer_impl` (precedence 140) is unchanged. Because rules are evaluated in precedence order and the new retarget rule fires at 138, the retarget lands first; the one-shot gate then flips the predicate False on subsequent ticks, freeing `dispatch_reviewer_impl` at precedence 140 to fire the next time around. Sequence: tick N evaluates retarget rule → predicate True → retarget action runs → execution log records `retarget_impl_pr_to_default` as completed. Tick N+1 evaluates retarget rule → predicate False (count_completed == 1) → falls through to `dispatch_reviewer_impl` → Reviewer dispatches against the now-retargeted base. Layer A handles the (impossible-after-Layer-B) case where Reviewer-on-impl still sees a stale stacked base.

- Existing `attempt_merge_impl` rule + `_handle_attempt_merge(target="impl")` retarget block continue to fire. They're now a no-op in the common path (Layer B already retargeted at precedence 138) but the helper's idempotence makes the duplicate call cost-free. Keeping the existing D9 call site is defense in depth against operators who removed the `foreman:impl-review` label (which would block Layer B from firing).

- New regression test `tests/test_roles_reviewer.py::test_get_pr_diff_recovers_from_missing_base_ref`:
  - Use a NEW helper `_seed_clone_with_bare_origin(tmp_path, issue_number)` (added in this PR to `test_roles_reviewer.py` alongside the existing `_seed_clone_with_spec_branch` at line 254). The helper:
    1. Calls `_seed_clone_with_spec_branch(clone, issue_number)` to build the seed + spec-branch commits as today.
    2. `git init --bare` at a sibling directory `bare_origin` (NOT shared with the clone — separate path).
    3. In the working clone, replace the origin URL: `git remote set-url origin <bare_origin_path>`.
    4. Push `main`, the spec branch (`foreman/issue-N`), AND the impl branch (`foreman/impl-N`, created and committed by the helper after switching off `main`) to the bare origin.
    5. Run `git fetch origin` in the clone so `refs/remotes/origin/main` and `refs/remotes/origin/foreman/issue-N` are populated.
    6. Returns `(impl_head_sha, bare_origin_path)` so the test can prune the spec ref from the bare origin via `git update-ref -d refs/heads/foreman/issue-N` (run with `cwd=bare_origin_path`) to simulate the operator-driven delete-on-merge scenario. The bare-origin layout is the only one where this `update-ref` is safe: it removes the ref from the remote, leaves the working clone's commits intact, and gives `_fetch_origin_branch`'s rc=128 self-heal a real "couldn't find remote ref" condition to recover from.
  - After the precondition is set up (bare origin without the spec branch; clone has the stale `refs/remotes/origin/foreman/issue-N` AND a live impl branch ahead of `origin/main`), call `_get_pr_diff(worktree, base_branch="foreman/issue-42", head_sha=impl_head, role_token="tok")`.
  - Assert the call returns a non-empty diff string (the diff against `origin/main`) WITHOUT raising.
  - Assert a WARNING-level log record was emitted matching the full message `reviewer._get_pr_diff: base ref origin/foreman/issue-42 missing for head <impl_head>; fell back to origin/main`. Use `caplog.set_level(logging.WARNING, logger="foreman.roles.reviewer")` and inspect `caplog.record_tuples` for the exact `(logger_name, level, message)` triple rather than prefix-matching, so a future refactor that quietly drops the SHA or the fallback ref from the message fails this test.

- New regression test `tests/test_roles_reviewer.py::test_get_pr_diff_normal_path_unchanged`:
  - Use the same `_seed_clone_with_bare_origin` helper but SKIP the spec-ref prune step — the bare origin still has `foreman/issue-N`. Confirm `_get_pr_diff` returns the diff against `origin/foreman/issue-42` (i.e., the spec branch's diff), no WARNING record present in `caplog.record_tuples`, no fallback path taken. This pins the "normal path is unchanged" contract so a future refactor cannot accidentally always-fall-back.

- New regression test `tests/test_roles_reviewer.py::test_get_pr_diff_reraises_when_fallback_also_fails`:
  - Use the same `_seed_clone_with_bare_origin` helper, then prune BOTH `refs/heads/foreman/issue-42` AND `refs/heads/main` from the bare origin (`git update-ref -d` each, run with `cwd=bare_origin_path`). The working clone's `git fetch` will then rc=128 on both branches, the self-heal will prune both local stale refs, and BOTH `git diff` calls (initial AND fallback) will fail with `bad revision`. Call `_get_pr_diff` and assert `CalledProcessError` is raised — surfacing through `_on_failure`. This pins the "we don't silently swallow real errors" contract.

- New regression test `tests/reconciler/test_actions.py::test_retarget_impl_pr_to_default_helper_idempotent`:
  - Construct an `ActionContext` for an impl PR whose base is already `main`. Call `_retarget_impl_pr_to_default_if_stacked(ctx, host)` with a fake `host` that records `retarget_pr_base` calls. Assert returns `False` and `retarget_pr_base` was NOT called.
  - Then construct an `ActionContext` for an impl PR whose base is `foreman/issue-N` AND the spec PR has merged. Assert returns `True` and `retarget_pr_base` was called exactly once with `new_base="main"`.
  - Then construct an `ActionContext` for an impl PR whose base is `foreman/issue-N` BUT the spec PR has NOT merged. Assert returns `False` and `retarget_pr_base` was NOT called (the safety guard fires).

- New regression test `tests/reconciler/test_rules.py::test_retarget_impl_pr_to_default_rule_eligibility`:
  - Build an `ActionContext` matching the predicate (impl PR open, head=foreman/impl-N, foreman:impl-review label, fresh ExecutionLog with no completed `retarget_impl_pr_to_default` entries). Assert `_retarget_impl_pr_eligible(ctx)` returns True.
  - Build a context with `is_merged=True` on the PR. Assert False.
  - Build a context without `foreman:impl-review` label. Assert False.
  - Build a context with head=`foreman/issue-N` (spec PR shape). Assert False — never retarget spec PRs.
  - Build a context where `ctx.log.count_completed("retarget_impl_pr_to_default", ctx.ticket_id) == 1` (the gate already fired). Assert False — pins the one-shot idempotence behavior.
  - Build a context where `ctx.log.has_unterminated("retarget_impl_pr_to_default", ctx.ticket_id)` is True (a retarget is mid-flight). Assert False — pins the don't-double-fire behavior.

- New regression test `tests/reconciler/test_rules.py::test_retarget_then_dispatch_reviewer_impl_on_next_tick`:
  - End-to-end pin on the two-tick sequence the predicate's one-shot gate enables. Build a context matching the retarget predicate on tick N. Call `evaluate_with_rule(ctx)` — assert it returns `(Action.RETARGET_IMPL_PR_TO_DEFAULT, "retarget_impl_pr_to_default")`.
  - Mark the `retarget_impl_pr_to_default` row as completed in `ctx.log` (use whatever test-helper the file already uses to seed log rows — grep `count_completed` in `tests/reconciler/` for the existing fake-log pattern).
  - Call `evaluate_with_rule(ctx)` again on the SAME otherwise-unchanged context. Assert it now returns `(Action.DISPATCH_REVIEWER_IMPL, "dispatch_reviewer_impl")`. This pins the structural escape valve — if a future refactor removes the one-shot gate, this test fails and the autonomous-loop deadlock is caught at PR-review time, not in production.

- New regression test `tests/reconciler/test_actions.py::test_handle_attempt_merge_impl_still_retargets_via_shared_helper`:
  - Existing D9 retarget regression (foreman#279) stays green after the refactor. The current test should continue to pass without modification; if it doesn't, the refactor changed behavior — fix the refactor.

- All existing tests in `packages/foreman/tests/test_roles_reviewer.py` and `packages/foreman/tests/reconciler/test_actions.py` + `test_rules.py` continue to pass without modification.

- `just check` exits 0 on the impl worktree: lint clean, mypy clean, full pytest suite green.

- The impl PR title uses a `fix(reviewer):` or `fix(reconciler):` conventional-commit prefix (e.g., `fix(reviewer): recover when impl PR spec branch is deleted before review`). Subject must NOT start with an uppercase letter per `CLAUDE.md:36`. The impl PR body references issue #294 plainly — NO closing-keyword references (per foreman#63; the merge gate lives in the daemon's close-out, not in PR auto-close).

## Approach

This change is best characterized by Google's "make the right thing easy" engineering principle (per `CLAUDE.md`'s Decision-4 calibrated lens), not by a single GoF pattern. The Layer B move (retarget the impl PR's base BEFORE the Reviewer-on-impl dispatches) makes the Reviewer's job trivially correct — it reads `base=main` from GitHub and runs `git diff origin/main...HEAD`, no stacked-PR plumbing required. The Layer A move is straightforward defensive recovery (no pattern fits cleanly — a try/except + fallback for an externally-mutable ref). Naming honesty per the lens: "no GoF pattern, this is error-recovery + earlier scheduling of a known-good state-machine step."

The two-layer ship is justified by the failure mode's two activation paths. **Layer B (state-machine retarget earlier)** eliminates the autonomous-loop failure: spec PR merges → tick fires `retarget_impl_pr_to_default` at precedence 138 → impl PR base flips from `foreman/issue-N` to `main` → spec branch is now safely deletable (auto-delete-on-merge OR manual prune) → Reviewer-on-impl reads `base=main` from the GitHub API → no crash. This is the structural fix and is sufficient for the auto-delete-on-merge scenario the issue body identifies as activation condition #1.

**Layer A (`_get_pr_diff` resilience)** is defense in depth for activation conditions #2 and #3 (operator-driven manual deletion at any time; future foreman features that touch the branch lifecycle). It uses the existing `_fetch_origin_branch` self-heal infrastructure (foreman#122) — the same prune-stale-ref logic that already handles "spec branch was deleted after fetch was cached" elsewhere in the codebase. The Reviewer was the one consumer that bypassed this infrastructure (it shells out to `git fetch` directly at `reviewer.py:296-302` and `git diff` directly at `reviewer.py:304-311`); routing both through the existing helpers brings the Reviewer into line with `WorktreeManager`'s tolerance contract. Without Layer A, an operator who manually deletes a merged spec branch between ticks could still trip the original crash before Layer B has a chance to fire.

The shared `_retarget_impl_pr_to_default_if_stacked` helper is the DRY move that makes Layer B cheap. The retarget logic already exists at `actions.py:465-496` (D9 / foreman#279) but was tightly coupled to `_handle_attempt_merge`. Extracting it preserves bit-for-bit semantics while enabling the new rule + handler to call it without copy-paste. The helper's idempotence (no-op when base != spec branch; safety guard when spec PR un-merged) is what allows both call sites (precedence 138 retarget rule AND precedence 162 attempt_merge_impl) to invoke it without coordination — exactly the property the rule engine needs.

Rule precedence 138 is the right slot because:
- `dispatch_worker` at 130 must complete first (impl PR doesn't exist before the Worker creates it).
- `dispatch_reviewer_impl` at 140 must read the retargeted base. The reconciler dispatches one action per tick per ticket and `evaluate_with_rule` returns the FIRST matching rule (`rules.py:756-760`), so on tick N the retarget rule fires (precedence 138 wins, retarget lands), and on tick N+1 the one-shot idempotence gate flips the retarget predicate False, allowing precedence 140 to evaluate next and dispatch the Reviewer against the freshly-retargeted base.
- The existing D9 retarget at 162 (`attempt_merge_impl`) is kept as a backstop — if an operator manually removes the `foreman:impl-review` label between Worker completion and a possible Layer B fire, the D9 path still catches the orphan-commit case.

The one-shot idempotence gate (the `count_completed == 0` + `not has_unterminated` pair on the predicate) is load-bearing: without it the retarget rule keeps matching every tick (its other conditions — impl-review label, impl-shape PR, not merged — are stable until the Reviewer fires and the Reviewer can never fire because precedence 138 keeps shadowing precedence 140), and the autonomous loop deadlocks on every impl PR. The gate is the SAME shape that `dispatch_reviewer_impl` at `rules.py:482` and `dispatch_fixer_impl` at `rules.py:496-497` already use for the same reason. The action-handler's internal short-circuit (`current_base != spec_branch_name`) is still useful — it makes a misfire harmless — but it is NOT sufficient on its own because it doesn't keep the predicate's host-call-free condition False on subsequent ticks.

The new public wrappers in `worktree.py` (`fetch_origin_branch`, `resolve_default_branch`) are deliberately thin. The existing private helpers' contracts are battle-tested (foreman#122, #291); the wrappers exist purely so `reviewer.py` doesn't have to import private names. Mirroring `fetch_origin_default_branch`'s shape (introduced by foreman#291) keeps the worktree module's public API coherent — there's now a `fetch_origin_branch(<any>)` AND a `fetch_origin_default_branch(<resolves the name first>)` AND a `resolve_default_branch(<just resolves>)`, each composable.

Per the issue's Out-of-scope list, this spec deliberately does NOT:
- Replace the worktree-based `git diff` with the GitHub Files API (separate refactor; the worktree approach has performance + accuracy reasons that the issue body calls out).
- Change the spec PR's auto-delete policy (operator decision).
- Move Reviewer-on-impl to run BEFORE spec PR merges (would break the stacked-PR contract).

## Sub-requests (topologically sorted)

1. **Add two public wrappers to `packages/foreman/src/foreman/worktree.py`.** Insert near the existing `fetch_origin_default_branch` (currently around `worktree.py:728-746`). No changes to existing private helpers.

   ```python
   def fetch_origin_branch(
       clone_path: Path,
       branch: str,
       *,
       role_token: str | None = None,
   ) -> None:
       """Best-effort refresh of ``origin/<branch>`` with stale-ref self-heal.

       Public wrapper around :func:`_fetch_origin_branch`. Same best-effort
       contract: network failures are logged as warnings and swallowed;
       rc=128 ``couldn't find remote ref`` triggers the foreman#122
       prune-stale-ref self-heal.

       Added in foreman#294 so :func:`foreman.roles.reviewer._get_pr_diff`
       can route through the shared self-heal instead of shelling out to
       ``git fetch`` directly.
       """
       _fetch_origin_branch(clone_path, branch, role_token=role_token)


   def resolve_default_branch(
       clone_path: Path,
       *,
       role_token: str | None = None,
   ) -> str:
       """Return the repo's default branch name (fallback to ``"main"``).

       Public wrapper around :func:`_resolve_default_branch`. Reads
       ``origin/HEAD`` via ``git symbolic-ref``; falls back to ``"main"``
       if ``origin/HEAD`` is missing — existing behavior preserved.

       Added in foreman#294 so :func:`foreman.roles.reviewer._get_pr_diff`
       can resolve the fallback base ref without importing a private name.
       """
       return _resolve_default_branch(clone_path, role_token=role_token)
   ```

2. **Refactor `_get_pr_diff` in `packages/foreman/src/foreman/roles/reviewer.py:275-312`.** Full replacement body (line numbers + signature unchanged):

   ```python
   def _get_pr_diff(
       worktree_path: Path, base_branch: str, head_sha: str, *, role_token: str
   ) -> str:
       """Return the unified diff for the PR's head against its base branch.

       Uses ``git diff`` in the worktree rather than the GitHub Files API so
       we don't pay round-trips for large PRs and so the diff matches whatever
       the worktree has checked out (which the LLM will read from with Read /
       Grep / Glob).

       ``role_token`` is the reviewer bot's installation token. We inject it
       into ``GH_TOKEN`` for both git invocations so any credential-helper
       authenticates as the reviewer bot (HIGH #10).

       foreman#294: if ``origin/<base_branch>`` does not exist on origin
       (auto-delete-on-merge, operator pruned, future cleanup feature),
       the diff falls back to ``origin/<default-branch>...<head_sha>``.
       The fetch step routes through
       :func:`foreman.worktree.fetch_origin_branch` so the existing
       foreman#122 prune-stale-ref self-heal fires at fetch time. A
       WARNING log identifies the recovery.
       """
       from foreman._env_filter import filtered_subprocess_env
       from foreman.worktree import fetch_origin_branch, resolve_default_branch

       role_env = filtered_subprocess_env(role_token=role_token)

       # Refresh the base ref. Routes through the shared self-heal: if the
       # base branch was deleted on origin, the local stale ref is pruned
       # here (foreman#122).
       fetch_origin_branch(worktree_path, base_branch, role_token=role_token)

       result = subprocess.run(
           ["git", "diff", f"origin/{base_branch}...{head_sha}"],
           cwd=worktree_path,
           check=False,
           capture_output=True,
           text=True,
           env=role_env,
       )
       if result.returncode == 0:
           return result.stdout

       stderr_lower = (result.stderr or "").lower()
       missing_ref = (
           "bad revision" in stderr_lower
           or "unknown revision" in stderr_lower
           or "ambiguous argument" in stderr_lower
       )
       if not missing_ref:
           # Other failure (e.g., real corruption). Surface the original
           # error to the existing _on_failure path.
           result.check_returncode()  # raises CalledProcessError

       default_branch = resolve_default_branch(worktree_path, role_token=role_token)
       fetch_origin_branch(worktree_path, default_branch, role_token=role_token)
       _LOG.warning(
           "reviewer._get_pr_diff: base ref origin/%s missing for head %s; "
           "fell back to origin/%s",
           base_branch,
           head_sha,
           default_branch,
       )
       fallback = subprocess.run(
           ["git", "diff", f"origin/{default_branch}...{head_sha}"],
           cwd=worktree_path,
           check=True,
           capture_output=True,
           text=True,
           env=role_env,
       )
       return fallback.stdout
   ```

   Add a module-level `_LOG = logging.getLogger(__name__)` at the top of `reviewer.py` if one doesn't already exist (grep first; the module already uses `print` in places, but a proper logger is required for the `caplog`-based regression test). If a logger already exists with a different name, reuse it.

3. **Extract D9 retarget block into a shared helper in `packages/foreman/src/foreman/reconciler/actions.py`.** Just above `_handle_attempt_merge` (around `actions.py:405`):

   ```python
   def _retarget_impl_pr_to_default_if_stacked(
       ctx: ActionContext, host: ReconcilerHost
   ) -> bool:
       """Retarget the impl PR's base from spec branch to default branch.

       Idempotent: returns False (no host call made) if the impl PR's
       base is already not the spec branch, or if the spec PR has not
       yet merged (safety guard — never retarget an impl PR that depends
       on un-landed spec changes).

       Shared between :func:`_handle_attempt_merge` (target=``"impl"``,
       precedence 162 — D9 / foreman#279) and the new
       ``RETARGET_IMPL_PR_TO_DEFAULT`` action (precedence 138 — foreman#294).
       Calling the helper twice in one ticket is harmless: the second call
       sees ``current_base != spec_branch_name`` and short-circuits.
       """
       if ctx.pr is None:
           return False
       spec_branch_name = spec_branch(ctx.issue.number)
       current_base = host.get_pr_base_ref(
           owner=ctx.snapshot.owner,
           repo=ctx.snapshot.repo,
           pr_number=ctx.pr.number,
       )
       if current_base != spec_branch_name:
           return False
       if not host.is_pr_merged_for_branch(
           owner=ctx.snapshot.owner,
           repo=ctx.snapshot.repo,
           branch=spec_branch_name,
       ):
           return False
       default_branch = host.get_default_branch(
           owner=ctx.snapshot.owner,
           repo=ctx.snapshot.repo,
       )
       host.retarget_pr_base(
           owner=ctx.snapshot.owner,
           repo=ctx.snapshot.repo,
           pr_number=ctx.pr.number,
           new_base=default_branch,
       )
       logger.info(
           "retarget_impl_pr_to_default: retargeted PR %s/%s#%d base %s -> %s "
           "(spec PR for issue #%d has merged)",
           ctx.snapshot.owner,
           ctx.snapshot.repo,
           ctx.pr.number,
           spec_branch_name,
           default_branch,
           ctx.issue.number,
       )
       return True
   ```

4. **Refactor `_handle_attempt_merge`'s D9 block at `actions.py:446-496` to delegate.** Replace the inline block (lines 446-496) with:

   ```python
   if target == "impl":
       _retarget_impl_pr_to_default_if_stacked(ctx, host)
   ```

   The existing `logger.info(...)` call inside the inlined block is now emitted from inside the helper. The semantics are preserved bit-for-bit — same idempotence, same safety guard, same log message.

5. **Add `RETARGET_IMPL_PR_TO_DEFAULT` to the `Action` enum in `actions.py` (the block around `actions.py:50-79`).** Insert near the impl-side action group:

   ```python
   RETARGET_IMPL_PR_TO_DEFAULT = "retarget_impl_pr_to_default"
   ```

6. **Add the new `elif` branch to `execute_action` in `actions.py` (the block around `actions.py:715-732`).** Insert near the other impl-side action branches:

   ```python
   elif action is Action.RETARGET_IMPL_PR_TO_DEFAULT:
       retargeted = _retarget_impl_pr_to_default_if_stacked(ctx, host)
       logger.info(
           "retarget_impl_pr_to_default: retargeted=%s for issue #%d",
           retargeted,
           ctx.issue.number,
       )
   ```

7. **Add the new rule predicate `_retarget_impl_pr_eligible` to `packages/foreman/src/foreman/reconciler/rules.py`** (near the other impl-side predicates around `rules.py:476-498`):

   ```python
   def _retarget_impl_pr_eligible(ctx: ActionContext) -> bool:
       """Eligibility for retargeting the impl PR's base to default branch
       before Reviewer-on-impl dispatches (foreman#294).

       Predicate is intentionally coarse on the GitHub-state side — it
       doesn't check the current base (snapshot doesn't carry base_ref)
       or the spec PR's merged state (rules can't make host calls). The
       action handler's helper
       (:func:`_retarget_impl_pr_to_default_if_stacked`) enforces both
       conditions and short-circuits as a no-op when they aren't met.

       The one-shot idempotence gate at the bottom of the predicate is
       LOAD-BEARING: without it the retarget rule keeps matching every
       tick (the other conditions are stable throughout impl-review),
       and ``evaluate_with_rule``'s first-match-wins ordering at
       ``rules.py:756-760`` would let precedence-138 shadow
       ``dispatch_reviewer_impl`` at precedence 140 forever — deadlocking
       the autonomous loop. Same gate shape used by ``dispatch_reviewer_impl``
       at ``rules.py:482`` and ``dispatch_fixer_impl`` at ``rules.py:496-497``.
       """
       return (
           ctx.pr is not None
           and not ctx.pr.is_merged
           and ctx.pr.head_ref.startswith("foreman/impl-")
           and "foreman:impl-review" in ctx.issue.labels
           and not ctx.log.has_unterminated(
               "retarget_impl_pr_to_default", ctx.ticket_id
           )
           and ctx.log.count_completed(
               "retarget_impl_pr_to_default", ctx.ticket_id
           )
           == 0
       )
   ```

8. **Add the new rule to the `_PROGRESS_RULES` tuple in `rules.py` (around `rules.py:683-689`).** Insert at precedence 138, immediately before `dispatch_reviewer_impl`:

   ```python
   Rule(
       name="retarget_impl_pr_to_default",
       tier=PrecedenceTier.FORWARD_PROGRESS,
       precedence=138,
       when=_retarget_impl_pr_eligible,
       then=Action.RETARGET_IMPL_PR_TO_DEFAULT,
   ),
   ```

9. **Write the three Reviewer regression tests** in `packages/foreman/tests/test_roles_reviewer.py`:
   - `test_get_pr_diff_recovers_from_missing_base_ref` — happy path of the new fallback.
   - `test_get_pr_diff_normal_path_unchanged` — pins the normal path.
   - `test_get_pr_diff_reraises_when_fallback_also_fails` — pins the surface-to-failure-handler path.

   Add a NEW helper `_seed_clone_with_bare_origin(tmp_path, issue_number)` alongside the existing `_seed_clone_with_spec_branch` (defined at `test_roles_reviewer.py:254`, not line 938 which is just a use-site — minor citation fix). The new helper composes with `_seed_clone_with_spec_branch` and ADDS: (i) a separate `bare_origin` directory initialized with `git init --bare`; (ii) `git remote set-url origin <bare_origin>` in the working clone so origin no longer points at the clone itself; (iii) `git push origin main foreman/issue-N foreman/impl-N` so the bare origin holds all three branches; (iv) `git fetch origin` to populate the working clone's `refs/remotes/origin/*`. The helper returns `(impl_head_sha, bare_origin_path)`. Each test mutates the bare-origin refs via `git update-ref -d refs/heads/<branch>` (with `cwd=bare_origin_path`) to set up the missing-ref precondition; the working clone's commits stay intact, and `_fetch_origin_branch`'s rc=128 self-heal sees a real "couldn't find remote ref" condition. Use `caplog.set_level(logging.WARNING, logger="foreman.roles.reviewer")` to capture the recovery log and assert via `caplog.record_tuples`.

10. **Write the three reconciler regression tests:**
    - `tests/reconciler/test_actions.py::test_retarget_impl_pr_to_default_helper_idempotent` (three sub-cases per AC).
    - `tests/reconciler/test_rules.py::test_retarget_impl_pr_to_default_rule_eligibility` (four sub-cases per AC).
    - `tests/reconciler/test_actions.py::test_handle_attempt_merge_impl_still_retargets_via_shared_helper` (D9 regression remains green).

    Use existing test patterns in those files for `ActionContext` construction (grep `ActionContext(` to find the local fixture style).

11. **Run `just check`.** Lint, mypy, full pytest suite green.

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/src/foreman/worktree.py` | Add public `fetch_origin_branch(clone_path, branch, *, role_token=None)` and `resolve_default_branch(clone_path, *, role_token=None)` wrappers near `fetch_origin_default_branch`. No changes to existing private helpers or their callers. |
| `packages/foreman/src/foreman/roles/reviewer.py` | Refactor `_get_pr_diff` (lines 275-312) to route fetch through `worktree.fetch_origin_branch`, run `git diff` with `check=False`, and fall back to `origin/<default>...<head>` on missing-base-ref. Add module-level `_LOG = logging.getLogger(__name__)` if absent. Add `import logging` if absent. |
| `packages/foreman/src/foreman/reconciler/actions.py` | (a) Extract D9 retarget block at lines 446-496 into new helper `_retarget_impl_pr_to_default_if_stacked(ctx, host) -> bool`. (b) Refactor `_handle_attempt_merge` to delegate. (c) Add `Action.RETARGET_IMPL_PR_TO_DEFAULT` enum value. (d) Add `execute_action` branch for the new action. |
| `packages/foreman/src/foreman/reconciler/rules.py` | (a) Add `_retarget_impl_pr_eligible` predicate. (b) Add `retarget_impl_pr_to_default` rule at precedence 138 to `_PROGRESS_RULES`. |
| `packages/foreman/tests/test_roles_reviewer.py` | Add a new `_seed_clone_with_bare_origin(tmp_path, issue_number)` helper alongside the existing `_seed_clone_with_spec_branch` (line 254). Add three regression tests: `test_get_pr_diff_recovers_from_missing_base_ref`, `test_get_pr_diff_normal_path_unchanged`, `test_get_pr_diff_reraises_when_fallback_also_fails`. |
| `packages/foreman/tests/reconciler/test_actions.py` | Add `test_retarget_impl_pr_to_default_helper_idempotent` (three sub-cases) + `test_handle_attempt_merge_impl_still_retargets_via_shared_helper`. |
| `packages/foreman/tests/reconciler/test_rules.py` | Add `test_retarget_impl_pr_to_default_rule_eligibility` (six sub-cases: original four + one-shot gate already-fired + has_unterminated mid-flight). Add `test_retarget_then_dispatch_reviewer_impl_on_next_tick` end-to-end pin for the precedence-138→precedence-140 handoff. |

No expected changes to:

- `packages/foreman/src/foreman/reconciler/host.py`, `v3_host.py`. The host already exposes `retarget_pr_base`, `get_pr_base_ref`, `is_pr_merged_for_branch`, `get_default_branch` (added by foreman#279). No new host methods required.
- `packages/foreman/src/foreman/reconciler/state.py`. `PRState` does not gain a `base_ref` field — the helper reads base via the host at action-execution time, mirroring D9's existing pattern.
- `packages/foreman/src/foreman/reconciler/daemon.py`, `observer.py`, `clone_refresh.py`. The strategy + reconciler tick layer is untouched.
- `packages/foreman/src/foreman/roles/worker.py`, `planner.py`, `fixer.py`. Worker's `_create_pull_with_base_fallback` (worker.py:450-488) is unchanged — it handles a DIFFERENT activation point (PR creation time) and stays as defense in depth.
- `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md`. The plan stays the source of truth; this spec is the Phase 2 #2 ticket in spirit (same family as #279).

## Alternatives considered

- **Layer A only (no state-machine change).** Smaller diff (~80 LOC vs ~250 LOC), no rule/action enum churn. Rejected because the issue body explicitly argues for shipping both: Layer A leaves the structural fragility in place, and any future autonomous-loop reasoning about stacked-PR base refs has to keep accounting for the missing-ref case downstream of the Reviewer. Layer B is the durable answer that composes with future stacked-PR work (Phase 2 #3 per the architecture stability plan).

- **Layer B only (no `_get_pr_diff` resilience).** Even cleaner conceptually — the Reviewer's read becomes trivially correct. Rejected because the issue body identifies operator-driven manual deletion as activation condition #2, and Layer B can't fire if an operator removes the `foreman:impl-review` label between Worker completion and the retarget tick. Layer A is the defense in depth that catches the residual paths.

- **Promote `_fetch_origin_branch` and `_resolve_default_branch` to public names (rename, drop underscore) instead of adding thin wrappers.** Rejected because it would touch every existing internal call site (5 call sites across `worktree.py` + `roles/worker.py`) AND every test (test_worktree.py grep shows ~15+ references). The thin-wrapper approach is one-line-each and keeps the spec scope contained to the failure mode being fixed; it mirrors foreman#291's `fetch_origin_default_branch` pattern for consistency.

- **Detect the missing-ref case BEFORE running `git diff` by probing `git rev-parse --verify origin/<base>` first.** Rejected because it adds an extra subprocess on every Reviewer dispatch (cost is in the normal path, not the failure path) and doesn't simplify the recovery code materially. The check=False + stderr-inspection approach pays the cost only in the failure path.

- **Use the GitHub Files API to fetch the diff instead of `git diff`.** Rejected per the issue body's Out-of-scope list (separate refactor; the worktree approach has performance + accuracy reasons).

- **Run the retarget at PR-open time inside the Worker** instead of at the reconciler tick layer. Rejected because the Worker doesn't know whether the spec PR has merged at the moment it creates the impl PR — the safety guard (`is_pr_merged_for_branch`) is a state check that belongs to the reconciler. Plus the Worker is invoked once per ticket; the reconciler ticks repeatedly, so retargeting from the reconciler closes the race window where the Worker happened to create the impl PR before the spec merge completed.

- **Add `base_ref` to `PRState` and let the rule predicate check `ctx.pr.base_ref != default_branch` directly.** Rejected because it expands the GraphQL observer's query surface (one more field to fetch per PR) for marginal gain — the `count_completed`-based one-shot gate on the predicate already keeps the rule from re-firing without snapshot-level base information. Cleaner: keep the snapshot minimal, defer the host call to the action handler, and rely on the ExecutionLog gate for one-shot idempotence (the same pattern `dispatch_reviewer_impl` and `dispatch_fixer_impl` already use).

- **Skip the predicate-level one-shot gate and rely only on the action-handler's internal short-circuit.** Rejected — this is the deadlock path. The action handler's `current_base != spec_branch_name` check makes a misfire harmless but doesn't keep the predicate False on subsequent ticks. Because `evaluate_with_rule` is first-match-wins and the retarget rule sits at precedence 138 (one slot before `dispatch_reviewer_impl` at 140), a predicate that keeps matching shadows the Reviewer dispatch forever, and the `foreman:impl-review` label never advances. The one-shot gate is the predicate-level mechanism that flips False on tick N+1 so precedence 140 gets a turn.

- **Place the new rule at precedence 142 (after `dispatch_reviewer_impl`).** Rejected — the retarget must precede the reviewer dispatch so the Reviewer sees the post-retarget base. Precedence 138 is correct.

- **Place the new rule at precedence 132 (immediately after `dispatch_worker`).** Tempting (earliest moment the impl PR exists), but rejected — the impl PR's `foreman:impl-review` label hasn't been applied yet at precedence 132 (Worker applies it on completion). Precedence 138 keys on `foreman:impl-review`, which is the cleanest "Worker has finished, Reviewer hasn't started" gate.

- **Do nothing; rely on the operator to leave auto-delete-on-merge off.** Rejected per the issue body: a single setting-toggle activates the failure across every future stacked-PR merge. The autonomous loop cannot rely on a repo-setting invariant the bot doesn't enforce.

## Open questions

(None. The issue body specifies the failure mode in detail, names the crash site exactly, and offers both layers explicitly. The codebase has the helper extension seams in place (`worktree._fetch_origin_branch` self-heal infra from foreman#122; D9 retarget block from foreman#279 ready to extract). Precedence 138 is the obviously-correct slot per the existing rule ordering. Calibrated bias check per `CLAUDE.md`'s Decision-4 lens: no GoF pattern fits cleanly; this is straightforward error-recovery + earlier scheduling of a known-good state-machine step.)

## Out of scope

- **Replacing the worktree-based `git diff` with the GitHub Files API.** Per the issue's Out-of-scope list; performance + accuracy reasons preserved.
- **Changing the spec PR's auto-delete policy.** Per the issue's Out-of-scope list; operator decision.
- **Moving Reviewer-on-impl to run BEFORE spec PR merges.** Per the issue's Out-of-scope list; would break the stacked-PR contract.
- **Promoting `_fetch_origin_branch` / `_resolve_default_branch` to public names.** Thin-wrapper approach used instead to keep scope tight.
- **Adding `base_ref` to `PRState`.** Helper reads base via host at action-execution time.
- **Removing the existing D9 retarget block at `_handle_attempt_merge(target="impl")`.** Refactored to delegate to the shared helper, but the call site is preserved as defense in depth against operators who remove the `foreman:impl-review` label.
- **Refactoring the Worker's `_create_pull_with_base_fallback`** (worker.py:450-488). That handles a different activation point (PR creation time, 422 invalid-base) and stays as-is.
- **Updating the architecture stability plan doc.** The plan stays the source of truth; merged-PR + CHANGELOG carry the propagation status (same pattern as foreman#286 / D4 propagation).
- **Empirical end-to-end smoke test with `delete_branch_on_merge=true`** on a real repo. Listed as an AC bullet in the issue body, but this is operator-driven verification (out-of-band of Worker automation) — recommended as a post-merge dogfood step rather than a Worker-automatable check.

## References

- foreman#294 — this ticket. Surfaces the Reviewer-on-impl crash mode.
- foreman#279 / PR #280 — D9 impl PR retarget guard. Same retarget logic, called from a later point in the state machine; this spec extracts the helper for reuse at the earlier point.
- foreman#122 — origin-PR-merged-and-deleted self-heal in `worktree._fetch_origin_branch`. The `couldn't-find-remote-ref` prune-stale-ref path this spec reuses for Layer A.
- foreman#291 / PR #292 — per-poll clone auto-fetch refresh. Introduced `fetch_origin_default_branch` as a public wrapper around `_fetch_origin_branch`; this spec mirrors that pattern with `fetch_origin_branch` and `resolve_default_branch`.
- foreman#63 — issue close-out gating; rationale for the no-closing-keyword constraint on the impl PR body.
- `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md` — Decision 4's calibrated bias toward structural patterns; Phase 2 ticket #2 referenced by the issue body.
- Source pointers used by this spec:
  - `packages/foreman/src/foreman/roles/reviewer.py:275-312` — `_get_pr_diff`, the crash site.
  - `packages/foreman/src/foreman/roles/reviewer.py:296-302` — direct `git fetch` call replaced by the shared wrapper.
  - `packages/foreman/src/foreman/worktree.py:657-725` — `_fetch_origin_branch` with foreman#122 self-heal; gains a public wrapper.
  - `packages/foreman/src/foreman/worktree.py:622-654` — `_resolve_default_branch`; gains a public wrapper.
  - `packages/foreman/src/foreman/worktree.py:728-746` — `fetch_origin_default_branch` (foreman#291); the shape this spec mirrors.
  - `packages/foreman/src/foreman/reconciler/actions.py:446-496` — existing D9 retarget block extracted into the shared helper.
  - `packages/foreman/src/foreman/reconciler/actions.py:50-79` — `Action` enum gains `RETARGET_IMPL_PR_TO_DEFAULT`.
  - `packages/foreman/src/foreman/reconciler/actions.py:618-732` — `execute_action`; gains a new branch.
  - `packages/foreman/src/foreman/reconciler/rules.py:476-498` — impl-side predicate locations.
  - `packages/foreman/src/foreman/reconciler/rules.py:683-689` — `_PROGRESS_RULES`; gains the new rule at precedence 138.
  - `packages/foreman/src/foreman/reconciler/host.py:106-151` — `retarget_pr_base`, `get_pr_base_ref`, `is_pr_merged_for_branch`, `get_default_branch` host methods (foreman#279); reused by the shared helper.
  - `packages/foreman/src/foreman/roles/worker.py:450-488` — `_create_pull_with_base_fallback`; preserved as defense in depth (not modified by this spec).
  - `packages/foreman/tests/test_roles_reviewer.py:254` — `_seed_clone_with_spec_branch` fixture definition. The new `_seed_clone_with_bare_origin` helper added by this spec composes with it (initializes a separate bare repo, repoints origin, pushes branches) so the missing-ref simulation operates on a real bare origin instead of the working clone itself.
