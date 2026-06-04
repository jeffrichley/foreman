# Foreman v3 — State Machine + Auto-Merge Flags Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tighten the v3 reconciler's Reviewer-gate behavior (driven by GitHub PR review state, not internal labels) and add global + per-project flags governing auto-merge of spec and impl PRs.

**Architecture:** Extend the GraphQL observer to read `pullRequest.reviewDecision`. New rules drive label transitions from that review state (matching v3's "GH is truth" principle). Two new global config flags (`auto_merge_spec` default true, `auto_merge_impl` default false) with per-project override; the merge rules consult the effective value.

**Tech Stack:** Python 3.13, uv workspace, httpx (existing GraphQL client), pydantic v2 (config), pytest.

**Repo state:** Working in `e:/workspaces/ai/agents/foreman` on branch `fix/v3-operational-gaps`. PR #111 OPEN against main. New commits land on this branch and update PR #111 in place. Pre-push hook runs full pytest — investigate failures, never `--no-verify`. Stage specific files, not `git add -A`. Conventional commits, lowercase. `Implements #N` not `Closes #N` (foreman#63). Local git: `wrenrichley` / `wrenrichley@gmail.com` (already set).

---

## Files Touched

| File | Responsibility |
|---|---|
| `packages/foreman/src/foreman/reconciler/state.py` | Add `review_decision` field to `PRState` |
| `packages/foreman/src/foreman/reconciler/observer.py` | Extend GraphQL query to fetch `reviewDecision`; parse into `PRState` |
| `packages/foreman/src/foreman/reconciler/v3_host.py` | Fix Reviewer subprocess argv shape (positional `pr_url` for `foreman review`, not `--issue-url`) |
| `packages/foreman/src/foreman/reconciler/actions.py` | Add `Action.ADVANCE_LABEL_TO_IMPL_APPROVED` + executor branch |
| `packages/foreman/src/foreman/reconciler/rules.py` | New rules: `dispatch_reviewer_spec`, `advance_label_to_plan_approved_via_review`, `advance_label_to_impl_approved`. Tighten `merge_spec_pr` + `merge_impl_pr` to consult config flags. Drop the old `merge_spec_pr` immediate-on-CI-green rule shape (replace with: only after Reviewer-approve). |
| `packages/foreman/src/foreman/config.py` | Add `GlobalConfig.auto_merge_spec` (default `True`) + `GlobalConfig.auto_merge_impl` (default `False`); `ProjectConfig.auto_merge_impl` optional; helper `ReconcilerConfig.effective_auto_merge_spec(project)` and `.effective_auto_merge_impl(project)` |
| `packages/foreman/src/foreman/reconciler/loop.py` (or wherever the rule evaluator is wired up) | Plumb the effective merge flags into the `ActionContext` or into a callable predicate registered with the rule |
| `packages/foreman/prompts/worker_impl.md` | Add a step: when re-running on `foreman:impl-fix`, post a PR comment summarizing what was addressed before pushing the fix commit |
| `~/.foreman/config.toml` | Out-of-repo: add `[global]` section + explicit `auto_merge_impl = false` on `[projects.foreman]` |

Tests live alongside each module under `packages/foreman/tests/reconciler/` and `packages/foreman/tests/`.

---

## Decision Log (locked from brainstorm with Jeff 2026-06-04)

1. Spec PRs require Reviewer approve on GH before label moves `planning → plan-approved`. No auto-rerun of Planner if Reviewer rejects — sits there for human intervention.
2. Impl PRs require Reviewer approve on GH before label moves `impl-review → impl-approved`. Same human-intervention exit on reject.
3. `merge_spec_pr` rule fires on `plan-approved` + (PR mergeable + CI green) + `effective_auto_merge_spec` (default true).
4. `merge_impl_pr` rule fires on `impl-approved` + (PR mergeable + CI green) + `effective_auto_merge_impl` (default false).
5. Per-project `auto_merge_*` value, when set, overrides global. When unset, falls through to global default.
6. PR-comment-on-Reviewer-rejection: Reviewer's GH review IS the comment trail (already uses `gh pr review --request-changes`). Worker, on impl-fix re-run, posts a PR comment summarizing what was addressed. Planner doesn't re-run (no auto-loop on spec).
7. `foreman:planning` is a state-noun (the planning phase), kept as-is. `foreman:hold` continues to NOOP everything.

---

## Task 0: Verify baseline

**Files:** none

- [ ] **Step 0.1: Confirm working tree clean and on `fix/v3-operational-gaps`**

```bash
cd e:/workspaces/ai/agents/foreman
git status -sb
git rev-parse --abbrev-ref HEAD
```

Expected: branch `fix/v3-operational-gaps`, no staged changes (untracked `.foreman/` is OK — it's the daemon's working dir).

- [ ] **Step 0.2: Confirm baseline test count**

```bash
cd e:/workspaces/ai/agents/foreman
uv run pytest packages/foreman/tests -q 2>&1 | tail -3
```

Expected: `696 passed` (or close to it — record exact number for delta tracking).

---

## Task 1: Reviewer CLI arg fix

**Files:**
- Modify: `packages/foreman/src/foreman/reconciler/v3_host.py:144-158`
- Test: `packages/foreman/tests/reconciler/test_v3_host.py`

**Why:** `foreman review` takes positional `pr_url`, not `--issue-url`. Current v3_host argv shape would crash the Reviewer subprocess on dispatch.

- [ ] **Step 1.1: Write failing test that asserts reviewer argv shape**

Add to `packages/foreman/tests/reconciler/test_v3_host.py`:

```python
def test_dispatch_role_reviewer_uses_positional_pr_url():
    """Reviewer's `foreman review` CLI takes positional pr_url, not --issue-url."""
    captured: list[list[str]] = []

    class _FakeProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        async def wait(self) -> int:
            return 0

    def runner(argv: list[str]) -> _FakeProc:
        captured.append(argv)
        return _FakeProc(pid=42)

    log = ExecutionLog(db_path=":memory:")
    log.init()
    host = V3GitHubHost(
        v2_host=_FakeV2Host(),  # existing helper in test file
        log=log,
        subprocess_runner=runner,
    )

    host.dispatch_role(
        role="reviewer",
        owner="jeffrichley",
        repo="foreman",
        issue=63,
        pr_number=99,
    )

    assert captured, "runner not called"
    argv = captured[0]
    assert "review" in argv
    # Reviewer must NOT use --issue-url
    assert "--issue-url" not in argv
    # Reviewer must receive PR URL positionally
    assert "https://github.com/jeffrichley/foreman/pull/99" in argv
    # The PR URL must be the positional argument after "review" (and any --project flag).
    review_idx = argv.index("review")
    rest = argv[review_idx + 1 :]
    pr_idx = rest.index("https://github.com/jeffrichley/foreman/pull/99")
    # No flag immediately preceding the URL (i.e., it's positional).
    if pr_idx > 0:
        assert not rest[pr_idx - 1].startswith("--") or rest[pr_idx - 1] == "--project"
```

Run: `uv run pytest packages/foreman/tests/reconciler/test_v3_host.py::test_dispatch_role_reviewer_uses_positional_pr_url -v`
Expected: FAIL (current code constructs `--issue-url <url>`).

- [ ] **Step 1.2: Fix `dispatch_role` to branch on role for the URL arg shape**

In `packages/foreman/src/foreman/reconciler/v3_host.py`, replace the existing `dispatch_role` body's argv assembly (lines ~141–158):

```python
        subcommand = _ROLE_TO_SUBCOMMAND.get(role)
        if subcommand is None:
            raise ValueError(f"unknown role for dispatch: {role!r}")

        argv: list[str] = ["uv", "run", "foreman", subcommand]

        if role == "reviewer":
            # `foreman review` takes a positional PR URL, no --issue-url flag.
            if pr_number is None:
                raise ValueError("dispatch_role(role='reviewer') requires pr_number")
            pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
            argv.extend([pr_url, "--project", self._project_name])
        else:
            issue_url = f"https://github.com/{owner}/{repo}/issues/{issue}"
            argv.extend(["--issue-url", issue_url, "--project", self._project_name])
            if pr_number is not None:
                pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
                argv.extend(["--pr-url", pr_url])
```

Run: `uv run pytest packages/foreman/tests/reconciler/test_v3_host.py -v`
Expected: PASS for the new test + all existing v3_host tests.

- [ ] **Step 1.3: Commit**

```bash
cd e:/workspaces/ai/agents/foreman
git add packages/foreman/src/foreman/reconciler/v3_host.py packages/foreman/tests/reconciler/test_v3_host.py
git commit -m "fix(reconciler): use positional pr_url for reviewer dispatch in v3"
```

---

## Task 2: Extend `PRState` with `review_decision`

**Files:**
- Modify: `packages/foreman/src/foreman/reconciler/state.py` (add field to PRState)
- Test: `packages/foreman/tests/reconciler/test_state.py`

**Why:** Rules need to see Reviewer signoff state from GitHub. Single source: `PullRequest.reviewDecision` (one of `APPROVED`, `CHANGES_REQUESTED`, `REVIEW_REQUIRED`, or null).

- [ ] **Step 2.1: Write failing test**

Add to `packages/foreman/tests/reconciler/test_state.py` (or create it):

```python
from foreman.reconciler.state import PRState

def test_pr_state_carries_review_decision():
    pr = PRState(
        number=10,
        head_ref="feat/x",
        mergeable="MERGEABLE",
        ci_status="SUCCESS",
        body="",
        linked_issue_numbers=(),
        is_merged=False,
        review_decision="APPROVED",
    )
    assert pr.review_decision == "APPROVED"

def test_pr_state_review_decision_defaults_to_none():
    pr = PRState(
        number=10,
        head_ref="feat/x",
        mergeable="MERGEABLE",
        ci_status="SUCCESS",
        body="",
        linked_issue_numbers=(),
        is_merged=False,
    )
    assert pr.review_decision is None
```

Run: `uv run pytest packages/foreman/tests/reconciler/test_state.py -v -k review_decision`
Expected: FAIL (PRState has no `review_decision` field).

- [ ] **Step 2.2: Add field to `PRState` (default `None` for back-compat)**

In `packages/foreman/src/foreman/reconciler/state.py`, locate the `PRState` dataclass and add (as the LAST field, with default None for back-compat):

```python
    review_decision: str | None = None
```

Then re-run the test. Expected: PASS.

- [ ] **Step 2.3: Commit**

```bash
cd e:/workspaces/ai/agents/foreman
git add packages/foreman/src/foreman/reconciler/state.py packages/foreman/tests/reconciler/test_state.py
git commit -m "feat(reconciler): add review_decision field to PRState"
```

---

## Task 3: Observer fetches `reviewDecision`

**Files:**
- Modify: `packages/foreman/src/foreman/reconciler/observer.py` (extend GraphQL query + parser)
- Test: `packages/foreman/tests/reconciler/test_observer.py`

- [ ] **Step 3.1: Write failing test that injects a fake GraphQL client returning a PR node with `reviewDecision: "APPROVED"`**

Add to `packages/foreman/tests/reconciler/test_observer.py`:

```python
def test_fetch_project_state_parses_review_decision():
    class FakeGH:
        def graphql(self, query, variables):
            assert "reviewDecision" in query
            return {
                "data": {
                    "repository": {
                        "issues": {"nodes": []},
                        "pullRequests": {
                            "nodes": [
                                {
                                    "number": 42,
                                    "headRefName": "feat/x",
                                    "body": "",
                                    "mergeable": "MERGEABLE",
                                    "merged": False,
                                    "statusCheckRollup": {"state": "SUCCESS"},
                                    "closingIssuesReferences": {"nodes": []},
                                    "reviewDecision": "APPROVED",
                                }
                            ]
                        },
                    }
                }
            }

    snap = fetch_project_state(
        project="foreman", owner="jeffrichley", repo="foreman", gh=FakeGH()
    )
    assert len(snap.prs) == 1
    assert snap.prs[0].review_decision == "APPROVED"

def test_fetch_project_state_handles_null_review_decision():
    class FakeGH:
        def graphql(self, query, variables):
            return {
                "data": {
                    "repository": {
                        "issues": {"nodes": []},
                        "pullRequests": {
                            "nodes": [
                                {
                                    "number": 42,
                                    "headRefName": "feat/x",
                                    "body": "",
                                    "mergeable": "MERGEABLE",
                                    "merged": False,
                                    "statusCheckRollup": {"state": "SUCCESS"},
                                    "closingIssuesReferences": {"nodes": []},
                                    "reviewDecision": None,
                                }
                            ]
                        },
                    }
                }
            }

    snap = fetch_project_state(
        project="foreman", owner="jeffrichley", repo="foreman", gh=FakeGH()
    )
    assert snap.prs[0].review_decision is None
```

Run: `uv run pytest packages/foreman/tests/reconciler/test_observer.py -v -k review_decision`
Expected: FAIL.

- [ ] **Step 3.2: Add `reviewDecision` to the GraphQL query string in `observer.py`**

In the `_QUERY` constant, inside the `pullRequests` `nodes` block, add `reviewDecision` to the field list (between `closingIssuesReferences` and the closing brace works fine):

```graphql
pullRequests(first: 100, states: OPEN) {
  nodes {
    number
    headRefName
    body
    mergeable
    merged
    statusCheckRollup { state }
    closingIssuesReferences(first: 10) { nodes { number } }
    reviewDecision
  }
}
```

- [ ] **Step 3.3: Update `_parse_pr` to extract `reviewDecision`**

Modify `_parse_pr` to read the field and pass it to `PRState`:

```python
def _parse_pr(node: dict[str, Any]) -> PRState:
    linked = tuple(
        int(n["number"])
        for n in (node.get("closingIssuesReferences") or {}).get("nodes", [])
    )
    rollup = node.get("statusCheckRollup")
    ci = rollup["state"] if rollup else None
    review_decision = node.get("reviewDecision")  # may be None
    return PRState(
        number=int(node["number"]),
        head_ref=str(node.get("headRefName", "")),
        mergeable=str(node.get("mergeable", "UNKNOWN")),
        ci_status=ci,
        body=str(node.get("body", "") or ""),
        linked_issue_numbers=linked,
        is_merged=bool(node.get("merged", False)),
        review_decision=review_decision,
    )
```

Re-run test. Expected: PASS.

- [ ] **Step 3.4: Commit**

```bash
cd e:/workspaces/ai/agents/foreman
git add packages/foreman/src/foreman/reconciler/observer.py packages/foreman/tests/reconciler/test_observer.py
git commit -m "feat(reconciler): observer fetches PR reviewDecision from graphql"
```

---

## Task 4: Add config flags + effective resolver

**Files:**
- Modify: `packages/foreman/src/foreman/config.py` (new fields + resolver)
- Test: `packages/foreman/tests/test_config.py`

**Why:** Two global flags (`auto_merge_spec` default `True`, `auto_merge_impl` default `False`); per-project may override; effective resolver chooses.

- [ ] **Step 4.1: Write failing tests for the resolver**

Add to `packages/foreman/tests/test_config.py`:

```python
def test_global_auto_merge_defaults():
    """Global defaults: spec=true, impl=false."""
    cfg = ReconcilerConfig()
    assert cfg.auto_merge_spec is True
    assert cfg.auto_merge_impl is False

def test_project_auto_merge_inherits_global_when_unset():
    cfg = ReconcilerConfig(auto_merge_spec=True, auto_merge_impl=False)
    project = ProjectConfig(repo="foo/bar", local_clone_path="/tmp/foo")
    assert cfg.effective_auto_merge_spec(project) is True
    assert cfg.effective_auto_merge_impl(project) is False

def test_project_auto_merge_override_wins():
    cfg = ReconcilerConfig(auto_merge_spec=True, auto_merge_impl=False)
    # Project disables spec auto-merge, leaves impl alone.
    project = ProjectConfig(
        repo="foo/bar",
        local_clone_path="/tmp/foo",
        auto_merge_spec=False,
    )
    assert cfg.effective_auto_merge_spec(project) is False
    assert cfg.effective_auto_merge_impl(project) is False

def test_project_can_enable_impl_auto_merge():
    cfg = ReconcilerConfig(auto_merge_spec=True, auto_merge_impl=False)
    project = ProjectConfig(
        repo="foo/bar",
        local_clone_path="/tmp/foo",
        auto_merge_impl=True,
    )
    assert cfg.effective_auto_merge_impl(project) is True
```

Run: `uv run pytest packages/foreman/tests/test_config.py -v -k auto_merge`
Expected: FAIL.

- [ ] **Step 4.2: Add fields + resolver methods to `ReconcilerConfig` and `ProjectConfig`**

In `packages/foreman/src/foreman/config.py`, identify the `ReconcilerConfig` and `ProjectConfig` classes. Add to `ReconcilerConfig`:

```python
    auto_merge_spec: bool = True
    auto_merge_impl: bool = False

    def effective_auto_merge_spec(self, project: ProjectConfig) -> bool:
        if project.auto_merge_spec is not None:
            return project.auto_merge_spec
        return self.auto_merge_spec

    def effective_auto_merge_impl(self, project: ProjectConfig) -> bool:
        if project.auto_merge_impl is not None:
            return project.auto_merge_impl
        return self.auto_merge_impl
```

To `ProjectConfig`, add (optional override fields):

```python
    auto_merge_spec: bool | None = None
    auto_merge_impl: bool | None = None
```

If `ProjectConfig` already has an `auto_merge_spec` field with a non-None default, change it to `bool | None = None` so "unset" means "inherit from global." Update any existing usage of `project.auto_merge_spec` to use `cfg.effective_auto_merge_spec(project)` instead.

Re-run test. Expected: PASS for the four new tests.

- [ ] **Step 4.3: Verify existing config-loading tests still pass**

```bash
cd e:/workspaces/ai/agents/foreman
uv run pytest packages/foreman/tests/test_config.py -v
```

Expected: ALL config tests pass (no regression from field-default change).

- [ ] **Step 4.4: Commit**

```bash
cd e:/workspaces/ai/agents/foreman
git add packages/foreman/src/foreman/config.py packages/foreman/tests/test_config.py
git commit -m "feat(config): global+per-project auto_merge_spec/impl flags"
```

---

## Task 5: Wire effective flags into ActionContext

**Files:**
- Modify: `packages/foreman/src/foreman/reconciler/actions.py` (add `auto_merge_spec` + `auto_merge_impl` fields to `ActionContext`)
- Modify: `packages/foreman/src/foreman/reconciler/loop.py` (or wherever `ActionContext` is constructed per-tick) — pass effective values from config
- Test: `packages/foreman/tests/reconciler/test_actions_context.py`

**Why:** Rules need to see the effective merge flags. Cleanest is to carry them on the context — rules check `ctx.auto_merge_spec` / `ctx.auto_merge_impl` exactly like they check labels.

- [ ] **Step 5.1: Write failing test**

Add to `packages/foreman/tests/reconciler/test_actions_context.py` (create if needed):

```python
from foreman.reconciler.actions import ActionContext
from foreman.reconciler.state import ProjectSnapshot, IssueState, PRState
from foreman.reconciler.exec_log import ExecutionLog
from datetime import datetime, UTC

def _snap():
    return ProjectSnapshot(
        project="foreman",
        owner="jeffrichley",
        repo="foreman",
        issues=(),
        prs=(),
        fetched_at=datetime.now(UTC),
    )

def _issue():
    return IssueState(
        number=1,
        title="t",
        labels=(),
        assignees=(),
        body="",
        updated_at=datetime.now(UTC),
    )

def test_action_context_carries_merge_flags():
    log = ExecutionLog(db_path=":memory:")
    log.init()
    ctx = ActionContext(
        snapshot=_snap(),
        issue=_issue(),
        pr=None,
        log=log,
        auto_merge_spec=True,
        auto_merge_impl=False,
    )
    assert ctx.auto_merge_spec is True
    assert ctx.auto_merge_impl is False
```

Expected: FAIL (ActionContext has no `auto_merge_*` fields).

- [ ] **Step 5.2: Add fields to `ActionContext`**

In `packages/foreman/src/foreman/reconciler/actions.py`, the `ActionContext` dataclass — add two fields with sensible defaults so existing tests don't all break:

```python
@dataclass(frozen=True)
class ActionContext:
    snapshot: ProjectSnapshot
    issue: IssueState
    pr: PRState | None
    log: ExecutionLog
    auto_merge_spec: bool = True
    auto_merge_impl: bool = False
    ...
```

Run the new test. Expected: PASS. Run all reconciler tests. Expected: no regressions.

- [ ] **Step 5.3: Wire effective flags at the loop construction site**

Locate where `ActionContext(...)` is constructed in the live reconciler loop (search: `grep -rn "ActionContext(" packages/foreman/src/foreman/reconciler/`). At each call site (likely `loop.py` and the executor wiring), pull the per-project `ProjectConfig` from the config, and compute:

```python
auto_merge_spec = config.effective_auto_merge_spec(project_cfg)
auto_merge_impl = config.effective_auto_merge_impl(project_cfg)
ctx = ActionContext(..., auto_merge_spec=auto_merge_spec, auto_merge_impl=auto_merge_impl)
```

If the loop has a `_build_action_context` helper or similar, modify it to accept (or look up) project config. Be careful to thread the config through any test fixtures so existing tests can construct `ActionContext` with explicit flags.

Run the full reconciler test suite. Expected: green.

- [ ] **Step 5.4: Commit**

```bash
cd e:/workspaces/ai/agents/foreman
git add packages/foreman/src/foreman/reconciler/actions.py packages/foreman/src/foreman/reconciler/loop.py packages/foreman/tests/reconciler/test_actions_context.py
git commit -m "feat(reconciler): plumb effective auto_merge flags into ActionContext"
```

---

## Task 6: Tighten `merge_spec_pr` to require Reviewer-approve + auto_merge_spec flag

**Files:**
- Modify: `packages/foreman/src/foreman/reconciler/rules.py`
- Test: `packages/foreman/tests/reconciler/test_rules.py`

**Why:** Currently `merge_spec_pr` fires on `foreman:planning + PR mergeable + CI green`. That auto-merges specs with no Reviewer signoff. Tighten to require GH-side approve AND respect `auto_merge_spec`.

- [ ] **Step 6.1: Write failing tests**

Add to `packages/foreman/tests/reconciler/test_rules.py`:

```python
def test_merge_spec_pr_requires_review_approve(make_ctx):
    """Spec PR with mergeable+green CI but no Reviewer signoff should NOT fire merge."""
    ctx = make_ctx(
        labels=("foreman:planning",),
        pr_state={
            "mergeable": "MERGEABLE",
            "ci_status": "SUCCESS",
            "review_decision": None,  # not yet reviewed
        },
        auto_merge_spec=True,
    )
    assert evaluate(ctx) != Action.MERGE_SPEC_PR

def test_merge_spec_pr_fires_on_review_approve_and_flag_on(make_ctx):
    ctx = make_ctx(
        labels=("foreman:plan-approved",),
        pr_state={
            "mergeable": "MERGEABLE",
            "ci_status": "SUCCESS",
            "review_decision": "APPROVED",
        },
        auto_merge_spec=True,
    )
    assert evaluate(ctx) == Action.MERGE_SPEC_PR

def test_merge_spec_pr_blocked_when_flag_off(make_ctx):
    ctx = make_ctx(
        labels=("foreman:plan-approved",),
        pr_state={
            "mergeable": "MERGEABLE",
            "ci_status": "SUCCESS",
            "review_decision": "APPROVED",
        },
        auto_merge_spec=False,
    )
    assert evaluate(ctx) != Action.MERGE_SPEC_PR
```

You may need to extend `make_ctx` (existing test fixture) with `auto_merge_spec` / `auto_merge_impl` parameters.

Run: `uv run pytest packages/foreman/tests/reconciler/test_rules.py -v -k merge_spec_pr`
Expected: FAIL on at least the flag-off test and the review-required test.

- [ ] **Step 6.2: Redesign the spec-PR rules**

In `packages/foreman/src/foreman/reconciler/rules.py`:

1. Add a new rule predicate + rule `dispatch_reviewer_spec` (precedence between planner and merge):

```python
def _planning_pr_needs_review(ctx: ActionContext) -> bool:
    return (
        "foreman:planning" in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.pr.is_merged
        and ctx.pr.review_decision in (None, "REVIEW_REQUIRED")
        and not ctx.log.has_unterminated("dispatch_reviewer", ctx.ticket_id)
    )
```

2. Replace the existing `_planning_pr_green` predicate with one for the new transition rule:

```python
def _planning_pr_approved(ctx: ActionContext) -> bool:
    return (
        "foreman:planning" in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.pr.is_merged
        and ctx.pr.review_decision == "APPROVED"
    )
```

3. Update `_spec_pr_merged_label_lagging` is fine; keep.

4. Update `_merge_spec_pr` to depend on `foreman:plan-approved` (post-label-transition) AND the flag:

```python
def _plan_approved_pr_green_and_flag(ctx: ActionContext) -> bool:
    return (
        "foreman:plan-approved" in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.pr.is_merged
        and ctx.pr.mergeable == "MERGEABLE"
        and ctx.pr.ci_status == "SUCCESS"
        and ctx.auto_merge_spec
    )
```

Wait — `foreman:plan-approved` was used elsewhere (`dispatch_worker`). Need to make sure semantics still work: now `plan-approved` means "spec approved, ready to merge or already merged." After merge, label flips to `plan-approved` already set (Worker dispatches on it). So we need a way to distinguish "plan-approved AND PR still open" vs "plan-approved AND PR merged." That's already handled by `ctx.pr is None` (PR merged → not in observer's open-PR set → ctx.pr is None) — but Worker's `_plan_approved_no_impl_pr` predicate uses `ctx.pr is None`. So the flow is:
- planning + PR open + review APPROVED → advance_label_to_plan_approved → label becomes plan-approved (PR still open)
- plan-approved + PR open + green + flag → merge_spec_pr → PR merged
- plan-approved + PR gone (merged or no impl PR yet) → dispatch_worker

