# Foreman v4 Phase 8d — Role-side v3 cleanup (make v4 actually run end-to-end)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Parent plan:** [../2026-06-13-foreman-v4-substrate-redesign-implementation.md](../2026-06-13-foreman-v4-substrate-redesign-implementation.md) — read its v4 isolation principle first.
> **Recon (read this):** [../../recon/2026-06-15-v4-phase-9-v3-recon.md](../../recon/2026-06-15-v4-phase-9-v3-recon.md) — the file-by-file v3 inventory + v3-coupling line list. Phase 8d is driven by it.
> **Branch:** `feat/foreman-v4-substrate`.
> **Gate at end:** `just check` green + algokit#21 dogfood reaches **terminal `Done`** (spec PR merged, impl PR opened, reviewed, merged, issue closed) AND daemon survives >60min. This is the empirical proof v4 actually runs autonomously.

**Goal:** Strip every v3-state-machine concern from the survival-set role CLIs so v4's "SQLite is gospel, labels are write-only observability" principle holds end-to-end. After 8d, the role CLIs do not read labels for decisions, do not write `foreman:*` labels for state, and use only `V4IdentityRegistry` + `V4Config`. Phase 9 then becomes pure deletion.

**Architecture:** Four role CLIs (`planner`, `reviewer`, `fixer`, `worker`) still carry v3 baggage — preflight label gates, post-execute label mutations, attempt-counter label writes, escape-hatch `foreman:needs-help` writes, and import of v3 `IdentityRegistry` + `Config`. 8d gutes the label code, ports identity + config to v4, moves the trigger-label removal into `LabelObservabilityObserver`, and rewires `foreman init` to write `V4Config`.

**Tech Stack:** Existing v4 stack — `foreman.v4.identity.V4IdentityRegistry`, `foreman.v4.config.V4Config`, `foreman.v4.observers.label_observability.LabelObservabilityObserver`. No new dependencies.

**Architectural decisions committed here (from Jeff sync 2026-06-15):**

1. **Identity:** roles use `V4IdentityRegistry` directly. v3 `identity.py` survives until Phase 9 only because the rip-out is one direction (port roles → delete v3) and we don't want to do both in one commit.
2. **Config:** roles use `V4Config` directly. Drop the `V3*` import aliases in `foreman.v4.cli.__init__`.
3. **`foreman:needs-help` writes:** dropped from role-side. v4 transitions to `NeedsHelp` state on any role failure, and `LabelObservabilityObserver` writes `foreman:state-needs-help` — one label, one source, correct v4 namespace.
4. **`foreman:plan` removal:** moved from `roles/planner.py` to `LabelObservabilityObserver` — observer removes the trigger label on first state transition (Queued or Planning entry). Centralizes the "trigger is one-shot" rule.
5. **No new sticky label.** `foreman:state-*` already carries "this is foreman-managed." No `foreman:work` rename.
6. **Phase 9 = pure deletion.** All v4-compat surgery lives in 8d. Phase 9 is delete-only.

---

## File Structure

**Modify (no creation):**
- `packages/foreman/src/foreman/v4/cli/daemon.py` — revert 8c.5's narrowed `_is_pid_alive`
- `packages/foreman/tests/v4/cli/test_daemon_commands.py` — drop 3 winerror-87 tests + the `_winerror_87_oserror` helper
- `packages/foreman/src/foreman/roles/planner.py` — drop L395-406 label mutation, switch to V4 identity + V4Config
- `packages/foreman/src/foreman/roles/reviewer.py` — drop preflight gate (L67-80, L537-558) + post-execute mutation (L641-675) + label constants, switch to V4 identity + V4Config
- `packages/foreman/src/foreman/roles/fixer.py` — drop preflight gates + label writes + label constants, switch to V4 identity + V4Config
- `packages/foreman/src/foreman/roles/worker.py` — drop preflight gates + label writes + label constants, switch to V4 identity + V4Config
- `packages/foreman/src/foreman/roles/__init__.py` — drop `TERMINAL_BLOCKING_LABEL` + `set_needs_help_label` callback plumbing in `handle_unhandled_role_exception`
- `packages/foreman/src/foreman/v4/observers/label_observability.py` — add `foreman:plan` removal on Queued / Planning state entry
- `packages/foreman/src/foreman/v4/cli/__init__.py` — drop `V3*` import aliases (or change to direct `V4Config` references)
- `packages/foreman/src/foreman/init.py` — rewrite `_write_project_block_to_config` + `_format_project_block` to write `V4Config`-shaped TOML; rewire `run_init` to take + write a `V4Config` instead of `Config`
- Tests under `packages/foreman/tests/test_roles_*.py` and `packages/foreman/tests/test_init.py` — drop tests that pin v3 label gates, update tests that mock v3 identity/config to mock v4 instead

