> **Parent plan:** [../2026-06-13-foreman-v4-substrate-redesign-implementation.md](../2026-06-13-foreman-v4-substrate-redesign-implementation.md) — read its v4 isolation principle first.
> **Spec:** [../../specs/2026-06-13-foreman-v4-substrate-redesign-design.md](../../specs/2026-06-13-foreman-v4-substrate-redesign-design.md).
> **Branch:** `feat/foreman-v4-substrate`.
> **Gate at end:** `just check` green; then stop for human review before next phase.

## Phase 5 — Role-side Outcome reporting + real subprocess dispatch

### Carry-overs from Phase 4

- **`WorkerPool.in_flight_count()` convenience helper** — surfaced during Task 4.4 review. Today tests reach through `qm.in_flight_count()`. If Phase 5's `SubprocessRoleDispatcher` work or the Phase 7 daemon shell wants to inspect the worker pool's load directly, add a one-liner: `def in_flight_count(self) -> int: return self._qm.in_flight_count()`. Defer if no caller materializes.

### Goal

The substrate is correct; nothing real yet drives it. Phase 5 makes two changes:

1. **Each of the four role CLIs gains a NEW `--issue-number`-based variant that emits `FOREMAN_OUTCOME:` JSON on stdout.** The existing positional-`<issue_url>` commands + their label-writing tails stay intact and untouched — they're the v3 fallback path during Phases 5-7 and the existing 8,500+ lines of role tests keep covering them. Phase 8 deletes the old path atomically as part of the v3 kill-set cutover.
2. **`SubprocessRoleDispatcher`** — the production `RoleDispatcher` impl that shells out to `foreman <role>-v4` with the appropriate per-role identity (PAT / App token) and returns stdout.

Roles affected (all in the **survival set** — they pre-date v4 and the bodies stay through Phase 7):
- `foreman/roles/planner.py` — gains `run_planner_cli`; existing functions untouched
- `foreman/cli.py` — gains `plan-v4` command alongside existing `plan`
- Same shape for reviewer/fixer/worker

### Why additive, not replace

Naïve cutover deletes the label-writing tails in Phase 5 and breaks ~8,500 lines of `test_roles_*.py` mid-phase. Google-style "head stays green" says: keep the suite green at every commit. Two-phase migration pattern:

- **Phase 5 (additive):** add v4 emit-based commands alongside v3 label-writing commands. Tag the obsolete code with `# v4-PHASE-8-KILL: <reason>` breadcrumbs so Phase 8 can grep-and-delete.
- **Phase 8 (atomic deletion):** grep for `v4-PHASE-8-KILL`, delete every tagged block + the legacy CLI commands + the obsolete role test files in one shot. Phase 8's existing v3 kill set extends to cover these.

Cost: v3 code paths remain runnable through Phase 7. Cheap, and matches the spec's existing "Phase 8 = deletion" structure.

### Breadcrumb convention (READ BEFORE EDITING ANY SURVIVAL-SET FILE)

When adding the v4 emit path alongside existing v3 code:

1. **Tag every block of code that Phase 8 should delete** with a line-or-block comment containing the literal string `v4-PHASE-8-KILL`. Format:
   ```python
   # v4-PHASE-8-KILL: <short reason — what replaced it and where the replacement lives>
   def _legacy_helper(...): ...
   ```
   For multi-line blocks (a whole function or CLI command body):
   ```python
   # v4-PHASE-8-KILL-BEGIN: legacy positional-arg planner CLI; replaced by `plan-v4` command using run_planner_cli
   @cli.command()
   @click.argument("issue_url", type=str)
   def plan(issue_url, ...):
       ...
   # v4-PHASE-8-KILL-END
   ```
2. **Do NOT delete or modify the tagged code in Phase 5.** Just add the new code nearby.
3. **Each new v4-side function or test that REPLACES tagged code MUST cross-reference the breadcrumb.** Example: above `run_planner_cli`, write `"""...Replaces label-writing tail in roles/planner.py (tagged v4-PHASE-8-KILL)."""`.
4. **Phase 8 verifies via grep.** `git grep 'v4-PHASE-8-KILL' packages/foreman/` enumerates every site to delete. The Phase 8 cutover task includes "after `git rm` block, run the grep — any surviving matches are bugs in the cutover task."

If a v3 helper has no v4 replacement (e.g., it just goes away), still tag it — the breadcrumb is what makes Phase 8 mechanical.

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

### Task 5.2: Planner — additive v4 emit path

