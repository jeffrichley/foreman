> **Parent plan:** [../2026-06-13-foreman-v4-substrate-redesign-implementation.md](../2026-06-13-foreman-v4-substrate-redesign-implementation.md) — read its v4 isolation principle first.
> **Spec:** [../../specs/2026-06-13-foreman-v4-substrate-redesign-design.md](../../specs/2026-06-13-foreman-v4-substrate-redesign-design.md).
> **Branch:** `feat/foreman-v4-substrate`.
> **Gate at end:** `just check` green; then stop for human review before next phase.

## Phase 6 — Typer CLI (operator surface)

### Pre-flight notes (read before dispatching)

**Naming:** `foreman` is the typer CLI starting in Phase 6. The legacy Click app in `foreman/cli.py` stays for `tests/test_roles_*.py` + `tests/test_cli.py` (they import the `cli` Click object directly via `CliRunner` — they don't shell out). The legacy Click commands have NO binary entry point after Phase 6.

**`-v4` suffix already gone.** Commit `b29632e` (before Phase 6) dropped the `-v4` suffix from `SubprocessRoleDispatcher._ROLE_TO_INVOCATION` and deleted the `plan-v4`/`review-v4`/`fix-v4`/`implement-v4` Click commands from `foreman/cli.py`. The dispatcher now invokes `foreman plan --project p --issue-number 1`. Phase 6 makes that actually work by giving typer those command names.

**Verifications done:**
- `typer.testing.CliRunner.invoke(app, [...], obj=ctx)` passes through to `ctx.obj` when the typer app has a `@app.callback()` to force multi-command mode. Plan's `_root` callback (Task 6.1) already does this.
- PyYAML is available in the venv but NOT declared in `packages/foreman/pyproject.toml`. Task 6.1 adds both `typer` and `pyyaml`.
- Repository currently has `list_open_tickets()` only. Task 6.2 adds `list_state_instances_for_ticket()` AND `list_all_tickets()` to the Protocol + InMem + SQLite + contract tests.

The substrate runs but there's no way for a human to look at it. Phase 6 builds the operator-facing CLI in typer + rich, mounted at `foreman.v4.cli`.

Command surface — six groups:

| Group | Commands |
| --- | --- |
| **Query** | `ps`, `show <ticket>`, `queue` |
| **Log** | `log` (recent N), `log --tail` (rich.Live) |
| **Mutation** | `hold`, `resume`, `retry`, `skip`, `drop`, `set-state` |
| **Daemon** | `daemon start`, `daemon stop`, `daemon reload`, `daemon status` |
| **Roles** | `plan`, `review`, `fix`, `implement` (typer wrappers over `run_<role>_cli`) |
| **Output** | global `--format=table|json|yaml` flag (Strategy pattern) |

Phase 5 left the role commands in Click `cli.py` as one-liner shims; Phase 6 replaces the entire `cli.py` body with a thin import that mounts the typer app. Console script entry point in `pyproject.toml` already maps `foreman` → `foreman.cli:main`; we keep that and rewrite `main` to invoke the typer app.

### Task 6.1: Formatter Strategy + typer app skeleton + `CliContext` builder

**Files:**
- Modify: `packages/foreman/pyproject.toml` (add `typer` + `pyyaml` to deps)
- Create: `packages/foreman/src/foreman/v4/cli/__init__.py`
- Create: `packages/foreman/src/foreman/v4/cli/context.py`
- Create: `packages/foreman/src/foreman/v4/cli/formatters.py`
- Test: `packages/foreman/tests/v4/cli/test_context.py`
- Test: `packages/foreman/tests/v4/cli/test_formatters.py`
- Test: `packages/foreman/tests/v4/cli/test_app_skeleton.py`

**FIRST: add the deps.** In `packages/foreman/pyproject.toml`, add to the `[project.dependencies]` list:
```toml
"typer>=0.12,<1",
"pyyaml>=6,<7",
```
Then run `uv sync` to install. These are required for the imports in the new modules.

Strategy pattern per the spec: `TableFormatter`, `JsonFormatter`, `YamlFormatter` implement a common `format(rows: list[dict]) -> str` interface. CLI selects via `--format`.

**Single source of construction for the per-invocation context.** Every typer command needs the same handful of injected dependencies (repo, qm, daemon, etc.). Only one function builds that context — `build_cli_context()`. Production startup calls it. Tests call it. There is NO ad-hoc `obj={"repo": r, "qm": q}` anywhere; if a test or production site assembles those fields by hand, the typed `CliContext` shape would catch it at static check time and `build_cli_context` would catch any missing-required-dependency at runtime. This is the "one builder, no drift" discipline.

`CliContext` is a frozen dataclass with explicit fields; commands access dependencies as `ctx.obj.repo`, never via dict subscript. Adding a new dependency means: add a field to `CliContext`, add a parameter to `build_cli_context`, update production wiring once. Type checker flags every site that missed the rename.

- [ ] **Step 1: Write the failing tests**

```python
# packages/foreman/tests/v4/cli/__init__.py
```

```python
# packages/foreman/tests/v4/cli/test_context.py
"""CliContext — single source of construction for per-invocation deps."""
from __future__ import annotations

import pytest

from foreman.v4.cli.context import CliContext, build_cli_context
from foreman.v4.queue_manager import QueueManager
from foreman.v4.sqlite_repository import SqliteTicketRepository


def test_build_returns_frozen_dataclass():
    repo = SqliteTicketRepository.in_memory()
    ctx = build_cli_context(repo=repo)
    assert isinstance(ctx, CliContext)
    with pytest.raises(AttributeError):
        ctx.repo = None  # frozen


def test_repo_is_required():
    with pytest.raises(TypeError):
        build_cli_context()  # missing repo


def test_optional_fields_default_to_none():
    ctx = build_cli_context(repo=SqliteTicketRepository.in_memory())
    assert ctx.qm is None
    assert ctx.daemon is None
    assert ctx.git is None
    assert ctx.dispatcher is None


def test_all_fields_passed_through():
    repo = SqliteTicketRepository.in_memory()
    qm = QueueManager(repo=repo, max_in_flight=2)
    ctx = build_cli_context(repo=repo, qm=qm)
    assert ctx.repo is repo
    assert ctx.qm is qm
```

```python
# packages/foreman/tests/v4/cli/test_formatters.py
"""Strategy pattern for output formatting — table | json | yaml."""
from __future__ import annotations

import json

from foreman.v4.cli.formatters import (
    JsonFormatter,
    TableFormatter,
    YamlFormatter,
    get_formatter,
)


_ROWS = [
    {"id": 1, "project": "p", "state": "Planning"},
    {"id": 2, "project": "p", "state": "Done"},
]


def test_get_formatter_returns_correct_strategy():
    assert isinstance(get_formatter("table"), TableFormatter)
    assert isinstance(get_formatter("json"), JsonFormatter)
    assert isinstance(get_formatter("yaml"), YamlFormatter)


def test_unknown_format_raises():
    import pytest
    with pytest.raises(ValueError):
        get_formatter("xml")


def test_json_formatter_round_trips():
    out = JsonFormatter().format(_ROWS)
    parsed = json.loads(out)
    assert parsed == _ROWS


def test_yaml_formatter_emits_valid_yaml():
    import yaml
    out = YamlFormatter().format(_ROWS)
    assert yaml.safe_load(out) == _ROWS


def test_table_formatter_includes_column_headers():
    out = TableFormatter().format(_ROWS)
    # Rich.Table renders with column names somewhere; loose assertion
    # avoids over-fitting to escape codes.
    plain = out.replace("\x1b[", "").lower()
    assert "id" in plain and "project" in plain and "state" in plain


def test_table_formatter_empty_rows_is_empty_table():
    out = TableFormatter().format([])
    # No exception; some kind of "no data" affordance is fine.
    assert isinstance(out, str)
```

```python
# packages/foreman/tests/v4/cli/test_app_skeleton.py
"""Typer app skeleton — root invocation prints help, --version returns string."""
from __future__ import annotations

from typer.testing import CliRunner

from foreman.v4.cli import app


def test_app_help_lists_command_groups():
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    # Each group's primary command should appear in --help:
    for cmd in ("ps", "show", "log", "queue", "hold", "resume", "daemon"):
        assert cmd in result.output


def test_app_version_prints_version_string():
    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "foreman" in result.output.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest packages/foreman/tests/v4/cli/ -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the formatter module**

```python
# packages/foreman/src/foreman/v4/cli/formatters.py
"""Strategy pattern for CLI output formatting.

Each formatter consumes a list[dict] and returns a string. Concrete
strategies pluck different shapes from the same input. CLI's --format
flag picks the strategy at the top of each command.
"""

from __future__ import annotations

import io
import json
from typing import Any, Protocol

import yaml
from rich.console import Console
from rich.table import Table


class OutputFormatter(Protocol):
    def format(self, rows: list[dict[str, Any]]) -> str: ...


class JsonFormatter:
    def format(self, rows: list[dict[str, Any]]) -> str:
        return json.dumps(rows, default=str, indent=2)


class YamlFormatter:
    def format(self, rows: list[dict[str, Any]]) -> str:
        return yaml.safe_dump(rows, sort_keys=False, default_flow_style=False)


class TableFormatter:
    def format(self, rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "(no rows)\n"
        buffer = io.StringIO()
        console = Console(file=buffer, force_terminal=True, width=120)
        table = Table(show_header=True, header_style="bold")
        for column in rows[0].keys():
            table.add_column(column)
        for row in rows:
            table.add_row(*(str(row.get(col, "")) for col in rows[0].keys()))
        console.print(table)
        return buffer.getvalue()


_FORMATTERS: dict[str, type[OutputFormatter]] = {
    "table": TableFormatter,
    "json": JsonFormatter,
    "yaml": YamlFormatter,
}


def get_formatter(name: str) -> OutputFormatter:
    try:
        return _FORMATTERS[name]()
    except KeyError as exc:
        raise ValueError(f"unknown format: {name}") from exc
```

```python
# packages/foreman/src/foreman/v4/cli/context.py
"""CliContext — the one-and-only builder for per-invocation deps.

Production startup (Phase 7 daemon entry) calls build_cli_context()
with concretes. Tests call it with fakes. There is no other call site
that assembles these fields — adding a new dep means adding a field
here and updating both call sites once.

Why frozen + typed: ad-hoc dict construction (``obj={"repo": r}``) is
how drift sneaks in — a test forgets the new field, production forgets
the rename. Frozen dataclass makes the shape a single point of edit;
the type checker flags every site that hasn't migrated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foreman.v4.daemon import Daemon
    from foreman.v4.git_provider import GitProvider
    from foreman.v4.queue_manager import QueueManager
    from foreman.v4.repository import TicketRepository
    from foreman.v4.role_dispatcher import RoleDispatcher


@dataclass(frozen=True, slots=True)
class CliContext:
    """Per-invocation context passed via typer's ctx.obj."""
    repo: "TicketRepository"
    qm: "QueueManager | None" = None
    daemon: "Daemon | None" = None
    git: "GitProvider | None" = None
    dispatcher: "RoleDispatcher | None" = None


def build_cli_context(
    *,
    repo: "TicketRepository",
    qm: "QueueManager | None" = None,
    daemon: "Daemon | None" = None,
    git: "GitProvider | None" = None,
    dispatcher: "RoleDispatcher | None" = None,
) -> CliContext:
    """The single point of construction for CliContext.

    Do NOT instantiate CliContext directly. Do NOT pass raw dicts as
    ``obj=`` to runner.invoke / typer. Both paths route through here.
    """
    return CliContext(
        repo=repo, qm=qm, daemon=daemon, git=git, dispatcher=dispatcher,
    )
```

- [ ] **Step 4: Write the typer app skeleton**

```python
# packages/foreman/src/foreman/v4/cli/__init__.py
"""Foreman v4 CLI — typer app.

Command groups land in sibling files (ps.py, show.py, etc.); each
registers itself with this top-level ``app``. The console script
entry point is foreman.cli:main, which imports + invokes this app.
"""

from __future__ import annotations

import typer

from foreman.v4 import __doc__ as _v4_doc

__version__ = "0.4.0"

app = typer.Typer(
    name="foreman",
    help="Foreman v4 — autonomous-loop coordinator",
    no_args_is_help=True,
)


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", help="Print version and exit",
    ),
) -> None:
    if version:
        typer.echo(f"foreman {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


# Stub subcommands so the help text exercise has something to list.
# Real impls land in subsequent tasks; each one replaces its stub here.
@app.command("ps")
def _ps_stub() -> None:
    typer.echo("ps — replaced in Task 6.2")


@app.command("show")
def _show_stub(ticket: int) -> None:
    typer.echo(f"show {ticket} — replaced in Task 6.2")


@app.command("log")
def _log_stub() -> None:
    typer.echo("log — replaced in Task 6.3")


@app.command("queue")
def _queue_stub() -> None:
    typer.echo("queue — replaced in Task 6.2")


@app.command("hold")
def _hold_stub(ticket: int) -> None:
    typer.echo(f"hold {ticket} — replaced in Task 6.4")


@app.command("resume")
def _resume_stub(ticket: int) -> None:
    typer.echo(f"resume {ticket} — replaced in Task 6.4")


daemon_app = typer.Typer(name="daemon", help="Daemon lifecycle")
app.add_typer(daemon_app)


@daemon_app.command("status")
def _daemon_status_stub() -> None:
    typer.echo("daemon status — replaced in Task 6.5")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/cli/ -v`
Expected: 7 passed (5 formatter + 2 skeleton)

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/v4/cli/ packages/foreman/tests/v4/cli/
git commit -m "feat(v4): typer app skeleton + output formatter Strategy"
```

### Task 6.2: Query commands — `ps`, `show`, `queue`

**Files:**
- Create: `packages/foreman/src/foreman/v4/cli/ps.py`
- Create: `packages/foreman/src/foreman/v4/cli/show.py`
- Create: `packages/foreman/src/foreman/v4/cli/queue.py`
- Modify: `packages/foreman/src/foreman/v4/cli/__init__.py` (replace stubs)
- Test: `packages/foreman/tests/v4/cli/test_query_commands.py`

`ps` lists open tickets with current state, held status, last update; columns degrade based on `--format`. `show <ticket>` walks the state_instances journal and renders a `rich.Tree` of the lifecycle. `queue` reports QueueManager depth + in-flight count.

All three query commands accept a `--db` option that defaults to the configured SQLite path. Tests pass an in-memory db directly via a `--repo` injection hook (Typer doesn't support that; tests construct the typer Context with the repo and use `runner.invoke` with the `obj=` argument).

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/cli/test_query_commands.py
"""ps, show, queue — query commands against an in-memory repository."""
from __future__ import annotations

import datetime as dt
import json

from typer.testing import CliRunner

from foreman.v4.cli import app
from foreman.v4.cli.context import build_cli_context
from foreman.v4.outcome import OutcomeKind
from foreman.v4.queue_manager import QueueManager
from foreman.v4.sqlite_repository import SqliteTicketRepository
from foreman.v4.work import WorkItem


def _setup_repo_with_two_tickets() -> SqliteTicketRepository:
    repo = SqliteTicketRepository.in_memory()
    now = dt.datetime(2026, 6, 13, 12, 0, 0)
    a = repo.create_ticket(project="p", issue_number=1, now=now)
    b = repo.create_ticket(project="p", issue_number=2, now=now)
    repo.set_ticket_state(a.id, "Planning", now=now)
    repo.set_ticket_state(b.id, "Done", now=now)
    return repo


def test_ps_lists_open_tickets_as_table():
    repo = _setup_repo_with_two_tickets()
    runner = CliRunner()
    result = runner.invoke(app, ["ps"], obj=build_cli_context(repo=repo))
    assert result.exit_code == 0
    # Only the non-terminal ticket shows by default
    assert "Planning" in result.output
    assert "Done" not in result.output  # filtered out by ps default


def test_ps_all_includes_terminal_tickets():
    repo = _setup_repo_with_two_tickets()
    runner = CliRunner()
    result = runner.invoke(app, ["ps", "--all"], obj=build_cli_context(repo=repo))
    assert "Planning" in result.output
    assert "Done" in result.output


def test_ps_format_json_emits_parseable_json():
    repo = _setup_repo_with_two_tickets()
    runner = CliRunner()
    result = runner.invoke(app, ["ps", "--format", "json"], obj=build_cli_context(repo=repo))
    assert result.exit_code == 0
    rows = json.loads(result.output)
    assert isinstance(rows, list)
    assert any(r["state"] == "Planning" for r in rows)


def test_show_renders_state_history_tree():
    repo = SqliteTicketRepository.in_memory()
    now = dt.datetime(2026, 6, 13, 12, 0, 0)
    ticket = repo.create_ticket(project="p", issue_number=1, now=now)
    inst1 = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Queued", sequence=1, now=now,
    )
    repo.mark_execute_completed(
        inst1.id, now=now, outcome_kind=OutcomeKind.CLEAN,
        outcome_payload={"summary": "ok"}, next_state="Planning",
    )
    repo.close_state_instance(inst1.id, now=now)
    runner = CliRunner()
    result = runner.invoke(app, ["show", str(ticket.id)], obj=build_cli_context(repo=repo))
    assert result.exit_code == 0
    assert "Queued" in result.output
    assert "clean" in result.output.lower()


def test_show_unknown_ticket_returns_nonzero():
    repo = SqliteTicketRepository.in_memory()
    runner = CliRunner()
    result = runner.invoke(app, ["show", "999"], obj=build_cli_context(repo=repo))
    assert result.exit_code != 0


def test_queue_reports_depth_and_in_flight():
    repo = _setup_repo_with_two_tickets()
    qm = QueueManager(repo=repo, max_in_flight=4)
    qm.enqueue(WorkItem(ticket_id=1, state_name="Planning"))
    qm.dequeue()  # 1 in flight, 0 queued
    qm.enqueue(WorkItem(ticket_id=2, state_name="Done"))  # +1 queued
    runner = CliRunner()
    result = runner.invoke(app, ["queue"], obj=build_cli_context(repo=repo, qm=qm))
    assert result.exit_code == 0
    assert "in_flight" in result.output.lower() or "in flight" in result.output.lower()
    assert "1" in result.output  # 1 in flight or 1 queued
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/cli/test_query_commands.py -v`
Expected: FAIL (stubs print placeholders, not real output)

- [ ] **Step 3: Implement `ps`**

```python
# packages/foreman/src/foreman/v4/cli/ps.py
"""ps — list open tickets."""

from __future__ import annotations

import datetime as dt

import typer

from foreman.v4.cli.formatters import get_formatter
from foreman.v4.repository import TicketRepository


def cmd_ps(
    ctx: typer.Context,
    show_all: bool = typer.Option(False, "--all", help="Include terminal states"),
    format: str = typer.Option("table", "--format", help="table|json|yaml"),
) -> None:
    repo: TicketRepository = ctx.obj.repo
    tickets = repo.list_open_tickets() if not show_all else _list_all(repo)
    rows = [
        {
            "id": t.id,
            "project": t.project,
            "issue": t.issue_number,
            "state": t.current_state,
            "held": "yes" if t.is_held else "",
            "updated": t.updated_at.isoformat(),
        }
        for t in tickets
    ]
    typer.echo(get_formatter(format).format(rows), nl=False)


def _list_all(repo: TicketRepository) -> list:
    # Repo doesn't expose "list all tickets" today; if needed, we can extend.
    # For now, fall back to list_open_tickets — Phase 6 doesn't strictly need
    # the all-list since the operator can `show` a specific terminal ticket.
    return repo.list_open_tickets()
```

**`list_all_tickets()` is REQUIRED** (not "if needed"). The test `test_ps_all_includes_terminal_tickets` asserts `"Done"` appears in `--all` output, which means `--all` must surface terminal tickets. Extend the Repository Protocol with `list_all_tickets() -> list[TicketRecord]` and add it to both impls (mirror `list_open_tickets()` shape). Add a contract test for it in `_repository_contract.py`.

- [ ] **Step 4: Implement `show`**

```python
# packages/foreman/src/foreman/v4/cli/show.py
"""show — render state history for one ticket as a rich.Tree."""

from __future__ import annotations

import io

import typer
from rich.console import Console
from rich.tree import Tree

from foreman.v4.repository import TicketNotFoundError, TicketRepository


def cmd_show(
    ctx: typer.Context,
    ticket_id: int = typer.Argument(...),
) -> None:
    repo: TicketRepository = ctx.obj.repo
    try:
        ticket = repo.get_ticket(ticket_id)
    except TicketNotFoundError:
        typer.echo(f"ticket {ticket_id} not found", err=True)
        raise typer.Exit(code=1)

    instances = _instances_for_ticket(repo, ticket_id)
    tree = Tree(
        f"[bold]Ticket {ticket.id}[/bold] "
        f"({ticket.project}#{ticket.issue_number}) — {ticket.current_state}"
    )
    for inst in sorted(instances, key=lambda i: i.sequence):
        outcome = inst.outcome_kind.value if inst.outcome_kind else "in-flight"
        next_ = inst.next_state or "—"
        node = tree.add(
            f"[cyan]{inst.state_name}[/cyan] #{inst.sequence} "
            f"→ {outcome} → {next_}"
        )
        if inst.failure_reason:
            node.add(f"[red]failed @ {inst.failure_phase}: {inst.failure_reason}[/red]")

    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True, width=120)
    console.print(tree)
    typer.echo(buffer.getvalue(), nl=False)


