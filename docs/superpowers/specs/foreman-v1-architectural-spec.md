# Foreman v1 — design spec (locked)

> Pre-code spec capturing the architectural decisions locked in conversation
> Saturday 2026-05-30 between Jeff and Wren. Supersedes the v1 sketch.
> Author: Wren · Date: 2026-05-30 · Status: locked, ready for scaffolding

## 1. Vision

**Foreman v1 orchestrates one GitHub-issue-to-merged-PR cycle end-to-end on
agent-core substrate, with multi-identity baked in.** Plan B replacement for
Looper after Saturday's smoke test (voice#8/#9) exposed coordinator, identity,
and trigger gaps.

User story (the demand-validation case):

1. Jeff (or Wren) labels a GitHub issue `foreman:plan`
2. Foreman daemon discovers it on next 30s poll
3. **Planner** drafts a spec PR (planning doc), advances label to `foreman:spec-review`
4. **Reviewer** reads the spec PR independently, advances to `foreman:spec-ready` (clean)
   or `foreman:spec-fix` (findings)
5. **Fixer** (only on `foreman:spec-fix`) applies review findings, advances back to `foreman:spec-review` for re-review
6. **Worker** discovers `foreman:spec-ready`, implements onto the same branch,
   advances label to `foreman:ready-for-merge`
7. Human merges via `gh pr merge` or GitHub UI. No auto-merge in v1.

## 2. Architecture

### 2.1 Roles & responsibilities

Four roles, each a distinct identity:

| Role | What it does | Authoring identity |
|---|---|---|
| **Planner** | Reads issue → drafts spec PR | `foreman-planner-bot` |
| **Reviewer** | Reads spec PR with fresh eyes → clean/findings | `foreman-reviewer-bot` |
| **Fixer** | Applies reviewer findings → pushes to same PR | `foreman-fixer-bot` |
| **Worker** | Implements approved spec → pushes to same PR | `foreman-worker-bot` |

**No separate coordinator.** Each role self-advances its own next-state label
on completion. Eliminates the coordinator-out-of-sync bugs that bit us in
Looper.

### 2.2 State machine

9 explicit states + 2 modifier labels. Tag always shows where a ticket is.

| Label | Set by | Triggers next role |
|---|---|---|
| `foreman:plan` | Human | Planner |
| `foreman:planning` | Daemon (on Planner start) | (in-flight indicator) |
| `foreman:spec-review` | Planner (on spec PR creation) | Reviewer |
| `foreman:spec-fix` | Reviewer (on findings) | Fixer |
| `foreman:spec-ready` | Reviewer (on clean review) | Worker |
| `foreman:implementing` | Daemon (on Worker start) | (in-flight indicator) |
| `foreman:impl-review` | Worker (on impl PR ready) | Reviewer (impl pass — v1.5) |
| `foreman:impl-fix` | Reviewer (on impl findings — v1.5) | Fixer |
| `foreman:ready-for-merge` | Worker (impl complete) | Human |
| `foreman:hold` | Human (modifier) | Pauses NEW node starts; in-flight continues |
| `foreman:failed` | Daemon (modifier) | Pipeline halted; human inspection required |

Transitions are explicit; review↔fix loops happen at the STATE level (label
change), never inside a node ("ping-pong inside a state" rejected).

### 2.3 Identities & GitHub auth

**4 GitHub Apps** (one per role) instead of bot accounts. Each App installs
on a project repo and produces a distinct `[bot]` identity for commits and
PRs:

| Role | App handle (post-install) |
|---|---|
| Planner | `foreman-planner[bot]` |
| Reviewer | `foreman-reviewer[bot]` |
| Fixer | `foreman-fixer[bot]` |
| Worker | `foreman-worker[bot]` |

**Why GitHub Apps over bot accounts + PATs:** the bot-invitation-acceptance
flow doesn't scale (every new project repo requires manually accepting four
collaborator invites under four separate Gmail accounts). GitHub Apps
install once per repo from a single org-level dashboard.

