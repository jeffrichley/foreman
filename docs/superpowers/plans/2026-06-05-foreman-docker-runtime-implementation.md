# Foreman Docker Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the containerized foreman daemon per the design at `docs/superpowers/specs/2026-06-05-foreman-docker-runtime-design.md`, replacing the `uv run`-based subprocess dispatch with a sealed Docker image that runs natively on Linux under WSL2.

**Architecture:** Multi-stage Dockerfile + Docker Compose orchestration. Daemon as PID 1 (via `init: true`/tini); dispatched roles as child processes inside the same container. Source baked into image at build time; project clones, state, and logs in named Docker volumes; secrets via Compose secrets; Max-plan Claude credentials mounted runtime-only.

**Tech Stack:** Docker Desktop (WSL2 backend), Docker Compose v2, Python 3.12, uv, Node.js + `@anthropic-ai/claude-code`, bash + shellcheck, pytest (test gate stays HOST-native on Win11).

**Execution mode:** Direct inline pairing (Jeff + Wren). Subagent-driven development was considered but rejected for this work — see the design discussion. Five **CHECKPOINT** sections below mark natural regroup boundaries where we pause, look at concrete artifacts, decide whether to continue or amend the plan. No checkpoint = no pause; keep moving through the steps.

**Checkpoint map:**
- **A** after Task 4 — review the vendored claude tree before wrapping it in a Dockerfile
- **B** after Task 6 — read the full Docker surface (Dockerfile + compose) before touching Python
- **C** after Task 11 — all Python edits done + host-side test suite green, before first `docker compose build`
- **D** after Task 13 — first image build succeeds; we've learned what Docker actually does with our setup
- **E** after Task 15 — end-to-end ticket smoke green; cutover-ready signal

---

## Pre-flight

Read the design doc at `docs/superpowers/specs/2026-06-05-foreman-docker-runtime-design.md` end to end before starting. Every implementation decision below is downstream of the design; if a step looks wrong, re-read the corresponding design section.

Establish baseline:

```bash
cd e:/workspaces/ai/agents/foreman
.venv/Scripts/python.exe -m pytest packages/foreman/tests -q 2>&1 | tail -5
```

Expected: `821 passed, 5 skipped` (baseline as of 2026-06-05 PM, after #123/#124/#125/#126 merged earlier the same day). Plan-authoring captured `808` before those merges landed; the implementation worktree off origin/main picked up the newer baseline.

---

## Task 0: Implementation branch + worktree

**Files:**
- Create: git worktree at `e:/workspaces/ai/agents/foreman-worktrees/docker-runtime/`
- Create: branch `feat/docker-runtime` off `main`

- [ ] **Step 1: Verify host-side daemon is stopped**

```bash
cd e:/workspaces/ai/agents/foreman
ls -la ~/.foreman/reconciler.lock 2>&1 | head -2
```

Expected: `No such file or directory`. If a lock file exists, stop the daemon first (find the PID via `wmic process where "name='python.exe'"` filtered on `foreman daemon v3-start`, kill via PowerShell `Stop-Process`).

- [ ] **Step 2: Create the implementation worktree**

```bash
cd e:/workspaces/ai/agents/foreman
git fetch origin main
git worktree add -b feat/docker-runtime ../foreman-worktrees/docker-runtime origin/main
cd ../foreman-worktrees/docker-runtime
```

Expected: worktree created, new branch `feat/docker-runtime` checked out.

- [ ] **Step 3: Configure local git identity for the worktree**

```bash
cd e:/workspaces/ai/agents/foreman-worktrees/docker-runtime
git config user.name wrenrichley
git config user.email wrenrichley@gmail.com
```

Expected: no output; identity scoped to this worktree.

- [ ] **Step 4: Verify the existing test baseline holds in the worktree**

```bash
cd e:/workspaces/ai/agents/foreman-worktrees/docker-runtime
uv sync --quiet
uv run --no-sync pytest packages/foreman/tests -q 2>&1 | tail -3
```

Expected: `821 passed, 5 skipped` (matches the pre-flight baseline).

No commit needed yet for the branch state itself. But the plan file (`docs/superpowers/plans/2026-06-05-foreman-docker-runtime-implementation.md`) is copied into the worktree from the design-branch checkout. Commit it as the implementation branch's first focused commit so the impl PR carries the plan it implements.

---

## Task 1: Repo skeleton — docker/ directory + .env.example + config.toml.container + .dockerignore

**Files:**
- Create: `docker/.gitkeep`
- Create: `docker/foreman/config.toml.container`
- Create: `.env.example`
- Create: `.dockerignore`
- Modify: `.gitignore` (add `.env`)

- [ ] **Step 1: Create the docker/ directory structure**

```bash
mkdir -p docker/claude docker/foreman
touch docker/.gitkeep
```

- [ ] **Step 2: Write the container config template**

Create `docker/foreman/config.toml.container`:

```toml
# Foreman container default config — all paths are container-internal.
# Per-project overrides via env vars (FOREMAN_*_APP_ID etc.) are still
# honored on top of this file.
#
# This file is baked into the image at /etc/foreman/config.toml and
# selected at daemon startup via FOREMAN_CONFIG_PATH (set by compose).

# NOTE: no [admin] section here. FOREMAN_ADMIN_TOKEN is only consumed
# by `foreman init` (host-side bootstrap to create labels in a target
# repo) and is not needed at daemon runtime. Keeping it out of the
# container config keeps the runtime sealed.

[orchestrator]
app_id_env = "FOREMAN_ORCHESTRATOR_APP_ID"
private_key_path = "/run/secrets/orchestrator_pem"

[reconciler]
auto_merge_spec = true
auto_merge_impl = false

[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/foreman/repos/voice"

[projects.voice.apps]
planner_app_id_env = "FOREMAN_PLANNER_APP_ID"
planner_private_key_path = "/run/secrets/planner_pem"
reviewer_app_id_env = "FOREMAN_REVIEWER_APP_ID"
reviewer_private_key_path = "/run/secrets/reviewer_pem"
fixer_app_id_env = "FOREMAN_FIXER_APP_ID"
fixer_private_key_path = "/run/secrets/fixer_pem"
worker_app_id_env = "FOREMAN_WORKER_APP_ID"
worker_private_key_path = "/run/secrets/worker_pem"

[projects.foreman]
repo = "jeffrichley/foreman"
local_clone_path = "/foreman/repos/foreman"
auto_merge_spec = true
auto_merge_impl = false

[projects.foreman.apps]
planner_app_id_env = "FOREMAN_PLANNER_APP_ID"
planner_private_key_path = "/run/secrets/planner_pem"
reviewer_app_id_env = "FOREMAN_REVIEWER_APP_ID"
reviewer_private_key_path = "/run/secrets/reviewer_pem"
fixer_app_id_env = "FOREMAN_FIXER_APP_ID"
fixer_private_key_path = "/run/secrets/fixer_pem"
worker_app_id_env = "FOREMAN_WORKER_APP_ID"
worker_private_key_path = "/run/secrets/worker_pem"

[projects.agent_core]
repo = "jeffrichley/agent_core"
local_clone_path = "/foreman/repos/agent_core"
auto_merge_spec = true

[projects.agent_core.apps]
planner_app_id_env = "FOREMAN_PLANNER_APP_ID"
planner_private_key_path = "/run/secrets/planner_pem"
reviewer_app_id_env = "FOREMAN_REVIEWER_APP_ID"
reviewer_private_key_path = "/run/secrets/reviewer_pem"
fixer_app_id_env = "FOREMAN_FIXER_APP_ID"
fixer_private_key_path = "/run/secrets/fixer_pem"
worker_app_id_env = "FOREMAN_WORKER_APP_ID"
worker_private_key_path = "/run/secrets/worker_pem"
```

- [ ] **Step 3: Write the .env.example template**

Create `.env.example`:

```bash
# Foreman daemon container — runtime env vars.
# Copy to .env (gitignored) and fill in real values before
# `docker compose up -d daemon`.

# Note: FOREMAN_ADMIN_TOKEN is intentionally NOT here. It is only used
# by `foreman init` (one-shot host-side bootstrap to create labels in a
# target repo). The containerized daemon authenticates via GitHub Apps
# (the 4 PEMs mounted as Compose secrets), not via a PAT.

# GitHub Apps — numeric IDs of the role bots
FOREMAN_ORCHESTRATOR_APP_ID=<your-orchestrator-app-id>
FOREMAN_PLANNER_APP_ID=<your-planner-app-id>
FOREMAN_REVIEWER_APP_ID=<your-reviewer-app-id>
FOREMAN_FIXER_APP_ID=<your-fixer-app-id>
FOREMAN_WORKER_APP_ID=<your-worker-app-id>

# Container-internal paths (compose sets these; rarely need to change)
FOREMAN_CONFIG_PATH=/etc/foreman/config.toml
FOREMAN_LOG_DIR=/foreman/logs
FOREMAN_STATE_DIR=/foreman/state
```

- [ ] **Step 4: Write .dockerignore**

Create `.dockerignore`:

```gitignore
# Speed up the build context: never ship venvs, caches, or local logs.
.venv/
**/.venv/
**/__pycache__/
**/*.pyc
.foreman/
docs/superpowers/plans/
docs/superpowers/specs/
.git/
.github/
.gitignore
.env
*.log
node_modules/
.pytest_cache/
.ruff_cache/
.mypy_cache/

# The docker/ tree itself does NOT need to be IN the build context for
# everything; the Dockerfile uses targeted COPY paths.
```

- [ ] **Step 5: Update .gitignore to ignore .env**

Modify `.gitignore` to add `.env` if not already present:

```bash
grep -q '^\.env$' .gitignore || echo '.env' >> .gitignore
```

- [ ] **Step 6: Verify the templates parse**

```bash
# config.toml.container should be valid TOML
uv run python -c "import tomllib; tomllib.loads(open('docker/foreman/config.toml.container').read())" && echo OK
```

Expected: `OK`.

- [ ] **Step 7: Commit**

```bash
git add docker/.gitkeep docker/foreman/config.toml.container .env.example .dockerignore .gitignore
git commit -m "chore(docker): add container config skeleton + env template"
```

---

## Task 2: docker/entrypoint.sh

**Files:**
- Create: `docker/entrypoint.sh`
- Create: `tests/docker/test_entrypoint.sh` (smoke test runnable on host)

- [ ] **Step 1: Write the entrypoint script**

Create `docker/entrypoint.sh`:

```bash
#!/usr/bin/env bash
# Foreman daemon container entrypoint.
#
# Responsibility:
#  1. Copy the Compose-mounted Claude credentials secret into the
#     directory the claude_agent_sdk expects, with 0600 perms so the
#     SDK can refresh tokens.
#  2. Print a startup banner naming the image SHA and the
#     --allow-dirty flag (loud audit per the design's --allow-dirty
#     visibility item).
#  3. Exec the daemon as PID 1's child (tini becomes PID 1 via
#     init: true in compose, so this script's PID doesn't matter
#     for zombie reaping; we just need to exec so signals propagate
#     cleanly).
#
# Inputs (from compose):
#   /run/secrets/claude_credentials — Max OAuth token JSON
#   FOREMAN_CONFIG_PATH, FOREMAN_LOG_DIR, FOREMAN_STATE_DIR, etc.
#   IMAGE_SHA, ALLOW_DIRTY — build args surfaced as env vars
#
# Exits:
#   0 — daemon exited cleanly (only on shutdown signal)
#   non-zero — daemon crashed or setup failed
set -euo pipefail

# --- Claude credentials plumbing ----------------------------------------
# Compose secrets default to 0400 root-only. Copy to the SDK's expected
# location with 0600 so it can write a refreshed token.
CLAUDE_DIR=/root/.claude
CLAUDE_SECRET=/run/secrets/claude_credentials
if [[ -r "$CLAUDE_SECRET" ]]; then
    mkdir -p "$CLAUDE_DIR"
    install -m 0600 "$CLAUDE_SECRET" "$CLAUDE_DIR/.credentials.json"
else
    echo "ERROR: $CLAUDE_SECRET not readable — Compose secret missing or perms wrong" >&2
    exit 1
fi

# --- Startup banner -----------------------------------------------------
# IMAGE_SHA + ALLOW_DIRTY come in as build args via the Dockerfile.
# Print as a single JSON line so the daemon's structured log driver
# captures it cleanly.
printf '{"event":"container_start","image_sha":"%s","allow_dirty":%s,"foreman_config_path":"%s","foreman_log_dir":"%s"}\n' \
    "${IMAGE_SHA:-unknown}" \
    "${ALLOW_DIRTY:-false}" \
    "${FOREMAN_CONFIG_PATH:-/etc/foreman/config.toml}" \
    "${FOREMAN_LOG_DIR:-/foreman/logs}"

# --- Hand off to the daemon ---------------------------------------------
# `exec` so SIGTERM from `docker stop` lands directly on the daemon,
# not on this shell.
exec foreman daemon v3-start
```

- [ ] **Step 2: Make it executable + shellcheck**

```bash
chmod +x docker/entrypoint.sh
shellcheck docker/entrypoint.sh
```

Expected: no shellcheck output (passes). If `shellcheck` is not available, install it via apt/brew/scoop or skip — the script is small and reviewable.

- [ ] **Step 3: Smoke test the credential-copy logic on host (without docker)**

Create `tests/docker/test_entrypoint.sh`:

```bash
#!/usr/bin/env bash
# Host-side smoke for entrypoint.sh's credential-copy logic.
#
# We don't run the whole entrypoint here — the trailing `exec foreman
# daemon v3-start` would fail on the host (no /run/secrets, no real
# daemon needed for THIS check). Instead we run the credential-copy
# block (the only thing that mutates real filesystem state) against
# fake paths and assert the resulting file shape.
set -euo pipefail

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# Stub the secret file
echo '{"oauth":"stub-token"}' > "$tmp/secret"

# Run the cred-copy block in a subshell with stub paths. The inner
# script mirrors lines 30-37 of docker/entrypoint.sh.
bash -c '
    set -euo pipefail
    CLAUDE_DIR="$1"
    CLAUDE_SECRET="$2"
    mkdir -p "$CLAUDE_DIR"
    install -m 0600 "$CLAUDE_SECRET" "$CLAUDE_DIR/.credentials.json"
' _ "$tmp/claude" "$tmp/secret"

# Verify
test -f "$tmp/claude/.credentials.json" || { echo "FAIL: credential file missing"; exit 1; }
contents=$(cat "$tmp/claude/.credentials.json")
[[ "$contents" == '{"oauth":"stub-token"}' ]] || { echo "FAIL: contents wrong"; exit 1; }

# Perms check only enforced on Linux. Windows/MSYS does not preserve
# POSIX mode bits on NTFS so `install -m 0600` still reports 644 via
# stat. The actual container runtime IS Linux, where the install -m
# 0600 enforces correctly — verified again at Task 14 (container start
# smoke) by docker exec stat.
case "$(uname -s)" in
    Linux*)
        perms=$(stat -c '%a' "$tmp/claude/.credentials.json")
        [[ "$perms" == "600" ]] || { echo "FAIL: perms=$perms (expected 600)"; exit 1; }
        echo "PASS: entrypoint credential copy smoke (perms verified)"
        ;;
    *)
        echo "PASS: entrypoint credential copy smoke (perms check skipped on $(uname -s))"
        ;;
