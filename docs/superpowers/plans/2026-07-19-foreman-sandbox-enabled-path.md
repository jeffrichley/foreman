# Foreman Sandbox Enabled-Path Completion (#556) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `config.sandbox.enabled = true` work end-to-end — each role runs in a bwrap box against its own private, hardlinked per-job clone, loads config, commits, and pushes to GitHub, with the shared base repo never mounted and cleaned up on terminal landing.

**Architecture:** The daemon (trusted, outside the box) does a co-located `git clone --local` of the project's base repo into a per-job scratch dir (hardlinked objects → ~free) and re-points the private clone's `origin` at GitHub. `SandboxLauncher.build_argv` binds that private clone **read-write at the exact in-box path the role's config already names** (`ProjectConfig.local_clone_path`, e.g. `/foreman/repos/<project>`), plus the config + projects TOML files read-only, plus the writable worktree root at `/scratch`. Because the private clone sits where the role already looks, the role's `WorktreeManager.create` / `attach` path runs unchanged — its `git worktree add` now links off a **private** `.git`, not the shared base — so **no role or WorktreeManager code changes are required.** The shared base repo is simply never bound; the box sees a private copy at the same path. On terminal landing (Done/Failed/NeedsHelp) the daemon `rmtree`s the per-job scratch (hardlinked → cheap, cannot corrupt the base).

**Tech Stack:** Python 3.12, bubblewrap (`bwrap`), `git`, pydantic v2 config, pytest (+ pytest-cov, xdist), uv workspace.

## Global Constraints

Every task's requirements implicitly include this section. Copied verbatim from foreman canon.

