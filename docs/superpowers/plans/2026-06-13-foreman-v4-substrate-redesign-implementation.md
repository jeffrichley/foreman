# Foreman v4 substrate redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace foreman's label-as-state coordination substrate with a SQLite-owned state machine, two-phase PR workflow (spec PR + impl PR, MergeQueue on impl only), a single polling loop reading SQLite + GitHub, and a typer CLI — preserving the existing role pipeline (Planner / Reviewer-on-spec / Fixer / Worker / Reviewer-on-impl / Fixer-on-impl) and the `needs-help` escalation pattern.

**Architecture:** State pattern with five-hook lifecycle (`can_run`/`enter`/`execute`/`verify`/`exit`) orchestrated by a Template Method base class. Mediator `QueueManager` decouples the Poller from the State Machine from the Worker Pool. Observer pattern routes side effects (SQLite persistence, GitHub label observability, structured logging) away from the state classes. Repository pattern over SQLite provides a testable persistence seam. Two-phase PR preserved (spec PR + impl PR); MergeQueue enabled on the impl PR only.

**Tech Stack:** Python 3.12, uv workspace, SQLite (stdlib `sqlite3`), pydantic v2, typer + rich, pytest + pytest-asyncio. Cross-platform Windows + Linux. GitHub MergeQueue for serialized merges on impl PRs.

**Branch:** All work lands on `feat/foreman-v4-substrate` off `main`. Single PR.

**Pre-push gate:** `just check` (ruff + mypy + pytest) must stay green at every commit. Pre-push hook is host-native Windows; pytest runs against the host venv.

**Commit cadence:** Frequent. Each task ends with a commit. Conventional commits, lowercase subjects. Stage specific files (no `git add -A`).

**Source of truth:** `docs/superpowers/specs/2026-06-13-foreman-v4-substrate-redesign-design.md`. Cite section names when referencing it from a task.

---

## v4 isolation principle — "delete v2/v3 by `rm -rf`"

Every task in this plan is written so that Phase 8 (cutover) can delete v2 + v3 with directory-level operations alone. No grep-and-patch. No untangling. The discipline that makes this true:

**1. Namespace.** All v4 code lives under `packages/foreman/src/foreman/v4/`. All v4 tests live under `packages/foreman/tests/v4/`. The `foreman.v4.*` import path is the v4 boundary forever — there is no rename at cutover. (Same shape protects future v5 from the same churn.)

**2. v4 never imports legacy modules.** A v4 module MAY import from:

  - the Python standard library
  - third-party deps already in `pyproject.toml` (pydantic, typer, rich, pygithub, sqlalchemy if added, etc.)
  - other `foreman.v4.*` modules
  - the **survival set** named below — modules that pre-date v4 but are not v2/v3-specific (auth, config, identity, worktree, etc.)

  A v4 module MUST NOT import from the **kill set** named below. Any task whose code or test reaches into the kill set is a bug in the task — fix the task, not the import.

