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
# NOTE: worker.pem deliberately NOT stubbed — the production compose file
# does not reference it (see compose comment). When Worker bot setup lands
# in a follow-up ticket, add `worker` to this loop AND add the
# worker_pem secret block to docker-compose.yml.
for r in planner reviewer fixer orchestrator; do
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
grep -q 'orchestrator_pem' "$tmp/resolved.yml" || { echo "FAIL: orchestrator_pem secret missing"; exit 1; }
grep -q 'claude_credentials' "$tmp/resolved.yml" || { echo "FAIL: claude_credentials secret missing"; exit 1; }
grep -q 'max-size' "$tmp/resolved.yml" || { echo "FAIL: log rotation max-size missing"; exit 1; }

# Worker pem is INTENTIONALLY absent until follow-up ticket
if grep -q 'worker_pem' "$tmp/resolved.yml"; then
    echo "FAIL: worker_pem in compose but Worker bot PEM not yet set up (see follow-up ticket)"
    exit 1
fi

echo "PASS: compose config resolves with all required pieces (worker_pem deferred)"
