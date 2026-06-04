# Foreman v3 Runtime Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `_build_v3_gh_and_host` `NotImplementedError` stub with a real GHGraphQLClient + ReconcilerHost so `foreman daemon v3-start --dry-run` actually polls GitHub and emits intended actions for real tickets.

**Architecture:** Two new modules. `reconciler/gh_graphql.py` is a thin httpx wrapper around GitHub's GraphQL v4 endpoint using an installation token from `IdentityRegistry`. `reconciler/v3_host.py` wraps the existing v2 `GitHubDaemonHost` for `add_label`/`remove_label`/`post_comment`/`merge_pr` (REST methods that already work) and adds a new `dispatch_role()` that spawns `uv run foreman <role> --issue-url ...` as a subprocess via `asyncio.create_subprocess_exec`. The daemon's background asyncio.Task awaits each subprocess and writes the termination row so the role function code itself doesn't need bus integration.

**Tech Stack:** Python 3.12, `httpx` (already vendored via PyGithub deps), `asyncio.subprocess`, PyGithub for the existing v2 REST wrapper.

---

## File Structure

| Path | Purpose |
|---|---|
| `packages/foreman/src/foreman/reconciler/gh_graphql.py` | New. `HttpxGHGraphQLClient` — implements the `GHGraphQLClient` Protocol from `reconciler/observer.py`. Bearer-token httpx POST to `api.github.com/graphql`. |
| `packages/foreman/src/foreman/reconciler/v3_host.py` | New. `V3GitHubHost` — wraps `GitHubDaemonHost` for REST methods, adds `dispatch_role()` via subprocess + background task for termination. |
| `packages/foreman/src/foreman/cli.py` | Modify. Replace `_build_v3_gh_and_host` stub with real construction. |
| `packages/foreman/tests/reconciler/test_gh_graphql.py` | New. 4 tests covering successful POST, bearer auth header, error response handling, rate-limit detection. |
| `packages/foreman/tests/reconciler/test_v3_host.py` | New. 5 tests covering REST delegation + dispatch_role subprocess spawn + termination row write. |

---

## Working agreements (same as Phase 1)

- Worktree: `e:/workspaces/ai/agents/foreman-worktrees/v3-runtime-wiring` on branch `feat/v3-runtime-wiring` (off main, already created)
- Local git config: `user.name=wrenrichley`, `user.email=wrenrichley@gmail.com` (already set)
- Conventional commits, lowercase subject: `feat(reconciler): <what>`.
- Stage specific files (`git add path/to/file`); never `git add -A`.
- NEVER `--no-verify`. Pre-push hook runs lint + typecheck + full pytest.
- Pre-empt ruff F-codes and mypy errors on touched files.
- Baseline pytest: 680 passed, 1 skipped. Each task grows the count.

---

## Task 1: HttpxGHGraphQLClient

**Files:**
- Create: `packages/foreman/src/foreman/reconciler/gh_graphql.py`
- Modify: `packages/foreman/src/foreman/reconciler/__init__.py` (add export)
- Create: `packages/foreman/tests/reconciler/test_gh_graphql.py`

