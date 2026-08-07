# Spec: prevent new state-instance dispatch after SIGTERM (issue #598)

## Goal

Stop the daemon from opening new `state_instances` rows after receiving SIGTERM. When a SIGTERM is delivered, any `WorkerPool` thread that starts after the signal must abort before calling `open_state_instance`, leaving the ticket's row count unchanged. See issue [#598](https://github.com/jeffrichley/foreman/issues/598).

## Acceptance criteria

- After `WorkerPool.set_shutting_down()` is called, a dispatch attempt submitted to the pool opens **zero** new `state_instances` rows, verified by asserting `repo.list_state_instances_for_ticket(ticket.id) == []` after `_wait_until_idle(qm)`.
- The ticket's `current_state` is unchanged after the blocked dispatch — it is NOT moved to `NeedsHelp`.
- `Daemon.stop()` propagates the shutting-down signal to the pool: after `daemon.stop()`, a `WorkerPool.tick()` call followed by `_wait_until_idle` produces zero new `state_instances` rows.
- `WorkerPool._run_transition` checks `_shutting_down` **before** any call to `repo.open_state_instance`, so no row is ever inserted for a post-SIGTERM dispatch.
- The existing `test_drain_lease_defers_dispatch_without_parking_the_ticket` and `test_dispatch_resumes_once_the_drain_lease_is_released` tests continue to pass unchanged — the new flag is orthogonal to the drain lease.
- `just check` exits zero.

## Approach

**Pattern (Decision 4):** No GoF pattern fits. The applicable Google engineering principle is **SRP / "make the right thing easy"**: `WorkerPool` already has one guard for post-redeploy dispatch (`DrainLeaseActiveError`); this adds a second guard in the identical position — a process-local `threading.Event` that fires on SIGTERM. The two guards are complementary, not alternatives: the drain lease is DB-backed and covers the gate-update → SIGTERM window; the new flag is process-local and covers any SIGTERM source (manual `docker stop`, watchdog kill, or operator `kill`).

### What the code actually does today

Reading `packages/foreman/src/foreman/v4/cli/daemon.py:201–202`:

```python
for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, lambda *_args: daemon.stop())
```

SIGTERM handlers **are** installed. `daemon.stop()` (`daemon.py:469–475`) sets `self._stop`. The `run_forever()` loop (`daemon.py:449–467`) exits after the current tick, then calls `self.shutdown(wait=True)`. So the loop stops — but threads already submitted to the `ThreadPoolExecutor` in `pool.tick()` during the in-progress `tick_once()` are not affected by `_stop`. Those threads call `_run_transition`, which calls `open_state_instance` — opening new rows after SIGTERM.

The issue's measurement (instances 14863/14864 opened 8–9 s after SIGTERM) matches this race: the tick in progress submitted those transitions before `_stop` was checked at the loop boundary.

### The drain lease gap