def _instances_for_ticket(repo: TicketRepository, ticket_id: int) -> list:
    # Use Repository helper added in Phase 4 if it exposes a list-by-ticket;
    # otherwise extend it now. Same pattern as latest_pr_number_for_ticket —
    # add list_state_instances_for_ticket() to the Protocol + both impls +
    # the contract test.
    return repo.list_state_instances_for_ticket(ticket_id)
```

Add `list_state_instances_for_ticket(ticket_id) -> list[StateInstanceRecord]` to the Protocol + both impls + contract tests (same shape as the existing `count_state_instances_for_ticket`).

- [ ] **Step 5: Implement `queue`**

```python
# packages/foreman/src/foreman/v4/cli/queue.py
"""queue — report QueueManager depth + in-flight."""

from __future__ import annotations

import typer

from foreman.v4.cli.formatters import get_formatter


def cmd_queue(
    ctx: typer.Context,
    format: str = typer.Option("table", "--format"),
) -> None:
    qm = ctx.obj.qm
    if qm is None:
        typer.echo("queue manager not configured", err=True)
        raise typer.Exit(code=1)
    rows = [{
        "in_flight": qm.in_flight_count(),
        "queued": qm.queue_depth(),
    }]
    typer.echo(get_formatter(format).format(rows), nl=False)