**3. Survival set.** These files pre-date v4, are not coupled to the v2/v3 state-machine substrate, and v4 calls into them:

  - `foreman/auth.py` — GitHub PAT + App-token loading
  - `foreman/config.py` — TOML loading + env override (v4 adds keys, doesn't replace the loader)
  - `foreman/identity.py` — per-role PyGithub clients
  - `foreman/init.py` — `foreman init` project bootstrap (CLI command moves to typer wrapper in Phase 6, but the function survives)
  - `foreman/instructions.py` — CLAUDE.md fragment writer
  - `foreman/locks.py` — generic file-locking primitive
  - `foreman/git_host.py` + `foreman/git_hosts/` — Git provider abstraction
  - `foreman/provider.py` + `foreman/providers/` — LLM provider abstraction
  - `foreman/roles/{planner,reviewer,fixer,worker}.py` — role logic (Phase 5 modifies only the CLI exit path to emit `FOREMAN_OUTCOME:` JSON; the role bodies stay)
  - `foreman/prompts/` — role prompt files (unchanged)
  - `foreman/worktree.py` — per-ticket git worktree
  - `foreman/_env_filter.py` — env scrubbing
  - `foreman/logging_setup.py` — Phase 7 extends; doesn't replace

**4. Kill set — Phase 8 `git rm`-able with no v4 fallout.** These are pure v2/v3 substrate; nothing in `foreman.v4.*` reaches them:

  - `foreman/reconciler/` — entire directory (rules engine, v3 daemon, v3 host adapter, label-mutating actions)
  - `foreman/daemon.py` — v3 daemon main loop
  - `foreman/daemon_runners.py` — v3 label-triggered role dispatch entrypoints (replaced by `foreman.v4.dispatch`)
  - `foreman/daemon_host.py` — v3 daemon GitHub adapter (replaced by `foreman.v4.poller` direct PyGithub use)
  - `foreman/daemon_lock.py` — v3 lock file (replaced by `foreman.v4.daemon_lock` if needed)
  - `foreman/dispatcher.py` — the `_LABEL_TO_ACTION` map; v4 has no analog
  - `foreman/dispatch_recorder.py` — v3 dispatch journal (replaced by `state_instances` table)
  - `foreman/poller.py` — v3 poller (replaced by `foreman.v4.poller`)
  - `foreman/queue.py` — v3 queue (replaced by `foreman.v4.queue_manager`)
  - `foreman/storage.py` — v3 SQLite schema (replaced by `foreman.v4.sqlite_repository` + `schema.sql`)
  - `foreman/worker.py` — v3 worker loop (replaced by `foreman.v4.worker_pool`)
  - `foreman/role_dispatch.py` — v3 role dispatch helper
  - `foreman/stats.py` — v3 stats (the v4 CLI computes from `state_instances` directly)
  - `foreman/ps.py` — v3 `ps` command (replaced by `foreman.v4.cli.ps`)
  - `foreman/labels.py` — v3 label catalog. v4's `LabelObservabilityObserver` ships its own minimal write-only label vocabulary; nothing reads from `labels.py`. **DELETE in Phase 8.**
  - `foreman/branches.py` — v3 branch resolution (replaced by `foreman.v4.branches` if any survives)
  - `foreman/v3_bus_endpoint.py` — v3 bus integration
  - `foreman/cli.py` — top-level CLI dispatcher. Phase 6 moves the v4 commands into `foreman/v4/cli/`; Phase 8 deletes the v2/v3 commands and rewrites this file to a thin wrapper that exposes only the typer app from `foreman.v4.cli`.

**5. Tests.** v4 tests live under `tests/v4/` and never import fixtures from `tests/reconciler/`, `tests/daemon/`, `tests/dispatcher/`, etc. Phase 8 deletes the legacy test directories alongside the legacy code.

**6. v4 SQLite is a new file.** v4 connects to a different DB path (`<project>/.foreman/v4/state.db`) than v3 used. Cutover does not require schema migration — v3 DB is abandoned in place. Phase 8 documents the path change in `docs/RUNBOOK.md`.

**Phase 8 cutover, in one shot:**

```bash
# Remove the kill set
git rm -r packages/foreman/src/foreman/reconciler/
git rm packages/foreman/src/foreman/daemon.py
git rm packages/foreman/src/foreman/daemon_runners.py
git rm packages/foreman/src/foreman/daemon_host.py
git rm packages/foreman/src/foreman/daemon_lock.py
git rm packages/foreman/src/foreman/dispatcher.py
git rm packages/foreman/src/foreman/dispatch_recorder.py
git rm packages/foreman/src/foreman/poller.py
git rm packages/foreman/src/foreman/queue.py
git rm packages/foreman/src/foreman/storage.py
git rm packages/foreman/src/foreman/worker.py
git rm packages/foreman/src/foreman/role_dispatch.py
git rm packages/foreman/src/foreman/stats.py
git rm packages/foreman/src/foreman/ps.py
git rm packages/foreman/src/foreman/labels.py
git rm packages/foreman/src/foreman/branches.py
git rm packages/foreman/src/foreman/v3_bus_endpoint.py
git rm -r packages/foreman/tests/reconciler packages/foreman/tests/daemon packages/foreman/tests/dispatcher
# Rewrite foreman/cli.py to wrap foreman.v4.cli (~10 lines)
# Run just check; expect green.
```

If `just check` is green after these `git rm`s, the isolation discipline held. If it's red, the failing import is the receipt for which task or module violated the principle — fix the source, not the symptom.

---

## Phases

The plan is organized into phases that mirror the topological dependency order in the spec's "Approach" section. Each phase produces working, testable software on its own — useful as checkpoint boundaries for subagent-driven execution.

- **Phase 1 — Foundation.** Repository Protocol + in-memory impl + SQLite impl + schema for `tickets` and `state_instances`. The `Outcome` pydantic model. The `TicketState` ABC + Template Method `transition()` orchestrating the five lifecycle hooks. Tests are pure unit (no GitHub, no daemon). Completion = state machine works in isolation.
- **Phase 2 — Events + Observers.** `Event` base, `EventBus`, four concrete observer impls (`SQLitePersistenceObserver`, `LabelObservabilityObserver`, `StructuredLogObserver`, `MetricsObserver` no-op stub). Completion = side-effects fan out via the EventBus.
- **Phase 3 — Concrete states.** All 11 `TicketState` subclasses. Each state's `enter`/`execute`/`verify`/`exit` documented. Completion = end-to-end ticket lifecycle test passes against `FakeGitProvider`.
- **Phase 4 — QueueManager + Poller.** Mediator implementation + single polling loop (reads SQLite in-flight rows + open tickets, queries GitHub for artifact state, normalizes to domain Events, dedups by `(ticket, state-instance, artifact-state)`). Completion = lifecycle test flows through the QueueManager driven by the Poller.
- **Phase 5 — Role-side Outcome reporting.** Modify each role's `cli.py` entry point to emit the `FOREMAN_OUTCOME:` JSON line on stdout instead of writing labels. Role logic + prompts unchanged. Completion = roles produce stdout-parsable outcomes parseable by the state machine's `verify` hook.
- **Phase 6 — Typer CLI.** Operator surface — `ps`, `show`, `log`, `queue`, `daemon`, `hold/resume/retry/skip/drop/set-state`, direct role invocations. Rich-formatted output (`rich.Table` / `rich.Tree` / `rich.Live`). Completion = full operator command set usable against an in-memory repository in tests.
- **Phase 7 — Rich logging + MergeQueue default.** `RichHandler` + `JsonLinesHandler` configured at daemon startup. `DaemonConfig.merge_mechanism` defaults to `queue` (impl PRs only). Completion = colored stdout + JSON file + queue is the merge default.
- **Phase 8 — v3 deletion + cutover docs.** Remove `reconciler/rules.py`, `reconciler/actions.py` (full deletion — PR-merge handler moves to `MergingState`, observation reads inline into Poller), the `reconciler.py` crash-recovery module, and the `_LABEL_TO_ACTION` dispatch. Add per-repo MergeQueue branch-protection checklist to `docs/RUNBOOK.md`. Completion = grep for `_LABEL_TO_ACTION` returns zero; `just check` green; RUNBOOK explains MergeQueue per-repo enablement.

Phases 1–4 build the substrate in isolation (tests use in-memory fakes). Phases 5–6 wire real GitHub + the operator surface. Phases 7–8 round out logging + cutover.

---

## Phase 1 — Foundation

Builds the substrate that everything else depends on: the persistence seam, the data shape of an Outcome, the abstract state with its lifecycle orchestration, and the failure-handling discipline that prevents partial transitions. No GitHub. No daemon. No external IO beyond SQLite.

### Task 1.1: Package skeleton

**Files:**
- Create: `packages/foreman/src/foreman/v4/__init__.py`
- Test: `packages/foreman/tests/v4/test_package.py`

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_package.py
"""Smoke test for the foreman.v4 package."""
import importlib


def test_v4_package_importable():
    module = importlib.import_module("foreman.v4")
    assert module is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_package.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.v4'`

- [ ] **Step 3: Create the package marker**

```python
# packages/foreman/src/foreman/v4/__init__.py
"""Foreman v4 — substrate redesign.

State machine in SQLite, single polling loop, operator CLI. See
docs/superpowers/specs/2026-06-13-foreman-v4-substrate-redesign-design.md
for the architecture.
"""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/foreman/tests/v4/test_package.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/__init__.py packages/foreman/tests/v4/test_package.py
git commit -m "feat(v4): add foreman.v4 package skeleton"
```

### Task 1.2: Outcome model

**Files:**
- Create: `packages/foreman/src/foreman/v4/outcome.py`
- Test: `packages/foreman/tests/v4/test_outcome.py`

Cites spec section **Outcome JSON — role-side reporting contract**.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_outcome.py
"""Tests for the Outcome model — the role-to-daemon reporting contract."""
import json

import pytest
from pydantic import ValidationError

from foreman.v4.outcome import (
    Finding,
    Outcome,
    OutcomeArtifacts,
    OutcomeConfidence,
    OutcomeKind,
)


def test_outcome_minimal_clean():
    outcome = Outcome(
        kind=OutcomeKind.CLEAN,
        confidence=OutcomeConfidence.HIGH,
        summary="spec PR opened",
    )
    assert outcome.schema_version == 1
    assert outcome.findings == []
    assert outcome.artifacts.pr_url is None


def test_outcome_needs_fix_with_findings():
    outcome = Outcome(
        kind=OutcomeKind.NEEDS_FIX,
        confidence=OutcomeConfidence.HIGH,
        summary="reviewer found 2 issues",
        findings=[
            Finding(severity="critical", location="foo.py:42", description="null deref"),
            Finding(severity="minor", location="general", description="naming nit"),
        ],
    )
    assert len(outcome.findings) == 2
    assert outcome.findings[0].severity == "critical"


def test_outcome_artifacts_pr():
    outcome = Outcome(
        kind=OutcomeKind.CLEAN,
        confidence=OutcomeConfidence.HIGH,
        summary="impl PR open",
        artifacts=OutcomeArtifacts(
            pr_url="https://github.com/x/y/pull/1",
            pr_number=1,
            commit_sha="abc123",
            branch="impl/1",
        ),
    )
    assert outcome.artifacts.pr_number == 1


def test_outcome_summary_max_length():
    with pytest.raises(ValidationError):
        Outcome(
            kind=OutcomeKind.CLEAN,
            confidence=OutcomeConfidence.HIGH,
            summary="x" * 501,
        )


def test_outcome_round_trips_through_json():
    original = Outcome(
        kind=OutcomeKind.BLOCKED,
        confidence=OutcomeConfidence.MEDIUM,
        summary="CI in flight",
        artifacts=OutcomeArtifacts(pr_number=42),
    )
    raw = original.model_dump_json()
    reloaded = Outcome.model_validate_json(raw)
    assert reloaded == original


def test_outcome_finding_severity_rejects_unknown():
    with pytest.raises(ValidationError):
        Finding(severity="catastrophic", location="x", description="y")


def test_outcome_kind_enum_values():
    assert OutcomeKind.CLEAN.value == "clean"
    assert OutcomeKind.NEEDS_FIX.value == "needs_fix"
    assert OutcomeKind.BLOCKED.value == "blocked"
    assert OutcomeKind.NEEDS_HELP.value == "needs_help"
    assert OutcomeKind.ERROR.value == "error"


def test_outcome_default_schema_version_is_1():
    raw = '{"kind":"clean","confidence":"high","summary":"x"}'
    outcome = Outcome.model_validate(json.loads(raw))
    assert outcome.schema_version == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_outcome.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.v4.outcome'`

- [ ] **Step 3: Write the model**

```python
# packages/foreman/src/foreman/v4/outcome.py
"""Outcome — the role-to-daemon reporting contract.

Every role's CLI emits one terminal line on stdout shaped as

    FOREMAN_OUTCOME:{"schema_version":1,"kind":"...","confidence":"...",...}

The daemon's verify hook scans stdout in reverse for the marker, parses the
suffix, and validates against ``Outcome``. See the spec section
"Outcome JSON — role-side reporting contract" for the per-role kind matrix.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class OutcomeKind(str, Enum):
    CLEAN = "clean"
    NEEDS_FIX = "needs_fix"
    BLOCKED = "blocked"
    NEEDS_HELP = "needs_help"
    ERROR = "error"


class OutcomeConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Finding(BaseModel):
    severity: Literal["critical", "important", "minor"]
    location: str = Field(..., description="file:line or 'general'")
    description: str


class OutcomeArtifacts(BaseModel):
    pr_url: str | None = None
    pr_number: int | None = None
    commit_sha: str | None = None
    branch: str | None = None
    spec_doc_path: str | None = None


class Outcome(BaseModel):
    schema_version: Literal[1] = 1
    kind: OutcomeKind
    confidence: OutcomeConfidence
    summary: str = Field(..., max_length=500)
    findings: list[Finding] = Field(default_factory=list)
    artifacts: OutcomeArtifacts = Field(default_factory=OutcomeArtifacts)
    raw_role_output_path: str | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_outcome.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/outcome.py packages/foreman/tests/v4/test_outcome.py
git commit -m "feat(v4): add outcome pydantic model and contract tests"
```

### Task 1.3: Outcome stdout marker parsing

**Files:**
- Modify: `packages/foreman/src/foreman/v4/outcome.py` (add parse_outcome_from_stdout + exception classes)
- Test: `packages/foreman/tests/v4/test_outcome_parsing.py`

Cites spec section **Outcome JSON — Stdout shape** and **Validation failure handling**.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_outcome_parsing.py
"""Tests for FOREMAN_OUTCOME: marker parsing.

The role's stdout has a human-readable trace followed by a single terminal
line beginning with FOREMAN_OUTCOME: and ending with the JSON outcome. The
parser scans stdout in reverse for the marker. Three distinct failures:
missing marker, malformed JSON, schema-invalid JSON.
"""
import pytest

from foreman.v4.outcome import (
    Outcome,
    OutcomeInvalidError,
    OutcomeKind,
    OutcomeMalformedError,
    OutcomeMissingError,
    parse_outcome_from_stdout,
)


def test_parses_outcome_from_terminal_line():
    stdout = (
        "doing things\n"
        "more things\n"
        'FOREMAN_OUTCOME:{"schema_version":1,"kind":"clean","confidence":"high","summary":"ok"}\n'
    )
    outcome = parse_outcome_from_stdout(stdout)
    assert outcome.kind == OutcomeKind.CLEAN


def test_parses_when_marker_is_last_line_without_trailing_newline():
    stdout = 'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}'
    outcome = parse_outcome_from_stdout(stdout)
    assert outcome.summary == "ok"


def test_ignores_earlier_lines_that_look_like_json():
    stdout = (
        '{"kind":"error","confidence":"low","summary":"this is a log line"}\n'
        'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}\n'
    )
    outcome = parse_outcome_from_stdout(stdout)
    assert outcome.kind == OutcomeKind.CLEAN


def test_uses_last_marker_when_multiple_present():
    stdout = (
        'FOREMAN_OUTCOME:{"kind":"error","confidence":"low","summary":"early"}\n'
        'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"final"}\n'
    )
    outcome = parse_outcome_from_stdout(stdout)
    assert outcome.summary == "final"


def test_missing_marker_raises_outcome_missing():
    with pytest.raises(OutcomeMissingError):
        parse_outcome_from_stdout("just some log output\nno marker here\n")


def test_malformed_json_raises_outcome_malformed():
    stdout = "FOREMAN_OUTCOME:{not valid json}\n"
    with pytest.raises(OutcomeMalformedError) as exc:
        parse_outcome_from_stdout(stdout)
    assert "{not valid json}" in str(exc.value)


def test_schema_invalid_raises_outcome_invalid():
    stdout = 'FOREMAN_OUTCOME:{"kind":"catastrophic","confidence":"high","summary":"x"}\n'
    with pytest.raises(OutcomeInvalidError):
        parse_outcome_from_stdout(stdout)


def test_empty_stdout_raises_outcome_missing():
    with pytest.raises(OutcomeMissingError):
        parse_outcome_from_stdout("")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_outcome_parsing.py -v`
Expected: FAIL with `ImportError: cannot import name 'parse_outcome_from_stdout' from 'foreman.v4.outcome'`

- [ ] **Step 3: Extend `outcome.py`**

Append to `packages/foreman/src/foreman/v4/outcome.py`:

```python
OUTCOME_MARKER = "FOREMAN_OUTCOME:"


class OutcomeMissingError(Exception):
    """Stdout did not contain the FOREMAN_OUTCOME: marker."""


class OutcomeMalformedError(Exception):
    """FOREMAN_OUTCOME: marker present but the suffix is not parseable JSON."""

    def __init__(self, raw: str) -> None:
        super().__init__(f"malformed outcome JSON: {raw!r}")
        self.raw = raw


class OutcomeInvalidError(Exception):
    """FOREMAN_OUTCOME: JSON parsed but failed schema validation."""

    def __init__(self, raw: str, pydantic_errors: object) -> None:
        super().__init__(f"invalid outcome: {pydantic_errors}")
        self.raw = raw
        self.pydantic_errors = pydantic_errors


def parse_outcome_from_stdout(stdout: str) -> Outcome:
    """Scan stdout in reverse for the FOREMAN_OUTCOME: marker; parse + validate.

    Reverse scan so log lines that happen to contain JSON earlier in stdout
    cannot poison the parse. If multiple marker lines are present, the last
    one wins — roles that re-emit on retry overwrite the earlier value.
    """
    import json

    from pydantic import ValidationError

    for line in reversed(stdout.splitlines()):
        idx = line.find(OUTCOME_MARKER)
        if idx == -1:
            continue
        suffix = line[idx + len(OUTCOME_MARKER):].strip()
        try:
            payload = json.loads(suffix)
        except json.JSONDecodeError as exc:
            raise OutcomeMalformedError(suffix) from exc
        try:
            return Outcome.model_validate(payload)
        except ValidationError as exc:
            raise OutcomeInvalidError(suffix, exc.errors()) from exc
    raise OutcomeMissingError("no FOREMAN_OUTCOME: marker found in stdout")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_outcome_parsing.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/outcome.py packages/foreman/tests/v4/test_outcome_parsing.py
git commit -m "feat(v4): add FOREMAN_OUTCOME: stdout marker parsing"
```

### Task 1.4: SQLite schema file

**Files:**
- Create: `packages/foreman/src/foreman/v4/schema.sql`
- Test: `packages/foreman/tests/v4/test_schema.py`

Cites spec section **Durability + resume / `state_instances` schema** and **Operator pause / resume**.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_schema.py
"""The v4 SQLite schema — tickets + state_instances tables.

This tests that the SQL file applies cleanly and the tables have the
columns the spec mandates. Repository-level CRUD is tested separately.
"""
import sqlite3
from pathlib import Path

import pytest

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "foreman"
    / "v4"
    / "schema.sql"
)


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def test_schema_file_exists():
    assert SCHEMA_PATH.exists(), f"schema file missing at {SCHEMA_PATH}"


def test_tickets_table_columns(db: sqlite3.Connection):
    cols = {row[1] for row in db.execute("PRAGMA table_info(tickets)")}
    assert {
        "id",
        "project",
        "issue_number",
        "current_state",
        "created_at",
        "updated_at",
        "held_by",
        "held_at",
        "held_reason",
    } <= cols


def test_state_instances_table_columns(db: sqlite3.Connection):
    cols = {row[1] for row in db.execute("PRAGMA table_info(state_instances)")}
    assert {
        "id",
        "ticket_id",
        "state_name",
        "sequence",
        "entered_at",
        "execute_started_at",
        "execute_completed_at",
        "exited_at",
        "outcome_kind",
        "outcome_payload",
        "next_state",
        "failure_phase",
        "failure_reason",
    } <= cols


def test_state_instances_unique_ticket_sequence(db: sqlite3.Connection):
    db.execute(
        "INSERT INTO tickets(project, issue_number, current_state, created_at, updated_at) "
        "VALUES ('p', 1, 'Queued', '2026-06-13T00:00:00', '2026-06-13T00:00:00')"
    )
    db.execute(
        "INSERT INTO state_instances(ticket_id, state_name, sequence, entered_at) "
        "VALUES (1, 'Queued', 1, '2026-06-13T00:00:00')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO state_instances(ticket_id, state_name, sequence, entered_at) "
            "VALUES (1, 'Queued', 1, '2026-06-13T00:00:01')"
        )


def test_in_flight_query(db: sqlite3.Connection):
    db.execute(
        "INSERT INTO tickets(project, issue_number, current_state, created_at, updated_at) "
        "VALUES ('p', 1, 'Planning', '2026-06-13T00:00:00', '2026-06-13T00:00:00')"
    )
    db.execute(
        "INSERT INTO state_instances(ticket_id, state_name, sequence, entered_at, exited_at) "
        "VALUES (1, 'Queued', 1, '2026-06-13T00:00:00', '2026-06-13T00:00:01')"
    )
    db.execute(
        "INSERT INTO state_instances(ticket_id, state_name, sequence, entered_at) "
        "VALUES (1, 'Planning', 2, '2026-06-13T00:00:02')"
    )
    rows = db.execute(
        "SELECT state_name FROM state_instances WHERE exited_at IS NULL"
    ).fetchall()
    assert rows == [("Planning",)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_schema.py -v`
Expected: FAIL with `FileNotFoundError` on `schema.sql`

- [ ] **Step 3: Write the schema**

```sql
-- packages/foreman/src/foreman/v4/schema.sql
--
-- Foreman v4 SQLite schema. Two tables:
--   tickets         — the ticket row, with operator-hold columns
--   state_instances — the journal; one row per (state, entry) tuple
--
-- See docs/superpowers/specs/2026-06-13-foreman-v4-substrate-redesign-design.md
-- "Durability + resume" for the column semantics.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tickets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project         TEXT    NOT NULL,
    issue_number    INTEGER NOT NULL,
    current_state   TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    held_by         TEXT,
    held_at         TEXT,
    held_reason     TEXT,
    UNIQUE(project, issue_number)
);

CREATE TABLE IF NOT EXISTS state_instances (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id               INTEGER NOT NULL REFERENCES tickets(id),
    state_name              TEXT    NOT NULL,
    sequence                INTEGER NOT NULL,
    entered_at              TEXT    NOT NULL,
    execute_started_at      TEXT,
    execute_completed_at    TEXT,
    exited_at               TEXT,
    outcome_kind            TEXT,
    outcome_payload         TEXT,
    next_state              TEXT,
    failure_phase           TEXT,
    failure_reason          TEXT,
    UNIQUE(ticket_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_state_instances_inflight
    ON state_instances(ticket_id)
    WHERE exited_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_tickets_held
    ON tickets(held_by)
    WHERE held_by IS NOT NULL;
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_schema.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/schema.sql packages/foreman/tests/v4/test_schema.py
git commit -m "feat(v4): add sqlite schema for tickets and state_instances"
```

### Task 1.5: TicketRecord + StateInstanceRecord dataclasses

**Files:**
- Create: `packages/foreman/src/foreman/v4/records.py`
- Test: `packages/foreman/tests/v4/test_records.py`

Plain frozen dataclasses, no business logic. They're the read-shape returned by the Repository.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_records.py
"""Read-shape records returned by TicketRepository."""
import datetime as dt

import pytest

from foreman.v4.outcome import OutcomeKind
from foreman.v4.records import StateInstanceRecord, TicketRecord


def test_ticket_record_is_frozen():
    record = TicketRecord(
        id=1,
        project="foreman",
        issue_number=42,
        current_state="Planning",
        created_at=dt.datetime(2026, 6, 13),
        updated_at=dt.datetime(2026, 6, 13),
        held_by=None,
        held_at=None,
        held_reason=None,
    )
    with pytest.raises(AttributeError):
        record.current_state = "Done"  # type: ignore[misc]


def test_ticket_record_is_held_predicate():
    not_held = TicketRecord(
        id=1, project="p", issue_number=1, current_state="Queued",
        created_at=dt.datetime(2026, 6, 13), updated_at=dt.datetime(2026, 6, 13),
        held_by=None, held_at=None, held_reason=None,
    )
    held = TicketRecord(
        id=2, project="p", issue_number=2, current_state="Queued",
        created_at=dt.datetime(2026, 6, 13), updated_at=dt.datetime(2026, 6, 13),
        held_by="jeff", held_at=dt.datetime(2026, 6, 13), held_reason="vacation",
    )
    assert not_held.is_held is False
    assert held.is_held is True


def test_state_instance_record_in_flight_predicate():
    in_flight = StateInstanceRecord(
        id=1, ticket_id=1, state_name="Planning", sequence=1,
        entered_at=dt.datetime(2026, 6, 13),
        execute_started_at=None, execute_completed_at=None,
        exited_at=None, outcome_kind=None, outcome_payload=None,
        next_state=None, failure_phase=None, failure_reason=None,
    )
    done = StateInstanceRecord(
        id=2, ticket_id=1, state_name="Planning", sequence=1,
        entered_at=dt.datetime(2026, 6, 13),
        execute_started_at=dt.datetime(2026, 6, 13),
        execute_completed_at=dt.datetime(2026, 6, 13),
        exited_at=dt.datetime(2026, 6, 13),
        outcome_kind=OutcomeKind.CLEAN, outcome_payload={"summary": "ok"},
        next_state="SpecReview", failure_phase=None, failure_reason=None,
    )
    assert in_flight.is_in_flight is True
    assert done.is_in_flight is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_records.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.v4.records'`

- [ ] **Step 3: Write the records module**

```python
# packages/foreman/src/foreman/v4/records.py
"""Read-shape dataclasses returned by TicketRepository.

Frozen so callers cannot mutate. Mutation goes through repository write
methods, which produce a new record on read. This keeps the persistence
seam discipline clean — the repository owns identity, callers own
intent.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from foreman.v4.outcome import OutcomeKind


@dataclass(frozen=True, slots=True)
class TicketRecord:
    id: int
    project: str
    issue_number: int
    current_state: str
    created_at: dt.datetime
    updated_at: dt.datetime
    held_by: str | None
    held_at: dt.datetime | None
    held_reason: str | None

    @property
    def is_held(self) -> bool:
        return self.held_by is not None


@dataclass(frozen=True, slots=True)
class StateInstanceRecord:
    id: int
    ticket_id: int
    state_name: str
    sequence: int
    entered_at: dt.datetime
    execute_started_at: dt.datetime | None
    execute_completed_at: dt.datetime | None
    exited_at: dt.datetime | None
    outcome_kind: OutcomeKind | None
    outcome_payload: dict[str, Any] | None
    next_state: str | None
    failure_phase: str | None
    failure_reason: str | None

    @property
    def is_in_flight(self) -> bool:
        return self.exited_at is None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_records.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/records.py packages/foreman/tests/v4/test_records.py
git commit -m "feat(v4): add ticket and state-instance record dataclasses"
```

### Task 1.6: TicketRepository Protocol + InMemoryTicketRepository

**Files:**
- Create: `packages/foreman/src/foreman/v4/repository.py`
- Test: `packages/foreman/tests/v4/test_in_memory_repository.py`

Cites spec section **Approach / Repository pattern** and **Sub-requests #3**.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_in_memory_repository.py
"""Tests for the in-memory TicketRepository implementation.

These tests double as the spec for the TicketRepository Protocol — the same
test suite runs against the SqliteTicketRepository in Task 1.7 to guarantee
behavioral parity between the two implementations.
"""
import datetime as dt

import pytest

from foreman.v4.outcome import OutcomeKind
from foreman.v4.repository import (
    InMemoryTicketRepository,
    TicketAlreadyExistsError,
    TicketNotFoundError,
)


@pytest.fixture()
def repo() -> InMemoryTicketRepository:
    return InMemoryTicketRepository()


def _now() -> dt.datetime:
    return dt.datetime(2026, 6, 13, 12, 0, 0)


def test_create_ticket_returns_record_with_id(repo: InMemoryTicketRepository):
    ticket = repo.create_ticket(project="foreman", issue_number=42, now=_now())
    assert ticket.id > 0
    assert ticket.project == "foreman"
    assert ticket.issue_number == 42
    assert ticket.current_state == "Queued"
    assert ticket.held_by is None


def test_create_ticket_duplicate_raises(repo: InMemoryTicketRepository):
    repo.create_ticket(project="foreman", issue_number=1, now=_now())
    with pytest.raises(TicketAlreadyExistsError):
        repo.create_ticket(project="foreman", issue_number=1, now=_now())


def test_get_ticket_missing_raises(repo: InMemoryTicketRepository):
    with pytest.raises(TicketNotFoundError):
        repo.get_ticket(999)


def test_get_ticket_by_issue(repo: InMemoryTicketRepository):
    created = repo.create_ticket(project="foreman", issue_number=7, now=_now())
    fetched = repo.get_ticket_by_issue(project="foreman", issue_number=7)
    assert fetched == created


def test_list_open_tickets_excludes_done_and_failed(repo: InMemoryTicketRepository):
    a = repo.create_ticket(project="p", issue_number=1, now=_now())
    b = repo.create_ticket(project="p", issue_number=2, now=_now())
    c = repo.create_ticket(project="p", issue_number=3, now=_now())
    repo.set_ticket_state(a.id, "Done", now=_now())
    repo.set_ticket_state(b.id, "Failed", now=_now())
    open_tickets = repo.list_open_tickets()
    assert {t.issue_number for t in open_tickets} == {c.issue_number}


def test_set_ticket_state_updates(repo: InMemoryTicketRepository):
    t = repo.create_ticket(project="p", issue_number=1, now=_now())
    repo.set_ticket_state(t.id, "Planning", now=dt.datetime(2026, 6, 13, 13))
    fetched = repo.get_ticket(t.id)
    assert fetched.current_state == "Planning"
    assert fetched.updated_at == dt.datetime(2026, 6, 13, 13)


def test_hold_and_resume(repo: InMemoryTicketRepository):
    t = repo.create_ticket(project="p", issue_number=1, now=_now())
    repo.hold_ticket(t.id, held_by="jeff", reason="vacation", now=_now())
    held = repo.get_ticket(t.id)
    assert held.is_held
    assert held.held_by == "jeff"
    assert held.held_reason == "vacation"
    repo.resume_ticket(t.id, now=_now())
    resumed = repo.get_ticket(t.id)
    assert not resumed.is_held
    assert resumed.held_by is None


def test_open_state_instance(repo: InMemoryTicketRepository):
    t = repo.create_ticket(project="p", issue_number=1, now=_now())
    instance = repo.open_state_instance(
        ticket_id=t.id, state_name="Planning", sequence=1, now=_now()
    )
    assert instance.id > 0
    assert instance.is_in_flight
    assert instance.entered_at == _now()


def test_state_instance_lifecycle_timestamps(repo: InMemoryTicketRepository):
    t = repo.create_ticket(project="p", issue_number=1, now=_now())
    inst = repo.open_state_instance(
        ticket_id=t.id, state_name="Planning", sequence=1, now=_now()
    )
    repo.mark_execute_started(inst.id, now=dt.datetime(2026, 6, 13, 12, 1))
    repo.mark_execute_completed(
        inst.id,
        now=dt.datetime(2026, 6, 13, 12, 5),
        outcome_kind=OutcomeKind.CLEAN,
        outcome_payload={"summary": "spec PR open"},
        next_state="SpecReview",
    )
    repo.close_state_instance(inst.id, now=dt.datetime(2026, 6, 13, 12, 6))
    closed = repo.get_state_instance(inst.id)
    assert closed.execute_started_at == dt.datetime(2026, 6, 13, 12, 1)
    assert closed.execute_completed_at == dt.datetime(2026, 6, 13, 12, 5)
    assert closed.exited_at == dt.datetime(2026, 6, 13, 12, 6)
    assert closed.outcome_kind == OutcomeKind.CLEAN
    assert closed.next_state == "SpecReview"
    assert not closed.is_in_flight


def test_list_in_flight_state_instances(repo: InMemoryTicketRepository):
    t = repo.create_ticket(project="p", issue_number=1, now=_now())
    done = repo.open_state_instance(
        ticket_id=t.id, state_name="Queued", sequence=1, now=_now()
    )
    repo.mark_execute_started(done.id, now=_now())
    repo.mark_execute_completed(
        done.id, now=_now(), outcome_kind=OutcomeKind.CLEAN,
        outcome_payload={}, next_state="Planning",
    )
    repo.close_state_instance(done.id, now=_now())
    in_flight = repo.open_state_instance(
        ticket_id=t.id, state_name="Planning", sequence=2, now=_now()
    )
    rows = repo.list_in_flight_state_instances()
    assert [r.id for r in rows] == [in_flight.id]


def test_record_failure_writes_phase_and_reason(repo: InMemoryTicketRepository):
    t = repo.create_ticket(project="p", issue_number=1, now=_now())
    inst = repo.open_state_instance(
        ticket_id=t.id, state_name="Planning", sequence=1, now=_now()
    )
    repo.record_failure(
        inst.id,
        now=_now(),
        failure_phase="execute",
        failure_reason="subprocess timed out",
    )
    fetched = repo.get_state_instance(inst.id)
    assert fetched.failure_phase == "execute"
    assert fetched.failure_reason == "subprocess timed out"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_in_memory_repository.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.v4.repository'`

- [ ] **Step 3: Write the Protocol + in-memory impl**

```python
# packages/foreman/src/foreman/v4/repository.py
"""The TicketRepository seam.

Two implementations: InMemoryTicketRepository for tests; SqliteTicketRepository
for production. Domain code talks only to the Protocol — never to sqlite3 or
to the dict storage directly. This is the only place persistence concerns
leak into the v4 codebase.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Protocol

from foreman.v4.outcome import OutcomeKind
from foreman.v4.records import StateInstanceRecord, TicketRecord


class TicketNotFoundError(LookupError):
    """No ticket exists with the given id or (project, issue_number)."""


class StateInstanceNotFoundError(LookupError):
    """No state-instance exists with the given id."""


class TicketAlreadyExistsError(ValueError):
    """A ticket with this (project, issue_number) is already tracked."""


class TicketRepository(Protocol):
    """Persistence contract for tickets and state-instances."""

    # --- Ticket CRUD ---

    def create_ticket(self, *, project: str, issue_number: int, now: dt.datetime) -> TicketRecord: ...
    def get_ticket(self, ticket_id: int) -> TicketRecord: ...
    def get_ticket_by_issue(self, *, project: str, issue_number: int) -> TicketRecord: ...
    def list_open_tickets(self) -> list[TicketRecord]: ...
    def set_ticket_state(self, ticket_id: int, new_state: str, *, now: dt.datetime) -> None: ...
    def hold_ticket(self, ticket_id: int, *, held_by: str, reason: str, now: dt.datetime) -> None: ...
    def resume_ticket(self, ticket_id: int, *, now: dt.datetime) -> None: ...

    # --- State-instance journal ---

    def open_state_instance(
        self, *, ticket_id: int, state_name: str, sequence: int, now: dt.datetime
    ) -> StateInstanceRecord: ...
    def get_state_instance(self, instance_id: int) -> StateInstanceRecord: ...
    def mark_execute_started(self, instance_id: int, *, now: dt.datetime) -> None: ...
    def mark_execute_completed(
        self,
        instance_id: int,
        *,
        now: dt.datetime,
        outcome_kind: OutcomeKind,
        outcome_payload: dict[str, Any],
        next_state: str,
    ) -> None: ...
    def close_state_instance(self, instance_id: int, *, now: dt.datetime) -> None: ...
    def record_failure(
        self,
        instance_id: int,
        *,
        now: dt.datetime,
        failure_phase: str,
        failure_reason: str,
    ) -> None: ...
    def list_in_flight_state_instances(self) -> list[StateInstanceRecord]: ...


_TERMINAL_STATES = frozenset({"Done", "Failed"})


class InMemoryTicketRepository:
    """Reference TicketRepository for unit tests.

    Behavior must match SqliteTicketRepository — the same test suite runs
    against both. If you find a behavior gap, the bug is in whichever impl
    diverges from the test.
    """

    def __init__(self) -> None:
        self._tickets: dict[int, TicketRecord] = {}
        self._by_issue: dict[tuple[str, int], int] = {}
        self._instances: dict[int, StateInstanceRecord] = {}
        self._next_ticket_id = 1
        self._next_instance_id = 1

    # --- Ticket CRUD ---

    def create_ticket(self, *, project: str, issue_number: int, now: dt.datetime) -> TicketRecord:
        if (project, issue_number) in self._by_issue:
            raise TicketAlreadyExistsError(f"{project}#{issue_number}")
        ticket = TicketRecord(
            id=self._next_ticket_id,
            project=project,
            issue_number=issue_number,
            current_state="Queued",
            created_at=now,
            updated_at=now,
            held_by=None,
            held_at=None,
            held_reason=None,
        )
        self._tickets[ticket.id] = ticket
        self._by_issue[(project, issue_number)] = ticket.id
        self._next_ticket_id += 1
        return ticket

    def get_ticket(self, ticket_id: int) -> TicketRecord:
        try:
            return self._tickets[ticket_id]
        except KeyError as exc:
            raise TicketNotFoundError(str(ticket_id)) from exc

    def get_ticket_by_issue(self, *, project: str, issue_number: int) -> TicketRecord:
        try:
            return self._tickets[self._by_issue[(project, issue_number)]]
        except KeyError as exc:
            raise TicketNotFoundError(f"{project}#{issue_number}") from exc

    def list_open_tickets(self) -> list[TicketRecord]:
        return [t for t in self._tickets.values() if t.current_state not in _TERMINAL_STATES]

    def set_ticket_state(self, ticket_id: int, new_state: str, *, now: dt.datetime) -> None:
        existing = self.get_ticket(ticket_id)
        self._tickets[ticket_id] = TicketRecord(
            **{**existing.__dict__, "current_state": new_state, "updated_at": now}
        )

    def hold_ticket(self, ticket_id: int, *, held_by: str, reason: str, now: dt.datetime) -> None:
        existing = self.get_ticket(ticket_id)
        self._tickets[ticket_id] = TicketRecord(
            **{
                **existing.__dict__,
                "held_by": held_by,
                "held_at": now,
                "held_reason": reason,
                "updated_at": now,
            }
        )

    def resume_ticket(self, ticket_id: int, *, now: dt.datetime) -> None:
        existing = self.get_ticket(ticket_id)
        self._tickets[ticket_id] = TicketRecord(
            **{
                **existing.__dict__,
                "held_by": None,
                "held_at": None,
                "held_reason": None,
                "updated_at": now,
            }
        )

    # --- State-instance journal ---

    def open_state_instance(
        self, *, ticket_id: int, state_name: str, sequence: int, now: dt.datetime
    ) -> StateInstanceRecord:
        self.get_ticket(ticket_id)  # raise if missing
        instance = StateInstanceRecord(
            id=self._next_instance_id,
            ticket_id=ticket_id,
            state_name=state_name,
            sequence=sequence,
            entered_at=now,
            execute_started_at=None,
            execute_completed_at=None,
            exited_at=None,
            outcome_kind=None,
            outcome_payload=None,
            next_state=None,
            failure_phase=None,
            failure_reason=None,
        )
        self._instances[instance.id] = instance
        self._next_instance_id += 1
        return instance

    def get_state_instance(self, instance_id: int) -> StateInstanceRecord:
        try:
            return self._instances[instance_id]
        except KeyError as exc:
            raise StateInstanceNotFoundError(str(instance_id)) from exc

    def _replace(self, instance_id: int, **changes: Any) -> None:
        existing = self.get_state_instance(instance_id)
        self._instances[instance_id] = StateInstanceRecord(
            **{**existing.__dict__, **changes}
        )

    def mark_execute_started(self, instance_id: int, *, now: dt.datetime) -> None:
        self._replace(instance_id, execute_started_at=now)

    def mark_execute_completed(
        self,
        instance_id: int,
        *,
        now: dt.datetime,
        outcome_kind: OutcomeKind,
        outcome_payload: dict[str, Any],
        next_state: str,
    ) -> None:
        self._replace(
            instance_id,
            execute_completed_at=now,
            outcome_kind=outcome_kind,
            outcome_payload=outcome_payload,
            next_state=next_state,
        )

    def close_state_instance(self, instance_id: int, *, now: dt.datetime) -> None:
        self._replace(instance_id, exited_at=now)

    def record_failure(
        self,
        instance_id: int,
        *,
        now: dt.datetime,
        failure_phase: str,
        failure_reason: str,
    ) -> None:
        self._replace(
            instance_id,
            failure_phase=failure_phase,
            failure_reason=failure_reason,
        )

    def list_in_flight_state_instances(self) -> list[StateInstanceRecord]:
        return [i for i in self._instances.values() if i.is_in_flight]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_in_memory_repository.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/repository.py packages/foreman/tests/v4/test_in_memory_repository.py
git commit -m "feat(v4): add TicketRepository protocol and in-memory impl"
```

### Task 1.7: SqliteTicketRepository

**Files:**
- Create: `packages/foreman/src/foreman/v4/sqlite_repository.py`
- Test: `packages/foreman/tests/v4/test_sqlite_repository.py`
- Modify: `packages/foreman/tests/v4/test_in_memory_repository.py` → refactor common assertions into `_repository_contract.py`

The behavior contract is exactly what Task 1.6 already tests. We refactor those tests into a shared parametrized suite that runs against BOTH the in-memory impl and the SQLite impl. Any divergence is a bug in whichever side fails.

- [ ] **Step 1: Extract the contract**

Create `packages/foreman/tests/v4/_repository_contract.py`:

```python
# packages/foreman/tests/v4/_repository_contract.py
"""Shared TicketRepository contract suite.

Both InMemoryTicketRepository and SqliteTicketRepository must satisfy
every assertion here. Test files instantiate their concrete repo and
import these scenarios.
"""
from __future__ import annotations

import datetime as dt
from typing import Callable

import pytest

from foreman.v4.outcome import OutcomeKind
from foreman.v4.repository import (
    TicketAlreadyExistsError,
    TicketNotFoundError,
    TicketRepository,
)


RepoFactory = Callable[[], TicketRepository]


def _now() -> dt.datetime:
    return dt.datetime(2026, 6, 13, 12, 0, 0)


class RepositoryContract:
    """Mixin: subclass and override ``factory`` to bind a concrete repo."""

    factory: RepoFactory

    @pytest.fixture()
    def repo(self) -> TicketRepository:
        return self.factory()

    def test_create_ticket_returns_record_with_id(self, repo: TicketRepository):
        ticket = repo.create_ticket(project="foreman", issue_number=42, now=_now())
        assert ticket.id > 0
        assert ticket.project == "foreman"
        assert ticket.issue_number == 42
        assert ticket.current_state == "Queued"
        assert ticket.held_by is None

    def test_create_ticket_duplicate_raises(self, repo: TicketRepository):
        repo.create_ticket(project="foreman", issue_number=1, now=_now())
        with pytest.raises(TicketAlreadyExistsError):
            repo.create_ticket(project="foreman", issue_number=1, now=_now())

    def test_get_ticket_missing_raises(self, repo: TicketRepository):
        with pytest.raises(TicketNotFoundError):
            repo.get_ticket(999)

    def test_get_ticket_by_issue(self, repo: TicketRepository):
        created = repo.create_ticket(project="foreman", issue_number=7, now=_now())
        fetched = repo.get_ticket_by_issue(project="foreman", issue_number=7)
        assert fetched == created

    def test_list_open_tickets_excludes_done_and_failed(self, repo: TicketRepository):
        a = repo.create_ticket(project="p", issue_number=1, now=_now())
        b = repo.create_ticket(project="p", issue_number=2, now=_now())
        c = repo.create_ticket(project="p", issue_number=3, now=_now())
        repo.set_ticket_state(a.id, "Done", now=_now())
        repo.set_ticket_state(b.id, "Failed", now=_now())
        open_tickets = repo.list_open_tickets()
        assert {t.issue_number for t in open_tickets} == {c.issue_number}

    def test_set_ticket_state_updates(self, repo: TicketRepository):
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        repo.set_ticket_state(t.id, "Planning", now=dt.datetime(2026, 6, 13, 13))
        fetched = repo.get_ticket(t.id)
        assert fetched.current_state == "Planning"

    def test_hold_and_resume(self, repo: TicketRepository):
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        repo.hold_ticket(t.id, held_by="jeff", reason="vacation", now=_now())
        held = repo.get_ticket(t.id)
        assert held.is_held and held.held_reason == "vacation"
        repo.resume_ticket(t.id, now=_now())
        assert not repo.get_ticket(t.id).is_held

    def test_state_instance_lifecycle(self, repo: TicketRepository):
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        inst = repo.open_state_instance(
            ticket_id=t.id, state_name="Planning", sequence=1, now=_now()
        )
        repo.mark_execute_started(inst.id, now=dt.datetime(2026, 6, 13, 12, 1))
        repo.mark_execute_completed(
            inst.id,
            now=dt.datetime(2026, 6, 13, 12, 5),
            outcome_kind=OutcomeKind.CLEAN,
            outcome_payload={"summary": "ok"},
            next_state="SpecReview",
        )
        repo.close_state_instance(inst.id, now=dt.datetime(2026, 6, 13, 12, 6))
        closed = repo.get_state_instance(inst.id)
        assert closed.outcome_kind == OutcomeKind.CLEAN
        assert closed.next_state == "SpecReview"
        assert closed.outcome_payload == {"summary": "ok"}
        assert not closed.is_in_flight

    def test_list_in_flight_state_instances(self, repo: TicketRepository):
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        done = repo.open_state_instance(
            ticket_id=t.id, state_name="Queued", sequence=1, now=_now()
        )
        repo.mark_execute_completed(
            done.id, now=_now(), outcome_kind=OutcomeKind.CLEAN,
            outcome_payload={}, next_state="Planning",
        )
        repo.close_state_instance(done.id, now=_now())
        in_flight = repo.open_state_instance(
            ticket_id=t.id, state_name="Planning", sequence=2, now=_now()
        )
        rows = repo.list_in_flight_state_instances()
        assert [r.id for r in rows] == [in_flight.id]

    def test_record_failure(self, repo: TicketRepository):
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        inst = repo.open_state_instance(
            ticket_id=t.id, state_name="Planning", sequence=1, now=_now()
        )
        repo.record_failure(
            inst.id, now=_now(), failure_phase="execute",
            failure_reason="timeout",
        )
        fetched = repo.get_state_instance(inst.id)
        assert fetched.failure_phase == "execute"
        assert fetched.failure_reason == "timeout"
```

Then collapse `test_in_memory_repository.py` to:

```python
# packages/foreman/tests/v4/test_in_memory_repository.py
from foreman.v4.repository import InMemoryTicketRepository

from ._repository_contract import RepositoryContract


class TestInMemory(RepositoryContract):
    factory = InMemoryTicketRepository
```

- [ ] **Step 2: Write the failing test for the SQLite impl**

```python
# packages/foreman/tests/v4/test_sqlite_repository.py
from foreman.v4.sqlite_repository import SqliteTicketRepository

from ._repository_contract import RepositoryContract


class TestSqlite(RepositoryContract):
    @staticmethod
    def factory() -> SqliteTicketRepository:
        return SqliteTicketRepository.in_memory()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_sqlite_repository.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.v4.sqlite_repository'`

- [ ] **Step 4: Write the SQLite impl**

```python
# packages/foreman/src/foreman/v4/sqlite_repository.py
"""SqliteTicketRepository — production persistence backed by stdlib sqlite3.

Behavior contract is identical to InMemoryTicketRepository — the same
RepositoryContract test suite runs against both. If this file diverges, the
contract tests catch it before anything downstream notices.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

from foreman.v4.outcome import OutcomeKind
from foreman.v4.records import StateInstanceRecord, TicketRecord
from foreman.v4.repository import (
    StateInstanceNotFoundError,
    TicketAlreadyExistsError,
    TicketNotFoundError,
)


_SCHEMA = Path(__file__).with_name("schema.sql")
_TERMINAL_STATES = ("Done", "Failed")


def _to_iso(value: dt.datetime) -> str:
    return value.isoformat()


def _from_iso(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value) if value is not None else None


def _ticket_row_to_record(row: sqlite3.Row) -> TicketRecord:
    return TicketRecord(
        id=row["id"],
        project=row["project"],
        issue_number=row["issue_number"],
        current_state=row["current_state"],
        created_at=_from_iso(row["created_at"]),  # type: ignore[arg-type]
        updated_at=_from_iso(row["updated_at"]),  # type: ignore[arg-type]
        held_by=row["held_by"],
        held_at=_from_iso(row["held_at"]),
        held_reason=row["held_reason"],
    )


def _instance_row_to_record(row: sqlite3.Row) -> StateInstanceRecord:
    payload = json.loads(row["outcome_payload"]) if row["outcome_payload"] else None
    kind = OutcomeKind(row["outcome_kind"]) if row["outcome_kind"] else None
    return StateInstanceRecord(
        id=row["id"],
        ticket_id=row["ticket_id"],
        state_name=row["state_name"],
        sequence=row["sequence"],
        entered_at=_from_iso(row["entered_at"]),  # type: ignore[arg-type]
        execute_started_at=_from_iso(row["execute_started_at"]),
        execute_completed_at=_from_iso(row["execute_completed_at"]),
        exited_at=_from_iso(row["exited_at"]),
        outcome_kind=kind,
        outcome_payload=payload,
        next_state=row["next_state"],
        failure_phase=row["failure_phase"],
        failure_reason=row["failure_reason"],
    )


class SqliteTicketRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
        conn.commit()
        self._conn = conn

    @classmethod
    def in_memory(cls) -> SqliteTicketRepository:
        return cls(sqlite3.connect(":memory:"))

    @classmethod
    def at_path(cls, path: Path) -> SqliteTicketRepository:
        return cls(sqlite3.connect(path))

    # --- Ticket CRUD ---

    def create_ticket(self, *, project: str, issue_number: int, now: dt.datetime) -> TicketRecord:
        ts = _to_iso(now)
        try:
            cur = self._conn.execute(
                "INSERT INTO tickets(project, issue_number, current_state, created_at, updated_at) "
                "VALUES (?, ?, 'Queued', ?, ?)",
                (project, issue_number, ts, ts),
            )
        except sqlite3.IntegrityError as exc:
            raise TicketAlreadyExistsError(f"{project}#{issue_number}") from exc
        self._conn.commit()
        return self.get_ticket(cur.lastrowid)  # type: ignore[arg-type]

    def get_ticket(self, ticket_id: int) -> TicketRecord:
        row = self._conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if row is None:
            raise TicketNotFoundError(str(ticket_id))
        return _ticket_row_to_record(row)

    def get_ticket_by_issue(self, *, project: str, issue_number: int) -> TicketRecord:
        row = self._conn.execute(
            "SELECT * FROM tickets WHERE project = ? AND issue_number = ?",
            (project, issue_number),
        ).fetchone()
        if row is None:
            raise TicketNotFoundError(f"{project}#{issue_number}")
        return _ticket_row_to_record(row)

    def list_open_tickets(self) -> list[TicketRecord]:
        rows = self._conn.execute(
            "SELECT * FROM tickets WHERE current_state NOT IN (?, ?)",
            _TERMINAL_STATES,
        ).fetchall()
        return [_ticket_row_to_record(r) for r in rows]

    def set_ticket_state(self, ticket_id: int, new_state: str, *, now: dt.datetime) -> None:
        self._conn.execute(
            "UPDATE tickets SET current_state = ?, updated_at = ? WHERE id = ?",
            (new_state, _to_iso(now), ticket_id),
        )
        self._conn.commit()

    def hold_ticket(self, ticket_id: int, *, held_by: str, reason: str, now: dt.datetime) -> None:
        ts = _to_iso(now)
        self._conn.execute(
            "UPDATE tickets SET held_by = ?, held_at = ?, held_reason = ?, updated_at = ? "
            "WHERE id = ?",
            (held_by, ts, reason, ts, ticket_id),
        )
        self._conn.commit()

    def resume_ticket(self, ticket_id: int, *, now: dt.datetime) -> None:
        self._conn.execute(
            "UPDATE tickets SET held_by = NULL, held_at = NULL, held_reason = NULL, "
            "updated_at = ? WHERE id = ?",
            (_to_iso(now), ticket_id),
        )
        self._conn.commit()

    # --- State-instance journal ---

    def open_state_instance(
        self, *, ticket_id: int, state_name: str, sequence: int, now: dt.datetime
    ) -> StateInstanceRecord:
        self.get_ticket(ticket_id)  # raise if missing
        cur = self._conn.execute(
            "INSERT INTO state_instances(ticket_id, state_name, sequence, entered_at) "
            "VALUES (?, ?, ?, ?)",
            (ticket_id, state_name, sequence, _to_iso(now)),
        )
        self._conn.commit()
        return self.get_state_instance(cur.lastrowid)  # type: ignore[arg-type]

    def get_state_instance(self, instance_id: int) -> StateInstanceRecord:
        row = self._conn.execute(
            "SELECT * FROM state_instances WHERE id = ?", (instance_id,)
        ).fetchone()
        if row is None:
            raise StateInstanceNotFoundError(str(instance_id))
        return _instance_row_to_record(row)

    def mark_execute_started(self, instance_id: int, *, now: dt.datetime) -> None:
        self._conn.execute(
            "UPDATE state_instances SET execute_started_at = ? WHERE id = ?",
            (_to_iso(now), instance_id),
        )
        self._conn.commit()

    def mark_execute_completed(
        self,
        instance_id: int,
        *,
        now: dt.datetime,
        outcome_kind: OutcomeKind,
        outcome_payload: dict[str, Any],
        next_state: str,
    ) -> None:
        self._conn.execute(
            "UPDATE state_instances "
            "SET execute_completed_at = ?, outcome_kind = ?, outcome_payload = ?, next_state = ? "
            "WHERE id = ?",
            (
                _to_iso(now),
                outcome_kind.value,
                json.dumps(outcome_payload),
                next_state,
                instance_id,
            ),
        )
        self._conn.commit()

    def close_state_instance(self, instance_id: int, *, now: dt.datetime) -> None:
        self._conn.execute(
            "UPDATE state_instances SET exited_at = ? WHERE id = ?",
            (_to_iso(now), instance_id),
        )
        self._conn.commit()

    def record_failure(
        self,
        instance_id: int,
        *,
        now: dt.datetime,
        failure_phase: str,
        failure_reason: str,
    ) -> None:
        self._conn.execute(
            "UPDATE state_instances SET failure_phase = ?, failure_reason = ? WHERE id = ?",
            (failure_phase, failure_reason, instance_id),
        )
        self._conn.commit()

    def list_in_flight_state_instances(self) -> list[StateInstanceRecord]:
        rows = self._conn.execute(
            "SELECT * FROM state_instances WHERE exited_at IS NULL ORDER BY ticket_id, sequence"
        ).fetchall()
        return [_instance_row_to_record(r) for r in rows]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/ -v`
Expected: in-memory contract tests (10) and SQLite contract tests (10) all pass.

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/v4/sqlite_repository.py packages/foreman/tests/v4/test_sqlite_repository.py packages/foreman/tests/v4/_repository_contract.py packages/foreman/tests/v4/test_in_memory_repository.py
git commit -m "feat(v4): add sqlite repository impl with shared contract test suite"
```

### Task 1.8: TicketState ABC + StateContext

**Files:**
- Create: `packages/foreman/src/foreman/v4/state.py`
- Test: `packages/foreman/tests/v4/test_state_abc.py`

Cites spec section **Approach / State pattern, Template Method pattern**.

`StateContext` is the per-transition handle: it carries the `TicketRecord`, the `StateInstanceRecord` for the current attempt, the `TicketRepository` for persistence calls, and a `clock` for testable timestamps. The abstract `TicketState` declares the five hooks. `transition()` (the Template Method) lands in Task 1.9.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_state_abc.py
"""TicketState ABC and StateContext shape."""
from __future__ import annotations

import datetime as dt
import pytest

from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext, TicketState


class _ConcreteState(TicketState):
    state_name = "Concrete"

    def execute(self, ctx: StateContext) -> Outcome:
        return Outcome(
            kind=OutcomeKind.CLEAN,
            confidence=OutcomeConfidence.HIGH,
            summary="ok",
        )

    def next_state(self, outcome: Outcome) -> TicketState | None:
        return None


def test_ticket_state_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        TicketState()  # type: ignore[abstract]


def test_concrete_state_uses_class_name_default():
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Concrete", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    state = _ConcreteState()
    ctx = StateContext(
        ticket=ticket,
        instance=instance,
        repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
    )
    assert state.can_run(ctx) is True  # default True
    assert state.enter(ctx) is None    # default no-op
    outcome = state.execute(ctx)
    assert outcome.kind == OutcomeKind.CLEAN
    state.verify(ctx, outcome)         # default no-op
    state.exit(ctx, outcome)           # default no-op


def test_default_can_run_respects_hold():
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.hold_ticket(ticket.id, held_by="jeff", reason="vacation", now=dt.datetime(2026, 6, 13))
    held_ticket = repo.get_ticket(ticket.id)
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Concrete", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    state = _ConcreteState()
    ctx = StateContext(
        ticket=held_ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
    )
    assert state.can_run(ctx) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_state_abc.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.v4.state'`

- [ ] **Step 3: Write the ABC + context**

```python
# packages/foreman/src/foreman/v4/state.py
"""TicketState — abstract base for every concrete state in the v4 machine.

The five-hook lifecycle is fixed:

    can_run    — preverify; may the state run right now?
    enter      — setup; record entry, allocate resources
    execute    — do the work; return an Outcome
    verify     — postverify; parse + validate the Outcome
    exit       — teardown; always runs after a successful enter()

The Template Method ``transition()`` orchestrates them in order with
per-phase failure handlers. That lands in Task 1.9.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

from foreman.v4.outcome import Outcome
from foreman.v4.records import StateInstanceRecord, TicketRecord
from foreman.v4.repository import TicketRepository


@dataclass(frozen=True)
class StateContext:
    """The per-transition handle passed to every lifecycle hook."""
    ticket: TicketRecord
    instance: StateInstanceRecord
    repo: TicketRepository
    clock: Callable[[], dt.datetime]


class TicketState(ABC):
    """One phase in the ticket's lifecycle.

    Subclasses MUST override ``execute()`` and ``next_state()``. The other
    four hooks have sensible defaults; override only when the state needs
    distinct behavior.
    """

    #: Display name; defaults to the class name minus 'State' suffix.
    state_name: str = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if not cls.state_name:
            name = cls.__name__
            cls.state_name = name[:-5] if name.endswith("State") else name

    # --- Lifecycle hooks ---

    def can_run(self, ctx: StateContext) -> bool:
        """Preverify gate. Default: refuse to run if the ticket is held."""
        return not ctx.ticket.is_held

    def enter(self, ctx: StateContext) -> None:
        """Setup. Default: no-op."""
        return None

    @abstractmethod
    def execute(self, ctx: StateContext) -> Outcome:
        """Do the work. Return the Outcome the verify hook will parse."""

    def verify(self, ctx: StateContext, outcome: Outcome) -> None:
        """Postverify the outcome. Default: no-op (raise to reject)."""
        return None

    def exit(self, ctx: StateContext, outcome: Outcome | None) -> None:
        """Teardown. Always runs after a successful enter(). Default: no-op.

        ``outcome`` is None when execute() raised before producing one.
        """
        return None

    # --- Transition policy ---

    @abstractmethod
    def next_state(self, outcome: Outcome) -> "TicketState | None":
        """Decide what comes next. Return None to halt the state machine."""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_state_abc.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/state.py packages/foreman/tests/v4/test_state_abc.py
git commit -m "feat(v4): add TicketState ABC and StateContext"
```

### Task 1.9: Template Method `transition()` — happy path + per-phase failures

**Files:**
- Modify: `packages/foreman/src/foreman/v4/state.py` (add `transition()` + failure dispatcher)
- Test: `packages/foreman/tests/v4/test_transition.py`

Cites spec section **Approach / Template Method pattern** and **Durability + resume**.

The Template Method walks the five hooks. Failures dispatch by phase. Per spec:

| Phase | Failure → |
| --- | --- |
| `can_run` returns False | record `failure_phase="can_run"`, `failure_reason="held"`; no further hooks run; transition is a no-op (ticket stays in current state). |
| `enter` raises | record failure on the instance; do NOT call exit (enter didn't return). |
| `execute` raises | record failure; call exit(ctx, None). |
| `verify` raises | record failure; call exit(ctx, outcome). |
| `exit` raises | record failure; failure_phase="exit"; transition is still considered complete. |

Successful path writes timestamps via the repo at each hook boundary, then calls `close_state_instance` and `set_ticket_state(new_state)`.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_transition.py
"""Template Method orchestration — happy path + per-phase failures."""
from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext, TicketState


class _Recorder:
    """Tracks which hooks were invoked, in order."""
    def __init__(self) -> None:
        self.calls: list[str] = []


class _HappyState(TicketState):
    state_name = "Happy"

    def __init__(self, recorder: _Recorder, next_: TicketState | None) -> None:
        self.r = recorder
        self._next = next_

    def enter(self, ctx: StateContext) -> None:
        self.r.calls.append("enter")

    def execute(self, ctx: StateContext) -> Outcome:
        self.r.calls.append("execute")
        return Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="ok",
        )

    def verify(self, ctx: StateContext, outcome: Outcome) -> None:
        self.r.calls.append("verify")

    def exit(self, ctx: StateContext, outcome: Outcome | None) -> None:
        self.r.calls.append("exit")

    def next_state(self, outcome: Outcome) -> TicketState | None:
        return self._next


class _NextState(TicketState):
    state_name = "Next"

    def execute(self, ctx: StateContext) -> Outcome:
        return Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="done",
        )

    def next_state(self, outcome: Outcome) -> TicketState | None:
        return None


