# Spec: auto-refresh stale worktrees + remote branches at role start (issue #484)

## Goal

Eliminate two classes of deterministic git failures that block retried or re-planned foreman tickets: (1) a worktree whose `.git` gitdir pointer resolves to a foreign/unresolvable path (e.g., a Windows host path inside a Linux container), and (2) a remote `foreman/*` branch with divergent history from a prior run that causes non-fast-forward push rejections. Both are self-healing changes — no operator intervention required after the fix lands. See issue [#484](https://github.com/jeffrichley/foreman/issues/484).

## Acceptance criteria

- `WorktreeManager.create()` detects a stale worktree with an unresolvable gitdir (validated by `git rev-parse --git-dir` returning non-zero inside the existing `wt_path`), removes it via `shutil.rmtree`, and recreates cleanly — instead of returning the broken path and letting the next `git add` die with rc=128 `fatal: not a git repository: <foreign-path>`.
- `WorktreeManager.create_impl()`, `WorktreeManager.attach()`, and `WorktreeManager.attach_impl()` apply the same gitdir-validation guard at their respective idempotent-return branches.
- A new module-level helper `_worktree_gitdir_is_valid(path, *, role_token)` in `packages/foreman/src/foreman/worktree.py` encapsulates the `git rev-parse --git-dir` probe; all four call sites use it.
- `GitHubProvider.push_branch()` in `packages/foreman/src/foreman/git_hosts/github.py` catches `GitCommandError` from the initial push; if `exc.returncode == 1` and `"[rejected]"` appears in `exc.stderr` alongside `"fetch first"` or `"non-fast-forward"`, retries with `--force-with-lease`; all other `GitCommandError` shapes re-raise without retry.
- `test_worktree.py` gains `test_create_replaces_worktree_with_foreign_gitdir`, `test_create_impl_replaces_worktree_with_foreign_gitdir`, `test_attach_replaces_worktree_with_foreign_gitdir`, and `test_attach_impl_replaces_worktree_with_foreign_gitdir` — each populates `wt_path` with a `.git` file pointing to a foreign path, calls the relevant `WorktreeManager` method, and asserts the resulting worktree is on the correct branch.
- `test_git_hosts_github.py` gains `test_push_branch_retries_with_force_on_non_fast_forward` (first push fails with `[rejected] ... (fetch first)`, second push with `--force-with-lease` succeeds; asserts two push calls with `--force-with-lease` present in the second) and `test_push_branch_does_not_retry_on_other_errors` (permission error does not trigger retry; asserts one push call and `GitCommandError` is re-raised).
- The existing test `test_push_branch_uses_installation_token_url` continues to pass unchanged — it stubs the push to succeed on the first try, so the retry path is never exercised.
- `just check` exits zero.

## Approach

**Pattern (Decision 4):** No GoF pattern fits either fix cleanly. Both embody the Google engineering principle **"make the right thing easy"** — the nominal path (worktree exists → reuse; push succeeds → done) remains unchanged; the self-healing logic fires only on explicitly detected exceptional states that Foreman itself caused and therefore can safely resolve without operator help.

### Fix 1 — Worktree gitdir validation (`worktree.py`)

`WorktreeManager.create()`, `create_impl()`, `attach()`, and `attach_impl()` all share an idempotent-return guard: `if wt_path.exists(): return wt_path`. When a prior run left a worktree with a `.git` file pointing to an unresolvable gitdir (a Windows host path in a Linux container, for example), this guard silently returns the broken path, and every subsequent git operation in that worktree fails with rc=128 `fatal: not a git repository: <unresolvable>`.

Fix: convert the guard to a validate-then-return-or-rmtree pattern:

```python
if wt_path.exists():
    if _worktree_gitdir_is_valid(wt_path, role_token=self._role_token):
        return wt_path
    # Foreign/unresolvable gitdir from a prior environment.
    # Tear down and recreate below.
    shutil.rmtree(wt_path)
# ... existing creation logic unchanged
```

The helper runs `git rev-parse --git-dir` in the worktree directory (same env filter as all other git calls):

```python
def _worktree_gitdir_is_valid(worktree_path: Path, *, role_token: str | None = None) -> bool:
    """Return True iff ``git rev-parse --git-dir`` exits 0 inside the worktree.

    A worktree whose .git gitdir pointer resolves to a foreign/unresolvable
    path (e.g., a Windows host path inside a Linux container) causes rc=128
    ``fatal: not a git repository: <path>`` on any subsequent git operation.
    This probe detects that state before returning the worktree to the caller.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=worktree_path,
        check=False,
        capture_output=True,
        text=True,
        env=filtered_subprocess_env(role_token=role_token),
    )
    return result.returncode == 0
```

`shutil` is already imported in `worktree.py` (used by `prune()`), so no new import is needed. The existing `_self_heal_orphaned_branch` call — which runs `git worktree prune` + `git branch -D <branch>` before `git worktree add -b` — fires in `create()` and `create_impl()` after the `wt_path.exists()` block, so on the rmtree path those cleanup steps still run and clear any stale worktree metadata in the clone's `.git/worktrees/` index.

`attach()` and `attach_impl()` do NOT call `_self_heal_orphaned_branch` (they never create a new branch); their recreation path goes straight to the `git fetch`/`_local_branch_exists` guard and then `git worktree add` — which is already correct for the attach case.

### Fix 2 — Non-fast-forward push recovery (`git_hosts/github.py`)

`GitHubProvider.push_branch()` currently issues a plain `git push <url> <branch>:<branch>` with no force flag. When the remote `foreman/issue-N` or `foreman/impl-N` branch has divergent history from a prior run (whether an old host-session branch or a Planner-pushed branch that the SpecFix's clone doesn't build linearly on top of), git exits with rc=1 and stderr containing `! [rejected] ... (fetch first)`.

Fix: catch `GitCommandError` from the initial push, detect the specific non-fast-forward shape, and retry with `--force-with-lease`:

```python
def push_branch(self, worktree_path: Path, branch: str) -> None:
    remote_url = self._git(worktree_path, "config", "--get", "remote.origin.url").stdout.strip()
    repo_slug = _extract_repo_slug(remote_url)
    push_url = f"https://x-access-token:{self._identity.token}@github.com/{repo_slug}.git"
    try:
        self._git(worktree_path, "push", push_url, f"{branch}:{branch}")
    except GitCommandError as exc:
        if exc.returncode == 1 and "[rejected]" in exc.stderr and (
            "fetch first" in exc.stderr or "non-fast-forward" in exc.stderr
        ):
            # Remote branch has divergent history from a prior run.
            # Foreman owns the foreman/issue-N and foreman/impl-N namespaces;
            # force-refresh is safe and self-correcting.
            self._git(worktree_path, "push", "--force-with-lease", push_url, f"{branch}:{branch}")
        else:
            raise
```

`--force-with-lease` (not bare `--force`) is used because: (a) it is the form the issue explicitly recommends, (b) it matches the "safe force" idiom the project follows elsewhere, and (c) on an anonymous push URL (no configured remote) it degrades gracefully to `--force` semantics without error — which is the right behaviour here since Foreman is the sole writer to these branch namespaces.

The narrow `[rejected] + (fetch first|non-fast-forward)` detection excludes other rejection types (e.g., `! [remote rejected] main -> main (protected branch hook declined)`) that should NOT be silently force-pushed. Those re-raise as before.

No imports are added: `GitCommandError` is already raised by `_git()` and is imported from `foreman.git_hosts._errors` at the module level.

## Sub-requests (topologically sorted)

1. **Add `_worktree_gitdir_is_valid()` to `packages/foreman/src/foreman/worktree.py`** — insert the helper after `_local_branch_exists` (approx. line 795). No new imports needed (`subprocess` and `filtered_subprocess_env` are already present). Full function body as shown in Approach §Fix 1.

2. **Update `WorktreeManager.create()` in `packages/foreman/src/foreman/worktree.py`** — replace the single-line guard `if wt_path.exists(): return wt_path` (line 251) with the validate-then-rmtree-or-return pattern shown in Approach §Fix 1.

3. **Update `WorktreeManager.create_impl()` in `packages/foreman/src/foreman/worktree.py`** — apply the same replacement at the equivalent guard (line 350–351 `if wt_path.exists(): return ImplWorktreeResult(path=wt_path, base_branch=base_branch)`). On the rmtree path, omit the `ImplWorktreeResult` early-return and fall through to the fetch + `_self_heal_orphaned_branch` + `git worktree add` block below. Note: `base_branch` must already be computed before the `if wt_path.exists()` block (it is, on line 341), so the fall-through path inherits it correctly.

4. **Update `WorktreeManager.attach()` (spec_pr path) in `packages/foreman/src/foreman/worktree.py`** — replace the guard `if wt_path.exists(): return wt_path` (line 452) with the validate-then-rmtree-or-return pattern. The fall-through path re-enters the `if not _local_branch_exists(...): git fetch` → `git worktree add` sequence.

5. **Update `WorktreeManager.attach_impl()` in `packages/foreman/src/foreman/worktree.py`** — same replacement at line 516. The fall-through path re-enters the `if not _local_branch_exists(...): git fetch` → `git worktree add` sequence.

6. **Update `GitHubProvider.push_branch()` in `packages/foreman/src/foreman/git_hosts/github.py`** — wrap the `self._git(..., "push", ...)` call in a try/except as shown in Approach §Fix 2. No new imports.

7. **Add four gitdir-validation tests to `packages/foreman/tests/test_worktree.py`**:

   `test_create_replaces_worktree_with_foreign_gitdir`:
   ```python
   def test_create_replaces_worktree_with_foreign_gitdir(tmp_path: Path) -> None:
       """A stale worktree with a foreign gitdir is rmtree'd and recreated by create()."""
       clone = tmp_path / "clone"
       clone.mkdir()
       _init_git_repo(clone, origin_path=tmp_path / "origin.git")

       worktrees_root = tmp_path / "worktrees"
       stale_wt = worktrees_root / "voice" / "issue-42"
       stale_wt.mkdir(parents=True)
       (stale_wt / ".git").write_text(
           "gitdir: E:/workspaces/ai/agents/voice/.git/worktrees/issue-42\n"
       )
       (stale_wt / "stale.txt").write_text("left over\n")

       mgr = WorktreeManager(worktrees_root=worktrees_root)
       wt_path = mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)

       assert wt_path.exists()
       assert not (wt_path / "stale.txt").exists(), "stale content must be gone"
       branch = subprocess.run(
           ["git", "branch", "--show-current"],
           cwd=wt_path, check=True, capture_output=True, text=True,
       ).stdout.strip()
       assert branch == "foreman/issue-42"
   ```

   `test_create_impl_replaces_worktree_with_foreign_gitdir`:
   ```python
   def test_create_impl_replaces_worktree_with_foreign_gitdir(tmp_path: Path) -> None:
       """A stale impl worktree with a foreign gitdir is rmtree'd and recreated by create_impl()."""
       clone = tmp_path / "clone"
       clone.mkdir()
       _init_git_repo(clone, origin_path=tmp_path / "origin.git")

       worktrees_root = tmp_path / "worktrees"
       stale_wt = worktrees_root / "voice" / "impl-42"
       stale_wt.mkdir(parents=True)
       (stale_wt / ".git").write_text(
           "gitdir: E:/workspaces/ai/agents/voice/.git/worktrees/impl-42\n"
       )
       (stale_wt / "stale.txt").write_text("left over\n")

       mgr = WorktreeManager(worktrees_root=worktrees_root)
       result = mgr.create_impl(clone_path=clone, repo_slug="voice", ticket_id=42)

       assert result.path.exists()
       assert not (result.path / "stale.txt").exists(), "stale content must be gone"
       branch = subprocess.run(
           ["git", "branch", "--show-current"],
           cwd=result.path, check=True, capture_output=True, text=True,
       ).stdout.strip()
       assert branch == "foreman/impl-42"
   ```

   `test_attach_replaces_worktree_with_foreign_gitdir`:
   ```python
   def test_attach_replaces_worktree_with_foreign_gitdir(tmp_path: Path) -> None:
       """attach() replaces a stale worktree with a foreign gitdir instead of returning it."""
       clone = tmp_path / "clone"
       clone.mkdir()
       origin_path = tmp_path / "origin.git"
       _init_git_repo(clone, origin_path=origin_path)

       # Push a spec branch so attach() can fetch and check it out.
       subprocess.run(
           ["git", "checkout", "-b", "foreman/issue-42"],
           cwd=clone, check=True, capture_output=True,
       )
       subprocess.run(
           ["git", "push", "origin", "foreman/issue-42"],
           cwd=clone, check=True, capture_output=True,
       )
       subprocess.run(
           ["git", "checkout", "main"],
           cwd=clone, check=True, capture_output=True,
       )
       subprocess.run(
           ["git", "branch", "-D", "foreman/issue-42"],
           cwd=clone, check=True, capture_output=True,
       )

       worktrees_root = tmp_path / "worktrees"
       stale_wt = worktrees_root / "voice" / "issue-42"
       stale_wt.mkdir(parents=True)
       (stale_wt / ".git").write_text(
           "gitdir: E:/workspaces/ai/agents/voice/.git/worktrees/issue-42\n"
       )
       (stale_wt / "stale.txt").write_text("left over\n")

       mgr = WorktreeManager(worktrees_root=worktrees_root)
       wt_path = mgr.attach(clone_path=clone, repo_slug="voice", ticket_id=42)

       assert wt_path.exists()
       assert not (wt_path / "stale.txt").exists(), "stale content must be gone"
       branch = subprocess.run(
           ["git", "branch", "--show-current"],
           cwd=wt_path, check=True, capture_output=True, text=True,
       ).stdout.strip()
       assert branch == "foreman/issue-42"
   ```

   `test_attach_impl_replaces_worktree_with_foreign_gitdir`:
   ```python
   def test_attach_impl_replaces_worktree_with_foreign_gitdir(tmp_path: Path) -> None:
       """attach_impl() replaces a stale worktree with a foreign gitdir."""
       clone = tmp_path / "clone"
       clone.mkdir()
       origin_path = tmp_path / "origin.git"
       _init_git_repo(clone, origin_path=origin_path)

       # Push an impl branch so attach_impl() can fetch and check it out.
       subprocess.run(
           ["git", "checkout", "-b", "foreman/impl-42"],
           cwd=clone, check=True, capture_output=True,
       )
       subprocess.run(
           ["git", "push", "origin", "foreman/impl-42"],
           cwd=clone, check=True, capture_output=True,
       )
       subprocess.run(
           ["git", "checkout", "main"],
           cwd=clone, check=True, capture_output=True,
       )
       subprocess.run(
           ["git", "branch", "-D", "foreman/impl-42"],
           cwd=clone, check=True, capture_output=True,
       )

       worktrees_root = tmp_path / "worktrees"
       stale_wt = worktrees_root / "voice" / "impl-42"
       stale_wt.mkdir(parents=True)
       (stale_wt / ".git").write_text(
           "gitdir: E:/workspaces/ai/agents/voice/.git/worktrees/impl-42\n"
       )
       (stale_wt / "stale.txt").write_text("left over\n")

       mgr = WorktreeManager(worktrees_root=worktrees_root)
       wt_path = mgr.attach_impl(clone_path=clone, repo_slug="voice", ticket_id=42)

       assert wt_path.exists()
       assert not (wt_path / "stale.txt").exists(), "stale content must be gone"
       branch = subprocess.run(
           ["git", "branch", "--show-current"],
           cwd=wt_path, check=True, capture_output=True, text=True,
       ).stdout.strip()
       assert branch == "foreman/impl-42"
   ```

8. **Add two push-retry tests to `packages/foreman/tests/test_git_hosts_github.py`**:

   `test_push_branch_retries_with_force_on_non_fast_forward`:
   ```python
   def test_push_branch_retries_with_force_on_non_fast_forward(tmp_path: Path) -> None:
       """push_branch() retries with --force-with-lease when rejected as non-fast-forward."""
       wt = _init_worktree(tmp_path)
       subprocess.run(
           ["git", "remote", "add", "origin", "https://github.com/owner/name.git"],
           cwd=wt, check=True, capture_output=True,
       )

       real_run = subprocess.run
       push_calls: list[list[str]] = []

       def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
           if isinstance(cmd, list) and len(cmd) > 1 and cmd[0] == "git" and cmd[1] == "push":
               push_calls.append(list(cmd))
               if len(push_calls) == 1:
                   raise subprocess.CalledProcessError(
                       1, cmd, output="",
                       stderr="! [rejected] foreman/issue-7 -> foreman/issue-7 (fetch first)\n",
                   )
               return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
           return real_run(cmd, *args, **kwargs)

       provider = GitHubProvider(identity=_identity("ghs_abc"), client=MagicMock())

       with patch("foreman.git_hosts.github.subprocess.run", side_effect=fake_run):
           provider.push_branch(worktree_path=wt, branch="foreman/issue-7")

       assert len(push_calls) == 2, "expected initial push + force-with-lease retry"
       assert "--force-with-lease" not in push_calls[0], "first push must be plain"
       assert "--force-with-lease" in push_calls[1], "retry must use --force-with-lease"
   ```

   `test_push_branch_does_not_retry_on_non_rejection_error`:
   ```python
   def test_push_branch_does_not_retry_on_non_rejection_error(tmp_path: Path) -> None:
       """A push failure that is NOT a non-fast-forward rejection must not trigger retry."""
       wt = _init_worktree(tmp_path)
       subprocess.run(
           ["git", "remote", "add", "origin", "https://github.com/owner/name.git"],
           cwd=wt, check=True, capture_output=True,
       )

       real_run = subprocess.run
       push_calls: list[list[str]] = []

       def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
           if isinstance(cmd, list) and len(cmd) > 1 and cmd[0] == "git" and cmd[1] == "push":
               push_calls.append(list(cmd))
               raise subprocess.CalledProcessError(
                   1, cmd, output="",
                   stderr="refusing to allow a GitHub App to create or update workflow without workflows permission",
               )
           return real_run(cmd, *args, **kwargs)

       provider = GitHubProvider(identity=_identity("ghs_abc"), client=MagicMock())

       with patch("foreman.git_hosts.github.subprocess.run", side_effect=fake_run):
           with pytest.raises(GitCommandError) as excinfo:
               provider.push_branch(worktree_path=wt, branch="foreman/issue-7")

       assert len(push_calls) == 1, "only one push attempt, no retry"
       assert "workflows permission" in excinfo.value.stderr
   ```

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/worktree.py` | Add `_worktree_gitdir_is_valid(worktree_path, *, role_token)` helper after `_local_branch_exists`; update `WorktreeManager.create()`, `create_impl()`, `attach()` (spec_pr path), and `attach_impl()` to validate-then-rmtree-or-return at each `wt_path.exists()` idempotency guard |
| `packages/foreman/src/foreman/git_hosts/github.py` | In `push_branch()`, wrap the push `_git()` call in try/except `GitCommandError`; on `[rejected] + (fetch first\|non-fast-forward)`, retry with `--force-with-lease`; all other errors re-raise |
| `packages/foreman/tests/test_worktree.py` | Add `test_create_replaces_worktree_with_foreign_gitdir`, `test_create_impl_replaces_worktree_with_foreign_gitdir`, `test_attach_replaces_worktree_with_foreign_gitdir`, `test_attach_impl_replaces_worktree_with_foreign_gitdir` |
| `packages/foreman/tests/test_git_hosts_github.py` | Add `test_push_branch_retries_with_force_on_non_fast_forward`, `test_push_branch_does_not_retry_on_non_rejection_error` |

## Alternatives considered

1. **Fetch-then-rebase before push instead of force-push.** Before pushing, run `git fetch origin <branch>` and then `git rebase origin/<branch>` to incorporate the remote's divergent commits before pushing. Rejected: the whole point of foreman-owned branches (`foreman/issue-N`, `foreman/impl-N`) is that each role run produces a clean, authoritative spec or impl — reconciling with a stale prior run's commits would produce a mixed history that the Reviewer or CI is not equipped to reason about. Force-push to the new clean HEAD is the correct semantic.

2. **Delete the remote branch before every push (`git push origin --delete <branch>` then push fresh).** More explicit than `--force-with-lease` and equally correct for owned-namespace branches. Rejected: requires an extra network round-trip and an authentication call; `--force-with-lease` achieves the same result in a single push. Also introduces a brief moment when the branch doesn't exist on the remote, which could confuse any concurrent observer.

3. **Apply gitdir validation only in `create()` (Planner), not in `attach()`/`attach_impl()` (Reviewer, Fixer).** The original issue's first failure manifested in the Planner, but the same stale-gitdir condition can block any role that reuses a worktree directory. Rejected: the fix is cheap (one `git rev-parse` that returns immediately on a valid gitdir) and applying it uniformly eliminates the failure class at the root rather than at one call site.

4. **Validate gitdir by reading `.git` file contents instead of running git.** Parse the `.git` file, extract the gitdir path, and check `Path(gitdir_path).exists()`. Rejected: brittle — would need to handle relative vs. absolute paths, OS path separators, and any edge cases git itself handles. Running `git rev-parse --git-dir` delegates that parsing to git, which knows the spec.

## Open questions

None. Both failure shapes are precisely described in the issue, the relevant code paths are small and isolated, and the fix approach follows existing patterns in the codebase (`_self_heal_orphaned_branch`, `_fetch_origin_branch` error detection).

## Out of scope

- Addressing the foreign-gitdir failure for `WorktreeManager.attach()` when `target="impl_pr"` dispatches to `attach_impl()` — the `attach_impl()` fix (sub-request 5) covers that path.
- Deleting stale REMOTE branches at startup (`foreman reset` remains the operator's tool for that; self-healing at push time is sufficient and less destructive).
- Force-pushing on the Fixer LLM's Bash-issued `git push` calls (the LLM has `GH_TOKEN` set and could push directly, but the push that matters is Python's `host.push_branch()` call after the LLM returns; fixing that covers the observed failure mode).
- Changing any retry limits, backoff, or state-machine behaviour — this is a pure git-layer fix.
- Validating worktrees created by the `prune()` method — `prune()` only removes, never returns, worktrees.