But `dispatch_worker` fires on `_plan_approved_no_impl_pr` which currently checks `ctx.pr is None`. After spec merge, the spec PR is gone — but the impl PR doesn't exist yet, so ctx.pr is None → Worker fires. Good.

Actually wait: there's a subtle issue. The observer fetches OPEN PRs. After spec merge, the spec PR is closed/merged and falls out of the observer set. So during the same tick where the merge happens, the next observer poll would not see the spec PR. So `dispatch_worker` would fire one tick later. Good.

5. Now arrange the new rules in `_PROGRESS_RULES`:

```python
_PROGRESS_RULES = (
    Rule(name="dispatch_planner", ..., when=_planning_no_pr, then=Action.DISPATCH_PLANNER, precedence=100),
    Rule(name="dispatch_reviewer_spec", ..., when=_planning_pr_needs_review, then=Action.DISPATCH_REVIEWER, precedence=110),
    Rule(name="advance_label_to_plan_approved", ..., when=_planning_pr_approved, then=Action.ADVANCE_LABEL_TO_PLAN_APPROVED, precedence=115),
    Rule(name="merge_spec_pr", ..., when=_plan_approved_pr_green_and_flag, then=Action.MERGE_SPEC_PR, precedence=120),
    Rule(name="advance_label_to_plan_approved_lagging", ..., when=_spec_pr_merged_label_lagging, then=Action.ADVANCE_LABEL_TO_PLAN_APPROVED, precedence=125),  # safety net
    Rule(name="dispatch_worker", ..., when=_plan_approved_no_impl_pr, then=Action.DISPATCH_WORKER, precedence=130),
    ...
)
```

