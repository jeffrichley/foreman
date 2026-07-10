# Spec: use `--force-with-lease` in `GitHubProvider.push_branch` to survive Fixer rebases (issue #494)

## Goal

Change `GitHubProvider.push_branch` to pass `--force-with-lease` on every `git push`, so the Fixer can push after a history-rewriting rebase or amend without crashing the pipeline. See issue [#494](https://github.com/jeffrichley/foreman/issues/494).

## Acceptance criteria

- `GitHubProvider.push_branch` calls `git push --force-with-lease <token-url> <branch>:<branch>`.
- A plain non-fast-forward push (e.g. after `git rebase`) from the Fixer no longer raises `GitCommandError`; the push succeeds.
- The existing `test_push_branch_uses_installation_token_url` test passes with updated position assertions (`--force-with-lease` at argv[2], token URL at argv[3], refspec at argv[4]).
- A new regression test `test_push_branch_uses_force_with_lease` is added to `packages/foreman/tests/test_git_hosts_github.py`, explicitly documenting the foreman#494 failure mode.
- `just check` (lint + typecheck + tests) exits zero.

## Approach

**Pattern (Decision 4):** No GoF pattern fits — this is a straightforward one-line flag addition to an existing method. The Google principle is **"make the right thing easy"**: `foreman/issue-*` and `foreman/impl-*` are single-owner bot branches (Planner, Worker, or Fixer is the only writer), so force-with-lease is always safe and removes the asymmetry between first-push and rebase-push callers.

**Why `--force-with-lease` rather than bare `--force`.** `--force-with-lease` (without a refname argument) compares the remote tracking state known at push time against the actual remote tip. If the remote moved unexpectedly (e.g. a concurrent daemon run that shouldn't exist), the push is rejected. Bare `--force` would overwrite silently. For bot-owned branches there is no concurrent writer, but `--force-with-lease` adds zero overhead and is the safe form per the issue author's explicit guidance.

**Why unconditional rather than opt-in per-caller.** Option 1 in the issue ("detect the rewrite and switch") requires the caller to track whether a rebase happened and propagate that decision up to the push site. The state is difficult to track reliably (the LLM may rebase in a shell command invisible to Python). Option 2 ("always `--force-with-lease` on bot branches") is simpler: every `push_branch` call is already on a bot-owned branch (Planner, Worker, Fixer are the only three callers), so the flag is always safe. No API surface change is needed; callers (`planner.py`, `worker.py`, `fixer.py`) are unchanged.

**One-line implementation change.** In `packages/foreman/src/foreman/git_hosts/github.py` line 210, the existing call is:

```python
self._git(worktree_path, "push", push_url, f"{branch}:{branch}")
```

The fix inserts `"--force-with-lease"` before the URL:

```python
self._git(worktree_path, "push", "--force-with-lease", push_url, f"{branch}:{branch}")
```

**Test updates.** The existing `test_push_branch_uses_installation_token_url` captures the full git argv and checks `pushed[2]` (currently the URL) and `pushed[3]` (currently the refspec). Inserting the flag at index 2 shifts URL to index 3 and refspec to index 4. The test must be updated to match. A stale comment ("so server-side rejects accidental force-push semantics") on that test is also updated to describe `--force-with-lease`.

A new test `test_push_branch_uses_force_with_lease` anchors the regression contract: it verifies `"--force-with-lease"` is present in the argv of the `git push` subprocess — a direct regression pin for the foreman#494 failure.

## Sub-requests (topologically sorted)

1. **Add `"--force-with-lease"` to `push_branch` in `packages/foreman/src/foreman/git_hosts/github.py`.**

   Current (line 210):
   ```python
   self._git(worktree_path, "push", push_url, f"{branch}:{branch}")
   ```

   New:
   ```python
   self._git(worktree_path, "push", "--force-with-lease", push_url, f"{branch}:{branch}")
   ```

   Also update the `push_branch` docstring to mention `--force-with-lease`:
   ```python
   def push_branch(self, worktree_path: Path, branch: str) -> None:
       """Push ``branch`` to origin, authenticating via an installation-token URL.

       Uses ``--force-with-lease`` so the push succeeds after a history-rewriting
       rebase or amend (e.g. the Fixer rebasing its impl branch onto origin/main).
       The flag is safe on bot-owned single-writer branches (``foreman/issue-*`` /
       ``foreman/impl-*``) and refuses to overwrite if the remote moved unexpectedly
       (unlike bare ``--force``).

       Reads the existing ``remote.origin.url`` to recover the owner/repo slug,
       then constructs an ``https://x-access-token:<token>@...`` push URL rather
       than using ``-c http.extraheader`` — the latter would leak the token into
       persistent git config, whereas the URL form scopes it to this one subprocess
       call.
       """
   ```

2. **Update `test_push_branch_uses_installation_token_url` in `packages/foreman/tests/test_git_hosts_github.py`.**

   With `--force-with-lease` inserted at argv[2], the URL shifts from [2] to [3] and the refspec from [3] to [4]. Update the assertion block:

   ```python
   assert len(push_calls) == 1
   pushed = push_calls[0]
   assert pushed[0:2] == ["git", "push"]
   assert pushed[2] == "--force-with-lease"
   url = pushed[3]
   assert url == "https://x-access-token:ghs_abc@github.com/owner/name.git"
   # refspec is branch:branch; --force-with-lease guards against unexpected
   # remote movement without silently overwriting concurrent writes.
   assert pushed[4] == "foreman/issue-7:foreman/issue-7"
   ```

3. **Add `test_push_branch_uses_force_with_lease` to `packages/foreman/tests/test_git_hosts_github.py`** — regression pin for foreman#494:

   ```python
   def test_push_branch_uses_force_with_lease(tmp_path: Path) -> None:
       """Regression for foreman#494: push_branch must use --force-with-lease.

       After the Fixer rebases its branch (rewriting commit SHAs), a plain
       ``git push`` is rejected non-fast-forward. ``--force-with-lease`` allows
       the rewritten history to land while still refusing to overwrite if the
       remote moved unexpectedly — the safe form for single-owner bot branches.
       """
       wt = _init_worktree(tmp_path)
       subprocess.run(
           ["git", "remote", "add", "origin", "https://github.com/owner/name.git"],
           cwd=wt,
           check=True,
           capture_output=True,
       )

       real_run = subprocess.run
       push_calls: list[list[str]] = []

       def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
           if isinstance(cmd, list) and len(cmd) > 1 and cmd[0] == "git" and cmd[1] == "push":
               push_calls.append(cmd)
               return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
           return real_run(cmd, *args, **kwargs)

       provider = GitHubProvider(identity=_identity("ghs_abc"), client=MagicMock())

       with patch("foreman.git_hosts.github.subprocess.run", side_effect=fake_run):
           provider.push_branch(worktree_path=wt, branch="foreman/impl-494")

       assert len(push_calls) == 1
       assert "--force-with-lease" in push_calls[0], (
           "push_branch must use --force-with-lease so Fixer rebases survive "
           "(foreman#494: plain push was rejected non-fast-forward after rebase)"
       )
   ```

4. **Run `just check`** — all tests must pass, mypy must exit zero.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/git_hosts/github.py` | Add `"--force-with-lease"` to the `git push` argv in `push_branch`; update method docstring |
| `packages/foreman/tests/test_git_hosts_github.py` | Update `test_push_branch_uses_installation_token_url` argv position assertions ([2]→flag, [3]→URL, [4]→refspec); update stale comment; add `test_push_branch_uses_force_with_lease` regression test |

## Alternatives considered

1. **Detect the rewrite (rebase/amend/reset in the Fixer's path) and switch to `--force-with-lease` only then.** This requires the Fixer LLM's git actions to be introspectable from Python (tracking `git rebase` / `git commit --amend` calls), which is unreliable — the LLM can rebase in any Bash invocation not observed by the role runner. Additionally, it introduces a conditional code path that adds complexity for zero benefit: the unconditional approach is safe on all three callers (Planner, Worker, Fixer) because all push to single-owner bot branches. Rejected.

2. **Add a `force_with_lease: bool = False` parameter to `push_branch` and have the Fixer pass `True`.** Requires callers to opt in explicitly, meaning future roles or paths that need force-with-lease would have to remember to set the flag. The interface also implies that some calls are "unsafe" without `force_with_lease=True`, which is misleading — all bot-branch pushes are single-owner, so all are safe with force-with-lease. Rejected in favor of the unconditional approach.

3. **Do nothing; tell the Reviewer not to request rebases.** The Reviewer correctly identifies BEHIND branches as a problem (per the paired issue #416). Suppressing that reviewer verdict would allow stale branches to accumulate undetected. The pipeline is designed to handle rebases; the push layer should support them. Rejected.

## Open questions

None. The root cause, the exact line to change, and the test update strategy are all unambiguous from the issue and the codebase.

## Out of scope

- Changes to the Reviewer prompt or Fixer prompt to discourage or encourage rebases.
- Adding a `force_with_lease` parameter to the `GitHostProvider` abstract base or any concrete implementation beyond `GitHubProvider`.
- The BEHIND-PR handling ticket (foreman#416) — that addresses how Foreman detects and initiates the rebase; this spec only fixes the push step that follows.
- Any change to the `_git` static method signature or error-surface behavior.
- Any change to `fixer.py`, `worker.py`, or `planner.py` call sites — they are unchanged.
