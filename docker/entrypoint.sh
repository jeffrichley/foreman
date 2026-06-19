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
#   FOREMAN_LOG_DIR, FOREMAN_STATE_DIR, FOREMAN_*_APP_ID, etc.
#   FOREMAN_CONFIG_TEMPLATE — path to the baked envsubst-able TOML
#       (default /etc/foreman/config.toml.template). Rendered at startup
#       into ``$FOREMAN_V4_CONFIG`` (default /foreman/state/config.toml).
#   IMAGE_SHA, ALLOW_DIRTY — build args surfaced as env vars
#
# Exits:
#   0 — daemon exited cleanly (only on shutdown signal)
#   non-zero — daemon crashed or setup failed
set -euo pipefail

# --- Claude credentials plumbing ----------------------------------------
# Compose secrets default to 0400 root-only. Copy to the SDK's expected
# location with 0600 so it can write a refreshed token.
#
# foreman#227 (2026-06-08): the initial copy goes stale after the host
# rotates the OAuth token (~hourly). Background a refresh loop that
# re-copies from the live bind-mounted secret whenever the host file is
# newer than our local copy. Runs every 5 minutes; cheap (one stat +
# possibly one copy). The reactive layer that handles in-flight 401s
# lives in providers/anthropic_sdk.py — this is just the proactive belt.
CLAUDE_DIR=/root/.claude
CLAUDE_SECRET=/run/secrets/claude_credentials
if [[ -r "$CLAUDE_SECRET" ]]; then
    mkdir -p "$CLAUDE_DIR"
    install -m 0600 "$CLAUDE_SECRET" "$CLAUDE_DIR/.credentials.json"
else
    echo "ERROR: $CLAUDE_SECRET not readable — Compose secret missing or perms wrong" >&2
    exit 1
fi

# Background periodic refresh. Detached via `&` and `disown` so SIGTERM
# from `docker stop` cascades to the daemon (PID 1 child) and this loop
# dies when the container does. Single-line JSON output lets the daemon's
# structured log driver pick up refresh events.
(
    while true; do
        if [[ "$CLAUDE_SECRET" -nt "$CLAUDE_DIR/.credentials.json" ]]; then
            install -m 0600 "$CLAUDE_SECRET" "$CLAUDE_DIR/.credentials.json"
            printf '{"event":"creds_refreshed","at":"%s"}\n' \
                "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
        fi
        sleep 300
    done
) &
disown

# --- Render the v4 config from the template -----------------------------
# v4 ``V4Config`` (foreman.v4.config) takes integer ``app_id`` values
# directly — no env-var indirection. The image baked
# ``/etc/foreman/config.toml.template`` contains ``${VAR}`` placeholders
# for each App ID; envsubst expands them at startup so the App IDs stay
# in operator-owned ``.env`` rather than baked into the image.
#
# Write the rendered file to a writable path (the volume-attached state
# dir) so the SQLAlchemyDataStore + daemon can write next to it.
FOREMAN_CONFIG_TEMPLATE="${FOREMAN_CONFIG_TEMPLATE:-/etc/foreman/config.toml.template}"
FOREMAN_V4_CONFIG="${FOREMAN_V4_CONFIG:-/foreman/state/config.toml}"
mkdir -p "$(dirname "$FOREMAN_V4_CONFIG")"
envsubst < "$FOREMAN_CONFIG_TEMPLATE" > "$FOREMAN_V4_CONFIG"
export FOREMAN_V4_CONFIG

# --- Startup banner -----------------------------------------------------
# IMAGE_SHA + ALLOW_DIRTY come in as build args via the Dockerfile.
# Print as a single JSON line so the daemon's structured log driver
# captures it cleanly.
printf '{"event":"container_start","image_sha":"%s","allow_dirty":%s,"foreman_v4_config":"%s","foreman_log_dir":"%s"}\n' \
    "${IMAGE_SHA:-unknown}" \
    "${ALLOW_DIRTY:-false}" \
    "$FOREMAN_V4_CONFIG" \
    "${FOREMAN_LOG_DIR:-/foreman/logs}"

# --- Hand off to the daemon ---------------------------------------------
# `exec` so SIGTERM from `docker stop` lands directly on the daemon,
# not on this shell. v4 entry point is ``foreman.v4.cli:main``; the
# ``daemon start`` subcommand was registered in Phase 6.6 as the
# replacement for v3's ``daemon v3-start``.
exec foreman daemon start