Note: the existing `advance_label_to_plan_approved` rule name fires on `_spec_pr_merged_label_lagging` (post-merge label catch-up). Keep that as a safety net under a different name to avoid duplicate rule names — or merge the two into one rule with an OR predicate. Pick whichever is cleaner; document the choice in the commit message.

**Action target:** `Action.DISPATCH_REVIEWER` is already plumbed for impl review. The same action fires on spec PR — the host's `dispatch_role(role="reviewer", pr_number=spec_pr.number)` will spawn `foreman review <spec_pr_url> --project foreman`. The reviewer prompt is already target-aware (from foreman#78/#79 work) — it detects spec vs impl by the PR's contents/branch.

Re-run tests. Expected: PASS for the three new tests + no regressions.

- [ ] **Step 6.3: Commit**

```bash
cd e:/workspaces/ai/agents/foreman
git add packages/foreman/src/foreman/reconciler/rules.py packages/foreman/tests/reconciler/test_rules.py
git commit -m "feat(reconciler): spec-pr reviewer gate driven by gh reviewDecision"
```

---

## Task 7: Impl-PR transition rule + tighten `merge_impl_pr`

**Files:**
- Modify: `packages/foreman/src/foreman/reconciler/actions.py` (add `Action.ADVANCE_LABEL_TO_IMPL_APPROVED` + executor branch)
- Modify: `packages/foreman/src/foreman/reconciler/rules.py`
- Test: `packages/foreman/tests/reconciler/test_rules.py` + `test_actions.py`

