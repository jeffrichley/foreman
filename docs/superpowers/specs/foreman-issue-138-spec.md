# Spec: mirror v3 daemon log to stdout so `docker logs` works (issue #138)

## Goal

Make `docker logs foreman-daemon` useful again. Today the v3 reconciler's
logger writes JSON-lines to `/foreman/state/v3-daemon.log` and a pretty
stderr stream via `RichHandler` — neither of which surfaces every
reconciler decision as JSON on the container's stdout. As a result
operators can only see the entrypoint banner via `docker logs` and must
`docker exec` + `cat` the file to diagnose anything. This spec teaches
the daemon's logger to mirror every record to **stdout** as JSON-lines
in addition to the existing file output, so `docker logs` becomes the
live operational tail.

Tracks issue [#138](https://github.com/jeffrichley/foreman/issues/138).

## Acceptance criteria

- `foreman.logging_setup.configure_daemon_logging`
  (`packages/foreman/src/foreman/logging_setup.py:69-115`) grows an
  opt-in `stdout_json: bool = False` keyword argument. When True the
  function attaches a `logging.StreamHandler(sys.stdout)` to the
  `foreman` logger, wearing the same `_JsonLinesFormatter` already used
  by the FileHandler — so every record published to the `foreman`
  logger (and any `foreman.<child>` logger via the propagation chain
  that bottoms out at this logger) appears on stdout as a JSON line
  identical in shape to what gets written to the log file.
- The existing FileHandler attachment behaviour at
  `logging_setup.py:94-96` is unchanged: it still writes JSON-lines to
  the same `log_path`, still creates `log_path.parent` with
  `parents=True, exist_ok=True`, still uses `encoding="utf-8"`.
- The existing `console: bool = True` stderr `RichHandler` branch at
  `logging_setup.py:98-112` is unchanged in default behaviour — v2
  callers and host operators who like the pretty stderr tail keep it.
  Callers may pass `console=False` to suppress the RichHandler
  independently of `stdout_json`.
- `foreman.cli.daemon_v3_start` (`cli.py:546-746`) — specifically the
  `configure_daemon_logging(...)` call at `cli.py:583-586` — passes
  `stdout_json=True, console=False`. The container's stdout becomes
  the single, uniform JSON-lines stream; the stderr-RichHandler is
  suppressed in the v3 path so `docker logs` does not interleave
  pretty text with the JSON. (`console=False` is the same flag the
  existing test suite passes — `tests/test_logging_setup.py:14,33,50,
  74,91` — so the contract is already exercised.)
- `foreman.daemon.Daemon.start`
  (`packages/foreman/src/foreman/daemon.py:66-69`) — the v2 daemon's
  `configure_daemon_logging` call — is **not** changed. v2 keeps its
  current file + stderr-Rich shape. v2 does not run in the container
  (`docker/entrypoint.sh:52` exec's `foreman daemon v3-start` only),
  so this issue does not require touching v2.
- `Dockerfile`: no change. `ENV PYTHONUNBUFFERED=1` is already set at
  `Dockerfile:24-27`; the new stdout `StreamHandler` honours that
  automatically because Python's `StreamHandler.emit` calls
  `stream.flush()` per record and unbuffered stdout means the bytes
  hit the docker log driver immediately.
- `docker-compose.yml` is not changed. The `json-file` log driver
  config at `docker-compose.yml:50-54` already captures stdout +
  stderr from PID 1; once the daemon emits JSON on stdout it is
  visible to `docker compose logs daemon` and to `docker logs
  foreman-daemon` without compose-side changes.
- New unit tests in `packages/foreman/tests/test_logging_setup.py`:
  - `test_configure_daemon_logging_mirrors_to_stdout_when_stdout_json_true`
    — call with `stdout_json=True, console=False`, capture stdout via
    pytest's `capsys`, emit one `logger.info(...)` with an `extra=`
    field, assert the captured stdout contains exactly one parseable
    JSON line whose `message`, `level`, and `extra`-derived keys match
    the file's last line.
  - `test_configure_daemon_logging_stdout_json_off_by_default` — call
    with the default kwargs (`stdout_json` omitted, `console=False` to
    suppress Rich), emit one record, assert nothing was printed to
    stdout (only the FileHandler fires). Use
    `capsys.readouterr().out == ""`.
  - `test_configure_daemon_logging_stdout_handler_uses_json_lines_formatter`
    — call with `stdout_json=True`, walk
    `logging.getLogger("foreman").handlers`, find the `StreamHandler`
    bound to `sys.stdout`, assert its `.formatter` is an instance of
    `_JsonLinesFormatter`. This is the "both handlers use the same
    format" contract from the acceptance criteria expressed
    structurally.
  - `test_configure_daemon_logging_is_idempotent_with_stdout_json` —
    call `configure_daemon_logging(stdout_json=True, console=False)`
    **twice** in a row, assert the `foreman` logger ends up with
    exactly **two** handlers (the FileHandler and the stdout
    `StreamHandler`), not four. The function already clears handlers
    at `logging_setup.py:92`; the new branch must not regress that.
- The existing tests in `test_logging_setup.py` pass without changes.
  They all pass `console=False` and assert on file content only;
  default `stdout_json=False` keeps stdout silent so they remain
  unaffected.
- `just check` exits zero.

## Approach

The issue isn't a missing channel — the daemon already publishes via
`logging.getLogger("foreman")` from many call sites, and
`configure_daemon_logging` is the one place the daemon configures the
handler set for that logger. The fix lives there.

Why a new parameter rather than always-on stdout mirroring? Because
the two callers of `configure_daemon_logging` have different
requirements:

- **v3 in-container** (`cli.py:583-586`): operator reads `docker logs`,
  wants every record as JSON, doesn't see the stderr stream as
  human-rendered text (the WSL2/docker layer mangles ANSI for the
  container's `docker logs` view, and even when it doesn't, mixing
  Rich text with JSON in the same stream defeats the
  machine-readability the JSON-lines format exists to provide).
- **v2 host-foreground** (`daemon.py:66-69`): operator runs `foreman
  daemon start` in a terminal and wants the pretty stderr tail. The
  on-disk JSON log is the audit record; stderr is for the human.

Making stdout mirroring opt-in lets the v3 path get clean JSON
stdout while v2 keeps its human-friendly stderr. It also leaves
legacy and test callers undisturbed — every existing test in
`test_logging_setup.py` continues to pass without modification.

Why ALSO pass `console=False` from the v3-start path? The current
stderr RichHandler still emits in the container. `docker logs` captures
stderr in addition to stdout, so leaving Rich on would interleave
pretty text with the new JSON-lines stream — the consuming operator
(or a log shipper grepping for `"level":"ERROR"`) would have to
filter out the noise. The v3-in-container reality is "nobody's
reading stderr pretty here"; suppressing it gives docker logs a single
clean JSON-lines stream, matching the entrypoint banner format
(`docker/entrypoint.sh:43-47`).

The implementation is intentionally narrow: one `StreamHandler` on
`sys.stdout`, the *same* `_JsonLinesFormatter` instance class the
FileHandler already uses, attached behind a default-False gate. No
structural refactor, no new module, no helper extraction — the file is
116 lines today and stays under 130 after the change.

The `_JsonLinesFormatter` (`logging_setup.py:45-66`) is already shared
between whatever handlers want to use it; it's a stateless formatter
that reads `record.__dict__` once per `format()` call. Constructing a
fresh instance per handler is fine (the existing FileHandler does
`file_handler.setFormatter(_JsonLinesFormatter())` on every
configure-call); the stdout handler does the same.

One subtlety worth being explicit about: `logging.StreamHandler`
defaults to `sys.stderr`. The new handler MUST be constructed as
`logging.StreamHandler(sys.stdout)` (explicit stdout reference) — not
`StreamHandler()` — or the change ships a no-op for the `docker logs`
use case the issue exists to fix. The unit test
`test_configure_daemon_logging_mirrors_to_stdout_when_stdout_json_true`
uses `capsys.readouterr().out` (not `.err`) precisely to pin this.

## Sub-requests (topologically sorted)

1. In `packages/foreman/src/foreman/logging_setup.py`:
   - Add `import sys` near the existing imports at the top of the
     module (currently imports are at `logging_setup.py:10-16`).
   - Add a new keyword parameter `stdout_json: bool = False` to
     `configure_daemon_logging`, placed AFTER `console: bool = True`
     in the signature so existing keyword callers are unaffected.
   - Update the docstring (`logging_setup.py:75-87`) to describe the
     new `stdout_json` flag: "When True, additionally attach a
     `StreamHandler(sys.stdout)` with the same `_JsonLinesFormatter`
     as the FileHandler. Used by the v3 reconciler so `docker logs`
     surfaces every record as JSON."
   - After the FileHandler attachment block (after
     `logging_setup.py:96`) and BEFORE the `if console:` branch at
     `logging_setup.py:98`, add a `if stdout_json:` branch that
     constructs `stdout_handler = logging.StreamHandler(sys.stdout)`,
     calls `stdout_handler.setFormatter(_JsonLinesFormatter())`, and
     calls `foreman_logger.addHandler(stdout_handler)`.

2. In `packages/foreman/src/foreman/cli.py`:
   - Update the `configure_daemon_logging(...)` call inside
     `daemon_v3_start` (currently at `cli.py:583-586`) to pass
     `stdout_json=True, console=False`. Keep the existing
     `log_path=v3_log_path, level=config.daemon.log_level` arguments.
   - Add a short comment above the call explaining that the v3 path
     opts into stdout JSON so `docker logs` works AND opts out of
     stderr Rich so docker logs aren't a mixed pretty/JSON stream.

3. In `packages/foreman/tests/test_logging_setup.py`:
   - Add the four new tests listed under Acceptance criteria:
     - `test_configure_daemon_logging_mirrors_to_stdout_when_stdout_json_true`
     - `test_configure_daemon_logging_stdout_json_off_by_default`
     - `test_configure_daemon_logging_stdout_handler_uses_json_lines_formatter`
     - `test_configure_daemon_logging_is_idempotent_with_stdout_json`
   - Each test must clear the `foreman` logger's handlers at teardown
     (or, simpler, let the next call to `configure_daemon_logging`
     handle it — the function clears at `logging_setup.py:92`). The
     existing tests in the file get away without explicit teardown
     because they all call `configure_daemon_logging`; new tests
     follow the same pattern.

4. `Dockerfile` — verify (do not edit). Open `Dockerfile`, confirm
   `PYTHONUNBUFFERED=1` is still set at `Dockerfile:24-27`. If it
   isn't, surface that as a follow-up (it currently IS — confirmed
   while writing this spec). No code change committed.

5. Run the targeted suite:
   `uv run pytest packages/foreman/tests/test_logging_setup.py -v`.
   Expected: 5 pre-existing tests + 4 new tests pass.

6. Run the full quality gate: `just check`. Expected: exits zero. Pay
   attention to mypy on the new `sys.stdout` reference in
   `logging_setup.py` (the `StreamHandler[TextIO]` generic should be
   inferred fine, but if mypy complains, annotate explicitly).

7. Manual smoke test (developer-machine, optional but recommended):
   - `docker compose up -d daemon`
   - `docker logs -f foreman-daemon` — confirm JSON lines appear
     beyond the entrypoint banner as the reconciler ticks.
   - `docker exec foreman-daemon cat /foreman/state/v3-daemon.log` —
     confirm the same lines are also in the file.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/logging_setup.py` | Add `import sys`. Add `stdout_json: bool = False` kwarg to `configure_daemon_logging`. After the FileHandler attachment, branch on `stdout_json` to attach a `StreamHandler(sys.stdout)` using the same `_JsonLinesFormatter`. Update the docstring to describe the new flag. |
| `packages/foreman/src/foreman/cli.py` | At the `configure_daemon_logging(...)` call site inside `daemon_v3_start` (currently `cli.py:583-586`), pass `stdout_json=True, console=False`. Add an inline comment explaining the v3-in-container rationale. |
| `packages/foreman/tests/test_logging_setup.py` | Add 4 new tests: stdout mirror happy path (capsys), stdout silent by default, stdout handler uses `_JsonLinesFormatter`, and idempotence when called twice with `stdout_json=True`. |
| `Dockerfile` | No change. `ENV PYTHONUNBUFFERED=1` at `Dockerfile:24-27` already does what's needed. Verify only. |
| `packages/foreman/src/foreman/daemon.py` | No change. v2 keeps its current shape (file + stderr Rich) because v2 doesn't run in the container. |
| `docker-compose.yml` | No change. The `json-file` driver already captures stdout. |

## Alternatives considered

- **Always attach the stdout JSON handler unconditionally** (no
  `stdout_json` parameter). Rejected: the v2 host-foreground use case
  loses nothing functionally but gains noise — the operator's terminal
  would see both Rich pretty output (stderr) AND JSON lines (stdout)
  in the same shell, doubling the on-screen volume per record. Behind
  a default-False gate the change is invisible to v2 and to every
  existing test, satisfying the principle of least surprise.

- **Replace the FileHandler with the stdout handler entirely**,
  treating `/foreman/state/v3-daemon.log` as obsolete in favour of
  `docker logs` + the docker `json-file` log driver. Rejected: the
  acceptance criteria in the issue body explicitly preserve the file
  ("current behavior, write to /foreman/state/v3-daemon.log"), and
  the named volume at `docker-compose.yml:45` is intended to survive
  `docker compose down`, which the docker `json-file` driver does
  not (logs are cleared with the container). Two channels, different
  retention semantics — both valuable.

- **Switch `RichHandler` from stderr to stdout instead of adding a
  separate JSON handler.** Rejected: Rich's pretty output is not
  JSON, defeats line-oriented `grep`/`jq` workflows, and bakes ANSI
  escape sequences into the docker log volume forever. The issue
  body specifically says "Same JSON-lines format on both" — Rich on
  stdout breaks that contract.

- **Have the entrypoint shell redirect the file to stdout** with `tail
  -F /foreman/state/v3-daemon.log &` before `exec foreman daemon
  v3-start`. Rejected: a `tail` subprocess is brittle (signal
  handling, log rotation, race on startup before the file exists),
  introduces a second process under tini that the daemon doesn't
  manage, and obscures the audit trail because the docker log driver
  would see lines that aren't ordered with respect to the daemon's
  own structured output. The Python-side handler is the cleaner cut.

- **Wire the stdout handler in v2 too** (i.e. update
  `daemon.py:66-69`). Out of scope. v2 doesn't run in the container
  (the entrypoint exec's `daemon v3-start`), v2 is deprecated per
  the `daemon.py:1-12` docstring, and changing v2's logging shape
  for no operational benefit invites accidental test breakage. If
  the team ever wants to run v2 in-container too, the same
  `stdout_json=True, console=False` parameter pair is ready to use.

## Open questions

(none — the change is narrowly scoped to one function, two call
sites, and a small test addition; the contracts at every layer are
clear and the manual smoke test is straightforward.)

## Out of scope

- **Log rotation for `/foreman/state/v3-daemon.log`.** The file grows
  unbounded under the FileHandler today and continues to do so after
  this change. Docker's `json-file` driver rotates the stdout copy
  (`docker-compose.yml:50-54` — 10MB × 5), but the on-disk file does
  not. A `RotatingFileHandler` swap is a separate concern.

- **Structured log levels per submodule.** The whole `foreman` logger
  tree runs at `config.daemon.log_level`. Per-module overrides
  (e.g. quiet `foreman.reconciler.gh_graphql`, verbose
  `foreman.reconciler.actions`) are a separate UX feature.

- **Switching the v3 file path from `/foreman/state/v3-daemon.log` to
  somewhere else.** The path is set by
  `resolve_state_dir() / "v3-daemon.log"` in `cli.py:582` and the
  named volume mount in `docker-compose.yml:45` is keyed to
  `/foreman/state`. Changing it is a larger surgery and not
  required to make `docker logs` useful.

- **Adding a `--log-format` CLI flag** to `foreman daemon v3-start`.
  The v3 path is the only in-container caller and always wants JSON
  stdout; a flag would be over-design for one caller. Revisit if
  another runtime mode emerges.

- **v2 daemon logging changes.** v2 is deprecated
  (`daemon.py:1-12`) and not run in-container; touching its logging
  configuration here would expand the blast radius without benefit.
