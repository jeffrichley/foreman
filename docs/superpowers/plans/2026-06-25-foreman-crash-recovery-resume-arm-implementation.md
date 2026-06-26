# Foreman Crash Recovery — Stage 2: Resume Arm — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make a crash re-run *cheap* by resuming the interrupted role's Claude session instead of re-running it from scratch — without ever crossing sessions between roles. Correctness already shipped (Stage 1a/1b); this is the efficiency layer.

**Architecture:** A role-dispatch state, before dispatching, computes a deterministic `session_id` bound to the work identity `(ticket, role, target)` and decides fresh-vs-resume via a `resolve_dispatch` healer (observe-before-act, like `attempt_merge`). The id + resume flag thread through the existing 5-layer dispatch chain (state → `RoleDispatcher` → role subprocess env → role-core → `ProviderFacade` → `ClaudeAgentOptions` → `claude` CLI). Resume happens **only** on an exact identity match of an *interrupted* same-work attempt; every other path biases to fresh (which the Stage-1 healer makes safe).

**Tech Stack:** Python 3.12, Pydantic v2, Postgres (asyncpg) + `InMemoryTicketRepository` (the two backends after the SQLite kill), Claude Agent SDK 0.1.63, pytest (incl. live Postgres via `postgres_fixture`), `just check`. TDD.

