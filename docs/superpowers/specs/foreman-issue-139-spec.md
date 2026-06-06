# Spec: point reconciler sqlite at the `/foreman/state/` named volume (issue #139)

## Goal

The v3 reconciler's `ExecutionLog` defaults `reconciler.db_path` to
`~/.foreman/reconciler.sqlite` (`packages/foreman/src/foreman/config.py:117-120`).
Inside the container, `~/` resolves to `/root/`, which is on the
container's ephemeral root filesystem — NOT on the `foreman-state` named
volume that compose mounts at `/foreman/state/`
(`docker-compose.yml:43-46`). Every `docker compose down` / restart loses
the execution log, which the reconciler reads via `has_unterminated()`
and `count_completed()` to decide whether to re-dispatch in-flight roles.
This spec overrides `reconciler.db_path` in the container config so the
sqlite file lives on the persistent named volume and survives container
restarts. Addresses issue #139.

## Acceptance criteria

- `docker/foreman/config.toml.container` declares
  `db_path = "/foreman/state/reconciler.sqlite"` inside its existing
  `[reconciler]` table (currently sitting at lines 17-19 with the
  `auto_merge_*` keys).
- Parsing the container TOML via `tomllib.load(...)` and passing the
  result to `foreman.config.Config.model_validate(...)` produces a
  `Config` whose `reconciler.db_path` equals
  `/foreman/state/reconciler.sqlite` (no `~`, no `${HOME}`, no
  expansion needed).
- A new regression test under `packages/foreman/tests/` pins the above
  invariant by loading the literal `docker/foreman/config.toml.container`
  file and asserting `Config.reconciler.db_path` resolves under
  `/foreman/state/`. The test fails today and passes after the change.
- No other key in the container config is added, removed, or modified.
  The bug is narrowly scoped to `reconciler.db_path`; locks and
  sentinels are out of scope (see `## Out of scope`).
- `just check` passes.

## Approach

The container already mounts a Docker named volume `foreman-state` at
`/foreman/state/` (`docker-compose.yml:45`) and the Dockerfile pre-creates
that mount point (`Dockerfile:92`). The other v3 state file that needed
to land there — `v3-daemon.log` — already does, because
`daemon_v3_start` resolves its path through `resolve_state_dir()`
(`packages/foreman/src/foreman/cli.py:581-582`) which reads
`FOREMAN_STATE_DIR=/foreman/state` from the compose env
(`.env.example:22`).

The reconciler sqlite path, however, goes through the config-file route
rather than the env-var route: `daemon_v3_start` reads
`config.reconciler.db_path`, expands `~`, and hands it to
`ExecutionLog` (`packages/foreman/src/foreman/cli.py:632-634`). The
container config file (`docker/foreman/config.toml.container`) does not
currently override that key, so the default `~/.foreman/reconciler.sqlite`
applies — which is on the ephemeral root filesystem inside the
container, NOT on the named volume.

The fix is a single key addition to the existing `[reconciler]` table
in `docker/foreman/config.toml.container`:

```toml
[reconciler]
db_path = "/foreman/state/reconciler.sqlite"
auto_merge_spec = true
auto_merge_impl = false
```

This is the minimum change that satisfies the issue's acceptance
criterion. It matches the project's convention of using absolute,
container-internal paths in `config.toml.container` (see the
`/run/secrets/...` private-key paths and the `/foreman/repos/...` clone
paths already in that file).

A small regression test goes in
`packages/foreman/tests/test_container_config.py` (a new file, since no
existing test loads the literal `config.toml.container`). It:

1. Resolves the path to the container config TOML relative to the repo
   root (parents from `__file__`).
2. Loads it with `tomllib.load` and validates with
   `Config.model_validate`.
3. Asserts `cfg.reconciler.db_path == "/foreman/state/reconciler.sqlite"`.

This pattern matches the style of `test_config.py`, which already
imports `Config, ProjectConfig, ReconcilerConfig, load_config` from
`foreman.config` (`packages/foreman/tests/test_config.py:10`). The test
is cheap, isolated, and prevents the bug from silently regressing if
someone edits the container config file again.

## Sub-requests (topologically sorted)

1. Add the new regression test
   `packages/foreman/tests/test_container_config.py` that loads
   `docker/foreman/config.toml.container` via `tomllib`, validates with
   `Config.model_validate`, and asserts
   `cfg.reconciler.db_path == "/foreman/state/reconciler.sqlite"`.
   Run it to confirm it fails on the current container TOML (no
   override present).

2. Add the `db_path` line to the existing `[reconciler]` table in
   `docker/foreman/config.toml.container`. Place it as the first key
   inside the table (above `auto_merge_spec`) so the persistence
   override is the first thing a reader sees in the section.

