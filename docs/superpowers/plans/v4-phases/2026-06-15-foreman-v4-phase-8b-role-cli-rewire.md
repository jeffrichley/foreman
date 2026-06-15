> **Parent plan:** [../2026-06-13-foreman-v4-substrate-redesign-implementation.md](../2026-06-13-foreman-v4-substrate-redesign-implementation.md) — read its v4 isolation principle first.
> **Spec:** [../../specs/2026-06-13-foreman-v4-substrate-redesign-design.md](../../specs/2026-06-13-foreman-v4-substrate-redesign-design.md).
> **Branch:** `feat/foreman-v4-substrate`.
> **Gate at end:** `just check` green + re-run the **algokit#21** dogfood ticket to at least Done (or NeedsHelp if a real Planner/Reviewer/Worker decision blocks it — but NOT a config-shaped crash).

## Phase 8b — Role CLI v4 config rewire + `foreman init` + Windows daemon-status fix

Phase 8's SDD-automatable surface (tasks 8.1–8.6) wired the daemon-side of v4 cleanly. Phase 8.7 (manual dogfood against `jeffrichley/algokit` issue #21, 2026-06-15) surfaced three production-runnability gaps that block ticket-to-Done flow on a fresh repo:

1. **Role CLIs hard-import v3 config.** `roles/planner.py:41` does `from foreman.config import Config` and `cfg.projects['algokit']` → KeyError because v3 config only has voice/foreman/agent_core. Same shape in reviewer/fixer/worker. The Phase 8.6 real-fork test patched around this with `FOREMAN_DRY_RUN=1` and never exercised real config-load in the subprocess fork.
2. **v4 CLI exposes no `foreman init`.** v3's `foreman.init` module (in v4 survival set) creates 19 foreman state/modifier/attempt labels, verifies bot installations, validates the local clone, and writes a per-project CLAUDE.md instructions template. v4 ships none of this. Operators hand-creating individual labels via `gh label create` is brittle (default gray, no description) and incomplete (modifier and attempt labels never appear because the daemon never writes them — they're operator-applied).
3. **`cmd_daemon_status` / `cmd_daemon_stop` crash on Windows after the daemon exits.** `os.kill(pid, 0)` to probe liveness raises `OSError: [WinError 87] "The parameter is incorrect"` on Windows when the PID is gone — not `ProcessLookupError` as on POSIX. The except clauses only catch `ProcessLookupError` → unhandled `OSError` traceback.

The architectural pattern across (1) and (2): **Phase 8 wired the daemon-side of v4 but never wired the operator-facing surface (`foreman init`) or the role-CLI surface (the subprocess fork target).** Both still depend on v3 internals.

### Architectural decision — α: role CLIs read the SAME v4 config file the daemon reads

Considered β (stateless role CLIs taking every per-call field as a CLI flag, no config file). Rejected: the "flexibility" β offers (`foreman plan --check-command X --dev-base-branch Y`) is something we'd never use in production, and the architectural-purity argument (subprocess inputs explicit) doesn't earn its keep when `FOREMAN_V4_CONFIG` is already a hidden env var anyway and foreman config doesn't change mid-tick. The race risk is theoretical.

Phase 8b commits to **α**: role CLIs load `os.environ["FOREMAN_V4_CONFIG"]` (same path the daemon read), look up their project by name in the V4Config's projects list, and operate against that. v4 `ProjectConfig` grows the per-project fields the legacy `run_<role>` functions need.

The legacy `run_planner` / `run_reviewer` / `run_fixer` / `run_worker` internal async functions still accept a v3 `Config` object — that body stays untouched in Phase 8b. Each role CLI's `_run_<role>_for_v4` wrapper builds a v3 `Config` shape on the fly from V4Config + the requested project. Phase 9 deletion (or a later Phase 8c if scope grows) can refactor the legacy functions to take V4Config directly.

### Why this is a separate phase