---

### Task 8d.0: Revert 8c.5 — drop Windows-narrowed `_is_pid_alive`

**Files:**
- Modify: `packages/foreman/src/foreman/v4/cli/daemon.py` (`_is_pid_alive`, `cmd_daemon_stop` race-window catch)
- Modify: `packages/foreman/tests/v4/cli/test_daemon_commands.py` (drop 3 tests + `_winerror_87_oserror` helper)

The 8c.5 narrowing only passed mocked tests; real Windows `os.kill(pid, 0)` raises `winerror=87` on every PID (alive or dead) because signal 0 isn't a Windows API. Docker prod is POSIX, where `os.kill(pid, 0)` works correctly — so the right fix is to revert 8c.5 and document the Windows-native dev-only caveat in the eventual RUNBOOK.

Restore the pre-8c.5 broad `(ProcessLookupError, OSError)` catch in `_is_pid_alive` (or just `ProcessLookupError` if that was the shape before 8b.4). Drop the `_winerror_87_oserror` helper and three tests that wired it up.

- [ ] **Step 1:** Revert `_is_pid_alive` body to the pre-8c.5 shape (POSIX-correct via `os.kill(pid, 0)` + `ProcessLookupError`)
- [ ] **Step 2:** Revert `cmd_daemon_stop`'s race-window catch to its pre-8c.5 shape
- [ ] **Step 3:** Delete `_winerror_87_oserror` helper and the 3 tests that use it (`test_status_when_pid_stale_windows_oserror`, `test_stop_when_pid_stale_windows_oserror`, `test_pid_alive_helper_treats_winerror_87_as_dead`, `test_pid_alive_helper_treats_winerror_5_as_alive`)
- [ ] **Step 4:** `just check` green
- [ ] **Step 5:** Commit — `revert(v4): drop 8c.5 windows-narrowed _is_pid_alive (docker prod = posix)`

---

### Task 8d.1: Port role CLIs to `V4IdentityRegistry`

**Files:**
- Modify: `packages/foreman/src/foreman/roles/planner.py`
- Modify: `packages/foreman/src/foreman/roles/reviewer.py`
- Modify: `packages/foreman/src/foreman/roles/fixer.py`
- Modify: `packages/foreman/src/foreman/roles/worker.py`
- Modify: `packages/foreman/src/foreman/v4/cli/__init__.py` (if role-CLI signatures change)
- Modify: tests under `packages/foreman/tests/test_roles_*.py` that mock v3 `IdentityRegistry`

Today the role CLIs import `from foreman.identity import IdentityRegistry` and call `reg.get_client(role)` to mint PyGithub clients. v4 has `foreman.v4.identity.V4IdentityRegistry` with `get_role_token(role) -> str` (returns the token; caller builds the PyGithub client). The two APIs differ by one indirection — V4 returns the token, V3 returns the client.

Two ways to do this. Pick **option B** (uniform v4 surface):

- ~~Option A:~~ add `get_client(role)` method to V4IdentityRegistry. Smallest role-side diff, but extends V4 with a method that exists only to ease migration.
- **Option B (chosen):** in each role CLI, replace `client = identity.get_client(role)` with `client = Github(identity.get_role_token(role))`. Slightly more lines per role but keeps V4IdentityRegistry tight and matches how v4's PyGithubGitProvider already does it.

