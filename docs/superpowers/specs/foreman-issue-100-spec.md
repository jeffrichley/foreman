# Spec: `foreman daemon reload` — pick up newly-added projects without a full restart (issue #100)

## Goal

Add a `foreman daemon reload` CLI subcommand that signals a running v3
reconciler daemon to re-read `~/.foreman/config.toml` and update its
in-memory project registry without restarting. After reload, projects
added to the config since startup begin being reconciled on the next
tick; projects removed stop being reconciled, while any role
subprocesses already in flight for them complete naturally. In-flight
work for unchanged projects is untouched.

Tracks issue [#100](https://github.com/jeffrichley/foreman/issues/100).
Related: foreman#72 (lock file as PID source), foreman#88 (lock file
as start-up mutex), foreman#106 (v3 reconciler architecture).

## Acceptance criteria

- A new `foreman daemon reload` subcommand exists alongside `daemon
  start`, `daemon v3-start`, `daemon stop`, `daemon status` in
  `packages/foreman/src/foreman/cli.py`. Its lifecycle ergonomics match
  `daemon stop`:
  - Resolves the daemon's lock path via the existing
    `_resolve_lock_path(config)` helper
    (`cli.py:397-411`).
  - **Gate on the lock file BEFORE writing the reload sentinel**: if
    the lock file is absent, print a diagnostic naming the resolved
    path and exit 0 WITHOUT writing the sentinel. A stale sentinel
    left on disk would silently fire on the next `daemon v3-start`
    (the reconciler's first tick would consume it and reload an
    already-fresh config), which mirrors the documented bug class
    that motivated the parallel gate on `daemon stop`
    (`cli.py:751-767`).
  - If the lock file is present but its PID content is unreadable,
    print a clear message and exit 0 without writing the sentinel —
    same shape as `daemon stop` (`cli.py:769-776`).
  - If the lock file is present and parseable, write
    `reconciler.reload_sentinel_path` (a new config field — see
    below) with a single line of content describing when + by what
    the request was made (mirroring the shutdown sentinel's
    timestamp body at `cli.py:785-788`), then echo
    `"reload requested via sentinel: <path>"`.
  - **No SIGHUP / `os.kill` on POSIX**. The sentinel is the only
    channel — keeping it cross-platform-symmetric eliminates a
    second code path to reason about, and the tick-poll latency
    (default `reconciler.poll_interval_seconds = 60`) is acceptable
    for an interactive operator gesture. The command's echo names
    the poll-interval-bounded latency so the operator knows what to
    expect.
- A new `ReconcilerConfig.reload_sentinel_path: str` field is added in
  `packages/foreman/src/foreman/config.py` with default
  `"~/.foreman/reload-requested"`. Description string mirrors the
  shape of `shutdown_sentinel_path`'s description
  (`config.py:136-147`): explains the cross-platform sentinel
  channel, why the reconciler polls it, and what the daemon does on
  detection.
- A new `_resolve_reload_sentinel_path(config: Config | None) -> Path`
  helper in `cli.py` lives next to `_resolve_shutdown_sentinel_path`
  (`cli.py:414-429`) and implements the same env-override precedence
  with a new env var `FOREMAN_RELOAD_SENTINEL_PATH`. Empty-string
  env value treated as unset; `None` config falls back to the
  hardcoded default — matching the existing helper byte-for-byte
  except for the path constants.
- The existing inline project-resolution loop in `daemon_v3_start`
  (`cli.py:566-587`) is extracted into a module-level helper
  `_build_reconciler_projects(config: Config) -> tuple[ReconcilerProject, ...]`.
  This is a pure refactor with identical behavior (including the
  `ClickException` on malformed `owner/name`); both `daemon_v3_start`
  and the new reload callback closure call this helper so they
  cannot drift.
