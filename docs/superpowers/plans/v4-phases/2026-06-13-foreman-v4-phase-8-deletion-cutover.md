> **Parent plan:** [../2026-06-13-foreman-v4-substrate-redesign-implementation.md](../2026-06-13-foreman-v4-substrate-redesign-implementation.md) — read its v4 isolation principle first.
> **Spec:** [../../specs/2026-06-13-foreman-v4-substrate-redesign-design.md](../../specs/2026-06-13-foreman-v4-substrate-redesign-design.md).
> **Branch:** `feat/foreman-v4-substrate`.
> **Gate at end:** `just check` green; then stop for human review before next phase.

## Phase 8 — v3 deletion + cutover docs + PR

### Dogfood watch-points (from Phase 4 code-quality reviews)

These are real-world behaviors that the v4 mocked test suite cannot exercise. They were consciously deferred because no current test surfaces them; Phase 8 cutover is the first time real PyGithub + real GitHub talks to v4 code. If any trigger fires during dogfood, file a ticket and fix before declaring v4 stable.

1. **`SqliteTicketRepository.latest_pr_number_for_ticket` — N+1 JSON decode under RLock.** Reads every non-null `outcome_payload` for the ticket, JSON-decodes each row Python-side, walks until pr_number found. Lock held the whole time. For v4 dogfood scale (tickets with ≤30 state instances) this is sub-ms. **Trigger to promote:** any single ticket exceeds 50 state instances (pathological BLOCKED loop) OR `tick()` observed >10ms in production logs. **Fix when triggered:** push `artifacts.pr_number` filter into SQL with `json_extract`:
   ```sql
   SELECT json_extract(outcome_payload, '$.artifacts.pr_number') AS pr
   FROM state_instances
   WHERE ticket_id = ? AND pr IS NOT NULL
   ORDER BY sequence DESC LIMIT 1
   ```
   Lock released before Python-side decode of N rows.

2. **`PyGithubGitProvider._gh._Github__requester` private-attr fragility.** PyGithub doesn't expose a typed GraphQL surface; we reach into the name-mangled `__requester` for the GraphQL mutations (`enqueue_merge_queue`, `merge_verdict`). The `# type: ignore[attr-defined]` is required because mypy can't see the mangled name. **Trigger to promote:** PyGithub minor-version bump that renames or removes the attribute. **Fix when triggered:** introduce a thin GraphQL wrapper, OR switch to `requests` directly for the two GraphQL methods (REST surface stays on PyGithub).

3. **GraphQL `payload is None` defensive unwrap.** `merge_verdict` does `(payload.get("data") or {}).get("node", {}).get("mergeQueueEntry")`. If `payload` itself is None (5xx response body), this raises AttributeError on `.get("data")`. PyGithub historically always returns a dict, but a real GitHub outage could surface it. **Trigger to promote:** any AttributeError on `merge_verdict` in production logs. **Fix when triggered:** wrap with `(payload or {}).get(...)`.

4. **GraphQL `node is None` semantic ambiguity in `merge_verdict`.** Currently returns `MergeVerdict.PENDING` when the GraphQL node is None. That's correct if "node is None" means "not in queue yet." It's WRONG if "node is None" means "PR has been deleted as a graph node." Today both map to PENDING, and the WorkerPool just keeps polling — an infinite loop on a deleted PR. **Trigger to promote:** any merge_verdict observed stuck in PENDING for >24h in production logs. **Fix when triggered:** separately query for PR existence; if PR was deleted, raise `PRNotFoundError` so MergingState can handle it correctly (move ticket to Failed or NeedsHelp).

### Goal

The v4 substrate is complete; v3 still occupies space in the repo. Phase 8 deletes it in one shot, fixes any survival-set files that broke, writes the operator-facing RUNBOOK additions, runs the standing adversarial-review pass, and opens the v4 PR.

The deletion is mechanical because the **v4 isolation principle** (set up at Phase 1) made it so: `foreman.v4.*` never imports from the kill set, the Task 1.10 isolation guard enforces that on every commit, and Phase 5 already deleted the role files' last reaches into `foreman.labels`. What's left is `git rm` plus survival-set cleanup.

### Task 8.1: Delete the v3 kill set

**Files:**
- Delete (16 files + 2 dirs): everything in the **kill set** named in the plan header

