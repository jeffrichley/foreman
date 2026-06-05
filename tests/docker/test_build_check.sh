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