- `Reconciler.__init__` (`reconciler/daemon.py:113-145`) gains:
  - A new keyword-only parameter
    `reload_callback: Callable[[], tuple[ReconcilerProject, ...]] | None = None`.
    When `None`, the reload mechanism is inert (used by tests that
    don't need it — parallel to how `shutdown_sentinel_path=None`
    disables shutdown-sentinel polling today).
  - A new keyword-only parameter
    `reload_sentinel_path: Path | str | None = None`, normalized to
    `Path(...).expanduser()` (parallel to `_shutdown_sentinel_path`
    at `reconciler/daemon.py:141-145`).
- `Reconciler.tick()` polls the reload sentinel **BEFORE** the
  per-project reconciliation loop so newly-added projects are
  reconciled THIS tick rather than waiting a full
  `poll_interval_seconds`. Order inside `tick()`:
  1. Check reload sentinel; on hit, consume + apply diff (see below).
  2. Iterate `self.projects` for reconciliation (existing logic at
     `reconciler/daemon.py:149-183`).
  3. Check shutdown sentinel (existing logic at
     `reconciler/daemon.py:192-198`).
- `Reconciler._apply_reload()` (new private method):
  - Calls `self._reload_callback()` to obtain a fresh
    `tuple[ReconcilerProject, ...]`.
  - Computes additions = new − current, removals = current − new
    (compared by `ReconcilerProject.name` since `auto_merge_*` may
    have been edited per-project without renaming).
  - If `len(additions) == 0 and len(removals) == 0 AND every
    surviving project's resolved fields match`: logs INFO
    `"config reload: no changes"` and returns. This is the
    issue's idempotence acceptance criterion.
  - Otherwise: sets `self.projects = new_tuple`,
    initializes `self._consecutive_failures[name] = 0` for each
    addition, deletes `self._consecutive_failures[name]` for each
    removal, and logs INFO with `extra={"added": [...names...],
    "removed": [...names...]}`. Also writes one
    `config_reload` action row to the execution log (ticket_id
    `daemon:reload`, project `""`, rule_name `None`, outcome
    `executed`, details containing the added/removed names) so
    audit logs show reloads alongside other reconciler activity.
  - If the callback raises (e.g., TOML parse error from the
    operator mid-editing the file, or pydantic validation error
    from a missing required field): logs a CLEAR error naming the
    config path and the exception, writes one `config_reload`
    row with outcome `failed` and details containing
    `error_class` + `error_message`, leaves `self.projects` AND
    `self._consecutive_failures` unchanged, and returns without
    re-raising. The daemon keeps running. **The sentinel is
    consumed regardless** so the operator's next reload attempt
    is the canonical retry — we do not loop on a broken config
    every tick.
- `Reconciler._reload_sentinel_present` and
  `_consume_reload_sentinel` private methods mirror the
  shutdown-sentinel helpers at `reconciler/daemon.py:200-229`
  byte-for-byte modulo the path/log-message constants.
- `daemon_v3_start` (`cli.py:474-673`) is updated to:
  - Resolve the reload sentinel path via the new helper.
  - Add a stale-reload-sentinel cleanup block parallel to the
    existing stale-shutdown-sentinel cleanup at
    `cli.py:537-549`. Same rationale: a sentinel left by a
    no-op reload (or by some prior edge case) would silently
    fire on first tick. Cleanup runs INSIDE the `DaemonLock`
    block so no second `daemon reload` can race the unlink.
  - Build a `reload_callback` closure that captures `cfg_path`
    (the same value already used to load the initial config)
    and, on each call, re-reads the config and re-runs
    `_build_reconciler_projects(config)`. The closure must
    NOT cache config across calls — every reload reads fresh.
  - Pass `reload_callback=...` and
    `reload_sentinel_path=config.reconciler.reload_sentinel_path`
    to the `Reconciler(...)` constructor.
