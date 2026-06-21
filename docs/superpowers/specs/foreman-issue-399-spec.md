# Spec: pass `merge_method` to `pr.merge()` to support squash-only repos (issue #399)

## Goal

Fix `PyGithubGitProvider.merge_pr()` so it resolves the correct merge method from
the target repository's allowed-method flags (`allow_squash_merge`,
`allow_rebase_merge`, `allow_merge_commit`) before calling `pr.merge()`, rather than
relying on PyGithub's default of `"merge"` which fails with HTTP 405 on
squash-only repos like `jeffrichley/agent_core`. Tracks
[foreman#399](https://github.com/jeffrichley/foreman/issues/399).

## Acceptance criteria

- [ ] `PyGithubGitProvider.merge_pr()` in
  `packages/foreman/src/foreman/v4/pygithub_git_provider.py` calls
  `pr.merge(merge_method=<resolved>)` where `<resolved>` comes from a new
  module-level helper `_resolve_merge_method`.
- [ ] `_resolve_merge_method(repo: Repository, preferred: str | None = None) -> str`
  is added as a private module-level function in `pygithub_git_provider.py`. It:
  - Returns `preferred` if `preferred` is one of `"squash"`, `"rebase"`, `"merge"`
    AND the corresponding repo flag is `True`.
  - Otherwise returns the first allowed method in the preference order
    `squash → rebase → merge`.
  - Raises `ValueError` with a descriptive message if none of the three repo flags
    are `True` (defensive guard; should not happen on real repos).
- [ ] The existing test `test_merge_pr_calls_merge` in
  `packages/foreman/tests/v4/test_pygithub_git_provider.py` is updated to assert
  `pr.merge` was called with a `merge_method` keyword argument (not zero args).
- [ ] Five new unit tests for `_resolve_merge_method` are added to
  `packages/foreman/tests/v4/test_pygithub_git_provider.py`:
  - squash-only repo (only `allow_squash_merge=True`) → `"squash"`
  - all-allowed repo (no preferred) → `"squash"` (first in preference order)
  - none-allowed repo → raises `ValueError`
  - preferred allowed (`preferred="rebase"`, all flags True) → `"rebase"`
  - preferred disallowed (`preferred="rebase"`, `allow_rebase_merge=False`,
    `allow_squash_merge=True`) → `"squash"` (fallback to first allowed)
- [ ] `just check` exits zero (ruff + mypy + lint-imports + pytest with the existing
  78% coverage gate).
- [ ] No changes to `GitProvider` Protocol in `git_provider.py` and no changes to
  `FakeGitProvider`.
- [ ] No changes to `SpecReviewState` or `MergingState` (the callers of
  `merge_pr` stay unchanged — the fix is entirely internal to
  `PyGithubGitProvider.merge_pr()`).

## Approach

The bug is a single silent assumption: `pr.merge()` without `merge_method` defaults
to `"merge"` (merge commit), but repos can disable merge commits and allow only
squash or rebase merges. The fix is entirely internal to `PyGithubGitProvider`
— no Protocol, no state machine, no config changes required.

**Pattern naming (per `CLAUDE.md` Decision 4):** No GoF pattern fits. This is
straightforward defensive programming — eliminate a silent assumption at a single
call site. The Google engineering principle is "fail loudly with a useful error
rather than let a 405 propagate through three retries and escalate to NeedsHelp."

**Helper function design.** A module-level private function
`_resolve_merge_method(repo: Repository, preferred: str | None = None) -> str`
is the right shape for two reasons:

1. It is a pure function (reads three booleans off the repo object, no side
   effects). Pure functions are trivially unit-tested — the five tests in the
   acceptance criteria each take three lines.

2. Including the `preferred` parameter now (even though no caller passes it yet)
   is the right forward compatibility hook for a future per-project
   `ProjectConfig.merge_method` config knob. Plumbing it at call time instead
   of at the spec boundary is a deliberate choice to keep this PR atomic and not
   touch `config.py`, `bootstrap.py`, or `state.py`.

**Preference order: squash → rebase → merge.** Most repos that restrict allowed
methods are squash-only (the `jeffrichley/agent_core` repro confirms this). Squash
keeps a linear history on `main`, which is the de-facto foreman deployment
convention. Rebase is next because it also preserves linearity. "Merge" (merge
commit) is last as legacy fallback — it's the old PyGithub default and the only
method disabled on squash-only repos, so putting it last means repos that disable
it never hit it.

**No changes to the `GitProvider` Protocol or `FakeGitProvider`.**
`merge_pr(*, project, pr_number)` stays the same Protocol shape. The merge method
selection is an implementation detail of `PyGithubGitProvider` — callers
(`SpecReviewState`, `MergingState`) have no business knowing which merge method was
used; they only care that the PR ended up merged. `FakeGitProvider.merge_pr()`
does not talk to PyGithub and does not need to model merge method selection.

**How `_resolve_merge_method` is called inside `merge_pr`.** The current
`merge_pr` body is:
```python
pr = self._repo.get_pull(pr_number)
pr.merge()
```
The fix is:
```python
repo = self._repo
pr = repo.get_pull(pr_number)
pr.merge(merge_method=_resolve_merge_method(repo))
```
Capturing `self._repo` once avoids a second property access (which would re-invoke
the `_gh` lazy-refresh check). The `repo` local variable is then passed to both
`get_pull` and `_resolve_merge_method`, which reads its `allow_*` boolean
attributes directly.

## Sub-requests (topologically sorted)

1. **Add `_resolve_merge_method` in `pygithub_git_provider.py`.** Insert the
   function after the module-level constants and before the `PyGithubGitProvider`
   class. The function signature and logic:
   ```python
   def _resolve_merge_method(
       repo: Repository, preferred: str | None = None,
   ) -> str:
       """Pick the merge method to pass to ``pr.merge(merge_method=...)``.

       Reads the three repo-level allow booleans (``allow_squash_merge``,
       ``allow_rebase_merge``, ``allow_merge_commit``) and selects a method
       that the target repo will accept. Preference order when ``preferred``
       is not given (or given but disallowed): squash → rebase → merge.

       Parameters
       ----------
       repo:
           PyGithub :class:`~github.Repository.Repository` handle for the
           target repo. Must expose the three ``allow_*`` boolean attributes.
       preferred:
           Caller-supplied preference (e.g. from ``ProjectConfig.merge_method``
           in a future follow-up). If the method is allowed on this repo the
           preferred value is returned; otherwise the function falls back to
           the first allowed method in the preference order.

       Raises
       ------
       ValueError
           If none of the three repo flags are ``True`` — which should not
           happen on a real GitHub repo, but raises a descriptive error rather
           than letting ``pr.merge()`` produce a cryptic 405.
       """
       allowed: dict[str, bool] = {
           "squash": bool(repo.allow_squash_merge),
           "rebase": bool(repo.allow_rebase_merge),
           "merge": bool(repo.allow_merge_commit),
       }
       if preferred is not None and allowed.get(preferred):
           return preferred
       for method in ("squash", "rebase", "merge"):
           if allowed[method]:
               return method
       raise ValueError(
           f"repo has no allowed merge method "
           f"(allow_squash_merge={allowed['squash']}, "
           f"allow_rebase_merge={allowed['rebase']}, "
           f"allow_merge_commit={allowed['merge']}); "
           f"cannot call pr.merge()"
       )
   ```

2. **Update `PyGithubGitProvider.merge_pr()`.** Replace the current body:
   ```python
   pr = self._repo.get_pull(pr_number)
   pr.merge()
   ```
   with:
   ```python
   repo = self._repo
   pr = repo.get_pull(pr_number)
   pr.merge(merge_method=_resolve_merge_method(repo))
   ```
   Update the docstring to note that merge_method is resolved automatically from
   the repo's allowed methods.

3. **Update the existing `test_merge_pr_calls_merge` test.** The current test
   ends with `mock_pr.merge.assert_called_once()`. Add the `merge_method` check.
   Since the default `MagicMock()` for `mock_repo` returns truthy values for all
   attribute accesses (including `allow_squash_merge`), `_resolve_merge_method`
   returns `"squash"` when called against the unmodified `mock_repo` fixture.
   Update the assertion to:
   ```python
   mock_pr.merge.assert_called_once_with(merge_method="squash")
   ```

4. **Add five unit tests for `_resolve_merge_method`.** Insert after the
   existing merge-related tests in
   `packages/foreman/tests/v4/test_pygithub_git_provider.py`. Import the helper:
   ```python
   from foreman.v4.pygithub_git_provider import PyGithubGitProvider, _resolve_merge_method
   ```

   ```python
   # ---------------------------------------------------------------------------
   # _resolve_merge_method unit tests (issue #399)
   # ---------------------------------------------------------------------------

   def test_resolve_merge_method_squash_only():
       """Squash-only repo (only allow_squash_merge=True) → 'squash'."""
       repo = MagicMock()
       repo.allow_squash_merge = True
       repo.allow_rebase_merge = False
       repo.allow_merge_commit = False
       assert _resolve_merge_method(repo) == "squash"


   def test_resolve_merge_method_all_allowed_no_preferred_returns_squash():
       """All three methods allowed, no preferred → 'squash' (first in order)."""
       repo = MagicMock()
       repo.allow_squash_merge = True
       repo.allow_rebase_merge = True
       repo.allow_merge_commit = True
       assert _resolve_merge_method(repo) == "squash"


   def test_resolve_merge_method_none_allowed_raises():
       """No allowed methods → ValueError with a descriptive message."""
       repo = MagicMock()
       repo.allow_squash_merge = False
       repo.allow_rebase_merge = False
       repo.allow_merge_commit = False
       with pytest.raises(ValueError, match="no allowed merge method"):
           _resolve_merge_method(repo)


   def test_resolve_merge_method_preferred_allowed_returns_preferred():
       """preferred='rebase', rebase allowed → 'rebase' (not overridden)."""
       repo = MagicMock()
       repo.allow_squash_merge = True
       repo.allow_rebase_merge = True
       repo.allow_merge_commit = True
       assert _resolve_merge_method(repo, preferred="rebase") == "rebase"


   def test_resolve_merge_method_preferred_disallowed_falls_back():
       """preferred='rebase', rebase NOT allowed → 'squash' (first allowed)."""
       repo = MagicMock()
       repo.allow_squash_merge = True
       repo.allow_rebase_merge = False
       repo.allow_merge_commit = True
       assert _resolve_merge_method(repo, preferred="rebase") == "squash"
   ```

5. **Run the quality gate.** From the worktree root, run `just check`. Confirm
   exit zero and `new_failures_count == 0`.

## File-level changes

| File | Change |
|------|--------|
| `packages/foreman/src/foreman/v4/pygithub_git_provider.py` | Add module-level `_resolve_merge_method` helper; update `merge_pr()` to call it and pass `merge_method` to `pr.merge()`. |
| `packages/foreman/tests/v4/test_pygithub_git_provider.py` | Update `test_merge_pr_calls_merge` assertion; add five new `_resolve_merge_method` unit tests; import `_resolve_merge_method`. |

No other files change.

## Alternatives considered

- **Add `merge_method` to `ProjectConfig` now.** The issue lists this as optional
  and explicitly says "OK to defer to a follow-up ticket if it complicates this
  fix." Adding it now would touch `config.py`, `bootstrap.py`, `state.py`,
  `worker_pool.py`, `daemon.py`, and every test that constructs those objects —
  far more surface for a fix whose correctness depends only on reading three repo
  booleans at call time. Deferred.
- **Change the `GitProvider.merge_pr` Protocol signature to accept
  `merge_method`.** Would require updating `FakeGitProvider`, all `StateContext`
  callers (`SpecReviewState`, `MergingState`), and all test sites that call
  `merge_pr`. The issue's source-file pointers explicitly call out that the
  Protocol "may need a new arg OR the implementation can be entirely internal" —
  entirely internal is the smaller cut and leaves the Protocol stable.
- **Hard-code `merge_method="squash"` in `merge_pr()` instead of auto-detecting.**
  Breaks repos that only allow merge commits (still the majority). The auto-detect
  approach handles all repos without per-project config.
- **Do nothing; require operators to `foreman skip` past the 405 failures.**
  The current recovery path (three retries, three Fixer runs, NeedsHelp
  escalation, manual operator skip) takes ~30 minutes per ticket and requires
  human intervention. The fix is five lines of code and two test additions.

## Open questions

None. The PyGithub `Repository` API (`allow_squash_merge`, `allow_rebase_merge`,
`allow_merge_commit`) is stable and documented. The preference order (squash →
rebase → merge) matches the empirical repro on `jeffrichley/agent_core`.

## Out of scope

- Per-project `merge_method` config knob in `ProjectConfig`. The `preferred`
  parameter on `_resolve_merge_method` is the wiring point for a future follow-up
  ticket; this PR does not plumb it through `ProjectConfig`, `bootstrap.py`,
  or `StateContext`.
- MergeQueue support. Phase 8d.19's rationale stands — most repos don't have it.
- Changing `SpecReviewState` or `MergingState` — they call `merge_pr` correctly
  and the fix is entirely inside the implementation.
- `FakeGitProvider` changes — the fake doesn't model merge method selection;
  tests that exercise `SpecReviewState` and `MergingState` through `FakeGitProvider`
  continue working unchanged.
- Operator documentation updates. The auto-detect behavior needs no operator
  action; the preference order is self-documenting in the helper's docstring.
