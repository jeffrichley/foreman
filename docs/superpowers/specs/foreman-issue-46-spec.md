# Spec: render `exc_info` in JSON-lines log formatter (issue #46)

## Goal

Make `foreman.logging_setup._JsonLinesFormatter` honor `record.exc_info`
(and `record.stack_info`) so that calls to `logger.exception(...)` —
which the daemon already uses on poll failures — produce a JSON line that
includes the exception type, message, and traceback. Today these calls
silently emit just the message line, hiding the failure mode from
operators tailing `~/.foreman/daemon.log`.

Tracks issue [#46](https://github.com/jeffrichley/foreman/issues/46).

## Acceptance criteria

- When a record is logged via `logger.exception(...)` (or any path that
  sets `record.exc_info` to a 3-tuple), the JSON line written by
  `_JsonLinesFormatter` contains a top-level `"exception"` object with
  three keys:
  - `"type"`: the exception class's `__name__` (string).
  - `"message"`: `str(exception_instance)`.
  - `"traceback"`: the formatted traceback string, as produced by
    `logging.Formatter.formatException(record.exc_info)`.
- When a record is logged with `stack_info=True`, the JSON line contains a
  top-level `"stack_info"` string key holding the formatted stack info
  (as produced by `logging.Formatter.formatStack(record.stack_info)`).
- Records logged without `exc_info` or `stack_info` produce the exact
  same JSON output they do today — no new keys, no reordering of existing
  keys, no change to `timestamp`/`level`/`logger`/`message` or to the
  top-level extras merge.
- The existing daemon call site
  `packages/foreman/src/foreman/daemon.py:141-145`
  (`_log.exception("poll_project failed", extra={"project": ...})`) is
  unchanged — the fix is in the formatter, not the call sites.
- A new test in `packages/foreman/tests/test_logging_setup.py` raises a
  real exception inside a `try/except`, calls `_log.exception(...)` from
  inside the `except` block, reads the resulting JSON line, and asserts
  the `exception.type`, `exception.message`, and `exception.traceback`
  fields are populated correctly (type name matches, message matches,
  traceback contains the raising frame).
- A second new test asserts that `stack_info=True` populates the
  `"stack_info"` top-level key.
- A third new test asserts that an ordinary `logger.info(...)` call
  produces a JSON line with no `"exception"` and no `"stack_info"` keys
  (regression guard for the "no change when absent" criterion).
- All existing tests in `packages/foreman/tests/test_logging_setup.py`
  continue to pass.
- `just check` exits zero.

## Approach

The bug is in `packages/foreman/src/foreman/logging_setup.py:45-57`. The
formatter builds its payload by reading a hand-picked set of fields off
the `LogRecord` (`timestamp`, `level`, `logger`, `message`) and then
merging in everything that ISN'T a standard `LogRecord` attribute as
top-level `extra`. `exc_info` and `stack_info` are both in
`_STANDARD_RECORD_FIELDS` (lines 18-42), so they're correctly filtered
out of the `extra` merge — but the formatter never reads them
separately, so they're dropped on the floor.

Python's stdlib `logging.Formatter` has the same two-stage pattern we
need:
- `formatException(exc_info)` renders the `(exc_type, exc_value, tb)`
  tuple into a string identical to what `traceback.print_exception()`
  would produce.
- `formatStack(stack_info)` renders the optional stack info string.

Both methods are available on the base class we already inherit, so the
fix is local and additive:

1. After building the base payload but before `return json.dumps(...)`,
   check `record.exc_info`. If it's a 3-tuple (not `None`), build an
   `exception` dict with `type`, `message`, `traceback` and add it to
   the payload.
2. Check `record.stack_info`. If non-empty, add `stack_info` as a
   top-level string field.

The `extra` merge loop is left alone — it already skips both attributes
via `_STANDARD_RECORD_FIELDS`, so the new code is purely additive and
the rendering for non-exception records is byte-identical to today's
output.

Why this shape:
- It matches the issue's "Fix sketch" verbatim: `exception.type`,
  `exception.message`, `exception.traceback`.
- It reuses `logging.Formatter.formatException`, which is the canonical
  way to render `exc_info` in Python's logging stack (used by every
  built-in formatter, including the one Rich wraps). No risk of
  divergence from `traceback.print_exception()` output.
- It keeps the formatter pure — no I/O, no global state, no new
  dependencies.
- It does not change the JSON schema for non-exception records, so log
  aggregators that consume `~/.foreman/daemon.log` see no surprise
  field changes; the new keys appear only on records that actually
  carry exception/stack info.
- It pairs cleanly with the existing `rich_tracebacks=True` on the
  RichHandler (`logging_setup.py:99`) — humans see Rich-formatted
  tracebacks on the terminal; machines see the same exception serialized
  as JSON in the file. Consistent surface, two renderings.

Test approach mirrors the convention already in
`packages/foreman/tests/test_logging_setup.py:12-28`: configure the
logger to write JSON to a tmp file, log, flush handlers, read the file,
parse each line as JSON, assert on field values. The new tests follow
the exact same shape — only the logger call and assertions change.

## Sub-requests (topologically sorted)

