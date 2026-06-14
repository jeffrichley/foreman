> **Parent plan:** [../2026-06-13-foreman-v4-substrate-redesign-implementation.md](../2026-06-13-foreman-v4-substrate-redesign-implementation.md) — read its v4 isolation principle first.
> **Spec:** [../../specs/2026-06-13-foreman-v4-substrate-redesign-design.md](../../specs/2026-06-13-foreman-v4-substrate-redesign-design.md).
> **Branch:** `feat/foreman-v4-substrate`.
> **Gate at end:** `just check` green; then stop for human review before next phase.

## Phase 7 — Rich logging + MergeQueue default + production bootstrap

The substrate, observers, role plumbing, and CLI are all in place but disconnected from production startup. Phase 7 ties them together: rich-formatted stdout + JSON-lines file logging at daemon boot, MergeQueue as the default merge mechanism, a TOML-driven config, and a single `bootstrap_cli_context()` that production startup calls to assemble everything — same builder pattern as Phase 6's `build_cli_context`, just at the layer that owns config + identity.

Phase 7 also handles **multi-project daemon support**: foreman today runs against `[projects.voice]`, `[projects.foreman]`, etc. The Phase 4 Poller was single-project; here we extend the Daemon to hold a list of `(project_config, Poller)` pairs and tick them all in one loop pass. Shared QueueManager, shared WorkerPool, per-project poller — minimum churn for multi-project support.

### Carry-overs from prior phases

**From Phase 4 (Task 4.4 review):**
- **`WorkerPool.in_flight_count()` convenience helper** — today tests reach through `qm.in_flight_count()`. If Phase 7's daemon shell wants to inspect the worker pool's load directly, add a one-liner: `def in_flight_count(self) -> int: return self._qm.in_flight_count()`. Defer if no caller materializes.

**From Phase 5 (Tasks 5.6 + 5.7 reviews):**
- **`SubprocessRoleDispatcher.timeout_seconds` is hardcoded (600s default).** Currently a constructor parameter. V4Config should expose this as a config knob — add `role_timeout_seconds: int = 600` to the `V4Config` Pydantic model + thread through `bootstrap_cli_context` to the dispatcher constructor.
- **Real-fork integration test under bootstrap harness.** Phase 5.7 e2e (`test_phase5_e2e_subprocess.py`) tests the dispatcher↔subprocess↔parser seam against a stub Python script — NOT the real `foreman` CLI. Phase 4.7 e2e uses `FakeRoleDispatcher`. No single test exercises Poller → QM → WorkerPool → SubprocessRoleDispatcher → real `foreman <role>-v4` subprocess. Add an integration test under Phase 7's bootstrap that invokes the installed `foreman` CLI end-to-end (use `uv run foreman ...` or a `pip install -e .` fixture). This is the seam where the real-foreman-binary contract first meets the v4 substrate.

### Task 7.1: JsonLinesHandler

**Files:**
- Create: `packages/foreman/src/foreman/v4/json_lines_handler.py`
- Test: `packages/foreman/tests/v4/test_json_lines_handler.py`

A `logging.Handler` subclass that writes one valid JSON object per log record. Used as the file sink for transition events; `RichHandler` covers stdout.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_json_lines_handler.py
"""JsonLinesHandler — one JSON object per log record."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from foreman.v4.json_lines_handler import JsonLinesHandler


def test_emit_writes_one_json_per_record(tmp_path: Path):
    log_path = tmp_path / "transitions.jsonl"
    handler = JsonLinesHandler(filename=str(log_path))
    log = logging.getLogger("test_jsonl")
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    try:
        log.info(json.dumps({"event": "state_entered", "ticket_id": 1}))
        log.info(json.dumps({"event": "execute_completed", "ticket_id": 1}))
    finally:
        log.removeHandler(handler)
        handler.close()

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "state_entered"
    assert json.loads(lines[1])["event"] == "execute_completed"


def test_emit_wraps_non_json_messages_safely(tmp_path: Path):
    """If a logger emits a plain string, the handler still produces valid JSON."""
    log_path = tmp_path / "transitions.jsonl"
    handler = JsonLinesHandler(filename=str(log_path))
    log = logging.getLogger("test_jsonl_safe")
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    try:
        log.warning("plain string warning")
    finally:
        log.removeHandler(handler)
        handler.close()

    line = log_path.read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    assert payload["level"] == "WARNING"
    assert payload["message"] == "plain string warning"


def test_emit_includes_timestamp(tmp_path: Path):
    log_path = tmp_path / "transitions.jsonl"
    handler = JsonLinesHandler(filename=str(log_path))
    log = logging.getLogger("test_jsonl_ts")
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    try:
        log.info('{"event": "x"}')
    finally:
        log.removeHandler(handler)
        handler.close()
    payload = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert "ts" in payload
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_json_lines_handler.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the handler**

```python
# packages/foreman/src/foreman/v4/json_lines_handler.py
"""JsonLinesHandler — file sink that emits one JSON object per record.