For tests, mocks that returned a `Github` from `get_client` now return a token string from `get_role_token`, and tests assert `Github(token_str)` was called.

- [ ] **Step 1:** Audit every `IdentityRegistry` use in `roles/planner.py`, `roles/reviewer.py`, `roles/fixer.py`, `roles/worker.py` — record file:line for each
- [ ] **Step 2:** Replace v3 `IdentityRegistry` import + use with `V4IdentityRegistry` + `get_role_token` + `Github(token)` per role
- [ ] **Step 3:** Update mocks in `tests/test_roles_*.py` for each role's tests
- [ ] **Step 4:** Run `pytest packages/foreman/tests/test_roles_*.py -q` — all green
- [ ] **Step 5:** `just check` green
- [ ] **Step 6:** Commit — `feat(roles): port planner/reviewer/fixer/worker to V4IdentityRegistry`

---

### Task 8d.2: Port role CLIs to `V4Config`

**Files:**
- Modify: same 4 role files as 8d.1
- Modify: `packages/foreman/src/foreman/v4/cli/__init__.py` (drop `V3*` import aliases)
- Modify: `packages/foreman/tests/test_roles_*.py` — change config-fixture shape

Today each `run_<role>_cli` takes a `Config` (v3) or constructs one internally from env. Replace with `V4Config` everywhere. The fields the roles actually USE are: `ProjectConfig.repo`, `ProjectConfig.local_clone_path`, `ProjectConfig.check_command`, `ProjectConfig.dev_base_branch`, `ProjectConfig.max_fix_attempts`, `ProjectConfig.max_impl_attempts`, `ProjectConfig.trigger_label`. V4's `ProjectConfig` already has all of these (Phase 8b.2 added them — see `v4/config.py:88-128`).

Drop the `V3Config / V3ProjectConfig / V3AppsConfig / V3OrchestratorConfig` aliases from `v4/cli/__init__.py`; they exist only as a v3→v4 bridge that 8d.1 + 8d.2 retire.

- [ ] **Step 1:** Audit every `Config` / `ProjectConfig` (v3) use in the 4 role files — record file:line + field accessed
- [ ] **Step 2:** Replace v3 `Config` import with `V4Config` in each role file; replace `config.projects[<n>]` access with the V4 equivalent (same field names)
- [ ] **Step 3:** Drop the `V3*` import aliases from `v4/cli/__init__.py`
- [ ] **Step 4:** Update test fixtures in `tests/test_roles_*.py` to build `V4Config` instead of `Config`
- [ ] **Step 5:** Run `pytest packages/foreman/tests/test_roles_*.py -q` — all green
- [ ] **Step 6:** `just check` green
- [ ] **Step 7:** Commit — `feat(roles): port planner/reviewer/fixer/worker to V4Config`

---

### Task 8d.3: Strip v3 label code from Planner

**Files:**
- Modify: `packages/foreman/src/foreman/roles/planner.py` (drop L395-406 label mutation)
- Modify: `packages/foreman/tests/test_roles_planner.py` — drop tests that assert label transitions on the issue

The Planner currently removes `foreman:plan` and adds `foreman:planning` after the spec PR opens (`roles/planner.py:395-406`). Under v4, the observer handles all `foreman:state-*` transitions, and 8d.7 will move the `foreman:plan` removal into the observer too. Planner should not touch labels at all post-execute.

- [ ] **Step 1:** Delete the label-mutation block at `roles/planner.py:395-406`
- [ ] **Step 2:** Delete any test in `tests/test_roles_planner.py` that asserts label state on the issue post-run (these become observer responsibilities)
- [ ] **Step 3:** Run `pytest packages/foreman/tests/test_roles_planner.py -q` — all green
- [ ] **Step 4:** `just check` green
- [ ] **Step 5:** Commit — `refactor(roles): strip v3 label mutation from Planner — observer owns labels`

---

### Task 8d.4: Strip preflight gate + label mutation from Reviewer

**Files:**
- Modify: `packages/foreman/src/foreman/roles/reviewer.py`
- Modify: `packages/foreman/tests/test_roles_reviewer.py`

