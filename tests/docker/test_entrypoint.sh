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