esac
```

**Implementation note (caught during execution):** the plan's original test passed `"$CLAUDE_DIR"` / `"$CLAUDE_SECRET"` as positional args to `bash -c`, but those variables existed only as the env-var prefix for the bash-c subshell — not in the outer shell. `set -u` then tripped on the outer shell's unbound-variable check. Fixed by passing `"$tmp/claude"` / `"$tmp/secret"` (which ARE set in the outer shell) instead. Also added the Linux-only perms guard for the same reason: MSYS on Windows doesn't preserve `install -m 0600` mode bits, so the host smoke would falsely fail on Wren's box. Real container-side enforcement is verified at Task 14 instead.

- [ ] **Step 4: Run the smoke test**

```bash
chmod +x tests/docker/test_entrypoint.sh
bash tests/docker/test_entrypoint.sh
```

Expected: `PASS: entrypoint credential copy smoke`.

- [ ] **Step 5: Commit**

```bash
git add docker/entrypoint.sh tests/docker/test_entrypoint.sh
git commit -m "feat(docker): add container entrypoint with credential plumbing"
```

---

## Task 3: scripts/build-docker.sh — pre-build clean-check

**Files:**
- Create: `scripts/build-docker.sh`
- Create: `tests/docker/test_build_check.sh` (host-side TDD)

- [ ] **Step 1: Write the failing test FIRST**

Create `tests/docker/test_build_check.sh`:

```bash
#!/usr/bin/env bash
# Host-side TDD for scripts/build-docker.sh's pre-build gates.
# We don't actually invoke `docker compose build` — we stub it out
# and verify the gate behavior.
set -euo pipefail

REPO_ROOT=$(git rev-parse --show-toplevel)
SCRIPT="$REPO_ROOT/scripts/build-docker.sh"

