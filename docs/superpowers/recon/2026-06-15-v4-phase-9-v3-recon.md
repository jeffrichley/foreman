# Phase 9 recon — v3 file inventory + v3-coupling sites in survival set

**Purpose.** Before writing Phase 9 (v3 deletion + RUNBOOK + PR), enumerate every file under `packages/foreman/src/foreman/` (the v3 root, excluding `v4/`), categorize each as KILL or SURVIVE, and for every SURVIVE file list every line that couples to v3 state-machine concerns (label gates, label mutations, `_BLOCKING_LABELS` reads, dispatcher logic).

**Method.** Direct file reads + grep across the v3 root. Not code-edited in this pass.

**Date.** 2026-06-15. Branch `feat/foreman-v4-substrate`.

---

## Entry point — already swapped

`packages/foreman/pyproject.toml:19-20`:
```toml
[project.scripts]
foreman = "foreman.v4.cli:main"
```

The `foreman` binary is v4 already. v3's `foreman/cli.py` is unreachable via the installed entry point. Deleting it can't break the binary contract.

---

## KILL set — 22 files + reconciler subtree (12) + 14 test files

These files have **zero imports from v4 or from any survival-set file**. Safe to delete in Phase 9.

| File | What it is |
|---|---|
| `_env_filter.py` | v3 env-passthrough filter |
| `cli.py` | v3 typer app (entry point no longer references it) |
| `daemon.py` | v3 asyncio daemon main |
| `daemon_host.py` | v3 `GitHubDaemonHost` |
| `daemon_lock.py` | v3 lock-file management |
| `daemon_runners.py` | v3 action runners (`merge_impl_pr → close_issue` etc.) |
| `dispatcher.py` | v3 `_LABEL_TO_ACTION` label-driven dispatcher |
| `git_hosts/__init__.py`, `_errors.py`, `github.py` | v3 GitHostProvider concrete impls |
| `labels.py` | v3 label-name vocabulary + helpers |
| `locks.py` | v3 per-ticket asyncio locks |
| `logging_setup.py` | v3 logging configuration (v4 has its own at `v4/logging_config.py`) |
| `poller.py` | v3 label-diff poller |
| `ps.py` | v3 `foreman ps` formatter |
| `queue.py` | v3 `DaemonQueue` |
| `reconciler/__init__.py`, `actions.py`, `clone_refresh.py`, `daemon.py`, `exec_log.py`, `gh_graphql.py`, `host.py`, `observer.py`, `outcomes.py`, `rules.py`, `state.py`, `v3_host.py` | v3 reconciler rule-evaluator subtree (12 files) |
| `role_dispatch.py` | v3 RealRoleDispatcher (v4 has SubprocessRoleDispatcher) |
| `storage.py` | v3 SQLite storage layer (v4 has `sqlite_repository.py`) |
| `v3_bus_endpoint.py` | v3 bus-endpoint adapter |
| `worker.py` | v3 worker loop (NOT the same as `roles/worker.py`) |

**Test side:**
- `tests/reconciler/` — 14 test files import exclusively from `foreman.reconciler.*`
- Additional v3-only tests in `tests/test_*.py` (49 files outside `tests/v4`) — need per-file confirmation that they only touch kill-set; counted but not yet enumerated

---

## SURVIVAL set — 17 source files + sub-packages

Imported by either `foreman.v4.*` or `foreman.roles.*`. Cannot delete; may need surgery.

### Files clean of v3 state-machine concerns (keep as-is)

| File | What v4/roles uses |
|---|---|
| `auth.py` (145 LoC) | `InstallationToken`, `mint_installation_token` |
| `branches.py` (34 LoC) | `spec_branch`, `impl_branch` |
| `dispatch_recorder.py` (723 LoC) | `DispatchRecorder`, `emit_recorder_complete` (run logging only — no label touches confirmed via grep) |
| `git_host.py` (148 LoC) | `GitHostProvider` Protocol + `IssueRef` |
| `instructions.py` (54 LoC) | `load_project_instructions` |
| `provider.py` (177 LoC) | `ProviderFacade`, `UsageInfo` |
| `providers/` subpackage | `make_provider`, `ProviderError`, `_usage`, `anthropic_sdk`, `recovery` |
| `schemas/` subpackage | Pydantic output models (4 files) |
| `stats.py` (585 LoC) | `log_planner_run`, `log_reviewer_run`, `log_fixer_run`, `log_worker_run` (stdout stats envelope emission only) |
| `templates/` subpackage | `instructions.md.template` |
| `worktree.py` (913 LoC) | `WorktreeManager` (git ops only) |

