# Ticket Dependency Graph — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Gate an execution ticket's dispatch on its GitHub-native `blocked_by` dependencies — read from GitHub each poll, converged by full-replace, resolved by issue closed-as-completed.

**Architecture:** A per-poll reconciler (in `Poller._enqueue_open_tickets`) reads each open ticket's `blocked_by` from GitHub, filters to the *currently-unmet* subset (dep issue not closed-as-completed), and full-replaces `depends_on`. The existing queue gate already skips tickets with unmet deps. Cycle among tracked tickets → `hold_ticket` with a reason. Display surfaces blocking deps.

**Tech stack:** Python 3.12, `foreman.v4`, Postgres (`depends_on` JSONB `list[int]`, unchanged), PyGithub + raw REST for the dependencies API.

## Global Constraints

- `just check` must stay green: ruff + ruff-format + mypy (strict) + lint-imports + pytest, **coverage floor `--cov-fail-under=80`**, `-n auto --dist=loadscope`.
- `depends_on` shape is **unchanged** `list[int]` of same-project issue numbers. No schema change.
- `depends_on` post-reconcile holds **only currently-unmet** dep issue numbers (met deps are filtered out by the reconciler). This is the definition the queue gate and display both rely on.
- No prose parsing. Dependencies come only from the native `dependencies/blocked_by` API.
- Never auto-apply `foreman:plan` to a dependency target.
- Foreman commits: **no** `Co-Authored-By` trailer.
- Design spec: `docs/superpowers/specs/2026-07-14-ticket-dependency-graph-design.md`.

---

### Task 1: `GitProvider.get_issue_state_reason`

**Files:**
- Modify: `packages/foreman/src/foreman/v4/git_provider.py` (Protocol + `FakeGitProvider`)
- Modify: `packages/foreman/src/foreman/v4/pygithub_git_provider.py`
- Modify: `packages/foreman/src/foreman/v4/routing_git_provider.py`
- Test: `packages/foreman/tests/v4/test_git_provider_fake.py`, `test_routing_git_provider.py`, `test_pygithub_git_provider.py`

**Interfaces:**
- Produces: `get_issue_state_reason(*, project: str, issue_number: int) -> str | None` — returns GitHub's `state_reason` (`"completed"`, `"not_planned"`, `"reopened"`, or `None`). A dep is "met" iff this equals `"completed"` (this uniquely identifies closed-as-completed; open issues return `None`).

- [ ] **Step 1: Failing test (Fake).** In `test_git_provider_fake.py`, seed a reason and assert readback:
```python
def test_get_issue_state_reason_defaults_none() -> None:
    p = FakeGitProvider()
    assert p.get_issue_state_reason(project="agent_core", issue_number=1) is None

def test_get_issue_state_reason_readback() -> None:
    p = FakeGitProvider()
    p.set_issue_state_reason(project="agent_core", issue_number=1, reason="completed")
    assert p.get_issue_state_reason(project="agent_core", issue_number=1) == "completed"
```
- [ ] **Step 2: Run — expect FAIL** (`AttributeError`). `uv run --no-sync pytest packages/foreman/tests/v4/test_git_provider_fake.py -q`
- [ ] **Step 3: Implement.** Add to the Protocol (with docstring), to `FakeGitProvider` a `_state_reasons: dict[tuple[str,int], str]`, a `set_issue_state_reason(...)` seeder, and `get_issue_state_reason(...)` returning `self._state_reasons.get((project, issue_number))`. In `pygithub_git_provider.py`, implement by reading `repo.get_issue(issue_number).state_reason`. In `routing_git_provider.py`, dispatch via `self._resolve(project).get_issue_state_reason(...)`.
- [ ] **Step 4: Run — expect PASS.** Also add a routing dispatch test mirroring the existing routing tests, and a PyGithub test using the existing PyGithub mock pattern.
- [ ] **Step 5: Commit** `feat(v4): add GitProvider.get_issue_state_reason`

---

### Task 2: `GitProvider.read_blocked_by`

**Files:** same provider files + tests as Task 1.

**Interfaces:**
- Produces: `read_blocked_by(*, project: str, issue_number: int) -> list[int]` — the issue numbers this issue is **blocked by**, from GitHub's native issue-dependencies. Empty list when none.

- [ ] **Step 1: Failing test (Fake).**
```python
def test_read_blocked_by_defaults_empty() -> None:
    p = FakeGitProvider()
    assert p.read_blocked_by(project="agent_core", issue_number=291) == []

def test_read_blocked_by_readback() -> None:
    p = FakeGitProvider()
    p.set_blocked_by(project="agent_core", issue_number=291, blocked_by=[290])
    assert p.read_blocked_by(project="agent_core", issue_number=291) == [290]
```
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement.** Protocol + Fake (`_blocked_by: dict[tuple[str,int], list[int]]` + `set_blocked_by` seeder). PyGithub: raw REST `GET /repos/{owner}/{repo}/issues/{n}/dependencies/blocked_by` via the underlying requester (the endpoint returns a list of issue objects; map to `[i["number"] for i in resp]`). Routing: dispatch.
- [ ] **Step 4: Run — expect PASS** (Fake + routing). For PyGithub, test the response-mapping with a stubbed requester returning a 2-element list.
- [ ] **Step 5: Commit** `feat(v4): add GitProvider.read_blocked_by (native issue-dependencies)`