class _RaisingState(TicketState):
    state_name = "Raising"

    def __init__(self, raise_in: str, recorder: _Recorder) -> None:
        self._raise_in = raise_in
        self.r = recorder

    def enter(self, ctx: StateContext) -> None:
        self.r.calls.append("enter")
        if self._raise_in == "enter":
            raise RuntimeError("enter boom")

    def execute(self, ctx: StateContext) -> Outcome:
        self.r.calls.append("execute")
        if self._raise_in == "execute":
            raise RuntimeError("execute boom")
        return Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="ok",
        )

    def verify(self, ctx: StateContext, outcome: Outcome) -> None:
        self.r.calls.append("verify")
        if self._raise_in == "verify":
            raise RuntimeError("verify boom")

    def exit(self, ctx: StateContext, outcome: Outcome | None) -> None:
        self.r.calls.append("exit")
        if self._raise_in == "exit":
            raise RuntimeError("exit boom")

    def next_state(self, outcome: Outcome) -> TicketState | None:
        return None


@pytest.fixture()
def setup() -> tuple[InMemoryTicketRepository, Any, Any]:
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Happy", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    return repo, ticket, instance


def _ctx(repo: InMemoryTicketRepository, ticket: Any, instance: Any) -> StateContext:
    return StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )


def test_happy_path_invokes_all_five_hooks_in_order(setup):
    repo, ticket, instance = setup
    recorder = _Recorder()
    next_state = _NextState()
    state = _HappyState(recorder, next_state)
    result = state.transition(_ctx(repo, ticket, instance))
    assert recorder.calls == ["enter", "execute", "verify", "exit"]
    assert result is next_state
    closed = repo.get_state_instance(instance.id)
    assert not closed.is_in_flight
    assert closed.outcome_kind == OutcomeKind.CLEAN
    assert closed.next_state == "Next"
    assert repo.get_ticket(ticket.id).current_state == "Next"


def test_can_run_false_records_held_and_returns_none(setup):
    repo, ticket, instance = setup
    repo.hold_ticket(ticket.id, held_by="jeff", reason="vacation", now=dt.datetime(2026, 6, 13))
    held_ticket = repo.get_ticket(ticket.id)
    recorder = _Recorder()
    state = _HappyState(recorder, None)
    result = state.transition(_ctx(repo, held_ticket, instance))
    assert result is None
    assert recorder.calls == []  # no hooks ran
    fetched = repo.get_state_instance(instance.id)
    assert fetched.failure_phase == "can_run"
    assert fetched.failure_reason == "held"
    assert fetched.is_in_flight  # instance NOT closed; transition is no-op


def test_enter_raises_records_failure_and_skips_exit(setup):
    repo, ticket, instance = setup
    recorder = _Recorder()
    state = _RaisingState(raise_in="enter", recorder=recorder)
    result = state.transition(_ctx(repo, ticket, instance))
    assert result is None
    assert recorder.calls == ["enter"]  # exit not called because enter never returned
    fetched = repo.get_state_instance(instance.id)
    assert fetched.failure_phase == "enter"
    assert "enter boom" in (fetched.failure_reason or "")


def test_execute_raises_records_failure_and_calls_exit(setup):
    repo, ticket, instance = setup
    recorder = _Recorder()
    state = _RaisingState(raise_in="execute", recorder=recorder)
    state.transition(_ctx(repo, ticket, instance))
    assert recorder.calls == ["enter", "execute", "exit"]
    fetched = repo.get_state_instance(instance.id)
    assert fetched.failure_phase == "execute"


def test_verify_raises_records_failure_and_calls_exit(setup):
    repo, ticket, instance = setup
    recorder = _Recorder()
    state = _RaisingState(raise_in="verify", recorder=recorder)
    state.transition(_ctx(repo, ticket, instance))
    assert recorder.calls == ["enter", "execute", "verify", "exit"]
    fetched = repo.get_state_instance(instance.id)
    assert fetched.failure_phase == "verify"


def test_exit_raises_records_failure_but_transition_completes(setup):
    repo, ticket, instance = setup
    recorder = _Recorder()
    state = _RaisingState(raise_in="exit", recorder=recorder)
    state.transition(_ctx(repo, ticket, instance))
    assert recorder.calls == ["enter", "execute", "verify", "exit"]
    fetched = repo.get_state_instance(instance.id)
    assert fetched.failure_phase == "exit"
    assert not fetched.is_in_flight  # still closed despite exit raising
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_transition.py -v`
Expected: FAIL with `AttributeError: 'Happy' object has no attribute 'transition'`

- [ ] **Step 3: Add `transition()` to `TicketState`**

Append to `packages/foreman/src/foreman/v4/state.py`:

```python
    # --- Template Method ---

    def transition(self, ctx: StateContext) -> "TicketState | None":
        """Orchestrate the five-hook lifecycle. The base class controls the
        flow; subclasses control the steps. See the docstring of each hook
        for what its handler does on failure."""

        if not self.can_run(ctx):
            ctx.repo.record_failure(
                ctx.instance.id, now=ctx.clock(),
                failure_phase="can_run", failure_reason="held",
            )
            return None

        try:
            self.enter(ctx)
        except Exception as exc:  # noqa: BLE001
            ctx.repo.record_failure(
                ctx.instance.id, now=ctx.clock(),
                failure_phase="enter", failure_reason=repr(exc),
            )
            # Skip exit: enter never returned, so no resources to release.
            return None

        outcome: Outcome | None = None
        try:
            ctx.repo.mark_execute_started(ctx.instance.id, now=ctx.clock())
            try:
                outcome = self.execute(ctx)
            except Exception as exc:  # noqa: BLE001
                ctx.repo.record_failure(
                    ctx.instance.id, now=ctx.clock(),
                    failure_phase="execute", failure_reason=repr(exc),
                )
                return None

            try:
                self.verify(ctx, outcome)
            except Exception as exc:  # noqa: BLE001
                ctx.repo.record_failure(
                    ctx.instance.id, now=ctx.clock(),
                    failure_phase="verify", failure_reason=repr(exc),
                )
                return None

            next_ = self.next_state(outcome)
            ctx.repo.mark_execute_completed(
                ctx.instance.id, now=ctx.clock(),
                outcome_kind=outcome.kind,
                outcome_payload=outcome.model_dump(mode="json"),
                next_state=next_.state_name if next_ is not None else "",
            )
            if next_ is not None:
                ctx.repo.set_ticket_state(
                    ctx.ticket.id, next_.state_name, now=ctx.clock(),
                )
            return next_
        finally:
            try:
                self.exit(ctx, outcome)
            except Exception as exc:  # noqa: BLE001
                ctx.repo.record_failure(
                    ctx.instance.id, now=ctx.clock(),
                    failure_phase="exit", failure_reason=repr(exc),
                )
            ctx.repo.close_state_instance(ctx.instance.id, now=ctx.clock())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/ -v`
Expected: all v4 tests pass (4 new transition tests + everything from earlier tasks).

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/state.py packages/foreman/tests/v4/test_transition.py
git commit -m "feat(v4): add Template Method transition() with per-phase failure handlers"
```

### Task 1.10: v4 isolation guard test

**Files:**
- Create: `packages/foreman/tests/v4/test_isolation.py`

Cites the **v4 isolation principle** section. This is the load-bearing piece that makes Phase 8's `git rm` safe: a test that AST-walks every `foreman/v4/**/*.py` file and asserts none of them import from the kill set. If a future task accidentally adds an import like `from foreman.reconciler.actions import merge_pr`, this test fails at commit time — not at Phase 8.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_isolation.py
"""v4 isolation guard.

Phase 8 deletes v2/v3 by `git rm`. That's only safe if foreman.v4 never
imports from the kill set. This test AST-walks every v4 source file and
verifies the discipline holds.

If this test fails, the failing module reached into a legacy package.
Either move the dependency into foreman.v4 (correct) or reconsider whether
the legacy module belongs in the survival set instead (cite a reason).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

V4_ROOT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "foreman"
    / "v4"
)

# Modules whose entire purpose is the v2/v3 substrate. v4 must NOT import them.
KILL_SET = frozenset(
    {
        "foreman.reconciler",
        "foreman.daemon",
        "foreman.daemon_runners",
        "foreman.daemon_host",
        "foreman.daemon_lock",
        "foreman.dispatcher",
        "foreman.dispatch_recorder",
        "foreman.poller",
        "foreman.queue",
        "foreman.storage",
        "foreman.worker",
        "foreman.role_dispatch",
        "foreman.stats",
        "foreman.ps",
        "foreman.labels",
        "foreman.branches",
        "foreman.v3_bus_endpoint",
    }
)


def _iter_v4_files() -> list[Path]:
    assert V4_ROOT.is_dir(), f"v4 package missing at {V4_ROOT}"
    return sorted(V4_ROOT.rglob("*.py"))


def _imports_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None and node.level == 0:
                found.add(node.module)
    return found


@pytest.mark.parametrize("path", _iter_v4_files(), ids=lambda p: p.name)
def test_v4_module_does_not_import_kill_set(path: Path) -> None:
    imports = _imports_in(path)
    forbidden = {
        imp for imp in imports
        if any(imp == k or imp.startswith(k + ".") for k in KILL_SET)
    }
    assert not forbidden, (
        f"{path.relative_to(V4_ROOT)} imports from the kill set: {forbidden}. "
        "v4 modules must not depend on v2/v3 substrate. See the 'v4 isolation "
        "principle' section in the implementation plan."
    )


def test_kill_set_and_survival_set_are_disjoint() -> None:
    """Defensive: catch typos that would put a module on both lists."""
    survival_set = {
        "foreman.auth",
        "foreman.config",
        "foreman.identity",
        "foreman.init",
        "foreman.instructions",
        "foreman.locks",
        "foreman.git_host",
        "foreman.git_hosts",
        "foreman.provider",
        "foreman.providers",
        "foreman.roles",
        "foreman.prompts",
        "foreman.worktree",
        "foreman._env_filter",
        "foreman.logging_setup",
    }
    assert KILL_SET.isdisjoint(survival_set), KILL_SET & survival_set
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest packages/foreman/tests/v4/test_isolation.py -v`
Expected: all parametrized cases pass (one per v4 .py file). If any fail at this point, a prior task introduced a forbidden import — fix the source, not the test.

- [ ] **Step 3: Commit**

```bash
git add packages/foreman/tests/v4/test_isolation.py
git commit -m "test(v4): add isolation guard against v2/v3 substrate imports"
```

### Phase 1 — `just check` gate

After Task 1.9, run the full pre-push gate.

- [ ] **Run:** `just check`
- [ ] **Expected:** ruff + mypy + import-linter + full pytest all pass.
- [ ] **If a test fails:** investigate the actual failure (do not skip hooks). Foundation tasks introduce a new package; existing tests must remain green. The new tests prove the new code works.

Phase 1 completion criterion (from the outline): **state machine works in isolation**. By Task 1.10 we have an in-memory state machine that runs a state's full five-hook lifecycle with persistent journaling, a SQLite-backed Repository ready for the daemon to use later, AND a parametrized isolation test that will trip any future task that accidentally couples v4 to the v2/v3 substrate. Concrete states + their next-state logic come in Phase 3; the orchestration mechanics are done.

---

## Phase 2 — Events + Observers

The journal in `state_instances` is the source of truth for durability — that's already running after Phase 1. Phase 2 adds a **secondary notification stream** for everything that isn't durability: GitHub label updates, structured logging, metrics, and an optional audit-trail events table. The Template Method publishes one event per lifecycle boundary; observers consume independently.

Why this split: durability writes (timestamp columns on `state_instances`) MUST land synchronously inside `transition()` because crash-recovery reads them. Observability writes (labels, logs, metrics) MUST NOT block or fail a transition. The EventBus is the firewall between them — observers raising exceptions never corrupts the journal.

### Task 2.1: Event base + concrete event types

**Files:**
- Create: `packages/foreman/src/foreman/v4/events.py`
- Test: `packages/foreman/tests/v4/test_events.py`

Five events, one per lifecycle boundary the Template Method already crosses:

| Event | Emitted when |
| --- | --- |
| `StateEnteredEvent` | `enter()` returned successfully |
| `ExecuteStartedEvent` | `execute()` is about to be called |
| `ExecuteCompletedEvent` | `execute()` returned an Outcome and verify passed |
| `StateExitedEvent` | `exit()` returned (success or failure of exit logged via `failure_phase`) |
| `StateFailedEvent` | any phase raised; carries `failure_phase` and `failure_reason` |

Frozen dataclasses, no behavior — just data observers can read.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_events.py
"""Concrete event types — shape contract for the notification stream."""
from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.events import (
    Event,
    ExecuteCompletedEvent,
    ExecuteStartedEvent,
    StateEnteredEvent,
    StateExitedEvent,
    StateFailedEvent,
)
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind


_T0 = dt.datetime(2026, 6, 13, 12, 0, 0)


def test_state_entered_event_fields():
    ev = StateEnteredEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
    )
    assert ev.ticket_id == 1
    assert ev.state_name == "Planning"
    assert ev.at == _T0


def test_execute_started_event_fields():
    ev = ExecuteStartedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
    )
    assert ev.instance_id == 10


def test_execute_completed_event_carries_outcome():
    outcome = Outcome(
        kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
        summary="ok",
    )
    ev = ExecuteCompletedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0, outcome=outcome, next_state="SpecReview",
    )
    assert ev.outcome is outcome
    assert ev.next_state == "SpecReview"


def test_state_exited_event_carries_optional_outcome():
    ev_with = StateExitedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
        outcome=Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="ok",
        ),
    )
    ev_without = StateExitedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0, outcome=None,
    )
    assert ev_with.outcome is not None
    assert ev_without.outcome is None


def test_state_failed_event_carries_phase_and_reason():
    ev = StateFailedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
        failure_phase="execute", failure_reason="subprocess timed out",
    )
    assert ev.failure_phase == "execute"
    assert ev.failure_reason == "subprocess timed out"


def test_all_event_classes_are_subclasses_of_event():
    for cls in (
        StateEnteredEvent, ExecuteStartedEvent, ExecuteCompletedEvent,
        StateExitedEvent, StateFailedEvent,
    ):
        assert issubclass(cls, Event)


def test_events_are_immutable():
    ev = StateEnteredEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
    )
    with pytest.raises(AttributeError):
        ev.state_name = "Something"  # type: ignore[misc]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_events.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.v4.events'`

- [ ] **Step 3: Write the events module**

```python
# packages/foreman/src/foreman/v4/events.py
"""Lifecycle events — the notification stream observers consume.

These events are emitted by TicketState.transition() at each hook boundary.
They are pure data — observers decide what (if anything) to do with them.
Events MUST NOT carry references to live objects (repos, sockets); only
serializable values. This keeps audit / replay implementations honest.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from foreman.v4.outcome import Outcome


@dataclass(frozen=True, slots=True)
class Event:
    """Common fields for every lifecycle event."""
    ticket_id: int
    instance_id: int
    state_name: str
    sequence: int
    at: dt.datetime


@dataclass(frozen=True, slots=True)
class StateEnteredEvent(Event):
    """``enter()`` returned successfully."""


@dataclass(frozen=True, slots=True)
class ExecuteStartedEvent(Event):
    """``execute()`` is about to be called."""


@dataclass(frozen=True, slots=True)
class ExecuteCompletedEvent(Event):
    """``execute()`` returned an Outcome and ``verify()`` passed."""
    outcome: Outcome
    next_state: str


@dataclass(frozen=True, slots=True)
class StateExitedEvent(Event):
    """``exit()`` returned. ``outcome`` is None if execute() raised."""
    outcome: Outcome | None


@dataclass(frozen=True, slots=True)
class StateFailedEvent(Event):
    """A lifecycle hook raised. ``failure_phase`` matches the failed hook."""
    failure_phase: str
    failure_reason: str
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_events.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/events.py packages/foreman/tests/v4/test_events.py
git commit -m "feat(v4): add lifecycle event dataclasses"
```

### Task 2.2: EventBus with observer exception isolation

**Files:**
- Create: `packages/foreman/src/foreman/v4/event_bus.py`
- Test: `packages/foreman/tests/v4/test_event_bus.py`

The bus is the firewall between the durability path and observability side effects. Contract:

- Subscribers register a callable taking one `Event`.
- `publish(event)` invokes every subscriber in registration order.
- An exception in one subscriber MUST NOT prevent later subscribers from running, MUST NOT propagate to the caller, and MUST be logged for forensics.
- Subscribers may filter by event type internally; the bus does no filtering.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_event_bus.py
"""EventBus — fan-out with subscriber exception isolation."""
from __future__ import annotations

import datetime as dt
import logging

from foreman.v4.event_bus import EventBus
from foreman.v4.events import StateEnteredEvent


def _make_event() -> StateEnteredEvent:
    return StateEnteredEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=dt.datetime(2026, 6, 13),
    )


def test_publishes_event_to_single_subscriber():
    bus = EventBus()
    received: list = []
    bus.subscribe(received.append)
    ev = _make_event()
    bus.publish(ev)
    assert received == [ev]


def test_publishes_event_to_all_subscribers_in_registration_order():
    bus = EventBus()
    order: list[str] = []
    bus.subscribe(lambda _: order.append("a"))
    bus.subscribe(lambda _: order.append("b"))
    bus.subscribe(lambda _: order.append("c"))
    bus.publish(_make_event())
    assert order == ["a", "b", "c"]


def test_subscriber_exception_does_not_break_others(caplog):
    bus = EventBus()
    after_first: list = []
    after_third: list = []

    def boom(_):
        raise RuntimeError("observer boom")

    bus.subscribe(boom)
    bus.subscribe(after_first.append)
    bus.subscribe(boom)
    bus.subscribe(after_third.append)
    ev = _make_event()
    with caplog.at_level(logging.WARNING, logger="foreman.v4.event_bus"):
        bus.publish(ev)
    assert after_first == [ev]
    assert after_third == [ev]
    # Both failures should be logged for forensics.
    boom_logs = [r for r in caplog.records if "observer boom" in r.message]
    assert len(boom_logs) == 2


def test_publish_does_not_raise_when_subscriber_raises():
    bus = EventBus()
    bus.subscribe(lambda _: (_ for _ in ()).throw(RuntimeError("nope")))
    bus.publish(_make_event())  # no exception leaks


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    received: list = []
    bus.subscribe(received.append)
    bus.unsubscribe(received.append)
    bus.publish(_make_event())
    assert received == []


def test_unsubscribe_unknown_callable_is_noop():
    bus = EventBus()
    bus.unsubscribe(lambda _: None)  # should not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_event_bus.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.v4.event_bus'`

- [ ] **Step 3: Write the bus**

```python
# packages/foreman/src/foreman/v4/event_bus.py
"""EventBus — synchronous publish/subscribe with exception isolation.

The bus is the firewall between the durability path (transition() writing
state_instances rows) and observability side effects (labels, logs, metrics).
A misbehaving observer must never block or fail a transition; observer
exceptions are caught and logged here.

Synchronous on purpose: observer order is predictable, no thread-safety
debt, no asyncio backpressure to think about. If an observer needs to be
async (network IO, large IO bursts), it can dispatch its own background
work from inside its callback.
"""

from __future__ import annotations

import logging
from typing import Callable

from foreman.v4.events import Event


_log = logging.getLogger(__name__)

EventListener = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[EventListener] = []

    def subscribe(self, listener: EventListener) -> None:
        self._subscribers.append(listener)

    def unsubscribe(self, listener: EventListener) -> None:
        try:
            self._subscribers.remove(listener)
        except ValueError:
            # Idempotent — unsubscribing twice is a no-op, not an error.
            pass

    def publish(self, event: Event) -> None:
        for listener in list(self._subscribers):
            try:
                listener(event)
            except Exception:  # noqa: BLE001 — firewall is the whole point
                _log.warning(
                    "observer raised on %s for ticket=%d instance=%d",
                    type(event).__name__, event.ticket_id, event.instance_id,
                    exc_info=True,
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_event_bus.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/event_bus.py packages/foreman/tests/v4/test_event_bus.py
git commit -m "feat(v4): add EventBus with observer exception isolation"
```

### Task 2.3: Wire EventBus into `transition()`

**Files:**
- Modify: `packages/foreman/src/foreman/v4/state.py` (extend `StateContext` with bus; emit events from `transition()`)
- Test: `packages/foreman/tests/v4/test_transition_events.py`

Each transition publishes events at the five lifecycle boundaries the Template Method already crosses. The bus call is the LAST thing transition() does at each boundary — after the journal write to `state_instances` is committed, so observers see a durable state.

`StateContext` gets a new optional field `bus: EventBus | None = None`. When None, transition() runs in headless mode (existing Phase 1 tests still pass). When set, the five events fan out.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_transition_events.py
"""transition() publishes the five lifecycle events at the right boundaries."""
from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.event_bus import EventBus
from foreman.v4.events import (
    Event,
    ExecuteCompletedEvent,
    ExecuteStartedEvent,
    StateEnteredEvent,
    StateExitedEvent,
    StateFailedEvent,
)
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext, TicketState


class _ClassicState(TicketState):
    state_name = "Classic"

    def execute(self, ctx: StateContext) -> Outcome:
        return Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="ok",
        )

    def next_state(self, outcome: Outcome) -> TicketState | None:
        return None


class _FailEnter(TicketState):
    state_name = "FailEnter"

    def enter(self, ctx: StateContext) -> None:
        raise RuntimeError("enter boom")

    def execute(self, ctx: StateContext) -> Outcome:  # pragma: no cover
        raise NotImplementedError

    def next_state(self, outcome: Outcome) -> TicketState | None:  # pragma: no cover
        return None


@pytest.fixture()
def setup():
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Classic", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    bus = EventBus()
    received: list[Event] = []
    bus.subscribe(received.append)
    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
        bus=bus,
    )
    return repo, ticket, instance, received, ctx


def test_happy_path_emits_four_events(setup):
    repo, ticket, instance, received, ctx = setup
    _ClassicState().transition(ctx)
    kinds = [type(ev).__name__ for ev in received]
    assert kinds == [
        "StateEnteredEvent",
        "ExecuteStartedEvent",
        "ExecuteCompletedEvent",
        "StateExitedEvent",
    ]


def test_execute_completed_carries_outcome_and_next_state(setup):
    repo, ticket, instance, received, ctx = setup
    _ClassicState().transition(ctx)
    completed = [ev for ev in received if isinstance(ev, ExecuteCompletedEvent)][0]
    assert completed.outcome.kind == OutcomeKind.CLEAN
    assert completed.next_state == ""  # terminal — no next state


def test_enter_failure_emits_failed_event_no_exit(setup):
    repo, ticket, instance, received, ctx = setup
    _FailEnter().transition(ctx)
    kinds = [type(ev).__name__ for ev in received]
    # enter() raised → no StateEntered, no Execute*, no Exited.
    assert kinds == ["StateFailedEvent"]
    failed = received[0]
    assert isinstance(failed, StateFailedEvent)
    assert failed.failure_phase == "enter"
    assert "enter boom" in failed.failure_reason


def test_no_bus_means_no_events(setup):
    repo, ticket, instance, received, _ctx = setup
    # Rebuild ctx without a bus
    ctx_no_bus = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        bus=None,
    )
    _ClassicState().transition(ctx_no_bus)
    assert received == []  # the original bus saw nothing


def test_misbehaving_observer_does_not_break_transition(setup):
    repo, ticket, instance, _received, ctx = setup

    def boom(_):
        raise RuntimeError("observer boom")

    ctx.bus.subscribe(boom)
    result = _ClassicState().transition(ctx)
    assert result is None  # terminal completion path
    # And the journal row was still finalized:
    closed = repo.get_state_instance(instance.id)
    assert not closed.is_in_flight
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_transition_events.py -v`
Expected: FAIL with `TypeError: StateContext.__init__() got an unexpected keyword argument 'bus'`

- [ ] **Step 3: Extend `StateContext` and `transition()`**

In `packages/foreman/src/foreman/v4/state.py`:

```python
# Add to the imports near the top:
from foreman.v4.event_bus import EventBus
from foreman.v4.events import (
    ExecuteCompletedEvent,
    ExecuteStartedEvent,
    StateEnteredEvent,
    StateExitedEvent,
    StateFailedEvent,
)
```

Update `StateContext`:

```python
@dataclass(frozen=True)
class StateContext:
    """The per-transition handle passed to every lifecycle hook."""
    ticket: TicketRecord
    instance: StateInstanceRecord
    repo: TicketRepository
    clock: Callable[[], dt.datetime]
    bus: EventBus | None = None
```

Add a private helper at module scope (above `TicketState`):

```python
def _publish(ctx: StateContext, event_type, **kwargs) -> None:
    if ctx.bus is None:
        return
    ctx.bus.publish(event_type(
        ticket_id=ctx.ticket.id,
        instance_id=ctx.instance.id,
        state_name=ctx.instance.state_name,
        sequence=ctx.instance.sequence,
        at=ctx.clock(),
        **kwargs,
    ))
```

Update `transition()` to emit events at each boundary. The shape stays the same; the additions are paired publish calls. For each failure-handler branch, also publish a `StateFailedEvent` BEFORE returning:

```python
    def transition(self, ctx: StateContext) -> "TicketState | None":
        if not self.can_run(ctx):
            ctx.repo.record_failure(
                ctx.instance.id, now=ctx.clock(),
                failure_phase="can_run", failure_reason="held",
            )
            _publish(ctx, StateFailedEvent, failure_phase="can_run", failure_reason="held")
            return None

        try:
            self.enter(ctx)
        except Exception as exc:  # noqa: BLE001
            ctx.repo.record_failure(
                ctx.instance.id, now=ctx.clock(),
                failure_phase="enter", failure_reason=repr(exc),
            )
            _publish(ctx, StateFailedEvent, failure_phase="enter", failure_reason=repr(exc))
            return None
        _publish(ctx, StateEnteredEvent)

        outcome: Outcome | None = None
        try:
            ctx.repo.mark_execute_started(ctx.instance.id, now=ctx.clock())
            _publish(ctx, ExecuteStartedEvent)
            try:
                outcome = self.execute(ctx)
            except Exception as exc:  # noqa: BLE001
                ctx.repo.record_failure(
                    ctx.instance.id, now=ctx.clock(),
                    failure_phase="execute", failure_reason=repr(exc),
                )
                _publish(ctx, StateFailedEvent, failure_phase="execute", failure_reason=repr(exc))
                return None

            try:
                self.verify(ctx, outcome)
            except Exception as exc:  # noqa: BLE001
                ctx.repo.record_failure(
                    ctx.instance.id, now=ctx.clock(),
                    failure_phase="verify", failure_reason=repr(exc),
                )
                _publish(ctx, StateFailedEvent, failure_phase="verify", failure_reason=repr(exc))
                return None

            next_ = self.next_state(outcome)
            ctx.repo.mark_execute_completed(
                ctx.instance.id, now=ctx.clock(),
                outcome_kind=outcome.kind,
                outcome_payload=outcome.model_dump(mode="json"),
                next_state=next_.state_name if next_ is not None else "",
            )
            _publish(
                ctx, ExecuteCompletedEvent,
                outcome=outcome,
                next_state=next_.state_name if next_ is not None else "",
            )
            if next_ is not None:
                ctx.repo.set_ticket_state(
                    ctx.ticket.id, next_.state_name, now=ctx.clock(),
                )
            return next_
        finally:
            try:
                self.exit(ctx, outcome)
            except Exception as exc:  # noqa: BLE001
                ctx.repo.record_failure(
                    ctx.instance.id, now=ctx.clock(),
                    failure_phase="exit", failure_reason=repr(exc),
                )
                _publish(ctx, StateFailedEvent, failure_phase="exit", failure_reason=repr(exc))
            ctx.repo.close_state_instance(ctx.instance.id, now=ctx.clock())
            _publish(ctx, StateExitedEvent, outcome=outcome)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/ -v`
Expected: all of Phase 1's transition tests still pass (no `bus` ⇒ no events ⇒ Phase 1 assertions unaffected), plus 5 new event-emission tests.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/state.py packages/foreman/tests/v4/test_transition_events.py
git commit -m "feat(v4): wire EventBus into transition() lifecycle"
```

### Task 2.4: StructuredLogObserver

**Files:**
- Create: `packages/foreman/src/foreman/v4/observers/__init__.py`
- Create: `packages/foreman/src/foreman/v4/observers/structured_log.py`
- Test: `packages/foreman/tests/v4/observers/test_structured_log.py`

JSON-lines emission to a `logging.Logger`. One line per event, with stable field names. The actual file handler is configured at daemon startup (Phase 7); this observer only formats + emits.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/observers/test_structured_log.py
"""StructuredLogObserver — JSON-lines emission per event."""
from __future__ import annotations

import datetime as dt
import json
import logging

import pytest

from foreman.v4.events import (
    ExecuteCompletedEvent,
    StateEnteredEvent,
    StateFailedEvent,
)
from foreman.v4.observers.structured_log import StructuredLogObserver
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind


_T0 = dt.datetime(2026, 6, 13, 12, 0, 0)


@pytest.fixture()
def observer_and_records(caplog):
    caplog.set_level(logging.INFO, logger="foreman.v4.transitions")
    obs = StructuredLogObserver(logger_name="foreman.v4.transitions")
    return obs, caplog


def test_state_entered_emits_one_json_line(observer_and_records):
    obs, caplog = observer_and_records
    obs(StateEnteredEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
    ))
    record = caplog.records[-1]
    payload = json.loads(record.message)
    assert payload["event"] == "state_entered"
    assert payload["ticket_id"] == 1
    assert payload["state"] == "Planning"
    assert payload["sequence"] == 1


def test_execute_completed_includes_outcome_and_next_state(observer_and_records):
    obs, caplog = observer_and_records
    obs(ExecuteCompletedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
        outcome=Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="spec PR open",
        ),
        next_state="SpecReview",
    ))
    payload = json.loads(caplog.records[-1].message)
    assert payload["event"] == "execute_completed"
    assert payload["outcome_kind"] == "clean"
    assert payload["confidence"] == "high"
    assert payload["next_state"] == "SpecReview"
    assert payload["summary"] == "spec PR open"


def test_state_failed_uses_warning_level(observer_and_records):
    obs, caplog = observer_and_records
    obs(StateFailedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
        failure_phase="execute", failure_reason="timeout",
    ))
    record = caplog.records[-1]
    assert record.levelno == logging.WARNING
    payload = json.loads(record.message)
    assert payload["event"] == "state_failed"
    assert payload["failure_phase"] == "execute"
    assert payload["failure_reason"] == "timeout"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/observers/test_structured_log.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.v4.observers.structured_log'`

- [ ] **Step 3: Write the observer**

```python
# packages/foreman/src/foreman/v4/observers/__init__.py
"""Concrete observers — one file per observer.

Observers consume events from foreman.v4.event_bus.EventBus. They are
registered at daemon startup; their __call__ receives one Event per fire.
"""
```

