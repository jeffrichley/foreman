# foreman#317 — Granular Merge-State Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route an open impl/spec PR's merge attempt on the ground-truth check-run state so a CI-*failed* PR goes to ImplFix instead of looping `BLOCKED` forever (review finding C1).

**Architecture:** Add a provider signal that classifies a PR's check-runs (`RequiredCheckState`). Turn `attempt_merge`'s "no healer applied → BLOCKED" fall-through into a classifier that maps `(mergeable_state × RequiredCheckState)` to an Outcome per the spec's routing table, reusing the existing `OutcomeKind.NEEDS_FIX` for the ImplFix routes. Add the missing `MergingState.next_state` edge `NEEDS_FIX → ImplFixState`.

**Tech Stack:** Python 3.12, PyGithub (check-runs API), pydantic, pytest.

**Spec:** `docs/superpowers/specs/foreman-issue-317-spec.md`. **Reference:** `docs/reference/github-merge-ci-state-semantics.md`.

## Global Constraints

- ruff (google docstrings, D-rules), mypy `--strict`, the `just check` gate (pytest, 80% floor, diff-cover 80). All must pass before every push.
- **No `Co-Authored-By` trailer** on foreman commits.
- Conventional-commit titles, subject lowercase-initial (the pr-title-lint gate).
- The worktree needs its own `uv sync` before `just check` runs (nested-worktree gotcha — the pre-push hook uses `--no-sync`).
- The FAKE provider must mirror the real one strictly — refuse shapes the real lib refuses (test-fakes rule).
- C-CI and C-STRICT are already configured on the managed repos; no repo-config work.
- `OutcomeKind.NEEDS_FIX` already exists and routes to ImplFix from Implementing/ImplReview — REUSE it, do not add a new kind.

## Precedence decision (resolved here, per spec §"ambiguities")

When a PR's required check-runs are in mixed states, `required_check_state` returns the **first** match in this order — **FAILED wins over PENDING (fail-fast)**: a concluded real failure will not un-fail, so routing to ImplFix immediately beats waiting on other checks.

`FAILED → ACTION_REQUIRED → TIMED_OUT_OR_CANCELLED → PENDING → PASSED`

"Required" checks: inspect ALL check-runs on the head SHA. Rationale (spec §9): a *non-required* failing/pending check surfaces the PR as `mergeable_state == "unstable"` (handled = merge), so under `blocked` any failing/pending check is effectively gating. This avoids a branch-protection required-contexts query. (Alternative, if this proves wrong: query the ruleset `required_status_checks` contexts and filter — noted, not implemented.)

## File Structure

- `packages/foreman/src/foreman/v4/git_provider.py` — add `RequiredCheckState` enum; add `required_check_state(...)` to the `GitProvider` Protocol and to `FakeGitProvider` (seedable).
- `packages/foreman/src/foreman/v4/pygithub_git_provider.py` — real `required_check_state` via `commit.get_check_runs()`.
- `packages/foreman/src/foreman/v4/states/merge_helper.py` — the classifier: replace the "no healer → BLOCKED" fall-through.
- `packages/foreman/src/foreman/v4/states/merging.py` — `next_state`: add `NEEDS_FIX → ImplFixState()`.
- Tests: `tests/v4/test_git_provider.py`, `tests/v4/test_pygithub_git_provider.py`, `tests/v4/states/test_merge_helper.py`, `tests/v4/states/test_merging.py`.

---

### Task 1: `RequiredCheckState` signal (enum + provider method + fake)

**Files:**
- Modify: `packages/foreman/src/foreman/v4/git_provider.py`
- Modify: `packages/foreman/src/foreman/v4/pygithub_git_provider.py`
- Test: `packages/foreman/tests/v4/test_git_provider.py`, `packages/foreman/tests/v4/test_pygithub_git_provider.py`

**Interfaces:**
- Produces: `RequiredCheckState` (StrEnum: `PENDING`, `FAILED`, `TIMED_OUT_OR_CANCELLED`, `ACTION_REQUIRED`, `PASSED`); `GitProvider.required_check_state(*, project: str, pr_number: int) -> RequiredCheckState`; `FakeGitProvider.seed_check_state(project, pr_number, RequiredCheckState)`.

