# Spec: Architect role — decompose spec docs into sub-tickets for greenfield work (issue #396)

## Goal

Add an `Architect` role to the Foreman v4 pipeline that reads an existing spec doc from the worktree, decomposes it into 8–25 Planner-ready sub-tickets, files them on GitHub, and transitions the parent to an `Epic` polling state. This unlocks greenfield projects (new subsystems, dashboards, new packages) without requiring operators to hand-author sub-tickets. Tracks [foreman#396](https://github.com/jeffrichley/foreman/issues/396).

## Acceptance criteria

- [ ] A ticket with the `foreman:architect` label and a body containing `Spec: docs/superpowers/specs/<file>.md` (where the file exists in the worktree) produces 3+ filed GitHub issues, each carrying the `foreman:plan` label and a `parent: #<original>` line in its body.
- [ ] Each filed sub-ticket body contains all five Planner-ready sections: Symptom, Source file pointers, Approach, Acceptance criteria, Out of scope.
- [ ] After sub-tickets are filed, the parent ticket transitions `Architecting` → `Epic` and the `foreman:architect` label is removed from the GitHub issue.
- [ ] The Epic state transitions to `Done` only when every child sub-ticket row in SQLite is in state `Done` or `Failed`. It emits `BLOCKED` while any child remains open.
- [ ] A ticket whose spec doc path cannot be resolved (file missing from the worktree OR `Spec:` line absent from the issue body) transitions to `NeedsHelp`, and the architect subprocess emits an `escalation_comment` naming the missing path.
- [ ] `ArchitectingState` + `EpicState` are registered in `STATE_REGISTRY` and can be revived via `build_state("Architecting")` / `build_state("Epic")`.
- [ ] `foreman:architect` and `epic-child` appear in the label catalog seeded by `foreman init`.
- [ ] `just check` passes (ruff + mypy + all tests including new tests in `packages/foreman/tests/v4/test_architecting_state.py`).

## Approach

**Pattern naming (Decision 4):** Three structural decisions, named honestly:

1. **Template Method (GoF)** — `ArchitectingState` extends `RoleDispatchState`'s existing Template Method for `TRANSIENT_PROVIDER_ERROR` retry by overriding only `execute()` and `next_state_for()`. The base class `next_state()` Template Method intercepts transients identically to the other six role-dispatch states; only the post-dispatch sub-ticket-filing logic is novel. This is an intentional extension of the existing hierarchy, not a new pattern.

2. **OCP (SOLID / Google "make the right thing easy")** — The state machine is extended without modifying existing Planning / Implementing / etc. state classes. The `GitProvider` Protocol is extended via additive `create_issue` method (Protocol extension is backward-compatible in Python structural typing — existing implementors only need to add the method). All existing tests remain green with no existing-file edits other than the Poller extension and label-observer extension.

3. **No GoF pattern for `EpicState`** — `EpicState` is straightforward polling logic (like `QueuedState`): `execute()` reads SQLite, returns `BLOCKED` until all children are terminal, then `CLEAN`. The BLOCKED self-loop is exempted from the runaway-defense cap by the existing `count_consecutive_same_state` logic that skips `OutcomeKind.BLOCKED` rows.

**The role subprocess contract:** The architect subprocess runs in the ticket's worktree, reads the spec doc path from the issue body (`Spec: docs/superpowers/specs/<file>.md`), runs the LLM with read-only tools to produce `ArchitectOutput`, and emits `FOREMAN_OUTCOME:{..., "details": {"architect_output": {...}}}` on stdout. The `ArchitectingState.execute()` override parses this outcome, then calls `ctx.git.create_issue()` for each sub-ticket in order, capturing the returned GitHub issue numbers. The final `Outcome` returned from `execute()` stores the filed child issue numbers in `details["child_issue_numbers"]` so `EpicState` can find them later via the ticket's `state_instances` outcome_payload.

**Poller multi-trigger support:** The existing `Poller` has a single `trigger_label` (defaulting to `"foreman:plan"`). A new optional `extra_trigger_to_initial_state: dict[str, str]` parameter is added to `Poller.__init__`. In `_adopt_new_tickets()`, a private `_adopt_for_trigger(label, initial_state)` helper is extracted, called once for the primary trigger and once per extra entry. For the architect trigger, `create_ticket()` is called (which always creates at `Queued`), immediately overridden to `Architecting` via `set_ticket_state()`, and the WorkItem enqueued with `state_name="Architecting"`. The `_enqueue_open_tickets()` path works unchanged because it reads `ticket.current_state`.

**Label cleanup:** `label_observability.py` hardcodes `_TRIGGER_LABEL = "foreman:plan"` and `_FIRST_STATES = frozenset({"Queued", "Planning"})`. Both are extended: `_TRIGGER_LABELS = frozenset({"foreman:plan", "foreman:architect"})` (set, not singular) and `_FIRST_STATES` gains `"Architecting"`. On first-state entry, the observer calls `remove_labels` with the full `_TRIGGER_LABELS` set; since `remove_labels` is idempotent on absent labels, removing both trigger labels on every first-state entry is safe regardless of which trigger fired.

**GitProvider extension:** `create_issue(*, project: str, title: str, body: str, labels: list[str]) -> int` is added to the `GitProvider` Protocol, `FakeGitProvider`, and `PyGithubGitProvider`. The return value is the created issue's number (needed by `ArchitectingState` to populate `child_issue_numbers`).

**`AppsConfig.architect`:** Added as `architect: AppCredentials | None = None`. When `None` (existing deployments), `V4IdentityRegistry.get_role_token("architect")` falls back to the orchestrator token. The `foreman init` output gains a note that an optional `[apps.architect]` block can be set for a dedicated Architect GitHub App identity.

**EpicState child-tracking:** `EpicState.execute()` finds the last `Architecting`-state instance row for the ticket whose `outcome_kind == OutcomeKind.CLEAN`, reads `outcome_payload["details"]["child_issue_numbers"]`, then calls `ctx.repo.get_ticket_by_issue()` on each child. If any child is not yet in SQLite (Poller hasn't picked it up yet) or is in a non-terminal state, returns `BLOCKED`. When all children are `Done` or `Failed`, returns `CLEAN`.

## Sub-requests (topologically sorted)

1. Add `create_issue(*, project: str, title: str, body: str, labels: list[str]) -> int` to the `GitProvider` Protocol at `packages/foreman/src/foreman/v4/git_provider.py`. Add a `create_issue` stub to `FakeGitProvider` there (records the call in `self.created_issues: list[dict]`, returns a monotonically-incrementing `int` starting at 1001). Add the concrete `create_issue` to `PyGithubGitProvider` at `packages/foreman/src/foreman/v4/pygithub_git_provider.py` using `self._repo.create_issue(title=title, body=body, labels=sorted(labels))` and returning `issue.number`.

2. Add `SubTicket` and `ArchitectOutput` pydantic models to `packages/foreman/src/foreman/schemas/architect.py` (new file, following `packages/foreman/src/foreman/schemas/planner.py` as the house template):
   ```python
   from typing import Literal
   from pydantic import BaseModel, Field

   class SubTicket(BaseModel):
       title: str = Field(..., description="Conventional-commit shape, e.g. 'feat(dashboard): metric tile widget'")
       body: str = Field(..., description="Full Planner-ready issue body with Symptom / Source file pointers / Approach / Acceptance criteria / Out of scope sections")
       labels: list[str] = Field(default_factory=lambda: ["foreman:plan"], description="Labels to apply when filing the issue")
       depends_on: list[int] = Field(default_factory=list, description="Indices (0-based) into sub_tickets list that must complete before this ticket. Filed as informational text in the body; not wired to SQLite dependency tracking in v1.")

   class ArchitectOutput(BaseModel):
       spec_summary: str = Field(..., description="1-2 sentence summary of what's being built")
       sub_tickets: list[SubTicket] = Field(..., min_length=1)
       confidence: Literal["high", "medium", "low"] = Field(default="medium")
       notes: str | None = Field(default=None, description="Operator-facing notes about decomposition decisions")
   ```
   Export from `packages/foreman/src/foreman/schemas/__init__.py`.

3. Write `packages/foreman/src/foreman/prompts/architect.md`. Structure: `<role>`, `<inputs>`, `<spec_reading>`, `<decomposition_rules>`, `<sub_ticket_template>`, `<process>`, `<self_review>`, `<output_schema>`. Key instructions: (a) read the parent issue body to find `Spec: <path>` or inline spec block; (b) if path is missing or unresolvable, emit `confidence: low` with an `escalation_comment` naming the missing file; (c) read the spec top-to-bottom; (d) identify natural slice boundaries (1 slice = 1–3 files of new code OR 1–2 files of changed code); (e) emit sub-tickets in dependency order; (f) write each sub-ticket body with the five sections named above; (g) include `parent: #<parent_issue_number>` as the first line of every sub-ticket body; (h) return `ArchitectOutput`.

4. Add `ArchitectingState` to `packages/foreman/src/foreman/v4/states/architecting.py`. Extends `RoleDispatchState`. Overrides `execute()` to: dispatch the `"architect"` role, parse the outcome, and if `CLEAN`, deserialize `ArchitectOutput` from `outcome.details["architect_output"]`, iterate `sub_tickets`, call `ctx.git.create_issue()` for each, return a new `Outcome` with `details={"architect_output": ..., "child_issue_numbers": [...]}`. If `ctx.git is None`, raise `RuntimeError`. Implements `next_state_for()`: `CLEAN` → `EpicState()`, `NEEDS_HELP` → `NeedsHelpState()`, else `FailedState()`.

5. Add `EpicState` to `packages/foreman/src/foreman/v4/states/epic.py`. Does NOT extend `RoleDispatchState` — extends `TicketState` directly (like `QueuedState`). `execute()`: walk `ctx.repo.list_state_instances_for_ticket()` in reverse to find the last `Architecting` instance with `outcome_kind == OutcomeKind.CLEAN`; extract `child_issue_numbers` from its `outcome_payload["details"]["child_issue_numbers"]`. If none found, return `Outcome(kind=OutcomeKind.NEEDS_HELP, ...)`. Otherwise check each child via `ctx.repo.get_ticket_by_issue()` (catch `TicketNotFoundError` → not yet picked up → still open). `next_state()` (NOT `next_state_for` — Epic doesn't use RoleDispatchState): CLEAN → `DoneState()`, BLOCKED → `EpicState()` (self-loop), else `NeedsHelpState()`.

6. Register `ArchitectingState` and `EpicState` in `STATE_REGISTRY` at `packages/foreman/src/foreman/v4/states/registry.py`. Import both and add `"Architecting": ArchitectingState` and `"Epic": EpicState`.

7. Add `"architect": _Invocation(subcommand="architect", target=None)` to `_ROLE_TO_INVOCATION` in `packages/foreman/src/foreman/v4/subprocess_dispatcher.py`.

8. Extend `Poller.__init__` at `packages/foreman/src/foreman/v4/poller.py` with `extra_trigger_to_initial_state: dict[str, str] | None = None`. Extract `_adopt_for_trigger(label: str, initial_state: str) -> None` private helper from the existing `_adopt_new_tickets` body. Update `_adopt_new_tickets` to call `_adopt_for_trigger(self._trigger_label, "Queued")` and then one call per extra entry. In `_adopt_for_trigger`: if `initial_state != "Queued"`, call `self._repo.set_ticket_state(ticket.id, initial_state, now=self._clock())` immediately after `create_ticket`.

9. Extend `packages/foreman/src/foreman/v4/observers/label_observability.py`: rename `_TRIGGER_LABEL` to `_TRIGGER_LABELS: frozenset[str] = frozenset({"foreman:plan", "foreman:architect"})`. Add `"Architecting"` to `_FIRST_STATES`. Update `_on_state_entered` to call `self._writer.remove_labels(..., labels=_TRIGGER_LABELS)` (set, not singleton). This change is backward-compatible because `remove_labels` is idempotent on absent labels.

10. Add `architect: AppCredentials | None = None` to `AppsConfig` at `packages/foreman/src/foreman/v4/config.py`. Add `architect_trigger_label: str = "foreman:architect"` to `ProjectConfig`.

11. Write `packages/foreman/src/foreman/roles/architect.py`: `run_architect_cli(*, project: str, issue_number: int) -> int`. Follows the `run_planner_cli` structure: resolve role resources, fetch issue, create worktree, dispatch LLM with read-only tools + `ArchitectOutput` schema, emit `FOREMAN_OUTCOME:{..., "details": {"architect_output": output.model_dump()}}`, return exit code. The LLM reads the spec doc path from the issue body. If the spec path resolves and the LLM confidence is `low`, populate the `escalation_comment` field and emit `NEEDS_HELP` outcome.

12. Add `foreman architect` CLI subcommand to `packages/foreman/src/foreman/v4/cli/__init__.py` mirroring the `cmd_plan` shape, importing `run_architect_cli` from `foreman.roles.architect`. Add `from foreman.roles.architect import run_architect_cli` at the top and wire `@app.command("architect")`.

13. Wire the Poller's `extra_trigger_to_initial_state` in `packages/foreman/src/foreman/v4/bootstrap.py`: pass `extra_trigger_to_initial_state={project_config.architect_trigger_label: "Architecting"}` to each `Poller(...)` construction at `bootstrap.py:78-86`.

14. Update `packages/foreman/src/foreman/init.py`: add `"foreman:architect"` (orange color, `"trigger: decompose a spec doc into sub-tickets"`) and `"epic-child"` (light blue, `"sub-ticket filed by the Architect role"`) to `_FOREMAN_LABELS`. Update `_ROLE_NAMES` to include `"architect"`.

15. Write integration tests in `packages/foreman/tests/v4/test_architecting_state.py`:
    - `test_happy_path_files_sub_tickets_and_transitions_to_epic`: seeds `FakeRoleDispatcher` with a canned `FOREMAN_OUTCOME` carrying 3 sub-tickets in `details.architect_output`; seeds `FakeGitProvider.create_issue` responses; runs `ArchitectingState.transition()`; asserts 3 calls to `create_issue`, ticket state transitions to `Epic`, `child_issue_numbers` stored in the outcome payload.
    - `test_low_confidence_transitions_to_needs_help`: seeds `FOREMAN_OUTCOME` with `kind="needs_help"`; asserts ticket reaches `NeedsHelp`.
    - `test_epic_state_transitions_to_done_when_all_children_done`: seeds SQLite with 2 child tickets both at state `Done`; runs `EpicState.transition()`; asserts ticket transitions to `Done`.
    - `test_epic_state_stays_blocked_while_child_pending`: seeds 1 child at `Done`, 1 at `Planning`; runs `EpicState.transition()`; asserts outcome is `BLOCKED` and state remains `Epic`.
    - `test_architecting_state_registered_in_state_registry`: asserts `build_state("Architecting")` and `build_state("Epic")` return instances of the right classes.

16. Add a "Architect flow" section to `README.md` between the existing pipeline description and the "Running the daemon" section. Cover: (a) operator writes the spec doc, (b) files a ticket with `foreman:architect` label and `Spec: docs/superpowers/specs/<file>.md` in the body, (c) daemon picks it up, Architect decomposes into sub-tickets with `foreman:plan`, (d) each sub-ticket goes through the normal Planner → Worker → Reviewer pipeline, (e) when all sub-tickets close, the parent Epic transitions to Done.

## File-level changes

| File | Action | Change |
|------|--------|--------|
| `packages/foreman/src/foreman/schemas/architect.py` | CREATE | `SubTicket` + `ArchitectOutput` pydantic models |
| `packages/foreman/src/foreman/schemas/__init__.py` | MODIFY | Re-export `ArchitectOutput`, `SubTicket` |
| `packages/foreman/src/foreman/prompts/architect.md` | CREATE | LLM system prompt for the Architect role |
| `packages/foreman/src/foreman/roles/architect.py` | CREATE | `run_architect_cli` role subprocess runner |
| `packages/foreman/src/foreman/v4/states/architecting.py` | CREATE | `ArchitectingState(RoleDispatchState)` |
| `packages/foreman/src/foreman/v4/states/epic.py` | CREATE | `EpicState(TicketState)` |
| `packages/foreman/src/foreman/v4/states/registry.py` | MODIFY | Add `"Architecting"` + `"Epic"` entries |
| `packages/foreman/src/foreman/v4/git_provider.py` | MODIFY | Add `create_issue` to `GitProvider` Protocol + `FakeGitProvider` |
| `packages/foreman/src/foreman/v4/pygithub_git_provider.py` | MODIFY | Implement `create_issue` |
| `packages/foreman/src/foreman/v4/poller.py` | MODIFY | Add `extra_trigger_to_initial_state`, extract `_adopt_for_trigger` helper |
| `packages/foreman/src/foreman/v4/observers/label_observability.py` | MODIFY | `_TRIGGER_LABELS` frozenset; `_FIRST_STATES` gains `"Architecting"` |
| `packages/foreman/src/foreman/v4/config.py` | MODIFY | `AppsConfig.architect: AppCredentials | None = None`; `ProjectConfig.architect_trigger_label: str = "foreman:architect"` |
| `packages/foreman/src/foreman/v4/subprocess_dispatcher.py` | MODIFY | Add `"architect"` to `_ROLE_TO_INVOCATION` |
| `packages/foreman/src/foreman/v4/cli/__init__.py` | MODIFY | Add `foreman architect` subcommand wired to `run_architect_cli` |
| `packages/foreman/src/foreman/v4/bootstrap.py` | MODIFY | Pass `extra_trigger_to_initial_state` to Poller; lookup `apps.architect` for identity wiring |
| `packages/foreman/src/foreman/init.py` | MODIFY | Add `"foreman:architect"` + `"epic-child"` to label catalog; add `"architect"` to `_ROLE_NAMES` |
| `packages/foreman/tests/v4/test_architecting_state.py` | CREATE | Integration tests (5 test functions enumerated above) |
| `README.md` | MODIFY | "Architect flow" section |

## Alternatives considered

1. **`create_ticket(initial_state=...)` parameter instead of `set_ticket_state` override in Poller.** Would clean up the Poller helper but requires changing both `TicketRepository` Protocol and both implementations (`InMemoryTicketRepository` + `SqliteTicketRepository`), which also cascades to `_repository_contract.py` tests. The two-step approach (`create_ticket` + `set_ticket_state`) touches only the Poller and is functionally equivalent. Ruled out to minimize the change surface.

2. **Two separate `Poller` instances per project (one for `foreman:plan`, one for `foreman:architect`).** Cleanest from a single-responsibility standpoint, but doubles the GitHub API calls per tick (two `list_open_issues_with_label` calls instead of one) and requires changing the `Daemon.pollers` wiring for every project. The `extra_trigger_to_initial_state` extension keeps it one Poller per project and one API call per trigger label. Ruled out for efficiency + bootstrap simplicity.

3. **`ArchitectingState` does not extend `RoleDispatchState`; reimplements dispatch from scratch.** Would avoid the `execute()` override and keep the Template Method clean, but means duplicating the TRANSIENT_PROVIDER_ERROR handling logic that `RoleDispatchState.next_state()` already provides. The existing six role-dispatch states all use this Template Method; the Architect's special filing logic is localized to `execute()` only. Ruled out to preserve the single location for transient-error retry logic.

4. **`EpicState` polls GitHub (via `git.list_open_issues_with_label`) instead of SQLite.** Would remove the need to store `child_issue_numbers` in the outcome payload (the Epic could just look up all issues tagged `epic-child` with `parent: #N` in their body). But: (a) the current `GitProvider` Protocol has no `search_issues` method and adding one is non-trivial, (b) label-based search would require GitHub API calls on every tick for every Epic, (c) the `child_issue_numbers` in the outcome payload is cheap, unambiguous, and already follows the pattern of `latest_pr_number_for_ticket` reading from `outcome_payload`. Ruled out for efficiency and to avoid extending the `GitProvider` Protocol further than needed.

5. **EpicState as a terminal-ish `NeedsHelpState`-like state with no automatic transition to `Done`.** The operator would close the epic manually after verifying sub-tickets. Simpler to implement (no child polling), but defeats the purpose of the Epic as an automated progress tracker. Ruled out because the acceptance criteria explicitly require the Epic to transition to `Done` automatically when all children close.

## Open questions

1. **`AppsConfig.architect` as required vs. optional.** The spec makes it optional with orchestrator-token fallback. If the team wants a dedicated Architect GitHub App identity (separate avatar, audit trail), the operator adds `[apps.architect]` to the TOML config. The daemon logs a warning when the fallback fires. No design decision needed before the Worker runs — the fallback is the safe default and the upgrade path is additive.

2. **`FOREMAN_DRY_RUN` check in `run_architect_cli`.** The existing role CLIs (`run_planner_cli`, `run_worker_cli`, etc.) short-circuit to a canned outcome when `FOREMAN_DRY_RUN=1` (for the real-fork integration test at Phase 8.6). The Worker should add the same check to `run_architect_cli`. The exact canned `FOREMAN_OUTCOME` shape for the dry-run path is not specified here — the Worker should follow the pattern in `foreman.roles.planner.run_planner_cli` exactly.

## Out of scope

- SQLite-level `depends_on` wiring for sub-ticket ordering. The `SubTicket.depends_on` field exists in the schema for forward compatibility, and the architect prompt instructs the LLM to emit sub-tickets in topological order. Enforcing the ordering in the Poller's enqueue gate (via `list_unmet_dependencies`) is a follow-up.
- Cross-repo sub-ticket filing. The Architect files sub-tickets in the SAME repo as the parent. Multi-repo decomposition is a v5 concern.
- LLM-driven spec doc generation. The Architect consumes a spec doc; it does not write one. If the operator hasn't done the brainstorm + spec work, `confidence: low` → `NeedsHelp`.
- A Debugger role for bug analysis. Separate ticket, independent design.
- Replacing the existing `foreman:plan` Planner for bugs. The current pipeline stays — the Architect is additive.
- Changing the `count_consecutive_same_state` cap exemption for `BLOCKED` outcomes. `EpicState`'s BLOCKED self-loop relies on the existing exemption already present in `InMemoryTicketRepository.count_consecutive_same_state` (line 411: `if inst.outcome_kind == OutcomeKind.BLOCKED: continue`). No change needed.
