# Spec: troubleshooting note for stuck dispatch (issue #253)

## Goal

Create a single, operator-facing troubleshooting document at
`docs/troubleshooting/stuck-dispatch.md` that walks an operator through
diagnosing a foreman daemon that "looks stuck" in under two minutes.
The signals an operator needs already exist — they're spread across
`v3_host.py`'s dispatch logging, `exec_log.py`'s `execution_log` table,
`entrypoint.sh`'s startup banner, and the per-dispatch log files under
`<log_dir>/<role>/`. This spec adds the missing one-page guide that
ties those signals together. See issue
[#253](https://github.com/jeffrichley/foreman/issues/253). Doc-only,
no code changes.

## Acceptance criteria

- A new file exists at the exact path
  `docs/troubleshooting/stuck-dispatch.md`. The `docs/troubleshooting/`
  directory does not currently exist on `main`; the Worker MUST create
  it as part of this PR (no other content goes in it).
- The doc contains exactly five top-level (`##`) sections, in this
  order:
  1. `## What "stuck" can mean` — names three concrete categories:
     (a) daemon process dead, (b) daemon alive but not polling,
     (c) daemon polling but every dispatch fails. Each category is
     1-3 sentences.
  2. `## First five commands` — a numbered (`1.` ... `5.`) list of at
     least five exact shell commands an operator can copy-paste. The
     commands MUST include, in order:
     - `docker ps --filter name=foreman`
     - `docker logs foreman-daemon | tail` (or `docker compose logs
       --tail=50 daemon` — Worker chooses the form most consistent with
       `docs/RUNBOOK.md:75` which uses `docker compose logs`)
     - `docker exec foreman-daemon python -c "import os;
       print(os.stat('/foreman/state/v3-daemon.log').st_mtime)"`
     - `foreman ps`
     - At least one `sqlite3` query against `execution_log` that returns
       unterminated-running rows. The recommended form mirrors
       `ExecutionLog.recover_orphaned` at
       `packages/foreman/src/foreman/reconciler/exec_log.py:375-391`:
       `sqlite3 /foreman/state/reconciler.sqlite "SELECT id, ts,
       ticket_id, action FROM execution_log WHERE outcome='running'
       AND id NOT IN (SELECT parent_log_id FROM execution_log WHERE
       parent_log_id IS NOT NULL);"`
  3. `## Interpreting the signals` — for each of the five commands
     in §2, a 1-2 line "healthy looks like X / stuck looks like Y"
     pairing. Format is the operator's choice (sub-headers, bulleted
     list, or table) as long as every command from §2 has a
     corresponding entry.
  4. `## Three most common stuck-shapes` — exactly three sub-sections,
     each with a named cause and a one-paragraph fix. The three
     stuck-shapes MUST be (the exact names below are what the
     interpreting-signals section will point at):
     - **Daemon process crashed silently** — fix: re-`up` the
       container via `docker compose up -d daemon`, then check
       `docker compose logs --tail=200 daemon` for the crash
       traceback (the daemon log goes to stderr inside the container
       and to `/foreman/state/v3-daemon.log` via
       `configure_daemon_logging` at `cli.py:657`).
     - **Stale reconciler lock blocks startup** — fix: the lock at
       `~/.foreman/reconciler.lock` (or `/foreman/state/reconciler.lock`
       in the container) is held by an OS-level `flock` that releases
       on process death (see `packages/foreman/src/foreman/daemon_lock.py:78`
       — "Closing the fd releases the OS lock"). If `foreman daemon
       status` reports "stale lock file (pid N dead)" per
       `cli.py:1126-1131`, the lock has already been released by the
       kernel and the next `daemon v3-start` will overwrite the file —
       no manual `rm` needed. The paragraph names this fix explicitly
       so operators don't reflexively `rm` the lock.
     - **Dispatch capacity cap reached** — fix: every running dispatch
       holds a slot in the semaphore at
       `packages/foreman/src/foreman/reconciler/v3_host.py:433`
       (`self._dispatch_capacity = threading.Semaphore(max_concurrent_dispatches)`);
       a `dispatch skipped role=... — concurrency cap N reached`
       line at `v3_host.py:800-806` in the daemon log is the
       diagnostic signal. Fix: confirm the in-flight `running` rows
       in `execution_log` correspond to live subprocesses (use the
       SQL from §2); if any row's parent process is dead, restart the
       daemon to trigger `ExecutionLog.recover_orphaned`
       (`exec_log.py:375`) which marks each orphan
       `errored:recovery` and frees the slot.
  5. `## When to escalate` — a bulleted list of conditions that
     warrant filing a ticket and surfacing to a human. At minimum:
     "the SQL query returns unterminated rows older than 1h after
     a daemon restart" (recover_orphaned didn't help), "the daemon
     log shows the same crash traceback on every restart", and
     "`foreman ps` and the `execution_log` query disagree about
     what's in flight". Worker may add a fourth bullet if it falls
     out naturally from §3/§4, but MUST NOT exceed five bullets.