```

- [ ] **Step 6: Wire into the typer app**

In `foreman/v4/cli/__init__.py`, replace the `_ps_stub`, `_show_stub`, `_queue_stub` registrations with:

```python
from foreman.v4.cli.ps import cmd_ps
from foreman.v4.cli.show import cmd_show
from foreman.v4.cli.queue import cmd_queue

app.command("ps")(cmd_ps)
app.command("show")(cmd_show)
app.command("queue")(cmd_queue)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/cli/test_query_commands.py -v`
Expected: 6 passed

- [ ] **Step 8: Commit**

```bash
git add packages/foreman/src/foreman/v4/cli/ packages/foreman/src/foreman/v4/repository.py packages/foreman/src/foreman/v4/sqlite_repository.py packages/foreman/tests/v4/_repository_contract.py packages/foreman/tests/v4/cli/test_query_commands.py
git commit -m "feat(v4): query commands — ps, show, queue with Strategy-formatted output"
```

### Task 6.3: `log` command (recent + `--tail`)

**Files:**
- Create: `packages/foreman/src/foreman/v4/cli/log.py`
- Modify: `packages/foreman/src/foreman/v4/cli/__init__.py`
- Test: `packages/foreman/tests/v4/cli/test_log_command.py`

`foreman log` prints the N most-recent JSON-lines from the structured log file (default N=50). `--tail` follows the file with `rich.Live` rendering. `--ticket <id>` / `--state <name>` filter inline.

Bounded scope for v4 ship: `--tail` is a polling-based reader (read file size; re-read on growth). No `inotify`/`ReadDirectoryChangesW` magic.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/cli/test_log_command.py
"""log — recent + filtered JSON-lines view."""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from foreman.v4.cli import app
from foreman.v4.cli.context import build_cli_context
from foreman.v4.sqlite_repository import SqliteTicketRepository


def _write_log(path: Path, lines: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def test_log_prints_recent_lines(tmp_path: Path):
    log_path = tmp_path / "transitions.jsonl"
    _write_log(log_path, [
        {"event": "state_entered", "ticket_id": 1, "state": "Planning",
         "at": "2026-06-13T12:00:00"},
        {"event": "execute_completed", "ticket_id": 1, "state": "Planning",
         "outcome_kind": "clean", "at": "2026-06-13T12:01:00"},
    ])
    runner = CliRunner()
    result = runner.invoke(
        app, ["log", "--log-path", str(log_path)],
        obj=build_cli_context(repo=SqliteTicketRepository.in_memory()),
    )
    assert result.exit_code == 0
    assert "state_entered" in result.output
    assert "Planning" in result.output


def test_log_filter_by_ticket(tmp_path: Path):
    log_path = tmp_path / "transitions.jsonl"
    _write_log(log_path, [
        {"event": "state_entered", "ticket_id": 1, "state": "Planning"},
        {"event": "state_entered", "ticket_id": 2, "state": "SpecReview"},
    ])
    runner = CliRunner()
    result = runner.invoke(
        app, ["log", "--log-path", str(log_path), "--ticket", "1"],
        obj=build_cli_context(repo=SqliteTicketRepository.in_memory()),
    )
    assert "Planning" in result.output
    assert "SpecReview" not in result.output


def test_log_filter_by_state(tmp_path: Path):
    log_path = tmp_path / "transitions.jsonl"
    _write_log(log_path, [
        {"event": "state_entered", "ticket_id": 1, "state": "Planning"},
        {"event": "state_entered", "ticket_id": 2, "state": "Merging"},
    ])
    runner = CliRunner()
    result = runner.invoke(
        app, ["log", "--log-path", str(log_path), "--state", "Merging"],
        obj=build_cli_context(repo=SqliteTicketRepository.in_memory()),
    )
    assert "Merging" in result.output
    assert "Planning" not in result.output


def test_log_limit_caps_output(tmp_path: Path):
    log_path = tmp_path / "transitions.jsonl"
    _write_log(log_path, [
        {"event": "state_entered", "ticket_id": i, "state": "Planning"}
        for i in range(100)
    ])
    runner = CliRunner()
    result = runner.invoke(
        app, ["log", "--log-path", str(log_path), "--limit", "5"],
        obj=build_cli_context(repo=SqliteTicketRepository.in_memory()),
    )
    assert result.output.count("state_entered") == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/cli/test_log_command.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `log`**

```python
# packages/foreman/src/foreman/v4/cli/log.py
"""log — recent + filtered JSON-lines view of foreman.v4.transitions.

Polls the file for --tail; no platform-specific watcher magic. The N most
recent lines are shown by default; --ticket / --state filter inline.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer


def cmd_log(
    ctx: typer.Context,
    log_path: Path = typer.Option(
        Path.home() / ".foreman/v4/transitions.jsonl", "--log-path",
        help="Path to the JSON-lines transition log",
    ),
    limit: int = typer.Option(50, "--limit"),
    ticket: int | None = typer.Option(None, "--ticket"),
    state: str | None = typer.Option(None, "--state"),
    tail: bool = typer.Option(False, "--tail", help="Follow the log (rich.Live)"),
) -> None:
    if tail:
        _tail(log_path, ticket=ticket, state=state)
        return
    rows = _read_last(log_path, limit, ticket=ticket, state=state)
    for row in rows:
        typer.echo(json.dumps(row))


def _read_last(
    path: Path, limit: int, *, ticket: int | None, state: str | None,
) -> list[dict]:
    if not path.exists():
        return []
    matched: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if ticket is not None and row.get("ticket_id") != ticket:
                continue
            if state is not None and row.get("state") != state:
                continue
            matched.append(row)
    return matched[-limit:]


def _tail(
    path: Path, *, ticket: int | None, state: str | None,
) -> None:
    """Polling-based follow. Cheap on small logs; not optimized for high-volume."""
    import time

    from rich.console import Console
    from rich.live import Live
    from rich.text import Text

    console = Console()
    seen_size = 0
    with Live(Text(""), console=console, refresh_per_second=4) as live:
        try:
            while True:
                if path.exists():
                    current_size = path.stat().st_size
                    if current_size != seen_size:
                        new_rows = _read_last(
                            path, limit=20,
                            ticket=ticket, state=state,
                        )
                        text = Text("\n".join(json.dumps(r) for r in new_rows))
                        live.update(text)
                        seen_size = current_size
                time.sleep(0.5)
        except KeyboardInterrupt:
            return
```

- [ ] **Step 4: Wire into app**

In `foreman/v4/cli/__init__.py`, replace `_log_stub`:

```python
from foreman.v4.cli.log import cmd_log
app.command("log")(cmd_log)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/cli/test_log_command.py -v`
Expected: 4 passed

(`--tail` is not unit-tested — it's a polling loop with `KeyboardInterrupt` exit. Validates manually during Phase 7 dogfood.)

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/v4/cli/log.py packages/foreman/src/foreman/v4/cli/__init__.py packages/foreman/tests/v4/cli/test_log_command.py
git commit -m "feat(v4): log command — recent + filtered JSON-lines view"
```

### Task 6.4: Mutation commands — `hold/resume/retry/skip/drop/set-state`

**Files:**
- Create: `packages/foreman/src/foreman/v4/cli/mutations.py`
- Modify: `packages/foreman/src/foreman/v4/cli/__init__.py`
- Test: `packages/foreman/tests/v4/cli/test_mutation_commands.py`

Each command operates on a ticket id. Semantics:

| Command | Effect |
| --- | --- |
| `hold <ticket> --reason <r>` | Set `held_by`/`held_at`/`held_reason`. Operator's name comes from `$USER` or `--by`. |
| `resume <ticket>` | Clear hold. |
| `retry <ticket>` | Enqueue WorkItem for current state. Re-dispatches without changing state. |
| `skip <ticket> <next-state>` | Like set-state but logs intent; only valid if current state has no in-flight execute. |
| `drop <ticket>` | Set state to `Failed`. Terminal — operator giving up on the ticket. |
| `set-state <ticket> <state>` | Move to arbitrary state. Power-user; logs warning if it crosses a non-adjacent edge. |

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/cli/test_mutation_commands.py
"""hold/resume/retry/skip/drop/set-state — operator mutations."""
from __future__ import annotations

import datetime as dt

from typer.testing import CliRunner

from foreman.v4.cli import app
from foreman.v4.cli.context import build_cli_context
from foreman.v4.queue_manager import QueueManager
from foreman.v4.sqlite_repository import SqliteTicketRepository
from foreman.v4.work import WorkItem


def _make(state: str = "Planning") -> tuple[SqliteTicketRepository, int]:
    repo = SqliteTicketRepository.in_memory()
    t = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.set_ticket_state(t.id, state, now=dt.datetime(2026, 6, 13))
    return repo, t.id


def test_hold_sets_held_columns():
    repo, tid = _make()
    runner = CliRunner()
    result = runner.invoke(
        app, ["hold", str(tid), "--reason", "vacation", "--by", "jeff"],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code == 0
    assert repo.get_ticket(tid).is_held
    assert repo.get_ticket(tid).held_reason == "vacation"


def test_resume_clears_held_columns():
    repo, tid = _make()
    repo.hold_ticket(tid, held_by="jeff", reason="x", now=dt.datetime(2026, 6, 13))
    runner = CliRunner()
    result = runner.invoke(app, ["resume", str(tid)], obj=build_cli_context(repo=repo))
    assert result.exit_code == 0
    assert not repo.get_ticket(tid).is_held


def test_retry_enqueues_workitem_for_current_state():
    repo, tid = _make()
    qm = QueueManager(repo=repo, max_in_flight=4)
    runner = CliRunner()
    result = runner.invoke(
        app, ["retry", str(tid)],
        obj=build_cli_context(repo=repo, qm=qm),
    )
    assert result.exit_code == 0
    assert qm.dequeue() == WorkItem(ticket_id=tid, state_name="Planning")


def test_set_state_changes_current_state():
    repo, tid = _make()
    runner = CliRunner()
    result = runner.invoke(
        app, ["set-state", str(tid), "SpecReview"],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code == 0
    assert repo.get_ticket(tid).current_state == "SpecReview"


def test_set_state_unknown_state_errors():
    repo, tid = _make()
    runner = CliRunner()
    result = runner.invoke(
        app, ["set-state", str(tid), "NotAState"],
        obj=build_cli_context(repo=repo),
    )
    assert result.exit_code != 0


def test_drop_sets_failed():
    repo, tid = _make()
    runner = CliRunner()
    result = runner.invoke(app, ["drop", str(tid)], obj=build_cli_context(repo=repo))
    assert repo.get_ticket(tid).current_state == "Failed"


def test_skip_targets_next_state():
    repo, tid = _make()
    runner = CliRunner()
    result = runner.invoke(
        app, ["skip", str(tid), "ImplReview"],
        obj=build_cli_context(repo=repo),
    )
    assert repo.get_ticket(tid).current_state == "ImplReview"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/cli/test_mutation_commands.py -v`
Expected: FAIL

- [ ] **Step 3: Implement mutations**

```python
# packages/foreman/src/foreman/v4/cli/mutations.py
"""hold/resume/retry/skip/drop/set-state — operator mutations.

