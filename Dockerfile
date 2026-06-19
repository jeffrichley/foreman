# syntax=docker/dockerfile:1.7
# Foreman daemon image.
#
# Layer order chosen for cache locality (per the Docker design spec,
# "Image build" section):
#   1. Base OS + system deps        ← changes rarely
#   2. uv                            ← changes rarely
#   3. Claude Code CLI (npm)         ← changes occasionally
#   4. context7 MCP server (npm)     ← changes occasionally
#   5. Python dependency layer       ← changes when pyproject.toml/uv.lock change
#   6. Foreman source                ← changes frequently (only this rebuilds on edits)
#   7. Claude config (skills, etc.)  ← changes occasionally
#   8. Foreman config + entrypoint   ← changes rarely
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
        gettext-base \
    && rm -rf /var/lib/apt/lists/*
# gettext-base provides ``envsubst``; the entrypoint uses it to expand
# ${FOREMAN_*_APP_ID} placeholders in /etc/foreman/config.toml.template
# into the runtime V4Config file at $FOREMAN_V4_CONFIG.

# --- uv -----------------------------------------------------------------
RUN pip install --no-cache-dir uv

# --- Claude Code CLI ----------------------------------------------------
# claude_agent_sdk shells out to `claude` for the actual LLM call.
# Pin to @latest for v1; bump intentionally if behavior changes.
RUN npm install -g @anthropic-ai/claude-code

# --- context7 MCP server (warm npm cache for runtime npx) --------------
# docker/claude/.mcp.json registers context7 via `npx -y @upstash/context7-mcp@latest`.
# Pre-installing globally here puts the package in npm's cache so the
# per-dispatch `npx` call hits the warm cache instead of fetching from
# the registry every time a role subprocess starts.
RUN npm install -g @upstash/context7-mcp

# --- Python dependency layer ------------------------------------------
# Strategy: use `uv export` to emit a deps-only requirements file from
# the manifest, then `uv pip install -r` it. This avoids needing the
# source package present in the dep layer (would fail `uv pip install .`).
# This is pattern (a) from the design's Image build IMPLEMENTATION NOTE.
#
# Foreman is a uv workspace: the lockfile + workspace declaration live
# at the repo ROOT (pyproject.toml + uv.lock), and per-package manifests
# live under packages/<name>/pyproject.toml. We need ALL THREE for
# `uv export` to resolve workspace members correctly.
WORKDIR /tmp/build
COPY pyproject.toml uv.lock ./
COPY packages/foreman/pyproject.toml ./packages/foreman/
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
# v4: the config.toml is rendered at container start by envsubst from
# this template (App IDs come from .env -> compose -> container env).
# The rendered file lives at /foreman/state/config.toml (the
# foreman-state volume) so it persists across container restarts.
COPY docker/foreman/config.toml.template /etc/foreman/config.toml.template
COPY docker/entrypoint.sh /entrypoint.sh
# Strip CRLF defensively: .gitattributes locks LF on .sh files going forward,
# but a developer with an existing working tree (autocrlf=true) may COPY a
# CRLF-tainted entrypoint into the image. The shebang then reads `#!/bin/bash\r`
# and exec fails with `bash\r: No such file or directory`. Belt + suspenders.
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

# Ensure /foreman volume mount points exist (volumes mount over these)
RUN mkdir -p /foreman/repos /foreman/state /foreman/logs

WORKDIR /app/source

CMD ["/entrypoint.sh"]
