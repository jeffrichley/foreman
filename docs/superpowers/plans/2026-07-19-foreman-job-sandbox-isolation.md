# Foreman job-execution sandbox isolation (bubblewrap) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wrap each foreman role subprocess in a bubblewrap sandbox so a job can only read a shared RO uv cache and write its own scratch worktree — structurally unable to corrupt the daemon's `foreman` install, a sibling job, or read another role's secret.

**Architecture:** The entire change lands at ONE seam — `foreman.v4.subprocess_dispatcher.SubprocessRoleDispatcher.dispatch()`. Today it builds `cmd = ['foreman', '<role>', ...]` and `subprocess.Popen`s it on the daemon's shared system Python. Behind a config flag, a new pure `SandboxLauncher` wraps `cmd` in a `bwrap …` argv; the daemon sets `FOREMAN_WORKTREES_ROOT=/scratch` inside the box so the role's existing `WorktreeManager` roots its worktree under the RW scratch mount (no role-logic change). Everything downstream of the seam — the reader-thread stdout capture and `FOREMAN_OUTCOME:` parsing — is unchanged.

**Tech Stack:** Python 3.12, pydantic v2 (config models), `subprocess` + threads (existing dispatcher), bubblewrap 0.11.x (`bwrap`), uv (per-job venv from shared cache), pytest + pytest-xdist, mypy strict, ruff.

## Global Constraints

_Every task's requirements implicitly include this section. Values copied verbatim from the spec + repo canon._