Each command resolves the ticket via repo + applies the change. retry
enqueues a WorkItem (needs the QueueManager from ctx); the rest are
repository-only.
"""

from __future__ import annotations

import datetime as dt
import os

import typer

from foreman.v4.queue_manager import QueueManager
from foreman.v4.repository import TicketNotFoundError, TicketRepository
from foreman.v4.states.registry import STATE_REGISTRY
from foreman.v4.work import WorkItem


def _resolve(ctx: typer.Context, ticket_id: int):
    repo: TicketRepository = ctx.obj.repo
    try:
        ticket = repo.get_ticket(ticket_id)
    except TicketNotFoundError:
        typer.echo(f"ticket {ticket_id} not found", err=True)
        raise typer.Exit(code=1)
    return repo, ticket


def cmd_hold(
    ctx: typer.Context,
    ticket_id: int = typer.Argument(...),
    reason: str = typer.Option(..., "--reason"),
    by: str = typer.Option(None, "--by", help="Operator name (defaults to $USER)"),
) -> None:
    repo, _ = _resolve(ctx, ticket_id)
    repo.hold_ticket(
        ticket_id,
        held_by=by or os.environ.get("USER", "operator"),
        reason=reason,
        now=dt.datetime.now(dt.UTC),
    )
    typer.echo(f"ticket {ticket_id} held")


def cmd_resume(
    ctx: typer.Context,
    ticket_id: int = typer.Argument(...),
) -> None:
    repo, _ = _resolve(ctx, ticket_id)
    repo.resume_ticket(ticket_id, now=dt.datetime.now(dt.UTC))
    typer.echo(f"ticket {ticket_id} resumed")


def cmd_retry(
    ctx: typer.Context,
    ticket_id: int = typer.Argument(...),
) -> None:
    repo, ticket = _resolve(ctx, ticket_id)
    qm: QueueManager | None = ctx.obj.qm
    if qm is None:
        typer.echo("retry requires a queue manager", err=True)
        raise typer.Exit(code=1)
    qm.enqueue(WorkItem(ticket_id=ticket_id, state_name=ticket.current_state))
    typer.echo(f"ticket {ticket_id} re-enqueued in {ticket.current_state}")


def cmd_set_state(
    ctx: typer.Context,
    ticket_id: int = typer.Argument(...),
    state: str = typer.Argument(...),
) -> None:
    repo, ticket = _resolve(ctx, ticket_id)
    if state not in STATE_REGISTRY:
        typer.echo(f"unknown state: {state}", err=True)
        raise typer.Exit(code=1)
    repo.set_ticket_state(ticket_id, state, now=dt.datetime.now(dt.UTC))
    typer.echo(f"ticket {ticket_id} moved {ticket.current_state} → {state}")


def cmd_drop(
    ctx: typer.Context,
    ticket_id: int = typer.Argument(...),
) -> None:
    repo, _ = _resolve(ctx, ticket_id)
    repo.set_ticket_state(ticket_id, "Failed", now=dt.datetime.now(dt.UTC))
    typer.echo(f"ticket {ticket_id} dropped (→ Failed)")


def cmd_skip(
    ctx: typer.Context,
    ticket_id: int = typer.Argument(...),
    next_state: str = typer.Argument(...),
) -> None:
    repo, _ = _resolve(ctx, ticket_id)
    if next_state not in STATE_REGISTRY:
        typer.echo(f"unknown state: {next_state}", err=True)
        raise typer.Exit(code=1)
    repo.set_ticket_state(ticket_id, next_state, now=dt.datetime.now(dt.UTC))
    typer.echo(f"ticket {ticket_id} skipped to {next_state}")
```

- [ ] **Step 4: Wire into app**

```python
# In foreman/v4/cli/__init__.py — replace the hold/resume stubs:
from foreman.v4.cli.mutations import (
    cmd_drop, cmd_hold, cmd_resume, cmd_retry, cmd_set_state, cmd_skip,
)
app.command("hold")(cmd_hold)
app.command("resume")(cmd_resume)
app.command("retry")(cmd_retry)
app.command("skip")(cmd_skip)
app.command("drop")(cmd_drop)
app.command("set-state")(cmd_set_state)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/cli/test_mutation_commands.py -v`
Expected: 7 passed

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/v4/cli/mutations.py packages/foreman/src/foreman/v4/cli/__init__.py packages/foreman/tests/v4/cli/test_mutation_commands.py
git commit -m "feat(v4): mutation commands — hold/resume/retry/skip/drop/set-state"
```

### Task 6.5: Daemon commands — `start/stop/reload/status`

**Files:**
- Create: `packages/foreman/src/foreman/v4/cli/daemon.py`
- Create: `packages/foreman/src/foreman/v4/daemon.py` (the actual daemon class — drives the Poller + WorkerPool loop)
- Test: `packages/foreman/tests/v4/cli/test_daemon_commands.py`
- Test: `packages/foreman/tests/v4/test_daemon.py`

`Daemon` class (in `v4/daemon.py`) hosts a list of Pollers + shared QueueManager + WorkerPool. **Built multi-project from the start** (don't build single-project here and refactor in Phase 7 — pull the multi-project shape forward). Runs a tick loop on a configurable cadence, handles SIGTERM/SIGINT gracefully (drain in-flight, exit). PID file under `~/.foreman/v4/daemon.pid`.

Daemon takes `pollers: list[Poller]` (one per project). Shared QM + WorkerPool across projects. Tick loop calls `tick()` on every poller in turn, then drains the pool. Per-project concurrency is enforced at the QM (max_in_flight cap is global). Phase 7 then only adds bootstrap wiring on top — no refactor.

CLI commands:
- `daemon start` — start in the foreground (or `--background` for nohup-style detach later); writes PID file
- `daemon stop` — read PID file, send SIGTERM, wait for clean exit
- `daemon reload` — re-read config without restart (basic — re-reads cadence + max_in_flight)
- `daemon status` — show PID file + lock state + tick count

This task is the most "moving parts" in Phase 6; consider breaking start/stop/reload into one subtask and the Daemon class into another.

- [ ] **Step 1: Write tests for the Daemon class first**

```python
# packages/foreman/tests/v4/test_daemon.py
"""Daemon class — owns the Poller + QM + WorkerPool tick loop."""
from __future__ import annotations

import datetime as dt
import threading
import time

from foreman.v4.daemon import Daemon, DaemonConfig
from foreman.v4.git_provider import FakeGitProvider, MergeVerdict, PRState
from foreman.v4.role_dispatcher import FakeRoleDispatcher
from foreman.v4.sqlite_repository import SqliteTicketRepository


def _canned(kind: str, *, pr_number: int | None = None) -> str:
    art = f',"artifacts":{{"pr_number":{pr_number}}}' if pr_number else ""
    return f'FOREMAN_OUTCOME:{{"kind":"{kind}","confidence":"high","summary":"x"{art}}}'


def test_daemon_one_tick_processes_one_ticket():
    repo = SqliteTicketRepository.in_memory()
    git = FakeGitProvider()
    git.set_open_issues_with_label(project="p", label="foreman:plan", issue_numbers={1})
    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 1): _canned("clean"),
    })
    poller = Poller(
        repo=repo, qm=None, git=git,
        project="p", trigger_label="foreman:plan",
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    daemon = Daemon(
        repo=repo, git=git, dispatcher=dispatcher,
        pollers=[poller],
        config=DaemonConfig(tick_seconds=0, max_in_flight=4),
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    daemon.tick_once()
    daemon.tick_once()
    ticket = repo.get_ticket_by_issue(project="p", issue_number=1)
    # After Queued advances to Planning (clean) → SpecReview
    assert ticket.current_state in ("Planning", "SpecReview")


def test_daemon_run_until_stopped_responds_to_stop_event():
    repo = SqliteTicketRepository.in_memory()
    daemon = Daemon(
        repo=repo, git=FakeGitProvider(),
        dispatcher=FakeRoleDispatcher(responses={}),
        config=DaemonConfig(
            project="p", trigger_label="foreman:plan",
            tick_seconds=0.01, max_in_flight=4,
        ),
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    thread = threading.Thread(target=daemon.run_forever)
    thread.start()
    time.sleep(0.05)
    daemon.stop()
    thread.join(timeout=2)
    assert not thread.is_alive()
```

- [ ] **Step 2: Write the Daemon class**

```python
# packages/foreman/src/foreman/v4/daemon.py
"""Daemon — owns the Poller + QueueManager + WorkerPool tick loop.