- **No changes to the v2 daemon (`foreman daemon start`)**. v2 is
  slated for removal once v3 is stable
  (`packages/foreman/src/foreman/daemon.py:1-12`); adding reload to
  a deprecated module pays no return. `daemon reload` against a
  v2-only deployment exits 0 with the same "no daemon running"
  diagnostic — there's no v2 lock file at the v3 path either.
- In-flight role subprocesses are NOT cancelled by reload. The
  V3GitHubHost's semaphore-bounded subprocesses run to completion;
  they hold no reference to `self.projects` after dispatch. Removing
  a project removes future ticks' actions for it, but a subprocess
  already running for that project lands its outcome via the normal
  execution-log path. This satisfies the issue's "existing in-flight
  pipelines for those projects complete naturally" criterion without
  special code.
- New tests in `packages/foreman/tests/reconciler/test_reconciler_e2e.py`
  (using the existing `_StubGHClient` + `_StubHost` fixtures):
  - `test_reconciler_reload_adds_project_on_next_tick` — start with
    one project, write the reload sentinel, set the callback to
    return two projects, run one tick, assert `self.projects` has
    both AND `_consecutive_failures` contains both names. Verify
    the sentinel file is deleted after the tick.
  - `test_reconciler_reload_removes_project_and_cleans_failures` —
    start with two projects, seed
    `_consecutive_failures["going-away"] = 2`, write the sentinel,
    set the callback to return only the surviving project, run one
    tick, assert `self.projects` has one entry, `"going-away"` is
    not in `_consecutive_failures`.
  - `test_reconciler_reload_idempotent_when_unchanged_logs_info` —
    callback returns the same projects, write the sentinel, run a
    tick, use `caplog` to assert one INFO entry with substring
    `"config reload: no changes"`, assert no exec_log row of
    action `config_reload`.
  - `test_reconciler_reload_failure_keeps_running_and_logs` — set
    the callback to raise (simulate TOML parse error), write the
    sentinel, run a tick, assert `self.projects` is unchanged,
    sentinel is consumed (file gone), an exec_log row was written
    with outcome `failed`, and the daemon's stop event is NOT set.
  - `test_reconciler_reload_inert_when_no_callback` — construct
    Reconciler without `reload_callback` (the `None` default),
    write to a sentinel path that was also not provided, assert
    `tick()` runs cleanly and no AttributeError surfaces.
- New tests in `packages/foreman/tests/test_cli.py`:
  - `test_daemon_reload_writes_sentinel_when_lock_present` — create
    a tmp lock file with a parseable PID, monkeypatch
    `FOREMAN_LOCK_PATH` + `FOREMAN_SHUTDOWN_SENTINEL_PATH` +
    `FOREMAN_RELOAD_SENTINEL_PATH` to tmp paths, run
    `foreman daemon reload`, assert exit 0, the reload sentinel
    file exists with the expected timestamp-bearing content, and
    the echo names the sentinel path.
  - `test_daemon_reload_refuses_when_no_lock_file` — no lock file,
    run `foreman daemon reload`, assert exit 0, the reload sentinel
    file does NOT exist, and the diagnostic names the resolved lock
    path. This is the defense-in-depth gate.
  - `test_daemon_reload_refuses_when_lock_pid_unreadable` — write
    garbage to the lock file, run reload, assert sentinel was NOT
    written and the diagnostic names the lock path.
- New tests in `packages/foreman/tests/test_cli_v3.py`:
  - `test_v3_start_removes_stale_reload_sentinel_at_startup` —
    parallel to the existing
    `test_v3_start_removes_stale_sentinel_at_startup`
    (`test_cli_v3.py:62-98`). Pre-write a reload sentinel to a tmp
    path, point config at it, run `daemon v3-start --max-ticks 0`,
    assert the sentinel file is gone.