**Why:** Move impl-review → impl-approved transition out of Reviewer subprocess and into a rule driven by GH `reviewDecision == "APPROVED"`. Tighten `merge_impl_pr` to consult `auto_merge_impl` flag.

- [ ] **Step 7.1: Add `ADVANCE_LABEL_TO_IMPL_APPROVED` action**

In `packages/foreman/src/foreman/reconciler/actions.py`:

```python
class Action(enum.Enum):
    ...
    ADVANCE_LABEL_TO_IMPL_APPROVED = "advance_label_to_impl_approved"
    ...
```

Add executor branch in `execute_action` (mirroring the existing `ADVANCE_LABEL_TO_PLAN_APPROVED` branch):

```python
        elif action is Action.ADVANCE_LABEL_TO_IMPL_APPROVED:
            host.remove_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:impl-review",
            )
            host.add_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:impl-approved",
            )
```

- [ ] **Step 7.2: Write failing rule tests**

Add to `packages/foreman/tests/reconciler/test_rules.py`:

```python
def test_impl_review_advances_to_impl_approved_on_gh_approve(make_ctx):
    ctx = make_ctx(
        labels=("foreman:impl-review",),
        pr_state={
            "mergeable": "MERGEABLE",
            "ci_status": "SUCCESS",
            "review_decision": "APPROVED",
        },
    )
    assert evaluate(ctx) == Action.ADVANCE_LABEL_TO_IMPL_APPROVED

def test_impl_review_no_advance_without_review_approve(make_ctx):
    ctx = make_ctx(
        labels=("foreman:impl-review",),
        pr_state={
            "mergeable": "MERGEABLE",
            "ci_status": "SUCCESS",
            "review_decision": None,
        },
    )
    assert evaluate(ctx) != Action.ADVANCE_LABEL_TO_IMPL_APPROVED

def test_merge_impl_pr_requires_flag(make_ctx):
    ctx = make_ctx(
        labels=("foreman:impl-approved",),
        pr_state={
            "mergeable": "MERGEABLE",
            "ci_status": "SUCCESS",
            "review_decision": "APPROVED",
        },
        auto_merge_impl=False,
    )
    assert evaluate(ctx) != Action.MERGE_IMPL_PR

def test_merge_impl_pr_fires_when_flag_on(make_ctx):
    ctx = make_ctx(
        labels=("foreman:impl-approved",),
        pr_state={
            "mergeable": "MERGEABLE",
            "ci_status": "SUCCESS",
            "review_decision": "APPROVED",
        },
        auto_merge_impl=True,
    )
    assert evaluate(ctx) == Action.MERGE_IMPL_PR
```

