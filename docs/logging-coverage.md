# Foreman logging coverage audit

**Filed 2026-06-09** after a session-long sprint that landed cost telemetry across all four roles (PR #232, #234, #236, #240, #241, #242). Audit motivated by Jeff's question "do we feel we have all the data being collected now" — answer: mostly yes for success paths and orchestrator-caught failures; specific gaps documented below for subprocess-level silent drops and SDK prompt-cache token undercounting.

Living document. Update when surfaces change.

---

## 1. Logging surfaces

Foreman has **four** distinct persistence surfaces. Knowing which is canonical for which signal matters.

### 1.1 JSONL stats files

- **Path:** `~/.foreman/stats/<owner>__<repo>/<role>.jsonl`
- **Writer:** in-process inside each role subprocess (`stats.log_<role>_run`)
- **Schema:** common envelope (12 fields including token usage) + role-specific extras. See `stats.CommonEnvelope`.
- **What it captures:**
  - Per-call LLM token usage (`input_tokens`, `output_tokens`, `total_cost_usd`, `model_usage`, `duration_ms`, `num_turns`)
  - Role-specific outcomes + role-level structured fields (sub_request counts, finding counts, etc.)
- **What it does NOT capture:**
  - SDK cache token fields (`cache_creation_input_tokens`, `cache_read_input_tokens`) — see §3.1
  - Per-tool-call breakdowns inside the agent loop — only the roll-up
  - Token cost when subprocess dies before reaching the log call (timeout, SIGKILL, OOM) — see §3.2

### 1.2 ExecutionLog (sqlite)

- **Path:** `~/.foreman/state/reconciler.db` → `execution_log` table
- **Writer:** parent reconciler + role subprocesses (via `v3_bus_endpoint.handle_envelope`)
- **Schema:** `(id, ts, ticket_id, project, rule_name, action, outcome, details, parent_log_id)`
- **What it captures:**
  - Every reconciler rule firing (`action` + `rule_name`)
  - Every role dispatch start row + termination row (`outcome` ∈ {success, error, timeout, skipped_capacity, errored:recovery})
  - Per-dispatch role subprocess log path (`details.log_path` per foreman#119)
  - Parent/child relationships across rule firings
- **What it does NOT capture:**
  - Token usage / cost (the schema predates the cost telemetry work)
  - Role-specific structured outcomes (those live in the JSONL)

### 1.3 Daemon structured log

- **Path:** `~/.foreman/logs/daemon.log` (env `FOREMAN_LOG_DIR`)
- **Writer:** daemon process (`logger` calls throughout)
- **Schema:** JSON-lines format from Task #303
- **What it captures:** daemon-level operational events (dispatch decisions, capacity-skip messages, error backtraces). Human + grep-friendly post-mortem.
- **What it does NOT capture:** token usage / cost. Information value tapers fast — this is observability, not telemetry.

### 1.4 Per-dispatch subprocess log files

- **Path:** `<log_dir>/<role>/<issue>__<iso-timestamp>.log` (env `FOREMAN_LOG_DIR`)
- **Writer:** subprocess stdout + stderr capture (foreman#119)
- **Schema:** raw text (whatever the role subprocess writes)
- **What it captures:** full stdout + stderr of each role subprocess
- **What it does NOT capture:** structured signal. Useful for debugging a specific failed run; not aggregate.

---

## 2. Coverage matrix — token cost telemetry

The audit question: **for every (role, outcome) cell, is the token cost recorded somewhere queryable?**

| Role | Success | LLM-self-reported failure | Uncaught exception | Subprocess timeout / SIGKILL |
|---|---|---|---|---|
| Planner | ✓ JSONL | n/a (no such outcome) | ✓ JSONL (PR #236) | ✗ — see §3.2 |
| Reviewer | ✓ JSONL | ✓ JSONL (`needs_fix`) | ✓ JSONL (PR #240) | ✗ — see §3.2 |
| Worker | ✓ JSONL | ✓ JSONL (`incomplete`, `spec_invalid`) | ✓ JSONL (PR #242) | ✗ — see §3.2 |
| Fixer | ✓ JSONL | ✓ JSONL (`incomplete`) | ✓ JSONL (PR #241) | ✗ — see §3.2 |

Both `execution_log` and the daemon structured log record THAT the dispatch died (success / error / timeout / errored:recovery outcomes). They do NOT record cost. So for the killed-subprocess column we know the failure happened — we just don't know what it cost.

**Bottom line:** 12 of 16 cost cells covered. Subprocess-kill column is the structural gap.

---

## 3. Faithfulness gaps

Three real issues beyond coverage.

### 3.1 SDK cache tokens not captured (undercount)

**Symptom:** `AnthropicSDKProvider.run_agent` reads only `usage_dict["input_tokens"]` and `usage_dict["output_tokens"]` from the Claude Agent SDK's `ResultMessage`. The SDK also returns `cache_creation_input_tokens` and `cache_read_input_tokens`, which are billed at 25% and 10% of the standard input-token rate respectively.

**Where:** `packages/foreman/src/foreman/providers/anthropic_sdk.py:242-243`

**Impact:** every JSONL row understates real cost when prompt caching kicks in (which it does, by default, for multi-turn agent loops with stable system prompts). The `total_cost_usd` field IS Anthropic-computed and IS correct — it's the per-token counts that miss two SDK fields.

**Fix:** extend `UsageInfo` model + extraction with `cache_creation_input_tokens` and `cache_read_input_tokens` fields. ADDITIVE — existing readers ignore unknown keys.

### 3.2 Subprocess-killed runs leave cost invisible

**Symptom:** when a role subprocess is killed by the parent before reaching its `log_<role>_run` call site, tokens were consumed but no JSONL row lands. Kill triggers include:
- `_track_subprocess_completion` timeout (default 3600s)
- OOM kill by the OS
- Manual `docker stop` / SIGTERM during shutdown
- Disk full or other write errors AFTER the LLM call but BEFORE the stats write

**Where:** `packages/foreman/src/foreman/reconciler/v3_host.py:_track_subprocess_completion` writes the termination row to `execution_log` with outcome `timeout` or `error`, but no JSONL row is written from there.

**Impact:** failed-and-killed runs disappear from cost telemetry. The `execution_log` shows the dispatch happened and that it died, but provides no cost number.

**Fix:** when `terminate_dispatch` writes outcome `timeout` or `error`, write a complementary JSONL row from the parent side with `outcome="subprocess_killed"` (or `subprocess_timeout`) and `input_tokens=0` / `total_cost_usd=null` (we don't know — but at least the existence of the row is captured). Downstream tooling can join JSONL ⨯ execution_log on `ticket_id + timestamp` to flag killed runs.

Alternative: do nothing in JSONL but add token columns to `execution_log` so the parent can record cost there for both happy AND killed paths. More invasive.

### 3.3 Ad-hoc CLI invocations bypass `execution_log`

**Symptom:** when a human runs `foreman plan <issue>` directly (not via daemon dispatch), the role runner still writes JSONL but `execution_log` gets no row. So the JSONL ledger has runs the SQL ledger doesn't know about. Cross-correlation queries undercount JSONL costs.

**Where:** `packages/foreman/src/foreman/cli.py` — ad-hoc CLI subcommands invoke role runners without going through `dispatch_role`.

**Impact:** anyone doing development / debugging accumulates JSONL rows the daemon-side observability misses. For "real" production this is rare; for dev velocity work it's noisy.

**Fix:** either (a) document that JSONL is canonical for cost, `execution_log` is canonical for daemon-driven attempts, and accept the asymmetry; OR (b) have ad-hoc CLI write a synthetic `execution_log` row tagged `actor="manual-cli"` so the row's existence is recorded.

---

## 4. Other questions worth tracking (lower priority)

These came up during the audit but don't warrant tickets today.

- **Cross-role aggregation per ticket.** No `foreman stats summarize` CLI. JSONL files can be `cat | jq` joined by `issue_number`; doable but error-prone. File a CLI ticket when the dashboard need shows up.
- **Per-MCP-tool token granularity.** ResultMessage rolls up everything. To know "graphify-mcp consumed X% of this run's tokens", we'd need per-tool-call instrumentation at the SDK layer. Major lift; defer until concrete need.
- **Concurrent role JSONL writes.** Today `max_concurrent_dispatches=1` so no race. If we bump concurrency, the append-only JSONL writes COULD race on Windows (POSIX guarantees atomic `O_APPEND` writes ≤ PIPE_BUF; Windows doesn't). File a "audit append safety under concurrency" ticket if we raise the cap.
- **Time-clock divergence.** `duration_seconds` (orchestrator wall-clock from `time.monotonic()`) vs `duration_ms` (provider-reported API time) vs `duration_api_ms` (SDK separate field). These should be close; ratio drift might surface SDK overhead vs network. Worth a one-time investigation when the data accumulates.
- **Provider plurality.** Today the only provider is `AnthropicSDKProvider`. When a second provider lands (Codex CLI was named in foreman#227 as the trigger), the `UsageInfo` model needs to support whatever shape that provider returns. Cross-bridge the cache_tokens question at that time.

---

## 5. Prioritized open work

1. **§3.1 cache tokens** — small additive change to `UsageInfo` + extraction. ~30 min via SDD. Filing as foreman#TBD.
2. **§3.2 subprocess-killed JSONL row** — bigger touch: parent-side write path. ~1-2 hr via SDD. Filing as foreman#TBD.
3. **§3.3 ad-hoc CLI bypass** — small, but design-call needed (synth row vs accepted asymmetry). Filing as a discussion ticket.

§4 items deferred — no ticket today, revisit when concrete pull surfaces.

---

## 6. Methodology note

The five-PR sprint (#232 → #234 → #236 → #240 → #241 → #242) was a case study in incremental scoping where the underlying surface was uniform-shaped (4 roles × {success, failure}). Each ticket's "out of scope" section named the next ticket I filed. This audit doc exists so the next telemetry-adjacent feature starts from a coverage map rather than discovering siblings PR-by-PR.

Rule going forward: **before writing a ticket touching a uniform-shaped surface, draw the matrix.** File ONE big ticket that covers all cells, or a planned sequence of small tickets with explicit cross-references and ordering.