- The doc is under 100 lines total, measured by `wc -l
  docs/troubleshooting/stuck-dispatch.md`. Content inside fenced
  code blocks (` ``` ... ``` `) is EXCLUDED from the count per the
  issue's wording, but the doc is still expected to read tight —
  the Worker should not pad prose to use the budget.
- Voice is operator-facing: imperative ("run X", "check Y"), short
  paragraphs, no design-doc tangents (no "rationale", no GoF pattern
  names, no class-diagram prose). Style precedent: `docs/RUNBOOK.md`
  (which is the closest existing analog — it's the day-to-day
  operator manual; this new file is the diagnostic peer).
- No source files outside `docs/troubleshooting/` are edited. No
  Python code changes. No test changes. `just check` should still
  pass since it's a doc-only change — the Worker MUST run `just
  check` once before opening the PR to confirm no incidental lint
  drift (the markdown isn't linted, but the gate also runs ruff +
  mypy + pytest, which a doc-only PR should leave green).

## Approach

The work is one new markdown file. The Worker creates
`docs/troubleshooting/` (the directory does not currently exist —
verified via `ls docs/`) and writes
`docs/troubleshooting/stuck-dispatch.md` inside it.

**Source-of-truth reading the Worker MUST do before writing:**

- `docs/RUNBOOK.md` (the day-to-day operator runbook) — match its
  voice and its `docker compose ...` command style. RUNBOOK already
  has a "Recovery: daemon won't start" section at line 129; this new
  file is the more granular "stuck dispatch" peer to that.
- `packages/foreman/src/foreman/reconciler/v3_host.py` lines 50-75
  (`resolve_log_dir`, `resolve_state_dir`) for the state-dir and
  log-dir conventions the §2 commands target. The container path
  is `/foreman/state/v3-daemon.log` (set by compose's
  `FOREMAN_STATE_DIR=/foreman/state`).
- `packages/foreman/src/foreman/reconciler/exec_log.py` lines 375-391
  (`recover_orphaned`) for the canonical orphan-detection SQL shape
  the §2 SQL command mirrors. The doc should NOT explain
  `recover_orphaned` itself (out of scope per issue) but the SQL
  query operators run by hand is structurally the same.
- `packages/foreman/src/foreman/reconciler/v3_host.py` lines 799-807
  (cap-skipped log line) and line 884
  (`logger.info("dispatched role=... pid=... log=... argv=...")`)
  for the exact log strings §3 tells operators to grep for.
- `packages/foreman/src/foreman/cli.py` line 1268-1276 (`ps_cmd`)
  for what `foreman ps` actually prints today (`PROJECT ISSUE
  STATE STARTED` header, "(no active pipelines)" empty state per
  `packages/foreman/src/foreman/ps.py:10-17`). The §3 interpreting
  block names what healthy vs. empty output looks like.
- `docker/entrypoint.sh` lines 60-68 (the `container_start` JSON
  banner) so §3 can tell operators which line confirms the daemon
  actually started inside the container, distinct from the "container
  is up" signal from `docker ps`.

**Structural decisions:**

- The file MUST open with a 1-2 line lede that names the audience
  (operators), what they'll diagnose (a daemon that looks stuck),
  and the time budget (two minutes). No background, no architecture.
- Use fenced code blocks with the appropriate language tag (`bash`
  for shell, `sql` for the SQLite query) so syntax highlighting
  works in GitHub's renderer.
- Cross-references to source files (e.g., "the dispatch-skipped log
  comes from `v3_host.py:800`") are fine inline but should be sparse
  — operator readers don't need code archaeology, they need the
  command and the fix.

**What this doc deliberately does NOT do** (each is a separately-
tracked follow-up per the issue's out-of-scope list):

- Add a `foreman doctor` CLI.
- Modify the daemon to surface stuck-ness more loudly.
- Add telemetry for lock-file age.
- Generalize to a broader ops runbook (RUNBOOK.md already exists).

## Sub-requests (topologically sorted)

1. Create the directory `docs/troubleshooting/` (e.g., via the
   parent path being implied by the file write; on POSIX systems
   `mkdir -p docs/troubleshooting` works, but most editors will
   create the parent path as part of writing the file).
2. Write `docs/troubleshooting/stuck-dispatch.md` following the
   five-section structure under Acceptance criteria, drawing the
   exact log strings and SQL shapes from the source files listed
   under Approach. The five sections, with their headings spelled
   exactly:
   - `## What "stuck" can mean`
   - `## First five commands`
   - `## Interpreting the signals`
   - `## Three most common stuck-shapes`
   - `## When to escalate`
3. Run `wc -l docs/troubleshooting/stuck-dispatch.md` and confirm
   the count is under 100. If over budget, tighten prose — do NOT
   drop required sections or required commands.
4. Run `just check` from the repo root. Confirm exit 0. The gate
   covers lint + typecheck + tests; a doc-only PR should be a no-op
   for all three, so any failure is unrelated drift the Worker must
   surface separately rather than absorb into this PR.