**Per-role credentials** stored in two pieces:

| Piece | Storage | Notes |
|---|---|---|
| App id (numeric) | `apps.<role>_app_id` in `~/.foreman/config.toml`, overridable by env var `FOREMAN_<ROLE>_APP_ID` | Not secret on its own |
| Private key (RSA PEM) | Filesystem path in `apps.<role>_private_key_path`; default `~/.foreman/keys/<role>.pem` (chmod 600) | The actual secret |

At runtime, `foreman.auth.mint_installation_token` signs a 10-minute JWT
with the App's private key, looks up the App's installation id for the
target repo, and exchanges the JWT for a 1-hour installation token. The
`IdentityRegistry` caches that token per role and auto-refreshes when
within 5 minutes of expiry. The installation-token string is what gets
injected into the agent subprocess as `GH_TOKEN` so the agent's `gh` CLI
runs as the App's bot identity.

Wren's own PAT is NOT used by Foreman. Wren = my identity only; the Apps =
Foreman's automation identities. This preserves audit-trail clarity.

A 5th identity is needed for `foreman project add` (admin/setup ops):
Jeff's own PAT, used only for repo-admin actions (installing the four
Foreman Apps onto a new repo, label creation). Read from
`FOREMAN_ADMIN_TOKEN` env var.

### 2.4 Worktrees

**Per-ticket worktree at `~/.foreman/worktrees/<repo>/<ticket-id>/`**, created
on Planner entry, destroyed on pipeline completion.

- All roles for one ticket share the same worktree (sequential — no concurrency
  conflict)
- Branch convention: `foreman/issue-<N>` — all node commits land here
- On `foreman:ready-for-merge`: cleanup deferred until human merges the PR
- On `foreman:failed`: worktree left in place for human inspection; operator
  runs `foreman worktree clean <ticket>` after diagnosis

### 2.5 Two facades: Provider + GitHostProvider

Foreman core sits behind **two abstractions**, each isolating a different
axis of swap-ability.

#### Provider facade (LLM vendor seam)

Single interface that all role modules dispatch through; first concrete
implementation is the **Anthropic Agent SDK** (`claude-agent-sdk` Python
package).

Per-role agent dispatch:

```python
result = provider.run_agent(
    system_prompt=role_prompt,
    user_prompt=ticket_context,
    tools=role_tool_set,         # see §4.1
    output_schema=role_output_schema,
    working_dir=worktree_path,
)
```

Prompt caching wired from day one (system prompt + repo context).

Vendor swappable in theory (`opencode`, `codex`, etc. via thin adapters), but
YAGNI for v1.

#### GitHostProvider facade (git-host seam)

`GitHostProvider` is the abstraction over the **git hosting platform**
(GitHub today; GitLab / Bitbucket later). It owns every deterministic
host-platform operation that any role needs:

| Method | Used by |
|---|---|
| `get_issue(repo, n) -> IssueRef` | Planner |
| `get_default_branch(repo) -> str` | Planner / Worker / Fixer |
| `configure_worktree_identity(wt)` | Planner / Worker / Fixer |
| `commit_files_to_worktree(wt, files, msg) -> sha` | Planner / Worker / Fixer |
| `push_branch(wt, branch)` | Planner / Worker / Fixer |
| `open_pull_request(repo, title, body, base, head) -> PRRef` | Planner |
| `update_issue_labels(repo, n, add, remove)` | All roles |

