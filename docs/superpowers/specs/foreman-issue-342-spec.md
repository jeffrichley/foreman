# Spec: Worker BLOCKED-retry idempotency — detect existing impl PR before create (issue #342)

## Goal

Fix the Worker so that re-dispatch after a BLOCKED outcome does not crash with GitHub 422 (`A pull request already exists`). On retry, the Worker MUST detect the already-open impl PR for `foreman/impl-<N>` BEFORE calling `repo.create_pull(...)`, re-emit BLOCKED / CLEAN / NEEDS_HELP based on the existing PR's GitHub-reported check state, and NEVER duplicate the create-PR call. Closes the autonomous-loop wedge first surfaced on foreman#337 (Implementing instance #5 → Failed at 04:21:31 UTC 2026-06-18). Tracks issue #342.

## Acceptance criteria

- [ ] `packages/foreman/src/foreman/roles/worker.py` adds a helper named `_find_open_impl_pr(repo, owner, branch) -> PullRequest | None` (or generalizes `_find_spec_pr` into a shared `_find_open_pr_by_head_branch` helper that both callers reuse — see Sub-request 1). The helper is a thin wrapper over `repo.get_pulls(state="open", head=f"{owner}:{branch}")`.
- [ ] In `_run_worker_core`, BEFORE the `host.push_branch(...)` / `_verify_impl_branch_remote_state(...)` / `_create_pull_with_base_fallback(...)` sequence that runs when `final_outcome == "implemented"`, the Worker calls the new helper with `branch=impl_branch_name` (already computed earlier in the function).
- [ ] When the helper returns a `PullRequest` (existing-PR case), the Worker:
  - [ ] Sets `pr_url = existing_pr.html_url` and `impl_pr_number = existing_pr.number` directly from the lookup result.
  - [ ] Does NOT call `host.push_branch`, `_verify_impl_branch_remote_state`, or `_create_pull_with_base_fallback`.
  - [ ] Re-derives `final_did_check_pass` from the existing PR's GitHub-reported state (PyGithub `pr.mergeable_state in {"clean", "unstable"}` — same set as `_CI_PASSING_STATES` in `foreman.v4.pygithub_git_provider`) instead of trusting the in-worktree `_run_check_command` rerun.
  - [ ] Logs a structured `_log.info(...)` line ("Worker BLOCKED retry: existing impl PR #N found for branch %r; skipping push+create_pull") so the dogfood replay is searchable.
