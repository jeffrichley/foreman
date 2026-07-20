# Sandbox Clone-Freshness Architecture — Design

**Status:** approved in brainstorm (Jeff, 2026-07-20)
**Author:** Wren
**Follows:** the 2026-07-20 sandbox keystone. #406's SpecReview crashed
(`git diff origin/main...cbcf00c` → exit 128 → Failed) because the reviewer's
box couldn't see a commit a prior role had pushed.

## Problem

The bubblewrap sandbox gives each role its own private clone (foreman#556).
Across a multi-role ticket (planner → SpecReview ↔ SpecFix → worker →
ImplReview ↔ ImplFix → merge), roles hand work to each other **through the
ticket branch on GitHub**: a role commits + pushes from its box, the box is
torn down, the next role gets a fresh box. But a role's box can be **stale
relative to a prior role's push**, so it acts on an out-of-date branch — or,
as in #406, crashes because a commit it was asked to review isn't in its clone.

### The freshness chain, and where every link goes stale

1. **GitHub** — the real remote. Always current; every role's box pushes here.
2. **The daemon's base clone** (`/foreman/repos/<project>`) — the hardlink
   source for boxes. Refreshed **only for `origin/main`, at most every 300s**
   (`CloneRefresher`, #407). Its **ticket branches are never refreshed** — and
   those are exactly where multi-role work accumulates. It has also become a
   junk drawer of **stale local working branches** (`refs/heads/foreman/impl-*`)
   left over from the pre-sandbox era, when `WorktreeManager` worked directly
   in the base.
3. **The box's private clone** — `git clone --local` hardlink of the base,
   which **inherits every stale local branch**, then re-points origin at
   GitHub but **does not fetch**.
4. **The in-box worktree setup** (`WorktreeManager.attach`) — fetches the
   ticket branch **only if it isn't already local**, so a stale inherited local
   branch silently wins and the fetch is skipped.

It is not one bug; it is a chain with multiple stale links, exposed by the
sandbox's per-role isolation (the old shared-clone model hid it — every role
literally shared one working clone).

## Approach

Make **GitHub the single source of truth**, demote the base clone to a **pure
object-cache mirror**, and put the entire freshness guarantee in **one
chokepoint** so no role — present or future — needs freshness code.

Two invariants:
- **Fetch:** every box gets current remote refs before any role logic runs.
- **Right branch at the right commit:** the box works from the fetched remote
  tip, never an inherited local branch. Guaranteed *structurally* by the base
  being a mirror (nothing stale to inherit) rather than by cleanup code.

## Architecture

```
GitHub  (source of truth; every box fetches + pushes here)
  ▲  push                                   │ fetch
  │                                          ▼
daemon base clone = BARE MIRROR  ── CloneRefresher (all refs, throttled; perf-only)
  │  git clone --local  (hardlink objects; NO local branches to inherit)
  ▼
prepare_sandbox_clone  (THE CHOKEPOINT)
  1. git clone --local <mirror> <dest>      (objects, fast)
  2. git remote set-url origin <tokenized GitHub>
  3. git fetch origin                        (blanket → all refs current)
  ▼
role box:  git worktree add <branch>  →  resolves origin/<branch> (fresh)
           …commits, pushes to GitHub…
```

### 1. Base clone = bare mirror

`ensure_clone` creates the base as a **bare mirror** (`git clone --mirror`) —
objects + `refs/*` remote-tracking, **no local working branches, no working
tree**. A mirror structurally cannot pollute a box: `git clone --local
<mirror>` gives the box the objects (hardlinked) plus refs that become
`origin/*`, with no local `foreman/*` heads to shadow the fresh remote.

**Migration:** existing base clones are working clones full of stale local
branches. The base holds **no unique state** — it is fully derivable from
GitHub — so on startup `ensure_clone` detects a non-mirror base and **recreates
it as a mirror** (remove + `git clone --mirror`). Safe and idempotent.

### 2. CloneRefresher fetches the whole mirror

The refresher's job becomes purely **perf**: keep the mirror's objects warm so
each box's clone-prep fetch is a tiny delta, not a re-download of history.
Change its per-project fetch from `fetch_origin_default_branch` (main only) to
a **whole-mirror fetch** (`git remote update --prune` / `git fetch --prune
origin '+refs/*:refs/*'` appropriate to a mirror). Still throttled
(`clone_refresh_seconds`, default 300s) and **best-effort** — a stale mirror
only costs a slightly larger clone-prep fetch, never correctness (see §4).

### 3. The chokepoint: `prepare_sandbox_clone`

After the hardlink clone and the origin re-point, add **one blanket fetch**:

```
git -C <dest> fetch origin        # all refs, current GitHub state
```

Every sandboxed role is dispatched through this one function
(`subprocess_dispatcher` calls it for planner/reviewer/fixer/worker alike), so
adding a 6th role tomorrow inherits freshness with zero new code. Blanket (not
targeted) by decision: no branch name to plumb or remember, impossible to
under-fetch, and only missing objects download (the mirror seeds the rest).

### 4. Right-branch guarantee (by construction)

With the base a mirror, the box's clone has **no local `foreman/*` branches** —
only `origin/*`, made current by the chokepoint fetch. So a role's existing
`git worktree add <branch>` resolves `<branch>` to `origin/<branch>` (the fresh
tip) and creates the working branch from it. **No role code changes and no
cleanup step are needed** — the hazard is removed at the source. The reviewer's
`git diff origin/{base}...{head_sha}` now finds `head_sha` because the blanket
fetch brought it.

**Why the window is fully closed:** roles for one ticket run *serially*
(per-repo cap = 1), so the order is always `role A commits+pushes → A's box
torn down → role B dispatched → B's clone-prep fetches → B sees A's commit`.
The fetch happens after the prior push and before the next role acts.

## Error handling

- **Chokepoint fetch = fail-closed.** If `git fetch origin` fails at clone-prep,
  `prepare_sandbox_clone` raises — the role does **not** run on stale refs. The
  state machine escalates/retries. A transient network failure is worth a retry;
  silently acting on stale state is the exact bug we are killing.
- **CloneRefresher = best-effort** (unchanged discipline): swallow + log per
  project, don't advance the throttle clock. Mirror staleness is perf-only.
- **Base recreation on startup:** if the base exists but isn't a mirror,
  remove + re-mirror; if removal/clone fails, fail closed (daemon refuses to
  start with an actionable message) — a half-migrated base must not run jobs.

## Non-goals / out of scope

- **The unsandboxed fallback is not preserved.** A bare mirror has no working
  tree, so `allow_unsandboxed`'s direct-`WorktreeManager`-in-base path cannot
  operate. This is a **deliberate, documented gap**: the non-sandbox path is
  being removed soon (Jeff, 2026-07-20), and it is dormant anyway (preflight
  passes on the production host, so the escape hatch never triggers). This
  design does not carry it forward.
- Not changing how roles create/name branches, push, or open PRs — only where
  their clone's refs come from and how fresh they are.
- Not touching the merge-coordinator or per-repo serialization (relied on, not
  modified).

## Testing

- **Unit — `ensure_clone`:** creates a bare mirror (no working tree, no local
  heads); detects a non-mirror base and recreates it; fails closed on a
  corrupt/unremovable base.
- **Unit — `CloneRefresher`:** fetches all refs (not just main); still
  throttled; still best-effort per project.
- **Unit — `prepare_sandbox_clone`:** issues the blanket `git fetch origin`
  after the clone + re-point; raises on fetch failure (fail-closed).
- **Hermetic integration (real bwrap, self-skips off userns) — the keystone
  lock:** stand up a real bare-mirror base + a real GitHub-shaped remote (a
  local bare repo standing in for origin); simulate a two-role handoff —
  *role A* prepares a box, commits + pushes a new commit; *role B* prepares a
  fresh box via `prepare_sandbox_clone` and asserts the new commit **is present
  and `git diff origin/main...<sha>` succeeds**. This reproduces #406's failure
  against the old flow and locks the fix.
- **Repro-first:** the plan's first task reproduces #406's exact
  `git diff … exit 128` against the current (pre-fix) flow, so we prove we
  understand the failure before changing it.

## Rollout

The base becomes a mirror **unconditionally** (it is created at daemon startup,
not per-sandbox-mode) — safe because the only path that needs a working-tree
base is the unsandboxed fallback, which is not preserved (see Non-goals) and is
dormant on the production host. In the sandboxed production path every consumer
of the base (clone-prep, the refresher) works with a mirror. First proof is
re-running a multi-role ticket (a fresh agent_core ticket, or #406 re-driven)
and watching a SpecFix → SpecReview handoff succeed end-to-end with no
`git diff` crash — the keystone this design exists to fix.