The LLM never sees this surface. Per the "Looper pattern" (Foreman issue
#8): the LLM returns spec content + metadata; **core** dispatches the
side-effecting host operations. This pattern is why the Planner LLM can
drop down to a Read/Glob/Grep-only tool surface (see §4.1) — it has no
need for `Bash` or `Edit/Write`.

Each `GitHostProvider` instance is bound to one role's `BotIdentity`
(slug + numeric App id + installation token). Token refresh stays inside
`IdentityRegistry`; the provider just consumes whatever fresh identity it
was constructed with.

The GitHub implementation combines PyGithub (API operations) with
subprocess git (worktree operations). Push auth uses the GitHub-Apps
HTTPS-URL convention `https://x-access-token:<token>@github.com/...` and
commit attribution uses `<slug>[bot]` + the noreply email pattern, both
verified in the App-auth spike script.

A future `GitLabProvider` is a clean drop-in — same method surface, different
implementation. Role dispatchers and LLM prompts stay untouched.

## 3. Operations

### 3.1 Polling & daemon loop

**30-second git poll, single loop, configurable.** No DB-fast/git-slow split
(internal handoffs are synchronous function-returns, not polled — the only
thing being polled is external label state).

Webhooks deferred to v2 if/when polling latency or rate limits become real
constraints.

### 3.2 Failure & hold semantics

**`foreman:failed`** is a multi-label addition (not a replacement) on top of
whatever state the ticket was in. A failure comment is posted to the PR/issue
naming the failed role + reason. Human inspection required to clear.

**`foreman:hold`** blocks the daemon from starting NEW node work on that
ticket. Work already in flight (a Planner subagent mid-run) completes normally
and advances the label normally. Removing `foreman:hold` resumes from current
state — the daemon picks up wherever the label is.

**Concurrency: serial. One ticket processed at a time.** Multi-ticket
concurrency deferred to a future version.

### 3.3 Bus integration (v2)

Foreman does NOT publish lifecycle events to agent-core's bus in v1. Reason:
publishing standard envelopes would wake subscribers (Pepper), and these are
observation events, not interrupts. Adding a `wake=false` bus capability is
real work in agent-core that we'd be doing without knowing what queries
subscribers actually want.

**v1 path:** lifecycle persists to local SQLite at `~/.foreman/foreman.sqlite`.
A Foreman MCP exposes query tools (`list_events`, `recent_pipelines`,
`pipeline_detail`) so Wren / Pepper / Jeff can query on demand without wake
events.

**v2 path:** after we've seen what queries Pepper actually runs, design the
proper bus filter capability and add publishing as a backward-compatible
extension. SQLite events are the source we'd replay through the new capability.

### 3.4 Lifecycle storage (SQLite)

`~/.foreman/foreman.sqlite` holds:

- **`pipelines`** table — ticket → current state → start/end timestamps
- **`node_runs`** table — every node invocation: role, identity, started_at,
  finished_at, outcome, structured_output (JSON blob)
- **`transitions`** table — every label change with actor + timestamp
- **`failures`** table — failure events with role + reason

This is the audit + replay surface AND the source for the Foreman MCP queries.
Not load-bearing for correctness (GitHub labels remain source of truth for
state), but load-bearing for observability and debugging.

## 4. Per-node design

### 4.1 Tool capabilities matrix

Each role gets a scoped tool set via the Anthropic Agent SDK. All file ops
scope to the worktree path. **Host-platform side effects (commit, push, PR,
labels) are performed by Foreman core via `GitHostProvider` (see §2.5), not
by the LLM** — so columns for `Edit/Write`, `Bash`, and `gh` reflect what
the LLM is permitted to do directly, with `—` meaning core handles it.

| Role | Read | Glob/Grep | Edit/Write | Bash | host ops |
|---|---|---|---|---|---|
| **Planner** | ✓ | ✓ | ✗ (spec content returned via structured output) | ✗ | — core via `GitHostProvider` |
| **Reviewer** | ✓ | ✓ | ✗ | ✗ | — core via `GitHostProvider` |
| **Fixer** | ✓ | ✓ | ✓ | ✓ (worktree, read-only git allowlist outside it) | — core via `GitHostProvider` |
| **Worker** | ✓ | ✓ | ✓ | ✓ (worktree, read-only git allowlist outside it) | — core via `GitHostProvider` |

**Planner is now read-only on the filesystem.** Post-refactor (Foreman
issue #8 — the "Looper pattern"), the Planner LLM returns spec doc content
as part of its structured output and Foreman core writes/commits/pushes/
opens-the-PR. This trims the Planner's tool surface from
`Read/Glob/Grep/Edit/Write/Bash` down to `Read/Glob/Grep` and removes the
need to inject a per-role `GH_TOKEN` into the agent subprocess.

**Critical constraint:** Reviewer is read-only on files + bash. Reviewer can
flag findings, post review comments, change labels — but cannot modify files.
This enforces the role boundary; Reviewer cannot accidentally "fix" something
they don't like. If Reviewer needs git-log-style history search later, we add a
**tightly scoped read-only git Bash allowlist** (log, diff, show, blame — no
push/commit/checkout/reset), NOT full Bash.

**Hard blocklists for all roles:**
- `gh repo delete` — never (and `gh` is no longer in any role's allowed tool
  set; PR/label ops route through `GitHostProvider`)
- `git push --force` — never; if Fixer/Worker need to rewrite history, they
  fail back to human

**Bash scoping:** Where Bash is granted (Fixer / Worker), it is constrained
to the worktree's working directory.

### 4.2 Structured handoff (B-strict)

Each role returns a structured JSON output (validated against a role-specific
Pydantic schema). All outputs persist to SQLite for audit + replay.

Per role, the LLM output is the *spec contract content*; the
side-effect-completed `RunResult` (LLM output + host-side `PRRef`,
commit SHA, etc.) is what `run_<role>` returns to the daemon. This
separation keeps the LLM decoupled from any specific git hosting platform
— a future GitLab provider drops in via §2.5's `GitHostProvider`
abstraction without touching role prompts.

**Default forwarding to next node: nothing.** Each next role starts fresh from
GitHub state alone. The artifact (spec PR, review comments) IS the contract.

**Explicit exception in v1:** Reviewer → Fixer passes `reviewer_findings` (the
actionable findings list, not Reviewer's full reasoning prose). Fixer cannot do
its job without knowing what to fix.

This protects against context poisoning:
- **Anchoring bias** — Reviewer doesn't see Planner's confidence flags
- **Cascading errors** — Reviewer doesn't take Planner's "this only touches X"
  as ground truth
- **Prompt injection-like effects** — Reviewer isn't primed by Planner's
  summary statements

Replay mode (operator-triggered debugging): daemon can re-inject any persisted
prior output into any node when re-running. Not the default pipeline flow.

## 5. Operator surface

### 5.1 CLI commands (walking-skeleton first; thicken later)

Walking-skeleton minimum:

- `foreman plan <issue-url>` — synchronously run the Planner on one issue.
  Manual trigger. No daemon loop.

Thickening order (each adds operator surface):

- `foreman review <pr-url>` — manually run Reviewer
- `foreman work <pr-url>` — manually run Worker
- `foreman daemon start` — start the poll loop (replaces manual CLI dispatch)
- `foreman daemon stop`
- `foreman daemon status`
- `foreman ps` — list in-flight tickets and their states
- `foreman worktree clean <ticket>` — cleanup after failed pipeline
- `foreman project add <repo>` — onboard a project (see §5.2)
- `foreman project list`
- `foreman project remove <repo>`

### 5.2 `foreman project add` design

**Turnkey one-command UX.** Idempotent — safe to re-run.

```
$ foreman project add jeffrichley/voice
[1/4] Writing project entry to ~/.foreman/config.toml ........ ✓
[2/4] Verifying bot tokens (4 bots) .......................... ✓
[3/4] Inviting bots as collaborators on jeffrichley/voice .... ✓
        ✓ foreman-planner-bot — invitation sent
        ✓ foreman-reviewer-bot — invitation sent
        ✓ foreman-fixer-bot — invitation sent
        ✓ foreman-worker-bot — invitation sent
[4/4] Creating foreman:* labels on jeffrichley/voice ......... ✓
        + foreman:plan
        + foreman:planning
        + foreman:spec-review
        + foreman:spec-fix
        + foreman:spec-ready
        + foreman:implementing
        + foreman:impl-review
        + foreman:impl-fix
        + foreman:ready-for-merge
        + foreman:hold
        + foreman:failed

Project jeffrichley/voice ready for foreman dispatch.

Next: gh issue create -R jeffrichley/voice ... && foreman plan <issue-url>
```

**Authentication note:** `project add` runs as Jeff's own identity (from
`FOREMAN_ADMIN_TOKEN`), NOT a bot. Bot collaborator invitations require
repo-admin perms; bots don't have them. Bot tokens are only for ongoing
pipeline operations.

**Idempotency rules:**
- Already-collaborator bots → skip (note in output)
- Already-existing labels → skip (note in output)
- Failed-step → report failure + how to retry; don't auto-rollback prior steps

**Deferred from v1 walking skeleton:** the full `project add` implementation
ships AFTER walking skeleton works. For walking skeleton: hand-edit
`~/.foreman/config.toml` once for the pilot project.

## 6. Infrastructure

### 6.1 Repo & file layout

**Repo:** `e:/workspaces/ai/agents/foreman/`. Sits next to voice, madrigal,
other agent-core-style projects.

```
foreman/
├── packages/
│   └── foreman/
│       ├── src/foreman/
│       │   ├── __init__.py
│       │   ├── daemon.py          # poll loop + dispatch
│       │   ├── cli.py             # `foreman` CLI entry
│       │   ├── mcp.py             # Foreman MCP server (query tools)
│       │   ├── config.py          # TOML config schema + loading
│       │   ├── identity.py        # per-role token resolution + PyGithub clients
│       │   ├── labels.py          # label state machine
│       │   ├── github.py          # PyGithub helpers + subprocess git helpers
│       │   ├── worktree.py        # per-ticket worktree mgmt
│       │   ├── provider.py        # provider facade
│       │   ├── providers/
│       │   │   └── anthropic_sdk.py  # claude-agent-sdk adapter
│       │   ├── storage.py         # SQLite lifecycle storage
│       │   ├── schemas/           # per-role Pydantic output schemas
│       │   │   ├── planner.py
│       │   │   ├── reviewer.py
│       │   │   ├── fixer.py
│       │   │   └── worker.py
│       │   └── roles/
│       │       ├── planner.py
│       │       ├── reviewer.py
│       │       ├── fixer.py
│       │       └── worker.py
│       ├── prompts/
│       │   ├── planner.md
│       │   ├── reviewer.md
│       │   ├── fixer.md
│       │   └── worker.md
│       └── tests/
├── pyproject.toml
├── README.md
└── .github/workflows/...     # (from scaffold skill)
```

### 6.2 Libraries

- **PyGithub** for GitHub API (PRs, reviews, labels, comments, issues, collaborators)
- **subprocess git** for repo ops (commit, push, branch, fetch) — runs in the
  worktree with the right per-identity git config
- **claude-agent-sdk** (Anthropic Agent SDK) as the first provider adapter
- **pydantic** for config + per-role output schemas
- **tomli / tomllib** for config loading
- **click** or **typer** for CLI (use whatever the scaffold skill picks; both
  fine)

### 6.3 Substrate

**Windows native.** Same OS as Wren and the rest of my infrastructure
(creds-management skill, gh wrapper, GPG/SSH keys, gitconfig). WSL rejected:
the original WSL appeal was Looper (Linux daemon); since we pivoted to Plan B,
that motivation evaporated.

Known Windows shell gotchas (cygpath, non-interactive bash, claude-code CLI
quirks) are bounded and documented in existing skills.

## 7. Build order — walking-skeleton first

Reject the bottom-up "config → roles → daemon → CLI" build order. Walking
skeleton first; thicken in slices that each ship something dogfoodable.

### Walking skeleton (~5 hours)

Goal: `foreman plan <issue-url>` runs end-to-end and opens a real spec PR.

Modules:
- `config.py` (one project, planner identity)
- `identity.py` (planner-bot PyGithub client)
- `worktree.py` (create worktree)
- `provider.py` + `providers/anthropic_sdk.py` (provider facade + Anthropic SDK adapter)
- `schemas/planner.py` (Planner output schema)
- `roles/planner.py` (dispatch + parse)
- `prompts/planner.md` (initial planner prompt)
- `cli.py` (just the `plan` command)

**Dogfood: run on one real ticket. See what Planner produces. Iterate prompt.**

### Thickening (each step ships something usable)

1. Add Reviewer → 2-node pipeline; CLI now: `foreman plan` + `foreman review`
2. Add Worker → 3-node pipeline; add `foreman work`
3. Add label-based auto-discovery (daemon poll loop) → CLI commands become
   optional; label-flip drives everything. **THIS is when we shift from CLI
   dogfooding to label-flip dogfooding.**
4. Add Fixer → review loop closes
5. Add SQLite lifecycle storage + Foreman MCP → observability
6. Add `foreman project add` → onboarding
7. Add bus event publishing (v2 — after bus capability lands)

## 8. Pre-build checklist

1. **Skill rebuild** (~45 min): rebuild `scaffold-agent-core-project` skill
   from voice repo + bake in the three GHA anti-recursion lessons (per memory
   `project_gha_anti_recursion_lessons`)
2. **Scaffold Foreman** (~15 min): run rebuilt skill on `foreman/`
3. **Bot account creation** (~20 min, parallelizable): 4 GitHub signups via
   the plus-alias pattern. Not blocking; Jeff can do whenever
4. **Walking skeleton implementation** (~5 hours)
5. **First dogfood** on a pilot ticket (selection deferred until walking
   skeleton works — we'll have more context then)

## 9. Locked decisions index

1. State machine: 9 explicit states + hold + failed
2. 4 roles → 4 identities (1:1 mapping)
3. 10 transitions; review↔fix loops at state-label level
4. Entry trigger: label-primary + CLI escape hatch
5. State storage: GitHub labels authoritative + SQLite mirror
6. Transition mechanics: nodes emit outcomes → central handler maps to labels
   via GitHub adapter
7. Failure handling: `foreman:failed` multi-label + PR/issue comment
8. Pause/hold: blocks NEW node starts; in-flight completes + advances
9. Concurrency: serial (one ticket at a time)
10. Provider facade as first abstraction; first adapter Anthropic Agent SDK;
    prompt caching from day one
11. Per-ticket worktree at `~/.foreman/worktrees/<repo>/<ticket>/`, scoped FS
    tools, Planner-entry → pipeline-completion lifecycle
12. 4 GitHub Apps (one per role); per-repo install replaces bot-account
    invitation flow
13. App credential storage: App id via env-var → config-file precedence;
    private key as PEM file on disk (chmod 600); installation tokens
    minted on demand and cached with 5-min refresh window
14. Polling: 30s git, single loop, configurable; webhooks v2
15. Bus integration deferred to v2; v1 uses SQLite + Foreman MCP
16. Per-node handoff: B-strict — all persisted, default forward = nothing,
    Reviewer→Fixer findings the only v1 exception
17. Substrate: Windows native
18. Tool capabilities matrix (Reviewer read-only on FS + bash); hard blocklist
    `gh repo delete` + `git push --force`
19. Scaffolding: rebuild scaffold-agent-core-project skill first, then use it
20. Repo location: `e:/workspaces/ai/agents/foreman/`
21. GitHub library: PyGithub for API + subprocess git for repo ops
22. Build order: walking-skeleton first, then thicken via dogfooded slices
23. `foreman project add`: turnkey one-command, idempotent, runs as
    `FOREMAN_ADMIN_TOKEN`; implementation deferred to after walking skeleton