Run. Expected: FAIL.

- [ ] **Step 7.3: Add the rule + tighten `merge_impl_pr`**

In `packages/foreman/src/foreman/reconciler/rules.py`:

```python
def _impl_review_approved_on_gh(ctx: ActionContext) -> bool:
    return (
        "foreman:impl-review" in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.pr.is_merged
        and ctx.pr.review_decision == "APPROVED"
    )


def _impl_approved_pr_green_and_flag(ctx: ActionContext) -> bool:
    return (
        "foreman:impl-approved" in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.pr.is_merged
        and ctx.pr.mergeable == "MERGEABLE"
        and ctx.pr.ci_status == "SUCCESS"
        and ctx.auto_merge_impl
    )
```

Add the rule (place after `dispatch_reviewer`, before `merge_impl_pr`):

```python
Rule(
    name="advance_label_to_impl_approved",
    tier=PrecedenceTier.FORWARD_PROGRESS,
    precedence=145,
    when=_impl_review_approved_on_gh,
    then=Action.ADVANCE_LABEL_TO_IMPL_APPROVED,
),
```

Update `merge_impl_pr` to use `_impl_approved_pr_green_and_flag`.

Re-run. Expected: PASS.

- [ ] **Step 7.4: Commit**

```bash
cd e:/workspaces/ai/agents/foreman
git add packages/foreman/src/foreman/reconciler/actions.py packages/foreman/src/foreman/reconciler/rules.py packages/foreman/tests/reconciler/test_rules.py
git commit -m "feat(reconciler): impl-review→impl-approved via gh reviewDecision + auto_merge_impl flag"
```

