# Spec: fold TerminalLandingObserver + SustainedBlockedObserver onto refresh-aware v4 GitProvider (issue #410)

## Goal

Fix a production 401-error bug: `TerminalLandingObserver` and `SustainedBlockedObserver` hold a statically-built `GitHostProvider` whose PyGithub client never refreshes its installation token, causing every comment-post call to fail with `BadCredentialsException: 401` after the first ~1 hour. The fix ports both observers to the refresh-aware v4 `GitProvider` (which already has the `github_factory` seam that rebuilds the client past a 3000s window) and deletes the legacy in-process orchestrator-host wiring. Tracks [foreman#410](https://github.com/jeffrichley/foreman/issues/410).

## Acceptance criteria

- [ ] `GitProvider` Protocol in `packages/foreman/src/foreman/v4/git_provider.py` gains two new methods: `get_issue_comments(*, project: str, issue_number: int) -> list[CommentRef]` and `post_issue_comment(*, project: str, issue_number: int, body: str) -> None`. `CommentRef` is imported from `foreman.git_host`.
- [ ] `PyGithubGitProvider` implements both methods using `self._repo` (which already flows through the `_gh` property's refresh seam — no new token-refresh logic required).
- [ ] `RoutingGitProvider` delegates both methods to `self._resolve(project)`.
- [ ] `FakeGitProvider` implements both methods with a seeding helper (`seed_issue_comments`) and a recorder attribute (`posted_comments`).
- [ ] `TerminalLandingObserver` and `SustainedBlockedObserver` accept `git: GitProvider` instead of `host_for_project: Callable[[str], GitHostProvider | None]`. Neither observer imports `GitHostProvider` or references `repo_slug_for`.
- [ ] `bootstrap.py` no longer constructs `per_project_git_hosts`, `_HostForProject`, or calls `build_role_resources` for the orchestrator host. Both observers receive `git=git_for_cross_project`. The dead `from foreman.git_host import GitHostProvider` and `from foreman.roles import build_role_resources` imports are removed from `bootstrap.py`.
- [ ] A regression test in `test_pygithub_git_provider.py` proves that `get_issue_comments` (and by extension `post_issue_comment`) goes through `_gh`'s refresh seam: a factory that returns a new client past the refresh window is called again, asserting the second client is used.
- [ ] All existing observer tests pass with the new constructor signatures (using `FakeGitProvider` in place of mock `host_for_project` callables). New tests cover the new `FakeGitProvider` methods and the two new routing methods.
- [ ] `just check` passes with `new_failures_count == 0`.

## Approach

**Pattern naming (per CLAUDE.md Decision 4):** No GoF pattern fits precisely. The structural principle is DIP (Dependency Inversion): both observers currently depend on the concrete `GitHostProvider` abstraction (the wrong layer); re-wiring them to depend on the v4 `GitProvider` Protocol (the right layer) gives them the token-refresh seam for free. The Google principle is "make the right thing easy" — once the observers inject `GitProvider`, they get refresh-correctness without any new logic.

**The root cause in one sentence:** `bootstrap.py:bootstrap_cli_context` calls `build_role_resources(registry=identity, role="orchestrator", ...)` once at daemon start to build a `GitHostProvider`, and the inner `Github(token)` client is never replaced — its installation token expires after 3600 s.

**Why the v4 provider doesn't have this bug:** `PyGithubGitProvider` stores a `github_factory` closure and rebuilds its cached `Github` client via `self._gh` (see `pygithub_git_provider.py:167-196`) when the cached client is older than `refresh_after_seconds` (default 3000 s). The cooperation with `V4IdentityRegistry._REFRESH_SAFETY_SECONDS = 900s` is documented in the module docstring and is unaffected by this change.

**Adding the two methods to `GitProvider`.** Both methods follow the same `*, project: str, issue_number: int` keyword-only signature every existing Protocol method uses. `CommentRef` (the existing return type from `foreman.git_host`) is imported into `git_provider.py` — this is a one-way import (`foreman.git_host` has no dependency on `foreman.v4`) and keeps the type shared across the legacy and v4 surfaces without duplication.

**`PyGithubGitProvider` implementation.** Both new methods use `self._repo.get_issue(issue_number)`, which forces a call through `self._repo` → `self._gh`, triggering the time-based rebuild check. No new refresh logic is needed.

**Observer rewiring.** The `project` kwarg the v4 Protocol expects is already known inside both observers as `ticket.project` (retrieved from `self._repo.get_ticket(ticket_id)`). The `repo_slug` variable and the `_resolve_repo_slug` helper vanish. The `ticket_ref` used in `build_escalation_comment_body` changes from `f"{repo_slug}#{issue_number}"` to `f"{ticket.project}#{issue_number}"`; this is an acceptable one-time dedup-key format change (any pre-existing comments with the old slug format won't be matched for dedup, producing at most one extra comment on first run after the fix).

`SustainedBlockedObserver` currently delegates to `post_escalation_comment(host=host, repo_slug=..., ...)`, which has a `GitHostProvider` signature. After the fix, it inlines the comment-post sequence that `TerminalLandingObserver` already uses (call `git.get_issue_comments`, call `already_posted_for_key`, call `build_escalation_comment_body`, call `git.post_issue_comment`). The `post_escalation_comment` helper in `_escalation_comment.py` is NOT touched — it remains the entry point for role subprocesses (Planner / Reviewer / Worker / Fixer) that still use `GitHostProvider`.

**Bootstrap cleanup.** The entire `per_project_git_hosts` dict, the `_HostForProject` class, and the `build_role_resources` call block (lines 145–195 in `bootstrap.py`) are deleted. Both observer subscriptions are moved inside the existing `if git_for_cross_project is not None:` guard so zero-project configs don't try to subscribe with a `None` provider.

## Sub-requests (topologically sorted)

1. **`git_provider.py`**: Import `CommentRef` from `foreman.git_host`. Add `get_issue_comments(*, project: str, issue_number: int) -> list[CommentRef]` and `post_issue_comment(*, project: str, issue_number: int, body: str) -> None` to `GitProvider` Protocol with docstrings. Add `_seeded_comments: dict[tuple[str, int], list[CommentRef]]` and `posted_comments: list[tuple[str, int, str]]` to `FakeGitProvider.__init__`; add `seed_issue_comments` helper, `get_issue_comments`, and `post_issue_comment` implementations.

2. **`pygithub_git_provider.py`**: Add `get_issue_comments` and `post_issue_comment` methods using `self._repo.get_issue(issue_number)`.

3. **`routing_git_provider.py`**: Add `get_issue_comments` and `post_issue_comment` delegating to `self._resolve(project)`.

4. **`observers/terminal_landing.py`**: Remove import of `GitHostProvider` from `foreman.git_host`. Add import of `GitProvider` from `foreman.v4.git_provider`. Replace `host_for_project: Callable[[str], GitHostProvider | None]` parameter with `git: GitProvider`. Store as `self._git`. Remove `_resolve_repo_slug` method. Update `_handle`: delete `host = self._host_for_project(...)` + slug lookup; replace `host.get_issue_comments(repo_slug, n)` with `self._git.get_issue_comments(project=ticket.project, issue_number=n)`; replace `host.post_issue_comment(repo_slug, n, body)` with `self._git.post_issue_comment(project=ticket.project, issue_number=n, body=body)`; set `ticket_ref=f"{ticket.project}#{ticket.issue_number}"` in `build_escalation_comment_body`.

5. **`observers/sustained_blocked.py`**: Same import swap. Replace `host_for_project` parameter with `git: GitProvider`. Remove `_resolve_repo_slug` method. In `_handle`: delete `host = self._host_for_project(...)` + slug lookup; inline the comment-post sequence matching `TerminalLandingObserver`'s pattern (try/except get_issue_comments → already_posted_for_key dedup → build_escalation_comment_body → try/except post_issue_comment).

6. **`bootstrap.py`**: Remove `from foreman.git_host import GitHostProvider` and `from foreman.roles import build_role_resources`. Delete lines 145–195 (`per_project_git_hosts` dict, `_HostForProject` class, `host_for_project` construction). Move the `SustainedBlockedObserver` and `TerminalLandingObserver` subscriptions inside the `if git_for_cross_project is not None:` guard; replace `host_for_project=host_for_project` with `git=git_for_cross_project`.

7. **`tests/v4/test_pygithub_git_provider.py`**: Add `test_get_issue_comments_returns_sorted_comment_refs` and `test_post_issue_comment_calls_create_comment`. Add the refresh regression test: `test_get_issue_comments_uses_refreshed_client_past_window` — two factories returning distinct mock clients; advance clock past `refresh_after_seconds`; assert second call to `get_issue_comments` used the second client.

8. **`tests/v4/test_git_provider_fake.py`**: Add tests for `seed_issue_comments` + `get_issue_comments` (empty default, seeded return) and `post_issue_comment` (recorder).

9. **`tests/v4/test_routing_git_provider.py`**: Add routing-dispatch tests for `get_issue_comments` and `post_issue_comment` following the existing per-method pattern (dispatch to right provider, not to other).

10. **`tests/v4/observers/test_terminal_landing_observer.py`**: Remove `_host_factory`; replace mock host callables with `FakeGitProvider`. Seed comments via `fake_git.seed_issue_comments(...)`; assert posted comments via `fake_git.posted_comments`.

11. **`tests/v4/observers/test_sustained_blocked_observer.py`**: Same migration to `FakeGitProvider`.

## File-level changes

| File | Change |
|------|--------|
| `packages/foreman/src/foreman/v4/git_provider.py` | Add `CommentRef` import; add `get_issue_comments` + `post_issue_comment` to Protocol; implement both in `FakeGitProvider` with seeding/recording helpers. |
| `packages/foreman/src/foreman/v4/pygithub_git_provider.py` | Implement `get_issue_comments` + `post_issue_comment` via `self._repo`. |
| `packages/foreman/src/foreman/v4/routing_git_provider.py` | Delegate `get_issue_comments` + `post_issue_comment` to `_resolve(project)`. |
| `packages/foreman/src/foreman/v4/observers/terminal_landing.py` | Swap `host_for_project` → `git: GitProvider`; remove `_resolve_repo_slug`; update call sites. |
| `packages/foreman/src/foreman/v4/observers/sustained_blocked.py` | Same swap; inline comment-post logic instead of delegating to `post_escalation_comment`. |
| `packages/foreman/src/foreman/v4/bootstrap.py` | Delete `per_project_git_hosts` / `_HostForProject` / `build_role_resources` wiring; remove dead imports; gate observers on `git_for_cross_project is not None`. |
| `packages/foreman/tests/v4/test_pygithub_git_provider.py` | Add tests for the two new methods + refresh regression test. |
| `packages/foreman/tests/v4/test_git_provider_fake.py` | Add tests for `seed_issue_comments`, `get_issue_comments`, `post_issue_comment`. |
| `packages/foreman/tests/v4/test_routing_git_provider.py` | Add dispatch tests for the two new routing methods. |
| `packages/foreman/tests/v4/observers/test_terminal_landing_observer.py` | Migrate from mock `host_for_project` to `FakeGitProvider`. |
| `packages/foreman/tests/v4/observers/test_sustained_blocked_observer.py` | Same migration. |

`packages/foreman/src/foreman/roles/_escalation_comment.py` is **not changed** — it continues to serve role subprocesses via the `GitHostProvider` surface.

## Alternatives considered

- **Band-aid the legacy `GitHostProvider` with a refresh seam.** The issue body explicitly rules this out ("Do NOT add a refresh seam to the legacy `foreman/git_hosts/github.py` provider"). Beyond the prohibition, it would mean maintaining two parallel refresh implementations and keep the observers coupled to the wrong abstraction layer.
- **Add a `repo_slug` parameter to the v4 `GitProvider` Protocol methods** (so the comment marker's `ticket_ref` preserves the `owner/repo#N` format). Ruled out: the Protocol's entire design is to route by project name, not by slug. Exposing `repo_slug` at the Protocol boundary would leak the legacy calling convention into the v4 surface and re-introduce the naming duality the v4 architecture was designed to eliminate.
- **Pass `project_configs: dict[str, ProjectConfig]` to the observers** so they can derive `repo_slug = project_configs[project].repo` for the `ticket_ref`. Ruled out: adds a config dependency to the observer for a cosmetic concern (comment dedup marker format). The one-time dedup reset (at most one duplicate comment on first run) is an acceptable price for keeping the observer dependencies minimal.

## Open questions

None. The approach is fully grounded in the existing codebase — files and patterns verified before writing this spec.

## Out of scope

- Adding a refresh seam to the legacy `foreman/git_hosts/github.py` provider.
- Changing any role-subprocess git path (Planner / Reviewer / Worker / Fixer build fresh tokens per dispatch — no change needed).
- Changing `MergingState` or any state-machine git calls (already on the v4 provider).
- Deleting any legacy `GitHostProvider` methods beyond the observer wiring (follow-up ticket).
- Changing the per-project `ProjectConfig.merge_method` or any unrelated config knobs.
