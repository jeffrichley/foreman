# Foreman Daemon Operations Runbook

Operational reference for the containerized foreman daemon.
Design context: see `docs/superpowers/specs/2026-06-05-foreman-docker-runtime-design.md`.

---

## Pre-cutover ritual (one-time, before first container start)

The container's `foreman-repos` volume starts empty and the daemon
clones each registered project fresh on first run. Any uncommitted
work in host-side worktrees at `e:/workspaces/ai/agents/<project>/worktrees/`
is **invisible to the container** and effectively orphaned.

Before cutover:

1. **Stop the host-side daemon** (if running):
   ```powershell
   foreman daemon stop
   # OR if the lock file is stale, find + kill manually:
   wmic process where "name='python.exe'" get processid,commandline | findstr "foreman daemon start"
   ```

2. **Sweep every registered project's worktrees for dirty trees**:
   ```bash
   for p in voice foreman agent_core; do
     for wt in "e:/workspaces/ai/agents/$p/worktrees"/*/; do
       [[ -d "$wt/.git" ]] || continue
       status=$(cd "$wt" && git status --short)
       if [[ -n "$status" ]]; then
         echo "DIRTY: $wt"
         echo "$status"
       fi
     done
   done
   ```

3. **For each dirty worktree**, either:
   - Commit + push the changes, OR
   - `git stash` and record the stash in a notebook so Wren can replay
     it manually post-cutover.

4. **Re-run the sweep** until it returns empty. Only then proceed to
   `docker compose up -d daemon`.

This ritual happens once. Day-to-day, the container manages its own
worktrees inside the `foreman-repos` volume; the host's scattered
worktrees won't be touched again post-cutover.

---

## v3 → v4 substrate cutover (one-shot, 2026-06-19)

The Phase 9 PR replaces the v3 reconciler-rules state engine with the
v4 state-machine + EventBus + observers substrate. There is no in-place
migration of in-flight tickets — the v3 and v4 SQLite schemas are
disjoint, the v4 Poller does NOT reconstruct ticket state from GitHub
labels, and any ticket mid-pipeline at cutover will be **silently
ignored** by the v4 daemon unless operator-rescued.

What the v4 Poller actually does on startup (`foreman.v4.poller`):

- `_adopt_new_tickets()` queries GitHub for issues bearing the single
  configured `trigger_label` (default `foreman:plan`) and creates fresh
  v4 SQLite rows for them in Planning state.
- `_enqueue_open_tickets()` re-enqueues anything already in the v4
  SQLite — but the v4 SQLite is **empty at first boot** because it's a
  brand-new schema file.

That means: a ticket past Planning at cutover (the Observer removed its
`foreman:plan` label on the first state transition per Phase 8d.8)
will not have `foreman:plan` to re-trigger Planning, and no v4 SQLite
row exists yet, so the daemon will not touch it.

### Pre-cutover: inventory + manual rescue plan

For every project the daemon manages, list each open issue that has any
`foreman:*` label and decide a per-ticket disposition BEFORE rebuilding:

```bash
# repeat per project; use the Wren PAT if hitting GitHub from a script
gh issue list --repo jeffrichley/<project> \
  --label foreman:plan,foreman:planning,foreman:plan-review,foreman:spec-fix,foreman:implementing,foreman:impl-review,foreman:impl-fix,foreman:merging,foreman:needs-help \
  --state open --json number,title,labels
```

For each ticket, pick one disposition:

| Current state | Disposition |
|---|---|
| Just submitted (`foreman:plan` only) | Leave it. v4's Poller will adopt it on first poll cycle. |
| Mid-pipeline with PR open | Either let the human finish it and merge by hand, OR strip all `foreman:*` labels + re-apply `foreman:plan` post-cutover to restart from Planning. |
| `foreman:needs-help` | Leave the label; manually `foreman reset <ticket-id>` post-cutover to wipe + re-queue. |
| Already merged spec but no impl PR yet | Strip `foreman:*` labels + re-apply `foreman:plan` post-cutover. |

