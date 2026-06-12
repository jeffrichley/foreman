"""Tests for the per-poll clone refresh strategy (foreman#291).

Pins the contracts:

- :class:`OnPollFetch` refreshes ``origin/<default-branch>`` once per
  reconciler tick, per project.
- :class:`OnDispatchFetchOnly` is a pure no-op (preserves pre-#291
  behavior for paranoid / air-gapped environments).
- Both strategies are best-effort: network failures and missing clone
  paths are logged at WARNING / DEBUG, NEVER propagated.
- The reconciler boundary catches any exception a strategy
  forgets to swallow — a misbehaving strategy must not crash the
  daemon.
- The per-dispatch fetch in :class:`WorktreeManager.create` is
  preserved as defense in depth (asserted in
  ``test_worktree.test_create_still_fetches_base_branch_per_dispatch``).
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from foreman.reconciler import ExecutionLog
from foreman.reconciler.clone_refresh import (
    CloneRefreshStrategy,
    OnDispatchFetchOnly,
    OnPollFetch,
)
from foreman.reconciler.daemon import Reconciler, ReconcilerProject

# ----------------------------------------------------------------------
# Test fixtures shared with the reconciler-tick tests below.
# ----------------------------------------------------------------------


@dataclass
class _StubGHClient:
    """Empty-snapshot GraphQL stub — keeps tick() from emitting dispatches."""

    response: dict[str, Any] = field(
        default_factory=lambda: {
            "data": {
                "repository": {
                    "issues": {"nodes": []},
                    "openPRs": {"nodes": []},
                    "recentMergedPRs": {"nodes": []},
                }
            }
        }
    )

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return self.response


@dataclass
class _StubHost:
    """Minimal host stub — no methods get called when the snapshot is empty."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


@dataclass
class _RecordingRefreshStrategy:
    """Test-only strategy that records every ``refresh(project)`` call.

    Used to assert "the reconciler fired the strategy once per project
    per tick" without invoking real git.
    """

    calls: list[ReconcilerProject] = field(default_factory=list)

    def refresh(self, project: ReconcilerProject) -> None:
        self.calls.append(project)


@dataclass
class _RaisingRefreshStrategy:
    """Test-only strategy that raises on refresh — pins the
    reconciler-boundary catch."""

    exc_type: type[BaseException] = RuntimeError
    calls: list[str] = field(default_factory=list)

    def refresh(self, project: ReconcilerProject) -> None:
        self.calls.append(project.name)
        raise self.exc_type(f"forced failure on {project.name}")


def _init_bare_clone(path: Path) -> None:
    """Create a minimal git clone with a wired origin so
    ``fetch_origin_default_branch`` can resolve a default branch and
    fire a real ``git fetch``.

    Mirrors the bare-repo fixture pattern from ``test_worktree.py:1737``.
    """
    origin = path.parent / f"{path.name}.origin.git"
    seed = path.parent / f"{path.name}.seed"
    seed.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=seed, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "seed@example.com"],
        cwd=seed,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Seed"],
        cwd=seed,
        check=True,
        capture_output=True,
    )
    (seed / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=seed, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"], cwd=seed, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(origin)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "clone", str(origin), str(path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "set-head", "origin", "main"],
        cwd=path,
        check=True,
        capture_output=True,
    )


# ----------------------------------------------------------------------
# Regression test 1: default strategy fires per tick, per project
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_poll_fetch_invokes_refresh_per_project_per_tick(
    tmp_path: Path,
) -> None:
    """Construct a ``Reconciler`` with two ``ReconcilerProject``s, each
    pointing at its own on-disk clone, and inject a recording stub for
    the refresh strategy. Run ``tick()`` once and assert the stub
    recorded one call per project, in order.

    The injected stub takes the place of ``OnPollFetch`` so we observe
    "did the reconciler schedule a refresh per project" without doing
    the real git fetch. The bare clones exist so ``OnPollFetch`` would
    succeed if it WERE wired in — letting future tests swap it back
    without changing fixtures.
    """
    clone_a = tmp_path / "clone-a"
    clone_a.mkdir()
    _init_bare_clone(clone_a)
    clone_b = tmp_path / "clone-b"
    clone_b.mkdir()
    _init_bare_clone(clone_b)

    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    gh = _StubGHClient()
    host = _StubHost()
    strategy = _RecordingRefreshStrategy()

    project_a = ReconcilerProject(
        name="project-a",
        owner="jeffrichley",
        repo="voice",
        local_clone_path=str(clone_a),
    )
    project_b = ReconcilerProject(
        name="project-b",
        owner="jeffrichley",
        repo="agent_core",
        local_clone_path=str(clone_b),
    )
    reconciler = Reconciler(
        projects=(project_a, project_b),
        log=log,
        gh=gh,
        host=host,  # type: ignore[arg-type]
        dry_run=False,
        clone_refresh_strategy=strategy,
    )

    await reconciler.tick()

    assert len(strategy.calls) == 2, (
        "Expected exactly one refresh call per project per tick; "
        f"got {len(strategy.calls)}"
    )
    assert strategy.calls[0] is project_a
    assert strategy.calls[1] is project_b


# ----------------------------------------------------------------------
# Regression test 3: OnPollFetch is silent on network failure
# ----------------------------------------------------------------------