---

### Task 3: dependency reconciler (pure logic)

**Files:**
- Create: `packages/foreman/src/foreman/v4/dependency_reconciler.py`
- Test: `packages/foreman/tests/v4/test_dependency_reconciler.py`

**Interfaces:**
- Consumes: `GitProvider.read_blocked_by`, `GitProvider.get_issue_state_reason` (Tasks 1-2).
- Produces: `compute_unmet_dependencies(*, project: str, issue_number: int, provider: GitProvider) -> list[int]` — the subset of `blocked_by` whose dep issue is **not** closed-as-completed (i.e. still blocking). Pure; no repo writes.

- [ ] **Step 1: Failing tests** (drive every rule):
```python
def test_no_blocked_by_returns_empty() -> None:
    p = FakeGitProvider()
    assert compute_unmet_dependencies(project="ac", issue_number=291, provider=p) == []

def test_open_dep_is_unmet() -> None:
    p = FakeGitProvider(); p.set_blocked_by(project="ac", issue_number=291, blocked_by=[290])
    # 290 open → state_reason None → unmet
    assert compute_unmet_dependencies(project="ac", issue_number=291, provider=p) == [290]

def test_completed_dep_is_filtered_out() -> None:
    p = FakeGitProvider(); p.set_blocked_by(project="ac", issue_number=291, blocked_by=[290])
    p.set_issue_state_reason(project="ac", issue_number=290, reason="completed")
    assert compute_unmet_dependencies(project="ac", issue_number=291, provider=p) == []

def test_not_planned_dep_stays_unmet() -> None:
    p = FakeGitProvider(); p.set_blocked_by(project="ac", issue_number=291, blocked_by=[290])
    p.set_issue_state_reason(project="ac", issue_number=290, reason="not_planned")
    assert compute_unmet_dependencies(project="ac", issue_number=291, provider=p) == [290]
```
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement.**
```python
def compute_unmet_dependencies(*, project: str, issue_number: int, provider: GitProvider) -> list[int]:
    blocked_by = provider.read_blocked_by(project=project, issue_number=issue_number)
    return [
        dep for dep in blocked_by
        if provider.get_issue_state_reason(project=project, issue_number=dep) != "completed"
    ]
```
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** `feat(v4): dependency reconciler (unmet = blocked_by minus closed-completed)`

---

### Task 4: wire reconciler into the Poller

> **Execution order:** run **Task 5 (gate alignment) before this task.** The dequeue gate
> calls `list_unmet_dependencies`, whose current impl calls `get_ticket(dep)` and would
> crash once the reconciler starts writing *untracked* issue numbers into `depends_on`.
> Task 5 makes it safe first. (Kept in this numeric slot for readability; the SDD controller
> executes 5→4.)

**Files:**
- Modify: `packages/foreman/src/foreman/v4/poller.py` (`_enqueue_open_tickets`, and the `Poller` ctor to accept the provider if not already available)
- Test: `packages/foreman/tests/v4/test_poller.py`

**Interfaces:**
- Consumes: `compute_unmet_dependencies` (Task 3), `repo.set_ticket_dependencies` (exists), plus the Task 5 gate.

- [ ] **Step 1: Failing integration test.** In `test_poller.py`, seed a ticket #291 whose GitHub `blocked_by=[290]`, 290 open. After a poll tick, assert `repo.get_ticket_dependencies(<291 id>) == [290]` and that #291 is NOT enqueued (unmet dep). Then mark 290 `completed`, tick again, assert `depends_on == []` and #291 IS enqueued.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement.** In `_enqueue_open_tickets`, for each non-terminal ticket, before enqueue:
```python
unmet = compute_unmet_dependencies(
    project=ticket.project, issue_number=ticket.issue_number, provider=self._provider
)
self._repo.set_ticket_dependencies(ticket.id, deps=unmet)
```
Ensure `Poller` holds a `GitProvider` (thread it through the ctor + `bootstrap` wiring; follow how the Poller already receives collaborators). With Task 5 already applied, the dequeue gate (`queue_manager.py:129`) correctly skips tickets whose reconciled `depends_on` is non-empty.
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** `feat(v4): reconcile blocked_by into depends_on each poll`

---

### Task 5: align the frontier gate to the unmet-only invariant

**Files:**
- Modify: `packages/foreman/src/foreman/v4/repository.py` (`list_unmet_dependencies`, InMemory) + `postgres_repository.py`
- Test: `packages/foreman/tests/v4/test_in_memory_repository.py` (+ shared contract suite), `test_postgres_repository.py`

**Interfaces:**
- Produces: `list_unmet_dependencies(ticket_id) -> list[int]` now returns the stored `depends_on` verbatim — because the reconciler (Task 4) guarantees `depends_on` holds only currently-unmet deps. Removes the old per-dep `get_ticket(dep).current_state != "Done"` logic (which broke on untracked issue numbers).