The existing drain lease (foreman#599, `repository.py:343–369`, `worker_pool.py:187–213`) blocks `open_state_instance` when gate-update set the lease before SIGTERM. It does **not** cover:
- Manual `docker stop` (Watchtower lifecycle hook not involved)
- Any stop path that bypasses gate-update

The new flag closes that gap: it fires unconditionally on any SIGTERM delivery path.

### The fix — two surgical changes

**1. `packages/foreman/src/foreman/v4/worker_pool.py`**

Add `_shutting_down: threading.Event` to `WorkerPool.__init__` and a `set_shutting_down()` method. In `_run_transition`, add the check as the **very first statement** (before `repo.get_ticket`, before `open_state_instance`), mirroring the `DrainLeaseActiveError` handling directly below it:

```python
def _run_transition(self, item: WorkItem) -> None:
    if self._shutting_down.is_set():
        _LOG.info(
            "ticket %s: dispatch skipped, daemon is shutting down",
            item.ticket_id,
        )
        return
    ctx: StateContext | None = None
    try:
        ...
```

Checking at the top (rather than immediately before `open_state_instance`) avoids unnecessary repo reads for a daemon already shutting down.

**2. `packages/foreman/src/foreman/v4/daemon.py`**

In `Daemon.stop()`, propagate the flag to the pool immediately after setting `_stop`. Both calls are to `threading.Event.set()` — async-signal-safe:

```python
def stop(self) -> None:
    self._stop.set()
    self._pool.set_shutting_down()
```

### Test additions

The acceptance criterion explicitly rejects "a signal handler is registered" assertions. Tests must count rows:

**`packages/foreman/tests/v4/test_worker_pool.py`** — add two tests:

1. `test_shutting_down_blocks_new_state_instances`: call `pool.set_shutting_down()`, enqueue a work item, `pool.tick()`, wait for idle, assert `repo.list_state_instances_for_ticket(ticket.id) == []` and `ticket.current_state` unchanged (not `NeedsHelp`). Use `ImplApproved` as the state (same as the drain-lease test) so the pattern is clearly parallel.

2. `test_dispatch_proceeds_before_shutting_down_is_set`: regression lock — confirm that without calling `set_shutting_down()`, a normal dispatch still opens a state-instance row (guards against accidentally setting the flag at construction).

**`packages/foreman/tests/v4/test_daemon.py`** — add one test:

3. `test_daemon_stop_prevents_new_state_instances`: build a `Daemon` with a ticket in `Planning`, call `daemon.stop()`, then call `daemon.tick_once()`, then assert `repo.list_state_instances_for_ticket(ticket.id) == []`. Use `FakeRoleDispatcher(responses={})` (no canned response needed — the flag fires before dispatch). Use a blocking clock so the tick returns immediately without sleeping.

## Sub-requests (topologically sorted)

1. **`worker_pool.py` — add `_shutting_down` field and `set_shutting_down()` method.** First, ensure `import threading` is present in the module-level imports (the current imports are `concurrent.futures`, `datetime`, `logging`, `collections.abc`, and `typing` — `threading` is absent and must be added). Then, in `WorkerPool.__init__`, after the executor construction, add:
   ```python
   self._shutting_down: threading.Event = threading.Event()
   ```
   Add the public method after `tick()`:
   ```python
   def set_shutting_down(self) -> None:
       """Block new state-instance dispatch. Called by Daemon.stop() on SIGTERM.

       Safe to call from a signal handler — threading.Event.set() is
       async-signal-safe. After this fires, any _run_transition call that
       has not yet reached open_state_instance returns immediately.
       """
       self._shutting_down.set()
   ```

2. **`worker_pool.py` — guard in `_run_transition`.** Insert at the very top of `_run_transition`, before the `try` block:
   ```python
   if self._shutting_down.is_set():
       _LOG.info(
           "ticket %s: dispatch skipped, daemon is shutting down",
           item.ticket_id,
       )
       return
   ```

3. **`daemon.py` — propagate in `stop()`.** Change `Daemon.stop()` from:
   ```python
   def stop(self) -> None:
       self._stop.set()
   ```
   to:
   ```python
   def stop(self) -> None:
       self._stop.set()
       self._pool.set_shutting_down()
   ```
   Update the docstring to mention that `_pool.set_shutting_down()` is also called so that any in-progress dispatch thread aborts before opening a new state-instance row.

4. **`test_worker_pool.py` — add `test_shutting_down_blocks_new_state_instances`.** Model it identically to `test_drain_lease_defers_dispatch_without_parking_the_ticket` (lines 253–291), replacing `repo.acquire_drain_lease(...)` with `pool.set_shutting_down()`. Assert both the empty instances list AND that `current_state` is unchanged (`"ImplApproved"`, not `"NeedsHelp"`).

5. **`test_worker_pool.py` — add `test_dispatch_proceeds_before_shutting_down_is_set`.** Build a `WorkerPool` with a `Planning` ticket and a canned `clean` dispatcher response. Do NOT call `set_shutting_down()`. `pool.tick()`, wait idle. Assert `repo.list_state_instances_for_ticket(ticket.id) != []` (dispatch happened normally). This is the regression lock.

6. **`test_daemon.py` — add `test_daemon_stop_prevents_new_state_instances`.** Use the same `Daemon` + `Poller` boilerplate as `test_daemon_one_tick_processes_one_ticket`. After creating the daemon but before ticking, call `daemon.stop()`. Then call `daemon.tick_once()`. Assert `repo.list_state_instances_for_ticket(ticket.id) == []`.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/v4/worker_pool.py` | Add `import threading` to module-level imports; add `_shutting_down: threading.Event` to `__init__`; add `set_shutting_down()` method; add `_shutting_down.is_set()` guard at the top of `_run_transition`. |
| `packages/foreman/src/foreman/v4/daemon.py` | In `Daemon.stop()`, add `self._pool.set_shutting_down()` call after `self._stop.set()`; update docstring. |
| `packages/foreman/tests/v4/test_worker_pool.py` | Add `test_shutting_down_blocks_new_state_instances` and `test_dispatch_proceeds_before_shutting_down_is_set`. |
| `packages/foreman/tests/v4/test_daemon.py` | Add `test_daemon_stop_prevents_new_state_instances`. |

## Alternatives considered

1. **Rely solely on the foreman#599 drain lease.** The drain lease already blocks `open_state_instance` when gate-update runs first. Rejected: the drain lease is only in place if Watchtower's lifecycle hook executed before SIGTERM. A manual `docker stop`, watchdog kill, or any stop path that bypasses gate-update leaves no lease — the post-SIGTERM dispatch continues until SIGKILL. The new flag is zero-cost (no DB round-trip) and fires unconditionally.

2. **Cancel executor futures.** Python 3.9+ `concurrent.futures.Future.cancel()` cancels pending futures (not yet started). Rejected: (a) futures submitted before SIGTERM that are currently running threads are not cancellable — the in-flight check is needed regardless; (b) the target environment is Python 3.9+ but the cancel path is more invasive and harder to test deterministically. The `_shutting_down` check in `_run_transition` is sufficient: submitted-but-not-started threads check the flag and exit; already-running threads are intentionally left to complete (in-flight work continues, per the issue's stated behavior).

3. **Check `Daemon._stop` from `WorkerPool.tick()`.** Have `tick()` read `_stop` before submitting futures. Rejected: `WorkerPool` has no reference to `Daemon._stop`, and adding that dependency would couple two previously-orthogonal classes. A dedicated `set_shutting_down()` method keeps the interface clean and the pool testable in isolation.

4. **Module-level global flag.** Use a `threading.Event` at module scope rather than on the instance. Rejected: module-level state breaks test isolation (parallel tests share one module) — the existing `DrainLeaseActiveError` path uses instance-level state for the same reason. Instance-level is correct here.

## Open questions

None. The SIGTERM handler path, the `run_forever` → `tick_once` → `pool.tick` → `_run_transition` call chain, the `DrainLeaseActiveError` guard pattern it mirrors, and every file to be modified were read before writing this spec.

## Out of scope

- Raising Docker's stop grace period from 10 s. The issue notes this as "a separate decision" — the compose change is architectural and does not belong in this PR. The fix here is correct regardless of the grace period.
- Checking `_shutting_down` in `WorkerPool.tick()` to avoid submitting futures at all. Correct optimization but not needed for correctness — the `_run_transition` guard is the enforcement point. Can be added in a follow-up.
- Changes to gate-update or the drain lease (foreman#599). The two fixes are complementary and independent.
- Changes to `crash_recovery`, `run_forever`, or `shutdown` — none of those paths change behavior here.
