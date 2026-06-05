#!/usr/bin/env bash
# Refresh the vendored docker/claude/ tree from the operator's host
# Claude Code config. This is a DELIBERATE update path — not a build
# step. Run it when you intentionally want to pick up a new version
# of superpowers, an updated CLAUDE.md, or a new settings.json.
#
# After running:
#   1. Review `git diff docker/claude/` carefully
#   2. Commit with a message naming what changed and why
#      (e.g. "chore(docker): refresh claude vendor — superpowers 5.2.0")
#   3. Rebuild the image: ./scripts/build-docker.sh
#
# The daemon's behavior is pinned to whatever git SHA the image was
# built from. There is NO automatic propagation of host changes.
#
# IMPORTANT: user-scope skills (~/.claude/skills/) are NOT vendored.
# At first-vendor time (Task 4 in the docker-runtime PR), the host's
# skills tree was 948 MB — dominated by gstack's browser binaries and
# model caches — and the foreman daemon's roles don't reference them
# (each role loads its own prompt from packages/foreman/src/foreman/
# prompts/*.md). If a future role DOES need a specific skill, vendor
# it explicitly under docker/claude/skills/<skill-name>/ with a commit
# that names the skill and why.
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

# User-scope skills are intentionally NOT refreshed. See the header
# comment above. Print a reminder so operators don't quietly miss it.
if [[ -d ~/.claude/skills ]]; then
    host_skills_count=$(find ~/.claude/skills -maxdepth 1 -mindepth 1 -type d | wc -l | tr -d ' ')
    if [[ "$host_skills_count" -gt 0 ]]; then
        echo "  skipped: ${host_skills_count} user-scope skills on host (vendor explicitly per-skill if needed)"
    fi
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

# .mcp.json is intentionally NOT refreshed from host. The host's MCP
# config (in ~/.claude.json) wraps commands with `cmd /c` for Windows
# compatibility; the container is Linux and needs the bare `npx` form.
# Edit docker/claude/.mcp.json by hand if the trimmed-to-context7
# block needs to change.

# Secret scan. We scan ONLY the foreman-controlled files (CLAUDE.md,
# settings.json, .mcp.json). Superpowers docs are third-party content
# that frequently uses the credential vocabulary descriptively ("token
# usage", "credential logging test", etc.) and would generate a flood
# of false positives — vendored upstream content goes through whatever
# review superpowers itself does.
echo "==> Scanning foreman-controlled vendor files for accidental credential leaks"
suspicious=""
for f in "$VENDOR_DIR/CLAUDE.md" "$VENDOR_DIR/settings.json" "$VENDOR_DIR/.mcp.json"; do
    [[ -f "$f" ]] || continue
    hits=$(grep -E '(api[_-]?key|secret|token|password|oauth|credential)' "$f" \
            | grep -v -E '(_env|description|EXAMPLE|placeholder|tokens|tokenLimit|usingCacheTokens)' \
            | grep -v -E '(treat as a credential|secret scanning|contain secrets|API keys|staging secrets|like they contain)' \
            || true)
    if [[ -n "$hits" ]]; then
        suspicious="$suspicious"$'\n'"--- $f ---"$'\n'"$hits"
    fi
done
if [[ -n "$suspicious" ]]; then
    echo "WARNING: possible credentials in refreshed vendor tree:" >&2
    echo "$suspicious" >&2
    echo "Review and scrub before committing." >&2
    exit 1
fi
echo "  no suspicious patterns"

echo ""
echo "==> Done. Review changes:"
echo "    git diff docker/claude/"
echo ""
echo "    Then commit with a message naming what changed and why."
