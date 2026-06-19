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
check: lint typecheck import-lint test

# Developer convenience: apply lint auto-fixes + formatter
fix:
    uv run --no-sync ruff check --fix packages/foreman
    uv run --no-sync ruff format packages/foreman

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
