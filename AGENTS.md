# Agent guidelines for foreman

This file documents conventions for AI agents (Claude Code, Codex, etc.)
working in this repo.

## Required reads before substantial changes

- `README.md` — project overview
- `CLAUDE.md` — repo-specific working conventions
- `docs/superpowers/specs/` — design specs for any in-flight feature work

## Conventions

- **Conventional commits** — PR titles enforced by CI (`pr-title-lint`).
- **Pre-push gate** — `.githooks/pre-push` runs `just check`. Don't skip.
- **Specs before code** — non-trivial features land a design doc in
  `docs/superpowers/specs/` before the implementation PR.
- **Squash-merge only** — repo is configured for squash-merge; the PR
  description becomes the commit body.

## Foreman-specific

- foreman IS the multi-identity orchestrator. Per-role identity is enforced
  by the per-bot PyGithub clients in `identity.py`. NEVER hard-code a single
  GitHub token at the daemon level — every gh/PyGithub call must route
  through the role's identity.
- All file operations during a pipeline scope to the per-ticket worktree at
  `~/.foreman/worktrees/<repo>/<ticket-id>/`. NEVER edit files in the main
  cloned repo working directory.
- Hard blocklist for ALL roles: `gh repo delete`, `git push --force`. These
  fail the call; if a role genuinely needs to rewrite history, it fails back
  to human.