Sites to remove (verified by 8d recon):
- `_ReviewerPreflightRefusal` class at L67-80
- `_LABEL_SPEC_REVIEW`, `_LABEL_SPEC_READY`, `_LABEL_SPEC_FIX`, `_LABEL_IMPL_REVIEW`, `_LABEL_READY_FOR_MERGE`, `_LABEL_IMPL_FIX` constants at L91-96
- `_REVIEWER_ENTRY_LABEL_BY_TARGET` if present
- Preflight gate block at L537-558 (read `issue.labels`, raise if entry label missing)
- Post-execute label-mutation block at ~L641-675 (set_labels call site)
- Any imports that become unused after the drops

What stays:
- The Reviewer's CORE logic — prompt loading, worktree attach, PR diff fetch, LLM call, FOREMAN_OUTCOME emission, PR review post
- All the v4-compatible side effects (posting the review comment on the PR, emitting stats)

- [ ] **Step 1:** Delete the `_ReviewerPreflightRefusal` class (L67-80)
- [ ] **Step 2:** Delete the label constants block (L91-96)
- [ ] **Step 3:** Delete the preflight gate block (L537-558)
- [ ] **Step 4:** Delete the post-execute label-mutation block (~L641-675)
- [ ] **Step 5:** Delete any helper `_REVIEWER_ENTRY_LABEL_BY_TARGET` mapping if present
- [ ] **Step 6:** Delete tests in `test_roles_reviewer.py` that assert label-gate behavior or post-execute label state
- [ ] **Step 7:** Run `pytest packages/foreman/tests/test_roles_reviewer.py -q` — all green
- [ ] **Step 8:** `just check` green
- [ ] **Step 9:** Commit — `refactor(roles): strip v3 preflight gate + label mutation from Reviewer`

---

### Task 8d.5: Strip preflight gate + label mutation from Fixer

**Files:**
- Modify: `packages/foreman/src/foreman/roles/fixer.py`
- Modify: `packages/foreman/tests/test_roles_fixer.py`

Sites to remove (verified by 8d recon):
- `_LABEL_SPEC_FIX`, `_LABEL_PLANNING`, `_LABEL_NEEDS_HELP`, `_LABEL_FAILED`, `_LABEL_IMPL_FIX`, `_LABEL_IMPL_REVIEW` constants at L101-114
- `_FIXER_ENTRY_LABEL_BY_TARGET` if present
- Preflight gate at L495-499 (raise if entry label missing)
- Second graceful-refusal gate at L516-517
- `attempt_label` write at L531-532 (`foreman:fix-attempt-N`)
- `foreman:fix-attempt-` pattern matching at L750, L788
- Post-execute `set_labels` block at L779-791

What stays:
- The Fixer's core logic — prompt composition, LLM call, FOREMAN_OUTCOME emission, code edits in worktree, push to fix branch

Note: the `max_fix_attempts` runaway defense is now enforced at the v4 state-machine level (via the retry cap from 8c.2), not via the label-attempt-counter. The role itself doesn't need to count attempts.

- [ ] **Step 1:** Delete label constants block (L101-114)
- [ ] **Step 2:** Delete preflight gate blocks (L495-499, L516-517)
- [ ] **Step 3:** Delete attempt-label write at L531-532
- [ ] **Step 4:** Delete `foreman:fix-attempt-` pattern matching at L750 and L788
- [ ] **Step 5:** Delete post-execute `set_labels` block at L779-791
- [ ] **Step 6:** Delete tests in `test_roles_fixer.py` that assert label-gate or attempt-label behavior
- [ ] **Step 7:** Run `pytest packages/foreman/tests/test_roles_fixer.py -q` — all green
- [ ] **Step 8:** `just check` green
- [ ] **Step 9:** Commit — `refactor(roles): strip v3 preflight gate + label mutation from Fixer`

---

### Task 8d.6: Strip preflight gate + label mutation from Worker

**Files:**
- Modify: `packages/foreman/src/foreman/roles/worker.py`
- Modify: `packages/foreman/tests/test_roles_worker.py`

