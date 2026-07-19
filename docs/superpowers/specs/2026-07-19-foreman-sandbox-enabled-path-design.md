# Foreman sandbox enabled-path completion (#556) — design

**Status:** design decided 2026-07-19 (Jeff chose the "clean way" — private per-job clone); flow prototyped + de-risked in a real bwrap container. Ready for an implementation plan.
**Depends on:** #557 (the sandbox ships flag-off) — merged. **Gates:** flipping `config.sandbox.enabled = true`.

## Problem

#557 shipped the bubblewrap sandbox flag-off. The *enabled* path does not yet work end-to-end: the whole-branch review found the launcher never binds the `FOREMAN_V4_CONFIG` file, and — more fundamentally — the role's worktree is created with `git worktree add`, which makes a *linked* worktree that shares the base clone's `.git` at `/foreman/repos/<project>` (a volume shared by all jobs + the daemon poller). Inside the sealed box the base repo is (correctly) invisible, so a linked worktree cannot function; and binding the shared base repo read-write into the box would reintroduce a shared-writable-resource hole — a job could corrupt the base clone every sibling uses, defeating the sandbox.

## Decision: the "clean way" — a private per-job clone

Each job gets its **own self-contained clone** instead of a shared linked worktree. The daemon (trusted, outside the box) does a **local clone** of the base repo into the job's scratch dir before launching the box; the box gets only that clone (read-write) + the config file (read-only) + the Claude creds (read-only), and never sees the shared base repo. The job works entirely in its own clone and pushes to GitHub over the open network.

## Prototype findings (validated in a container from the #557 image + real volumes)

- **The base repo is invisible in the box** — `--volumes-from` mounts it but bwrap doesn't bind it; confirmed the never-bind holds. So the clone MUST be prepped outside the box by the daemon.
- **`git clone --local` is free ONLY when co-located on the same filesystem.** A clone from `/foreman/repos` (volume, dev 2096) into `/foreman/scratch` (container writable layer, different device) FAILS with "Invalid cross-device link" (hardlinks can't cross devices). A clone into a dir UNDER `/foreman/repos` (same dev 2096) succeeds with **hardlinked objects** (`stat` shows link count 2 — shared inodes, ~zero new disk). **Therefore the per-job scratch must live on the same volume as the base repos.**
- **`foreman` is importable inside the box** (via the read-only `/usr` bind → system site-packages). So role subprocesses (`foreman <role>`) run fine; isolation comes from foreman being **read-only** (a job's `uv sync` cannot rewrite it — the 2026-07-18 corruption is structurally dead), NOT from import failing. (Note: this contradicts how #557's Task-7 regression test was described as "import foreman fails"; that assertion likely holds only in a fresh `/scratch` venv context, not for the role entry point. Reconcile/clarify that test as part of this work — the real guarantee is read-only + writable-scratch-only.)
- **Config binds + reads fine** (the file is readable at the RO mount); roles load it via `load_v4_config` (str-safe), not the raw `load_config`.

## Changes

1. **Scratch co-location** — the per-job scratch root must be on the same filesystem/volume as the base repos so the local clone hardlinks. Default `scratch_root` moves onto the repos volume (e.g. `/foreman/repos/.scratch`), or is derived per project from `local_clone_path`'s volume. Config default updated; documented that operators keeping repos + scratch on one volume is required for the free clone.
2. **Daemon prepares the private clone** — before launching the box, the dispatcher (or a helper it calls, running as the trusted daemon with access to `/foreman/repos`) runs `git clone --local <base_clone_path> <scratch>/<worktree-name>` into the co-located scratch (hardlinked). The box then binds that scratch read-write; the role operates in the clone. The role's existing `git worktree add` path is bypassed when sandboxed (the repo is already present as a standalone clone) — integrate with `WorktreeManager` so the role uses the pre-provided clone instead of creating a linked worktree. Confirm the exact role/worktree entry point and make the pre-provided clone satisfy it.
3. **Bind the config file** — `SandboxLauncher.build_argv` read-only-binds the `FOREMAN_V4_CONFIG` file into the box at its expected path and sets the env var accordingly, so `load_v4_config` succeeds. (Add to the launcher's binds, not the never-bind list.)
4. **Claude creds writability** — verify during the dogfood whether the Claude CLI needs to *write* its config/session dir (currently RO). If it does, provide a writable location (a tmpfs or a scratch-backed bind) for the session state without exposing the daemon's real creds writable. Discover empirically.
5. **Scratch cleanup** — the daemon removes the per-job scratch clone on terminal landing (Done/Failed/NeedsHelp). Since objects are hardlinked, cleanup is cheap and can't corrupt the base. Decide keep-on-failure-for-debugging vs always-clean; default clean on success, and add a periodic sweep for orphans. (This closes the gap Jeff raised: today the scratch dir lingers forever.)

## Testing

- **Unit:** the config-file RO bind appears in the argv; the clone-prep helper builds the right `git clone --local` command; scratch cleanup removes the dir on terminal landing.
- **Hermetic (real bwrap, self-skips without userns):** a daemon-prepped co-located clone + a sandboxed command that loads the config, operates in the clone (`git log`, `git commit`), and confirms the base repo is invisible + foreman is read-only (write attempt fails).
- **Dogfood (the keystone — manual, on a userns host / the production container):** run a foreman-**self** ticket concurrently with an agent_core ticket with `sandbox.enabled=true`; confirm both complete, no `foreman.prompts`-style corruption, and the scratch clones are cleaned up. This is the only real-bwrap end-to-end validation (CI can't run userns — see #555).

## Out of scope

- Flipping `config.sandbox.enabled = true` in production — a live config change, done by the operator after the dogfood passes (surface to Jeff).
- The LLM-proxy hardening (keep Claude creds out of the box entirely) — future.

## Success criteria

- With the flag on, a role runs sandboxed against its own private clone: loads config, implements, commits, pushes — no shared-writable base repo, base invisible in the box.
- The private clone is free (hardlinked) because scratch is co-located with the repos.
- Per-job scratch clones are cleaned up on terminal landing (no unbounded accumulation).
- The dogfood self-ticket-concurrency scenario completes without corruption.
