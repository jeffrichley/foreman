# Foreman v3 Cutover Runbook

> Operator-facing guide for switching from foreman v2 (pipelines-as-state)
> to v3 (GitHub-as-state + execution log). Read alongside
> `docs/superpowers/specs/foreman-issue-106-spec.md`.

## Pre-cutover state

As of 2026-06-03 the v2 daemon is already stopped + its sqlite database
archived. The cutover work below assumes this baseline. If `~/.foreman/`
shows a live `daemon.lock` or `foreman.sqlite`, the v2 cleanup hasn't
happened yet — see "Re-running v2 cleanup" at the bottom.

Expected `~/.foreman/` contents:
- `config.toml` — foreman config (unchanged)
- `foreman-v2-archive-2026-06-03.sqlite` — archived v2 db
- `daemon.log` — historical log; v3 writes a new file
- (no `daemon.lock`, no `daemon.pid`, no `foreman.sqlite`)

## Pre-flight gates (must pass before flip)

1. **All v3 unit + integration tests green** locally:

   ```bash
   uv run pytest packages/foreman/tests/reconciler -v
   uv run pytest packages/foreman/tests/test_v3_bus_endpoint.py -v
   uv run pytest packages/foreman/tests/test_cli_v3.py -v
   ```

2. **Full pytest baseline** unchanged or higher:

   ```bash
   uv run pytest packages/foreman -q
   ```

3. **CLI smoke test** (wires Config + Reconciler without running ticks):

   ```bash
   uv run foreman daemon v3-start --max-ticks 0 --dry-run
   ```

   Expected: prints "v3-start wired: N projects, db=…, dry_run=True" and
   exits 0.

## Cutover procedure

### Step 1: Deploy v3 in dry-run mode

```bash
uv run foreman daemon v3-start --dry-run
```

Let it run for ~6 polls (≈6 minutes at the 60s default cadence). The
reconciler will:
- Fetch GH state for every registered project
- Evaluate rules per ticket
- Write intended actions to `~/.foreman/reconciler.sqlite` with
  `outcome='dry_run'` (NO host calls; no labels added, no PRs merged)

Inspect the dry-run output:

```bash
sqlite3 ~/.foreman/reconciler.sqlite "
SELECT ts, ticket_id, action, outcome, details
FROM execution_log
ORDER BY id DESC
LIMIT 30
"
```

Gut-check: do the intended actions make sense for today's stuck tickets?
For foreman#143 specifically you should see one row roughly like:

```
2026-06-03T20:??:??Z | jeffrichley/foreman#143 | advance_label_to_plan_approved | dry_run | {…}
```

If something looks wrong, **stop here**. File an issue with the
unexpected action + the GH state that triggered it. Do not flip to
executing.

### Step 2: Flip to executing mode

Stop the dry-run daemon (Ctrl-C). Start fresh:

```bash
uv run foreman daemon v3-start
```

(No `--dry-run` flag = execute mode.)

### Step 3: Tight observation (24-48h)

The first day post-flip is the human-in-the-loop window. Wren is on
stream watching:

- `tail -f ~/.foreman/daemon.log` per cycle
- `sqlite3 ~/.foreman/reconciler.sqlite "SELECT * FROM execution_log ORDER BY id DESC LIMIT 10"` after each action
- Per-action: "is this the right action for this ticket's GH state?"

If v3 misbehaves: stop the daemon and run rollback.

## Rollback (escape hatch)

```bash
# 1. Stop v3
pkill -f "foreman daemon v3-start"
# (Use Stop-Process -Name foreman on Windows.)

# 2. Restore v2 db
mv ~/.foreman/foreman-v2-archive-2026-06-03.sqlite ~/.foreman/foreman.sqlite

# 3. Restart v2
uv run foreman daemon start
```

Tickets that progressed during v3's brief reign may need manual recovery
on the v2 side (re-set the right `foreman:*` label by hand). This is the
escape hatch, not a routine.

## Re-running v2 cleanup

If `~/.foreman/foreman.sqlite` still exists (v2 cleanup hasn't been done):

```bash
# 1. Stop any v2 daemon
pkill -f "foreman daemon start"

# 2. Archive db
mv ~/.foreman/foreman.sqlite ~/.foreman/foreman-v2-archive-$(date +%Y-%m-%d).sqlite

# 3. Remove stale runtime files
rm -f ~/.foreman/daemon.lock ~/.foreman/daemon.pid
```

Then return to "Pre-flight gates" above.

## Removing v2 from the codebase

This is a separate follow-up PR, blocked on v3 running stable for ~2
weeks post-cutover. Tracked in a new issue at that time. Don't remove v2
code as part of the cutover itself.