Largest gut surface — the Worker has 4 separate `set_labels` call sites plus attempt-label patterns. Sites to remove (verified by 8d recon):
- `_LABEL_PLAN_APPROVED`, `_LABEL_IMPL_REVIEW`, `_LABEL_SPEC_FIX`, `_LABEL_NEEDS_HELP`, `_LABEL_FAILED` constants at L116, L136-139
- Preflight gate at L662, L688 (raise if `foreman:plan-approved` missing)
- Second graceful-refusal at L710
- Pre-dispatch `set_labels` at L758-762
- `foreman:impl-attempt-` patterns at L1102, L1183
- Post-execute `set_labels` at L1172-1189
- Crash-revert `set_labels` at L1306-1324

What stays:
- The Worker's core logic — instructions read, worktree attach, prompt composition, LLM call, code edits, check command invocation, FOREMAN_OUTCOME emission, push + PR open

- [ ] **Step 1:** Delete label constants block (L116, L136-139)
- [ ] **Step 2:** Delete preflight gate + 2 graceful-refusal blocks (L662, L688, L710)
- [ ] **Step 3:** Delete pre-dispatch `set_labels` block (L758-762)
- [ ] **Step 4:** Delete `foreman:impl-attempt-` pattern matching at L1102, L1183
- [ ] **Step 5:** Delete post-execute `set_labels` block (L1172-1189)
- [ ] **Step 6:** Delete crash-revert `set_labels` block (L1306-1324)
- [ ] **Step 7:** Delete tests in `test_roles_worker.py` that assert label-gate or attempt-label behavior
- [ ] **Step 8:** Run `pytest packages/foreman/tests/test_roles_worker.py -q` — all green
- [ ] **Step 9:** `just check` green
- [ ] **Step 10:** Commit — `refactor(roles): strip v3 preflight gate + label mutation from Worker`

---

### Task 8d.7: Drop role-side `foreman:needs-help` write

**Files:**
- Modify: `packages/foreman/src/foreman/roles/__init__.py` — drop `TERMINAL_BLOCKING_LABEL` constant + `set_needs_help_label` plumbing in `handle_unhandled_role_exception`
- Modify: the 3 roles that pass `set_needs_help_label=lambda: ...` callbacks (reviewer/fixer/worker) — remove those callsites
- Modify: tests that exercise the unhandled-exception escape hatch

v4's state machine transitions to `NeedsHelp` whenever a role fails (any exception, missing FOREMAN_OUTCOME, or retry-cap trip), and `LabelObservabilityObserver` writes `foreman:state-needs-help`. The role-side `add_to_labels(foreman:needs-help)` is redundant AND uses the wrong namespace (`foreman:needs-help` vs v4's `foreman:state-needs-help`).

- [ ] **Step 1:** Delete `TERMINAL_BLOCKING_LABEL = "foreman:needs-help"` at `roles/__init__.py:22`
- [ ] **Step 2:** Drop the `set_needs_help_label` parameter from `handle_unhandled_role_exception` (or rename + repurpose if it has non-label work it still does)
- [ ] **Step 3:** Remove the `set_needs_help_label=lambda: bound_issue.add_to_labels(TERMINAL_BLOCKING_LABEL)` callsites in reviewer/fixer/worker (already partly removed by 8d.4-8d.6; this is the residual cleanup)
- [ ] **Step 4:** Delete tests asserting the role wrote `foreman:needs-help` on exception
- [ ] **Step 5:** `just check` green
- [ ] **Step 6:** Commit — `refactor(roles): drop role-side foreman:needs-help write — v4 NeedsHelp state owns it`

---

### Task 8d.8: Observer removes `foreman:plan` on first state transition

**Files:**
- Modify: `packages/foreman/src/foreman/v4/observers/label_observability.py`
- Modify: `packages/foreman/tests/v4/observers/test_label_observability.py`

The Planner-side removal of `foreman:plan` died in 8d.3. Move that behavior to the observer so it stays centralized: on `StateEnteredEvent` where the new state is `Queued` OR `Planning`, call `remove_labels(project, issue_number, {"foreman:plan"})` alongside the normal `add_labels(...)`.

