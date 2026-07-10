# foreman

Multi-identity GitHub-issue-to-PR orchestrator on agent-core substrate.

## Working in this repo

- `uv sync` to install / refresh dependencies
- `just check` runs the full quality gate (lint + typecheck + tests)
- `just fix` applies ruff auto-fixes + formatting
- `.githooks/pre-push` runs `just check` before push; emergency bypass: `git push --no-verify` (use sparingly)

## Running tests

`just check` runs the full test gate: random order (pytest-randomly), parallel
execution across CPU cores (pytest-xdist `-n auto`), 60-second per-test
wallclock cap (pytest-timeout), and a global coverage gate of 80%
(pytest-cov). Per-PR patch coverage is separately gated at 80% via diff-cover
in CI (`.github/workflows/ci.yml`).

Useful flags for tight inner loops (pass them to `pytest` directly, not
`just check`):

- `--no-cov` skips coverage measurement when you want a fast pass/fail
- `--randomly-seed=<N>` reproduces a specific test order — the seed prints at
  the top of every run, so copy it from a failing run to reproduce
- `-p no:randomly` disables randomization entirely (use only as a diagnostic
  to confirm an ordering bug; do not commit code that depends on this)

The 80% global cov-fail-under is a regression floor, not an aspirational
target — it stays slightly below measured coverage to leave room for normal
refactoring. Ratcheting it upward happens in dedicated test-debt tickets, not
in PRs that didn't intentionally add tests.

## Release

This project uses release-please. Conventional-commit messages on merged PRs
become CHANGELOG entries. release-please opens a PR with the next version's
release notes; merging it tags the release. `release.yml` then builds the
wheels and uploads them to the GitHub Release.

If `release.yml` doesn't auto-fire after release-please tags (GitHub's
GITHUB_TOKEN anti-recursion guard), trigger it manually:

```bash
gh workflow run release.yml -f tag=<the-tag>
```

OR toggle the release draft state to refire `release.published`:

```bash
gh release edit <tag> --draft
gh release edit <tag> --draft=false
```

## Conventions

- Conventional commits required (PR titles enforced by `.github/workflows/pr-title-lint.yml`)
- Subject must NOT start with an uppercase letter
- All allowed types: feat, fix, chore, docs, refactor, test, style, build, ci, perf, revert
- Module-level collection constants must be immutable (`frozenset`/`tuple`/`MappingProxyType`)

## Design

Calibrated bias toward structural patterns (per Decision 4 of `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md`):

> Before proposing a non-trivial design, name the GoF pattern and/or the Google engineering principle (SRP / OCP / DIP / "make the right thing easy") the design embodies. If neither applies cleanly, say so explicitly — "no pattern fits, this is straightforward X" is a legitimate output. Pattern-fishing produces worse code than no pattern at all.

## Architecture

See `docs/superpowers/specs/foreman-v1-architectural-spec.md` for the full v1
locked-decisions spec (state machine, role architecture, identity model,
provider facade, walking-skeleton-first build order).