> **Parent plan:** [../2026-06-13-foreman-v4-substrate-redesign-implementation.md](../2026-06-13-foreman-v4-substrate-redesign-implementation.md) — read its v4 isolation principle first.
> **Spec:** [../../specs/2026-06-13-foreman-v4-substrate-redesign-design.md](../../specs/2026-06-13-foreman-v4-substrate-redesign-design.md).
> **Branch:** `feat/foreman-v4-substrate`.
> **Gate at end:** `just check` green + re-run algokit#21 dogfood and reach a terminal state (Done, NeedsHelp, or Failed) **on a non-config-shaped reason**.

## Phase 8c — Production runaway defense + role-aliasing + token refresh

Phase 8b's dogfood proved the role CLI surface works end-to-end (Planner ran 3min against real Claude, opened real spec PR [algokit#22](https://github.com/jeffrichley/algokit/pull/22)). Five new production-runnability bugs surfaced after Planning:

1. **V4IdentityRegistry doesn't alias target-aware role names.** State machine dispatches `reviewer-spec` (target-aware) but `V4IdentityRegistry._resolve` only knows base roles `planner|reviewer|fixer|worker|orchestrator`. Raises `ValueError("Unknown role: 'reviewer-spec'")` immediately, blocking SpecReview and everything after. The SubprocessRoleDispatcher already maps `reviewer-spec → foreman review --target spec` correctly; only the identity layer needs to learn the suffix scheme.
2. **No retry cap on transient role-dispatch failures.** When SpecReview crashed with bug #1, the state machine kept re-entering SpecReview every 30s (one tick) for 86 attempts in 43 minutes, generating 86 state_instances rows and 86 GitHub API calls per attempt before the daemon eventually died of an unrelated cause. This is a real autonomous-loop runaway risk that needs a budget regardless of what the underlying failure is.
3. **PyGithub orchestrator client uses static 1-hour token, no refresh.** `main()` calls `Github(identity.get_role_token("orchestrator"))` once at bootstrap. PyGithub stores the token; it doesn't know to ask V4IdentityRegistry for a fresh one. The daemon WILL die at minute ~60 with `BadCredentialsException: 401`. v3 IdentityRegistry handles this by gating every PyGithub access through `get_client()` which refreshes from cache. v4 main()'s `_git_factory` skips that gate.
4. **Phase 8b.4 OSError catch too broad on Windows.** `os.kill(pid, 0)` on Windows raises OSError on *alive* PIDs in some scenarios (permission/access), not just dead ones. `cmd_daemon_status` now misreports live daemons as "stale". Narrow to `errno`/`winerror` check.
5. **LabelObservabilityObserver writes via PyGithub `set_labels`, which REPLACES all labels.** When the daemon enters Planning state, it writes `foreman:state-planning` and the trigger label `foreman:plan` is silently removed. If the daemon crashes after that point, the Poller can't re-find the ticket because the trigger label is gone. This wedged the morning dogfood for ~40 minutes until I noticed.

### Why this is a separate phase

Phase 8b shipped the gate it set ("config-load works"). The 5 new findings are runtime/runaway/architectural issues the empirical dogfood revealed. Splitting into 8c keeps the 8b commit chain stable (its tests + reviews stand) and gives the production-runnability fixes a defined home.

### Architectural decisions committed here

**Token refresh strategy.** v3 wraps PyGithub access through `IdentityRegistry.get_client()` which mints/refreshes on every call. We will do the same in v4 — wrap PyGithubGitProvider so it calls `V4IdentityRegistry.get_role_token("orchestrator")` and rebuilds the `Github(...)` client whenever the cached token is near expiry. The existing 5-min pre-expiry refresh logic in `V4IdentityRegistry` is correct; the seam that's missing is between V4IdentityRegistry and PyGithub.

