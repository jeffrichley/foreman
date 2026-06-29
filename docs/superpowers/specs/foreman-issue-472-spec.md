# Spec: per-project `max_in_flight` cap in `QueueManager` (issue #472)

## Goal

Layer a per-project concurrency cap on top of the existing global `max_in_flight` gate in `QueueManager`, resolving architecture-review finding T1 ("max_in_flight=1 couples all projects"). See issue [#472](https://github.com/jeffrichley/foreman/issues/472).

## Acceptance criteria

- `ProjectConfig` in `config.py` gains `max_in_flight: int | None = None`; `None` means no per-project limit beyond the global cap. A value < 1 is rejected by Pydantic (`ge=1`). Validated by a new `test_config.py` test.
- `WorkItem` in `work.py` gains a required `project: str` field; its frozen-dataclass equality and hash automatically include the new field.
- `QueueManager.__init__` accepts an optional `project_caps: dict[str, int | None] | None = None` kwarg; coerced in the body with `self._project_caps: dict[str, int | None] = project_caps or {}`. When a candidate's project has a cap `c` and `_in_flight_by_project[project] >= c`, the candidate is **skipped** (not `None` returned) — a different project may still be eligible.
- On a successful `dequeue`, `_in_flight_by_project[project]` is incremented; `mark_done` decrements it. Both structures (`_in_flight_tickets` and `_in_flight_by_project`) stay in sync under the existing `threading.RLock`.
- Unit test: a project with `max_in_flight=1` yields at most one dequeued ticket at a time even when the global cap is higher and multiple tickets from that project are ready.
- Unit test: two projects each capped at 1 with a global cap of 2 — one ticket from each project runs concurrently (cross-project cap does not block each other).
- Unit test: `mark_done` releases the per-project slot; a third ticket from the same capped project becomes dequeuable after the first completes.
- All existing `WorkItem(ticket_id=…, state_name=…)` callsites in tests and source are updated to include `project=`.
- `just check` exits zero.

## Approach

**Pattern (Decision 4):** No GoF pattern fits — this is a straightforward counter-based skip-filter extension ("no pattern fits, this is straightforward filter extension"). The Google principle is **SRP + "make the right thing easy"**: the `QueueManager` already owns all dequeue-eligibility logic; the per-project counter lives there so callers never have to think about it.

**WorkItem.project avoids a mark_done round-trip.** `mark_done(item)` has only the `WorkItem` at hand — no `TicketRecord`. Adding `project: str` to `WorkItem` lets `mark_done` decrement `_in_flight_by_project[item.project]` without a repo call on the critical `done_callback` path. The Poller already holds a `TicketRecord` when building `WorkItem` (both `_adopt_new_tickets` and `_enqueue_open_tickets` call `repo.create_ticket` / `repo.list_open_tickets`, which return records carrying `project`). The CLI `cmd_retry` in `mutations.py` also has a `TicketRecord` from `_resolve()`. All callsites have the project string at construction time.

**Dequeue filter placement.** The new filter is inserted _after_ the existing `is_held` and `list_unmet_dependencies` checks, in the same candidate loop that already skips filtered items (does **not** return `None` — `None` is reserved for "global cap hit" or "heap empty"). The project used for the cap lookup comes from `ticket.project` (the already-fetched `TicketRecord`), which equals `candidate.project` by construction.

```python
cap = self._project_caps.get(ticket.project)
if cap is not None and self._in_flight_by_project.get(ticket.project, 0) >= cap:
    continue
```

On success:
```python
self._in_flight_by_project[ticket.project] = (
    self._in_flight_by_project.get(ticket.project, 0) + 1
)
```

**mark_done:**
```python
count = self._in_flight_by_project.get(item.project, 0)
if count > 0:
    self._in_flight_by_project[item.project] = count - 1
```

**Daemon wiring.** `Daemon.__init__` already holds `self._project_configs: dict[str, ProjectConfig]`. Before constructing `QueueManager`, extract the per-project caps:
```python
project_caps = {
    name: pc.max_in_flight
    for name, pc in self._project_configs.items()
}
self._qm = QueueManager(
    repo=repo,
    max_in_flight=config.max_in_flight,
    project_caps=project_caps,
)
```

**Reconcile on startup.** No change to `reconcile.py` is needed. `QueueManager.__init__` initializes both `_in_flight_tickets` and `_in_flight_by_project` as empty structures. `reconcile_on_startup()` only touches the repository (closes orphaned state_instance rows); it never touches the QM. After reconcile, both counters remain empty and consistent. The Poller's first tick re-enqueues all open tickets via `enqueue()`, and subsequent `dequeue()`/`mark_done()` cycles maintain both structures in sync as designed.

**Global cap unchanged.** `V4Config.max_in_flight` stays pinned to 1 by the existing `_max_in_flight_pinned_to_one` validator (until #316 lands). Per-project cap unit tests construct `QueueManager(max_in_flight=2, …)` directly — bypassing `V4Config`, which is what existing tests already do (`test_global_max_in_flight_cap` uses `max_in_flight=2`). The per-project cap only ever _reduces_ what `dequeue()` hands out; it can never raise effective concurrency above the global cap.

**WorkItem callsite blast radius.** Adding `project: str` as a required field to the frozen dataclass breaks every `WorkItem(ticket_id=…, state_name=…)` construction. There are ~35 callsites across 7 files. All are updated to add `project="p"` (for test-only project names) or `project=ticket.project` (for source callsites). The existing `_ticket()` helper in `test_queue_manager.py` gains `project: str = "p"` as a keyword parameter (backward-compatible — all existing calls use the default).

## Sub-requests (topologically sorted)

1. **`config.py` — add `ProjectConfig.max_in_flight`**: Add `max_in_flight: int | None = Field(default=None, ge=1)` with docstring to `ProjectConfig`. Update the module-level `[[projects]]` docstring block to document the new field.

2. **`work.py` — add `WorkItem.project`**: Add `project: str` as the third field on `WorkItem`. No other changes; `frozen=True, slots=True` handles hash/equality automatically.

3. **Source callsites — add `project=`**: Update the two `WorkItem(...)` calls in `poller.py` (`_adopt_new_tickets`, `_enqueue_open_tickets`) to add `project=ticket.project`. Update the one `WorkItem(...)` call in `cli/mutations.py:cmd_retry` to add `project=ticket.project`.

4. **`queue_manager.py` — per-project counter**: Add `project_caps: dict[str, int | None] | None = None` kwarg to `__init__`; coerce in the body with `self._project_caps: dict[str, int | None] = project_caps or {}`. Add `self._in_flight_by_project: dict[str, int] = {}`. Insert the per-project cap filter in `dequeue()` after the `list_unmet_dependencies` check. Increment on dequeue success. Decrement in `mark_done`.

5. **`daemon.py` — wire `project_caps`**: Extract `project_caps` from `self._project_configs` before constructing `self._qm`, pass as `project_caps=project_caps`.

6. **Test callsites — update `WorkItem(...)` to add `project=`** (sub-request 3 must be done first to see the pattern):
   - `test_work.py`: 3 callsites → `project="p"`
   - `test_queue_manager.py`: ~20 callsites → `project="p"`; also update `_ticket()` signature to accept `project: str = "p"`
   - `test_worker_pool.py`: 5 callsites → `project="p"`
   - `test_poller.py`: 4 assertion callsites → `project="p"`
   - `cli/test_mutation_commands.py`: 4 assertion callsites → `project="p"`
   - `cli/test_query_commands.py`: 2 callsites → `project="p"`

7. **`test_queue_manager.py` — new behavioral tests** (sub-request 4 done first):

   ```python
   def test_per_project_cap_limits_single_project(repo):
       """Project capped at 1 yields at most 1 dequeued ticket even at global cap 4."""
       a = _ticket(repo, 1, project="fast")
       b = _ticket(repo, 2, project="fast")
       qm = QueueManager(repo=repo, max_in_flight=4, project_caps={"fast": 1})
       qm.enqueue(WorkItem(ticket_id=a, state_name="Planning", project="fast"))
       qm.enqueue(WorkItem(ticket_id=b, state_name="Planning", project="fast"))
       first = qm.dequeue()
       assert first is not None
       assert qm.dequeue() is None  # "fast" at its cap
   
   def test_per_project_cap_does_not_block_other_projects(repo):
       """Two projects capped at 1 with global cap 2: one from each runs concurrently."""
       a = _ticket(repo, 1, project="alpha")
       b = _ticket(repo, 2, project="beta")
       qm = QueueManager(
           repo=repo, max_in_flight=2,
           project_caps={"alpha": 1, "beta": 1},
       )
       qm.enqueue(WorkItem(ticket_id=a, state_name="Planning", project="alpha"))
       qm.enqueue(WorkItem(ticket_id=b, state_name="Planning", project="beta"))
       first = qm.dequeue()
       second = qm.dequeue()
       assert first is not None
       assert second is not None
       assert {first.project, second.project} == {"alpha", "beta"}
   
   def test_mark_done_releases_per_project_slot(repo):
       """After mark_done, a third ticket from the same capped project becomes dequeuable."""
       a = _ticket(repo, 1, project="proj")
       b = _ticket(repo, 2, project="proj")
       c = _ticket(repo, 3, project="proj")
       qm = QueueManager(repo=repo, max_in_flight=4, project_caps={"proj": 1})
       qm.enqueue(WorkItem(ticket_id=a, state_name="Planning", project="proj"))
       qm.enqueue(WorkItem(ticket_id=b, state_name="Planning", project="proj"))
       qm.enqueue(WorkItem(ticket_id=c, state_name="Planning", project="proj"))
       first = qm.dequeue()
       assert first is not None
       assert qm.dequeue() is None  # capped
       qm.mark_done(first)
       second = qm.dequeue()
       assert second is not None  # slot released
   ```

8. **`test_config.py` — config tests for per-project `max_in_flight`**:

   ```python
   def test_project_config_max_in_flight_defaults_to_none():
       p = ProjectConfig(name="p", repo="o/r", local_clone_path="/tmp/r")
       assert p.max_in_flight is None
   
   def test_project_config_max_in_flight_zero_rejected():
       with pytest.raises(ValidationError):
           ProjectConfig(name="p", repo="o/r", local_clone_path="/tmp/r", max_in_flight=0)
   
   def test_project_config_max_in_flight_positive_accepted():
       p = ProjectConfig(name="p", repo="o/r", local_clone_path="/tmp/r", max_in_flight=2)
       assert p.max_in_flight == 2
   ```

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/v4/config.py` | Add `max_in_flight: int \| None = Field(default=None, ge=1)` to `ProjectConfig`; update `[[projects]]` module docstring |
| `packages/foreman/src/foreman/v4/work.py` | Add `project: str` field to `WorkItem` frozen dataclass |
| `packages/foreman/src/foreman/v4/queue_manager.py` | Add `project_caps` kwarg and `_in_flight_by_project` counter; per-project filter in `dequeue()`; decrement in `mark_done()` |
| `packages/foreman/src/foreman/v4/daemon.py` | Extract `project_caps` from `self._project_configs` and pass to `QueueManager` constructor |
| `packages/foreman/src/foreman/v4/poller.py` | Add `project=ticket.project` to both `WorkItem(...)` calls |
| `packages/foreman/src/foreman/v4/cli/mutations.py` | Add `project=ticket.project` to `WorkItem(...)` in `cmd_retry` |
| `packages/foreman/tests/v4/test_work.py` | Add `project="p"` to all 3 `WorkItem(...)` calls |
| `packages/foreman/tests/v4/test_queue_manager.py` | Add `project="p"` to all existing `WorkItem(...)` calls; extend `_ticket()` with `project: str = "p"` kwarg; add 3 new per-project cap tests |
| `packages/foreman/tests/v4/test_worker_pool.py` | Add `project="p"` to all 5 `WorkItem(...)` calls |
| `packages/foreman/tests/v4/test_poller.py` | Add `project="p"` to all 4 `WorkItem(...)` assertion comparisons |
| `packages/foreman/tests/v4/cli/test_mutation_commands.py` | Add `project="p"` to all 4 `WorkItem(...)` assertion comparisons |
| `packages/foreman/tests/v4/cli/test_query_commands.py` | Add `project="p"` to both `WorkItem(...)` calls |
| `packages/foreman/tests/v4/test_config.py` | Add 3 new tests for `ProjectConfig.max_in_flight` validation |

## Alternatives considered

1. **Pass full `list[ProjectConfig]` to `QueueManager` instead of a `dict[str, int | None]`**: Would work but is heavier than necessary — the QM only needs the per-project cap value, not the full config shape. The minimal `dict[str, int | None]` keeps `QueueManager` decoupled from `ProjectConfig` details. Rejected as overengineering.

2. **Avoid adding `project` to `WorkItem`; instead do a repo round-trip in `mark_done`**: Keeps `WorkItem` at two fields but adds a repository call to every completion callback on the `ThreadPoolExecutor` done-callback path. The done-callback already avoids blocking — adding a network/DB call there is contrary to the existing design intent. Rejected.

## Open questions

None. The issue specifies the exact data structures, dequeue filter placement, `mark_done` decrement, and reconcile-on-startup behavior. All claims verified against the codebase.

## Out of scope

- Removing the `_max_in_flight_pinned_to_one` validator from `V4Config` (that lift is gated on #316 landing).
- Changing `WorkerPool` thread count — it stays sized to the global `max_in_flight`; the per-project cap only reduces what `dequeue()` hands out.
- Any change to `reconcile.py` — both structures start empty at QM construction; no rebuild logic is needed.
- Any change to the existing `_STATE_PRIORITY` dispatch ordering — the per-project filter is the last step in the candidate loop, after priority-ordering already determined which candidate to attempt.
