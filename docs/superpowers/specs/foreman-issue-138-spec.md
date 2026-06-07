# Spec: mirror v3 daemon log to stdout for `docker logs` visibility (issue #138)

## Goal

Make `docker logs foreman-daemon` actually useful. Today the v3 reconciler's
JSON-lines log goes only to `/foreman/state/v3-daemon.log` via a
`FileHandler`, and the container's stdout shows nothing but the entrypoint
banner — so ops have to `docker exec ... cat /foreman/state/v3-daemon.log`
to see anything. Extend `configure_daemon_logging` so the same JSON-lines
payload is also emitted to stdout via a stdlib `StreamHandler`. See
[#138](https://github.com/jeffrichley/foreman/issues/138).

## Acceptance criteria

- `configure_daemon_logging(log_path=..., level=..., console=True)` attaches
  exactly two handlers to the `foreman` logger: a `FileHandler` (writing
  JSON lines to `log_path`, unchanged from today) AND a stdlib
  `logging.StreamHandler` bound to `sys.stdout`.
- Both handlers use the same `_JsonLinesFormatter` instance type — so a
  single `logger.info(..., extra={...})` call produces byte-identical
  JSON-line records on disk and on stdout (the timestamp string and any
  exception traceback render identically; only the destination differs).
- The `RichHandler` block (and the lazy `from rich.console import Console`
  / `from rich.logging import RichHandler` imports inside the `if console:`
  block) is removed. The function no longer imports `rich` under any code
  path. (The `rich` distribution stays in `packages/foreman/pyproject.toml`
  for now — see `## Out of scope`.)
- `configure_daemon_logging(..., console=False)` continues to attach the
  `FileHandler` only (no `StreamHandler`, no `RichHandler`). All existing
  tests that pass `console=False` keep passing without modification.
- A new test
  `test_configure_daemon_logging_mirrors_to_stdout_as_json_when_console_true`
  in `packages/foreman/tests/test_logging_setup.py` uses pytest's `capsys`
  to capture stdout, calls `configure_daemon_logging(..., console=True)`,
  logs a message with `extra={...}`, and asserts the captured stdout
  contains a single JSON line whose `message`, `level`, and the `extra`
  keys all decode correctly via `json.loads`.
- A new test
  `test_configure_daemon_logging_disk_and_stdout_payloads_match`
  in the same file logs once with `console=True` and asserts that the
  parsed JSON record on stdout equals the parsed JSON record in the file
  for every key (modulo any handler-local fields — there shouldn't be any).
- The function's module docstring is updated to describe the new dual
  destination ("FileHandler → `log_path`; StreamHandler → `sys.stdout`;
  same JSON formatter on both") and to drop the RichHandler reference.
- `Dockerfile` is unchanged. The existing `ENV PYTHONUNBUFFERED=1` at
  line 26 already guarantees per-line stdout flush; the spec verifies
  this rather than altering it.
- `just check` passes (lint + typecheck + tests).

## Approach

The v3 daemon entry path is straightforward to trace:

- `foreman daemon v3-start` (`packages/foreman/src/foreman/cli.py:564`)
  calls `configure_daemon_logging(log_path=v3_log_path, level=...)` at
  `cli.py:601-604` with `console` defaulted to `True`.
- `configure_daemon_logging` lives at
  `packages/foreman/src/foreman/logging_setup.py:69-115` and attaches a
  `FileHandler` (always) plus a `RichHandler` writing pretty output to
  `Console(stderr=True)` (when `console=True`).

The issue's acceptance criteria specify two handlers — a `FileHandler`
and a `StreamHandler` — both emitting the same JSON-lines format. The
existing `RichHandler` does neither (it writes pretty-formatted text to
stderr, not JSON, and not to stdout). The minimal, contract-honest
change is to replace the `RichHandler` block with a stdlib
`logging.StreamHandler(sys.stdout)` configured with the same
`_JsonLinesFormatter()` already used by the file handler.

Rationale for replace (rather than add a third handler):

- The issue's AC says "attaches BOTH a FileHandler ... AND a
  StreamHandler" — "both" implies exactly two, not three.
- The RichHandler's value (pretty colored output to a TTY) is realised
  only when a human is watching the foreground daemon directly. The v3
  daemon runs in Docker by design — `docker/entrypoint.sh:52` exec's
  `foreman daemon v3-start` as PID-1's child — so the TTY branch is
  inert in the supported runtime. An operator who runs the daemon
  directly on a Linux host still gets the full JSON stream on stdout
  and can pipe through `jq` for human formatting (`foreman daemon
  v3-start | jq`). That preserves operator UX without baking in a
  TTY-aware code path.
- Keeping three handlers (File + Rich + StreamHandler) would produce
  duplicate output to two different streams (stderr from Rich, stdout
  from StreamHandler) every time the daemon logs — confusing to read
  and confusing to test.

The shared helper `configure_daemon_logging` is also called by the v2
daemon at `packages/foreman/src/foreman/daemon.py:66-69`. v2 is
deprecated (see `daemon.py:1-12`) but still importable. Changing the
shared helper means v2 also gets JSON-on-stdout instead of pretty-on-
stderr for whatever brief remaining life it has. That is acceptable:
v2's foreground UX is not a documented contract and the migration spec
at `docs/superpowers/specs/foreman-issue-106-spec.md` already calls for
its removal post-cutover. No change to the v2 call site is needed.

PYTHONUNBUFFERED=1 is already set at `Dockerfile:26`. `StreamHandler`
also calls `self.stream.flush()` after every `emit()` by default
(stdlib `logging.StreamHandler.emit` flushes unconditionally on success),
so per-line flush is double-guaranteed — no further changes to the
Dockerfile or entrypoint are required, and the spec explicitly captures
that verification rather than re-asserting it.

Test layout mirrors the existing patterns in
`packages/foreman/tests/test_logging_setup.py`: each test calls
`configure_daemon_logging(...)`, logs through a uniquely-named child
logger so prior-test handlers don't leak state, and reads either the
file (existing tests) or `capsys.readouterr().out` (new tests) to
assert structure. The two new tests use `console=True` and exercise
the previously-untested console path.

## Sub-requests (topologically sorted)

1. In `packages/foreman/src/foreman/logging_setup.py`: add `import sys`
   at the top of the file (alongside `import json` / `import logging`).
2. In the same file, replace the `if console:` block (currently
   `logging_setup.py:98-112`) with a stdlib
   `stream_handler = logging.StreamHandler(stream=sys.stdout)`;
   `stream_handler.setFormatter(_JsonLinesFormatter())`;
   `foreman_logger.addHandler(stream_handler)`. Remove the
   `from rich.console import Console` and
   `from rich.logging import RichHandler` imports inside the block.
3. Update the docstring of `configure_daemon_logging` (currently lines
   75-87): replace the "RichHandler writing pretty colored output to
   stderr" bullet with "StreamHandler writing JSON lines to
   ``sys.stdout`` — machine-readable mirror of the file payload so
   ``docker logs <container>`` and any log-aggregator that consumes
   container stdout see the same records, one per line." Keep the
   `console=False` behaviour bullet ("FileHandler only").
4. In `packages/foreman/tests/test_logging_setup.py`: add
   `test_configure_daemon_logging_mirrors_to_stdout_as_json_when_console_true(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None`.
   Body: call `configure_daemon_logging(log_path=tmp_path / "daemon.log",
   level="INFO", console=True)`; emit `logger.info("stdout-mirror",
   extra={"ticket": 138, "project": "foreman"})` through a uniquely-named
   child logger (`foreman.daemon.test_stdout_mirror`); flush every
   handler on the `foreman` logger; capture `capsys.readouterr().out`;
   parse the last non-empty line with `json.loads`; assert
   `record["message"] == "stdout-mirror"`, `record["level"] == "INFO"`,
   `record["ticket"] == 138`, `record["project"] == "foreman"`,
   `"timestamp" in record`.
5. In the same test file, add
   `test_configure_daemon_logging_disk_and_stdout_payloads_match(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None`.
   Body: call `configure_daemon_logging(log_path=tmp_path / "daemon.log",
   level="INFO", console=True)`; emit one log call through a uniquely-
   named child logger; flush all handlers; parse the last line of
   `log_path.read_text()` and the last line of `capsys.readouterr().out`;
   assert the two parsed dicts are equal.
6. Verify that none of the existing tests in `test_logging_setup.py`
   regress. All five existing tests pass `console=False` explicitly
   (`test_logging_setup.py:14, 33, 50, 74, 91`), so the FileHandler-
   only path is unchanged for them — no test edits required.
7. Run `just check` and confirm green. (Lint will flag `import sys`
   if it goes unused; the new `StreamHandler(stream=sys.stdout)` call
   keeps it referenced, so this is a sanity check, not a known fix.)

## File-level changes

| Path | Change |
| --- | --- |
| `packages/foreman/src/foreman/logging_setup.py` | Add `import sys`; replace the `RichHandler` block inside `if console:` with `StreamHandler(stream=sys.stdout)` + `_JsonLinesFormatter()`; remove the two `from rich...` lazy imports; update the function docstring to describe the new dual destination. |
| `packages/foreman/tests/test_logging_setup.py` | Add two new tests exercising the `console=True` path: one asserts stdout contains a parseable JSON line with the expected fields; one asserts the parsed stdout record equals the parsed file record. |

No other source files change. `Dockerfile`, `docker/entrypoint.sh`,
`packages/foreman/src/foreman/cli.py`, `packages/foreman/src/foreman/
reconciler/v3_host.py`, and `packages/foreman/src/foreman/daemon.py` are
all untouched — the call sites already invoke `configure_daemon_logging`
with default `console=True`, so the new behaviour activates without
re-wiring.

## Alternatives considered

- **Add a third `StreamHandler(stdout)` handler alongside the existing
  `FileHandler` + `RichHandler`.** Ruled out: the issue's AC says
  "BOTH a FileHandler ... AND a StreamHandler" (i.e., two handlers,
  not three). Keeping Rich would also double-log to two streams per
  call (stderr via Rich + stdout via StreamHandler), bloating
  `docker logs` with the Rich pretty form on stderr and the JSON form
  on stdout — confusing both for human readers and for log aggregators
  that merge streams.
- **Make the choice configurable via a new `console_format: Literal["rich", "json"]`
  kwarg, defaulting to `"json"` when stdout is not a TTY.** Ruled out:
  the issue scope is "make `docker logs` work"; TTY detection adds a
  branch and a code path with no current consumer. Operators who run
  the daemon directly on a host can still pipe through `jq` for human
  formatting (`foreman daemon v3-start | jq`). If a future ticket
  reintroduces the rich pretty-format path, the kwarg can be added then
  without breaking the JSON-on-stdout contract this spec establishes.
- **Point the existing `RichHandler` at `Console(stdout=True)` instead
  of `stderr=True`.** Ruled out: it would solve the visibility problem
  but breaks the "same JSON-lines format on both" AC — RichHandler
  emits pretty-formatted, ANSI-coloured text, not JSON. Log aggregators
  (Loki, CloudWatch, etc.) parsing the container's stdout would see
  unstructured pretty-printed output instead of structured records.
- **Reconfigure the existing `FileHandler` to also write to stdout via a
  `tee`-like custom handler.** Ruled out: stdlib already provides exactly
  the right primitive (`StreamHandler`), and Python's logging supports
  multiple handlers on one logger natively. A custom tee class would
  duplicate that capability and become its own maintenance burden.
- **Drop the `rich>=13,<15` dependency in
  `packages/foreman/pyproject.toml`.** Deferred (see `## Out of scope`)
  rather than ruled out: removing the dep is a clean follow-up once we
  confirm no other module in the package imports `rich`. The grep done
  during planning showed `rich` referenced only by
  `logging_setup.py:101-102`, but a separate ticket is cleaner than
  bundling a dependency removal into this spec.

## Open questions

- The issue body says "the v2 daemon already does dual-handler logging
  (see `daemon_runners.py` for reference pattern)." A grep of
  `packages/foreman/src/foreman/daemon_runners.py` shows no
  `StreamHandler` or `RichHandler` references — v2 uses the same shared
  `configure_daemon_logging` helper this spec is modifying. The
  reference appears to be inaccurate; this spec proceeds on the premise
  that the issue's stated AC (FileHandler + StreamHandler, same JSON
  format) is the actual contract. If the Reviewer reads the issue and
  expects a separate v2-specific pattern, escalate before the Worker
  ships — but the most likely outcome is that the issue text was
  approximate and the AC is what matters.

## Out of scope

- Removing `rich>=13,<15` from `packages/foreman/pyproject.toml`. The
  dep becomes unused after this change in the foreman package, but
  removing dependencies is a separate ticket so it can be paired with a
  fresh `uv.lock` regen and CI run.
- Modifying the v2 daemon's call site at
  `packages/foreman/src/foreman/daemon.py:66-69`. It inherits the new
  behaviour automatically; explicitly editing the v2 path would add
  noise for code marked deprecated.
- Adding any structured-log fields, changing the `_JsonLinesFormatter`
  output shape, or modifying how `extra={...}` propagates.
- Changing the FileHandler's target path or rotation behaviour. The
  spec only adds a mirror; the on-disk log keeps its current path
  (`/foreman/state/v3-daemon.log` via `resolve_state_dir()` in
  `cli.py:599-600`) and rotation policy (none today).
- TTY-detection or "smart" console-mode switching. Operators who want
  human-readable foreground output should pipe through `jq`.
- Touching `Dockerfile`, `docker/entrypoint.sh`, or `docker-compose.yml`.
  `PYTHONUNBUFFERED=1` already guarantees per-line flush; no Docker
  side change is required.
- Adding any log forwarding / aggregator wiring (Loki, Fluentd, etc.).
  Stdout-as-JSON is the prerequisite; downstream collection is a
  separate concern.