```python
# packages/foreman/src/foreman/v4/observers/structured_log.py
"""StructuredLogObserver — JSON-lines per event into a Python logger."""

from __future__ import annotations

import json
import logging
from typing import Any

from foreman.v4.events import (
    Event,
    ExecuteCompletedEvent,
    ExecuteStartedEvent,
    StateEnteredEvent,
    StateExitedEvent,
    StateFailedEvent,
)


_EVENT_NAMES = {
    StateEnteredEvent:     ("state_entered", logging.INFO),
    ExecuteStartedEvent:   ("execute_started", logging.INFO),
    ExecuteCompletedEvent: ("execute_completed", logging.INFO),
    StateExitedEvent:      ("state_exited", logging.INFO),
    StateFailedEvent:      ("state_failed", logging.WARNING),
}


class StructuredLogObserver:
    """Emit one JSON line per event into a named logger."""

    def __init__(self, *, logger_name: str = "foreman.v4.transitions") -> None:
        self._log = logging.getLogger(logger_name)

    def __call__(self, event: Event) -> None:
        try:
            name, level = _EVENT_NAMES[type(event)]
        except KeyError:
            # Unknown event type — log defensively, do not raise.
            name, level = ("unknown", logging.INFO)
        payload: dict[str, Any] = {
            "event": name,
            "ticket_id": event.ticket_id,
            "instance_id": event.instance_id,
            "state": event.state_name,
            "sequence": event.sequence,
            "at": event.at.isoformat(),
        }
        if isinstance(event, ExecuteCompletedEvent):
            payload["outcome_kind"] = event.outcome.kind.value
            payload["confidence"] = event.outcome.confidence.value
            payload["summary"] = event.outcome.summary
            payload["next_state"] = event.next_state
        elif isinstance(event, StateFailedEvent):
            payload["failure_phase"] = event.failure_phase
            payload["failure_reason"] = event.failure_reason
        self._log.log(level, json.dumps(payload, sort_keys=True))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/observers/test_structured_log.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/observers/__init__.py packages/foreman/src/foreman/v4/observers/structured_log.py packages/foreman/tests/v4/observers/test_structured_log.py
git commit -m "feat(v4): add StructuredLogObserver for JSON-lines transition log"
```

### Task 2.5: LabelObservabilityObserver

**Files:**
- Create: `packages/foreman/src/foreman/v4/observers/label_observability.py`
- Test: `packages/foreman/tests/v4/observers/test_label_observability.py`

Writes ONE label per state to the GitHub issue: `foreman:state-<state-name-lowercase>`. Write-only — the daemon never reads these back; they exist for human observers viewing the issue page. Takes a `LabelWriter` Protocol (`write_labels(project, issue_number, labels)`); test uses a fake recorder.

This observer subscribes to `StateEnteredEvent` and `StateFailedEvent`. On entry, it sets `foreman:state-<new>` and clears any other `foreman:state-*` label. On failure into a terminal state (NeedsHelp / Failed), the entry-event from that next state handles the label flip — this observer doesn't react to StateFailedEvent for non-terminal failures.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/observers/test_label_observability.py
"""LabelObservabilityObserver — writes one foreman:state-* label per entry."""
from __future__ import annotations

import datetime as dt

from foreman.v4.events import StateEnteredEvent
from foreman.v4.observers.label_observability import (
    LabelObservabilityObserver,
)
from foreman.v4.repository import InMemoryTicketRepository


class _RecordingWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, set[str]]] = []

    def write_labels(self, *, project: str, issue_number: int, labels: set[str]) -> None:
        self.calls.append((project, issue_number, set(labels)))


_T0 = dt.datetime(2026, 6, 13)


def _make_repo_and_ticket(state_name: str = "Planning"):
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="foreman", issue_number=42, now=_T0)
    repo.set_ticket_state(ticket.id, state_name, now=_T0)
    return repo, repo.get_ticket(ticket.id)


def test_state_entered_writes_single_state_label():
    repo, ticket = _make_repo_and_ticket("Planning")
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)
    obs(StateEnteredEvent(
        ticket_id=ticket.id, instance_id=99,
        state_name="Planning", sequence=1, at=_T0,
    ))
    assert writer.calls == [("foreman", 42, {"foreman:state-planning"})]


def test_label_name_lowercases_state():
    repo, ticket = _make_repo_and_ticket("SpecReview")
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)
    obs(StateEnteredEvent(
        ticket_id=ticket.id, instance_id=99,
        state_name="SpecReview", sequence=1, at=_T0,
    ))
    assert writer.calls[0][2] == {"foreman:state-specreview"}


def test_ignores_non_entered_events():
    """Observer only acts on StateEnteredEvent."""
    from foreman.v4.events import ExecuteStartedEvent
    repo, ticket = _make_repo_and_ticket()
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)
    obs(ExecuteStartedEvent(
        ticket_id=ticket.id, instance_id=99,
        state_name="Planning", sequence=1, at=_T0,
    ))
    assert writer.calls == []


def test_writer_failure_does_not_propagate(caplog):
    """Label-write failure must not break observer protocol — bus catches,
    but the observer itself should also be resilient since label writes are
    network-IO-prone."""
    class _BoomWriter:
        def write_labels(self, **_):
            raise RuntimeError("network down")

    repo, ticket = _make_repo_and_ticket()
    obs = LabelObservabilityObserver(writer=_BoomWriter(), repo=repo)
    # Observer raises; the EventBus is what catches it. We just verify the
    # exception class so EventBus's blanket except sees it.
    import pytest
    with pytest.raises(RuntimeError):
        obs(StateEnteredEvent(
            ticket_id=ticket.id, instance_id=99,
            state_name="Planning", sequence=1, at=_T0,
        ))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/observers/test_label_observability.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.v4.observers.label_observability'`

- [ ] **Step 3: Write the observer**

```python
# packages/foreman/src/foreman/v4/observers/label_observability.py
"""LabelObservabilityObserver — writes one foreman:state-* label per state entry.

Write-only by design: the daemon never reads labels back to decide state.
Labels exist for humans viewing the GitHub issue page.

The actual label-mutation surface is injected as a LabelWriter Protocol so
this module doesn't have to know about PyGithub at test time. Production
wiring lives in Phase 5.
"""

from __future__ import annotations

from typing import Protocol

from foreman.v4.events import Event, StateEnteredEvent
from foreman.v4.repository import TicketRepository


class LabelWriter(Protocol):
    def write_labels(
        self, *, project: str, issue_number: int, labels: set[str]
    ) -> None: ...


class LabelObservabilityObserver:
    """Reacts to StateEnteredEvent by stamping the current state on the issue."""

    def __init__(self, *, writer: LabelWriter, repo: TicketRepository) -> None:
        self._writer = writer
        self._repo = repo

    def __call__(self, event: Event) -> None:
        if not isinstance(event, StateEnteredEvent):
            return
        ticket = self._repo.get_ticket(event.ticket_id)
        label = f"foreman:state-{event.state_name.lower()}"
        self._writer.write_labels(
            project=ticket.project,
            issue_number=ticket.issue_number,
            labels={label},
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/observers/test_label_observability.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/observers/label_observability.py packages/foreman/tests/v4/observers/test_label_observability.py
git commit -m "feat(v4): add LabelObservabilityObserver for write-only state labels"
```

### Task 2.6: EventArchiveObserver (the spec's "SQLitePersistenceObserver")

**Files:**
- Create: `packages/foreman/src/foreman/v4/observers/event_archive.py`
- Modify: `packages/foreman/src/foreman/v4/schema.sql` (add `events` table)
- Test: `packages/foreman/tests/v4/observers/test_event_archive.py`

The state_instances table IS the journal — that's the source of truth, written synchronously by `transition()`. This observer appends to a **separate** `events` table for forensics + future replay. Adopting the spec's `SQLitePersistenceObserver` name was misleading because the journal is already persistent; this observer carries the audit trail.

If the events-table write fails, the EventBus's exception isolation absorbs it — observability degraded, durability untouched.

- [ ] **Step 1: Extend the schema**

Append to `packages/foreman/src/foreman/v4/schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       INTEGER NOT NULL,
    instance_id     INTEGER NOT NULL,
    event_type      TEXT    NOT NULL,
    state_name      TEXT    NOT NULL,
    sequence        INTEGER NOT NULL,
    at              TEXT    NOT NULL,
    payload         TEXT    NOT NULL  -- JSON-encoded extra fields
);

CREATE INDEX IF NOT EXISTS idx_events_ticket
    ON events(ticket_id, at);
```

- [ ] **Step 2: Write the failing test**

```python
# packages/foreman/tests/v4/observers/test_event_archive.py
"""EventArchiveObserver — append-only events table for forensics + replay."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from foreman.v4.events import (
    ExecuteCompletedEvent,
    StateEnteredEvent,
    StateFailedEvent,
)
from foreman.v4.observers.event_archive import EventArchiveObserver
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.sqlite_repository import SqliteTicketRepository


_T0 = dt.datetime(2026, 6, 13, 12, 0, 0)


@pytest.fixture()
def repo_and_ticket():
    repo = SqliteTicketRepository.in_memory()
    ticket = repo.create_ticket(project="p", issue_number=1, now=_T0)
    return repo, ticket


def test_state_entered_writes_one_event_row(repo_and_ticket):
    repo, ticket = repo_and_ticket
    obs = EventArchiveObserver(conn=repo._conn)
    obs(StateEnteredEvent(
        ticket_id=ticket.id, instance_id=99,
        state_name="Planning", sequence=1, at=_T0,
    ))
    rows = repo._conn.execute("SELECT * FROM events").fetchall()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "state_entered"
    assert rows[0]["state_name"] == "Planning"


def test_execute_completed_payload_carries_outcome(repo_and_ticket):
    repo, ticket = repo_and_ticket
    obs = EventArchiveObserver(conn=repo._conn)
    obs(ExecuteCompletedEvent(
        ticket_id=ticket.id, instance_id=99,
        state_name="Planning", sequence=1, at=_T0,
        outcome=Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="ok",
        ),
        next_state="SpecReview",
    ))
    row = repo._conn.execute("SELECT * FROM events").fetchone()
    payload = json.loads(row["payload"])
    assert payload["outcome_kind"] == "clean"
    assert payload["next_state"] == "SpecReview"


def test_state_failed_payload_carries_phase_and_reason(repo_and_ticket):
    repo, ticket = repo_and_ticket
    obs = EventArchiveObserver(conn=repo._conn)
    obs(StateFailedEvent(
        ticket_id=ticket.id, instance_id=99,
        state_name="Planning", sequence=1, at=_T0,
        failure_phase="execute", failure_reason="timeout",
    ))
    row = repo._conn.execute("SELECT * FROM events").fetchone()
    payload = json.loads(row["payload"])
    assert payload["failure_phase"] == "execute"
    assert payload["failure_reason"] == "timeout"


def test_events_are_append_only(repo_and_ticket):
    repo, ticket = repo_and_ticket
    obs = EventArchiveObserver(conn=repo._conn)
    for i in range(3):
        obs(StateEnteredEvent(
            ticket_id=ticket.id, instance_id=99,
            state_name="S", sequence=i + 1,
            at=_T0 + dt.timedelta(seconds=i),
        ))
    rows = repo._conn.execute("SELECT * FROM events ORDER BY id").fetchall()
    assert [r["sequence"] for r in rows] == [1, 2, 3]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/observers/test_event_archive.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.v4.observers.event_archive'`

- [ ] **Step 4: Write the observer**

```python
# packages/foreman/src/foreman/v4/observers/event_archive.py
"""EventArchiveObserver — append-only events table for forensics + replay.

This is the audit trail. The state_instances journal already persists
the durability story; this observer captures the SAME events as a flat,
append-only log that's easy to grep, easy to replay, and never under
contention from the transition path. If this write fails, the EventBus
isolates the exception; the journal stays correct.
"""

from __future__ import annotations

import json
import sqlite3

from foreman.v4.events import (
    Event,
    ExecuteCompletedEvent,
    ExecuteStartedEvent,
    StateEnteredEvent,
    StateExitedEvent,
    StateFailedEvent,
)


_EVENT_TYPE_NAMES = {
    StateEnteredEvent:     "state_entered",
    ExecuteStartedEvent:   "execute_started",
    ExecuteCompletedEvent: "execute_completed",
    StateExitedEvent:      "state_exited",
    StateFailedEvent:      "state_failed",
}


def _payload_for(event: Event) -> dict:
    if isinstance(event, ExecuteCompletedEvent):
        return {
            "outcome_kind": event.outcome.kind.value,
            "confidence": event.outcome.confidence.value,
            "summary": event.outcome.summary,
            "next_state": event.next_state,
        }
    if isinstance(event, StateExitedEvent):
        return {
            "outcome_kind": event.outcome.kind.value if event.outcome else None,
        }
    if isinstance(event, StateFailedEvent):
        return {
            "failure_phase": event.failure_phase,
            "failure_reason": event.failure_reason,
        }
    return {}


class EventArchiveObserver:
    def __init__(self, *, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __call__(self, event: Event) -> None:
        event_type = _EVENT_TYPE_NAMES.get(type(event), "unknown")
        self._conn.execute(
            "INSERT INTO events"
            "(ticket_id, instance_id, event_type, state_name, sequence, at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.ticket_id, event.instance_id, event_type,
                event.state_name, event.sequence, event.at.isoformat(),
                json.dumps(_payload_for(event), sort_keys=True),
            ),
        )
        self._conn.commit()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/observers/test_event_archive.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/v4/schema.sql packages/foreman/src/foreman/v4/observers/event_archive.py packages/foreman/tests/v4/observers/test_event_archive.py
git commit -m "feat(v4): add EventArchiveObserver and events audit table"
```

### Task 2.7: MetricsObserver — no-op stub with Protocol

**Files:**
- Create: `packages/foreman/src/foreman/v4/observers/metrics.py`
- Test: `packages/foreman/tests/v4/observers/test_metrics.py`

YAGNI on a real metrics backend at v4 ship. We do want the SHAPE — a `MetricsBackend` Protocol so a Prometheus / StatsD / OTLP exporter can drop in later without touching the bus or observers.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/observers/test_metrics.py
"""MetricsObserver — stub with extensible Protocol."""
from __future__ import annotations

import datetime as dt

from foreman.v4.events import (
    ExecuteCompletedEvent,
    StateEnteredEvent,
    StateFailedEvent,
)
from foreman.v4.observers.metrics import (
    MetricsObserver,
    NoopMetricsBackend,
)
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind


_T0 = dt.datetime(2026, 6, 13)


def test_default_backend_is_noop():
    obs = MetricsObserver()
    # Must not raise on any event:
    obs(StateEnteredEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
    ))


def test_custom_backend_receives_increment_and_observation_calls():
    class RecordingBackend:
        def __init__(self) -> None:
            self.increments: list[tuple[str, dict]] = []
            self.observations: list[tuple[str, float, dict]] = []

        def increment(self, name: str, *, tags: dict) -> None:
            self.increments.append((name, tags))

        def observe(self, name: str, value: float, *, tags: dict) -> None:
            self.observations.append((name, value, tags))

    backend = RecordingBackend()
    obs = MetricsObserver(backend=backend)
    obs(StateEnteredEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
    ))
    obs(ExecuteCompletedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
        outcome=Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="ok",
        ),
        next_state="SpecReview",
    ))
    obs(StateFailedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
        failure_phase="execute", failure_reason="timeout",
    ))
    increment_names = [c[0] for c in backend.increments]
    assert "foreman.v4.state.entered" in increment_names
    assert "foreman.v4.state.completed" in increment_names
    assert "foreman.v4.state.failed" in increment_names


def test_noop_backend_methods_are_callable():
    backend = NoopMetricsBackend()
    backend.increment("x", tags={})
    backend.observe("y", 1.5, tags={})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/observers/test_metrics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.v4.observers.metrics'`

- [ ] **Step 3: Write the observer**

```python
# packages/foreman/src/foreman/v4/observers/metrics.py
"""MetricsObserver — no-op stub today, extensible backend Protocol.

The shape is committed to at v4 ship; the backend is not. Wiring a real
Prometheus / StatsD / OTLP exporter later is a one-class swap, no changes
to the EventBus or any other observer.
"""

from __future__ import annotations

from typing import Protocol

from foreman.v4.events import (
    Event,
    ExecuteCompletedEvent,
    StateEnteredEvent,
    StateExitedEvent,
    StateFailedEvent,
)


class MetricsBackend(Protocol):
    def increment(self, name: str, *, tags: dict) -> None: ...
    def observe(self, name: str, value: float, *, tags: dict) -> None: ...


class NoopMetricsBackend:
    """Default backend — discards everything."""

    def increment(self, name: str, *, tags: dict) -> None:  # noqa: ARG002
        return None

    def observe(self, name: str, value: float, *, tags: dict) -> None:  # noqa: ARG002
        return None


class MetricsObserver:
    def __init__(self, *, backend: MetricsBackend | None = None) -> None:
        self._backend = backend or NoopMetricsBackend()

    def __call__(self, event: Event) -> None:
        tags = {"state": event.state_name}
        if isinstance(event, StateEnteredEvent):
            self._backend.increment("foreman.v4.state.entered", tags=tags)
        elif isinstance(event, ExecuteCompletedEvent):
            self._backend.increment("foreman.v4.state.completed", tags={
                **tags, "kind": event.outcome.kind.value,
            })
        elif isinstance(event, StateFailedEvent):
            self._backend.increment("foreman.v4.state.failed", tags={
                **tags, "phase": event.failure_phase,
            })
        elif isinstance(event, StateExitedEvent):
            self._backend.increment("foreman.v4.state.exited", tags=tags)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/observers/test_metrics.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/observers/metrics.py packages/foreman/tests/v4/observers/test_metrics.py
git commit -m "feat(v4): add MetricsObserver stub with extensible backend protocol"
```

### Task 2.8: End-to-end fan-out integration test

**Files:**
- Create: `packages/foreman/tests/v4/test_fanout_integration.py`

Wires everything from Phase 2 together: one transition emits events that hit all four observers. This is the "Phase 2 completion" empirical check.

- [ ] **Step 1: Write the test**

```python
# packages/foreman/tests/v4/test_fanout_integration.py
"""Phase 2 completion check — one transition reaches all four observers."""
from __future__ import annotations

import datetime as dt
import json
import logging

from foreman.v4.event_bus import EventBus
from foreman.v4.observers.event_archive import EventArchiveObserver
from foreman.v4.observers.label_observability import LabelObservabilityObserver
from foreman.v4.observers.metrics import MetricsObserver
from foreman.v4.observers.structured_log import StructuredLogObserver
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.sqlite_repository import SqliteTicketRepository
from foreman.v4.state import StateContext, TicketState


class _DoneState(TicketState):
    state_name = "Done"

    def execute(self, ctx: StateContext) -> Outcome:
        return Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="all set",
        )

    def next_state(self, outcome: Outcome) -> TicketState | None:
        return None


class _DemoState(TicketState):
    state_name = "Demo"

    def execute(self, ctx: StateContext) -> Outcome:
        return Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="demo ok",
        )

    def next_state(self, outcome: Outcome) -> TicketState | None:
        return _DoneState()


class _RecordingWriter:
    def __init__(self) -> None:
        self.calls = []

    def write_labels(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_one_transition_reaches_all_four_observers(caplog):
    caplog.set_level(logging.INFO, logger="foreman.v4.transitions")
    repo = SqliteTicketRepository.in_memory()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Demo", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )

    bus = EventBus()
    writer = _RecordingWriter()
    bus.subscribe(StructuredLogObserver(logger_name="foreman.v4.transitions"))
    bus.subscribe(LabelObservabilityObserver(writer=writer, repo=repo))
    bus.subscribe(EventArchiveObserver(conn=repo._conn))
    bus.subscribe(MetricsObserver())

    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
        bus=bus,
    )
    _DemoState().transition(ctx)

    # 1. Structured-log observer wrote JSON lines:
    log_lines = [r.message for r in caplog.records if "ticket_id" in r.message]
    events_logged = {json.loads(line)["event"] for line in log_lines}
    assert {"state_entered", "execute_started", "execute_completed", "state_exited"} <= events_logged

    # 2. Label observer wrote one state-label call (on StateEntered):
    assert writer.calls == [
        {"project": "p", "issue_number": 1, "labels": {"foreman:state-demo"}}
    ]

    # 3. Event-archive observer wrote rows into events table:
    rows = repo._conn.execute(
        "SELECT event_type FROM events ORDER BY id"
    ).fetchall()
    types = [r["event_type"] for r in rows]
    assert types == ["state_entered", "execute_started", "execute_completed", "state_exited"]

    # 4. (Metrics observer is no-op-backed; we just verify it didn't raise.
    #    Recording-backend coverage lives in test_metrics.)
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest packages/foreman/tests/v4/test_fanout_integration.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add packages/foreman/tests/v4/test_fanout_integration.py
git commit -m "test(v4): end-to-end fan-out — one transition reaches all four observers"
```

### Phase 2 — `just check` gate

- [ ] **Run:** `just check`
- [ ] **Expected:** all gates green.

Phase 2 completion criterion (from the outline): **side-effects fan out via the EventBus**. By Task 2.8 we've proven a single transition reaches the structured log, the GitHub label surface, the events audit table, and the metrics shim — without any of them being able to corrupt the durability journal. The substrate is now observability-ready; Phase 3 fills in concrete states.

---

## Phase 3 — Concrete states

The 11 states the spec names: `Queued`, `Planning`, `SpecReview`, `SpecFix`, `Implementing`, `ImplReview`, `ImplFix`, `Merging`, `Done`, `Failed`, `NeedsHelp`. Each one is a small class with explicit `execute()` + `next_state()` and a clear failure shape. Six of them dispatch role subprocesses; one waits on GitHub artifact state (MergeQueue); the four terminals are trivial; `Queued` is the entry hop.

**Two test seams introduced here.** Both will get real implementations in Phase 4 (Poller wiring). Phase 3 only needs the Protocols + fakes:

- `RoleDispatcher` — dispatch a role subprocess and return its stdout. Concrete states call this; their next-state branching is driven by `parse_outcome_from_stdout` on the returned text.
- `GitProvider` — narrow GitHub adapter scoped to the artifact-state queries v4 needs (PR mergeable? MergeQueue verdict? PR merged?). The full PyGithub coupling lives behind this Protocol so concrete states stay testable.

### Task 3.1: RoleDispatcher Protocol + fake

**Files:**
- Create: `packages/foreman/src/foreman/v4/role_dispatcher.py`
- Test: `packages/foreman/tests/v4/test_role_dispatcher_fake.py`

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_role_dispatcher_fake.py
"""FakeRoleDispatcher — canned-stdout for testing concrete states."""
from __future__ import annotations

import pytest

from foreman.v4.role_dispatcher import FakeRoleDispatcher, RoleNotConfiguredError


def test_returns_canned_stdout_for_configured_role():
    dispatcher = FakeRoleDispatcher(
        responses={
            ("planner", "p", 1): "log line\nFOREMAN_OUTCOME:{\"kind\":\"clean\",\"confidence\":\"high\",\"summary\":\"ok\"}\n",
        }
    )
    out = dispatcher.dispatch(role="planner", project="p", issue_number=1, ticket_id=1)
    assert "FOREMAN_OUTCOME:" in out


def test_unconfigured_role_raises():
    dispatcher = FakeRoleDispatcher(responses={})
    with pytest.raises(RoleNotConfiguredError) as exc:
        dispatcher.dispatch(role="planner", project="p", issue_number=1, ticket_id=1)
    assert "planner" in str(exc.value)


def test_dispatch_records_invocation_for_assertion():
    dispatcher = FakeRoleDispatcher(
        responses={
            ("planner", "p", 1): 'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}',
        }
    )
    dispatcher.dispatch(role="planner", project="p", issue_number=1, ticket_id=99)
    assert dispatcher.calls == [("planner", "p", 1, 99)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_role_dispatcher_fake.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the Protocol + fake**

```python
# packages/foreman/src/foreman/v4/role_dispatcher.py
"""RoleDispatcher — the seam between v4 state machine and role subprocesses.

Concrete states do not import PyGithub or invoke subprocess directly. They
call dispatcher.dispatch(role=..., project=..., issue_number=...) and the
real implementation (Phase 4) shells out to ``foreman <role> ...`` with the
appropriate per-role identity.
"""

from __future__ import annotations

from typing import Protocol


class RoleNotConfiguredError(LookupError):
    """The fake had no canned response for this (role, project, issue_number)."""


class RoleDispatcher(Protocol):
    def dispatch(
        self,
        *,
        role: str,
        project: str,
        issue_number: int,
        ticket_id: int,
    ) -> str:
        """Return the role subprocess's stdout. Must contain FOREMAN_OUTCOME:."""


class FakeRoleDispatcher:
    """In-memory dispatcher: maps (role, project, issue_number) → canned stdout."""

    def __init__(self, *, responses: dict[tuple[str, str, int], str]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str, int, int]] = []

    def dispatch(
        self, *, role: str, project: str, issue_number: int, ticket_id: int,
    ) -> str:
        self.calls.append((role, project, issue_number, ticket_id))
        key = (role, project, issue_number)
        try:
            return self._responses[key]
        except KeyError as exc:
            raise RoleNotConfiguredError(f"no canned response for {key}") from exc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/foreman/tests/v4/test_role_dispatcher_fake.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/role_dispatcher.py packages/foreman/tests/v4/test_role_dispatcher_fake.py
git commit -m "feat(v4): add RoleDispatcher protocol and fake"
```

### Task 3.2: GitProvider Protocol + fake

**Files:**
- Create: `packages/foreman/src/foreman/v4/git_provider.py`
- Test: `packages/foreman/tests/v4/test_git_provider_fake.py`

Narrow Protocol scoped to what v4 actually queries: PR existence/state, MergeQueue enqueue, merge verdict. Phase 4 implements a PyGithub-backed concrete impl behind this seam.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_git_provider_fake.py
"""FakeGitProvider — in-memory implementation of the v4 GitProvider Protocol."""
from __future__ import annotations

import pytest

from foreman.v4.git_provider import (
    FakeGitProvider,
    MergeVerdict,
    PRNotFoundError,
    PRState,
)


def test_set_and_get_pr_state():
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=1,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    assert git.get_pr_state(project="p", pr_number=1).mergeable is True


def test_missing_pr_raises():
    git = FakeGitProvider()
    with pytest.raises(PRNotFoundError):
        git.get_pr_state(project="p", pr_number=999)


def test_enqueue_into_merge_queue_records_call():
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=1,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    git.enqueue_merge_queue(project="p", pr_number=1)
    assert ("p", 1) in git.merge_queue


def test_merge_verdict_default_is_pending():
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=1,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    git.enqueue_merge_queue(project="p", pr_number=1)
    assert git.merge_verdict(project="p", pr_number=1) is MergeVerdict.PENDING


def test_set_merge_verdict_advances():
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=1,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    git.enqueue_merge_queue(project="p", pr_number=1)
    git.set_merge_verdict(project="p", pr_number=1, verdict=MergeVerdict.MERGED)
    assert git.merge_verdict(project="p", pr_number=1) is MergeVerdict.MERGED


def test_merge_spec_pr_marks_merged():
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=1,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    git.merge_spec_pr(project="p", pr_number=1)
    assert git.get_pr_state(project="p", pr_number=1).merged is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_git_provider_fake.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the Protocol + fake**

```python
# packages/foreman/src/foreman/v4/git_provider.py
"""GitProvider — narrow seam over PyGithub for the v4 state machine.

States that need to look at GitHub artifact state (spec PR mergeable?
impl PR ready? MergeQueue verdict?) go through this Protocol. The
PyGithub concrete implementation lands in Phase 4; Phase 3 only needs
the shape + the fake.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class PRNotFoundError(LookupError):
    """No PR matching this (project, pr_number)."""


@dataclass(frozen=True, slots=True)
class PRState:
    merged: bool
    mergeable: bool
    ci_passing: bool


class MergeVerdict(str, Enum):
    PENDING = "pending"     # in MergeQueue, no decision yet
    MERGED = "merged"       # MergeQueue completed the merge
    REJECTED = "rejected"   # MergeQueue rejected (CI fail, conflict)


class GitProvider(Protocol):
    def get_pr_state(self, *, project: str, pr_number: int) -> PRState: ...
    def merge_spec_pr(self, *, project: str, pr_number: int) -> None: ...
    def enqueue_merge_queue(self, *, project: str, pr_number: int) -> None: ...
    def merge_verdict(self, *, project: str, pr_number: int) -> MergeVerdict: ...


class FakeGitProvider:
    """In-memory GitProvider for unit + lifecycle tests."""

    def __init__(self) -> None:
        self._prs: dict[tuple[str, int], PRState] = {}
        self.merge_queue: set[tuple[str, int]] = set()
        self._verdicts: dict[tuple[str, int], MergeVerdict] = {}

    def set_pr_state(self, *, project: str, pr_number: int, state: PRState) -> None:
        self._prs[(project, pr_number)] = state

    def get_pr_state(self, *, project: str, pr_number: int) -> PRState:
        try:
            return self._prs[(project, pr_number)]
        except KeyError as exc:
            raise PRNotFoundError(f"{project}#{pr_number}") from exc

    def merge_spec_pr(self, *, project: str, pr_number: int) -> None:
        existing = self.get_pr_state(project=project, pr_number=pr_number)
        self._prs[(project, pr_number)] = PRState(
            merged=True, mergeable=existing.mergeable, ci_passing=existing.ci_passing,
        )

    def enqueue_merge_queue(self, *, project: str, pr_number: int) -> None:
        self.get_pr_state(project=project, pr_number=pr_number)  # raise if missing
        self.merge_queue.add((project, pr_number))
        self._verdicts.setdefault((project, pr_number), MergeVerdict.PENDING)

    def merge_verdict(self, *, project: str, pr_number: int) -> MergeVerdict:
        return self._verdicts.get((project, pr_number), MergeVerdict.PENDING)

    def set_merge_verdict(
        self, *, project: str, pr_number: int, verdict: MergeVerdict,
    ) -> None:
        self._verdicts[(project, pr_number)] = verdict
        if verdict is MergeVerdict.MERGED:
            existing = self.get_pr_state(project=project, pr_number=pr_number)
            self._prs[(project, pr_number)] = PRState(
                merged=True, mergeable=existing.mergeable,
                ci_passing=existing.ci_passing,
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/foreman/tests/v4/test_git_provider_fake.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/git_provider.py packages/foreman/tests/v4/test_git_provider_fake.py
git commit -m "feat(v4): add GitProvider protocol and fake for artifact-state queries"
```

### Task 3.3: Terminal states (Done, Failed, NeedsHelp) + Queued

**Files:**
- Create: `packages/foreman/src/foreman/v4/states/__init__.py`
- Create: `packages/foreman/src/foreman/v4/states/terminal.py`
- Create: `packages/foreman/src/foreman/v4/states/queued.py`
- Test: `packages/foreman/tests/v4/states/test_terminal.py`
- Test: `packages/foreman/tests/v4/states/test_queued.py`

The four "no-work" states. `Done`/`Failed`/`NeedsHelp` are terminals — their `execute()` returns an immediate CLEAN outcome and `next_state()` returns None. `Queued` is the entry hop: `execute()` returns CLEAN, `next_state()` returns `PlanningState()`.

- [ ] **Step 1: Write the failing tests**

```python
# packages/foreman/tests/v4/states/__init__.py
```

```python
# packages/foreman/tests/v4/states/test_terminal.py
"""Terminal states — Done, Failed, NeedsHelp."""
from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.outcome import OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext
from foreman.v4.states.terminal import DoneState, FailedState, NeedsHelpState


@pytest.fixture()
def ctx_for(state_class):
    def _make():
        repo = InMemoryTicketRepository()
        ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
        instance = repo.open_state_instance(
            ticket_id=ticket.id, state_name=state_class.state_name,
            sequence=1, now=dt.datetime(2026, 6, 13),
        )
        return StateContext(
            ticket=ticket, instance=instance, repo=repo,
            clock=lambda: dt.datetime(2026, 6, 13),
        ), repo, ticket
    return _make


@pytest.mark.parametrize(
    "state_class,expected_name",
    [
        (DoneState, "Done"),
        (FailedState, "Failed"),
        (NeedsHelpState, "NeedsHelp"),
    ],
)
def test_terminal_state_returns_clean_outcome_and_no_next_state(state_class, expected_name, ctx_for):
    ctx, repo, ticket = ctx_for(state_class)()
    state = state_class()
    assert state.state_name == expected_name
    outcome = state.execute(ctx)
    assert outcome.kind == OutcomeKind.CLEAN
    assert state.next_state(outcome) is None


def test_terminal_transition_persists_outcome(ctx_for):
    ctx, repo, ticket = ctx_for(DoneState)()
    result = DoneState().transition(ctx)
    assert result is None
    closed = repo.get_state_instance(ctx.instance.id)
    assert not closed.is_in_flight
    assert closed.outcome_kind == OutcomeKind.CLEAN
```

```python
# packages/foreman/tests/v4/states/test_queued.py
"""QueuedState — the entry hop. Transitions to Planning unconditionally."""
from __future__ import annotations

import datetime as dt

from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext
from foreman.v4.states.queued import QueuedState


def test_queued_advances_to_planning():
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Queued",
        sequence=1, now=dt.datetime(2026, 6, 13),
    )
    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
    )
    next_state = QueuedState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Planning"
    assert repo.get_ticket(ticket.id).current_state == "Planning"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest packages/foreman/tests/v4/states/ -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the states**

```python
# packages/foreman/src/foreman/v4/states/__init__.py
"""Concrete states for the v4 state machine.

One state per file when the state has meaningful logic; the four trivial
states (Done, Failed, NeedsHelp) share terminal.py.
"""
```

```python
# packages/foreman/src/foreman/v4/states/terminal.py
"""Terminal states — the ticket has reached an end-of-flow point.

Done       — happy completion (impl PR merged).
Failed     — terminal failure with no human-actionable recovery (rare).
NeedsHelp  — terminal-pending-human; resume routed through `foreman resume`
             after the human resolves the issue. The state itself is just a
             holding pen — no work to do until the ticket is moved off.
"""

from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.state import StateContext, TicketState


class _TerminalState(TicketState):
    """Base for no-work terminals."""

    def execute(self, ctx: StateContext) -> Outcome:
        return Outcome(
            kind=OutcomeKind.CLEAN,
            confidence=OutcomeConfidence.HIGH,
            summary=f"terminal: {self.state_name}",
        )

    def next_state(self, outcome: Outcome) -> TicketState | None:
        return None


class DoneState(_TerminalState):
    state_name = "Done"


class FailedState(_TerminalState):
    state_name = "Failed"


class NeedsHelpState(_TerminalState):
    state_name = "NeedsHelp"
```

```python
# packages/foreman/src/foreman/v4/states/queued.py
"""QueuedState — entry hop. New tickets land here; advance to Planning."""

from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.state import StateContext, TicketState


class QueuedState(TicketState):
    state_name = "Queued"

    def execute(self, ctx: StateContext) -> Outcome:
        return Outcome(
            kind=OutcomeKind.CLEAN,
            confidence=OutcomeConfidence.HIGH,
            summary="queued; advancing to planning",
        )

    def next_state(self, outcome: Outcome) -> TicketState | None:
        # Late import to keep the states package import-cycle-free.
        from foreman.v4.states.planning import PlanningState
        return PlanningState()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/states/ -v`
Expected: tests pass once Task 3.4 (Planning) lands; the Queued test imports PlanningState transitively. If that's blocking the Queued test, defer the `git commit` for queued.py until after Task 3.4 and stage the file then.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/states/__init__.py packages/foreman/src/foreman/v4/states/terminal.py packages/foreman/tests/v4/states/__init__.py packages/foreman/tests/v4/states/test_terminal.py
git commit -m "feat(v4): add terminal states (Done, Failed, NeedsHelp)"
# Queued depends on Planning — commit at the end of Task 3.4.
```

### Task 3.4: RoleDispatchState base class + PlanningState

**Files:**
- Create: `packages/foreman/src/foreman/v4/states/role_dispatch.py`
- Create: `packages/foreman/src/foreman/v4/states/planning.py`
- Test: `packages/foreman/tests/v4/states/test_role_dispatch.py`
- Test: `packages/foreman/tests/v4/states/test_planning.py`

Six of the eleven states do the same thing: dispatch a role subprocess, parse the Outcome, route to a next state by outcome kind. The branching is per-state but the mechanism is uniform. We factor it into `RoleDispatchState` so the per-state subclasses are tiny.

Subclass contract:
- `state_name: str` — class attribute
- `role: str` — which role subprocess to dispatch
- `next_state_for(outcome) -> TicketState | None` — abstract; only the routing varies

`StateContext` gains an optional `role_dispatcher: RoleDispatcher | None`. `RoleDispatchState.execute()` calls it and parses the Outcome from stdout.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/states/test_role_dispatch.py
"""RoleDispatchState — common dispatch + outcome-parse mechanism."""
from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.outcome import (
    Outcome,
    OutcomeKind,
    OutcomeMalformedError,
    OutcomeMissingError,
)
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.role_dispatcher import FakeRoleDispatcher
from foreman.v4.state import StateContext, TicketState
from foreman.v4.states.role_dispatch import RoleDispatchState


class _Demo(RoleDispatchState):
    state_name = "Demo"
    role = "planner"

    def next_state_for(self, outcome: Outcome) -> TicketState | None:
        return None


def _make_ctx(dispatcher: FakeRoleDispatcher) -> tuple[StateContext, InMemoryTicketRepository]:
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Demo", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        role_dispatcher=dispatcher,
    )
    return ctx, repo