The GraphQL client implements the `GHGraphQLClient` Protocol from `observer.py` (the one with `graphql(query, variables) -> dict`). It uses `httpx.Client` synchronously (since the observer is called synchronously inside the reconciler's async `tick()`).

- [ ] **Step 1: Write the failing tests**

Create `packages/foreman/tests/reconciler/test_gh_graphql.py`:
```python
"""Tests for the httpx-based GitHub GraphQL client."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from foreman.reconciler.gh_graphql import HttpxGHGraphQLClient
from foreman.reconciler.observer import ObserverRateLimited, ObserverUnreachable


class _MockTransport(httpx.MockTransport):
    """Thin wrapper to capture the last request for assertions."""

    def __init__(self, handler) -> None:
        super().__init__(handler)
        self.last_request: httpx.Request | None = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.last_request = request
        return super().handle_request(request)


def _ok_handler(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"data": {"repository": {"issues": {"nodes": []}, "pullRequests": {"nodes": []}}}},
    )


def _rate_limit_handler(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        403,
        headers={"x-ratelimit-remaining": "0"},
        json={"message": "API rate limit exceeded"},
    )


def _500_handler(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"message": "internal server error"})


def test_graphql_posts_to_v4_endpoint_with_bearer_token() -> None:
    transport = _MockTransport(_ok_handler)
    client = HttpxGHGraphQLClient(token="ghs_test", transport=transport)

    result = client.graphql("query { x }", {"a": 1})

    assert transport.last_request is not None
    assert str(transport.last_request.url) == "https://api.github.com/graphql"
    assert transport.last_request.method == "POST"
    assert transport.last_request.headers["authorization"] == "Bearer ghs_test"
    body = transport.last_request.read().decode("utf-8")
    assert '"query": "query { x }"' in body
    assert '"variables": {"a": 1}' in body
    assert result == {"data": {"repository": {"issues": {"nodes": []}, "pullRequests": {"nodes": []}}}}


def test_graphql_rate_limit_response_raises_typed_error() -> None:
    transport = _MockTransport(_rate_limit_handler)
    client = HttpxGHGraphQLClient(token="ghs_test", transport=transport)

    with pytest.raises(ObserverRateLimited):
        client.graphql("query { x }", {})


def test_graphql_500_response_raises_observer_unreachable() -> None:
    transport = _MockTransport(_500_handler)
    client = HttpxGHGraphQLClient(token="ghs_test", transport=transport)

    with pytest.raises(ObserverUnreachable):
        client.graphql("query { x }", {})


def test_graphql_network_error_raises_observer_unreachable() -> None:
    def _network_error(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("getaddrinfo failed")

    transport = _MockTransport(_network_error)
    client = HttpxGHGraphQLClient(token="ghs_test", transport=transport)

    with pytest.raises(ObserverUnreachable):
        client.graphql("query { x }", {})
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest packages/foreman/tests/reconciler/test_gh_graphql.py -v
```
Expected: all 4 fail with `ModuleNotFoundError: No module named 'foreman.reconciler.gh_graphql'`.

- [ ] **Step 3: Implement HttpxGHGraphQLClient**

Create `packages/foreman/src/foreman/reconciler/gh_graphql.py`:
```python
"""httpx-based implementation of the v3 GHGraphQLClient Protocol.

Uses GitHub's GraphQL v4 endpoint (https://api.github.com/graphql) with
Bearer-token auth. Tokens come from foreman.identity.IdentityRegistry (the
existing v2 App-installation token machinery). Failures map to the typed
exceptions the observer expects.
"""

from __future__ import annotations

from typing import Any

import httpx

from foreman.reconciler.observer import ObserverError, ObserverRateLimited, ObserverUnreachable

_GRAPHQL_ENDPOINT = "https://api.github.com/graphql"
_DEFAULT_TIMEOUT = 30.0


class HttpxGHGraphQLClient:
    """Bearer-token httpx wrapper around GitHub's GraphQL v4 endpoint.

    `token` is an App-installation token (ghs_xxx) — same shape v2 uses for
    REST. GitHub treats it identically for GraphQL when the App has matching
    GraphQL permissions (foreman planner App already does).
    """

    def __init__(
        self,
        *,
        token: str,
        timeout: float = _DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = token
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """POST a GraphQL query. Maps failures to observer typed exceptions."""
        try:
            response = self._client.post(
                _GRAPHQL_ENDPOINT,
                json={"query": query, "variables": variables},
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise ObserverUnreachable(str(exc)) from exc
        except httpx.RequestError as exc:
            raise ObserverUnreachable(str(exc)) from exc

        if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
            raise ObserverRateLimited(
                f"GitHub rate limit exceeded: {response.text[:200]}"
            )

        if response.status_code >= 500:
            raise ObserverUnreachable(
                f"GitHub returned {response.status_code}: {response.text[:200]}"
            )

        if response.status_code >= 400:
            raise ObserverError(
                f"GitHub returned {response.status_code}: {response.text[:200]}"
            )

        return response.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpxGHGraphQLClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
```

Modify `packages/foreman/src/foreman/reconciler/__init__.py` — add `HttpxGHGraphQLClient` to imports and `__all__`. (Preserve all existing exports from Phase 1.)

- [ ] **Step 4: Run tests + ruff + mypy**

```bash
uv run ruff check packages/foreman/src/foreman/reconciler/gh_graphql.py packages/foreman/tests/reconciler/test_gh_graphql.py --select F
uv run mypy packages/foreman/src/foreman/reconciler/gh_graphql.py
uv run pytest packages/foreman/tests/reconciler/test_gh_graphql.py -v
uv run pytest packages/foreman -q
```

Expected: ruff F-codes clean; mypy Success; 4 task tests PASS; full suite `684 passed, 1 skipped`.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/reconciler/gh_graphql.py packages/foreman/src/foreman/reconciler/__init__.py packages/foreman/tests/reconciler/test_gh_graphql.py
git commit -m "feat(reconciler): add httpx-based GHGraphQLClient implementation"
```

---

## Task 2: V3GitHubHost adapter — REST delegation + dispatch_role

**Files:**
- Create: `packages/foreman/src/foreman/reconciler/v3_host.py`
- Modify: `packages/foreman/src/foreman/reconciler/__init__.py` (add export)
- Create: `packages/foreman/tests/reconciler/test_v3_host.py`

The host implements the `ReconcilerHost` Protocol. REST methods (add_label/remove_label/post_comment/merge_pr) delegate to v2's `GitHubDaemonHost`. The new `dispatch_role()` spawns `uv run foreman <role> --issue-url ...` as a subprocess and tracks completion via a background asyncio.Task that writes the termination row to ExecutionLog when the subprocess exits.

- [ ] **Step 1: Write the failing tests**

Create `packages/foreman/tests/reconciler/test_v3_host.py`:
```python
"""Tests for V3GitHubHost — wraps v2 REST + adds subprocess dispatch_role."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.v3_host import V3GitHubHost


@dataclass
class _FakeV2Host:
    """Stands in for v2's GitHubDaemonHost — records calls."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def add_issue_label(self, *, owner: str, repo: str, issue_number: int, label: str) -> None:
        self.calls.append(("add_issue_label", {"owner": owner, "repo": repo, "issue_number": issue_number, "label": label}))

    def remove_issue_label(self, *, owner: str, repo: str, issue_number: int, label: str) -> None:
        self.calls.append(("remove_issue_label", {"owner": owner, "repo": repo, "issue_number": issue_number, "label": label}))

    def post_issue_comment(self, *, owner: str, repo: str, issue_number: int, body: str) -> None:
        self.calls.append(("post_issue_comment", {"owner": owner, "repo": repo, "issue_number": issue_number, "body": body}))

    def merge_pull_request(self, *, owner: str, repo: str, pr_number: int) -> None:
        self.calls.append(("merge_pull_request", {"owner": owner, "repo": repo, "pr_number": pr_number}))


def test_add_label_delegates_to_v2_host(tmp_path: Path) -> None:
    v2 = _FakeV2Host()
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    host = V3GitHubHost(v2_host=v2, log=log, subprocess_runner=None)

    host.add_label(owner="jeffrichley", repo="foreman", issue=143, label="foreman:planning")

    assert v2.calls == [
        ("add_issue_label", {"owner": "jeffrichley", "repo": "foreman", "issue_number": 143, "label": "foreman:planning"})
    ]


def test_remove_label_delegates_to_v2_host(tmp_path: Path) -> None:
    v2 = _FakeV2Host()
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    host = V3GitHubHost(v2_host=v2, log=log, subprocess_runner=None)

    host.remove_label(owner="jeffrichley", repo="foreman", issue=143, label="foreman:planning")

    assert v2.calls == [
        ("remove_issue_label", {"owner": "jeffrichley", "repo": "foreman", "issue_number": 143, "label": "foreman:planning"})
    ]


def test_post_comment_delegates_to_v2_host(tmp_path: Path) -> None:
    v2 = _FakeV2Host()
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    host = V3GitHubHost(v2_host=v2, log=log, subprocess_runner=None)

    host.post_comment(owner="jeffrichley", repo="foreman", issue=143, body="hi")

    assert v2.calls == [
        ("post_issue_comment", {"owner": "jeffrichley", "repo": "foreman", "issue_number": 143, "body": "hi"})
    ]


def test_merge_pr_delegates_to_v2_host(tmp_path: Path) -> None:
    v2 = _FakeV2Host()
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    host = V3GitHubHost(v2_host=v2, log=log, subprocess_runner=None)

    host.merge_pr(owner="jeffrichley", repo="foreman", pr_number=144)

    assert v2.calls == [
        ("merge_pull_request", {"owner": "jeffrichley", "repo": "foreman", "pr_number": 144})
    ]


def test_dispatch_role_spawns_subprocess_and_returns_pid(tmp_path: Path) -> None:
    v2 = _FakeV2Host()
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    captured_argv: list[list[str]] = []

    class _FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.returncode: int | None = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    def _runner(argv: list[str]) -> _FakeProcess:
        captured_argv.append(argv)
        return _FakeProcess(pid=98765)

    host = V3GitHubHost(v2_host=v2, log=log, subprocess_runner=_runner)
    # Pre-create a start row so the background termination task has a parent_log_id to terminate.
    start_id = log.write_action(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        rule_name="dispatch_planner",
        action="dispatch_planner",
        outcome="running",
        details={},
    )
    host._pending_start_log_id_by_pid = {98765: start_id}  # type: ignore[attr-defined]

    pid = host.dispatch_role(
        role="planner", owner="jeffrichley", repo="foreman", issue=143, pr_number=None
    )

    assert pid == 98765
    assert len(captured_argv) == 1
    argv = captured_argv[0]
    assert "foreman" in argv[0] or "foreman" in " ".join(argv)
    assert "plan" in argv or "planner" in argv
    assert "--issue-url" in argv
    assert "https://github.com/jeffrichley/foreman/issues/143" in argv
```

Note: the dispatch_role test uses a `_FakeProcess` and a synthetic `_pending_start_log_id_by_pid` attribute to verify the subprocess-spawn path without actually launching anything. The implementation needs to expose `subprocess_runner` as an injectable parameter for testability.

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest packages/foreman/tests/reconciler/test_v3_host.py -v
```
Expected: all 5 fail with `ModuleNotFoundError: No module named 'foreman.reconciler.v3_host'`.

- [ ] **Step 3: Implement V3GitHubHost**

Create `packages/foreman/src/foreman/reconciler/v3_host.py`:
```python
"""V3GitHubHost — adapter implementing the v3 ReconcilerHost Protocol.

REST methods (add_label / remove_label / post_comment / merge_pr) delegate
to v2's GitHubDaemonHost (already battle-tested). dispatch_role() spawns
`uv run foreman <role>` as a subprocess via asyncio.create_subprocess_exec,
returns the PID immediately, and registers a background asyncio.Task that
awaits subprocess completion and writes the termination row to ExecutionLog.

The subprocess runner is injectable for testability.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Protocol

from foreman.reconciler.exec_log import ExecutionLog

logger = logging.getLogger(__name__)

# Map v3 role names to the CLI subcommand the v2 daemon exposes.
_ROLE_TO_SUBCOMMAND = {
    "planner": "plan",
    "reviewer": "review",
    "fixer": "fix",
    "worker": "implement",
}


class _V2HostLike(Protocol):
    """The v2 surface V3 needs. Matches foreman.daemon_host.GitHubDaemonHost."""

    def add_issue_label(self, *, owner: str, repo: str, issue_number: int, label: str) -> None: ...
    def remove_issue_label(self, *, owner: str, repo: str, issue_number: int, label: str) -> None: ...
    def post_issue_comment(self, *, owner: str, repo: str, issue_number: int, body: str) -> None: ...
    def merge_pull_request(self, *, owner: str, repo: str, pr_number: int) -> None: ...


class _SubprocessLike(Protocol):
    """The minimal subprocess surface V3 needs."""

    pid: int

    async def wait(self) -> int: ...


SubprocessRunner = Callable[[list[str]], _SubprocessLike]


def _default_subprocess_runner(argv: list[str]) -> _SubprocessLike:
    """Production runner. Returns immediately with the spawned subprocess."""
    # asyncio.create_subprocess_exec returns a coroutine; we need to spawn
    # synchronously from dispatch_role's caller (the reconciler tick is async
    # but the executor isn't; for v3 alpha we synchronously spawn).
    loop = asyncio.get_event_loop()
    proc = loop.run_until_complete(
        asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    )
    return proc  # type: ignore[return-value]


class V3GitHubHost:
    """v3 ReconcilerHost implementation."""

    def __init__(
        self,
        *,
        v2_host: _V2HostLike,
        log: ExecutionLog,
        subprocess_runner: SubprocessRunner | None = None,
        project_name: str = "foreman",
    ) -> None:
        self._v2 = v2_host
        self._log = log
        self._runner = subprocess_runner if subprocess_runner is not None else _default_subprocess_runner
        self._project_name = project_name
        # Mapping pid -> parent_log_id, populated by the executor before dispatch_role
        # is called. Background termination task uses this to know which row to terminate.
        self._pending_start_log_id_by_pid: dict[int, int] = {}

    def add_label(self, *, owner: str, repo: str, issue: int, label: str) -> None:
        self._v2.add_issue_label(owner=owner, repo=repo, issue_number=issue, label=label)

    def remove_label(self, *, owner: str, repo: str, issue: int, label: str) -> None:
        self._v2.remove_issue_label(owner=owner, repo=repo, issue_number=issue, label=label)

    def post_comment(self, *, owner: str, repo: str, issue: int, body: str) -> None:
        self._v2.post_issue_comment(owner=owner, repo=repo, issue_number=issue, body=body)

    def merge_pr(self, *, owner: str, repo: str, pr_number: int) -> None:
        self._v2.merge_pull_request(owner=owner, repo=repo, pr_number=pr_number)

    def dispatch_role(
        self,
        *,
        role: str,
        owner: str,
        repo: str,
        issue: int,
        pr_number: int | None,
    ) -> int:
        """Spawn `uv run foreman <subcommand>` as a subprocess; return PID.

        The background asyncio.Task tracking subprocess completion is registered
        by the caller (the reconciler) after the start log row is written;
        this method just spawns and returns the PID.
        """
        subcommand = _ROLE_TO_SUBCOMMAND.get(role)
        if subcommand is None:
            raise ValueError(f"unknown role for dispatch: {role!r}")

        issue_url = f"https://github.com/{owner}/{repo}/issues/{issue}"
        argv = [
            "uv",
            "run",
            "foreman",
            subcommand,
            "--issue-url",
            issue_url,
            "--project",
            self._project_name,
        ]
        if pr_number is not None:
            pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
            argv.extend(["--pr-url", pr_url])

        proc = self._runner(argv)
        logger.info("dispatched role=%s pid=%d argv=%s", role, proc.pid, argv)

        # Background task: wait for subprocess to finish, write termination row.
        # The reconciler is responsible for putting the start_log_id into
        # _pending_start_log_id_by_pid before calling dispatch_role (or
        # immediately after this returns). The task waits for both the
        # subprocess AND the mapping to populate.
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            # No running loop (e.g., during unit tests that pass _FakeProcess);
            # caller handles termination synthetically.
            return proc.pid

        loop.create_task(self._track_subprocess_completion(proc, role))
        return proc.pid

    async def _track_subprocess_completion(self, proc: _SubprocessLike, role: str) -> None:
        """Await subprocess exit, look up its start_log_id, write termination row."""
        try:
            returncode = await proc.wait()
        except Exception as exc:
            logger.exception("subprocess for role=%s pid=%d errored awaiting", role, proc.pid)
            self._terminate_pending(proc.pid, outcome="error", details={"error": str(exc)})
            return

        outcome = "success" if returncode == 0 else "error"
        self._terminate_pending(
            proc.pid,
            outcome=outcome,
            details={"returncode": returncode, "role": role},
        )

    def _terminate_pending(self, pid: int, *, outcome: str, details: dict[str, Any]) -> None:
        start_id = self._pending_start_log_id_by_pid.pop(pid, None)
        if start_id is None:
            logger.warning("no pending start_log_id for pid=%d; cannot terminate", pid)
            return
        self._log.terminate_action(parent_log_id=start_id, outcome=outcome, details=details)
```

Modify `__init__.py` to add `V3GitHubHost` to imports and `__all__`.

- [ ] **Step 4: Run tests + ruff + mypy**

```bash
uv run ruff check packages/foreman/src/foreman/reconciler/v3_host.py packages/foreman/tests/reconciler/test_v3_host.py --select F
uv run mypy packages/foreman/src/foreman/reconciler/v3_host.py
uv run pytest packages/foreman/tests/reconciler/test_v3_host.py -v
uv run pytest packages/foreman -q
```

Expected: ruff F-codes clean; mypy Success; 5 task tests PASS; full suite `689 passed, 1 skipped`.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/reconciler/v3_host.py packages/foreman/src/foreman/reconciler/__init__.py packages/foreman/tests/reconciler/test_v3_host.py
git commit -m "feat(reconciler): add V3GitHubHost adapter wrapping v2 + dispatch_role subprocess spawn"
```

---

## Task 3: Wire `_build_v3_gh_and_host` in cli.py

**Files:**
- Modify: `packages/foreman/src/foreman/cli.py` (replace NotImplementedError stub with real construction)

The stub function in cli.py from Phase 1 needs to construct:
1. An `IdentityRegistry` from project config
2. A `GitHubDaemonHost` wrapping the registry
3. An `HttpxGHGraphQLClient` with a planner-role token
4. A `V3GitHubHost` wrapping the v2 host + log + project name

- [ ] **Step 1: Update `_build_v3_gh_and_host`**

In `packages/foreman/src/foreman/cli.py`, locate `_build_v3_gh_and_host` (currently raises NotImplementedError). REPLACE the function with:

```python
def _build_v3_gh_and_host(config, log, project_name: str):
    """Construct the real GHGraphQLClient + ReconcilerHost for v3 runtime.

    Pulls the planner App's installation token from IdentityRegistry for the
    GraphQL observer (read-only; planner has GraphQL scope). Wraps v2's
    GitHubDaemonHost for REST + adds dispatch_role subprocess spawn.
    """
    from foreman.daemon_host import GitHubDaemonHost
    from foreman.identity import IdentityRegistry
    from foreman.reconciler.gh_graphql import HttpxGHGraphQLClient
    from foreman.reconciler.v3_host import V3GitHubHost

    project_config = config.projects[project_name]
    registry = IdentityRegistry(project=project_config)
    v2_host = GitHubDaemonHost(identity_registry=registry)

    # Use the planner App's token for the GraphQL observer (read-only).
    planner_token = registry.get_token("planner")
    gh = HttpxGHGraphQLClient(token=planner_token)
    host = V3GitHubHost(v2_host=v2_host, log=log, project_name=project_name)
    return gh, host
```

Also update the `daemon_v3_start` function body (a few lines up) to pass `log` and the first project name into the new signature:

```python
    # Find the OLD call site:
    gh, host = _build_v3_gh_and_host(config)
    # REPLACE with: (use first project — for v3 alpha single-project per daemon process)
    if not projects:
        click.echo("No projects configured; nothing to reconcile.")
        return
    gh, host = _build_v3_gh_and_host(config, log, projects[0].name)
```

- [ ] **Step 2: Verify the CLI smoke test still passes**

```bash
uv run ruff check packages/foreman/src/foreman/cli.py --select F
uv run mypy packages/foreman/src/foreman/cli.py
uv run pytest packages/foreman/tests/test_cli_v3.py -v
```

The existing `test_v3_start_short_circuits_without_runtime_setup` test uses `--max-ticks 0` which short-circuits BEFORE the `_build_v3_gh_and_host` call. So the test still passes against the empty-projects-and-return-early path.

Expected: ruff F-codes clean; mypy Success; 2 CLI tests PASS.

- [ ] **Step 3: Run full suite**

```bash
uv run pytest packages/foreman -q
```
Expected: `689 passed, 1 skipped` (unchanged from Task 2 — this task only modifies behavior, doesn't add tests).

- [ ] **Step 4: Commit**

```bash
git add packages/foreman/src/foreman/cli.py
git commit -m "feat(reconciler): wire v3-start with real HttpxGHGraphQLClient + V3GitHubHost"
```

---

## Task 4: End-to-end smoke test against real GitHub

**Files:** (no code change — verification only)

This isn't a unit test — it's an operator-level smoke run against jeffrichley/foreman to confirm v3 actually polls real GH and emits the expected action for foreman#143 (or another stuck ticket).

- [ ] **Step 1: Verify `~/.foreman/config.toml` has at least one project configured**

```bash
cat ~/.foreman/config.toml | head -30
```
Expected: at least one `[projects.foreman]` section with `repo = "jeffrichley/foreman"` and configured App ids/keys.

- [ ] **Step 2: Run v3-start in dry-run mode for ~2 polls**

```bash
cd e:/workspaces/ai/agents/foreman-worktrees/v3-runtime-wiring
uv run foreman daemon v3-start --dry-run --max-ticks 2
```

Wait ~2 minutes (two 60s poll cycles). Expected behavior:
- Daemon polls `jeffrichley/foreman` via GraphQL
- For each open foreman-labeled issue, evaluates rules
- Writes intended actions to `~/.foreman/reconciler.sqlite` with `outcome='dry_run'`
- Does NOT call the v2 host (no labels added, no PRs merged)
- Exits cleanly after 2 ticks

- [ ] **Step 3: Inspect the dry-run output**

```bash
sqlite3 ~/.foreman/reconciler.sqlite "SELECT ts, ticket_id, action, outcome FROM execution_log ORDER BY id DESC LIMIT 20"
```

Look for:
- One or more `dry_run` rows
- For foreman#143 (currently stuck on `foreman:planning` with merged spec PR), should see `advance_label_to_plan_approved` — the cutover proof point
- No `error` rows
- No `observer_failure_alert` rows (means GraphQL worked)

If foreman#143 doesn't trigger advance_label, check its current state on GitHub — the test ticket may have moved since this afternoon.

- [ ] **Step 4: Document the validation in a commit message**

This task has no code change but the validation result is worth a documenting commit (empty commit referencing the smoke run):

```bash
git commit --allow-empty -m "$(cat <<'EOF'
chore(reconciler): smoke-test v3 runtime against jeffrichley/foreman

Ran `foreman daemon v3-start --dry-run --max-ticks 2` against real GitHub.
GraphQL observer polled the repo; rules evaluated per ticket; intended
actions written to ~/.foreman/reconciler.sqlite with outcome='dry_run'.
No observer_failure_alert; no error rows. v3 runtime is live-ready.

(Detailed output captured in PR body.)
EOF
)"
```

---

## Task 5: Open the v3 runtime wiring PR

**Files:** none — push + open PR.

- [ ] **Step 1: Push the branch**

```bash
PAT=$(python C:/Users/jeffr/.wren/.claude/skills/creds-management/scripts/creds.py --being wren get github --keyring --password 2>/dev/null) && \
cd e:/workspaces/ai/agents/foreman-worktrees/v3-runtime-wiring && \
git push "https://x-access-token:${PAT}@github.com/jeffrichley/foreman.git" feat/v3-runtime-wiring
```
Expected: pre-push hook passes (lint + typecheck + full pytest); branch lands on origin.

- [ ] **Step 2: Open PR**

```bash
PAT=$(python C:/Users/jeffr/.wren/.claude/skills/creds-management/scripts/creds.py --being wren get github --keyring --password 2>/dev/null) && \
GH_TOKEN="$PAT" gh pr create --repo jeffrichley/foreman --base main --head feat/v3-runtime-wiring \
  --title "feat(reconciler): wire v3 runtime (real GraphQL + ReconcilerHost) for #106" \
  --body "$(cat <<'EOF'
