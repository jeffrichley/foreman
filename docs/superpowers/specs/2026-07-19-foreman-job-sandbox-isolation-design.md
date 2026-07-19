# Foreman job-execution sandbox isolation (bubblewrap) — design

**Status:** design approved 2026-07-19 (Jeff, brainstorm). Ready for an implementation plan.
**Motivating incident:** 2026-07-18 — during the first cross-repo parallelism test (foreman#537 + agent_core#433 running concurrently), agent_core#433's Worker crashed with `No module named 'foreman.prompts'`. Root cause: the daemon's `foreman` package was editable-installed via a site-packages `.pth` pointing at an ephemeral per-ticket worktree (`/root/.foreman/worktrees/foreman/impl-537/...`). A foreman-*self* ticket's `uv sync --all-packages` had re-registered foreman editable in the container's shared system site-packages, so a concurrently-spawned role subprocess resolved a transient/inconsistent foreman and failed to import a submodule. The daemon was effectively running its own unmerged self-ticket code from a disposable worktree.

## Problem

The foreman daemon (the orchestrator) and the role subprocesses it spawns (Planner / Reviewer / Fixer / Worker) **share one container and one system Python**. A job can therefore reach out and mutate the daemon's own environment — the `foreman.prompts` crash is one instance; a job could equally delete a sibling job's worktree, fill the disk, or read another role's secrets. The current defense is a *blocklist* (`foreman._env_filter` strips `VIRTUAL_ENV` / `UV_PROJECT_ENVIRONMENT` before a worktree `uv sync`). That is a negative defense and it failed: because the daemon runs on the container's system Python (no venv) and its foreman lives in the shared system site-packages, a self-ticket `uv sync` still wrote there.

The fix must be *positive*: a job must be structurally unable to see or write anything except its own scratch workspace and a read-only shared cache.

## Goals

- A role subprocess **cannot** corrupt the daemon's `foreman` install or a sibling job's files/env — enforced by construction, not by convention.
- Preserve intra-repo and cross-repo parallelism (foreman#550) — multiple sealed jobs run concurrently without interfering.
- **Do not** rebuild heavy per-project dependency environments (e.g. torch) per job. Heavy deps are cached once and reused.
- Jobs keep an **open internet** connection (package registries, docs, API research) — isolation must not lobotomize the agent.
- Fits the existing single container + Docker default security profile (no `--privileged`).

## Non-goals (out of scope; future work)

- Container-per-job (rejected: duplicates/re-pulls heavy envs; cost).
- Syscall/seccomp sandboxing against a genuinely malicious dependency (gVisor / nsjail seccomp). The sandbox boundary is designed so this *can* be added later, but accidents-first is the current threat model.
- Network egress allowlisting / an egress-logging proxy (a future visibility layer; network stays open now).
- Changing the merge coordinator, state machine, or role logic beyond the dispatch seam.

## Trust model

**Accidents-first.** The daily, real threat is buggy-but-not-evil behavior — LLM-generated code that corrupts shared state by mistake (exactly this incident). The sandbox's core job is **filesystem + process + environment isolation**.

Two cheap, high-value hardenings against the *hostile* case are baked in from the start, because every Worker holds a GitHub token that can push code and runs arbitrary generated commands:

1. **Secret-scoping** — a job carries only its own short-lived, minimally-scoped role token; the **crown jewels** — the GitHub App **PEM keys** (which mint push tokens for *all* repos), the **credential vault**, and sibling roles' tokens — are never present in the box. One necessary exception (accepted for v1): the role makes its LLM call from inside the box, so the **Claude CLI credentials** are mounted read-only. Their blast radius (an LLM key) is far smaller than the repo-push PEMs. Future hardening: proxy the LLM call through the daemon so even the Claude key stays out.
2. **The existing human-merge gate** is the backstop — a job's token can only open a PR against one repo; nothing merges without human review.

Network stays **open** (agents need PyPI/npm/docs/research); we defend by keeping the crown-jewel secrets *out of the box* rather than fencing the network.

## Architecture

The entire change lands at **one seam**: `foreman.v4.subprocess_dispatcher.SubprocessRoleDispatcher` builds `cmd = ['foreman', '<role>', '--project', P, …]` and `subprocess.Popen`s it. Today that runs in the daemon's shared system-Python process. The change wraps that command in a **bubblewrap** invocation. The daemon becomes a pure orchestrator that hands each role a sealed box and reads back its `FOREMAN_OUTCOME:` stdout exactly as today. Everything downstream of the seam is unchanged.

### Components

1. **`SandboxLauncher`** (new module, e.g. `foreman/v4/sandbox.py`) — single responsibility: given `(project, worktree_path, role, role_token, cache_dir)`, return the `bwrap …` argv that wraps the role command. A pure function of its inputs → exact argv; no side effects; trivially unit-testable. It owns the mount plan and the never-bind list.
2. **Immutable daemon foreman** — the daemon's `foreman` lives only in the image's stable system site-packages and is *never writable from a job*, because a job's `uv sync` now runs inside a box that does not mount the daemon's site-packages writable. This is the structural realization of "layer 1" (env isolation), folded in rather than shipped separately. The image install must be a plain (non-editable) `--system` install, and nothing at runtime may re-register it editable.
3. **Shared uv cache + per-job venv** — the heavy wheels (torch, the ~270 packages) live in the shared `foreman-uv-cache` volume (already present). Each job's `.venv` is materialized cheaply (uv hardlinks from the cache) into its own writable scratch. **The cache is the reuse; the venv is the per-job cheap copy.** uv's cache is content-addressed and concurrency-safe, so many jobs share it without corruption, and a job adding a new dependency fetches only the one new wheel.

### The mount plan (validated by the spike, below)

```
bwrap box for one role job:
  RO   /usr /bin /lib /lib64          base tools; the job's OWN venv shadows the
                                      system site-packages so foreman never leaks in
  RO   /etc/resolv.conf, CA certs, git config
  RO   <cache_dir>  → /cache          shared uv cache (content-addressed)
  RW   <scratch>    → /scratch        the job's worktree + its .venv (ONLY writable path)
  tmpfs /tmp
  namespaces: --unshare-user --unshare-pid --unshare-ipc --unshare-uts
  network:    --share-net             (open egress)
  lifecycle:  --die-with-parent
  NEVER MOUNTED: daemon foreman source, role PEM keys, the credential vault,
                 /root/.foreman, sibling scratch dirs
  RO   Claude CLI creds (/root/.claude*)   necessary for the in-box LLM call (v1 exception)
  ENV IN:     GH_TOKEN=<this job's scoped role token>  (no PEM keys / vault)
```

**Implementation nuance (deliberate):** binding `/usr` read-only carries the daemon's foreman along inside the box. The job's Python therefore must run from **its own venv** (whose site-packages contain the project's deps, not foreman); the system site-packages must not be on the job's `sys.path`. The launcher pins this explicitly (activate the scratch venv; do not inherit `PYTHONPATH`/system site-packages) so foreman cannot be imported inside the box even by accident. This is also what makes the regression test below meaningful.

## Data flow — a job's lifecycle

1. Daemon (outside any box) prepares the ticket's worktree in the scratch area and mints the short-lived role token.
2. `SandboxLauncher` builds the bwrap-wrapped command.
3. `Popen` runs it → the role executes **inside** the box: `uv sync` materializes its `.venv` from the shared cache, it does its work, runs `just check`, `git push`, opens the PR — open network, its scoped token, blind to daemon + siblings.
4. The role prints `FOREMAN_OUTCOME:` on stdout (captured exactly as today by the dispatcher's reader threads).
5. The box exits; the scratch worktree is pruned. The daemon reads the outcome and transitions state.

## Error handling

- **bwrap / user-namespaces unavailable** (a host without unprivileged userns): a **preflight self-test at daemon startup** runs the spike's minimal sandbox. On failure the daemon **fails closed** with a loud, actionable operator message — it does **not** silently fall through to unsandboxed execution. An explicit `--allow-unsandboxed` escape hatch may exist for local dev, but it must be set deliberately and logged loudly on every dispatch (per "bypassed gates must be tracked").
- **Sandbox setup failure** (bad mount, missing cache dir): the role dispatch fails cleanly → the ticket retries / lands in NeedsHelp; it never crashes the daemon (rides the existing per-job / per-project fault isolation).
- **Timeout / kill**: `--die-with-parent` guarantees the box dies with the dispatcher; the existing kill-and-reap logic in `SubprocessRoleDispatcher` is unchanged.

## Testing

- **Unit** — `SandboxLauncher` inputs → exact argv: assert the never-bind paths (role PEMs, vault, foreman source, `/root/.foreman`) are absent from the argv, and the scoped token is present in the env and no other secret is; assert the mount plan (RO cache, RW scratch, open net, the namespace flags).
- **Hermetic integration** — a real `bwrap` run in CI (the spike, as a test) proving RO-cache / RW-scratch / open-net / daemon-secret-invisible / PID-isolation. Self-skips if userns is unavailable on the runner (so CI without nested-userns support doesn't hard-fail).
- **The regression test for this incident** — a role process attempts to write to the daemon's foreman install path → fails because it is not mounted; and `import foreman` inside the box fails because the job's venv shadows the system site-packages. Locks the fix in permanently.
- **Preflight** — a unit test for the startup self-test's pass/fail branches (fail → fail-closed with the operator message).

## Rollout / migration

1. Bake `bubblewrap` into the daemon image (Dockerfile `apt-get install -y bubblewrap`); add the startup preflight self-test.
2. Ensure the image's foreman is a plain non-editable `--system` install and add a guard/assertion that it is never re-registered editable at runtime.
3. Land `SandboxLauncher` and wire it into `SubprocessRoleDispatcher` behind a config flag (default off during bring-up).
4. Flip role dispatch through the box; validate with a foreman-self ticket (the case that broke) running concurrently with an agent_core ticket — the exact 2026-07-18 scenario, now expected to pass.
5. Remove the interim operational caveat ("don't run foreman-self tickets"): once the sandbox is live, self-tickets are safe because a self-ticket's `uv sync` runs inside a box that cannot reach the daemon's install.

## Feasibility spike (completed 2026-07-18/19 — de-risks the design)

Run inside a throwaway container from the live daemon image (`ghcr.io/jeffrichley/foreman:dev`), under Docker's **default** security profile (`Privileged=false`, no added caps, no `SecurityOpt`):

- `bubblewrap` 0.11.0 installs via `apt` in the image.
- A nested user + PID + IPC + UTS namespace sandbox is created successfully — **no `--privileged`, no added capabilities**.
- The full designed shape verified: **scratch writable**, **cache read-only** (writes rejected, reads succeed), **network reachable** (`pypi.org` resolved), an unbound daemon-secret directory **invisible** inside the box, and **PID isolation** (a handful of procs visible vs the host's hundreds).

The single make-or-break risk (nested namespaces inside the daemon container) is retired.

## Open questions for the implementation plan

- **Cache write policy:** mount the shared uv cache read-only (jobs reuse but cannot add wheels for next time) vs. read-write (jobs warm the shared cache; relies on uv's content-addressed concurrency safety). Leaning RW-shared for cache warming; confirm uv's concurrent-writer guarantees during planning.
- **Scratch location & cleanup:** where per-job scratch lives (a dedicated volume vs. the existing worktree area) and how/when it is pruned relative to the current `WorktreeManager` lifecycle.
- **Exact minimal RO root set:** the smallest `/usr` `/lib` `/etc` bind set that still lets `uv`, `git`, `just`, and the project's check command run.

## Success criteria

- A foreman-self ticket running concurrently with another repo's ticket completes without the `foreman.prompts` (or any shared-env) corruption — the 2026-07-18 incident cannot recur.
- A job can add a new dependency (edit `pyproject.toml`/`uv.lock`, `uv add`, install into its venv) with the heavy shared deps served from cache (no torch rebuild).
- Attempting, from inside a job, to write the daemon's foreman install or read a sibling's secret fails — proven by the regression test.
- The daemon fails closed (clear operator message) on a host where the sandbox cannot be created.