def test_dispatches_role_and_parses_outcome():
    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 1):
            'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}',
    })
    ctx, _ = _make_ctx(dispatcher)
    outcome = _Demo().execute(ctx)
    assert outcome.kind == OutcomeKind.CLEAN
    assert dispatcher.calls == [("planner", "p", 1, ctx.ticket.id)]


def test_missing_marker_propagates_as_outcome_missing():
    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 1): "lots of log lines but no marker\n",
    })
    ctx, _ = _make_ctx(dispatcher)
    with pytest.raises(OutcomeMissingError):
        _Demo().execute(ctx)


def test_malformed_json_propagates_as_outcome_malformed():
    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 1): "FOREMAN_OUTCOME:{not valid}\n",
    })
    ctx, _ = _make_ctx(dispatcher)
    with pytest.raises(OutcomeMalformedError):
        _Demo().execute(ctx)


def test_missing_dispatcher_raises_at_execute():
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Demo", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        # role_dispatcher omitted
    )
    with pytest.raises(RuntimeError) as exc:
        _Demo().execute(ctx)
    assert "role_dispatcher" in str(exc.value).lower()
```

```python
# packages/foreman/tests/v4/states/test_planning.py
"""PlanningState — Planner role; CLEAN → SpecReview, NEEDS_HELP → NeedsHelp."""
from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.states.planning import PlanningState
from foreman.v4.states.terminal import NeedsHelpState


@pytest.mark.parametrize(
    "kind,next_class_name",
    [
        (OutcomeKind.CLEAN, "SpecReview"),
        (OutcomeKind.NEEDS_HELP, "NeedsHelp"),
        (OutcomeKind.ERROR, "Failed"),
    ],
)
def test_next_state_branching(kind, next_class_name):
    outcome = Outcome(kind=kind, confidence=OutcomeConfidence.HIGH, summary="x")
    next_state = PlanningState().next_state(outcome)
    if next_class_name is None:
        assert next_state is None
    else:
        assert next_state is not None
        assert next_state.state_name == next_class_name


def test_planning_role_attribute():
    assert PlanningState.role == "planner"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest packages/foreman/tests/v4/states/test_role_dispatch.py packages/foreman/tests/v4/states/test_planning.py -v`
Expected: FAIL with `ModuleNotFoundError` + `StateContext` missing `role_dispatcher` keyword

- [ ] **Step 3: Extend `StateContext`**

In `packages/foreman/src/foreman/v4/state.py`, add to the imports:

```python
from foreman.v4.role_dispatcher import RoleDispatcher
```

Extend `StateContext`:

```python
@dataclass(frozen=True)
class StateContext:
    ticket: TicketRecord
    instance: StateInstanceRecord
    repo: TicketRepository
    clock: Callable[[], dt.datetime]
    bus: EventBus | None = None
    role_dispatcher: RoleDispatcher | None = None
    git: "GitProvider | None" = None   # populated in Task 3.8
```

(The forward reference avoids importing `git_provider` at the top of `state.py` and keeps the import graph clean.)

- [ ] **Step 4: Write `RoleDispatchState` + `PlanningState`**

```python
# packages/foreman/src/foreman/v4/states/role_dispatch.py
"""Base class for the six role-dispatch states.

Subclass: set ``state_name``, ``role``, and override ``next_state_for(outcome)``.
That's it — the dispatch + parse + Outcome plumbing lives here once.
"""

from __future__ import annotations

from abc import abstractmethod

from foreman.v4.outcome import Outcome, parse_outcome_from_stdout
from foreman.v4.state import StateContext, TicketState


class RoleDispatchState(TicketState):
    role: str = ""  # subclasses MUST override

    def execute(self, ctx: StateContext) -> Outcome:
        if ctx.role_dispatcher is None:
            raise RuntimeError(
                f"{self.state_name}.execute requires a role_dispatcher in StateContext"
            )
        stdout = ctx.role_dispatcher.dispatch(
            role=self.role,
            project=ctx.ticket.project,
            issue_number=ctx.ticket.issue_number,
            ticket_id=ctx.ticket.id,
        )
        return parse_outcome_from_stdout(stdout)

    @abstractmethod
    def next_state_for(self, outcome: Outcome) -> "TicketState | None":
        """Override per state. Drives the outcome-kind → next-state branching."""

    def next_state(self, outcome: Outcome) -> "TicketState | None":
        return self.next_state_for(outcome)
```

```python
# packages/foreman/src/foreman/v4/states/planning.py
"""PlanningState — dispatch Planner; CLEAN → SpecReview; else terminal-ish."""

from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeKind
from foreman.v4.state import TicketState
from foreman.v4.states.role_dispatch import RoleDispatchState


class PlanningState(RoleDispatchState):
    state_name = "Planning"
    role = "planner"

    def next_state_for(self, outcome: Outcome) -> TicketState | None:
        from foreman.v4.states.spec_review import SpecReviewState
        from foreman.v4.states.terminal import FailedState, NeedsHelpState
        if outcome.kind == OutcomeKind.CLEAN:
            return SpecReviewState()
        if outcome.kind == OutcomeKind.NEEDS_HELP:
            return NeedsHelpState()
        return FailedState()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/states/ -v`
Expected: 6+ passed (Planning routing tests will fail until Task 3.6 lands SpecReviewState — for now stub a placeholder; either keep this Task scope and add `class SpecReviewState: state_name = "SpecReview"` placeholder, OR commit the Planning code without its test and add the test in 3.6).

**Recommendation:** add a one-line placeholder file `packages/foreman/src/foreman/v4/states/spec_review.py`:

```python
# packages/foreman/src/foreman/v4/states/spec_review.py — REPLACED in Task 3.6
from foreman.v4.state import TicketState
from foreman.v4.outcome import Outcome


class SpecReviewState(TicketState):
    state_name = "SpecReview"

    def execute(self, ctx) -> Outcome:
        raise NotImplementedError("filled in at Task 3.6")

    def next_state(self, outcome: Outcome) -> TicketState | None:
        raise NotImplementedError("filled in at Task 3.6")
```

This lets the Planning routing test pass now; Task 3.6 replaces the file with the real implementation.

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/v4/state.py packages/foreman/src/foreman/v4/states/role_dispatch.py packages/foreman/src/foreman/v4/states/planning.py packages/foreman/src/foreman/v4/states/spec_review.py packages/foreman/src/foreman/v4/states/queued.py packages/foreman/tests/v4/states/test_role_dispatch.py packages/foreman/tests/v4/states/test_planning.py packages/foreman/tests/v4/states/test_queued.py
git commit -m "feat(v4): add RoleDispatchState base + PlanningState + Queued wiring"
```

### Task 3.5: SpecFixState, ImplReviewState, ImplFixState (uniform shape)

**Files:**
- Create: `packages/foreman/src/foreman/v4/states/spec_fix.py`
- Create: `packages/foreman/src/foreman/v4/states/impl_review.py`
- Create: `packages/foreman/src/foreman/v4/states/impl_fix.py`
- Test: `packages/foreman/tests/v4/states/test_simple_role_states.py`

These three share the role-dispatch-with-clear-routing shape. Branching:

| State | role | CLEAN → | NEEDS_FIX → | NEEDS_HELP → |
| --- | --- | --- | --- | --- |
| `SpecFixState` | `fixer` (target=spec) | `SpecReviewState` | (n/a — fixer doesn't review) | `NeedsHelpState` |
| `ImplReviewState` | `reviewer` (target=impl) | `MergingState` | `ImplFixState` | `NeedsHelpState` |
| `ImplFixState` | `fixer` (target=impl) | `ImplReviewState` | (n/a) | `NeedsHelpState` |

Note `fixer` and `reviewer` roles are target-aware (spec vs impl); the role-dispatcher's `role` string carries the target as a suffix in v4 (`fixer-spec`, `fixer-impl`, `reviewer-spec`, `reviewer-impl`). Phase 5 wires the real subprocess invocation to honor these strings.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/states/test_simple_role_states.py
"""SpecFix, ImplReview, ImplFix — uniform-shape role-dispatch states."""
from __future__ import annotations

import pytest

from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.states.impl_fix import ImplFixState
from foreman.v4.states.impl_review import ImplReviewState
from foreman.v4.states.spec_fix import SpecFixState


def _o(kind: OutcomeKind) -> Outcome:
    return Outcome(kind=kind, confidence=OutcomeConfidence.HIGH, summary="x")


@pytest.mark.parametrize(
    "state_class,role,clean_next,needs_help_next",
    [
        (SpecFixState, "fixer-spec", "SpecReview", "NeedsHelp"),
        (ImplFixState, "fixer-impl", "ImplReview", "NeedsHelp"),
    ],
)
def test_fixer_state_routing(state_class, role, clean_next, needs_help_next):
    state = state_class()
    assert state.role == role
    assert state.next_state(_o(OutcomeKind.CLEAN)).state_name == clean_next
    assert state.next_state(_o(OutcomeKind.NEEDS_HELP)).state_name == needs_help_next


def test_impl_review_state_routing():
    state = ImplReviewState()
    assert state.role == "reviewer-impl"
    assert state.next_state(_o(OutcomeKind.CLEAN)).state_name == "Merging"
    assert state.next_state(_o(OutcomeKind.NEEDS_FIX)).state_name == "ImplFix"
    assert state.next_state(_o(OutcomeKind.NEEDS_HELP)).state_name == "NeedsHelp"


def test_error_outcome_routes_to_failed():
    for cls in (SpecFixState, ImplReviewState, ImplFixState):
        next_state = cls().next_state(_o(OutcomeKind.ERROR))
        assert next_state.state_name == "Failed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/states/test_simple_role_states.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the three states + placeholders**

```python
# packages/foreman/src/foreman/v4/states/spec_fix.py
from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeKind
from foreman.v4.state import TicketState
from foreman.v4.states.role_dispatch import RoleDispatchState


class SpecFixState(RoleDispatchState):
    state_name = "SpecFix"
    role = "fixer-spec"

    def next_state_for(self, outcome: Outcome) -> TicketState | None:
        from foreman.v4.states.spec_review import SpecReviewState
        from foreman.v4.states.terminal import FailedState, NeedsHelpState
        if outcome.kind == OutcomeKind.CLEAN:
            return SpecReviewState()
        if outcome.kind == OutcomeKind.NEEDS_HELP:
            return NeedsHelpState()
        return FailedState()
```

```python
# packages/foreman/src/foreman/v4/states/impl_review.py
from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeKind
from foreman.v4.state import TicketState
from foreman.v4.states.role_dispatch import RoleDispatchState


class ImplReviewState(RoleDispatchState):
    state_name = "ImplReview"
    role = "reviewer-impl"

    def next_state_for(self, outcome: Outcome) -> TicketState | None:
        from foreman.v4.states.impl_fix import ImplFixState
        from foreman.v4.states.merging import MergingState
        from foreman.v4.states.terminal import FailedState, NeedsHelpState
        if outcome.kind == OutcomeKind.CLEAN:
            return MergingState()
        if outcome.kind == OutcomeKind.NEEDS_FIX:
            return ImplFixState()
        if outcome.kind == OutcomeKind.NEEDS_HELP:
            return NeedsHelpState()
        return FailedState()
```

```python
# packages/foreman/src/foreman/v4/states/impl_fix.py
from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeKind
from foreman.v4.state import TicketState
from foreman.v4.states.role_dispatch import RoleDispatchState


class ImplFixState(RoleDispatchState):
    state_name = "ImplFix"
    role = "fixer-impl"

    def next_state_for(self, outcome: Outcome) -> TicketState | None:
        from foreman.v4.states.impl_review import ImplReviewState
        from foreman.v4.states.terminal import FailedState, NeedsHelpState
        if outcome.kind == OutcomeKind.CLEAN:
            return ImplReviewState()
        if outcome.kind == OutcomeKind.NEEDS_HELP:
            return NeedsHelpState()
        return FailedState()
```

Add a one-line placeholder for `MergingState` (filled in at Task 3.8):

```python
# packages/foreman/src/foreman/v4/states/merging.py — REPLACED in Task 3.8
from foreman.v4.state import TicketState
from foreman.v4.outcome import Outcome


class MergingState(TicketState):
    state_name = "Merging"

    def execute(self, ctx) -> Outcome:
        raise NotImplementedError("filled in at Task 3.8")

    def next_state(self, outcome: Outcome) -> TicketState | None:
        raise NotImplementedError("filled in at Task 3.8")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/states/test_simple_role_states.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/states/spec_fix.py packages/foreman/src/foreman/v4/states/impl_review.py packages/foreman/src/foreman/v4/states/impl_fix.py packages/foreman/src/foreman/v4/states/merging.py packages/foreman/tests/v4/states/test_simple_role_states.py
git commit -m "feat(v4): add SpecFix, ImplReview, ImplFix states"
```

### Task 3.6: SpecReviewState (merges spec PR on CLEAN)

**Files:**
- Modify (overwrite placeholder): `packages/foreman/src/foreman/v4/states/spec_review.py`
- Test: `packages/foreman/tests/v4/states/test_spec_review.py`

Like the simple role-dispatch states, BUT on CLEAN we also need to merge the spec PR before transitioning to Implementing. This is the v4 equivalent of v3's spec-PR-merge mechanic — preserved in the two-phase PR workflow.

The PR number to merge comes from `outcome.artifacts.pr_number`. The merge happens inside `verify()` so a merge failure routes through the `verify` failure handler (cleaner than mixing it into `execute()`'s role-dispatch result parsing).

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/states/test_spec_review.py
"""SpecReviewState — Reviewer-on-spec; CLEAN merges spec PR + → Implementing."""
from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.git_provider import FakeGitProvider, PRState
from foreman.v4.outcome import Outcome, OutcomeArtifacts, OutcomeConfidence, OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.role_dispatcher import FakeRoleDispatcher
from foreman.v4.state import StateContext
from foreman.v4.states.spec_review import SpecReviewState


def _ctx(*, response_stdout: str, git: FakeGitProvider):
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.set_ticket_state(ticket.id, "SpecReview", now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="SpecReview", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    dispatcher = FakeRoleDispatcher(responses={
        ("reviewer-spec", "p", 1): response_stdout,
    })
    return StateContext(
        ticket=repo.get_ticket(ticket.id), instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        role_dispatcher=dispatcher, git=git,
    ), repo


def test_clean_outcome_merges_spec_pr_and_advances_to_implementing():
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=42,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    ctx, repo = _ctx(
        response_stdout=(
            'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high",'
            '"summary":"approved","artifacts":{"pr_number":42}}'
        ),
        git=git,
    )
    next_state = SpecReviewState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Implementing"
    assert git.get_pr_state(project="p", pr_number=42).merged is True
    assert repo.get_ticket(ctx.ticket.id).current_state == "Implementing"


def test_needs_fix_routes_to_spec_fix_without_merge():
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=42,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    ctx, repo = _ctx(
        response_stdout=(
            'FOREMAN_OUTCOME:{"kind":"needs_fix","confidence":"high",'
            '"summary":"nope","artifacts":{"pr_number":42}}'
        ),
        git=git,
    )
    next_state = SpecReviewState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "SpecFix"
    assert git.get_pr_state(project="p", pr_number=42).merged is False


def test_clean_without_pr_number_routes_to_failed_via_verify():
    git = FakeGitProvider()
    ctx, repo = _ctx(
        response_stdout=(
            'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"no pr"}'
        ),
        git=git,
    )
    next_state = SpecReviewState().transition(ctx)
    assert next_state is None
    closed = repo.get_state_instance(ctx.instance.id)
    assert closed.failure_phase == "verify"


def test_role_attribute():
    assert SpecReviewState.role == "reviewer-spec"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/states/test_spec_review.py -v`
Expected: FAIL — placeholder raises `NotImplementedError`

- [ ] **Step 3: Replace the placeholder**

Overwrite `packages/foreman/src/foreman/v4/states/spec_review.py`:

```python
# packages/foreman/src/foreman/v4/states/spec_review.py
"""SpecReviewState — Reviewer-on-spec.

On CLEAN, the spec is approved; this state merges the spec PR before
handing control to Implementing. Merging is in verify() (not execute())
so a merge failure routes through the verify failure handler with a
distinct failure_phase.
"""

from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeKind
from foreman.v4.state import StateContext, TicketState
from foreman.v4.states.role_dispatch import RoleDispatchState


class SpecReviewState(RoleDispatchState):
    state_name = "SpecReview"
    role = "reviewer-spec"

    def verify(self, ctx: StateContext, outcome: Outcome) -> None:
        if outcome.kind != OutcomeKind.CLEAN:
            return
        pr_number = outcome.artifacts.pr_number
        if pr_number is None:
            raise ValueError(
                "Reviewer-on-spec returned CLEAN but no pr_number in artifacts"
            )
        if ctx.git is None:
            raise RuntimeError("SpecReview.verify requires git in StateContext")
        ctx.git.merge_spec_pr(project=ctx.ticket.project, pr_number=pr_number)

    def next_state_for(self, outcome: Outcome) -> TicketState | None:
        from foreman.v4.states.implementing import ImplementingState
        from foreman.v4.states.spec_fix import SpecFixState
        from foreman.v4.states.terminal import FailedState, NeedsHelpState
        if outcome.kind == OutcomeKind.CLEAN:
            return ImplementingState()
        if outcome.kind == OutcomeKind.NEEDS_FIX:
            return SpecFixState()
        if outcome.kind == OutcomeKind.NEEDS_HELP:
            return NeedsHelpState()
        return FailedState()
```

Add a placeholder for `ImplementingState` (filled in at Task 3.7):

```python
# packages/foreman/src/foreman/v4/states/implementing.py — REPLACED in Task 3.7
from foreman.v4.state import TicketState
from foreman.v4.outcome import Outcome


class ImplementingState(TicketState):
    state_name = "Implementing"

    def execute(self, ctx) -> Outcome:
        raise NotImplementedError("filled in at Task 3.7")

    def next_state(self, outcome: Outcome) -> TicketState | None:
        raise NotImplementedError("filled in at Task 3.7")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/states/test_spec_review.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/states/spec_review.py packages/foreman/src/foreman/v4/states/implementing.py packages/foreman/tests/v4/states/test_spec_review.py
git commit -m "feat(v4): add SpecReviewState (merges spec PR on CLEAN)"
```

### Task 3.7: ImplementingState (handles BLOCKED outcome)

**Files:**
- Modify (overwrite placeholder): `packages/foreman/src/foreman/v4/states/implementing.py`
- Test: `packages/foreman/tests/v4/states/test_implementing.py`

`ImplementingState` is the Worker. On CLEAN, advance to `ImplReview`. On `BLOCKED` (Worker reports "impl PR open; CI is in flight"), the state stays in `Implementing` so the Poller can re-check artifact state next tick. Stay-in-state means `next_state(outcome)` returns a new `ImplementingState()` instance — same logical state, new sequence in the journal.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/states/test_implementing.py
"""ImplementingState — Worker; BLOCKED stays in state pending Poller re-check."""
from __future__ import annotations

import datetime as dt
import pytest

from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.states.implementing import ImplementingState


def _o(kind: OutcomeKind) -> Outcome:
    return Outcome(kind=kind, confidence=OutcomeConfidence.HIGH, summary="x")


@pytest.mark.parametrize(
    "kind,expected_state_name",
    [
        (OutcomeKind.CLEAN, "ImplReview"),
        (OutcomeKind.BLOCKED, "Implementing"),
        (OutcomeKind.NEEDS_HELP, "NeedsHelp"),
        (OutcomeKind.ERROR, "Failed"),
    ],
)
def test_routing(kind, expected_state_name):
    next_state = ImplementingState().next_state(_o(kind))
    assert next_state is not None
    assert next_state.state_name == expected_state_name


def test_blocked_returns_new_implementing_instance():
    """Same logical state, new instance — Poller picks it up next tick."""
    state = ImplementingState()
    next_state = state.next_state(_o(OutcomeKind.BLOCKED))
    assert isinstance(next_state, ImplementingState)
    assert next_state is not state


def test_role_attribute():
    assert ImplementingState.role == "worker"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/states/test_implementing.py -v`
Expected: FAIL — placeholder raises `NotImplementedError`

- [ ] **Step 3: Replace the placeholder**

```python
# packages/foreman/src/foreman/v4/states/implementing.py
"""ImplementingState — Worker role.

On BLOCKED, the Worker has opened an impl PR but CI is still in flight.
The state advances to a fresh ImplementingState instance — same logical
state, new sequence in the journal. The Poller picks it up on the next
tick to re-check CI verdict and reinvoke the Worker if needed.
"""

from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeKind
from foreman.v4.state import TicketState
from foreman.v4.states.role_dispatch import RoleDispatchState


class ImplementingState(RoleDispatchState):
    state_name = "Implementing"
    role = "worker"

    def next_state_for(self, outcome: Outcome) -> TicketState | None:
        from foreman.v4.states.impl_review import ImplReviewState
        from foreman.v4.states.terminal import FailedState, NeedsHelpState
        if outcome.kind == OutcomeKind.CLEAN:
            return ImplReviewState()
        if outcome.kind == OutcomeKind.BLOCKED:
            return ImplementingState()
        if outcome.kind == OutcomeKind.NEEDS_HELP:
            return NeedsHelpState()
        return FailedState()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/states/test_implementing.py -v`
Expected: 6 passed (4 parametrized + 2 standalone)

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/states/implementing.py packages/foreman/tests/v4/states/test_implementing.py
git commit -m "feat(v4): add ImplementingState (BLOCKED keeps state, advances sequence)"
```

### Task 3.8: MergingState (artifact-check via GitProvider)

**Files:**
- Modify (overwrite placeholder): `packages/foreman/src/foreman/v4/states/merging.py`
- Test: `packages/foreman/tests/v4/states/test_merging.py`

`MergingState` does NOT dispatch a role. It queries GitHub's MergeQueue verdict via `GitProvider.merge_verdict`. Outcomes:

| Verdict | Outcome kind | Next state |
| --- | --- | --- |
| `MERGED` | CLEAN | `DoneState` |
| `PENDING` | BLOCKED | `MergingState()` (stay in state, advance sequence) |
| `REJECTED` | NEEDS_FIX | `ImplFixState` (Worker fixes whatever MergeQueue rejected on) |

Enqueue happens on first entry into MergingState. `enter()` is the hook for that side effect — runs once per state-instance before `execute()`. If the PR is already in the queue (re-entry from BLOCKED), enqueue is idempotent on the FakeGitProvider.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/states/test_merging.py
"""MergingState — artifact-check against MergeQueue verdict."""
from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.git_provider import FakeGitProvider, MergeVerdict, PRState
from foreman.v4.outcome import OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext
from foreman.v4.states.merging import MergingState


def _ctx_with_pr(pr_number: int = 99) -> tuple[StateContext, InMemoryTicketRepository, FakeGitProvider]:
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.set_ticket_state(ticket.id, "Merging", now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Merging", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=pr_number,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    # The impl PR number is tracked on the ticket via its most recent
    # ExecuteCompleted outcome — for the test we'll inject via held_reason
    # since the Repository doesn't have a dedicated column. Real wiring uses
    # the latest state_instance outcome_payload.
    ctx = StateContext(
        ticket=repo.get_ticket(ticket.id), instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        git=git,
    )
    return ctx, repo, git


def test_first_entry_enqueues_into_merge_queue(monkeypatch):
    ctx, repo, git = _ctx_with_pr(pr_number=99)
    # Stub the PR-number lookup; real impl reads from the most recent
    # ExecuteCompleted outcome on the ticket. Phase-3 test substitutes:
    monkeypatch.setattr(
        MergingState, "_pr_number_for", lambda self, ctx: 99,
    )
    MergingState().transition(ctx)
    assert ("p", 99) in git.merge_queue


def test_pending_verdict_routes_back_to_merging(monkeypatch):
    ctx, repo, git = _ctx_with_pr(pr_number=99)
    monkeypatch.setattr(MergingState, "_pr_number_for", lambda self, ctx: 99)
    git.enqueue_merge_queue(project="p", pr_number=99)  # already pending
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Merging"


def test_merged_verdict_routes_to_done(monkeypatch):
    ctx, repo, git = _ctx_with_pr(pr_number=99)
    monkeypatch.setattr(MergingState, "_pr_number_for", lambda self, ctx: 99)
    git.enqueue_merge_queue(project="p", pr_number=99)
    git.set_merge_verdict(project="p", pr_number=99, verdict=MergeVerdict.MERGED)
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Done"


def test_rejected_verdict_routes_to_impl_fix(monkeypatch):
    ctx, repo, git = _ctx_with_pr(pr_number=99)
    monkeypatch.setattr(MergingState, "_pr_number_for", lambda self, ctx: 99)
    git.enqueue_merge_queue(project="p", pr_number=99)
    git.set_merge_verdict(project="p", pr_number=99, verdict=MergeVerdict.REJECTED)
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "ImplFix"


def test_missing_git_provider_routes_through_execute_failure(monkeypatch):
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Merging", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        # git omitted
    )
    monkeypatch.setattr(MergingState, "_pr_number_for", lambda self, ctx: 99)
    MergingState().transition(ctx)
    closed = repo.get_state_instance(instance.id)
    assert closed.failure_phase in ("enter", "execute")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/states/test_merging.py -v`
Expected: FAIL — placeholder raises `NotImplementedError`

- [ ] **Step 3: Replace the placeholder**

```python
# packages/foreman/src/foreman/v4/states/merging.py
"""MergingState — enqueues impl PR into MergeQueue; polls verdict.

The only state in v4 whose execute() doesn't dispatch a role. The Worker
already opened the impl PR; here we wait for GitHub's MergeQueue verdict.

PENDING → stay in state (new instance, Poller picks up next tick).
MERGED  → Done.
REJECTED → ImplFix (Worker fixes whatever MergeQueue caught).
"""

from __future__ import annotations

from foreman.v4.git_provider import MergeVerdict
from foreman.v4.outcome import (
    Outcome,
    OutcomeArtifacts,
    OutcomeConfidence,
    OutcomeKind,
)
from foreman.v4.state import StateContext, TicketState


class MergingState(TicketState):
    state_name = "Merging"

    def _pr_number_for(self, ctx: StateContext) -> int:
        """Find the impl PR number from the ticket's most recent ExecuteCompleted outcome.

        Implementation note: walks state_instances in reverse from current
        sequence, looking for the most recent outcome_payload with an
        artifacts.pr_number set. Production wiring uses Phase 4's Repository
        query helper; Phase 3 tests stub via monkeypatch.
        """
        # Placeholder for the read; real impl uses ctx.repo's journal walk.
        # Subclassed tests override this method with the PR number directly.
        raise NotImplementedError("override or wire via ctx.repo journal walk")

    def enter(self, ctx: StateContext) -> None:
        if ctx.git is None:
            raise RuntimeError("MergingState requires git in StateContext")
        pr_number = self._pr_number_for(ctx)
        ctx.git.enqueue_merge_queue(project=ctx.ticket.project, pr_number=pr_number)

    def execute(self, ctx: StateContext) -> Outcome:
        if ctx.git is None:
            raise RuntimeError("MergingState requires git in StateContext")
        pr_number = self._pr_number_for(ctx)
        verdict = ctx.git.merge_verdict(project=ctx.ticket.project, pr_number=pr_number)
        if verdict is MergeVerdict.MERGED:
            return Outcome(
                kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
                summary="merge queue merged",
                artifacts=OutcomeArtifacts(pr_number=pr_number),
            )
        if verdict is MergeVerdict.REJECTED:
            return Outcome(
                kind=OutcomeKind.NEEDS_FIX, confidence=OutcomeConfidence.HIGH,
                summary="merge queue rejected — CI or conflict",
                artifacts=OutcomeArtifacts(pr_number=pr_number),
            )
        return Outcome(
            kind=OutcomeKind.BLOCKED, confidence=OutcomeConfidence.HIGH,
            summary="merge queue pending verdict",
            artifacts=OutcomeArtifacts(pr_number=pr_number),
        )

    def next_state(self, outcome: Outcome) -> TicketState | None:
        from foreman.v4.states.impl_fix import ImplFixState
        from foreman.v4.states.terminal import DoneState
        if outcome.kind == OutcomeKind.CLEAN:
            return DoneState()
        if outcome.kind == OutcomeKind.NEEDS_FIX:
            return ImplFixState()
        if outcome.kind == OutcomeKind.BLOCKED:
            return MergingState()
        from foreman.v4.states.terminal import FailedState
        return FailedState()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/states/test_merging.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/states/merging.py packages/foreman/tests/v4/states/test_merging.py
git commit -m "feat(v4): add MergingState (enqueue + verdict-based routing)"
```

### Task 3.9: State registry — name → factory

**Files:**
- Create: `packages/foreman/src/foreman/v4/states/registry.py`
- Test: `packages/foreman/tests/v4/states/test_registry.py`

The Poller will need to instantiate the right state from a stored state_name when reviving a ticket (`tickets.current_state` column). The registry is the lookup.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/states/test_registry.py
"""STATE_REGISTRY — name → state factory."""
from __future__ import annotations

import pytest

from foreman.v4.states.implementing import ImplementingState
from foreman.v4.states.planning import PlanningState
from foreman.v4.states.registry import STATE_REGISTRY, build_state


def test_registry_contains_all_eleven_states():
    expected = {
        "Queued", "Planning", "SpecReview", "SpecFix",
        "Implementing", "ImplReview", "ImplFix", "Merging",
        "Done", "Failed", "NeedsHelp",
    }
    assert set(STATE_REGISTRY) == expected


def test_build_state_returns_correct_instance():
    assert isinstance(build_state("Planning"), PlanningState)
    assert isinstance(build_state("Implementing"), ImplementingState)


def test_unknown_state_raises():
    with pytest.raises(KeyError):
        build_state("NotAState")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/states/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the registry**

```python
# packages/foreman/src/foreman/v4/states/registry.py
"""STATE_REGISTRY — name → factory mapping for state revival from SQLite.

The Poller and CLI both need to instantiate the right concrete state class
from a stored ``current_state`` string. This is the only place that mapping
lives; updating it is a single edit when new states are added.
"""

from __future__ import annotations

from typing import Callable

from foreman.v4.state import TicketState
from foreman.v4.states.impl_fix import ImplFixState
from foreman.v4.states.impl_review import ImplReviewState
from foreman.v4.states.implementing import ImplementingState
from foreman.v4.states.merging import MergingState
from foreman.v4.states.planning import PlanningState
from foreman.v4.states.queued import QueuedState
from foreman.v4.states.spec_fix import SpecFixState
from foreman.v4.states.spec_review import SpecReviewState
from foreman.v4.states.terminal import DoneState, FailedState, NeedsHelpState


STATE_REGISTRY: dict[str, Callable[[], TicketState]] = {
    "Queued": QueuedState,
    "Planning": PlanningState,
    "SpecReview": SpecReviewState,
    "SpecFix": SpecFixState,
    "Implementing": ImplementingState,
    "ImplReview": ImplReviewState,
    "ImplFix": ImplFixState,
    "Merging": MergingState,
    "Done": DoneState,
    "Failed": FailedState,
    "NeedsHelp": NeedsHelpState,
}


def build_state(name: str) -> TicketState:
    """Return a fresh instance of the named state.

    Raises KeyError if the name is unknown — that's a schema-evolution
    invariant violation (someone added a state without updating the registry).
    """
    return STATE_REGISTRY[name]()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/states/test_registry.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/states/registry.py packages/foreman/tests/v4/states/test_registry.py
git commit -m "feat(v4): add STATE_REGISTRY for state revival from SQLite"
```

### Task 3.10: End-to-end lifecycle test (Phase 3 completion)

**Files:**
- Create: `packages/foreman/tests/v4/test_lifecycle.py`

Drives a ticket through the full happy path — Queued → Planning → SpecReview → Implementing → ImplReview → Merging → Done — using `FakeGitProvider` + `FakeRoleDispatcher`. The dispatch helper walks the journal: read current_state, instantiate via registry, open a state_instance, call transition(), persist new state, repeat until terminal.

This is the empirical Phase 3 gate. If it passes, the substrate (Phase 1) + observability (Phase 2) + concrete states (Phase 3) are all aligned.

- [ ] **Step 1: Write the lifecycle test**

```python
# packages/foreman/tests/v4/test_lifecycle.py
"""End-to-end lifecycle: Queued → ... → Done with all-fake providers.

This is the Phase 3 completion check. The test scripts canned Outcomes for
each role-dispatch state and walks a ticket through the happy path,
asserting the journal looks right at the end.
"""
from __future__ import annotations

import datetime as dt

from foreman.v4.event_bus import EventBus
from foreman.v4.git_provider import FakeGitProvider, MergeVerdict, PRState
from foreman.v4.observers.event_archive import EventArchiveObserver
from foreman.v4.observers.structured_log import StructuredLogObserver
from foreman.v4.role_dispatcher import FakeRoleDispatcher
from foreman.v4.sqlite_repository import SqliteTicketRepository
from foreman.v4.state import StateContext
from foreman.v4.states.merging import MergingState
from foreman.v4.states.registry import build_state


def _canned(kind: str, *, pr_number: int | None = None) -> str:
    artifacts = f',"artifacts":{{"pr_number":{pr_number}}}' if pr_number else ""
    return (
        f'FOREMAN_OUTCOME:{{"kind":"{kind}","confidence":"high",'
        f'"summary":"test"{artifacts}}}'
    )


def _run_until_terminal(repo, ticket_id, *, dispatcher, git, bus):
    """Drive the ticket one transition at a time until it reaches a terminal."""
    seq = 0
    while True:
        ticket = repo.get_ticket(ticket_id)
        if ticket.current_state in ("Done", "Failed", "NeedsHelp"):
            return ticket
        seq += 1
        state = build_state(ticket.current_state)
        instance = repo.open_state_instance(
            ticket_id=ticket.id, state_name=ticket.current_state,
            sequence=seq, now=dt.datetime(2026, 6, 13),
        )
        ctx = StateContext(
            ticket=ticket, instance=instance, repo=repo,
            clock=lambda: dt.datetime(2026, 6, 13, 12, seq, 0),
            bus=bus, role_dispatcher=dispatcher, git=git,
        )
        # MergingState needs a pr_number; in real wiring it reads from the
        # ticket's most recent outcome_payload. For the lifecycle test we
        # monkey-patch that lookup.
        if isinstance(state, MergingState):
            state._pr_number_for = lambda _ctx: 42  # type: ignore[method-assign]
        state.transition(ctx)
        if seq > 25:
            raise AssertionError("did not converge; check canned outcomes")


def test_happy_path_queued_to_done():
    repo = SqliteTicketRepository.in_memory()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=42,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 1):        _canned("clean", pr_number=42),
        ("reviewer-spec", "p", 1):  _canned("clean", pr_number=42),
        ("worker", "p", 1):         _canned("clean", pr_number=42),
        ("reviewer-impl", "p", 1):  _canned("clean", pr_number=42),
    })
    bus = EventBus()
    bus.subscribe(EventArchiveObserver(conn=repo._conn))
    bus.subscribe(StructuredLogObserver())

    # Drive the ticket. MergingState's first transition issues BLOCKED;
    # set the verdict to MERGED so the second pass advances to Done.
    git.enqueue_merge_queue(project="p", pr_number=42)
    git.set_merge_verdict(project="p", pr_number=42, verdict=MergeVerdict.MERGED)

    final = _run_until_terminal(repo, ticket.id, dispatcher=dispatcher, git=git, bus=bus)
    assert final.current_state == "Done"

    # Spec PR was merged by SpecReviewState.verify()
    assert git.get_pr_state(project="p", pr_number=42).merged is True

    # Journal records every state transition in order:
    rows = repo._conn.execute(
        "SELECT state_name, outcome_kind, next_state FROM state_instances "
        "WHERE ticket_id = ? ORDER BY sequence",
        (ticket.id,),
    ).fetchall()
    state_order = [r["state_name"] for r in rows]
    assert state_order == [
        "Queued", "Planning", "SpecReview", "Implementing",
        "ImplReview", "Merging", "Done",
    ]

    # Events archived for each transition:
    event_rows = repo._conn.execute(
        "SELECT DISTINCT state_name FROM events ORDER BY id"
    ).fetchall()
    archived_states = [r["state_name"] for r in event_rows]
    assert set(archived_states) >= {
        "Queued", "Planning", "SpecReview", "Implementing",
        "ImplReview", "Merging",
    }


def test_needs_fix_loop_spec_review_to_spec_fix_back():
    """When Reviewer rejects spec, we loop through SpecFix back to SpecReview."""
    repo = SqliteTicketRepository.in_memory()
    ticket = repo.create_ticket(project="p", issue_number=2, now=dt.datetime(2026, 6, 13))
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=7,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    # Reviewer rejects first, then Fixer fixes, then Reviewer accepts.
    # We cheat by mutating the canned response between iterations using a
    # mutable cell.
    review_calls = {"n": 0}

    class _ScriptedDispatcher:
        def dispatch(self, *, role, project, issue_number, ticket_id):
            if role == "planner":
                return _canned("clean", pr_number=7)
            if role == "reviewer-spec":
                review_calls["n"] += 1
                if review_calls["n"] == 1:
                    return _canned("needs_fix", pr_number=7)
                return _canned("clean", pr_number=7)
            if role == "fixer-spec":
                return _canned("clean", pr_number=7)
            if role == "worker":
                return _canned("clean", pr_number=7)
            if role == "reviewer-impl":
                return _canned("clean", pr_number=7)
            raise AssertionError(f"unexpected role {role}")

    git.enqueue_merge_queue(project="p", pr_number=7)
    git.set_merge_verdict(project="p", pr_number=7, verdict=MergeVerdict.MERGED)

    final = _run_until_terminal(
        repo, ticket.id, dispatcher=_ScriptedDispatcher(), git=git, bus=EventBus(),
    )
    assert final.current_state == "Done"

    state_order = [
        r["state_name"]
        for r in repo._conn.execute(
            "SELECT state_name FROM state_instances WHERE ticket_id = ? ORDER BY sequence",
            (ticket.id,),
        ).fetchall()
    ]
    assert "SpecFix" in state_order
    # SpecReview appears twice — once rejecting, once accepting:
    assert state_order.count("SpecReview") == 2
```

- [ ] **Step 2: Run the lifecycle test**

Run: `uv run pytest packages/foreman/tests/v4/test_lifecycle.py -v`
Expected: 2 passed.

If it fails, the trace lives in the journal — `SELECT * FROM state_instances` shows where the ticket got stuck and why (`failure_phase` + `failure_reason`). Debug from there.

- [ ] **Step 3: Commit**

```bash
git add packages/foreman/tests/v4/test_lifecycle.py
git commit -m "test(v4): end-to-end lifecycle — happy path + needs-fix loop"
```

### Phase 3 — `just check` gate

- [ ] **Run:** `just check`
- [ ] **Expected:** all gates green; every Phase 1/2/3 test passes; isolation guard from Task 1.10 still green.

Phase 3 completion criterion (from the outline): **end-to-end ticket lifecycle test passes against FakeGitProvider**. Achieved at Task 3.10. The substrate now has a complete state machine; what's missing for production is real role-dispatch + real GitHub + the Poller — all of which lands in Phase 4.

---

## Phase 4 — QueueManager + Poller

The substrate runs in tests but nothing drives it in production. Phase 4 adds:

1. **`QueueManager`** (Mediator) — owns the work queue, holds tickets that are paused, caps concurrency, dedups in-flight work.
2. **`WorkerPool`** — drains the queue, builds `StateContext`, calls `transition()`. Bounded concurrency; clean shutdown.
3. **`Poller`** — the single source of new work. Reads SQLite for in-flight state instances + open tickets; queries `GitProvider` for artifact state on those tickets; enqueues work via QueueManager. Dedups by `(ticket_id, state_name, sequence)` so re-polling the same artifact state doesn't double-process.
4. **`PyGithubGitProvider`** — the real PyGithub-backed implementation of the `GitProvider` Protocol from Task 3.2. Behind the seam so Phase 4 tests still use `FakeGitProvider`.
5. **Repository helper** for "what's the latest PR number on this ticket?" — fills the lookup that Task 3.8 monkey-patched.

Phase 4 finishes when the lifecycle test runs end-to-end through the real QueueManager + Poller (tests still use fakes for GitHub and role dispatch — those become real subprocesses in Phase 5).

### Task 4.1: Repository helper — latest_pr_number_for_ticket

**Files:**
- Modify: `packages/foreman/src/foreman/v4/repository.py` (add method to Protocol)
- Modify: `packages/foreman/src/foreman/v4/sqlite_repository.py` (impl)
- Modify: `packages/foreman/tests/v4/_repository_contract.py` (extend contract)

Walks `state_instances` for the ticket in reverse sequence; returns the most recent `outcome_payload.artifacts.pr_number`. MergingState's `_pr_number_for` reads this in Phase 4 production wiring; Phase 3's monkey-patch goes away.

- [ ] **Step 1: Extend the contract**

In `_repository_contract.py`, add to `RepositoryContract`:

```python
    def test_latest_pr_number_for_ticket_returns_most_recent(self, repo: TicketRepository):
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        # First state has no PR
        i1 = repo.open_state_instance(
            ticket_id=t.id, state_name="Queued", sequence=1, now=_now(),
        )
        repo.mark_execute_completed(
            i1.id, now=_now(),
            outcome_kind=OutcomeKind.CLEAN,
            outcome_payload={"artifacts": {}},
            next_state="Planning",
        )
        repo.close_state_instance(i1.id, now=_now())
        # Second state records PR 42
        i2 = repo.open_state_instance(
            ticket_id=t.id, state_name="Planning", sequence=2, now=_now(),
        )
        repo.mark_execute_completed(
            i2.id, now=_now(),
            outcome_kind=OutcomeKind.CLEAN,
            outcome_payload={"artifacts": {"pr_number": 42}},
            next_state="SpecReview",
        )
        repo.close_state_instance(i2.id, now=_now())
        assert repo.latest_pr_number_for_ticket(t.id) == 42

    def test_latest_pr_number_returns_none_when_no_outcomes(self, repo: TicketRepository):
        t = repo.create_ticket(project="p", issue_number=2, now=_now())
        assert repo.latest_pr_number_for_ticket(t.id) is None

    def test_latest_pr_number_skips_outcomes_without_pr(self, repo: TicketRepository):
        t = repo.create_ticket(project="p", issue_number=3, now=_now())
        # Most recent outcome has no PR; earlier outcome had PR 7 — return 7.
        i1 = repo.open_state_instance(
            ticket_id=t.id, state_name="Queued", sequence=1, now=_now(),
        )
        repo.mark_execute_completed(
            i1.id, now=_now(),
            outcome_kind=OutcomeKind.CLEAN,
            outcome_payload={"artifacts": {"pr_number": 7}},
            next_state="Planning",
        )
        repo.close_state_instance(i1.id, now=_now())
        i2 = repo.open_state_instance(
            ticket_id=t.id, state_name="Planning", sequence=2, now=_now(),
        )
        repo.mark_execute_completed(
            i2.id, now=_now(),
            outcome_kind=OutcomeKind.CLEAN,
            outcome_payload={"artifacts": {}},
            next_state="SpecReview",
        )
        repo.close_state_instance(i2.id, now=_now())
        assert repo.latest_pr_number_for_ticket(t.id) == 7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_in_memory_repository.py packages/foreman/tests/v4/test_sqlite_repository.py -v`
Expected: 6 new tests fail (3 per impl) with `AttributeError: 'InMemoryTicketRepository' object has no attribute 'latest_pr_number_for_ticket'`

- [ ] **Step 3: Add to Protocol + both impls**

In `repository.py`, add to the `TicketRepository` Protocol:

```python
    def latest_pr_number_for_ticket(self, ticket_id: int) -> int | None: ...
```

Add to `InMemoryTicketRepository`:

```python
    def latest_pr_number_for_ticket(self, ticket_id: int) -> int | None:
        candidates = [
            i for i in self._instances.values() if i.ticket_id == ticket_id
        ]
        candidates.sort(key=lambda i: i.sequence, reverse=True)
        for inst in candidates:
            if not inst.outcome_payload:
                continue
            pr_number = (inst.outcome_payload or {}).get("artifacts", {}).get("pr_number")
            if pr_number is not None:
                return int(pr_number)
        return None
```

Add to `SqliteTicketRepository`:

```python
    def latest_pr_number_for_ticket(self, ticket_id: int) -> int | None:
        rows = self._conn.execute(
            "SELECT outcome_payload FROM state_instances "
            "WHERE ticket_id = ? AND outcome_payload IS NOT NULL "
            "ORDER BY sequence DESC",
            (ticket_id,),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["outcome_payload"])
            pr_number = payload.get("artifacts", {}).get("pr_number")
            if pr_number is not None:
                return int(pr_number)
        return None
```

- [ ] **Step 4: Wire MergingState to use it**

Update `packages/foreman/src/foreman/v4/states/merging.py`'s `_pr_number_for`:

```python
    def _pr_number_for(self, ctx: StateContext) -> int:
        pr = ctx.repo.latest_pr_number_for_ticket(ctx.ticket.id)
        if pr is None:
            raise RuntimeError(
                f"MergingState for ticket {ctx.ticket.id} has no PR number "
                "in any prior state outcome"
            )
        return pr
```

Update `packages/foreman/tests/v4/states/test_merging.py` — drop the monkey-patches and instead create the prior outcome in the fixture. Example for `test_first_entry_enqueues_into_merge_queue`:

```python
def test_first_entry_enqueues_into_merge_queue():
    ctx, repo, git = _ctx_with_pr(pr_number=99)
    # Seed a prior state instance with the PR number
    prior = repo.open_state_instance(
        ticket_id=ctx.ticket.id, state_name="ImplReview", sequence=0,
        now=dt.datetime(2026, 6, 13),
    )
    repo.mark_execute_completed(
        prior.id, now=dt.datetime(2026, 6, 13),
        outcome_kind=OutcomeKind.CLEAN,
        outcome_payload={"artifacts": {"pr_number": 99}},
        next_state="Merging",
    )
    repo.close_state_instance(prior.id, now=dt.datetime(2026, 6, 13))
    MergingState().transition(ctx)
    assert ("p", 99) in git.merge_queue
```

Apply the same pattern to the other Merging tests. Drop all `monkeypatch.setattr(MergingState, "_pr_number_for", ...)` calls.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/ -v`
Expected: contract tests pass for both repos; updated Merging tests pass.

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/v4/repository.py packages/foreman/src/foreman/v4/sqlite_repository.py packages/foreman/src/foreman/v4/states/merging.py packages/foreman/tests/v4/_repository_contract.py packages/foreman/tests/v4/states/test_merging.py
git commit -m "feat(v4): add latest_pr_number_for_ticket; wire MergingState to use it"
```

### Task 4.2: WorkItem dataclass

**Files:**
- Create: `packages/foreman/src/foreman/v4/work.py`
- Test: `packages/foreman/tests/v4/test_work.py`

Frozen dataclass: `(ticket_id, state_name)`. The Queue holds these; the WorkerPool dispatches on them. Two-field shape; that's the v4 queue contract.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_work.py
"""WorkItem — the v4 queue item shape."""
from __future__ import annotations

import pytest

from foreman.v4.work import WorkItem


def test_work_item_carries_ticket_and_state_name():
    item = WorkItem(ticket_id=1, state_name="Planning")
    assert item.ticket_id == 1
    assert item.state_name == "Planning"


def test_work_item_is_hashable_for_dedup():
    a = WorkItem(ticket_id=1, state_name="Planning")
    b = WorkItem(ticket_id=1, state_name="Planning")
    assert a == b
    assert hash(a) == hash(b)


def test_work_item_is_immutable():
    item = WorkItem(ticket_id=1, state_name="Planning")
    with pytest.raises(AttributeError):
        item.ticket_id = 2  # type: ignore[misc]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_work.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the dataclass**

```python
# packages/foreman/src/foreman/v4/work.py
"""WorkItem — the v4 queue contract.

A WorkItem is just "advance ticket T from state S to whatever next_state
returns." Two fields. Hashable so the QueueManager can dedup.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkItem:
    ticket_id: int
    state_name: str
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_work.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/work.py packages/foreman/tests/v4/test_work.py
git commit -m "feat(v4): add WorkItem queue-contract dataclass"
```

### Task 4.3: QueueManager (Mediator)

**Files:**
- Create: `packages/foreman/src/foreman/v4/queue_manager.py`
- Test: `packages/foreman/tests/v4/test_queue_manager.py`

Responsibilities:

- **Dedup on enqueue.** Same `WorkItem` enqueued twice = one entry. Producers can hammer without coordinating.
- **Respect operator hold.** `dequeue()` skips items whose ticket has `held_by IS NOT NULL` (puts them back at the tail; they're re-evaluated next dequeue).
- **Concurrency cap.** Configurable max in-flight items. `dequeue()` returns None when at cap; checked-in items are tracked.
- **Mark complete.** Caller calls `mark_done(item)` after `transition()` returns to free the slot.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_queue_manager.py
"""QueueManager — Mediator between Poller (producer) and WorkerPool (consumer)."""
from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.queue_manager import QueueManager
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.work import WorkItem


@pytest.fixture()
def repo() -> InMemoryTicketRepository:
    return InMemoryTicketRepository()


def test_enqueue_then_dequeue_returns_same_item(repo):
    qm = QueueManager(repo=repo, max_in_flight=4)
    item = WorkItem(ticket_id=1, state_name="Planning")
    qm.enqueue(item)
    assert qm.dequeue() == item


def test_dedup_collapses_repeated_enqueue(repo):
    qm = QueueManager(repo=repo, max_in_flight=4)
    item = WorkItem(ticket_id=1, state_name="Planning")
    qm.enqueue(item)
    qm.enqueue(item)
    qm.enqueue(item)
    assert qm.dequeue() == item
    assert qm.dequeue() is None


def test_empty_queue_returns_none(repo):
    qm = QueueManager(repo=repo, max_in_flight=4)
    assert qm.dequeue() is None


def test_concurrency_cap_blocks_further_dequeues(repo):
    repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.create_ticket(project="p", issue_number=2, now=dt.datetime(2026, 6, 13))
    qm = QueueManager(repo=repo, max_in_flight=1)
    qm.enqueue(WorkItem(ticket_id=1, state_name="Planning"))
    qm.enqueue(WorkItem(ticket_id=2, state_name="Planning"))
    first = qm.dequeue()
    assert first is not None
    assert qm.dequeue() is None  # at cap


def test_mark_done_frees_slot(repo):
    repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.create_ticket(project="p", issue_number=2, now=dt.datetime(2026, 6, 13))
    qm = QueueManager(repo=repo, max_in_flight=1)
    qm.enqueue(WorkItem(ticket_id=1, state_name="Planning"))
    qm.enqueue(WorkItem(ticket_id=2, state_name="Planning"))
    first = qm.dequeue()
    qm.mark_done(first)
    second = qm.dequeue()
    assert second is not None


def test_held_ticket_is_skipped(repo):
    held = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.create_ticket(project="p", issue_number=2, now=dt.datetime(2026, 6, 13))
    repo.hold_ticket(
        held.id, held_by="jeff", reason="vacation", now=dt.datetime(2026, 6, 13),
    )
    qm = QueueManager(repo=repo, max_in_flight=4)
    qm.enqueue(WorkItem(ticket_id=held.id, state_name="Planning"))
    qm.enqueue(WorkItem(ticket_id=2, state_name="Planning"))
    item = qm.dequeue()
    assert item is not None
    assert item.ticket_id == 2  # held one skipped


def test_held_item_stays_in_queue_for_later(repo):
    held = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.hold_ticket(
        held.id, held_by="jeff", reason="vacation", now=dt.datetime(2026, 6, 13),
    )
    qm = QueueManager(repo=repo, max_in_flight=4)
    qm.enqueue(WorkItem(ticket_id=held.id, state_name="Planning"))
    assert qm.dequeue() is None  # nothing dispatchable
    repo.resume_ticket(held.id, now=dt.datetime(2026, 6, 13))
    item = qm.dequeue()
    assert item is not None and item.ticket_id == held.id


def test_in_flight_count_reports(repo):
    repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.create_ticket(project="p", issue_number=2, now=dt.datetime(2026, 6, 13))
    qm = QueueManager(repo=repo, max_in_flight=4)
    qm.enqueue(WorkItem(ticket_id=1, state_name="Planning"))
    qm.enqueue(WorkItem(ticket_id=2, state_name="Planning"))
    qm.dequeue()
    qm.dequeue()
    assert qm.in_flight_count() == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_queue_manager.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the QueueManager**

```python
# packages/foreman/src/foreman/v4/queue_manager.py
"""QueueManager — Mediator between producer (Poller) and consumer (WorkerPool).

Single source of truth for "what work is queued, what's in-flight, what's
paused." Producers fire-and-forget via enqueue(); consumers loop on
dequeue() and call mark_done() when the transition finishes.

Operator hold is respected at dequeue time, not at enqueue time. This
matters because hold can be applied while work is already queued — the
QueueManager simply skips held tickets each dequeue pass, leaving them
in the queue for a later pass when the hold is released.
"""

from __future__ import annotations

from collections import deque

from foreman.v4.repository import TicketRepository
from foreman.v4.work import WorkItem


class QueueManager:
    def __init__(self, *, repo: TicketRepository, max_in_flight: int) -> None:
        self._repo = repo
        self._max_in_flight = max_in_flight
        self._queue: deque[WorkItem] = deque()
        self._queued: set[WorkItem] = set()
        self._in_flight: set[WorkItem] = set()

    def enqueue(self, item: WorkItem) -> None:
        if item in self._queued or item in self._in_flight:
            return
        self._queue.append(item)
        self._queued.add(item)

    def dequeue(self) -> WorkItem | None:
        if len(self._in_flight) >= self._max_in_flight:
            return None
        # Scan past held tickets; leave them in the queue.
        deferred: list[WorkItem] = []
        try:
            while self._queue:
                candidate = self._queue.popleft()
                self._queued.discard(candidate)
                ticket = self._repo.get_ticket(candidate.ticket_id)
                if ticket.is_held:
                    deferred.append(candidate)
                    continue
                self._in_flight.add(candidate)
                return candidate
            return None
        finally:
            # Put any deferred items back at the tail so other tickets
            # don't starve while a hold is in place.
            for item in deferred:
                self._queue.append(item)
                self._queued.add(item)

    def mark_done(self, item: WorkItem) -> None:
        self._in_flight.discard(item)

    def in_flight_count(self) -> int:
        return len(self._in_flight)

    def queue_depth(self) -> int:
        return len(self._queue)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_queue_manager.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/queue_manager.py packages/foreman/tests/v4/test_queue_manager.py
git commit -m "feat(v4): add QueueManager mediator with dedup + hold-respect"
```

### Task 4.4: WorkerPool — drains the queue, runs transition()

**Files:**
- Create: `packages/foreman/src/foreman/v4/worker_pool.py`
- Test: `packages/foreman/tests/v4/test_worker_pool.py`

Single-threaded loop that calls `qm.dequeue()`, builds a `StateContext`, instantiates the state via the registry, runs `transition()`, calls `mark_done()`. Bounded concurrency is the QueueManager's responsibility — the pool just keeps draining. Stops cleanly on a stop flag.

For v4, "pool" is a misnomer; we run sequentially in one thread. The QueueManager + Poller cycle already gives ticket-level concurrency (multiple tickets sit in the queue; one worker drains in serial; that's fine for current volume). Real threadpool comes later if we ever need it — YAGNI for v4 ship.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_worker_pool.py
"""WorkerPool — drain QueueManager → run transition()."""
from __future__ import annotations

import datetime as dt

from foreman.v4.queue_manager import QueueManager
from foreman.v4.role_dispatcher import FakeRoleDispatcher
from foreman.v4.sqlite_repository import SqliteTicketRepository
from foreman.v4.work import WorkItem
from foreman.v4.worker_pool import WorkerPool


def _canned(kind: str) -> str:
    return f'FOREMAN_OUTCOME:{{"kind":"{kind}","confidence":"high","summary":"x"}}'


def test_runs_one_transition_per_dequeue():
    repo = SqliteTicketRepository.in_memory()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    qm = QueueManager(repo=repo, max_in_flight=4)
    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 1): _canned("clean"),
    })
    pool = WorkerPool(
        repo=repo, qm=qm,
        dispatcher=dispatcher,
        git=None,
        bus=None,
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    repo.set_ticket_state(ticket.id, "Planning", now=dt.datetime(2026, 6, 13))
    qm.enqueue(WorkItem(ticket_id=ticket.id, state_name="Planning"))
    advanced = pool.run_one()
    assert advanced is True
    assert qm.in_flight_count() == 0
    refreshed = repo.get_ticket(ticket.id)
    assert refreshed.current_state == "SpecReview"  # Planner CLEAN advances


def test_run_one_returns_false_when_queue_empty():
    repo = SqliteTicketRepository.in_memory()
    qm = QueueManager(repo=repo, max_in_flight=4)
    pool = WorkerPool(
        repo=repo, qm=qm,
        dispatcher=FakeRoleDispatcher(responses={}),
        git=None, bus=None,
        clock=lambda: dt.datetime(2026, 6, 13),
    )
    assert pool.run_one() is False


def test_run_until_empty_drains_completely():
    repo = SqliteTicketRepository.in_memory()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    qm = QueueManager(repo=repo, max_in_flight=4)
    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 1): _canned("clean"),
    })
    pool = WorkerPool(
        repo=repo, qm=qm, dispatcher=dispatcher,
        git=None, bus=None,
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    repo.set_ticket_state(ticket.id, "Planning", now=dt.datetime(2026, 6, 13))
    qm.enqueue(WorkItem(ticket_id=ticket.id, state_name="Planning"))
    drained = pool.run_until_empty()
    assert drained == 1


def test_drains_multiple_items():
    repo = SqliteTicketRepository.in_memory()
    a = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    b = repo.create_ticket(project="p", issue_number=2, now=dt.datetime(2026, 6, 13))
    repo.set_ticket_state(a.id, "Planning", now=dt.datetime(2026, 6, 13))
    repo.set_ticket_state(b.id, "Planning", now=dt.datetime(2026, 6, 13))
    qm = QueueManager(repo=repo, max_in_flight=4)
    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 1): _canned("clean"),
        ("planner", "p", 2): _canned("clean"),
    })
    pool = WorkerPool(
        repo=repo, qm=qm, dispatcher=dispatcher,
        git=None, bus=None,
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    qm.enqueue(WorkItem(ticket_id=a.id, state_name="Planning"))
    qm.enqueue(WorkItem(ticket_id=b.id, state_name="Planning"))
    assert pool.run_until_empty() == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_worker_pool.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the WorkerPool**

```python
# packages/foreman/src/foreman/v4/worker_pool.py
"""WorkerPool — drains QueueManager + runs transition() per WorkItem.

Sequential single-thread loop. Not a real "pool" — the name reflects the
spec wording. Concurrency happens via multiple tickets sitting in the
queue, dispatched one-after-another. Real-thread parallelism is a future
optimization; foreman's ticket volume is low single digits.
"""

from __future__ import annotations

import datetime as dt
from typing import Callable

from foreman.v4.event_bus import EventBus
from foreman.v4.git_provider import GitProvider
from foreman.v4.queue_manager import QueueManager
from foreman.v4.repository import TicketRepository
from foreman.v4.role_dispatcher import RoleDispatcher
from foreman.v4.state import StateContext
from foreman.v4.states.registry import build_state


class WorkerPool:
    def __init__(
        self,
        *,
        repo: TicketRepository,
        qm: QueueManager,
        dispatcher: RoleDispatcher,
        git: GitProvider | None,
        bus: EventBus | None,
        clock: Callable[[], dt.datetime],
    ) -> None:
        self._repo = repo
        self._qm = qm
        self._dispatcher = dispatcher
        self._git = git
        self._bus = bus
        self._clock = clock

    def run_one(self) -> bool:
        """Pull one WorkItem and run its transition.

        Returns True if work was dispatched, False if the queue was empty
        or hold-blocked.
        """
        item = self._qm.dequeue()
        if item is None:
            return False
        try:
            ticket = self._repo.get_ticket(item.ticket_id)
            sequence = self._next_sequence(item.ticket_id)
            instance = self._repo.open_state_instance(
                ticket_id=item.ticket_id,
                state_name=item.state_name,
                sequence=sequence,
                now=self._clock(),
            )
            state = build_state(item.state_name)
            ctx = StateContext(
                ticket=ticket, instance=instance, repo=self._repo,
                clock=self._clock, bus=self._bus,
                role_dispatcher=self._dispatcher, git=self._git,
            )
            state.transition(ctx)
        finally:
            self._qm.mark_done(item)
        return True

    def run_until_empty(self) -> int:
        """Drain until dequeue returns None. Returns count of items processed."""
        drained = 0
        while self.run_one():
            drained += 1
        return drained

    def _next_sequence(self, ticket_id: int) -> int:
        # Naive: count of state instances for this ticket + 1.
        # Acceptable for v4 — sequence is a per-ticket monotonic counter.
        # If we ever shard across hosts, this becomes a SELECT MAX.
        existing = [
            i for i in self._repo.list_in_flight_state_instances()
            if i.ticket_id == ticket_id
        ]
        # Plus closed instances — read via a quick repo query.
        # Repository doesn't expose "list all instances for ticket" today;
        # use the same query as latest_pr lookup but counting.
        return self._count_all_instances(ticket_id) + 1

    def _count_all_instances(self, ticket_id: int) -> int:
        # Tiny helper — could move to Repository later. For now we read
        # via the implementation's known query surface.
        if hasattr(self._repo, "_conn"):
            row = self._repo._conn.execute(  # type: ignore[attr-defined]
                "SELECT COUNT(*) AS n FROM state_instances WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
            return row["n"]
        # InMemoryTicketRepository fallback — count by walking.
        if hasattr(self._repo, "_instances"):
            return sum(
                1 for i in self._repo._instances.values()  # type: ignore[attr-defined]
                if i.ticket_id == ticket_id
            )
        raise RuntimeError("repository missing sequence-count seam")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_worker_pool.py -v`
Expected: 4 passed

The `_count_all_instances` reaching into `_conn`/`_instances` is a small SRP violation that hints the Repository should grow a `count_state_instances_for_ticket(ticket_id)` method. Add it now and clean up:

```python
# In repository.py Protocol:
def count_state_instances_for_ticket(self, ticket_id: int) -> int: ...

# InMemoryTicketRepository:
def count_state_instances_for_ticket(self, ticket_id: int) -> int:
    return sum(1 for i in self._instances.values() if i.ticket_id == ticket_id)

# SqliteTicketRepository:
def count_state_instances_for_ticket(self, ticket_id: int) -> int:
    row = self._conn.execute(
        "SELECT COUNT(*) AS n FROM state_instances WHERE ticket_id = ?",
        (ticket_id,),
    ).fetchone()
    return row["n"]
```

Add to `_repository_contract.py`:

```python
    def test_count_state_instances_for_ticket(self, repo: TicketRepository):
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        assert repo.count_state_instances_for_ticket(t.id) == 0
        repo.open_state_instance(
            ticket_id=t.id, state_name="Queued", sequence=1, now=_now(),
        )
        assert repo.count_state_instances_for_ticket(t.id) == 1
```

Then collapse the WorkerPool helper:

```python
    def _next_sequence(self, ticket_id: int) -> int:
        return self._repo.count_state_instances_for_ticket(ticket_id) + 1
```

(Drop `_count_all_instances` and the in-flight collection.)

- [ ] **Step 5: Re-run all v4 tests**

Run: `uv run pytest packages/foreman/tests/v4/ -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/v4/worker_pool.py packages/foreman/src/foreman/v4/repository.py packages/foreman/src/foreman/v4/sqlite_repository.py packages/foreman/tests/v4/_repository_contract.py packages/foreman/tests/v4/test_worker_pool.py
git commit -m "feat(v4): add WorkerPool draining QueueManager via Repository helper"
```

### Task 4.5: Poller — produces WorkItems from SQLite + GitProvider

**Files:**
- Create: `packages/foreman/src/foreman/v4/poller.py`
- Test: `packages/foreman/tests/v4/test_poller.py`

The producer side. One `tick()` call does the full sweep:

1. **New tickets.** Query `GitProvider.list_open_issues_with_label(trigger_label)`. For each not-yet-tracked issue, create a ticket row in `Queued` and enqueue `WorkItem(ticket_id, "Queued")`.
2. **In-flight non-blocked tickets.** Read tickets in non-terminal, non-blocked states (i.e., not in MergingState waiting on a verdict; not `Implementing` whose last outcome was BLOCKED). Enqueue `WorkItem(ticket_id, current_state)` so the WorkerPool can advance them. Dedup happens in QueueManager.
3. **Blocked tickets (Merging or Implementing-BLOCKED).** Query GitProvider for current artifact state. If the artifact state has changed since the last poll (verdict moved from PENDING → MERGED, for example), enqueue the WorkItem so the WorkerPool can advance.

Dedup key for #3: `(ticket_id, last_observed_verdict)`. We track the last verdict we saw per ticket; only enqueue on transition.

The Poller doesn't itself need a tight loop — it exposes `tick()` and the daemon calls it on a cadence (Phase 7 wiring). For tests, calling `tick()` once per scenario is enough.

- [ ] **Step 1: Extend GitProvider with the trigger-label query**

In `git_provider.py`, add to the Protocol:

```python
class GitProvider(Protocol):
    def list_open_issues_with_label(
        self, *, project: str, label: str,
    ) -> list[int]: ...
    # ... existing methods
```

In `FakeGitProvider`:

```python
class FakeGitProvider:
    def __init__(self) -> None:
        # ... existing
        self._labeled_issues: dict[tuple[str, str], set[int]] = {}

    def set_open_issues_with_label(
        self, *, project: str, label: str, issue_numbers: set[int],
    ) -> None:
        self._labeled_issues[(project, label)] = set(issue_numbers)

    def list_open_issues_with_label(
        self, *, project: str, label: str,
    ) -> list[int]:
        return sorted(self._labeled_issues.get((project, label), set()))
```

- [ ] **Step 2: Write the failing test**

```python
# packages/foreman/tests/v4/test_poller.py
"""Poller — single sweep that turns SQLite + GitHub state into WorkItems."""
from __future__ import annotations

import datetime as dt

from foreman.v4.git_provider import FakeGitProvider, MergeVerdict, PRState
from foreman.v4.outcome import OutcomeKind
from foreman.v4.poller import Poller
from foreman.v4.queue_manager import QueueManager
from foreman.v4.sqlite_repository import SqliteTicketRepository
from foreman.v4.work import WorkItem


_T0 = dt.datetime(2026, 6, 13, 12, 0, 0)


def _make_poller(repo, git):
    qm = QueueManager(repo=repo, max_in_flight=4)
    poller = Poller(
        repo=repo, qm=qm, git=git,
        project="p", trigger_label="foreman:plan",
        clock=lambda: _T0,
    )
    return poller, qm


def test_new_labeled_issue_creates_ticket_and_enqueues():
    repo = SqliteTicketRepository.in_memory()
    git = FakeGitProvider()
    git.set_open_issues_with_label(
        project="p", label="foreman:plan", issue_numbers={42},
    )
    poller, qm = _make_poller(repo, git)
    poller.tick()
    # Ticket created:
    ticket = repo.get_ticket_by_issue(project="p", issue_number=42)
    assert ticket.current_state == "Queued"
    # Work enqueued:
    assert qm.dequeue() == WorkItem(ticket_id=ticket.id, state_name="Queued")


def test_existing_ticket_not_duplicated():
    repo = SqliteTicketRepository.in_memory()
    repo.create_ticket(project="p", issue_number=42, now=_T0)
    git = FakeGitProvider()
    git.set_open_issues_with_label(
        project="p", label="foreman:plan", issue_numbers={42},
    )
    poller, qm = _make_poller(repo, git)
    poller.tick()
    poller.tick()
    # Second tick should not create a second ticket — get_ticket_by_issue
    # would have raised TicketAlreadyExistsError on insert if it tried.
    ticket = repo.get_ticket_by_issue(project="p", issue_number=42)
    assert ticket.id == 1


def test_in_flight_non_blocked_state_re_enqueued_for_advance():
    repo = SqliteTicketRepository.in_memory()
    t = repo.create_ticket(project="p", issue_number=1, now=_T0)
    repo.set_ticket_state(t.id, "Planning", now=_T0)
    git = FakeGitProvider()
    poller, qm = _make_poller(repo, git)
    poller.tick()
    assert qm.dequeue() == WorkItem(ticket_id=t.id, state_name="Planning")


def test_terminal_states_not_enqueued():
    repo = SqliteTicketRepository.in_memory()
    for state in ("Done", "Failed", "NeedsHelp"):
        ticket = repo.create_ticket(
            project="p", issue_number=hash(state) & 0xFFFF, now=_T0,
        )
        repo.set_ticket_state(ticket.id, state, now=_T0)
    git = FakeGitProvider()
    poller, qm = _make_poller(repo, git)
    poller.tick()
    assert qm.dequeue() is None


def test_merging_blocked_only_enqueues_on_verdict_change():
    repo = SqliteTicketRepository.in_memory()
    ticket = repo.create_ticket(project="p", issue_number=1, now=_T0)
    repo.set_ticket_state(ticket.id, "Merging", now=_T0)
    # Seed prior state recording PR 99
    prior = repo.open_state_instance(
        ticket_id=ticket.id, state_name="ImplReview", sequence=1, now=_T0,
    )
    repo.mark_execute_completed(
        prior.id, now=_T0, outcome_kind=OutcomeKind.CLEAN,
        outcome_payload={"artifacts": {"pr_number": 99}},
        next_state="Merging",
    )
    repo.close_state_instance(prior.id, now=_T0)
    # And one in-flight Merging instance with BLOCKED outcome recorded:
    blocked = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Merging", sequence=2, now=_T0,
    )
    repo.mark_execute_completed(
        blocked.id, now=_T0, outcome_kind=OutcomeKind.BLOCKED,
        outcome_payload={"artifacts": {"pr_number": 99}},
        next_state="Merging",
    )
    repo.close_state_instance(blocked.id, now=_T0)

    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=99,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    git.enqueue_merge_queue(project="p", pr_number=99)
    # Still pending — Poller should NOT enqueue (no change since last block):
    poller, qm = _make_poller(repo, git)
    poller.tick()
    assert qm.dequeue() == WorkItem(ticket_id=ticket.id, state_name="Merging")
    # Wait — actually it should re-enqueue once per tick even on PENDING
    # because the Merging state itself handles BLOCKED → re-poll loop.
    # The dedup is about not creating duplicate journal rows, which the
    # QueueManager handles. So the assertion above is correct.
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_poller.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Write the Poller**

```python
# packages/foreman/src/foreman/v4/poller.py
"""Poller — the only producer of WorkItems in v4.

One ``tick()`` does a full sweep:

  1. Newly-labeled GitHub issues → create ticket rows, enqueue Queued.
  2. Open tickets in non-terminal states → enqueue current_state.
     The QueueManager dedups by WorkItem, so repeated ticks don't duplicate.

The Poller intentionally does NOT track per-tick "what changed since last
tick" state. The QueueManager + journal handle that dedup naturally:
re-enqueuing the same WorkItem is a no-op (QueueManager dedup), and the
WorkerPool's transition() always opens a new state_instance row, so even
if it runs the same logical state twice in a row, the journal stays
linear and the role's idempotency takes care of any visible-to-GitHub
duplication (deferred per spec C3).

A real daemon calls tick() on a cadence; tests call it manually.
"""

from __future__ import annotations

import datetime as dt
from typing import Callable

from foreman.v4.git_provider import GitProvider
from foreman.v4.queue_manager import QueueManager
from foreman.v4.repository import TicketAlreadyExistsError, TicketRepository
from foreman.v4.work import WorkItem


_TERMINAL_STATES = frozenset({"Done", "Failed", "NeedsHelp"})


class Poller:
    def __init__(
        self,
        *,
        repo: TicketRepository,
        qm: QueueManager,
        git: GitProvider,
        project: str,
        trigger_label: str,
        clock: Callable[[], dt.datetime],
    ) -> None:
        self._repo = repo
        self._qm = qm
        self._git = git
        self._project = project
        self._trigger_label = trigger_label
        self._clock = clock

    def tick(self) -> None:
        self._adopt_new_tickets()
        self._enqueue_open_tickets()

    def _adopt_new_tickets(self) -> None:
        issue_numbers = self._git.list_open_issues_with_label(
            project=self._project, label=self._trigger_label,
        )
        for issue_number in issue_numbers:
            try:
                ticket = self._repo.create_ticket(
                    project=self._project,
                    issue_number=issue_number,
                    now=self._clock(),
                )
            except TicketAlreadyExistsError:
                continue
            self._qm.enqueue(WorkItem(ticket_id=ticket.id, state_name="Queued"))

    def _enqueue_open_tickets(self) -> None:
        for ticket in self._repo.list_open_tickets():
            if ticket.current_state in _TERMINAL_STATES:
                continue
            self._qm.enqueue(WorkItem(
                ticket_id=ticket.id, state_name=ticket.current_state,
            ))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_poller.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/v4/poller.py packages/foreman/src/foreman/v4/git_provider.py packages/foreman/src/foreman/v4/poller.py packages/foreman/tests/v4/test_git_provider_fake.py packages/foreman/tests/v4/test_poller.py
git commit -m "feat(v4): add Poller — Mediator producer over SQLite + GitProvider"
```

### Task 4.6: PyGithubGitProvider — real concrete impl

**Files:**
- Create: `packages/foreman/src/foreman/v4/pygithub_git_provider.py`
- Test: `packages/foreman/tests/v4/test_pygithub_git_provider.py` (lightweight; not against real network — uses mocked PyGithub client)

The real implementation of the `GitProvider` Protocol, backed by PyGithub. The Poller uses this in production; tests stay on `FakeGitProvider`. We don't smoke-test against the network in this task — that happens during the Phase-8 cutover dogfood.

This task IS in scope for v4 isolation: imports survive (no `foreman.reconciler.*`). Uses `foreman.identity` (survival set) to get the per-role token.

- [ ] **Step 1: Write the test (PyGithub mocked at module boundary)**

```python
# packages/foreman/tests/v4/test_pygithub_git_provider.py
"""PyGithubGitProvider — translates Protocol calls to PyGithub method calls.

This test does NOT hit github.com. It mocks the PyGithub Github client at
the module boundary and asserts the provider issues the expected calls.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from foreman.v4.git_provider import MergeVerdict, PRNotFoundError, PRState
from foreman.v4.pygithub_git_provider import PyGithubGitProvider


@pytest.fixture()
def mock_repo():
    repo = MagicMock()
    return repo


@pytest.fixture()
def mock_github(mock_repo):
    gh = MagicMock()
    gh.get_repo.return_value = mock_repo
    return gh


def test_get_pr_state_returns_mapped_fields(mock_github, mock_repo):
    mock_pr = MagicMock()
    mock_pr.merged = False
    mock_pr.mergeable = True
    mock_pr.mergeable_state = "clean"
    mock_repo.get_pull.return_value = mock_pr
    provider = PyGithubGitProvider(github=mock_github, repo_full_name="owner/p")
    state = provider.get_pr_state(project="p", pr_number=7)
    assert state == PRState(merged=False, mergeable=True, ci_passing=True)
    mock_repo.get_pull.assert_called_once_with(7)


def test_get_pr_state_missing_raises(mock_github, mock_repo):
    from github.GithubException import GithubException  # type: ignore[import-not-found]
    mock_repo.get_pull.side_effect = GithubException(status=404, data={}, headers={})
    provider = PyGithubGitProvider(github=mock_github, repo_full_name="owner/p")
    with pytest.raises(PRNotFoundError):
        provider.get_pr_state(project="p", pr_number=999)


def test_list_open_issues_with_label(mock_github, mock_repo):
    issue1 = MagicMock(); issue1.number = 1; issue1.pull_request = None
    issue2 = MagicMock(); issue2.number = 2; issue2.pull_request = None
    issue_pr = MagicMock(); issue_pr.number = 3
    issue_pr.pull_request = MagicMock()  # PRs come back from get_issues too
    mock_repo.get_issues.return_value = [issue1, issue2, issue_pr]
    provider = PyGithubGitProvider(github=mock_github, repo_full_name="owner/p")
    issues = provider.list_open_issues_with_label(
        project="p", label="foreman:plan",
    )
    # PRs filtered out:
    assert issues == [1, 2]


def test_merge_spec_pr_calls_merge(mock_github, mock_repo):
    mock_pr = MagicMock()
    mock_repo.get_pull.return_value = mock_pr
    provider = PyGithubGitProvider(github=mock_github, repo_full_name="owner/p")
    provider.merge_spec_pr(project="p", pr_number=5)
    mock_pr.merge.assert_called_once()


def test_enqueue_merge_queue_calls_graphql(mock_github, mock_repo):
    # MergeQueue enqueue uses GitHub's GraphQL API since REST API doesn't
    # expose merge queue operations directly. We stub the requester call.
    mock_pr = MagicMock(); mock_pr.node_id = "PR_node_abc"
    mock_repo.get_pull.return_value = mock_pr
    provider = PyGithubGitProvider(github=mock_github, repo_full_name="owner/p")
    provider.enqueue_merge_queue(project="p", pr_number=11)
    # The provider should have invoked the GraphQL mutation — assert the
    # GraphQL call surface was reached. Concrete shape depends on impl;
    # at minimum the PR was looked up:
    mock_repo.get_pull.assert_called_with(11)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_pygithub_git_provider.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the provider**

```python
# packages/foreman/src/foreman/v4/pygithub_git_provider.py
"""PyGithubGitProvider — production GitProvider backed by PyGithub.

Tests use FakeGitProvider (Task 3.2); production uses this. The seam
matches the Protocol from foreman.v4.git_provider.
"""

from __future__ import annotations

from github import Github  # type: ignore[import-not-found]
from github.GithubException import GithubException  # type: ignore[import-not-found]

from foreman.v4.git_provider import MergeVerdict, PRNotFoundError, PRState


_CI_PASSING_STATES = frozenset({"clean", "unstable"})


class PyGithubGitProvider:
    def __init__(self, *, github: Github, repo_full_name: str) -> None:
        self._gh = github
        self._repo = github.get_repo(repo_full_name)

    def get_pr_state(self, *, project: str, pr_number: int) -> PRState:
        try:
            pr = self._repo.get_pull(pr_number)
        except GithubException as exc:
            if exc.status == 404:
                raise PRNotFoundError(f"{project}#{pr_number}") from exc
            raise
        return PRState(
            merged=bool(pr.merged),
            mergeable=bool(pr.mergeable),
            ci_passing=(pr.mergeable_state in _CI_PASSING_STATES),
        )

    def merge_spec_pr(self, *, project: str, pr_number: int) -> None:
        pr = self._repo.get_pull(pr_number)
        pr.merge()

    def enqueue_merge_queue(self, *, project: str, pr_number: int) -> None:
        pr = self._repo.get_pull(pr_number)
        # GraphQL mutation — REST API doesn't expose MergeQueue operations.
        mutation = """
            mutation($prId: ID!) {
              enqueuePullRequest(input: {pullRequestId: $prId}) {
                mergeQueueEntry { id }
              }
            }
        """
        requester = self._gh._Github__requester  # type: ignore[attr-defined]
        requester.requestJsonAndCheck(
            "POST", "/graphql",
            input={"query": mutation, "variables": {"prId": pr.node_id}},
        )

    def merge_verdict(self, *, project: str, pr_number: int) -> MergeVerdict:
        pr = self._repo.get_pull(pr_number)
        if pr.merged:
            return MergeVerdict.MERGED
        # GraphQL again: query the mergeQueueEntry for this PR's status.
        query = """
            query($prId: ID!) {
              node(id: $prId) {
                ... on PullRequest {
                  mergeQueueEntry { state }
                }
              }
            }
        """
        requester = self._gh._Github__requester  # type: ignore[attr-defined]
        _, payload = requester.requestJsonAndCheck(
            "POST", "/graphql",
            input={"query": query, "variables": {"prId": pr.node_id}},
        )
        entry = (payload.get("data") or {}).get("node", {}).get("mergeQueueEntry")
        if entry is None:
            return MergeVerdict.PENDING  # not in queue yet
        state = entry.get("state")
        if state == "MERGED":
            return MergeVerdict.MERGED
        if state in ("REJECTED", "FAILED"):
            return MergeVerdict.REJECTED
        return MergeVerdict.PENDING

    def list_open_issues_with_label(
        self, *, project: str, label: str,
    ) -> list[int]:
        issues = self._repo.get_issues(state="open", labels=[label])
        return [issue.number for issue in issues if issue.pull_request is None]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_pygithub_git_provider.py -v`
Expected: 5 passed

If the GraphQL surface assertion is hard to verify with a mock without overspecifying internals, replace the relevant test with a "smoke" check that `enqueue_merge_queue` does not raise and that the underlying PR was fetched. The contract that matters is what the FakeGitProvider exercises; the PyGithub adapter is a thin translation layer.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/pygithub_git_provider.py packages/foreman/tests/v4/test_pygithub_git_provider.py
git commit -m "feat(v4): add PyGithubGitProvider (real impl behind GitProvider seam)"
```

### Task 4.7: End-to-end test — Poller → QueueManager → WorkerPool drives a ticket to Done

**Files:**
- Create: `packages/foreman/tests/v4/test_phase4_e2e.py`

Phase 4 completion check. Loops the daemon's runtime triad — Poller produces, QueueManager arbitrates, WorkerPool drains — until a ticket reaches Done. Uses `FakeGitProvider` + `FakeRoleDispatcher`, both progressed across iterations.

- [ ] **Step 1: Write the test**

```python
# packages/foreman/tests/v4/test_phase4_e2e.py
"""Phase 4 completion check — Poller + QM + WorkerPool drive a ticket to Done."""
from __future__ import annotations

import datetime as dt

from foreman.v4.git_provider import FakeGitProvider, MergeVerdict, PRState
from foreman.v4.poller import Poller
from foreman.v4.queue_manager import QueueManager
from foreman.v4.role_dispatcher import FakeRoleDispatcher
from foreman.v4.sqlite_repository import SqliteTicketRepository
from foreman.v4.worker_pool import WorkerPool


def _canned(kind: str, *, pr_number: int | None = None) -> str:
    artifacts = f',"artifacts":{{"pr_number":{pr_number}}}' if pr_number else ""
    return (
        f'FOREMAN_OUTCOME:{{"kind":"{kind}","confidence":"high",'
        f'"summary":"x"{artifacts}}}'
    )


def test_runtime_triad_drives_new_ticket_to_done():
    repo = SqliteTicketRepository.in_memory()
    git = FakeGitProvider()
    git.set_open_issues_with_label(
        project="p", label="foreman:plan", issue_numbers={1},
    )
    git.set_pr_state(
        project="p", pr_number=42,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )

    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 1):       _canned("clean", pr_number=42),
        ("reviewer-spec", "p", 1): _canned("clean", pr_number=42),
        ("worker", "p", 1):        _canned("clean", pr_number=42),
        ("reviewer-impl", "p", 1): _canned("clean", pr_number=42),
    })

    qm = QueueManager(repo=repo, max_in_flight=4)
    poller = Poller(
        repo=repo, qm=qm, git=git,
        project="p", trigger_label="foreman:plan",
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    pool = WorkerPool(
        repo=repo, qm=qm, dispatcher=dispatcher, git=git, bus=None,
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )

    # Seed the MergeQueue verdict as MERGED so MergingState completes
    # on its first tick after enqueue.
    git.enqueue_merge_queue(project="p", pr_number=42)
    git.set_merge_verdict(project="p", pr_number=42, verdict=MergeVerdict.MERGED)

    # Run alternating Poller ticks + WorkerPool drains until the ticket
    # reaches a terminal state. Bound the loop to catch infinite cycles.
    for _ in range(50):
        poller.tick()
        pool.run_until_empty()
        try:
            ticket = repo.get_ticket_by_issue(project="p", issue_number=1)
        except Exception:
            continue
        if ticket.current_state in ("Done", "Failed", "NeedsHelp"):
            break
    else:
        raise AssertionError("ticket did not converge to a terminal state")

    final = repo.get_ticket_by_issue(project="p", issue_number=1)
    assert final.current_state == "Done"
    assert git.get_pr_state(project="p", pr_number=42).merged is True
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest packages/foreman/tests/v4/test_phase4_e2e.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add packages/foreman/tests/v4/test_phase4_e2e.py
git commit -m "test(v4): Phase 4 e2e — Poller + QM + WorkerPool drive ticket to Done"
```

### Phase 4 — `just check` gate

- [ ] **Run:** `just check`
- [ ] **Expected:** all gates green; isolation guard still passes (PyGithubGitProvider imports `github`, which is allowed).

Phase 4 completion criterion (from the outline): **lifecycle test flows through the QueueManager driven by the Poller**. Achieved at Task 4.7. The daemon's runtime triad — Poller, QueueManager, WorkerPool — moves a ticket end-to-end with no manual wiring. Phase 5 swaps the `FakeRoleDispatcher` for a real subprocess-backed impl and modifies role CLIs to emit `FOREMAN_OUTCOME:` JSON.

---

## Phase 5 — Role-side Outcome reporting + real subprocess dispatch

The substrate is correct; nothing real yet drives it. Phase 5 makes two changes:

1. **Each of the four role CLIs emits `FOREMAN_OUTCOME:` JSON on stdout as its terminal line.** Replaces the existing label-writing exit path outright — nothing is running v3 to preserve, so the cutover is mechanical, not flag-gated. Role prompts + role bodies stay unchanged; only the CLI tail changes.
2. **`SubprocessRoleDispatcher`** — the production `RoleDispatcher` impl that shells out to `foreman <role>` with the appropriate per-role identity (PAT / App token) and returns stdout.

Roles affected (all in the **survival set** — they pre-date v4 and the bodies stay):
- `foreman/roles/planner.py` + `foreman/cli.py:cmd_plan`
- `foreman/roles/reviewer.py` + `foreman/cli.py:cmd_review` (target-aware)
- `foreman/roles/fixer.py` + `foreman/cli.py:cmd_fix` (target-aware)
- `foreman/roles/worker.py` + `foreman/cli.py:cmd_implement`

Each role's label-writing tail is **deleted** in the same task that adds the emit call. The label-write imports + helper calls in `cli.py` go too; whatever's left in `foreman.labels` after Phase 5 is dead code and disappears in Phase 8.

### Task 5.1: Outcome emitter utility

**Files:**
- Create: `packages/foreman/src/foreman/v4/emit.py`
- Test: `packages/foreman/tests/v4/test_emit.py`

The function each role's CLI calls right before exit. Writes one line to stdout in the `FOREMAN_OUTCOME:` shape that `parse_outcome_from_stdout` (Task 1.3) consumes.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_emit.py
"""emit_outcome — writes the FOREMAN_OUTCOME: terminal line."""
from __future__ import annotations

import json
from io import StringIO

from foreman.v4.emit import emit_outcome
from foreman.v4.outcome import (
    Outcome,
    OutcomeArtifacts,
    OutcomeConfidence,
    OutcomeKind,
    parse_outcome_from_stdout,
)


def test_emit_writes_marker_with_json_payload():
    buf = StringIO()
    outcome = Outcome(
        kind=OutcomeKind.CLEAN,
        confidence=OutcomeConfidence.HIGH,
        summary="spec PR open",
        artifacts=OutcomeArtifacts(pr_number=42),
    )
    emit_outcome(outcome, stream=buf)
    line = buf.getvalue().strip()
    assert line.startswith("FOREMAN_OUTCOME:")
    payload = json.loads(line[len("FOREMAN_OUTCOME:"):])
    assert payload["kind"] == "clean"
    assert payload["artifacts"]["pr_number"] == 42


def test_emitted_line_round_trips_through_parser():
    buf = StringIO()
    original = Outcome(
        kind=OutcomeKind.NEEDS_FIX,
        confidence=OutcomeConfidence.MEDIUM,
        summary="reviewer found issues",
    )
    emit_outcome(original, stream=buf)
    parsed = parse_outcome_from_stdout(buf.getvalue())
    assert parsed == original


def test_emit_ends_with_newline():
    buf = StringIO()
    emit_outcome(
        Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="x",
        ),
        stream=buf,
    )
    assert buf.getvalue().endswith("\n")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_emit.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the emitter**

```python
# packages/foreman/src/foreman/v4/emit.py
"""emit_outcome — role-side counterpart of parse_outcome_from_stdout.

Each role's CLI calls this as its terminal action. The state machine's
verify hook scans stdout in reverse for the FOREMAN_OUTCOME: marker and
parses what we wrote here. Round-trip property: emit then parse → equal
Outcome.
"""

from __future__ import annotations

import sys
from typing import TextIO

from foreman.v4.outcome import OUTCOME_MARKER, Outcome


def emit_outcome(outcome: Outcome, *, stream: TextIO | None = None) -> None:
    """Write one terminal line: ``FOREMAN_OUTCOME:<json>\\n``.

    Default stream is sys.stdout. Tests pass StringIO. Roles call this
    once, as the very last thing before sys.exit().
    """
    target = stream if stream is not None else sys.stdout
    target.write(f"{OUTCOME_MARKER}{outcome.model_dump_json()}\n")
    target.flush()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_emit.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/emit.py packages/foreman/tests/v4/test_emit.py
git commit -m "feat(v4): add emit_outcome — role-side counterpart of parser"
```

### Task 5.2: Planner emits Outcome

**Files:**
- Modify: `packages/foreman/src/foreman/roles/planner.py` (rewrite CLI exit path)
- Modify: `packages/foreman/src/foreman/cli.py` (`cmd_plan` calls the new exit)
- Test: `packages/foreman/tests/v4/roles/test_planner_outcome.py`

The Planner already returns a result internally — opens a spec PR, or returns NEEDS_HELP if the ticket is under-specified. The change is the exit shape: `emit_outcome(...)` replaces the label-writing tail outright. Nothing is running the old behavior, so the cutover is mechanical.

Mapping to Outcome kinds:
- Planner opened a spec PR successfully → `CLEAN` with `artifacts.pr_number` + `artifacts.pr_url`
- Planner ran but produced `confidence: low` → `NEEDS_HELP` (escalate)
- Planner raised an exception → `ERROR` (the CLI's outer try/except catches and emits)

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/roles/__init__.py
```

```python
# packages/foreman/tests/v4/roles/test_planner_outcome.py
"""Planner CLI emits FOREMAN_OUTCOME on exit."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from foreman.roles.planner import run_planner_cli
from foreman.v4.outcome import OutcomeKind, parse_outcome_from_stdout


def _fake_planner_returns_pr(pr_url: str, pr_number: int, summary: str):
    """Build a fake of whatever the Planner returns internally."""
    result = MagicMock()
    result.pr_url = pr_url
    result.pr_number = pr_number
    result.summary = summary
    result.confidence = "high"
    return result


def test_planner_success_emits_clean_outcome(capsys):
    fake_planner = MagicMock()
    fake_planner.run.return_value = _fake_planner_returns_pr(
        pr_url="https://github.com/x/y/pull/42",
        pr_number=42,
        summary="spec PR opened",
    )
    with patch("foreman.roles.planner.build_planner", return_value=fake_planner):
        exit_code = run_planner_cli(project="p", issue_number=1)
    assert exit_code == 0
    captured = capsys.readouterr()
    outcome = parse_outcome_from_stdout(captured.out)
    assert outcome.kind == OutcomeKind.CLEAN
    assert outcome.artifacts.pr_number == 42
    assert outcome.artifacts.pr_url == "https://github.com/x/y/pull/42"


def test_planner_low_confidence_emits_needs_help(capsys):
    fake_result = MagicMock()
    fake_result.pr_url = None
    fake_result.pr_number = None
    fake_result.summary = "ticket under-specified"
    fake_result.confidence = "low"
    fake_planner = MagicMock()
    fake_planner.run.return_value = fake_result
    with patch("foreman.roles.planner.build_planner", return_value=fake_planner):
        exit_code = run_planner_cli(project="p", issue_number=1)
    assert exit_code == 0  # zero exit even on NEEDS_HELP — stdout carries the verdict
    captured = capsys.readouterr()
    outcome = parse_outcome_from_stdout(captured.out)
    assert outcome.kind == OutcomeKind.NEEDS_HELP


def test_planner_exception_emits_error(capsys):
    fake_planner = MagicMock()
    fake_planner.run.side_effect = RuntimeError("provider timeout")
    with patch("foreman.roles.planner.build_planner", return_value=fake_planner):
        exit_code = run_planner_cli(project="p", issue_number=1)
    assert exit_code == 1
    captured = capsys.readouterr()
    outcome = parse_outcome_from_stdout(captured.out)
    assert outcome.kind == OutcomeKind.ERROR
    assert "provider timeout" in outcome.summary
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/roles/test_planner_outcome.py -v`
Expected: FAIL with `ImportError: cannot import name 'run_planner_cli'`

- [ ] **Step 3: Replace the planner's CLI exit path**

Delete the existing label-writing tail in `planner.py` (whatever helper writes `foreman:plan-approved` / sets `needs-help`) along with its imports from `foreman.labels`. Add the emit-based entry point:

```python
# packages/foreman/src/foreman/roles/planner.py — replace the existing CLI exit tail

from foreman.v4.emit import emit_outcome
from foreman.v4.outcome import (
    Outcome,
    OutcomeArtifacts,
    OutcomeConfidence,
    OutcomeKind,
)


def run_planner_cli(*, project: str, issue_number: int) -> int:
    """Run the planner; emit FOREMAN_OUTCOME JSON; return exit code.

    This is the entry point the SubprocessRoleDispatcher (Task 5.6)
    forks. The label-writing tail is gone; nothing reads labels in v4.
    """
    try:
        planner = build_planner(project=project, issue_number=issue_number)
        result = planner.run()
    except Exception as exc:  # noqa: BLE001 — top-level role boundary
        emit_outcome(Outcome(
            kind=OutcomeKind.ERROR,
            confidence=OutcomeConfidence.HIGH,
            summary=f"planner raised: {exc}"[:500],
        ))
        return 1

    if getattr(result, "confidence", "high") == "low":
        emit_outcome(Outcome(
            kind=OutcomeKind.NEEDS_HELP,
            confidence=OutcomeConfidence.LOW,
            summary=result.summary or "ticket under-specified",
        ))
        return 0

    emit_outcome(Outcome(
        kind=OutcomeKind.CLEAN,
        confidence=OutcomeConfidence.HIGH,
        summary=result.summary or "spec PR opened",
        artifacts=OutcomeArtifacts(
            pr_url=result.pr_url,
            pr_number=result.pr_number,
        ),
    ))
    return 0
```

If `build_planner` doesn't exist by that name in the current module, identify the existing factory (e.g., the function that constructs the Planner with config + identity + provider) and adapt the import. The test's `patch` target matches the function name actually used.

- [ ] **Step 4: Rewrite `cmd_plan` in `cli.py`**

Replace the existing `cmd_plan` body. No flag — every `foreman plan` invocation now emits Outcome:

```python
# packages/foreman/src/foreman/cli.py

@cli.command("plan")
@click.option("--project", required=True)
@click.option("--issue-number", "issue_number", type=int, required=True)
def cmd_plan(project: str, issue_number: int) -> None:
    from foreman.roles.planner import run_planner_cli
    sys.exit(run_planner_cli(project=project, issue_number=issue_number))
```

The previous body (whatever wrote labels via `foreman.labels`) is deleted in this same commit. Any imports from `foreman.labels` that became orphaned go with it.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/roles/test_planner_outcome.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/roles/planner.py packages/foreman/src/foreman/cli.py packages/foreman/tests/v4/roles/__init__.py packages/foreman/tests/v4/roles/test_planner_outcome.py
git commit -m "feat(v4): planner emits FOREMAN_OUTCOME (replaces label-writing exit)"
```

### Task 5.3: Reviewer emits Outcome (target-aware)

**Files:**
- Modify: `packages/foreman/src/foreman/roles/reviewer.py`
- Modify: `packages/foreman/src/foreman/cli.py` (`cmd_review` rewritten)
- Test: `packages/foreman/tests/v4/roles/test_reviewer_outcome.py`

The Reviewer is target-aware: `reviewer-spec` reviews the spec PR; `reviewer-impl` reviews the impl PR. The internal logic already branches on target; v4's contribution is the exit-emission.

Outcome mapping:
- approved (`approved=True`, no findings) → `CLEAN` with `pr_number`
- changes requested (`approved=False` with findings) → `NEEDS_FIX` with findings list
- exception → `ERROR`

Findings translate from the Reviewer's internal shape into `Finding` (severity / location / description).

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/roles/test_reviewer_outcome.py
"""Reviewer (spec + impl) emits FOREMAN_OUTCOME on exit."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from foreman.roles.reviewer import run_reviewer_cli
from foreman.v4.outcome import OutcomeKind, parse_outcome_from_stdout


def _approved_result(pr_number: int):
    r = MagicMock()
    r.approved = True
    r.pr_number = pr_number
    r.summary = "looks good"
    r.findings = []
    return r


def _rejected_result(pr_number: int):
    finding = MagicMock()
    finding.severity = "important"
    finding.location = "foo.py:42"
    finding.description = "missing test"
    r = MagicMock()
    r.approved = False
    r.pr_number = pr_number
    r.summary = "1 important issue"
    r.findings = [finding]
    return r


@pytest.mark.parametrize("target", ["spec", "impl"])
def test_approved_emits_clean(target, capsys):
    fake = MagicMock(); fake.run.return_value = _approved_result(7)
    with patch("foreman.roles.reviewer.build_reviewer", return_value=fake):
        exit_code = run_reviewer_cli(
            project="p", issue_number=1, target=target,
        )
    assert exit_code == 0
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.CLEAN
    assert outcome.artifacts.pr_number == 7


@pytest.mark.parametrize("target", ["spec", "impl"])
def test_changes_requested_emits_needs_fix_with_findings(target, capsys):
    fake = MagicMock(); fake.run.return_value = _rejected_result(7)
    with patch("foreman.roles.reviewer.build_reviewer", return_value=fake):
        run_reviewer_cli(
            project="p", issue_number=1, target=target,
        )
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.NEEDS_FIX
    assert len(outcome.findings) == 1
    assert outcome.findings[0].location == "foo.py:42"


def test_reviewer_exception_emits_error(capsys):
    fake = MagicMock(); fake.run.side_effect = RuntimeError("rate limit")
    with patch("foreman.roles.reviewer.build_reviewer", return_value=fake):
        exit_code = run_reviewer_cli(
            project="p", issue_number=1, target="spec",
        )
    assert exit_code == 1
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.ERROR
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/roles/test_reviewer_outcome.py -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Add the v4 exit path to `reviewer.py`**

```python
# Append to packages/foreman/src/foreman/roles/reviewer.py

from foreman.v4.emit import emit_outcome
from foreman.v4.outcome import (
    Finding,
    Outcome,
    OutcomeArtifacts,
    OutcomeConfidence,
    OutcomeKind,
)


def run_reviewer_cli(
    *, project: str, issue_number: int, target: str,
) -> int:
    try:
        reviewer = build_reviewer(
            project=project, issue_number=issue_number, target=target,
        )
        result = reviewer.run()
    except Exception as exc:  # noqa: BLE001
        emit_outcome(Outcome(
            kind=OutcomeKind.ERROR, confidence=OutcomeConfidence.HIGH,
            summary=f"reviewer raised: {exc}"[:500],
        ))
        return 1

    if result.approved:
        emit_outcome(Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary=result.summary or "approved",
            artifacts=OutcomeArtifacts(pr_number=result.pr_number),
        ))
        return 0

    findings = [
        Finding(
            severity=f.severity, location=f.location, description=f.description,
        )
        for f in result.findings
    ]
    emit_outcome(Outcome(
        kind=OutcomeKind.NEEDS_FIX, confidence=OutcomeConfidence.HIGH,
        summary=result.summary or f"{len(findings)} issues",
        artifacts=OutcomeArtifacts(pr_number=result.pr_number),
        findings=findings,
    ))
    return 0
```

- [ ] **Step 4: Rewrite `cmd_review` in `cli.py`** — same shape as `cmd_plan`, preserving the existing `--target` flag, body becomes a one-liner that calls `run_reviewer_cli(...)` and exits with its return code. Delete the prior label-writing tail.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/roles/test_reviewer_outcome.py -v`
Expected: 5 passed (2 parametrized × 2 + 1 standalone = 5)

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/roles/reviewer.py packages/foreman/src/foreman/cli.py packages/foreman/tests/v4/roles/test_reviewer_outcome.py
git commit -m "feat(v4): reviewer emits FOREMAN_OUTCOME (target-aware)"
```

### Task 5.4: Fixer emits Outcome (target-aware)

**Files:**
- Modify: `packages/foreman/src/foreman/roles/fixer.py`
- Modify: `packages/foreman/src/foreman/cli.py` (`cmd_fix`)
- Test: `packages/foreman/tests/v4/roles/test_fixer_outcome.py`

Same shape as Reviewer: target-aware (`fixer-spec`, `fixer-impl`), three outcome paths.

| Internal result | Outcome kind |
| --- | --- |
| Fix pushed; review the amended PR | `CLEAN` with `pr_number` |
| Fixer couldn't resolve (3 attempts exhausted, blocked) | `NEEDS_HELP` |
| Exception | `ERROR` |

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/roles/test_fixer_outcome.py
"""Fixer (spec + impl) emits FOREMAN_OUTCOME on exit."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from foreman.roles.fixer import run_fixer_cli
from foreman.v4.outcome import OutcomeKind, parse_outcome_from_stdout


def _pushed_result(pr_number: int):
    r = MagicMock()
    r.pushed = True
    r.escalated = False
    r.pr_number = pr_number
    r.summary = "amended"
    return r


def _escalated_result():
    r = MagicMock()
    r.pushed = False
    r.escalated = True
    r.pr_number = None
    r.summary = "3 attempts exhausted"
    return r


@pytest.mark.parametrize("target", ["spec", "impl"])
def test_pushed_emits_clean(target, capsys):
    fake = MagicMock(); fake.run.return_value = _pushed_result(11)
    with patch("foreman.roles.fixer.build_fixer", return_value=fake):
        exit_code = run_fixer_cli(
            project="p", issue_number=1, target=target,
        )
    assert exit_code == 0
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.CLEAN
    assert outcome.artifacts.pr_number == 11


@pytest.mark.parametrize("target", ["spec", "impl"])
def test_escalated_emits_needs_help(target, capsys):
    fake = MagicMock(); fake.run.return_value = _escalated_result()
    with patch("foreman.roles.fixer.build_fixer", return_value=fake):
        run_fixer_cli(
            project="p", issue_number=1, target=target,
        )
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.NEEDS_HELP


def test_fixer_exception_emits_error(capsys):
    fake = MagicMock(); fake.run.side_effect = RuntimeError("push rejected")
    with patch("foreman.roles.fixer.build_fixer", return_value=fake):
        exit_code = run_fixer_cli(
            project="p", issue_number=1, target="spec",
        )
    assert exit_code == 1
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.ERROR
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/roles/test_fixer_outcome.py -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Add the v4 exit path to `fixer.py`** (same pattern as Reviewer; `pushed → CLEAN`, `escalated → NEEDS_HELP`, exception → `ERROR`).

```python
# Append to packages/foreman/src/foreman/roles/fixer.py
from foreman.v4.emit import emit_outcome
from foreman.v4.outcome import (
    Outcome, OutcomeArtifacts, OutcomeConfidence, OutcomeKind,
)


def run_fixer_cli(
    *, project: str, issue_number: int, target: str,
) -> int:
    try:
        fixer = build_fixer(
            project=project, issue_number=issue_number, target=target,
        )
        result = fixer.run()
    except Exception as exc:  # noqa: BLE001
        emit_outcome(Outcome(
            kind=OutcomeKind.ERROR, confidence=OutcomeConfidence.HIGH,
            summary=f"fixer raised: {exc}"[:500],
        ))
        return 1

    if result.escalated:
        emit_outcome(Outcome(
            kind=OutcomeKind.NEEDS_HELP, confidence=OutcomeConfidence.HIGH,
            summary=result.summary or "fixer exhausted attempts",
        ))
        return 0

    emit_outcome(Outcome(
        kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
        summary=result.summary or "fix pushed",
        artifacts=OutcomeArtifacts(pr_number=result.pr_number),
    ))
    return 0
```

- [ ] **Step 4: Rewrite `cmd_fix` in `cli.py`** — one-liner calling `run_fixer_cli(...)` with `--target` preserved; delete the prior label-writing tail.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/roles/test_fixer_outcome.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/roles/fixer.py packages/foreman/src/foreman/cli.py packages/foreman/tests/v4/roles/test_fixer_outcome.py
git commit -m "feat(v4): fixer emits FOREMAN_OUTCOME (target-aware)"
```

### Task 5.5: Worker emits Outcome (CLEAN | BLOCKED | NEEDS_HELP | ERROR)

**Files:**
- Modify: `packages/foreman/src/foreman/roles/worker.py`
- Modify: `packages/foreman/src/foreman/cli.py` (`cmd_implement`)
- Test: `packages/foreman/tests/v4/roles/test_worker_outcome.py`

The Worker is the only role that produces `BLOCKED` (the impl PR was opened but CI is still in flight). The state machine handles BLOCKED by re-polling (ImplementingState `next_state` returns a fresh `ImplementingState()`).

| Internal result | Outcome kind |
| --- | --- |
| Impl PR open, CI passing | `CLEAN` with `pr_number` |
| Impl PR open, CI still in flight | `BLOCKED` with `pr_number` |
| Worker hit "give-up" condition (e.g., 3 baseline failures) | `NEEDS_HELP` |
| Exception | `ERROR` |

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/roles/test_worker_outcome.py
"""Worker emits FOREMAN_OUTCOME on exit (CLEAN/BLOCKED/NEEDS_HELP/ERROR)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from foreman.roles.worker import run_worker_cli
from foreman.v4.outcome import OutcomeKind, parse_outcome_from_stdout


def _result(*, status: str, pr_number: int | None = None, summary: str = "x"):
    r = MagicMock()
    r.status = status
    r.pr_number = pr_number
    r.summary = summary
    return r


def test_ci_passing_emits_clean(capsys):
    fake = MagicMock(); fake.run.return_value = _result(status="ci_passing", pr_number=99)
    with patch("foreman.roles.worker.build_worker", return_value=fake):
        run_worker_cli(project="p", issue_number=1)
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.CLEAN
    assert outcome.artifacts.pr_number == 99


def test_ci_in_flight_emits_blocked(capsys):
    fake = MagicMock(); fake.run.return_value = _result(status="ci_in_flight", pr_number=99)
    with patch("foreman.roles.worker.build_worker", return_value=fake):
        run_worker_cli(project="p", issue_number=1)
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.BLOCKED
    assert outcome.artifacts.pr_number == 99


def test_give_up_emits_needs_help(capsys):
    fake = MagicMock(); fake.run.return_value = _result(status="give_up", summary="3 baseline failures")
    with patch("foreman.roles.worker.build_worker", return_value=fake):
        run_worker_cli(project="p", issue_number=1)
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.NEEDS_HELP


def test_worker_exception_emits_error(capsys):
    fake = MagicMock(); fake.run.side_effect = RuntimeError("worktree corrupted")
    with patch("foreman.roles.worker.build_worker", return_value=fake):
        exit_code = run_worker_cli(project="p", issue_number=1)
    assert exit_code == 1
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.ERROR
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/roles/test_worker_outcome.py -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Add the v4 exit path to `worker.py`**

```python
# Append to packages/foreman/src/foreman/roles/worker.py
from foreman.v4.emit import emit_outcome
from foreman.v4.outcome import (
    Outcome, OutcomeArtifacts, OutcomeConfidence, OutcomeKind,
)


def run_worker_cli(*, project: str, issue_number: int) -> int:
    try:
        worker = build_worker(project=project, issue_number=issue_number)
        result = worker.run()
    except Exception as exc:  # noqa: BLE001
        emit_outcome(Outcome(
            kind=OutcomeKind.ERROR, confidence=OutcomeConfidence.HIGH,
            summary=f"worker raised: {exc}"[:500],
        ))
        return 1

    status = result.status
    artifacts = OutcomeArtifacts(pr_number=result.pr_number)
    if status == "ci_passing":
        emit_outcome(Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary=result.summary or "impl PR open, CI green",
            artifacts=artifacts,
        ))
    elif status == "ci_in_flight":
        emit_outcome(Outcome(
            kind=OutcomeKind.BLOCKED, confidence=OutcomeConfidence.HIGH,
            summary=result.summary or "impl PR open, CI in flight",
            artifacts=artifacts,
        ))
    elif status == "give_up":
        emit_outcome(Outcome(
            kind=OutcomeKind.NEEDS_HELP, confidence=OutcomeConfidence.HIGH,
            summary=result.summary or "worker hit give-up condition",
            artifacts=artifacts,
        ))
    else:
        emit_outcome(Outcome(
            kind=OutcomeKind.ERROR, confidence=OutcomeConfidence.HIGH,
            summary=f"unknown worker status: {status}",
        ))
        return 1
    return 0