3. Re-run the new test; confirm it now passes. Run `just check` to
   confirm the full quality gate is green.

## File-level changes

| Path | Change |
| --- | --- |
| `docker/foreman/config.toml.container` | Add `db_path = "/foreman/state/reconciler.sqlite"` as the first key inside the existing `[reconciler]` table. No other keys touched. |
| `packages/foreman/tests/test_container_config.py` | NEW. One test that loads the container TOML, validates it as a `Config`, and asserts `reconciler.db_path` points at `/foreman/state/reconciler.sqlite`. Pins the invariant. |

No source code under `packages/foreman/src/` changes. The default in
`ReconcilerConfig.db_path` stays at `~/.foreman/reconciler.sqlite`
because that is the correct default for ad-hoc host invocation of
`foreman daemon v3-start` (without container env / mounts). The
container override lives in the container's config file, which is the
correct seam.

## Alternatives considered

- **Change the default in `ReconcilerConfig.db_path` to use
  `FOREMAN_STATE_DIR`/`resolve_state_dir()`.** Ruled out: that path
  resolution model belongs to env-var-driven resolution (used by
  `v3-daemon.log`), not to TOML-config-driven resolution. Mixing the
  two is what created the inconsistency that hid the bug; doubling
  down on env-var resolution for one specific config key would make
  the model harder to reason about. The cleanest fix uses the same
  config-file override pattern the container TOML already uses for
  `auto_merge_spec`, `auto_merge_impl`, and the `/run/secrets/...`
  private-key paths.
- **Bind-mount `/root/.foreman/` to a named volume instead of putting
  state under `/foreman/state/`.** Ruled out: contradicts the design
  spec at `docs/superpowers/specs/2026-06-05-foreman-docker-runtime-design.md`
  ("`/foreman/state/` vs `/var/lib/foreman/`" — `/foreman/state/`
  mirrors the chosen container layout) and would create a second
  state-persistence location, fragmenting where operators look during
  incident response (`docker exec foreman-daemon ls /foreman/state`
  per `docs/RUNBOOK.md:81`).
- **Also override `reconciler.lock_path`, `reconciler.shutdown_sentinel_path`,
  `reconciler.reload_sentinel_path`, `daemon.lock_path`,
  `daemon.sqlite_path`, and `daemon.log_path` in the container TOML.**
  Ruled out for THIS spec: the issue's acceptance criterion names only
  `reconciler.db_path`, and the other paths fall into different
  categories — PID locks and CLI-to-daemon sentinels are ephemeral by
  design (new lock per daemon process, sentinel exists only between a
  `daemon stop`/`reload` invocation and the next reconciler tick), and
  the v2 `daemon.*_path` keys are not exercised by the v3 daemon
  entrypoint. A broader audit of "every container path that
  optimistically resolves under `~/`" is worth doing, but doing it
  here would inflate scope past what the bug demands and delay the
  state-loss fix. Captured in `## Out of scope` for a follow-up.
- **Do nothing; document that operators must `docker exec` to copy the
  sqlite before `docker compose down`.** Ruled out: the issue
  documents that this bug ALREADY caused real divergence between the
  reconciler's execution-log state and GitHub label state during
  tonight's cutover. The cost of the fix is one TOML line; the cost of
  not fixing it is silent loss of in-flight dispatch state on every
  restart.

## Open questions

(none)

## Out of scope

- Overriding `reconciler.lock_path`, `reconciler.shutdown_sentinel_path`,
  or `reconciler.reload_sentinel_path` in the container TOML. These are
  ephemeral signaling/PID-lock files that do not need to survive
  restarts; auditing them is its own ticket.
- Overriding `daemon.lock_path`, `daemon.sqlite_path`, or
  `daemon.log_path` (the v2 daemon's state paths). The v3 daemon does
  not exercise the v2 sqlite/log paths at runtime; touching them here
  would muddle the diff.
- Changing the default value of `ReconcilerConfig.db_path` in
  `packages/foreman/src/foreman/config.py`. The default is correct for
  host-side ad-hoc invocation; only the container override needs to
  change.
- Migrating an existing `reconciler.sqlite` from the ephemeral root
  filesystem to the named volume on first start after the fix. The
  whole point of the bug is that prior-restart data is already lost;
  there is nothing to migrate. The new sqlite gets created in place.
- Adding a `foreman-state` volume health-check or backup tooling.
- Refactoring `cli.py:582`'s env-var-driven `v3_log_path` resolution
  to go through `ReconcilerConfig` instead. That's a consistency
  cleanup tracked separately from this state-loss fix.
- Any change to `docker-compose.yml`, `Dockerfile`, `.env.example`, or
  `docker/entrypoint.sh`. The named volume and mount point already
  exist; this fix is purely a TOML config edit (+ regression test).