Test: a ticket triggered by `foreman:plan` should end up with `foreman:state-queued` (or `foreman:state-planning`) as the ONLY foreman label after the first transition — `foreman:plan` removed, no other state labels present.

- [ ] **Step 1: Write failing test** `test_observer_removes_trigger_label_on_first_state_transition` — simulate StateEnteredEvent for Queued (or Planning), assert observer called `remove_labels(... {"foreman:plan"})` exactly once
- [ ] **Step 2: Run test, confirm FAIL** — observer doesn't remove trigger label
- [ ] **Step 3: Add logic to `LabelObservabilityObserver.on_state_entered`** — when new state is `Queued` or `Planning`, queue a `remove_labels(... {"foreman:plan"})` call alongside the existing `add_labels(state_label)`
- [ ] **Step 4: Run test, confirm PASS**
- [ ] **Step 5: Add second test** `test_observer_does_not_remove_trigger_label_on_later_transitions` — simulate StateEnteredEvent for SpecReview (not first), assert `remove_labels` NOT called with `foreman:plan`
- [ ] **Step 6:** `just check` green
- [ ] **Step 7:** Commit — `feat(v4/observer): remove foreman:plan trigger label on first state transition`

---

### Task 8d.9: Rewire `foreman init` to write `V4Config`

**Files:**
- Modify: `packages/foreman/src/foreman/init.py` — replace v3 `Config` write with `V4Config` write; drop v3 `Config` import; update `_format_project_block`, `_project_block_re`, `_write_project_block_to_config`, `_load_config_or_empty`, `run_init` signatures
- Modify: `packages/foreman/tests/test_init.py` — update assertions for new TOML shape
- Possibly: `packages/foreman/src/foreman/templates/init_config.toml.template` if it exists; otherwise inline the template

`foreman init` writes the config file the v4 daemon reads. Today it writes v3 shape (`[daemon]` + `[[projects]]` with v3 fields). Update to v4 shape: same `[daemon]` block but with `tick_seconds`, `max_in_flight`, `role_timeout_seconds`, `max_state_attempts`, `merge_mechanism`; `[apps.<role>]` blocks (Phase 8.3); `[orchestrator]` block (Phase 8.4); `[[projects]]` with v4 fields.

The default config path moves too — v4 uses `~/.foreman/v4/config.toml`, v3 used `~/.foreman/config.toml`. `foreman init` should write to the v4 path.

- [ ] **Step 1:** Audit every `Config` / `ProjectConfig` use in `init.py` — record file:line
- [ ] **Step 2:** Rewrite `_format_project_block` to emit v4 TOML shape
- [ ] **Step 3:** Rewrite `_load_config_or_empty` to return `V4Config`
- [ ] **Step 4:** Update `_DEFAULT_CONFIG_PATH` to `~/.foreman/v4/config.toml`
- [ ] **Step 5:** Update `_ensure_labels` to write the v4 label set (`foreman:plan`, `foreman:state-queued`, `foreman:state-planning`, `foreman:state-specreview`, `foreman:state-implementing`, `foreman:state-implreview`, `foreman:state-implfix`, `foreman:state-specfix`, `foreman:state-merging`, `foreman:state-done`, `foreman:state-needs-help`, `foreman:state-failed`) — verify the exact set matches `STATE_REGISTRY` in `v4/states/__init__.py`
- [ ] **Step 6:** Update tests in `test_init.py` to assert v4 TOML shape and v4 label set
- [ ] **Step 7:** `just check` green
- [ ] **Step 8:** Commit — `feat(init): rewire foreman init to write V4Config`

---

### Task 8d.10: `foreman enqueue` CLI — direct ticket ingest bypassing Poller

**Files:**
- Create or modify: `packages/foreman/src/foreman/v4/cli/enqueue.py` (new command module — match the shape of `hold.py` / `resume.py` etc. if those are separate files; otherwise add to the appropriate existing module)
- Modify: `packages/foreman/src/foreman/v4/cli/__init__.py` — register the new typer command
- Modify or create: `packages/foreman/tests/v4/cli/test_enqueue.py` — TDD tests