---

## Task 8: Worker prompt — post addressing-summary on impl-fix re-runs

**Files:**
- Modify: `packages/foreman/prompts/worker_impl.md` (or whatever the impl-target worker prompt file is)

**Why:** Jeff's requirement: "the reviewer's and planner's comments being logged to the pr." Reviewer already does this via `gh pr review`. For the Worker on impl-fix re-runs, add an explicit step.

- [ ] **Step 8.1: Locate the Worker impl prompt**

```bash
cd e:/workspaces/ai/agents/foreman
ls packages/foreman/prompts/ 2>&1
```

Find the one targeting impl-fix runs (likely `worker.md` or `worker_impl.md`).

- [ ] **Step 8.2: Add instruction at the appropriate point in the prompt**

Insert a section near the top of the impl-fix branch of the prompt (or as a new step):

```markdown
## When re-running on `foreman:impl-fix`

Before pushing your fix commit, post a comment on the existing PR summarizing:

1. Which Reviewer comments you addressed (link to each)
2. What change you made for each
3. Anything you chose NOT to address and why

Use `gh pr comment <pr-url> --body "..."` to post the comment. This goes on the PR conversation so the Reviewer (or a human reviewer) can audit the response without re-reading the diff.
```

- [ ] **Step 8.3: Commit**