**Design:** `docs/superpowers/specs/2026-06-25-foreman-crash-recovery-design.md` (Stage 2). **Prerequisite shipped:** Task 0 session-dir volume + startup check (#436).

## Decisions baked in (resolved with Jeff)
1. **`session_id` = deterministic `uuid5(NS, f"{ticket}:{role}:{target}:{run_key}")`** — recomputable for the dispatch decision AND persisted on the row for auditability + exact-match verification.
2. **Worktree scoping is target-scoped** (`issue-<N>` vs `impl-<N>`) — confirmed in code; the cwd wall separates spec-side from impl-side, the role-in-session_id separates the 3 spec-side roles sharing `issue-<N>`.
3. **Resume-attempt bound = 1** — one failed resume → fall straight to fresh (the healer makes it safe); mirrors a small `MAX_HEAL_ACTIONS`.
4. **Resume is uniform across all 6 role-dispatch states** — lives in the `RoleDispatchState` base, not per-role.

**Unit of resumability = the consecutive-same-state run**, NOT the raw `state_instance_id` (which changes each retry). Same state retried after a crash → resume the prior attempt's session. State *changed* → no prior same-state session → fresh.

**Routing by `execute_started_at`:** orphan with `execute_started_at IS NULL` → role never started → **fresh** (zero side-effect risk); set → role was running → **resume** arm.

**Key code refs (verified):**
- Schema: `postgres_schema.sql:29` `state_instances` (`execute_started_at` :35, `failure_phase` :41, inflight index :46).
- Record: `records.py:51` `StateInstanceRecord` (`execute_started_at` :57, `outcome_kind` :60).
- Repo surface: `repository.py` Protocol + `InMemoryTicketRepository` (`open_state_instance` :308, `mark_execute_started` :341, `close_state_instance` :361, `record_failure` :364, `list_state_instances_for_ticket` :381); Postgres mirror in `postgres_repository.py`.
- State base: `states/role_dispatch.py:30` `RoleDispatchState.execute` (calls `ctx.role_dispatcher.dispatch(role=..., state_instance_id=ctx.instance.id)`).
- Dispatcher: `subprocess_dispatcher.py:221` `dispatch(...)`; `:247-248` sets `FOREMAN_STATE_INSTANCE_ID` env when not None — the pattern `session_id`/`resume` mirror.
- Role-core env reads: `roles/{planner,reviewer,fixer,worker}.py` each read `FOREMAN_STATE_INSTANCE_ID` (planner:449, reviewer:617, worker:1362, fixer:722).
- Provider: `providers/anthropic_sdk.py:295` `run_agent(..., env=...)`; `options_kwargs` built :308-318 → `ClaudeAgentOptions(**options_kwargs)` :318. `session_id`/`resume`/`fork_session` are `ClaudeAgentOptions` fields the SDK forwards to `claude --session-id/--resume`.
- Contract suite: `tests/v4/_repository_contract.py` (runs InMemory + Postgres).

---

## File structure
- `postgres_schema.sql` — add `session_id TEXT` to `state_instances`.
- `records.py` — `StateInstanceRecord.session_id: str | None`.
- `repository.py` / `postgres_repository.py` — read `session_id` in row mapping; add `set_session_id(instance_id, session_id)` to the Protocol + both impls.
- `session_ids.py` (**new**) — deterministic `derive_session_id(ticket_id, role, target, run_key)` + the namespace UUID.
- `states/resolve_dispatch.py` (**new**) — `resolve_dispatch(ctx) -> DispatchPlan` (fresh | resume) + `DispatchPlan` dataclass.
- `states/role_dispatch.py` — `execute()` calls `resolve_dispatch`, stamps `session_id`, passes `session_id`+`resume` to dispatch.
- `role_dispatcher.py` (Protocol + Fake) + `subprocess_dispatcher.py` — `dispatch()` gains `session_id`/`resume` params; subprocess sets `FOREMAN_SESSION_ID`/`FOREMAN_RESUME_SESSION_ID` env.
- `roles/{planner,reviewer,fixer,worker}.py` — read the new env vars, pass `session_id`/`resume` to `run_agent`.
- `providers/anthropic_sdk.py` (+ `provider.py` Protocol/Fake) — `run_agent` gains `session_id`/`resume`; sets them in `options_kwargs`.
- Tests: contract test for `set_session_id`; `test_session_ids.py`; `test_resolve_dispatch.py` (incl. anti-mixing); a resumed-run validation test.

---

### Task 1: Schema + record + repo `session_id` column

**Files:** `postgres_schema.sql`, `records.py`, `repository.py`, `postgres_repository.py`, contract test.

- [ ] **Step 1: Failing contract test.** In `tests/v4/_repository_contract.py`, add a test: open a state instance, call `repo.set_session_id(inst.id, "sess-abc")`, reload via `list_state_instances_for_ticket`, assert the record's `session_id == "sess-abc"` and that a freshly-opened instance has `session_id is None`. Run against InMemory binding with `-o addopts=""` → FAIL (no such method/field).
- [ ] **Step 2: Add the column + field.** `postgres_schema.sql`: add `session_id TEXT` to `state_instances` (nullable, after `failure_reason`). `records.py`: add `session_id: str | None` to `StateInstanceRecord` (default `None` so existing construction sites stay valid — confirm with `git grep "StateInstanceRecord("`).
- [ ] **Step 3: Repo plumbing.** Add `set_session_id(self, instance_id: int, session_id: str) -> None` to the `TicketRepository` Protocol; implement in `InMemoryTicketRepository` (mutate the stored record) and `PostgresTicketRepository` (`UPDATE state_instances SET session_id=$1 WHERE id=$2`). Include `session_id` in every row→`StateInstanceRecord` mapping in both impls (InMemory dict, Postgres SELECT column list).
- [ ] **Step 4: Run the contract suite** against InMemory + Postgres: `uv run pytest packages/foreman/tests/v4/test_in_memory_repository.py packages/foreman/tests/v4/test_postgres_repository.py -o addopts="" -q`. Expected green.
- [ ] **Step 5: Commit** — `feat(repo): add session_id column to state_instances`.

---

### Task 2: Deterministic `session_id` derivation

**Files:** `session_ids.py` (new), `test_session_ids.py` (new).

- [ ] **Step 1: Failing test.** Assert `derive_session_id(ticket_id=1, role="planner", target=None, run_key="seq-3")` is a valid UUID string, is **stable** across calls, and **differs** for any change in ticket/role/target/run_key (4 inequality assertions). Run → FAIL.
- [ ] **Step 2: Implement.** `derive_session_id(ticket_id: int, role: str, target: str | None, run_key: str) -> str` = `str(uuid.uuid5(_NS, f"{ticket_id}:{role}:{target}:{run_key}"))` with a fixed module-level `_NS = uuid.UUID("…")` (hardcode one generated value — do NOT call `uuid4()` at import). `target` normalized (`None` → the literal `"none"`). Document that `run_key` is the consecutive-same-state run identity (Task 4 supplies it).
- [ ] **Step 3: Run → PASS. Commit** — `feat: deterministic session_id derivation bound to (ticket, role, target, run)`.

---

### Task 3: Thread `session_id` + `resume` through the dispatch chain (no decision logic yet)

Pure plumbing: carry two optional params end-to-end; behavior unchanged when both are None/False.

**Files:** `provider.py` (Protocol/Fake) + `providers/anthropic_sdk.py`; `role_dispatcher.py` (Protocol/Fake) + `subprocess_dispatcher.py`; `roles/{planner,reviewer,fixer,worker}.py`.

- [ ] **Step 1: Provider.** Add `session_id: str | None = None` and `resume: bool = False` to `run_agent` (Protocol + `AnthropicSDKProvider` + any Fake). In `anthropic_sdk.py`, when `session_id` is set add `options_kwargs["session_id"] = session_id`; when `resume` is True add `options_kwargs["resume"] = session_id` (resume forwards the id to `claude --resume`). Test: a fake/inspection asserts the kwargs carry them.
- [ ] **Step 2: Role-core.** In each of the 4 role modules, read `FOREMAN_SESSION_ID` and `FOREMAN_RESUME_SESSION_ID` from `os.environ` (mirroring the existing `FOREMAN_STATE_INSTANCE_ID` read), and pass `session_id=` / `resume=` into the `run_agent` call. `resume` is True iff `FOREMAN_RESUME_SESSION_ID` is set and equals `FOREMAN_SESSION_ID`.
- [ ] **Step 3: Dispatcher.** Add `session_id: str | None = None`, `resume: bool = False` to `RoleDispatcher.dispatch` (Protocol + Fake + `SubprocessRoleDispatcher`). In `subprocess_dispatcher.py`, when set, add `env["FOREMAN_SESSION_ID"] = session_id` and (if resume) `env["FOREMAN_RESUME_SESSION_ID"] = session_id` — same conditional-env pattern as `FOREMAN_STATE_INSTANCE_ID` at :247.
- [ ] **Step 4: Run** the provider + dispatcher + role-core unit suites with `-o addopts=""`. Expected green (params are optional; nothing passes them yet).
- [ ] **Step 5: Commit** — `feat: thread session_id + resume through the role dispatch chain (inert)`.

---

### Task 4: `resolve_dispatch` healer — the fresh-vs-resume decision

The heart. Observe-before-act, like `attempt_merge`. **No positional/"latest session" path exists** — resume only on an exact, verified identity match.

**Files:** `states/resolve_dispatch.py` (new), `test_resolve_dispatch.py` (new).

- [ ] **Step 1: Define `DispatchPlan`.** Frozen dataclass: `session_id: str`, `resume: bool`. (`session_id` is always set — it names the session even on a fresh run; `resume` gates `--resume`.)
- [ ] **Step 2: Write the decision tests FIRST** (drive the logic):
  - No prior same-state attempt in this run → `resume=False` (fresh), `session_id` = derived for the current run.
  - Prior same-state attempt, `execute_started_at IS NULL` (never started) → fresh.
  - Prior same-state attempt, `execute_started_at` set, **not** completed (interrupted), recorded `session_id` matches the derived id → `resume=True` with that id.
  - Prior attempt completed (has `outcome_kind`) → fresh (never resume a finished session).
  - **Resume bound:** ≥1 prior resume attempt already in this run that failed → fresh (don't re-resume a poison session).
- [ ] **Step 3: Implement `resolve_dispatch(ctx) -> DispatchPlan`.** Compute `run_key` from the current consecutive-same-state run (the same window `count_consecutive_same_state` uses — derive from `list_state_instances_for_ticket` filtered to the trailing run of `ctx.instance.state_name`). Derive `session_id` via Task 2. Walk the prior same-state instances in the current run; apply the routing rules above; the **only** path to `resume=True` is an interrupted prior attempt whose stored `session_id` equals the derived id AND the resume-bound isn't exhausted.
- [ ] **Step 4: Run → PASS. Commit** — `feat: resolve_dispatch healer (fresh vs verified resume)`.

---

### Task 5: Wire `resolve_dispatch` + session stamping into `RoleDispatchState.execute`

**Files:** `states/role_dispatch.py`, `test_role_dispatch.py` (or the lifecycle tests).

- [ ] **Step 1: Failing test.** Drive `RoleDispatchState.execute` with a fake dispatcher; assert it (a) calls `resolve_dispatch`, (b) calls `repo.set_session_id(ctx.instance.id, <derived id>)` before dispatch, (c) forwards `session_id`+`resume` to `dispatch(...)`.
- [ ] **Step 2: Implement.** In `execute()`, before the `dispatch(...)` call: `plan = resolve_dispatch(ctx)`, `ctx.repo.set_session_id(ctx.instance.id, plan.session_id)`, then `ctx.role_dispatcher.dispatch(..., session_id=plan.session_id, resume=plan.resume)`. Uniform for all 6 states (base class). `mark_execute_started` already records `execute_started_at` — confirm it fires before/around dispatch so the routing signal exists for the next attempt.
- [ ] **Step 3: Run** the role-dispatch + lifecycle suites. Expected green. **Commit** — `feat: role-dispatch states stamp session_id + resume verified sessions`.

---

### Task 6: Anti-mixing tests (LOAD-BEARING)

A wrong-role resume is catastrophic. These are the safety proof, per the design.

**Files:** `test_resolve_dispatch.py` (extend), an integration test.

- [ ] **Step 1:** Assert `resolve_dispatch` returns **fresh** for every cross-identity case: prior attempt with a stored `session_id` from a **different role**, **different ticket**, **different target**, and a **completed** session — each must NOT resume. (Construct rows with mismatched stored ids; the derived id won't match → fresh.)
- [ ] **Step 2:** Assert the derived ids are distinct by construction across `(ticket, role, target)` (ties Task 2 to the wall).
- [ ] **Step 3:** Document the **cwd wall** as the third independent guard in a test comment (worktrees are per-`(ticket,target)`; spec-side roles share `issue-<N>` but are separated by the role-in-session_id). **Commit** — `test: adversarial cross-role/ticket/target session-mixing → always fresh`.

---

### Task 7: Validation gate — a resumed run still emits `FOREMAN_OUTCOME`

The spike left this unproven (it used plain text/files, not the structured `output_format=json_schema`). **Must pass before this ships.**

- [ ] **Step 1:** Add a test that exercises a resumed `run_agent` (session_id set, resume=True) against the structured-output path and asserts a valid `FOREMAN_OUTCOME` is still parsed. If a pure unit can't reach the real SDK behavior, mark this as a **real-engine validation step** (like the spike) and run it manually in-container — document the result in the PR. Do NOT ship Stage 2 until this is green.
- [ ] **Step 2: Commit** — `test: resumed role run still emits structured FOREMAN_OUTCOME`.

---

### Task 8: Full gate + PR

- [ ] **Step 1:** `just check` fully green (ruff + mypy + import-linter + full pytest incl. live Postgres + coverage ≥ 78%).
- [ ] **Step 2:** Adversarial review of the whole diff — focus the hostile pass on the resume decision and the anti-mixing walls (the catastrophic-if-wrong surface).
- [ ] **Step 3:** Push (Wren PAT, GH_TOKEN env only, never echoed; pre-push runs `just check`). Open the PR (no `Co-Authored-By`); link the design + #436. Surface the URL; do not merge — Jeff reviews.

---

## Self-review checklist
1. **Spec coverage:** session_id stamp (T1/T2/T5) ✓; resolve_dispatch healer + routing + bound (T4) ✓; 5-layer plumbing (T3) ✓; uniform across 6 states (T5) ✓; anti-mixing (T6) ✓; validation gate (T7) ✓.
2. **Fail-safe-to-fresh:** every non-exact-match path returns fresh; the only `resume=True` path requires an interrupted same-work attempt with a matching stored id within the current run.
3. **Two-backend world:** schema touches Postgres + InMemory only (SQLite gone). Contract suite covers both.
4. **Ordering:** schema (T1) → derivation (T2) → inert plumbing (T3) → decision (T4) → wiring (T5) → safety (T6) → validation (T7). Each task leaves the suite green.

## Out of scope (tracked separately)
- Postgres DR backup — #434.
- `max_in_flight` per-project / review items I1–I3.