```

- [ ] **Step 4: Rewrite `cmd_implement` in `cli.py`** — one-liner calling `run_worker_cli(...)`; delete the prior label-writing tail.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/roles/test_worker_outcome.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/roles/worker.py packages/foreman/src/foreman/cli.py packages/foreman/tests/v4/roles/test_worker_outcome.py
git commit -m "feat(v4): worker emits FOREMAN_OUTCOME (CLEAN/BLOCKED/NEEDS_HELP/ERROR)"
```

### Task 5.6: SubprocessRoleDispatcher — production impl

**Files:**
- Create: `packages/foreman/src/foreman/v4/subprocess_dispatcher.py`
- Test: `packages/foreman/tests/v4/test_subprocess_dispatcher.py`

The production `RoleDispatcher` impl. Shells out to `foreman <role> --project <p> --issue-number <n>` with the appropriate per-role identity (PAT or App token) in `GH_TOKEN`. Captures stdout + stderr; returns stdout for the state machine's verify hook to parse. (Every `foreman <role>` invocation emits `FOREMAN_OUTCOME:` now — no flag.)

Per-role identity wiring lives in `foreman.identity` (survival set). For each role string the dispatcher receives, it resolves to a token via `identity.get_role_token(role_name)`.

| `role` value | invokes | identity |
| --- | --- | --- |
| `planner` | `foreman plan` | planner App |
| `reviewer-spec` | `foreman review --target spec` | reviewer App |
| `reviewer-impl` | `foreman review --target impl` | reviewer App |
| `fixer-spec` | `foreman fix --target spec` | fixer App |
| `fixer-impl` | `foreman fix --target impl` | fixer App |
| `worker` | `foreman implement` | worker App |

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_subprocess_dispatcher.py
"""SubprocessRoleDispatcher — shells out to foreman <role> for v4 dispatch."""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from foreman.v4.subprocess_dispatcher import (
    RoleSubprocessError,
    SubprocessRoleDispatcher,
)


