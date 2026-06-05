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
