# Spec: v4 state-machine coverage for the comment+label injection path (issue #335)

## Goal
Restore the end-to-end coverage of PR #331's spec-side comment+label injection feature that was displaced when PR #333 cut the substrate over from v3 to v4. Add v4-shape integration tests against `_run_planner_core`, `_run_reviewer_core`, and `_run_fixer_core`; add the impl-side "never fetched comments" regression assertions (the v3 versions lived in the old `test_roles_*.py` files); and add explicit unit coverage for `V4IdentityRegistry.get_role_bot_logins`, which was added in PR #333 but has no direct tests today. Tracks [foreman#335](https://github.com/jeffrichley/foreman/issues/335).

## Acceptance criteria
- [ ] A new test file `packages/foreman/tests/v4/roles/test_prompt_injection.py` exists and exercises `_run_planner_core`, `_run_reviewer_core` (target=spec_pr), and `_run_fixer_core` (target=spec_pr) against an instrumented `provider.run_agent` mock that captures the `user_prompt` kwarg.
- [ ] One Planner test seeds two `CommentRef` entries (one from `foreman-planner[bot]`, one from a human `alice`) plus labels `{"foreman:state-planning", "priority:high"}` and asserts the captured `user_prompt` contains `### @alice`, does NOT contain `### @foreman-planner[bot]`, and contains a `## Labels\nforeman:state-planning, priority:high` block.
- [ ] One Reviewer-on-spec test (head branch `foreman/issue-<N>`) seeds one human comment via the PyGithub `_FakeIssue.get_comments()` shape and asserts the captured `user_prompt` contains `## Comments` + `### @alice`, with NO `## Labels` section (Reviewer never gets labels).
- [ ] One Fixer-on-spec test (target='spec_pr') seeds one human comment via the same fake-Issue shape and asserts the captured `user_prompt` contains `## Comments` + `### @alice`, with NO `## Labels` section.
- [ ] A Reviewer-on-impl regression test (head branch `foreman/impl-<N>`, parsed to `target=impl_pr`) asserts `fake_issue.get_comments.call_count == 0` AND the captured `user_prompt` does NOT contain `## Comments`.
- [ ] A Fixer-on-impl regression test (target='impl_pr') asserts `fake_issue.get_comments.call_count == 0` AND the captured `user_prompt` does NOT contain `## Comments`.
- [ ] A Worker regression test asserts `mock_host.get_issue_comments.assert_not_called()` after `_run_worker_core` returns on a normal implemented run (the Worker never touches the helper module; this pins by construction).
- [ ] Three new unit tests added to `packages/foreman/tests/v4/test_identity.py` covering `V4IdentityRegistry.get_role_bot_logins`:
  - `test_get_role_bot_logins_returns_one_login_per_role_when_slugs_collapse`: all four roles share one slug → set collapses to one `"{slug}[bot]"` entry.
  - `test_get_role_bot_logins_returns_all_four_when_slugs_differ`: per-role `AppMetadata.slug` distinct → set has four entries.
  - `test_get_role_bot_logins_caches_metadata_per_role`: three repeated calls fire exactly four `fetch_app_metadata` calls total (one per role on first invocation).
- [ ] `just check` exits zero; `new_failures_count == 0` per the project's pre-push gate.

## Approach
This is a **test-only ticket**. No production code changes. The work splits cleanly along the two test files named in the issue body, plus shared scaffolding for the role-core tests.