```bash
cd e:/workspaces/ai/agents/foreman
git add packages/foreman/prompts/  # adjust to the specific file
git commit -m "feat(prompts): worker posts addressing-summary on impl-fix re-runs"
```

---

## Task 9: Update `~/.foreman/config.toml` with new flags

**Files:**
- Modify: `~/.foreman/config.toml` (out-of-repo)

**Why:** Make the foreman project's `auto_merge_impl = false` explicit in the live config so the daemon picks it up on next start.

- [ ] **Step 9.1: Append `[global]` section + `auto_merge_impl = false` to `[projects.foreman]`**

Edit `~/.foreman/config.toml`. Add at the top of the file (after the header comment):

```toml
[global]
auto_merge_spec = true
auto_merge_impl = false
```

Find the `[projects.foreman]` block and add `auto_merge_impl = false` as a sibling of the existing `auto_merge_spec = true`.

- [ ] **Step 9.2: Verify config parses**

```bash
cd e:/workspaces/ai/agents/foreman
uv run python -c "from foreman.config import load_config; cfg = load_config(); print('spec:', cfg.auto_merge_spec); print('impl:', cfg.auto_merge_impl)"
```

Expected: `spec: True` `impl: False`.

(This task touches a file outside the repo — no commit. Note in the PR body that the live config was updated.)

