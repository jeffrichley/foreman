# foreman

Multi-identity GitHub-issue-to-PR orchestrator on agent-core substrate.

## What it does

foreman watches a GitHub repo for issues labeled `foreman:plan` and walks
each one through a multi-node pipeline that produces a merged PR. The
daemon auto-transitions `foreman:plan` to `foreman:planning` on the next
poll, so you only ever set the entry label by hand:

1. **Planner** drafts a spec PR from the issue
2. **Reviewer** reviews the spec PR with fresh-eyes independence
3. **Fixer** (if needed) applies review findings
4. **Worker** implements the approved spec

Each role runs as a distinct GitHub identity (4 bot accounts via Gmail
plus-aliasing), so GitHub's no-self-approval rule is satisfied naturally
and the repo audit trail shows exactly which role did what.

## Status

v1 walking skeleton in progress. See
`docs/superpowers/specs/foreman-v1-architectural-spec.md` for the locked-
decisions spec.

## Architecture (one-paragraph version)

GitHub labels drive a 9-state machine. The daemon polls every 30s. On
state advance, the relevant role is dispatched via a provider facade
(Anthropic Agent SDK is the first provider) inside a per-ticket git
worktree at `~/.foreman/worktrees/<repo>/<ticket-id>/`. Tool capabilities
are scoped per-role (Reviewer is read-only on files; Worker can push
commits). Lifecycle events persist to a local SQLite database; a foreman
MCP server exposes query tools for cross-being observation. Add projects
via `foreman init` then call `foreman daemon reload` to register them
with the running daemon (no restart needed).

## Working in this repo

`just check` for the full quality gate. See `CLAUDE.md` for the working
conventions.

## Architectural boundaries

foreman uses [`import-linter`](https://import-linter.readthedocs.io) to
enforce architectural boundaries at the import-graph level. `just check`
runs it transitively. See `docs/RUNBOOK.md` § Import-graph boundaries for
how to run it locally, read failure output, and add new rules.
