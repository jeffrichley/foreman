> **Parent plan:** [../2026-06-13-foreman-v4-substrate-redesign-implementation.md](../2026-06-13-foreman-v4-substrate-redesign-implementation.md) — read its v4 isolation principle first.
> **Spec:** [../../specs/2026-06-13-foreman-v4-substrate-redesign-design.md](../../specs/2026-06-13-foreman-v4-substrate-redesign-design.md).
> **Branch:** `feat/foreman-v4-substrate`.
> **Gate at end:** `just check` green; then stop for human review before next phase.

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
from enum import StrEnum
from typing import Protocol


class PRNotFoundError(LookupError):
    """No PR matching this (project, pr_number)."""


@dataclass(frozen=True, slots=True)
class PRState:
    merged: bool
    mergeable: bool
    ci_passing: bool


class MergeVerdict(StrEnum):
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