---

## Task 10: Run full suite, push, update PR #111

**Files:** none (test + push)

- [ ] **Step 10.1: Full pytest run**

```bash
cd e:/workspaces/ai/agents/foreman
uv run pytest packages/foreman/tests -q 2>&1 | tail -5
```

Expected: all green, count ≥ baseline + ~15 new tests.

- [ ] **Step 10.2: Lint + typecheck**

```bash
cd e:/workspaces/ai/agents/foreman
uv run ruff check packages/foreman 2>&1 | tail -5
uv run mypy packages/foreman/src 2>&1 | tail -5
```

Both clean.

- [ ] **Step 10.3: Push to PR #111**

```bash
cd e:/workspaces/ai/agents/foreman
PAT=$(python C:/Users/jeffr/.wren/.claude/skills/creds-management/scripts/creds.py --being wren get github --keyring --password 2>/dev/null)
GH_TOKEN="$PAT" git push origin fix/v3-operational-gaps 2>&1 | tail -10
```

Pre-push hook runs full pytest. If it fails, INVESTIGATE — never `--no-verify`. Common cause: a test fixture that wasn't updated for the new `ActionContext` defaults.

- [ ] **Step 10.4: Update PR #111 body with new scope**

```bash
cd e:/workspaces/ai/agents/foreman
PAT=$(python C:/Users/jeffr/.wren/.claude/skills/creds-management/scripts/creds.py --being wren get github --keyring --password 2>/dev/null)
GH_TOKEN="$PAT" gh pr edit 111 --repo jeffrichley/foreman --body "$(cat <<'EOF'
[existing PR #111 body content — append the new scope]

## Update 2026-06-04: state-machine + auto-merge flags

Spec PR Reviewer flow:
- Observer fetches `PullRequest.reviewDecision`
- New `dispatch_reviewer_spec` rule fires on planning + spec PR + not-yet-reviewed
- New `advance_label_to_plan_approved` rule fires on planning + spec PR + `reviewDecision == APPROVED`

Impl PR Reviewer flow tightening:
- New `advance_label_to_impl_approved` rule fires on impl-review + impl PR + `reviewDecision == APPROVED` (was: Reviewer subprocess wrote the label internally)

Auto-merge config:
- Global `auto_merge_spec` (default `True`) + `auto_merge_impl` (default `False`)
- Per-project override via `auto_merge_spec` / `auto_merge_impl`
- Effective resolver: project value wins if set, else global default
- `merge_spec_pr` + `merge_impl_pr` rules consult the effective flag

Reviewer dispatch fix:
- Reviewer subprocess argv now uses positional `pr_url` per `foreman review` CLI shape

Worker prompt:
- Post addressing-summary PR comment on impl-fix re-runs

Live config: `~/.foreman/config.toml` updated explicitly with `auto_merge_impl = false` on the foreman project.

Tests: +N new (state, observer, config, action context, rule catalog).
EOF
)" 2>&1 | tail -3
```

Adjust the `[existing PR #111 body content — append the new scope]` line to actually preserve the existing body (fetch with `gh pr view 111 --json body` first if needed).

- [ ] **Step 10.5: Final verification**

Confirm CI is green on the PR, leave a comment summarizing the SDD run + test count delta.

---

## Self-Review Checklist (controller runs after all tasks)

1. **Spec coverage** — each of Jeff's 5 items is covered by at least one task. ✓
2. **No placeholders** — all code blocks are concrete; no TBD/TODO. ✓
3. **Type consistency** — `PRState.review_decision`, `ActionContext.auto_merge_spec/impl`, `Action.ADVANCE_LABEL_TO_IMPL_APPROVED` used consistently across tasks. ✓
4. **Test coverage** — every new rule has at least one positive + one negative test.
5. **Pre-push hook awareness** — every commit assumes the hook passes; any task that touches shared fixtures should check existing callers.
6. **Worktree safety** — all work on `fix/v3-operational-gaps`. No `main` mutations.