### Files with v3-coupling that needs surgery

Bold lines below are **must-gut**.

**`roles/__init__.py:22`**
```python
TERMINAL_BLOCKING_LABEL = "foreman:needs-help"
```
- Used in all 3 roles as `set_needs_help_label=lambda: issue.add_to_labels(TERMINAL_BLOCKING_LABEL)` escape hatch.
- Under v4, transitioning to `NeedsHelp` state already triggers `LabelObservabilityObserver` to write `foreman:state-needs-help`. Role-side write is redundant duplication.
- **Decision needed:** drop, OR keep as belt-and-suspenders if the role crashes before v4 transitions.

**`roles/planner.py`** (616 LoC)
- L395-406: `issue.remove_from_labels` drops `foreman:plan` after the spec PR opens.
- Under v4, the LabelObservabilityObserver replaces `foreman:plan` → `foreman:state-planning` on state entry. Planner's manual removal is redundant + fights the observer.
- **Must gut:** L395-406 entire label-mutation block.

**`roles/reviewer.py`** (967 LoC)
- L67-80: `_ReviewerPreflightRefusal` exception class.
- L91-96: 6 `_LABEL_*` constants (`foreman:planning`, `foreman:plan-approved`, `foreman:spec-fix`, `foreman:impl-review`, `foreman:impl-approved`, `foreman:impl-fix`).
- L537: `in_review_label = _REVIEWER_ENTRY_LABEL_BY_TARGET[target]` — gate lookup.
- **L545-558: preflight gate that blocked v4 dogfood today.** Raises if `foreman:planning` (spec) or `foreman:impl-review` (impl) not on issue.
- L670-675: post-execute `issue.set_labels(*final_labels)` writes the next-state label.
- **Must gut:** preflight gate (L545-558) + post-execute label mutation (L641-675). Strip label constants.

**`roles/fixer.py`** (1106 LoC)
- L101-114: 5 `_LABEL_*` constants.
- L481: `issue_labels = {label.name for label in issue.labels}` — gate read.
- L495-499: `_FixerPreflightRefusal` raise for missing `foreman:spec-fix` / `foreman:impl-fix`.
- L516-517: second graceful refusal site.
- L531-532: `issue.add_to_labels(attempt_label)` writes `foreman:fix-attempt-N`.
- L750, 788: `startswith("foreman:fix-attempt-")` pattern matches in cleanup.
- L779-791: post-execute `issue.set_labels(*sorted(final_label_set))`.
- **Must gut:** preflight gates (L481-499, L516-517) + attempt-label writes (L531-532, L750, L788) + post-execute mutation (L779-791). Strip label constants.

**`roles/worker.py`** (1601 LoC) — largest gut surface
- L116, L136-139: 5 `_LABEL_*` constants.
- L662, 675: gate read + needs-help escape.
- L688: `_WorkerPreflightRefusal` raise for missing `foreman:plan-approved`.
- L710: second graceful refusal site.
- L742: `attempt_label = f"foreman:impl-attempt-{attempt}"`.
- L758-762: pre-dispatch `issue.set_labels(...)`.
- L1102, 1183: `startswith("foreman:impl-attempt-")` patterns.
- L1172-1189: post-execute `issue.set_labels(*sorted(final_label_set))`.
- L1306-1324: crash-revert `issue.set_labels(*revert_labels)`.
- **Must gut:** preflight gates + 4 separate set_labels call sites + attempt-label pattern matching. Strip label constants.

**`identity.py` (v3)** (270 LoC)
- Survival rationale: legacy roles call `IdentityRegistry.get_client / get_role_token / get_role_identity_env`.
- v4 has `foreman.v4.identity.V4IdentityRegistry` — superset; supports target-aware role aliasing (8c.1).
- **Decision needed (Q1):** port the 4 role CLIs to use `foreman.v4.identity`, then delete v3 `identity.py`. OR keep both, with the cost of two registries to maintain.