- [ ] **Step 1: Write the failing fake test** in `test_git_provider.py`:
```python
from foreman.v4.git_provider import FakeGitProvider, RequiredCheckState

def test_fake_required_check_state_roundtrips():
    p = FakeGitProvider()
    p.seed_check_state("proj", 7, RequiredCheckState.FAILED)
    assert p.required_check_state(project="proj", pr_number=7) == RequiredCheckState.FAILED

def test_fake_required_check_state_defaults_pending_when_unseeded():
    # Mirror reality: a PR whose checks haven't registered yet reads PENDING
    # (C-CI guarantees CI exists), never a silent PASSED.
    p = FakeGitProvider()
    assert p.required_check_state(project="proj", pr_number=9) == RequiredCheckState.PENDING
```

- [ ] **Step 2: Run to verify failure** — `uv run --no-sync pytest tests/v4/test_git_provider.py -k required_check_state -v` → FAIL (no attribute).

- [ ] **Step 3: Implement enum + Protocol + fake** in `git_provider.py`:
```python
from enum import StrEnum

class RequiredCheckState(StrEnum):
    """Classification of a PR's check-runs on its head SHA (foreman#317).

    Precedence when mixed: FAILED > ACTION_REQUIRED > TIMED_OUT_OR_CANCELLED
    > PENDING > PASSED (fail-fast on a concluded failure).
    """
    PENDING = "pending"
    FAILED = "failed"
    TIMED_OUT_OR_CANCELLED = "timed_out_or_cancelled"
    ACTION_REQUIRED = "action_required"
    PASSED = "passed"
```
On the `GitProvider` Protocol add:
```python
    def required_check_state(self, *, project: str, pr_number: int) -> RequiredCheckState:
        """Classify the check-runs on the PR's head SHA. See RequiredCheckState."""
        ...
```
On `FakeGitProvider.__init__` add `self._check_states: dict[tuple[str, int], RequiredCheckState] = {}` and:
```python
    def seed_check_state(self, project: str, pr_number: int, state: RequiredCheckState) -> None:
        self._check_states[(project, pr_number)] = state

    def required_check_state(self, *, project: str, pr_number: int) -> RequiredCheckState:
        return self._check_states.get((project, pr_number), RequiredCheckState.PENDING)
```

- [ ] **Step 4: Run to verify pass** — same command → PASS.

- [ ] **Step 5: Write the failing real-provider test** in `test_pygithub_git_provider.py`. Build a fake PyGithub `commit.get_check_runs()` returning MagicMock check-runs with `.status` / `.conclusion`, and assert classification + precedence:
```python
import pytest
from unittest.mock import MagicMock
from foreman.v4.git_provider import RequiredCheckState

def _run(status, conclusion=None):
    r = MagicMock(); r.status = status; r.conclusion = conclusion; return r

@pytest.mark.parametrize("runs, expected", [
    ([_run("completed", "success")], RequiredCheckState.PASSED),
    ([_run("completed", "success"), _run("completed", "neutral")], RequiredCheckState.PASSED),
    ([_run("in_progress")], RequiredCheckState.PENDING),
    ([_run("completed", "failure")], RequiredCheckState.FAILED),
    # fail-fast: failure wins over a still-pending sibling
    ([_run("completed", "failure"), _run("queued")], RequiredCheckState.FAILED),
    ([_run("completed", "timed_out")], RequiredCheckState.TIMED_OUT_OR_CANCELLED),
    ([_run("completed", "action_required"), _run("queued")], RequiredCheckState.ACTION_REQUIRED),
    ([], RequiredCheckState.PENDING),  # no checks registered yet (C-CI)
])
def test_pygithub_required_check_state_classifies(runs, expected, monkeypatch):
    provider = _make_provider_with_check_runs(runs)  # helper: stub client → repo → pull → head sha → commit.get_check_runs()
    assert provider.required_check_state(project="o/r", pr_number=7) == expected
```
(The `_make_provider_with_check_runs` helper follows the existing PyGithub-stub pattern in this test file — a MagicMock client whose `get_repo().get_pull().head.sha` and `get_commit().get_check_runs()` return the given runs.)

