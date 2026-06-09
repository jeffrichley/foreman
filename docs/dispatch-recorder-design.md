# DispatchRecorder design — unify dispatch telemetry behind a single mediator

**Status:** design, not yet implemented
**Author:** Wren (with Jeff)
**Date:** 2026-06-09
**Related:** foreman#243 (logging coverage audit), the seven cost-telemetry PRs (#232 → #246), and the double-row concern surfaced in PR #248

This is the structural answer to "we keep finding sibling bugs every time we add cost telemetry." The pattern repeats because we have multiple writers touching shared mutable telemetry files with no coordination protocol. This doc proposes the protocol.

The doc is intentionally long. Skim §1 and §3 if you want the bottom line; §2 explains the principles; §4-§7 cover the data, contract, migration, and what's out of scope.

---

## 1. The problem we keep hitting

We've shipped seven PRs in two days adding cost telemetry. Each one looked surgical at the time. Each one surfaced a sibling that needed its own PR:

- #232 added token capture (success path only) → sibling: failure paths
- #234 standardized the JSONL envelope → sibling: Planner had no `outcome`
- #236 fixed Planner's failure path → sibling: Reviewer/Worker/Fixer had the same bug
- #240/#241/#242 fixed the other three failure paths → sibling: subprocess-killed runs leave no row
- #243 audit doc surfaced two more siblings (#244 cache tokens, #246 ad-hoc CLI)
- #245 added a parent-side "killed" JSONL row → sibling: now we can double-count when the role catches its own exception AND the subprocess exits non-zero

The pattern is not "we keep finding edge cases." The pattern is "we have three writers (role subprocess, parent reconciler, ad-hoc CLI) all reaching into the same shared mutable telemetry files with no coordinated authority." Every new writer is a new race. Every new outcome is a new sibling.

**The right question is not "how do we fix the double-count?" The right question is "why is double-write possible at all?"**

---

## 2. Principles

Borrowed from Google-grade observability practice, distilled to fit a 1-person team:

### 2.1 One writer per signal

For any signal (cost, lifecycle, role-specific structured data), exactly one component in the system has write authority. Other components emit events; the writer decides what gets persisted.

Today: cost is written by both the role subprocess (JSONL) AND, since #248, the parent (`subprocess_killed` rows on JSONL). Two writers. Predictable conflict.

### 2.2 Trace-id propagation + idempotent writes

Every dispatch gets a unique `trace_id` (we already have `start_log_id` from `execution_log` — it can play this role). Every event about that dispatch carries the trace_id. Writes that target the same `(trace_id, event_kind)` are idempotent — second write is a no-op.

This makes double-write impossible **by design**, not by convention. We're not relying on "the role caught the exception so the parent should skip" — we're relying on the writer noticing it's seen this trace_id + kind before.

### 2.3 Telemetry is best-effort

Don't make the workload wait on a transactional cost write. If the bus is briefly unavailable, the subprocess emits to a local fallback and the parent picks it up later. The cost ledger is allowed to be 99.9% complete in exchange for never blocking the workload. (We're not there yet — current writes block — but the design should leave room.)

---

## 3. GoF patterns

Four GoF patterns map to this problem. Naming each one and what it does in our system:

### 3.1 Mediator (the central one)

`DispatchRecorder` is a Mediator. Every component that wants to record dispatch telemetry sends events to the Recorder. The Recorder is the only thing that touches persistent storage. Role runners don't know about JSONL or execution_log; they know about Recorder.

This eliminates "subprocess and parent both write to JSONL." There is no JSONL writer except the Recorder. Subprocess and parent both **emit events to the Recorder via the bus**.

### 3.2 Observer (for fan-out)

Inside the Recorder, multiple subscribers can attach to the event stream:

- `CostSubscriber` — writes token / USD to `execution_log`
- `RoleStatsSubscriber` — writes role-specific structured data to JSONL
- (future) `PrometheusSubscriber` — emits counters for dashboards
- (future) `SlackAlertSubscriber` — fires on `subprocess_killed`

Today every subscriber has its own write-site scattered through the codebase. Observer means we add a new concern (Prometheus, OpenTelemetry, etc.) without touching role runners.

### 3.3 Strategy (for storage backends)