There is no "preserve mid-flight progress" path. The conservative move
for tickets the operator wants to keep is to let the v3 daemon finish
them before cutover — but the v3 substrate is dying with this PR, so
"finish them" means human-driving each through the remaining steps.

### Cutover sequence

1. **Stop the running v3 container** (preserves volumes — important):
   ```bash
   docker compose stop foreman-daemon
   ```

2. **Verify no host-side daemon is running.** A rogue host daemon
   competes with the container for the same GitHub repos. PowerShell
   one-liner to confirm absence:
   ```powershell
   Get-CimInstance Win32_Process | Where-Object {
     $_.CommandLine -match 'foreman daemon start' -and
     $_.CommandLine -notmatch 'foreman-repos'
   } | Measure-Object | Select-Object -ExpandProperty Count
   # Must print 0.
   ```
   If non-zero, find the parent (usually a stray ``uv run foreman
   daemon start`` from a Bash invocation) and `Stop-Process -Id <pid>
   -Force`.

3. **Drain the v3 state volume** (optional but recommended — fresh v4
   SQLite is cheaper than coexistence-debugging). With the v3 container
   stopped:
   ```bash
   docker run --rm -v foreman-state:/state alpine sh -c \
     'rm -f /state/reconciler.sqlite /state/v3-daemon.log'
   ```
   This leaves the volume intact but clears v3-specific files. v4
   creates its own `foreman.sqlite` and writes a rendered `config.toml`
   into the same volume on first boot.

4. **Rebuild the container image against the v4 code:**
   ```bash
   ./scripts/build-docker.sh
   ```

5. **Start the v4 container daemon:**
   ```bash
   docker compose up -d foreman-daemon
   ```

6. **Verify it boots cleanly** — the JSONL log should show one
   `container_start` record naming `foreman_v4_config`, then the
   daemon's own `state_entered`/`tick` records:
   ```bash
   docker compose logs -f foreman-daemon | head -40
   docker exec foreman-daemon foreman daemon status
   docker exec foreman-daemon foreman ps
   docker exec foreman-daemon cat /foreman/logs/transitions.jsonl | head -10
   ```

7. **Apply the per-ticket disposition plan** from the pre-cutover
   inventory:
   ```bash
   # Re-trigger from Planning on tickets that need it:
   gh issue edit <N> --repo <owner>/<repo> \
     --remove-label foreman:planning,foreman:spec-fix,... \
     --add-label foreman:plan

   # Wipe + re-queue stuck tickets:
   docker exec foreman-daemon foreman reset <project>#<issue>
   ```

If the v4 daemon's boot fails, the rollback is to redeploy the previous
image: `git checkout pre-phase-9-cutover` (the tag marks the last
v3-compatible commit) then `./scripts/build-docker.sh && docker compose
up -d foreman-daemon`. The state volume keeps both `reconciler.sqlite`
(v3, untouched until step 3) and `foreman.sqlite` (v4, fresh) so
rollback restores v3 behavior cleanly.

---

## Daily operations

### Start daemon

```bash
cd e:/workspaces/ai/agents/foreman
./scripts/build-docker.sh        # rebuild if source changed
docker compose up -d daemon
```

For a dev iteration that needs uncommitted source changes:

```bash
./scripts/build-docker.sh --allow-dirty
docker compose up -d daemon
```

The daemon's startup log line will shout `allow_dirty=true` so you
know an escape-hatch build is running.

### Tail the structured log

```bash
docker compose logs -f daemon
```

### Inspect persistent state

```bash
docker exec foreman-daemon ls /foreman/state
docker exec foreman-daemon cat /foreman/logs/transitions.jsonl | tail -50
docker exec foreman-daemon ls /foreman/logs/planner
```

### Stop daemon

```bash
docker compose stop daemon          # preserves containers + volumes
docker compose down                 # removes container; volumes survive
docker compose down -v              # DESTRUCTIVE: wipes all foreman state
```

