# Sandbox Clone-Freshness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Guarantee every sandboxed role's clone reflects current GitHub — no role's box is ever stale relative to a prior role's pushed commits.

**Architecture:** GitHub is the source of truth. The daemon base clone becomes a bare mirror (an object cache with no local working branches, so a box can't inherit stale state). One chokepoint — `prepare_sandbox_clone` — force-fetches current refs into each box (fail-closed). The `CloneRefresher` keeps the mirror warm (best-effort, perf only).

**Tech Stack:** Python 3.12, uv, pytest, git, bubblewrap. Worktree `.worktrees/clone-freshness`, branch `feat/sandbox-clone-freshness` off `ba17670`.

## Global Constraints

- NO `Co-Authored-By` trailer. Conventional-commit lowercase subject.
- ruff google-style docstrings; `mypy --strict` clean, UNSCOPED: `uv run --no-sync mypy packages/foreman/src`.
- `just check` gate: 85% coverage floor, diff-cover 80.
- Worktree already `uv sync`-ed — use `uv run --no-sync`.
- Keep `ruff format` clean (`just check` doesn't run `--check`, per #433).
- **Error-handling split:** the chokepoint fetch is **fail-closed** (raise, never run on stale refs); the `CloneRefresher` is **best-effort** (swallow + log, don't advance throttle).
- Base-clone migration touches daemon startup — it MUST be idempotent and fail-closed.
- `prepare_sandbox_clone` is daemon-side git, so Tasks 1-4 tests are pure-git and run everywhere; only Task 5 needs real bwrap (self-skips off userns).

---

### Task 1: Reproduce #406 — a box prepped from a stale base lacks a pushed commit

**Files:**
- Test: `packages/foreman/tests/v4/test_sandbox_clone.py` (extend)

**Interfaces:**
- Consumes: `foreman.v4.sandbox_clone.prepare_sandbox_clone(*, base_clone_path, dest_clone_path, repo_url, role_token, runner=None)`.
- Produces: a failing test that pins the bug — after `prepare_sandbox_clone`, the box lacks a commit pushed to origin after the base was cloned.

- [ ] **Step 1: Write the failing test.** Add to `test_sandbox_clone.py`. It builds a real stand-in origin + a stale base, preps a box via the CURRENT flow, and asserts the box is missing the new commit (reproducing the exit-128 diff):

```python
def test_repro_406_box_from_stale_base_lacks_pushed_commit(tmp_path: Path) -> None:
    """REPRO of #406: prepare_sandbox_clone clones from the base and re-points
    origin, but does NOT fetch — so a commit pushed to origin after the base
    was cloned is absent in the box, and `git diff origin/main...<sha>` fails
    (exit 128). This test FAILS today and passes once Task 2 adds the fetch."""
    import subprocess
    from foreman.v4 import sandbox_clone

    def git(*args: str, cwd: Path) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    # stand-in origin (bare) with a main branch + one commit
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "t"], check=True)
    (seed / "a.txt").write_text("a\n")
    git("add", "-A", cwd=seed)
    git("commit", "-qm", "init", cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)

    # daemon base clone (co-located so --local hardlinks) — stale snapshot
    base = tmp_path / "base"
    subprocess.run(["git", "clone", str(origin), str(base)], check=True)

    # a prior role pushes a NEW commit to origin AFTER the base was cloned
    (seed / "b.txt").write_text("b\n")
    git("add", "-A", cwd=seed)
    git("commit", "-qm", "role-A change", cwd=seed)
    new_sha = git("rev-parse", "HEAD", cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)

    # next role's box, via the current prepare flow (origin re-pointed at the
    # real origin path here since there's no token auth in the test)
    dest = tmp_path / "scratch" / "box"
    sandbox_clone.prepare_sandbox_clone(
        base_clone_path=base,
        dest_clone_path=dest,
        repo_url=str(origin),
        role_token="ghs_UNUSED",
        runner=lambda argv: subprocess.run(argv, check=True, capture_output=True, text=True),
    )
    # BUG: the box lacks new_sha, so a diff against it fails (git exit 128)
    result = subprocess.run(
        ["git", "-C", str(dest), "diff", f"origin/main...{new_sha}"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"box is stale: diff against pushed commit failed (rc={result.returncode}): "
        f"{result.stderr.strip()}"
    )
```

Note: the test passes `runner=` so `prepare_sandbox_clone` runs plain `git` (no token URL). The `origin re-point` sets origin to `repo_url` (the local `origin.git`), which is what makes a later fetch reach the new commit.

- [ ] **Step 2: Run it — confirm it FAILS.**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox_clone.py::test_repro_406_box_from_stale_base_lacks_pushed_commit -q --no-cov`
Expected: FAIL — the diff returns 128 ("box is stale…"), because the current `prepare_sandbox_clone` never fetches `new_sha`.

- [ ] **Step 3: Commit the failing repro** (keep it red until Task 2; commit so the repro is recorded):

```bash
uv run --no-sync ruff format packages/foreman/tests/v4/test_sandbox_clone.py
git add packages/foreman/tests/v4/test_sandbox_clone.py
git commit -m "test(v4): repro #406 — box from stale base lacks pushed commit (xfail until fetch)"
```

Mark the test `@pytest.mark.xfail(reason="fixed in the next commit: prepare_sandbox_clone blanket fetch", strict=True)` in this commit so the suite stays green, then REMOVE the xfail in Task 2 when the fetch lands (strict=True makes it fail loudly once fixed if you forget).

---

### Task 2: `prepare_sandbox_clone` blanket-fetches (fail-closed) — the chokepoint

**Files:**
- Modify: `packages/foreman/src/foreman/v4/sandbox_clone.py` (`prepare_sandbox_clone`, after the origin re-point ~line 137-147)
- Test: `packages/foreman/tests/v4/test_sandbox_clone.py` (un-xfail Task 1's test; add a fail-closed test)

**Interfaces:**
- Consumes: existing `prepare_sandbox_clone` signature (unchanged).
- Produces: after the origin re-point, `prepare_sandbox_clone` runs `git -C <dest> fetch origin`; raises if the fetch fails.

- [ ] **Step 1: Remove the xfail from Task 1's test** so it becomes a live failing test again.

- [ ] **Step 2: Add the fail-closed test:**

```python
def test_prepare_sandbox_clone_raises_when_fetch_fails() -> None:
    """The chokepoint fetch is fail-closed: a fetch failure must raise, never
    let the role run on stale refs."""
    import subprocess
    import pytest
    from foreman.v4 import sandbox_clone

    calls: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        # let clone + set-url succeed; fail the fetch
        if argv[:1] == ["git"] and "fetch" in argv:
            raise subprocess.CalledProcessError(1, argv, stderr="boom")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(subprocess.CalledProcessError):
        sandbox_clone.prepare_sandbox_clone(
            base_clone_path=Path("/base"),
            dest_clone_path=Path("/dest"),
            repo_url="https://github.com/o/n.git",
            role_token="ghs_X",
            runner=runner,
        )
    assert any("fetch" in a for a in calls), "the chokepoint must attempt a fetch"
```

Adjust the fake-clone `.git` existence: since the test's `dest_clone_path=/dest` won't have `.git`, `prepare_sandbox_clone` will attempt the clone (via `runner`, which returns success) then set-url then fetch. Confirm the real function's `if not (dest/.git).exists()` guard — in this unit test `/dest/.git` does not exist, so the clone runner call happens; that's fine (runner is faked).

- [ ] **Step 3: Run both — confirm they FAIL** (repro fails on the stale box; fail-closed test fails because no fetch is issued yet).

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox_clone.py -q --no-cov`
Expected: the repro test FAILs (rc 128) and the fail-closed test FAILs (no fetch attempted).

- [ ] **Step 4: Implement the blanket fetch.** In `sandbox_clone.py`, after the `remote set-url origin` call, add:

```python
    # Freshness chokepoint (foreman clone-freshness design): after the local
    # hardlink clone (which reflects the base's snapshot) and the origin
    # re-point, fetch the current remote so the box sees every ref another
    # role may have just pushed. Blanket (all refs) — no branch to plumb, and
    # only missing objects download since the base seeds the rest. FAIL-CLOSED:
    # a fetch failure raises so the role never runs on a stale clone.
    run(["git", "-C", str(dest_clone_path), "fetch", "origin"])
```

(`run` is the same runner used for the clone/set-url; `check=True` in the default runner turns a non-zero fetch into `CalledProcessError`, which is the fail-closed behavior.)

- [ ] **Step 5: Run — confirm both PASS,** plus the full file:

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox_clone.py -q --no-cov`
Expected: PASS (repro now green — the box has `new_sha`; fail-closed green).

- [ ] **Step 6: Lint, mypy, commit:**

```bash
uv run --no-sync ruff format packages/foreman/src/foreman/v4/sandbox_clone.py packages/foreman/tests/v4/test_sandbox_clone.py
uv run --no-sync ruff check packages/foreman/src/foreman/v4/sandbox_clone.py packages/foreman/tests/v4/test_sandbox_clone.py
uv run --no-sync mypy packages/foreman/src
git add packages/foreman/src/foreman/v4/sandbox_clone.py packages/foreman/tests/v4/test_sandbox_clone.py
git commit -m "fix(v4): prepare_sandbox_clone blanket-fetches origin (fail-closed freshness chokepoint)"
```

---

### Task 3: Base clone becomes a bare mirror (with startup migration)

**Files:**
- Modify: `packages/foreman/src/foreman/worktree.py` (`ensure_clone`, ~line 57-95)
- Test: `packages/foreman/tests/test_worktree.py` (extend — the existing `ensure_clone` tests live here)

**Interfaces:**
- Consumes: existing `ensure_clone(*, repo_url, clone_path, token=None)`.
- Produces: `ensure_clone` creates the base as a **bare mirror** (`git clone --mirror`); if a base exists but is not a bare mirror, it is removed and re-mirrored. Fail-closed on removal/clone failure.

- [ ] **Step 1: Write the failing tests.** Add to `test_worktree.py` (mirror a local bare origin so no network):

```python
def test_ensure_clone_creates_bare_mirror(tmp_path: Path) -> None:
    import subprocess
    from foreman.worktree import ensure_clone

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True)
    base = tmp_path / "base"
    ensure_clone(repo_url=str(origin), clone_path=base)
    # a bare mirror: no working tree, is-bare true, has no local branch checkout
    is_bare = subprocess.run(
        ["git", "-C", str(base), "rev-parse", "--is-bare-repository"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert is_bare == "true"
    assert not (base / ".git").exists()  # a mirror IS the git dir


def test_ensure_clone_recreates_a_non_mirror_base(tmp_path: Path) -> None:
    import subprocess
    from foreman.worktree import ensure_clone

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True)
    base = tmp_path / "base"
    # simulate a legacy working clone with a stray local branch
    subprocess.run(["git", "clone", str(origin), str(base)], check=True)
    (base / "junk.txt").write_text("junk\n")
    assert (base / ".git").exists()  # working clone
    ensure_clone(repo_url=str(origin), clone_path=base)  # migrates
    is_bare = subprocess.run(
        ["git", "-C", str(base), "rev-parse", "--is-bare-repository"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert is_bare == "true"
    assert not (base / ".git").exists()
    assert not (base / "junk.txt").exists()  # working tree gone
```

- [ ] **Step 2: Run — confirm FAIL** (current `ensure_clone` makes a working clone, not a mirror; doesn't migrate).

Run: `uv run --no-sync pytest packages/foreman/tests/test_worktree.py -k ensure_clone -q --no-cov`
Expected: FAIL (`is-bare` == "false"; migration test fails).

- [ ] **Step 3: Implement.** Rewrite `ensure_clone`'s body. Read the current function first (worktree.py:57-95) to preserve the token-URL construction (`clone_url`); change the clone command to `--mirror` and add mirror-detection + recreation:

```python
    import shutil

    def _is_bare_mirror(path: Path) -> bool:
        if not path.exists():
            return False
        r = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-bare-repository"],
            capture_output=True, text=True,
        )
        return r.returncode == 0 and r.stdout.strip() == "true"

    if clone_path.exists() and not _is_bare_mirror(clone_path):
        # Legacy working clone (or a corrupt dir): the base holds no unique
        # state — everything is on GitHub — so recreate it as a mirror.
        # Fail-closed: a failed removal must not leave a half-migrated base.
        shutil.rmtree(clone_path)
    if _is_bare_mirror(clone_path):
        return
    clone_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--mirror", clone_url, str(clone_path)],
        check=True,
    )
```

Replace the old `if clone_path.exists() and not (clone_path/".git")…: raise` + `if (clone_path/".git").exists(): return` + the `git clone` call with the above. Update the docstring: base is now a bare mirror; a non-mirror base is recreated (idempotent; fail-closed). Keep the `clone_url` token-embedding lines above unchanged.

- [ ] **Step 4: Run — confirm PASS,** plus the whole `test_worktree.py`:

Run: `uv run --no-sync pytest packages/foreman/tests/test_worktree.py -q --no-cov`
Expected: PASS. If other `ensure_clone` call sites in the tests assumed a working clone, they should now get a mirror — check for and fix any that break (they should be few; `ensure_clone` is a startup helper).

- [ ] **Step 5: Lint, mypy, commit:**

```bash
uv run --no-sync ruff format packages/foreman/src/foreman/worktree.py packages/foreman/tests/test_worktree.py
uv run --no-sync ruff check packages/foreman/src/foreman/worktree.py packages/foreman/tests/test_worktree.py
uv run --no-sync mypy packages/foreman/src
git add packages/foreman/src/foreman/worktree.py packages/foreman/tests/test_worktree.py
git commit -m "feat(worktree): base clone is a bare mirror; recreate a non-mirror base"
```

---

### Task 4: `CloneRefresher` fetches the whole mirror (perf, best-effort)

**Files:**
- Create/Modify: `packages/foreman/src/foreman/worktree.py` (add a `fetch_mirror(clone_path)` helper near `fetch_origin_default_branch`, ~line 834)
- Modify: `packages/foreman/src/foreman/v4/clone_refresh.py` (default `fetch` → `fetch_mirror`)
- Test: `packages/foreman/tests/v4/test_clone_refresh.py` (extend) and `packages/foreman/tests/test_worktree.py` (helper)

**Interfaces:**
- Consumes: `CloneRefresher(clone_paths, interval_seconds, clock, fetch=…)`.
- Produces: `foreman.worktree.fetch_mirror(clone_path: Path) -> None` — best-effort `git remote update --prune` on a bare mirror; the refresher's default `fetch` becomes `fetch_mirror`.

- [ ] **Step 1: Write the helper test** in `test_worktree.py`:

```python
def test_fetch_mirror_updates_all_refs(tmp_path: Path) -> None:
    import subprocess
    from foreman.worktree import ensure_clone, fetch_mirror

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "t"], check=True)
    (seed / "a.txt").write_text("a\n")
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True)

    base = tmp_path / "base"
    ensure_clone(repo_url=str(origin), clone_path=base)  # bare mirror at main

    # push a new branch to origin, then refresh the mirror
    subprocess.run(["git", "-C", str(seed), "checkout", "-qb", "feature"], check=True)
    (seed / "b.txt").write_text("b\n")
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-qm", "feat"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "feature"], check=True)

    fetch_mirror(base)
    # mirror now has the feature ref
    r = subprocess.run(
        ["git", "-C", str(base), "rev-parse", "--verify", "refs/heads/feature"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
```

- [ ] **Step 2: Run — confirm FAIL** (`fetch_mirror` doesn't exist).

Run: `uv run --no-sync pytest packages/foreman/tests/test_worktree.py::test_fetch_mirror_updates_all_refs -q --no-cov`
Expected: FAIL — `ImportError: cannot import name 'fetch_mirror'`.

- [ ] **Step 3: Implement `fetch_mirror`** in `worktree.py` (mirror the best-effort style of `fetch_origin_default_branch`):

```python
def fetch_mirror(clone_path: Path) -> None:
    """Best-effort refresh of a bare-mirror base clone: fetch ALL refs.

    Used by the daemon's throttled CloneRefresher to keep the mirror's object
    store warm so each box's clone-prep fetch is a small delta. Perf only —
    correctness comes from the per-box chokepoint fetch, so a failure here is
    logged and swallowed (the caller does not advance its throttle clock).
    """
    result = subprocess.run(
        ["git", "-C", str(clone_path), "remote", "update", "--prune"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"[foreman.worktree] warning: mirror refresh failed in {clone_path}: "
            f"{result.stderr.strip()}",
            file=sys.stderr,
        )
```

(Confirm `import sys` is present in worktree.py; the existing `fetch_origin_default_branch` warning uses the same pattern — match it.)

- [ ] **Step 4: Point the refresher at it.** In `clone_refresh.py`, change the import + default:

```python
from foreman.worktree import fetch_mirror
...
        fetch: FetchFn = fetch_mirror,   # was fetch_origin_default_branch (both places)
```

Update the two `fetch: FetchFn = fetch_origin_default_branch` defaults (`__init__` and `from_projects`) and the module docstring reference. Add a `test_clone_refresh.py` assertion that the default fetch is `fetch_mirror` (import both and `assert CloneRefresher.__init__.__defaults__` includes it, or simpler: construct `CloneRefresher.from_projects({"p": path}, interval_seconds=1, clock=…)` with a fake fetch and assert throttle/best-effort behavior is unchanged — reuse the existing throttle/best-effort tests, which should still pass with the new default).

- [ ] **Step 5: Run — confirm PASS,** plus both test files:

Run: `uv run --no-sync pytest packages/foreman/tests/test_worktree.py packages/foreman/tests/v4/test_clone_refresh.py -q --no-cov`
Expected: PASS.

- [ ] **Step 6: Lint, mypy, commit:**

```bash
uv run --no-sync ruff format packages/foreman/src/foreman/worktree.py packages/foreman/src/foreman/v4/clone_refresh.py packages/foreman/tests/test_worktree.py packages/foreman/tests/v4/test_clone_refresh.py
uv run --no-sync ruff check packages/foreman/src/foreman/worktree.py packages/foreman/src/foreman/v4/clone_refresh.py packages/foreman/tests/test_worktree.py packages/foreman/tests/v4/test_clone_refresh.py
uv run --no-sync mypy packages/foreman/src
git add packages/foreman/src/foreman/worktree.py packages/foreman/src/foreman/v4/clone_refresh.py packages/foreman/tests/test_worktree.py packages/foreman/tests/v4/test_clone_refresh.py
git commit -m "feat(v4): CloneRefresher warms the whole mirror (all refs, best-effort)"
```

---

### Task 5: Hermetic two-role-handoff lock (real bwrap)

**Files:**
- Test: `packages/foreman/tests/v4/test_sandbox_integration.py` (add one case; self-skips off userns via the module's `pytestmark`)

**Interfaces:**
- Consumes: `prepare_sandbox_clone` (Task 2), `ensure_clone` mirror (Task 3), `SandboxLauncher.build_argv`.
- Produces: an end-to-end lock — role B's box, prepared after role A pushes, sees A's commit and diffs it successfully, exercised through a real bwrap box.

- [ ] **Step 1: Write the test.** It builds a bare-mirror base + a stand-in origin, does a role-A-pushes → role-B-preps handoff, and runs the reviewer's diff shape inside a real box:

```python
def test_two_role_handoff_box_sees_prior_role_commit(tmp_path: Path) -> None:
    """The clone-freshness keystone: role B's box, prepared after role A pushed,
    must see A's commit and diff it cleanly (reproduces #406's exit-128 against
    the pre-fix flow). Runs a real bwrap box; self-skips off userns."""
    from foreman.worktree import ensure_clone
    from foreman.v4 import sandbox_clone

    def git(*a: str, cwd: Path) -> str:
        return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True)
    git("config", "user.email", "t@t.t", cwd=seed)
    git("config", "user.name", "t", cwd=seed)
    (seed / "a.txt").write_text("a\n")
    git("add", "-A", cwd=seed); git("commit", "-qm", "init", cwd=seed); git("push", "-q", "origin", "main", cwd=seed)

    base = tmp_path / "base"
    ensure_clone(repo_url=str(origin), clone_path=base)  # bare mirror

    # role A pushes a new commit to origin
    (seed / "b.txt").write_text("b\n")
    git("add", "-A", cwd=seed); git("commit", "-qm", "role A", cwd=seed)
    new_sha = git("rev-parse", "HEAD", cwd=seed); git("push", "-q", "origin", "main", cwd=seed)

    # role B's box, via the chokepoint (fetches origin)
    runner = lambda argv: subprocess.run(argv, check=True, capture_output=True, text=True)
    dest = tmp_path / "scratch" / "box"
    sandbox_clone.prepare_sandbox_clone(
        base_clone_path=base, dest_clone_path=dest, repo_url=str(origin),
        role_token="ghs_UNUSED", runner=runner,
    )
    # inside a REAL box, run the reviewer's diff shape against A's commit
    launcher = SandboxLauncher(cache_dir=str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir(exist_ok=True)
    argv = launcher.build_argv(
        role_token="ghs_UNUSED",
        scratch_dir=str(tmp_path / "scratch"),
        role_cmd=["git", "-C", str(dest), "diff", f"origin/main...{new_sha}"],
        repo_bind=(str(dest), str(dest)),
    )
    r = subprocess.run(argv, capture_output=True, text=True)
    assert r.returncode == 0, f"box could not diff prior-role commit: {r.stderr[-400:]}"
```

(Adjust `build_argv` binds to whatever the current signature needs so `dest` is readable in the box; the point is the diff runs against `new_sha` and succeeds.)

- [ ] **Step 2: Run it.** On Windows it self-skips (userns). In a userns container it passes. Run:
`uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox_integration.py -q --no-cov`
Expected on Windows: `skipped`. In-container (Task 6 step 2): `passed`.

- [ ] **Step 3: Lint, commit:**

```bash
uv run --no-sync ruff format packages/foreman/tests/v4/test_sandbox_integration.py
uv run --no-sync ruff check packages/foreman/tests/v4/test_sandbox_integration.py
git add packages/foreman/tests/v4/test_sandbox_integration.py
git commit -m "test(v4): real-bwrap lock — box sees a prior role's pushed commit"
```

---

### Task 6: Manual keystone — a real multi-role handoff (not a pytest)

Executed by the controller after Tasks 1-5 land, the whole-branch review is clean, the branch is merged, and the `:dev` image rebuilds. This is the real proof, per "never done without running the real backend."

- [ ] **Step 1: Merge + rebuild** the `:dev` image (main `image` workflow).
- [ ] **Step 2: In-container hermetic run** (the userns proof CI can't do): from the `:dev` image with the branch source mounted, run `test_sandbox_integration.py` and confirm the two-role-handoff case passes (no skip).
- [ ] **Step 3: Redeploy** the daemon (sandbox flag already on): deploy dir `git pull`, `docker compose pull daemon`, `docker compose up -d daemon`. **Note the daemon startup will migrate the base clones to mirrors** — check the logs for a clean startup (the existing agent_core base gets re-mirrored; confirm the daemon comes up healthy, not fail-closed).
- [ ] **Step 4: Re-drive #406** (or a fresh agent_core ticket): `docker exec foreman-daemon foreman set-state <ticket_id> SpecReview` (set-state works; `reset` doesn't — see the ticket-mutation reference). Watch `foreman ps` + the reviewer/fixer logs.
- [ ] **Step 5: Assert the keystone.** A SpecFix → SpecReview handoff completes with **no `git diff … exit 128`** — the reviewer's box sees the fixer's pushed commit. The ticket advances (SpecReview clean → next state) rather than erroring to Failed.
- [ ] **Step 6: On a clean pass**, update the `project_foreman_job_sandbox_isolation` memory (clone-freshness fixed) and let the ticket continue. On failure, capture the reviewer log's git error, and diagnose (base migrated? chokepoint fetch ran? origin token valid?).