- New tests in `packages/foreman/tests/test_config.py` (if one
  doesn't already exist for the field, otherwise add):
  - `test_reconciler_reload_sentinel_path_default` — load an empty
    `[reconciler]` block, assert `reload_sentinel_path` is
    `"~/.foreman/reload-requested"`.
  - `test_reconciler_reload_sentinel_path_override` — load TOML
    that sets the field, assert it round-trips.
- `README.md`: under "Architecture", add one sentence to the
  paragraph describing the daemon: "Add projects via `foreman init`
  then call `foreman daemon reload` to register them with the
  running daemon (no restart needed)." Keep the existing
  one-paragraph architecture summary — do not expand it into a
  separate section.
- `packages/foreman/src/foreman/templates/instructions.md.template`:
  add a new section after "Quality gate" titled "Daemon lifecycle"
  containing 2-3 sentences: adding projects to the config requires
  `foreman daemon reload`; removing projects also requires it; the
  next tick (up to `reconciler.poll_interval_seconds`, default 60s)
  is when the change takes effect.
- `just check` exits zero on the worktree branch with all the
  changes applied.

## Approach

The shutdown-sentinel pattern shipped in v3 (config.py:136-147,
cli.py:414-429, cli.py:782-789, reconciler/daemon.py:141-145,
200-229) already provides a clean cross-platform daemon-signaling
mechanism: a known file path that the CLI writes to and the
reconciler polls each tick. Reloading projects is structurally the
same signal — "operator wants daemon to do X soon" — so we add a
parallel sentinel rather than invent a second mechanism. One pattern,
two uses, predictable surface.

Why a callback rather than a `Config` reference inside the
Reconciler? The Reconciler currently has no dependency on
`foreman.config.Config` or `load_config` — it consumes
`ReconcilerProject` instances assembled by the CLI. Threading
`Config` into the Reconciler would couple the v3 core to the
config-loading layer for one operation. A callback returning the
already-resolved project tuple keeps the Reconciler's surface narrow
and keeps the per-project field-resolution logic
(`effective_auto_merge_spec` / `effective_auto_merge_impl`) in the
CLI's `_build_reconciler_projects` helper where it already lives —
which is the same shape the constructor uses at startup.

Why poll BEFORE the reconciliation loop in `tick()`? The issue's
"start polling any newly-added projects" criterion is best satisfied
when the operator's wait is one tick rather than two. Detecting the
sentinel at the top of the tick means a project added moments before
a tick gets its first observer query in that same tick. The shutdown
sentinel is checked after the loop because shutdown should let an
in-flight tick complete; for reload, an in-flight tick has already
iterated `self.projects` as it stood at tick-start, so checking
first only changes the *next* tick's project set — there's no
mid-tick race to defend against.

Why gate on the lock file before writing the reload sentinel? Same
class of bug `daemon stop` already avoids
(`cli.py:751-767`): if there's no daemon to consume the sentinel,
the sentinel sits on disk and silently kills... no, *reloads* the
next `daemon v3-start` on its first tick. Reload of an already-fresh
config is technically a no-op, but it's an audit-log entry that
confuses anyone tracing through "why did the daemon log a reload it
never received a command for?". Defense in depth: write only when a
live daemon's PID is in the lock file, and rely on the v3-start
stale-cleanup as a second line.

Why no SIGHUP on POSIX? Adding a second signaling channel would
mean two different latency profiles to document and two code paths
to keep working. The shutdown sentinel design already accepted the
poll-interval-bounded latency for cross-platform symmetry; reload is
even less urgent than shutdown. Keep it sentinel-only.

Why is config reload-failure non-fatal? The issue's out-of-scope
clause says "surface clearly but keep the running daemon alive on
parse failure". A daemon that crashes when the operator mistypes a
TOML key is a strictly worse UX than the current "have to bounce the
daemon" — the operator would still have to bounce, AND they'd have
lost the in-flight pipelines. We log the error, write a
`config_reload` exec_log row with outcome `failed` for the
operator's audit, and keep going. The sentinel gets consumed so the
operator's NEXT reload command is the canonical retry, not a
broken-config retry loop on every tick.

## Sub-requests (topologically sorted)

1. Add `reload_sentinel_path: str` field to `ReconcilerConfig` in
   `packages/foreman/src/foreman/config.py`, defaulting to
   `"~/.foreman/reload-requested"`. Description string mirrors the
   `shutdown_sentinel_path` description shape.

2. Add `_resolve_reload_sentinel_path(config: Config | None) -> Path`
   in `cli.py`, parallel to `_resolve_shutdown_sentinel_path`
   (`cli.py:414-429`). Env var name:
   `FOREMAN_RELOAD_SENTINEL_PATH`.

3. Extract the existing project-resolution loop in `daemon_v3_start`
   (`cli.py:566-587`) into a module-level helper
   `_build_reconciler_projects(config: Config) -> tuple[ReconcilerProject, ...]`.
   Replace the inline code with a call to the helper. Keep the
   `ClickException` on malformed `owner/name` inside the helper.

4. Add three new keyword-only parameters to `Reconciler.__init__`
   in `packages/foreman/src/foreman/reconciler/daemon.py`:
   `reload_callback: Callable[[], tuple[ReconcilerProject, ...]] | None = None`,
   `reload_sentinel_path: Path | str | None = None`, normalized via
   `Path(...).expanduser()` parallel to the existing
   `_shutdown_sentinel_path` field at `reconciler/daemon.py:141-145`.

5. Add `_reload_sentinel_present(self) -> bool` and
   `_consume_reload_sentinel(self) -> None` private methods on
   `Reconciler`, mirroring the shutdown helpers at
   `reconciler/daemon.py:200-229` modulo path/log message.

6. Add `_apply_reload(self) -> None` private method on
   `Reconciler` per the Acceptance Criteria's diff + log + exec_log
   row + idempotence + failure-tolerance contract.

7. Update `Reconciler.tick()` (`reconciler/daemon.py:147-198`) so
   that — BEFORE the per-project reconciliation loop — it checks
   `_reload_sentinel_present()`; on hit, consumes the sentinel then
   calls `_apply_reload()`. The shutdown-sentinel check remains
   AFTER the project loop (no behavior change).

8. Add a stale-reload-sentinel cleanup block in `daemon_v3_start`
   (`cli.py:537-549`), parallel to the stale-shutdown-sentinel
   block. Place it immediately after the existing shutdown-sentinel
   cleanup. Same try/except/log shape.

9. Wire the reload callback closure in `daemon_v3_start` after the
   refactor in (3). The closure captures `cfg_path` (already in
   scope) and calls
   `_build_reconciler_projects(load_config(cfg_path))` each
   invocation — no caching. Pass `reload_callback=...` and
   `reload_sentinel_path=config.reconciler.reload_sentinel_path` to
   the `Reconciler(...)` constructor at `cli.py:602-611`.

10. Add the `@daemon.command("reload")` subcommand in `cli.py`
    after `daemon_stop` (`cli.py:718-840`). It resolves the lock
    path, gates on lock-file presence + parseable PID (re-using
    `_read_lock_file_pid` from `cli.py:432-446`), then writes the
    reload sentinel via `_resolve_reload_sentinel_path(config)`.
    Output messages match the `daemon stop` echo shape.

11. Add Reconciler reload tests in
    `packages/foreman/tests/reconciler/test_reconciler_e2e.py`
    using the existing `_StubGHClient` + `_StubHost` fixtures.
    Five tests per the Acceptance Criteria. Use `caplog` for the
    INFO assertions; use `log.query(...)`-equivalent assertions
    against the exec_log SQLite directly (the file already uses
    `ExecutionLog` in this test module).

12. Add CLI reload tests in `packages/foreman/tests/test_cli.py`.
    Three tests per the Acceptance Criteria. Monkeypatch all three
    env vars (`FOREMAN_LOCK_PATH`, `FOREMAN_SHUTDOWN_SENTINEL_PATH`,
    `FOREMAN_RELOAD_SENTINEL_PATH`) to tmp paths so the test
    cannot touch `~/.foreman/*`.

13. Add the stale-reload-sentinel cleanup test in
    `packages/foreman/tests/test_cli_v3.py`, parallel to the
    existing `test_v3_start_removes_stale_sentinel_at_startup`.

14. Add the two config-field tests in
    `packages/foreman/tests/test_config.py` if not already covered
    by a parametrized "all ReconcilerConfig defaults round-trip"
    test (if such a test exists, extend its expectations rather
    than duplicate).

15. Update `README.md` per the Acceptance Criteria — one sentence,
    no new section.

16. Update
    `packages/foreman/src/foreman/templates/instructions.md.template`
    with the new "Daemon lifecycle" section.

17. Run `uv run --no-sync pytest packages/foreman/tests/reconciler/test_reconciler_e2e.py packages/foreman/tests/test_cli.py packages/foreman/tests/test_cli_v3.py packages/foreman/tests/test_config.py -v`.

18. Run `just check` and confirm exit zero.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/config.py` | Add `reload_sentinel_path: str` field on `ReconcilerConfig` with default `"~/.foreman/reload-requested"` and a description string mirroring `shutdown_sentinel_path`. |
| `packages/foreman/src/foreman/cli.py` | Add `_resolve_reload_sentinel_path` helper (parallel to `_resolve_shutdown_sentinel_path`). Extract `_build_reconciler_projects(config)` helper from the existing inline loop in `daemon_v3_start`. In `daemon_v3_start`, add stale-reload-sentinel cleanup parallel to the existing stale-shutdown-sentinel cleanup, build the reload callback closure, and pass `reload_callback` + `reload_sentinel_path` into the `Reconciler` constructor. Add the new `@daemon.command("reload")` subcommand: lock-file gate, PID-parseability gate, write sentinel, echo. |
| `packages/foreman/src/foreman/reconciler/daemon.py` | Extend `Reconciler.__init__` with `reload_callback` and `reload_sentinel_path` keyword-only params. Add `_reload_sentinel_present`, `_consume_reload_sentinel`, `_apply_reload` private methods. Wire the reload sentinel poll at the TOP of `tick()` (shutdown sentinel poll stays at the bottom). |
| `packages/foreman/tests/reconciler/test_reconciler_e2e.py` | Add five new tests: add-project, remove-project (+failures cleanup), idempotent-no-changes, callback-failure-non-fatal, no-callback-inert. |
| `packages/foreman/tests/test_cli.py` | Add three new tests for `foreman daemon reload`: writes sentinel when lock present; refuses when no lock; refuses when lock PID unreadable. |
| `packages/foreman/tests/test_cli_v3.py` | Add `test_v3_start_removes_stale_reload_sentinel_at_startup`, parallel to the existing stale-shutdown-sentinel test. |
| `packages/foreman/tests/test_config.py` | Add default + override round-trip tests for `reconciler.reload_sentinel_path` (or extend an existing parametrized test for `ReconcilerConfig` defaults). |
| `README.md` | One sentence under "Architecture" naming `foreman daemon reload` as the way to register newly-added projects. |
| `packages/foreman/src/foreman/templates/instructions.md.template` | New "Daemon lifecycle" section: 2-3 sentences explaining when `foreman daemon reload` is needed. |

## Alternatives considered

- **Option B from the issue: periodic config-file mtime hot-reload.**
  Rejected (matches the issue author's recommendation): the
  daemon-polls-the-file design forces a decision on how to handle a
  partial TOML mid-edit (the operator's editor may write
  `config.toml` in two steps), introduces a new tunable
  (`daemon.config_reload_interval`), and provides no extra value
  over Option A for the dogfood use case (one project added during a
  meeting). Filed in Out of scope as a follow-up after Option A is
  exercised in production.

- **SIGHUP on POSIX as a fast-path companion to the sentinel.**
  Rejected: two channels, two latency profiles, two code paths. The
  shutdown-sentinel design already accepted poll-interval latency
  for cross-platform symmetry; reload is less time-sensitive than
  shutdown. One pattern beats two-pattern hedge.

- **Re-read `Config` from inside the Reconciler instead of via a
  callback.** Rejected: would couple the v3 core to
  `foreman.config.load_config` and to the per-project resolution
  logic (`effective_auto_merge_spec` etc.), which currently lives
  cleanly in the CLI layer. A callback returning already-resolved
  `ReconcilerProject` instances keeps the Reconciler's API surface
  narrow and lets the CLI own the resolution rules.

- **Make `daemon reload` also re-evaluate non-project knobs
  (poll_interval, alert_after_n_failures, auto_merge_*).**
  Rejected as scope creep: the issue is about project add / remove.
  Reloading runtime knobs mid-loop has separate implications
  (does poll_interval take effect this tick or next? does
  alert_after_n_failures reset the failure counter?). Filed in Out
  of scope; a separate spec can lift the scope deliberately.

- **Auto-call `foreman daemon reload` from `foreman init` after
  successful registration.** Rejected for this spec — the issue's
  out-of-scope clause names it as a deliberate follow-up. The
  current spec ships the building block; the ergonomic chaining
  belongs in a separate ticket so its UX trade-offs (silent failure
  if no daemon running? prompt the user?) can be reasoned about on
  their own.

- **Add `foreman daemon reload` to the v2 daemon.** Rejected: v2 is
  slated for removal once v3 is proven stable
  (`packages/foreman/src/foreman/daemon.py:1-12`). Investing in v2
  pays no return; the operator gets reload via v3, which is the
  production daemon.

- **Do nothing (recommend operators script `daemon stop && daemon
  v3-start`).** Rejected: the issue explicitly enumerates the cost —
  interrupted in-flight pipelines, churned adapter state,
  Discord-shard reconnect storms downstream. The whole point of
  reload is to make the common operation (add one project)
  zero-cost.

## Open questions

(none — the issue's preferred shape is Option A with a concrete
acceptance-criteria list, the codebase already has a
shutdown-sentinel pattern this spec can mirror byte-for-byte, and
both the in-flight and idempotence semantics map cleanly onto the
existing `tick()` structure.)

## Out of scope

- **Option B periodic config-mtime hot-reload.** File as a
  follow-up after Option A has been exercised in production.

- **`foreman init` auto-calling `daemon reload` after a successful
  project registration.** Per the issue's explicit out-of-scope
  clause; tracked separately.

- **Reloading non-project ReconcilerConfig knobs (poll_interval,
  alert_after_n_failures, auto_merge_*).** Open as a separate
  spec if needed; this spec intentionally limits the contract to
  the project registry.

- **Reloading `[orchestrator]` / `[apps]` credentials.** Same
  rationale — would require deciding what to do with in-flight role
  subprocesses still holding the old installation token. Out of
  scope here.

- **Adding `foreman daemon reload` to the v2 `daemon start`
  daemon.** v2 is deprecated; no return on the investment.

- **Foregrounding the latency wait: making `daemon reload` block
  until the running daemon's exec_log shows the
  `config_reload` row.** Nicer UX, but introduces a second sqlite
  consumer of the exec_log file and a new `foreman daemon
  reload --wait` flag. Filed as a follow-up; the sub-second TODO
  cost of `up to <poll_interval_seconds> latency` is acceptable for
  the dogfood path the issue motivates.

- **Reloading project paths (`local_clone_path`,
  `dev_base_branch`) for projects that ALREADY have an in-flight
  pipeline.** The contract here is "future actions use the new
  values"; in-flight subprocesses already captured their paths at
  dispatch time. A separate spec can address mid-flight
  re-targeting if it ever becomes an operator need.
