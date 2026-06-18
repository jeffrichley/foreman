# Spec: harden daemon JSON-lines log writer against silent-stop failure mode (issue #323)

## Goal
Reduce the blast radius of the failure mode described in [foreman#323](https://github.com/jeffrichley/foreman/issues/323) — a structured-log writer silently stops emitting while the daemon continues working — by adding defensive coverage to the existing `_JsonLinesFormatter` + `logging.FileHandler` pipeline in `packages/foreman/src/foreman/logging_setup.py`. The literal v4 files the issue points at do not exist in this repo (see Open questions), so this spec applies the *spirit* of the fix to the code that actually exists today; it does **not** create a v4 daemon, a `transitions.jsonl` writer, or a `JsonLinesHandler` class.

## Acceptance criteria
- [ ] A new test in `packages/foreman/tests/test_logging_setup.py` asserts that a single `logger.info(...)` call is visible on disk **without** the caller calling `handler.flush()` first. (This proves the current `FileHandler.emit()` → `StreamHandler.emit()` chain already flushes per-record, and locks that behavior in as a regression guard.)
- [ ] A new test asserts that a non-JSON-serializable value passed via `extra={...}` does **not** silently disable the `foreman` logger's file handler. The expected behavior is: the offending record is dropped (or rendered with `default=str`, which is what `_JsonLinesFormatter` already does — verify), the handler stays attached, and the **next** record after the bad one lands on disk. This is the in-codebase analog of "an exception inside emit() triggered handleError() which silently swallowed and disabled the handler".
- [ ] A new test calls `configure_daemon_logging(...)` twice in a row (re-entry, the closest analog to the issue's "Phase 8.5 SIGHUP reset"), writes one record after each call, and asserts **both** records land on disk and are JSON-parseable. Today `test_configure_daemon_logging_idempotent_does_not_accumulate_own_handlers` verifies handler count after re-entry; this new test extends that to verify **writes continue to work** after re-entry, closing the "stranded FD" concern from the issue.
- [ ] All three new tests pass; existing tests in `test_logging_setup.py` continue to pass; `just check` exits zero (Worker enforces `new_failures_count == 0`).
- [ ] No production code changes unless the third test surfaces a real defect. If the new tests all pass on the unmodified `logging_setup.py`, the spec is satisfied — the tests are the deliverable (regression guards against a bug-class that *might* manifest later, especially if a v4 logging layer ever lands).

## Approach
The issue describes a custom `JsonLinesHandler` in a non-existent `packages/foreman/src/foreman/v4/json_lines_handler.py` whose `emit()` allegedly lacks `self.stream.flush()`. In this repo the equivalent surface is the stdlib `logging.FileHandler` instantiated in `packages/foreman/src/foreman/logging_setup.py:113`, wrapped by `_JsonLinesFormatter` (`logging_setup.py:53-74`). `logging.StreamHandler.emit()` (which `FileHandler` inherits) already calls `self.flush()` after every `write`, so the **literal** "missing per-write flush" bug cannot apply here.

What *can* apply, and what this spec hardens against, is the broader failure class — the issue's own root-cause sketch (item 2 of the bug body):

> The fact that writes stop and never resume — not slow, not buffered, fully silent — suggests either (a) the file handle was closed by something the handler doesn't notice, or (b) an exception inside `emit()` triggered `Handler.handleError()` which silently swallowed and disabled the handler.

Path (b) is testable in the existing codebase: if a caller passes a non-JSON-serializable value via `extra={...}`, `_JsonLinesFormatter.format()` calls `json.dumps(payload, default=str)`. The `default=str` fallback should absorb every Python object that isn't already JSON-native, but `_STANDARD_RECORD_FIELDS` excludes some keys and the formatter's `for key, value in record.__dict__.items()` loop is loose. A regression test that feeds the formatter a payload designed to provoke a formatting exception locks down the boundary: either it works (good, prove it) or it surfaces a real defect (good, fix it now).

Path (a) — stranded file descriptor across handler replacement — is testable via the existing re-entry flow at `logging_setup.py:107-116`. We already verify the handler **count** stays at 1 after re-entry; we don't yet verify that **writes after re-entry actually land on disk**. The extension is small and the bug-class is exactly what the issue is asking us to guard against.

**Pattern naming (per CLAUDE.md Decision 4):** No GoF pattern fits — this is straightforward defensive test coverage on the boundary of a stdlib class. The closest engineering principle is "make the right thing easy" applied to operators: when an operator suspects the log writer is broken, they should be able to point at a recent passing test that proves it isn't. The "make the wrong thing loud" corollary applies to path (b): if a formatter exception ever does silently disable the handler, a regression test wired exactly at that boundary catches it.

## Sub-requests (topologically sorted)
1. Add `test_logger_info_writes_to_disk_without_explicit_flush` to `packages/foreman/tests/test_logging_setup.py`. The test: call `configure_daemon_logging(...)`, immediately `logger.info("payload", extra={"k": "v"})`, then read `log_path.read_text()` and assert the line is present **without** an intervening `handler.flush()` call. This proves `FileHandler.emit()` is flushing per-record today (regression guard against a future refactor that swaps `FileHandler` for a buffered handler).
2. Add `test_non_json_extra_does_not_disable_handler` to the same file. The test: call `configure_daemon_logging(...)`, then `logger.info("first", extra={"obj": <something exotic, e.g. a `set` of `bytes`>})`, then `logger.info("second", extra={"ticket": 1})`. Assert that `"second"` appears in the log file's last line and that the `foreman` logger still has exactly one `FileHandler`. (The first record may or may not appear depending on how `_JsonLinesFormatter`'s `default=str` handles it; the load-bearing assertion is that the **handler survived**.)
3. Add `test_re_entry_does_not_strand_writes` to the same file. The test: call `configure_daemon_logging(...)`, `logger.info("before-reentry", ...)`, call `configure_daemon_logging(...)` again with the same `log_path`, `logger.info("after-reentry", ...)`, then assert both messages appear in the file. This is the in-codebase analog of the SIGHUP-reset regression test the issue asks for.
4. Run `just check` from the repo root. Confirm zero new test failures.
5. If step 2 surfaces a real defect in `_JsonLinesFormatter` (e.g., a payload that bypasses `default=str` and raises out of `format()`), add a minimal defensive `try/except` in `_JsonLinesFormatter.format()` that emits a fallback record (`{"timestamp": ..., "level": "ERROR", "logger": "foreman.logging_setup", "message": "log record dropped: <repr>"}`) instead of letting the exception escape into `Handler.handleError()`. Only do this if step 2 actually fails; do not pre-emptively add the try/except.

## File-level changes
| File | Change |
|------|--------|
| `packages/foreman/tests/test_logging_setup.py` | Add three new tests (sub-requests 1–3). |
| `packages/foreman/src/foreman/logging_setup.py` | **Only if sub-request 2's test fails on the unmodified code**: add a defensive `try/except` around `_JsonLinesFormatter.format()`'s body (sub-request 5). If the test passes as-is, no production change. |

## Alternatives considered
- **Build a v4 logging layer matching the issue's source pointers (`packages/foreman/src/foreman/v4/json_lines_handler.py` et al.).** Rejected: massive scope, no existing scaffolding, and the issue is filed as a bug-fix, not a new-architecture ticket. If a v4 daemon is the eventual target, that's a separate plan-level discussion, not a single-issue fix.
- **Do nothing — recommend the issue be closed as "files do not exist in this repo".** Rejected: the underlying failure class (silent handler death + stranded FD across re-entry) is real and observable in any logging setup; locking in regression tests against the equivalent surface in `logging_setup.py` is a cheap, durable win even if the original report came from a different codebase.
- **Swap `logging.FileHandler` for an explicit `open(..., 'a', buffering=1) + custom Handler` with `os.fsync` per emit.** Rejected: `FileHandler` already flushes per-record; replacing it adds a custom-handler maintenance surface for no observable behavior change, and `os.fsync` per emit is a measurable performance regression that the issue does not justify (the operator's UX complaint is about *visibility under `tail -F`*, which line-flush already covers; durability across power-loss isn't the bug).
- **Add a periodic handler-health-check task (the issue's "or" branch in section 2 of Approach).** Rejected: out of proportion to a regression-test fix, introduces a new background task to schedule and shut down, and there's no evidence the current code needs it. Revisit if the test in sub-request 3 ever actually fails.

## Open questions
- **The issue's source pointers do not exist in this repo.** `packages/foreman/src/foreman/v4/` is not present; there is no `json_lines_handler.py`, no `logging_config.py` under v4, no `subprocess_dispatcher.py`, and no `transitions.jsonl` output path. The actual daemon log path is `~/.foreman/daemon.log` (`config.py:90`), written by `logging.FileHandler` (`logging_setup.py:113`). The Reviewer should sanity-check: is this issue meant for a different repo (e.g., a private v4 fork, or the `agent_core` repo referenced in the issue body)? If yes, this spec should be closed and the issue rerouted upstream rather than implemented here.
- **The issue references "Phase 8.5 SIGHUP handler reset."** This repo's `cli.py:1080` explicitly says "Sentinel-only on every platform — no SIGHUP — to keep the latency profile symmetric"; see also the rationale captured in `docs/superpowers/specs/foreman-issue-100-spec.md:270`. There is no SIGHUP handler to regression-test. Sub-request 3 covers the closest analog (idempotent re-entry of `configure_daemon_logging`); the Reviewer should confirm that's an acceptable substitute given the architecture mismatch.
- **The operator's symptom (writes stop, never resume) is not currently observed against `~/.foreman/daemon.log` in this repo's known operator reports** — the issue body's concrete observation is dated against a `C:/Users/jeffr/.foreman/v4/logs/transitions.jsonl` path which does not exist for this codebase. If the bug ever does manifest against `daemon.log`, the three regression tests landed here will narrow the suspect surface; if it never does, they remain cheap insurance.

Confidence rationale: **low**. The mapping from issue → codebase is uncertain enough that the Reviewer should make an explicit decision about whether this spec is the right interpretation or whether the issue should be rerouted before the Worker runs.

## Out of scope
- Creating a `packages/foreman/src/foreman/v4/` directory or any v4 module.
- Implementing a `JsonLinesHandler` custom class.
- Adding a `transitions.jsonl` output path or any new log file.
- Implementing SIGHUP handling for the daemon — `cli.py` explicitly designs against this and `docs/superpowers/specs/foreman-issue-100-spec.md` records the decision.
- Migrating away from the stdlib `logging.FileHandler` + `_JsonLinesFormatter` pipeline.
- Adding `os.fsync` for crash-durability (the issue describes a visibility bug, not a durability bug).
- Adding a background handler-health-check task.
- Rotation policy / log compaction for `daemon.log` (explicitly out of scope in the issue body too).
