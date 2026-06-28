# Spec: collapse PyGithubGitProvider + V4IdentityRegistry refresh into single source of truth (issue #312)

## Goal

Remove the time-based `_DEFAULT_REFRESH_AFTER_SECONDS` / `clock` / `refresh_after_seconds` caching logic from `PyGithubGitProvider` and replace it with a token-string equality check against `V4IdentityRegistry`. After this change, `V4IdentityRegistry._REFRESH_SAFETY_SECONDS` becomes the only tunable controlling refresh aggressiveness, and the two-constant coordination invariant that caused the 2026-06-15 dogfood crash (and was patched empirically in 8d.13) becomes structurally impossible. See issue [#312](https://github.com/jeffrichley/foreman/issues/312).

## Acceptance criteria

- `PyGithubGitProvider` no longer has `_DEFAULT_REFRESH_AFTER_SECONDS`, `refresh_after_seconds`, or `clock` parameters (field or constructor).
- `PyGithubGitProvider` constructor takes `identity: V4IdentityRegistry` and `role: str` instead of `github_factory: Callable[[], Github]`.
- `PyGithubGitProvider._gh` property calls `self._identity.get_role_token(self._role)` on every access; rebuilds the `Github` client iff the returned token string differs from `self._cached_token`.
- Cached `Repository` handle (`_cached_repo`) is invalidated (set to `None`) whenever the `Github` client is rebuilt — same behaviour as before.
- `PyGithubGitProvider._gh` is still lazy: neither `identity.get_role_token` nor `Github(...)` is called at construction time.
- The module-level `"Cooperation with V4IdentityRegistry's pre-expiry safety window"` docstring section is deleted from `pygithub_git_provider.py` (it becomes dead documentation after this refactor).
- The `_REFRESH_SAFETY_SECONDS` comment block in `identity.py` is rewritten to remove the cross-reference to `PyGithubGitProvider._DEFAULT_REFRESH_AFTER_SECONDS`, and to explicitly declare `_REFRESH_SAFETY_SECONDS` as the single tunable for refresh aggressiveness.
- A "Single source of truth for token freshness" note is added to the `identity.py` module docstring, explicitly stating that after foreman#312 this registry governs refresh with no coordination requirement from callers.
- `V4IdentityRegistry` class docstring is updated to remove the stale "cooperates with PyGithubGitProvider's 50-min rebuild cadence" language.
- The stale `"5-min-pre-expiry refresh"` comment inside `cli/__init__.py`'s `_git_factory` (line 220) is updated to accurately describe the new token-delegation model.
- `cli/__init__.py`'s `_git_factory` is rewritten to pass `identity=identity, role="orchestrator"` to the constructor; the `lambda: Github(identity.get_role_token("orchestrator"))` closure and the now-unused `from github import Github` import are removed.
- Existing time-based refresh tests (`test_uses_cached_client_within_refresh_window`, `test_rebuilds_client_when_cache_expires`, `test_repo_access_alone_triggers_gh_rebuild_past_window`, `test_get_issue_comments_uses_refreshed_client_past_window`) are rewritten to use a mock identity whose `get_role_token` return value is controlled by the test — not a `clock` parameter.
- New test `test_gh_does_not_rebuild_when_registry_returns_same_token`: call `provider._gh` 100 times with a constant token; assert `Github(...)` was called exactly once.
- New test `test_gh_rebuilds_when_registry_token_changes`: mock identity returns `"token_v1"` then `"token_v2"` on successive accesses; assert two distinct `Github` instances were returned.
- All other existing tests (non-refresh) are updated to pass the new constructor shape (`identity=mock_identity, role="orchestrator"` in place of `github_factory=lambda: mock_github`).
- `just check` exits 0.

## Approach

**Pattern:** no GoF pattern applies. The Google principle is *"make the right thing easy"* (or equivalently, Single Responsibility: the registry owns token freshness, the provider owns HTTP dispatch — neither should own the other's concern). Delegating every `get_role_token` call to the registry eliminates the coordination invariant because there is simply nothing to coordinate: the registry answers "here is the current token" on every ask, and the provider rebuilds iff it's different.

**Structural mechanics of the new `_gh` property** (`pygithub_git_provider.py:169–197`):

```python
@property
def _gh(self) -> Github:
    current_token = self._identity.get_role_token(self._role)
    if self._cached_token != current_token:
        self._cached_github = Github(current_token)
        self._cached_token = current_token
        self._cached_repo = None
    assert self._cached_github is not None
    return self._cached_github
```

`Github(token)` is a pure local constructor — no network call, ~µs. `get_role_token` already caches and only mints a new installation token (one GitHub REST call) when within `_REFRESH_SAFETY_SECONDS` of expiry. String equality on the token string is essentially free. The `Repository` handle is reused while the token is unchanged (which is hours), and invalidated only when the token rotates — matching the current behaviour where it's invalidated on time-based refresh.

**Constructor signature change** (`pygithub_git_provider.py:130–167`):

Remove: `github_factory: Callable[[], Github]`, `clock: Callable[[], float]`, `refresh_after_seconds: float`.  
Add: `identity: V4IdentityRegistry`, `role: str`.  
Keep: `repo_full_name: str`.

The `V4IdentityRegistry` import moves from TYPE_CHECKING to runtime (we call its method). The `Github` import moves from TYPE_CHECKING to runtime (we call `Github(token)` inside `_gh`). Both `import time` and `from collections.abc import Callable` are removed.

**Bootstrap wiring change** (`cli/__init__.py:216–230`):

Currently:
```python
def _git_factory(repo: str) -> PyGithubGitProvider:
    return PyGithubGitProvider(
        github_factory=lambda: Github(identity.get_role_token("orchestrator")),
        repo_full_name=repo,
    )
```

After:
```python
def _git_factory(repo: str) -> PyGithubGitProvider:
    return PyGithubGitProvider(
        identity=identity,
        role="orchestrator",
        repo_full_name=repo,
    )
```

The `from github import Github` at `cli/__init__.py:187` is removed since the factory no longer calls it. The comment in the factory body is updated to remove the stale "5-min-pre-expiry refresh" wording; it now says the provider delegates token-freshness to the registry on every `_gh` access.

**Test seam change** (`tests/v4/test_pygithub_git_provider.py`):

The current `github_factory` seam let tests inject a mock `Github` client via a lambda. After the refactor the injection points are:
1. `identity.get_role_token()` return value (controls whether a rebuild fires)
2. `foreman.v4.pygithub_git_provider.Github` (controls what client is built when a rebuild fires)

The `mock_github` fixture should be updated to patch `foreman.v4.pygithub_git_provider.Github` so it returns the mock client for the duration of the test. A new `mock_identity` fixture provides a `MagicMock` whose `get_role_token` returns `"token_v1"` by default. Refresh-specific tests control the token sequence via `mock_identity.get_role_token.side_effect = ["token_v1", "token_v1", "token_v2", ...]` (or `return_value`) rather than a `clock` callable.

The one tricky test is `test_gh_does_not_rebuild_when_registry_returns_same_token` (calls `_gh` 100 times) — for that test the `Github` mock must be a `MagicMock` whose call count is inspectable. Patching at the module level gives exactly that.

**Doc-level changes** (`identity.py`): The cross-reference comment block above `_REFRESH_SAFETY_SECONDS` (lines 59–74) currently describes a three-constant arithmetic invariant with `PyGithubGitProvider._DEFAULT_REFRESH_AFTER_SECONDS`. After the refactor, those six lines become a two-line description: "Pre-expiry safety window: refresh when fewer than this many seconds remain. After foreman#312 this is the sole refresh tunable." A new paragraph at the end of the module docstring (`identity.py:49`) declares the single-source-of-truth property explicitly, per @wrenrichley's 2026-06-16 F5 comment.

## Sub-requests (topologically sorted)

1. **Update `pygithub_git_provider.py` — module docstring**: Delete the "Token-refresh seam" and "Cooperation with V4IdentityRegistry's pre-expiry safety window" sections from the module-level docstring (`pygithub_git_provider.py:6–45`). Replace with a short paragraph describing the new token-delegation model: "PyGithubGitProvider rebuilds its cached Github client iff identity.get_role_token(role) returns a different string than the last cached token. V4IdentityRegistry is the sole arbiter of when a new installation token is minted."

2. **Update `pygithub_git_provider.py` — constructor and instance state**: Remove `import time` and `from collections.abc import Callable` (both now unused). Move `from github import Github` and add `from foreman.v4.identity import V4IdentityRegistry` as runtime imports (not TYPE_CHECKING). Delete the `_DEFAULT_REFRESH_AFTER_SECONDS = 3000.0` module-level constant and its comment block. Rewrite `__init__` to accept `identity: V4IdentityRegistry, role: str, repo_full_name: str` (all keyword-only). Replace `self._github_factory`, `self._clock`, `self._refresh_after`, `self._cached_at` with `self._identity`, `self._role`, `self._cached_token: str | None = None`. Keep `self._cached_github: Github | None = None` and `self._cached_repo: Repository | None = None` unchanged.

3. **Update `pygithub_git_provider.py` — `_gh` property**: Replace the time-based rebuild logic with the token-equality check shown in the Approach section. Update the `_gh` and `_repo` property docstrings to remove references to `refresh_after_seconds`, `clock`, and "time-based rebuild"; describe the token-equality seam instead.

4. **Update `identity.py` — `_REFRESH_SAFETY_SECONDS` comment block**: Replace lines 58–74 with:
   ```python
   # Pre-expiry safety window: refresh the cached token when fewer than this
   # many seconds remain until expiry.
   #
   # After foreman#312, this is the ONLY tunable controlling how aggressively
   # tokens are refreshed. PyGithubGitProvider no longer has its own time-based
   # rebuild interval — it delegates token-freshness to this registry on every
   # _gh access. Raising or lowering this value is the single knob an operator
   # needs; no matching change in PyGithubGitProvider is required.
   _REFRESH_SAFETY_SECONDS = 900
   ```

5. **Update `identity.py` — module docstring and class docstring**: Append a "Single source of truth for token freshness" paragraph at the end of the module-level docstring (after line 48), explaining that after foreman#312 `_REFRESH_SAFETY_SECONDS` is the sole refresh tunable and there is no cross-file arithmetic invariant to maintain. In `V4IdentityRegistry`'s class docstring (around line 113–115), replace "the window cooperates with PyGithubGitProvider's 50-min rebuild cadence so client-rebuilds always pick up a fresh token" with "this registry is the sole arbiter of token freshness (foreman#312); callers receive a fresh token on every call that falls within the safety window."

6. **Update `cli/__init__.py` — `_git_factory`**: Remove `from github import Github` (line 187; no longer needed). Rewrite `_git_factory` (lines 216–230) to pass `identity=identity, role="orchestrator"` instead of the `github_factory` lambda. Update the inline comment from the stale "5-min-pre-expiry refresh" wording to: "PyGithubGitProvider delegates token-freshness to the registry — it calls identity.get_role_token('orchestrator') on every _gh access and rebuilds the Github client only when the token string changes."

7. **Rewrite test fixtures in `test_pygithub_git_provider.py`**: Add a `mock_identity` fixture returning a `MagicMock` with `get_role_token.return_value = "token_v1"`. Update `mock_github` fixture to use `unittest.mock.patch("foreman.v4.pygithub_git_provider.Github", return_value=gh)` as a context manager (yield fixture), so all tests that use `mock_github` automatically have `Github(...)` patched to return the mock client. Update every existing test that constructs `PyGithubGitProvider` to replace `github_factory=lambda: mock_github` with `identity=mock_identity, role="orchestrator"` (and `identity=mock_identity` taking the fixture where applicable).

8. **Rewrite time-based refresh tests**: Replace the three clock-seam tests:
   - `test_constructor_does_not_invoke_factory` → `test_constructor_does_not_invoke_identity`: construct `PyGithubGitProvider(identity=mock_identity, role="orchestrator", repo_full_name="owner/p")`; assert `mock_identity.get_role_token.assert_not_called()`.
   - `test_uses_cached_client_within_refresh_window` → becomes `test_gh_does_not_rebuild_when_registry_returns_same_token` (sub-request 9 below).
   - `test_rebuilds_client_when_cache_expires` → becomes `test_gh_rebuilds_when_registry_token_changes` (sub-request 9 below).
   - `test_repo_access_alone_triggers_gh_rebuild_past_window` → `test_repo_access_alone_triggers_gh_rebuild_on_token_change`: access `_repo` multiple times with the same token (assert one `Github()` call); change the token via `mock_identity.get_role_token.return_value = "token_v2"`; next `_repo` access must return a new `Repository` instance and the `Github` mock must have been called twice total.
   - `test_get_issue_comments_uses_refreshed_client_past_window` → `test_get_issue_comments_uses_refreshed_client_on_token_change`: two clients (`client_v1`, `client_v2`) returned by patched `Github` via `side_effect`; first `get_issue_comments` call while token is `"token_v1"` uses `client_v1`; change token to `"token_v2"`; second call uses `client_v2`.

9. **Add two new tests**:
   - `test_gh_does_not_rebuild_when_registry_returns_same_token`: patch `Github` as a `MagicMock`; call `provider._gh` 100 times with `mock_identity.get_role_token.return_value = "token_v1"`; assert `MockGithub.call_count == 1`.
   - `test_gh_rebuilds_when_registry_token_changes`: patch `Github` with `side_effect = [MagicMock(name="v1"), MagicMock(name="v2")]`; first `_gh` access returns `v1`; set `mock_identity.get_role_token.return_value = "token_v2"`; second `_gh` access returns `v2`; assert `v1 is not v2`.

10. **Run `just check`** and confirm the gate is green.

## File-level changes

| File | Change |
|------|--------|
| `packages/foreman/src/foreman/v4/pygithub_git_provider.py` | Delete `_DEFAULT_REFRESH_AFTER_SECONDS`, `github_factory`/`clock`/`refresh_after_seconds` constructor params, time-based `_gh` rebuild logic, "Cooperation" module-docstring section. Add `identity: V4IdentityRegistry`, `role: str` constructor params, `_cached_token: str \| None` instance field, token-equality `_gh` property. Move `Github` to runtime import; add `V4IdentityRegistry` runtime import. Remove `import time`, `from collections.abc import Callable`. |
| `packages/foreman/src/foreman/v4/identity.py` | Rewrite `_REFRESH_SAFETY_SECONDS` comment block (remove PyGithubGitProvider cross-reference; declare as sole tunable). Add "Single source of truth" paragraph to module docstring. Update `V4IdentityRegistry` class docstring to remove 50-min rebuild cadence reference. |
| `packages/foreman/src/foreman/v4/cli/__init__.py` | Remove `from github import Github`. Rewrite `_git_factory` to use new constructor shape. Fix stale "5-min-pre-expiry refresh" inline comment. |
| `packages/foreman/tests/v4/test_pygithub_git_provider.py` | Add `mock_identity` fixture. Update `mock_github` fixture to patch module-level `Github`. Rewrite all constructor calls to use new signature. Rewrite 5 time-based refresh tests as token-based. Add 2 new tests per acceptance criteria. |

## Alternatives considered

- **Keep the time-based rebuild in PyGithubGitProvider, raise the comment coverage:** A "better documented invariant" is not a "removed invariant." Any future touch to either constant (CI timeout change, different token TTL) re-introduces the arithmetic race. Rejected because documentation rot is exactly the mechanism that caused the three-commit 8c.3 → 8d.13 → 8d.14 chase in the first place.

- **Remove the identity registry's pre-expiry window entirely (always mint on every `get_role_token` call):** Simpler: no caching at all. Rejected because minting a GitHub App installation token is a network call with rate-limit exposure; calling it on every Poller tick (every few seconds) would hammer the endpoint. The registry's cache-with-safety-window is the right shape; the problem was that PyGithubGitProvider duplicated the "when to fetch" decision.

- **Use the `IdentityProvider` Protocol from `bootstrap.py` instead of the concrete `V4IdentityRegistry` type:** The Protocol (`get_role_token(role: str) -> str`) is structurally sufficient and would avoid importing the concrete class into `pygithub_git_provider.py`. However, the issue body explicitly names `identity: V4IdentityRegistry` as the constructor parameter type, and using the Protocol would require either re-exporting the Protocol from identity.py or importing it from bootstrap.py (creating a dependency on bootstrap from the provider). Rejected in favour of the concrete type as specified; if a future decoupling is needed, the Protocol can be extracted then.

- **Extract a `TokenAwareGithubFactory` callable wrapper rather than passing the registry directly:** A thin adapter `class TokenAwareGithubFactory: def __init__(self, identity, role): ...; def __call__(self) -> Github: return Github(identity.get_role_token(role))` could let `PyGithubGitProvider` keep a `Callable[[], Github]` shape but have the factory do the token comparison. Rejected because it adds an abstraction layer without adding value — the "factory vs. registry delegation" distinction is exactly what the issue asks to collapse. The token-equality check inside `_gh` is both simpler and more explicit about what triggers a rebuild.

## Open questions

(None. All file paths, method signatures, line numbers, and design choices verified against the actual codebase during spec drafting.)

## Out of scope

- Changing `V4IdentityRegistry._REFRESH_SAFETY_SECONDS` from 900s (the 8d.13 value is appropriate for the new single-tunable shape too).
- Any changes to `V4IdentityRegistry` internals or its `get_role_token` method — the API is unchanged; only consumers of PyGithubGitProvider change.
- Per-role `PyGithubGitProvider` instances (the orchestrator role is the only consumer today; multi-role wiring is Phase 9+).
- Changing the `bootstrap_cli_context` signature or `RoutingGitProvider` — the factory still takes `repo: str` and returns a `GitProvider`; only what the factory builds changes.
- `V4IdentityRegistry.clock` parameter (tests for the registry itself keep using the clock seam — that's orthogonal to this refactor).
- The 60+ minute empirical validation against algokit (listed in the issue's acceptance criteria as the final gate) — this is an operator run, not a code gate. The spec covers all code-level acceptance criteria; the empirical run is post-merge validation.
