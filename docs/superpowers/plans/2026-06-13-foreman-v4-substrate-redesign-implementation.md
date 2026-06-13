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
