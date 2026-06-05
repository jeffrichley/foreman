# Foreman Docker Runtime Design

**Status:** Design — pending review by Jeff + Pepper criterion-check, then implementation plan.
**Date:** 2026-06-05
**Author:** Wren (with Jeff)
**Reviewers:** Jeff, Pepper

---

## Goal

Containerize the foreman daemon so its runtime is fully decoupled from the
Windows host filesystem and `uv run`'s editable-install behavior. The
single load-bearing principle: **the running binary lives in a different
file than the source tree.** Adopting that principle eliminates an entire
class of "the daemon's own executable is file-locked while subprocesses
try to rewrite it" failures, removes Windows-only test flakes from the
CI critical path, and consolidates foreman's runtime surface onto a Linux
target where the rest of our infrastructure already lives.

## Background

This morning the first real autonomous v3 run (foreman daemon dispatching
Planner on agent_core#136) crashed inside 60 seconds, then respawned every
poll cycle for 15 minutes before being killed. Per-dispatch log capture
(foreman#119, shipped earlier today as PR #126) made the root cause
one-look diagnosable from a 366-byte log:

```
error: failed to remove file `E:\...\.venv\Lib\site-packages\../../Scripts/foreman.exe`:
The process cannot access the file because it is being used by another process. (os error 32)
```

**The mechanism:** the v3 reconciler's `dispatch_role` builds argv
`["uv", "run", "foreman", subcommand, ...]`. `uv run` re-syncs the
editable install on every invocation. On Windows, the daemon's own
running `foreman.exe` holds an exclusive file lock that `uv` cannot
satisfy. Every dispatch dies non-zero. The reconciler observes the
`foreman:planning` label is still on the ticket (Planner never
transitioned it — it crashed first), fires `dispatch_planner` again,
crash repeats, label stays, infinite respawn. (Bonus: this also means
yesterday's foreman#117 fix did not bite — the Planner never reached its
commit step.)

This is one specific instance of a broader pattern. Over the last two
weeks the same `uv` ↔ Windows file-lock conflict has surfaced 9-10
distinct times across Jeff's active project surface, including his
research project, Dakupress, and other repos that share the same
`uv run` + editable-install + long-running-daemon shape. The pattern
is now showing up in essentially every project he touches. A
defensive `--no-sync` flag fixes the symptom tactically per
call-site, but does not address the root cause that the source tree
and runtime install share files. Every long-running Python daemon
Jeff ships will hit the same shape until source/runtime are
physically separated.

**The principle behind containerization:** the daemon's runtime
artifact (its installed venv + executable) lives in a sealed image
that is built once and reused. The source tree on the host is the
input to image builds, not the runtime substrate. When you change the
source, you rebuild the image; you do not let a running process mutate
its own installation.

**Secondary win:** Linux containers under WSL2 sidestep every Windows
file-lock anomaly in the daemon path. foreman#98 (Windows CI subprocess
hang) and foreman#99 (Windows Server 2025 accumulated-state pressure)
become irrelevant — we drop Windows from the CI matrix permanently
because the daemon never runs there. Local Windows 11 development on
the source tree stays unchanged; only the daemon moves.

## Scope

**In scope (v1):**

- foreman daemon + dispatched role subprocesses (Planner, Reviewer,
  Worker, Fixer) all run inside one long-running Linux container on
  Jeff's local Windows 11 box (Docker Desktop / WSL2 backend).
- All foreman runtime state (project clones, worktrees, execution log,
  dispatch logs) lives inside Docker-managed named volumes.
- Secrets (GitHub App pem keys + Claude credentials) plumbed via
  Docker Compose secrets, not bind mounts or environment-pasted
  contents.
- Pre-existing host-side workflows (editing foreman source, committing,
  pushing PRs, running pre-push pytest natively on Win11) continue
  unchanged.

**Alternative considered + rejected: WSL2-native daemon (no Docker).**

A cheaper option exists: run the foreman daemon directly inside the
WSL2 Ubuntu shell, no container. That also eliminates the
Windows-native file-lock cluster — the daemon's `foreman` binary
would live at `/home/jeffr/.../foreman/.venv/bin/foreman` on the
Linux ext4 filesystem inside WSL2, and `uv run` would behave like
Linux uv (no Windows file-lock conflict).

We are rejecting this for three reasons:

1. **Source/runtime separation is the principle we want to invest
   in.** WSL2-native still has the daemon editing-and-running from
   the same install. We solve today's bug but not the broader class.
2. **The pre-build clean-check is the discipline we want.** It
   enforces "daemon only runs reviewed code" by refusing dirty
   builds. WSL2-native can't enforce this without bolt-on tooling.
3. **Future-deployment alignment.** A container image is the same
   shape we'll eventually ship to a server. WSL2-native is
   local-only by construction.

Matches Jeff's "do it right not cheap" calibration. Docker costs
more upfront work; in return we get a structural fix that compounds.

**Out of scope (deferred):**

- Production hosting (running the container on a server other than
  Jeff's box). The image structure leaves the door open but no
  deployment pipeline is shipped in v1.
- Multi-machine deployment (running foreman on Jeff's Mac in addition
  to his Windows box).
- Role-per-container isolation (each dispatched role running in its
  own short-lived container). We stay with shared-container dispatch
  for v1.
- MCP server sidecars (Canva, Drive, Hugging Face, etc.). Foreman roles
  don't appear to use them today; if a future role does, we add
  sidecars then.
- Prompt edits to teach roles to use `context7` for library API
  research. Tracked as a separate small task; not part of the
  containerization itself.

## Architecture

**One container, Linux base, daemon + dispatched roles share the
runtime.**

The current `_default_subprocess_runner` shape (a callable that returns
a `_SubprocessLike` wrapper with `pid` + async `wait()`, with optional
`log_path` injection for foreman#119 capture) stays intact. What
changes is _where_ the dispatched subprocess lives and how its argv
resolves to a binary on disk:

- **Today:** `["uv", "run", "foreman", "plan", ...]` runs against the
  editable install at `e:/workspaces/ai/agents/foreman/.venv/Scripts/`
  on Windows NTFS. `uv run` re-syncs the install. The daemon's own
  binary at the same path is file-locked. Crash.
- **In the container:** `["foreman", "plan", ...]` — PATH-resolved
  from the venv at `/app/venv/bin/` which was built into the image
  and is never re-synced at runtime. The daemon's binary and the
  dispatched subprocess's binary are the same file, but nothing tries
  to rewrite it. No lock fight possible.

Per-dispatch output capture (foreman#119) carries over as-is: the log
file at `/foreman/logs/<role>/<issue>__<iso-utc-ts>Z.log` is opened
in append-binary mode by `_default_subprocess_runner` and passed as
the subprocess's stdout (with stderr merged via `subprocess.STDOUT`).
The `_PopenWithLog` wrapper closes the parent-side handle in `wait()`'s
`finally`. Linux happily writes to that file; Windows's reluctance
becomes irrelevant.

Concurrency: the existing `Semaphore(max_concurrent_dispatches)` cap
remains the throttle on how many role subprocesses run at once inside
the container. Default 2 stays unchanged for v1.

**PID 1 / signal forwarding.** Python is not a well-behaved init
process — it doesn't reap exited child PIDs as zombies, and it
doesn't forward signals to descendants by default. A daemon
running long-lived as PID 1 inside a container that spawns 20+
subprocesses per day would accumulate zombies indefinitely and
shrug at `SIGTERM`. The fix is `init: true` in
`docker-compose.yml` for the daemon service — Compose injects
`tini` as PID 1, which reaps zombies and forwards signals to the
daemon process correctly. No code change needed in foreman; this
is purely a Compose flag.

## Image build

**Multi-stage Dockerfile with cache-friendly layer ordering.**

```dockerfile
# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

# System deps — git for clone + worktree ops, curl + ca-certificates
# for HTTPS to github.com + Anthropic. Node + npm because the
# `claude` CLI (which claude_agent_sdk shells out to) is an npm package.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl gnupg \
        nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Install uv (rarely changes — early layer, cached aggressively)
RUN pip install --no-cache-dir uv

# Install Claude Code CLI globally so claude_agent_sdk can shell out
# to it. Pin a known-working channel; bump when intentional.
RUN npm install -g @anthropic-ai/claude-code

# ---- Dependency layer (rare changes → cached) ----
# IMPLEMENTATION NOTE: `uv pip install .` requires a buildable source
# tree, not just metadata. We have two viable patterns here; pick
# during the spike based on what `uv` accepts cleanly:
#   (a) Use `uv export --no-hashes --format requirements-txt` to emit a
#       deps-only requirements file, then `uv pip install -r reqs.txt`
#       in this layer. Deps cached; the actual foreman package install
#       happens entirely in the source layer below.
#   (b) Copy a stub package skeleton alongside the manifest (just a
#       `src/foreman/__init__.py` placeholder) so `uv pip install .`
#       has enough to resolve, then overwrite with real source in the
#       next layer.
# Pattern (a) is cleaner if `uv export` is available; (b) is the
# fallback. Either gives us the cache locality we want.
WORKDIR /tmp/build
COPY packages/foreman/pyproject.toml packages/foreman/uv.lock ./
RUN uv pip install --system --no-cache .

# ---- Source layer (frequent changes → invalidates only this) ----
WORKDIR /app/source
COPY packages/foreman ./
RUN uv pip install --system --no-cache --no-deps .

# ---- Claude Code support files: skills + plugins + MCP config ----
# Plugins: full superpowers (referenced by foreman's vendored prompts).
# Drop other plugins (gstack, etc.) — foreman roles don't invoke them.
# Configs: CLAUDE.md (global instructions) + settings.json.
# Secrets (`.credentials.json`) come at runtime via Compose, NOT here.
COPY docker/claude/plugins/superpowers /root/.claude/plugins/cache/claude-plugins-official/superpowers
COPY docker/claude/skills /root/.claude/skills
COPY docker/claude/CLAUDE.md /root/.claude/CLAUDE.md
COPY docker/claude/settings.json /root/.claude/settings.json
COPY docker/claude/.mcp.json /root/.claude/.mcp.json

# ---- Default config baked in; env vars override per project ----
COPY docker/foreman/config.toml.container /etc/foreman/config.toml

# ---- Entrypoint ----
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]
```

**Layer ordering rationale (foreman#119 caching concern):** edits to
`packages/foreman/src/foreman/**.py` only invalidate the source layer
+ the `--no-deps` install + everything after. With BuildKit's
incremental layer cache, that rebuild is in the 5–15 second range.
Edits to `pyproject.toml` or `uv.lock` invalidate the dep layer too
and take 60+ seconds. Edits to the Dockerfile's `RUN apt-get` line
invalidate everything from there down.

**Pre-build cleanliness check** (host-side, runs before `docker build`):

```bash
# scripts/build-docker.sh
#!/usr/bin/env bash
set -euo pipefail

allow_dirty=false
[[ "${1:-}" == "--allow-dirty" ]] && allow_dirty=true

git fetch origin main --quiet
local_head=$(git rev-parse main)
origin_head=$(git rev-parse origin/main)
if [[ "$local_head" != "$origin_head" ]]; then
    echo "ERROR: local main ($local_head) differs from origin/main ($origin_head)" >&2
    echo "Push your branch and rebase main, or rerun with --allow-dirty." >&2
    [[ "$allow_dirty" == true ]] || exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
    echo "ERROR: working tree dirty — uncommitted changes would land in the image." >&2
    [[ "$allow_dirty" == true ]] || exit 1
fi

docker compose build daemon
```

This enforces "daemon only runs reviewed code" by construction. The
`--allow-dirty` escape hatch is for explicit dev-time experiments;
otherwise the build refuses and tells you why.

**`--allow-dirty` visibility.** When `--allow-dirty` is used, the
build stamps a marker into the image (e.g. an `ALLOW_DIRTY=1` build
arg surfaced as an env var in the running container). The daemon's
startup log line emits the image build SHA plus a loud
`built_with_allow_dirty=true` field when set. This is the
audit-by-loud-comment discipline: dev-iteration escape hatches
should never be invisible.

Once foreman stabilizes enough to no longer warrant the dev-iteration
fast path, we will migrate the Dockerfile to a `git clone` from
`origin/main` instead of a `COPY` from the working tree. Tracked as
future work; not v1.

## File layout (inside the container)

```
/app/                              # daemon runtime (immutable post-build)
  venv/bin/foreman                 # PATH-resolved entrypoint
  source/                          # COPY'd source (for reference + prompts)
    packages/foreman/src/foreman/prompts/...

/foreman/repos/                    # → Docker volume `foreman-repos`
  voice/                           # git clone of jeffrichley/voice
    worktrees/issue-N/             # per-ticket worktrees
  foreman/                         # git clone of jeffrichley/foreman
    worktrees/issue-N/
  agent_core/                      # git clone of jeffrichley/agent_core
    worktrees/issue-N/

/foreman/state/                    # → Docker volume `foreman-state`
  log.sqlite                       # execution log (v3 storage)
  v3-daemon.log                    # daemon's structured JSON-lines log
  reconciler.lock                  # daemon lifetime lock

/foreman/logs/                     # → Docker volume `foreman-logs`
  planner/<issue>__<iso-ts>Z.log   # foreman#119 dispatch capture
  reviewer/<issue>__<iso-ts>Z.log
  worker/<issue>__<iso-ts>Z.log
  fixer/<issue>__<iso-ts>Z.log

/run/secrets/                      # → Compose secrets (tmpfs)
  planner_pem
  reviewer_pem
  fixer_pem
  worker_pem
  claude_credentials               # copied into /root/.claude/ by entrypoint

/etc/foreman/                      # baked-in config
  config.toml                      # container-path defaults, env overrides

/root/.claude/                     # Claude Code config (baked-in mostly,
  CLAUDE.md                        #  credentials at runtime)
  settings.json
  .mcp.json
  .credentials.json                # copied here from /run/secrets at start
  skills/                          # user-scope skills
  plugins/cache/.../superpowers/   # full plugin
```

## Volumes

Three named Docker volumes, all managed by Docker (not visible on the
Windows host filesystem). Inspect via `docker exec foreman-daemon ls
/foreman/...` or `docker cp foreman-daemon:/foreman/... .`.

| Volume          | Mount point     | Purpose                                              |
| --------------- | --------------- | ---------------------------------------------------- |
| `foreman-repos` | `/foreman/repos`| Per-project git clones + worktrees                   |
| `foreman-state` | `/foreman/state`| Execution log SQLite, daemon log, reconciler.lock    |
| `foreman-logs`  | `/foreman/logs` | Per-dispatch subprocess logs (foreman#119)           |

Volumes persist across `docker compose stop/start` AND across
`docker compose down`. Wiped only by `docker volume rm <name>` or
`docker compose down -v`.

**`down -v` guardrail.** `docker compose down` and
`docker compose down -v` differ by one character but the latter
permanently wipes all volume state — every project clone, every
worktree, every dispatch log, the execution log. The build script
and runbook documentation both call this out explicitly: `down -v`
is the explicit-reset form, never run by reflex.

**First-run behavior**: the daemon's startup path clones each project
in `~/.foreman/config.toml` into `/foreman/repos/<project>/` if the
directory doesn't already exist. No manual migration from
`e:/workspaces/ai/agents/<project>/` needed — first run clones fresh
into the volume.

## Secrets

**Docker Compose secrets** — declared in `docker-compose.yml`, sourced
from `~/.foreman/keys/*.pem` and `~/.claude/.credentials.json` on the
host, exposed at `/run/secrets/<name>` inside the container (tmpfs
mount, never persisted to disk, not in the image).

```yaml
# docker-compose.yml (excerpt)
secrets:
  planner_pem:
    file: ${HOME}/.foreman/keys/planner.pem
  reviewer_pem:
    file: ${HOME}/.foreman/keys/reviewer.pem
  fixer_pem:
    file: ${HOME}/.foreman/keys/fixer.pem
  worker_pem:
    file: ${HOME}/.foreman/keys/worker.pem
  claude_credentials:
    file: ${HOME}/.claude/.credentials.json
```

**API tokens** — set via `.env` file at the foreman repo root, loaded
by compose automatically. These are not "secrets" in the Compose sense
(no tmpfs mount needed; they're env vars):

```
# .env (gitignored)
FOREMAN_ADMIN_TOKEN=ghp_...
FOREMAN_PLANNER_APP_ID=3922445
FOREMAN_REVIEWER_APP_ID=3922454
FOREMAN_FIXER_APP_ID=3922458
FOREMAN_WORKER_APP_ID=3922460
FOREMAN_ORCHESTRATOR_APP_ID=3934489
```

The entrypoint script copies `/run/secrets/claude_credentials` to
`/root/.claude/.credentials.json` so `claude_agent_sdk` (and the
`claude` CLI it shells out to) find their expected auth file path.
This is the only mutation the entrypoint performs on the Claude Code
config tree.

**Permission gotcha.** Compose secrets are mounted as mode `0400`
owned by root. If `claude_agent_sdk` or the `claude` CLI needs to
write back to the credentials file (e.g. token refresh), the
entrypoint's `cp` must also `chmod 0600` the destination so the
SDK can write. If reads-only suffices (read-only OAuth token, no
refresh), the default `0400` is fine. Surfaces during spike #1;
the entrypoint accommodates either by defaulting to `0600` on the
copied file.

## Lifecycle

**Foreground / background / restart:**

- Start (background): `docker compose up -d daemon`
- Stop: `docker compose stop daemon`
- Restart after code change: `./scripts/build-docker.sh && docker
  compose up -d daemon` (rebuild + recreate)
- Tail daemon log: `docker compose logs -f daemon`
- Shell into running daemon: `docker exec -it foreman-daemon bash`
- Inspect runtime state: `docker exec foreman-daemon ls /foreman/state/`
  / `cat /foreman/logs/planner/...`

No `restart: unless-stopped` policy in v1 — we want the daemon down
to mean down, not "auto-restarting through your debug session." If
the daemon crashes, it stays down; surfaces a real signal we can
catch via heartbeat / Pepper-ping.

## Logging

**Dual-handler pattern.** The daemon's Python logging is configured
with two handlers in the container:

1. **StreamHandler → stdout.** Picked up by Docker's json-file
   logging driver. Tail via `docker compose logs -f daemon`. Lives
   inside `/var/lib/docker/containers/<id>/<id>-json.log` in the
   WSL2 VM — persists through `compose stop`, lost on `compose down`.

2. **FileHandler → `/foreman/state/v3-daemon.log`.** Same JSON-lines
   format, written to the `foreman-state` volume. Persists through
   `compose down`. Useful for `docker exec foreman-daemon tail -100
   /foreman/state/v3-daemon.log` after a crash.

Per-dispatch subprocess output (foreman#119) is unchanged — still
written to files in `/foreman/logs/<role>/`. That path is in the
`foreman-logs` volume, persists through `compose down`.

## Claude Code / Skills / MCP

The foreman role subprocesses invoke `claude_agent_sdk.query(...)`,
which spawns the `claude` CLI under the hood. The CLI reads its
config from `/root/.claude/` (inside the container) and authenticates
against Anthropic via the OAuth tokens in `.credentials.json`. This
is Jeff's Max plan — no Anthropic API key involved.

**What goes into the image** (baked at build, refreshed on rebuild):

- `~/.claude/CLAUDE.md` → `/root/.claude/CLAUDE.md`
- `~/.claude/settings.json` → `/root/.claude/settings.json`
- `~/.claude/.mcp.json` → `/root/.claude/.mcp.json` (trimmed to only
  the `context7` server — drop Canva, Excalidraw, Drive, Calendar,
  HF, Mermaid)
- `~/.claude/skills/` → `/root/.claude/skills/` (user-scope skills)
- `~/.claude/plugins/cache/claude-plugins-official/superpowers/` →
  `/root/.claude/plugins/cache/claude-plugins-official/superpowers/`
  (full plugin — foreman's vendored superpowers prompts reference
  other superpowers skills by name)

**What does NOT go into the image:**

- `~/.claude/.credentials.json` — runtime-only via Compose secret.
- Other plugins (gstack, etc.) — foreman roles don't invoke them.
- Other MCP servers (Canva, Drive, Calendar, etc.) — foreman roles
  don't use them; the running processes would add container
  complexity for no benefit.
- `~/.claude/history.jsonl`, `~/.claude/cache/`, `~/.claude/channels/`
  — Claude Code session state irrelevant to non-interactive SDK calls.

**Why superpowers is the only plugin we keep:** foreman vendors three
superpowers skills into its own package source
(`packages/foreman/src/foreman/prompts/superpowers/`):
`writing-plans.md`, `executing-plans.md`,
`finishing-a-development-branch.md`. Those vendored prompts
reference other superpowers skills by name
(`superpowers:subagent-driven-development`,
`superpowers:using-git-worktrees`, etc.) which the running role needs
to invoke. Without the full plugin cached, those references break.

## Code changes required in foreman

Minimal — most of the work is configuration. The Python codebase needs
these edits:

1. **`reconciler/v3_host.py::dispatch_role`** — build argv as
   `["foreman", subcommand, ...]` instead of `["uv", "run", "foreman",
   subcommand, ...]`. The `--no-sync` workaround is unnecessary; PATH
   resolves directly to the venv-installed binary.

2. **`reconciler/v3_host.py::V3GitHubHost.__init__`** — `log_dir`
   parameter wired from an env var (`FOREMAN_LOG_DIR`) with a
   sensible default. Container compose sets `FOREMAN_LOG_DIR=/foreman/logs`;
   the host fallback at `Path.home() / ".foreman" / "logs"` keeps
   stand-alone CLI invocations of `foreman daemon v3-start` working
   on the host for ad-hoc debug.

3. **`cli.py` + `config.py`** — config file path resolution becomes
   env-var driven: `FOREMAN_CONFIG_PATH` overrides; default is
   `~/.foreman/config.toml` (host) or `/etc/foreman/config.toml`
   (container). Compose sets `FOREMAN_CONFIG_PATH=/etc/foreman/config.toml`
   on the daemon service. Same pattern as `FOREMAN_LOG_DIR` —
   consistent shape for both, no hidden precedence rules.

4. **`config.py`** — paths in `~/.foreman/config.toml` change to
   container-internal:
   - `local_clone_path = "/foreman/repos/<project>"`
   - `private_key_path = "/run/secrets/<role>_pem"`
   - `database_path = "/foreman/state/log.sqlite"`
   - `lock_path = "/foreman/state/reconciler.lock"`

5. **Logging setup** in the daemon's entry — add the second
   FileHandler writing to `/foreman/state/v3-daemon.log`. Existing
   StreamHandler stays.

6. **`worktree.py`** — clone-on-first-run logic for project repos
   (today the daemon assumes clones already exist; in the container
   we want to clone fresh into `/foreman/repos/<project>/` if absent).

These are surgical changes, not a rewrite.

## CI

**Keep `ubuntu-latest` + native pytest unchanged.** The container is
for the running daemon; CI is for unit + integration tests that
already work fine on Linux runners. Pre-push hook on Jeff's Win11
host stays native (existing pytest invocation). Both gate green
today; neither needs touching.

**Windows CI is removed permanently** as part of this work.
foreman#98 (subprocess hang on Win Server 2025) and foreman#99
(accumulated-state pressure) become irrelevant — the daemon never
runs on Windows. Both tickets get closed referencing this design.

## Migration path

From today's state to containerized state:

1. Land this design doc + a small implementation plan via
   `superpowers:writing-plans`.
2. Implement the Dockerfile + docker-compose.yml + entrypoint +
   build script + the six Python edits above.
3. Verify the build: `./scripts/build-docker.sh` produces a working
   image, `docker compose up -d daemon` starts cleanly, daemon log
   shows successful Anthropic auth + GraphQL observe of registered
   projects.
4. Smoke test: queue a tiny ticket against the dockerized daemon and
   verify the full Planner → Reviewer → spec-PR-merge cycle runs
   without intervention.
5. Update `~/.foreman/config.toml` to the new container paths (or
   keep a host-side copy for local-CLI use; the container loads from
   `/etc/foreman/config.toml`).
6. Ship — merge the implementation PR, run the daemon containerized
   going forward.
7. Close foreman#98 and foreman#99 referencing this design.
8. Update the comment in `.github/workflows/ci.yml` that explains why
   Windows is removed from the CI matrix — PR #114 already removed
   the runner; this design is the permanent rationale ("daemon never
   runs on Windows, so we never test there"). `ubuntu-latest` stays
   as the sole matrix entry.

**Data we lose on cutover (accepted):**

- The pre-cutover SQLite execution log history at
  `~/.foreman/log.sqlite`. Acceptable because v3 is GH-as-truth —
  ticket state reconstructs from GitHub on first tick.
- Per-dispatch log files at `~/.foreman/logs/` (foreman#119). Useful
  for post-mortem of historical runs but not for ongoing work.

**Data we MUST protect before cutover (REAL risk):**

- **In-flight worktree state.** At cutover, any worktree under
  `e:/workspaces/ai/agents/<project>/worktrees/issue-N/` with
  uncommitted edits is lost when the container clones fresh into
  `/foreman/repos/<project>/`. Concretely: a Worker mid-run, a
  Fixer mid-edit, or Wren doing manual recovery on a worktree all
  hold uncommitted changes that the container will never see.

**Pre-cutover ritual (required):**

1. Stop the host-side foreman daemon if running.
2. Sweep each registered project's `worktrees/` directory for
   non-empty `git status` output:
   ```bash
   for p in voice foreman agent_core; do
     find "e:/workspaces/ai/agents/$p/worktrees" -maxdepth 2 -name '.git' \
       -execdir bash -c 'echo "=== $(pwd) ==="; git status --short' \;
   done
   ```
3. For each dirty worktree: either commit + push it, OR explicitly
   stash + record (so Wren can replay manually after cutover).
4. Cutover only when the sweep returns empty.

This ritual happens once, at cutover. Day-to-day the container
manages its own worktrees inside the `foreman-repos` volume; the
host-side scattered worktrees won't be touched again post-cutover.

**Host-side foreman source tree stays where it is** at
`e:/workspaces/ai/agents/foreman/`. Jeff edits, commits, pushes
exactly as today. Pre-push pytest stays native. The only difference
is that "run the daemon" goes from `foreman daemon v3-start` to
`docker compose up -d daemon` (with a build step if source changed).

## Spikes during implementation

These are unknowns we verify by doing, not by deciding up-front:

1. **Claude SDK auth with only `.credentials.json`** — does
   `claude_agent_sdk.query()` work with ONLY the credentials file
   exposed at `/root/.claude/.credentials.json`, plus the baked-in
   `settings.json` / `CLAUDE.md` / `.mcp.json`? Or does it need
   additional state from `~/.claude/cache/` or `~/.claude/channels/`?
   First boot of the container tells us. If it complains, we either
   add what it needs to the image or generate a stub.

2. **Which skills foreman roles actually invoke** — instrument
   `claude_agent_sdk` calls to log every `Skill` tool invocation +
   every MCP tool call made during a v3 cycle. After a few real runs
   we have data; use it to prune the image footprint. Belt of "bake
   in all of superpowers" suspenders for v1; data drives the diet for
   v2.

3. **Build-time penalty in practice** — measure: how long is a clean
   build of `python:3.12-slim` + apt deps + uv + claude-code + foreman
   on Jeff's box? How long is an incremental build after a single
   `.py` edit? Tune Dockerfile layer ordering if reality diverges
   from our 5-15s incremental estimate.

4. **WSL2 volume performance** — measure git clone + git worktree-add
   throughput inside the container against Docker volumes. If
   measurably worse than native, investigate (probably WSL2 distro
   storage driver). v1 ships with Docker volumes regardless; tuning
   comes later.

## Risks

- **Docker Desktop dependency.** If Docker Desktop is down or stuck
  (which it occasionally is on WSL2), foreman daemon is down. Today
  it depends on uv + Python + Windows working; new dependency on
  Docker Desktop's WSL2 backend working. Net-net more reliable
  given the bug class we're eliminating, but a new failure surface.

- **Image rebuild discipline.** "Edit source → forget to rebuild →
  daemon runs stale code" is a real failure mode. Mitigated by the
  pre-build clean-check in `scripts/build-docker.sh` (refuses to
  build if working tree diverges from origin/main) + a hint in the
  daemon's startup log printing the build SHA so Jeff can verify
  what's actually running.

- **Compose secrets rotation.** If a pem key is rotated on the host,
  the container needs `docker compose down && up -d` to pick up the
  new file. Compose secrets are read at container start, not live.
  Document the rotation flow.

- **Per-dispatch log volume growth.** Per foreman#119 we don't
  currently age out dispatch logs. Growing forever inside a Docker
  volume is functionally identical to growing forever on host disk
  (it's the same bytes). The aging-out task remains a future ticket
  — not introduced by this design, not solved by it.

- **Docker `json-file` log driver growth.** Docker's default
  logging driver does not rotate. The daemon's stdout (captured by
  Docker) grows forever inside the WSL2 VM until manually pruned.
  Mitigated by configuring per-service log rotation in
  `docker-compose.yml`:
  ```yaml
  logging:
    driver: "json-file"
    options:
      max-size: "10m"
      max-file: "5"
  ```
  Caps the daemon's stdout log at ~50MB rolling, which is plenty
  for a v3 reconciler's structured logging cadence.

## Acceptance criteria

- [ ] `./scripts/build-docker.sh` builds a `foreman:dev` image from
      the local source tree, refusing to build if working tree is
      dirty or diverges from origin/main (unless `--allow-dirty`).
- [ ] `docker compose up -d daemon` starts the daemon successfully:
      lock file created, GraphQL observer authenticates, first
      reconciler tick logs cleanly.
- [ ] A real Planner dispatch against a registered project completes
      end-to-end (LLM call succeeds via Max auth, spec doc committed,
      branch pushed, PR opened, label transitioned) without any
      manual intervention.
- [ ] The dispatched subprocess's stdout + stderr land in
      `/foreman/logs/<role>/<issue>__<ts>Z.log` (foreman#119 carryover),
      and the path is recorded in the execution log's `details`
      column.
- [ ] `docker compose stop daemon && docker compose up -d daemon`
      cycles cleanly: prior project clones, execution log, and
      dispatch logs all still present.
- [ ] `docker compose down -v` (the explicit reset) wipes everything,
      next `up -d` clones fresh.
- [ ] Pre-push pytest on Jeff's Win11 host still runs native and
      green (no change to that flow).
- [ ] foreman#98 and foreman#99 closed with a reference to this
      design as the permanent resolution.
- [ ] Pepper criterion-check completed before implementation PR
      merges.
- [ ] Pre-cutover worktree-sweep ritual completed for every
      registered project before the container takes over.
- [ ] Daemon startup log emits image build SHA and
      `built_with_allow_dirty` flag (loud-audit on the
      escape-hatch path).
- [ ] PID 1 / `tini`: confirm `init: true` in `docker-compose.yml`
      and verify no zombie processes accumulate after 100+
      dispatch cycles.
- [ ] Docker `json-file` log rotation configured
      (`max-size: 10m`, `max-file: 5`).

---

## Open questions for review

- **Compose project name.** `foreman` reads naturally, but if Jeff
  runs other compose stacks named `foreman` it conflicts. Suggest
  `foreman-daemon` as the compose project name (independent of the
  service name `daemon` and image name `foreman:dev`).
- **`/foreman/state/` vs `/var/lib/foreman/`** — the latter is more
  conventional for Linux service state. `/foreman/state/` mirrors the
  existing `~/.foreman/` mental model and keeps the path short.
  Personal preference; happy to flip if there's a reason.
- **Image registry.** v1 builds locally only — no `docker push` step.
  Future production hosting will add a registry; not deciding now.
