# Spec: add `foreman v3-ps` operator diagnostic command (issue #208)

## Goal

Add a new top-level CLI command `foreman v3-ps` that prints, in one
view, every foreman-labeled GitHub issue across all configured
projects along with the most recent `execution_log` action and outcome
for each ticket. This collapses the three-tool investigation flow
operators use today (`gh issue list ...` + `sqlite3 reconciler.db ...`
+ `tail v3-daemon.log`) into a single read-only diagnostic command.
Closes the §7 item-5 drift entry in `docs/architecture/v3-reconciler.md`
("No `foreman daemon ps` equivalent"). See issue
[#208](https://github.com/jeffrichley/foreman/issues/208).

## Acceptance criteria

- A new top-level Click command is registered: `foreman v3-ps`
  (not nested under `foreman daemon ...`). `foreman --help` lists it
  next to `ps` and `pipeline-detail`.
- New module `packages/foreman/src/foreman/cli_v3_ps.py` contains the
  command's implementation logic (data fetch + formatting helpers).
  The `@cli.command("v3-ps")` decorator + click options live in
  `packages/foreman/src/foreman/cli.py`, with the body delegating to
  `cli_v3_ps.run_v3_ps(config=..., project=..., stuck=..., json_=...)`.
  This mirrors the existing `foreman ps` pattern
  (`cli.py:1190-1198` delegates into `foreman.ps.format_active_pipelines`).
- The command accepts these flags:
  - `--project NAME` (optional): filter to one project. Unknown name
    exits non-zero with a `click.ClickException`
    (`"unknown project: <NAME>"`).
  - `--stuck` (boolean flag, default False): filter applied AFTER
    fetch — keeps issues whose labels contain `foreman:needs-help`
    OR `foreman:failed`, OR whose `time_since_seconds` for the last
    `execution_log` row is `> 86_400` (24h), OR which have no
    `execution_log` history at all.
  - `--json` (boolean flag, default False): emit NDJSON to stdout —
    one JSON object per issue per line, NOT a JSON array, NOT a
    JSON document.
- Default output (no `--json`) is a `rich.table.Table` printed via
  `rich.console.Console().print(table)`. Columns, in order:
  `project`, `issue#`, `title`, `labels`, `last_action`, `outcome`,
  `time_since`. `title` is truncated to 40 chars (suffix `...` when
  truncated). `labels` is a comma-joined list of `foreman:*` labels
  with the `"foreman:"` prefix stripped (so `foreman:needs-help`
  renders as `needs-help`). Non-foreman labels (e.g., `bug`,
  `enhancement`) are NOT included in this column.
- `last_action` / `outcome` / `time_since` are `-` (literal dash)
  when the ticket has no `execution_log` row whose
  `parent_log_id IS NOT NULL` (i.e., no terminated action yet).
- `time_since` in table output is a short humanized string from a
  helper `_humanize_seconds(seconds: int) -> str` defined in
  `cli_v3_ps.py`: `<60s → "{n}s"`, `<3600s → "{n}m"`,
  `<86400s → "{n}h"`, `≥86400s → "{n}d"`. The helper accepts
  `None` and returns `"-"`.
- `--json` output is one JSON object per line. Each line's schema:
  ```json
  {"project": "foreman", "issue": 208, "title": "...", "labels": ["needs-help", "plan"], "last_action": "dispatch_role", "outcome": "success", "time_since_seconds": 3540}
  ```
  When the ticket has no exec_log row, `last_action`, `outcome`,
  and `time_since_seconds` are `null` (JSON null), not `"-"`.
  `labels` in NDJSON output is a list of stripped-prefix label
  names (matches table behavior).
- Across all projects in `config.projects`, the command fetches each
  project's snapshot via the existing
  `foreman.reconciler.observer.fetch_project_state(...)` — NO new
  GraphQL query is added.
- For each fetched `IssueState`, the command queries the same
  `execution_log` SQLite file the v3 reconciler writes
  (`config.reconciler.db_path`, expanduser'd, opened read-only) with:
  ```sql
  SELECT action, outcome, ts
  FROM execution_log
  WHERE ticket_id = ?
    AND parent_log_id IS NOT NULL
  ORDER BY ts DESC
  LIMIT 1
  ```
  The `ticket_id` value matches `ProjectSnapshot.ticket_id_for(issue.number)`
  (i.e., `f"{owner}/{repo}#{number}"`).
- If `foreman.reconciler.observer.fetch_project_state` raises
  `ObserverUnreachable` (or any `ObserverError` subclass) for a
  project, the command exits non-zero with a `click.ClickException`
  whose message includes the project name and the underlying error
  message. It does NOT silently continue to the next project — the
  diagnostic command must surface fetch failures clearly.
- Six new tests live in
  `packages/foreman/tests/test_cli_v3_ps.py`:
  1. `test_v3_ps_lists_all_issues_across_projects`
  2. `test_v3_ps_project_flag_filters`
  3. `test_v3_ps_project_flag_unknown_name_exits_nonzero`
  4. `test_v3_ps_stuck_flag_filters`
  5. `test_v3_ps_json_emits_ndjson`
  6. `test_v3_ps_handles_no_exec_log_history`
  7. `test_v3_ps_handles_observer_failure_gracefully`
  (Seven total — the issue listed six but the unknown-`--project`-name
  case is an explicit acceptance criterion above and deserves its own
  test. The Worker may merge tests 2 and 3 if they get awkward; the
  count is not load-bearing.)
- All seven tests pass and `just check` exits zero.
- The legacy `foreman ps` Click command (`cli.py:1190-1198`) and the
  `foreman.ps` module are UNTOUCHED — no edits, no deletions. They
  continue to query the v2 `pipelines` table via `Storage`.
- The new file imports the existing module-internal observer entry
  point: `from foreman.reconciler.observer import fetch_project_state`.
  No copy/paste of the GraphQL query string into the new module.

## Approach

`foreman v3-ps` is a read-only diagnostic over two existing data
sources joined in Python. It does not need a new bus message, a new
observer query, or new GitHub permissions. The work is mostly
plumbing: assemble a list of `(ProjectSnapshot, IssueState)` tuples,
join each issue against its most recent terminated `execution_log`
row, and render either a `rich.table.Table` or NDJSON.

**1. Where the data comes from.** Two sources, joined on
`ticket_id = f"{owner}/{repo}#{issue.number}"`:

- GitHub side: reuse the v3 observer's
  `fetch_project_state(project=..., owner=..., repo=..., gh=...)`
  in `packages/foreman/src/foreman/reconciler/observer.py:121`.
  That function already returns a `ProjectSnapshot` (defined in
  `reconciler/state.py:57-83`) with `issues: tuple[IssueState, ...]`,
  each `IssueState` carrying `number`, `title`, and `labels: tuple[str, ...]`.
  The observer's `_QUERY` already filters to the full set of foreman
  state labels, so every result row is by definition foreman-labeled.
- SQLite side: open the same `execution_log` database that the v3
  reconciler writes to (`config.reconciler.db_path` from
  `foreman.config.ReconcilerConfig`, default
  `~/.foreman/reconciler.sqlite`, container override
  `/foreman/state/reconciler.sqlite`). Run the query in the
  acceptance-criteria block above. The new module opens its own
  read-only `sqlite3.connect(...)` rather than going through
  `ExecutionLog` because the existing class has no
  "latest-terminated row per ticket" helper and the criterion 2
  (don't extend the exec_log API) is explicit.

**2. How the new module is structured.** `cli_v3_ps.py` exports one
public function `run_v3_ps(*, config, project, stuck, json_)` plus
two helper-private functions, modeled on the
`foreman.ps` / `cli.py:ps_cmd` pattern:

```python
# cli_v3_ps.py — sketch (not the final code)

def run_v3_ps(
    *,
    config: Config,
    project: str | None,
    stuck: bool,
    json_: bool,
) -> None:
    rows = _build_rows(config=config, project_filter=project)
    if stuck:
        rows = [r for r in rows if _is_stuck(r)]
    if json_:
        _emit_ndjson(rows)
    else:
        _emit_table(rows)

def _build_rows(*, config, project_filter): ...
def _humanize_seconds(seconds: int | None) -> str: ...
def _is_stuck(row: _PsRow) -> bool: ...
def _emit_table(rows: list[_PsRow]) -> None: ...
def _emit_ndjson(rows: list[_PsRow]) -> None: ...
```

`_PsRow` is a small frozen `dataclass` carrying the joined fields
(`project: str`, `issue: int`, `title: str`,
`labels: tuple[str, ...]` (the stripped-prefix display set),
`last_action: str | None`, `outcome: str | None`,
`time_since_seconds: int | None`). The dataclass is the boundary
between fetching and rendering; tests can construct `_PsRow`
instances directly without needing the live observer or SQLite.

**3. Wiring into Click.** The `@cli.command("v3-ps")` decorator in
`cli.py` constructs the `Config` via `_load_config_from_env()`
(already in `cli.py:1056`) and the real GraphQL client via the
existing `_build_v3_gh_and_host(config, log)` helper
(`cli.py:771-833`). For this command we DO need the gh client but
NOT the host — we throw the host away. We also need a log argument
for `_build_v3_gh_and_host` (the `log_dir` it sets up); the simplest
honest path is to construct an `ExecutionLog` against
`config.reconciler.db_path` (same way `daemon v3-start` does at
`cli.py:654-656`) and pass it in. That lets us call
`_build_v3_gh_and_host(config, log)` unchanged. We then call
`run_v3_ps(config=config, project=project, stuck=stuck, json_=json_)`,
passing the gh client through via a sentinel parameter or by letting
`run_v3_ps` build it itself — see Approach §5.

Actually, the simpler design is for `run_v3_ps` to accept the
GraphQL client as a parameter (just like the reconciler does). The
Click body owns the wiring; `run_v3_ps` owns the logic. This makes
the test harness trivial — tests pass a `_FakeGHClient` and a
temp-SQLite database.

```python
# cli.py — sketch

@cli.command("v3-ps")
@click.option("--project", default=None, help="...")
@click.option("--stuck", is_flag=True, help="...")
@click.option("--json", "json_", is_flag=True, help="...")
def v3_ps_cmd(project: str | None, stuck: bool, json_: bool) -> None:
    """Operator diagnostic showing foreman-labeled issues across projects."""
    from foreman.cli_v3_ps import run_v3_ps
    from foreman.reconciler import ExecutionLog

    config = _load_config_from_env()
    db_path = Path(os.path.expanduser(config.reconciler.db_path))
    log = ExecutionLog(db_path)
    log.init()  # idempotent — also creates the file if missing.
    gh, _host = _build_v3_gh_and_host(config, log)
    run_v3_ps(
        config=config,
        gh=gh,
        db_path=db_path,
        project=project,
        stuck=stuck,
        json_=json_,
    )
```

The CLI body unconditionally calls `log.init()` so a fresh install
without the v3 reconciler ever having run still produces a sane
(empty) result rather than a `sqlite3.OperationalError: no such table`.

**4. Label-prefix stripping.** The issue body suggests
`Labels.PREFIX`. Investigation: there is no `Labels` module on
`main` today (the centralization work in foreman#194's spec PR
#196 merged the spec but its impl PR is still open on the
`foreman/impl-194` branch). `cli_v3_ps.py` defines a private
module-level constant:

```python
_FOREMAN_LABEL_PREFIX = "foreman:"
_LABEL_NEEDS_HELP = f"{_FOREMAN_LABEL_PREFIX}needs-help"
_LABEL_FAILED = f"{_FOREMAN_LABEL_PREFIX}failed"
```

This matches the pattern already in `roles/worker.py:115` and
`roles/fixer.py:102` (each module's own `_LABEL_NEEDS_HELP =
"foreman:needs-help"`). When the labels-centralization impl PR
lands, a follow-up issue can sweep these new constants into
`Labels` along with the existing per-role constants. The Worker
should NOT block on labels.py landing — see Open Questions.

For the label display, the helper:
`tuple(lbl.removeprefix(_FOREMAN_LABEL_PREFIX) for lbl in issue.labels if lbl.startswith(_FOREMAN_LABEL_PREFIX))`
filters out non-foreman labels and strips the prefix from foreman
ones. `str.removeprefix` is Python 3.9+; the package's
`pyproject.toml` requires `>=3.11`, so it's safe.

**5. Stuck detection.** Defined as: any of
- `_LABEL_NEEDS_HELP in row.foreman_labels_raw`, OR
- `_LABEL_FAILED in row.foreman_labels_raw`, OR
- `row.time_since_seconds is None`
  (never had a terminated exec_log row), OR
- `row.time_since_seconds > 86_400` (24h)

The raw `foreman_labels_raw` is stored on `_PsRow` alongside the
display-stripped `labels` so the predicate doesn't have to
re-prefix.

**6. Time computation.** The `execution_log.ts` column is written
by SQLite's `CURRENT_TIMESTAMP` (see `exec_log.py:28`) in the format
`YYYY-MM-DD HH:MM:SS` UTC (no tz suffix). The new module parses it
back via `datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)`
and subtracts `datetime.now(UTC)` to get the delta in seconds (as
an int via `int(delta.total_seconds())`). The new module imports
`UTC` from `datetime` (already a Python 3.11+ symbol).

**7. Why `--json` is NDJSON, not a JSON array.** The issue
explicitly demands it (`"NOT a single JSON array"`). NDJSON streams
cleanly through `jq .` and tools like `grep`. The implementation
calls `print(json.dumps(record, sort_keys=True), flush=False)` once
per row — a single trailing newline at end of stdout follows
naturally from `print`. The test asserts the output ends in a
newline AND contains exactly N `\n`-delimited JSON-parseable lines
where N is the input row count.

## Sub-requests (topologically sorted)

1. Create `packages/foreman/src/foreman/cli_v3_ps.py`. Implement,
   in order:
   - The `_FOREMAN_LABEL_PREFIX`, `_LABEL_NEEDS_HELP`, `_LABEL_FAILED`
     module-level constants.
   - The frozen `dataclass _PsRow` with fields
     `project: str`, `issue: int`, `title: str`,
     `labels: tuple[str, ...]`,
     `foreman_labels_raw: tuple[str, ...]`,
     `last_action: str | None`, `outcome: str | None`,
     `time_since_seconds: int | None`.
   - `_humanize_seconds(seconds: int | None) -> str` per Approach §6.
   - `_query_latest_terminated(db_path: Path, ticket_id: str)
     -> tuple[str, str, int] | None` returning
     `(action, outcome, time_since_seconds)` or None. Internally
     opens its own short-lived `sqlite3.connect(db_path)` and runs
     the acceptance-criteria SQL. Parses `ts` per Approach §6.
   - `_build_rows(*, config: Config, gh: GHGraphQLClient, db_path: Path,
     project_filter: str | None) -> list[_PsRow]`. Iterates
     `config.projects.items()` (skipping projects whose name does
     not match `project_filter` when set); per project, calls
     `fetch_project_state(...)`; per issue, calls
     `_query_latest_terminated(...)`; returns the joined list.
     If `project_filter` is set and matches NO project name,
     raises `click.ClickException("unknown project: <NAME>")`
     BEFORE any GitHub call.
   - `_is_stuck(row: _PsRow) -> bool` per Approach §5.
   - `_emit_table(rows: list[_PsRow]) -> None` using
     `rich.table.Table` + `rich.console.Console`.
   - `_emit_ndjson(rows: list[_PsRow]) -> None` using
     `json.dumps(..., sort_keys=True)` + `print(...)`.
   - `run_v3_ps(*, config: Config, gh: GHGraphQLClient,
     db_path: Path, project: str | None, stuck: bool, json_: bool)
     -> None` — the orchestrating entry point. Wraps the call to
     `_build_rows` in a try/except for `ObserverError` (catching
     the base class catches `ObserverUnreachable` +
     `ObserverRateLimited` + plain `ObserverError`) and re-raises
     as `click.ClickException` with a clear message including the
     project name.

2. In `packages/foreman/src/foreman/cli.py`:
   - Add the new top-level command. Place the `@cli.command("v3-ps")`
     block immediately after the existing `@cli.command("ps")` /
     `@cli.command("pipeline-detail")` block at
     `cli.py:1190-1211` so the three diagnostic commands live
     together. Do NOT touch `ps_cmd` or `pipeline_detail_cmd`.
   - The new command body matches the Approach §3 sketch:
     load config, construct `ExecutionLog` + `init()`, call
     `_build_v3_gh_and_host(config, log)` to obtain `gh` (discard
     the returned host), then delegate to
     `cli_v3_ps.run_v3_ps(...)`.

3. Create `packages/foreman/tests/test_cli_v3_ps.py`. Tests use
   `click.testing.CliRunner` plus monkeypatching where needed.
   Shared test fixtures and helpers (all in the new file):
   - A `_FakeGHClient` class mirroring `tests/reconciler/test_observer.py`'s
     fake: implements `graphql(self, query, variables)` returning a
     dict shaped per the real GraphQL response, parameterized by
     a `{(owner, repo) -> response}` map so a single fake serves
     multiple projects.
   - A helper `_write_v3_ps_config(tmp_path, projects: list[tuple[str, str]],
     db_path: Path) -> Path` that writes a minimal TOML config to
     `tmp_path / "config.toml"` with `[reconciler]` (`db_path =
     "<tmp>"`) and one `[projects.<name>]` block per project tuple
     (`repo = "owner/name"`, `local_clone_path = "/tmp/<name>"`,
     and `apps.planner_app_id = 1`, `apps.planner_private_key_path =
     "/tmp/planner.pem"` so config-validation passes).
   - A helper `_seed_exec_log(db_path: Path, rows: list[dict]) -> None`
     that opens a fresh `sqlite3.connect(db_path)`, runs
     `ExecutionLog(db_path).init()` for schema creation, then inserts
     paired start + termination rows. Each `rows` entry shape:
     `{"ticket_id": "owner/repo#42", "project": "foreman",
       "action": "dispatch_role", "outcome": "success",
       "ts_offset_seconds": -300}` where `ts_offset_seconds` is
     negative-seconds-from-now used to derive the start row's `ts`
     via direct INSERT (overriding `CURRENT_TIMESTAMP`).
   - Tests patch `foreman.cli._build_v3_gh_and_host` to return
     `(fake_gh, None)`, then invoke `cli` via `CliRunner` with
     `FOREMAN_CONFIG_PATH=str(config_path)` in `env=` or via
     `monkeypatch.setenv`.

4. Implement the seven test cases listed in Acceptance criteria.
   Each test follows this pattern:
   1. Write a v3-ps config TOML to `tmp_path`.
   2. Seed the SQLite db at `db_path` with `_seed_exec_log`.
   3. Build the `_FakeGHClient` response map.
   4. Patch `foreman.cli._build_v3_gh_and_host` with a `MagicMock`
      that returns `(fake_gh, None)` regardless of args.
   5. `runner = CliRunner(); result = runner.invoke(cli,
      ["v3-ps", ...], env={"FOREMAN_CONFIG_PATH": str(config_path)})`.
   6. Assert `result.exit_code == 0` (or `!= 0` for the negative
      cases). Assert on `result.output` content (or use
      `result.stdout` for json mode).

   Specific assertions per test:
   - **test_v3_ps_lists_all_issues_across_projects**: two projects,
     A with issues #1+#2, B with issue #3. Each has one terminated
     exec_log row. Output contains all three issue numbers in
     order, contains both project names, and the
     `last_action`/`outcome` columns render the seeded values.
   - **test_v3_ps_project_flag_filters**: same setup. Invocation
     with `--project A` shows only #1+#2; #3 is absent from output.
   - **test_v3_ps_project_flag_unknown_name_exits_nonzero**:
     `--project NONEXISTENT`; assert `result.exit_code != 0` and
     `"unknown project" in result.output.lower()`.
   - **test_v3_ps_stuck_flag_filters**: three issues — one with
     `foreman:needs-help` label and a recent exec_log row, one
     with no label but a 48h-old exec_log row, one with no
     special labels and a fresh exec_log row. `--stuck` returns
     the first two. The healthy one is absent.
   - **test_v3_ps_json_emits_ndjson**: two issues, exec_log seeded.
     Invocation with `--json`. Assert `result.output` splits on
     `\n` (after rstrip) into exactly 2 non-empty lines, each
     of which is valid JSON via `json.loads`. Assert NDJSON
     schema: `{"project", "issue", "title", "labels",
     "last_action", "outcome", "time_since_seconds"}`.
   - **test_v3_ps_handles_no_exec_log_history**: one issue, db is
     empty (or contains rows for a different ticket_id). Table
     output shows `-` in `last_action`/`outcome`/`time_since`.
     NDJSON output (when also tested with `--json`) shows `null`
     for `last_action`/`outcome`/`time_since_seconds`.
   - **test_v3_ps_handles_observer_failure_gracefully**: fake
     GraphQL client whose `graphql(...)` raises
     `ObserverUnreachable("simulated outage")`. Assert
     `result.exit_code != 0`, `"simulated outage" in result.output`,
     and the project name appears in the error message.

5. Run `just check`. Confirm exit 0. If a previously-passing test
   in `test_cli.py` started failing because the new `v3-ps`
   command surfaces in `--help` output that an existing test
   string-matches against, update only the test's expected text
   (no behavior change). Investigation found no such test
   today (`test_cli.py` does not assert against the top-level
   `--help`).

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/src/foreman/cli_v3_ps.py` | **New file.** Contains `_PsRow` dataclass, `_humanize_seconds`, `_query_latest_terminated`, `_build_rows`, `_is_stuck`, `_emit_table`, `_emit_ndjson`, `run_v3_ps`. ~150 lines. Imports: `json`, `sqlite3`, `dataclasses`, `datetime` (UTC + datetime + strptime), `pathlib.Path`, `click`, `rich.console`, `rich.table`, `foreman.config.Config`, `foreman.reconciler.observer` (`GHGraphQLClient`, `ObserverError`, `fetch_project_state`). |
| `packages/foreman/src/foreman/cli.py` | Add `@cli.command("v3-ps")` block with three options (`--project`, `--stuck`, `--json`) immediately after `pipeline_detail_cmd`. The body loads config, builds `ExecutionLog` + `init`, calls `_build_v3_gh_and_host(config, log)`, then delegates to `cli_v3_ps.run_v3_ps(...)`. No other edits. |
| `packages/foreman/tests/test_cli_v3_ps.py` | **New file.** Contains the `_FakeGHClient` class, `_write_v3_ps_config` helper, `_seed_exec_log` helper, and the seven test functions listed under Sub-request 4. ~250-350 lines depending on shared-helper extraction. |

No expected changes to (sanity-checked via grep):
- `packages/foreman/src/foreman/ps.py` (v2 `foreman ps` source — explicitly out of scope).
- `packages/foreman/src/foreman/reconciler/observer.py` — reused as-is.
- `packages/foreman/src/foreman/reconciler/exec_log.py` — NOT extended; the new module owns its own short-lived SQLite connection. The issue's out-of-scope rule explicitly forbids extending the API.
- `packages/foreman/src/foreman/reconciler/__init__.py` — no new exports.
- `packages/foreman/src/foreman/config.py` — no new config knobs.
- `docs/architecture/v3-reconciler.md` §7 item 5 — the drift entry it resolves will be removed by a follow-up PR after this lands; not removed in this PR because the entry's removal is semantically gated on the impl PR's merge. Worker should NOT touch this doc.

## Verification

Before opening the impl PR, the Worker MUST run AND record the
output of these commands in the PR body so the Reviewer can
cross-check:

1. `just check` — exit code 0. Capture tail of pytest output
   showing test count + pass/fail summary.
2. `python -c "from foreman.cli_v3_ps import run_v3_ps; print('ok')"` —
   exit 0, prints `ok`. Sanity check that the new module imports
   cleanly without circular-import issues.
3. `foreman v3-ps --help` — exit 0. Output mentions all three
   flags (`--project`, `--stuck`, `--json`) plus a one-line
   command description.
4. (Optional, in container; skip if not in container) Against the
   live container: `foreman v3-ps` prints a table containing
   foreman-labeled issues across all configured projects.
   `foreman v3-ps --project foreman` filters to one project.
   `foreman v3-ps --stuck` returns at least the currently-stuck
   tickets (#138, #139, #170, #200 per the issue body — exact
   set will depend on container state at verification time).
   `foreman v3-ps --json | jq .` produces valid JSON Lines.
5. `grep -rE '"foreman ps"|format_active_pipelines' packages/foreman/src/foreman/` —
   should still contain the legacy `foreman ps` references in
   `cli.py:1190` and `ps.py` (untouched).

## Alternatives considered

- **Put all logic inline in `cli.py`.** Rejected — the issue body
  allows "inline in `cli.py` if simpler", but the existing
  `foreman ps` / `foreman pipeline-detail` pattern already splits
  Click decoration from logic via a separate module (`ps.py`).
  Following that pattern keeps `cli.py` from growing further (it's
  already 1242 lines) and makes the new logic unit-testable
  without going through `CliRunner`. The Worker can collapse to
  inline if it ends up smaller, but the split is the recommended
  default.

- **Use `ExecutionLog` API methods instead of raw SQL.** Rejected —
  `ExecutionLog` exposes `has_unterminated`, `has_recent`,
  `count_completed`, and `recover_orphaned`, none of which return
  the latest-terminated `(action, outcome, ts)` triple per ticket.
  Adding a method to `ExecutionLog` is explicitly forbidden by the
  issue's out-of-scope rule. The new module opens its own
  short-lived `sqlite3.connect(...)` — the file format is stable
  and the SQL is straightforward.

- **Make `v3-ps` a subcommand of `daemon` (`foreman daemon v3-ps`).**
  Rejected — the issue body explicitly says `foreman v3-ps` (top-
  level), matching the operator-facing `foreman ps` it deprecates.
  Nesting it under `daemon` would imply lifecycle parity with
  `daemon start`/`daemon stop`, which it does not have (it's a
  read-only diagnostic).

- **Cache the GraphQL fetch across multiple invocations.** Rejected —
  YAGNI for a diagnostic command. Operators run it manually, not
  on a loop. If `--watch` is ever added (out of scope per the
  issue), that's the right time to think about caching.

- **Render the table using plain `print` instead of `rich`.**
  Rejected — `rich` is already a project dependency
  (`pyproject.toml` declares `rich>=13,<15`) and the project
  already imports from `rich.console` + `rich.logging` in
  `logging_setup.py:101-102`. Using `rich.table` matches the
  established pattern.

- **Use `pretty_table` or `tabulate` instead of `rich`.** Rejected —
  introducing a new dependency for a one-off diagnostic is
  gratuitous. `rich.table.Table` covers every formatting feature
  the issue's acceptance criteria require.

- **Build a `--watch` mode that re-runs every N seconds.**
  Rejected per the issue's out-of-scope rule
  (`"DO NOT add interactive features (no watch, no live update,
  no curses)"`). Operators who want this can wrap the command in
  `watch -n 30 foreman v3-ps` from their shell.

## Open questions

- **`foreman.labels.Labels` is not on `main` yet.** The issue body
  references `Labels.PREFIX`, `Labels.NEEDS_HELP`, and `Labels.FAILED`
  and frames the labels-centralization work (foreman#194) as "just
  merged". Investigation: the SPEC PR (#196) merged on 2026-06-07
  but only landed
  `docs/superpowers/specs/foreman-issue-194-spec.md`. The
  implementation PR is still open on the `foreman/impl-194` branch
  and adds `packages/foreman/src/foreman/labels.py`. The
  labels.py file the impl-194 branch defines does NOT include
  a `Labels.PREFIX` constant.

  Spec resolution: use local module-level constants
  (`_FOREMAN_LABEL_PREFIX`, `_LABEL_NEEDS_HELP`, `_LABEL_FAILED`)
  matching the pattern in `roles/worker.py:115` and
  `roles/fixer.py:102`. When the impl PR for foreman#194 merges,
  a follow-up issue can sweep these constants into `Labels` along
  with the existing per-role copies. The Worker should NOT add
  `Labels.PREFIX` to `labels.py` as part of THIS PR — that would
  couple the merge order to foreman#194 and risks breaking the
  keystone test in `tests/test_labels_keystone.py` that the impl
  PR adds. Confidence: medium on this point; if the Reviewer
  prefers waiting for foreman#194's impl to merge first, the cost
  is one round of label-import edits at impl time.

- **No GraphQL identity for the `foreman v3-ps` command on a
  fresh install without `[apps.planner_*]` configured.**
  `_build_v3_gh_and_host` requires the planner App's app_id +
  private key to construct the GraphQL token supplier. On a
  fresh `foreman init` host that hasn't yet configured the apps,
  `v3-ps` will raise `RuntimeError("No planner app_id: ...")`.
  This matches `foreman daemon v3-start`'s behavior — the daemon
  also can't run without identity. The spec does NOT add a
  "lightweight no-auth mode" because the GraphQL query the
  observer issues requires a token (anonymous GitHub GraphQL is
  unauthenticated and gets rate-limited fast). If the Reviewer
  wants the command to degrade gracefully (e.g., "no projects
  configured for GraphQL; showing exec_log data only"), that's a
  follow-up issue — out of scope here.

- **Verification step 4 ("against the live container") cannot run
  in the worker's pre-push gate.** The Worker's local environment
  is not the production container; it has no `[orchestrator]`
  credentials and no foreman-labeled issues. The Worker should
  treat verification step 4 as optional, run steps 1-3 + 5
  unconditionally, and note step 4 as "verified after merge" in
  the impl PR body.

## Out of scope

- **Removing or modifying the legacy `foreman ps` command.** Stays
  exactly as it is at `cli.py:1190-1198`. Its deprecation is a
  separate ticket per the issue body.
- **Removing the v3-reconciler.md §7 item-5 drift entry.** The
  entry's removal is semantically gated on this impl PR's merge —
  a follow-up doc-only PR removes it after this lands.
- **Adding a `--watch` mode, curses UI, or any interactive view.**
  Explicitly forbidden by the issue's out-of-scope rule.
- **Adding a `--show-log` flag that tails the daemon log.**
  Explicitly forbidden by the issue's out-of-scope rule.
- **Adding a `--pr` lookup mode.** Explicitly forbidden by the
  issue's out-of-scope rule.
- **Extending the `ExecutionLog` API with a "latest terminated"
  helper.** Explicitly forbidden by the issue's out-of-scope rule;
  the new module opens its own SQLite connection instead.
- **Server-side `--stuck` filtering at the GraphQL layer.**
  Explicitly forbidden by the issue's out-of-scope rule; filter
  is applied in Python after the existing observer query.
- **Adding `Labels.PREFIX` to `foreman.labels`.** That module
  doesn't exist on main yet (see Open Questions). Local
  module-level constants in `cli_v3_ps.py` are the right scope.
- **Refactoring the existing `_build_v3_gh_and_host` helper.** The
  new command reuses it as-is; any "while we're here" cleanups are
  out of scope.
- **Adding any new GraphQL query, new permission scope, or new
  observer entry point.** The new command reuses
  `fetch_project_state` exactly as today.
- **Persisting the diagnostic output anywhere.** The command is
  pure stdout. No new tables, no new files, no new env vars.