- [ ] When the helper returns `None` (first-run case), behavior is byte-identical to today: push, verify, create, with the same `_create_pull_with_base_fallback` invocation arguments.
- [ ] New unit test in `packages/foreman/tests/v4/roles/test_worker_core.py`: simulate a Worker invocation where `repo.get_pulls(state="open", head="testowner:foreman/impl-341")` returns a single mock `PullRequest`. Assertions: `mock_repo.create_pull.assert_not_called()`, `mock_host.push_branch.assert_not_called()`, and `result.pr_url` equals the existing PR's `html_url`. The Worker LLM is still dispatched (the LLM's actual idempotency on existing-PR retry is out of scope for this ticket); the assertion is strictly on the post-LLM push/create surface.
- [ ] New unit test (same file): the existing first-run regression — when `repo.get_pulls` returns `[]` for the impl branch, `create_pull` IS called and the kwargs match today's contract (`base=wt_result.base_branch`, `head=impl_branch_name`).
- [ ] v4 e2e regression coverage: extend or add a test under `packages/foreman/tests/v4/states/test_implementing.py` (or `packages/foreman/tests/v4/test_phase8_real_fork.py` if that's where end-to-end Worker re-dispatch lives) that asserts a Worker re-invocation after BLOCKED with an existing impl PR does NOT result in an ERROR / Failed state. Concretely: drive `ImplementingState` through two consecutive subprocess dispatches; the second invocation's emitted `Outcome.kind` is BLOCKED or CLEAN, never ERROR.
- [ ] No new failures: `just check` exits zero. New-failures count vs baseline == 0.
- [ ] No regression on the `_find_spec_pr` lookup contract (if Sub-request 1 generalizes the helper). Existing callers of `_find_spec_pr` continue to receive the same result type and None-on-empty behavior.

## Approach

**Pattern (per Decision 4 of the architecture-stability plan):** This is a textbook "make the right thing easy" + idempotency guard. No GoF pattern fits — the change is a defensive idempotency probe at a narrow side-effecting boundary (the `create_pull` REST call). The closest principle is the HTTP idempotency-key pattern: detect prior success and short-circuit rather than retrying the create.

The Worker (`packages/foreman/src/foreman/roles/worker.py`) already owns a direct PyGithub `Repository` handle (`repo`, obtained via `worker_client.get_repo(actual_repo_slug)` in `_run_worker_core`) and already uses the `repo.get_pulls(state="open", head=...)` query in `_find_spec_pr` (line ~492). The same query, retargeted at `impl_branch(N)`, gives us the open impl PR if any prior Worker dispatch opened one. We do NOT need to introduce a `GitHostProvider.find_open_pr_by_head_branch` method; the issue body references that name from the v4 `GitProvider` Protocol (which is a separate abstraction the Worker doesn't consume), and adding it to `GitHostProvider` is unnecessary scope creep — the inline PyGithub query is what `_find_spec_pr` already does and matches house style.

The new helper either replaces `_find_spec_pr` with a shared `_find_open_pr_by_head_branch(repo, owner, branch) -> PullRequest | None` (preferred — DRY win, one query site, both spec and impl callers use it) or adds a parallel `_find_open_impl_pr(repo, owner, branch)`. Sub-request 1 commits to the generalization because it costs nothing extra and removes the long-term drift risk of two near-identical helpers.

The critical decision is what to emit on the existing-PR branch. Today, `_run_worker_core` always re-runs `_run_check_command` in the worktree as orchestrator ground truth (D4 in the Worker's docstring), then flattens to v4 outcome in `_run_worker_for_v4`:

```
if llm.outcome == "implemented" and pr_url is not None:
    if core_result.final_did_check_pass:  status = "ci_passing"   (CLEAN)
    else:                                  status = "ci_in_flight" (BLOCKED)
```

On retry, the in-worktree `_run_check_command` is unreliable as a proxy for "is the impl PR's CI green" — the worktree was freshly created from `origin/<dev_base>`, the LLM may have re-run unrelated code, and the answer we actually want is the PR's GitHub-reported CI status. Per the issue's Approach point 2, we read the existing PR's `mergeable_state`:

- `mergeable_state in {"clean", "unstable"}` → treat as CI passing → `final_did_check_pass = True` → `_run_worker_for_v4` flattens to `ci_passing` → CLEAN → `ImplReviewState`.
- Any other state ("blocked", "unknown", "dirty", failure shapes) → treat as still in flight → `final_did_check_pass = False` → BLOCKED → `ImplementingState` (re-enter for the next Poller tick).

This logic is the SAME `_CI_PASSING_STATES` frozenset already defined at `packages/foreman/src/foreman/v4/pygithub_git_provider.py:61`. We hoist that constant to a shared location (`foreman.v4.pygithub_git_provider` keeps the canonical definition; the Worker imports it) so there's one home for the rule.

Out-of-band: the existing `_run_check_command` rerun still happens (it's threaded through `baseline_failures` computation and stats logging). We KEEP that rerun on the existing-PR path so `log_worker_run` continues to report a consistent `new_failures_count` shape. The change is strictly: on existing-PR, `final_did_check_pass` is overridden from PyGithub's `mergeable_state` instead of from the rerun. The rerun's output still feeds telemetry.

Idempotency on the LLM side is out of scope per the issue's "Out of scope" section — the LLM may re-implement (producing duplicate local commits that aren't pushed) and that's a separate dimension. The fix here is strictly to make the post-LLM Python-side push+create idempotent.

## Sub-requests (topologically sorted)

1. **Generalize the PR-lookup helper.** Rename `_find_spec_pr(repo, owner, branch)` in `packages/foreman/src/foreman/roles/worker.py` to `_find_open_pr_by_head_branch(repo, owner, branch)` and update both its docstring (drop "spec" framing) and its existing caller at the spec-PR lookup site (the line that today reads `spec_pr = _find_spec_pr(repo, owner=owner, branch=spec_branch_name)`). The return type and None-on-empty behavior do not change.
2. **Export the CI-passing states.** In `packages/foreman/src/foreman/v4/pygithub_git_provider.py`, change `_CI_PASSING_STATES` to a public name `CI_PASSING_MERGEABLE_STATES` (keep `_CI_PASSING_STATES` as a deprecated alias for one release if any external test reaches in — grep confirms none do, so a clean rename is fine). Update the existing `get_pr_state` caller.
3. **Add existing-PR detection in the implemented branch.** In `_run_worker_core` inside `packages/foreman/src/foreman/roles/worker.py`, immediately before `host.push_branch(worktree_path=wt_path, branch=impl_branch_name)` in the `if final_outcome == "implemented":` block, call `existing_impl_pr = _find_open_pr_by_head_branch(repo, owner=owner, branch=impl_branch_name)`. Wrap the existing push/verify/create sequence in `if existing_impl_pr is None:` so the original three-call sequence only runs in the first-PR case.
4. **Existing-PR branch wiring.** In an `else:` arm under sub-request 3, set `pr_url = existing_impl_pr.html_url`, log the `_log.info("Worker BLOCKED retry: existing impl PR #%d found for branch %r; skipping push+create_pull", existing_impl_pr.number, impl_branch_name)` line, and override `final_did_check_pass` from `existing_impl_pr.mergeable_state in CI_PASSING_MERGEABLE_STATES`.
5. **Provenance + auto-close sanitization gating.** `_ensure_provenance_trailers` and `_sanitize_head_commit_auto_close` (currently called inside the `if final_outcome == "implemented":` block before the push) are tied to "we are about to push a NEW commit." On the existing-PR branch we are not pushing anything new; gate both calls so they only run when `existing_impl_pr is None`. Document in the call-site comments that the existing-PR branch deliberately skips the amend backstops (the commits being attributed are already on origin from the prior run).
6. **Unit test: existing-PR retry skips push and create.** In `packages/foreman/tests/v4/roles/test_worker_core.py`, add an async test `test_worker_existing_impl_pr_skips_push_and_create_pull` that mirrors the existing `test_worker_opens_impl_pr_with_base_main_not_spec_branch` setup but configures `mock_repo.get_pulls` to return a single `PullRequest`-shaped MagicMock for the `head="testowner:foreman/impl-341"` query. Assertions: `mock_repo.create_pull.assert_not_called()`, `mock_host.push_branch.assert_not_called()`, `result.pr_url` equals the existing mock PR's `html_url`. Use the same patches scaffolding (WorktreeManager, build_role_resources, _run_check_command, _read_spec_doc_from_branch, _sanitize_head_commit_auto_close, _verify_impl_branch_remote_state, load_project_instructions, log_worker_run).
7. **Unit test: existing-PR + CI clean → CLEAN outcome.** Same scaffolding as sub-request 6. Set the mock PR's `mergeable_state = "clean"`. Run `run_worker_cli` (or `_run_worker_for_v4`) and assert the emitted `Outcome.kind == OutcomeKind.CLEAN` with `artifacts.pr_number` equal to the existing PR's number.
8. **Unit test: existing-PR + CI in flight → BLOCKED outcome.** Same as sub-request 7 but with `mergeable_state = "blocked"`. Assert `Outcome.kind == OutcomeKind.BLOCKED`.
9. **Unit test: no existing PR → unchanged first-run path.** Defensive — explicitly assert that with `mock_repo.get_pulls` returning `[]`, `create_pull` IS called with the existing `base=wt_result.base_branch`, `head=impl_branch_name` kwargs. Today's `test_worker_opens_impl_pr_with_base_main_not_spec_branch` already covers most of this; add an explicit `mock_host.push_branch.assert_called_once_with(worktree_path=ANY, branch="foreman/impl-341")` so the regression bar covers the push side too.
10. **v4 e2e regression.** In `packages/foreman/tests/v4/states/test_implementing.py`, add a test that drives two consecutive `ImplementingState` dispatches through the role-dispatch seam where the first emits BLOCKED with `artifacts.pr_number = 9001` and the second emits BLOCKED again (CI still in flight) — assert the second Outcome's `kind` is `OutcomeKind.BLOCKED`, NOT `OutcomeKind.ERROR`. The exact seam point depends on whether existing tests stub at `RoleDispatchState` or at `run_worker_cli`; copy the closest existing pattern.

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/src/foreman/roles/worker.py` | Rename `_find_spec_pr` → `_find_open_pr_by_head_branch`; update its docstring; add existing-PR branch in `_run_worker_core`'s `implemented` arm; gate push/verify/create/`_ensure_provenance_trailers`/`_sanitize_head_commit_auto_close` on the no-existing-PR case; override `final_did_check_pass` from `mergeable_state` on the existing-PR case. |
| `packages/foreman/src/foreman/v4/pygithub_git_provider.py` | Rename `_CI_PASSING_STATES` → `CI_PASSING_MERGEABLE_STATES` (public). |
| `packages/foreman/tests/v4/roles/test_worker_core.py` | Add four async tests: existing-PR skips push/create; existing-PR + clean → CLEAN outcome; existing-PR + blocked → BLOCKED outcome; first-run regression (no-PR → push + create_pull called). |
| `packages/foreman/tests/v4/states/test_implementing.py` | Add e2e regression: two consecutive Worker dispatches with an existing impl PR don't transition to Failed/ERROR. |

No other files change. The v4 `GitProvider` Protocol (`foreman.v4.git_provider`) is NOT modified — the Worker uses PyGithub directly, same as `_find_spec_pr` does today.

## Alternatives considered

- **Add `find_open_pr_by_head_branch` to `GitHostProvider`.** Would mirror the v4 `GitProvider.find_open_pr_by_head_branch` shape and centralize the lookup in the abstraction layer. Rejected because (a) `GitHostProvider` is the legacy role-side facade and we are not gaining new consumers, (b) the inline `repo.get_pulls(state="open", head=...)` matches the existing `_find_spec_pr` pattern in the same file, and (c) introducing a new abstract method requires updating `GitHubProvider` + every test double — a strictly larger blast radius for no win.
- **Short-circuit BEFORE LLM dispatch on existing-PR detection.** Detect the existing PR right after `_find_spec_pr` and skip `provider.run_agent(...)` entirely. Saves cost on every BLOCKED retry. Rejected for this ticket because (a) the issue's stated approach is "before `create_pull`", and (b) the LLM may legitimately want to look at the existing PR's check output and reason about whether to give up — that's a richer design question that warrants its own ticket. Filed as a follow-up consideration in Out-of-scope.
- **Catch the GithubException 422 from `create_pull` and lookup-after-the-fact.** Wrap `_create_pull_with_base_fallback` in a try/except that catches the specific "A pull request already exists" 422 and falls back to looking up the existing PR. Rejected because pre-emptive detection is cleaner — the existing-PR case is a known, expected state under foreman#453 BLOCKED-exempts-retry-cap, not an exceptional condition.
- **Do nothing; bound the BLOCKED retry cap.** Cap BLOCKED retries at 1, letting the second invocation crash but then NeedsHelp. Rejected because that defeats foreman#453's intentional unbounded-BLOCKED design (CI runs can take minutes; the cap would burn the loop on slow CI).

## Open questions

None. The implementation surface, the `mergeable_state` rule, and the test seams all have direct analogues already in the codebase. Confidence: high.

## Out of scope

- LLM-side idempotency on retry. The Worker LLM still re-implements from a fresh worktree on BLOCKED retry; the prompt is not updated to recognize "your PR already exists, just poll status." That's a richer prompt-design question worth its own ticket.
- Modifying the BLOCKED retry mechanism itself (foreman#453's exempt-from-retry-cap rule is correct and stays).
- Per-state retry caps. Separate concern, separate ticket.
- Adding `find_open_pr_by_head_branch` to `GitHostProvider`. The inline PyGithub query in `worker.py` matches existing convention.
- Polling the impl PR's GitHub Checks API for detailed per-check status (foreman#317 territory). `mergeable_state` is the coarse signal we already trust for CI green/red in `pygithub_git_provider.get_pr_state`; richer per-check granularity is downstream.
- Behavior on impl PR baseline CI failure. The existing `blocked-by-baseline-failure` path stays. This ticket strictly fixes the duplicate-create-pull crash; CI failures the impl PR's CI itself reports are a separate dimension that the Reviewer-on-impl + Fixer cycle handles.
