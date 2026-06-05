# gstack

Use the `/browse` skill from gstack for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

Available gstack skills:
- `/office-hours` — structured engineering office hours
- `/plan-ceo-review` — prepare a plan for CEO review
- `/plan-eng-review` — prepare a plan for engineering review
- `/plan-design-review` — prepare a plan for design review
- `/design-consultation` — get design consultation
- `/review` — code review
- `/ship` — ship a feature
- `/land-and-deploy` — land and deploy changes
- `/canary` — canary deployment
- `/benchmark` — run benchmarks
- `/browse` — web browsing (use this for all web browsing)
- `/qa` — QA testing
- `/qa-only` — QA only (no dev)
- `/design-review` — design review
- `/setup-browser-cookies` — set up browser cookies
- `/setup-deploy` — set up deployment
- `/retro` — retrospective
- `/investigate` — investigate an issue
- `/document-release` — document a release
- `/codex` — codex tasks
- `/cso` — CSO review
- `/autoplan` — automatic planning
- `/careful` — careful mode
- `/freeze` — freeze deployments
- `/guard` — guard mode
- `/unfreeze` — unfreeze deployments
- `/gstack-upgrade` — upgrade gstack

## GBrain Configuration (Windows install + Supabase cutover, 2026-04-28)
- Engine: postgres (Supabase) — cut over from PGLite same day to enable
  cross-machine work between Windows desktop and Mac laptop.
- Config file: ~/.gbrain/config.json (mode 0600 by gbrain). Holds the pooler
  URL — treat as a credential.
- gbrain repo: ~/gbrain (v0.22.6.1, `bun link` for global CLI).
- MCP registered: yes (user scope) with `-e OPENAI_API_KEY=sk-...` so the
  gbrain subprocess can embed inline on `put_page`.
- Second-machine setup: see brain page `gbrain-second-machine-setup` (use
  `mcp__gbrain__get_page` with that slug). Same Supabase URL on Mac via
  `read -s GBRAIN_DATABASE_URL && gbrain init --non-interactive`.
- gstack session memory: `~/.gstack/` is a git repo synced to private GitHub
  repo `jeffrichley/gstack-brain-jeffrichley`. Federated into gbrain as
  source `gstack-brain-jeffrichley` so artifacts (CEO plans, retros, design
  docs) are searchable cross-machine via `mcp__gbrain__search`. Privacy mode:
  `artifacts-only`. On a new machine, restore via `gstack-brain-restore`
  (needs `~/.gstack-brain-remote.txt` copied over first).
- python3 wrapper: `~/.local/bin/python3` is a bash shim that forwards to
  `python` (pyenv-win has no real python3 binary). The gstack-brain
  pre-commit hook needs python3 for secret scanning.
- Free Supabase tier note: pauses after 7 days of inactivity, ~30s to wake
  on the next query. Pro avoids this.
- Bun-on-Windows postinstall quirk: `bun install` in ~/gbrain prints a benign
  shell parse error on the postinstall hook. Migrations run fine via `gbrain
  init` and `gbrain apply-migrations --yes`.

# Git preferences

- **Commit frequently** as focused units of work complete. Don't ask
  permission for each commit — just make the commit when there's a coherent
  chunk done. Default to small, atomic commits with conventional-commits-style
  messages (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`).
- **One concern per commit.** Don't bundle unrelated changes. If multiple
  unrelated chunks are uncommitted, make multiple commits.
- **Match the repo's existing convention** for `Co-Authored-By` lines — check
  recent commits via `git log -3 --format='%B'`. If existing commits have a
  `Co-Authored-By: Claude` trailer, include one; if not, omit it.
- **Stage specific files**, not `git add -A` or `git add .`, to avoid
  accidentally staging secrets or large binaries.

**Still always ask** before destructive operations: `git push --force` to any
shared branch, `git reset --hard`, amending a commit that's already been
pushed, deleting branches, dropping stashes. The "commit freely" rule does
NOT extend to history rewriting or destructive ops.

**Still always refuse** to commit files that look like they contain secrets
(`.env`, `credentials.json`, `*.pem`, anything with API keys) without an
explicit override from me.
