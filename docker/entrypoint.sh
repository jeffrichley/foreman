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