1. In `packages/foreman/src/foreman/logging_setup.py`, inside
   `_JsonLinesFormatter.format()` (lines 45-57), after the existing
   `for key, value in record.__dict__.items()` loop and before
   `return json.dumps(payload, default=str)`, add two conditional
   blocks:
   - If `record.exc_info` is truthy, populate
     `payload["exception"] = {"type": ..., "message": ..., "traceback": self.formatException(record.exc_info)}`.
     Guard `record.exc_info[0]` and `record.exc_info[1]` for `None` (a
     defensive practice the issue's fix sketch already calls out — the
     tuple slots can be `None` in edge cases like `sys.exc_info()`
     called outside an `except` block).
   - If `record.stack_info` is truthy, set
     `payload["stack_info"] = self.formatStack(record.stack_info)`.
2. In `packages/foreman/tests/test_logging_setup.py`, add a new test
   `test_configure_daemon_logging_renders_exc_info` that:
   - Calls `configure_daemon_logging(log_path=tmp_path / "daemon.log", level="INFO", console=False)`.
   - Inside a `try`, raises `ValueError("bad creds")`; inside `except`,
     calls `logger.exception("poll_project failed", extra={"project": "foreman"})`
     on a logger named `"foreman.daemon.test_exc"`.
   - Flushes handlers, reads the last line, parses JSON.
   - Asserts:
     - `record["message"] == "poll_project failed"`
     - `record["level"] == "ERROR"`
     - `record["project"] == "foreman"`
     - `record["exception"]["type"] == "ValueError"`
     - `record["exception"]["message"] == "bad creds"`
     - `"Traceback" in record["exception"]["traceback"]`
     - `"ValueError: bad creds" in record["exception"]["traceback"]`
3. In `packages/foreman/tests/test_logging_setup.py`, add a new test
   `test_configure_daemon_logging_renders_stack_info` that calls
   `logger.info("hello", stack_info=True)`, parses the resulting JSON
   line, and asserts `record["stack_info"]` is a non-empty string
   containing `"Stack (most recent call last)"` (the prefix stdlib's
   `formatStack` emits).
4. In `packages/foreman/tests/test_logging_setup.py`, add a new test
   `test_configure_daemon_logging_omits_exception_key_when_absent` that
   logs a plain `logger.info("hello", extra={"ticket": 1})`, parses the
   resulting JSON line, and asserts both `"exception" not in record` and
   `"stack_info" not in record` — the regression guard for the
   schema-stability acceptance criterion.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/logging_setup.py` | Extend `_JsonLinesFormatter.format()` with two conditional blocks that render `record.exc_info` into a structured `"exception"` object and `record.stack_info` into a top-level `"stack_info"` string. |
| `packages/foreman/tests/test_logging_setup.py` | Add three new tests: exception rendering, stack_info rendering, and absence-of-keys regression guard. |

No other files change. `daemon.py` already uses `_log.exception(...)` at
the call site cited in the issue — the fix is purely in the formatter,
and that call site benefits automatically without modification.

## Alternatives considered

- **Switch the formatter to inherit nothing custom and instead override
  the stdlib `logging.Formatter`'s `format()` by calling
  `super().format(record)` first.** Rejected: the base class's `format`
  returns a single string with the traceback appended to the message,
  which would force us to either parse it back out or accept the
  traceback being concatenated into the JSON `"message"` field. Both are
  worse than just calling `formatException` directly on `exc_info`.
- **Render the traceback as a list of frames (using
  `traceback.extract_tb`) instead of a single string.** Rejected: the
  issue's fix sketch asks for `traceback` as a string, that matches
  every JSON-logging library convention (structlog, python-json-logger,
  loguru), and stringified tracebacks grep cleanly in log aggregators.
  Frame lists are more work to render and harder to read in `jq`.
- **Add a separate `"exc_type"`, `"exc_message"`, `"exc_traceback"` set
  of top-level keys instead of a nested `"exception"` object.**
  Rejected: nesting groups the three together for clean filtering
  (`jq '.exception'`), and the issue's fix sketch explicitly proposes the
  nested shape.
- **Do nothing and rely on the RichHandler's pretty traceback on
  stderr.** Rejected: that only helps the human running the daemon in
  the foreground; the JSON file log is the source of truth for
  post-hoc forensics (and the daemon is typically run in the background
  via the launcher). The issue's 8-hour debugging story is exactly the
  case Rich-on-stderr doesn't cover.

## Open questions

(none — the fix surface is small, the stdlib APIs needed
(`formatException`, `formatStack`) are well-documented, and the
existing test file demonstrates the JSON-roundtrip assertion pattern
this work needs to extend.)

## Out of scope

- Restructuring the formatter into a generic structlog-style processor
  pipeline. Issue is a one-field bug, not a re-architecture.
- Adding `exc_info` rendering to the RichHandler stream — RichHandler
  already has `rich_tracebacks=True` (`logging_setup.py:99`), so it
  already renders tracebacks on stderr; only the file sink was broken.
- Changing the JSON-lines schema for non-exception records (no new
  default fields, no key renames, no reordering).
- Audit/refactor of `_log.exception` vs `_log.error` call-site choices
  elsewhere in the codebase. The bug is the formatter; call sites are
  correct.
- Daemon token-refresh / foreman#44 itself (the companion issue). This
  PR makes that bug easier to diagnose next time; it does not fix it.
