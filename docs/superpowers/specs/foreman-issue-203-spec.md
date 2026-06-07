# Spec: delete unused GitHub merge-queue path (issue #203)

## Goal

Delete the dead GitHub-MergeQueue code path from V3GitHubHost +
surrounding config + executor plumbing. The cap-1 concurrency cap in
`V3GitHubHost` plus the `foreman:merging-plan` / `foreman:merging-impl`
state-machine labels (foreman#165) ARE Foreman's queue; GitHub's
built-in Merge Queue feature is wired but has no production path to
ever firing (the global default is `direct` and no project sets
`queue`). Leaving it in place risks accidental activation via a config
typo against an untested GraphQL mutation. See issue
[#203](https://github.com/jeffrichley/foreman/issues/203).

## Acceptance criteria

- `grep -rE 'merge_mechanism|MergeMechanism|_enqueue_pull_request|enqueuePullRequest' packages/foreman/src/foreman/` returns 0 results.
- `grep -rE 'merge_mechanism|MergeMechanism' packages/foreman/tests/` returns 0 results.
- `V3GitHubHost.merge_pr` has exactly ONE code path — synchronous
  `self._v2.merge_pull_request(...)` via `GitHubDaemonHost` — and no
  longer accepts a `mechanism` kwarg.
- `ReconcilerHost` Protocol's `merge_pr` no longer declares a
  `mechanism` parameter.
- `ActionContext` no longer carries a `merge_mechanism` field. Its
  remaining fields (`snapshot`, `issue`, `pr`, `log`, `auto_merge_spec`,
  `auto_merge_impl`) and their defaults are unchanged.
- `ReconcilerProject` no longer carries a `merge_mechanism` field.
- `ReconcilerConfig.merge_mechanism` and
  `ReconcilerConfig.effective_merge_mechanism` are removed from
  `foreman.config`. `MergeMechanism = Literal["direct", "queue"]` is
  removed from the same file. `ProjectConfig.merge_mechanism` is removed.
- `_handle_attempt_merge` in
  `packages/foreman/src/foreman/reconciler/actions.py` calls
  `host.merge_pr(owner=..., repo=..., pr_number=...)` (no `mechanism=`
  argument).
- `cli.py:_build_reconciler_projects` no longer threads
  `merge_mechanism=` into `ReconcilerProject(...)`.
- The `_QUEUE_HEALTHY_STATES`, `_QUEUE_NEEDS_HELP_STATES`, and
  `_ENQUEUE_PR_MUTATION` module-level constants in
  `packages/foreman/src/foreman/reconciler/v3_host.py` are removed.
- `_PR_NODE_ID_QUERY` STAYS (it's used by `update_branch`, which the
  issue's Out-of-scope rule says not to touch).
- The `gh_queue_client` constructor parameter on `V3GitHubHost.__init__`
  STAYS (see Approach §3 for the rationale; the issue's bullet asking
  to remove it conflicts with the foreman#165 state-machine paths
  that the issue's Out-of-scope rule says not to touch). Its docstring
  is updated to remove queue-only framing.
- `cli.py:_build_v3_gh_and_host` STILL constructs the
  orchestrator-bot-authenticated `HttpxGHGraphQLClient` and passes it
  as `gh_queue_client=gh_queue` (see Approach §3). The comment block
  that frames it as foreman#158-queue-specific is replaced with one
  that names its real purpose: powering `get_pr_mergeability` +
  `update_branch` for the merge state machine.
- All tests in `packages/foreman/tests/reconciler/test_v3_host.py`
  that target queue-mechanism behavior (today named with
  `merge_pr_queue*` and `merge_pr_direct*`) are deleted or rewritten
  to assert direct-merge-only semantics (see Sub-request 8 for
  exact list). The four tests at lines 280-494 covering
  `get_pr_mergeability` and `update_branch` STAY unchanged except for
  removing any `gh_queue_client` named-kwarg usage IF the constructor
  signature changes (it does NOT — see above), which means they
  require no edits.
- The four `merge_mechanism` tests in
  `packages/foreman/tests/test_config.py` (lines 686-731) are deleted.
- The `_FakeHost.merge_pr` signature in
  `packages/foreman/tests/reconciler/test_actions.py` (today line
  139-152) drops the `mechanism: str` kwarg and its corresponding
  `"mechanism": mechanism` entry in the recorded call dict.
- The assertion `assert merge_call[1]["mechanism"] == "direct"` in
  `test_attempt_merge_plan_clean_calls_host_merge_pr` (line 610) is
  removed. The other assertions in that test (owner, repo, pr_number)
  stay.
- Stub `merge_pr` signatures in
  `test_actions.py` (e.g., line 353 `def merge_pr(self, **k: Any) -> None: ...`)
  are not affected since they take `**kwargs`.
- The `__init__.py` files in `packages/foreman/src/foreman/` and
  `packages/foreman/src/foreman/reconciler/` are not re-exporting
  `MergeMechanism`. Confirmed by grep before/after; no changes needed
  unless the search finds otherwise.
- `just check` exits 0 (lint + typecheck + tests).
- Net deletion in `v3_host.py` is on the order of 100 lines (the
  issue's claim of ~80 lines is approximate — the actual delta
  depends on docstring shrinkage; the criterion is "the file got
  meaningfully smaller", not a specific count).

## Approach

The cleanup is mostly mechanical removal — delete the constant,
delete the field, delete the kwarg, delete the test — but four
non-obvious decisions need to land correctly.

**1. The merge state machine stays untouched.** The issue's
Out-of-scope rule is emphatic: do not touch the `foreman:merging-*`
labels, the `ADVANCE_LABEL_TO_MERGING_*` actions, the
`ATTEMPT_MERGE_*` actions, or `_handle_attempt_merge`'s control
flow. The only edit to `_handle_attempt_merge` is at one line — drop
the `mechanism=ctx.merge_mechanism` kwarg from the `host.merge_pr`
call. The rest of the function body (BEHIND→update_branch,
BLOCKED→needs-help, UNSTABLE/DIRTY→needs-help, UNKNOWN→noop, etc.)
is unchanged.

**2. `merge_pr` shrinks to one-liner delegation.** Today's
`V3GitHubHost.merge_pr` branches on `mechanism`. After this PR it
becomes a thin wrapper:

```python
def merge_pr(self, *, owner: str, repo: str, pr_number: int) -> None:
    """Merge a PR via the v2 REST path (synchronous pr.merge())."""
    self._v2.merge_pull_request(f"{owner}/{repo}", pr_number)
```

The Protocol in `host.py` mirrors this — no `mechanism` parameter.

**3. The `gh_queue_client` parameter STAYS — the issue's bullet on
this point is wrong (see Open Questions).** The issue's "Things to
delete" list calls out:

> The `gh_queue_client` constructor parameter on `V3GitHubHost.__init__`
> (and the attribute storage). Direct merges don't use it.

But `self._gh_queue_client` is also used by `get_pr_mergeability`
(v3_host.py:594-629) and `update_branch` (v3_host.py:682-714), both
of which power the merge state machine introduced by foreman#165 —
which the issue's Out-of-scope rule explicitly says not to touch.
Removing the parameter would break the state machine. The
correct cleanup is: keep the parameter and the attribute storage
intact; only remove the parts of its DOCSTRING that frame it as
queue-only (today v3_host.py:408-419). The Out-of-scope rule "do
not rename anything" prevents us from renaming it to something more
accurate (e.g., `gh_graphql_client`); a future cleanup issue can
do the rename when there's not an active no-rename constraint.

Symmetric reasoning applies to `_build_v3_gh_and_host` in cli.py:
the orchestrator-bot-authenticated `HttpxGHGraphQLClient` STILL
needs to exist because the state machine reads through it. The
comment block at cli.py:810-815 that frames it as queue-specific
gets edited to name its actual current job.

**4. `_PR_NODE_ID_QUERY` stays.** That module-level GraphQL query
constant is used by both `_enqueue_pull_request` (going away) AND
`update_branch` (staying). The query itself does not become dead.

The order of operations is bottom-up: drop the `mechanism` parameter
from the leaf (`V3GitHubHost.merge_pr`) and the protocol, then drop
it from the caller (`_handle_attempt_merge`), then drop it from the
context (`ActionContext`), then drop the field plumbing
(`ReconcilerProject`, `_build_reconciler_projects`), and finally
drop the config layer (`ReconcilerConfig`, `ProjectConfig`,
`MergeMechanism`). This order keeps each commit individually
type-checking if the Worker chooses to break the change into commits
(strongly recommended for review legibility, though not required).

The tests are split per Sub-request 8 below: a small number of
tests must be DELETED outright (they exclusively exercise the queue
path), one test needs an assertion REMOVED, the `_FakeHost`
signature in test_actions.py needs the `mechanism` kwarg DROPPED,
and the four config tests in test_config.py go away.

## Sub-requests (topologically sorted)

1. In `packages/foreman/src/foreman/reconciler/v3_host.py`:
   - Delete the `_ENQUEUE_PR_MUTATION` constant (today lines
     118-129).
   - Delete the `_QUEUE_HEALTHY_STATES` and `_QUEUE_NEEDS_HELP_STATES`
     constants (today lines 202-203).
   - Delete the entire `_enqueue_pull_request` method
     (today lines 454-581) — including its docstring.
   - Replace the body of `merge_pr` (today lines 430-452) with a
     single line: `self._v2.merge_pull_request(f"{owner}/{repo}", pr_number)`.
     Drop the `mechanism: MergeMechanism` parameter from its signature.
     Remove the parameter's mention from the method's docstring;
     replace the docstring with a one-line statement explaining
     this calls the v2 REST merge path.
   - In `V3GitHubHost.__init__` (today lines 379-419), KEEP the
     `gh_queue_client` parameter and the `self._gh_queue_client =
     gh_queue_client` assignment. Replace the docstring block at
     lines 400-419 (the foreman#158 framing) with a comment that
     names the parameter's real role: powering
     `get_pr_mergeability` + `update_branch` for the merge state
     machine; rename is deferred to a follow-up issue.
   - Remove the `from foreman.config import MergeMechanism` import
     at line 33.
   - Keep `_PR_NODE_ID_QUERY` (lines 110-116), `_PR_MERGEABILITY_QUERY`
     (lines 141-171), and `_UPDATE_BRANCH_MUTATION` (lines 173-179)
     untouched — all three are still used by the state-machine paths.
   - Keep `_CHECK_FAILING_CONCLUSIONS`, `_STATUS_FAILING_STATES`,
     `_STATUS_PENDING_STATES`, and the `_GraphQLClientLike` Protocol
     untouched.

2. In `packages/foreman/src/foreman/reconciler/host.py`:
   - Drop `mechanism: MergeMechanism` from the `merge_pr` Protocol
     method (today lines 49-74). Replace the method docstring with
     a one-line description: this method merges a PR via the host's
     direct-merge path.
   - Remove the `from foreman.config import MergeMechanism` import
     at line 12.

3. In `packages/foreman/src/foreman/reconciler/actions.py`:
   - Remove the `merge_mechanism: MergeMechanism = "direct"` field
     from `ActionContext` (today line 99) and its corresponding
     docstring paragraph (today lines 85-90).
   - In `_handle_attempt_merge` (today lines 125-246), drop the
     `mechanism=ctx.merge_mechanism` kwarg from the `host.merge_pr`
     call at line 173 (the call becomes
     `host.merge_pr(owner=ctx.snapshot.owner, repo=ctx.snapshot.repo,
     pr_number=ctx.pr.number)`). Also update the docstring's
     state-table entry for `CLEAN` (today lines 141-142) to drop
     the "configured `merge_mechanism`" framing.
   - Remove the `from foreman.config import MergeMechanism` import
     at line 13.

4. In `packages/foreman/src/foreman/reconciler/daemon.py`:
   - Remove the `merge_mechanism: MergeMechanism = "direct"` field
     from `ReconcilerProject` (today line 117) and its docstring
     paragraph (today lines 107-109).
   - In `_reconcile_project` (today lines 382-411), drop the
     `merge_mechanism=project.merge_mechanism` kwarg from the
     `ActionContext(...)` construction at line 397.
   - Remove the `from foreman.config import MergeMechanism` import
     at line 17.

5. In `packages/foreman/src/foreman/config.py`:
   - Remove the `MergeMechanism = Literal["direct", "queue"]` type
     alias (today lines 23-26) and its surrounding comment.
   - Remove the `merge_mechanism: MergeMechanism = Field(default="direct", ...)`
     field on `ReconcilerConfig` (today lines 210-232).
   - Remove the `effective_merge_mechanism` method on
     `ReconcilerConfig` (today lines 254-268).
   - Remove the `merge_mechanism: MergeMechanism | None = Field(default=None, ...)`
     field on `ProjectConfig` (today lines 418-434).
   - Remove the `from typing import Literal` import at line 19 ONLY
     if no other use of `Literal` remains in the file (grep first).

6. In `packages/foreman/src/foreman/cli.py`:
   - In `_build_reconciler_projects` (today lines 470-503), drop the
     `merge_mechanism=config.reconciler.effective_merge_mechanism(proj_cfg)`
     line at line 500 from the `ReconcilerProject(...)` construction.
     Leave every other resolved field (auto_merge_spec, auto_merge_impl,
     name, owner, repo) intact.
   - In `_build_v3_gh_and_host` (today lines 771-833), KEEP both the
     `gh = HttpxGHGraphQLClient(token_supplier=...)` construction
     (observer) AND the `gh_queue = HttpxGHGraphQLClient(token_supplier=...)`
     construction (orchestrator-bot-authenticated). KEEP the
     `gh_queue_client=gh_queue` kwarg in the `V3GitHubHost(...)`
     call. Replace the comment block at lines 809-815 (foreman#158
     queue framing) with: "Orchestrator-bot-authenticated GraphQL
     client used by V3GitHubHost's get_pr_mergeability +
     update_branch methods (the merge state machine paths from
     foreman#165). The parameter name `gh_queue_client` is a
     historical artifact of foreman#158; rename is deferred to a
     follow-up issue."

7. In `packages/foreman/__init__.py` and
   `packages/foreman/reconciler/__init__.py`:
   - Grep both files for `MergeMechanism`. If found, remove those
     imports and `__all__` entries. (Investigation found no such
     re-exports today; sub-request is defensive.)

8. In `packages/foreman/tests/reconciler/test_v3_host.py`:
   - DELETE the following six tests in their entirety (they exist
     only to exercise the queue path):
     - `test_merge_pr_queue_mechanism_uses_graphql_mutation` (today
       lines 130-167)
     - `test_merge_pr_queue_treats_healthy_states_as_success` (today
       lines 170-195)
     - `test_merge_pr_queue_surfaces_needs_help_states` (today
       lines 198-222)
     - `test_merge_pr_queue_propagates_graphql_errors` (today
       lines 225-260)
     - `test_merge_pr_queue_without_client_raises_clear_error` (today
       lines 263-277)
   - In `test_merge_pr_direct_mechanism_delegates_to_v2_host` (today
     lines 75-88): rename to `test_merge_pr_delegates_to_v2_host`,
     drop the `mechanism="direct"` kwarg from the `host.merge_pr`
     call, and update the docstring to drop the
     "today's behavior preserved" framing (replace with: "merge_pr
     synchronously delegates to the v2 REST merge path").
   - In `_build_queue_responses` (today lines 111-127): DELETE the
     helper entirely — nothing else uses it after the queue tests
     are gone.
   - DELETE the `_FakeGraphQLClient` class (today lines 91-108) IF
     it's only used by the deleted queue tests. Re-grep — the
     state-machine tests at lines 280-494 also use it; if so, KEEP
     it. (Investigation showed it's used by both — keep it.)
   - The four state-machine tests
     (`test_get_pr_mergeability_returns_state_from_graphql`,
     `test_get_pr_mergeability_computes_check_counts_from_rollup`,
     `test_update_branch_issues_graphql_mutation`,
     `test_get_pr_mergeability_without_client_raises_clear_error`,
     `test_update_branch_without_client_raises_clear_error`) are
     unchanged.

9. In `packages/foreman/tests/reconciler/test_actions.py`:
   - In `_FakeHost.merge_pr` (today lines 139-152): drop the
     `mechanism: str` parameter and the `"mechanism": mechanism`
     entry in the recorded call dict.
   - In `test_attempt_merge_plan_clean_calls_host_merge_pr` (today
     lines 583-610): delete line 610 (`assert merge_call[1]["mechanism"] == "direct"`)
     and remove the "with the configured merge_mechanism" phrase
     from the docstring. The other assertions stay.
   - Grep for any other test that constructs `ActionContext` with
     `merge_mechanism=...` — investigation found none, but if any
     surface during the change, drop the kwarg.

10. In `packages/foreman/tests/test_config.py`:
    - DELETE the four merge-mechanism tests (today lines 686-731):
      `test_merge_mechanism_defaults_to_direct`,
      `test_merge_mechanism_inherits_global_when_project_unset`,
      `test_merge_mechanism_per_project_override_wins`,
      `test_merge_mechanism_rejects_unknown_value`.
    - Re-grep `test_config.py` for any remaining `merge_mechanism` /
      `MergeMechanism` usage; remove if found.

11. Run the verification gates the issue requires (under
    `## Verification` below). Run `just check` and confirm it exits
    0. If a previously-overlooked test still references a deleted
    symbol, fix it minimally (no new test scaffolding) and re-run.

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/src/foreman/reconciler/v3_host.py` | Delete `_enqueue_pull_request`, `_ENQUEUE_PR_MUTATION`, `_QUEUE_HEALTHY_STATES`, `_QUEUE_NEEDS_HELP_STATES`. Shrink `merge_pr` to one-line v2 delegation; drop its `mechanism` kwarg. Keep `gh_queue_client` parameter (state machine needs it); update its docstring. Drop the `MergeMechanism` import. |
| `packages/foreman/src/foreman/reconciler/host.py` | Drop `mechanism` kwarg from the Protocol's `merge_pr`. Drop the `MergeMechanism` import. |
| `packages/foreman/src/foreman/reconciler/actions.py` | Drop `merge_mechanism` field from `ActionContext`. Drop `mechanism=` kwarg from the `host.merge_pr` call in `_handle_attempt_merge`. Update docstrings. Drop the `MergeMechanism` import. |
| `packages/foreman/src/foreman/reconciler/daemon.py` | Drop `merge_mechanism` field from `ReconcilerProject`. Drop the kwarg from the `ActionContext(...)` construction in `_reconcile_project`. Drop the `MergeMechanism` import. |
| `packages/foreman/src/foreman/config.py` | Delete `MergeMechanism` type alias, `ReconcilerConfig.merge_mechanism`, `ReconcilerConfig.effective_merge_mechanism`, `ProjectConfig.merge_mechanism`. Possibly drop `from typing import Literal` if nothing else needs it. |
| `packages/foreman/src/foreman/cli.py` | In `_build_reconciler_projects`, drop the `merge_mechanism=` kwarg from `ReconcilerProject(...)`. In `_build_v3_gh_and_host`, keep both GraphQL client constructions and the `gh_queue_client=` pass-through; replace the queue-framing comment with one naming the client's state-machine role. |
| `packages/foreman/tests/reconciler/test_v3_host.py` | Delete five queue-mechanism tests + `_build_queue_responses` helper. Rename + simplify `test_merge_pr_direct_mechanism_delegates_to_v2_host` to drop the `mechanism=` kwarg. Keep all state-machine tests + `_FakeGraphQLClient` (still used). |
| `packages/foreman/tests/reconciler/test_actions.py` | Drop `mechanism: str` from `_FakeHost.merge_pr` signature + the recorded-call dict. In `test_attempt_merge_plan_clean_calls_host_merge_pr`, delete the `mechanism == "direct"` assertion and update the docstring. |
| `packages/foreman/tests/test_config.py` | Delete the four `merge_mechanism` tests (lines 686-731). |

No expected changes to (sanity-checked via grep):
- `packages/foreman/src/foreman/__init__.py`
- `packages/foreman/src/foreman/reconciler/__init__.py` (no
  `MergeMechanism` re-export today)
- Any v2 daemon path (`daemon.py`, `daemon_host.py`,
  `daemon_runners.py`) — pre-v3 paths are independently scheduled
  for removal and not touched here.
- The merge state machine modules (`rules.py`, all
  `ATTEMPT_MERGE_*` / `ADVANCE_LABEL_TO_MERGING_*` rule predicates).
- Any prompt or role file (`packages/foreman/src/foreman/prompts/`,
  `packages/foreman/src/foreman/roles/`).
- `docs/architecture/v3-reconciler.md` — that file does not exist
  in the repo today (the issue's "Related" link is aspirational;
  see Open questions).

## Verification

Before opening the impl PR, the Worker MUST run AND record the
output of these commands in the PR body so the Reviewer can
cross-check:

1. `grep -rE 'merge_mechanism|MergeMechanism|_enqueue_pull_request|enqueuePullRequest' packages/foreman/src/foreman/`
   — expected: 0 matches.
2. `grep -rE 'merge_mechanism|MergeMechanism' packages/foreman/tests/`
   — expected: 0 matches.
3. `grep -rE 'gh_queue_client|enqueuePullRequest' packages/foreman/src/foreman/`
   — expected: matches limited to `gh_queue_client` references in
   `v3_host.py` (parameter + attribute storage + state-machine
   error messages) and in `cli.py` (the kwarg). The
   `enqueuePullRequest` substring must be 0. (This deviates from the
   issue body's verification command, which expected
   `gh_queue_client` to also be 0 — but per Approach §3 and the
   Open Questions below, the parameter survives.)
4. `just check` — exit code 0. Capture the tail of pytest output
   showing test count + pass/fail summary.
5. Smoke-test that the merge state machine still works: any
   existing test that exercises `_handle_attempt_merge` (e.g.,
   `test_attempt_merge_plan_clean_calls_host_merge_pr`,
   `test_attempt_merge_plan_behind_calls_host_update_branch_and_does_not_merge`)
   must pass unchanged. `just check` covers this; the assertion
   here is that no merge-state-machine test required modification
   beyond the `mechanism` kwarg drop in sub-request 9.
6. Line count check on `v3_host.py`: `wc -l packages/foreman/src/foreman/reconciler/v3_host.py`
   should report substantially fewer lines than today's 963
   (~100-line reduction expected; exact number is not a gate).

## Alternatives considered

- **Follow the issue body literally and remove `gh_queue_client`
  from `V3GitHubHost.__init__` + `_build_v3_gh_and_host`.**
  Rejected — would break `get_pr_mergeability` and `update_branch`,
  which the merge state machine (foreman#165) depends on, and which
  the issue's Out-of-scope rule explicitly forbids touching. The
  issue's author appears to have written that bullet from a mental
  model of `v3_host.py` predating foreman#165's addition of those
  methods. The right call is to apply the spirit of the cleanup
  (remove the queue functionality) without breaking the state
  machine the issue says to preserve.

- **Rename `gh_queue_client` to `gh_graphql_client` while we're in
  the file.** Rejected — the issue's Out-of-scope rule says "do not
  rename anything (no opportunistic renames of helpers, no 'clarify
  while we're here' changes)." A rename would be cleaner long-term
  and the Approach section documents the inaccuracy that survives;
  a follow-up issue can do the rename without the no-rename
  constraint.

- **Land the cleanup in two PRs (config layer first, then host +
  tests).** Rejected — the change is small enough that splitting
  it leaves an intermediate state where `ReconcilerProject` has no
  `merge_mechanism` but `ActionContext` still expects one, which
  is harder to type-check than the all-at-once landing. The
  bottom-up order in Sub-requests 1-10 means each individual
  edit type-checks if the Worker commits incrementally.

- **Build a "merge mechanism plugin" interface as future-proofing.**
  Rejected per the issue body's explicit Out-of-scope rule and
  general YAGNI: if Foreman ever needs queues again, it will be
  rebuilt knowing what we know then. Carrying a plugin shell with
  one implementation costs ongoing maintenance for no current
  value.

- **Keep the `MergeMechanism` Literal and drop only the queue
  branch.** Rejected — once nothing else uses the type alias, it
  becomes dead code that suggests-by-its-existence that queue
  support might come back. Cleaner to delete entirely; the issue's
  acceptance criteria call for "all references to ... `MergeMechanism`
  ... are gone from `packages/foreman/src/foreman/`."

## Open questions

- **The issue's `gh_queue_client` removal bullet conflicts with
  foreman#165.** The issue body's "Things to delete" list for
  `v3_host.py` says to remove "The `gh_queue_client` constructor
  parameter on `V3GitHubHost.__init__` (and the attribute storage)."
  But that same attribute is read by `get_pr_mergeability`
  (v3_host.py:594-629) and `update_branch` (v3_host.py:682-714) —
  both methods that the issue's Out-of-scope rule explicitly says
  not to touch. The two instructions cannot both be satisfied; this
  spec keeps the parameter (Approach §3) and flags the conflict
  for Reviewer attention. If the Reviewer disagrees and prefers
  removing the parameter, the state-machine paths must be re-wired
  to share the observer's `gh` client OR receive a separate
  GraphQL-client kwarg — both of which expand scope beyond what
  the issue actually asks for. Confidence: low on this point
  specifically; the rest of the cleanup is mechanical.

- **The "Related" section references `docs/architecture/v3-reconciler.md`
  §7 item 3.** That file does not exist in this repo today (no
  `docs/architecture/` directory). The closure of the drift entry
  cannot land here. Recommend: the Worker documents this in the
  impl PR body so whoever later authors `v3-reconciler.md` has the
  audit trail. (Issue #198's spec hit the identical missing-doc
  situation and resolved it the same way; this spec follows that
  precedent.)

- **The issue's "v2 daemon code" exclusion overlaps with shared
  modules.** `foreman.config`, `foreman.cli`, and the
  `__init__.py` files are used by both v2 and v3. The spec's edits
  to `config.py` (deleting `MergeMechanism` + the field on
  `ReconcilerConfig`/`ProjectConfig`) and `cli.py` (the
  `_build_reconciler_projects` edit) ARE necessary for the cleanup
  and do NOT touch v2-only logic — `merge_mechanism` was only ever
  consumed by v3. If the Worker discovers a v2-only code path that
  reads `merge_mechanism` (investigation found none), they should
  flag rather than guess; that would mean the cleanup is larger
  than the issue scopes.

## Out of scope

- **The merge state machine itself** — `foreman:merging-plan` /
  `foreman:merging-impl` labels, `ADVANCE_LABEL_TO_MERGING_*` and
  `ATTEMPT_MERGE_*` actions, all `_handle_attempt_merge` control
  flow (BEHIND→update_branch, BLOCKED→needs-help, etc.), and the
  `get_pr_mergeability` + `update_branch` methods on V3GitHubHost.
  The ONLY edit to `_handle_attempt_merge` is dropping the
  `mechanism=` kwarg from its `host.merge_pr` call.
- **Renaming `gh_queue_client`** — even though its name becomes
  inaccurate after this PR, renames are explicitly forbidden by
  the issue's Out-of-scope rule. A follow-up issue can do the
  rename.
- **The `auto_merge_spec` / `auto_merge_impl` config flags** — those
  control WHETHER the daemon merges; this PR is about HOW.
  Unchanged here.
- **`merge_pr`'s positional/keyword shape beyond removing
  `mechanism`** — owner, repo, pr_number stay exactly as they are.
- **Refactoring helpers that survive the cleanup** — no "clarify
  while we're here" edits to `_PR_NODE_ID_QUERY`, `_PR_MERGEABILITY_QUERY`,
  `_UPDATE_BRANCH_MUTATION`, or any other constants that remain
  in `v3_host.py`.
- **v2 daemon code** — `daemon.py`, `daemon_host.py`,
  `daemon_runners.py` are scheduled for separate removal and not
  touched by this PR.
- **The observer's GraphQL client** (`gh = HttpxGHGraphQLClient(token_supplier=lambda: registry.get_token("planner"))`)
  — stays unchanged.
- **Any prompt or role file** — this is a reconciler-internal
  cleanup; no role behavior changes.
- **Adding a future-proof "merge mechanism plugin" interface** —
  YAGNI per issue body.
- **Creating or editing `docs/architecture/v3-reconciler.md`** —
  the doc does not exist; see Open questions.