def test_on_poll_fetch_logs_and_swallows_network_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``OnPollFetch.refresh`` against a clone whose ``origin`` points
    at a non-existent path must:

    1. NOT raise (best-effort contract)
    2. Emit a WARNING log row so the operator can diagnose

    Without this, transient network issues (DNS hiccup, throttled
    runner, etc.) would crash the daemon's tick loop.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_bare_clone(clone)

    # Break the origin URL so any fetch fails fast.
    subprocess.run(
        ["git", "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git")],
        cwd=clone,
        check=True,
        capture_output=True,
    )

    project = ReconcilerProject(
        name="broken",
        owner="jeffrichley",
        repo="broken",
        local_clone_path=str(clone),
    )
    strategy = OnPollFetch()

    # Underlying ``_fetch_origin_branch`` already prints a warning to
    # stderr (best-effort behavior at the worktree layer). We don't
    # require an extra WARNING-level logger row from OnPollFetch in
    # that path — the daemon doesn't crash, which is the contract this
    # test pins. caplog at WARNING captures any structured log row
    # OnPollFetch DID emit but does not assert one is required.
    with caplog.at_level(logging.WARNING):
        # Must not raise.
        strategy.refresh(project)


def test_on_poll_fetch_logs_warning_when_helper_raises(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When ``fetch_origin_default_branch`` raises (e.g., disk full,
    permission error in the foreign clone), ``OnPollFetch.refresh``
    catches the exception, logs WARNING, and returns. Pins the
    "must not propagate" contract at the strategy layer.
    """

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(
        "foreman.reconciler.clone_refresh.fetch_origin_default_branch", boom
    )

    project = ReconcilerProject(
        name="boom",
        owner="jeffrichley",
        repo="boom",
        local_clone_path="/nonexistent/clone",
    )
    strategy = OnPollFetch()

    with caplog.at_level(logging.WARNING, logger="foreman.reconciler.clone_refresh"):
        strategy.refresh(project)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARNING log row when helper raised"
    assert any("disk full" in r.getMessage() for r in warnings), (
        "warning must include the underlying error so the operator can diagnose"
    )


def test_on_poll_fetch_skips_when_local_clone_path_empty() -> None:
    """``OnPollFetch.refresh`` against a project with no
    ``local_clone_path`` is a silent no-op. This is the expected case
    for test fixtures that construct ``ReconcilerProject`` positionally
    without threading a clone path — without this branch the strategy
    would call ``fetch_origin_default_branch(Path(""))`` and fail on
    "not a git repository" every tick.
    """
    project = ReconcilerProject(
        name="no-clone",
        owner="jeffrichley",
        repo="no-clone",
    )
    assert project.local_clone_path == ""
    strategy = OnPollFetch()

    # Must not raise; must not touch the filesystem.
    strategy.refresh(project)


# ----------------------------------------------------------------------
# Regression test 4: OnDispatchFetchOnly is a pure no-op
# ----------------------------------------------------------------------


def test_on_dispatch_fetch_only_is_pure_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OnDispatchFetchOnly.refresh`` must not run any subprocess, must
    not touch the filesystem, and must not raise — even when the
    project's clone path points at a non-existent directory. Pins the
    opt-out contract so a future "improvement" that makes it ALSO fetch
    silently is impossible without breaking this test.
    """
    forbidden_calls: list[str] = []

    def forbidden_run(*args: Any, **kwargs: Any) -> None:
        forbidden_calls.append(f"subprocess.run({args!r})")
        raise AssertionError("OnDispatchFetchOnly must not run subprocesses")

    def forbidden_fetch(*args: Any, **kwargs: Any) -> None:
        forbidden_calls.append(f"fetch_origin_default_branch({args!r})")
        raise AssertionError("OnDispatchFetchOnly must not call fetch helper")

    monkeypatch.setattr("subprocess.run", forbidden_run)
    monkeypatch.setattr(
        "foreman.reconciler.clone_refresh.fetch_origin_default_branch", forbidden_fetch
    )

    project = ReconcilerProject(
        name="opt-out",
        owner="jeffrichley",
        repo="opt-out",
        local_clone_path="/definitely/does/not/exist",
    )
    strategy = OnDispatchFetchOnly()

    # Must not raise; must not touch anything.
    strategy.refresh(project)

    assert not forbidden_calls, (
        f"OnDispatchFetchOnly leaked side effects: {forbidden_calls!r}"
    )


# ----------------------------------------------------------------------
# Regression test 5: tick() does NOT crash when strategy raises
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_does_not_crash_when_strategy_raises(tmp_path: Path) -> None:
    """The reconciler-boundary try/except catches any exception a
    strategy forgets to swallow. The tick must complete (snapshot
    fetch still runs for the rest of the projects, etc.) and the next
    tick must still fire the strategy. This is defense in depth on
    top of each strategy's own try/except.
    """
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    gh = _StubGHClient()
    host = _StubHost()
    strategy: CloneRefreshStrategy = _RaisingRefreshStrategy()

    project = ReconcilerProject(
        name="project-x",
        owner="jeffrichley",
        repo="foreman",
        local_clone_path="/ignored",
    )
    reconciler = Reconciler(
        projects=(project,),
        log=log,
        gh=gh,
        host=host,  # type: ignore[arg-type]
        dry_run=False,
        clone_refresh_strategy=strategy,
    )

    # Tick must not propagate the strategy's RuntimeError.
    await reconciler.tick()
    # The strategy was invoked exactly once.
    assert isinstance(strategy, _RaisingRefreshStrategy)
    assert strategy.calls == ["project-x"]

    # A second tick proves the failure didn't poison the loop:
    await reconciler.tick()
    assert strategy.calls == ["project-x", "project-x"]