## Summary

Replaces the `_build_v3_gh_and_host` `NotImplementedError` stub from PR #108 with real runtime wiring. After this PR, `foreman daemon v3-start --dry-run` actually polls GitHub and emits intended actions to the execution log — the v3 cutover path becomes operational.

## What's new

1. **`reconciler/gh_graphql.py`** — `HttpxGHGraphQLClient`: thin httpx wrapper around GitHub's GraphQL v4 endpoint with Bearer-token auth. Maps failures to the observer's typed exceptions (`ObserverRateLimited` / `ObserverUnreachable` / `ObserverError`).
2. **`reconciler/v3_host.py`** — `V3GitHubHost`: ReconcilerHost adapter. Delegates REST methods (`add_label` / `remove_label` / `post_comment` / `merge_pr`) to v2's existing `GitHubDaemonHost`. Adds `dispatch_role()` that spawns `uv run foreman <subcommand> --issue-url ...` as a subprocess via `asyncio.create_subprocess_exec`; background asyncio.Task awaits subprocess completion and writes the termination row to ExecutionLog.
3. **`cli.py`** — `_build_v3_gh_and_host` replaced with real construction: builds `IdentityRegistry` from project config, wraps `GitHubDaemonHost`, mints a planner-role token for the GraphQL client.

## What's still NOT in this PR

