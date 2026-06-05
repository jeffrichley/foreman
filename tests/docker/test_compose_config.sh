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

# Stub the .env file the compose service requires. Real .env is
# gitignored, populated from .env.example by the operator at deploy.
# We write a stub at the repo root and clean it up via the trap below.
needs_env_cleanup=false
if [[ ! -f .env ]]; then
    cat > .env <<'STUB'
FOREMAN_ORCHESTRATOR_APP_ID=test-stub-orchestrator
FOREMAN_PLANNER_APP_ID=test-stub-planner
FOREMAN_REVIEWER_APP_ID=test-stub-reviewer
FOREMAN_FIXER_APP_ID=test-stub-fixer
FOREMAN_WORKER_APP_ID=test-stub-worker
FOREMAN_CONFIG_PATH=/etc/foreman/config.toml
FOREMAN_LOG_DIR=/foreman/logs
FOREMAN_STATE_DIR=/foreman/state
STUB
    needs_env_cleanup=true
fi
trap '[[ "$needs_env_cleanup" == "true" ]] && rm -f "$REPO_ROOT/.env"; rm -rf "$tmp"' EXIT

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
grep -q 'orchestrator_pem' "$tmp/resolved.yml" || { echo "FAIL: orchestrator_pem secret missing"; exit 1; }
grep -q 'claude_credentials' "$tmp/resolved.yml" || { echo "FAIL: claude_credentials secret missing"; exit 1; }
grep -q 'max-size' "$tmp/resolved.yml" || { echo "FAIL: log rotation max-size missing"; exit 1; }

echo "PASS: compose config resolves with all required pieces"
