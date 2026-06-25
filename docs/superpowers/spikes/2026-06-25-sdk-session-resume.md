# Spike: Claude Agent SDK session naming + resume-after-crash

**Date:** 2026-06-25 · **Driver:** Wren (paired with Jeff)
**Question:** Can foreman recover a role that was interrupted mid-run by *resuming
its Claude session* (continue where it left off) instead of re-running from
scratch — and does that survive a real container restart?

**Why it matters:** This is the load-bearing unknown for the crash-recovery
(C1) design. If resume works, the reconciler's "mid-flight crash" arm can
*resume* the role (only the role itself can finish/judge its own partial work —
an external observer can see "a PR exists" but not "the PR is correct"). If it
doesn't, Layer 2 falls back to healer-only (observe-before-act idempotency +
clean re-run). See the architecture reviews (`architecture-review-2026-06-25*.md`),
finding C1/I1.

## TL;DR verdict

**Resume is real and works. The only blocker to durable resume in prod is a
one-line volume mount.**

| Rung | Result |
|---|---|
| SDK supports naming (`session_id`) + `resume` | ✅ proven (local SDK 0.1.63 + in-container `claude` CLI) |
| Resume survives a **hard SIGKILL mid-task** | ✅ proven local — resumed agent picks up *at the interrupted step*, full context, honors idempotency |
| Survives in the **foreman container** | ⚠️ conditional — session dir is **ephemeral**; survives in-place restart, **wiped on container recreate** |
| Git worktree survives a restart | ✅ `/foreman/repos` is a persistent volume |

## Mechanism (verified, not assumed)

Foreman calls the Python SDK `claude_agent_sdk.query()`. The SDK does **not**
hit the API directly — its transport (`_internal/transport/subprocess_cli.py`)
shells out to the **`claude` CLI binary** and forwards the resume flags verbatim:

- `ClaudeAgentOptions.session_id` → `claude --session-id <id>` (line 277)
- `ClaudeAgentOptions.resume` → `claude --resume <id>` (line 274)
- `ClaudeAgentOptions.fork_session` → `claude --fork-session` (line 320)

So session persistence + resume are the **`claude` CLI's** behavior; the SDK is a
thin pass-through. Crash-time process tree:
`foreman daemon → role subprocess (foreman plan) → SDK query() → claude CLI subprocess`.

## Rung 1 — naming + clean resume (local)

Two separate `query()` calls: phase 1 names a session (`session_id=<uuid>`) and
plants a secret word; phase 2 (`resume=<uuid>`, fresh process) asks for the word
with no re-telling.

- Naming honored: requested id == returned id. ✅
- Resume carried context: phase 2 answered "BANANA" with no hint. ✅

## Rung 2 — resume after a hard kill mid-task (local)

Harness (`.spike-resume/runner.py` + `kill_test.py`): phase A runs an ordered
3-step task (`step1.txt` → `sleep 45` → `step2.txt`) with `session_id` fixed;
the orchestrator polls for `step1.txt` then **SIGKILLs the whole process tree**
(python + claude CLI + bash) — the real foreman crash shape. Phase B resumes
with `resume=<id>` and a prompt that says only "continue where you left off"
(the 3 steps are never re-stated).

Result — all four checks green:
```
P1 session persisted after hard kill ........ True
P2 killed mid-task (step1 yes / step2 no) ... True   (killed at +26.5s)
P3 resume finished work (step2.txt + ALL-DONE) True
P4 continued not restarted (step1 mtime same)  True
```
The killer evidence is phase B's trace: given only "continue," the resumed agent
ran `ls`, **picked up at step 2 (the `sleep` — the step it was killed during)**,
then wrote `step2.txt` and replied `ALL-DONE`. It recovered the entire task,
knew step 1 was done, resumed at the interrupted step, and honored the
"don't redo completed steps" instruction.

**Design nuance this surfaced:** the agent resumed *at* the interrupted step and
**redid it** (re-ran `sleep`). Harmless for a sleep, but if the interrupted step
were `git push`, resume could re-attempt it. So **resume narrows but does not
eliminate the redo-the-interrupted-step window** — resume and observe-before-act
(the healer) **compose**; resume alone is not a complete idempotency story.

