# Justfile — single quality-command surface for foreman workspace.
#
# Keep this as the source of truth for local + CI check commands so
# developers and agents run the same gates in the same order.

set windows-shell := ["cmd.exe", "/c"]

# Workspace package scopes
pkg-src := "packages/foreman/src"
pkg-tests := "packages/foreman/tests"

default:
    @just --list

# Composite gate (recommended before push)
check: lock-check lint typecheck import-lint test

# Developer convenience: apply lint auto-fixes + formatter
fix:
    uv run --no-sync ruff check --fix packages/foreman
    uv run --no-sync ruff format packages/foreman

# Validate uv.lock parses cleanly and is up-to-date with pyproject.toml.
# Cheap fail-fast gate so a malformed lockfile (e.g. duplicate package
# blocks from a stitched merge) is rejected pre-push instead of in CI.
lock-check:
    uv lock --check

# Lint
lint:
    uv run --no-sync ruff check packages/foreman

# Type-check
typecheck:
    uv run --no-sync mypy packages/foreman/src

# Import-boundary linter (Decision 7 / foreman#311 + foreman#318 R2).
# PYTHONPATH lets grimp's graph walker see "tests" as a top-level
# package alongside "foreman" so the R1 contract resolves. The recipe
# splits per-OS because the justfile uses `set windows-shell :=
# ["cmd.exe", "/c"]` and cmd.exe doesn't parse the `VAR=value cmd` shape.
[unix]
import-lint:
    PYTHONPATH=packages/foreman uv run --no-sync lint-imports

[windows]
import-lint:
    set PYTHONPATH=packages/foreman && uv run --no-sync lint-imports

# Tests
test:
    uv run --no-sync pytest

# Build wheels locally (sanity-check that release.yml would succeed)
build:
    uv build --all-packages --wheel --out-dir dist/

# Rebuild + relaunch the daemon container from the current working
# tree. Use this when CI/Watchtower are unavailable (offline dev,
# GHCR outage) OR when you need to test an uncommitted change
# without going through PR + merge. For the normal flow the daemon
# auto-updates from GHCR via Watchtower (foreman#363).
rebuild-daemon:
    ./scripts/build-docker.sh
    docker compose up -d daemon
