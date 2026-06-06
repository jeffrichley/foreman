# Spec: audit pre-docker defensive code in foreman package (issue #141)

## Goal

Inventory every module, function, and test in `packages/foreman/` whose
primary purpose is defending the daemon against runtime concerns that the
Docker runtime (PRs #129, #135, #136 — see
`docs/superpowers/specs/2026-06-05-foreman-docker-runtime-design.md`) now
handles by construction. Classify each item as **Remove** (Docker handles
it; the code is dead weight inside the container AND the project has
documented that the daemon "never runs on Windows" post-cutover),
**Keep, host-only** (still relevant when an operator invokes `foreman
daemon v3-start` directly on a Linux host without compose), or **Keep,
both** (relevant regardless of runtime). Propose one follow-up ticket per
Remove cluster so the autonomous loop can chew through them. Issue
[#141](https://github.com/jeffrichley/foreman/issues/141). **No code
changes land in the PR that fulfils this spec** — the deliverable is
this analysis and the follow-up ticket proposals listed in
`## Sub-requests`.

## Acceptance criteria

- The audit spec under `docs/superpowers/specs/` enumerates every item
  matched by the inventory predicates in `## Approach` (Windows-specific
  `sys.platform`/`msvcrt`/`TerminateProcess`/`os.kill(...SIGTERM)` paths
  in daemon code, the `DaemonLock` PID-file singleton mutex, the v2
  daemon's crash-recovery hook, and the per-tick sentinel rationale).
- Each inventoried item carries an explicit classification of
  **Remove**, **Keep, host-only**, or **Keep, both** plus a one-sentence
  rationale grounded in the docker-runtime design's explicit choices
  (single named container `foreman-daemon`, `init: true` for zombie
  reaping, daemon process IS PID 1 of an isolated FS, Windows dropped
  from supported runtimes).
- A bulleted list of follow-up tickets — one per Remove cluster — names
  the files touched, the lines/symbols to drop, and the post-change
  invariant the unit tests should pin (e.g. "after-change: `Grep -n
  'msvcrt' packages/foreman/` returns no hits").
- The spec does NOT propose any code edits in the PR that ships it. The
  PR is docs-only; merging it queues the follow-up tickets for separate
  Planner cycles.
- The spec calls out the one nuance the issue body did not anticipate:
  `docker-compose.yml` does NOT currently set a `restart` policy (see
  `docker-compose.yml:11-54`; the design spec at
  `2026-06-05-foreman-docker-runtime-design.md:430-433` explicitly
  rejects `restart: unless-stopped` for v1). So "Compose `restart`
  policies handle daemon crash recovery" is not yet true — both host
  and container modes "stay down on crash". The audit must flag this
  so we don't remove crash-recovery code on a runtime claim that
  hasn't shipped.

## Approach

The docker design spec
(`docs/superpowers/specs/2026-06-05-foreman-docker-runtime-design.md`)
locks four runtime invariants that the daemon's defensive code predates:

1. **Single named container.** `docker-compose.yml:17` sets
   `container_name: foreman-daemon`; `docker compose up -d daemon`
   errors when a container by that name already exists. That is the
   docker-side replacement for "PID-based singleton daemon enforcement"
   inside the process.
2. **`init: true` (`docker-compose.yml:21`) injects tini as PID 1.**
   tini reaps zombie children and forwards `SIGTERM` to the daemon
   process. The "Python-as-PID-1 won't reap zombies" defensive shape
   doesn't exist in the daemon today (Python wasn't PID 1 on the
   host), so this isn't a removal candidate — it's just confirmation
   the docker side is sound.
3. **Immutable image — no stale executable.** The `foreman` binary at
   `/app/venv/bin/foreman` is built once per `docker compose build` and
   never overwritten by a running subprocess. The original 2026-06-05
   crash class (`uv run foreman …` rewriting `foreman.exe` while the
   running daemon held the lock) cannot occur in-container. None of
   foreman's Python defends against this — the defense was
   `--no-sync` on the `uv run` argv path, and PR #135 already removed
   that codepath in favor of direct `["foreman", subcommand, …]`
   dispatch (see `packages/foreman/src/foreman/reconciler/v3_host.py:279-310`).
4. **Daemon never runs on Windows post-cutover.** The design spec is
   explicit
   (`2026-06-05-foreman-docker-runtime-design.md:64-69, 537-547`):
   "We drop Windows from the CI matrix permanently because the daemon
   never runs there." That alone makes every `sys.platform == "win32"`
   branch in `packages/foreman/src/foreman/{cli.py, daemon_lock.py}`
   dead weight in the supported runtime — even though the code still
   compiles cleanly on a Windows interpreter.

The one runtime concern that the docker side does NOT currently handle
is daemon-crash auto-restart: `docker-compose.yml` ships without a
`restart:` policy, by design spec choice. So crash-recovery code (the
v3 `ExecutionLog.recover_orphaned()` path at
`packages/foreman/src/foreman/reconciler/exec_log.py:213-238` and its
caller at `packages/foreman/src/foreman/cli.py:636-640`) is still
load-bearing in both runtimes, because *something* manually restarts
the daemon (operator-driven `docker compose up -d daemon` or the host
equivalent), and on restart we need to sweep `outcome='running'`
orphans. The audit treats these as **Keep, both**.

The inventory uses these predicates against the foreman package:

- `Grep -n "sys.platform" packages/foreman/src/`
- `Grep -n "msvcrt\|TerminateProcess" packages/foreman/`
- `Grep -nI "DaemonLock\|daemon_lock" packages/foreman/`
- Module-level "DEPRECATED" docstrings in v2 surfaces
  (`packages/foreman/src/foreman/daemon.py:1`).
- Test-side `@pytest.mark.skipif(sys.platform == "win32", …)`
  decorators in `packages/foreman/tests/test_cli.py`.

Each hit is then mapped against the docker invariants above to assign
a classification.

## Sub-requests (topologically sorted)

These are follow-up ticket proposals. They are listed in dependency
order so a Planner picking them up can execute without back-references.
The PR that ships this audit doesn't make any of these edits — it just
records the proposal.

1. **Inventory table (this spec, no follow-up ticket).** Captured in
   `## File-level changes` below — itself the deliverable.
2. **Follow-up ticket A — "remove Windows branches in daemon_lock.py".**
   Drop the `if sys.platform == "win32": import msvcrt` branch from
   `_acquire_exclusive_nonblocking`
   (`packages/foreman/src/foreman/daemon_lock.py:86-108`). Drop the
   `_WINDOWS_LOCK_OFFSET = 1024` constant
   (`packages/foreman/src/foreman/daemon_lock.py:83`). Keep the
   POSIX `fcntl.flock` path. Update the module docstring to drop the
   "Windows-specific lock semantics" framing. Invariant after change:
   `Grep -n "msvcrt\|_WINDOWS_LOCK_OFFSET" packages/foreman/src/foreman/daemon_lock.py`
   returns no hits.
3. **Follow-up ticket B — "remove Windows branches in cli.py daemon
   subcommands".** Drop the `if sys.platform == "win32"` branch in
   `daemon_v3_start::_run` that installs `signal.signal`-based
   handlers via `_windows_handler` and `loop.call_soon_threadsafe`
   (`packages/foreman/src/foreman/cli.py:711-720`). Drop the
   `daemon_stop` early-return Windows branch
   (`packages/foreman/src/foreman/cli.py:881-892`). Collapse the
   `discover = "tasklist | findstr foreman" if sys.platform == "win32"
   else "ps aux | grep foreman"` ternaries in `daemon_stop`
   (`packages/foreman/src/foreman/cli.py:843-848`) and `daemon_reload`
   (`packages/foreman/src/foreman/cli.py:962-966`) to the POSIX
   branch only. Update docstrings to drop the TerminateProcess
   justification paragraphs. Invariant after change:
   `Grep -n "win32\|TerminateProcess\|tasklist" packages/foreman/src/foreman/cli.py`
   returns no hits.
4. **Follow-up ticket C — "remove Windows skipif + msvcrt fixtures
   in test_cli.py".** Drop the two
   `@pytest.mark.skipif(sys.platform == "win32", …)` decorators at
   `packages/foreman/tests/test_cli.py:995-1004` and
   `packages/foreman/tests/test_cli.py:1044-1051`. Drop the
   `if sys.platform == "win32": import msvcrt; … msvcrt.locking(…)`
   branch in `test_daemon_start_handles_unreadable_lock_content_gracefully`
   (`packages/foreman/tests/test_cli.py:728-740`), leaving only the
   POSIX `fcntl.flock` path. Strip the TerminateProcess rationale
   block comment at
   `packages/foreman/tests/test_cli.py:1223-1231`. Invariant after
   change: `Grep -n "msvcrt\|win32" packages/foreman/tests/test_cli.py`
   returns no hits.
5. **Follow-up ticket D — "drop TerminateProcess rationale prose
   from daemon docs + comments".** In the four sites where the
   "On Windows, `os.kill` maps to TerminateProcess" rationale appears
   purely as prose (no code branch): the
   `daemon_v3_start` docstring
   (`packages/foreman/src/foreman/cli.py:553-560`), the
   `daemon_stop` docstring
   (`packages/foreman/src/foreman/cli.py:808-829`), the
   `ReconcilerConfig.shutdown_sentinel_path` field description
   (`packages/foreman/src/foreman/config.py:151-161`), and the
   reconciler tick-end sentinel-check comment
   (`packages/foreman/src/foreman/reconciler/daemon.py:209-215`)
   — replace the Windows-specific rationale with a docker/host-mode-
   neutral "sentinel-file signal for graceful in-tick shutdown"
   phrasing. The sentinel mechanism itself stays (it's a Keep-both —
   `docker exec foreman-daemon touch /foreman/state/shutdown-requested`
   is a useful container-side channel). Invariant after change:
   `Grep -n "TerminateProcess" packages/foreman/` returns no hits.
6. **Follow-up ticket E (optional, dependency on v2-deletion) — "after
   v2 daemon is deleted, also drop `_reconcile_in_flight`".** The v2
   daemon's crash-recovery hook
   (`packages/foreman/src/foreman/daemon.py:79-116`) sweeps
   `foreman:planning` / `foreman:implementing` labels at startup and
   marks tickets `foreman:failed`. This is a v3 carryover that goes
   with the v2 module itself; v2 is already on death row per the
   module's own docstring
   (`packages/foreman/src/foreman/daemon.py:1-11`). Don't ship this
   as a standalone ticket — fold it into whatever existing ticket
   deletes the v2 daemon module wholesale.

## File-level changes

This PR (the docs-only spec PR for issue #141) modifies a single file:

- **Create:** `docs/superpowers/specs/foreman-issue-141-spec.md`
  containing the inventory table below.

The follow-up tickets enumerated in `## Sub-requests` would touch the
following files (listed here as the audit's record, not as edits this
PR performs):

| File | Inventory | Classification | Follow-up |
| --- | --- | --- | --- |
| `packages/foreman/src/foreman/daemon_lock.py:88-108` (`_acquire_exclusive_nonblocking` Windows branch + `_WINDOWS_LOCK_OFFSET`) | Windows-mandatory-lock via `msvcrt.locking` | **Remove** — daemon never runs on Windows post-cutover (design spec line 64-69) | Ticket A |
| `packages/foreman/src/foreman/daemon_lock.py` (module body, POSIX path) | Singleton daemon PID-file lock | **Keep, host-only** — still relevant for direct `foreman daemon v3-start` on a Linux host without compose | — |
| `packages/foreman/tests/test_daemon_lock.py` | Unit tests for the lock module | **Keep, host-only** — exercises the POSIX path that ticket A preserves | — |
| `packages/foreman/src/foreman/cli.py:711-720` (`_run` Windows signal-handler branch) | Windows asyncio signal-handler bridge | **Remove** | Ticket B |
| `packages/foreman/src/foreman/cli.py:881-892` (`daemon_stop` Windows early-return) | Skip `os.kill(SIGTERM)` on Windows | **Remove** | Ticket B |
| `packages/foreman/src/foreman/cli.py:843-848`, `962-966` (tasklist-vs-ps discovery ternaries) | Windows-side process-discovery hint | **Remove** | Ticket B |
| `packages/foreman/src/foreman/cli.py:553-560`, `808-829`, `865-867` (TerminateProcess prose in docstrings) | Justification text only, no branch | **Remove (prose)** | Ticket D |
| `packages/foreman/src/foreman/cli.py:636-640` (`recover_orphaned` call at v3 daemon startup) | Marks orphan `outcome='running'` rows as `errored:recovery` after a crashed-prior-daemon restart | **Keep, both** — neither runtime ships auto-restart today; manual restart is the supported flow and still needs sweep | — |
| `packages/foreman/src/foreman/cli.py:893-926` (POSIX SIGTERM + grace-period polling in `daemon_stop`) | Send SIGTERM to PID from lock file, poll for death | **Keep, host-only** — relevant for direct `foreman daemon v3-start` on a host | — |
| `packages/foreman/src/foreman/cli.py:998-1021` (`daemon_status`) | Lock-file-based status display | **Keep, host-only** — UX for host-mode operators | — |
| `packages/foreman/src/foreman/cli.py:830-996` (sentinel-write paths in `daemon_stop`/`daemon_reload`) | Writes shutdown/reload sentinel files | **Keep, both** — sentinel is a useful `docker exec … touch …` channel inside the container AND the only cross-platform channel for host mode | — |
| `packages/foreman/src/foreman/config.py:151-180` (`ReconcilerConfig.shutdown_sentinel_path` / `reload_sentinel_path` field descriptions) | Config-knob docstrings referencing TerminateProcess | **Keep both (knobs); Remove (Windows prose only)** | Ticket D |
| `packages/foreman/src/foreman/reconciler/daemon.py:209-222` (per-tick sentinel-file check) | Polls the sentinel each tick; sets `_stop_event` when present | **Keep, both** — still the right shutdown signal in-container; comment rationale is the only Windows-specific bit | Ticket D |
| `packages/foreman/src/foreman/reconciler/exec_log.py:213-238` (`recover_orphaned`) | Sweeps orphan running rows on daemon restart | **Keep, both** — both runtimes restart manually after crash today | — |
| `packages/foreman/src/foreman/daemon.py:79-116` (`Daemon._reconcile_in_flight` — v2 only) | Labels in-flight tickets `foreman:failed` on v2 daemon restart | **Remove** (but only when the v2 module is deleted; the module is already marked DEPRECATED at line 1) | Ticket E (folded into existing v2-deletion work) |
| `packages/foreman/src/foreman/worktree.py:95, 280-318` (idempotent base-branch recompute on re-call) | Crash-recovery for impl-worktree base resolution | **Keep, both** — operates on git/origin state, runtime-agnostic | — |
| `packages/foreman/tests/test_cli.py:995-1004`, `1044-1051` (`@skipif sys.platform == "win32"`) | Skips POSIX-only daemon_stop tests on Windows | **Remove** — Windows no longer supported runtime | Ticket C |
| `packages/foreman/tests/test_cli.py:728-740` (Windows-msvcrt fixture branch in `test_daemon_start_handles_unreadable_lock_content_gracefully`) | Reproduces the Windows lock-at-offset-1024 path | **Remove** | Ticket C |
| `packages/foreman/tests/test_cli.py:1223-1231` (TerminateProcess block comment) | Prose-only justification | **Remove** | Ticket C |
| `packages/foreman/tests/reconciler/test_reconciler_e2e.py:235-244` (sentinel mechanism comment) | Prose-only justification | **Remove (prose only)** | Ticket D |

The "speculated" items from the issue body that the inventory did NOT
find:

- **"Retry-on-startup, lock-acquisition-with-backoff paths"** — none
  exist. `DaemonLock.__enter__` calls
  `_acquire_exclusive_nonblocking` once and raises
  `LockAcquisitionError` immediately on contention
  (`packages/foreman/src/foreman/daemon_lock.py:48-67`). The audit
  records this as a not-found.
- **"Reconciler crash-recovery logic that assumed unclean shutdown
  was common"** — the only crash-recovery surface in the v3 reconciler
  is `ExecutionLog.recover_orphaned`, which is small and runtime-
  agnostic. Classified Keep-both above; no overgrown speculative
  logic to delete.

## Alternatives considered

- **One follow-up ticket covering all of Windows-branch removal, not
  five.** Tempting (it's all one motivating principle: "daemon never
  runs on Windows"), and the issue itself uses one sub-list. Rejected
  because the autonomous loop's per-ticket isolation is more useful
  when each ticket is small enough that a single Worker dispatch can
  finish it cleanly. Splitting by file cluster (lock module, CLI,
  tests, prose) means a Reviewer flagging a problem in one cluster
  doesn't block the others.
- **Include the v2 daemon deletion as part of this audit's follow-up
  list.** Rejected — v2 deletion is its own non-trivial sweep
  (storage module, queue module, v2 tests) and is already implicitly
  tracked by the "DEPRECATED" docstring at
  `packages/foreman/src/foreman/daemon.py:1-11`. The audit notes the
  `_reconcile_in_flight` removal would go with v2 deletion (ticket E),
  but does not schedule v2 deletion itself.
- **Recommend dropping `DaemonLock` entirely (not just the Windows
  branch).** Rejected on the basis of the explicit role-contract
  requirement to preserve host-mode invocations: `foreman daemon
  v3-start` on a bare Linux host without compose still needs the
  POSIX file-lock to refuse a second concurrent launch (originally
  foreman#88's contract). Container-mode operators get singleton
  enforcement from `container_name: foreman-daemon`; host-mode
  operators get it from `DaemonLock`.

## Open questions

- **Should the audit also flag `docker-compose.yml`'s missing
  `restart` policy as a defect to fix?** The issue body assumes
  "compose restart policies handle daemon crash recovery", but
  `docker-compose.yml:11-54` does not set one and the design spec
  explicitly chose to omit it for v1
  (`2026-06-05-foreman-docker-runtime-design.md:430-433`: "No
  `restart: unless-stopped` policy in v1 — we want the daemon down
  to mean down"). This audit treats that as a documented design
  choice and leaves `recover_orphaned` classified Keep-both
  accordingly. If a separate Reviewer wants to revisit the v1 choice,
  that's a different ticket from this audit. Setting `confidence:
  medium` overall to reflect that nuance.

## Out of scope

- **Actually deleting any code.** The issue body is explicit: "Out of
  scope for this ticket: actually making the changes." The follow-up
  tickets do the deletes.
- **Auditing role-side crash-recovery code.** `worker.py`'s
  "Worker crashed before outcome" label-revert path
  (`packages/foreman/src/foreman/roles/worker.py:941-953`) is per-role
  recovery inside the dispatched subprocess, not daemon-runtime
  defense. The issue scope is daemon runtime; role recovery is
  separately motivated and not docker-displaceable.
- **Auditing CI workflow files for Windows runners.** The design spec
  already documents this is permanent
  (`2026-06-05-foreman-docker-runtime-design.md:537-547`), PR #114
  already removed the Windows runner, and `.github/workflows/` is not
  in `packages/foreman/`. Out of scope for this audit's predicate.
- **Auditing the `agent-core` package or any other repo.** This
  ticket is scoped to `packages/foreman/` (the issue's "Files"
  section lists `packages/foreman/` paths exclusively).
- **The Docker design itself.** Whether `restart: unless-stopped`
  should be added, whether the container should expose the sentinel
  files for `docker exec` triggers, etc. — all separate from this
  audit.