- [ ] **Step 1: Update failing tests.** Adjust the contract-suite test so `list_unmet_dependencies` reflects stored `depends_on` regardless of whether the dep is a tracked ticket (seed `set_ticket_dependencies(id, [999])` where 999 is not a ticket; assert it returns `[999]`, not a crash).
- [ ] **Step 2: Run — expect FAIL** (old impl calls `get_ticket(999)`).
- [ ] **Step 3: Implement.** Both repos: `return list(self.get_ticket(ticket_id).depends_on)`.
- [ ] **Step 4: Run — expect PASS.** Confirm `queue_manager.py:129` (`if self._repo.list_unmet_dependencies(...): continue`) now gates purely on stored unmet deps — no code change needed there.
- [ ] **Step 5: Commit** `refactor(v4): list_unmet_dependencies returns stored depends_on (unmet-only invariant)`

---

### Task 6: dependency-cycle detection → hold with reason

**Files:**
- Modify: `packages/foreman/src/foreman/v4/dependency_reconciler.py` (add `find_cycles`)
- Modify: `packages/foreman/src/foreman/v4/poller.py` (after reconciling all tickets, detect cycles among tracked tickets and `hold_ticket`)
- Test: `test_dependency_reconciler.py`, `test_poller.py`

**Interfaces:**
- Produces: `find_cycles(edges: dict[int, list[int]]) -> list[list[int]]` — given `{issue_number: [blocked_by...]}` over **tracked** tickets, return the cycles (each as a list of issue numbers). Pure, testable.

- [ ] **Step 1: Failing tests.** Pure: `find_cycles({1: [2], 2: [1]}) == [[1, 2]]` (order-normalized); `find_cycles({1: [2], 2: []}) == []`. Poller integration: two tracked tickets #1↔#2 both open+mutually-blocked → after tick, both are held with `held_reason` containing `dependency cycle`, and neither is enqueued.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement.** DFS/coloring cycle finder over the tracked-ticket edge map (build the map from each tracked ticket's reconciled `depends_on`, restricted to targets that are themselves tracked tickets). In the poller, after reconcile, for each ticket in a cycle call `self._repo.hold_ticket(id, held_by="orchestrator", reason=f"dependency cycle: {' ↔ '.join('#'+str(n) for n in cycle)}", now=self._clock())`. Guard idempotency: don't re-hold an already-held ticket with the same reason.
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** `feat(v4): detect dependency cycles and hold with a reason`

---

### Task 7: surface dependencies in `foreman show` + `foreman ps`

**Files:**
- Modify: `packages/foreman/src/foreman/v4/cli/show.py`, `packages/foreman/src/foreman/v4/cli/ps.py`
- Test: existing CLI test modules for show/ps (follow their pattern)

**Interfaces:**
- Consumes: `repo.list_unmet_dependencies`, `repo.get_ticket` (to test tracked-ness of each dep).

- [ ] **Step 1: Failing tests.** `show` of a ticket with `depends_on=[290]` where 290 is a tracked ticket renders `relies on #290`; where 290 is NOT a tracked ticket renders `relies on #290 (untracked)`. `ps` includes a `blocked_by` column listing the same, `(untracked)`-tagged.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement.** Helper: for each dep `n` in `list_unmet_dependencies(id)`, `"#{n}"` if a ticket exists for `(project, n)` else `"#{n} (untracked)"`. In `show.py` add a `tree.add("relies on " + ", ".join(...))` block after the next_action_at block (~line 51). In `ps.py` add a `"blocked_by"` key to the row dict (~line 49).
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** `feat(v4): show/ps surface unmet dependencies (with untracked tag)`

---

### Task 8: full-suite gate + branch finish

- [ ] **Step 1:** `just check` in `packages/foreman` — ruff, format, mypy, lint-imports, pytest ≥80% cov, all green.
- [ ] **Step 2:** Fix any coverage gaps in the new modules (aim the new code well above floor).
- [ ] **Step 3:** Final whole-branch adversarial review (dispatch code-reviewer on the full diff `git merge-base origin/main HEAD`..HEAD).
- [ ] **Step 4:** Address Critical/Important findings.
- [ ] **Step 5:** Push branch, open PR against `foreman` main, link foreman#524. **Human-gated merge (Jeff).**

## Deferred (follow-up tickets, not this plan)

- Native dependency **write** endpoint (`add_blocked_by`) for API-authoring deps when filing tickets.
- **Cross-project** deps (`(project, issue)` keying).
- Per-entry **`source` tag** / manual-dep protection.
- Sub-issue **hierarchy** ingestion + epic roll-up.

## Self-review notes

- Spec coverage: Tasks 1-2 = source (native APIs); 3-4 = reconcile/convergence; 5 = frontier met-semantics + untracked-safety; 6 = cycle; 7 = display. All acceptance bullets mapped.
- The unmet-only `depends_on` invariant is the load-bearing simplification: it makes convergence, gating, and the cheap hot-path all fall out of one full-replace per poll. Every task that touches `depends_on` must preserve it.