## Rung 3 — container durability (the gate)

Inspected the live `foreman-daemon` container.

- **Session dir is relocated:** `CLAUDE_CONFIG_DIR=/root/.claude-container`.
- **Functionally confirmed** sessions land there: ran
  `claude --session-id <uuid> -p 'reply with exactly: ok'` in-container (exit 0,
  output `ok`); transcript appeared at
  `/root/.claude-container/projects/-tmp/<uuid>.jsonl`.
- **That dir is NOT on any volume** (`docker inspect` mount destinations contain
  no `claude` path) → it lives on the container's **ephemeral writable layer**.
- The persistent volumes are `/foreman/logs`, `/foreman/repos` (worktrees),
  `/foreman/state`, `/foreman/backups`, `/root/.foreman` — none cover the
  session dir.

**Consequence (docker semantics):**
- **In-place restart** (`docker restart`, host reboot, crash + restart policy) →
  writable layer persists → the transcript survives → resume would work.
- **Container recreate** (redeploy / **Watchtower image update**) → writable
  layer wiped → transcript gone → resume breaks.

Foreman's *dominant* restart cause is **Watchtower auto-deploy = a recreate** —
exactly the case that wipes the transcript (and the case foreman#412's idle-gate
addresses). So as-shipped, resume survives a crash-restart but **not a deploy**.

Auth note (incidental): the container authenticates `claude` via
`CLAUDE_CODE_OAUTH_TOKEN` env (no `~/.claude/.credentials.json`); auth was
working at spike time (the in-container run succeeded).

## Required fix (prerequisite for the resume arm)

Mount a persistent volume at the session dir, e.g. in docker-compose:
```yaml
volumes:
  - foreman-claude-sessions:/root/.claude-container
# or repoint CLAUDE_CONFIG_DIR at an existing volume, e.g. /foreman/state/claude
```
Plus a **startup assertion** that the session dir resolves onto a mount, so we
never silently lose resumability again. This is **Task 0** of the reconciler
implementation plan — the resume arm cannot ship without it. (It is inert until
resume is wired, so there is no standalone bug to fix today.)

## What this does NOT yet prove

- **`output_format` on resume** — foreman roles emit a structured
  `FOREMAN_OUTCOME` via `output_format=json_schema`. This spike used plain text /
  files; whether a *resumed* run still emits the structured Outcome is untested.
- **Git-worktree-specific continue** — rung 2 used plain files in a dir as a
  stand-in for "remaining work"; the real role works in a git worktree. The
  worktree volume persists, but resuming *into* a git worktree with partial
  commits wasn't exercised end-to-end.
- **The recreate-wipe was concluded from docker semantics + mount inspection +
  the functional path confirmation**, not from an actual prod recreate
  (avoided disrupting the live daemon). A throwaway-container demo could show it
  directly if ever needed; the semantics are definitional.
- Container SDK is **0.2.87** (newer than foreman's local 0.1.63); resume is core
  in both, and the in-container `claude --session-id` honored it, so the gap is
  not a concern — noted for completeness.

## Implications for the reconciler design

- The **"mid-flight crash → resume"** arm is **viable** — green to design.
- **Hard prerequisite:** the session-dir volume mount + startup assertion (Task 0).
- **Compose, don't choose:** resume (role finishes its own partial work) +
  observe-before-act healer (no duplicate PR on the redone interrupted step).
- **Routing by `execute_started_at`** still holds: never-started orphans →
  clean re-run (zero side-effect risk); was-executing orphans → resume arm.
- Persist the role's `session_id` on the `state_instances` row at dispatch so
  reconciliation knows which session to resume.

## Reproduction

Scratch harness (throwaway): `C:/Users/jeffr/.wren/.spike-resume/`
(`check_naming_resume.py`, `runner.py`, `kill_test.py`). In-container check:
`docker exec foreman-daemon claude --session-id <uuid> -p '...'` then
`find /root/.claude-container -name '*.jsonl'`.
