# Spec: Debugger role — root-cause analysis for foreman:bug tickets (issue #397)

## Goal

Add a `Debugger` role to the Foreman v4 pipeline that reads a `foreman:bug`-labelled issue, analyses the symptom + repro + evidence by grepping and reading the worktree, and produces a grounded root-cause hypothesis. On `confidence: high`, it rewrites the parent ticket body into a Planner-ready shape and transitions to `Planning`. On `confidence: medium`, it parks the ticket in a `DebugReview` holding state for operator triage. On `confidence: low`, it escalates to `NeedsHelp`. See [foreman#397](https://github.com/jeffrichley/foreman/issues/397).

## Acceptance criteria

- [ ] A ticket with `foreman:bug` label and a clear repro + error message runs the Debugger subprocess and produces a `DebuggerOutput` with a `primary` hypothesis naming a specific `file_path`. The issue gets a comment with the full diagnostic.
- [ ] On `confidence: high`, the issue body is rewritten to a Planner-ready shape (idempotent fenced section `<!-- foreman:debug-findings:begin -->` / `<!-- foreman:debug-findings:end -->`), `foreman:state-debugging` is removed, `foreman:state-planning` is applied, and the ticket transitions to `Planning` in the state machine.
- [ ] On `confidence: medium`, a diagnostic comment is posted, the ticket transitions to `DebugReview`, and `foreman:state-debugreview` is applied. No body rewrite.
- [ ] On `confidence: low`, a diagnostic comment is posted and the ticket transitions to `NeedsHelp`.
- [ ] The body-rewrite helper is idempotent: running the Debugger twice on the same `confidence: high` ticket produces one fenced section (second run replaces, not appends).
- [ ] `foreman:bug` trigger label is removed from the issue when the `Debugging` state is entered (mirrors `foreman:plan` removal on `Queued` / `Planning` entry in `label_observability.py`).
- [ ] `DebugReview` is treated as a terminal-ish state by the Poller (added to `_TERMINAL_STATES` in `poller.py`); the daemon does not re-dispatch a ticket in `DebugReview`.
- [ ] `foreman init` seeds `foreman:bug` (green, `LabelClass.QUEUE`) and `foreman:debug-review` (yellow, `LabelClass.BLOCKING`) on the target repo.
- [ ] `foreman debug --project <name> --issue-number <N>` CLI subcommand runs the Debugger role subprocess and emits `FOREMAN_OUTCOME:`.
- [ ] All existing v4 tests pass. New tests in `packages/foreman/tests/v4/test_debugging_state.py` cover the three confidence branches + body-rewrite idempotency.
- [ ] `just check` clean (ruff + mypy + pytest with the existing coverage gate).

## Approach

**Pattern naming (Decision 4 — calibrated lens):**

1. **Template Method (GoF)** — `DebuggingState` extends `RoleDispatchState` and overrides only `next_state_for(outcome)`, exactly like every other role-dispatch state (`PlanningState`, `SpecReviewState`, etc.). The dispatch + parse + TRANSIENT_PROVIDER_ERROR plumbing is inherited.
2. **Straightforward holding state (no pattern)** — `DebugReviewState` extends `_TerminalState` from `terminal.py`. It is a parking state; no role logic runs in it. The same `_TerminalState` base is used by `DoneState`, `FailedState`, and `NeedsHelpState` today.
3. **Idempotent fenced sections (project-local idiom)** — the body-rewrite helper uses HTML comment fences (`<!-- foreman:debug-findings:begin -->` / `<!-- foreman:debug-findings:end -->`) to mark its contribution to the issue body. This is the same idiom as `ESCALATION_MARKER_BEGIN/END` in `roles/_escalation_comment.py` and `FINDINGS_BEGIN_MARKER/END_MARKER` in `roles/reviewer.py`. Pattern-fishing is not needed here; the project already established the idiom.

**Role runner shape** (`packages/foreman/src/foreman/roles/debugger.py`):

The Debugger role runner follows the existing `_run_planner_core` / `run_planner_cli` split in `roles/planner.py`. The core function (`_run_debugger_core`) is an `async def` that fetches the issue, creates the worktree (for the LLM to grep/read from), dispatches the LLM with `DEBUGGER_ALLOWED_TOOLS = ["Read", "Glob", "Grep"]`, parses the `DebuggerOutput`, posts a diagnostic comment, and on `confidence: high` calls `host.update_issue_body(...)` with the rewritten body. The CLI entry point (`run_debugger_cli`) emits `FOREMAN_OUTCOME:` via `emit_outcome`.

**Credential resolution**: `apps.debugger` is added as an OPTIONAL field to `AppsConfig` (`app_id: int = 0`, `private_key_path: str = ""`). When the operator has not configured a Debugger App (i.e. `app_id == 0`), `_run_debugger_core` falls back to the Planner's credentials (`config.apps.planner`). This keeps existing config files valid and lets operators share the Planner App for the Debugger in small deployments.

**Confidence → outcome mapping** (mirrors how `run_planner_cli` maps `confidence: low` → `NEEDS_HELP`):

| `DebuggerOutput.confidence` | Emitted `OutcomeKind` | Emitted `OutcomeConfidence` | `DebuggingState.next_state_for` result |
|---|---|---|---|
| `high` | `CLEAN` | `HIGH` | `PlanningState()` |
| `medium` | `CLEAN` | `MEDIUM` | `DebugReviewState()` |
| `low` | `NEEDS_HELP` | `LOW` | `NeedsHelpState()` |

This reuses the existing `OutcomeConfidence` discriminator (already a field on `Outcome`) to distinguish the three paths without adding a new outcome kind.

**Poller parameterization**: `Poller.__init__` gains a new optional parameter `initial_state: str = "Queued"`. `_adopt_new_tickets` enqueues `WorkItem(ticket_id=..., state_name=self._initial_state)` instead of hardcoded `"Queued"`. In `bootstrap_cli_context`, EACH project gets two Pollers: one at the existing `project_config.trigger_label` / `initial_state="Queued"` (the existing Planner path) and one at `trigger_label="foreman:bug"` / `initial_state="Debugging"` (the new Debugger path). Both are appended to `pollers`.

**Label observability**: `label_observability.py`'s `_TRIGGER_LABEL` (a single string constant) is replaced by `_TRIGGER_LABELS: frozenset[str] = frozenset({"foreman:plan", "foreman:bug"})`, and `_FIRST_STATES` grows to include `"Debugging"`. `_on_state_entered` removes all `_TRIGGER_LABELS` (idempotent: `remove_labels` is a no-op on absent labels) when entering any first state.

**`update_issue_body`**: Added as an abstract method to `GitHostProvider` in `git_host.py` and implemented in `git_hosts/github.py` via PyGithub's `issue.edit(body=...)`. The Debugger role runner calls this only on `confidence: high`.

**Body-rewrite helper** (`roles/_body_rewriter.py`): A pure function `rewrite_body_with_debug_findings(*, original_body, primary, alternatives, recommended_fix, notes)` that replaces the fenced section if present, otherwise appends it. The fenced section contains a Planner-ready `## Source pointers` list (the `file_path:line_number` from the primary + alternatives) and a `## Approach` prose paragraph (the `recommended_fix`). Idempotency is guaranteed by the fence scan.

## Sub-requests (topologically sorted)

1. **`schemas/debugger.py`**: Add `RootCauseHypothesis` and `DebuggerOutput` Pydantic models in `packages/foreman/src/foreman/schemas/debugger.py`. Add `DebuggerRunResult` analogous to `PlannerRunResult`. `DebuggerOutput` fields: `primary: RootCauseHypothesis`, `alternatives: list[RootCauseHypothesis] = []`, `recommended_fix: str`, `confidence: Literal["high", "medium", "low"]`, `notes: str | None = None`. `DebuggerRunResult` fields: `llm_output: DebuggerOutput`, `final_labels: list[str]`. (No `pr` field since the Debugger does not open a PR.)

2. **`labels.py`**: Add `BUG = ("foreman:bug", LabelClass.QUEUE)` and `DEBUG_REVIEW = ("foreman:debug-review", LabelClass.BLOCKING)` to `Label` in `packages/foreman/src/foreman/labels.py`.

3. **`prompts/debugger.md`**: Write the Debugger system prompt in `packages/foreman/src/foreman/prompts/debugger.md`. Instructs the LLM to: (1) read the symptom + repro + evidence verbatim; (2) grep for the error-message string, function names, and source pointers named in the issue; (3) trace the call path from entry point to failure site; (4) propose the single most-likely root cause with `file_path + line_number + evidence`; (5) propose 1–3 alternatives if `primary.confidence < high`; (6) emit a 2–5 sentence `recommended_fix` sketch (not a full spec); (7) self-rate `confidence` as `high` (error message directly names a code path), `medium` (plausible path, not certain), or `low` (many candidates, no clear winner). Emphasises: read code before claiming; cite the exact line; do NOT produce a fix if you cannot ground the root cause. Uses the `StructuredOutput` tool to return `DebuggerOutput`.

4. **`git_host.py`**: Add abstract method `update_issue_body(self, repo_slug: str, issue_number: int, body: str) -> None` to `GitHostProvider` in `packages/foreman/src/foreman/git_host.py`.

5. **`git_hosts/github.py`**: Implement `update_issue_body` in `GitHubProvider` (call `repo.get_issue(issue_number).edit(body=body)`).

6. **`roles/_body_rewriter.py`**: Add `packages/foreman/src/foreman/roles/_body_rewriter.py` exporting module-level constants `DEBUG_FINDINGS_BEGIN = "<!-- foreman:debug-findings:begin -->"` / `DEBUG_FINDINGS_END = "<!-- foreman:debug-findings:end -->"` and the function `rewrite_body_with_debug_findings(*, original_body: str, primary: RootCauseHypothesis, alternatives: list[RootCauseHypothesis], recommended_fix: str, notes: str | None) -> str`. The function builds a fenced block and either replaces the existing fenced section (regex sub between `DEBUG_FINDINGS_BEGIN` and `DEBUG_FINDINGS_END`) or appends it. Running twice produces the same output.

7. **`roles/debugger.py`**: Add `packages/foreman/src/foreman/roles/debugger.py` following `roles/planner.py`'s structure. `DEBUGGER_ALLOWED_TOOLS = ["Read", "Glob", "Grep"]`. `_load_debugger_prompt()` reads `prompts/debugger.md` via `compose_role_prompt(role="debugger", superpowers=[])`. `_run_debugger_core` fetches the issue, builds a worktree (same `WorktreeManager` pattern as the Planner), dispatches the LLM with `output_model=DebuggerOutput`, posts a diagnostic comment via `host.post_issue_comment`, and on `confidence: high` calls `host.update_issue_body` using `rewrite_body_with_debug_findings`. Credential resolution: try `config.apps.debugger` when `app_id > 0`; fall back to `config.apps.planner`. `run_debugger_cli` emits `FOREMAN_OUTCOME:` mapping the three confidence levels as described in the Approach section.

8. **`v4/config.py`**: Add optional `debugger: AppCredentials | None = None` field to `AppsConfig`. Update `load_config` to pass `raw["apps"]["debugger"]` when present. Add a note that when absent, the Debugger borrows the Planner's credentials at runtime.

9. **`v4/subprocess_dispatcher.py`**: Add `"debugger": _Invocation(subcommand="debug", target=None)` to `_ROLE_TO_INVOCATION` in `packages/foreman/src/foreman/v4/subprocess_dispatcher.py`. Update `_STATE_NAME_TO_ROLE` in `terminal_landing.py` (if it exists) to add `"Debugging": "debugger"`.

10. **`v4/states/debugging.py`**: Add `DebuggingState` (extends `RoleDispatchState`, `role = "debugger"`, `state_name = "Debugging"`) and `DebugReviewState` (extends `_TerminalState` from `terminal.py`, `state_name = "DebugReview"`) in `packages/foreman/src/foreman/v4/states/debugging.py`. `DebuggingState.next_state_for` maps `CLEAN+HIGH → PlanningState`, `CLEAN+MEDIUM → DebugReviewState`, `NEEDS_HELP → NeedsHelpState`, else `FailedState`.

11. **`v4/states/registry.py`**: Add `"Debugging": DebuggingState` and `"DebugReview": DebugReviewState` to `STATE_REGISTRY` in `packages/foreman/src/foreman/v4/states/registry.py`.

12. **`v4/poller.py`**: (a) Add `initial_state: str = "Queued"` to `Poller.__init__`, store as `self._initial_state`; (b) In `_adopt_new_tickets`, change `state_name="Queued"` to `state_name=self._initial_state`; (c) Add `"DebugReview"` to `_TERMINAL_STATES` so the Poller does not re-enqueue parked tickets.

13. **`v4/observers/label_observability.py`**: (a) Replace `_TRIGGER_LABEL = "foreman:plan"` with `_TRIGGER_LABELS: frozenset[str] = frozenset({"foreman:plan", "foreman:bug"})`; (b) Add `"Debugging"` to `_FIRST_STATES`; (c) Update `_on_state_entered` to remove `_TRIGGER_LABELS` (the full set) instead of the single `_TRIGGER_LABEL`.

14. **`v4/cli/__init__.py`**: Add `from foreman.roles.debugger import run_debugger_cli` at the top and add `@app.command("debug")` `cmd_debug(project: str, issue_number: int)` function that calls `run_debugger_cli`, following the exact shape of `cmd_plan`.

15. **`init.py`**: (a) In `_build_v4_label_catalog`, add `(Label.BUG, "0E8A16", "Foreman v4: trigger label — Debugger picks up the ticket")` and `("foreman:state-debugreview", "FBCA04", "Foreman v4: Debugger posted medium-confidence diagnosis; awaiting operator")` alongside the existing trigger label entry. (b) Extend `state_metadata` in the function to include `"Debugging": ("FBCA04", "Foreman v4: Debugger running")` and `"DebugReview": ("FBCA04", "Foreman v4: Debugger: medium confidence, awaiting operator review")`.

16. **`v4/bootstrap.py`**: In the per-project Poller construction loop, add a second Poller per project with `trigger_label="foreman:bug"` and `initial_state="Debugging"`.

17. **Tests** (`packages/foreman/tests/v4/test_debugging_state.py`): four test functions: (a) `test_high_confidence_transitions_to_planning` — `CLEAN+HIGH` outcome → `PlanningState`; (b) `test_medium_confidence_transitions_to_debug_review` — `CLEAN+MEDIUM` outcome → `DebugReviewState`; (c) `test_low_confidence_transitions_to_needs_help` — `NEEDS_HELP` outcome → `NeedsHelpState`; (d) `test_body_rewrite_is_idempotent` — calling `rewrite_body_with_debug_findings` twice on the same body produces identical output (only one fenced section present).

18. **`README.md`**: Add a section "Bug triage with `foreman:bug`" to `packages/foreman/README.md` contrasting `foreman:plan` (diagnosis settled) and `foreman:bug` (diagnosis needed), and describing the three-branch outcome flow.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/schemas/debugger.py` | NEW: `RootCauseHypothesis`, `DebuggerOutput`, `DebuggerRunResult` |
| `packages/foreman/src/foreman/labels.py` | ADD `Label.BUG` and `Label.DEBUG_REVIEW` |
| `packages/foreman/src/foreman/prompts/debugger.md` | NEW: Debugger system prompt |
| `packages/foreman/src/foreman/git_host.py` | ADD abstract `update_issue_body` method |
| `packages/foreman/src/foreman/git_hosts/github.py` | IMPLEMENT `update_issue_body` |
| `packages/foreman/src/foreman/roles/_body_rewriter.py` | NEW: `rewrite_body_with_debug_findings`, fence constants |
| `packages/foreman/src/foreman/roles/debugger.py` | NEW: `_run_debugger_core`, `run_debugger_cli` |
| `packages/foreman/src/foreman/v4/config.py` | ADD optional `debugger: AppCredentials \| None = None` to `AppsConfig` |
| `packages/foreman/src/foreman/v4/subprocess_dispatcher.py` | ADD `"debugger"` entry to `_ROLE_TO_INVOCATION` |
| `packages/foreman/src/foreman/v4/states/debugging.py` | NEW: `DebuggingState`, `DebugReviewState` |
| `packages/foreman/src/foreman/v4/states/registry.py` | ADD `"Debugging"` and `"DebugReview"` entries |
| `packages/foreman/src/foreman/v4/poller.py` | ADD `initial_state` param; ADD `"DebugReview"` to `_TERMINAL_STATES` |
| `packages/foreman/src/foreman/v4/observers/label_observability.py` | REPLACE `_TRIGGER_LABEL` with `_TRIGGER_LABELS` frozenset; ADD `"Debugging"` to `_FIRST_STATES` |
| `packages/foreman/src/foreman/v4/cli/__init__.py` | ADD `cmd_debug` role command |
| `packages/foreman/src/foreman/init.py` | ADD `foreman:bug` + state labels to `_build_v4_label_catalog()` |
| `packages/foreman/src/foreman/v4/bootstrap.py` | ADD second per-project Poller for `foreman:bug` |
| `packages/foreman/tests/v4/test_debugging_state.py` | NEW: four test functions (three confidence branches + body-rewrite idempotency) |
| `packages/foreman/README.md` | ADD "Bug triage" section |

## Alternatives considered

1. **Have the Debugger share the Planner's App identity unconditionally (no `apps.debugger` field).** Rejected because it prevents operators who want distinct GitHub App attribution for Debugger actions from configuring one, and because adding an optional `apps.debugger` field is strictly backward-compatible (existing configs load unchanged thanks to the `None` default).

2. **Use a new `OutcomeKind.DEBUG_REVIEW` to route the medium-confidence path.** Rejected because `OutcomeKind` is a shared enum across all roles; adding a role-specific value leaks Debugger semantics into the outcome contract. Using `CLEAN` + `OutcomeConfidence.MEDIUM` reuses the existing discriminator without widening the enum.

3. **Route medium confidence to `NeedsHelpState` directly (no `DebugReviewState`).** Rejected because the issue explicitly distinguishes `confidence: medium` ("I have a plausible diagnosis, operator please verify") from `confidence: low` ("I cannot identify a root cause, operator must take over"). Conflating them into one `NeedsHelpState` makes the label set ambiguous — operators cannot distinguish "debugger gave up" from "debugger found something worth reviewing". `DebugReviewState` + `foreman:debug-review` preserve this signal.

4. **Extend `Poller.tick()` to look for multiple trigger labels in one scan.** Rejected because the current multi-Poller design (one Poller per project, one `trigger_label` per Poller) is already the established pattern. Adding a second Poller per project (two lines in `bootstrap_cli_context`) follows the existing convention exactly; a "multi-label Poller" would be a larger refactor with no benefit for the Debugger use case.

5. **Put the body-rewrite helper inside `git_host.py` / `PyGithubGitProvider` instead of a new `_body_rewriter.py`.** Rejected because body rewriting is a pure string transformation with no I/O; mixing it into the provider layer violates SRP. The Reviewer's `FINDINGS_BEGIN_MARKER` / Fixer's `ESCALATION_MARKER` precedent places string-manipulation helpers in `roles/` — `_body_rewriter.py` follows the same convention.

## Open questions

None. Every acceptance criterion traces to a specific file path and verified pattern in the worktree. One minor implementation note left to the Worker's judgment: whether `AppsConfig.debugger` should be typed as `AppCredentials | None = None` (nullable optional) or as an `AppCredentials` with sentinel `app_id = 0` (zero-value optional). Both are backward-compatible; the nullable form is slightly more idiomatic for Pydantic v2; either passes `just check`.

## Out of scope

- Executing tests or running code as part of the diagnostic. The Debugger is grep + Read only in this spec. Test execution is v2 scope.
- Multi-repo bug debugging. The Debugger reads ONLY the ticket's repo worktree.
- Suggesting a fix on `confidence: low`. The Debugger explicitly does NOT propose a fix when it cannot ground the root cause.
- Replacing the existing `foreman:plan` Planner. Many bugs skip the Debugger entirely — operator files with `foreman:plan` directly.
- `TerminalLandingObserver._STATE_NAME_TO_ROLE` update for `"Debugging": "debugger"`. That map currently lives in `v4/observers/terminal_landing.py`; it needs `"Debugging"` added, but that file is owned by foreman#367 spec changes and the Worker for this issue should add it in the same PR to avoid a map-drift regression. (The Worker should grep for `_STATE_NAME_TO_ROLE` to find the map.)
- An Architect role for greenfield. Separate ticket.