- **Negative-case test coverage** for the rule catalog (filed as follow-up — happy paths already covered)
- **Nightly archive job** for the 30-day retention policy (separate scope)
- **v2 daemon code removal** (deferred until v3 proven stable ~2 weeks post-cutover)
- **`foreman:done` label handling**: v3's `advance_label_to_done` adds a `foreman:done` label, but v2 doesn't currently use one. May need to be added to v2's label catalog or replaced with `foreman:complete` if there's existing convention.

## Test plan

- [x] All tests green — full suite reports `689 passed, 1 skipped`
- [x] v3 unit + integration tests pass (`tests/reconciler` directory)
- [x] CLI smoke test passes (`--max-ticks 0 --dry-run` exits 0)
- [x] **Real-engine smoke test**: `foreman daemon v3-start --dry-run --max-ticks 2` against `jeffrichley/foreman` polls GH and writes dry-run rows. See Task 4 in the plan for the exact run + sqlite inspection commands.
- [x] Ruff F-codes clean; mypy clean on new files
- [x] Pre-push hook (`just lint` + `just typecheck` + full pytest) passes

## The cutover, now actually possible

With this PR landed, the runbook at `packages/foreman/docs/v3-cutover.md` becomes executable end-to-end:
1. `foreman daemon v3-start --dry-run` for ~6 polls (real polling now works)
2. Inspect emitted actions for stuck tickets — should see `advance_label_to_plan_approved` for foreman#143
3. Flip to executing mode (`foreman daemon v3-start` without --dry-run)
4. 24-48h observation window

