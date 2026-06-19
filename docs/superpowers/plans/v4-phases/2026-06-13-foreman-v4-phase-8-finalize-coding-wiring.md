> **Parent plan:** [../2026-06-13-foreman-v4-substrate-redesign-implementation.md](../2026-06-13-foreman-v4-substrate-redesign-implementation.md) — read its v4 isolation principle first.
> **Spec:** [../../specs/2026-06-13-foreman-v4-substrate-redesign-design.md](../../specs/2026-06-13-foreman-v4-substrate-redesign-design.md).
> **Branch:** `feat/foreman-v4-substrate`.
> **Gate at end:** `just check` green + manual dogfood smoke (`uv run foreman daemon start` against a real config processes at least one real ticket end-to-end through a real `foreman` subprocess). Then stop for human review before Phase 9 deletion.

## Phase 8 — Finalize coding + wiring (dogfood-ready)

Phase 7 closed the substrate work but left the production binary unable to actually run: `main()` has `# type: ignore` covering an identity-registry wiring gap, the EventBus is wired post-construction by reaching into `_bus` private attrs, and no test has ever exercised the real `foreman` binary end-to-end against the real `SubprocessRoleDispatcher`. Before Phase 9 deletes v3 (irreversible without revert), Phase 8 makes v4 actually production-runnable AND verifies it with a real dogfood ticket.

### Why this is a separate phase

Phase 7 reviews kept surfacing items that should have been in Phase 7 but landed in Phase 8 by default. Splitting them out:
- Keeps Phase 9 truly mechanical (delete + docs + push), as originally intended
- Makes the "v4 actually runs" gate explicit and verifiable
- Gives the dogfood verification a defined home, instead of trusting "all unit tests pass" as proxy

### Task 8.1: `bootstrap_cli_context()` owns the EventBus + standard observers

**Files:**
- Modify: `packages/foreman/src/foreman/v4/bootstrap.py`
- Modify: `packages/foreman/tests/v4/test_bootstrap.py` (extend)
- Modify: `packages/foreman/tests/v4/test_phase7_e2e.py` (drop the manual `_bus`/`_pool._bus` reach-ins)

The bootstrap should construct an `EventBus`, subscribe the four standard observers (`StructuredLogObserver`, `LabelObservabilityObserver`, `EventArchiveObserver`, `MetricsObserver`), and thread the bus into `Daemon` at construction. After this lands, `ctx.daemon._bus` reach-ins disappear from tests + production wiring.

The `EventArchiveObserver` needs the SQLite connection — bootstrap already has the repo, so it can call `EventArchiveObserver(conn=repo._conn)` or the equivalent public accessor. If the connection is only available privately, add a `SqliteTicketRepository.connection` property (read-only) as part of this task.

- [ ] **Step 1: Extend `bootstrap_cli_context` to subscribe observers + pass bus to Daemon**
- [ ] **Step 2: Update Phase 7.6 e2e test to drop `_bus`/`_pool._bus` reach-ins**
- [ ] **Step 3: Add a bootstrap test verifying `ctx.daemon._bus is not None` (or via a `bus` property)**
- [ ] **Step 4: `just check` green**
- [ ] **Step 5: Commit** — `feat(v4): bootstrap owns EventBus + subscribes 4 standard observers`

### Task 8.2: Public `Daemon.shutdown()` method

**Files:**
- Modify: `packages/foreman/src/foreman/v4/daemon.py`
- Modify: `packages/foreman/src/foreman/v4/cli/daemon.py` (use the public shape)
- Modify: `packages/foreman/tests/v4/test_daemon.py` (extend)

Phase 7.6 e2e reaches into `_pool` directly for shutdown. Add `Daemon.shutdown(*, wait: bool = True)` that calls `self._pool.shutdown(wait=wait)` then cleans up handler state if needed. `cmd_daemon_stop` and tests both call the public method.

- [ ] **Step 1: Add `shutdown(*, wait: bool = True)` to Daemon**
- [ ] **Step 2: Update `cmd_daemon_stop` to call `daemon.shutdown()` before clearing PID file** (the current shape does SIGTERM remote-kill; this task adds the in-process shutdown for the foreground-start case)
- [ ] **Step 3: Add a Daemon test asserting `shutdown(wait=True)` drains in-flight work**
- [ ] **Step 4: `just check` green**
- [ ] **Step 5: Commit** — `feat(v4): public Daemon.shutdown() method`