Today the only way to get a ticket into SQLite is "apply `foreman:plan` on GitHub, wait ≤30s for Poller to scan." Useful for production but annoying for dogfood + recovery. Add `foreman enqueue --project <name> --issue-number <N>` which inserts a ticket directly at state `Queued`, no GitHub round-trip.

**Validation:**
- `--project <name>` must match a `ProjectConfig.name` in the loaded `V4Config`; else error with the project list.
- `--issue-number <N>` must be a positive int.
- Idempotency: if a ticket already exists for this `(project, issue_number)`, error with the existing ticket's `id` + `state`. Don't silently create a duplicate.

**Behavior:**
- Insert a `Ticket` row with `project=<name>`, `issue_number=<N>`, `state=Queued`, `held_by=None`, no dependencies.
- Print the new ticket's `id` so the operator can chain follow-up commands.
- No GitHub API calls. Pure SQLite write. The LabelObservabilityObserver will catch up the next time the worker pool processes this ticket.

- [ ] **Step 1:** Write failing test `test_enqueue_creates_queued_ticket_in_sqlite` — invoke `foreman enqueue --project algokit --issue-number 99`, assert row appears in `tickets` table with `state="Queued"`, no GitHub calls (use the existing fake GitProvider — should remain untouched)
- [ ] **Step 2:** Write failing test `test_enqueue_rejects_duplicate` — call enqueue twice with same project+issue, assert second call errors with a clear message
- [ ] **Step 3:** Write failing test `test_enqueue_rejects_unknown_project` — call enqueue with a project name not in V4Config, assert error names the configured projects
- [ ] **Step 4:** Implement the enqueue command using the existing typer pattern from the other mutation commands (hold/resume/etc.)
- [ ] **Step 5:** Run the 3 new tests, confirm PASS
- [ ] **Step 6:** `just check` green
- [ ] **Step 7:** Commit — `feat(v4/cli): foreman enqueue — direct ticket ingest bypassing Poller`

---

### Task 8d.11: Manual dogfood — drive algokit#21 to terminal Done

**This is a manual task.**

After 8d.0-8d.10 land:

1. Inspect ticket #1 state: `FOREMAN_V4_CONFIG=~/.foreman/v4/config.toml uv run foreman show 1`
2. Reset failure history if needed (delete SpecReview state_instances rows + reset `current_state='SpecReview'` — same procedure as today's dogfood reset), OR use `foreman enqueue --project algokit --issue-number 21` to start a fresh ticket if the existing one is too polluted.
3. Resume the held ticket (if applicable): `uv run foreman resume <id>`
4. Start daemon (background): `uv run foreman daemon start &`
5. Watch `~/.foreman/v4/logs/transitions.jsonl` + `foreman ps` + `foreman show <id>`
6. Watch the algokit GitHub side: spec PR #22 review → merge → impl branch open → impl PR open → impl review → impl PR merge → issue #21 close

**Acceptance criteria (one must hold):**
- Ticket reaches `Done` (full chain end-to-end). **Primary goal.**
- Ticket reaches `NeedsHelp` via a real role decision (Reviewer judges spec/impl bad, Worker reports BLOCKED, etc.) — that's v4 working correctly even though this particular ticket didn't reach Done; bugs surfaced are real findings, not v3/v4 surface gaps.
- Ticket reaches `Failed` via the retry-cap (8c.2). The CAUSE of the retry-cap is then itself a Phase 8e finding.

**Daemon stays alive >60min** — proves 8c.3 token refresh works under real wall-clock.

---

## Phase 8d gate

- [ ] `just check` green after every commit
- [ ] Task 8d.11 reports the algokit#21 dogfood reached `Done` (or, per acceptance criteria above, a v4-working real-role-decision terminal)
- [ ] Daemon survived >60min during 8d.11

Phase 8d completion criterion: **v4's role CLIs are v4-native (no v3 label code, no v3 config/identity imports) AND v4 runs autonomously end-to-end on a real ticket.** Phase 9 (kill-set deletion + RUNBOOK + adversarial review + PR) is safe when 8d.11 succeeds.