Phase 8 tasks 8.1–8.6 reviewed cleanly because they were SDD-automatable against mocked seams. The dogfood task 8.7 was the first time the full chain ran against real GitHub + real config, and surfaced gaps no mocked test could have caught. Splitting into Phase 8b:
- Keeps Phase 8 tasks 8.1–8.6's commit chain stable (they passed spec + code-quality review)
- Makes "what the dogfood found" explicit and trackable
- Lets Phase 9 (v3 deletion + RUNBOOK + PR) remain truly mechanical, as originally intended
- Provides a defined re-run target (algokit#21) as the Phase 8b gate

### Task 8b.1: `foreman init <project>` typer command

**Files:**
- Create: `packages/foreman/src/foreman/v4/cli/init.py`
- Modify: `packages/foreman/src/foreman/v4/cli/__init__.py` (register the typer command)
- Create: `packages/foreman/tests/v4/cli/test_init.py`

v4 ships `foreman init <project>` that bridges V4Config → `foreman.init.InitConfig` → calls `foreman.init.run_init(...)`. The v3 init module is in v4 survival set per the isolation guard — wholesale delegating to it does not violate v4 isolation discipline.

Behavior:
- Reads V4Config from `FOREMAN_V4_CONFIG` (default `~/.foreman/v4/config.toml`).
- Looks up the requested project by name. Errors clearly if the name isn't in `config.projects`.
- Constructs an `InitConfig` from V4Config + the project's data (apps come from the shared `config.apps`, orchestrator from `config.orchestrator`, repo + local_clone_path from the project entry).
- Calls `foreman.init.run_init(config)`. Surfaces the human-readable result (labels created/existed, bots verified, instructions template written) to stdout.
- Exits 0 on success, non-zero with a clear error message on validation failure.

The test uses `monkeypatch` to stub `foreman.init.run_init` and asserts:
- Missing project name → typer.Exit with non-zero code + clear message
- Valid project → `run_init` called with the expected InitConfig shape (repo, clone path, apps, orchestrator)
- Result printed to stdout

- [ ] **Step 1: Build the `cmd_init` typer command + InitConfig bridge in `cli/init.py`**
- [ ] **Step 2: Register the command in `cli/__init__.py` (`app.command("init")(cmd_init)`)**
- [ ] **Step 3: Write `test_init.py` with the 3 cases above**
- [ ] **Step 4: `just check` green**
- [ ] **Step 5: Commit** — `feat(v4): foreman init <project> — bridge V4Config to v3 run_init`

### Task 8b.2: Grow v4 `ProjectConfig` with the per-project fields role CLIs need

**Files:**
- Modify: `packages/foreman/src/foreman/v4/config.py`
- Modify: `packages/foreman/tests/v4/test_config.py`

Fields the v3 `ProjectConfig` carries that the legacy `run_<role>` async bodies use (audit while implementing — start from these, drop any that turn out unused in v4 scope):

```python
class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    repo: str
    local_clone_path: str
    trigger_label: str = "foreman:plan"
    # NEW fields:
    check_command: str | None = None
    """Project's verification command. Worker runs this before claiming
    done. Resolves to ``"just check"`` when None. Matches v3
    ProjectConfig.check_command semantics."""
    dev_base_branch: str | None = None
    """Optional alternate branch to use as the base when creating spec
    worktrees, instead of origin/<default_branch>. Set when the project's
    active development line lives on a feature branch (e.g., during a
    walking-skeleton phase) rather than on main. The branch must exist
    on origin; Foreman will fetch it before branching."""
    max_fix_attempts: int = 3
    """Maximum fix cycles before the role escalates to NeedsHelp.
    Matches v3 ProjectConfig.max_fix_attempts default."""
    merge_mechanism: Literal["queue", "merge", "squash", "rebase"] | None = None
    """Per-project override for the merge mechanism. None inherits the
    daemon-level config.merge_mechanism. Per-project granularity matters
    because MergeQueue is enabled at the GitHub repo level — different
    projects may have it set up at different times. See foreman#158."""
```

**Audit during implementation:** read `run_planner`, `run_reviewer`, `run_fixer`, `run_worker` and every helper they call. If any field beyond the four above is used (e.g., `auto_merge_spec` per-project override, an admin token reference), surface it in the implementer report; do NOT silently add fields without flagging.

Tests:
- `test_project_growth_defaults` — minimal TOML still parses with all new fields at their defaults
- `test_project_check_command_override` — TOML override flows through
- `test_project_dev_base_branch_override` — TOML override flows through
- `test_project_max_fix_attempts_override` — TOML override flows through
- `test_project_merge_mechanism_override` — TOML override flows through; None default; invalid value raises

Update every existing v4 test that constructs a V4Config inline (test_phase7_e2e, test_bootstrap, test_phase8_real_fork) — they already work with the minimal ProjectConfig shape; the new fields default to None / sensible defaults, so existing tests should pass unchanged. Verify.

- [ ] **Step 1: Add the 4 new fields to v4 ProjectConfig**
- [ ] **Step 2: Update the top-of-file docstring TOML schema**
- [ ] **Step 3: Add 5 new test cases**
- [ ] **Step 4: `just check` green — confirm existing tests still pass with the default-None additions**
- [ ] **Step 5: Commit** — `feat(v4): ProjectConfig grows check_command + dev_base_branch + max_fix_attempts + merge_mechanism`

### Task 8b.3: Rewire 4 role CLIs to read v4 config

**Files:**
- Modify: `packages/foreman/src/foreman/roles/planner.py`
- Modify: `packages/foreman/src/foreman/roles/reviewer.py`
- Modify: `packages/foreman/src/foreman/roles/fixer.py`
- Modify: `packages/foreman/src/foreman/roles/worker.py`
- Modify: `packages/foreman/tests/v4/test_phase8_real_fork.py` (drop FOREMAN_DRY_RUN=1 from at least one case to prove real config-load path works)

The pattern is identical across all 4 role CLIs. Per-file changes:

1. Add `from foreman.v4.config import load_config as v4_load_config`. Keep the existing `from foreman.config import load_config` import for now (the legacy `run_<role>` still needs v3 `Config`).
2. In the `_run_<role>_for_v4` wrapper function, replace:
   ```python
   cfg_path = os.environ.get("FOREMAN_CONFIG_PATH") or os.environ.get("FOREMAN_CONFIG") \
       or str(Path("~/.foreman/config.toml").expanduser())
   cfg = load_config(cfg_path)
   project_cfg = cfg.projects[project]
   ```
   with:
   ```python
   v4_cfg_path = os.environ.get("FOREMAN_V4_CONFIG") \
       or str(Path("~/.foreman/v4/config.toml").expanduser())
   v4_cfg = v4_load_config(Path(v4_cfg_path))
   project_cfg = next(
       (p for p in v4_cfg.projects if p.name == project),
       None,
   )
   if project_cfg is None:
       raise ValueError(
           f"project {project!r} not found in V4Config at {v4_cfg_path}. "
           f"Known projects: {[p.name for p in v4_cfg.projects]}"
       )
   ```
3. Build a v3 `Config` shape from `v4_cfg` + `project_cfg` for the legacy `run_<role>` call. Helper function `_v3_config_from_v4(v4_cfg, project_cfg) -> Config` lives in a shared spot. **Recommended placement: a new private module `packages/foreman/src/foreman/v4/_v3_config_adapter.py`** so the four role CLIs share one adapter implementation rather than duplicating it four times. The adapter is allowed to import `foreman.config` (it's survival set) and `foreman.v4.config`.
4. Replace the existing `config=cfg, project_name=project` kwargs in the `run_<role>(...)` call with `config=adapted_cfg, project_name=project`.

**Adapter `_v3_config_from_v4` shape:**
```python
from foreman.config import (
    AdminConfig, AppsConfig as V3AppsConfig, Config as V3Config,
    OrchestratorConfig as V3OrchestratorConfig,
    ProjectConfig as V3ProjectConfig,
    ReconcilerConfig,
)
from foreman.v4.config import ProjectConfig as V4ProjectConfig, V4Config


def v3_config_from_v4(v4_cfg: V4Config, project: V4ProjectConfig) -> V3Config:
    """Build a v3 Config shape from a v4 V4Config + a chosen project.

    Used by the role CLIs so the legacy run_<role> async functions can
    keep their v3 Config signature while the CLI outer skin reads v4
    config. The legacy functions still use only the project_cfg fields
    (repo / local_clone_path / apps / check_command / dev_base_branch /
    auto_merge_* / merge_mechanism / max_fix_attempts) and the
    orchestrator block — those are the fields filled in here.

    Phase 8c or Phase 9 can later refactor run_<role> to take V4Config
    directly; at that point this adapter becomes unused and is deleted.
    """
    # Build per-project v3 AppsConfig from the shared v4 AppsConfig.
    # In v3 apps are per-project; in v4 they're shared. Constants flow
    # through unchanged — the same App credentials drive every project.
    v3_apps = V3AppsConfig(
        planner_app_id=v4_cfg.apps.planner.app_id,
        planner_private_key_path=v4_cfg.apps.planner.private_key_path,
        reviewer_app_id=v4_cfg.apps.reviewer.app_id,
        reviewer_private_key_path=v4_cfg.apps.reviewer.private_key_path,
        fixer_app_id=v4_cfg.apps.fixer.app_id,
        fixer_private_key_path=v4_cfg.apps.fixer.private_key_path,
        worker_app_id=v4_cfg.apps.worker.app_id,
        worker_private_key_path=v4_cfg.apps.worker.private_key_path,
    )
    v3_project = V3ProjectConfig(
        repo=project.repo,
        local_clone_path=project.local_clone_path,
        apps=v3_apps,
        check_command=project.check_command,
        dev_base_branch=project.dev_base_branch,
        max_fix_attempts=project.max_fix_attempts,
        merge_mechanism=project.merge_mechanism,
    )
    v3_orchestrator = V3OrchestratorConfig(
        app_id=v4_cfg.orchestrator.app_id,
        private_key_path=v4_cfg.orchestrator.private_key_path,
    )
    return V3Config(
        admin=AdminConfig(),
        orchestrator=v3_orchestrator,
        reconciler=ReconcilerConfig(
            merge_mechanism=v4_cfg.merge_mechanism,
        ),
        projects={project.name: v3_project},
    )
```

Audit during implementation:
- Verify the v3 `AppsConfig` field names match what the v3 model actually exposes (the field names in v3 likely use `_app_id` / `_private_key_path` suffixes per role — verify before writing).
- If the v3 `Config` has a required field not listed here (e.g., `admin.github_token_env`), set a sensible default or surface the gap in the implementer report.
- If the v3 `ProjectConfig` has required fields not listed here, surface those too.

Test `test_phase8_real_fork.py` — drop `FOREMAN_DRY_RUN=1` from at least one case (the planner) to prove the real config-load path runs cleanly. Use `monkeypatch` to point `FOREMAN_V4_CONFIG` at a tmp_path-built valid TOML. The subprocess can still patch `mint_installation_token` and the GitHub round-trips via env vars — the goal of this test change is to prove "subprocess can load v4 config without crashing," not to exercise the full PyGithub round-trip in a subprocess test.

- [ ] **Step 1: Create the `_v3_config_adapter.py` module + audit field names against actual v3 Pydantic shapes**
- [ ] **Step 2: Rewire `planner.py`'s `_run_planner_for_v4`**
- [ ] **Step 3: Rewire `reviewer.py`'s `_run_reviewer_for_v4`**
- [ ] **Step 4: Rewire `fixer.py`'s `_run_fixer_for_v4`**
- [ ] **Step 5: Rewire `worker.py`'s equivalent function**
- [ ] **Step 6: Update `test_phase8_real_fork.py` to drop DRY_RUN from at least one case**
- [ ] **Step 7: `just check` green**
- [ ] **Step 8: Commit** — `feat(v4): role CLIs read V4Config + v3 Config adapter for legacy run_<role> functions`

Multi-commit fine if natural — e.g., the adapter module in one commit, the 4 role CLI rewires in a second, the test update in a third.

### Task 8b.4: `cmd_daemon_status` + `cmd_daemon_stop` Windows OSError catch

**Files:**
- Modify: `packages/foreman/src/foreman/v4/cli/daemon.py`
- Modify: `packages/foreman/tests/v4/cli/test_daemon_commands.py`

`os.kill(pid, 0)` to probe liveness raises `OSError: [WinError 87] "The parameter is incorrect"` on Windows when the PID is gone, instead of `ProcessLookupError`. Both `cmd_daemon_status` and `cmd_daemon_stop` catch only `ProcessLookupError` → Windows users see unhandled tracebacks.

Fix: catch `OSError` (which `ProcessLookupError` is a subclass of — same except handler covers both POSIX + Windows) and treat as "PID not alive."

Concrete change in `cmd_daemon_status`:
```python
try:
    os.kill(pid, 0)
    typer.echo(f"daemon: running (pid {pid})")
except (ProcessLookupError, OSError):
    typer.echo(f"daemon: stale PID file (pid {pid} not alive)")
```

Same shape in `cmd_daemon_stop`:
```python
try:
    os.kill(pid, signal.SIGTERM)
except (ProcessLookupError, OSError):
    typer.echo(f"PID {pid} not running; cleaning stale file")
    _PID_PATH.unlink()
    return
```

Test addition: simulate a stale-PID-file case with a PID that's definitely not alive (e.g., `99999`) and assert both commands report cleanly without traceback. The test must work on Windows (where the OSError shape was originally hit).

- [ ] **Step 1: Update `cmd_daemon_status` + `cmd_daemon_stop` except clauses**
- [ ] **Step 2: Add the stale-PID test cases**
- [ ] **Step 3: `just check` green**
- [ ] **Step 4: Commit** — `fix(v4): cmd_daemon_status + stop catch Windows OSError on dead PID`

### Task 8b.5: Re-run the algokit#21 dogfood

**This is a manual task, not a TDD task.**

After 8b.1–8b.4 land, run the algokit#21 dogfood again. Specifically:

1. The test issue already exists at https://github.com/jeffrichley/algokit/issues/21 with the `foreman:plan` label applied.
2. Run `foreman init algokit` first to land the full 19-label set + bot verification + instructions template (Task 8b.1).
3. Start the daemon: `FOREMAN_V4_CONFIG=~/.foreman/v4/config.toml uv run foreman daemon start` from the foreman repo dir.
4. Watch `~/.foreman/v4/logs/transitions.jsonl` + `foreman ps` to confirm Queued → Planning advances without a config-shaped crash.

Expected outcomes are the same as the original Task 8.7 acceptance criteria:
- [ ] Ticket adopted by the Poller (was confirmed in original 8.7 run — kept)
- [ ] Planner role invoked via subprocess **and config loads cleanly** (new gate — this is what 8b fixes)
- [ ] Spec PR opened on GitHub
- [ ] Reviewer role invoked
- [ ] (optional) Worker / ImplReview / Merging stages exercised
- [ ] At least one ticket transitions to Done in `~/.foreman/v4/state.db`
- [ ] `transitions.jsonl` populated with the state journey

If the Planner crashes again — same kind of config-shaped crash or a different one — file a Phase 8c plan. Phase 9 deletion still cannot proceed until at least one ticket reaches a terminal state.

If the Planner runs but reaches a real semantic blocker (e.g., it can't decide between two mkdocs invocations, opens a spec PR with confidence=low), that's a SUCCESS for Phase 8b's purposes — the substrate works, the Planner's quality is a separate concern.

### Phase 8b gate

- [ ] `just check` green
- [ ] Task 8b.5 reports the dogfood reached a state past Planning without a config-shaped crash

Phase 8b completion criterion: **v4 actually runs against a real GitHub project (algokit) end-to-end through the role CLI subprocess fork.** Phase 9 (deletion + RUNBOOK + PR) is safe to execute when 8b.5 reports success.

---