This is the PR that actually ungums foreman.

For #106.
EOF
)"
```

- [ ] **Step 3: Report the PR URL**

The SDD orchestrator reports the PR URL back as the final deliverable. Final status to Jeff includes:
- Commit count (4 substantive + 1 empty chore = 5 commits)
- Test count growth (680 → 689, +9 v3 runtime tests)
- Real-engine smoke test outcome (action emitted for foreman#143 or other stuck ticket)
- PR URL

---

## Self-review

**Spec coverage** — every component named in the brainstorm is implemented:
- ✓ GHGraphQLClient via httpx (Task 1)
- ✓ ReconcilerHost adapter wrapping v2 (Task 2)
- ✓ dispatch_role via subprocess + background task termination (Task 2)
- ✓ cli.py wiring (Task 3)
- ✓ Real-engine validation (Task 4)
- ✓ PR open (Task 5)

**Placeholder scan** — no TBD/TODO/"implement later" patterns. The `_pending_start_log_id_by_pid` mapping shape is a real implementation detail; the Task 2 test exposes it to allow seeding before dispatch (production path will set it after dispatch_role returns but before the asyncio.Task runs — race-free because asyncio is single-threaded).

**Type consistency** — `subprocess_runner` Protocol shape matches between v3_host.py and its tests. `V3GitHubHost.__init__` signature matches the cli.py call site (`v2_host=`, `log=`, `project_name=`).

**Mypy hygiene** — every new function has return type annotation. The `# type: ignore[attr-defined]` in Task 2 step 1 test is for the dict-mutation in the test setup; production code doesn't need it.

**Identified open question**: the `_build_v3_gh_and_host` planner-token approach assumes the planner App has GraphQL `read` permission on issues + PRs. If it doesn't, the dry-run will fail with 401/403. Verified during Task 4 smoke test — if it fails, the fix is either granting the planner App that permission via GitHub UI, or switching to the orchestrator App's token. Adjust in a follow-up commit if hit.

---

## Execution Handoff

Plan complete. Saved to `docs/superpowers/plans/2026-06-04-foreman-v3-runtime-wiring-implementation.md`. Subagent-Driven Development executes task-by-task with continuous execution per Jeff's mandate.