**`config.py` (v3)** (571 LoC)
- v4 imports `Config`, `ProjectConfig`, `AppsConfig`, `OrchestratorConfig`, `AdminConfig`, `ReconcilerConfig` via `V3*` aliases — these are still used by the legacy roles' config loading.
- v4 has its own `foreman.v4.config.V4Config` with disjoint shape (`v4/config.py`).
- **Decision needed (Q2):** port roles' config-reading to `V4Config`, drop V3 aliases from v4 imports. OR keep dual config types — V3 for role-side, V4 for daemon-side.

**`init.py`** (759 LoC) — `foreman init` command
- Defines `InitConfig`, `BotVerification`, `InitResult`, `run_init`.
- Verified imports of v3 `Config`. Currently writes v3-format `~/.foreman/config.toml`.
- **Status of v4 wiring:** task #431 (Phase 8.8) is pending — "draft + execute role CLI v4 config rewire + foreman init". This is the v4 init rewire.
- **Decision needed (Q5):** is Phase 8.8 still alive, or fold into Phase 9?

---

## Cross-cutting decisions Jeff needs to make

Before Phase 9 planning:

- **Q1: Identity registry.** Port roles to `foreman.v4.identity`? (clean cutover, more work) OR keep v3 `identity.py` for roles only? (smaller diff, dual-registry world to maintain)
- **Q2: Config.** Port roles to `V4Config`? OR keep `V3*` alias bridge for roles?
- **Q3: `TERMINAL_BLOCKING_LABEL`.** Drop role-side `add_to_labels(foreman:needs-help)` writes? v4's `NeedsHelp` state writes `foreman:state-needs-help` via the observer. Role-side write predates v4 and is now redundant + uses the wrong label name.
- **Q4: Planner label mutation.** Drop `roles/planner.py:395-406` (the `foreman:plan` removal)? v4's `Planning` state entry writes `foreman:state-planning` and removes `foreman:plan` via `LabelObservabilityObserver` already (via the new `add_labels`/`remove_labels` ops).
- **Q5: foreman init.** Is the original Phase 8.8 (`foreman init` v4 rewire, task #431) still alive, or fold into Phase 9?

---

## Provisional Phase 9 scope (subject to Jeff's Q1-Q5 answers)

Numbers are file-counts to make scope concrete; not commits.

1. **Delete kill-set source:** 22 top-level files + entire `reconciler/` subtree (12 files) = **34 source files**
2. **Delete kill-set tests:** `tests/reconciler/` (14 files) + per-file enumeration of v3-only tests in `tests/test_*.py` (up to 49 files; needs separate pass)
3. **Gut v3-coupling lines from survival set:** ~125 lines across 5 role files (~30 in reviewer, ~30 in fixer, ~40 in worker, ~12 in planner, ~1 in roles/__init__.py)
4. **(Q1-dependent)** Port roles' identity-registry use to `foreman.v4.identity`, drop v3 `identity.py` (or keep it; smaller scope but dual-world)
5. **(Q2-dependent)** Port roles' config-reading to `V4Config`, drop v3 `config.py` + v4's `V3*` import aliases (or keep them; bridge persists)
6. **(Q5-dependent)** Rewire `foreman init` to v4 config format
7. Write RUNBOOK.md (Docker prod deploy + Windows-dev caveats)
8. Adversarial review pass before PR

**Followup-not-in-Phase-9:**
- 8c.5 revert is task #434 (separate small commit)
- Windows-native daemon-status fix only if/when needed; documented as dev-only in RUNBOOK

---

## What I did NOT verify in this recon (gaps)

- Per-file confirmation that the 49 non-reconciler v3-root tests (`tests/test_*.py`) import only from kill-set. Some may touch survival-set files and would need to stay.
- Whether `foreman.v3_bus_endpoint` is referenced by any agent-core config file outside this repo (e.g., `~/.agent-core/agent_core.yaml`).
- `prompts/` and `templates/` subpackage contents — assumed survival (no v3 state-machine logic), not confirmed.
- Whether v4's `foreman.v4.bootstrap` imports anything from `init.py`.

These gaps fold into Phase 9's first task — finalize the file list — once the cross-cutting Q1-Q5 are answered.