`docker compose down -v` differs from `down` by one character. Run
deliberately, never reflexively.

### Rotate Claude credentials

When you renew `~/.claude/.credentials.json` on the host:

```bash
docker compose down
docker compose up -d daemon
```

Compose secrets are read at container start, not live. A `restart`
alone won't pick up the new file.

### Refresh the vendored Claude config

When you intentionally want the container to pick up a new version of
superpowers or an updated `docker/claude/CLAUDE.md`:

```bash
./scripts/refresh-claude-vendor.sh   # writes into docker/claude/
git diff docker/claude/               # review changes
git add docker/claude/ && git commit -m "chore(docker): refresh claude vendor — <what+why>"
./scripts/build-docker.sh             # rebuild image with new vendor
docker compose down && docker compose up -d daemon
```

The refresh script never touches `docker/claude/CLAUDE.md` (the daemon
contract — hand-maintained) or `docker/claude/.mcp.json` (Linux-form,
not Windows-cmd-wrapped). User-scope `~/.claude/skills/` is also
intentionally skipped.

---

## Recovery: daemon won't start

1. Check the daemon-log file directly (no need for the container to be
   alive — the named volume persists):
   ```bash
   docker run --rm -v foreman-logs:/logs alpine cat /logs/transitions.jsonl | tail -30
   ```
2. Check container exit reason:
   ```bash
   docker compose ps -a daemon
   docker inspect foreman-daemon --format '{{json .State}}' | jq
   ```
3. If volumes are corrupted (e.g., partial clone), drop them with
   `docker compose down -v` and re-run the pre-cutover ritual.

---

## Pre-commit hooks (one-time setup per clone)

The repo uses the `pre-commit` framework (config: `.pre-commit-config.yaml`).
Two hooks are configured:

- **gitleaks** (at commit time) — scans staged content for secrets:
  PEM bodies, ghp_-prefixed PATs, anthropic OAuth tokens, etc.
- **just check** (at push time) — runs the same pytest + lint gate
  CI runs. Mirrors what was previously in `.githooks/pre-push`.

One-time install per fresh clone:

```bash
# If you previously had `core.hooksPath = .githooks` set, unset it
# first so the framework can install to .git/hooks/ where git looks
# by default. Without this, `pre-commit install` refuses with
# "cowardly refusing to install hooks with `core.hooksPath` set."
git config --unset core.hooksPath || true

uv run pre-commit install --hook-type pre-commit --hook-type pre-push
```

After that, hooks run automatically. Manual invocation:

```bash
uv run pre-commit run gitleaks --all-files    # full-repo secret scan
uv run pre-commit run --all-files             # run every configured hook
```

If gitleaks blocks a commit:

1. Read the finding — what file, what line, what rule fired
2. If it's a real secret, **rotate it immediately** (the PAT/key is
   compromised even though the commit was blocked — the value was
   typed/pasted into a working tree)
3. Remove the secret from the staged file and re-commit
4. If it's a false positive, add the pattern to `.gitleaks.toml`
   (not yet present) in a separate commit

**Never** pass `--no-verify` to bypass gitleaks. The hook exists
because operator error is the dominant secret-leak vector.

---

## What survives what

| Action                         | Container | foreman-repos | foreman-state | foreman-logs | image |
| ------------------------------ | --------- | ------------- | ------------- | ------------ | ----- |
| `docker compose stop daemon`   | killed    | survives      | survives      | survives     | survives |
| `docker compose down`          | removed   | survives      | survives      | survives     | survives |
| `docker compose down -v`       | removed   | **WIPED**     | **WIPED**     | **WIPED**    | survives |
| `docker rmi foreman:dev`       | (none)    | survives      | survives      | survives     | removed  |

The image is reproducible from any commit-SHA via `scripts/build-docker.sh`.
Volume contents are NOT reproducible (they hold cloned project state +
SQLite + logs from real runs). Treat `down -v` as the destructive
command it is.