Pair-with-RichHandler design: RichHandler renders human-readable colored
output to stdout; this handler persists structured records to disk for
grep, replay, and ad-hoc analysis. The transitions log written by
StructuredLogObserver (Phase 2) is the primary consumer.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any


class JsonLinesHandler(logging.Handler):
    def __init__(self, *, filename: str, encoding: str = "utf-8") -> None:
        super().__init__()
        self._stream = open(filename, "a", encoding=encoding)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload: dict[str, Any] = {
                "ts": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
            }
            # If the log message is itself JSON (StructuredLogObserver), merge
            # its keys at top level so consumers don't dig into a nested object.
            message = record.getMessage()
            try:
                parsed = json.loads(message)
                if isinstance(parsed, dict):
                    payload.update(parsed)
                else:
                    payload["message"] = message
            except (json.JSONDecodeError, TypeError):
                payload["message"] = message
            self._stream.write(json.dumps(payload, default=str) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            super().close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_json_lines_handler.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/json_lines_handler.py packages/foreman/tests/v4/test_json_lines_handler.py
git commit -m "feat(v4): JsonLinesHandler — file sink emitting one JSON per record"
```

### Task 7.2: `configure_logging()` — RichHandler + JsonLinesHandler wiring

**Files:**
- Create: `packages/foreman/src/foreman/v4/logging_config.py`
- Test: `packages/foreman/tests/v4/test_logging_config.py`

One function called at daemon startup. Installs:
- `RichHandler` on stderr (colored, level-highlighted) for human-readable output
- `JsonLinesHandler` on `<log_dir>/transitions.jsonl` for machine-readable persistence
- Routes everything from `foreman.v4.*` loggers through both
- Idempotent — calling twice doesn't add duplicate handlers (defensive)

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_logging_config.py
"""configure_logging — installs RichHandler + JsonLinesHandler."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from foreman.v4.json_lines_handler import JsonLinesHandler
from foreman.v4.logging_config import configure_logging, reset_logging


@pytest.fixture(autouse=True)
def _reset():
    yield
    reset_logging()


def test_installs_both_handlers_on_foreman_v4_logger(tmp_path: Path):
    configure_logging(log_dir=tmp_path, level="INFO")
    log = logging.getLogger("foreman.v4.transitions")
    handler_types = {type(h).__name__ for h in log.handlers}
    assert "RichHandler" in handler_types
    assert "JsonLinesHandler" in handler_types


def test_jsonl_file_receives_writes(tmp_path: Path):
    configure_logging(log_dir=tmp_path, level="INFO")
    log = logging.getLogger("foreman.v4.transitions")
    log.info(json.dumps({"event": "test", "ticket_id": 42}))
    for handler in log.handlers:
        if isinstance(handler, JsonLinesHandler):
            handler.flush()
    line = (tmp_path / "transitions.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "test"
    assert payload["ticket_id"] == 42


def test_idempotent_does_not_add_duplicate_handlers(tmp_path: Path):
    configure_logging(log_dir=tmp_path, level="INFO")
    configure_logging(log_dir=tmp_path, level="INFO")
    log = logging.getLogger("foreman.v4.transitions")
    handler_types = [type(h).__name__ for h in log.handlers]
    # Each handler type appears exactly once
    assert handler_types.count("RichHandler") == 1
    assert handler_types.count("JsonLinesHandler") == 1


def test_level_is_honored(tmp_path: Path):
    configure_logging(log_dir=tmp_path, level="WARNING")
    log = logging.getLogger("foreman.v4.transitions")
    log.info("info-level should be filtered")
    log.warning("warning-level should pass")
    for handler in log.handlers:
        if isinstance(handler, JsonLinesHandler):
            handler.flush()
    content = (tmp_path / "transitions.jsonl").read_text(encoding="utf-8")
    assert "info-level" not in content
    assert "warning-level" in content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_logging_config.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the configurator**

```python
# packages/foreman/src/foreman/v4/logging_config.py
"""configure_logging — one call at daemon startup wires both handlers.

RichHandler renders human-readable colored output to stderr; JsonLinesHandler
appends machine-readable records to <log_dir>/transitions.jsonl. The transitions
logger is the v4 default sink; StructuredLogObserver (Phase 2) writes to it.

Idempotent so callers can re-invoke after a daemon reload without doubling
up handlers.
"""

from __future__ import annotations

import logging
from pathlib import Path

from rich.logging import RichHandler

from foreman.v4.json_lines_handler import JsonLinesHandler


_V4_LOGGER_NAMES = (
    "foreman.v4",
    "foreman.v4.transitions",
    "foreman.v4.event_bus",
)


def configure_logging(*, log_dir: Path, level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = log_dir / "transitions.jsonl"

    for name in _V4_LOGGER_NAMES:
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = False

        existing_types = {type(h).__name__ for h in logger.handlers}
        if "RichHandler" not in existing_types:
            rich = RichHandler(
                show_time=True, show_level=True, show_path=False,
                rich_tracebacks=True,
            )
            rich.setLevel(level)
            logger.addHandler(rich)
        if "JsonLinesHandler" not in existing_types:
            jsonl = JsonLinesHandler(filename=str(jsonl_path))
            jsonl.setLevel(level)
            logger.addHandler(jsonl)


def reset_logging() -> None:
    """Tear down handlers — test helper, not for production use."""
    for name in _V4_LOGGER_NAMES:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_logging_config.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/logging_config.py packages/foreman/tests/v4/test_logging_config.py
git commit -m "feat(v4): configure_logging — RichHandler stderr + JsonLinesHandler file"
```

### Task 7.3: `V4Config` — TOML-loaded settings with MergeQueue default

**Files:**
- Create: `packages/foreman/src/foreman/v4/config.py`
- Test: `packages/foreman/tests/v4/test_config.py`

Pydantic `BaseSettings`-style config for the v4 daemon:

- `db_path`, `log_dir`, `log_level`
- `tick_seconds`, `max_in_flight`
- `merge_mechanism: Literal["queue", "merge", "squash", "rebase"] = "queue"` — MergeQueue is the v4 default for impl PRs
- `projects: list[ProjectConfig]` — one entry per `[projects.X]` block in TOML
- `ProjectConfig`: `name`, `repo` (owner/name), `local_clone_path`, `trigger_label="foreman:plan"`

Loaded from `~/.foreman/v4/config.toml` (or `FOREMAN_V4_CONFIG` env var override). Validates at construction — invalid TOML aborts startup loudly.

This IS a pydantic model — config is a boundary (TOML file on disk = untrusted input from filesystem). `CliContext` stays dataclass; `V4Config` is pydantic.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_config.py
"""V4Config — TOML-loaded settings with MergeQueue default."""
from __future__ import annotations

from pathlib import Path

import pytest

from foreman.v4.config import ProjectConfig, V4Config, load_config


def test_defaults_set_merge_mechanism_to_queue(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.merge_mechanism == "queue"
    assert config.tick_seconds > 0
    assert config.max_in_flight > 0


def test_explicit_merge_mechanism_override(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        'merge_mechanism = "merge"\n'
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.merge_mechanism == "merge"


def test_invalid_merge_mechanism_raises(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        'merge_mechanism = "not-a-thing"\n'
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    with pytest.raises(Exception):
        load_config(config_path)


def test_projects_round_trip(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        '[[projects]]\n'
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
        '[[projects]]\n'
        'name = "foreman"\n'
        'repo = "jeffrichley/foreman"\n'
        'local_clone_path = "/tmp/foreman"\n'
    )
    config = load_config(config_path)
    assert len(config.projects) == 2
    assert config.projects[0].name == "voice"
    assert config.projects[1].name == "foreman"


def test_missing_project_name_raises(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[daemon]\n'
        'db_path = "/tmp/foreman.db"\n'
        'log_dir = "/tmp/foreman-logs"\n'
        '[[projects]]\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    with pytest.raises(Exception):
        load_config(config_path)


def test_project_config_default_trigger_label():
    p = ProjectConfig(
        name="voice", repo="jeffrichley/voice",
        local_clone_path="/tmp/voice",
    )
    assert p.trigger_label == "foreman:plan"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the config module**

```python
# packages/foreman/src/foreman/v4/config.py
"""V4Config — TOML-loaded daemon configuration.

This IS a pydantic model: config is a boundary surface. TOML on disk
is untrusted input the daemon parses at startup; pydantic's validation
catches typos, wrong types, invalid enum values before the daemon
half-starts and confuses everything downstream.

Schema:
  [daemon]
    db_path        - SQLite DB path
    log_dir        - directory for rich-stdout + jsonl
    log_level      - default INFO
    tick_seconds   - cadence between Poller ticks (default 30)
    max_in_flight  - Single concurrency knob: sizes BOTH the QM in-flight
                     cap AND the WorkerPool ThreadPoolExecutor. One number
                     so a stuck-ticket scenario can't strand pool threads
                     while the QM still has slots (or vice versa). Default
                     1 (serial); opt in to higher after dogfood stability.
    merge_mechanism - "queue" (default) | "merge" | "squash" | "rebase"

  [[projects]]
    name              - project slug
    repo              - "owner/name"
    local_clone_path  - filesystem path
    trigger_label     - GH label (default "foreman:plan")
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    repo: str
    local_clone_path: str
    trigger_label: str = "foreman:plan"


class V4Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    db_path: str
    log_dir: str
    log_level: str = "INFO"
    tick_seconds: float = 30.0
    max_in_flight: int = 1
    merge_mechanism: Literal["queue", "merge", "squash", "rebase"] = "queue"
    projects: list[ProjectConfig] = Field(default_factory=list)


def load_config(path: Path) -> V4Config:
    """Parse the TOML at ``path`` and validate as V4Config.

    Invalid TOML / missing required fields / wrong types raise pydantic
    ValidationError. The daemon's startup catches these and exits with
    a useful message; this function deliberately does not swallow.
    """
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    daemon = raw.get("daemon", {})
    projects = raw.get("projects", [])
    return V4Config(**daemon, projects=projects)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_config.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/config.py packages/foreman/tests/v4/test_config.py
git commit -m "feat(v4): V4Config — TOML loader with MergeQueue default"
```

### Task 7.4: Multi-project Daemon — one Poller per project

**Files:**
- Modify: `packages/foreman/src/foreman/v4/daemon.py` (extend Daemon to hold a list of Pollers)
- Test: `packages/foreman/tests/v4/test_daemon_multi_project.py`

The Phase 4 Daemon was single-project. Extending: Daemon takes a `list[Poller]` instead of a single Poller; `tick_once()` calls `tick()` on each in turn, then drains the WorkerPool. Same QueueManager + WorkerPool shared across projects — multi-project concurrency lives in the QM, not in multiple WorkerPools.

This is a small but real change. Backward compat: the Phase 4 single-project tests in `test_daemon.py` need a one-line update (`pollers=[poller]` instead of `poller=poller`).

- [ ] **Step 1: Write the multi-project test**

```python
# packages/foreman/tests/v4/test_daemon_multi_project.py
"""Daemon with multiple Pollers — one per project, shared QM + WorkerPool."""
from __future__ import annotations

import datetime as dt

from foreman.v4.daemon import Daemon, DaemonConfig
from foreman.v4.git_provider import FakeGitProvider
from foreman.v4.poller import Poller
from foreman.v4.role_dispatcher import FakeRoleDispatcher
from foreman.v4.sqlite_repository import SqliteTicketRepository


def _canned(kind: str) -> str:
    return f'FOREMAN_OUTCOME:{{"kind":"{kind}","confidence":"high","summary":"x"}}'


def test_tick_polls_every_project_and_advances_each():
    repo = SqliteTicketRepository.in_memory()
    git = FakeGitProvider()
    # Two projects, each with a new ticket
    git.set_open_issues_with_label(project="voice", label="foreman:plan", issue_numbers={1})
    git.set_open_issues_with_label(project="foreman", label="foreman:plan", issue_numbers={2})
    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "voice", 1): _canned("clean"),
        ("planner", "foreman", 2): _canned("clean"),
    })
    clock = lambda: dt.datetime(2026, 6, 13, 12, 0, 0)
    daemon = Daemon(
        repo=repo, dispatcher=dispatcher, git=git,
        config=DaemonConfig(tick_seconds=0, max_in_flight=4),
        clock=clock,
        pollers=[
            Poller(repo=repo, qm=None, git=git, project="voice",
                   trigger_label="foreman:plan", clock=clock),
            Poller(repo=repo, qm=None, git=git, project="foreman",
                   trigger_label="foreman:plan", clock=clock),
        ],
    )
    # Pollers built without QM get the QM wired by the Daemon constructor.
    daemon.tick_once()
    daemon.tick_once()
    voice = repo.get_ticket_by_issue(project="voice", issue_number=1)
    foreman_t = repo.get_ticket_by_issue(project="foreman", issue_number=2)
    assert voice.current_state != "Queued"  # advanced
    assert foreman_t.current_state != "Queued"  # advanced
```

- [ ] **Step 2: Refactor `Daemon` to accept multiple Pollers**

Replace `Daemon.__init__` and `tick_once`:

```python
# packages/foreman/src/foreman/v4/daemon.py
from foreman.v4.queue_manager import QueueManager
from foreman.v4.worker_pool import WorkerPool


@dataclass
class DaemonConfig:
    tick_seconds: float = 30.0
    max_in_flight: int = 4


class Daemon:
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
        # Wire the shared QM into every Poller that was built without one.
        self._pollers = [self._with_qm(p) for p in pollers]
        self._pool = WorkerPool(
            repo=repo, qm=self._qm, dispatcher=dispatcher,
            git=git, bus=bus, clock=clock,
        )
        self._stop = threading.Event()

    def _with_qm(self, poller: Poller) -> Poller:
        # Pollers can be constructed without a QM at config time; we
        # inject the daemon's shared QM here. If the caller passed a QM
        # already, leave it alone.
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

(The `_with_qm` helper reaches into Poller's private attr — acceptable for v4 since both classes live in the same package. If it grows uncomfortable, promote `Poller.set_queue_manager(qm)` to public API.)

- [ ] **Step 3: Update Phase 4 single-project Daemon tests**

In `packages/foreman/tests/v4/test_daemon.py`, change every `Daemon(...)` construction to pass `pollers=[Poller(...)]` instead of `poller=Poller(...)`. The Phase 4 test that built the Daemon with `project="p", trigger_label="..."` directly on `DaemonConfig` needs to move those fields into a Poller construction step (the new `DaemonConfig` doesn't carry per-project info).

- [ ] **Step 4: Run all daemon tests**

Run: `uv run pytest packages/foreman/tests/v4/test_daemon.py packages/foreman/tests/v4/test_daemon_multi_project.py -v`
Expected: all green; multi-project test passes.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/daemon.py packages/foreman/tests/v4/test_daemon.py packages/foreman/tests/v4/test_daemon_multi_project.py
git commit -m "feat(v4): Daemon holds list of Pollers (multi-project support)"
```

### Task 7.5: `bootstrap_cli_context()` — production wiring

**Files:**
- Create: `packages/foreman/src/foreman/v4/bootstrap.py`
- Modify: `packages/foreman/src/foreman/cli.py` (main() reads config, bootstraps, runs app)
- Test: `packages/foreman/tests/v4/test_bootstrap.py`

Single function called at production startup. Takes a config path; assembles `Repository`, `GitProvider`, `Dispatcher`, `Daemon`, `QueueManager` from `V4Config` + `foreman.identity`; configures logging; returns a `CliContext` for the typer app.

Same pattern as Phase 6's `build_cli_context` — but at the layer that owns config + identity. Tests fake identity + GitProvider construction via dependency injection; production wires the real PyGithub + creds providers.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_bootstrap.py
"""bootstrap_cli_context — turns V4Config into a CliContext."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from foreman.v4.bootstrap import bootstrap_cli_context
from foreman.v4.config import ProjectConfig, V4Config


def _stub_identity():
    mod = MagicMock()
    mod.get_role_token.return_value = "ghp_TOKEN"
    return mod


def _stub_git_factory():
    return MagicMock()


def test_bootstrap_returns_clicontext_with_all_fields(tmp_path: Path):
    config = V4Config(
        db_path=str(tmp_path / "foreman.db"),
        log_dir=str(tmp_path / "logs"),
        projects=[
            ProjectConfig(
                name="voice", repo="owner/voice",
                local_clone_path=str(tmp_path / "voice"),
            ),
        ],
    )
    ctx = bootstrap_cli_context(
        config=config,
        identity=_stub_identity(),
        git_provider_factory=lambda repo: _stub_git_factory(),
    )
    assert ctx.repo is not None
    assert ctx.qm is not None
    assert ctx.daemon is not None
    assert ctx.dispatcher is not None


def test_db_file_created_at_configured_path(tmp_path: Path):
    db_path = tmp_path / "v4.db"
    config = V4Config(
        db_path=str(db_path),
        log_dir=str(tmp_path / "logs"),
        projects=[
            ProjectConfig(
                name="voice", repo="owner/voice",
                local_clone_path=str(tmp_path / "voice"),
            ),
        ],
    )
    bootstrap_cli_context(
        config=config,
        identity=_stub_identity(),
        git_provider_factory=lambda repo: _stub_git_factory(),
    )
    # SQLite creates the file lazily on first write; the bootstrap
    # should have applied the schema, which IS a write.
    assert db_path.exists()


def test_bootstrap_builds_one_poller_per_project(tmp_path: Path):
    config = V4Config(
        db_path=str(tmp_path / "v4.db"),
        log_dir=str(tmp_path / "logs"),
        projects=[
            ProjectConfig(name="a", repo="o/a", local_clone_path=str(tmp_path / "a")),
            ProjectConfig(name="b", repo="o/b", local_clone_path=str(tmp_path / "b")),
            ProjectConfig(name="c", repo="o/c", local_clone_path=str(tmp_path / "c")),
        ],
    )
    ctx = bootstrap_cli_context(
        config=config,
        identity=_stub_identity(),
        git_provider_factory=lambda repo: _stub_git_factory(),
    )
    assert len(ctx.daemon._pollers) == 3  # type: ignore[attr-defined]
```

- [ ] **Step 2: Write the bootstrap module**

```python
# packages/foreman/src/foreman/v4/bootstrap.py
"""bootstrap_cli_context — production startup wiring.

This is the single place where V4Config gets turned into the live
object graph. Tests use the factories to inject fakes; production
calls with the real PyGithub-backed factories.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Callable, Protocol

from foreman.v4.cli.context import CliContext, build_cli_context
from foreman.v4.config import V4Config
from foreman.v4.daemon import Daemon, DaemonConfig
from foreman.v4.git_provider import GitProvider
from foreman.v4.logging_config import configure_logging
from foreman.v4.poller import Poller
from foreman.v4.sqlite_repository import SqliteTicketRepository
from foreman.v4.subprocess_dispatcher import SubprocessRoleDispatcher


class IdentityProvider(Protocol):
    def get_role_token(self, role: str) -> str: ...


def bootstrap_cli_context(
    *,
    config: V4Config,
    identity: IdentityProvider,
    git_provider_factory: Callable[[str], GitProvider],
    foreman_cli: list[str] | None = None,
) -> CliContext:
    """Build the full v4 object graph from config.

    ``git_provider_factory`` takes a repo full name (``owner/name``) and
    returns a GitProvider for it. Production passes a function that
    constructs PyGithubGitProvider; tests pass a function that returns
    a FakeGitProvider.
    """
    configure_logging(log_dir=Path(config.log_dir), level=config.log_level)
    repo = SqliteTicketRepository.at_path(Path(config.db_path))

    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=foreman_cli or ["foreman"],
        identity=identity,
    )

    pollers: list[Poller] = []
    git_for_pollers: GitProvider | None = None
    for project_config in config.projects:
        git_for_project = git_provider_factory(project_config.repo)
        if git_for_pollers is None:
            git_for_pollers = git_for_project
        pollers.append(Poller(
            repo=repo, qm=None, git=git_for_project,
            project=project_config.name,
            trigger_label=project_config.trigger_label,
            clock=dt.datetime.now,
        ))

    daemon = Daemon(
        repo=repo,
        git=git_for_pollers,  # type: ignore[arg-type]
        dispatcher=dispatcher,
        pollers=pollers,
        config=DaemonConfig(
            tick_seconds=config.tick_seconds,
            max_in_flight=config.max_in_flight,
        ),
        clock=dt.datetime.now,
    )

    return build_cli_context(
        repo=repo,
        qm=daemon._qm,  # type: ignore[attr-defined]
        daemon=daemon,
        git=git_for_pollers,
        dispatcher=dispatcher,
    )
```

- [ ] **Step 3: Rewire `foreman/cli.py:main()` to use bootstrap**

```python
# packages/foreman/src/foreman/cli.py
"""Top-level CLI entry point — loads config, bootstraps, invokes typer app."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from foreman.v4.bootstrap import bootstrap_cli_context
from foreman.v4.cli import app
from foreman.v4.config import load_config


_DEFAULT_CONFIG = Path.home() / ".foreman" / "v4" / "config.toml"


def main() -> None:
    config_path = Path(os.environ.get("FOREMAN_V4_CONFIG", _DEFAULT_CONFIG))
    # Identity + git_provider_factory wired here from foreman.identity +
    # PyGithubGitProvider. The narrow typing makes it easy to test
    # bootstrap in isolation; this layer just composes the concretes.
    from foreman import identity
    from foreman.v4.pygithub_git_provider import PyGithubGitProvider
    from github import Github  # type: ignore[import-not-found]

    config = load_config(config_path)

    def _git_factory(repo: str) -> PyGithubGitProvider:
        # Reuse the identity layer's "orchestrator" token for read paths;
        # role-specific tokens flow through SubprocessRoleDispatcher.
        token = identity.get_role_token("orchestrator")
        return PyGithubGitProvider(github=Github(token), repo_full_name=repo)

    ctx = bootstrap_cli_context(
        config=config, identity=identity,
        git_provider_factory=_git_factory,
    )
    app(obj=ctx)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run bootstrap tests**

Run: `uv run pytest packages/foreman/tests/v4/test_bootstrap.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/bootstrap.py packages/foreman/src/foreman/cli.py packages/foreman/tests/v4/test_bootstrap.py
git commit -m "feat(v4): bootstrap_cli_context — production wiring from config"
```

### Task 7.6: Phase 7 e2e smoke — config to running daemon

**Files:**
- Create: `packages/foreman/tests/v4/test_phase7_e2e.py`

Boot a daemon end-to-end from a TOML config: write config → call bootstrap → run one tick → verify the journal logged + JSON-lines file populated + a ticket reached terminal.

- [ ] **Step 1: Write the test**

```python
# packages/foreman/tests/v4/test_phase7_e2e.py
"""Phase 7 e2e — TOML config in, live daemon out."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from foreman.v4.bootstrap import bootstrap_cli_context
from foreman.v4.config import load_config
from foreman.v4.event_bus import EventBus
from foreman.v4.git_provider import FakeGitProvider, MergeVerdict, PRState
from foreman.v4.logging_config import reset_logging
from foreman.v4.observers.event_archive import EventArchiveObserver
from foreman.v4.observers.structured_log import StructuredLogObserver
from foreman.v4.role_dispatcher import FakeRoleDispatcher


def _canned(kind: str, pr_number: int | None = None) -> str:
    art = f',"artifacts":{{"pr_number":{pr_number}}}' if pr_number else ""
    return f'FOREMAN_OUTCOME:{{"kind":"{kind}","confidence":"high","summary":"x"{art}}}'


def test_full_boot_from_toml_to_done(tmp_path: Path, monkeypatch):
    reset_logging()
    db_path = tmp_path / "v4.db"
    log_dir = tmp_path / "logs"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[daemon]\n'
        f'db_path = "{db_path.as_posix()}"\n'
        f'log_dir = "{log_dir.as_posix()}"\n'
        f'tick_seconds = 0\n'
        f'max_in_flight = 1\n'
        f'[[projects]]\n'
        f'name = "p"\n'
        f'repo = "owner/p"\n'
        f'local_clone_path = "{(tmp_path / "p").as_posix()}"\n'
    )
    config = load_config(config_path)

    git = FakeGitProvider()
    git.set_open_issues_with_label(project="p", label="foreman:plan", issue_numbers={1})
    git.set_pr_state(
        project="p", pr_number=42,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    git.enqueue_merge_queue(project="p", pr_number=42)
    git.set_merge_verdict(project="p", pr_number=42, verdict=MergeVerdict.MERGED)

    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 1):       _canned("clean", pr_number=42),
        ("reviewer-spec", "p", 1): _canned("clean", pr_number=42),
        ("worker", "p", 1):        _canned("clean", pr_number=42),
        ("reviewer-impl", "p", 1): _canned("clean", pr_number=42),
    })
    # Monkey-patch the production dispatcher path; bootstrap builds a
    # SubprocessRoleDispatcher otherwise.
    monkeypatch.setattr(
        "foreman.v4.bootstrap.SubprocessRoleDispatcher",
        lambda **_kwargs: dispatcher,
    )

    identity = MagicMock()
    identity.get_role_token.return_value = "ghp_TEST"
    ctx = bootstrap_cli_context(
        config=config, identity=identity,
        git_provider_factory=lambda repo: git,
    )

    # Wire observers onto the bus the daemon uses
    bus = EventBus()
    bus.subscribe(StructuredLogObserver())
    bus.subscribe(EventArchiveObserver(conn=ctx.repo._conn))  # type: ignore[attr-defined]
    # The Phase-7 Daemon doesn't auto-subscribe these yet; for the smoke
    # we manually wire to prove the chain works end-to-end.
    ctx.daemon._bus = bus  # type: ignore[attr-defined]
    ctx.daemon._pool._bus = bus  # type: ignore[attr-defined]

    for _ in range(30):
        ctx.daemon.tick_once()
        ticket = ctx.repo.get_ticket_by_issue(project="p", issue_number=1)
        if ticket.current_state in ("Done", "Failed", "NeedsHelp"):
            break
    assert ticket.current_state == "Done"

    # JSON-lines log file populated
    jsonl = (log_dir / "transitions.jsonl")
    assert jsonl.exists()
    content = jsonl.read_text(encoding="utf-8")
    assert any(json.loads(line).get("event") == "state_entered"
               for line in content.splitlines() if line.strip())
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest packages/foreman/tests/v4/test_phase7_e2e.py -v`
Expected: PASS

If the observer-wiring monkeypatch feels hacky, the real fix is making `bootstrap_cli_context` subscribe the four standard observers to a bus it creates and threads through `Daemon` + `WorkerPool`. Do that inline if time allows — bootstrap should produce a complete object graph, not one missing observers.

- [ ] **Step 3: Commit**

```bash
git add packages/foreman/tests/v4/test_phase7_e2e.py
git commit -m "test(v4): phase 7 e2e — TOML config to running daemon to Done"
```

### Phase 7 — `just check` gate

- [ ] **Run:** `just check`
- [ ] **Expected:** all green; the isolation guard from Task 1.10 still passes (Phase 7 only adds modules under `foreman/v4/` and a thin import in `foreman/cli.py` from the survival set).

Phase 7 completion criterion (from the outline): **colored stdout + JSON file + queue is the merge default**. Achieved:
- `RichHandler` + `JsonLinesHandler` wired at daemon boot (Tasks 7.1, 7.2)
- `V4Config.merge_mechanism` defaults to `"queue"` (Task 7.3)
- Production startup composes the full graph from one TOML file (Task 7.5)
- The Phase 7 e2e proves config-in → terminal-state-out (Task 7.6)

Phase 8 is the v3 deletion sweep + per-repo MergeQueue setup docs.

---