### Task 8.3: V4Config `[apps]` + `[orchestrator]` sections

**Files:**
- Modify: `packages/foreman/src/foreman/v4/config.py` (extend V4Config)
- Modify: `packages/foreman/tests/v4/test_config.py` (extend)

Add to `V4Config`:

```python
class AppCredentials(BaseModel):
    app_id: int
    private_key_path: str  # PEM file on disk

class AppsConfig(BaseModel):
    planner: AppCredentials
    reviewer: AppCredentials
    fixer: AppCredentials
    worker: AppCredentials

class OrchestratorConfig(BaseModel):
    pat_env_var: str = "FOREMAN_ORCHESTRATOR_PAT"

class V4Config(BaseModel):
    # ... existing fields ...
    apps: AppsConfig
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
```

TOML shape:

```toml
[daemon]
db_path = "/path/to/foreman.db"
log_dir = "/path/to/logs"
# ...

[apps.planner]
app_id = 12345
private_key_path = "/path/to/planner-key.pem"

[apps.reviewer]
app_id = 12346
private_key_path = "/path/to/reviewer-key.pem"

# ... fixer, worker ...

[orchestrator]
pat_env_var = "FOREMAN_ORCHESTRATOR_PAT"  # default

[[projects]]
# ... existing per-project shape ...
```

Add tests: required `[apps.*]` block missing → ValidationError; valid full-config round-trips.

- [ ] **Step 1: Extend Pydantic models (AppCredentials, AppsConfig, OrchestratorConfig)**
- [ ] **Step 2: Add 4 new test cases (missing apps block, missing role app, full round-trip, orchestrator default)**
- [ ] **Step 3: `just check` green**
- [ ] **Step 4: Commit** — `feat(v4): V4Config — apps + orchestrator sections for IdentityRegistry construction`

### Task 8.4: Real IdentityRegistry construction in `main()`

**Files:**
- Modify: `packages/foreman/src/foreman/v4/cli/__init__.py` (rewrite `main()`)
- Modify: `packages/foreman/src/foreman/v4/bootstrap.py` (accept a built IdentityRegistry rather than a stub)
- Test: `packages/foreman/tests/v4/test_main.py` (new — verifies `main()` builds the registry from config without crashing)

The current `main()` does `from foreman import identity` and passes the module — but `foreman.identity` exposes `IdentityRegistry` (a class), not a top-level `get_role_token` function. The 2 `# type: ignore` comments hide the gap.

Fix: in `main()`, construct an `IdentityRegistry` from `config.apps` + `config.orchestrator` (load the per-role app credentials from disk + read the orchestrator PAT from the env var), then pass the registry to `bootstrap_cli_context` as the `identity` parameter. The registry exposes `get_role_token(role: str) -> str` matching the bootstrap's `IdentityProvider` Protocol — no type-ignores needed.

The test uses `tmp_path` to write dummy PEM files + sets the env var + verifies `main()` exits cleanly (no traceback) when invoked with `--help` (which short-circuits before any real work).

- [ ] **Step 1: Construct IdentityRegistry from V4Config in main()**
- [ ] **Step 2: Drop the 2 `# type: ignore` comments**
- [ ] **Step 3: Write `test_main.py` with the `--help` smoke**
- [ ] **Step 4: `just check` green — confirms the type-ignore drop didn't break mypy**
- [ ] **Step 5: Commit** — `feat(v4): main() builds real IdentityRegistry from V4Config`

### Task 8.5: `reset_logging()` on daemon reload

**Files:**
- Modify: `packages/foreman/src/foreman/v4/cli/daemon.py` (cmd_daemon_reload)
- Modify: `packages/foreman/tests/v4/cli/test_daemon_commands.py` (extend)

`cmd_daemon_reload` currently sends SIGHUP and returns. After Phase 8.4's bootstrap consolidation, reload should call `reset_logging()` + `configure_logging(...)` so file handlers don't stack on repeated reloads. Small change.

The current shape sends SIGHUP to a remote process; the handler that receives SIGHUP would need to call the reset+configure. In the foreground-start case (where `cmd_daemon_start` ran `daemon.run_forever()` in-process), the SIGHUP handler is installed in `cmd_daemon_start`. Add the reset+reconfigure to that handler.

