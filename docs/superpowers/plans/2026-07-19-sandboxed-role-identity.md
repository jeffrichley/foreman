# Sandboxed-Role Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a role subprocess inside the bubblewrap sandbox authenticate to GitHub (git-CLI *and* PyGithub API) with its injected `GH_TOKEN` instead of minting from the withheld PEM keys.

**Architecture:** One env marker, `FOREMAN_SANDBOXED=1` (set by `SandboxLauncher.build_argv`), flips three things in the role's `main()`: use a new `EnvTokenIdentity` (returns the injected `GH_TOKEN`, no PEM) instead of the PEM-based `V4IdentityRegistry`, and pass `run_startup_clone=False` to `bootstrap_cli_context` to skip the daemon-level clone loop (the only other PEM consumer). Non-sandboxed runs are byte-for-byte unchanged.

**Tech Stack:** Python 3.12, uv, pytest, typer, PyGithub, bubblewrap. Repo `e:/workspaces/ai/agents/foreman`, worktree `.worktrees/role-identity`, branch `feat/sandboxed-role-identity` off `a0ef15a`.

## Global Constraints

- NO `Co-Authored-By` trailer on any commit.
- Conventional-commit, lowercase subject (`feat:`, `test:`, `fix:`, `docs:`).
- ruff google-style docstrings; `mypy --strict` clean, run UNSCOPED: `uv run --no-sync mypy packages/foreman/src`.
- `just check` gate: 85% coverage floor, diff-cover 80.
- Worktree already `uv sync`-ed — use `uv run --no-sync` for every command.
- `just check` does NOT run `ruff format --check` (issue #433), but keep format clean: run `uv run --no-sync ruff format <files>` before each commit.
- Real-bwrap tests self-skip where unprivileged userns is unavailable (CI, Windows) via `_userns_available()` in `test_sandbox_integration.py`; pure argv/unit tests run everywhere.
- Non-sandboxed path (marker absent) MUST remain the exact current PEM path — do not alter its behavior.

---

### Task 1: `EnvTokenIdentity` + `SandboxIdentityError`

**Files:**
- Modify: `packages/foreman/src/foreman/v4/identity.py` (add class + exception; ensure `import os` present)
- Test: `packages/foreman/tests/v4/test_env_token_identity.py` (create)

**Interfaces:**
- Consumes: the `IdentityProvider` Protocol at `foreman/v4/bootstrap.py:41` — one method `get_role_token(self, role: str) -> str`.
- Produces: `EnvTokenIdentity` (satisfies `IdentityProvider`); `SandboxIdentityError(RuntimeError)`.

- [ ] **Step 1: Write the failing tests**

Create `packages/foreman/tests/v4/test_env_token_identity.py`:

```python
"""Unit tests for EnvTokenIdentity — the sandbox's GH_TOKEN-backed identity."""

from __future__ import annotations

import pytest

from foreman.v4.identity import EnvTokenIdentity, SandboxIdentityError


def test_returns_injected_token_for_any_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghs_INJECTED")
    ident = EnvTokenIdentity()
    # The box holds exactly one token; the role argument is inert here.
    assert ident.get_role_token("planner") == "ghs_INJECTED"
    assert ident.get_role_token("orchestrator") == "ghs_INJECTED"
    assert ident.get_role_token("reviewer") == "ghs_INJECTED"


def test_raises_when_token_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(SandboxIdentityError):
        EnvTokenIdentity().get_role_token("planner")


def test_raises_when_token_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "")
    with pytest.raises(SandboxIdentityError):
        EnvTokenIdentity().get_role_token("planner")


def test_satisfies_identity_provider_protocol() -> None:
    from foreman.v4.bootstrap import IdentityProvider

    ident: IdentityProvider = EnvTokenIdentity()  # structural typing check
    assert callable(ident.get_role_token)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_env_token_identity.py -q`
Expected: FAIL — `ImportError: cannot import name 'EnvTokenIdentity'`.

- [ ] **Step 3: Implement**

In `packages/foreman/src/foreman/v4/identity.py`, confirm `import os` is at the top (add it if missing). Add, after the existing `V4IdentityRegistry` class:

```python
class SandboxIdentityError(RuntimeError):
    """Raised when a sandboxed role has no injected GH_TOKEN to authenticate with.

    Typed (not a bare ``RuntimeError``) so the fail-closed path is greppable,
    mirroring ``SandboxUnavailableError`` in :mod:`foreman.v4.sandbox`.
    """


class EnvTokenIdentity:
    """``IdentityProvider`` backed by a single injected ``GH_TOKEN``.

    Used only inside the bubblewrap sandbox, where the daemon injects the
    dispatched role's short-lived installation token as ``GH_TOKEN`` and the
    PEM keys are deliberately absent (see ``DAEMON_NEVER_BIND``). Returns that
    one token for ANY ``role`` argument: the box holds exactly one role's
    identity and nothing else, so ``role`` is inert here — the sandbox mount
    plan, not this class, is what guarantees single-role. Never reads a PEM;
    fail-closed if ``GH_TOKEN`` is missing.

    Satisfies :class:`~foreman.v4.bootstrap.IdentityProvider`.
    """

    _ENV_VAR = "GH_TOKEN"

    def get_role_token(self, role: str) -> str:
        """Return the injected ``GH_TOKEN``, ignoring ``role``.

        Args:
            role: The requested role. Inert — see the class docstring.

        Returns:
            The ``GH_TOKEN`` from the process environment.

        Raises:
            SandboxIdentityError: if ``GH_TOKEN`` is unset or empty.
        """
        token = os.environ.get(self._ENV_VAR)
        if not token:
            raise SandboxIdentityError(
                "FOREMAN_SANDBOXED is set but GH_TOKEN is empty/unset; the "
                "sandboxed role has no injected token to authenticate with. "
                "The dispatcher must set --setenv GH_TOKEN <role token>. "
                "Refusing to run."
            )
        return token
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_env_token_identity.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Lint, format, commit**

```bash
uv run --no-sync ruff format packages/foreman/src/foreman/v4/identity.py packages/foreman/tests/v4/test_env_token_identity.py
uv run --no-sync ruff check packages/foreman/src/foreman/v4/identity.py packages/foreman/tests/v4/test_env_token_identity.py
uv run --no-sync mypy packages/foreman/src
git add packages/foreman/src/foreman/v4/identity.py packages/foreman/tests/v4/test_env_token_identity.py
git commit -m "feat(v4): EnvTokenIdentity — GH_TOKEN-backed identity for the sandbox"
```

---

### Task 2: `run_startup_clone` param on `bootstrap_cli_context`

**Files:**
- Modify: `packages/foreman/src/foreman/v4/bootstrap.py:54` (signature) and `:96` (clone-loop guard)
- Test: `packages/foreman/tests/v4/test_bootstrap.py` (extend — create if absent)

**Interfaces:**
- Consumes: existing `bootstrap_cli_context(*, config, identity, git_provider_factory, foreman_cli=None, projects=None, projects_loader=None)`.
- Produces: same, plus keyword `run_startup_clone: bool = True`. When `False`, the `orch_token = identity.get_role_token("orchestrator")` clone loop (`bootstrap.py:96-97`) does not run.

- [ ] **Step 1: Write the failing test**

Add to `packages/foreman/tests/v4/test_bootstrap.py` (reuse the module's existing config/projects/git-provider-factory construction that the current `bootstrap_cli_context` tests use — a minimal `V4Config`, one `ProjectConfig`, and a fake git provider factory):

```python
def test_skips_clone_loop_when_run_startup_clone_false() -> None:
    """run_startup_clone=False must not mint the orchestrator token."""

    class _RaisingIdentity:
        def get_role_token(self, role: str) -> str:
            raise AssertionError(f"clone loop ran: minted token for {role!r}")

    ctx = bootstrap_cli_context(
        config=_minimal_config(),  # existing helper in this test module
        identity=_RaisingIdentity(),
        git_provider_factory=_fake_git_factory,  # existing helper
        projects=[_one_project()],  # existing helper; non-empty on purpose
        run_startup_clone=False,
    )
    assert ctx is not None
```

If the test module has no such helpers, build the minimal `V4Config` /
`ProjectConfig` / fake factory the same way the module's *existing*
`bootstrap_cli_context` test does (read it first), then add the case above.
Do NOT invent a new config shape.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_bootstrap.py::test_skips_clone_loop_when_run_startup_clone_false -q`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'run_startup_clone'`.

- [ ] **Step 3: Implement**

In `packages/foreman/src/foreman/v4/bootstrap.py`, add the parameter to the `bootstrap_cli_context` signature (keyword-only, default `True`, documented in the docstring):

```python
def bootstrap_cli_context(
    *,
    config: V4Config,
    identity: IdentityProvider,
    git_provider_factory: Callable[[str], GitProvider],
    foreman_cli: list[str] | None = None,
    projects: list[ProjectConfig] | None = None,
    projects_loader: Callable[[], list[ProjectConfig]] | None = None,
    run_startup_clone: bool = True,
) -> CliContext:
```

Add to the docstring's Args:

```
    run_startup_clone: When False, skip the daemon-level all-projects
        clone-maintenance loop (and the orchestrator-token mint it needs).
        Set False for a sandboxed role subprocess, whose private clone the
        daemon already prepped and which has no PEM to mint with.
```

Change the guard at `bootstrap.py:96` from `if active_projects:` to:

```python
    if run_startup_clone and active_projects:
        orch_token = identity.get_role_token("orchestrator")
        for pc in active_projects:
            ...
```

(Everything else in the function is unchanged; the object-graph build below the loop still runs.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_bootstrap.py -q`
Expected: PASS — the new test plus all existing bootstrap tests (which exercise the default `run_startup_clone=True` path, confirming unchanged behavior).

- [ ] **Step 5: Lint, format, commit**

```bash
uv run --no-sync ruff format packages/foreman/src/foreman/v4/bootstrap.py packages/foreman/tests/v4/test_bootstrap.py
uv run --no-sync ruff check packages/foreman/src/foreman/v4/bootstrap.py packages/foreman/tests/v4/test_bootstrap.py
uv run --no-sync mypy packages/foreman/src
git add packages/foreman/src/foreman/v4/bootstrap.py packages/foreman/tests/v4/test_bootstrap.py
git commit -m "feat(v4): run_startup_clone flag to skip the daemon clone loop"
```

---

### Task 3: `_select_identity` helper + `FOREMAN_SANDBOXED` branch in `main()`

**Files:**
- Modify: `packages/foreman/src/foreman/v4/cli/__init__.py` (add `_select_identity`; branch in `main()` around `:249`-`:271`)
- Test: `packages/foreman/tests/v4/test_cli_select_identity.py` (create)

**Interfaces:**
- Consumes: `EnvTokenIdentity` (Task 1); `V4IdentityRegistry` (`identity.py:109`); `run_startup_clone` (Task 2).
- Produces: `_select_identity(*, config, projects, sandboxed) -> tuple[IdentityProvider, bool]`.

- [ ] **Step 1: Write the failing tests**

Create `packages/foreman/tests/v4/test_cli_select_identity.py`:

```python
"""Unit tests for the sandbox/PEM identity selection in the CLI entrypoint."""

from __future__ import annotations

from unittest.mock import MagicMock

from foreman.v4.cli import _select_identity
from foreman.v4.identity import EnvTokenIdentity, V4IdentityRegistry


def test_sandboxed_returns_env_token_identity_and_skips_clone() -> None:
    ident, run_startup_clone = _select_identity(
        config=MagicMock(), projects=[], sandboxed=True
    )
    assert isinstance(ident, EnvTokenIdentity)
    assert run_startup_clone is False  # sandbox skips the daemon clone loop


def test_sandboxed_does_not_index_projects() -> None:
    # projects=[] must not raise — sandbox mode never reads projects[0].repo.
    ident, _ = _select_identity(config=MagicMock(), projects=[], sandboxed=True)
    assert isinstance(ident, EnvTokenIdentity)


def test_unsandboxed_returns_registry_and_runs_clone() -> None:
    project = MagicMock()
    project.repo = "owner/repo"
    ident, run_startup_clone = _select_identity(
        config=MagicMock(), projects=[project], sandboxed=False
    )
    assert isinstance(ident, V4IdentityRegistry)
    assert run_startup_clone is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_cli_select_identity.py -q`
Expected: FAIL — `ImportError: cannot import name '_select_identity'`.

- [ ] **Step 3: Implement**

In `packages/foreman/src/foreman/v4/cli/__init__.py`, add a module-level helper (local imports keep the module importable without PyGithub, matching the existing pattern in `main()`):

```python
def _select_identity(
    *, config: "V4Config", projects: "list[ProjectConfig]", sandboxed: bool
) -> "tuple[IdentityProvider, bool]":
    """Choose the identity provider and whether to run the startup clone loop.

    Sandboxed role subprocesses use the injected GH_TOKEN (no PEM) and skip
    the daemon-level clone loop; the daemon and unsandboxed runs use the
    PEM-based registry and run the loop.

    Returns:
        ``(identity, run_startup_clone)``.
    """
    from foreman.v4.identity import EnvTokenIdentity, V4IdentityRegistry

    if sandboxed:
        return EnvTokenIdentity(), False
    return (
        V4IdentityRegistry(
            apps=config.apps,
            orchestrator=config.orchestrator,
            installation_repo=projects[0].repo,
        ),
        True,
    )
```

Add the typing imports under the existing `TYPE_CHECKING` block (or create one) so the annotations resolve without runtime PyGithub imports:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foreman.v4.bootstrap import IdentityProvider
    from foreman.v4.config import V4Config
    from foreman.v4.models import ProjectConfig  # adjust to ProjectConfig's real module
```

(Confirm `ProjectConfig`'s import path by grepping — `grep -rn "class ProjectConfig" packages/foreman/src`.)

In `main()`, replace the direct `identity = V4IdentityRegistry(...)` block (`cli/__init__.py:249-253`) with:

```python
    sandboxed = os.environ.get("FOREMAN_SANDBOXED") == "1"
    identity, run_startup_clone = _select_identity(
        config=config, projects=projects, sandboxed=sandboxed
    )
```

Keep the existing `if not projects: raise ...` guard *before* this (both paths still require a project list; projects.toml is RO-bound in the box). Leave `_git_factory` unchanged — under `EnvTokenIdentity` its `role="orchestrator"` label is inert (the injected token is returned regardless); add a one-line comment saying so. Change the `bootstrap_cli_context(...)` call (`:268`) to pass `run_startup_clone=run_startup_clone`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_cli_select_identity.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Lint, format, commit**

```bash
uv run --no-sync ruff format packages/foreman/src/foreman/v4/cli/__init__.py packages/foreman/tests/v4/test_cli_select_identity.py
uv run --no-sync ruff check packages/foreman/src/foreman/v4/cli/__init__.py packages/foreman/tests/v4/test_cli_select_identity.py
uv run --no-sync mypy packages/foreman/src
git add packages/foreman/src/foreman/v4/cli/__init__.py packages/foreman/tests/v4/test_cli_select_identity.py
git commit -m "feat(v4): select EnvTokenIdentity + skip clone loop when FOREMAN_SANDBOXED"
```

---

### Task 4: `FOREMAN_SANDBOXED=1` marker in `build_argv`

**Files:**
- Modify: `packages/foreman/src/foreman/v4/sandbox.py` (the `setenv` dict in `build_argv`, ~`:161`)
- Test: `packages/foreman/tests/v4/test_sandbox.py` (add one argv assertion)

**Interfaces:**
- Consumes: existing `SandboxLauncher.build_argv`.
- Produces: every box argv now carries `--setenv FOREMAN_SANDBOXED 1`.

- [ ] **Step 1: Write the failing test**

Add to `packages/foreman/tests/v4/test_sandbox.py` (uses the file's existing `_argv()` and `_has_triple` helpers):

```python
def test_argv_marks_the_box_as_sandboxed() -> None:
    """The box must carry FOREMAN_SANDBOXED=1 so the role uses the injected
    GH_TOKEN identity (EnvTokenIdentity) instead of minting from a withheld
    PEM. Regression lock for the 2026-07-19 canary auth gap."""
    argv = _argv()
    assert _has_triple(argv, "--setenv", "FOREMAN_SANDBOXED", "1")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox.py::test_argv_marks_the_box_as_sandboxed -q --no-cov`
Expected: FAIL — assertion (no such triple in argv).

- [ ] **Step 3: Implement**

In `packages/foreman/src/foreman/v4/sandbox.py`, add the marker to the `setenv` dict `build_argv` builds (alongside `PATH`, `HOME`, `GH_TOKEN`, …):

```python
        setenv: dict[str, str] = {
            "PATH": SANDBOX_STD_PATH,
            "HOME": "/root",
            "PYTHONUNBUFFERED": "1",
            # Marks the box as sandboxed so foreman's CLI entrypoint uses the
            # injected GH_TOKEN (EnvTokenIdentity) and skips the daemon clone
            # loop — no PEM is available in the box.
            "FOREMAN_SANDBOXED": "1",
            ...  # existing keys unchanged
        }
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox.py -q --no-cov`
Expected: PASS — the new test plus all existing `build_argv` argv-shape tests (the marker is additive; `--clearenv` + existing `--setenv` order-independent assertions still hold).

- [ ] **Step 5: Lint, format, commit**

```bash
uv run --no-sync ruff format packages/foreman/src/foreman/v4/sandbox.py packages/foreman/tests/v4/test_sandbox.py
uv run --no-sync ruff check packages/foreman/src/foreman/v4/sandbox.py packages/foreman/tests/v4/test_sandbox.py
uv run --no-sync mypy packages/foreman/src
git add packages/foreman/src/foreman/v4/sandbox.py packages/foreman/tests/v4/test_sandbox.py
git commit -m "feat(v4): set FOREMAN_SANDBOXED=1 in the sandbox box env"
```

---

### Task 5: Hermetic real-bwrap test — sandbox identity needs no PEM

**Files:**
- Test: `packages/foreman/tests/v4/test_sandbox_integration.py` (add one case)

**Interfaces:**
- Consumes: `SandboxLauncher.build_argv` (with the Task 4 marker), `EnvTokenIdentity` (Task 1).
- Produces: a real-box regression lock proving the sandbox identity resolves a token with **no `/run/secrets`** present.

**Rationale:** the unit tests prove each piece; the #408 keystone proves the full CLI wiring. This test proves the core invariant in a *real* box: with no PEM mounted, the sandbox identity still yields a token. It self-skips off userns via the module's existing `pytestmark`.

- [ ] **Step 1: Write the test**

Add to `packages/foreman/tests/v4/test_sandbox_integration.py`:

```python
def test_sandbox_identity_resolves_token_with_no_pem(tmp_path: Path) -> None:
    """In a real bwrap box with NO /run/secrets bound, the sandbox identity
    must still resolve a token from the injected GH_TOKEN. Regression lock for
    the 2026-07-19 canary, where the box tried to read
    /run/secrets/orchestrator_pem and crash-failed."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    launcher = SandboxLauncher(cache_dir=str(cache_dir))
    probe = (
        "import os;"
        "assert not os.path.exists('/run/secrets'), 'PEM dir leaked into box';"
        "from foreman.v4.identity import EnvTokenIdentity;"
        "t = EnvTokenIdentity().get_role_token('orchestrator');"
        "print('TOKEN_OK' if t == os.environ['GH_TOKEN'] else 'TOKEN_BAD')"
    )
    argv = launcher.build_argv(
        role_token="ghs_INJECTED_TESTTOKEN",
        scratch_dir=str(scratch_dir),
        role_cmd=["python", "-c", probe],
    )
    r = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    assert "TOKEN_OK" in r.stdout
    # the PEM path must never appear in a failure trace
    assert "orchestrator_pem" not in (r.stdout + r.stderr)
    assert "/run/secrets" not in r.stderr
```

- [ ] **Step 2: Run it**

On a userns host (e.g. an ephemeral container from the `:dev` image — see Task 6 for the invocation): the case runs and passes. On CI / Windows it self-skips (`_userns_available()` is False). Run:
`uv run --no-sync pytest packages/foreman/tests/v4/test_sandbox_integration.py -q --no-cov`
Expected locally on Windows: `skipped`. In the container: `passed`.

- [ ] **Step 3: Lint, format, commit**

```bash
uv run --no-sync ruff format packages/foreman/tests/v4/test_sandbox_integration.py
uv run --no-sync ruff check packages/foreman/tests/v4/test_sandbox_integration.py
git add packages/foreman/tests/v4/test_sandbox_integration.py
git commit -m "test(v4): real-bwrap lock — sandbox identity resolves with no PEM"
```

---

## Roles-layer extension (Tasks 6-9; added after whole-branch review)

**Why:** the foundation (Tasks 1-5) fixes only the `main()` bootstrap layer, but role subcommands bypass it. These tasks give the `roles/*` layer a PEM-free identity so the box authenticates entirely on injected data. See the spec's "Extension: roles layer" section. Each task is TDD; run `uv run --no-sync` for everything; NO Co-Authored-By; lowercase conventional subjects; ruff + `mypy --strict` (unscoped: `uv run --no-sync mypy packages/foreman/src`) clean; `git add` only the named files.

### Task 6: `SandboxIdentityRegistry` + `V4IdentityRegistry.get_app_slug`

**Files:**
- Modify: `packages/foreman/src/foreman/v4/identity.py` (add `SandboxIdentityRegistry`; add public `get_app_slug` to `V4IdentityRegistry`)
- Test: `packages/foreman/tests/v4/test_sandbox_identity_registry.py` (create); extend `packages/foreman/tests/v4/test_identity.py` for `get_app_slug`

**Interfaces:**
- Consumes: `EnvTokenIdentity` (Task 1), `SandboxIdentityError` (Task 1), the existing private `V4IdentityRegistry._get_app_metadata(role) -> AppMetadata` (has `.slug`).
- Produces: `SandboxIdentityRegistry(EnvTokenIdentity)` with `get_app_slug(role: str) -> str` and `get_role_bot_logins() -> set[str]`; `V4IdentityRegistry.get_app_slug(role: str) -> str`.

- [ ] **Step 1 — failing tests.** Create `test_sandbox_identity_registry.py`:

```python
"""Unit tests for SandboxIdentityRegistry — env-backed identity for the box."""

from __future__ import annotations

import pytest

from foreman.v4.identity import SandboxIdentityError, SandboxIdentityRegistry


def test_get_role_token_returns_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghs_X")
    assert SandboxIdentityRegistry().get_role_token("planner") == "ghs_X"


def test_get_app_slug_returns_env_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOREMAN_BOT_SLUG", "my-planner-app")
    assert SandboxIdentityRegistry().get_app_slug("planner") == "my-planner-app"


def test_get_app_slug_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOREMAN_BOT_SLUG", raising=False)
    with pytest.raises(SandboxIdentityError):
        SandboxIdentityRegistry().get_app_slug("planner")


def test_get_role_bot_logins_parses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOREMAN_BOT_LOGINS", "a[bot] b[bot]  c[bot]")
    assert SandboxIdentityRegistry().get_role_bot_logins() == {"a[bot]", "b[bot]", "c[bot]"}


def test_get_role_bot_logins_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOREMAN_BOT_LOGINS", raising=False)
    with pytest.raises(SandboxIdentityError):
        SandboxIdentityRegistry().get_role_bot_logins()
```

For `get_app_slug` on the real registry, add to `test_identity.py` a test that mirrors the module's existing `get_role_bot_logins`/metadata tests (they already patch/fake `_get_app_metadata` or the `GET /app` fetch — reuse that exact fake) and asserts `registry.get_app_slug("planner")` returns the faked slug. Read the existing metadata test first; do not invent a new fake.

- [ ] **Step 2 — run, confirm fail** (`ImportError` / `AttributeError`).

- [ ] **Step 3 — implement.** In `identity.py` add:

```python
class SandboxIdentityRegistry(EnvTokenIdentity):
    """Env-backed registry for a sandboxed role subprocess.

    Extends :class:`EnvTokenIdentity` (token from ``GH_TOKEN``) with the two
    other pieces the role path needs, sourced from data the daemon injected
    into the box — never a PEM: the dispatched role's bot slug
    (``FOREMAN_BOT_SLUG``, for commit attribution) and the set of role-bot
    logins (``FOREMAN_BOT_LOGINS``, whitespace-separated, for self-comment
    filtering). Duck-types the role-facing surface of
    :class:`V4IdentityRegistry` used by ``build_role_resources`` and the role
    delegates. Fail-closed (:class:`SandboxIdentityError`) if a needed var is
    absent.
    """

    def get_app_slug(self, role: str) -> str:
        slug = os.environ.get("FOREMAN_BOT_SLUG")
        if not slug:
            raise SandboxIdentityError(
                "FOREMAN_SANDBOXED is set but FOREMAN_BOT_SLUG is unset; the "
                "sandboxed role has no bot slug for commit attribution. The "
                "dispatcher must inject it. Refusing to run."
            )
        return slug

    def get_role_bot_logins(self) -> set[str]:
        raw = os.environ.get("FOREMAN_BOT_LOGINS")
        if not raw:
            raise SandboxIdentityError(
                "FOREMAN_SANDBOXED is set but FOREMAN_BOT_LOGINS is unset; the "
                "sandboxed role cannot filter bot self-comments. The dispatcher "
                "must inject it. Refusing to run."
            )
        return {tok for tok in raw.split() if tok}
```

And add to `V4IdentityRegistry`, next to `get_role_bot_logins`:

```python
    def get_app_slug(self, role: str) -> str:
        """Return the role App's bot slug, fetched via ``GET /app`` and cached."""
        return self._get_app_metadata(role).slug
```

- [ ] **Step 4 — run, confirm pass.** Full `test_identity.py` + the new file; `mypy` unscoped; ruff+format.

- [ ] **Step 5 — commit** (`feat(v4): SandboxIdentityRegistry + V4IdentityRegistry.get_app_slug`).

### Task 7: `build_role_resources` sources slug via the registry

**Files:**
- Modify: `packages/foreman/src/foreman/roles/__init__.py` (`build_role_resources`: drop the direct `fetch_app_metadata` + `private_key_path`; use `registry.get_app_slug(role)`)
- Modify: the four call sites passing `private_key_path=` to `build_role_resources` (`roles/planner.py`, `reviewer.py`, `fixer.py`, `worker.py`) — remove that kwarg.
- Test: extend the existing `build_role_resources` test.

**Interfaces:**
- Consumes: `registry.get_app_slug(role)` (Task 6 — on both registries).
- Produces: `build_role_resources(*, registry, role, app_id) -> tuple[GitHostProvider, str, Github]` (`private_key_path` REMOVED).

- [ ] **Step 1 — failing test.** Find the existing `build_role_resources` test (grep `build_role_resources` under `packages/foreman/tests/`). Update it so the fake `registry` also provides `get_app_slug(role) -> "<slug>"`, drop `private_key_path`, and assert the returned `BotIdentity.slug` comes from `registry.get_app_slug`, and that `fetch_app_metadata` is NOT called (patch it to raise). Fails against the current signature.

- [ ] **Step 2 — run, confirm fail.**

- [ ] **Step 3 — implement.** In `roles/__init__.py`, change the body to:

```python
    token = registry.get_role_token(role)
    client = Github(auth=Auth.Token(token))
    slug = registry.get_app_slug(role)
    identity = BotIdentity(slug=slug, user_id=app_id, token=token)
    host = GitHubProvider(identity=identity, client=client)
    return host, token, client
```

Remove `private_key_path` from the signature, remove the now-unused `fetch_app_metadata` import if nothing else in the file uses it (check), and update the docstring (no more `GET /app` fetch — slug comes from the registry). Then remove `private_key_path=...` from the four call sites (grep `build_role_resources(` in `roles/`).

- [ ] **Step 4 — run, confirm pass.** Role tests touching `build_role_resources` + `mypy` unscoped + ruff.

- [ ] **Step 5 — commit** (`refactor(v4): build_role_resources sources bot slug via registry`).

### Task 8: role delegates select `SandboxIdentityRegistry` when sandboxed

**Files:**
- Modify: `roles/planner.py`, `reviewer.py`, `fixer.py`, `worker.py` (the `V4IdentityRegistry(...)` construction in each delegate).
- Add: shared helper `role_identity(config, *, installation_repo)` in `roles/__init__.py`.
- Test: `packages/foreman/tests/` — a `role_identity` selection test.

**Interfaces:**
- Consumes: `SandboxIdentityRegistry` (Task 6), `V4IdentityRegistry`.
- Produces: `role_identity(config, *, installation_repo: str)`; the four delegates call it instead of constructing `V4IdentityRegistry` directly.

- [ ] **Step 1 — failing test.** `test_role_identity_selection.py`: with `FOREMAN_SANDBOXED=1` monkeypatched → `role_identity(MagicMock(), installation_repo="o/r")` returns a `SandboxIdentityRegistry`; unset → a `V4IdentityRegistry`. Mirror Task 3's `_select_identity` test shape.

- [ ] **Step 2 — run, confirm fail.**

- [ ] **Step 3 — implement.** Add to `roles/__init__.py`:

```python
def role_identity(config: Any, *, installation_repo: str) -> Any:
    """Return the identity registry a role subprocess should use.

    Sandboxed (``FOREMAN_SANDBOXED=1``) -> :class:`SandboxIdentityRegistry`
    (env-backed, no PEM). Otherwise the PEM-based
    :class:`~foreman.v4.identity.V4IdentityRegistry`.
    """
    import os

    from foreman.v4.identity import SandboxIdentityRegistry, V4IdentityRegistry

    if os.environ.get("FOREMAN_SANDBOXED") == "1":
        return SandboxIdentityRegistry()
    return V4IdentityRegistry(
        apps=config.apps,
        orchestrator=config.orchestrator,
        installation_repo=installation_repo,
    )
```

In each of the four role modules, replace `registry = V4IdentityRegistry(apps=..., orchestrator=..., installation_repo=<X>)` with `registry = role_identity(config, installation_repo=<X>)` — read each site (grep `V4IdentityRegistry(` per file) and preserve the exact `installation_repo` expression it passes today. Import `role_identity` from `foreman.roles`. If a module no longer references `V4IdentityRegistry` directly, drop that import (ruff).

- [ ] **Step 4 — run, confirm pass.** Each role's test module + `mypy` unscoped + ruff.

- [ ] **Step 5 — commit** (`feat(v4): role delegates use SandboxIdentityRegistry when sandboxed`).

### Task 9: dispatcher injects `FOREMAN_BOT_SLUG` + `FOREMAN_BOT_LOGINS`

**Files:**
- Modify: `packages/foreman/src/foreman/v4/subprocess_dispatcher.py` (inject the two vars in sandbox mode).
- Possibly modify the daemon's dispatcher-construction site so the dispatcher can source the metadata from the daemon's registry.
- Test: extend the dispatcher / sandbox-dispatch tests.

**Interfaces:**
- Consumes: `get_app_slug` + `get_role_bot_logins` on the daemon's registry (Task 6); `build_argv` `passthrough`.
- Produces: in sandbox mode the box env carries `FOREMAN_BOT_SLUG=<role slug>` and `FOREMAN_BOT_LOGINS="<all four bot logins>"`.

- [ ] **Step 1 — failing test.** In the dispatcher test, drive a sandboxed dispatch with a fake identity exposing `get_app_slug`/`get_role_bot_logins`, and assert the built argv (or passthrough) contains `--setenv FOREMAN_BOT_SLUG <slug>` and `--setenv FOREMAN_BOT_LOGINS "<logins>"`. Reuse the existing sandboxed-dispatch test construction (grep `FOREMAN_SANDBOXED` / `build_argv` in the dispatcher tests).

- [ ] **Step 2 — run, confirm fail.**

- [ ] **Step 3 — implement.** In `dispatch()` (near `env["GH_TOKEN"] = self._identity.get_role_token(role)`), on the sandbox branch compute:

```python
        sandbox_setenv = {
            "FOREMAN_BOT_SLUG": self._identity.get_app_slug(role),
            "FOREMAN_BOT_LOGINS": " ".join(sorted(self._identity.get_role_bot_logins())),
        }
```

and merge into the `passthrough` handed to `build_argv` (with whatever passthrough is already assembled). If `self._identity`'s static type doesn't expose the two methods, read the dispatcher construction site and either type it to the concrete `V4IdentityRegistry` there or thread a small `bot_metadata` provider — pick the minimal change and note it in the report. Ensure this runs ONLY on the sandbox branch (never unsandboxed dispatch).

- [ ] **Step 4 — run, confirm pass.** Dispatcher test module + `mypy` unscoped + ruff.

- [ ] **Step 5 — commit** (`feat(v4): inject bot slug + logins into the sandbox box`).

### Task 10: canary keystone (manual) — supersedes the old Task 6

Same as the earlier "Task 6: Canary keystone" section — merge to main, rebuild `:dev`, run the in-container hermetic suite, flip the flag on the deploy host, label agent_core #408, and assert it advances PAST Planning with no `/run/secrets/*_pem` error in the planner log. This now exercises the full roles-layer path, not just bootstrap.
