# Foreman Crash Recovery — Stage 1b: Planner PR-idempotency — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A daemon crash mid-Planning must not, on re-run, create a duplicate spec PR (GitHub 422 → subprocess crash → ticket → Failed). Give the Planner the same existing-PR adopt guard the Worker already has — via one shared helper (no duplicated PR-lookup logic).

**Architecture:** Extract the Worker's already-generalized `_find_open_pr_by_head_branch` into a shared role module; both Planner and Worker call it. The Planner, before opening the spec PR, checks for an open PR on `spec_branch(issue_number)` and **adopts** it (skips commit/push/create) instead of unconditionally creating — mirroring `roles/worker.py:1242-1257`.

**Tech Stack:** Python 3.12, v3 survival-set roles (`roles/planner.py`, `roles/worker.py`), PyGithub, pytest. TDD.

**Scope:** Stage 1b only (Planner spec-PR idempotency). The Worker is already idempotent (issue #342). Reviewer/Fixer comment-idempotency and Stage 2 (resume) are out of scope. Design: `docs/superpowers/specs/2026-06-25-foreman-crash-recovery-design.md` (I1).

**Key code refs (verified):**
- Worker helper: `roles/worker.py:498` `def _find_open_pr_by_head_branch(repo, owner, branch) -> PullRequest | None` (general; 2 call sites in worker.py).
- Worker adopt pattern: `roles/worker.py:1242-1257` (probe → if existing, skip push+create).
- Worker gets repo: `roles/worker.py:862,869` `host, _, worker_client = build_role_resources(...)`; `repo = worker_client.get_repo(actual_repo_slug)`.
- Planner create (the gap): `roles/planner.py:380-405` (commit → push → `open_pull_request` unconditionally → `pr_number`).
- Planner has client: `roles/planner.py:308` `host, planner_token, _planner_client = build_role_resources(...)` (client currently discarded).
- Branch name: `branches.py` `spec_branch(N) → "foreman/issue-<N>"`.

---

## File structure

- `packages/foreman/src/foreman/roles/_pr_lookup.py` — **new**: the shared `find_open_pr_by_head_branch` helper (moved verbatim from worker.py).
- `packages/foreman/src/foreman/roles/worker.py` — import the helper from the new module; delete the local def; call sites unchanged.
- `packages/foreman/src/foreman/roles/planner.py` — un-discard the client, fetch `repo`, add the adopt-or-create guard around the spec-PR creation.
- Tests: `packages/foreman/tests/test_pr_lookup.py` (**new**), `packages/foreman/tests/test_roles_planner*.py` (Planner adopt path — match existing planner test file/pattern), and the existing worker tests must stay green (no behavior change).

---

### Task 1: Extract the shared PR-lookup helper (no behavior change)

**Files:**
- Create: `packages/foreman/src/foreman/roles/_pr_lookup.py`
- Modify: `packages/foreman/src/foreman/roles/worker.py`
- Test: `packages/foreman/tests/test_pr_lookup.py`

- [ ] **Step 1: Move the helper verbatim**

Cut the entire `_find_open_pr_by_head_branch(repo, owner, branch) -> PullRequest | None` function (worker.py:498 through the end of its body, including its docstring) into the new `roles/_pr_lookup.py`. Rename to the public `find_open_pr_by_head_branch` (drop the leading underscore — it's now a shared API). Carry its imports (`from github.Repository import Repository`, `from github.PullRequest import PullRequest`, etc. — read worker.py's import block to get the exact symbols).

- [ ] **Step 2: Rewire worker.py to import it**

In `worker.py`, delete the local def and add `from foreman.roles._pr_lookup import find_open_pr_by_head_branch`. Update both call sites (the spec-PR lookup and the impl-PR detection) from `_find_open_pr_by_head_branch(...)` to `find_open_pr_by_head_branch(...)`. No argument changes.

- [ ] **Step 3: Add a focused unit test for the helper**

`test_pr_lookup.py`: construct a fake/mocked PyGithub `repo` whose `get_pulls(state="open", head=...)` yields a PR for a known branch and nothing for another; assert `find_open_pr_by_head_branch` returns the PR for the match and `None` otherwise. (Mirror how existing worker tests fake the repo — read `test_roles_worker*.py` for the mock pattern.)

- [ ] **Step 4: Run helper test + the full worker suite (no regressions)**

Run: `uv run pytest packages/foreman/tests/test_pr_lookup.py packages/foreman/tests/test_roles_worker.py -o addopts="" -v`
Expected: new test passes; every existing worker test still passes (the extraction is behavior-preserving).

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/roles/_pr_lookup.py packages/foreman/src/foreman/roles/worker.py packages/foreman/tests/test_pr_lookup.py
git commit -m "refactor(roles): extract shared find_open_pr_by_head_branch helper"
```

---

### Task 2: Planner adopts an existing spec PR instead of creating a duplicate

**Files:**
- Modify: `packages/foreman/src/foreman/roles/planner.py`
- Test: the Planner role test file (match the existing one)

- [ ] **Step 1: Write the failing test**

In the Planner test module, add a test that drives `_run_planner_core` (or the same entry the existing planner tests use — read them first) with a **fake host/client where an open PR already exists on `foreman/issue-<N>`**. Assert the Planner:
- does NOT call `open_pull_request` (no second PR / no 422),
- emits a **CLEAN** outcome whose `pr_number` equals the existing PR's number.
Without the fix this fails (the Planner calls `open_pull_request` → the fake raises 422 / records a second create).

- [ ] **Step 2: Run it; confirm FAIL**

Run: `uv run pytest packages/foreman/tests/<planner_test_file>.py -k adopt -o addopts="" -v`
Expected: FAIL.

- [ ] **Step 3: Implement the adopt-or-create guard**

In `planner.py`:
1. Un-discard the client: `host, planner_token, planner_client = build_role_resources(...)` (was `_planner_client`).
2. After computing `branch = spec_branch(issue_number)` and before the commit/push/create block (planner.py:380-399), fetch the repo and probe:

```python
from foreman.roles._pr_lookup import find_open_pr_by_head_branch
...
repo = planner_client.get_repo(actual_repo_slug)
existing_spec_pr = find_open_pr_by_head_branch(repo, owner=owner, branch=branch)
if existing_spec_pr is not None:
    # A previous (crashed) Planner dispatch already opened the spec PR.
    # Adopt it — re-creating would 422 and crash the subprocess, wedging
    # the ticket to Failed (the foreman#... crash-re-run case). Mirrors
    # the Worker's existing impl-PR idempotency (issue #342). We skip the
    # commit/push/create entirely; the spec doc the prior attempt
    # committed is already on the branch.
    pr = existing_spec_pr
else:
    host.commit_files_to_worktree(...)   # unchanged
    host.push_branch(...)                # unchanged
    pr = host.open_pull_request(...)     # unchanged
pr_number = pr.number
```

(Keep `pr_number = pr.number` and everything after it unchanged — both branches converge on the same CLEAN-outcome path. `usage` is still hoisted before this block per foreman#235.)

- [ ] **Step 4: Run the test; confirm PASS**

Run: `uv run pytest packages/foreman/tests/<planner_test_file>.py -o addopts="" -v`
Expected: PASS — the adopt test plus all existing planner tests (the create path is unchanged when no PR exists).

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/roles/planner.py packages/foreman/tests/<planner_test_file>.py
git commit -m "fix(roles): planner adopts existing spec PR on crash re-run (no duplicate)"
```

---

### Task 3: Full gate

- [ ] **Step 1:** Run `just check`. Expected fully green: ruff + mypy + import-linter (R1/R2) + full pytest (incl. live Postgres) + coverage ≥ 78%. Fix only Stage-1b-caused failures; stop and report any pre-existing/unrelated failure rather than forcing green.
- [ ] **Step 2:** If a fix was needed, commit it separately (`fix(roles): ...`).

---

## Self-review checklist

1. **Spec coverage:** Planner no longer duplicates the spec PR on re-run (Task 2) ✓; logic shared, not duplicated (Task 1 — the divergent-duplication smell the reviews flagged is avoided) ✓.
2. **No placeholders:** `<planner_test_file>` is the one hedge — the implementer reads the existing planner tests to find it.
3. **Behavior preservation:** Task 1 is a pure move (worker behavior identical); Task 2's `else` branch is byte-for-byte the old create path.

## Decision baked in (the adopt edge)

On adopt we **keep the prior attempt's spec doc** (skip commit/push) rather than re-pushing the re-run's freshly-generated doc — mirroring the Worker, simplest, and consistent (don't mutate a PR that may already be entering review). The re-run's LLM output is discarded (wasteful but correct; Stage 2's resume removes the waste). Alternative — push the fresh doc to update the PR, skipping only `create_pull` — is a one-line change if ever wanted.

## Not in this plan

- Reviewer/Fixer comment-idempotency (softer dup; separate follow-up).
- Stage 2 — session resume (volume mount + `resolve_dispatch` + mixing tests).
