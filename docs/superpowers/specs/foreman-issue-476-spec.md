# Spec: auto-clone missing project repos at daemon startup (issue #476)

## Goal

Ensure that for every configured `[[projects]]` entry, the daemon auto-clones the
project's `local_clone_path` at bootstrap if the path is absent or not a valid git
working tree — eliminating the `FileNotFoundError` the daemon currently raises on its
first poll of a project whose clone doesn't exist on disk. This is Half A of the
"add/rename a repo = edit config + bounce foreman, no manual ops" pair (see issue
#477 for Half B: hot-reloadable project list).

Tracks issue [#476](https://github.com/jeffrichley/foreman/issues/476).

## Acceptance criteria

- A configured project whose `local_clone_path` is missing is auto-cloned at
  daemon boot (before any poll tick fires) and then polls cleanly — no
  `FileNotFoundError` in `CloneRefresher.tick()`.
- An existing valid clone (path exists with a `.git` directory) is NOT
  re-cloned, reset, or force-fetched — `ensure_clone` is a no-op.
- A path that exists but is NOT a git repo (no `.git` directory) causes
  `ensure_clone` to raise a `RuntimeError` with an actionable message
  ("remove the directory or repair it manually") that propagates to daemon
  startup and stops the daemon from starting. This is explicitly documented
  behavior, not a silent swallow.
- The clone uses the orchestrator GitHub App installation token (minted via
  `identity.get_role_token("orchestrator")`) embedded as
  `https://x-access-token:<token>@github.com/...` in the git clone URL. The
  orchestrator identity is used because cloning is a daemon-level boot
  operation, not tied to any specific role. This is documented here.
- A structured log line is emitted at `INFO` level on clone attempt and on
  clone success, carrying `project=`, `repo=`, and `clone_path=` fields. No
  log on the no-op path (valid clone already present).
- Tests cover:
  - `ensure_clone` with `token=` threads the token into the clone URL.
  - `ensure_clone` raises `RuntimeError` when path exists but has no `.git`.
  - `bootstrap_cli_context`: missing path → `ensure_clone` called with
    orchestrator token; existing `.git` → `ensure_clone` not called.
- `just check` exits zero.

## Approach

**Design principle**: "make the right thing easy" (Google §). The idempotent
`ensure_clone` helper already exists in `packages/foreman/src/foreman/worktree.py:44`
and is already called from `WorktreeManager.create()` at worktree-creation time.
The fix is to **wire it earlier** — at bootstrap — so the clone is guaranteed to
exist before `CloneRefresher.tick()` or any other path-dependent git subprocess runs.

**No GoF pattern**: this is a straightforward precondition gate at startup.

**Why `ensure_clone` already exists but isn't wired early enough**: it was written
to handle the first-run-container case when a role runs, not the boot case.
`CloneRefresher.tick()` (added in #407) runs `fetch_origin_default_branch(clone_path)`
on every tick, which calls `subprocess.run([...], cwd=clone_path)`. When
`clone_path` doesn't exist, the subprocess's `cwd` doesn't exist and Python raises
`FileNotFoundError` before git can run. The `CloneRefresher` catches this per-project
and logs a warning — but the project never recovers because the path never appears.

**Where to put the startup clone loop**: at the top of `bootstrap_cli_context` in
`packages/foreman/src/foreman/v4/bootstrap.py`, after `configure_logging(...)` (so
structured logs land in the configured log output) and before `CloneRefresher` is
built (so the refresher's first tick has a valid clone to fetch from). Import
`ensure_clone` from `foreman.worktree` at module level so tests can monkeypatch
`foreman.v4.bootstrap.ensure_clone` to a no-op.

**Authentication for the clone**: the daemon's main process has no `GH_TOKEN` in its
environment (unlike role subprocesses, which have `GH_TOKEN` injected by
`SubprocessRoleDispatcher`). The `IdentityProvider` in `bootstrap_cli_context`
provides `get_role_token("orchestrator")`, which returns a valid GitHub App
installation token. Pass this token to `ensure_clone`, which embeds it in the clone
URL as `https://x-access-token:<token>@github.com/<owner>/<repo>.git`. This avoids
reliance on any git credential helper being configured in the container. The token is
minted once before the loop and reused for all projects (the `V4IdentityRegistry`
caches it internally, but minting before the loop avoids multiple `get_role_token`
calls).

**"Path exists but no `.git`"** handling: `ensure_clone` raises `RuntimeError` with an
actionable message. The daemon then fails to start with that message visible in the
daemon log. The operator's fix is to remove the path (or inspect what's there) and
restart. This is the right policy: silently overwriting or "repairing" an unknown
directory is too destructive; a loud failure is safer.

**`ensure_clone` API change**: add `token: str | None = None` as a keyword parameter.
Default `None` keeps every existing caller (including `WorktreeManager.create()` and
`WorktreeManager.create_impl()`) unchanged. When the token is set, the authenticated
URL is constructed as:
```python
auth_url = f"https://x-access-token:{token}@" + repo_url[len("https://"):]
```
(only applied when `repo_url.startswith("https://")` — local-path test fixtures use
non-HTTPS URLs and must pass through unchanged).

**Test isolation for `test_bootstrap.py`**: existing bootstrap tests pass
`local_clone_path` paths that don't exist on disk. After this change,
`bootstrap_cli_context` would try to clone from those paths. An `autouse` fixture in
`test_bootstrap.py` monkeypatches `foreman.v4.bootstrap.ensure_clone` to a
`MagicMock()` so all existing tests continue to pass without network or filesystem
side-effects. New tests in the same file can override this mock to assert call
behavior.

## Sub-requests (topologically sorted)

1. **Enhance `ensure_clone` in `packages/foreman/src/foreman/worktree.py`** (lines
   44–70):
   - Add `token: str | None = None` keyword parameter.
   - Before the `if (clone_path / ".git").exists(): return` guard, add a check:
     if `clone_path.exists()` AND NOT `(clone_path / ".git").exists()`, raise
     `RuntimeError(f"ensure_clone: {clone_path} exists but is not a git "
     f"repository (no .git directory). Remove the path or repair it "
     f"manually before restarting the daemon.")`.
   - When `token` is not None and `repo_url.startswith("https://")`, build an
     authenticated URL:
     `auth_url = f"https://x-access-token:{token}@" + repo_url[len("https://"):]`
   - Pass `auth_url` (or `repo_url` when `token` is None) to the `git clone`
     subprocess. The `env=` argument is NOT changed (git reads auth from the
     embedded URL, not from `GH_TOKEN`).

2. **Add two tests to `packages/foreman/tests/test_worktree.py`** (after the
   existing `test_ensure_clone_creates_clone_when_missing`):
   - `test_ensure_clone_raises_on_non_git_dir`: create `tmp_path / "target"` as a
     plain (non-git) directory; call `ensure_clone(repo_url="...", clone_path=target)`;
     assert `RuntimeError` is raised with the string "not a git repository" in the
     message.
   - `test_ensure_clone_token_embedded_in_clone_url`: monkeypatch
     `foreman.worktree.subprocess.run` to a spy; call `ensure_clone` with a local
     origin URL that does NOT start with `"https://"` (so token is NOT embedded for
     local tests, the spy just confirms clone was called); SEPARATELY verify the
     token-embedding logic with a plain string test: assert
     `f"https://x-access-token:tok@" + "github.com/o/r.git"` equals what the
     function would build for `repo_url="https://github.com/o/r.git"`, `token="tok"`.
     (This is a unit test of the URL construction, not a full subprocess test, to
     avoid needing a live HTTPS endpoint.)

3. **Add startup clone loop to `packages/foreman/src/foreman/v4/bootstrap.py`**:
   - Add `from foreman.worktree import ensure_clone` to the import block.
   - Immediately after `configure_logging(log_dir=Path(config.log_dir), level=config.log_level)`,
     add a startup-clone block:
     ```python
     # Startup auto-clone: ensure each project's local_clone_path exists and
     # is a valid git clone before any poll or clone-refresh runs.
     # Uses the orchestrator App installation token (read-only; daemon-level
     # operation, not tied to any role). Raises RuntimeError if a path exists
     # but is not a git repo — daemon refuses to start with an actionable
     # message. Idempotent: an existing valid clone is a no-op.
     if config.projects:
         orch_token = identity.get_role_token("orchestrator")
         for pc in config.projects:
             clone_path = Path(pc.local_clone_path)
             if (clone_path / ".git").exists():
                 continue  # already a valid clone — skip, no log
             logger.info(
                 "startup: cloning missing project repo",
                 extra={
                     "project": pc.name,
                     "repo": pc.repo,
                     "clone_path": str(clone_path),
                 },
             )
             ensure_clone(
                 repo_url=f"https://github.com/{pc.repo}.git",
                 clone_path=clone_path,
                 token=orch_token,
             )
             logger.info(
                 "startup: project repo cloned",
                 extra={
                     "project": pc.name,
                     "repo": pc.repo,
                     "clone_path": str(clone_path),
                 },
             )
     ```
   - The pre-check `if (clone_path / ".git").exists(): continue` is intentional
     fast-path duplication: it avoids minting the orchestrator token when all
     clones are already present (the common post-first-boot case).

4. **Update `packages/foreman/tests/v4/test_bootstrap.py`**:
   - Add an `autouse=True` fixture `_stub_ensure_clone` that monkeypatches
     `foreman.v4.bootstrap.ensure_clone` to a `MagicMock()` so the existing tests
     are not affected by the new startup-clone step.
   - Add `test_bootstrap_clones_missing_project_at_startup`: override
     `_stub_ensure_clone` (non-autouse, named mock captured in a local), call
     `bootstrap_cli_context` with one project whose path doesn't exist; assert the
     mock was called once with `repo_url="https://github.com/owner/voice.git"`,
     `clone_path=Path(str(tmp_path / "voice"))`, and a non-empty `token`.
   - Add `test_bootstrap_skips_clone_for_existing_valid_clone`: create
     `(tmp_path / "voice" / ".git")` (make `.git` a directory so the guard fires);
     call `bootstrap_cli_context`; assert the `ensure_clone` mock was NOT called.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/worktree.py` | Add `token: str | None = None` param to `ensure_clone`; add "exists but no .git" guard; embed token in URL when set |
| `packages/foreman/src/foreman/v4/bootstrap.py` | Import `ensure_clone`; add startup clone loop after `configure_logging` |
| `packages/foreman/tests/test_worktree.py` | Add two tests: `test_ensure_clone_raises_on_non_git_dir`, `test_ensure_clone_token_embedded_in_clone_url` |
| `packages/foreman/tests/v4/test_bootstrap.py` | Add `_stub_ensure_clone` autouse fixture; add two new startup-clone tests |

## Alternatives considered

- **Clone at first-poll time (in `CloneRefresher.tick()`) instead of at bootstrap**:
  rejected — `CloneRefresher` doesn't have access to `IdentityProvider` and wiring
  the token there would be a larger refactor. Startup is a cleaner, single-shot
  guarantee: by the time the daemon starts ticking, all clones exist.
- **Add `repo_url` to `CloneRefresher` so it can clone on tick when the path is
  missing**: rejected — `CloneRefresher`'s job is periodic fetch, not initial
  provision. Mixing concerns makes it harder to test the two behaviors independently.
  SRP: bootstrap provisions clones, refresher keeps them current.
- **Do nothing / document the manual volume surgery**: rejected — this is the
  current state and is the exact failure mode the issue is filing against.

## Open questions

(none — the approach is clearly specified in the issue, the `ensure_clone` function
already exists and is tested, the `IdentityProvider.get_role_token` interface is the
established token-minting surface, and the `x-access-token` URL pattern is the
standard GitHub App HTTPS auth pattern.)

## Out of scope

- Hot-reloadable project list (#477 Half B) — that spec is separate and
  already filed.
- Automatically moving/repairing an existing non-git directory at
  `local_clone_path` — the loud-error approach is deliberate.
- Updating `CloneRefresher` to skip projects whose clone doesn't exist
  (the startup clone loop means this should never happen post-fix; the
  existing `CloneRefresher` warning log already covers the impossible case).
- Any change to how role subprocesses pass tokens to git (they already use
  `filtered_subprocess_env` and the existing `ensure_clone` path).
- `foreman init` or any CLI path — only the daemon bootstrap is in scope.