Single-thread loop: every ``tick_seconds`` we poll then drain. Stop
mechanic is a threading.Event; SIGTERM/SIGINT installation lives in
the CLI start command, not here.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from dataclasses import dataclass
from typing import Callable

from foreman.v4.event_bus import EventBus
from foreman.v4.git_provider import GitProvider
from foreman.v4.poller import Poller
from foreman.v4.queue_manager import QueueManager
from foreman.v4.repository import TicketRepository
from foreman.v4.role_dispatcher import RoleDispatcher
from foreman.v4.worker_pool import WorkerPool


@dataclass
class DaemonConfig:
    tick_seconds: float
    max_in_flight: int


class Daemon:
    """Multi-project daemon. Holds a list of Pollers (one per project)
    sharing one QueueManager + one WorkerPool. Per-project Pollers can
    be constructed without a QM; the Daemon injects its shared QM into
    any Poller that arrived without one."""

    def __init__(
        self,
        *,
        repo: TicketRepository,
        git: GitProvider,
        dispatcher: RoleDispatcher,
        pollers: list[Poller],
        config: DaemonConfig,
        clock: Callable[[], dt.datetime],
        bus: EventBus | None = None,
    ) -> None:
        self._repo = repo
        self._git = git
        self._dispatcher = dispatcher
        self._config = config
        self._clock = clock
        self._bus = bus
        self._qm = QueueManager(repo=repo, max_in_flight=config.max_in_flight)
        # Wire the shared QM into every Poller that was constructed without one.
        self._pollers = [self._with_qm(p) for p in pollers]
        self._pool = WorkerPool(
            repo=repo, qm=self._qm, dispatcher=dispatcher,
            git=git, bus=bus, clock=clock,
        )
        self._stop = threading.Event()

    def _with_qm(self, poller: Poller) -> Poller:
        # Pollers can be constructed without a QM at config time;
        # inject the daemon's shared QM here.
        if poller._qm is None:  # type: ignore[attr-defined]
            poller._qm = self._qm  # type: ignore[attr-defined]
        return poller

    def tick_once(self) -> None:
        for poller in self._pollers:
            poller.tick()
        self._pool.run_until_empty()

    def run_forever(self) -> None:
        while not self._stop.is_set():
            self.tick_once()
            self._stop.wait(self._config.tick_seconds)

    def stop(self) -> None:
        self._stop.set()