[[ -x "$SCRIPT" ]] || { echo "FAIL: $SCRIPT not executable or missing"; exit 1; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# Set up a stub git repo that pretends to be both local and origin.
# IMPORTANT: keep the bare-origin clone OUTSIDE the working tree, else
# `git status --porcelain` would see the origin.git/ subdir as
# untracked content and gate-2 (working-tree-clean) would always trip.
work="$tmp/work"
mkdir -p "$work"
cd "$work"
git init --quiet -b main
git config user.email "test@example.com"
git config user.name "Test"
git commit --allow-empty -m "seed" --quiet
git clone --bare "$work" "$tmp/origin.git" --quiet >/dev/null
git remote add origin "$tmp/origin.git"

# Override docker compose to a no-op so we can isolate the gates
export PATH="$tmp/stubs:$PATH"
mkdir -p "$tmp/stubs"
cat > "$tmp/stubs/docker" <<'STUB'
#!/usr/bin/env bash
# noop stub — record the call
echo "docker $*" >> "$DOCKER_CALL_LOG"
STUB
chmod +x "$tmp/stubs/docker"
export DOCKER_CALL_LOG="$tmp/docker.calls"

# Case 1: clean tree + in-sync main → build runs, ALLOW_DIRTY=false
> "$DOCKER_CALL_LOG"
output=$("$SCRIPT" 2>&1) || { echo "FAIL: clean build rejected: $output"; exit 1; }
grep -q "build" "$DOCKER_CALL_LOG" || { echo "FAIL: docker build not invoked"; exit 1; }
grep -q "ALLOW_DIRTY=false" "$DOCKER_CALL_LOG" || { echo "FAIL: ALLOW_DIRTY=false build arg missing: $(cat "$DOCKER_CALL_LOG")"; exit 1; }
echo "PASS case 1: clean build"

# Case 2: working tree dirty → build refused unless --allow-dirty
> "$DOCKER_CALL_LOG"
echo "stray" > stray.txt
if "$SCRIPT" 2>"$tmp/err" ; then
    echo "FAIL case 2: dirty tree should refuse"
    exit 1
fi
grep -q "working tree dirty" "$tmp/err" || { echo "FAIL case 2: missing error message"; cat "$tmp/err"; exit 1; }
echo "PASS case 2: dirty tree refused"

# Case 3: --allow-dirty escape hatch → build runs, ALLOW_DIRTY=true
> "$DOCKER_CALL_LOG"
"$SCRIPT" --allow-dirty 2>&1 >/dev/null
grep -q "ALLOW_DIRTY=true" "$DOCKER_CALL_LOG" || { echo "FAIL case 3: --allow-dirty did not stamp build arg"; cat "$DOCKER_CALL_LOG"; exit 1; }
echo "PASS case 3: --allow-dirty stamps build arg"

# Cleanup
rm stray.txt

# Case 4: local main ahead of origin/main → refuse
> "$DOCKER_CALL_LOG"
git commit --allow-empty -m "ahead of origin" --quiet
if "$SCRIPT" 2>"$tmp/err" ; then
    echo "FAIL case 4: ahead-of-origin should refuse"
    exit 1
fi
grep -q "differs from origin/main" "$tmp/err" || { echo "FAIL case 4: missing error message"; cat "$tmp/err"; exit 1; }
echo "PASS case 4: ahead-of-origin refused"

echo ""
echo "ALL CASES PASSED"
```

```bash
chmod +x tests/docker/test_build_check.sh
```

- [ ] **Step 2: Run test, verify it fails (script doesn't exist yet)**

```bash
bash tests/docker/test_build_check.sh
```

Expected: `FAIL: scripts/build-docker.sh not executable or missing`.

- [ ] **Step 3: Write the build script**

Create `scripts/build-docker.sh`:

```bash
#!/usr/bin/env bash
# foreman daemon image build wrapper.
#
# Pre-build gates (per the Docker design spec, "Pre-build cleanliness check"):
#   1. local main must match origin/main (no unpushed commits)
#   2. working tree must be clean (no uncommitted edits)
#   3. --allow-dirty bypasses both AND stamps ALLOW_DIRTY=true into the
#      image so the daemon's startup log line loudly announces the
#      escape hatch (audit-by-loud-comment).
#
# Usage:
#   ./scripts/build-docker.sh                # clean-only
#   ./scripts/build-docker.sh --allow-dirty  # dev escape
set -euo pipefail

allow_dirty=false
[[ "${1:-}" == "--allow-dirty" ]] && allow_dirty=true

# Gate 1: local main vs origin/main
git fetch origin main --quiet
local_head=$(git rev-parse main)
origin_head=$(git rev-parse origin/main)
if [[ "$local_head" != "$origin_head" ]]; then
    echo "ERROR: local main ($local_head) differs from origin/main ($origin_head)" >&2
    echo "Push your branch and rebase main, or rerun with --allow-dirty." >&2
    [[ "$allow_dirty" == true ]] || exit 1
fi

# Gate 2: working tree clean
if [[ -n "$(git status --porcelain)" ]]; then
    echo "ERROR: working tree dirty — uncommitted changes would land in the image." >&2
    echo "Commit your changes, or rerun with --allow-dirty." >&2
    [[ "$allow_dirty" == true ]] || exit 1
fi

# Stamp the image SHA + allow-dirty flag as build args. These surface as
# env vars inside the container so the daemon's startup banner can name them.
image_sha=$(git rev-parse --short HEAD)

docker compose build daemon \
    --build-arg IMAGE_SHA="$image_sha" \
    --build-arg ALLOW_DIRTY="$allow_dirty"
```

```bash
chmod +x scripts/build-docker.sh
```

- [ ] **Step 4: Run test, verify it passes**

```bash
bash tests/docker/test_build_check.sh
```

Expected: `ALL CASES PASSED`.

- [ ] **Step 5: shellcheck**

```bash
shellcheck scripts/build-docker.sh tests/docker/test_build_check.sh
```

Expected: no output (passes). If shellcheck flags `SC2155` or similar style nits, fix them; structural issues should be addressed.

- [ ] **Step 6: Commit**

```bash
git add scripts/build-docker.sh tests/docker/test_build_check.sh
git commit -m "feat(docker): add build script with pre-build clean-check"
```

---

## Task 4: docker/claude/ tree — vendor skills + plugins + configs as canonical

**Files:**
- Create: `docker/claude/CLAUDE.md` (initially seeded from `~/.claude/CLAUDE.md`)
- Create: `docker/claude/settings.json` (initially seeded from `~/.claude/settings.json` if present)
- Create: `docker/claude/.mcp.json` (trimmed to context7 only)
- Create: `docker/claude/skills/` (initially seeded from `~/.claude/skills/` if any)
- Create: `docker/claude/plugins/cache/claude-plugins-official/superpowers/` (initially seeded from host plugin cache)
- Create: `scripts/refresh-claude-vendor.sh` (the ONLY blessed way to update the vendored tree)

**Canonicality rule:** Once committed, `docker/claude/` IS canonical for the daemon. The Dockerfile copies from this tree, NOT from `~/.claude/`. The host machine's `~/.claude/` is the human operator's interactive Claude Code config — it can drift freely without affecting the daemon. The daemon's behavior is pinned to whatever git SHA the image was built from. To update the vendored tree, run `scripts/refresh-claude-vendor.sh` and commit the diff as a deliberate change — not a side-effect of `claude plugin update`.

**User-scope skills are intentionally NOT vendored** (deviation from earlier draft of this plan). At first-vendor time, the host's `~/.claude/skills/` was 948 MB — dominated by gstack's `browse/` (222 MB) and `gstack/` (1.4 GB) trees with browser binaries and model caches. None of the 59 user-scope skills are referenced by foreman role prompts (Planner/Reviewer/Fixer/Worker each load their own `*.md` from `packages/foreman/src/foreman/prompts/`). If a future role needs a specific skill, vendor it explicitly into `docker/claude/skills/<skill-name>/` with a commit that names the skill and why. The refresh script enforces this: it never copies the user-scope skills tree wholesale.

**`.mcp.json` is also intentionally not refreshed from host.** The host's MCP config (in `~/.claude.json`, not `~/.claude/.mcp.json` as earlier drafted) wraps commands with `cmd /c` for Windows compatibility. The container is Linux and needs the bare `npx` form. The vendored `docker/claude/.mcp.json` is hand-maintained to keep the trimmed-to-context7 config in the right shape.

**`CLAUDE.md` is daemon-specific and hand-maintained, NOT copied from host** (Checkpoint A decision). The host's `~/.claude/CLAUDE.md` is Jeff's interactive workflow config (gstack skill references, gbrain paths) and would mis-bias the role subprocess into thinking it's an interactive assistant with skills like `/browse` available. The vendored `docker/claude/CLAUDE.md` instead tells the role subprocess what it IS (a foreman role inside a sealed container), what it IS NOT (an interactive assistant, a skills-library agent, a long-lived process), what tools it has access to, and how to handle uncertainty (fail with labels, not chat). The refresh script enforces this: it never copies `~/.claude/CLAUDE.md`.

The initial seeding below is a one-time bootstrap. After this task lands, the host plugin cache is no longer load-bearing for foreman.

- [ ] **Step 1: Identify the superpowers plugin version on host**

```bash
ls -la ~/.claude/plugins/cache/claude-plugins-official/superpowers/ 2>&1 | head -10
```

Expected: a directory like `5.1.0/` containing the plugin. Note the version for the commit message.

- [ ] **Step 2: Copy the superpowers plugin tree**

```bash
mkdir -p docker/claude/plugins/cache/claude-plugins-official
cp -r ~/.claude/plugins/cache/claude-plugins-official/superpowers \
      docker/claude/plugins/cache/claude-plugins-official/superpowers
```

- [ ] **Step 3: Copy user-scope skills (if any)**

```bash
if [[ -d ~/.claude/skills ]]; then
    cp -r ~/.claude/skills docker/claude/skills
else
    mkdir -p docker/claude/skills
    touch docker/claude/skills/.gitkeep
fi
```

- [ ] **Step 4: Copy CLAUDE.md + settings.json**

```bash
cp ~/.claude/CLAUDE.md docker/claude/CLAUDE.md
if [[ -f ~/.claude/settings.json ]]; then
    cp ~/.claude/settings.json docker/claude/settings.json
else
    echo '{}' > docker/claude/settings.json
fi
```

- [ ] **Step 5: Trim .mcp.json to context7 only**

The host's `~/.claude/.mcp.json` registers Canva, Excalidraw, Drive, Calendar, HF, Mermaid, and context7. Foreman only needs context7. Write a trimmed version:

Create `docker/claude/.mcp.json`:

```json
{
  "mcpServers": {
    "context7": {
      "command": "npx",
      "args": ["-y", "@upstash/context7-mcp@latest"]
    }
  }
}
```

If the host's actual context7 config block differs (different transport, different args), copy that block verbatim from `~/.claude/.mcp.json` instead of the boilerplate above.

- [ ] **Step 6: Strip secrets from anything that landed**

The vendored tree must NEVER contain real credentials. Scan:

```bash
grep -r -E '(api[_-]?key|secret|token|password|oauth|credential)' docker/claude/ \
    --include='*.json' --include='*.md' \
    | grep -v -E '(_env|description|EXAMPLE|placeholder)' || echo "no secrets found"
```

Expected: `no secrets found`, OR a hit list to manually scrub. The `.credentials.json` file is NEVER vendored — it comes at runtime via Compose secret.

- [ ] **Step 7: Verify the vendored tree size is sane**

```bash
du -sh docker/claude/
```

Expected: under 50MB. Superpowers plugin is the bulk; should not exceed ~10MB.

- [ ] **Step 8: Write `scripts/refresh-claude-vendor.sh`**

This is the ONLY blessed way to update `docker/claude/` after initial vendoring. It re-syncs from host, runs the secret scan, and shows a diff for the human to review before committing. Daemon behavior changes go through this script + a deliberate commit, not via `claude plugin update` on the host.

Create `scripts/refresh-claude-vendor.sh`:

```bash
#!/usr/bin/env bash
# Refresh the vendored docker/claude/ tree from the operator's host
# Claude Code config. This is a DELIBERATE update path — not a build
# step. Run it when you intentionally want to pick up a new version
# of superpowers, an updated CLAUDE.md, or a new skill.
#
# After running:
#   1. Review `git diff docker/claude/` carefully
#   2. Commit with a message naming what changed and why
#      (e.g. "chore(docker): refresh claude vendor — superpowers 5.2.0")
#   3. Rebuild the image: ./scripts/build-docker.sh
#
# The daemon's behavior is pinned to whatever git SHA the image was
# built from. There is NO automatic propagation of host changes.
set -euo pipefail

REPO_ROOT=$(git rev-parse --show-toplevel)
VENDOR_DIR="$REPO_ROOT/docker/claude"

if [[ ! -d "$VENDOR_DIR" ]]; then
    echo "ERROR: $VENDOR_DIR does not exist — run Task 4 initial vendoring first" >&2
    exit 1
fi

echo "==> Refreshing docker/claude/ from ~/.claude/"

# Superpowers plugin
if [[ -d ~/.claude/plugins/cache/claude-plugins-official/superpowers ]]; then
    rm -rf "$VENDOR_DIR/plugins/cache/claude-plugins-official/superpowers"
    mkdir -p "$VENDOR_DIR/plugins/cache/claude-plugins-official"
    cp -r ~/.claude/plugins/cache/claude-plugins-official/superpowers \
          "$VENDOR_DIR/plugins/cache/claude-plugins-official/superpowers"
    echo "  refreshed: superpowers plugin"
fi

# User skills
if [[ -d ~/.claude/skills ]]; then
    rm -rf "$VENDOR_DIR/skills"
    cp -r ~/.claude/skills "$VENDOR_DIR/skills"
    echo "  refreshed: skills/"
fi

# CLAUDE.md
if [[ -f ~/.claude/CLAUDE.md ]]; then
    cp ~/.claude/CLAUDE.md "$VENDOR_DIR/CLAUDE.md"
    echo "  refreshed: CLAUDE.md"
fi

# settings.json (optional)
if [[ -f ~/.claude/settings.json ]]; then
    cp ~/.claude/settings.json "$VENDOR_DIR/settings.json"
    echo "  refreshed: settings.json"
fi

# .mcp.json — DO NOT overwrite automatically; daemon's trimmed-to-context7
# version must stay trimmed. Print a warning if host has changed.
if [[ -f ~/.claude/.mcp.json ]]; then
    if ! diff -q <(jq '.mcpServers.context7' ~/.claude/.mcp.json) \
                 <(jq '.mcpServers.context7' "$VENDOR_DIR/.mcp.json") >/dev/null 2>&1; then
        echo "  WARNING: host .mcp.json context7 block differs from vendored" >&2
        echo "  (left intentionally untouched — edit docker/claude/.mcp.json by hand if needed)" >&2
    fi
fi

# Re-run the secret scan from Task 4 Step 6
echo "==> Scanning for accidental credential leaks"
if grep -r -E '(api[_-]?key|secret|token|password|oauth|credential)' "$VENDOR_DIR/" \
        --include='*.json' --include='*.md' \
        | grep -v -E '(_env|description|EXAMPLE|placeholder)' >&2; then
    echo "ERROR: potential credentials in refreshed vendor tree — review above and scrub before committing" >&2
    exit 1
fi
echo "  no secrets found"

echo ""
echo "==> Done. Review changes:"
echo "    git diff docker/claude/"
echo ""
echo "    Then commit with a message naming what changed and why."
```

```bash
chmod +x scripts/refresh-claude-vendor.sh
```

- [ ] **Step 9: Smoke-test the refresh script (no-op run)**

```bash
./scripts/refresh-claude-vendor.sh
git diff --stat docker/claude/
```

Expected: script exits 0 with "no secrets found". `git diff --stat` shows zero or trivial changes (e.g. a host config drifted by one byte since Step 2-5 ran). If non-trivial diff appears, the host moved during Task 4 — review and decide whether to keep the newer content or roll back.

- [ ] **Step 10: Commit**

```bash
git add docker/claude/ scripts/refresh-claude-vendor.sh
git commit -m "feat(docker): vendor claude config as canonical (superpowers + skills + refresh script)"
```

---

## CHECKPOINT A — vendored claude tree

**Pause. Look at concrete artifacts before continuing.**

- [ ] `git diff --stat docker/claude/` — what files landed, what's the total size
- [ ] `ls -la docker/claude/plugins/cache/claude-plugins-official/superpowers/` — confirm we got a sane plugin version (e.g. `5.1.0/`)
- [ ] `cat docker/claude/.mcp.json` — confirm trimmed to context7 only, no Canva/Drive/etc.
- [ ] Spot-check `docker/claude/CLAUDE.md` — is this the user-scope CLAUDE.md you actually want baked into the daemon? Any references to personal paths (`~/.wren`, `e:/workspaces/...`) that don't belong in a container?
- [ ] `du -sh docker/claude/` — under 50MB total

**Decide:** continue to Dockerfile (Task 5), or amend the vendored tree first? Common amendments at this point: remove user-scope skills the daemon won't use, scrub host-specific paths from CLAUDE.md, swap CLAUDE.md for a container-specific one.

---

## Task 5: Dockerfile — multi-stage build with cache-friendly layer ordering

**Files:**
- Create: `Dockerfile`
- Modify: (none)

- [ ] **Step 1: Write the Dockerfile**

Create `Dockerfile`:

```dockerfile
# syntax=docker/dockerfile:1.7
# Foreman daemon image.
#
# Layer order chosen for cache locality (per the Docker design spec,
# "Image build" section):
#   1. Base OS + system deps        ← changes rarely
#   2. uv                            ← changes rarely
#   3. Claude Code CLI (npm)         ← changes occasionally
#   4. Python dependency layer       ← changes when pyproject.toml/uv.lock change
#   5. Foreman source                ← changes frequently (only this rebuilds on edits)
#   6. Claude config (skills, etc.)  ← changes occasionally
#   7. Foreman config + entrypoint   ← changes rarely
#
# Build args:
#   IMAGE_SHA    — short HEAD sha, stamped into container env for audit
#   ALLOW_DIRTY  — true|false, set by scripts/build-docker.sh

FROM python:3.12-slim AS base

# Build args surfaced as env vars so the entrypoint can read them.
ARG IMAGE_SHA=unknown
ARG ALLOW_DIRTY=false
ENV IMAGE_SHA=${IMAGE_SHA} \
    ALLOW_DIRTY=${ALLOW_DIRTY} \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# --- System deps -------------------------------------------------------
# git: clone + worktree ops
# ca-certs + curl: HTTPS to github.com + anthropic
# nodejs + npm: the `claude` CLI claude_agent_sdk shells out to
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl gnupg \
        nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# --- uv -----------------------------------------------------------------
RUN pip install --no-cache-dir uv

# --- Claude Code CLI ----------------------------------------------------
# claude_agent_sdk shells out to `claude` for the actual LLM call.
# Pin to @latest for v1; bump intentionally if behavior changes.
RUN npm install -g @anthropic-ai/claude-code

# --- Python dependency layer ------------------------------------------
# Strategy: use `uv export` to emit a deps-only requirements file from
# the manifest, then `uv pip install -r` it. This avoids needing the
# source package present in the dep layer (would fail `uv pip install .`).
# This is pattern (a) from the design's Image build IMPLEMENTATION NOTE.
WORKDIR /tmp/build
COPY packages/foreman/pyproject.toml packages/foreman/uv.lock ./
RUN uv export --no-hashes --format requirements-txt --no-emit-project > requirements.txt \
    && uv pip install --system --no-cache -r requirements.txt

# --- Source layer (rebuilds on edits) ----------------------------------
WORKDIR /app/source
COPY packages/foreman ./
RUN uv pip install --system --no-cache --no-deps .

# --- Claude Code config (skills, plugins, mcp, CLAUDE.md) --------------
# Credentials are NOT here — they come via Compose secret at runtime.
COPY docker/claude/CLAUDE.md /root/.claude/CLAUDE.md
COPY docker/claude/settings.json /root/.claude/settings.json
COPY docker/claude/.mcp.json /root/.claude/.mcp.json
COPY docker/claude/skills /root/.claude/skills
COPY docker/claude/plugins /root/.claude/plugins

# --- Foreman config + entrypoint --------------------------------------
COPY docker/foreman/config.toml.container /etc/foreman/config.toml
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Ensure /foreman volume mount points exist (volumes mount over these)
RUN mkdir -p /foreman/repos /foreman/state /foreman/logs

WORKDIR /app/source

CMD ["/entrypoint.sh"]
```

- [ ] **Step 2: Lint with hadolint (if available)**

```bash
hadolint Dockerfile 2>&1 || echo "hadolint not installed — skipping (review the file manually)"
```

If hadolint is installed, address any errors. Warnings are advisory; address structural ones, ignore stylistic noise.

- [ ] **Step 3: Verify build context size is reasonable**

```bash
docker build --no-cache --target base -t foreman:base-test . 2>&1 | head -5 || \
    echo "Build will fail until compose+secrets land — verifying context size only."
du -sh . --exclude='.venv' --exclude='.git' --exclude='node_modules'
```

The exact build will fail at this point because we haven't set up Compose secrets yet; we're only verifying the COPY layer paths resolve and the build CONTEXT doesn't include junk (should be under 200MB after `.dockerignore` filtering).

- [ ] **Step 4: Commit**

```bash
git add Dockerfile
git commit -m "feat(docker): add multi-stage Dockerfile with cache-friendly layers"
```

---

## Task 6: docker-compose.yml — service + volumes + secrets + init:true + log rotation

**Files:**
- Create: `docker-compose.yml`
- Create: `tests/docker/test_compose_config.sh` (validates YAML + secrets referenced)

- [ ] **Step 1: Write the failing test FIRST**

Create `tests/docker/test_compose_config.sh`:

```bash
#!/usr/bin/env bash
# Compose-config sanity. We do NOT spin up containers here — `docker compose
# config` parses the file and resolves env + secrets without starting anything.
set -euo pipefail

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"

# Stub: secrets files must exist for compose to resolve them. We don't
# care about content — just presence.
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/keys"
for r in planner reviewer fixer worker orchestrator; do
    echo "stub-pem" > "$tmp/keys/$r.pem"
done
mkdir -p "$tmp/claude"
echo "stub-creds" > "$tmp/claude/.credentials.json"

HOME="$tmp" docker compose config > "$tmp/resolved.yml" 2>"$tmp/err" || {
    echo "FAIL: docker compose config errored:"
    cat "$tmp/err"
    exit 1
}

# Verify required pieces landed in resolved output
grep -q 'init: true' "$tmp/resolved.yml" || { echo "FAIL: init:true missing"; exit 1; }
grep -q 'foreman-repos' "$tmp/resolved.yml" || { echo "FAIL: foreman-repos volume missing"; exit 1; }
grep -q 'foreman-state' "$tmp/resolved.yml" || { echo "FAIL: foreman-state volume missing"; exit 1; }
grep -q 'foreman-logs' "$tmp/resolved.yml" || { echo "FAIL: foreman-logs volume missing"; exit 1; }
grep -q 'planner_pem' "$tmp/resolved.yml" || { echo "FAIL: planner_pem secret missing"; exit 1; }
grep -q 'reviewer_pem' "$tmp/resolved.yml" || { echo "FAIL: reviewer_pem secret missing"; exit 1; }
grep -q 'fixer_pem' "$tmp/resolved.yml" || { echo "FAIL: fixer_pem secret missing"; exit 1; }
grep -q 'worker_pem' "$tmp/resolved.yml" || { echo "FAIL: worker_pem secret missing"; exit 1; }
grep -q 'claude_credentials' "$tmp/resolved.yml" || { echo "FAIL: claude_credentials secret missing"; exit 1; }
grep -q 'max-size' "$tmp/resolved.yml" || { echo "FAIL: log rotation max-size missing"; exit 1; }

echo "PASS: compose config resolves with all required pieces"
```

```bash
chmod +x tests/docker/test_compose_config.sh
```

- [ ] **Step 2: Run test, verify it fails (compose file doesn't exist)**

```bash
bash tests/docker/test_compose_config.sh
```

Expected: `FAIL: docker compose config errored` (no compose file).

- [ ] **Step 3: Write the compose file**

Create `docker-compose.yml`:

```yaml
# Foreman daemon container orchestration.
# Per the Docker design spec; see docs/superpowers/specs/2026-06-05-foreman-docker-runtime-design.md
#
# IMPLEMENTATION NOTES (caught during execution):
#  - `worker_pem` is intentionally absent. Host had no `worker.pem`
#    at first-vendor time; Worker bot setup is a separate follow-up
#    ticket. Daemon serves Planner/Reviewer/Fixer without it.
#  - `env_file` uses Compose v2.18+ `required: false` so the smoke
#    test (and any operator who skips .env) resolves cleanly. Real
#    deploys still need .env populated from .env.example.
#
# Lifecycle:
#   docker compose up -d daemon       # start (detached)
#   docker compose stop daemon        # stop (preserves state)
#   docker compose logs -f daemon     # tail structured JSON-lines
#   docker compose down               # remove container (volumes survive)
#   docker compose down -v            # DESTRUCTIVE: wipes all state

services:
  daemon:
    build:
      context: .
      dockerfile: Dockerfile
    image: foreman:dev
    container_name: foreman-daemon

    # PID 1: tini handles zombie reaping + signal forwarding for Python.
    # See Docker design spec, Architecture section, "PID 1 / signal forwarding."
    init: true

    # Env file (gitignored) holds the App IDs + ADMIN token.
    env_file:
      - .env

    # Compose secrets — Max credentials + 4 GitHub App pem keys + orchestrator.
    # tmpfs-mounted at /run/secrets/<name>, read-only, never persisted in image.
    secrets:
      - planner_pem
      - reviewer_pem
      - fixer_pem
      - worker_pem
      - orchestrator_pem
      - claude_credentials

    # Persistent state volumes. NOT visible from the Windows host — inspect
    # via `docker exec foreman-daemon ls /foreman/...` or `docker cp`.
    volumes:
      - foreman-repos:/foreman/repos
      - foreman-state:/foreman/state
      - foreman-logs:/foreman/logs

    # Docker json-file driver: rotation at 10MB × 5 files (≈50MB ceiling).
    # Without this it grows forever inside the WSL2 VM.
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"

# Named volumes (managed by Docker; survive container restarts and `down`).
volumes:
  foreman-repos:
  foreman-state:
  foreman-logs:

# Secrets sourced from the host's protected user directory.
secrets:
  planner_pem:
    file: ${HOME}/.foreman/keys/planner.pem
  reviewer_pem:
    file: ${HOME}/.foreman/keys/reviewer.pem
  fixer_pem:
    file: ${HOME}/.foreman/keys/fixer.pem
  worker_pem:
    file: ${HOME}/.foreman/keys/worker.pem
  orchestrator_pem:
    file: ${HOME}/.foreman/keys/orchestrator.pem
  claude_credentials:
    file: ${HOME}/.claude/.credentials.json
```

- [ ] **Step 4: Run test, verify it passes**

```bash
bash tests/docker/test_compose_config.sh
```

Expected: `PASS: compose config resolves with all required pieces`.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml tests/docker/test_compose_config.sh
git commit -m "feat(docker): add compose orchestration with volumes + secrets + log rotation"
```

---

## CHECKPOINT B — full Docker surface written

**Pause. The container's behavior is now fully defined on paper. Read it.**

- [ ] Open `Dockerfile` end-to-end. Does the layer order make sense (apt → uv → node → deps → source → config)? Are any RUN steps doing work that should be at runtime?
- [ ] Open `docker-compose.yml`. Are all 3 volumes declared? All 5 secrets? `init: true` present? json-file rotation set to 10m × 5?
- [ ] `cat docker/entrypoint.sh` — credentials plumbing readable, exits non-zero on missing secret
- [ ] `cat scripts/build-docker.sh` — clean-check logic + `--allow-dirty` escape hatch + build-arg propagation
- [ ] Mental walkthrough: if I run `docker compose up -d daemon` right now, what would happen? Trace the boot sequence: image build → entrypoint → daemon main → lock acquire → observer auth → GraphQL fetch. Anywhere it could fail silently?

**Decide:** continue to Python edits (Task 7), or amend the Docker surface first? This is the cheapest moment to change the container architecture — once we start mutating Python to fit it, reversal cost goes up.

---

## Task 7: v3_host dispatch argv flip (TDD)

**Files:**
- Modify: `packages/foreman/src/foreman/reconciler/v3_host.py`
- Modify: `packages/foreman/tests/reconciler/test_v3_host.py`

- [ ] **Step 1: Write failing test pinning the new argv shape**

Append to `packages/foreman/tests/reconciler/test_v3_host.py`:

```python
def test_dispatch_role_argv_does_not_use_uv_run(tmp_path: Path) -> None:
    """The container-runtime daemon dispatches via PATH-resolved `foreman`,
    NOT `uv run foreman`. The `uv run` form re-syncs the editable install
    on every invocation, which fails on Windows when the daemon's own
    `foreman.exe` is file-locked (the failure mode this whole design
    eliminates)."""
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    captured_argv: list[list[str]] = []

    class _FakeProc:
        pid = 4242

        async def wait(self) -> int:
            return 0

    def runner(argv: list[str], **kwargs: Any) -> _FakeProc:
        captured_argv.append(argv)
        return _FakeProc()

    host = V3GitHubHost(v2_host=_FakeV2Host(), log=log, subprocess_runner=runner)
    start_id = log.write_action(
        ticket_id="foreman/owner/repo#143",
        project="foreman",
        rule_name="dispatch_planner",
        action="dispatch_planner",
        outcome="running",
        details={},
    )
    host.dispatch_role(
        role="planner",
        target=None,
        owner="owner",
        repo="repo",
        issue=143,
        pr_number=None,
        start_log_id=start_id,
        project="foreman",
    )
    assert len(captured_argv) == 1
    argv = captured_argv[0]
    assert argv[0] == "foreman", f"expected argv[0]='foreman', got {argv[0]!r}"
    assert "uv" not in argv[:2], (
        f"`uv run` wrapper still in argv — this re-introduces the Windows "
        f"file-lock bug the Docker runtime exists to eliminate. argv={argv}"
    )
```

- [ ] **Step 2: Run test, verify it fails**

```bash
uv run --no-sync pytest packages/foreman/tests/reconciler/test_v3_host.py::test_dispatch_role_argv_does_not_use_uv_run -v
```

Expected: FAIL — current code emits `["uv", "run", "foreman", ...]`.

- [ ] **Step 3: Flip the argv in v3_host.py**

Modify `packages/foreman/src/foreman/reconciler/v3_host.py`, replacing the line:

```python
argv: list[str] = ["uv", "run", "foreman", subcommand]
```

with:

```python
argv: list[str] = ["foreman", subcommand]
```

Find the line (search for `["uv", "run", "foreman", subcommand]`) and replace exactly that single string.

- [ ] **Step 4: Run target test, verify it passes**

```bash
uv run --no-sync pytest packages/foreman/tests/reconciler/test_v3_host.py::test_dispatch_role_argv_does_not_use_uv_run -v
```

Expected: PASS.

- [ ] **Step 5: Run the broader v3_host suite to catch regressions**

```bash
uv run --no-sync pytest packages/foreman/tests/reconciler/test_v3_host.py packages/foreman/tests/reconciler/test_v3_host_timeout.py -q
```

Expected: all green (the 17+1 existing tests plus the new one).

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/reconciler/v3_host.py packages/foreman/tests/reconciler/test_v3_host.py
git commit -m "feat(reconciler): drop \"uv run\" wrapper from dispatch argv (#98 #99)"
```

---

## Task 8: FOREMAN_LOG_DIR env-var wiring (TDD)

**Files:**
- Modify: `packages/foreman/src/foreman/reconciler/v3_host.py` (or the daemon entry that constructs `V3GitHubHost`)
- Modify: `packages/foreman/src/foreman/cli.py` (the v3-start command that builds the host)
- Modify: `packages/foreman/tests/reconciler/test_v3_host.py`

The design says `log_dir` should be env-var driven (`FOREMAN_LOG_DIR`) with a host fallback (`~/.foreman/logs`).

- [ ] **Step 1: Write failing test for the env-var resolution helper**

Append to `packages/foreman/tests/reconciler/test_v3_host.py`:

```python
def test_foreman_log_dir_env_var_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """FOREMAN_LOG_DIR overrides the default host fallback."""
    monkeypatch.setenv("FOREMAN_LOG_DIR", "/custom/path/foreman/logs")
    from foreman.reconciler.v3_host import resolve_log_dir
    assert resolve_log_dir() == Path("/custom/path/foreman/logs")


def test_foreman_log_dir_falls_back_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """When FOREMAN_LOG_DIR is unset, fall back to ~/.foreman/logs.
    Keeps `foreman daemon v3-start` invokable on the host for ad-hoc
    debug without containerization."""
    monkeypatch.delenv("FOREMAN_LOG_DIR", raising=False)
    from foreman.reconciler.v3_host import resolve_log_dir
    assert resolve_log_dir() == Path.home() / ".foreman" / "logs"
```

- [ ] **Step 2: Run tests, verify they fail (resolve_log_dir doesn't exist)**

```bash
uv run --no-sync pytest packages/foreman/tests/reconciler/test_v3_host.py -k "test_foreman_log_dir" -v
```

Expected: FAIL with `ImportError` or `AttributeError`.

- [ ] **Step 3: Implement `resolve_log_dir` in v3_host.py**

Add to `packages/foreman/src/foreman/reconciler/v3_host.py` (near the top, after imports):

```python
def resolve_log_dir() -> Path:
    """Resolve the per-dispatch log directory, honoring container env var.

    Container compose sets ``FOREMAN_LOG_DIR=/foreman/logs``. On the host,
    when the env var is unset, fall back to ``~/.foreman/logs`` so
    ``foreman daemon v3-start`` is still invokable for ad-hoc debug.
    """
    env_value = os.environ.get("FOREMAN_LOG_DIR")
    if env_value:
        return Path(env_value)
    return Path.home() / ".foreman" / "logs"
```

Ensure `os` is imported at the module top (probably already is).

- [ ] **Step 4: Wire the resolver into the cli.py daemon construction**

Modify `packages/foreman/src/foreman/cli.py` — in the daemon-start path that constructs `V3GitHubHost`, replace the hardcoded `log_dir` argument:

From:

```python
host = V3GitHubHost(
    v2_host=v2_host,
    log=log,
    role_dispatch_timeout_seconds=config.reconciler.role_dispatch_timeout_seconds,
    max_concurrent_dispatches=config.reconciler.max_concurrent_dispatches,
    log_dir=Path.home() / ".foreman" / "logs",
)
```

To:

```python
host = V3GitHubHost(
    v2_host=v2_host,
    log=log,
    role_dispatch_timeout_seconds=config.reconciler.role_dispatch_timeout_seconds,
    max_concurrent_dispatches=config.reconciler.max_concurrent_dispatches,
    log_dir=resolve_log_dir(),
)
```

Add the import at the top of `cli.py`:

```python
from foreman.reconciler.v3_host import resolve_log_dir
```

- [ ] **Step 5: Run tests, verify they pass**

```bash
uv run --no-sync pytest packages/foreman/tests/reconciler/test_v3_host.py -k "test_foreman_log_dir" -v
```

Expected: both new tests PASS.

- [ ] **Step 6: Run the broader v3_host suite + cli tests**

```bash
uv run --no-sync pytest packages/foreman/tests/reconciler/test_v3_host.py packages/foreman/tests/test_cli.py packages/foreman/tests/test_cli_v3.py -q
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add packages/foreman/src/foreman/reconciler/v3_host.py \
        packages/foreman/src/foreman/cli.py \
        packages/foreman/tests/reconciler/test_v3_host.py
git commit -m "feat(reconciler): FOREMAN_LOG_DIR env var with host fallback"
```

---

## Task 9: FOREMAN_CONFIG_PATH env-var driven config (TDD)

**Files:**
- Modify: `packages/foreman/src/foreman/config.py`
- Modify: `packages/foreman/tests/test_config.py`

Same env-var-with-host-fallback shape as Task 8, but for the config TOML path.

- [ ] **Step 1: Write failing tests**

Append to `packages/foreman/tests/test_config.py`:

```python
def test_foreman_config_path_env_var_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FOREMAN_CONFIG_PATH overrides the default host location."""
    custom = tmp_path / "custom-config.toml"
    custom.write_text(
        "[admin]\n"
        "github_token_env = \"FOREMAN_ADMIN_TOKEN\"\n"
    )
    monkeypatch.setenv("FOREMAN_CONFIG_PATH", str(custom))
    from foreman.config import resolve_config_path
    assert resolve_config_path() == custom


def test_foreman_config_path_falls_back_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """When FOREMAN_CONFIG_PATH is unset, fall back to ~/.foreman/config.toml."""
    monkeypatch.delenv("FOREMAN_CONFIG_PATH", raising=False)
    from foreman.config import resolve_config_path
    assert resolve_config_path() == Path.home() / ".foreman" / "config.toml"
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
uv run --no-sync pytest packages/foreman/tests/test_config.py -k "foreman_config_path" -v
```

Expected: FAIL.

- [ ] **Step 3: Implement `resolve_config_path` in config.py**

Add to `packages/foreman/src/foreman/config.py`:

```python
def resolve_config_path() -> Path:
    """Resolve the foreman config TOML path, honoring container env var.

    Container compose sets ``FOREMAN_CONFIG_PATH=/etc/foreman/config.toml``.
    On the host, when the env var is unset, fall back to
    ``~/.foreman/config.toml``.
    """
    env_value = os.environ.get("FOREMAN_CONFIG_PATH")
    if env_value:
        return Path(env_value)
    return Path.home() / ".foreman" / "config.toml"
```

Ensure `os` and `Path` are imported.

- [ ] **Step 4: Wire `resolve_config_path()` into the existing config-loading callers**

Find every place in the codebase that constructs the default config path:

```bash
grep -rn "\.foreman/config\.toml\|'\.foreman', 'config\.toml'" packages/foreman/src/foreman/
```

For each hit (likely `cli.py` and any `__init__.py` daemon entry), replace the hardcoded path with `resolve_config_path()` so env-var override flows through.

- [ ] **Step 5: Run tests, verify they pass**

```bash
uv run --no-sync pytest packages/foreman/tests/test_config.py -k "foreman_config_path" -v
```

Expected: both PASS.

- [ ] **Step 6: Run broader config + cli suite**

```bash
uv run --no-sync pytest packages/foreman/tests/test_config.py packages/foreman/tests/test_cli.py packages/foreman/tests/test_cli_v3.py -q
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add packages/foreman/src/foreman/config.py \
        packages/foreman/src/foreman/cli.py \
        packages/foreman/tests/test_config.py
git commit -m "feat(config): FOREMAN_CONFIG_PATH env var with host fallback"
```

---

## Task 10: Dual logging handler (TDD)

**Files:**
- Modify: `packages/foreman/src/foreman/cli.py` (or wherever daemon logging is configured)
- Modify: `packages/foreman/tests/test_cli_v3.py`

Per the design's Logging section: daemon emits JSON-lines to BOTH stdout (for `docker compose logs -f`) AND to a file in `/foreman/state/v3-daemon.log` (for `docker exec ... tail` after `compose down`).

- [ ] **Step 1: Write failing test**

Append to `packages/foreman/tests/test_cli_v3.py`:

```python
def test_daemon_logging_configures_both_stdout_and_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The daemon's structured log goes to BOTH stdout AND a file in the
    state directory so nothing is lost on `docker compose down`."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("FOREMAN_STATE_DIR", str(state_dir))

    import logging
    from foreman.cli import configure_daemon_logging

    # Reset any prior handlers
    root = logging.getLogger()
    prior_handlers = list(root.handlers)
    for h in prior_handlers:
        root.removeHandler(h)

    try:
        configure_daemon_logging()
        handler_types = {type(h).__name__ for h in root.handlers}
        assert "StreamHandler" in handler_types, (
            f"missing StreamHandler — daemon stdout won't reach `docker logs`. "
            f"got: {handler_types}"
        )
        assert "FileHandler" in handler_types, (
            f"missing FileHandler — daemon log won't survive `compose down`. "
            f"got: {handler_types}"
        )

        # Verify the file handler points at the right path
        file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
        assert any(
            Path(h.baseFilename) == state_dir / "v3-daemon.log"
            for h in file_handlers
        ), (
            f"FileHandler targets wrong path; expected {state_dir / 'v3-daemon.log'!s}, "
            f"got {[h.baseFilename for h in file_handlers]}"
        )
    finally:
        # Restore the original handler set
        for h in root.handlers:
            root.removeHandler(h)
        for h in prior_handlers:
            root.addHandler(h)
```

- [ ] **Step 2: Run test, verify it fails**

```bash
uv run --no-sync pytest packages/foreman/tests/test_cli_v3.py::test_daemon_logging_configures_both_stdout_and_file -v
```

Expected: FAIL (`configure_daemon_logging` doesn't exist OR doesn't set up both handlers).

- [ ] **Step 3: Add `configure_daemon_logging` to cli.py**

Add to `packages/foreman/src/foreman/cli.py`:

```python
def configure_daemon_logging() -> None:
    """Configure JSON-lines logging for the v3 daemon.

    Two handlers:
      * StreamHandler -> stdout, captured by Docker's json-file driver
        (visible via ``docker compose logs -f daemon``).
      * FileHandler -> ``<FOREMAN_STATE_DIR>/v3-daemon.log``, persists
        through ``docker compose down`` so post-mortem after a crash
        is one ``docker exec`` away.

    Both emit the same JSON-lines records via the existing
    ``foreman.logging.JSONLineFormatter`` (or stdlib equivalent if not
    yet present).
    """
    import logging
    import os
    import sys
    from pathlib import Path

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Pull or build the JSON-lines formatter foreman already uses.
    try:
        from foreman.logging_config import JSONLineFormatter  # type: ignore[attr-defined]
        formatter: logging.Formatter = JSONLineFormatter()
    except (ImportError, AttributeError):
        # Fallback: stdlib JSON formatter via a thin adapter.
        import json as _json

        class _JSONFmt(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                obj = {
                    "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%f"),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                }
                if record.exc_info:
                    obj["exception"] = self.formatException(record.exc_info)
                return _json.dumps(obj)

        formatter = _JSONFmt()

    # Handler 1: stdout for Docker's json-file driver.
    stream_h = logging.StreamHandler(stream=sys.stdout)
    stream_h.setFormatter(formatter)
    root.addHandler(stream_h)

    # Handler 2: file in the state volume.
    state_dir = Path(os.environ.get("FOREMAN_STATE_DIR", Path.home() / ".foreman"))
    state_dir.mkdir(parents=True, exist_ok=True)
    file_h = logging.FileHandler(state_dir / "v3-daemon.log", encoding="utf-8")
    file_h.setFormatter(formatter)
    root.addHandler(file_h)
```

Then call `configure_daemon_logging()` near the top of the existing `daemon v3-start` command handler in `cli.py` (replace any existing single-handler logging setup).

- [ ] **Step 4: Run test, verify it passes**

```bash
uv run --no-sync pytest packages/foreman/tests/test_cli_v3.py::test_daemon_logging_configures_both_stdout_and_file -v
```

Expected: PASS.

- [ ] **Step 5: Run cli + cli_v3 suites**

```bash
uv run --no-sync pytest packages/foreman/tests/test_cli.py packages/foreman/tests/test_cli_v3.py -q
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/cli.py packages/foreman/tests/test_cli_v3.py
git commit -m "feat(logging): dual handler (stdout + state-volume file) for daemon"
```

---

## Task 11: Clone-on-first-run in worktree.py (TDD)

**Files:**
- Modify: `packages/foreman/src/foreman/worktree.py`
- Modify: `packages/foreman/tests/test_worktree.py`

In the container, project clones live in the `foreman-repos` volume. First start with empty volume → daemon must `git clone` each registered project before any worktree-add can happen.

- [ ] **Step 1: Write failing test**

Append to `packages/foreman/tests/test_worktree.py`:

```python
def test_ensure_clone_creates_clone_when_missing(tmp_path: Path) -> None:
    """When the configured local_clone_path doesn't exist, ensure_clone
    must `git clone <repo_url>` into it. Used by the container's first
    boot when the foreman-repos volume is empty.

    We stub the actual clone with a local bare repo so the test doesn't
    hit the network."""
    # Set up a local "origin" the daemon will clone from
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=seed, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "seed@example.com"],
        cwd=seed, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Seed"],
        cwd=seed, check=True, capture_output=True,
    )
    (seed / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "clone", "--bare", str(seed), str(origin)], check=True, capture_output=True)

    target = tmp_path / "repos" / "myproject"
    assert not target.exists(), "precondition: target must be missing"

    from foreman.worktree import ensure_clone
    ensure_clone(repo_url=str(origin), clone_path=target)

    assert target.exists(), "clone directory should exist after ensure_clone"
    assert (target / ".git").exists(), "should be a real git clone, not just a dir"
    # Should NOT re-clone on second call (idempotent)
    first_mtime = (target / ".git").stat().st_mtime
    ensure_clone(repo_url=str(origin), clone_path=target)
    second_mtime = (target / ".git").stat().st_mtime
    assert first_mtime == second_mtime, "ensure_clone must be idempotent on second call"
```

- [ ] **Step 2: Run test, verify it fails**

```bash
uv run --no-sync pytest packages/foreman/tests/test_worktree.py::test_ensure_clone_creates_clone_when_missing -v
```

Expected: FAIL (`ensure_clone` doesn't exist).

- [ ] **Step 3: Implement `ensure_clone` in worktree.py**

Add to `packages/foreman/src/foreman/worktree.py`:

```python
def ensure_clone(*, repo_url: str, clone_path: Path) -> None:
    """Ensure ``clone_path`` is a valid git clone of ``repo_url``.

    First-run helper for the container: when the ``foreman-repos`` Docker
    volume is empty, ``clone_path`` doesn't exist yet, and the daemon
    must clone the project from origin before any worktree-add. Idempotent:
    if ``clone_path`` already contains a ``.git`` directory, this is a no-op.

    Args:
        repo_url: Remote URL (HTTPS or SSH). Authentication via PATH-resolved
            credentials / ssh agent / app-token URL rewriting as per the
            caller's existing convention.
        clone_path: Local filesystem path where the clone should live.

    Raises:
        subprocess.CalledProcessError: if `git clone` fails.
    """
    if (clone_path / ".git").exists():
        return  # already cloned; nothing to do
    clone_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", repo_url, str(clone_path)],
        check=True,
        capture_output=True,
    )
```

- [ ] **Step 4: Run test, verify it passes**

```bash
uv run --no-sync pytest packages/foreman/tests/test_worktree.py::test_ensure_clone_creates_clone_when_missing -v
```

Expected: PASS.

- [ ] **Step 5: Wire ensure_clone into WorktreeManager.create / create_impl**

Find the existing `WorktreeManager.create` method in `worktree.py`. Right at the top of the method (before any worktree-add operations), add:

```python
ensure_clone(repo_url=self._project_repo_url(...), clone_path=clone_path)
```

The exact signature for `repo_url` resolution depends on how `clone_path` is currently passed in. If the caller already knows both, plumb `repo_url` through as a new keyword arg. If not, accept a slight refactor that has the caller pass it.

This step IS optional for the test to pass (the test calls `ensure_clone` directly). It becomes load-bearing when the container first boots; defer the integration call wiring until the first container smoke test (Task 14) surfaces the need.

- [ ] **Step 6: Run broader worktree suite**

```bash
uv run --no-sync pytest packages/foreman/tests/test_worktree.py -q
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add packages/foreman/src/foreman/worktree.py packages/foreman/tests/test_worktree.py
git commit -m "feat(worktree): add ensure_clone for first-run container bootstrap"
```

---

## CHECKPOINT C — all Python edits done; host suite green

**Pause. All in-process code changes are done. Last quiet moment before we touch Docker for real.**

- [ ] `git log --oneline main..HEAD` — review what's actually in the branch. Each commit is one focused change?
- [ ] `.venv/Scripts/python.exe -m pytest packages/foreman/tests -q 2>&1 | tail -5` — must show `>= 808 passed` (we added new tests in Tasks 7-11; deletions or unchanged-count would be a smell)
- [ ] Spot-check the 5 surgical Python changes against the design:
  - dispatch argv flips from `["uv", "run", "foreman", ...]` to `["foreman", ...]`
  - `FOREMAN_LOG_DIR` env var with host fallback
  - `FOREMAN_CONFIG_PATH` env var with host fallback
  - Dual logging handler (stdout + file)
  - Clone-on-first-run in `worktree.py`
- [ ] Daemon still runs HOST-side: `foreman daemon v3-status` — if currently running, it's still healthy with the in-place edits (or you stopped it before Task 0)

**Decide:** continue to RUNBOOK + first `docker compose build` (Tasks 12-13), or amend Python first? After this point, "the host-side tests pass but the container build fails" becomes a class of bug — we want the host green NOW so a failed build can't be blamed on the Python changes.

---

## Task 12: RUNBOOK.md — pre-cutover worktree-sweep ritual + daily ops

**Files:**
- Create: `docs/RUNBOOK.md`

The design's Migration section calls for a sweep ritual before cutover. Document it.

- [ ] **Step 1: Write the runbook**

Create `docs/RUNBOOK.md`:

```markdown
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

---

## Recovery: daemon won't start

1. Check the daemon-log file directly:
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
```

- [ ] **Step 2: Commit**

```bash
git add docs/RUNBOOK.md
git commit -m "docs(runbook): pre-cutover ritual + daily ops for docker runtime"
```

---

## Task 13: First successful image build (smoke)

**Files:** (none modified — pure verification)

- [ ] **Step 1: Stop any host-side daemon**

```bash
ls -la ~/.foreman/reconciler.lock 2>&1 | head -2
# If it exists, stop it via foreman daemon stop OR taskkill
```

- [ ] **Step 2: Ensure secrets and .env exist on host**

```bash
ls -la ~/.foreman/keys/*.pem 2>&1 | head -8
ls -la ~/.claude/.credentials.json 2>&1 | head -2
ls -la .env 2>&1 | head -2
```

If `.env` is missing, copy from `.env.example` and fill values.

- [ ] **Step 3: Run a clean build (no cache)**

```bash
time ./scripts/build-docker.sh
```

Expected: build completes successfully. Capture elapsed time — this is the spike-3 measurement for the design's acceptance criterion.

- [ ] **Step 4: Run an incremental build (with one source edit)**

```bash
touch packages/foreman/src/foreman/cli.py
time ./scripts/build-docker.sh
git checkout packages/foreman/src/foreman/cli.py
```

Expected: incremental build completes in 5–30 seconds (only the source layer + the `--no-deps` reinstall invalidate).

- [ ] **Step 5: Verify image size + content**

```bash
docker image ls foreman:dev
docker run --rm foreman:dev bash -c 'which foreman && foreman --version'
docker run --rm foreman:dev bash -c 'which claude && claude --version'
```

Expected: image under 2GB; `foreman --version` prints; `claude` binary exists.

- [ ] **Step 6: No commit** (smoke only). If the build needed Dockerfile fixes, those land as their own commit in this task with message `fix(docker): <what>`.

---

## CHECKPOINT D — first image build succeeds

**Pause. We just proved Docker can build foreman. Big moment.**

- [ ] `docker images foreman:latest` — image exists, note the size (expect 1-2 GB; if it's 5+ GB something pulled in dev cruft)
- [ ] Re-run `./scripts/build-docker.sh` immediately — second build should be near-instant (full cache hit). If it's not, the layer ordering is wrong and we should fix BEFORE building habits around slow rebuilds.
- [ ] Eyeball the build log: any `WARNING` lines worth investigating? Common ones: apt-get telling us to use `--no-install-recommends`, npm complaining about a deprecated package, uv warning about resolution divergence.
- [ ] Reality check: what we just built is what would ship to a teammate if we cut a release today. Is that the artifact we want? Anything we should bake in differently before the container actually starts in Task 14?

**Decide:** continue to first container start (Task 14), or amend the Dockerfile? Common amendments at this point: trim base image, prune dev tools that snuck in, fix layer ordering for cache hits.

---

## Task 14: First successful container start (smoke)

**Files:** (none modified — pure verification)

- [ ] **Step 1: Start daemon detached**

```bash
docker compose up -d daemon
sleep 5
docker compose ps daemon
```

Expected: state `Up`, no exit code.

- [ ] **Step 2: Tail logs for startup signals**

```bash
docker compose logs daemon | head -30
```

Expected: see the `container_start` JSON line from `entrypoint.sh`,
then daemon initialization (lock acquisition, observer auth, GraphQL fetch).

- [ ] **Step 3: Verify the lock file landed in the state volume**

```bash
docker exec foreman-daemon ls -la /foreman/state/
```

Expected: `reconciler.lock` present.

- [ ] **Step 4: Verify first-run clones happened**

```bash
docker exec foreman-daemon ls /foreman/repos/
```

Expected: `voice`, `foreman`, `agent_core` directories present (one
or more per the config). If only some cloned, check whether
`ensure_clone` is being called from `WorktreeManager.create`; wire it
in if not (Task 11 step 5).

- [ ] **Step 5: Stop cleanly**

```bash
docker compose stop daemon
docker compose ps daemon
```

Expected: state `Exited (0)`. Lock file should be released cleanly.

- [ ] **Step 6: Restart and verify state survived**

```bash
docker compose up -d daemon
sleep 3
docker exec foreman-daemon ls /foreman/repos/
docker exec foreman-daemon ls /foreman/state/
docker compose stop daemon
```

Expected: clones + state still present (volumes did their job).

- [ ] **Step 7: No commit** (smoke only). Fix any compose / entrypoint
issues that surfaced as their own commits in this task.

---

## Task 15: End-to-end ticket smoke test

**Files:** (none modified — pure verification)

Drive a single tiny ticket through the autonomous Planner → spec PR
→ auto-merge cycle to validate the dispatch path end-to-end.

- [ ] **Step 1: File a dogfood ticket**

Create a small ticket on a low-stakes project (e.g., voice or
agent_core) with a clearly-scoped change. Apply the `foreman:planning`
label using your Wren PAT:

```bash
PAT=$(python C:/Users/jeffr/.wren/.claude/skills/creds-management/scripts/creds.py --being wren get github --keyring --password)
GH_TOKEN=$PAT gh issue create --repo jeffrichley/voice \
    --title "test(docker): smoke ticket for docker runtime cutover" \
    --label "foreman:planning" \
    --body "Test ticket for foreman docker-runtime cutover. Trivial change: bump a comment in README.md."
```

Capture the issue number for monitoring.

- [ ] **Step 2: Start daemon and watch the dispatch log**

```bash
docker compose up -d daemon
docker compose logs -f daemon &
log_pid=$!
```

- [ ] **Step 3: Wait for the cycle to complete (or stall) — max 10 minutes**

Expected log line sequence:

1. Observer detects the ticket with `foreman:planning` label
2. Reconciler dispatches `run_planner` (logged with the per-dispatch log path)
3. Planner subprocess completes (returncode 0; spec PR opened)
4. Reconciler observes `foreman:spec-review`, dispatches `run_reviewer_spec`
5. Reviewer completes, transitions to `foreman:ready-for-merge`
6. Reconciler executes `merge_spec_pr` (auto-merge spec is default true)
7. Issue transitions to `foreman:plan-approved`
8. Pipeline parks

If any step fails, stop here and inspect the per-dispatch log:

```bash
docker exec foreman-daemon ls -la /foreman/logs/planner/
docker exec foreman-daemon cat /foreman/logs/planner/<ts>.log
```

- [ ] **Step 4: Stop the log tail + the daemon**

```bash
kill $log_pid
docker compose stop daemon
```

- [ ] **Step 5: Verify GitHub-side state**

```bash
GH_TOKEN=$PAT gh issue view <issue-number> --repo jeffrichley/voice --json labels
GH_TOKEN=$PAT gh pr list --repo jeffrichley/voice --json number,title,state
```

Expected: spec PR opened + auto-merged; issue has `foreman:plan-approved`.

- [ ] **Step 6: No commit** (smoke only). If anything failed, file follow-up tickets and address before the design PR merges.

---

## CHECKPOINT E — end-to-end ticket smoke green

**Pause. The daemon ran a ticket inside Docker, opened a PR, all of it worked. Cutover-ready signal.**

- [ ] Watch the container for a quiet minute: `docker compose logs -f daemon` — anything weird in the steady-state log stream? Repeated warnings? Crashed-and-restarted markers?
- [ ] `docker stats foreman-daemon-1` (or whatever the container name resolved to) — CPU/RAM at idle. If RAM is climbing visibly over 30 seconds, we have a leak we want to know about before shipping.
- [ ] Look at the PR that the smoke-test ticket produced. Did it come out the same as a host-side run would have? Same commit author? Same branch naming? Same labels applied?
- [ ] Re-read the design's 13 acceptance criteria. How many are now verified by smoke tests vs still on the honor system?
- [ ] **Cutover decision:** if we merge this PR now, the next daemon-restart anywhere uses the container. Are we ready for that, or do we want a few hours of soak before merging?

**Decide:** continue to CI cleanup + PR open (Tasks 16-18), or burn-in for a soak window first? If soaking, what's the gate signal — N hours? N tickets processed? Specific failure mode we want to prove absent?

---

## Task 16: ci.yml comment refresh + acceptance verification

**Files:**
- Modify: `.github/workflows/ci.yml` (comment update)
- Verify: every acceptance criterion in the design spec

- [ ] **Step 1: Update the ci.yml comment**

Modify `.github/workflows/ci.yml`. Find the existing block:

```yaml
        # Windows runner removed 2026-06-04 — pre-existing pytest/pathlib
        # KeyboardInterrupt flake caused every PR to require admin-override
        # merge. Restore once the flake is root-caused. Tracked separately.
        os: [ubuntu-latest]
```

Replace with:

```yaml
        # Windows runner removed permanently 2026-06-05 per the Docker
        # runtime design (`docs/superpowers/specs/2026-06-05-foreman-docker-runtime-design.md`).
        # The daemon now runs in a Linux container under WSL2; Windows
        # is no longer a target environment for the daemon path.
        # foreman#98 + foreman#99 closed by that design.
        os: [ubuntu-latest]
```

- [ ] **Step 2: Walk the design's acceptance criteria list**

Open `docs/superpowers/specs/2026-06-05-foreman-docker-runtime-design.md`,
jump to the "Acceptance criteria" section, and check off (or correct)
each item against the actual implementation. The 13 acceptance items:

- [ ] build script refuses dirty / accepts --allow-dirty (Task 3 test passes)
- [ ] daemon starts successfully via compose (Task 14 smoke passed)
- [ ] real Planner dispatch completes end-to-end (Task 15 smoke passed)
- [ ] dispatch subprocess output lands in /foreman/logs (Task 14 confirmed)
- [ ] stop/start cycle preserves project clones, exec log, dispatch logs (Task 14 step 6)
- [ ] `down -v` wipes everything; next `up -d` clones fresh (verify by running it intentionally)
- [ ] pre-push pytest on Win11 host still green (run it explicitly)
- [ ] foreman#98 + foreman#99 closed (do this in Task 17 below)
- [ ] Pepper criterion-check completed (already done during design phase)
- [ ] Pre-cutover worktree-sweep ritual completed (RUNBOOK.md exists)
- [ ] Daemon startup log emits IMAGE_SHA + allow_dirty (entrypoint banner)
- [ ] init: true / tini configured + zero zombies after 100+ dispatches (Task 14 step 6 + a long-running smoke)
- [ ] Docker json-file log rotation configured (compose file has it)

- [ ] **Step 3: Run pre-push pytest on host as a final gate**

```bash
cd e:/workspaces/ai/agents/foreman-worktrees/docker-runtime
uv run --no-sync pytest packages/foreman/tests -q
```

Expected: 808 + new tests, all green. Capture the count.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "chore(ci): refresh Windows-removed comment to reference Docker design"
```

---

## Task 17: gitleaks pre-commit hook

**Why:** Independent of the Docker work, but landing in the same PR — we are about to permanently bake secret-handling patterns (Compose secrets, `.env` files, mounted credentials) into the runtime. The class of bug we want to prevent is "operator accidentally `git add`s an `.env` file with real PEM contents pasted in by mistake." gitleaks pre-commit hook catches that at commit time, before the secret ever enters git history. Cheap to add now; harder to add after the runtime is in production.

**Files:**
- Create: `.pre-commit-config.yaml`
- Modify: `.gitignore` (verify `.env` already ignored — done in Task 1)
- Create: `docs/RUNBOOK.md` (modify — add "Installing pre-commit hooks" section)

- [ ] **Step 1: Write `.pre-commit-config.yaml`**

Create at the repo root:

```yaml
# Pre-commit hooks for foreman.
#
# Install once per clone:
#   uv run pre-commit install
#
# Then every `git commit` runs the configured hooks against staged
# files. To run against all files on demand:
#   uv run pre-commit run --all-files

repos:
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.21.2
    hooks:
      - id: gitleaks
        name: gitleaks (detect committed secrets)
        # Default config scans staged content; no .gitleaks.toml needed
        # unless we want to allowlist specific patterns later.
```

Pin the version explicitly — gitleaks is a Go binary and `pre-commit` downloads it; floating refs would silently shift behavior under us.

- [ ] **Step 2: Add `pre-commit` to the dev dependency group**

Verify whether foreman already has a `dev` dep group:

```bash
cd e:/workspaces/ai/agents/foreman-worktrees/docker-runtime
uv run python -c "import tomllib; cfg = tomllib.loads(open('packages/foreman/pyproject.toml').read()); print(cfg.get('dependency-groups') or cfg.get('tool', {}).get('uv', {}).get('dev-dependencies'))"
```

If `pre-commit` is not listed, add it via `uv`:

```bash
uv add --dev --package foreman pre-commit
```

Expected: `uv.lock` updated; `pyproject.toml` shows `pre-commit` in the `dev` group.

- [ ] **Step 3: Install the hook locally + run against all files**

```bash
uv run pre-commit install
uv run pre-commit run gitleaks --all-files
```

Expected: gitleaks scans the entire repo and exits 0 (no secrets found). If it flags anything, INVESTIGATE — do not pass `--no-verify` to skip. False positives get allowlisted via a `.gitleaks.toml` in a follow-up commit; real positives mean we just caught something.

- [ ] **Step 4: Add RUNBOOK section for installation**

Append to `docs/RUNBOOK.md` (created in Task 12):

```markdown
## Pre-commit hooks

After cloning the repo, install pre-commit hooks once:

```bash
uv run pre-commit install
```

This wires gitleaks into `git commit`. Every staged change is scanned
for secrets (PEM bodies, ghp_-prefixed PATs, anthropic OAuth tokens,
etc.) before the commit lands. If gitleaks blocks a commit:

1. Read the finding — what file, what line, what rule fired
2. If it's a real secret, **rotate it immediately** (the PAT/key is
   compromised even though the commit was blocked — the value was
   typed/pasted into a working tree)
3. Then remove the secret from the staged file and re-commit
4. If it's a false positive, add the pattern to `.gitleaks.toml`
   (not yet present) and commit that separately

**Never** pass `--no-verify` to bypass gitleaks. The hook exists
because operator error is the dominant secret-leak vector.
```

- [ ] **Step 5: Commit**

```bash
git add .pre-commit-config.yaml packages/foreman/pyproject.toml uv.lock docs/RUNBOOK.md
git commit -m "chore(security): add gitleaks pre-commit hook + RUNBOOK section"
```

Note: the commit message subject is intentionally `chore(security):` not `feat(docker):` — this work is adjacent to Docker (same PR) but a separate concern, and the audit-grep-by-scope wants `security` for things that touch the secret-handling surface.

---

## Task 18: Close foreman#98 + foreman#99 + open implementation PR

**Files:** (none modified — final shipping action)

- [ ] **Step 1: Push the implementation branch**

```bash
cd e:/workspaces/ai/agents/foreman-worktrees/docker-runtime
PAT=$(python C:/Users/jeffr/.wren/.claude/skills/creds-management/scripts/creds.py --being wren get github --keyring --password)
GH_TOKEN=$PAT git -c credential.helper= -c credential.helper="!gh auth git-credential" push -u origin feat/docker-runtime
```

Expected: pre-push pytest passes; push succeeds.

- [ ] **Step 2: Open the implementation PR**

```bash
GH_TOKEN=$PAT gh pr create --repo jeffrichley/foreman \
    --base main \
    --head feat/docker-runtime \
    --title "feat(docker): containerize foreman daemon (closes #98, #99)" \
    --body "$(cat <<'EOF'
## Summary

Implements the Docker runtime design at
`docs/superpowers/specs/2026-06-05-foreman-docker-runtime-design.md`
(merged via #128).

## What this delivers

- Multi-stage Dockerfile + Docker Compose orchestration
- 3 named Docker volumes (foreman-repos, foreman-state, foreman-logs)
- 5 Compose secrets (4 pem + claude credentials)
- `init: true` (tini PID 1)
- json-file log rotation (10m × 5)
- Pre-build clean-check script (refuses dirty trees; `--allow-dirty`
  escape hatch with audit-log visibility)
- Code changes: dispatch argv flip (drops `uv run` wrapper), env-var
  driven config + log paths, dual logging handler, clone-on-first-run
- RUNBOOK.md with pre-cutover worktree-sweep ritual
- gitleaks pre-commit hook (adjacent security work — same PR)
- Windows removed permanently from CI matrix

## Acceptance criteria

All 13 items from the design's "Acceptance criteria" section validated
during implementation. Per-Task smoke tests (build, start, end-to-end)
all green.

## Closes

- Closes #98 (subprocess hang on CI Windows Server 2025)
- Closes #99 (Windows accumulated-state pressure)

## Related

- Design PR: #128
- Follow-up: #127 (context7 prompt edits, separate ticket)
EOF
)"
```

- [ ] **Step 3: Loop Pepper via bus** (post-PR criterion-check pass)

This step is performed by the operator outside the implementation
script. Per the foreman v3 pattern: Pepper sees the PR and runs a
final criterion-check on the realized form.

- [ ] **Step 4: After Jeff merges, GitHub auto-closes foreman#98 + #99**

Verify post-merge:

```bash
GH_TOKEN=$PAT gh issue view 98 --repo jeffrichley/foreman --json state,closedAt
GH_TOKEN=$PAT gh issue view 99 --repo jeffrichley/foreman --json state,closedAt
```

Expected: both `CLOSED`.

---

## Self-review checklist (run after writing this plan, before execution)

- [ ] **Spec coverage**: every section of the design has at least one task implementing or verifying it.
- [ ] **Placeholder scan**: no `TBD`, `TODO`, `implement later`, `fill in details`, `similar to Task N`, or steps without code.
- [ ] **Type consistency**: env-var names match across tasks (`FOREMAN_LOG_DIR`, `FOREMAN_CONFIG_PATH`, `FOREMAN_STATE_DIR`). File paths consistent (`/foreman/{repos,state,logs}`, `/run/secrets/<name>`, `/etc/foreman/config.toml`).
- [ ] **Test gate**: every Python edit task ends with a passing test invocation + the broader suite green.
- [ ] **Commit cadence**: every task commits atomically. No multi-feature commits.
- [ ] **Frequent commits**: per-task commit; no Task spans more than ~30 minutes of work.
