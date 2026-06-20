#!/usr/bin/env bash
# Foreman daemon container entrypoint.
#
# Responsibility:
#  1. Validate that the Anthropic OAuth token env var is set (fail loud
#     at startup rather than crashing on first API call).
#  2. Print a startup banner naming the image SHA and the
#     --allow-dirty flag (loud audit per the design's --allow-dirty
#     visibility item).
#  3. Exec the daemon as PID 1's child (tini becomes PID 1 via
#     init: true in compose, so this script's PID doesn't matter
#     for zombie reaping; we just need to exec so signals propagate
#     cleanly).
#
# Inputs (from compose):
#   CLAUDE_CODE_OAUTH_TOKEN — long-lived Anthropic Max OAuth token
#       (generate via `docker run --rm -it foreman:dev claude setup-token`;
#       persist in .env as FOREMAN_CLAUDE_OAUTH_TOKEN; lasts ~1 year)
#   CLAUDE_CONFIG_DIR — path the container's claude CLI uses for any
#       private state writes (compose sets /root/.claude-container).
#       Isolates the container from /root/.claude/ which carries the
#       image-baked CLAUDE.md, settings.json, .mcp.json, skills, plugins.
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

# --- Anthropic auth validation ------------------------------------------
# foreman#392: the container authenticates entirely via the
# CLAUDE_CODE_OAUTH_TOKEN env var, NOT via a shared credentials.json
# file. Fail loud at startup if the token is missing so operators see
# the cause immediately instead of debugging a 401 on the first role
# dispatch ~minutes into the run.
if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
    cat >&2 <<'ERR'
ERROR: CLAUDE_CODE_OAUTH_TOKEN is not set.

The container authenticates to Anthropic via a long-lived OAuth token
(superseded the prior shared-credentials.json shape; see foreman#392).

To generate one:
    docker run --rm -it foreman:dev claude setup-token

Paste the printed token into your .env file as:
    FOREMAN_CLAUDE_OAUTH_TOKEN=<paste-token-here>

Then restart the daemon:
    docker compose up -d daemon
ERR
    exit 1
fi

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
