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

> **Historical.** This documents a one-time past cutover. The SQLite
> stores it references (`reconciler.sqlite`, `foreman.sqlite`) are no
> longer the live store — v4/v5 has since moved to a Postgres sidecar
> (`foreman-postgres`); SQLite was removed entirely. Read this section
> only for the migration history, not for current state layout.

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
docker compose up -d daemon
```

The recommended flow on a healthy setup is `docker compose up -d
daemon` alone — Watchtower will pull the latest
`ghcr.io/jeffrichley/foreman:dev` image from GHCR within 2 minutes
of the next merge to main. See "Image lifecycle (auto-rebuild)"
below for the full loop. Use `just rebuild-daemon` only for offline
dev (no GHCR reachability) or when testing uncommitted changes
that haven't been merged yet.

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

## Image lifecycle (auto-rebuild)

The daemon image is dev-rolling: every merge to `main` rebuilds
`ghcr.io/jeffrichley/foreman:dev` and a Watchtower sidecar on the host
pulls + recreates the daemon container automatically. Operators never
pin versions; they always run latest. See
[foreman#363](https://github.com/jeffrichley/foreman/issues/363) for
the motivating failure (a stale container shipping a known-buggy
Worker against real tickets) and the full design.

### One-shot operator setup

The first `image.yml` workflow push creates a **private** package on
GHCR (GitHub's default for the org owner's namespace). For Watchtower
to pull without an auth token, flip the package to public exactly once:

1. Open
   https://github.com/jeffrichley/foreman/pkgs/container/foreman
2. Click "Package settings" (right sidebar).
3. Scroll to "Change visibility" → "Change visibility".
4. Select "Public" and confirm.

This is safe: the image contains no secrets. Credentials flow in at
runtime via Compose secrets (`/run/secrets/*`, tmpfs-mounted), and
the `IMAGE_SHA` / `ALLOW_DIRTY` build-args stamped into the image
are not sensitive.

### The auto-update loop

```
merge to main
  └─→ image.yml workflow (CI) builds + pushes to GHCR  (~2-3 min)
        ├─→ ghcr.io/jeffrichley/foreman:dev               (rolling pointer)
        └─→ ghcr.io/jeffrichley/foreman:sha-<short>       (immutable)
              └─→ foreman-watchtower polls every 2 min
                    └─→ pulls new digest, recreates foreman-daemon (~30s)
```

Typical wall-clock from merge to running daemon: ~5 minutes. The
`:dev` tag is the rolling pointer Watchtower follows; the
`:sha-<short>` tag is the immutable handle for "which exact commit
is this container running?" (use it to pin a temporary rollback —
see below).

### Verify the running image is up-to-date

```bash
docker exec foreman-daemon foreman doctor
# Expected on a fresh container:
# [doctor] image-fresh: OK — running sha-<X> matches main
```

`foreman doctor` exit codes are scripting-friendly: 0 on OK / SKIPPED
/ WARN (transient network blip), 1 only on confirmed-stale. Use it in
`&&`-chained scripts without false alarms.

### Manual override (offline dev OR Watchtower / GHCR outage)

```bash
cd e:/workspaces/ai/agents/foreman
just rebuild-daemon
```

`just rebuild-daemon` calls `./scripts/build-docker.sh` (local clean-
tree build) then `docker compose up -d daemon` (recreate the
container). Use this whenever GHCR is unreachable, Watchtower is
down, or you need to test an uncommitted change without going through
PR + merge.

### Tail the Watchtower log

```bash
docker compose logs -f watchtower
```

Structured JSON-lines output (matches the daemon's log convention).
Look for `"msg":"Found new"` lines for pull events and
`"msg":"Updated"` lines for successful container recreations.

### Pin a specific image temporarily (rollback)

```bash
# Pin to a known-good immutable sha tag:
# Edit docker-compose.yml: image: ghcr.io/jeffrichley/foreman:sha-<X>
docker compose up -d daemon

# Restore by reverting docker-compose.yml back to:
#   image: ghcr.io/jeffrichley/foreman:dev
docker compose up -d daemon
```

Watchtower will not stomp the pinned tag (it only updates `:dev`),
so a sha-pinned container stays put until the operator reverts.

### Tuning the poll interval

Edit `WATCHTOWER_POLL_INTERVAL` in the `watchtower:` service block of
`docker-compose.yml`. Operators on a fast iteration loop can drop to
60s; operators who care about GHCR rate-limit quota can raise to 300s.
The 120s default is the trade-off the foreman#363 spec settled on.

---

## Backups and restoration

The daemon-internal SQLite snapshot scheduler (issue #360) and the
`foreman restore` command were **removed** with the SQLite kill. They
snapshotted `/foreman/state/foreman.sqlite`, which is no longer the
authoritative store, so they no longer apply.

Production state now lives in the `foreman-postgres` sidecar
(`postgres:16-alpine`), with its data on the `foreman-pg-data` named
volume.

### Current DR gap (read this)

There is **no Postgres snapshot mechanism yet**. The `foreman-pg-data`
volume survives `docker compose stop` and `docker compose down`, but is
**wiped by `docker compose down -v`**. With no backup in place, a
`down -v` against this volume is irrecoverable — treat it as a
destructive, point-of-no-return command.

The replacement Postgres DR backup is tracked as
[foreman#434](https://github.com/jeffrichley/foreman/issues/434) and is
not yet built. Until it lands, the only protection is operator
discipline around `down -v`.

---

## Provider transient failures and backoff suspension

Foreman classifies Anthropic-side transport blips (5xx, 429, connection
refused, transport-level timeout) as `TRANSIENT_PROVIDER_ERROR` outcomes
distinct from genuine role failures (foreman#361). The state machine
exempts these from the runaway-defense `max_state_attempts` cap and
schedules a delayed retry on an exponential-backoff schedule. A short
Anthropic blip therefore does NOT escalate the ticket to `NeedsHelp`
by the wrong path.

### What `next_action_at` in `foreman show <id>` means

When a ticket hits a transient provider error, the state machine writes
an ISO-8601 timestamp into the ticket's `next_action_at` column. The
Poller refuses to enqueue the ticket until wall-clock time has passed
that timestamp. The header line of `foreman show <id>` renders this
suspension as:

```
[yellow]suspended until 2026-06-20T14:25:00+00:00 (provider-throttled, attempt 2/4)[/yellow]
```

The `attempt N/4` count is the number of transient outcomes already
observed; `4` is the schedule length (after which the next transient
escalates to NeedsHelp).

### The backoff schedule

Delays in seconds, indexed by prior-attempt count:

| Prior attempts | Delay  |
|----------------|--------|
| 0              | 30s    |
| 1              | 2m     |
| 2              | 10m    |
| 3              | 30m    |
| 4              | escalate to NeedsHelp |

Cumulative wall-clock window before escalation: ~42 minutes 30 seconds.

### How to verify "this is Anthropic, not us"

The structured log emits one `transient_provider_error` line per
observed transient. From the daemon container:

```bash
docker run --rm -v foreman-logs:/logs alpine \
    grep '"event": "transient_provider_error"' /logs/transitions.jsonl | tail -20
```

Each line carries:
- `attempt` — the prior-attempt count at that moment
- `next_retry_at` — when the suspension lifts (or `null` if escalating)
- `provider_status` — the verbatim cause string from the SDK
  (e.g. `"Claude Code returned an error result: 503 Service Unavailable"`)

If you see a sustained run of these lines with rising `attempt` values
across multiple tickets in the same minute, Anthropic itself is
degraded. Check https://status.anthropic.com/.

### Operator override

To bypass the suspension immediately (e.g. to confirm Anthropic has
recovered):

```bash
foreman retry <ticket-id>
```

`cmd_retry` clears `next_action_at` before enqueuing the WorkItem. The
CLI prints a `(cleared next_action_at)` parenthetical when a suspension
was active.

### When escalation to NeedsHelp legitimately means "Anthropic is out"

After 4 transient attempts (~40 min cumulative), the next transient
escalates the ticket to NeedsHelp via the same path as any other
NeedsHelp landing — except the `transient_provider_error` log line at
that moment carries `next_retry_at: null` and `attempt: 4`. The
operator playbook on this:

1. Check Anthropic status (above).
2. If Anthropic is healthy: investigate the `provider_status` cause
   string. A `429` that persists ~40 minutes is usually a quota issue
   on the foreman App's API key — rotate or wait.
3. If Anthropic is degraded: leave the ticket parked; once Anthropic
   recovers, `foreman retry <ticket-id>` resumes from the last state
   without re-running prior work.
4. If degraded for hours: switch the affected projects to manual mode
   (`foreman hold <ticket-id> --reason "anthropic outage 2026-06-20"`).

---

## Operator-visible escalation comments

Issue #367 wired the autonomous loop's operator-significant state
transitions to the GitHub issue's comment stream. When a ticket
escalates to `NeedsHelp`, lands on `Failed`, or sits BLOCKED on the
same async signal for ≥15 minutes, foreman now posts a comment to
the originating issue so the operator who opens the issue page sees
the context without spelunking daemon logs.

### Comment sources (the `source=...` marker)

Every escalation comment is wrapped in HTML markers carrying a
dedup key. The `source` field identifies WHICH surface posted:

| `source` | When fires |
|---|---|
| `role:planner` | Planner LLM returned `confidence: low`. Why the spec is under-specified. |
| `role:reviewer-spec_pr` | Reviewer-on-spec returned `confidence: low`. Why the verdict is uncertain. |
| `role:reviewer-impl_pr` | Reviewer-on-impl returned `confidence: low`. |
| `role:fixer-spec_pr` | Fixer-on-spec returned `outcome: incomplete` or `confidence: low`. What rejection it received. |
| `role:fixer-impl_pr` | Fixer-on-impl returned `outcome: incomplete` or `confidence: low`. |
| `role:worker` | Worker returned `outcome: incomplete` or `outcome: spec_invalid`. Why it could not finish. |
| `fixer-received-rejection` | Pre-dispatch: Fixer is about to act on the Reviewer's rejection. |
| `sustained-blocked` | A ticket has been BLOCKED ≥15 minutes on the same async signal (e.g., CI not completing). |
| `terminal-landing` | Subprocess crash / TIMEOUT / retry-cap-trip; the role-side path could not post. Includes exit code + log tail inline. |

### Reading the marker for triage

Each comment opens with a hidden HTML comment carrying the source +
dedup key:

```
<!-- foreman:escalation:begin ticket=<owner>/<repo>#<N>:source=<source>:key=<key> -->
**[role] · [outcome_label] · [iso8601 timestamp]**
...
<!-- foreman:escalation:end -->
```

Operators triaging a stuck ticket should:

1. Scan the comment stream for the most recent `source=...` marker
   to identify which surface raised the escalation.
2. Read the `## Why` section for the structured cause.
3. On `terminal-landing` comments, expand the `<details>` fold to
   see the role subprocess's exit code and the last 500 chars of
   its log. The named log file under
   `<log_dir>/<role>/<ticket_id>__<iso>.log` carries the full
   stderr if more context is needed.

### The 15-minute sustained-BLOCKED threshold

A ticket polling on the same BLOCKED reason for 15 minutes triggers
exactly one comment per `(ticket, reason)` pair. Subsequent ticks on
the same signal do NOT re-post. When the underlying signal changes
(e.g., CI moves from "pending" to "mergeable but conflicted"), the
new reason gets its own comment. The threshold is a module-level
constant in `foreman.v4.observers.sustained_blocked`
(`SUSTAINED_BLOCKED_THRESHOLD`); tests override it via the
observer's constructor.

### Re-dispatch via `foreman:retry`

The standard re-dispatch verb is the `foreman:retry` label. Every
escalation comment's footer names it explicitly. Applying the label
clears the suspension (`next_action_at`) and re-enqueues the ticket
through the v4 Poller.

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

## Operator identities (DCO sign-off + supervision attribution)

Issue #347 added a required top-level `[operator]` block to
`V4Config` carrying two operator-identity sub-tables — one for the
human who actively orchestrated the run, and one for the human who
legally attests the DCO sign-off. The daemon refuses to boot
without it. Every role-bot commit (Planner spec doc, Worker impl,
Fixer on either target) carries BOTH a `Supervised-by:` and a
`Signed-off-by:` trailer in the commit body.

### Schema

```toml
[operator.supervisor]
name = "Wren Richley"
email = "wren@example.com"

[operator.signer]
name = "Jeff Richley"
email = "jeff@example.com"
```

In the common single-operator case the same person fills both
roles — the schema does not enforce uniqueness; set both blocks
to the same values.

### Per-project override

A `[[projects]]` block MAY override either identity independently
via `[[projects.operator.supervisor]]` and/or
`[[projects.operator.signer]]`. Unset fields inherit from the
top-level block.

```toml
[[projects]]
name = "external-project"
repo = "other-org/external-project"
local_clone_path = "/foreman/repos/external-project"

  [[projects.operator.signer]]
  name = "External Maintainer"
  email = "ext@example.com"
```

The resolver in `foreman.v4.config.resolve_operator` returns a
fresh `OperatorConfig` per project with both identities resolved
independently — project-side `supervisor` (if set) plus top-level
`signer`, etc.

### Environment variables

The container template
(`docker/foreman/config.toml.template`) consumes four envsubst
placeholders at container start:

- `FOREMAN_OPERATOR_SUPERVISOR_NAME`
- `FOREMAN_OPERATOR_SUPERVISOR_EMAIL`
- `FOREMAN_OPERATOR_SIGNER_NAME`
- `FOREMAN_OPERATOR_SIGNER_EMAIL`

Set them in the same `.env` that carries
`FOREMAN_PLANNER_APP_ID` etc. The same env-var quadruple is also
exported into the Worker / Fixer LLM subprocess environment so
the LLM's `<provenance_trailers>` prompt instruction can splice
the trailers via `--trailer "Supervised-by: $FOREMAN_OPERATOR_SUPERVISOR_NAME <$FOREMAN_OPERATOR_SUPERVISOR_EMAIL>"`
etc.

### Trailer policy

Every Foreman bot commit emits up to four trailers:

| Trailer | Source | DCO-enforced? |
|---|---|---|
| `Co-Authored-By: foreman-<role>[bot] <...>` | role-bot identity | no |
| `Co-Authored-By: <model> <noreply@<provider>>` | model attribution | no |
| `Supervised-by: <name> <<email>>` | resolved operator supervisor | no |
| `Signed-off-by: <name> <<email>>` | resolved operator signer | yes |

Only `Signed-off-by:` is enforced by the DCO CI gate (validated
in non-blocking mode in PR #346). The other three are recommended
but not required.

### Rationale

- See [PR #346](https://github.com/jeffrichley/foreman/pull/346) —
  the DCO CI test PR that validated the gate works.
- See the [2026-06-19T17:17:36 @wrenrichley comment on issue
  #347](https://github.com/jeffrichley/foreman/issues/347) — the
  authoritative direction for the two-identity / two-trailer
  shape this section documents.
- See the [Linux kernel coding-assistants policy](https://docs.kernel.org/process/coding-assistants.html)
  — the canonical AI-disclosure-via-trailer pattern Foreman is
  adopting.

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