**Retry-cap shape.** Add a per-state-instance attempt counter persisted in SQLite. State machine refuses to re-enter the same state more than `max_state_attempts` times consecutively (default 3) and forces a transition to `NeedsHelp` instead. This is symmetric to `max_fix_attempts` and `max_impl_attempts` but applies at the state-level granularity rather than role-level.

**Label-write strategy.** Switch LabelObservabilityObserver from `set_labels(labels)` to `add_labels(new_state)` + `remove_labels(old_state)` — granular operations that DON'T touch the trigger label or any other labels the operator added. Phase 8.1's `GitProvider.write_labels` Protocol method needs to evolve.

### Task 8c.1: V4IdentityRegistry aliases target-aware role names → blocker fix

**Files:**
- Modify: `packages/foreman/src/foreman/v4/identity.py`
- Modify: `packages/foreman/tests/v4/test_identity.py` (extend)

`V4IdentityRegistry._resolve(role)` needs to strip `-spec` / `-impl` suffixes and fall back to the base role's credentials. Specifically:
- `reviewer-spec` → resolves to `reviewer` App credentials
- `reviewer-impl` → resolves to `reviewer` App credentials (same App, target carried via CLI arg, not identity)
- `fixer-spec` → resolves to `fixer` App credentials
- `fixer-impl` → resolves to `fixer` App credentials
- Base roles `planner`, `reviewer`, `fixer`, `worker`, `orchestrator` continue to work as before