**Files:**
- Modify: `packages/foreman/src/foreman/roles/planner.py` — ADD `run_planner_cli` function; mark existing entry-point with `# v4-PHASE-8-KILL` breadcrumb; existing body untouched
- Modify: `packages/foreman/src/foreman/cli.py` — ADD `@cli.command("plan-v4")` decorated function; mark existing `plan` command with `# v4-PHASE-8-KILL-BEGIN`/`-END` breadcrumbs; existing body untouched
- Create: `packages/foreman/tests/v4/roles/test_planner_outcome.py` (purely new — tests v4 path only)
- DO NOT TOUCH: `packages/foreman/tests/test_roles_planner.py` (covers the v3 path, still running)

**Pre-flight survey (5 min):** Read `roles/planner.py` to identify the existing main entry-point function (NOT named `build_planner` — there's no such factory in the current code; the actual entry is whatever the `@cli.command()` decorator at `cli.py:90` wraps, which calls into planner internals). The `run_planner_cli` you're adding is a NEW thin wrapper that constructs the same internals and invokes them.

Mapping to Outcome kinds:
- Planner opened a spec PR successfully → `CLEAN` with `artifacts.pr_number` + `artifacts.pr_url`
- Planner ran but produced `confidence: low` → `NEEDS_HELP`
- Planner raised an exception → `ERROR`

The new `run_planner_cli` calls the same internal planner logic the existing `plan` command does — but instead of writing labels at exit, it emits `FOREMAN_OUTCOME:` and exits zero (or 1 on raise).

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

- [ ] **Step 3: Add `run_planner_cli` to `planner.py` (additive)**

Append to `packages/foreman/src/foreman/roles/planner.py` — do NOT delete or modify the existing entry-point function:

```python
# v4 emit path — additive alongside the legacy label-writing entry-point.
# Phase 8 removes this comment after the legacy entry-point is deleted.
from foreman.v4.emit import emit_outcome
from foreman.v4.outcome import (
    Outcome,
    OutcomeArtifacts,
    OutcomeConfidence,
    OutcomeKind,
)


def run_planner_cli(*, project: str, issue_number: int) -> int:
    """Run the planner; emit FOREMAN_OUTCOME JSON; return exit code.

    Entry point the SubprocessRoleDispatcher (Task 5.6) forks via the
    `plan-v4` CLI command. The legacy `plan` command + label-writing
    tail in this module are tagged ``v4-PHASE-8-KILL`` and removed
    together in Phase 8.
    """
    try:
        # Construct the same internals the legacy entry-point uses.
        # Implementer: identify the right factory shape — the existing
        # `plan` command (cli.py:~90) calls into planner internals
        # passing (issue_url, project). Synthesize an equivalent issue
        # URL or refactor the internal call into a shared helper. The
        # legacy code path stays running; this just adds a parallel one.
        result = _run_planner_internal(project=project, issue_number=issue_number)
    except Exception as exc:  # broad: role boundary
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

About `_run_planner_internal`: the implementer's call here. If the existing `plan` command body can be cleanly extracted into a helper that both the legacy command AND `run_planner_cli` call into, do that — extract once, two callers, both still passing tests. If extraction would touch too much (>50 lines of refactor), keep the legacy body untouched and duplicate the internal-call sequence inside `run_planner_cli`. Note: code duplication here is FINE; both copies go away in Phase 8.

Above the legacy entry-point function in `planner.py`, add the breadcrumb:

```python
# v4-PHASE-8-KILL: legacy planner entry-point with label-writing tail; replaced by run_planner_cli (this file) + plan-v4 CLI command. Remove this function and its imports from foreman.labels in Phase 8.
def <existing_function_name>(...):  # whatever it's actually called
    ...  # existing body untouched
```

If `foreman.labels` import lines are scoped only to the legacy function, you can leave them — Phase 8 deletes them alongside the function. If they're shared with `run_planner_cli` code (they shouldn't be), refactor.

- [ ] **Step 4: Add `plan-v4` CLI command in `cli.py` (additive)**

Append to `packages/foreman/src/foreman/cli.py` — do NOT modify the existing `plan` command:

```python
# v4: additive CLI command using --issue-number; SubprocessRoleDispatcher invokes this.
# Legacy `plan` command (above, tagged v4-PHASE-8-KILL) stays running through Phase 7.
@cli.command("plan-v4")
@click.option("--project", required=True)
@click.option("--issue-number", "issue_number", type=int, required=True)
def plan_v4(project: str, issue_number: int) -> None:
    from foreman.roles.planner import run_planner_cli
    sys.exit(run_planner_cli(project=project, issue_number=issue_number))
```

And above the existing `plan` command (around line 90), add the block breadcrumb:

```python
# v4-PHASE-8-KILL-BEGIN: legacy positional-arg planner CLI; replaced by `plan-v4` (below). Remove this command + decorator stack + label-write imports in Phase 8.
@cli.command()
@click.argument("issue_url", type=str)
@click.option("--project", required=True, ...)
...
def plan(issue_url: str, project: str, ...) -> None:
    ...  # existing body untouched
# v4-PHASE-8-KILL-END
```

In Phase 8, `plan-v4` is renamed to `plan` after the legacy command is deleted.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/roles/test_planner_outcome.py -v`
Expected: 3 passed

- [ ] **Step 6: Verify legacy tests still pass**

Run: `uv run pytest packages/foreman/tests/test_roles_planner.py -q`
Expected: legacy suite stays green — we didn't touch its code paths.

- [ ] **Step 7: Commit**

```bash
git add packages/foreman/src/foreman/roles/planner.py packages/foreman/src/foreman/cli.py packages/foreman/tests/v4/roles/__init__.py packages/foreman/tests/v4/roles/test_planner_outcome.py
git commit -m "feat(v4): planner adds emit-based plan-v4 CLI (legacy plan untouched)"
```

### Task 5.3: Reviewer — additive v4 emit path (target-aware)

**Files:**
- Modify: `packages/foreman/src/foreman/roles/reviewer.py` — ADD `run_reviewer_cli`; mark existing entry-point with breadcrumb
- Modify: `packages/foreman/src/foreman/cli.py` — ADD `review-v4` command; mark existing `review` command with breadcrumbs
- Create: `packages/foreman/tests/v4/roles/test_reviewer_outcome.py` (purely new)
- DO NOT TOUCH: `packages/foreman/tests/test_roles_reviewer.py` (covers the v3 path, still running)

**Follow the Task 5.2 additive template.** Same shape: append `run_reviewer_cli(*, project, issue_number, target)` and tag the legacy entry-point with `# v4-PHASE-8-KILL`. Add a new `@cli.command("review-v4")` alongside the existing `review` command (which gets the BEGIN/END breadcrumb block).

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

- [ ] **Step 3: Add the v4 exit path to `reviewer.py` (additive — see Task 5.2 template)**

Append the following alongside the existing `reviewer.py` code. Tag the legacy entry-point function with `# v4-PHASE-8-KILL` before the function header.

```python
# Append to packages/foreman/src/foreman/roles/reviewer.py — additive

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

- [ ] **Step 4: ADD `review-v4` CLI command in `cli.py`** — alongside the existing `review` command (which gets the `v4-PHASE-8-KILL-BEGIN`/`-END` breadcrumb block). The new command takes `--project`, `--issue-number`, `--target`. One-liner body that calls `run_reviewer_cli(...)` and exits with its return code.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/roles/test_reviewer_outcome.py -v`
Expected: 5 passed (2 parametrized × 2 + 1 standalone = 5)

- [ ] **Step 6: Verify legacy tests still pass**

Run: `uv run pytest packages/foreman/tests/test_roles_reviewer.py -q`
Expected: legacy suite green.

- [ ] **Step 7: Commit**

```bash
git add packages/foreman/src/foreman/roles/reviewer.py packages/foreman/src/foreman/cli.py packages/foreman/tests/v4/roles/test_reviewer_outcome.py
git commit -m "feat(v4): reviewer adds emit-based review-v4 CLI (target-aware; legacy untouched)"
```

### Task 5.4: Fixer emits Outcome (target-aware)

**Files:**
- Modify: `packages/foreman/src/foreman/roles/fixer.py` — ADD `run_fixer_cli`; breadcrumb legacy entry-point
- Modify: `packages/foreman/src/foreman/cli.py` — ADD `fix-v4` command; breadcrumb legacy `fix`
- Create: `packages/foreman/tests/v4/roles/test_fixer_outcome.py` (purely new)
- DO NOT TOUCH: `packages/foreman/tests/test_roles_fixer.py` (covers v3 path, still running)

**Follow the Task 5.2 additive template.** Same shape as Reviewer (5.3): target-aware (`fixer-spec`, `fixer-impl`), three outcome paths. Additive — do NOT delete or modify the existing entry-point or `fix` command.

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

- [ ] **Step 3: Add the v4 exit path to `fixer.py` (additive — see Task 5.2 template)** — same pattern as Reviewer; `pushed → CLEAN`, `escalated → NEEDS_HELP`, exception → `ERROR`. Tag the legacy entry-point with `# v4-PHASE-8-KILL`.

```python
# Append to packages/foreman/src/foreman/roles/fixer.py — additive
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

- [ ] **Step 4: ADD `fix-v4` CLI command in `cli.py`** — alongside the existing `fix` command (which gets the `v4-PHASE-8-KILL-BEGIN`/`-END` breadcrumb block). Takes `--project`, `--issue-number`, `--target`. One-liner calling `run_fixer_cli(...)`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/roles/test_fixer_outcome.py -v`
Expected: 5 passed

- [ ] **Step 6: Verify legacy tests still pass**

Run: `uv run pytest packages/foreman/tests/test_roles_fixer.py -q`
Expected: legacy suite green.

- [ ] **Step 7: Commit**

```bash
git add packages/foreman/src/foreman/roles/fixer.py packages/foreman/src/foreman/cli.py packages/foreman/tests/v4/roles/test_fixer_outcome.py
git commit -m "feat(v4): fixer adds emit-based fix-v4 CLI (target-aware; legacy untouched)"
```

### Task 5.5: Worker emits Outcome (CLEAN | BLOCKED | NEEDS_HELP | ERROR)

**Files:**
- Modify: `packages/foreman/src/foreman/roles/worker.py` — ADD `run_worker_cli`; breadcrumb legacy entry-point
- Modify: `packages/foreman/src/foreman/cli.py` — ADD `implement-v4` command; breadcrumb legacy `implement`
- Create: `packages/foreman/tests/v4/roles/test_worker_outcome.py` (purely new)
- DO NOT TOUCH: `packages/foreman/tests/test_roles_worker.py` (covers v3 path, still running)

**Follow the Task 5.2 additive template.** The Worker is the only role that produces `BLOCKED` (the impl PR was opened but CI is still in flight). The state machine handles BLOCKED by re-polling (ImplementingState `next_state` returns a fresh `ImplementingState()`).

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

- [ ] **Step 3: Add the v4 exit path to `worker.py` (additive — see Task 5.2 template)**

Tag the legacy entry-point with `# v4-PHASE-8-KILL` before its `def`. Append:

```python
# Append to packages/foreman/src/foreman/roles/worker.py — additive
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

- [ ] **Step 4: ADD `implement-v4` CLI command in `cli.py`** — alongside the existing `implement` command (which gets the `v4-PHASE-8-KILL-BEGIN`/`-END` breadcrumb block). Takes `--project`, `--issue-number`. One-liner calling `run_worker_cli(...)`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/roles/test_worker_outcome.py -v`
Expected: 4 passed

- [ ] **Step 6: Verify legacy tests still pass**

Run: `uv run pytest packages/foreman/tests/test_roles_worker.py -q`
Expected: legacy suite green.

- [ ] **Step 7: Commit**

```bash
git add packages/foreman/src/foreman/roles/worker.py packages/foreman/src/foreman/cli.py packages/foreman/tests/v4/roles/test_worker_outcome.py
git commit -m "feat(v4): worker adds emit-based implement-v4 CLI (legacy untouched)"
```

### Task 5.6: SubprocessRoleDispatcher — production impl

**Files:**
- Create: `packages/foreman/src/foreman/v4/subprocess_dispatcher.py`
- Test: `packages/foreman/tests/v4/test_subprocess_dispatcher.py`

The production `RoleDispatcher` impl. Shells out to `foreman <role> --project <p> --issue-number <n>` with the appropriate per-role identity (PAT or App token) in `GH_TOKEN`. Captures stdout + stderr; returns stdout for the state machine's verify hook to parse. (Every `foreman <role>` invocation emits `FOREMAN_OUTCOME:` now — no flag.)

Per-role identity wiring lives in `foreman.identity` (survival set). For each role string the dispatcher receives, it resolves to a token via `identity.get_role_token(role_name)`.

| `role` value | invokes | identity |
| --- | --- | --- |
| `planner` | `foreman plan-v4` | planner App |
| `reviewer-spec` | `foreman review-v4 --target spec` | reviewer App |
| `reviewer-impl` | `foreman review-v4 --target impl` | reviewer App |
| `fixer-spec` | `foreman fix-v4 --target spec` | fixer App |
| `fixer-impl` | `foreman fix-v4 --target impl` | fixer App |
| `worker` | `foreman implement-v4` | worker App |

The `-v4` suffix on subcommands matches the additive CLI commands added in Tasks 5.2-5.5. Phase 8 renames the `-v4` variants to their unsuffixed names after the legacy commands are deleted; at that point this dispatcher's `_ROLE_TO_INVOCATION` table is updated in lockstep.

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


# v4-PHASE-8-RENAME: subcommand strings carry "-v4" suffix to coexist with
# legacy v3 commands during Phases 5-7. Phase 8 strips the suffix after
# the legacy commands are deleted. This is the ONLY change required here
# during cutover.
_ROLE_TO_INVOCATION: dict[str, _Invocation] = {
    "planner":       _Invocation(subcommand="plan-v4",      target=None),
    "reviewer-spec": _Invocation(subcommand="review-v4",    target="spec"),
    "reviewer-impl": _Invocation(subcommand="review-v4",    target="impl"),
    "fixer-spec":    _Invocation(subcommand="fix-v4",       target="spec"),
    "fixer-impl":    _Invocation(subcommand="fix-v4",       target="impl"),
    "worker":        _Invocation(subcommand="implement-v4", target=None),
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