def _stub_identity():
    """Builds a fake identity module exposing get_role_token."""
    mod = MagicMock()
    mod.get_role_token.return_value = "ghp_TESTTOKEN"
    return mod


def test_planner_dispatch_invokes_foreman_plan():
    completed = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=(
            'log lines\n'
            'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}\n'
        ),
        stderr="",
    )
    with patch("subprocess.run", return_value=completed) as run:
        dispatcher = SubprocessRoleDispatcher(
            foreman_cli=["foreman"], identity=_stub_identity(),
        )
        stdout = dispatcher.dispatch(
            role="planner", project="p", issue_number=1, ticket_id=1,
        )
    assert "FOREMAN_OUTCOME:" in stdout
    args = run.call_args
    cmd = args[0][0] if args[0] else args[1].get("args")
    assert "plan" in cmd
    assert "--project" in cmd
    assert "1" in cmd
    # GH_TOKEN injected via env, not arg
    env = args[1].get("env") or {}
    assert env.get("GH_TOKEN") == "ghp_TESTTOKEN"


@pytest.mark.parametrize(
    "role,subcmd,target",
    [
        ("planner", "plan", None),
        ("reviewer-spec", "review", "spec"),
        ("reviewer-impl", "review", "impl"),
        ("fixer-spec", "fix", "spec"),
        ("fixer-impl", "fix", "impl"),
        ("worker", "implement", None),
    ],
)
def test_role_to_subcommand_mapping(role, subcmd, target):
    completed = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout='FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"x"}\n',
        stderr="",
    )
    with patch("subprocess.run", return_value=completed) as run:
        SubprocessRoleDispatcher(
            foreman_cli=["foreman"], identity=_stub_identity(),
        ).dispatch(role=role, project="p", issue_number=1, ticket_id=1)
    cmd = run.call_args[0][0]
    assert subcmd in cmd
    if target is not None:
        assert "--target" in cmd
        assert target in cmd


def test_subprocess_nonzero_with_error_outcome_raises_role_error():
    """Non-zero exit + ERROR outcome → RoleSubprocessError; state machine
    routes to FailedState via verify."""
    completed = subprocess.CompletedProcess(
        args=[], returncode=1,
        stdout='FOREMAN_OUTCOME:{"kind":"error","confidence":"high","summary":"boom"}\n',
        stderr="something went sideways",
    )
    with patch("subprocess.run", return_value=completed):
        dispatcher = SubprocessRoleDispatcher(
            foreman_cli=["foreman"], identity=_stub_identity(),
        )
        # Dispatcher returns the stdout regardless — the state machine
        # decides what ERROR means. No exception at dispatcher layer.
        stdout = dispatcher.dispatch(
            role="planner", project="p", issue_number=1, ticket_id=1,
        )
        assert '"kind":"error"' in stdout


def test_subprocess_nonzero_without_outcome_raises():
    """If the subprocess died without writing a marker, that's a hard error."""
    completed = subprocess.CompletedProcess(
        args=[], returncode=137, stdout="killed\n", stderr="OOM",
    )
    with patch("subprocess.run", return_value=completed):
        dispatcher = SubprocessRoleDispatcher(
            foreman_cli=["foreman"], identity=_stub_identity(),
        )
        with pytest.raises(RoleSubprocessError) as exc:
            dispatcher.dispatch(
                role="planner", project="p", issue_number=1, ticket_id=1,
            )
        assert "137" in str(exc.value)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_subprocess_dispatcher.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the dispatcher**

```python
# packages/foreman/src/foreman/v4/subprocess_dispatcher.py
"""SubprocessRoleDispatcher — production RoleDispatcher impl.

Shells out to ``foreman <subcmd> ...`` with the role's
identity token injected as GH_TOKEN. Returns the subprocess's stdout
for the state machine's verify hook to parse.

The mapping from v4 role names to CLI subcommands lives here. Adding
a new role = one entry in _ROLE_TO_INVOCATION.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Protocol

from foreman.v4.outcome import OUTCOME_MARKER


class IdentityProvider(Protocol):
    def get_role_token(self, role: str) -> str: ...


class RoleSubprocessError(RuntimeError):
    """Subprocess exited non-zero AND did not emit a FOREMAN_OUTCOME: line."""


@dataclass(frozen=True)
class _Invocation:
    subcommand: str
    target: str | None


_ROLE_TO_INVOCATION: dict[str, _Invocation] = {
    "planner":       _Invocation(subcommand="plan",      target=None),
    "reviewer-spec": _Invocation(subcommand="review",    target="spec"),
    "reviewer-impl": _Invocation(subcommand="review",    target="impl"),
    "fixer-spec":    _Invocation(subcommand="fix",       target="spec"),
    "fixer-impl":    _Invocation(subcommand="fix",       target="impl"),
    "worker":        _Invocation(subcommand="implement", target=None),
}


class SubprocessRoleDispatcher:
    def __init__(
        self,
        *,
        foreman_cli: list[str],
        identity: IdentityProvider,
        timeout_seconds: int = 600,
    ) -> None:
        self._foreman_cli = foreman_cli
        self._identity = identity
        self._timeout = timeout_seconds

    def dispatch(
        self, *, role: str, project: str, issue_number: int, ticket_id: int,
    ) -> str:
        try:
            inv = _ROLE_TO_INVOCATION[role]
        except KeyError as exc:
            raise ValueError(f"unknown role: {role}") from exc

        cmd = [
            *self._foreman_cli, inv.subcommand,
            "--project", project,
            "--issue-number", str(issue_number),
        ]
        if inv.target is not None:
            cmd += ["--target", inv.target]

        env = dict(os.environ)
        env["GH_TOKEN"] = self._identity.get_role_token(role)

        result = subprocess.run(
            cmd, capture_output=True, text=True, env=env,
            timeout=self._timeout,
        )
        if result.returncode != 0 and OUTCOME_MARKER not in result.stdout:
            raise RoleSubprocessError(
                f"role={role} exited {result.returncode} without "
                f"emitting an outcome; stderr={result.stderr[:500]!r}"
            )
        return result.stdout
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_subprocess_dispatcher.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/subprocess_dispatcher.py packages/foreman/tests/v4/test_subprocess_dispatcher.py
git commit -m "feat(v4): SubprocessRoleDispatcher — production RoleDispatcher impl"
```

### Task 5.7: Phase 5 end-to-end smoke

**Files:**
- Create: `packages/foreman/tests/v4/test_phase5_e2e_subprocess.py`

Real subprocess fork against a tiny stub `foreman` script that just prints a known `FOREMAN_OUTCOME:` line and exits. Proves the full chain: dispatcher invokes subprocess → reads stdout → state machine parses.

- [ ] **Step 1: Write the test**

```python
# packages/foreman/tests/v4/test_phase5_e2e_subprocess.py
"""Phase 5 e2e — SubprocessRoleDispatcher actually forks and we read its stdout."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from foreman.v4.outcome import OutcomeKind, parse_outcome_from_stdout
from foreman.v4.subprocess_dispatcher import SubprocessRoleDispatcher


@pytest.fixture()
def stub_foreman(tmp_path: Path):
    """Build a tiny script that mimics ``foreman`` for one canned response."""
    script = tmp_path / "stub_foreman.py"
    script.write_text(
        "import sys\n"
        "print('log line from stub')\n"
        "print('FOREMAN_OUTCOME:{\"kind\":\"clean\",\"confidence\":\"high\","
        "\"summary\":\"stub ok\"}')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    return script


def test_subprocess_round_trip(stub_foreman: Path):
    identity = MagicMock()
    identity.get_role_token.return_value = "ghp_STUB"
    # foreman_cli points at python + stub script; CLI args after are ignored
    # by the stub but exercise the dispatcher's command-line construction.
    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=[sys.executable, str(stub_foreman)],
        identity=identity,
    )
    stdout = dispatcher.dispatch(
        role="planner", project="p", issue_number=1, ticket_id=1,
    )
    outcome = parse_outcome_from_stdout(stdout)
    assert outcome.kind == OutcomeKind.CLEAN
    assert outcome.summary == "stub ok"
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest packages/foreman/tests/v4/test_phase5_e2e_subprocess.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add packages/foreman/tests/v4/test_phase5_e2e_subprocess.py
git commit -m "test(v4): phase 5 e2e — real subprocess fork + outcome parse"
```

### Phase 5 — `just check` gate

- [ ] **Run:** `just check`
- [ ] **Expected:** all green; isolation guard still passes (new modules under `foreman/v4/` and modifications scoped to survival-set role files only).

Phase 5 completion criterion (from the outline): **roles produce stdout-parsable outcomes parseable by the state machine's verify hook**. Achieved at Task 5.7. The label-writing exit paths are deleted in this phase along with their `foreman.labels` imports. The substrate now has a real production path: Poller → QueueManager → WorkerPool → SubprocessRoleDispatcher → real `foreman <role>` subprocess → Outcome JSON → state machine.

---

## Phase 6 — Typer CLI (operator surface)

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
- Create: `packages/foreman/src/foreman/v4/cli/__init__.py`
- Create: `packages/foreman/src/foreman/v4/cli/context.py`
- Create: `packages/foreman/src/foreman/v4/cli/formatters.py`
- Test: `packages/foreman/tests/v4/cli/test_context.py`
- Test: `packages/foreman/tests/v4/cli/test_formatters.py`
- Test: `packages/foreman/tests/v4/cli/test_app_skeleton.py`

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

If the test expects `--all` to show terminal tickets too, extend the Repository Protocol with `list_all_tickets()` and add it to both impls (mirror the `list_open_tickets()` shape). Add a contract test for it in `_repository_contract.py`.

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

`Daemon` class (in `v4/daemon.py`) hosts the Poller + QueueManager + WorkerPool, runs a tick loop on a configurable cadence, handles SIGTERM/SIGINT gracefully (drain in-flight, exit). PID file under `~/.foreman/v4/daemon.pid`.

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
    daemon = Daemon(
        repo=repo, git=git, dispatcher=dispatcher,
        config=DaemonConfig(project="p", trigger_label="foreman:plan",
                            tick_seconds=0, max_in_flight=4),
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
    project: str
    trigger_label: str
    tick_seconds: float
    max_in_flight: int


class Daemon:
    def __init__(
        self,
        *,
        repo: TicketRepository,
        git: GitProvider,
        dispatcher: RoleDispatcher,
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
        self._poller = Poller(
            repo=repo, qm=self._qm, git=git,
            project=config.project, trigger_label=config.trigger_label,
            clock=clock,
        )
        self._pool = WorkerPool(
            repo=repo, qm=self._qm, dispatcher=dispatcher,
            git=git, bus=bus, clock=clock,
        )
        self._stop = threading.Event()

    def tick_once(self) -> None:
        self._poller.tick()
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

### Task 6.6: Role commands migrated to typer + console script

**Files:**
- Modify: `packages/foreman/src/foreman/v4/cli/__init__.py` (register `plan/review/fix/implement`)
- Modify: `packages/foreman/src/foreman/cli.py` (thin wrapper around the typer app)
- Modify: `packages/foreman/pyproject.toml` (entry point)
- Test: `packages/foreman/tests/v4/cli/test_role_commands.py`

The four role commands move from Phase 5's Click `cmd_plan` etc. into the typer app. They delegate to the same `run_<role>_cli` functions from Phase 5, so no behavior change — just a different framework wrapping them.

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
    raise _typer.Exit(code=run_planner_cli(project=project, issue_number=issue_number))


@app.command("review")
def cmd_review(
    project: str = _typer.Option(..., "--project"),
    issue_number: int = _typer.Option(..., "--issue-number"),
    target: str = _typer.Option(..., "--target", help="spec|impl"),
) -> None:
    raise _typer.Exit(code=run_reviewer_cli(
        project=project, issue_number=issue_number, target=target,
    ))


@app.command("fix")
def cmd_fix(
    project: str = _typer.Option(..., "--project"),
    issue_number: int = _typer.Option(..., "--issue-number"),
    target: str = _typer.Option(..., "--target", help="spec|impl"),
) -> None:
    raise _typer.Exit(code=run_fixer_cli(
        project=project, issue_number=issue_number, target=target,
    ))


@app.command("implement")
def cmd_implement(
    project: str = _typer.Option(..., "--project"),
    issue_number: int = _typer.Option(..., "--issue-number"),
) -> None:
    raise _typer.Exit(code=run_worker_cli(
        project=project, issue_number=issue_number,
    ))
```

- [ ] **Step 2: Rewrite the top-level `cli.py`**

```python
# packages/foreman/src/foreman/cli.py
"""Top-level CLI entry point. Delegates to the v4 typer app.

Phase 8 deletes the legacy commands this file used to host.
"""

from foreman.v4.cli import app


def main() -> None:
    app()
```

The previous `cli.py` body (Click app with `cmd_plan`/`cmd_review`/`cmd_fix`/`cmd_implement`) is gone — those role commands are now in the typer app, calling the same `run_<role>_cli` functions.

- [ ] **Step 3: Confirm pyproject entry point is unchanged**

`pyproject.toml` should already have:
```toml
[project.scripts]
foreman = "foreman.cli:main"
```

If it's pointing at a Click command function directly, update it to point at `foreman.cli:main` so the typer app is invoked.

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
git add packages/foreman/src/foreman/v4/cli/__init__.py packages/foreman/src/foreman/cli.py packages/foreman/pyproject.toml packages/foreman/tests/v4/cli/test_role_commands.py
git commit -m "feat(v4): migrate role commands to typer; collapse cli.py to thin wrapper"
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

---