The cache key should use the BASE role name (so `reviewer-spec` and `reviewer-impl` share one cached token from the `reviewer` App — they're the same App identity).

Add new tests:
- `test_role_aliases_reviewer_spec_and_impl_to_reviewer_app` — assert `get_role_token("reviewer-spec")` and `get_role_token("reviewer-impl")` both mint with reviewer's app_id.
- `test_role_aliases_fixer_spec_and_impl_to_fixer_app` — same for fixer.
- `test_alias_cache_is_shared_across_target_suffixes` — assert `reviewer-spec` and `reviewer-impl` calls share one cached `InstallationToken` instance.
- `test_unknown_role_still_raises` — `get_role_token("not-a-role")` and `get_role_token("planner-spec")` (planner is NOT target-aware) both raise ValueError.

- [ ] **Step 1: Add a `_BASE_ROLES` constant + `_normalize_role` helper that strips known suffixes**
- [ ] **Step 2: Rewrite `_resolve` to call `_normalize_role` before the if/elif chain**
- [ ] **Step 3: Update cache key in `get_role_token` to use the normalized name**
- [ ] **Step 4: Add 4 new tests**
- [ ] **Step 5: `just check` green**
- [ ] **Step 6: Commit** — `fix(v4): V4IdentityRegistry aliases reviewer-spec/impl + fixer-spec/impl to base App`

### Task 8c.2: State-machine retry cap → runaway defense

**Files:**
- Modify: `packages/foreman/src/foreman/v4/state.py` (transition() method or wherever state-entry logic lives)
- Modify: `packages/foreman/src/foreman/v4/config.py` (add `max_state_attempts: int = 3` to DaemonConfig)
- Modify: `packages/foreman/src/foreman/v4/sqlite_repository.py` (query consecutive same-state count)
- Modify: `packages/foreman/tests/v4/test_state.py` or wherever the relevant tests are (extend)

The state machine should count consecutive `state_instances` rows for the same `(ticket_id, state)` pair and refuse to re-enter once `max_state_attempts` is reached, forcing a transition to `NeedsHelp` with a clear `failure_reason` like "state X failed N consecutive times; escalating to operator".

Implementation hints:
- Add `count_consecutive_same_state(ticket_id, state) -> int` to `TicketRepository` Protocol + Sqlite impl + InMem impl. Walks back through `state_instances` rows ordered by `sequence DESC`, counts how many in a row match the target state before hitting a different state (or end of history).
- In the state machine's pre-execute check, query this count. If `>= max_state_attempts`, write a synthetic `Outcome(kind=NEEDS_HELP, summary="state X failed N consecutive times")` instead of running the role.
- Default to `max_state_attempts = 3` (matches `max_fix_attempts` / `max_impl_attempts` shape).

Tests:
- `test_state_retry_cap_escalates_to_needs_help_after_n_failures` — simulate N+1 consecutive same-state failures; assert the N+1'th attempt transitions to NeedsHelp without running the role.
- `test_state_retry_cap_resets_after_state_advance` — succeed once; ensure subsequent re-entries of a different state don't see the historical count.
- `test_repository_count_consecutive_same_state` (contract test for both InMem and Sqlite repos).

- [ ] **Step 1: Add `max_state_attempts: int = 3` to DaemonConfig + V4Config docstring**
- [ ] **Step 2: Add `count_consecutive_same_state` to TicketRepository Protocol + 2 impls**
- [ ] **Step 3: Wire the retry-cap check into state machine's transition() (or the state-instance creation path)**
- [ ] **Step 4: Add 3 new tests**
- [ ] **Step 5: `just check` green**
- [ ] **Step 6: Commit** — `feat(v4): state-machine retry cap escalates to NeedsHelp after N consecutive failures`

### Task 8c.3: Token-refresh-aware orchestrator GitProvider → architectural fix

**Files:**
- Modify: `packages/foreman/src/foreman/v4/pygithub_git_provider.py`
- Modify: `packages/foreman/src/foreman/v4/cli/__init__.py` (main() rewires `_git_factory`)
- Modify: `packages/foreman/tests/v4/test_pygithub_git_provider.py` (extend)

Replace `Github(token)` static construction with a token-getter callable. `PyGithubGitProvider`'s `__init__` becomes:
```python
def __init__(self, *, github_factory: Callable[[], Github], repo_full_name: str) -> None:
    self._github_factory = github_factory
    self._repo_full_name = repo_full_name
    self._cached_github: Github | None = None
    self._cached_at: float | None = None

@property
def _gh(self) -> Github:
    """Return a Github client with a non-expired token. Rebuilds the
    client if the cached one is older than ~50min (well inside the
    1-hour App installation token TTL)."""
    now = time.time()
    if self._cached_github is None or self._cached_at is None or (now - self._cached_at) > 3000:
        self._cached_github = self._github_factory()
        self._cached_at = now
    return self._cached_github
```

`main()` rewires:
```python
def _git_factory(repo: str) -> PyGithubGitProvider:
    return PyGithubGitProvider(
        github_factory=lambda: Github(identity.get_role_token("orchestrator")),
        repo_full_name=repo,
    )
```

Now every PyGithub API call (`list_open_issues_with_label`, etc.) hits `self._gh` first, which refreshes the underlying client if the cache is stale.

Tests:
- `test_pygithub_provider_rebuilds_client_when_cache_expires` — monkey-patch `time.time` to simulate 51 minutes passing; assert `_gh` returns a new Github instance.
- `test_pygithub_provider_uses_cached_client_within_window` — assert two `_gh` accesses within the window return the same instance.
- `test_pygithub_provider_constructor_does_not_invoke_factory` — assert the factory isn't called until first API access (lazy bootstrap).

- [ ] **Step 1: Refactor PyGithubGitProvider constructor + add `_gh` property**
- [ ] **Step 2: Update all PyGithub access sites in the provider to use `self._gh`**
- [ ] **Step 3: Update main()'s `_git_factory` to pass `github_factory=...`**
- [ ] **Step 4: Add 3 new tests**
- [ ] **Step 5: `just check` green**
- [ ] **Step 6: Commit** — `fix(v4): PyGithubGitProvider rebuilds client every ~50min so installation tokens refresh`

### Task 8c.4: ~~Granular label writes~~ — DROPPED, design-decision-no-action

**Resolution:** Re-examined 2026-06-15 after Jeff's pushback. The framing of bug #5 was wrong.

The v4 design is explicit:
- **SQLite is the gospel.** Once a ticket is in SQLite, the state machine drives it from SQLite; labels on the GitHub issue are not re-read.
- **Labels are write-only observability.** Their purpose is for humans viewing the issue page to see *current state*. Accumulating prior state labels would be noise.
- **`foreman:plan` is a one-shot adoption signal.** The Poller uses it ONLY to discover NEW tickets not yet in SQLite. After adoption, it can disappear without harm — the state machine continues from SQLite.

The morning's 40-minute "wedge" was an operator artifact, not a production bug: I had deleted `~/.foreman/v4/state.db` (cleaning SQLite) without realizing the prior run had already replaced `foreman:plan` with `foreman:state-planning` on GitHub. The new daemon's Poller found no `foreman:plan`-labeled issues and (correctly) did nothing. In production, where SQLite isn't manually wiped, this scenario never occurs.

**Keep PyGithub `set_labels(*sorted(labels))` REPLACE semantics.** A single `foreman:state-X` label on the issue at a time IS the right shape for observability. No code change.

If multi-project operators ever need recovery from SQLite loss (genuine production hazard), that's a separate Phase 8d concern: a `foreman reconcile-from-labels` admin command, OR documented "to restart from scratch you must also strip foreman: labels from open tickets". Not in scope here.

**This task is intentionally a no-op.** Renumbering 8c.5 / 8c.6 deferred to keep cross-references stable.

---

#### Original task spec (preserved for archival reference)

**Files:**
- Modify: `packages/foreman/src/foreman/v4/git_provider.py` (GitProvider Protocol — `write_labels` semantics)
- Modify: `packages/foreman/src/foreman/v4/pygithub_git_provider.py` (impl)
- Modify: `packages/foreman/src/foreman/v4/observers/label_observability.py` (call shape)
- Modify: `packages/foreman/tests/v4/test_git_provider_fake.py` (extend FakeGitProvider + tests)
- Modify: `packages/foreman/tests/v4/test_pygithub_git_provider.py` (extend)

Phase 8.1's `write_labels(project, issue_number, labels: set[str])` does PyGithub `set_labels(*sorted(labels))` which REPLACES the entire label set. Switch to granular operations:

New Protocol methods on `GitProvider`:
```python
def add_labels(self, *, project: str, issue_number: int, labels: set[str]) -> None: ...
def remove_labels(self, *, project: str, issue_number: int, labels: set[str]) -> None: ...
```

`LabelObservabilityObserver.on_event(StateEnteredEvent)` becomes:
- Add the new `foreman:state-<new_state>` label via `add_labels`.
- If transitioning FROM a non-Queued state, remove the old `foreman:state-<old_state>` label via `remove_labels`.
- NEVER touch `foreman:plan` or any non-`foreman:state-*` label.

PyGithub implementations:
- `add_labels` → `issue.add_to_labels(*sorted(labels))`
- `remove_labels` → for each label, `issue.remove_from_labels(name)`

The old `write_labels` Protocol method can be **deprecated and dropped** as part of this task (it's not used outside the observer). Or kept as a convenience that internally does `add_labels` — implementer's call, document the choice.

FakeGitProvider extensions:
- Track per-issue label sets accurately (already has `_issue_labels` from Phase 8.1).
- `add_labels` → union with existing.
- `remove_labels` → difference with existing.
- `get_issue_labels` (Phase 8.1's helper) still returns the current set.

Tests:
- `test_add_labels_does_not_touch_existing_labels` — set issue to `{foreman:plan, custom-label}`; call `add_labels({foreman:state-planning})`; assert result is `{foreman:plan, custom-label, foreman:state-planning}`.
- `test_remove_labels_only_touches_specified` — symmetric.
- `test_label_observability_observer_preserves_trigger_label` — simulate state transition; assert `foreman:plan` survives.
- PyGithub-level tests for `add_to_labels` + `remove_from_labels` call shape (mock PyGithub).

- [ ] **Step 1: Add `add_labels` + `remove_labels` to Protocol + FakeGitProvider + PyGithubGitProvider**
- [ ] **Step 2: Rewrite LabelObservabilityObserver to use granular ops + track old state**
- [ ] **Step 3: Add 5+ tests covering preservation + granular semantics**
- [ ] **Step 4: Decide on `write_labels` — drop or keep as add_labels-alias**
- [ ] **Step 5: `just check` green**
- [ ] **Step 6: Commit** — `feat(v4): GitProvider grows add_labels + remove_labels; LabelObservabilityObserver preserves trigger label`

### Task 8c.5: Narrow Phase 8b.4 OSError catch on Windows

**Files:**
- Modify: `packages/foreman/src/foreman/v4/cli/daemon.py`
- Modify: `packages/foreman/tests/v4/cli/test_daemon_commands.py` (extend)

Phase 8b.4's `(ProcessLookupError, OSError)` catches OSError too widely on Windows. Narrow to errno/winerror check that distinguishes dead-PID from alive-but-inaccessible:

```python
def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False  # POSIX dead PID
    except OSError as exc:
        # Windows: WinError 87 = ERROR_INVALID_PARAMETER = PID isn't a
        # live process. Other OSError values (e.g., WinError 5 =
        # ERROR_ACCESS_DENIED) indicate a LIVE process we can't probe;
        # treat those as alive.
        if hasattr(exc, "winerror") and exc.winerror == 87:
            return False
        return True
```

Both `cmd_daemon_status` and `cmd_daemon_stop` call this helper instead of inline try/except.

Tests:
- Existing tests stay valid; tighten the Windows-OSError-on-dead-PID test to use `OSError(0, "", None, 87)` (winerror=87) instead of `OSError(22, "Invalid argument")`.
- Add `test_pid_alive_helper_treats_winerror_87_as_dead` — assert helper returns False on WinError 87.
- Add `test_pid_alive_helper_treats_other_winerror_as_alive` — assert helper returns True on WinError 5 (access denied), simulating a live PID we can't probe.

- [ ] **Step 1: Extract `_is_pid_alive(pid)` helper to module-level**
- [ ] **Step 2: Use helper in `cmd_daemon_status` + `cmd_daemon_stop`**
- [ ] **Step 3: Update existing Windows-OSError tests + add 2 new ones**
- [ ] **Step 4: `just check` green**
- [ ] **Step 5: Commit** — `fix(v4): _is_pid_alive helper distinguishes Windows dead-PID (winerror 87) from access-denied`

### Task 8c.6: Re-run the algokit#21 dogfood — drive to terminal state

**This is a manual task.**

After 8c.1–8c.5 land:

1. Resume the held ticket: `FOREMAN_V4_CONFIG=~/.foreman/v4/config.toml uv run foreman resume 1` (will need to re-apply `foreman:plan` if 8c.4's preservation isn't retro-active to the existing state).
2. Restart the daemon: `uv run foreman daemon start`.
3. Watch `~/.foreman/v4/logs/transitions.jsonl` + `foreman ps`.

**Acceptance criteria — at least one of:**
- Ticket reaches `Done` (full chain: spec PR → spec merge → impl PR → impl merge → close issue)
- Ticket reaches `NeedsHelp` via a real role decision (Reviewer rejects, Worker can't implement, etc.) — not a config/role-aliasing crash
- Ticket reaches `Failed` via the new retry-cap (proves 8c.2 works) — but if so, the underlying retry trigger is itself a finding for Phase 8d

**Daemon stays alive for >60min** — proves 8c.3 token refresh works.

### Phase 8c gate

- [ ] `just check` green
- [ ] Task 8c.6 reports a terminal state reached on algokit#21 AND the daemon survived past minute 60

Phase 8c completion criterion: **v4 actually runs autonomously through the full chain against a real project without wedging.** Phase 9 (deletion + RUNBOOK + PR) is safe when 8c.6 succeeds.

---