- [ ] **Step 6: Run to verify failure** → FAIL.

- [ ] **Step 7: Implement `required_check_state`** in `pygithub_git_provider.py`:
```python
def required_check_state(self, *, project: str, pr_number: int) -> RequiredCheckState:
    self._gh  # token-equality check (mirror get_pr_state's first line)
    repo = self._client.get_repo(self._repo_slug(project))
    pr = repo.get_pull(pr_number)
    runs = list(repo.get_commit(pr.head.sha).get_check_runs())
    pending, failed, timed, action = False, False, False, False
    for r in runs:
        if r.status != "completed":
            pending = True
        elif r.conclusion in ("failure", "startup_failure"):
            failed = True
        elif r.conclusion in ("timed_out", "cancelled"):
            timed = True
        elif r.conclusion == "action_required":
            action = True
        # success / neutral / skipped / stale → non-gating
    if failed:
        return RequiredCheckState.FAILED
    if action:
        return RequiredCheckState.ACTION_REQUIRED
    if timed:
        return RequiredCheckState.TIMED_OUT_OR_CANCELLED
    if pending or not runs:
        return RequiredCheckState.PENDING
    return RequiredCheckState.PASSED
```
(Match the file's existing `get_pr_state` for the repo-slug/head-sha access idioms.)

- [ ] **Step 8: Run to verify pass** → PASS. Then `uv run --no-sync mypy src/foreman/v4/git_provider.py src/foreman/v4/pygithub_git_provider.py` → clean.

- [ ] **Step 9: Commit** — `git add` the two src + two test files; `git commit -m "feat(v4): add RequiredCheckState check-run signal for merge routing (#317)"`.

---

### Task 2: The classifier in `attempt_merge`

**Files:**
- Modify: `packages/foreman/src/foreman/v4/states/merge_helper.py` (the "no healer applied → BLOCKED" fall-through, ~line 230)
- Test: `packages/foreman/tests/v4/states/test_merge_helper.py`

**Interfaces:**
- Consumes: `ctx.git.required_check_state(...)`, `state.mergeable_state`, `OutcomeKind.NEEDS_FIX/BLOCKED/NEEDS_HELP`.
- Produces: the routing per the spec table. `NEEDS_FIX` outcomes carry `details={"fix_reason": "ci_failed" | "merge_conflict"}` so ImplFix can tailor its directive.

- [ ] **Step 1: Write failing tests** — one per cell. Use `FakeGitProvider` seeding `set_pr_state` (mergeable_state) + `seed_check_state`. Example:
```python
def test_blocked_and_failed_routes_to_needs_fix(fake_ctx):
    ctx, git = fake_ctx
    git.set_pr_state("proj", 7, PRState(mergeable=False, ci_passing=False, mergeable_state="blocked", ...))
    git.seed_check_state("proj", 7, RequiredCheckState.FAILED)
    out = attempt_merge(ctx, pr_number=7, on_merge_success=lambda: None)
    assert out.kind == OutcomeKind.NEEDS_FIX
    assert out.details["fix_reason"] == "ci_failed"

def test_blocked_and_pending_stays_blocked(fake_ctx):
    ...  # seed_check_state(PENDING) → out.kind == BLOCKED

def test_dirty_routes_to_needs_fix_conflict(fake_ctx):
    ...  # mergeable_state="dirty" → NEEDS_FIX, details["fix_reason"]=="merge_conflict"

def test_action_required_routes_to_needs_help(fake_ctx):
    ...  # blocked + ACTION_REQUIRED → NEEDS_HELP

def test_timed_out_reruns_once_then_needs_help(fake_ctx):
    ...  # first call BLOCKED (re-run requested); after MAX_RERUN, NEEDS_HELP
```
(`fake_ctx` fixture: a `StateContext` with `ctx.git = FakeGitProvider()` and a ticket; follow existing `test_merge_helper.py` fixtures.)

- [ ] **Step 2: Run → FAIL** (`unstable`/`clean`/`behind` cells already pass via existing code + healers; the new cells fail).

- [ ] **Step 3: Implement the classifier.** Replace the final `# No healer applied … return Outcome(kind=BLOCKED, summary="PR not yet mergeable (CI pending or merge conflict)")` block with:
```python
    # foreman#317: no healer applied — classify on ground truth instead of a
    # blanket BLOCKED (which looped forever on CI-failed PRs, review C1).
    if state.mergeable_state == "dirty":
        return Outcome(
            kind=OutcomeKind.NEEDS_FIX, confidence=OutcomeConfidence.HIGH,
            summary="merge conflict with base — routing to ImplFix to resolve",
            artifacts=OutcomeArtifacts(pr_number=pr_number),
            details={"fix_reason": "merge_conflict"},
        )
    check = ctx.git.required_check_state(project=ctx.ticket.project, pr_number=pr_number)
    if check == RequiredCheckState.FAILED:
        return Outcome(
            kind=OutcomeKind.NEEDS_FIX, confidence=OutcomeConfidence.HIGH,
            summary="required CI check failed — routing to ImplFix",
            artifacts=OutcomeArtifacts(pr_number=pr_number),
            details={"fix_reason": "ci_failed"},
        )
    if check == RequiredCheckState.ACTION_REQUIRED:
        return Outcome(
            kind=OutcomeKind.NEEDS_HELP, confidence=OutcomeConfidence.HIGH,
            summary="a required check needs manual action — escalating",
            artifacts=OutcomeArtifacts(pr_number=pr_number),
        )
    if check == RequiredCheckState.TIMED_OUT_OR_CANCELLED:
        return _rerun_or_escalate(ctx, pr_number)   # see Step 4
    # PENDING (CI still running) or PASSED-but-blocked (transient) → wait.
    return Outcome(
        kind=OutcomeKind.BLOCKED, confidence=OutcomeConfidence.HIGH,
        summary="CI still in flight — re-polling", artifacts=OutcomeArtifacts(pr_number=pr_number),
    )
```

- [ ] **Step 4: Implement the bounded re-run** helper (module-level in `merge_helper.py`), mirroring `MAX_HEAL_ACTIONS`/`_prior_blocked_heal_count`:
```python
MAX_CHECK_RERUNS = 1
def _rerun_or_escalate(ctx: StateContext, pr_number: int) -> Outcome:
    """timed_out/cancelled: re-run the checks once (infra flake), else escalate."""
    prior = _prior_rerun_count(ctx)  # count details["reran_checks"]==True BLOCKED rows, like _prior_blocked_heal_count
    if prior >= MAX_CHECK_RERUNS:
        return Outcome(kind=OutcomeKind.NEEDS_HELP, confidence=OutcomeConfidence.HIGH,
            summary=f"checks timed out/cancelled after {MAX_CHECK_RERUNS} re-run — escalating",
            artifacts=OutcomeArtifacts(pr_number=pr_number))
    ctx.git.rerun_failed_checks(project=ctx.ticket.project, pr_number=pr_number)  # add to provider+fake, mirror Task 1
    return Outcome(kind=OutcomeKind.BLOCKED, confidence=OutcomeConfidence.HIGH,
        summary="checks timed out/cancelled — re-running once",
        artifacts=OutcomeArtifacts(pr_number=pr_number), details={"reran_checks": True})
```
(Add `rerun_failed_checks` to the provider Protocol + real impl `repo.get_workflow_run(...).rerun_failed_jobs()` or the check-run rerequest endpoint + a fake no-op that records the call. `_prior_rerun_count` copies `_prior_blocked_heal_count`, keying on `details.get("reran_checks")`.)

- [ ] **Step 5: Run → PASS** for all cells. `uv run --no-sync pytest tests/v4/states/test_merge_helper.py -v`.

- [ ] **Step 6: Commit** — `git commit -m "feat(v4): route merge attempts on check-run state — CI-failed→ImplFix, dirty→resolve (#317)"`.

---

### Task 3: The `Merging → ImplFix` edge

**Files:**
- Modify: `packages/foreman/src/foreman/v4/states/merging.py` (`next_state`, ~line 169)
- Test: `packages/foreman/tests/v4/states/test_merging.py`

**Interfaces:**
- Consumes: `OutcomeKind.NEEDS_FIX`, `ImplFixState`.

- [ ] **Step 1: Write the failing test**:
```python
def test_needs_fix_routes_merging_to_impl_fix():
    from foreman.v4.states.impl_fix import ImplFixState
    st = MergingState()
    out = Outcome(kind=OutcomeKind.NEEDS_FIX, confidence=OutcomeConfidence.HIGH, summary="x")
    nxt = st.next_state(_ctx(), out)
    assert isinstance(nxt, ImplFixState)
```

- [ ] **Step 2: Run → FAIL** (falls through to NeedsHelp today).

- [ ] **Step 3: Implement** — in `MergingState.next_state`, before the `NEEDS_HELP` branch, add:
```python
        if outcome.kind == OutcomeKind.NEEDS_FIX:
            from foreman.v4.states.impl_fix import ImplFixState
            return ImplFixState()
```

- [ ] **Step 4: Run → PASS.** Also run the whole `test_merging.py` to confirm the existing CLEAN/BLOCKED/NEEDS_HELP routes are unchanged.

- [ ] **Step 5: Commit** — `git commit -m "feat(v4): add Merging→ImplFix edge for NEEDS_FIX outcomes (#317)"`.

---

### Task 4: End-to-end regression — the #390 scenario

**Files:**
- Test: `packages/foreman/tests/v4/states/test_merge_helper.py` (or a new `test_merge_routing_e2e.py`)

**Interfaces:** Consumes everything above.

- [ ] **Step 1: Write the regression test** proving the exact incident closes — a blocked PR with a failed required check reaches ImplFix, not an infinite BLOCKED loop:
```python
def test_ci_failed_pr_reaches_impl_fix_not_infinite_blocked(fake_ctx):
    ctx, git = fake_ctx
    git.set_pr_state("proj", 430, PRState(mergeable=False, ci_passing=False, mergeable_state="blocked", ...))
    git.seed_check_state("proj", 430, RequiredCheckState.FAILED)
    out = attempt_merge(ctx, pr_number=430, on_merge_success=lambda: None)
    assert out.kind == OutcomeKind.NEEDS_FIX
    nxt = MergingState().next_state(ctx, out)
    from foreman.v4.states.impl_fix import ImplFixState
    assert isinstance(nxt, ImplFixState)  # #390 would have looped BLOCKED here
```

- [ ] **Step 2: Run → PASS** (all prior tasks make this green).

- [ ] **Step 3: Full gate** — from the worktree root: `uv sync` (once), then `just check`. Expected: all pass, coverage ≥ 80, diff-cover ≥ 80, ruff + mypy clean.

- [ ] **Step 4: Commit** — `git commit -m "test(v4): regression — CI-failed PR routes to ImplFix, not infinite BLOCKED (#390/#317)"`.

---

## Self-Review

**Spec coverage:** routing table cells — clean/unstable/behind (existing code+healers, unchanged) ✓; dirty→NEEDS_FIX (T2) ✓; blocked+pending→BLOCKED (T2) ✓; blocked+failed→NEEDS_FIX (T2) ✓; action_required→NEEDS_HELP (T2) ✓; timed_out/cancelled→re-run-once→NEEDS_HELP (T2) ✓; unknown→BLOCKED (existing, unchanged) ✓; Merging→ImplFix edge (T3) ✓; ground-truth signal + fake mirror (T1) ✓; convergence bound (T2 `MAX_CHECK_RERUNS`) ✓.

**Type consistency:** `RequiredCheckState` values and `required_check_state` signature identical across T1 (definition), T2 (use), fake + real. `NEEDS_FIX` reused (no new kind). `details["fix_reason"]` set in T2, available to ImplFix downstream.

**Open follow-up (not blocking #317):** ImplFix should read `details["fix_reason"] == "merge_conflict"` to give the Fixer a resolve-conflict directive (spec Decision D) — file as a small sibling task if ImplFix's prompt doesn't already generalize. The `rerun_failed_checks` provider method is new surface; keep its real impl minimal (rerequest the check suite).
