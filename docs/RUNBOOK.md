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
   wmic process where "name='python.exe'" get processid,commandline | findstr "foreman daemon v3-start"
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
docker exec foreman-daemon cat /foreman/state/v3-daemon.log | tail -50
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
   docker run --rm -v foreman-state:/state alpine cat /state/v3-daemon.log | tail -30
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

## Import-graph boundaries (`import-linter`)

foreman uses [`import-linter`](https://import-linter.readthedocs.io) as a
CI gate to enforce architectural boundaries that aren't expressible at the
AST/type-checker level. Config lives at `[tool.importlinter]` in the
workspace-root `pyproject.toml`. Decision 7 of the
2026-06-11 architecture stability plan owns the rule-source discipline.

Run locally:

```bash
just import-linter                            # gate (matches CI)
PYTHONPATH=packages/foreman uv run --no-sync lint-imports   # direct
```

Failure output shape: import-linter prints `Contracts: N kept / M broken`.
For a broken contract, the report names the contract, the source module
that violated it, and the forbidden import chain. Example:

```
R1: production code does not import from tests BROKEN

foreman is not allowed to import tests:
-   foreman.roles.worker -> tests.conftest (l.70)
```

Decode: a production module under `foreman.*` gained an import from the
test tree. Either move the helper into `foreman/*` proper so it isn't a
test-tree dependency, or remove the import. The historical motivating bug
is foreman#19 (test-fixture pollution surfaced at pre-push).

How to add a new rule: see
`docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md`
Decision 7 § How to add a rule.

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