- **Python:** `requires-python = ">=3.12"`.
- **Docstrings:** ruff google-style docstring convention on every public symbol.
- **Types:** mypy `strict = true` — run unscoped: `uv run --no-sync mypy packages/foreman/src`.
- **Gate:** `just check` = `lock-check lint format-check typecheck import-lint test`. Coverage floor 80; diff-cover 80.
- **Import boundaries:** import-linter R1 (production code never imports `tests`) + R2 (`foreman.v4` never imports the deleted v3-substrate modules). New module `foreman.v4.sandbox` lives under `foreman.v4`; it must NOT import any v3-substrate name.
- **Commits:** conventional-commits, lowercase subject (`feat:`, `fix:`, `test:`, `docs:`, `chore:`). **NO `Co-Authored-By` trailer** (repo convention — verified against recent history).
- **Venv:** worktree venv is not committed. Run `uv sync` **once** if `.venv` is missing, then use `uv run --no-sync …` for every command below (the worktree is nested in the main repo; `--no-sync` alone would test MAIN src).
- **The mount plan is canon (SandboxLauncher must emit exactly this):**
  - namespaces: `--unshare-user --unshare-pid --unshare-ipc --unshare-uts`
  - network: `--share-net` (open egress — agents need PyPI/npm/docs)
  - lifecycle: `--die-with-parent`
  - RO: `/usr /bin /lib /lib64` (+ `/sbin`), `/etc/resolv.conf`, CA certs (`/etc/ssl/certs`), git config (`/etc/gitconfig`)
  - RW: `cache_dir` → `/cache` (shared, content-addressed uv cache — writable so jobs warm it; uv's cache is concurrency-safe)
  - RW: `scratch_dir` → `/scratch` (the job's worktree + `.venv` — the ONLY writable bind)
  - `--tmpfs /tmp`
  - env into box: `GH_TOKEN=<this job's scoped role token>` and nothing else secret; the job's Python runs from its OWN venv (do NOT inherit system site-packages / `PYTHONPATH` so `import foreman` fails inside the box for job code)
  - **NEVER bind:** daemon foreman source (`/app/source`), role PEM volumes (`/run/secrets/{planner,reviewer,fixer,worker,orchestrator}_pem`, i.e. the whole `/run/secrets`), the credential vault + `/root/.foreman`, sibling scratch dirs
- **Cache write policy (resolved): RW-shared.** The cache mounts **read-write** at `/cache`. This is required for the cost goal: with a read-only cache, a brand-new package — and worse, *every dependency of a brand-new project* (including multi-GB torch) — would re-download on **every** job forever, since nothing ever writes it back. RW lets the **first** job that touches a package/project warm the shared cache; every job after (same or another project) reuses it via hardlink. Safe because (a) uv's cache is content-addressed + lock-protected and supports concurrent writers by design (the standard CI cache-sharing pattern), and (b) the cache is a wheel store, NOT an installed environment — writing wheels to it cannot corrupt the daemon's `foreman` install (which lives in system site-packages and is never mounted into the box). The corruption incident was an *editable install into site-packages*, an entirely different mechanism.
- **Worktree location (resolved):** **scratch-rooted.** The role subprocess creates its own worktree in-process (`WorktreeManager`, rooted at `os.environ["FOREMAN_WORKTREES_ROOT"]`, default `~/.foreman/worktrees`). The dispatcher sets `FOREMAN_WORKTREES_ROOT=/scratch` inside the box and bind-mounts a per-job host scratch dir → `/scratch` RW. Role logic is unchanged. (Pre-creating the worktree outside the box + mounting it in was rejected: it would require ripping worktree creation out of every role — a change to role logic, which the spec lists as a non-goal.)

---

## File Structure

| File | Create/Modify | Responsibility |
| --- | --- | --- |
| `packages/foreman/src/foreman/v4/config.py` | Modify (~line 366, add `SandboxConfig` + field on `V4Config`) | Declare the `[sandbox]` config block: `enabled`, `allow_unsandboxed`, `cache_dir`, `scratch_root`, `bwrap_path`. |
| `packages/foreman/src/foreman/v4/sandbox.py` | Create | The core. `SandboxLauncher` (pure argv builder owning the mount plan + never-bind list), `SandboxUnavailableError`, `preflight()` self-test, `DAEMON_NEVER_BIND`. |
| `packages/foreman/src/foreman/v4/subprocess_dispatcher.py` | Modify (`__init__` ~254, `dispatch` ~301-345, `_write_banner` ~148) | Wrap `cmd` via `SandboxLauncher` behind the flag; create the per-job scratch dir; redact `GH_TOKEN` in the log banner. |
| `packages/foreman/src/foreman/v4/bootstrap.py` | Modify (~line 176) | Build `SandboxLauncher` from config, run `preflight()` fail-closed at startup, thread launcher + scratch_root into the dispatcher. |
| `Dockerfile` | Modify (line 38 apt block; after line 103) | Install `bubblewrap`; assert foreman is a non-editable `--system` install (immutable-foreman guard). |
| `packages/foreman/tests/v4/test_config.py` | Modify | Config-parse tests for the `[sandbox]` block. |
| `packages/foreman/tests/v4/test_sandbox.py` | Create | Pure unit tests for `SandboxLauncher.build_argv` (mount plan + never-bind + token-in-env) and `preflight()` branches. Runs on all platforms. |
| `packages/foreman/tests/v4/test_sandbox_dispatch.py` | Create | Dispatcher-wiring tests (flag off = unchanged argv; flag on = wrapped argv; scratch dir created; banner redaction). Pure, runs everywhere. |
| `packages/foreman/tests/v4/test_sandbox_integration.py` | Create | Hermetic real-`bwrap` integration + permanent 2026-07-18 regression test. Self-skips when unprivileged userns is unavailable. |

---

## Task 1: `[sandbox]` config block

**Files:**
- Modify: `packages/foreman/src/foreman/v4/config.py` (add `SandboxConfig` before `class V4Config`; add `sandbox` field on `V4Config` after `backup`)
- Test: `packages/foreman/tests/v4/test_config.py`

**Interfaces:**
- Consumes: nothing (leaf).
- Produces:
  - `class SandboxConfig(BaseModel)` with fields: `enabled: bool = False`, `allow_unsandboxed: bool = False`, `cache_dir: str = "/root/.cache/uv"`, `scratch_root: str = "/foreman/scratch"`, `bwrap_path: str = "bwrap"`. `model_config = ConfigDict(extra="forbid")`.
  - `V4Config.sandbox: SandboxConfig = Field(default_factory=SandboxConfig)` — optional; configs without a `[sandbox]` block default to disabled.

- [ ] **Step 1: Write the failing test**

Add to `packages/foreman/tests/v4/test_config.py` (the file already imports `load_config`, `V4Config`, and defines `_APPS_TOML`; add `SandboxConfig` to the `from foreman.v4.config import (...)` block):

```python
def test_sandbox_defaults_off_when_block_absent(tmp_path: Path) -> None:
    toml = (
        'log_dir = "/tmp/logs"\n'
        "[storage]\n"
        'dsn = "postgresql://x"\n'
        + _APPS_TOML
    )
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(toml, encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.sandbox.enabled is False
    assert cfg.sandbox.allow_unsandboxed is False
    assert cfg.sandbox.cache_dir == "/root/.cache/uv"
    assert cfg.sandbox.scratch_root == "/foreman/scratch"
    assert cfg.sandbox.bwrap_path == "bwrap"


def test_sandbox_block_parses_and_rejects_unknown_key(tmp_path: Path) -> None:
    toml = (
        'log_dir = "/tmp/logs"\n'
        "[storage]\n"
        'dsn = "postgresql://x"\n'
        "[sandbox]\n"
        "enabled = true\n"
        "allow_unsandboxed = true\n"
        'cache_dir = "/mnt/uv"\n'
        'scratch_root = "/mnt/scratch"\n'
        + _APPS_TOML
    )
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(toml, encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.sandbox.enabled is True
    assert cfg.sandbox.allow_unsandboxed is True
    assert cfg.sandbox.cache_dir == "/mnt/uv"

    bad = cfg_path.with_name("bad.toml")
    bad.write_text(toml + "\n[sandbox]\nbogus = 1\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        # duplicate-table TOML is a parse error; use a fresh unknown-key case
        pass
    bad2 = cfg_path.with_name("bad2.toml")
    bad2.write_text(
        'log_dir = "/tmp/logs"\n'
        "[storage]\n"
        'dsn = "postgresql://x"\n'
        "[sandbox]\n"
        "bogus = 1\n"
        + _APPS_TOML,
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_config(bad2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_config.py::test_sandbox_defaults_off_when_block_absent -v`
Expected: FAIL — `AttributeError: 'V4Config' object has no attribute 'sandbox'` (or ImportError on `SandboxConfig`).

- [ ] **Step 3: Write minimal implementation**

In `packages/foreman/src/foreman/v4/config.py`, add this class immediately before `class V4Config(BaseModel):`:

```python
class SandboxConfig(BaseModel):
    """Bubblewrap job-isolation settings (foreman#job-sandbox-isolation).

    Each role subprocess is wrapped in a ``bwrap`` box so it can only read
    a shared uv cache and write its own scratch worktree. The
    block is optional; a config without ``[sandbox]`` defaults to
    ``enabled = False`` so the daemon behaves exactly as before until the
    sandbox is deliberately turned on.

    Attributes:
        enabled: Master switch. When ``False`` the dispatcher runs role
            subprocesses unwrapped (pre-sandbox behavior).
        allow_unsandboxed: Local-dev escape hatch. When ``True`` the
            startup preflight failure is downgraded from fail-closed to a
            loud per-dispatch warning. Must be set deliberately.
        cache_dir: Host path of the shared uv cache, bind-mounted
            read-write to ``/cache`` inside the box (jobs warm the shared
            cache; uv's content-addressed cache is concurrency-safe).
        scratch_root: Host directory under which per-job scratch dirs are
            created and bind-mounted read-write to ``/scratch``.
        bwrap_path: Path to the ``bwrap`` binary (overridable for tests).
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    allow_unsandboxed: bool = False
    cache_dir: str = "/root/.cache/uv"
    scratch_root: str = "/foreman/scratch"
    bwrap_path: str = "bwrap"
```

Then add the field to `V4Config`, immediately after the `backup: BackupConfig = ...` field (before the closing docstring comment block):

```python
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    """foreman#job-sandbox-isolation: bubblewrap isolation for role
    subprocesses. Optional with a default — existing operator configs
    without a ``[sandbox]`` block load and default to ``enabled=False``."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_config.py::test_sandbox_defaults_off_when_block_absent packages/foreman/tests/v4/test_config.py::test_sandbox_block_parses_and_rejects_unknown_key -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/config.py packages/foreman/tests/v4/test_config.py
git commit -m "feat: add [sandbox] config block (default disabled)"
```

---

## Task 2: `SandboxLauncher` — pure bwrap argv builder (the core)

**Files:**
- Create: `packages/foreman/src/foreman/v4/sandbox.py`
- Test: `packages/foreman/tests/v4/test_sandbox.py`

**Interfaces:**
- Consumes: `SandboxConfig` values (`cache_dir`, `scratch_root`, `bwrap_path`) — passed as plain args, no import of config needed.
- Produces:
  - `DAEMON_NEVER_BIND: tuple[str, ...]` = `("/run/secrets", "/root/.foreman", "/app/source")`.
  - `class SandboxLauncher` (frozen dataclass) with attrs `cache_dir: str`, `bwrap_path: str = "bwrap"`, `cache_mount: str = "/cache"`, `scratch_mount: str = "/scratch"`, `extra_ro_binds: tuple[str, ...] = ("/root/.claude", "/root/.claude-container")`.
  - `SandboxLauncher.build_argv(self, *, role_token: str, scratch_dir: str, role_cmd: list[str], passthrough: Mapping[str, str] | None = None) -> list[str]` — returns the bwrap argv wrapping `role_cmd`.
  - `SANDBOX_STD_PATH: str` (the box's `PATH`).

- [ ] **Step 1: Write the failing test**

Create `packages/foreman/tests/v4/test_sandbox.py`:

```python
"""Pure unit tests for SandboxLauncher.build_argv — runs on every platform."""

from __future__ import annotations

from foreman.v4.sandbox import DAEMON_NEVER_BIND, SandboxLauncher


def _argv() -> list[str]:
    launcher = SandboxLauncher(cache_dir="/root/.cache/uv")
    return launcher.build_argv(
        role_token="ghs_SECRET",
        scratch_dir="/foreman/scratch/foreman/worker-537",
        role_cmd=["foreman", "implement", "--project", "foreman", "--issue-number", "537"],
    )


def test_argv_wraps_role_cmd_after_double_dash() -> None:
    argv = _argv()
    assert argv[0] == "bwrap"
    dd = argv.index("--")
    assert argv[dd + 1 :] == [
        "foreman", "implement", "--project", "foreman", "--issue-number", "537",
    ]


def test_argv_has_the_canon_namespace_and_lifecycle_flags() -> None:
    argv = _argv()
    for flag in (
        "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--share-net", "--die-with-parent",
    ):
        assert flag in argv, flag
    # tmpfs /tmp
    assert _has_pair(argv, "--tmpfs", "/tmp")


def test_argv_mounts_cache_rw_and_scratch_rw() -> None:
    argv = _argv()
    # RW cache: --bind <cache_dir> /cache (writable so jobs warm the shared cache)
    assert _has_triple(argv, "--bind", "/root/.cache/uv", "/cache")
    # cache must NOT be mounted read-only (would force re-download of new deps/projects)
    assert not _has_triple(argv, "--ro-bind", "/root/.cache/uv", "/cache")
    # RW scratch: --bind <scratch_dir> /scratch (writable bind, NOT ro-bind)
    assert _has_triple(argv, "--bind", "/foreman/scratch/foreman/worker-537", "/scratch")
    # scratch must never be mounted read-only
    assert not _has_triple(argv, "--ro-bind", "/foreman/scratch/foreman/worker-537", "/scratch")


def test_argv_mounts_base_ro_roots() -> None:
    argv = _argv()
    assert _has_triple(argv, "--ro-bind", "/usr", "/usr")
    assert _has_triple(argv, "--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf")
    assert _has_triple(argv, "--ro-bind", "/etc/ssl/certs", "/etc/ssl/certs")


def test_argv_sets_scoped_token_and_scratch_rooted_env() -> None:
    argv = _argv()
    assert _has_triple(argv, "--setenv", "GH_TOKEN", "ghs_SECRET")
    assert _has_triple(argv, "--setenv", "FOREMAN_WORKTREES_ROOT", "/scratch")
    assert _has_triple(argv, "--setenv", "UV_CACHE_DIR", "/cache")
    # the box starts from a cleared env so no daemon secret leaks in
    assert "--clearenv" in argv


def test_argv_never_binds_daemon_secrets() -> None:
    argv = _argv()
    joined = " ".join(argv)
    for forbidden in DAEMON_NEVER_BIND:
        assert forbidden not in joined, forbidden
    # explicit PEM paths
    for pem in ("planner_pem", "reviewer_pem", "fixer_pem", "worker_pem", "orchestrator_pem"):
        assert pem not in joined, pem


def test_argv_passthrough_forwards_non_secret_env_only() -> None:
    launcher = SandboxLauncher(cache_dir="/root/.cache/uv")
    argv = launcher.build_argv(
        role_token="ghs_X",
        scratch_dir="/foreman/scratch/foreman/planner-42",
        role_cmd=["foreman", "plan"],
        passthrough={"FOREMAN_STATE_INSTANCE_ID": "9", "CLAUDE_CONFIG_DIR": "/root/.claude-container"},
    )
    assert _has_triple(argv, "--setenv", "FOREMAN_STATE_INSTANCE_ID", "9")
    assert _has_triple(argv, "--setenv", "CLAUDE_CONFIG_DIR", "/root/.claude-container")


def _has_pair(argv: list[str], a: str, b: str) -> bool:
    return any(argv[i] == a and argv[i + 1] == b for i in range(len(argv) - 1))


def _has_triple(argv: list[str], a: str, b: str, c: str) -> bool:
    return any(
        argv[i] == a and argv[i + 1] == b and argv[i + 2] == c
        for i in range(len(argv) - 2)
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'foreman.v4.sandbox'`.

- [ ] **Step 3: Write minimal implementation**

Create `packages/foreman/src/foreman/v4/sandbox.py`:

```python
"""Bubblewrap job-isolation launcher (foreman#job-sandbox-isolation).

The daemon spawns each role (Planner / Reviewer / Fixer / Worker) as a
``foreman <subcmd> ...`` subprocess. Historically that ran on the
daemon's shared system Python, so a job could reach out and corrupt the
daemon's own ``foreman`` install (the 2026-07-18 ``foreman.prompts``
incident), delete a sibling job's worktree, or read another role's
secret.

:class:`SandboxLauncher` wraps the role command in a ``bwrap`` invocation
that gives the job a positive, by-construction boundary: it can read a
shared uv cache (``/cache``) and write only its own scratch
worktree (``/scratch``); the daemon's foreman source, the role PEM keys,
the credential vault, and sibling scratch dirs are simply not mounted.

:func:`preflight` runs a minimal sandbox at daemon startup so a host
without unprivileged user namespaces fails closed with an actionable
operator message instead of silently running jobs unsandboxed.

This module is pure orchestration plumbing under ``foreman.v4`` — it must
never import a v3-substrate module (import-linter R2).
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field

# Paths that must NEVER be bind-mounted into a job box. Asserted absent
# from the argv by the unit tests and by the hermetic integration test.
#   /run/secrets  — the role PEM keys (planner/reviewer/fixer/worker/
#                   orchestrator _pem) that mint installation tokens
#   /root/.foreman — the credential vault, projects.toml, keys/, backups
#   /app/source    — the daemon's own foreman source checkout
DAEMON_NEVER_BIND: tuple[str, ...] = ("/run/secrets", "/root/.foreman", "/app/source")

# The box's PATH. A fixed, minimal system PATH — the job's own venv under
# /scratch is activated by the role via ``uv run``, not by prepending to
# PATH here.
SANDBOX_STD_PATH: str = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass(frozen=True)
class SandboxLauncher:
    """Pure builder of the ``bwrap`` argv that wraps a role command.

    A pure function of its inputs to an exact argv: no side effects, no
    filesystem access, trivially unit-testable. It owns the mount plan
    and the never-bind list. The dispatcher is responsible for creating
    the per-job ``scratch_dir`` on disk before calling
    :meth:`build_argv`.

    Attributes:
        cache_dir: Host path of the shared uv cache (mounted RO at
            ``cache_mount``).
        bwrap_path: The ``bwrap`` binary (overridable for tests).
        cache_mount: In-box mountpoint for the shared cache.
        scratch_mount: In-box mountpoint for the job's writable scratch.
        extra_ro_binds: Additional host paths to bind read-only if they
            exist — the Claude CLI config + session dirs the role needs
            to make its LLM call. These are operational config, not the
            crown-jewel secrets in :data:`DAEMON_NEVER_BIND`.
    """

    cache_dir: str
    bwrap_path: str = "bwrap"
    cache_mount: str = "/cache"
    scratch_mount: str = "/scratch"
    extra_ro_binds: tuple[str, ...] = field(
        default_factory=lambda: ("/root/.claude", "/root/.claude-container")
    )

    def build_argv(
        self,
        *,
        role_token: str,
        scratch_dir: str,
        role_cmd: list[str],
        passthrough: Mapping[str, str] | None = None,
    ) -> list[str]:
        """Return the ``bwrap`` argv wrapping ``role_cmd``.

        Args:
            role_token: The job's short-lived scoped GitHub role token;
                set as ``GH_TOKEN`` inside the box and nothing else
                secret.
            scratch_dir: Host path of this job's scratch dir, bind-mounted
                read-write to ``scratch_mount``. The role's
                ``WorktreeManager`` roots its worktree here via
                ``FOREMAN_WORKTREES_ROOT=/scratch``.
            role_cmd: The unwrapped role command
                (``["foreman", "implement", ...]``) to run inside the box.
            passthrough: Extra non-secret env vars to forward
                (state-instance id, session-resume ids, Claude config
                dir). The dispatcher curates this allowlist.

        Returns:
            The full argv: ``bwrap`` + namespace/mount/env flags + ``--``
            + ``role_cmd``.
        """
        # The box starts from a CLEARED environment (positive defense):
        # nothing from the daemon's env leaks in. Every var the job needs
        # is re-added explicitly below.
        setenv: dict[str, str] = {
            "PATH": SANDBOX_STD_PATH,
            "HOME": "/root",
            "PYTHONUNBUFFERED": "1",
            # Keep the job's Python off the daemon's user site-packages so
            # `import foreman` cannot resolve there by accident.
            "PYTHONNOUSERSITE": "1",
            # Scratch-rooted worktree: the role's WorktreeManager reads
            # this env var to place its per-ticket worktree under the
            # writable scratch mount (the only writable path in the box).
            "FOREMAN_WORKTREES_ROOT": self.scratch_mount,
            # uv reads the shared content-addressed cache from here (RO).
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
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/sandbox.py packages/foreman/tests/v4/test_sandbox.py
git commit -m "feat: add SandboxLauncher pure bwrap argv builder"
```

---

## Task 3: Preflight self-test + fail-closed

**Files:**
- Modify: `packages/foreman/src/foreman/v4/sandbox.py` (add `SandboxUnavailableError` + `preflight`)
- Test: `packages/foreman/tests/v4/test_sandbox.py` (append)

**Interfaces:**
- Consumes: `SandboxLauncher` module (same file).
- Produces:
  - `class SandboxUnavailableError(RuntimeError)`.
  - `def preflight(*, bwrap_path: str = "bwrap", runner: Callable[[list[str]], int] | None = None) -> None` — runs a minimal `bwrap … /bin/true`; raises `SandboxUnavailableError` with an actionable message on non-zero exit or missing binary. `runner` is a seam: `(argv) -> returncode`, defaults to a real `subprocess.run`.

- [ ] **Step 1: Write the failing test**

Append to `packages/foreman/tests/v4/test_sandbox.py`:

```python
import pytest

from foreman.v4.sandbox import SandboxUnavailableError, preflight


def test_preflight_passes_when_runner_returns_zero() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    preflight(bwrap_path="bwrap", runner=runner)  # no raise
    assert calls, "preflight must actually invoke bwrap"
    assert calls[0][0] == "bwrap"
    assert "--unshare-user" in calls[0]


def test_preflight_fails_closed_when_runner_returns_nonzero() -> None:
    def runner(argv: list[str]) -> int:
        return 1

    with pytest.raises(SandboxUnavailableError) as exc:
        preflight(bwrap_path="bwrap", runner=runner)
    assert "user namespace" in str(exc.value).lower() or "bwrap" in str(exc.value).lower()


def test_preflight_fails_closed_when_bwrap_missing() -> None:
    def runner(argv: list[str]) -> int:
        raise FileNotFoundError("bwrap")

    with pytest.raises(SandboxUnavailableError):
        preflight(bwrap_path="bwrap", runner=runner)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox.py::test_preflight_passes_when_runner_returns_zero -v`
Expected: FAIL — `ImportError: cannot import name 'preflight'`.

- [ ] **Step 3: Write minimal implementation**

Add to the imports at the top of `packages/foreman/src/foreman/v4/sandbox.py`:

```python
from collections.abc import Callable, Mapping
```

(replace the existing `from collections.abc import Mapping` line). Then append to the module:

```python
class SandboxUnavailableError(RuntimeError):
    """The bubblewrap sandbox cannot be created on this host.

    Raised by :func:`preflight` when a minimal ``bwrap`` boot fails —
    typically because the host lacks unprivileged user namespaces. The
    daemon fails closed on this rather than silently running role
    subprocesses unsandboxed.
    """


def _default_runner(argv: list[str]) -> int:
    """Run ``argv`` discarding output; return its exit code."""
    return subprocess.run(  # noqa: S603 - argv is built here, not user input
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode


def preflight(
    *,
    bwrap_path: str = "bwrap",
    runner: Callable[[list[str]], int] | None = None,
) -> None:
    """Boot a minimal sandbox to prove the host supports it; else fail closed.

    Runs ``bwrap`` with the same namespaces the real jobs use, a RO
    ``/usr`` bind, a ``/tmp`` tmpfs, and ``/bin/true`` as the payload. A
    zero exit proves unprivileged user namespaces + the requested
    namespaces work under the container's default (non-privileged)
    security profile — the make-or-break capability the spike validated.

    Args:
        bwrap_path: The ``bwrap`` binary to probe.
        runner: Test seam ``(argv) -> returncode``. Defaults to a real
            ``subprocess.run`` that discards output.

    Raises:
        SandboxUnavailableError: on non-zero exit or a missing ``bwrap``
            binary, with an actionable operator message.
    """
    run = runner if runner is not None else _default_runner
    argv = [
        bwrap_path,
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
        "--ro-bind",
        "/usr",
        "/usr",
        "--tmpfs",
        "/tmp",
        "--",
        "/bin/true",
    ]
    try:
        rc = run(argv)
    except FileNotFoundError as exc:
        raise SandboxUnavailableError(
            f"bwrap binary not found at {bwrap_path!r}. Install bubblewrap "
            f"in the daemon image (apt-get install -y bubblewrap) or set "
            f"[sandbox].bwrap_path. Refusing to run jobs unsandboxed."
        ) from exc
    if rc != 0:
        raise SandboxUnavailableError(
            f"bwrap preflight exited {rc}: the host cannot create an "
            f"unprivileged user namespace sandbox. Verify unprivileged "
            f"user namespaces are enabled (kernel.unprivileged_userns_clone=1 "
            f"/ user.max_user_namespaces > 0). Refusing to run jobs "
            f"unsandboxed; set [sandbox].allow_unsandboxed = true only for "
            f"local dev."
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox.py -v`
Expected: PASS (10 passed).

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/sandbox.py packages/foreman/tests/v4/test_sandbox.py
git commit -m "feat: add sandbox preflight self-test with fail-closed error"
```

---

## Task 4: Dockerfile — bubblewrap + immutable-foreman guard

**Files:**
- Modify: `Dockerfile` (line 38 apt block; new guard `RUN` after line 103)

**Interfaces:**
- Consumes: nothing (build-time).
- Produces: `bwrap` on PATH in the image; a build-time assertion that `foreman` is installed non-editable under site-packages.

- [ ] **Step 1: Add bubblewrap to the system-deps apt block**

In `Dockerfile`, edit the apt install list (currently lines 37-42) to add `bubblewrap` and a comment:

```dockerfile
# bubblewrap: job-execution sandbox (foreman#job-sandbox-isolation). Each
#   role subprocess is wrapped in `bwrap` so it can only read a shared RO
#   uv cache and write its own scratch worktree.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl gnupg \
        nodejs npm \
        gettext-base \
        just \
        bubblewrap \
    && rm -rf /var/lib/apt/lists/*
```

- [ ] **Step 2: Add the immutable-foreman guard after the source install**

Immediately after the existing source-layer install (line 103,
`RUN uv pip install --system --no-cache --no-deps .`), add:

```dockerfile
# Immutable-foreman guard (foreman#job-sandbox-isolation). The 2026-07-18
# incident happened because foreman was editable-installed via a .pth
# pointing at an ephemeral worktree, so a concurrent job's `uv sync`
# re-registered it and broke the daemon's import. Assert at build time
# that foreman lives in the image's stable site-packages and NO editable
# (.pth / __editable__) registration exists — fail the build loudly if it
# ever regresses.
RUN python - <<'PY'
import glob
import pathlib
import sys

import foreman

mod = pathlib.Path(foreman.__file__).resolve()
assert "site-packages" in mod.parts, f"foreman not in site-packages: {mod}"

editable = []
for root in ("/usr/lib", "/usr/local/lib"):
    editable += glob.glob(f"{root}/python3*/**/__editable__*foreman*", recursive=True)
    editable += glob.glob(f"{root}/python3*/**/__editable___foreman*.pth", recursive=True)
assert not editable, f"editable foreman install found: {editable}"
print(f"immutable-foreman guard OK: {mod}")
PY
```

- [ ] **Step 3: Verify the guard logic locally (no full image build required)**

Run (proves the guard script's assertions pass against the installed package; the worktree venv has foreman installed):

`uv run --no-sync python -c "import foreman, pathlib; p=pathlib.Path(foreman.__file__).resolve(); print(p); assert p.suffix=='.py'"`

Expected: prints a path ending in `foreman/__init__.py`. (The Dockerfile guard runs at image build; this local check just confirms the import + path shape the guard relies on.)

- [ ] **Step 4: Commit**

```bash
git add Dockerfile
git commit -m "chore: install bubblewrap and assert immutable foreman install"
```

---

## Task 5: Wire `SandboxLauncher` into the dispatcher behind the flag

**Files:**
- Modify: `packages/foreman/src/foreman/v4/subprocess_dispatcher.py` (`__init__` ~254-271; `dispatch` ~301-345; `_write_banner` ~148-167)
- Test: `packages/foreman/tests/v4/test_sandbox_dispatch.py` (create)

**Interfaces:**
- Consumes: `SandboxLauncher.build_argv(...)` from Task 2 (`role_token`, `scratch_dir`, `role_cmd`, `passthrough`).
- Produces:
  - `SubprocessRoleDispatcher.__init__` gains kwargs `sandbox: SandboxLauncher | None = None` and `sandbox_scratch_root: Path | None = None` (both default `None` = unchanged behavior).
  - Module function `_redact_cmd(cmd: list[str]) -> list[str]` — masks the value following `--setenv GH_TOKEN` so the on-disk log banner never records the token.
  - When `sandbox` is set, `dispatch()` builds the per-job scratch dir `<sandbox_scratch_root>/<project>/<role_base>-<issue_number>`, wraps `cmd` via the launcher, and Popens the wrapped argv. Reader threads + outcome parsing are unchanged.

- [ ] **Step 1: Write the failing test**

Create `packages/foreman/tests/v4/test_sandbox_dispatch.py`:

```python
"""Dispatcher wiring: flag off = unchanged; flag on = bwrap-wrapped. Pure."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

from foreman.v4.sandbox import SandboxLauncher
from foreman.v4.subprocess_dispatcher import _redact_cmd


def test_redact_masks_gh_token_after_setenv() -> None:
    cmd = ["bwrap", "--setenv", "GH_TOKEN", "ghs_SECRET", "--", "foreman", "plan"]
    red = _redact_cmd(cmd)
    assert "ghs_SECRET" not in red
    assert red[red.index("GH_TOKEN") + 1] == "***"
    # non-token args untouched
    assert red[-2:] == ["foreman", "plan"]


def test_redact_is_noop_without_gh_token() -> None:
    cmd = ["foreman", "implement", "--project", "foreman"]
    assert _redact_cmd(cmd) == cmd


def test_dispatch_flag_off_runs_unwrapped(tmp_path: Path) -> None:
    """With no sandbox, the stub role runs directly (no bwrap prefix)."""
    from foreman.v4.subprocess_dispatcher import SubprocessRoleDispatcher

    stub = tmp_path / "stub.py"
    stub.write_text(
        "import sys\n"
        'print(\'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}\')\n'
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    identity = MagicMock()
    identity.get_role_token.return_value = "ghs_X"
    d = SubprocessRoleDispatcher(
        foreman_cli=[sys.executable, str(stub)],
        identity=identity,
        log_dir=tmp_path,
    )
    out = d.dispatch(role="planner", project="foreman", issue_number=1, ticket_id=1)
    assert "FOREMAN_OUTCOME:" in out


def test_dispatch_flag_on_creates_scratch_and_wraps(tmp_path: Path, monkeypatch) -> None:
    """With a sandbox launcher, dispatch creates the per-job scratch dir and
    Popens a bwrap-prefixed argv. We intercept Popen to assert the argv shape
    without needing real bwrap."""
    from foreman.v4 import subprocess_dispatcher as sd

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

    identity = MagicMock()
    identity.get_role_token.return_value = "ghs_TOK"
    scratch_root = tmp_path / "scratch"
    d = sd.SubprocessRoleDispatcher(
        foreman_cli=["foreman"],
        identity=identity,
        log_dir=tmp_path / "logs",
        sandbox=SandboxLauncher(cache_dir="/root/.cache/uv", bwrap_path="bwrap"),
        sandbox_scratch_root=scratch_root,
    )
    out = d.dispatch(role="worker", project="foreman", issue_number=537, ticket_id=99)
    assert "FOREMAN_OUTCOME:" in out
    cmd = captured["cmd"]
    assert cmd[0] == "bwrap"
    assert "foreman" in cmd and "implement" in cmd
    # scratch dir created and mounted RW at /scratch
    expected_scratch = scratch_root / "foreman" / "worker-537"
    assert expected_scratch.exists()
    dd = cmd.index("--bind")
    assert cmd[dd + 1] == str(expected_scratch)
    assert cmd[dd + 2] == "/scratch"


class _StubStream:
    def __init__(self, text: str) -> None:
        self._lines = text.splitlines(keepends=True)

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox_dispatch.py -v`
Expected: FAIL — `ImportError: cannot import name '_redact_cmd'` (and `TypeError` on the unknown `sandbox=` kwarg).

- [ ] **Step 3: Write minimal implementation**

In `packages/foreman/src/foreman/v4/subprocess_dispatcher.py`:

(a) Add the import near the top (after `from foreman.v4.outcome import OUTCOME_MARKER`):

```python
from foreman.v4.sandbox import SandboxLauncher
```

(b) Add the redaction helper at module level (after `_STDERR_PREFIX` constants block, before `class IdentityProvider`):

```python
def _redact_cmd(cmd: list[str]) -> list[str]:
    """Return a copy of ``cmd`` with any ``--setenv GH_TOKEN <value>`` masked.

    The log banner records the full command. Under the sandbox the role
    token is passed as a ``bwrap --setenv GH_TOKEN <token>`` triple, which
    would otherwise write the secret to the on-disk log. Mask the value so
    the banner is safe to keep.
    """
    redacted = list(cmd)
    for i in range(len(redacted) - 2):
        if redacted[i] == "--setenv" and redacted[i + 1] == "GH_TOKEN":
            redacted[i + 2] = "***"
    return redacted
```

(c) Extend `__init__` (add the two kwargs + store them). Replace the current signature/body head:

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
    ) -> None:
        self._foreman_cli = foreman_cli
        self._identity = identity
        self._log_dir = log_dir
        self._timeout = timeout_seconds
        self._inactivity_timeout = inactivity_timeout_seconds
        # foreman#job-sandbox-isolation: when set, every role command is
        # wrapped in a bwrap box and the per-job worktree is rooted under
        # the RW scratch mount. Both None => pre-sandbox behavior.
        self._sandbox = sandbox
        self._sandbox_scratch_root = sandbox_scratch_root
```

(d) In `dispatch()`, after the `env` dict is fully built (right after the `session_id` block, before `started_at = dt.datetime.now(dt.UTC)`), insert the wrapping:

```python
        if self._sandbox is not None:
            if self._sandbox_scratch_root is None:
                raise RoleSubprocessError(
                    f"role={role}: sandbox enabled but no scratch root "
                    f"configured; cannot create the job's writable mount"
                )
            role_base = _base_role(role)
            scratch_dir = (
                self._sandbox_scratch_root / project / f"{role_base}-{issue_number}"
            )
            scratch_dir.mkdir(parents=True, exist_ok=True)
            # Curated non-secret passthrough allowlist (positive defense):
            # only these keys cross into the box, plus GH_TOKEN handled by
            # the launcher. Everything else in the daemon env is dropped by
            # bwrap --clearenv.
            passthrough = {
                key: env[key]
                for key in (
                    "FOREMAN_STATE_INSTANCE_ID",
                    "FOREMAN_SESSION_ID",
                    "FOREMAN_RESUME_SESSION_ID",
                    "FOREMAN_V4_CONFIG",
                    "CLAUDE_CONFIG_DIR",
                    "ANTHROPIC_API_KEY",
                    "LANG",
                )
                if key in env
            }
            cmd = self._sandbox.build_argv(
                role_token=env["GH_TOKEN"],
                scratch_dir=str(scratch_dir),
                role_cmd=cmd,
                passthrough=passthrough,
            )
```

(e) In `_write_banner`, redact the token before writing. Change the `cmd=` line — replace:

```python
        f"cmd={cmd}\n"
```

with:

```python
        f"cmd={_redact_cmd(cmd)}\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox_dispatch.py packages/foreman/tests/v4/test_phase5_e2e_subprocess.py -v`
Expected: PASS (all — the phase5 e2e proves the flag-off path is byte-for-byte unchanged).

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/subprocess_dispatcher.py packages/foreman/tests/v4/test_sandbox_dispatch.py
git commit -m "feat: wrap role dispatch in bwrap behind the sandbox flag"
```

---

## Task 6: Bootstrap wiring — build launcher, preflight fail-closed, thread scratch root

**Files:**
- Modify: `packages/foreman/src/foreman/v4/bootstrap.py` (imports; ~line 176 dispatcher construction)
- Test: `packages/foreman/tests/v4/test_bootstrap.py` (append)

**Interfaces:**
- Consumes: `SandboxConfig` (Task 1), `SandboxLauncher` + `preflight` + `SandboxUnavailableError` (Tasks 2-3), the dispatcher kwargs (Task 5).
- Produces: `bootstrap_cli_context` constructs a `SandboxLauncher` from `config.sandbox` and passes it (+ `sandbox_scratch_root`) to `SubprocessRoleDispatcher` when `config.sandbox.enabled`; runs `preflight()` first and re-raises `SandboxUnavailableError` unless `allow_unsandboxed` (then logs a loud warning and runs unsandboxed).

- [ ] **Step 1: Write the failing test**

Append to `packages/foreman/tests/v4/test_bootstrap.py` (mirror the existing fixtures there for `config` + `identity` + `git_provider_factory`; the snippet below assumes a `_minimal_config(...)` helper analogous to what the file already uses — reuse the existing config-builder in that test module):

```python
import pytest

from foreman.v4.sandbox import SandboxUnavailableError


def test_bootstrap_fails_closed_when_preflight_fails(minimal_config_with_sandbox, fake_identity, fake_git_factory, monkeypatch):
    """sandbox.enabled + preflight failure + allow_unsandboxed False => raise."""
    from foreman.v4 import bootstrap

    cfg = minimal_config_with_sandbox(enabled=True, allow_unsandboxed=False)

    def boom(**kwargs):
        raise SandboxUnavailableError("no userns")

    monkeypatch.setattr(bootstrap, "preflight", boom)
    with pytest.raises(SandboxUnavailableError):
        bootstrap.bootstrap_cli_context(
            config=cfg,
            identity=fake_identity,
            git_provider_factory=fake_git_factory,
        )


def test_bootstrap_warns_and_continues_when_allow_unsandboxed(minimal_config_with_sandbox, fake_identity, fake_git_factory, monkeypatch, caplog):
    from foreman.v4 import bootstrap

    cfg = minimal_config_with_sandbox(enabled=True, allow_unsandboxed=True)

    def boom(**kwargs):
        raise SandboxUnavailableError("no userns")

    monkeypatch.setattr(bootstrap, "preflight", boom)
    ctx = bootstrap.bootstrap_cli_context(
        config=cfg,
        identity=fake_identity,
        git_provider_factory=fake_git_factory,
    )
    assert ctx is not None
    assert any("unsandboxed" in r.message.lower() for r in caplog.records)


def test_bootstrap_disabled_sandbox_skips_preflight(minimal_config_with_sandbox, fake_identity, fake_git_factory, monkeypatch):
    from foreman.v4 import bootstrap

    cfg = minimal_config_with_sandbox(enabled=False, allow_unsandboxed=False)

    def boom(**kwargs):
        raise AssertionError("preflight must not run when sandbox disabled")

    monkeypatch.setattr(bootstrap, "preflight", boom)
    ctx = bootstrap.bootstrap_cli_context(
        config=cfg,
        identity=fake_identity,
        git_provider_factory=fake_git_factory,
    )
    assert ctx is not None
```

Add a `minimal_config_with_sandbox` fixture to that test module that returns a `V4Config` with a `SandboxConfig(enabled=..., allow_unsandboxed=...)` and the existing required blocks (reuse the module's existing config-building helper, then `.model_copy(update={"sandbox": SandboxConfig(...)})`).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_bootstrap.py -k sandbox -v`
Expected: FAIL — `AttributeError: module 'foreman.v4.bootstrap' has no attribute 'preflight'` (preflight not imported/wired yet).

- [ ] **Step 3: Write minimal implementation**

In `packages/foreman/src/foreman/v4/bootstrap.py`:

(a) Add the import (with the other `from foreman.v4.*` imports):

```python
from foreman.v4.sandbox import SandboxLauncher, SandboxUnavailableError, preflight
```

(b) Replace the current dispatcher construction block (lines ~176-185) with:

```python
    sandbox_launcher: SandboxLauncher | None = None
    sandbox_scratch_root: Path | None = None
    if config.sandbox.enabled:
        try:
            preflight(bwrap_path=config.sandbox.bwrap_path)
        except SandboxUnavailableError:
            if not config.sandbox.allow_unsandboxed:
                logger.error(
                    "sandbox preflight failed and allow_unsandboxed is false; "
                    "refusing to start. Fix the host's unprivileged user "
                    "namespaces or set [sandbox].allow_unsandboxed for local dev."
                )
                raise
            logger.warning(
                "sandbox preflight FAILED but allow_unsandboxed=true: running "
                "role subprocesses UNSANDBOXED (local-dev escape hatch). Every "
                "dispatch is unprotected — do not use this in production."
            )
        else:
            sandbox_launcher = SandboxLauncher(
                cache_dir=config.sandbox.cache_dir,
                bwrap_path=config.sandbox.bwrap_path,
            )
            sandbox_scratch_root = Path(config.sandbox.scratch_root)
            logger.info(
                "sandbox enabled: role subprocesses run in bwrap boxes",
                extra={
                    "cache_dir": config.sandbox.cache_dir,
                    "scratch_root": config.sandbox.scratch_root,
                },
            )

    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=foreman_cli or ["foreman"],
        identity=identity,
        log_dir=Path(config.log_dir),
        timeout_seconds=config.role_timeout_seconds,
        inactivity_timeout_seconds=config.role_inactivity_timeout_seconds,
        sandbox=sandbox_launcher,
        sandbox_scratch_root=sandbox_scratch_root,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_bootstrap.py -k sandbox -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/bootstrap.py packages/foreman/tests/v4/test_bootstrap.py
git commit -m "feat: build sandbox launcher and run preflight fail-closed at startup"
```

---

## Task 7: Hermetic real-bwrap integration + permanent 2026-07-18 regression test

**Files:**
- Create: `packages/foreman/tests/v4/test_sandbox_integration.py`

**Interfaces:**
- Consumes: `SandboxLauncher.build_argv(...)` (Task 2). Runs real `bwrap`.
- Produces: an end-to-end proof of the mount plan + the permanent regression lock for the incident. Self-skips when unprivileged userns / `bwrap` is unavailable (Windows, CI runners without nested userns) so `just check` never hard-fails on an unsupported runner.

- [ ] **Step 1: Write the test (it self-skips until real bwrap is present)**

Create `packages/foreman/tests/v4/test_sandbox_integration.py`:

```python
"""Hermetic real-bwrap integration + the permanent 2026-07-18 regression lock.

These exercise a REAL bwrap boot, so they only run on Linux with
unprivileged user namespaces. On any other runner (Windows dev box, CI
without nested userns) the whole module self-skips — the pure argv tests
in test_sandbox.py provide the cross-platform coverage.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from foreman.v4.sandbox import SandboxLauncher


def _userns_available() -> bool:
    """True iff a minimal bwrap userns sandbox actually boots on this host."""
    if shutil.which("bwrap") is None:
        return False
    try:
        rc = subprocess.run(
            ["bwrap", "--unshare-user", "--ro-bind", "/usr", "/usr",
             "--tmpfs", "/tmp", "--", "/bin/true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return False
    return rc == 0


pytestmark = pytest.mark.skipif(
    not _userns_available(),
    reason="requires bwrap + unprivileged user namespaces",
)


def _run_in_box(tmp_path: Path, role_cmd: list[str], *, cache_dir: Path, scratch_dir: Path):
    launcher = SandboxLauncher(cache_dir=str(cache_dir))
    argv = launcher.build_argv(
        role_token="ghs_TESTTOKEN",
        scratch_dir=str(scratch_dir),
        role_cmd=role_cmd,
    )
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def test_scratch_is_writable_and_cache_is_readonly(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "wheel.txt").write_text("cached", encoding="utf-8")
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()

    # scratch writable
    r = _run_in_box(
        tmp_path,
        ["/bin/sh", "-c", "echo hi > /scratch/out.txt && cat /scratch/out.txt"],
        cache_dir=cache_dir,
        scratch_dir=scratch_dir,
    )
    assert r.returncode == 0, r.stderr
    assert "hi" in r.stdout
    assert (scratch_dir / "out.txt").exists()  # write landed on the real host dir

    # cache read succeeds
    r = _run_in_box(
        tmp_path, ["/bin/sh", "-c", "cat /cache/wheel.txt"],
        cache_dir=cache_dir, scratch_dir=scratch_dir,
    )
    assert r.returncode == 0 and "cached" in r.stdout

    # cache write is rejected (RO mount)
    r = _run_in_box(
        tmp_path, ["/bin/sh", "-c", "echo x > /cache/nope.txt"],
        cache_dir=cache_dir, scratch_dir=scratch_dir,
    )
    assert r.returncode != 0
    assert not (cache_dir / "nope.txt").exists()


def test_daemon_secret_dir_is_invisible(tmp_path: Path) -> None:
    """A path we never bind (simulating /run/secrets, /root/.foreman) is absent."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    secret = tmp_path / "daemon_secret"
    secret.mkdir()
    (secret / "worker.pem").write_text("PRIVATE KEY", encoding="utf-8")

    r = _run_in_box(
        tmp_path,
        ["/bin/sh", "-c", f"cat {secret / 'worker.pem'} 2>&1 || echo INVISIBLE"],
        cache_dir=cache_dir, scratch_dir=scratch_dir,
    )
    assert "PRIVATE KEY" not in r.stdout
    assert "INVISIBLE" in r.stdout


def test_pid_isolation(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    r = _run_in_box(
        tmp_path, ["/bin/sh", "-c", "ls /proc | grep -c '^[0-9]*$'"],
        cache_dir=cache_dir, scratch_dir=scratch_dir,
    )
    assert r.returncode == 0
    # a fresh PID namespace shows only a handful of procs, never the host's hundreds
    assert int(r.stdout.strip()) < 20


def test_regression_2026_07_18_import_foreman_and_daemon_install_write_fail(tmp_path: Path) -> None:
    """PERMANENT lock for the 2026-07-18 foreman.prompts incident.

    From inside the box: (1) a write to the daemon's foreman install path
    fails because /usr is read-only and the daemon source is never mounted;
    (2) a fresh venv under /scratch cannot import foreman because the box's
    Python does not inherit the daemon's system site-packages on its path.
    """
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()

    # (1) writing into the RO system tree (where the daemon foreman lives) fails.
    r = _run_in_box(
        tmp_path,
        ["/bin/sh", "-c", "echo pwn > /usr/lib/foreman_marker.py"],
        cache_dir=cache_dir, scratch_dir=scratch_dir,
    )
    assert r.returncode != 0

    # (2) a clean venv under /scratch has no foreman on its path.
    r = _run_in_box(
        tmp_path,
        [
            "/bin/sh",
            "-c",
            "python3 -m venv /scratch/.venv "
            "&& /scratch/.venv/bin/python -c 'import foreman' "
            "&& echo IMPORTED || echo NO_FOREMAN",
        ],
        cache_dir=cache_dir, scratch_dir=scratch_dir,
    )
    assert "NO_FOREMAN" in r.stdout
    assert "IMPORTED" not in r.stdout
```

- [ ] **Step 2: Run the test**

On a Linux host with userns:
Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox_integration.py -v`
Expected: PASS (5 passed).

On Windows / a runner without userns:
Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox_integration.py -v`
Expected: `5 skipped` (module-level skip via `_userns_available()`), never a hard failure.

- [ ] **Step 3: Commit**

```bash
git add packages/foreman/tests/v4/test_sandbox_integration.py
git commit -m "test: hermetic bwrap integration + permanent 2026-07-18 regression lock"
```

---

## Final gate

- [ ] **Run the full check gate**

Run: `just check`
Expected: PASS — `lock-check`, `lint` (ruff, google docstrings), `format-check`, `typecheck` (mypy strict, unscoped over `packages/foreman/src`), `import-lint` (R1/R2 satisfied — `foreman.v4.sandbox` imports no v3-substrate module), `test` (coverage ≥ 80, diff-cover ≥ 80). The `test_sandbox_integration.py` module self-skips on the CI runner if userns is unavailable; the pure `test_sandbox.py` / `test_sandbox_dispatch.py` coverage carries the diff.

- [ ] **Manual dogfood validation (post-merge, on the live daemon)**

Flip `[sandbox].enabled = true` (+ rebuild the image so `bwrap` is present), then run a foreman-self ticket concurrently with an agent_core ticket — the exact 2026-07-18 scenario. Expect both to complete without any `foreman.prompts` / shared-env corruption. Then remove the interim "don't run foreman-self tickets" operational caveat (spec rollout step 5).

---

## Self-Review

**Spec coverage:**

| Spec requirement | Task |
| --- | --- |
| One-seam change at `SubprocessRoleDispatcher` | Task 5 |
| `SandboxLauncher` pure argv builder, owns mount plan + never-bind | Task 2 |
| Immutable daemon foreman (non-editable `--system`, no runtime re-register) | Task 4 (build-time guard) |
| Shared RW uv cache + per-job venv in scratch | Tasks 2 (`/cache` RW, `UV_CACHE_DIR`), 6 (scratch), 5 (`FOREMAN_WORKTREES_ROOT`) |
| Full mount plan (namespaces/net/lifecycle/RO roots/tmpfs/never-bind/GH_TOKEN) | Task 2 |
| Job Python from own venv; `import foreman` fails in box | Task 2 (`--clearenv`, `PYTHONNOUSERSITE`, no system site-packages) + Task 7 regression |
| Preflight self-test at startup, fail-closed | Task 3 (module) + Task 6 (wired) |
| `allow_unsandboxed` escape hatch, logged loudly per bring-up | Tasks 1, 6 |
| Sandbox setup failure → clean dispatch fail, never crash daemon | Task 5 (`RoleSubprocessError` on missing scratch root; rides existing per-job fault isolation) |
| `--die-with-parent`, existing kill/reap unchanged | Task 2 (flag) + Task 5 (reader threads/outcome parsing untouched) |
| Unit tests: never-bind absent, token present + no other secret, mount plan | Task 2 |
| Hermetic integration (RO cache / RW scratch / open net / secret-invisible / PID iso), self-skip on no-userns | Task 7 |
| Permanent regression test for the incident | Task 7 |
| Preflight unit test for pass/fail branches | Task 3 |
| Dockerfile `apt-get install bubblewrap` | Task 4 |
| Config flag default off | Task 1 |

Open-net coverage nuance: Task 7 asserts scratch/cache/secret/PID/import behavior directly; the `--share-net` flag itself is asserted in the pure Task 2 argv test (a real outbound-network assertion is deliberately omitted from the hermetic test so it does not flake on offline CI). Noted as an intentional deviation, not a gap.

**Placeholder scan:** No `TBD`/`TODO`/"similar to Task N"/"add error handling". Every code step carries complete code. The one cross-reference — Task 6's reuse of the test module's existing config/identity/git fixtures — names the concrete fixtures (`minimal_config_with_sandbox`, `fake_identity`, `fake_git_factory`) and gives the construction recipe (`model_copy(update={"sandbox": SandboxConfig(...)})`) rather than hand-waving.

**Type consistency:** `SandboxConfig` fields (`enabled`, `allow_unsandboxed`, `cache_dir`, `scratch_root`, `bwrap_path`) are consumed with those exact names in Task 6. `SandboxLauncher(cache_dir=..., bwrap_path=...)` and `.build_argv(role_token=..., scratch_dir=..., role_cmd=..., passthrough=...)` are identical across Tasks 2, 5, 6, 7. Dispatcher kwargs `sandbox` / `sandbox_scratch_root` match between Task 5 (definition) and Task 6 (call site). `_redact_cmd(cmd: list[str]) -> list[str]` and `preflight(*, bwrap_path=..., runner=...)` / `SandboxUnavailableError` names are consistent across their definition and use sites.