The Recorder's persistence layer is a Strategy. Today execution_log is sqlite + JSONL is files. Tomorrow it might be Postgres + Prometheus, or just OpenTelemetry. Strategy lets us swap backends without changing emit-site code.

We don't need this on day one — the initial impl can hard-wire execution_log + JSONL. But the seam needs to exist.

### 3.4 Template Method (for role runners)

`BaseRoleRunner` is a Template Method. The base class owns:

- `start_time` capture
- The `try / except Exception` wrap
- The Recorder emit calls (start envelope, terminate envelope)
- The `usage` and `pr_number` state tracking
- The bare `raise` re-propagation

The subclass owns ONLY:

- `_synthesize()` — the role-specific LLM call
- `_role_specific_data()` — what extra structured fields to attach to the terminate event (for JSONL)

Future role #5 cannot accidentally skip the failure path because the failure path is in the base class.

---

## 4. The data model

### 4.1 Extend `ExecutionLogWritePayload`

Add an optional `usage: UsageInfo | None = None` field. The bus envelope already carries `details: dict` for free-form data — we could just stuff usage there — but a typed `usage` field gives us schema validation and makes intent obvious.

### 4.2 Extend `execution_log` schema

Add columns:

```sql
ALTER TABLE execution_log ADD COLUMN input_tokens INTEGER;
ALTER TABLE execution_log ADD COLUMN output_tokens INTEGER;
ALTER TABLE execution_log ADD COLUMN cache_creation_input_tokens INTEGER;
ALTER TABLE execution_log ADD COLUMN cache_read_input_tokens INTEGER;
ALTER TABLE execution_log ADD COLUMN total_cost_usd REAL;
ALTER TABLE execution_log ADD COLUMN model_usage_json TEXT;
ALTER TABLE execution_log ADD COLUMN duration_ms INTEGER;
ALTER TABLE execution_log ADD COLUMN num_turns INTEGER;
```

All nullable — start rows have NULL cost (cost not yet known), terminate rows carry the cost. Schema migration goes through the existing version mechanism (`CURRENT_SCHEMA_VERSION` bump).

### 4.3 What JSONL keeps

JSONL stays for **role-specific structured data only**:

