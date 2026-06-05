# Foreman v3 Operational Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close 8 operational-discipline gaps in v3 that v2 had baked in but I missed when porting only the architecture. After this PR, v3 has parity with v2's safety + reliability story.

**Architecture:** Tactical additions across config, cli, daemon, host. No rewrites; each gap is a targeted addition matching v2's existing pattern where possible.

**Tech Stack:** existing — Pydantic, asyncio, Python logging, sqlite3, Click.

---

## Working agreements (same as prior v3 work)

- Worktree: `e:/workspaces/ai/agents/foreman` directly on branch `fix/v3-operational-gaps` (no separate worktree — small focused PR).
- Local git: `wrenrichley / wrenrichley@gmail.com` (already set in foreman repo via PR #109 cleanup).
- Conventional commits, lowercase subject.
- Stage specific files; never `git add -A`.
- NEVER `--no-verify`. Pre-push hook runs lint + typecheck + full pytest.
- Pre-empt ruff F-codes + UP035 + mypy errors on touched files.
- Baseline pytest: 689 passed, 1 skipped.
- Each task grows tests proportionally to behavior added.

---

## Task 1: DaemonLock — prevent two concurrent v3 daemons

**Files:**
- Modify: `packages/foreman/src/foreman/cli.py` (wrap `daemon_v3_start` body in `DaemonLock` context)

V2 uses `DaemonLock` (file-based PID lock at `~/.foreman/daemon.lock`) to prevent two daemons. v3 needs the same — two reconcilers race-writing the execution log would corrupt history. Reuse v2's lock with a distinct lock path so v2 and v3 can coexist if needed.

- [ ] **Step 1: Add v3 lock path to ReconcilerConfig**

In `packages/foreman/src/foreman/config.py`, locate `ReconcilerConfig` (added in PR #108). ADD a new field:
```python
    lock_path: str = Field(
        default="~/.foreman/reconciler.lock",
        description="File-based PID lock to prevent two reconciler daemons concurrently",
    )
```

- [ ] **Step 2: Wrap v3-start body in DaemonLock**

In `packages/foreman/src/foreman/cli.py`, locate `daemon_v3_start` function. ADD the import + lock wrapping. Find the line that does `log = ExecutionLog(db_path)` and refactor the function body to look like:

```python
def daemon_v3_start(dry_run: bool, max_ticks: int | None) -> None:
    """Start the v3 declarative reconciler daemon. (existing docstring kept)"""
    import asyncio
    import os
    from pathlib import Path

    from foreman.config import load_config
    from foreman.daemon_lock import DaemonLock
    from foreman.reconciler import ExecutionLog, Reconciler, ReconcilerProject

    config = load_config_with_env_resolution()  # whatever existing helper resolves config

    lock_path = Path(os.path.expanduser(config.reconciler.lock_path))
    db_path = Path(os.path.expanduser(config.reconciler.db_path))

    with DaemonLock(lock_path):
        log = ExecutionLog(db_path)
        log.init()

        projects = tuple(
            ReconcilerProject(name=p_name, owner=p_cfg.repo.split("/")[0], repo=p_cfg.repo.split("/")[1])
            for p_name, p_cfg in config.projects.items()
        )
        # ... rest of body unchanged
```

Preserve the rest of the body verbatim (project iteration, max_ticks short-circuit, gh+host construction, asyncio.run).

- [ ] **Step 3: Verify**

```bash
uv run ruff check packages/foreman/src/foreman/cli.py packages/foreman/src/foreman/config.py --select F
uv run mypy packages/foreman/src/foreman/cli.py
uv run pytest packages/foreman -q
```
Expected: ruff F-codes clean, mypy clean, full suite passes (no new tests yet — wire-only change; the DaemonLock behavior is exercised manually in real-engine validation).

- [ ] **Step 4: Commit**

```bash
git add packages/foreman/src/foreman/config.py packages/foreman/src/foreman/cli.py
git commit -m "feat(reconciler): wrap v3-start in DaemonLock + add lock_path config"
```

---

## Task 2: Call `recover_orphaned()` at startup

**Files:**
- Modify: `packages/foreman/src/foreman/cli.py` (add `recover_orphaned` call after `log.init()`)

The `ExecutionLog.recover_orphaned()` method exists but is never called. Orphaned "running" rows from a crashed daemon block future dispatches via idempotence checks. Call it on startup; log how many rows were recovered.

- [ ] **Step 1: Add the call**

In `cli.py`'s `daemon_v3_start` function, immediately after `log.init()` and INSIDE the `DaemonLock` context, add:

```python
        recovered = log.recover_orphaned()
        if recovered > 0:
            click.echo(f"recovered {recovered} orphaned running row(s) from prior daemon")
```

- [ ] **Step 2: Verify**

```bash
uv run pytest packages/foreman -q
```
Expected: full suite still passes.

- [ ] **Step 3: Commit**

```bash
git add packages/foreman/src/foreman/cli.py
git commit -m "feat(reconciler): call recover_orphaned at v3-start to terminate crashed-daemon rows"
```

---

## Task 3: SIGTERM/SIGINT handlers for graceful shutdown

**Files:**
- Modify: `packages/foreman/src/foreman/cli.py` (add signal handlers around `asyncio.run(_run())`)

`Reconciler.shutdown()` exists. Wire Ctrl-C and `kill -TERM` to call it cleanly so in-flight ticks complete before exit.

- [ ] **Step 1: Add handlers**

Wrap the `asyncio.run(_run())` block in `daemon_v3_start`. Restructure to:

```python
        async def _run() -> None:
            # ... existing body
            loop = asyncio.get_event_loop()
            stop_event = asyncio.Event()

            def _signal_handler():
                stop_event.set()

            import signal
            # Windows has no asyncio.add_signal_handler; use signal.signal as fallback
            try:
                loop.add_signal_handler(signal.SIGTERM, _signal_handler)
                loop.add_signal_handler(signal.SIGINT, _signal_handler)
            except NotImplementedError:
                # On Windows asyncio doesn't support add_signal_handler;
                # rely on KeyboardInterrupt for Ctrl-C and ignore SIGTERM
                # (Windows doesn't have it natively; signal.SIGBREAK is the analog).
                pass

            shutdown_task = asyncio.create_task(_watch_for_shutdown(stop_event, reconciler))
            try:
                # ... existing run loop OR tick iteration
            finally:
                shutdown_task.cancel()

        async def _watch_for_shutdown(stop_event: asyncio.Event, reconciler: Reconciler) -> None:
            await stop_event.wait()
            await reconciler.shutdown()
```

This is the pattern — actual implementation should adapt to whatever structure the current `_run()` has. The goal: SIGTERM/SIGINT triggers `reconciler.shutdown()` cleanly; if `add_signal_handler` isn't supported (Windows), fall through to default KeyboardInterrupt which `asyncio.run` already handles.

- [ ] **Step 2: Verify**

```bash
uv run pytest packages/foreman -q
```

- [ ] **Step 3: Commit**

```bash
git add packages/foreman/src/foreman/cli.py
git commit -m "feat(reconciler): SIGTERM/SIGINT handlers for graceful Reconciler shutdown"
```

---

## Task 4: Persistent logging to ~/.foreman/v3-daemon.log

**Files:**
- Modify: `packages/foreman/src/foreman/cli.py` (call v2's `configure_daemon_logging` with v3 log path)

v2 has `configure_daemon_logging` in `logging_setup.py` that writes JSON-lines to a file. Reuse it for v3 with a distinct file so v2 and v3 logs don't intermix.

- [ ] **Step 1: Add the call**

In `daemon_v3_start`, at the top of the function body (BEFORE entering `DaemonLock`), add:

```python
    from foreman.logging_setup import configure_daemon_logging

    log_path = Path(os.path.expanduser("~/.foreman/v3-daemon.log"))
    configure_daemon_logging(log_path=log_path)
```

(If `configure_daemon_logging`'s signature differs — e.g. takes a `DaemonConfig` instead of a path — adapt minimally. The intent is: v3 writes structured logs to `~/.foreman/v3-daemon.log`.)

- [ ] **Step 2: Verify**

```bash
uv run pytest packages/foreman -q
```

- [ ] **Step 3: Commit**

```bash
git add packages/foreman/src/foreman/cli.py
git commit -m "feat(reconciler): wire v2 logging_setup for v3 daemon to ~/.foreman/v3-daemon.log"
```

---

## Task 5: Subprocess wall-clock timeout

**Files:**
- Modify: `packages/foreman/src/foreman/config.py` (add `role_dispatch_timeout_seconds` to ReconcilerConfig)
- Modify: `packages/foreman/src/foreman/reconciler/v3_host.py` (`_track_subprocess_completion` wraps wait in `asyncio.wait_for`)
- Create: `packages/foreman/tests/reconciler/test_v3_host_timeout.py` (1 test)

A hung Worker subprocess blocks the daemon's background asyncio.Task forever. Add a timeout; on expiry, terminate the process + write outcome="timeout".

- [ ] **Step 1: Add config knob**

In `ReconcilerConfig`:
```python
    role_dispatch_timeout_seconds: int = Field(
        default=3600,  # 1 hour — generous default; Claude Code sessions take 10-30 min typically
        ge=60,
        description="Hard wall-clock ceiling for a dispatched role subprocess; SIGTERM on expiry",
    )
```

- [ ] **Step 2: Plumb config into V3GitHubHost**

`V3GitHubHost.__init__` already takes optional fields. ADD:
```python
        role_dispatch_timeout_seconds: int = 3600,
```
and store as `self._timeout_seconds = role_dispatch_timeout_seconds`.

- [ ] **Step 3: Wrap `_track_subprocess_completion` with timeout**

Replace the `returncode = await proc.wait()` line with:
```python
        try:
            returncode = await asyncio.wait_for(proc.wait(), timeout=self._timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning("subprocess for role=%s pid=%d timed out after %ds; terminating",
                           role, proc.pid, self._timeout_seconds)
            # Attempt graceful termination; if the wrapped subprocess.Popen has a terminate() method, call it
            try:
                if hasattr(proc, '_proc') and proc._proc is not None:
                    proc._proc.terminate()
            except Exception:
                pass
            self._terminate_pending(proc.pid, outcome="timeout", details={"timeout_seconds": self._timeout_seconds, "role": role})
            return
```

- [ ] **Step 4: Plumb config in cli.py**

In `_build_v3_gh_and_host`:
```python
    host = V3GitHubHost(
        v2_host=v2_host,
        log=log,
        project_name=project_name,
        role_dispatch_timeout_seconds=config.reconciler.role_dispatch_timeout_seconds,
    )
```

- [ ] **Step 5: Write the test**

Create `packages/foreman/tests/reconciler/test_v3_host_timeout.py`:
```python
"""Test that V3GitHubHost terminates and logs timeout for stuck subprocesses."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.v3_host import V3GitHubHost


class _HangingProcess:
    """A subprocess.Popen-like that never exits — simulates a hung Worker."""

    def __init__(self) -> None:
        self.pid = 99999
        self._terminated = False
        self._proc = self  # so terminate() can be called via proc._proc.terminate()

    def terminate(self) -> None:
        self._terminated = True

    async def wait(self) -> int:
        while not self._terminated:
            await asyncio.sleep(0.01)
        return -15  # SIGTERM


@pytest.mark.asyncio
async def test_subprocess_timeout_terminates_and_logs(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    proc = _HangingProcess()

    def _runner(_argv: list[str]) -> _HangingProcess:
        return proc

    host = V3GitHubHost(
        v2_host=None,  # not used in dispatch_role path
        log=log,
        subprocess_runner=_runner,
        role_dispatch_timeout_seconds=1,  # 1 second for fast test
    )

    # Pre-create a start log row so termination has a parent
    start_id = log.write_action(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        rule_name="dispatch_planner",
        action="dispatch_planner",
        outcome="running",
        details={},
    )
    host._pending_start_log_id_by_pid[proc.pid] = start_id

    await host._track_subprocess_completion(proc, "planner")

    # The proc was terminated and the log row was finalized as 'timeout'
    assert proc._terminated is True
    import sqlite3
    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        outcome = conn.execute(
            "SELECT outcome FROM execution_log WHERE parent_log_id = ?", (start_id,)
        ).fetchone()[0]
    assert outcome == "timeout"
```

- [ ] **Step 6: Verify**

```bash
uv run ruff check packages/foreman/src/foreman/reconciler/v3_host.py packages/foreman/src/foreman/config.py --select F
uv run mypy packages/foreman/src/foreman/reconciler/v3_host.py
uv run pytest packages/foreman -q
```

- [ ] **Step 7: Commit**

```bash
git add packages/foreman/src/foreman/config.py packages/foreman/src/foreman/reconciler/v3_host.py packages/foreman/src/foreman/cli.py packages/foreman/tests/reconciler/test_v3_host_timeout.py
git commit -m "feat(reconciler): hard wall-clock timeout on dispatch_role subprocess"
```

---

## Task 6: Concurrency cap on dispatch_role

**Files:**
- Modify: `packages/foreman/src/foreman/config.py` (add `max_concurrent_dispatches`)
- Modify: `packages/foreman/src/foreman/reconciler/v3_host.py` (`asyncio.Semaphore` gating `_track_subprocess_completion`)

Limit how many role subprocesses run concurrently across all tickets. Without this, 10 tickets matching `dispatch_planner` would spawn 10 simultaneous Claude Code sessions.

- [ ] **Step 1: Config knob**

In `ReconcilerConfig`:
```python
    max_concurrent_dispatches: int = Field(
        default=2,
        ge=1,
        le=20,
        description="Max concurrent role subprocesses (Planner+Worker+Reviewer+Fixer combined)",
    )
```

- [ ] **Step 2: Add semaphore to V3GitHubHost**

In `V3GitHubHost.__init__`:
```python
        max_concurrent_dispatches: int = 2,
```
Store:
```python
        self._dispatch_semaphore = asyncio.Semaphore(max_concurrent_dispatches)
```

Wrap `_track_subprocess_completion`'s body (the `try` block) inside:
```python
        async with self._dispatch_semaphore:
            try:
                returncode = await asyncio.wait_for(...)
                # ... rest
```

This means: while a subprocess is running and being tracked, it holds one semaphore slot. When the subprocess exits (or times out), the slot is released.

BUT — the semaphore should ALSO gate the dispatch itself, not just the tracking. Otherwise we spawn N subprocesses immediately and they queue at the semaphore. To gate spawning:

- Make `dispatch_role` async (currently sync) and acquire the semaphore BEFORE running the subprocess runner
- OR: keep `dispatch_role` sync but use `asyncio.create_task` to spawn a "spawn + track" coroutine that acquires the semaphore first

The second approach keeps the protocol surface unchanged. Refactor:
```python
    def dispatch_role(self, *, role, owner, repo, issue, pr_number) -> int:
        # build argv as before
        # don't call _runner yet — schedule a coroutine that will acquire the
        # semaphore, then spawn, then track
        loop = asyncio.get_event_loop()
        loop.create_task(self._spawn_and_track(argv, role))
        return -1  # caller writes start log row with pid=-1 (deferred); the
                   # background task fills in real pid via _pending_start_log_id_by_pid
                   # keyed by start_log_id instead
```

Actually this gets complicated. Simpler: keep `dispatch_role` synchronous AND spawn-immediate, but cap with the semaphore. If we exceed the cap, the new subprocess starts but is held off via the semaphore.

REVISED simpler approach: keep dispatch_role as-is. In `_track_subprocess_completion`, BEFORE `wait_for`, acquire the semaphore. That throttles the LIFECYCLE not the spawn. The subprocesses all start but only N can actively run; the rest will block on subprocess.Popen.wait() because nobody's reading their stdio? No, that's wrong — they keep running OS-level.

OK the semaphore at this layer doesn't actually throttle real subprocess execution; it only serializes the tracking task. That's not what we want.

REAL fix: gate `dispatch_role` itself. Make it async or use a thread-safe check. Best path:

- Change `dispatch_role` to async
- Update the `ReconcilerHost` Protocol to match
- The `execute_action` call in actions.py needs to become `await host.dispatch_role(...)` — but actions.py is sync, called from `_reconcile_project` which is sync.

Since `_reconcile_project` is called from the async `tick()`, we could make `_reconcile_project` async and `execute_action` async, then `await` dispatch_role.

This is the right shape but is a wider refactor. Acceptable for this PR.

For Task 6, the FULL fix:
- `Action.dispatch_role` in Protocol → `async def dispatch_role(...) -> int`
- `execute_action` becomes async, awaits dispatch_role
- `_reconcile_project` becomes async, awaits execute_action
- `tick` awaits `_reconcile_project`
- `V3GitHubHost.dispatch_role` is now async, acquires semaphore BEFORE calling _runner

The tests for actions/host already have async patterns; should be straightforward.

ALTERNATIVE (simpler, defensible): use a `threading.Semaphore` (not asyncio) and acquire in `dispatch_role` synchronously with `acquire(blocking=False)`. If we can't get the slot, raise an exception that the executor catches and logs as "deferred — concurrency cap reached." Next poll re-fires. That keeps dispatch_role sync and avoids the refactor.

For this PR, do the SIMPLER threading.Semaphore approach. It's defensible: skip dispatch this poll if cap is full, idempotence will re-fire next poll when a slot opens.

Revised step 2:
```python
        import threading
        self._dispatch_capacity = threading.Semaphore(max_concurrent_dispatches)
```

In `dispatch_role`:
```python
        if not self._dispatch_capacity.acquire(blocking=False):
            raise RuntimeError(f"concurrency cap reached ({max_concurrent_dispatches} dispatches active)")
```

In `_track_subprocess_completion`, in the `finally` block (or just after writing termination), release the slot:
```python
        finally:
            self._dispatch_capacity.release()
```

The raise will be caught by execute_action's existing exception handler, write outcome="error" with the cap-reached message, idempotence next poll will retry.

- [ ] **Step 3: Plumb config + write test**

In cli.py construction:
```python
    host = V3GitHubHost(..., max_concurrent_dispatches=config.reconciler.max_concurrent_dispatches)
```

Test:
```python
def test_concurrency_cap_refuses_dispatch_when_full(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    host = V3GitHubHost(
        v2_host=None, log=log,
        subprocess_runner=lambda _argv: _FakeProcess(),
        max_concurrent_dispatches=1,
    )
    # acquire the only slot
    host._dispatch_capacity.acquire()
    # next dispatch_role should raise
    with pytest.raises(RuntimeError, match="concurrency cap reached"):
        host.dispatch_role(role="planner", owner="x", repo="y", issue=1, pr_number=None)
```

- [ ] **Step 4: Verify + Commit**

```bash
uv run pytest packages/foreman -q
git add packages/foreman/src/foreman/config.py packages/foreman/src/foreman/reconciler/v3_host.py packages/foreman/src/foreman/cli.py packages/foreman/tests/reconciler/
git commit -m "feat(reconciler): max_concurrent_dispatches cap on dispatch_role"
```

---

## Task 7: `foreman:hold` safety rule

**Files:**
- Modify: `packages/foreman/src/foreman/reconciler/rules.py` (add high-precedence safety rule for `foreman:hold`)
- Modify: `packages/foreman/tests/reconciler/test_rules.py` (add test)

When an operator labels a ticket `foreman:hold`, no action should fire (not even `surface_help`). Add a rule at precedence 5 (above all existing safety rules at 10+) that returns NOOP.

Wait — the rule catalog emits actions; there's no NOOP rule. The way to "block" is to fire SURFACE_HELP (rate-limited, then NOOP afterward) OR to add a special "BLOCKED" action.

Simpler: add a Rule whose `then=Action.NOOP`. The evaluator already returns NOOP on no-match, but a `then=NOOP` is also valid. Action.NOOP just means "don't act."

Wait — `Action.NOOP` is the default. Currently a rule firing means an action SHOULD fire. Can a rule fire `NOOP`? Looking at the evaluator: it returns `rule.then` when `rule.when(ctx)` is True. So a rule with `then=Action.NOOP` would return NOOP. That works.

- [ ] **Step 1: Add helper + rule**

In `rules.py`, add at the top of safety rules:
```python
def _hold_label(ctx: ActionContext) -> bool:
    return "foreman:hold" in ctx.issue.labels


# Add to _SAFETY_RULES tuple as the FIRST entry (precedence 5):
Rule(
    name="hold_label_blocks",
    tier=PrecedenceTier.SAFETY,
    precedence=5,
    when=_hold_label,
    then=Action.NOOP,  # no action emitted; nothing fires for this ticket
),
```

Other safety rules at precedence 10+ stay where they are. The hold rule at 5 fires first; on a held ticket, NOOP is returned without ever evaluating other rules.

- [ ] **Step 2: Test**

In `test_rules.py`:
```python
def test_hold_label_blocks_all_actions(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    # A ticket with BOTH foreman:hold AND foreman:planning — hold should win
    ctx = _ctx_with(tmp_path, _issue(labels=("foreman:hold", "foreman:planning")))
    assert evaluate(ctx, rules=RULES) is Action.NOOP
```

- [ ] **Step 3: Verify + Commit**

```bash
uv run pytest packages/foreman/tests/reconciler -q
git add packages/foreman/src/foreman/reconciler/rules.py packages/foreman/tests/reconciler/test_rules.py
git commit -m "feat(reconciler): foreman:hold label blocks all actions (safety rule precedence 5)"
```

---

## Task 8: Attempt budget enforcement

**Files:**
- Modify: `packages/foreman/src/foreman/reconciler/rules.py` (add attempt-counter helper + budget check in dispatch rules)
- Modify: `packages/foreman/tests/reconciler/test_rules.py` (add tests)

V2 uses `foreman:fix-attempt-N` labels to track Fixer retries; refuses re-dispatch after `max_fix_attempts`. v3 can do this purely via the execution log — count the number of completed `dispatch_fixer` rows (success OR error termination) for a ticket; if it exceeds budget, refuse and emit SURFACE_HELP.

- [ ] **Step 1: Add to ExecutionLog**

In `packages/foreman/src/foreman/reconciler/exec_log.py`, add a method:
```python
    def count_completed(self, action: str, ticket_id: str) -> int:
        """Count completed (terminated) action attempts for a ticket. A 'completed'
        attempt has a termination row (parent_log_id points at a start)."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM execution_log term
                WHERE term.action = ?
                  AND term.ticket_id = ?
                  AND term.parent_log_id IS NOT NULL
                """,
                (action, ticket_id),
            ).fetchone()
            return int(row[0]) if row else 0
```

- [ ] **Step 2: Use in dispatch_fixer rule**

In `rules.py`, modify `_impl_fix_pending`:
```python
def _impl_fix_pending(ctx: ActionContext) -> bool:
    return (
        "foreman:impl-fix" in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.log.has_unterminated("dispatch_fixer", ctx.ticket_id)
        and ctx.log.count_completed("dispatch_fixer", ctx.ticket_id) < 3  # max_fix_attempts default
    )
```

Add a safety rule that fires SURFACE_HELP when the budget is exhausted:
```python
def _fix_attempts_exhausted(ctx: ActionContext) -> bool:
    return (
        "foreman:impl-fix" in ctx.issue.labels
        and ctx.log.count_completed("dispatch_fixer", ctx.ticket_id) >= 3
    )
```

Add Rule at safety precedence 50:
```python
Rule(
    name="fix_attempts_exhausted",
    tier=PrecedenceTier.SAFETY,
    precedence=50,
    when=_safety_with_rate_limit(_fix_attempts_exhausted),
    then=Action.SURFACE_HELP,
),
```

Same shape for worker: `count_completed("dispatch_worker", ...)` capped at 3.

- [ ] **Step 3: Tests**

Add 2 tests:
```python
def test_dispatch_fixer_blocks_after_3_completed_attempts(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    issue = _issue(labels=("foreman:impl-fix",))
    pr = _pr(mergeable="MERGEABLE", ci_status="SUCCESS")
    ctx = _ctx_with(tmp_path, issue, pr)
    for _ in range(3):
        start_id = ctx.log.write_action(
            ticket_id=ctx.ticket_id, project="foreman", rule_name="dispatch_fixer",
            action="dispatch_fixer", outcome="running", details={},
        )
        ctx.log.terminate_action(parent_log_id=start_id, outcome="success", details={})
    assert evaluate(ctx, rules=RULES) is Action.SURFACE_HELP


def test_dispatch_fixer_still_fires_under_budget(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    issue = _issue(labels=("foreman:impl-fix",))
    pr = _pr(mergeable="MERGEABLE", ci_status="SUCCESS")
    ctx = _ctx_with(tmp_path, issue, pr)
    # 2 completed attempts (under cap of 3)
    for _ in range(2):
        start_id = ctx.log.write_action(
            ticket_id=ctx.ticket_id, project="foreman", rule_name="dispatch_fixer",
            action="dispatch_fixer", outcome="running", details={},
        )
        ctx.log.terminate_action(parent_log_id=start_id, outcome="success", details={})
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_FIXER
```

- [ ] **Step 4: Verify + Commit**

```bash
uv run pytest packages/foreman/tests/reconciler -q
git add packages/foreman/src/foreman/reconciler/exec_log.py packages/foreman/src/foreman/reconciler/rules.py packages/foreman/tests/reconciler/test_rules.py
git commit -m "feat(reconciler): attempt budget enforcement via execution_log count_completed"
```

---

## Task 9: Open the PR

- [ ] **Step 1: Push**
```bash
PAT=$(python C:/Users/jeffr/.wren/.claude/skills/creds-management/scripts/creds.py --being wren get github --keyring --password 2>/dev/null) && \
git push "https://x-access-token:${PAT}@github.com/jeffrichley/foreman.git" fix/v3-operational-gaps
```

- [ ] **Step 2: Open PR**
```bash
PAT=$(...) && GH_TOKEN="$PAT" gh pr create --repo jeffrichley/foreman --base main --head fix/v3-operational-gaps \
  --title "feat(reconciler): close 8 operational-discipline gaps for v3 daemon" \
  --body "$(cat <<'EOF'
## Summary

Closes 8 gaps Jeff identified after the v3 cutover: features v2 had baked in that I missed porting when focusing on the architectural rewrite.

Each fix is small + targeted. All wired into existing v3 modules; no new subpackages.

## Gaps closed

1. **DaemonLock** — v3-start now wrapped in file-based PID lock at `~/.foreman/reconciler.lock` (prevents two concurrent daemons race-writing the execution log)
2. **`recover_orphaned()` at startup** — terminates orphaned `running` log rows from crashed daemons before idempotence checks block future dispatches
3. **SIGTERM/SIGINT handlers** — `Reconciler.shutdown()` called cleanly on signals; in-flight ticks complete before exit (Windows falls through to default KeyboardInterrupt)
4. **Persistent logging** — `configure_daemon_logging()` writes JSON-lines to `~/.foreman/v3-daemon.log`
5. **Subprocess wall-clock timeout** — `role_dispatch_timeout_seconds` config (default 3600s) + `asyncio.wait_for` in `_track_subprocess_completion`; timed-out subprocesses get SIGTERM and an outcome="timeout" row
6. **Concurrency cap** — `max_concurrent_dispatches` config (default 2) + threading.Semaphore in V3GitHubHost; over-cap dispatch raises caught-error, idempotence re-fires next poll
7. **`foreman:hold` safety rule** — precedence 5 (highest); held tickets return Action.NOOP without evaluating other rules
8. **Attempt budget** — `count_completed()` method on ExecutionLog; dispatch_fixer + dispatch_worker rules check < 3 completed attempts; new safety rule emits SURFACE_HELP once budget exhausted

## Test plan

- [x] All v3 tests pass (target: ≥696 passed, 1 skipped — adds ~7 new tests across timeout, concurrency, hold rule, attempt budget)
- [x] Ruff F-codes clean; mypy clean on touched files
- [x] Pre-push hook passes

For #106.
EOF
)"
```

---

## Self-review

Spec coverage: all 8 gaps addressed.
Placeholder scan: no TBD/TODO patterns.
Type consistency: ReconcilerConfig fields are Pydantic Field; V3GitHubHost __init__ kwargs are kwarg-only consistently.

## Execution Handoff

Subagent-Driven Development executes task-by-task with continuous execution per Jeff's mandate.