**Layer 1 — V4 role-core integration tests (`test_prompt_injection.py`).**
Today `packages/foreman/tests/v4/roles/test_worker_core.py` (foreman#341 / foreman#342) is the established pattern for exercising `_run_<role>_core` with every collaborator mocked. Mirror that exact shape — same `_build_v4_config` helper for `V4Config` setup, same `patch("foreman.roles.<role>.WorktreeManager", ...)` + `patch("foreman.roles.<role>.build_role_resources", ...)` + `patch("foreman.roles.<role>.load_project_instructions", ...)` ring, and an `AsyncMock` `provider.run_agent` whose `call_args.kwargs["user_prompt"]` is the assertion subject. The displaced v3 tests at `packages/foreman/tests/test_roles_planner.py` (foreman#328 era) provide the exact `CommentRef`-seeding + assertion vocabulary; the v4 version simply moves it onto the new core function.

The Planner test routes comments through `host.get_issue_comments` (the GitHostProvider abstraction Planner already uses). The Reviewer + Fixer tests route comments through a `_FakeIssue.get_comments()` shape on the PyGithub `repo.get_issue(N)` return value — that mirrors the production code path at `reviewer.py:551` and `fixer.py:624`, which iterate `issue.get_comments()` directly. The `identity_registry` mock returns `{"foreman-planner[bot]", "foreman-reviewer[bot]", "foreman-fixer[bot]", "foreman-worker[bot]"}` from `get_role_bot_logins()` so `filter_bot_self_comments` has something to filter against.

**Layer 2 — Impl-side regression pins.** The three impl-side tests live in the same file as the spec-side tests (one file per concern is cleaner than splintering across three test files). The Reviewer-on-impl test sets `pr.head.ref = "foreman/impl-<N>"` so `_parse_review_branch` returns `target="impl_pr"`; the Fixer-on-impl test passes `target="impl_pr"` directly into `_run_fixer_core`. Both assert on `MagicMock.call_count == 0` against the fake Issue's `get_comments` method — the same call-counter discipline PR #331 used in `test_roles_worker.py` on main before #333 displaced it. The Worker case is a one-line `mock_host.get_issue_comments.assert_not_called()` added to the existing `test_worker_first_run_with_no_existing_pr_calls_push_and_create_pull` (or its own micro-test) — Worker's `_run_worker_core` never imports `_prompt_helpers`, so this is regression-by-construction; the assertion makes the "Worker doesn't touch this surface" contract explicit.

**Layer 3 — `V4IdentityRegistry.get_role_bot_logins` unit tests.** `packages/foreman/tests/v4/test_identity.py` already patches `mint_installation_token` for the existing `get_role_token` tests. The new tests patch `foreman.v4.identity.fetch_app_metadata` instead. Mirror the v3 trio at `packages/foreman/tests/test_identity.py:586-643` line-for-line on the v4 surface — same three scenarios (slug-collapse, per-role-distinct, cache-amortized). The `AppMetadata` import + `_make_metadata`-style helper come over with minor adjustments for the v4 `AppsConfig` / `OrchestratorConfig` shape.

**Pattern naming (per `CLAUDE.md` Decision 4):** No GoF pattern applies — this is straightforward test coverage. The Google engineering principle that frames it is **"tests pin contracts that survive substrate cutovers"**: PR #331 shipped a real behavior, the v3 e2e tests pinned it at the orchestration boundary, and the v4 substrate cutover (PR #333) re-shaped the orchestration boundary without porting the assertions. The fix is to pin the same contract on the v4 boundary so the *next* substrate cutover (or any large refactor inside `_run_<role>_core`) catches a regression at PR time instead of in dogfood.

## Sub-requests (topologically sorted)
1. Create `packages/foreman/tests/v4/roles/test_prompt_injection.py`. Module docstring cites foreman#335, foreman#328, PR #331, and PR #333; explains the file is the v4 successor to the v3 prompt-injection assertions that lived in `tests/test_roles_planner.py` / `tests/test_roles_reviewer.py` / `tests/test_roles_fixer.py`.
2. In the new file, copy `_build_v4_config` from `tests/v4/roles/test_worker_core.py:45-75` (small `V4Config` constructor) — or refactor into a shared helper if the imports are cheap. Either choice is acceptable; pick whichever yields fewer cross-file imports.
3. In the new file, add `_seed_comment_refs(...)` and `_seed_fake_issue_comments(...)` helpers. The first returns `list[CommentRef]` for the Planner path (which goes through `host.get_issue_comments`). The second returns a list of `_FakePyGithubIssueComment(user=NS(login=...), created_at=..., body=...)` objects for the Reviewer/Fixer paths (which iterate `issue.get_comments()` directly per `reviewer.py:551` / `fixer.py:624`).
4. Add `test_planner_injects_filtered_comments_and_labels`:
   - Build `V4Config` via `_build_v4_config`.
   - `mock_host = MagicMock()`; `mock_host.get_issue.return_value = IssueRef(number=N, title="t", body="b", labels=["foreman:state-planning", "priority:high"])`.
   - `mock_host.get_issue_comments.return_value = _seed_comment_refs([("foreman-planner[bot]", ...), ("alice", ...)])`.
   - `identity_registry.get_role_bot_logins.return_value = {"foreman-planner[bot]", "foreman-reviewer[bot]", "foreman-fixer[bot]", "foreman-worker[bot]"}`.
   - Patch `WorktreeManager`, `build_role_resources` (returning the host + token + a stub client), `load_project_instructions` (return `None`), `log_planner_run`. Patch `provider.run_agent` as `AsyncMock` returning a synthesized `PlannerOutput` + `UsageInfo()`.
   - `await _run_planner_core(issue_url=..., config=cfg, project_name="p", worktrees_root=tmp_path, provider=mock_provider, identity_registry=identity_registry)`.
   - Capture `user_prompt = mock_provider.run_agent.call_args.kwargs["user_prompt"]`.
   - Assert `"### @alice"` in `user_prompt`; `"### @foreman-planner[bot]"` NOT in `user_prompt`; `"## Labels\nforeman:state-planning, priority:high"` in `user_prompt`.
5. Add `test_planner_emits_no_comments_section_when_issue_has_none`:
   - Same scaffolding but `mock_host.get_issue_comments.return_value = []` and `mock_host.get_issue.return_value.labels = []`.
   - Assert `"## Comments"` NOT in `user_prompt` and `"## Labels"` NOT in `user_prompt`.
6. Add `test_reviewer_on_spec_injects_filtered_comments`:
   - Build the v4 config + identity registry mock as above.
   - Mock the PyGithub side: `mock_repo.get_pull.return_value = mock_pr` with `pr.head.ref = "foreman/issue-<N>"`, `pr.head.sha = "deadbeef"`, `pr.base.ref = "main"`, `pr.title = "..."`, `pr.body = "..."`. `mock_repo.get_issue.return_value = fake_issue` where `fake_issue.title = "t"`, `fake_issue.body = "b"`, and `fake_issue.get_comments = MagicMock(return_value=_seed_fake_issue_comments([("alice", ...)]))`.
   - Patch `foreman.roles.reviewer.build_role_resources` returning `(mock_host, "fake-token", mock_client)`; `mock_client.get_repo.return_value = mock_repo`. Patch `WorktreeManager.attach`, `_get_pr_diff` (return `""`), `_read_spec_doc` (return `"# Spec\n"`), `load_project_instructions` (return `None`), `log_reviewer_run`, and `provider.run_agent` as `AsyncMock` returning a synthesized `ReviewerOutput` + `UsageInfo()`. The `pr.create_review` call also needs to be a no-op mock; mirror what the existing v4 reviewer outcome tests do.
   - Capture `user_prompt` from `mock_provider.run_agent.call_args.kwargs["user_prompt"]`.
   - Assert `"## Comments"` in `user_prompt` and `"### @alice"` in `user_prompt`; assert `"## Labels"` NOT in `user_prompt`.
7. Add `test_fixer_on_spec_injects_filtered_comments`:
   - Same shape as the Reviewer test but call `_run_fixer_core(target="spec_pr", ...)`. Mock `_find_role_pr` to return `mock_pr`, `_latest_reviewer_review_comment` to return a synthetic review body, `_extract_findings_from_review_comment` to return `[]`. The `mock_repo.get_issue.return_value.get_comments` seam stays the same as the Reviewer test.
   - Capture `user_prompt`; assert `"## Comments"` + `"### @alice"` present, `"## Labels"` absent.
8. Add `test_reviewer_on_impl_does_not_fetch_comments`:
   - Same scaffolding as the spec_pr Reviewer test but set `pr.head.ref = "foreman/impl-<N>"` so `_parse_review_branch` returns `target="impl_pr"`. Use `WorktreeManager.attach_impl` instead of `attach`.
   - `fake_issue.get_comments = MagicMock(return_value=[<a populated comment to prove the path was skipped, not just empty>])`.
   - Assert `fake_issue.get_comments.call_count == 0` AND `"## Comments"` NOT in captured `user_prompt`.
9. Add `test_fixer_on_impl_does_not_fetch_comments`:
   - Same shape as sub-request 7 but call `_run_fixer_core(target="impl_pr", ...)`.
   - `fake_issue.get_comments = MagicMock(return_value=[<a populated comment>])`.
   - Assert `fake_issue.get_comments.call_count == 0` AND `"## Comments"` NOT in captured `user_prompt`.
10. Add `test_worker_does_not_fetch_comments`:
    - Reuse the existing `_build_existing_impl_pr_scaffold(tmp_path, existing_impl_pr=None)` helper from `tests/v4/roles/test_worker_core.py:282`.
    - Run `_run_worker_core` as in `test_worker_first_run_with_no_existing_pr_calls_push_and_create_pull`.
    - Assert `mock_host.get_issue_comments.assert_not_called()`. (The Worker never imports the helpers, but this pin makes the contract explicit so a future Worker change that accidentally calls the method fails this test.)
11. Open `packages/foreman/tests/v4/test_identity.py`. Add `from foreman.auth import AppMetadata` and `from unittest.mock import patch` at the top if not already imported. Add a `_fake_app_metadata(app_id: int, slug: str) -> AppMetadata` helper near the top (after `_orchestrator`).
12. Add `test_get_role_bot_logins_returns_one_login_per_role_when_slugs_collapse`:
    - Mirror `tests/test_identity.py:586-598`. Patch `foreman.v4.identity.fetch_app_metadata` to return a single `AppMetadata(app_id=1, slug="foreman-planner", name="x")` for all calls.
    - Construct the registry, call `get_role_bot_logins()`, assert `logins == {"foreman-planner[bot]"}`.
13. Add `test_get_role_bot_logins_returns_all_four_when_slugs_differ`:
    - Mirror `tests/test_identity.py:601-626`. Patch `foreman.v4.identity.fetch_app_metadata` with a `_meta_per_role(app_id, key_path)` side_effect that maps `_apps()`'s four distinct `app_id` values (1, 2, 3, 4) to four distinct slugs (`foreman-planner`, `foreman-reviewer`, `foreman-fixer`, `foreman-worker`).
    - Assert `logins == {"foreman-planner[bot]", "foreman-reviewer[bot]", "foreman-fixer[bot]", "foreman-worker[bot]"}`.
14. Add `test_get_role_bot_logins_caches_metadata_per_role`:
    - Mirror `tests/test_identity.py:629-643`. Patch `foreman.v4.identity.fetch_app_metadata` with `return_value=_fake_app_metadata(1, "foreman-planner")` and a `MagicMock` to count calls.
    - Call `reg.get_role_bot_logins()` three times.
    - Assert `mock_fetch.call_count == 4` (one per role on first invocation; zero on the next two).
15. Run `just check` from the worktree root. Confirm exit zero and `new_failures_count == 0`. If any of the v4 role-core mocks need adjustment for the actual `provider.run_agent` signature drift between the worker test file and the new role tests (Planner/Reviewer/Fixer take different `allowed_tools`, `output_model`, etc.), adjust inline — the goal is a passing suite, not a perfect copy of the worker fixture.

## File-level changes
| File | Change |
|------|--------|
| `packages/foreman/tests/v4/roles/test_prompt_injection.py` | NEW. Three spec-side injection tests, two impl-side regression tests, one Worker regression test, plus seeding helpers and a copy (or import) of `_build_v4_config`. |
| `packages/foreman/tests/v4/test_identity.py` | Add `_fake_app_metadata` helper + three `get_role_bot_logins` tests at the bottom (after the existing `test_orchestrator_uses_same_installation_repo`). |

No production code changes. No changes to: `packages/foreman/src/foreman/roles/*.py`, `packages/foreman/src/foreman/v4/identity.py`, `packages/foreman/src/foreman/v4/states/*.py`, `packages/foreman/src/foreman/roles/_prompt_helpers.py`, or any existing test files outside `test_identity.py`.

## Alternatives considered
- **Drive integration through `PlanningState.execute()` instead of `_run_planner_core`.** Rejected. `PlanningState.execute` delegates to `RoleDispatcher.dispatch(...)`, which under v4 shells out to a subprocess and returns canned stdout (see `foreman.v4.role_dispatcher.FakeRoleDispatcher`). The dispatcher seam is *below* the prompt-construction code path — driving through it would never exercise `_build_user_prompt`. The `_run_<role>_core` boundary is the right level: it owns the comment fetch + filter + prompt composition and is the boundary `tests/v4/roles/test_worker_core.py` (foreman#341 / foreman#342) already pins. Following the established pattern.
- **Re-create v3-shape `run_planner` orchestration tests.** Rejected. The issue body's "Out of scope" section explicitly says "Re-creating v3-shape `run_planner` orchestration tests. v3 is gone." The v3 orchestration entry points (`run_planner` / `run_reviewer` / `run_fixer`) no longer exist; resurrecting them just to host these tests would couple us to a substrate we deleted.
- **Add a separate test file per role (`test_planner_prompt_injection.py`, `test_reviewer_prompt_injection.py`, `test_fixer_prompt_injection.py`).** Rejected. The shared seeding helpers (`_seed_comment_refs`, `_seed_fake_issue_comments`, the identity-registry mock setup) want to live in one place. Three files would either duplicate the helpers or invent a separate `_test_helpers.py` module — both heavier than the single-file solution for six related tests. The v3 layout used per-role files but those files already existed for other reasons; the v4 versions are greenfield and can be organized by concern.
- **Mock `host.get_issue_comments` for Reviewer + Fixer too** (instead of seeding through PyGithub's `issue.get_comments()` shape). Rejected. Reviewer + Fixer call `issue.get_comments()` *directly* on the PyGithub-returned Issue object (`reviewer.py:551`, `fixer.py:624`); the `GitHostProvider.get_issue_comments` abstraction is only used by Planner. Mocking the wrong seam would test a path the production code does not take and would leave the `issue.get_comments()` call un-counted — defeating the purpose of the impl-side regression assertion.
- **Skip the `V4IdentityRegistry.get_role_bot_logins` unit tests** (rely on indirect coverage via the spec-side injection tests). Rejected. Indirect coverage tells you the method *runs*, not that its three contracts (per-role enumeration, slug-collapse, cache amortization) are pinned. The v3 surface has explicit per-method tests at `tests/test_identity.py:586-643`; the v4 surface deserves the same — and the spec_doc-on-main pattern from PR #333 onward should be "every public method on `V4IdentityRegistry` has its own test."

## Open questions
- None. The `_run_<role>_core` mock surface is already established by `tests/v4/roles/test_worker_core.py`; the `CommentRef` seeding vocabulary is already established by `tests/test_roles_prompt_helpers.py` and the historical (now-deleted) `tests/test_roles_planner.py` foreman#328 tests; the `get_role_bot_logins` test shape is the v3 trio at `tests/test_identity.py:586-643`.

## Out of scope
- Re-introducing v3-shape orchestration tests for `run_planner` / `run_reviewer` / `run_fixer`. Those entry points no longer exist on main; this ticket targets the v4 successors.
- Changing the prompt-injection feature behavior (comment formatting, label sorting, bot-self filtering). PR #331's contract stands; this ticket only adds coverage that pins it on the v4 substrate.
- Migrating the `tests/test_roles_prompt_helpers.py` unit tests into `tests/v4/`. The helpers are layer-1 pure functions; the existing v3-namespaced tests still cover them correctly because the helpers haven't moved. No v4 isolation rule forbids `tests/test_*` from importing `foreman.roles._prompt_helpers`.
- Coverage for the `mcp__context7` MCP path or any other role-side tool wiring outside the prompt-injection feature.
- Refactoring `tests/v4/roles/test_worker_core.py` to share its `_build_v4_config` helper with the new file. Either copy-paste (current acceptance criteria) or move-and-import is acceptable; the choice is the Worker's call at implementation time.
- Adding hypothesis-based property tests for the helpers (per `.claude/rules/testing.md`'s guidance: example-driven prompt shapes are the right fit; no structural invariant beyond what the existing unit tests already pin).
- Adding new tests for `V4IdentityRegistry.get_role_token` — that surface is already covered by the existing nine tests in `tests/v4/test_identity.py`.