5. Stage and commit:
   ```bash
   git add docs/troubleshooting/stuck-dispatch.md
   git commit -m "docs(troubleshooting): add stuck-dispatch note for operators"
   ```
   (Conventional-commit `docs(troubleshooting)` matches the title
   style and passes `pr-title-lint`.)

## File-level changes

| File | Change |
| --- | --- |
| `docs/troubleshooting/stuck-dispatch.md` | **New file.** Operator troubleshooting note, five sections per Acceptance criteria, under 100 lines (excluding fenced code). |
| `docs/troubleshooting/` (directory) | **New directory.** Created implicitly by the file write. No other content. |

No expected changes to (sanity-checked via grep / ls):

- `docs/RUNBOOK.md` — adjacent but unchanged. The new doc is a peer,
  not a replacement.
- `docs/dispatch-recorder-design.md` — design doc, unrelated.
- `packages/foreman/src/foreman/reconciler/v3_host.py` — read-only
  reference for log strings; no edits.
- `packages/foreman/src/foreman/reconciler/exec_log.py` — read-only
  reference for SQL shape; no edits.
- `packages/foreman/src/foreman/cli.py` — read-only reference for
  `foreman ps` output; no edits.
- `docker/entrypoint.sh` — read-only reference for the startup
  banner JSON; no edits.

## Verification

Before opening the impl PR, the Worker MUST run and record:

1. `ls docs/troubleshooting/stuck-dispatch.md` — exits 0; confirms
   file at exact path.
2. `wc -l docs/troubleshooting/stuck-dispatch.md` — total line
   count; should be under 100.
3. `grep -c '^## ' docs/troubleshooting/stuck-dispatch.md` — count
   of top-level sections; MUST equal 5.
4. `grep -E '^## ' docs/troubleshooting/stuck-dispatch.md` — list
   the five section headings; MUST appear in the order specified in
   Acceptance criteria.
5. `just check` — exit code 0. Capture pass/fail summary so the
   Reviewer can confirm the doc-only PR didn't pick up unrelated
   drift.

## Alternatives considered

- **Add the troubleshooting content as a new section inside
  `docs/RUNBOOK.md` instead of a new file.** Rejected — RUNBOOK is
  already 200+ lines and serves a different purpose (daily ops,
  cutover ritual, recovery for "won't start"). The issue explicitly
  asks for a "single concise note an operator can read in 2 minutes
  and act on" — embedding it in RUNBOOK buries the lede. A
  standalone file under `docs/troubleshooting/` also leaves room for
  a small directory of peer troubleshooting notes later (out of
  scope here, but the path is the future-friendly choice).

- **Put it at the repo root as `TROUBLESHOOTING.md`.** Rejected —
  the existing convention is operator-facing docs live under
  `docs/` (RUNBOOK is there; architecture docs are at
  `docs/architecture/`; superpowers specs at
  `docs/superpowers/specs/`). The issue body explicitly names the
  path `docs/troubleshooting/stuck-dispatch.md`; matching that path
  honors the issue's intent and the repo's existing layout.

- **Auto-generate the doc from `exec_log.py` schema + log-string
  constants so the doc can't drift from code.** Rejected — YAGNI
  for a doc-only PR. The doc is short enough to maintain by hand;
  the file paths and log strings it cites are stable. If drift
  becomes a recurring problem, a follow-up issue can add a doc-gen
  step.

- **Make this doc a `--help` blurb on a future `foreman doctor`
  CLI command instead of a markdown file.** Rejected explicitly by
  the issue's out-of-scope rule ("Building a CLI for self-diagnosis
  (`foreman doctor`). Separate ticket if useful.").

## Open questions

(none — the issue is unambiguous, the paths and signals to cite all
exist in the repo today, and the voice + length budget are explicit.)

## Out of scope

- **Building `foreman doctor` or any self-diagnosis CLI.** Issue
  explicitly defers this to a separate ticket.
- **Modifying the daemon to surface stuck-ness more loudly.** Issue
  explicitly defers this.
- **Adding telemetry for lock-file age.** Issue explicitly defers
  this.
- **Generalizing the doc into a broader ops runbook.** Issue
  explicitly scopes this to dispatch-stuck only.
- **Editing or removing `docs/RUNBOOK.md`'s "Recovery: daemon won't
  start" section.** That section stays exactly as it is at
  `RUNBOOK.md:129`. This new file is a peer that handles the
  finer-grained dispatch-stuck case; the broader "daemon won't
  start at all" path stays in RUNBOOK.
- **Adding the new doc to a README index or table of contents.**
  No such index exists today; introducing one is out of scope for
  a single-doc PR.
- **Creating `docs/logging-coverage.md`.** The issue's References
  list mentions this file as "adjacent telemetry inventory" but
  it does not exist on `main` (verified via `find docs/ -name
  "logging-coverage.md"` returning nothing). The new doc may
  reference its name in a "see also" line if helpful, but the
  Worker MUST NOT create the file as part of this PR — that's
  a separate doc with its own scope.
- **Linting the markdown.** No markdown linter is configured in
  this repo (`just check` runs ruff + mypy + pytest, no
  markdownlint). The Worker should not introduce one.
