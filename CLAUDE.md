# foreman

Multi-identity GitHub-issue-to-PR orchestrator on agent-core substrate.

## Working in this repo

- `uv sync` to install / refresh dependencies
- `just check` runs the full quality gate (lint + typecheck + tests)
- `just fix` applies ruff auto-fixes + formatting
- `.githooks/pre-push` runs `just check` before push; emergency bypass: `git push --no-verify` (use sparingly)

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

## Architecture

See `docs/superpowers/specs/foreman-v1-architectural-spec.md` for the full v1
locked-decisions spec (state machine, role architecture, identity model,
provider facade, walking-skeleton-first build order).