```

- [ ] **Step 3: Run Daemon tests**

Run: `uv run pytest packages/foreman/tests/v4/test_daemon.py -v`
Expected: 2 passed

- [ ] **Step 4: Implement CLI daemon commands**

```python
# packages/foreman/src/foreman/v4/cli/daemon.py
"""daemon start/stop/reload/status — lifecycle commands."""

from __future__ import annotations

import os
import signal
from pathlib import Path

import typer


_PID_PATH = Path.home() / ".foreman" / "v4" / "daemon.pid"


def cmd_daemon_start(ctx: typer.Context) -> None:
    """Start the daemon in the foreground.

    Tests inject the prepared Daemon via build_cli_context(daemon=...).
    Production wiring builds the Daemon from config and feeds it into
    build_cli_context the same way.
    """
    daemon = ctx.obj.daemon
    if daemon is None:
        typer.echo("daemon not configured", err=True)
        raise typer.Exit(code=1)
    _PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PID_PATH.write_text(str(os.getpid()))
    try:
        # Install SIGTERM/SIGINT handlers to call daemon.stop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_args: daemon.stop())
        daemon.run_forever()
    finally:
        if _PID_PATH.exists():
            _PID_PATH.unlink()


def cmd_daemon_stop(ctx: typer.Context) -> None:
    if not _PID_PATH.exists():
        typer.echo("no daemon PID file", err=True)
        raise typer.Exit(code=1)
    pid = int(_PID_PATH.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        typer.echo(f"PID {pid} not running; cleaning stale file")
        _PID_PATH.unlink()
        return
    typer.echo(f"sent SIGTERM to {pid}")


def cmd_daemon_status(ctx: typer.Context) -> None:
    if not _PID_PATH.exists():
        typer.echo("daemon: not running")
        return
    pid = int(_PID_PATH.read_text().strip())
    try:
        os.kill(pid, 0)
        typer.echo(f"daemon: running (pid {pid})")
    except ProcessLookupError:
        typer.echo(f"daemon: stale PID file (pid {pid} not alive)")


def cmd_daemon_reload(ctx: typer.Context) -> None:
    if not _PID_PATH.exists():
        typer.echo("no daemon PID file", err=True)
        raise typer.Exit(code=1)
    pid = int(_PID_PATH.read_text().strip())
    os.kill(pid, signal.SIGHUP)
    typer.echo(f"sent SIGHUP to {pid}")
```

- [ ] **Step 5: Write CLI tests (status only — start/stop need real PIDs)**

```python
# packages/foreman/tests/v4/cli/test_daemon_commands.py
"""daemon status — read PID file, report state."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from foreman.v4.cli import app
from foreman.v4.cli.context import build_cli_context
from foreman.v4.sqlite_repository import SqliteTicketRepository


def test_status_when_no_pid_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "foreman.v4.cli.daemon._PID_PATH", tmp_path / "missing.pid",
    )
    result = CliRunner().invoke(app, ["daemon", "status"], obj=build_cli_context(repo=SqliteTicketRepository.in_memory()))
    assert "not running" in result.output


def test_status_when_pid_alive(tmp_path: Path, monkeypatch):
    pid_path = tmp_path / "daemon.pid"
    pid_path.write_text("12345")
    monkeypatch.setattr("foreman.v4.cli.daemon._PID_PATH", pid_path)
    with patch("os.kill") as mock_kill:
        mock_kill.return_value = None
        result = CliRunner().invoke(app, ["daemon", "status"], obj=build_cli_context(repo=SqliteTicketRepository.in_memory()))
    assert "running" in result.output
    assert "12345" in result.output


def test_status_when_pid_stale(tmp_path: Path, monkeypatch):
    pid_path = tmp_path / "daemon.pid"
    pid_path.write_text("99999")
    monkeypatch.setattr("foreman.v4.cli.daemon._PID_PATH", pid_path)
    with patch("os.kill", side_effect=ProcessLookupError):
        result = CliRunner().invoke(app, ["daemon", "status"], obj=build_cli_context(repo=SqliteTicketRepository.in_memory()))
    assert "stale" in result.output
```

- [ ] **Step 6: Wire into the typer app**

```python
# In foreman/v4/cli/__init__.py:
from foreman.v4.cli.daemon import (
    cmd_daemon_reload, cmd_daemon_start, cmd_daemon_status, cmd_daemon_stop,
)
daemon_app.command("start")(cmd_daemon_start)
daemon_app.command("stop")(cmd_daemon_stop)
daemon_app.command("reload")(cmd_daemon_reload)
daemon_app.command("status")(cmd_daemon_status)
```

(`daemon_app` was added in Task 6.1.)

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/cli/test_daemon_commands.py packages/foreman/tests/v4/test_daemon.py -v`
Expected: 5 passed

- [ ] **Step 8: Commit**

```bash
git add packages/foreman/src/foreman/v4/daemon.py packages/foreman/src/foreman/v4/cli/daemon.py packages/foreman/src/foreman/v4/cli/__init__.py packages/foreman/tests/v4/test_daemon.py packages/foreman/tests/v4/cli/test_daemon_commands.py
git commit -m "feat(v4): Daemon class + daemon start/stop/reload/status CLI"
```

### Task 6.6: Role commands in typer + console script swap

**Files:**
- Modify: `packages/foreman/src/foreman/v4/cli/__init__.py` (register `plan/review/fix/implement`)
- Modify: `packages/foreman/pyproject.toml` (entry point: `foreman = "foreman.v4.cli:main"`)
- Test: `packages/foreman/tests/v4/cli/test_role_commands.py`

**Files NOT touched in this task:**
- `packages/foreman/src/foreman/cli.py` — legacy Click app stays intact. The legacy positional-`<issue_url>` `plan`/`review`/`fix`/`implement` commands stay (tagged `# v4-PHASE-8-KILL` since Phase 5). `tests/test_roles_*.py` and `tests/test_cli.py` import the `cli` Click object directly and exercise via `CliRunner` — no binary needed. Phase 8 atomic cutover deletes the entire Click `cli.py` + the 5 legacy test files.

The four role commands `plan`/`review`/`fix`/`implement` go into the typer app. They delegate to the `run_<role>_cli` functions from Phase 5 (in `foreman/roles/*.py`), so no behavior change — just a different framework wrapping them. The `-v4` suffix is gone (cleanup commit `b29632e` before Phase 6).

- [ ] **Step 1: Add the typer commands**

```python
# Append to packages/foreman/src/foreman/v4/cli/__init__.py
import typer as _typer

from foreman.roles.fixer import run_fixer_cli
from foreman.roles.planner import run_planner_cli
from foreman.roles.reviewer import run_reviewer_cli
from foreman.roles.worker import run_worker_cli


@app.command("plan")
def cmd_plan(
    project: str = _typer.Option(..., "--project"),
    issue_number: int = _typer.Option(..., "--issue-number"),
) -> None:
    """Run the v4 Planner: emit FOREMAN_OUTCOME; exit code carries success/failure."""
    raise _typer.Exit(code=run_planner_cli(project=project, issue_number=issue_number))


@app.command("review")
def cmd_review(
    project: str = _typer.Option(..., "--project"),
    issue_number: int = _typer.Option(..., "--issue-number"),
    target: str = _typer.Option(..., "--target", help="spec|impl"),
) -> None:
    """Run the v4 Reviewer (target-aware): emit FOREMAN_OUTCOME; exit code carries verdict."""
    raise _typer.Exit(code=run_reviewer_cli(
        project=project, issue_number=issue_number, target=target,
    ))


@app.command("fix")
def cmd_fix(
    project: str = _typer.Option(..., "--project"),
    issue_number: int = _typer.Option(..., "--issue-number"),
    target: str = _typer.Option(..., "--target", help="spec|impl"),
) -> None:
    """Run the v4 Fixer (target-aware): emit FOREMAN_OUTCOME; exit code carries verdict."""
    raise _typer.Exit(code=run_fixer_cli(
        project=project, issue_number=issue_number, target=target,
    ))


@app.command("implement")
def cmd_implement(
    project: str = _typer.Option(..., "--project"),
    issue_number: int = _typer.Option(..., "--issue-number"),
) -> None:
    """Run the v4 Worker: emit FOREMAN_OUTCOME; exit code carries verdict."""
    raise _typer.Exit(code=run_worker_cli(
        project=project, issue_number=issue_number,
    ))
```

- [ ] **Step 2: Add typer `main()` entry point in `foreman/v4/cli/__init__.py`**

```python
def main() -> None:
    """Console-script entry point. Invokes the typer app."""
    app()
```

This sits alongside the existing `app = typer.Typer(...)` declaration.

- [ ] **Step 3: Swap `pyproject.toml` entry point to typer**

In `packages/foreman/pyproject.toml`, change:
```toml
[project.scripts]
foreman = "foreman.cli:main"
```
to:
```toml
[project.scripts]
foreman = "foreman.v4.cli:main"
```

After `uv sync` (or `pip install -e .`), `foreman plan --project p --issue-number 1` invokes the typer app. The legacy Click app in `foreman/cli.py` is unreachable via the binary BUT still importable by tests as `from foreman.cli import cli` for `CliRunner` (this is what `tests/test_roles_*.py` + `tests/test_cli.py` do). DO NOT delete the legacy `foreman/cli.py` — Phase 8 handles that as part of the atomic cutover.

- [ ] **Step 4: Write the failing tests**

```python
# packages/foreman/tests/v4/cli/test_role_commands.py
"""Typer role commands delegate to run_<role>_cli."""
from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from foreman.v4.cli import app


def test_plan_command_invokes_run_planner_cli():
    with patch("foreman.v4.cli.run_planner_cli", return_value=0) as mock:
        result = CliRunner().invoke(
            app, ["plan", "--project", "p", "--issue-number", "1"],
        )
        assert result.exit_code == 0
        mock.assert_called_once_with(project="p", issue_number=1)


def test_review_command_passes_target():
    with patch("foreman.v4.cli.run_reviewer_cli", return_value=0) as mock:
        CliRunner().invoke(
            app,
            ["review", "--project", "p", "--issue-number", "1", "--target", "spec"],
        )
        mock.assert_called_once_with(project="p", issue_number=1, target="spec")


def test_implement_command_invokes_run_worker_cli():
    with patch("foreman.v4.cli.run_worker_cli", return_value=0) as mock:
        CliRunner().invoke(
            app, ["implement", "--project", "p", "--issue-number", "1"],
        )
        mock.assert_called_once_with(project="p", issue_number=1)


def test_fix_command_passes_target():
    with patch("foreman.v4.cli.run_fixer_cli", return_value=0) as mock:
        CliRunner().invoke(
            app,
            ["fix", "--project", "p", "--issue-number", "1", "--target", "impl"],
        )
        mock.assert_called_once_with(project="p", issue_number=1, target="impl")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/cli/test_role_commands.py -v`
Expected: 4 passed

Phase 5's `tests/v4/roles/test_*_outcome.py` tests still pass against the same `run_<role>_cli` functions — the typer wrapper doesn't change the function-level test.

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/v4/cli/__init__.py packages/foreman/pyproject.toml packages/foreman/tests/v4/cli/test_role_commands.py
git commit -m "feat(v4): add role commands to typer; swap foreman entry to v4 cli (legacy cli.py untouched)"
```

### Task 6.7: End-to-end CLI smoke

**Files:**
- Create: `packages/foreman/tests/v4/cli/test_phase6_e2e.py`

Drives a ticket through one cycle of mutation + query: `hold` then `ps` (should show held), `resume` then `ps` (no longer held), `retry` then `queue` (depth = 1).

- [ ] **Step 1: Write the test**

```python
# packages/foreman/tests/v4/cli/test_phase6_e2e.py
"""Phase 6 e2e — operator commands work against a live repo+QM."""
from __future__ import annotations

import datetime as dt

from typer.testing import CliRunner

from foreman.v4.cli import app
from foreman.v4.cli.context import build_cli_context
from foreman.v4.queue_manager import QueueManager
from foreman.v4.sqlite_repository import SqliteTicketRepository


def test_hold_ps_resume_retry_queue_workflow():
    repo = SqliteTicketRepository.in_memory()
    t = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.set_ticket_state(t.id, "Planning", now=dt.datetime(2026, 6, 13))
    qm = QueueManager(repo=repo, max_in_flight=4)
    runner = CliRunner()
    ctx = build_cli_context(repo=repo, qm=qm)

    # 1. hold
    r1 = runner.invoke(app, ["hold", str(t.id), "--reason", "test"], obj=ctx)
    assert r1.exit_code == 0

    # 2. ps — held column populated
    r2 = runner.invoke(app, ["ps"], obj=ctx)
    assert "yes" in r2.output  # the "held" column shows "yes"

    # 3. resume
    r3 = runner.invoke(app, ["resume", str(t.id)], obj=ctx)
    assert r3.exit_code == 0
    assert not repo.get_ticket(t.id).is_held

    # 4. retry → enqueues
    r4 = runner.invoke(app, ["retry", str(t.id)], obj=ctx)
    assert r4.exit_code == 0
    assert qm.queue_depth() == 1

    # 5. queue reports the depth
    r5 = runner.invoke(app, ["queue"], obj=ctx)
    assert "1" in r5.output
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest packages/foreman/tests/v4/cli/test_phase6_e2e.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add packages/foreman/tests/v4/cli/test_phase6_e2e.py
git commit -m "test(v4): phase 6 e2e — hold/ps/resume/retry/queue operator chain"
```

### Phase 6 — `just check` gate

- [ ] **Run:** `just check`
- [ ] **Expected:** all green; isolation guard passes (new code under `foreman/v4/cli/`; `foreman/cli.py` only imports from the survival set + `foreman.v4.cli`).

Phase 6 completion criterion (from the outline): **full operator command set usable against an in-memory repository in tests**. Achieved at Task 6.7. The operator can list, inspect, mutate, and drive tickets entirely through typer commands. Phase 7 layers rich logging on top + sets MergeQueue as the default merge mechanism.

### Deferred polish (from Phase 6 reviews)

- **v4 CLI-wide migration to `Annotated[T, typer.Option(...)]` form.** Surfaced during Task 6.3 review. Modern typer's preferred shape is `log_path: Annotated[Path, typer.Option("--log-path")] = None` which preserves `Path` typing (better completion, type safety) while sidestepping ruff B008 (the default is a literal `None`, not a function call). Current v4 CLI files (ps.py, queue.py, log.py, mutations.py) use the legacy `param: Type = typer.Option(...)` shape; a one-off change would create inconsistency. Migrate all v4 CLI commands together in a single follow-up PR rather than per-file.

---