- **Docstrings:** ruff Google-style docstrings (`D` rules enabled). Every public module/class/function gets a Google-style docstring.
- **Types:** mypy **strict**, run **unscoped** over the whole src tree: `uv run --no-sync mypy packages/foreman/src`. A scoped run misses cross-module implementers — always run the full path.
- **Quality gate:** `just check` must pass. It runs `lock-check lint format-check typecheck import-lint test`. Coverage floor **80%** (`--cov-fail-under=80`, branch coverage, `-n auto --dist=loadscope`); patch coverage **80%** via diff-cover.
- **Import-lint (R1/R2):** `foreman.v4.*` is pure substrate — it must **never** import a v3-substrate module. New v4 code (`sandbox_clone.py`, dispatcher, state) obeys R2. Run `just import-lint` (`PYTHONPATH=packages/foreman uv run --no-sync lint-imports`).
- **Commits:** conventional-commit, lowercase subject (`feat:`, `fix:`, `test:`, `refactor:`, `chore:`). **NO `Co-Authored-By` trailer** on foreman commits (check `git log -3 --format=%B` — foreman commits carry none).
- **Worktree venv:** this worktree (`e:/workspaces/ai/agents/foreman/.worktrees/sandbox-556`) already has its own `.venv` (uv-synced). Always invoke tools with `uv run --no-sync ...` so uv does not re-sync or create an empty venv.
- **Sandbox-behavior tests self-skip:** any test that boots a **real** `bwrap` guards with a `_userns_available()` skip (Linux + unprivileged user namespaces). CI cannot run userns (#555), so these are dogfood/host-only; the pure argv + helper unit tests carry cross-platform coverage.

---

## File Structure

**Created**

- `packages/foreman/src/foreman/v4/sandbox_clone.py` — daemon-side private-clone prep (`git clone --local` co-located + `origin` re-point) and per-job scratch cleanup. Pure argv builders + a thin runner seam; no v3 imports.
- `packages/foreman/tests/v4/test_sandbox_clone.py` — unit tests for the clone-prep argv/URL/idempotency and the cleanup helper (real `git` in `tmp_path`; no bwrap).

**Modified**

- `packages/foreman/src/foreman/v4/config.py` — `SandboxConfig.scratch_root` default moves onto the repos volume (`/foreman/repos/.scratch`) so the local clone hardlinks; docstrings updated.
- `packages/foreman/src/foreman/v4/sandbox.py` — `build_argv` gains a RW `repo_bind` (private clone → in-box repo path), RO `ro_file_binds` (config + projects files), and an optional writable Claude-session tmpfs; new fields documented.
- `packages/foreman/src/foreman/v4/subprocess_dispatcher.py` — the sandbox branch preps the private clone, binds it at `local_clone_path`, binds the config/projects files, and threads `FOREMAN_PROJECTS_PATH`. New ctor params: `sandbox_projects`, `sandbox_clone_prep`.
- `packages/foreman/src/foreman/v4/bootstrap.py` — passes the project map + scratch root (under the repos volume) into the dispatcher and the cleanup wiring.
- `packages/foreman/src/foreman/v4/state.py` — `StateContext.sandbox_scratch_root`; `_enter_terminal` fires per-job scratch cleanup on terminal landing.
- `packages/foreman/src/foreman/v4/worker_pool.py` + `daemon.py` — thread `sandbox_scratch_root` into every `StateContext`, parallel to `project_configs`.
- `packages/foreman/tests/v4/test_sandbox.py` — argv unit tests for the new binds + tmpfs.
- `packages/foreman/tests/v4/test_sandbox_dispatch.py` — dispatcher wiring tests (clone-prep called; repo + config binds present; two-scratch layout).
- `packages/foreman/tests/v4/test_sandbox_integration.py` — hermetic real-bwrap enabled-path test (self-skips); Task-7 regression docstring clarification.

---

## Task 1: Co-locate the sandbox scratch on the repos volume

The private clone hardlinks only when scratch and base repo share a filesystem. The proven failure ("Invalid cross-device link") happens when scratch is on the container writable layer. Move the default onto the repos volume.

**Files:**
- Modify: `packages/foreman/src/foreman/v4/config.py` (`SandboxConfig.scratch_root`, class + module docstrings)
- Test: `packages/foreman/tests/v4/test_config.py` (add a focused test; if a sandbox-config test module already exists, add there)

**Interfaces:**
- Consumes: nothing.
- Produces: `SandboxConfig.scratch_root: str` default `"/foreman/repos/.scratch"`.

- [ ] **Step 1: Write the failing test**

Add to `packages/foreman/tests/v4/test_config.py`:

```python
def test_sandbox_scratch_root_defaults_onto_repos_volume() -> None:
    """The per-job scratch defaults under the repos volume so `git clone --local` hardlinks.

    A scratch dir on a different filesystem than the base repo makes the
    local clone fail with "Invalid cross-device link" (hardlinks cannot
    cross devices). Co-locating under /foreman/repos guarantees the free clone.
    """
    from foreman.v4.config import SandboxConfig

    cfg = SandboxConfig()
    assert cfg.scratch_root == "/foreman/repos/.scratch"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_config.py::test_sandbox_scratch_root_defaults_onto_repos_volume -v`
Expected: FAIL — `assert '/foreman/scratch' == '/foreman/repos/.scratch'`.

- [ ] **Step 3: Write minimal implementation**

In `packages/foreman/src/foreman/v4/config.py`, change the field default:

```python
    scratch_root: str = "/foreman/repos/.scratch"
```

Update the `SandboxConfig` class docstring `scratch_root:` line and the module docstring `[sandbox]` `scratch_root` line to read:

```
        scratch_root: Host directory under which per-job scratch dirs are
            created. MUST sit on the same filesystem as the projects' base
            clones (``local_clone_path``, i.e. the repos volume) so the
            daemon's ``git clone --local`` into scratch hardlinks the object
            store (free) instead of failing with "Invalid cross-device
            link". Defaults under ``/foreman/repos`` for exactly this reason.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_config.py::test_sandbox_scratch_root_defaults_onto_repos_volume -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/config.py packages/foreman/tests/v4/test_config.py
git commit -m "feat: co-locate sandbox scratch on repos volume for hardlinked clones"
```

---

## Task 2: `build_argv` binds the private clone RW + config/projects files RO

The box must (a) see the private clone read-write at the path the role's config names, and (b) read the config + projects TOML so `load_v4_config` / `load_projects` succeed. Add both to the launcher's mount plan.

**Files:**
- Modify: `packages/foreman/src/foreman/v4/sandbox.py` (`SandboxLauncher.build_argv`)
- Test: `packages/foreman/tests/v4/test_sandbox.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SandboxLauncher.build_argv(self, *, role_token: str, scratch_dir: str, role_cmd: list[str], passthrough: Mapping[str, str] | None = None, repo_bind: tuple[str, str] | None = None, ro_file_binds: tuple[tuple[str, str], ...] = ()) -> list[str]`. `repo_bind` is `(host_clone_path, box_repo_path)` bound `--bind` (RW); each `ro_file_binds` entry is `(host_path, box_path)` bound `--ro-bind`.

- [ ] **Step 1: Write the failing test**

Add to `packages/foreman/tests/v4/test_sandbox.py` (the helpers `_has_triple` already exist in that file):

```python
def test_argv_binds_private_repo_rw_and_config_files_ro() -> None:
    launcher = SandboxLauncher(cache_dir="/root/.cache/uv")
    argv = launcher.build_argv(
        role_token="ghs_SECRET",
        scratch_dir="/foreman/repos/.scratch/foreman/worker-537/wt",
        role_cmd=["foreman", "implement", "--project", "foreman"],
        repo_bind=(
            "/foreman/repos/.scratch/foreman/worker-537/clone",
            "/foreman/repos/foreman",
        ),
        ro_file_binds=(
            ("/foreman/state/config.toml", "/foreman/state/config.toml"),
            ("/root/.foreman/projects.toml", "/root/.foreman/projects.toml"),
        ),
    )
    # private clone is RW-bound at the in-box repo path the role's config names
    assert _has_triple(
        argv,
        "--bind",
        "/foreman/repos/.scratch/foreman/worker-537/clone",
        "/foreman/repos/foreman",
    )
    # it is NOT read-only — the role commits into it
    assert not _has_triple(
        argv, "--ro-bind", "/foreman/repos/.scratch/foreman/worker-537/clone", "/foreman/repos/foreman"
    )
    # config + projects files are RO-bound at their expected paths
    assert _has_triple(argv, "--ro-bind", "/foreman/state/config.toml", "/foreman/state/config.toml")
    assert _has_triple(argv, "--ro-bind", "/root/.foreman/projects.toml", "/root/.foreman/projects.toml")


def test_argv_omits_repo_and_file_binds_when_not_requested() -> None:
    """Back-compat: existing callers that pass neither get the pre-#556 argv shape."""
    launcher = SandboxLauncher(cache_dir="/root/.cache/uv")
    argv = launcher.build_argv(
        role_token="ghs_X",
        scratch_dir="/scratch",
        role_cmd=["foreman", "plan"],
    )
    # no stray config bind, no RW bind other than cache + scratch
    assert argv.count("--bind") == 2  # cache + scratch only
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox.py::test_argv_binds_private_repo_rw_and_config_files_ro -v`
Expected: FAIL — `build_argv() got an unexpected keyword argument 'repo_bind'`.

- [ ] **Step 3: Write minimal implementation**

In `packages/foreman/src/foreman/v4/sandbox.py`, extend `build_argv`'s signature and mount plan. Replace the signature and the tail (from the two `--bind` mount pairs through `return argv`):

```python
    def build_argv(
        self,
        *,
        role_token: str,
        scratch_dir: str,
        role_cmd: list[str],
        passthrough: Mapping[str, str] | None = None,
        repo_bind: tuple[str, str] | None = None,
        ro_file_binds: tuple[tuple[str, str], ...] = (),
    ) -> list[str]:
        """Return the ``bwrap`` argv wrapping ``role_cmd``.

        Args:
            role_token: The job's short-lived scoped GitHub role token; set
                as ``GH_TOKEN`` inside the box and nothing else secret.
            scratch_dir: Host path of this job's writable worktree root,
                bind-mounted read-write to ``scratch_mount``. The role's
                ``WorktreeManager`` roots its worktree here via
                ``FOREMAN_WORKTREES_ROOT=/scratch``.
            role_cmd: The unwrapped role command (``["foreman", "implement",
                ...]``) to run inside the box.
            passthrough: Extra non-secret env vars to forward (state-instance
                id, session-resume ids, config paths). The dispatcher curates
                this allowlist.
            repo_bind: ``(host_clone_path, box_repo_path)`` for the daemon's
                private per-job clone, bind-mounted READ-WRITE at
                ``box_repo_path`` — the exact in-box path the role's
                ``ProjectConfig.local_clone_path`` names. The role's normal
                ``git worktree add`` then links off this PRIVATE ``.git``, not
                the shared base repo (which is never mounted). ``None`` for
                pre-#556 / test callers.
            ro_file_binds: ``(host_path, box_path)`` pairs bind-mounted
                READ-ONLY — the ``FOREMAN_V4_CONFIG`` + ``FOREMAN_PROJECTS_PATH``
                TOML files the role loads via ``load_v4_config`` /
                ``load_projects``. Small config files, not the crown-jewel
                secrets in :data:`DAEMON_NEVER_BIND`.

        Returns:
            The full argv: ``bwrap`` + namespace/mount/env flags + ``--`` +
            ``role_cmd``.
        """
        setenv: dict[str, str] = {
            "PATH": SANDBOX_STD_PATH,
            "HOME": "/root",
            "PYTHONUNBUFFERED": "1",
            "PYTHONNOUSERSITE": "1",
            "FOREMAN_WORKTREES_ROOT": self.scratch_mount,
            "UV_CACHE_DIR": self.cache_mount,
            "GH_TOKEN": role_token,
        }
        if passthrough:
            setenv.update(passthrough)

        argv: list[str] = [
            self.bwrap_path,
            "--clearenv",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--share-net",
            "--die-with-parent",
            "--tmpfs",
            "/tmp",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/bin",
            "/bin",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind-try",
            "/lib64",
            "/lib64",
            "--ro-bind-try",
            "/sbin",
            "/sbin",
            "--ro-bind",
            "/etc/resolv.conf",
            "/etc/resolv.conf",
            "--ro-bind",
            "/etc/ssl/certs",
            "/etc/ssl/certs",
            "--ro-bind-try",
            "/etc/ca-certificates",
            "/etc/ca-certificates",
            "--ro-bind-try",
            "/etc/gitconfig",
            "/etc/gitconfig",
            "--bind",
            self.cache_dir,
            self.cache_mount,
            "--bind",
            scratch_dir,
            self.scratch_mount,
        ]
        # RW: the daemon's private per-job clone, at the box path the role's
        # config already names (so the role's worktree-add path is unchanged).
        if repo_bind is not None:
            host_clone, box_repo = repo_bind
            argv += ["--bind", host_clone, box_repo]
        # RO: the config + projects TOML the role loads at startup.
        for host_file, box_file in ro_file_binds:
            argv += ["--ro-bind", host_file, box_file]
        for extra in self.extra_ro_binds:
            argv += ["--ro-bind-try", extra, extra]
        for key in sorted(setenv):
            argv += ["--setenv", key, setenv[key]]
        argv += ["--chdir", self.scratch_mount, "--"]
        argv += role_cmd
        return argv
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox.py -v`
Expected: PASS (new tests + all pre-existing argv tests still green).

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/sandbox.py packages/foreman/tests/v4/test_sandbox.py
git commit -m "feat: bind private clone rw + config files ro in sandbox argv"
```

---

## Task 3: Daemon private-clone prep helper (`sandbox_clone.py`)

The trusted daemon clones the base repo into the co-located scratch (hardlinked) and re-points `origin` at GitHub so the box — which never sees the base repo path — can fetch/push over the network. Pure argv/URL builders + a runner seam make it unit-testable without touching the network.

**Files:**
- Create: `packages/foreman/src/foreman/v4/sandbox_clone.py`
- Test: `packages/foreman/tests/v4/test_sandbox_clone.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `sandbox_clone_argv(base_clone_path: Path, dest_clone_path: Path) -> list[str]`
  - `tokenized_origin_url(repo_url: str, role_token: str) -> str`
  - `prepare_sandbox_clone(*, base_clone_path: Path, dest_clone_path: Path, repo_url: str, role_token: str, runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None) -> None`

- [ ] **Step 1: Write the failing test**

Create `packages/foreman/tests/v4/test_sandbox_clone.py`:

```python
"""Unit tests for the daemon-side sandbox clone-prep helper (foreman#556)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from foreman.v4.sandbox_clone import (
    prepare_sandbox_clone,
    sandbox_clone_argv,
    tokenized_origin_url,
)


def test_clone_argv_is_local_clone() -> None:
    argv = sandbox_clone_argv(Path("/foreman/repos/foreman"), Path("/scratch/clone"))
    assert argv == ["git", "clone", "--local", "/foreman/repos/foreman", "/scratch/clone"]


def test_tokenized_origin_url_embeds_token_for_https() -> None:
    url = tokenized_origin_url("https://github.com/o/n.git", "ghs_TOK")
    assert url == "https://x-access-token:ghs_TOK@github.com/o/n.git"


def test_tokenized_origin_url_passes_non_https_through() -> None:
    assert tokenized_origin_url("git@github.com:o/n.git", "ghs_TOK") == "git@github.com:o/n.git"


def test_prepare_clones_when_absent_then_repoints_origin(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    dest = tmp_path / "job" / "clone"
    prepare_sandbox_clone(
        base_clone_path=tmp_path / "base",
        dest_clone_path=dest,
        repo_url="https://github.com/o/n.git",
        role_token="ghs_TOK",
        runner=fake_runner,
    )
    assert calls[0] == ["git", "clone", "--local", str(tmp_path / "base"), str(dest)]
    assert calls[1] == [
        "git", "-C", str(dest), "remote", "set-url", "origin",
        "https://x-access-token:ghs_TOK@github.com/o/n.git",
    ]
    assert dest.parent.exists()  # parent created


def test_prepare_skips_clone_when_present_but_always_refreshes_origin(tmp_path: Path) -> None:
    """Idempotent re-entry (retry): reuse the existing clone but refresh the (short-lived) token."""
    calls: list[list[str]] = []

    def fake_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    dest = tmp_path / "clone"
    (dest / ".git").mkdir(parents=True)  # simulate an existing clone
    prepare_sandbox_clone(
        base_clone_path=tmp_path / "base",
        dest_clone_path=dest,
        repo_url="https://github.com/o/n.git",
        role_token="ghs_NEW",
        runner=fake_runner,
    )
    assert not any(c[:3] == ["git", "clone", "--local"] for c in calls)
    assert calls[-1][-1] == "https://x-access-token:ghs_NEW@github.com/o/n.git"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox_clone.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'foreman.v4.sandbox_clone'`.

- [ ] **Step 3: Write minimal implementation**

Create `packages/foreman/src/foreman/v4/sandbox_clone.py`:

```python
"""Daemon-side private per-job clone prep + scratch cleanup (foreman#556).

The sandbox's enabled path gives each role its OWN self-contained clone
instead of a linked worktree that shares the base repo's ``.git`` (a
volume every job + the daemon poller touch). The trusted daemon — which
CAN see ``/foreman/repos`` — does a co-located ``git clone --local`` of
the base into the per-job scratch (hardlinked object store: ~zero disk,
free ONLY when scratch and base share a filesystem), then re-points the
private clone's ``origin`` at GitHub so the sealed box (which never sees
the base repo path) can fetch/push over the open network.

The box binds this private clone READ-WRITE at the in-box path the role's
``ProjectConfig.local_clone_path`` already names, so the role's normal
``WorktreeManager`` path runs unchanged — its ``git worktree add`` links
off a PRIVATE ``.git``.

Pure orchestration under ``foreman.v4`` — no v3-substrate import (R2).
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

#: Role bases whose per-job scratch dirs a ticket accumulates. Terminal
#: cleanup removes ``<scratch_root>/<project>/<base>-<issue>`` for each.
_SANDBOX_ROLE_BASES: tuple[str, ...] = ("planner", "reviewer", "fixer", "worker")

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def sandbox_clone_argv(base_clone_path: Path, dest_clone_path: Path) -> list[str]:
    """Return the ``git clone --local`` argv (hardlinks when co-located).

    ``--local`` hardlinks the object store instead of copying — free, but
    ONLY when ``dest_clone_path`` sits on the same filesystem as
    ``base_clone_path`` (else git falls back to a cross-device copy, or
    fails outright on a hardlink attempt). Co-location is guaranteed by
    :attr:`SandboxConfig.scratch_root` defaulting under the repos volume.
    """
    return ["git", "clone", "--local", str(base_clone_path), str(dest_clone_path)]


def tokenized_origin_url(repo_url: str, role_token: str) -> str:
    """Embed the role token in an HTTPS remote URL for credential-less auth.

    Mirrors ``foreman.worktree.ensure_clone``: for an ``https://`` URL the
    token is inlined as ``https://x-access-token:<token>@...`` so ``git
    fetch`` / ``git push origin`` inside the box authenticate as the role
    bot without a credential helper. Non-HTTPS URLs pass through unchanged.
    """
    prefix = "https://"
    if repo_url.startswith(prefix):
        return f"https://x-access-token:{role_token}@" + repo_url[len(prefix) :]
    return repo_url


def _default_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` capturing output, raising on non-zero exit."""
    return subprocess.run(  # argv is built here, not user input
        argv,
        check=True,
        capture_output=True,
        text=True,
    )


def prepare_sandbox_clone(
    *,
    base_clone_path: Path,
    dest_clone_path: Path,
    repo_url: str,
    role_token: str,
    runner: Runner | None = None,
) -> None:
    """Ensure a private per-job clone exists at ``dest_clone_path``.

    Idempotent: when ``dest_clone_path/.git`` already exists (a retry
    reusing the same scratch) the clone step is skipped. The ``origin``
    re-point runs EVERY call so a rotated short-lived role token is always
    refreshed on the private clone's remote.

    Args:
        base_clone_path: The project's shared base clone (e.g.
            ``/foreman/repos/<project>``); read-only source for the local
            clone. Never mounted into the box.
        dest_clone_path: Where the private clone lands — must be co-located
            with ``base_clone_path`` for the hardlink (see
            :func:`sandbox_clone_argv`).
        repo_url: The GitHub HTTPS URL (``https://github.com/<owner>/<name>.git``)
            the private clone's ``origin`` is re-pointed at.
        role_token: The dispatching role's short-lived GitHub token, inlined
            into the ``origin`` URL for network fetch/push.
        runner: Test seam ``(argv) -> CompletedProcess``. Defaults to a real
            ``subprocess.run(check=True)``.
    """
    run = runner if runner is not None else _default_runner
    if not (dest_clone_path / ".git").exists():
        dest_clone_path.parent.mkdir(parents=True, exist_ok=True)
        run(sandbox_clone_argv(base_clone_path, dest_clone_path))
    run(
        [
            "git",
            "-C",
            str(dest_clone_path),
            "remote",
            "set-url",
            "origin",
            tokenized_origin_url(repo_url, role_token),
        ]
    )


def cleanup_ticket_scratch(
    *,
    scratch_root: Path,
    project: str,
    issue_number: int,
    role_bases: Sequence[str] = _SANDBOX_ROLE_BASES,
) -> list[Path]:
    """Remove every per-job scratch dir a ticket accumulated. Returns the removed paths.

    Called on terminal landing (Done/Failed/NeedsHelp). Because the clone's
    objects are hardlinked to the base, ``rmtree`` only drops the extra
    directory entries — it cannot corrupt the shared base repo. Silent when
    a dir is absent (a role that never ran, or already-cleaned).
    """
    removed: list[Path] = []
    project_root = scratch_root / project
    for base in role_bases:
        job_dir = project_root / f"{base}-{issue_number}"
        if not job_dir.exists():
            continue
        shutil.rmtree(job_dir, ignore_errors=True)
        if not job_dir.exists():
            removed.append(job_dir)
    return removed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox_clone.py -v`
Expected: PASS (4 tests). The `cleanup_ticket_scratch` helper is exercised in Task 6.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/sandbox_clone.py packages/foreman/tests/v4/test_sandbox_clone.py
git commit -m "feat: add daemon private-clone prep + scratch cleanup helpers"
```

---

## Task 4: Dispatcher preps the clone and wires the binds

The dispatcher's sandbox branch replaces the single-scratch mount with the two-part per-job layout (`clone/` + `wt/`), calls the clone-prep, and threads the config/projects file binds. It needs the project map to resolve `local_clone_path` + `repo`.

**Files:**
- Modify: `packages/foreman/src/foreman/v4/subprocess_dispatcher.py`
- Test: `packages/foreman/tests/v4/test_sandbox_dispatch.py`

**Interfaces:**
- Consumes: `SandboxLauncher.build_argv(..., repo_bind, ro_file_binds)` (Task 2); `prepare_sandbox_clone(...)` (Task 3); `ProjectConfig.local_clone_path`, `ProjectConfig.repo` (existing).
- Produces: `SubprocessRoleDispatcher.__init__(..., sandbox: SandboxLauncher | None = None, sandbox_scratch_root: Path | None = None, sandbox_projects: Mapping[str, ProjectConfig] | None = None, sandbox_clone_prep: Callable[..., None] | None = None)`.

- [ ] **Step 1: Write the failing test**

Add to `packages/foreman/tests/v4/test_sandbox_dispatch.py`. This reuses the existing `FakeProc` / `_StubStream` intercept pattern in that file — copy the `FakeProc` class shape from `test_dispatch_flag_on_creates_scratch_and_wraps` (it is defined inside that test; lift a module-level copy if preferred). Full test:

```python
def test_dispatch_flag_on_preps_clone_and_binds_repo_and_config(
    tmp_path: Path, monkeypatch
) -> None:
    """Enabled path: clone-prep runs, private clone RW-bound at local_clone_path,
    config + projects RO-bound, worktree root at /scratch."""
    from foreman.v4 import subprocess_dispatcher as sd
    from foreman.v4.config import ProjectConfig

    captured: dict[str, object] = {}

    class FakeProc:
        def __init__(self, cmd, **kw):
            captured["cmd"] = cmd
            self.stdout = _StubStream(
                'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}\n'
            )
            self.stderr = _StubStream("")
            self.pid = 4321
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(sd.subprocess, "Popen", FakeProc)
    monkeypatch.setenv("FOREMAN_V4_CONFIG", "/foreman/state/config.toml")
    monkeypatch.setenv("FOREMAN_PROJECTS_PATH", "/root/.foreman/projects.toml")

    prep_calls: list[dict[str, object]] = []

    def fake_prep(*, base_clone_path, dest_clone_path, repo_url, role_token):
        prep_calls.append(
            {
                "base": str(base_clone_path),
                "dest": str(dest_clone_path),
                "url": repo_url,
                "token": role_token,
            }
        )

    identity = MagicMock()
    identity.get_role_token.return_value = "ghs_TOK"
    scratch_root = tmp_path / "repos" / ".scratch"
    projects = {
        "foreman": ProjectConfig(
            name="foreman", repo="jeffrichley/foreman", local_clone_path="/foreman/repos/foreman"
        )
    }
    d = sd.SubprocessRoleDispatcher(
        foreman_cli=["foreman"],
        identity=identity,
        log_dir=tmp_path / "logs",
        sandbox=SandboxLauncher(cache_dir="/root/.cache/uv", bwrap_path="bwrap"),
        sandbox_scratch_root=scratch_root,
        sandbox_projects=projects,
        sandbox_clone_prep=fake_prep,
    )
    out = d.dispatch(role="worker", project="foreman", issue_number=537, ticket_id=99)
    assert "FOREMAN_OUTCOME:" in out

    # clone-prep called with base=local_clone_path, dest under scratch, github url + token
    assert len(prep_calls) == 1
    job = scratch_root / "foreman" / "worker-537"
    assert prep_calls[0]["base"] == "/foreman/repos/foreman"
    assert prep_calls[0]["dest"] == str(job / "clone")
    assert prep_calls[0]["url"] == "https://github.com/jeffrichley/foreman.git"
    assert prep_calls[0]["token"] == "ghs_TOK"

    cmd = captured["cmd"]
    assert cmd[0] == "bwrap"
    # worktree root (wt/) bound at /scratch
    assert str(job / "wt") in cmd
    assert (job / "wt").exists()
    # private clone RW-bound at the in-box repo path
    i = cmd.index("--bind", cmd.index("--bind", cmd.index("--bind") + 1) + 1)  # 3rd --bind
    assert cmd[i + 1] == str(job / "clone")
    assert cmd[i + 2] == "/foreman/repos/foreman"
    # config + projects RO-bound
    assert "/foreman/state/config.toml" in cmd
    assert "/root/.foreman/projects.toml" in cmd


def test_dispatch_flag_on_without_project_map_raises(tmp_path: Path) -> None:
    from foreman.v4.subprocess_dispatcher import RoleSubprocessError, SubprocessRoleDispatcher

    identity = MagicMock()
    identity.get_role_token.return_value = "ghs_X"
    d = SubprocessRoleDispatcher(
        foreman_cli=["foreman"],
        identity=identity,
        log_dir=tmp_path,
        sandbox=SandboxLauncher(cache_dir="/c"),
        sandbox_scratch_root=tmp_path / "s",
        sandbox_projects=None,
    )
    import pytest

    with pytest.raises(RoleSubprocessError, match="project map"):
        d.dispatch(role="worker", project="foreman", issue_number=1, ticket_id=1)
```

Note: the existing `test_dispatch_flag_on_creates_scratch_and_wraps` asserts the OLD single-scratch path (`scratch_root/foreman/worker-537` bound directly at `/scratch`). Update that test to the new `wt/` sub-layout: change `expected_scratch = scratch_root / "foreman" / "worker-537"` to `scratch_root / "foreman" / "worker-537" / "wt"`, and give the dispatcher a `sandbox_projects` map + `sandbox_clone_prep=lambda **kw: None` so it does not shell out to `git`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox_dispatch.py -v`
Expected: FAIL — `__init__() got an unexpected keyword argument 'sandbox_projects'`.

- [ ] **Step 3: Write minimal implementation**

In `packages/foreman/src/foreman/v4/subprocess_dispatcher.py`:

Add imports near the top (with the other `from collections.abc` / `foreman.v4` imports):

```python
from collections.abc import Callable, Mapping
from foreman.v4.config import ProjectConfig
from foreman.v4.sandbox_clone import prepare_sandbox_clone
```

Extend `__init__` (add the two params + store them; keep existing params unchanged):

```python
    def __init__(
        self,
        *,
        foreman_cli: list[str],
        identity: IdentityProvider,
        log_dir: Path,
        timeout_seconds: int = 600,
        inactivity_timeout_seconds: int = 0,
        sandbox: SandboxLauncher | None = None,
        sandbox_scratch_root: Path | None = None,
        sandbox_projects: Mapping[str, ProjectConfig] | None = None,
        sandbox_clone_prep: Callable[..., None] | None = None,
    ) -> None:
        self._foreman_cli = foreman_cli
        self._identity = identity
        self._log_dir = log_dir
        self._timeout = timeout_seconds
        self._inactivity_timeout = inactivity_timeout_seconds
        self._sandbox = sandbox
        self._sandbox_scratch_root = sandbox_scratch_root
        # foreman#556: project map (local_clone_path + repo) so the
        # dispatcher can prep the private clone; clone-prep is a seam so
        # unit tests don't shell out to git.
        self._sandbox_projects = sandbox_projects
        self._sandbox_clone_prep = sandbox_clone_prep or prepare_sandbox_clone
```

Replace the sandbox branch in `dispatch()` (the block currently starting `if self._sandbox is not None:` through the `cmd = self._sandbox.build_argv(...)` call) with:

```python
        if self._sandbox is not None:
            if self._sandbox_scratch_root is None:
                raise RoleSubprocessError(
                    f"role={role}: sandbox enabled but no scratch root "
                    f"configured; cannot create the job's writable mount"
                )
            if self._sandbox_projects is None:
                raise RoleSubprocessError(
                    f"role={role}: sandbox enabled but no project map "
                    f"configured; cannot resolve the base clone to prep"
                )
            project_cfg = self._sandbox_projects.get(project)
            if project_cfg is None:
                raise RoleSubprocessError(
                    f"role={role}: project {project!r} absent from the "
                    f"sandbox project map; cannot prep its private clone"
                )
            role_base = _base_role(role)
            job_dir = self._sandbox_scratch_root / project / f"{role_base}-{issue_number}"
            clone_dir = job_dir / "clone"
            wt_dir = job_dir / "wt"
            wt_dir.mkdir(parents=True, exist_ok=True)
            # foreman#556: prep the private clone OUTSIDE the box (trusted
            # daemon has /foreman/repos). Co-located → hardlinked; origin
            # re-pointed at GitHub for network fetch/push.
            self._sandbox_clone_prep(
                base_clone_path=Path(project_cfg.local_clone_path),
                dest_clone_path=clone_dir,
                repo_url=f"https://github.com/{project_cfg.repo}.git",
                role_token=env["GH_TOKEN"],
            )
            # Bind the config + projects TOML RO so load_v4_config /
            # load_projects succeed; thread FOREMAN_PROJECTS_PATH into the box.
            ro_file_binds: list[tuple[str, str]] = []
            config_path = env.get("FOREMAN_V4_CONFIG")
            if config_path:
                ro_file_binds.append((config_path, config_path))
            projects_path = env.get("FOREMAN_PROJECTS_PATH")
            if projects_path:
                ro_file_binds.append((projects_path, projects_path))
            passthrough = {
                key: env[key]
                for key in (
                    "FOREMAN_STATE_INSTANCE_ID",
                    "FOREMAN_SESSION_ID",
                    "FOREMAN_RESUME_SESSION_ID",
                    "FOREMAN_V4_CONFIG",
                    "FOREMAN_PROJECTS_PATH",
                    "CLAUDE_CONFIG_DIR",
                    "ANTHROPIC_API_KEY",
                    "LANG",
                )
                if key in env
            }
            cmd = self._sandbox.build_argv(
                role_token=env["GH_TOKEN"],
                scratch_dir=str(wt_dir),
                role_cmd=cmd,
                passthrough=passthrough,
                repo_bind=(str(clone_dir), project_cfg.local_clone_path),
                ro_file_binds=tuple(ro_file_binds),
            )
```

Note: `env` already reads `os.environ` (line ~335 `env = dict(os.environ)`), so `FOREMAN_V4_CONFIG` / `FOREMAN_PROJECTS_PATH` are visible via `env.get(...)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox_dispatch.py -v`
Expected: PASS (new tests + the updated `test_dispatch_flag_on_creates_scratch_and_wraps`).

- [ ] **Step 5: Verify types + imports**

Run: `uv run --no-sync mypy packages/foreman/src && just import-lint`
Expected: no errors (importing `foreman.v4.config` / `foreman.v4.sandbox_clone` from a `v4` module is R2-clean).

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/v4/subprocess_dispatcher.py packages/foreman/tests/v4/test_sandbox_dispatch.py
git commit -m "feat: dispatcher preps private clone and binds repo + config in sandbox"
```

---

## Task 5: Bootstrap wires the project map into the dispatcher

`bootstrap.py` already builds `active_projects` and the launcher. Pass the project map so the dispatcher can prep clones.

**Files:**
- Modify: `packages/foreman/src/foreman/v4/bootstrap.py` (the `SubprocessRoleDispatcher(...)` construction, ~lines 215–226)
- Test: `packages/foreman/tests/v4/test_bootstrap.py` (extend an existing bootstrap test, or add a focused one)

**Interfaces:**
- Consumes: `SubprocessRoleDispatcher(..., sandbox_projects=...)` (Task 4).
- Produces: no new public symbol; dispatcher now receives `sandbox_projects={pc.name: pc for pc in active_projects}` when the sandbox is enabled.

- [ ] **Step 1: Write the failing test**

Add to `packages/foreman/tests/v4/test_bootstrap.py` (adapt fixtures to the module's existing config-builder helper; the assertion is the load-bearing part):

```python
def test_bootstrap_threads_project_map_into_dispatcher_when_sandbox_enabled(...) -> None:
    """When sandbox.enabled, the dispatcher gets the project map so it can prep clones."""
    # ... build a V4Config with sandbox.enabled=True, allow_unsandboxed=True
    #     (so preflight failure on the test host downgrades to launcher=None),
    #     and one project "foreman". Construct the context via the same entry
    #     point the other bootstrap tests use.
    # The dispatcher's _sandbox_projects must contain "foreman".
    assert "foreman" in ctx.dispatcher._sandbox_projects  # type: ignore[attr-defined]
```

If `allow_unsandboxed=True` sets `launcher=None` and therefore leaves `sandbox_projects` unused, assert instead that the map is passed regardless (see Step 3 — pass it unconditionally so a later enable doesn't need a bootstrap change). Prefer a test host without userns so the branch is exercised deterministically.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_bootstrap.py -k project_map -v`
Expected: FAIL — `_sandbox_projects` is `None`.

- [ ] **Step 3: Write minimal implementation**

In `bootstrap.py`, pass the project map to the dispatcher. Change the `SubprocessRoleDispatcher(...)` call to add:

```python
    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=foreman_cli or ["foreman"],
        identity=identity,
        log_dir=Path(config.log_dir),
        timeout_seconds=config.role_timeout_seconds,
        inactivity_timeout_seconds=config.role_inactivity_timeout_seconds,
        sandbox=sandbox_launcher,
        sandbox_scratch_root=sandbox_scratch_root,
        # foreman#556: the map lets the dispatcher resolve each project's
        # base clone + repo URL to prep the private per-job clone. Passed
        # unconditionally — harmless when the launcher is None.
        sandbox_projects={pc.name: pc for pc in active_projects},
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_bootstrap.py -k project_map -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/bootstrap.py packages/foreman/tests/v4/test_bootstrap.py
git commit -m "feat: pass project map to dispatcher for sandbox clone prep"
```

---

## Task 6: Clean up per-job scratch on terminal landing

Today the scratch dir lingers forever. Hook cleanup at the single terminal choke point (`_enter_terminal`), threading the scratch root through `StateContext` parallel to `project_configs`.

**Files:**
- Modify: `packages/foreman/src/foreman/v4/state.py` (`StateContext` field + `_enter_terminal`)
- Modify: `packages/foreman/src/foreman/v4/worker_pool.py` (ctor param + `StateContext` construction)
- Modify: `packages/foreman/src/foreman/v4/daemon.py` (pass scratch root into `WorkerPool`)
- Modify: `packages/foreman/src/foreman/v4/merge_coordinator.py` (its `StateContext(...)` at ~line 445 — pass the same field so merge-driven Failed landings also clean up)
- Modify: `packages/foreman/src/foreman/v4/bootstrap.py` (thread `sandbox_scratch_root` into `WorkerPool`)
- Test: `packages/foreman/tests/v4/test_sandbox_clone.py` (cleanup helper) + `packages/foreman/tests/v4/test_state.py` (terminal hook)

**Interfaces:**
- Consumes: `cleanup_ticket_scratch(*, scratch_root, project, issue_number)` (Task 3); `ctx.ticket.project`, `ctx.ticket.issue_number` (existing).
- Produces: `StateContext.sandbox_scratch_root: Path | None = None`; `WorkerPool.__init__(..., sandbox_scratch_root: Path | None = None)`.

- [ ] **Step 1: Write the failing tests**

Add to `packages/foreman/tests/v4/test_sandbox_clone.py`:

```python
def test_cleanup_removes_all_role_job_dirs_for_ticket(tmp_path: Path) -> None:
    from foreman.v4.sandbox_clone import cleanup_ticket_scratch

    root = tmp_path / ".scratch"
    for base in ("planner", "reviewer", "worker"):
        (root / "foreman" / f"{base}-42" / "clone").mkdir(parents=True)
    # a different ticket's dir must survive
    (root / "foreman" / "worker-7" / "clone").mkdir(parents=True)

    removed = cleanup_ticket_scratch(scratch_root=root, project="foreman", issue_number=42)

    assert not (root / "foreman" / "planner-42").exists()
    assert not (root / "foreman" / "worker-42").exists()
    assert (root / "foreman" / "worker-7").exists()  # untouched
    assert len(removed) == 3
```

Add to `packages/foreman/tests/v4/test_state.py` (adapt the `StateContext` / repo fixtures already used there):

```python
def test_enter_terminal_cleans_sandbox_scratch(tmp_path, ...) -> None:
    """Landing on a terminal state removes the ticket's per-job scratch dirs."""
    from foreman.v4.state import StateContext, _enter_terminal
    from foreman.v4.states.terminal import DoneState

    scratch_root = tmp_path / ".scratch"
    (scratch_root / "foreman" / "worker-42" / "clone").mkdir(parents=True)

    ctx = StateContext(
        ticket=...,          # a TicketRecord with project="foreman", issue_number=42
        instance=...,
        repo=...,            # in-memory / fake repo used by the other state tests
        clock=...,
        sandbox_scratch_root=scratch_root,
    )
    _enter_terminal(ctx, DoneState())

    assert not (scratch_root / "foreman" / "worker-42").exists()


def test_enter_terminal_noop_when_no_scratch_root(...) -> None:
    """Default (sandbox off / tests): no scratch root → no cleanup, no error."""
    ctx = StateContext(ticket=..., instance=..., repo=..., clock=...)  # sandbox_scratch_root default None
    _enter_terminal(ctx, DoneState())  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox_clone.py::test_cleanup_removes_all_role_job_dirs_for_ticket packages/foreman/tests/v4/test_state.py -k "terminal_cleans or terminal_noop" -v`
Expected: FAIL — `StateContext.__init__() got an unexpected keyword argument 'sandbox_scratch_root'`.

- [ ] **Step 3: Write minimal implementation**

In `state.py`, add the field to the `StateContext` dataclass (after `project_configs`):

```python
    # foreman#556: root of the per-job sandbox scratch. When set (sandbox
    # enabled), _enter_terminal removes the ticket's per-job clone dirs on
    # terminal landing so scratch does not accumulate unbounded. None in
    # headless tests / sandbox-off runs — cleanup is then a no-op.
    sandbox_scratch_root: Path | None = None
```

Ensure `Path` is imported in `state.py` (add `from pathlib import Path` if absent).

Append to the end of `_enter_terminal` (after `ctx.repo.close_state_instance(...)`):

```python
    # foreman#556: reclaim the ticket's per-job sandbox scratch (hardlinked
    # → cheap, cannot corrupt the shared base repo). Import locally to keep
    # the module-load graph flat and R2-clean.
    if ctx.sandbox_scratch_root is not None:
        from foreman.v4.sandbox_clone import cleanup_ticket_scratch

        cleanup_ticket_scratch(
            scratch_root=ctx.sandbox_scratch_root,
            project=ctx.ticket.project,
            issue_number=ctx.ticket.issue_number,
        )
```

In `worker_pool.py`, add `sandbox_scratch_root: Path | None = None` to `WorkerPool.__init__`, store `self._sandbox_scratch_root = sandbox_scratch_root`, and pass it in `_run_transition`'s `StateContext(...)`:

```python
                project_configs=self._registry.current,
                sandbox_scratch_root=self._sandbox_scratch_root,
```

In `merge_coordinator.py`'s `StateContext(...)` (~line 445), add the same `sandbox_scratch_root=self._sandbox_scratch_root,` line and thread the field through the `MergeCoordinator` ctor identically (default `None`).

In `daemon.py` (`WorkerPool(...)` at ~line 142) and `bootstrap.py`, pass `sandbox_scratch_root=Path(config.sandbox.scratch_root) if config.sandbox.enabled else None` (reuse the `sandbox_scratch_root` local already computed in bootstrap at line 206). Wire the same value into `MergeCoordinator` where it is constructed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox_clone.py packages/foreman/tests/v4/test_state.py -v`
Expected: PASS.

- [ ] **Step 5: Full type + import gate**

Run: `uv run --no-sync mypy packages/foreman/src && just import-lint`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/v4/state.py packages/foreman/src/foreman/v4/worker_pool.py packages/foreman/src/foreman/v4/daemon.py packages/foreman/src/foreman/v4/merge_coordinator.py packages/foreman/src/foreman/v4/bootstrap.py packages/foreman/tests/v4/test_sandbox_clone.py packages/foreman/tests/v4/test_state.py
git commit -m "feat: clean up per-job sandbox scratch on terminal landing"
```

---

## Task 7: Optional writable Claude-session dir (guarded; verify during dogfood)

The Claude CLI may need to WRITE its session dir (currently RO via `extra_ro_binds`). Add a minimal, off-by-default writable overlay so the fix is one config flip away once the dogfood confirms the need. Keep it empirical: no behavior change unless the attribute is set.

**Files:**
- Modify: `packages/foreman/src/foreman/v4/sandbox.py` (`SandboxLauncher` attr + `build_argv` tmpfs)
- Test: `packages/foreman/tests/v4/test_sandbox.py`

**Interfaces:**
- Consumes: `build_argv` (Task 2).
- Produces: `SandboxLauncher.claude_writable_session_dir: str | None = None`; when set, `build_argv` appends `--tmpfs <dir>` AFTER the `extra_ro_binds` so the session subdir is writable (ephemeral) without exposing the daemon's real creds writable.

- [ ] **Step 1: Write the failing test**

Add to `packages/foreman/tests/v4/test_sandbox.py`:

```python
def test_argv_adds_writable_claude_session_tmpfs_when_set() -> None:
    launcher = SandboxLauncher(
        cache_dir="/root/.cache/uv",
        claude_writable_session_dir="/root/.claude/projects",
    )
    argv = launcher.build_argv(role_token="ghs_X", scratch_dir="/scratch", role_cmd=["foreman", "plan"])
    assert _has_pair(argv, "--tmpfs", "/root/.claude/projects")
    # the tmpfs must come AFTER the RO creds bind so it overlays (writable) it
    ro_i = argv.index("/root/.claude")  # from extra_ro_binds
    tmp_i = argv.index("/root/.claude/projects")
    assert tmp_i > ro_i


def test_argv_no_claude_tmpfs_by_default() -> None:
    argv = SandboxLauncher(cache_dir="/c").build_argv(
        role_token="ghs_X", scratch_dir="/scratch", role_cmd=["foreman", "plan"]
    )
    assert not _has_pair(argv, "--tmpfs", "/root/.claude/projects")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox.py -k claude -v`
Expected: FAIL — `SandboxLauncher.__init__() got an unexpected keyword argument 'claude_writable_session_dir'`.

- [ ] **Step 3: Write minimal implementation**

In `sandbox.py`, add the field to `SandboxLauncher` (after `extra_ro_binds`):

```python
    claude_writable_session_dir: str | None = None
    """Optional in-box path made WRITABLE via a ``--tmpfs`` overlay AFTER
    the read-only Claude creds bind, so the Claude CLI can write its
    session/session-lock files without the daemon's real creds dir being
    writable. Off by default — verify the CLI actually needs this during
    the #556 dogfood before enabling; the tmpfs is ephemeral (dropped when
    the box exits), so no session state leaks between jobs."""
```

In `build_argv`, immediately after the `for extra in self.extra_ro_binds:` loop, add:

```python
        # foreman#556 (dogfood-gated): overlay a writable tmpfs on the
        # Claude session dir if configured, so the CLI can write its
        # session files. Must follow the RO creds bind to take effect.
        if self.claude_writable_session_dir is not None:
            argv += ["--tmpfs", self.claude_writable_session_dir]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox.py -k claude -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/sandbox.py packages/foreman/tests/v4/test_sandbox.py
git commit -m "feat: optional writable claude-session tmpfs in sandbox (off by default)"
```

---

## Task 8: Hermetic real-bwrap enabled-path test + Task-7 regression clarification

Add the keystone automated check the design's "Hermetic" bullet names: a daemon-prepped co-located clone + a sandboxed command that loads config, commits in the private clone, and confirms the shared base is invisible + foreman is read-only. It self-skips without userns. Also reconcile the Task-7 regression test.

**Finding (Task-7 reconcile):** the existing `test_regression_2026_07_18_import_foreman_and_daemon_install_write_fail` is **correct as written — do NOT change its assertions.** Its assertion (2) creates a *fresh isolated* venv (`python3 -m venv`, no `--system-site-packages`) under `/scratch` and asserts `import foreman` fails there — a true, real guarantee (an isolated job venv cannot reach the daemon's foreman). The spec's note only warns that this must not be mis-read as "foreman is never importable in the box" — the *role entry point* runs on the `/usr`-bound system Python and CAN import foreman; isolation comes from foreman being **read-only** (assertion (1)), not from import failing. Fix = a docstring clarification + a companion assertion that encodes the REAL guarantee, not an assertion change.

**Files:**
- Modify: `packages/foreman/tests/v4/test_sandbox_integration.py`

**Interfaces:**
- Consumes: `prepare_sandbox_clone` (Task 3); `SandboxLauncher.build_argv(..., repo_bind, ro_file_binds)` (Task 2). Reuses the module's `_userns_available()` skip guard.

- [ ] **Step 1: Write the failing test + clarify the regression docstring**

In `test_sandbox_integration.py`, extend the existing regression test's docstring with a clarifying paragraph (no assertion change):

```
    Clarification (foreman#556): assertion (2) is specifically the FRESH
    ISOLATED venv context (``python3 -m venv`` with no system site-packages).
    That import failing is a real guarantee. It does NOT contradict the role
    entry point — which runs on the /usr-bound system Python and CAN import
    foreman. The isolation the sandbox actually provides is that foreman is
    READ-ONLY (assertion (1)): a job's ``uv sync`` cannot rewrite the daemon's
    install (the 2026-07-18 foreman.prompts corruption is structurally dead).
```

Add a new hermetic test that drives the enabled path end-to-end:

```python
def test_enabled_path_role_operates_in_private_clone(tmp_path: Path) -> None:
    """Enabled path (real bwrap): prep a co-located private clone, bind it at the
    role's local_clone_path, and confirm a sandboxed command loads config,
    commits in the clone, sees the base repo as INVISIBLE, and cannot write foreman."""
    from foreman.v4.sandbox_clone import prepare_sandbox_clone

    # --- a real base git repo (stands in for /foreman/repos/<project>) ---
    base = tmp_path / "base"
    base.mkdir()
    subprocess.run(["git", "init", "-q", str(base)], check=True)
    subprocess.run(["git", "-C", str(base), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(base), "config", "user.name", "t"], check=True)
    (base / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(base), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(base), "commit", "-qm", "init"], check=True)

    # --- daemon preps the co-located private clone (same tmp fs → hardlinks) ---
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    job = tmp_path / ".scratch" / "proj" / "worker-1"
    clone_dir = job / "clone"
    wt_dir = job / "wt"
    wt_dir.mkdir(parents=True)
    prepare_sandbox_clone(
        base_clone_path=base,
        dest_clone_path=clone_dir,
        repo_url="https://github.com/o/n.git",  # never contacted in this test
        role_token="ghs_TESTTOKEN",
    )
    # config file the box will load
    config_file = tmp_path / "config.toml"
    config_file.write_text("log_dir = '/tmp'\n", encoding="utf-8")

    box_repo = "/foreman/repos/proj"
    launcher = SandboxLauncher(cache_dir=str(cache_dir))
    script = (
        # config file is readable at its RO mount
        f"cat {config_file} >/dev/null && "
        # operate in the private clone bound at the role's local_clone_path
        f"cd {box_repo} && git config user.email t@t.t && git config user.name t && "
        "echo work > f.txt && git add -A && git commit -qm work && git log --oneline | head -1 && "
        # the shared BASE repo path is NOT mounted → invisible
        f"(cat {base}/README.md 2>/dev/null && echo BASE_VISIBLE || echo BASE_INVISIBLE) && "
        # /usr (where the daemon foreman lives) is read-only
        "(echo x > /usr/lib/foreman_marker.py 2>/dev/null && echo FOREMAN_WRITABLE || echo FOREMAN_RO)"
    )
    argv = launcher.build_argv(
        role_token="ghs_TESTTOKEN",
        scratch_dir=str(wt_dir),
        role_cmd=["/bin/sh", "-c", script],
        repo_bind=(str(clone_dir), box_repo),
        ro_file_binds=((str(config_file), str(config_file)),),
    )
    r = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    assert "work" in r.stdout  # the commit landed in the private clone
    assert "BASE_INVISIBLE" in r.stdout
    assert "BASE_VISIBLE" not in r.stdout
    assert "FOREMAN_RO" in r.stdout
    assert "FOREMAN_WRITABLE" not in r.stdout
```

- [ ] **Step 2: Run test to verify it fails (or skips off-userns)**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox_integration.py -v`
Expected on a **Windows dev host / no-userns CI:** the whole module SKIPS (`pytestmark` skip) — that is the correct, expected outcome; the automated units in Tasks 1–7 carry the cross-platform coverage. Expected on a **userns Linux host:** the new test FAILS first only if the implementation is incomplete, else PASSES.

- [ ] **Step 3: (implementation already done in Tasks 2–3)** — no new source; this task is test-only.

- [ ] **Step 4: Run the full sandbox suite**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox.py packages/foreman/tests/v4/test_sandbox_dispatch.py packages/foreman/tests/v4/test_sandbox_clone.py packages/foreman/tests/v4/test_sandbox_integration.py -v`
Expected: PASS (integration module skips off-userns).

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/tests/v4/test_sandbox_integration.py
git commit -m "test: hermetic enabled-path sandbox test + clarify 2026-07-18 regression lock"
```

---

## Final gate

- [ ] **Run the full quality gate**

Run: `just check`
Expected: `lock-check lint format-check typecheck import-lint test` all pass; coverage ≥ 80%; patch coverage ≥ 80%.

- [ ] **Dogfood (manual keystone — CANNOT be automated; #555 blocks userns in CI)**

On a userns host / the production container, set `config.sandbox.enabled = true` and run a foreman-**self** ticket concurrently with an agent_core ticket. Confirm: both complete; no `foreman.prompts`-style corruption; each role committed/pushed from its own private clone; the base repo stayed untouched; the per-job scratch dirs were removed on terminal landing. During this run, verify empirically whether the Claude CLI needs `claude_writable_session_dir` (Task 7) and enable it if so. **Flipping `sandbox.enabled = true` in production config is an operator action, out of scope here — surface to Jeff after the dogfood passes.**

---

## Self-Review

**1. Spec coverage**

| Spec item | Task |
|---|---|
| Change 1 — scratch co-location default on repos volume | Task 1 |
| Change 2 — daemon preps private clone; role uses it (no worktree-add of shared base) | Tasks 3 (helper) + 4 (dispatcher) + 2 (RW bind at `local_clone_path`) + 5 (project map) — see integration note below |
| Change 3 — bind the config file (+ projects file) RO; set env | Task 2 (argv) + Task 4 (dispatcher threads both) |
| Change 4 — Claude creds writability (guarded, dogfood-verify) | Task 7 |
| Change 5 — scratch cleanup on terminal landing | Task 6 |
| Change 6 — reconcile Task-7 "import fails" regression | Task 8 (finding: test is correct; docstring + companion assertion) |
| Testing: unit (config bind argv, clone-prep cmd, cleanup) | Tasks 2, 3, 6 |
| Testing: hermetic real-bwrap self-skip | Task 8 |
| Testing: dogfood keystone (manual) | Final gate |

**Integration decision (Change 2, daemon-clone-prep ↔ role-worktree):** the daemon binds the private clone **read-write at the exact in-box path `ProjectConfig.local_clone_path` already names** (e.g. `/foreman/repos/<project>`). The role's `WorktreeManager.create` / `create_impl` / `attach` / `attach_impl` therefore run **unchanged** — `git worktree add` links off this PRIVATE `.git`, and every existing `Path(project.local_clone_path)` reference (worktree ops, `load_project_instructions`, the Worker's rebase probe) resolves to the private clone. The shared base repo is never mounted → invisible; isolation holds because the clone is per-job. This is the "place the clone where the role already looks" option the design offered, and it satisfies every success criterion with **zero role/`WorktreeManager` edits** (see Deviation note). Exact integration points: `SubprocessRoleDispatcher.dispatch` (Task 4) → `prepare_sandbox_clone` (Task 3) → `SandboxLauncher.build_argv(repo_bind=..., ro_file_binds=...)` (Task 2).

**Deviation from spec prose (flagged for reviewer/Jeff):** the design's Change-2 text says the role's `git worktree add` is "bypassed when sandboxed" via a `WorktreeManager` sandbox branch. This plan does **not** bypass it — it keeps `git worktree add`, but off a *private* clone bound at `local_clone_path`, which resolves the design's actual concern (never share the base `.git`) with far less surface and risk. Every stated **success criterion** is met. If a reviewer requires literal fidelity to "bypass worktree add" (a standalone clone AT the worktree path + a `WorktreeManager` sandbox branch + `clone_path→wt_path` switches across all four roles), that is a larger, higher-risk variant — raise before execution.

**2. Placeholder scan:** No `TBD`/`TODO`/"handle edge cases"/"similar to Task N". Every code step carries complete code. Two test steps (Task 5 bootstrap, Task 6 `test_state.py`) show `...` for **fixture wiring that must match each test module's existing helpers** — the load-bearing assertion in each is concrete; flagged inline so the implementer copies the module's established fixture shape rather than inventing one.

**3. Type consistency:** `prepare_sandbox_clone` keyword args (`base_clone_path`, `dest_clone_path`, `repo_url`, `role_token`) are identical at definition (Task 3), dispatcher call site (Task 4), and the `fake_prep` test double (Task 4). `build_argv`'s `repo_bind: tuple[str, str] | None` / `ro_file_binds: tuple[tuple[str, str], ...]` are consistent across Task 2 (def), Task 4 (call), Task 8 (call). `cleanup_ticket_scratch` / `sandbox_clone_argv` / `tokenized_origin_url` signatures match across Tasks 3 and 6. `StateContext.sandbox_scratch_root: Path | None` matches `WorkerPool.__init__` param and the `_enter_terminal` read.