The exact `git rm` block from the isolation principle section, executed verbatim:

- [ ] **Step 1: Run the deletion block**

```bash
cd packages/foreman
git rm -r src/foreman/reconciler/
git rm src/foreman/daemon.py
git rm src/foreman/daemon_runners.py
git rm src/foreman/daemon_host.py
git rm src/foreman/daemon_lock.py
git rm src/foreman/dispatcher.py
git rm src/foreman/dispatch_recorder.py
git rm src/foreman/poller.py
git rm src/foreman/queue.py
git rm src/foreman/storage.py
git rm src/foreman/worker.py
git rm src/foreman/role_dispatch.py
git rm src/foreman/stats.py
git rm src/foreman/ps.py
git rm src/foreman/labels.py
git rm src/foreman/branches.py
git rm src/foreman/v3_bus_endpoint.py
git rm -r tests/reconciler tests/daemon tests/dispatcher 2>/dev/null || true
cd ../..
```

(The `|| true` on the test dirs covers the case where some weren't created yet — non-fatal.)

- [ ] **Step 2: Run the test gate to see what broke**

Run: `just check`
Expected: import errors from any survival-set file that still references the kill set. Task 8.2 fixes them.

If `just check` is GREEN immediately, the isolation discipline held perfectly — Task 8.2 has nothing to do; advance to Task 8.3.

- [ ] **Step 3: Commit the deletion**

```bash
git add -u  # stages the deletions
git commit -m "feat(v4): delete v3 substrate (reconciler/, label-driven daemon, label catalog)"
```

### Task 8.2: Repair any survival-set files that referenced the kill set

**Files:** discovered by the Task 8.1 test-gate failure

The two likeliest offenders:

1. **`foreman/cli.py`** — Phase 5 left some Click imports that referenced `foreman.labels` for the label-writing path. Phase 6 collapsed `cli.py` to a typer wrapper, so this should be clean — but verify the actual file content.

2. **`foreman/roles/{planner,reviewer,fixer,worker}.py`** — Phase 5 deleted the label-writing tails, but if any helper imports (e.g., `from foreman.labels import LABEL_NEEDS_HELP`) survived, they break now. Verify and fix.

- [ ] **Step 1: Run `just check`; collect the failures**

Run: `just check`
Note every `ImportError` or `ModuleNotFoundError`. The traceback names the survival-set file + the kill-set import that broke.

- [ ] **Step 2: For each failing import**

Either delete the import (if its caller path no longer exists) or replace with the v4 equivalent (if the survival-set code legitimately needs a thing the kill set used to provide). Most fixes will be deletions; the role files in particular should not need anything from `foreman.labels` after Phase 5.

- [ ] **Step 3: Re-run `just check` until green**

Run: `just check`
Expected: all gates pass. Includes the Phase 1 isolation guard (which now has nothing left to compare against in the kill set — the guard test still passes because no v4 file imports anything that no longer exists).

- [ ] **Step 4: Commit the repairs**

```bash
git add packages/foreman/src/foreman/
git commit -m "fix(v4): drop orphaned imports from survival-set files after v3 deletion"
```

(If Task 8.1 produced a green build with no follow-ups, this commit is empty — skip it.)

### Task 8.3: RUNBOOK — MergeQueue per-repo + daemon setup + cutover

**Files:**
- Modify: `docs/RUNBOOK.md` (or create if it doesn't exist)

Three sections to add (or update if RUNBOOK already covers daemon setup):

1. **Per-repo MergeQueue enablement.** Step-by-step for each repo foreman drives.
2. **Daemon config + identity setup.** What `~/.foreman/v4/config.toml` looks like; how identity tokens get minted.
3. **Cutover from v3 to v4.** Stop v3 daemon, kill in-flight tickets (or re-trigger them), start v4 daemon. Spec says clean break — abandon in-flight tickets.

- [ ] **Step 1: Append the MergeQueue section**

```markdown
## MergeQueue per-repo enablement (v4)

v4 enqueues impl PRs into GitHub MergeQueue rather than merging directly.
Each repo foreman drives needs MergeQueue enabled. Steps per repo:

1. **Repo Settings → Pull Requests → Allow merge queue.** Enable.
2. **Repo Settings → Branches → Branch protection rules → `main`** (or whatever the target branch is):
   - Require pull request before merging.
   - Require status checks to pass before merging.
   - Add the **"Require merge queue"** rule.
3. **Confirm the workflow.** Open a draft PR; observe the "Merge when ready"
   button instead of "Merge pull request". When clicked, GitHub takes over
   the rebase + CI + merge dance and reports back via the GraphQL surface
   `PyGithubGitProvider.merge_verdict` consumes.
4. **App permissions.** The Worker bot needs the MergeQueue API permission
   in its installation. Check Settings → Integrations → Apps → foreman-worker
   has "Read and write" on Pull Requests.
```

- [ ] **Step 2: Append the daemon setup section**

```markdown
## v4 daemon setup

Config file: `~/.foreman/v4/config.toml` (override via `$FOREMAN_V4_CONFIG`).

Minimum viable shape:

\`\`\`toml
[daemon]
db_path = "~/.foreman/v4/state.db"
log_dir = "~/.foreman/v4/logs"
tick_seconds = 30
max_in_flight = 4
merge_mechanism = "queue"

[[projects]]
name = "voice"
repo = "jeffrichley/voice"
local_clone_path = "~/code/voice"
\`\`\`

Add a `[[projects]]` block per repo. Identity wiring still lives in the
v3 `[admin]` / `[orchestrator]` / per-role app blocks in `~/.foreman/config.toml`
(survival set); v4 reads role tokens through `foreman.identity` the same
way v3 did.

Start the daemon:
\`\`\`bash
foreman daemon start
\`\`\`

Operator commands:
\`\`\`bash
foreman ps                           # open tickets
foreman show <ticket-id>             # state history tree
foreman log --tail                   # live transition feed
foreman queue                        # in-flight + queued counts
foreman hold <ticket> --reason "x"   # pause
foreman resume <ticket>              # un-pause
foreman daemon status                # PID + alive check
\`\`\`
```

- [ ] **Step 3: Append the cutover procedure**

```markdown
## v3 → v4 cutover (one-shot, no migration)

Spec decision (2026-06-13): clean break. Any tickets in flight at cutover
are abandoned; their PRs are left on GitHub for manual re-triggering.

Cutover order:

1. **Stop the v3 daemon.**
   \`\`\`bash
   # If v3 is running:
   foreman daemon stop          # v3 PID file → SIGTERM
   # OR (if v3 binary is already gone after merging the v4 PR):
   kill $(cat ~/.foreman/reconciler.lock | jq -r .pid)
   \`\`\`
2. **Write the v4 config** (see "v4 daemon setup" above). The v4 daemon
   uses a different SQLite DB path (`~/.foreman/v4/state.db`); v3's DB
   is left untouched.
3. **Enable MergeQueue** on every repo foreman drives (see above).
4. **Start the v4 daemon.**
   \`\`\`bash
   foreman daemon start
   \`\`\`
5. **Smoke check.** File one fresh test ticket with `foreman:plan`; verify
   `foreman ps` shows it; verify `foreman log --tail` streams transitions.
6. **Stale-PR cleanup (optional).** Old in-flight PRs from v3 that were
   abandoned have a `foreman:state-*` label written by v3. If any are
   blocking re-triggering, close them by hand or re-trigger via re-applying
   `foreman:plan` to the issue.

If anything goes wrong, `foreman daemon stop` and inspect:
- `~/.foreman/v4/logs/transitions.jsonl` — the structured journal of every
  state transition.
- `foreman show <ticket>` — the tree view of where a single ticket got
  stuck and why.
```

- [ ] **Step 4: Commit**

```bash
git add docs/RUNBOOK.md
git commit -m "docs(v4): RUNBOOK — MergeQueue setup + daemon config + v3 cutover"
```

### Task 8.4: Adversarial review of the v4 branch

**Standing rule:** adversarial review before every PR (from `feedback_adversarial_review_before_pr` memory). For a substrate rewrite of this size, this is the most important quality gate.

Dispatch a subagent with the Plan / Explore agent type and a hostile prompt:

- [ ] **Step 1: Dispatch the reviewer**

```
Agent({
  subagent_type: "Plan",
  description: "Adversarial review of v4 substrate rewrite",
  prompt: `
    Adversarially review the foreman v4 substrate rewrite landing on branch
    feat/foreman-v4-substrate (off main) in repo e:/workspaces/ai/agents/foreman/.

    Spec: docs/superpowers/specs/2026-06-13-foreman-v4-substrate-redesign-design.md
    Plan: docs/superpowers/plans/2026-06-13-foreman-v4-substrate-redesign-implementation.md
    Diff: git diff main...HEAD

    Adopt a hostile-reviewer stance. Look for:

    1. **Correctness gaps.** State transition edges that the test suite
       doesn't actually cover. Outcome routing for kinds the role can emit
       but the state machine doesn't handle. Crash-recovery edge cases the
       resume query misses.

    2. **Hidden coupling.** Anywhere foreman.v4.* reaches into the kill set
       despite the isolation guard. Anywhere two CliContext fields could
       reasonably be required-together but the dataclass allows one without
       the other.

    3. **MergeQueue + branch-protection holes.** Repos where MergeQueue can't
       be enabled (private free-tier orgs). Race conditions between Worker
       opening impl PR and Reviewer-on-impl looking at it before CI runs.

    4. **CLI surface gaps.** Operator commands that look like they work but
       silently corrupt state (e.g., set-state on a held ticket; retry while
       in flight). Mutations that should require confirmation but don't.

    5. **Config / identity drift.** The v4 config + v3 identity coexist
       during a window — anywhere the daemon reads identity from a config
       path that the v3 deletion broke.

    6. **YAGNI violations + over-engineering.** Anywhere we have a Protocol
       with one impl that doesn't earn its abstraction.

    Report findings as a numbered list with severity (CRITICAL / IMPORTANT /
    MINOR) and file:line citations. Surface ONLY genuine defects; don't pad
    with style nits. The goal is shipping a substrate that actually works,
    not a clean-feeling diff.
  `
})
```

- [ ] **Step 2: Triage the findings**

For each finding:
- **CRITICAL:** fix inline before the PR opens.
- **IMPORTANT:** fix inline if cheap; otherwise file a follow-up ticket with `foreman:plan` and reference it in the PR body.
- **MINOR:** fix inline only if obviously cheap; otherwise note in the PR body's "Known follow-ups" section.

(Per the standing `feedback_inline_vs_ticket_vs_drop_for_code_review_findings` rule: most findings fix inline while implementer is warm; tickets are for multi-file/design-judgment items.)

- [ ] **Step 3: Commit the fixes**

```bash
# One commit per discrete fix
git add <files>
git commit -m "fix(v4): <what the adversarial review caught>"
```

### Task 8.5: Push branch + open PR

**Files:**
- Remote: GitHub PR against `main`

The v4 work is currently stacked on `foreman/issue-307` (the original spec branch). Move it to a fresh `feat/foreman-v4-substrate` branch off `main` before opening the PR — keeps git history clean and lets PR #308 be closed with a note.

- [ ] **Step 1: Move the v4 commits to a fresh branch**

```bash
# Identify the first v4 commit (the spec or session-snapshot commit;
# everything from there is v4 work):
git log --oneline foreman/issue-307 | head -20

# Create the new branch from main:
git checkout main
git pull
git checkout -b feat/foreman-v4-substrate

# Cherry-pick or rebase the v4 commit range onto it:
git cherry-pick <first-v4-sha>^..foreman/issue-307
# OR
git rebase --onto main <last-non-v4-sha> foreman/issue-307
git branch -m foreman/issue-307 feat/foreman-v4-substrate
```

(Pick whichever is cleaner given the current commit topology — there's no need to litigate this; the spec branch can be safely abandoned once `feat/foreman-v4-substrate` carries the commits.)

- [ ] **Step 2: Push the branch (Wren PAT)**

Use Wren's PAT via the creds-management skill; pass via `GH_TOKEN` env var only; **never echo**.

```bash
GH_TOKEN=$(python C:/Users/jeffr/.wren/.claude/skills/creds-management/scripts/creds.py \
  --being wren get github --keyring --password) \
  git push -u origin feat/foreman-v4-substrate
```

- [ ] **Step 3: Open the PR**

```bash
gh pr create \
  --base main \
  --head feat/foreman-v4-substrate \
  --title "feat(v4): substrate redesign — state machine + polling + typer CLI" \
  --body "$(cat <<'EOF'
## Summary
Replaces the v3 label-as-state coordination substrate with a daemon-owned state machine in SQLite, a single polling loop, and a typer-based operator CLI. Preserves the existing role pipeline (Planner / Reviewer-on-spec / Fixer-on-spec / Worker / Reviewer-on-impl / Fixer-on-impl) and the `needs-help` escalation pattern.

## Spec
docs/superpowers/specs/2026-06-13-foreman-v4-substrate-redesign-design.md

## Plan
docs/superpowers/plans/2026-06-13-foreman-v4-substrate-redesign-implementation.md (8 phases, ~57 tasks)

## What changed
- `packages/foreman/src/foreman/v4/` — the new substrate (Repository, state machine, observers, QueueManager, Poller, WorkerPool, typer CLI, daemon, bootstrap)
- `packages/foreman/src/foreman/roles/*.py` — role CLI exits replaced with `emit_outcome` calls (label-writing tails deleted)
- `packages/foreman/src/foreman/cli.py` — rewritten as a thin typer-app wrapper
- v3 substrate deleted: `reconciler/`, `daemon.py`, `daemon_runners.py`, `daemon_host.py`, `daemon_lock.py`, `dispatcher.py`, `dispatch_recorder.py`, `poller.py`, `queue.py`, `storage.py`, `worker.py`, `role_dispatch.py`, `stats.py`, `ps.py`, `labels.py`, `branches.py`, `v3_bus_endpoint.py`, plus `tests/{reconciler,daemon,dispatcher}/`
- `docs/RUNBOOK.md` — MergeQueue setup + daemon config + cutover procedure

## Test plan
- [x] Unit tests across foreman.v4.* (state machine, observers, repository, CLI commands)
- [x] Phase 3 lifecycle e2e — happy path + needs-fix loop against FakeGitProvider
- [x] Phase 4 e2e — Poller + QM + WorkerPool drive ticket to Done
- [x] Phase 5 e2e — SubprocessRoleDispatcher forks real subprocess + parses Outcome JSON
- [x] Phase 6 e2e — operator command chain (hold → ps → resume → retry → queue)
- [x] Phase 7 e2e — TOML config → bootstrap → running daemon → terminal state
- [x] Isolation guard test prevents future v4 → kill-set imports
- [x] just check green
- [ ] Dogfood: file a fresh `foreman:plan` ticket post-merge; observe it through to Done

## Decisions worth surfacing
- **State pattern + Template Method + Mediator + Observer + Repository + Strategy** — five GoF patterns explicitly named per the spec.
- **Two-phase PR preserved** — spec PR + impl PR; MergeQueue on impl PR only. (Single-PR was rejected during adversarial review C1.)
- **No webhooks** — single polling loop; tailscale dependency dropped. (Adversarial review C2+C4.)
- **Clean break, no migration** — v3 daemon stops, v4 daemon starts on a fresh SQLite. Any in-flight tickets abandoned (Jeff approved).
- **Pydantic at boundaries, dataclass for internal records** — `Outcome` and `V4Config` are pydantic (parsing JSON / TOML); `CliContext` and `StateContext` are dataclasses (in-process DI containers).

Closes #307 (spec issue).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Close PR #308 with a pointer**

```bash
gh pr close 308 --comment "Superseded by feat/foreman-v4-substrate; v4 substrate redesign absorbs the LabelManager work. See the new PR for the full scope."
```

- [ ] **Step 5: Notify Jeff + Pepper via bus**

Per the standing authorization to send Pepper envelopes, send a single update:

```
mcp__agent-core__send(
    to="pepper",
    kind="TextMessage",
    payload={
        "kind": "TextMessage",
        "text": "Foreman v4 substrate PR is open: <pr-url>. Eight phases land in one drop — state machine + polling + typer CLI + v3 deletion. Adversarial review pass complete; cutover procedure documented in RUNBOOK. Looping you in for awareness; no action requested. 🪶",
    },
)
```

### Phase 8 — `just check` gate

- [ ] **Run:** `just check`
- [ ] **Expected:** green; PR ready for merge.

Phase 8 completion criterion (from the outline): **grep for `_LABEL_TO_ACTION` returns zero; `just check` green; RUNBOOK explains MergeQueue per-repo enablement**.

Phase 8 verification:
```bash
grep -r _LABEL_TO_ACTION packages/foreman/src/  # expect: no matches
just check                                       # expect: all green
grep -A 2 "MergeQueue per-repo" docs/RUNBOOK.md  # expect: the runbook section
```

---