- Worker: `total_sub_requests`, `implemented_count`, `skipped_count`, `skipped_by_reason`, `did_check_pass`, `confidence`, `baseline_failures_count`, `new_failures_count`
- Fixer: `total_findings`, `addressed_count`, `unaddressed_count`, `unaddressed_by_reason`, `disagreed_count`, `confidence`
- Reviewer: `target`, role-specific outcome
- Planner: minimal (it had no role-specific structured data before #236)

The common envelope (timestamp, role, issue_number, etc.) stays for `cat *.jsonl | jq` ergonomics. Token usage and cost fields LEAVE the JSONL because they live in `execution_log` now.

### 4.4 The new emit shape

A role subprocess no longer calls `log_<role>_run` for cost data. It calls `DispatchRecorder.record_dispatch_complete(...)` with:

- The `trace_id` (= `start_log_id` it received via env var when dispatched)
- The `UsageInfo` from the provider
- The role-specific structured data (sub_request counts, etc.)
- The role-level outcome (`success`, `worker_failed`, etc.)

Recorder sends ONE envelope to the parent's bus endpoint. Parent translates → `execution_log` update + JSONL write. **One emit, two persistence destinations via Observer subscribers.**

The parent's `_track_subprocess_completion` similarly calls `DispatchRecorder.record_subprocess_terminated(trace_id, exit_outcome)`. Recorder dedupes: if a `record_dispatch_complete` event for this trace_id already arrived, the terminated event is downgraded to "just record exit code" (no JSONL row). If no completion arrived (subprocess died early), Recorder writes the killed row.

**This is where idempotency lives.** Recorder knows what it's already persisted for a trace_id. Second writer for the same `(trace_id, event_kind)` is a no-op. No double-count is structurally possible.

---

## 5. Concrete contract (the API)

### 5.1 `DispatchRecorder` interface

```python
class DispatchRecorder:
    """Mediator for dispatch telemetry. ALL telemetry writes go through this."""

    def __init__(self, *, log: ExecutionLog, stats_root: Path) -> None:
        self._log = log
        self._stats_root = stats_root
        self._seen: set[tuple[int, str]] = set()  # (trace_id, event_kind) dedup

    def record_dispatch_started(
        self, *, trace_id: int, role: str, repo_slug: str, issue_number: int
    ) -> None: ...

    def record_dispatch_complete(
        self,
        *,
        trace_id: int,
        role: str,
        repo_slug: str,
        issue_number: int,
        pr_number: int | None,
        outcome: str,  # role-level: "success" | "worker_failed" | ...
        usage: UsageInfo,
        role_data: dict[str, Any],  # role-specific structured fields
        duration_seconds: float,
    ) -> None: ...

    def record_subprocess_terminated(
        self,
        *,
        trace_id: int,
        exit_outcome: str,  # parent-level: "success" | "timeout" | "subprocess_killed" | ...
        duration_seconds: float,
    ) -> None: ...
```

Inside, Recorder uses Observer to dispatch to subscribers:

- `CostSubscriber` reacts to `record_dispatch_complete` (writes usage to execution_log) and `record_subprocess_terminated` (writes zero-cost row if no prior completion)
- `RoleStatsSubscriber` reacts to `record_dispatch_complete` only (writes role-specific JSONL row)

### 5.2 `BaseRoleRunner` interface

```python
class BaseRoleRunner(ABC):
    """Template Method for role runners. Subclasses override only _synthesize()."""

    role: ClassVar[str]  # "planner" | "reviewer" | "worker" | "fixer"

    @abstractmethod
    async def _synthesize(self, ctx: RoleContext) -> RoleSynthesisResult:
        """Run the role-specific LLM call + side effects.

        Returns the synthesis result; the base class handles all telemetry.
        """

    @abstractmethod
    def _role_data(self, result: RoleSynthesisResult) -> dict[str, Any]:
        """Extract role-specific structured fields for the JSONL row."""

    async def run(self, ctx: RoleContext, *, recorder: DispatchRecorder) -> RoleRunResult:
        """Template Method: setup → synthesize → record → handle failure."""
        recorder.record_dispatch_started(...)
        start = time.monotonic()
        usage = UsageInfo()  # default zeros
        pr_number = None
        try:
            result = await self._synthesize(ctx)
            usage = result.usage
            pr_number = result.pr_number
            recorder.record_dispatch_complete(
                outcome=result.outcome,
                usage=usage,
                role_data=self._role_data(result),
                duration_seconds=time.monotonic() - start,
                ...
            )
            return RoleRunResult(...)
        except Exception:
            recorder.record_dispatch_complete(
                outcome=f"{self.role}_failed",
                usage=usage,  # partial — last successful state
                role_data={},  # safe defaults
                duration_seconds=time.monotonic() - start,
                ...
            )
            raise
```

The four concrete role runners (`PlannerRunner`, `ReviewerRunner`, etc.) subclass this and override `_synthesize` + `_role_data`. The try/except + recorder calls live in ONE place. Future role #5 inherits the same contract.

### 5.3 `_track_subprocess_completion` change

```python
async def _track_subprocess_completion(self, proc, *, trace_id, recorder, ...):
    try:
        returncode = await asyncio.wait_for(proc.wait(), timeout=...)
        outcome = "success" if returncode == 0 else "subprocess_nonzero_exit"
    except TimeoutError:
        outcome = "subprocess_timeout"
    except Exception:
        outcome = "subprocess_error"
    recorder.record_subprocess_terminated(
        trace_id=trace_id,
        exit_outcome=outcome,
        duration_seconds=...,
    )
```

The parent no longer writes JSONL directly. It tells the Recorder "subprocess terminated with outcome X." If the Recorder has already seen a `record_dispatch_complete` for this trace_id, the terminated event updates only the parent-side fields in `execution_log` and skips JSONL. If not, the Recorder writes a `subprocess_killed` row.

**Double-write impossible.**

---

## 6. Migration path

This is a refactor of seven PRs' worth of recent work. To avoid breakage, ship in three phases:

### Phase 1 — additive: introduce Recorder + Template Method, dual-write

- Add `DispatchRecorder` + `CostSubscriber` + `RoleStatsSubscriber` + `BaseRoleRunner` classes.
- Extend `execution_log` schema (`ALTER TABLE` for the new columns).
- Extend `ExecutionLogWritePayload` with optional `usage` field.
- Add `record_dispatch_complete` and `record_subprocess_terminated` paths.
- **Keep the existing `log_<role>_run` calls in place.** Role runners now write to BOTH ledgers temporarily.
- Tests verify the two ledgers agree (cost in execution_log == cost in JSONL).

This is the biggest PR but it's risk-free: nothing breaks because we're additive.

### Phase 2 — switch source of truth, remove cost from JSONL

- Update queries / downstream tooling to read cost from `execution_log`.
- Stop writing cost fields to JSONL (the role-specific structured data stays).
- Tests verify cost queries hit `execution_log`.
- Update `docs/logging-coverage.md` to reflect the new canonical surface.

### Phase 3 — refactor role runners onto BaseRoleRunner

- Convert each role runner to subclass `BaseRoleRunner` with just `_synthesize` + `_role_data`.
- Remove the per-role try/except code (now in base class).
- Tests verify behavior unchanged.

Each phase is ONE PR. After Phase 1, the double-count bug is gone (Recorder dedupes). Phases 2 and 3 are cleanup.

---

## 7. Out of scope

Explicitly NOT in this design:

- **OpenTelemetry export.** The Observer architecture leaves room for it; not in initial impl.
- **Prometheus exporter.** Same.
- **Cross-role aggregation CLI** (`foreman stats summarize`). Querying `execution_log` directly is fine for now.
- **Per-MCP-tool token granularity.** Requires SDK-level instrumentation; separate concern.
- **Async / batched telemetry.** Today's writes are synchronous. Telemetry-best-effort means we could batch + drop on bus failure, but that's a future hardening pass.
- **Distributed tracing across multiple foreman instances.** Single-daemon today.
- **Multi-provider support.** When Codex CLI provider lands, `UsageInfo` may need to grow (different provider, different fields). Handle then; design accommodates.

---

## 8. Acceptance criteria (the epic ticket)

Phase 1 ships when:

- [ ] `DispatchRecorder` class exists with the three `record_*` methods.
- [ ] `CostSubscriber` and `RoleStatsSubscriber` exist and are wired into Recorder.
- [ ] `execution_log` schema has the 8 new cost columns with migration.
- [ ] `ExecutionLogWritePayload.usage` is an optional `UsageInfo` field.
- [ ] `_track_subprocess_completion` calls `recorder.record_subprocess_terminated` in all three kill branches (timeout / error / nonzero) AND on the success path.
- [ ] Role runners call `recorder.record_dispatch_complete` AT THE END of `_synthesize` (success path) AND in the failure-path `except` branch (carrying partial usage).
- [ ] `BaseRoleRunner` exists with the Template Method `run`, the abstract `_synthesize` and `_role_data`.
- [ ] Recorder dedupes on `(trace_id, event_kind)` — second write is no-op.
- [ ] Tests verify: (a) success path writes both ledgers with matching cost, (b) failure path writes both ledgers, (c) subprocess-killed path writes only execution_log, (d) double-emit for same `(trace_id, event_kind)` is dedup'd.
- [ ] Existing tests stay green throughout.
- [ ] `new_failures_count == 0` at PR merge.

Phase 2 and 3 ship as separate PRs after Phase 1 lands.

---

## 9. Why this is terminal

After Phase 1, every cost-telemetry question has the same answer: query `execution_log`. The Recorder is the only writer. `(trace_id, event_kind)` dedup makes double-write impossible. New concerns (Prometheus, OpenTelemetry, per-tool granularity, Codex provider) attach as new Subscribers or new envelope fields — they never require touching role runners.

We will stop writing 1-2 PRs per cost-telemetry concern. The pattern of "discover sibling bug, file ticket, ship PR, find next sibling" ends with Phase 1.

That's the bet. It's a real refactor — maybe 600-800 lines net across the three phases. But it eliminates an entire class of bugs we've been chasing for two days.

---

## 10. References

- foreman#243 — logging coverage audit (the inventory + gap matrix)
- PRs #232 / #234 / #236 / #240 / #241 / #242 / #244 / #245 / #246 — the seven incremental cost-telemetry PRs this design supersedes
- PR #248's note on double-counting — the smell that motivated this design
- `packages/foreman/src/foreman/v3_bus_endpoint.py` — existing bus envelope plumbing (transport already exists)
- `packages/foreman/src/foreman/reconciler/exec_log.py` — existing `execution_log` API (we extend, don't replace)