- [ ] **Step 1: Update `cmd_daemon_start`'s SIGHUP handler to reset+reconfigure logging**
- [ ] **Step 2: Add a daemon-commands test verifying handlers don't stack on simulated SIGHUP**
- [ ] **Step 3: `just check` green**
- [ ] **Step 4: Commit** — `feat(v4): daemon reload resets + reconfigures logging`

### Task 8.6: Real-fork integration test under bootstrap harness

**Files:**
- Create: `packages/foreman/tests/v4/test_phase8_real_fork.py`

This is the carryover from Phase 5/7 reviews — no current test exercises the full chain `Poller → QM → WorkerPool → SubprocessRoleDispatcher → real foreman CLI → real role → real OUTCOME → state advance`. Phase 5.7 used a stub Python script; Phase 7.6 monkey-patched the dispatcher.

Build a test that invokes the actual installed `foreman plan-via-stub-role` subprocess. The stub role is a real `foreman.roles.planner.run_planner_cli` invocation but with the underlying provider mocked at the `foreman.providers` boundary (the layer that talks to Anthropic). This proves the typer command + bootstrap + dispatcher + parser chain works against the real binary even if the role's actual work is mocked.

Approach:
- Use `subprocess.run([sys.executable, "-m", "foreman.v4.cli", "plan", "--project", "p", "--issue-number", "1"], ...)` from the test
- The role internals patch their provider layer to return a canned PlannerOutput
- Verify FOREMAN_OUTCOME parses cleanly from stdout
- Verify exit code propagates

If the role internals aren't cleanly mockable from outside, fall back to a flag like `FOREMAN_DRY_RUN=1` that the role reads and short-circuits with a canned outcome. Either way: the real binary IS invoked, the real dispatcher IS used, the real parser IS used.

- [ ] **Step 1: Decide mock surface (provider boundary vs FOREMAN_DRY_RUN flag)**
- [ ] **Step 2: Write the test**
- [ ] **Step 3: Run + verify**
- [ ] **Step 4: `just check` green**
- [ ] **Step 5: Commit** — `test(v4): real-fork integration test against installed foreman binary`

### Task 8.7: Dogfood — drive one real ticket end-to-end on a test repo

**This is a manual task, not a TDD task.**

Set up a sacrificial test repo on GitHub (something small like a CONTRIBUTING.md typo fix). Create `~/.foreman/v4/config.toml` with the test repo configured. Add the `foreman:plan` label to an issue. Run `uv run foreman daemon start` and watch the daemon process the ticket through the full v4 chain to completion.

This is the empirical evidence that v4 works against real GitHub + real Claude. If something breaks, fix it in v4 — DO NOT proceed to Phase 9 deletion until at least one real ticket reaches a terminal state.

Acceptance criteria (write down what you saw):
- [ ] Ticket created and adopted by the Poller
- [ ] Planner role invoked via subprocess (verify in process list / log)
- [ ] Spec PR opened on GitHub
- [ ] Reviewer role invoked
- [ ] (Optional, depends on how complete the chain needs to be) Worker, ImplReview, Merging stages exercised
- [ ] At least one ticket transitions to Done in `~/.foreman/v4/state.db`
- [ ] `transitions.jsonl` populated with the state journey

If the test repo + ticket flow isn't yet possible (e.g., apps aren't set up for the test repo), document what was tried + what blocked. Phase 9 cannot proceed until this task reports actual ticket-to-terminal behavior.

If 8.7 surfaces production-runnability gaps (as the 2026-06-15 algokit run did — role CLIs hard-imported v3 config; no `foreman init`; Windows OSError on daemon-status), file Phase 8b at [phase-8b-role-cli-rewire.md](./2026-06-15-foreman-v4-phase-8b-role-cli-rewire.md), land its tasks, then re-run the dogfood there.

### Phase 8 gate

- [ ] `just check` green
- [ ] Task 8.7 manual dogfood reports successful end-to-end transition

Phase 8 completion criterion: **v4 actually works against real GitHub + real Claude end-to-end.** Phase 9 (deletion + RUNBOOK + PR) is safe to execute once Phase 8 + (if needed) Phase 8b both report success.

---
