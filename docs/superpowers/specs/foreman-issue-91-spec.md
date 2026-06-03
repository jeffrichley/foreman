# Spec: replace post-mutation label re-read with role-authoritative labels (issue #91)

## Goal

Stop the daemon worker from re-dispatching a role on a stale label
snapshot immediately after the prior dispatch already transitioned the
issue. Today the post-role label set the worker re-enqueues with comes
from a fresh GET against GitHub
(`DaemonRunners._read_labels` →
`GitHubDaemonHost.get_issue_labels`,
`packages/foreman/src/foreman/daemon_runners.py:115-117` and
`packages/foreman/src/foreman/daemon_host.py:81-85`). That GET happens
milliseconds after the role wrote the new labels via its own PyGithub
client, and GitHub's label read-after-write is not guaranteed
consistent — especially when the read uses the orchestrator client
while the write used the role's bot client. The stale read is then
written back to the queue; the next iteration dispatches the same role
again; the role's precondition gate
(`packages/foreman/src/foreman/roles/reviewer.py:368-374`,
`packages/foreman/src/foreman/roles/fixer.py:445-452`) catches the
mismatch ~3s later and raises with
`Issue #N does not carry the 'X' label`. The fix is to stop using GitHub
as the source of truth for "what labels did the role just put on the
issue" and instead have each role return its post-transition label set
as part of its existing `*RunResult` wrapper. The role knows what it
wrote; trust that signal.

Tracks issue [#91](https://github.com/jeffrichley/foreman/issues/91).
Related: foreman#79 (Fixer's precondition mismatch) produces the same
error-message shape from a different cause, so this spec fixes the
race-condition source without changing the message shape — the
`using-foreman-debug` follow-up of distinguishing the two log
signatures is explicitly out of scope here per the issue body.

## Acceptance criteria

- `packages/foreman/src/foreman/schemas/reviewer.py` gains a new
  `ReviewerRunResult` Pydantic model with two fields:
  `llm_output: ReviewerOutput` and
  `final_labels: list[str]` (sorted; same shape as the `to_labels`
  audit-log column the worker already persists). The class follows
  the docstring + field-description conventions used by
  `PlannerRunResult` (`packages/foreman/src/foreman/schemas/planner.py:57-67`),
  `FixerRunResult`, and `WorkerRunResult`. `arbitrary_types_allowed`
  is NOT needed (both fields are pydantic-native).
- `packages/foreman/src/foreman/schemas/fixer.py:172-192` (`FixerRunResult`),
  `packages/foreman/src/foreman/schemas/worker.py:315-352`
  (`WorkerRunResult`), and
  `packages/foreman/src/foreman/schemas/planner.py:57-67`
  (`PlannerRunResult`) each gain a new
  `final_labels: list[str]` field with the same description shape
  ("Sorted list of foreman labels on the originating issue after the
  role's transitions ran — the authoritative post-run label set,
  computed in-process from the role's known mutations, not via a
  post-mutation host re-read.").
- `packages/foreman/src/foreman/roles/reviewer.py` returns a
  `ReviewerRunResult` (not bare `ReviewerOutput`). The role function
  computes `final_labels` from the in-memory pre-dispatch label set
  (`issue_labels` at line 367) minus `in_review_label` plus
  `add_label` (per the existing transition at lines 440-441), then
  sorts the result. The return-type annotation at line 308 changes
  from `ReviewerOutput` to `ReviewerRunResult`.
- `packages/foreman/src/foreman/roles/planner.py:228`,
  `packages/foreman/src/foreman/roles/fixer.py` (the
  `return FixerRunResult(...)` call sites — there are several, one
  per outcome branch at lines ~544-562), and
  `packages/foreman/src/foreman/roles/worker.py` (the
  `WorkerRunResult` construction near line 750-ish — verify the
  exact line during implementation) each compute and pass
  `final_labels` as a sorted list. Each call site computes its
  final set as `(initial_issue_labels - removed_labels) |
  added_labels` from variables already in local scope; no extra
  GitHub API calls are introduced.
- `packages/foreman/src/foreman/daemon_runners.py` is updated:
  - `DaemonRunners.run_planner`, `run_reviewer`, `run_fixer`,
    `run_worker` (lines 119-188) replace
    `new_labels=self._read_labels(ticket, config)` with
    `new_labels=frozenset(result.final_labels)` where
    `result` is the role-returned `*RunResult`.
  - `DaemonRunners.merge_spec_pr` and `merge_impl_pr` (lines 190-255)
    compute `new_labels` deterministically from
    `ticket.labels` plus the merge's own transitions
    (`merge_spec_pr`: drop `foreman:spec-ready`, add
    `foreman:implementing-ready`; `merge_impl_pr`: drop
    `foreman:ready-for-merge`, no add — issue is closed). The
    `_read_labels` helper at lines 115-117 stays in the file (it is
    used as a fallback by the per-role-not-implemented path in the
    backward-compat branch described in sub-request 7, and removed
    in sub-request 8 once the migration is complete) but is no
    longer called from the six `run_*` / `merge_*` methods after
    sub-request 8.
- A new test
  `test_run_reviewer_uses_role_final_labels_not_host_reread`
  in `packages/foreman/tests/test_daemon_runners.py` stubs
  `host.get_issue_labels` to return a STALE label set (e.g.,
  `["foreman:impl-review"]`) and stubs the injected reviewer role
  (`_reviewer=...`) to return a `ReviewerRunResult` whose
  `final_labels` is the FRESH set (e.g.,
  `["foreman:ready-for-merge"]`). Assert that
  `runners.run_reviewer(ticket=..., config=..., target="impl_pr")`
  returns a `RoleRunResult` with
  `new_labels == frozenset({"foreman:ready-for-merge"})` AND that
  `host.get_issue_labels` was NOT called during the run
  (`host.get_issue_labels.assert_not_called()`). This locks in the
  authoritative-source contract.
- A parallel test
  `test_merge_spec_pr_uses_deterministic_labels_not_host_reread`
  exercises the merge path: stub `host.get_issue_labels` to return
  a stale set, call `runners.merge_spec_pr(ticket=Ticket(...,
  labels=frozenset({"foreman:spec-ready"}), ...), config=...)`,
  assert the returned `RoleRunResult.new_labels` is
  `frozenset({"foreman:implementing-ready"})` AND
  `host.get_issue_labels.assert_not_called()`. Add the symmetric
  test for `merge_impl_pr` (input labels
  `{"foreman:ready-for-merge"}`, expected output
  `frozenset()` since the issue is closed and the label is removed).
- An integration-shaped test
  `test_run_one_iteration_does_not_re_dispatch_on_stale_labels` in
  `packages/foreman/tests/test_worker.py` exercises a full
  role-success → re-enqueue → next-action sequence to lock the
  contract end-to-end at the worker layer. Setup: enqueue a ticket
  with `labels=frozenset({"foreman:impl-review"})`; inject a
  `_FakeRoleDispatcher` whose `dispatch` returns a `RoleResult`
  with `new_labels=frozenset({"foreman:ready-for-merge"})`; run
  `run_one_iteration` twice; assert the second iteration's
  `dispatcher.calls[1]` action kind is `MERGE_IMPL_PR` (or, if
  `auto_merge_impl=False` in the test project config, that the
  second iteration found no action and returned True without
  dispatching). Assert there is NO call whose action kind is
  `RUN_REVIEWER_IMPL` on the second iteration.
- The existing test
  `test_run_planner_calls_role_with_issue_url` in
  `packages/foreman/tests/test_daemon_runners.py:60-77` (which
  currently asserts `"foreman:spec-review" in result.new_labels`
  via the `host.get_issue_labels` stub) is updated. The new
  shape: the mocked planner role returns a `PlannerRunResult` with
  `final_labels=["foreman:spec-review"]`, and the assertion stays
  `"foreman:spec-review" in result.new_labels`. The point of the
  test (planner is called with the right URL) is preserved; only
  the source of the label data shifts from host stub to role mock.
- The existing tests at
  `packages/foreman/tests/test_daemon_runners.py` lines 175, 197,
  236, 268, 294 that set `host.get_issue_labels.return_value =
  [...]` are migrated to set the role mock's return value's
  `final_labels` instead. The `host.get_issue_labels` stubbing is
  removed from those tests (it becomes dead test setup). The
  fixture default at line 43
  (`m.get_issue_labels = MagicMock(return_value=[...])`) is
  removed.
- `packages/foreman/tests/test_roles_reviewer.py` is updated for
  the return-type change. Existing tests that assert the role
  returns a `ReviewerOutput` are updated to either:
  - access `.llm_output` on the new `ReviewerRunResult`, or
  - construct the expected `ReviewerRunResult` and compare.
  Add one new test
  `test_run_reviewer_returns_authoritative_final_labels` that
  exercises the clean and needs_fix branches and asserts
  `result.final_labels` is the sorted list of
  `(initial_labels - {in_review_label}) | {add_label}` for each
  branch.
- `packages/foreman/tests/test_roles_planner.py`,
  `packages/foreman/tests/test_roles_fixer.py`,
  `packages/foreman/tests/test_roles_worker.py` each gain a
  `..._returns_authoritative_final_labels` test that asserts
  `final_labels` matches the deterministic transition for the
  outcome path the test exercises. Existing assertions on
  `llm_output` fields are left intact.
- `packages/foreman/tests/test_schemas_reviewer.py` adds a
  construction-shape test for the new `ReviewerRunResult` model
  matching the pattern in `test_schemas_planner.py` for
  `PlannerRunResult`.
- The CLI surface
  (`packages/foreman/src/foreman/cli.py` — wherever the CLI consumes
  `run_reviewer`'s return for human display) is updated to read
  `.llm_output.outcome` etc. through the new wrapper. Search for
  call sites with `grep -rn "run_reviewer\(" packages/foreman/src`
  and update each to dereference `.llm_output`. Symmetric to how
  the CLI already handles `PlannerRunResult` / `WorkerRunResult` /
  `FixerRunResult`.
- `just check` exits zero (lint + typecheck + tests). The daemon's
  end-to-end test
  (`packages/foreman/tests/test_daemon_e2e.py`) continues to pass —
  if it stubs `host.get_issue_labels` anywhere as the source of
  post-role labels, those stubs are migrated to role-result mocks
  too.

## Approach

The bug has one mechanical cause:
`DaemonRunners._read_labels` (lines 115-117) makes a fresh
`get_issue_labels` GET against GitHub right after the role wrote
the new labels. The orchestrator client used for the GET is a
different identity from the role's bot client used for the
`issue.remove_from_labels` / `issue.add_to_labels` writes (see
`packages/foreman/src/foreman/roles/reviewer.py:347` for the
reviewer-bot client vs.
`packages/foreman/src/foreman/daemon_host.py:82` for the
orchestrator client). GitHub's read-after-write for labels via
different App installations is not guaranteed atomic. The GET can
return the OLD labels even though the writes returned 200.

The fix replaces "ask GitHub what the labels are now" with "have
the role tell us what it set". The role's transition is
deterministic in local memory:
- pre-mutation label set (`issue_labels` in the role function,
  before `remove_from_labels` / `add_to_labels` calls),
- minus the labels the role removed,
- plus the labels the role added.

Every role already has all three pieces in scope at the point it
makes its mutation. We just need to capture the result and bubble
it up.

The capture happens via the `*RunResult` wrappers each role
already returns (`PlannerRunResult`, `FixerRunResult`,
`WorkerRunResult`). We extend those with a `final_labels: list[str]`
field and create `ReviewerRunResult` (Reviewer currently returns a
bare `ReviewerOutput`; the missing wrapper is why this bug bit the
Reviewer path most visibly). The roles compute `final_labels`
in-process; `DaemonRunners.run_*` reads `result.final_labels`
instead of calling `_read_labels`.

For the two merge actions in `DaemonRunners` (`merge_spec_pr`,
`merge_impl_pr`), the transitions are owned by `DaemonRunners`
itself — it makes the `add_issue_label` / `remove_issue_label`
calls inline (lines 200-205, plus `close_issue` for impl). So the
deterministic post-merge label set is also computed inline, no
helper or schema change needed:

```python
# merge_spec_pr:
final_labels = sorted(
    (set(ticket.labels) - {"foreman:spec-ready"}) | {"foreman:implementing-ready"}
)
# merge_impl_pr (issue is closed but label is also removed for queue consistency):
final_labels = sorted(set(ticket.labels) - {"foreman:ready-for-merge"})
```

`ticket.labels` is the pre-merge label set the worker handed in
when it dispatched the merge action; that set is what the merge
action sees, and the merge transition rules above are fixed. No
remote read needed.

Why is this safe? Two scenarios to think about:

1. **Role-internal label drift.** A future role refactor might
   add a label transition path that doesn't update `final_labels`
   correctly. This is the same surface area as today's
   `add_to_labels` / `remove_from_labels` discipline — get the
   transitions right OR the queue advances wrong. The new field
   doesn't add risk; it just moves the truth source from "ask
   GitHub" (which can be stale) to "what the role computed"
   (which is the role's local memory of what it wrote). The role
   already had to be right; we just stop asking GitHub
   redundantly.

2. **External label changes mid-run.** What if a human edits the
   label set on the issue while the role is running? Today: the
   `_read_labels` GET picks up the external edit and the queue
   reflects it on next iteration. After the fix: the queue
   reflects the role's transition, and the next poll (within
   `poll_interval_seconds`, default 30s) picks up the human's
   edit. The lag is at most one poll cycle. This is acceptable —
   the daemon's design already assumes the poller is the
   authoritative source for human-driven label changes, and the
   worker is the authoritative source for daemon-driven label
   changes. Asking GitHub mid-iteration for daemon-driven changes
   muddies that split for no benefit; the role's intent is
   strictly more authoritative than a remote read of what the
   role just wrote.

Why not Option B (re-fetch labels via host adapter) or Option C
(skip next-iteration dispatch entirely)? From the issue body:
- Option B keeps the re-read but does it AFTER a delay. Doesn't
  fix the cause (it just narrows the race window) and adds API
  cost (one extra GET per role).
- Option C drops the self-notify re-enqueue entirely and waits
  for the poller. Adds up to `poll_interval_seconds` (default
  30s) of latency per stage transition. That undoes the
  fast-iteration property that makes the daemon snappy.

Option A (role-authoritative labels) fixes the cause, costs zero
extra API calls, and preserves fast iteration. The fix sketch in
the issue ranks it cleanest first; the investigation confirms
that ranking.

## Sub-requests (topologically sorted)

1. Add `final_labels: list[str]` to existing wrapper schemas:
   - `packages/foreman/src/foreman/schemas/planner.py:57-67` —
     `PlannerRunResult`.
   - `packages/foreman/src/foreman/schemas/fixer.py:172-192` —
     `FixerRunResult`.
   - `packages/foreman/src/foreman/schemas/worker.py:315-352` —
     `WorkerRunResult`.

   Each field uses the same description text:

   ```python
   final_labels: list[str] = Field(
       ...,
       description=(
           "Sorted list of foreman labels on the originating issue "
           "after the role's transitions ran — the authoritative "
           "post-run label set, computed in-process from the role's "
           "known mutations, not via a post-mutation host re-read. "
           "Consumed by ``DaemonRunners.run_*`` to populate the "
           "worker's ``RoleResult.new_labels`` without the eventual-"
           "consistency hazard of a GitHub GET right after a write."
       ),
   )
   ```

2. Create a new wrapper `ReviewerRunResult` in
   `packages/foreman/src/foreman/schemas/reviewer.py` (the Reviewer
   role currently returns bare `ReviewerOutput`; the missing wrapper
   is why this bug surfaced on the impl-review path):

   ```python
   class ReviewerRunResult(BaseModel):
       """What :func:`foreman.roles.reviewer.run_reviewer` returns.

       Bundles the LLM's :class:`ReviewerOutput` with the
       deterministic post-run label set computed in-process from
       the role's known transitions. Mirrors
       :class:`~foreman.schemas.planner.PlannerRunResult` /
       :class:`~foreman.schemas.fixer.FixerRunResult` /
       :class:`~foreman.schemas.worker.WorkerRunResult`.

       The ``final_labels`` field is the fix for foreman#91:
       ``DaemonRunners.run_reviewer`` used to populate the
       worker's ``RoleResult.new_labels`` via a fresh
       ``host.get_issue_labels`` GET, which raced GitHub's
       eventual-consistency window and produced stale-snapshot
       re-dispatches at the next worker iteration.
       """

       llm_output: ReviewerOutput = Field(
           ...,
           description="The structured output the Reviewer LLM produced.",
       )
       final_labels: list[str] = Field(
           ...,
           description=(
               "Sorted list of foreman labels on the originating issue "
               "after the Reviewer's clean→spec-ready/ready-for-merge "
               "or needs_fix→spec-fix/impl-fix transition ran. The "
               "authoritative post-run label set, computed in-process; "
               "not a remote re-read."
           ),
       )
   ```

3. Update `packages/foreman/src/foreman/roles/reviewer.py`:
   - Import `ReviewerRunResult` alongside `ReviewerOutput` and
     `Finding`.
   - Change the return type annotation at line 308 from
     `-> ReviewerOutput` to `-> ReviewerRunResult`.
   - Replace `return llm_output` at line 443 with a computed
     `final_labels`:

     ```python
     final_labels = sorted(
         (issue_labels - {in_review_label}) | {add_label}
     )
     return ReviewerRunResult(
         llm_output=llm_output,
         final_labels=final_labels,
     )
     ```

     `issue_labels` at line 367 is already a `set[str]` of the
     pre-mutation labels; `in_review_label` and `add_label` are
     the exact strings passed to `remove_from_labels` /
     `add_to_labels`. No re-read.

4. Update `packages/foreman/src/foreman/roles/planner.py`:
   - At line 228 (`return PlannerRunResult(llm_output=llm_output, pr=pr)`),
     compute `final_labels` from the planner's transition. The
     Planner's mutation: removes `foreman:plan` (and the
     in-flight `foreman:planning` sentinel if it was added), adds
     `foreman:spec-review`. Capture the pre-mutation label set
     before the role's label writes (search the file for the
     `add_to_labels` / `remove_from_labels` calls; they are the
     anchor points) and compute
     `sorted((pre - {removed}) | {added})`.

5. Update `packages/foreman/src/foreman/roles/fixer.py`:
   - The Fixer has multiple `return FixerRunResult(...)` paths
     (lines ~540-562 area, depending on outcome). At each return
     point, compute the post-transition label set from the
     in-memory pre-set + the branch's mutations
     (e.g. clean→`impl-review`, fix→stays on `impl-fix` with
     attempt label; on incomplete + max-attempts, add
     `foreman:failed` and `foreman:needs-help`).

   The straightforward implementation: track a `current_labels`
   `set[str]` local that the role updates parallel to each
   `add_to_labels` / `remove_from_labels` call. At return time,
   pass `sorted(current_labels)` to `FixerRunResult(...,
   final_labels=...)`.

   Initialize `current_labels = set(issue_labels)` after the
   pre-flight label read (line 441). Each subsequent
   `issue.add_to_labels(X)` is paired with `current_labels.add(X)`;
   each `issue.remove_from_labels(X)` paired with
   `current_labels.discard(X)`. The attempt label add at line 472
   gets the same pairing.

6. Update `packages/foreman/src/foreman/roles/worker.py`:
   - Same shape as fixer: maintain a `current_labels` set local,
     pair each `issue.add_to_labels` / `issue.remove_from_labels`
     with the matching `.add()` / `.discard()`. At
     `WorkerRunResult(...)` construction (find the single call
     site), pass `sorted(current_labels)` as `final_labels`.
   - The Worker's transitions span lines ~554-701: entry-label
     removal + `foreman:implementing` add + (per-outcome) clear
     + `impl-review` add (implemented), or `spec-fix` +
     `needs-help` add (spec_invalid), or `needs-help` (+ on max
     attempt, `failed`) add (incomplete). All are sequential and
     in-scope.

7. Update `packages/foreman/src/foreman/daemon_runners.py`:
   - The `run_planner`, `run_reviewer`, `run_fixer`, `run_worker`
     methods (lines 119-188) change to use the role-returned
     wrapper's `final_labels`:

     ```python
     async def run_planner(self, *, ticket: Ticket, config: Config) -> RoleRunResult:
         project = self._project(ticket, config)
         result = await self._planner_fn(
             issue_url=_issue_url(project.repo, ticket.issue_number),
             config=config,
             project_name=ticket.project_name,
             worktrees_root=self._worktrees_root,
             provider=self._provider,
         )
         return RoleRunResult(
             new_labels=frozenset(result.final_labels),
             structured_output=_safe_dump(result.llm_output),
         )
     ```

     Apply the symmetric change to `run_reviewer`, `run_fixer`,
     `run_worker`. Note that `_safe_dump(result)` becomes
     `_safe_dump(result.llm_output)` — we want the LLM output
     persisted in the audit row, not the wrapper.

   - `merge_spec_pr` (lines 190-209) computes `new_labels`
     inline:

     ```python
     async def merge_spec_pr(self, *, ticket: Ticket, config: Config) -> RoleRunResult:
         project = self._project(ticket, config)
         branch = spec_branch(ticket.issue_number)
         pr_number = self._host.find_pr_for_branch(project.repo, branch)
         if pr_number is None:
             raise RuntimeError(
                 f"No open spec PR found for branch {branch} on {project.repo}"
             )
         self._host.merge_pull_request(project.repo, pr_number)
         self._host.remove_issue_label(
             project.repo, ticket.issue_number, "foreman:spec-ready"
         )
         self._host.add_issue_label(
             project.repo, ticket.issue_number, "foreman:implementing-ready"
         )
         final_labels = frozenset(
             (set(ticket.labels) - {"foreman:spec-ready"})
             | {"foreman:implementing-ready"}
         )
         return RoleRunResult(
             new_labels=final_labels,
             structured_output={"merged_spec_pr": pr_number},
         )
     ```

   - `merge_impl_pr` (lines 211-255) computes `new_labels`
     inline:

     ```python
     final_labels = frozenset(
         set(ticket.labels) - {"foreman:ready-for-merge"}
     )
     return RoleRunResult(
         new_labels=final_labels,
         structured_output={"merged_impl_pr": pr_number},
     )
     ```

     (The issue is closed via `self._host.close_issue(...)`, but
     the label set the queue tracks is what matters for the
     worker's `next_action`. `next_action` returns `None` for an
     empty actionable-label set, so the queue will park the
     ticket — correct.)

8. Remove the now-unused `_read_labels` method from
   `packages/foreman/src/foreman/daemon_runners.py:115-117`. Run
   `grep -rn "_read_labels" packages/foreman` to confirm no
   external callers; tests at lines 175, 197, 236, 268, 294 in
   `test_daemon_runners.py` stub `host.get_issue_labels` for the
   removed code path — those stubs become dead-weight and are
   removed in sub-request 10. The `_HostLike` protocol at
   `packages/foreman/src/foreman/daemon_runners.py:49-63` keeps
   the `get_issue_labels` method declaration because reconciliation
   in `Daemon._reconcile_in_flight`
   (`packages/foreman/src/foreman/daemon.py:67-105`) and the
   poller still legitimately read labels — verify with grep that
   no other production caller breaks before deleting.

9. Update `packages/foreman/src/foreman/cli.py` and any CLI helper
   that calls `run_reviewer` and treats the return as
   `ReviewerOutput`. Search:

   ```bash
   grep -rn "run_reviewer\|ReviewerOutput" packages/foreman/src/foreman/cli.py
   ```

   For each call site, dereference `.llm_output` for fields like
   `outcome`, `findings`, `confidence`, `review_comment`. Mirror
   the existing `PlannerRunResult`/`WorkerRunResult` consumption
   patterns in the same file.

10. Update tests in `packages/foreman/tests/test_daemon_runners.py`:
    - Remove the fixture default
      `m.get_issue_labels = MagicMock(return_value=[...])` at
      line 43 (becomes dead).
    - For each test that sets
      `host.get_issue_labels.return_value = [...]` (lines 175,
      197, 236, 268, 294), remove that stubbing and instead
      configure the role-mock to return a wrapper with
      `final_labels=[...]`. Example for the planner test at
      lines 60-77:

      ```python
      mock_role = AsyncMock(
          return_value=PlannerRunResult(
              llm_output=PlannerOutput(...minimal valid fields...),
              pr=PRRef(...),
              final_labels=["foreman:spec-review"],
          )
      )
      ```

      For tests that don't care about `llm_output`'s specific
      fields, build a `MagicMock(spec=PlannerRunResult)` with
      `final_labels=[...]` and a `model_dump` that returns `{}`
      (matches the existing test scaffold pattern).
    - Add the two new tests specified in acceptance criteria:
      `test_run_reviewer_uses_role_final_labels_not_host_reread`
      and `test_merge_spec_pr_uses_deterministic_labels_not_host_reread`
      (plus the symmetric `merge_impl_pr` test). All three assert
      `host.get_issue_labels.assert_not_called()` after the
      respective runner call.

11. Update `packages/foreman/tests/test_roles_reviewer.py`:
    - Tests asserting `isinstance(result, ReviewerOutput)` become
      `isinstance(result, ReviewerRunResult)` with
      `isinstance(result.llm_output, ReviewerOutput)`.
    - Tests asserting `result.outcome` become
      `result.llm_output.outcome` (etc. for `findings`,
      `confidence`, `review_comment`).
    - Add `test_run_reviewer_returns_authoritative_final_labels`:
      exercise both `clean` and `needs_fix` LLM paths on both
      `target="spec_pr"` and `target="impl_pr"`, assert
      `result.final_labels` is the sorted list of
      `(initial_issue_labels - {entry_label}) | {add_label}` for
      the corresponding branch.

12. Update `packages/foreman/tests/test_roles_planner.py`,
    `packages/foreman/tests/test_roles_fixer.py`, and
    `packages/foreman/tests/test_roles_worker.py` to assert
    `final_labels` matches the deterministic transition for the
    outcome the test exercises. One new test per file
    (`..._returns_authoritative_final_labels`) is sufficient if
    that test parametrizes across the outcome branches.

13. Update `packages/foreman/tests/test_schemas_reviewer.py` to
    add a construction-shape test for `ReviewerRunResult`,
    matching the `PlannerRunResult` pattern in
    `test_schemas_planner.py`. Also add a test that
    `ReviewerRunResult(final_labels=...)` accepts a `list[str]`
    and rejects (Pydantic validation error) non-list inputs and
    non-string elements.

14. Add the worker-level integration test
    `test_run_one_iteration_does_not_re_dispatch_on_stale_labels`
    in `packages/foreman/tests/test_worker.py`. Use the existing
    `_FakeRoleDispatcher` pattern; configure
    `result_factory` to return
    `RoleResult(new_labels=frozenset({"foreman:ready-for-merge"}),
    structured_output=..., outcome="success")`. Run
    `run_one_iteration` once with a starting ticket carrying
    `{foreman:impl-review}`; then assert the queue contains the
    ticket with `{foreman:ready-for-merge}` (already covered by
    `test_run_one_iteration_re_enqueues_when_more_work_remains`
    at lines 121-138 — model the new test on it). Then run
    `run_one_iteration` again and assert:
    - if `auto_merge_impl=True`: the second call's
      `dispatcher.calls[1].action.kind == ActionKind.MERGE_IMPL_PR`
      (NOT `RUN_REVIEWER_IMPL`),
    - if `auto_merge_impl=False`: the second call advances False
      OR the ticket is parked (no second dispatch happens with
      `RUN_REVIEWER_IMPL`).

    Use the project config helper to construct an
    `auto_merge_impl=True` project for the first variant; the
    existing `_project_configs()` defaults to
    `auto_merge_impl=False`, so explicit override is needed.

15. Run targeted tests:

    ```bash
    uv run pytest packages/foreman/tests/test_daemon_runners.py \
        packages/foreman/tests/test_worker.py \
        packages/foreman/tests/test_schemas_reviewer.py \
        packages/foreman/tests/test_roles_reviewer.py \
        -v
    ```

16. Run `just check` and confirm exit zero. Address any
    type-check fallout from the wrapper change (mypy will catch
    any leftover `ReviewerOutput`-typed call sites that didn't
    get migrated).

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/schemas/reviewer.py` | New `ReviewerRunResult(BaseModel)` with `llm_output: ReviewerOutput` and `final_labels: list[str]`. Mirrors the existing `PlannerRunResult` shape. |
| `packages/foreman/src/foreman/schemas/planner.py` | `PlannerRunResult` gains `final_labels: list[str]` (with the standard description string). |
| `packages/foreman/src/foreman/schemas/fixer.py` | `FixerRunResult` gains `final_labels: list[str]`. |
| `packages/foreman/src/foreman/schemas/worker.py` | `WorkerRunResult` gains `final_labels: list[str]`. |
| `packages/foreman/src/foreman/roles/reviewer.py` | Return type changes from `ReviewerOutput` to `ReviewerRunResult`. Compute `final_labels = sorted((issue_labels - {in_review_label}) | {add_label})` and return both. |
| `packages/foreman/src/foreman/roles/planner.py` | Compute `final_labels` from the Planner's known transition (`foreman:plan` / `foreman:planning` → `foreman:spec-review`) at the return site (line 228). |
| `packages/foreman/src/foreman/roles/fixer.py` | Track `current_labels: set[str]` parallel to each `add_to_labels` / `remove_from_labels` call; pass `sorted(current_labels)` at each `FixerRunResult(...)` return. |
| `packages/foreman/src/foreman/roles/worker.py` | Same pattern as fixer: track `current_labels` parallel to the role's label mutations; pass it at the single `WorkerRunResult(...)` return. |
| `packages/foreman/src/foreman/daemon_runners.py` | `run_planner` / `run_reviewer` / `run_fixer` / `run_worker` consume `result.final_labels` instead of calling `self._read_labels(...)`. `_safe_dump(result)` becomes `_safe_dump(result.llm_output)`. `merge_spec_pr` / `merge_impl_pr` compute `new_labels` inline from `ticket.labels` + the merge transitions. Remove `_read_labels` after confirming no callers via grep. |
| `packages/foreman/src/foreman/cli.py` | Update call sites that consume `run_reviewer`'s return — read fields through `.llm_output` (same pattern as the other roles' wrappers already there). |
| `packages/foreman/tests/test_daemon_runners.py` | Remove `host.get_issue_labels` fixture default + stubbing at lines 43, 175, 197, 236, 268, 294. Mock role returns to set `final_labels`. Add three new tests (one per `run_reviewer` / `merge_spec_pr` / `merge_impl_pr`) that assert `host.get_issue_labels.assert_not_called()` and the deterministic label set. |
| `packages/foreman/tests/test_roles_reviewer.py` | Migrate `ReviewerOutput` return-type assertions to `ReviewerRunResult`. Field access through `.llm_output`. New `test_run_reviewer_returns_authoritative_final_labels` covering both targets × both outcomes. |
| `packages/foreman/tests/test_roles_planner.py` | One new `..._returns_authoritative_final_labels` test. |
| `packages/foreman/tests/test_roles_fixer.py` | One new `..._returns_authoritative_final_labels` test parametrized over outcomes. |
| `packages/foreman/tests/test_roles_worker.py` | One new `..._returns_authoritative_final_labels` test parametrized over implemented / incomplete / spec_invalid. |
| `packages/foreman/tests/test_schemas_reviewer.py` | Construction-shape test for `ReviewerRunResult`; validation tests for `final_labels`. |
| `packages/foreman/tests/test_worker.py` | New `test_run_one_iteration_does_not_re_dispatch_on_stale_labels` modeled on `test_run_one_iteration_re_enqueues_when_more_work_remains` — exercises the full role-success → re-enqueue → next-action sequence, asserts no stale-snapshot dispatch. |

## Alternatives considered

- **Re-fetch labels from GitHub with a short delay (Option B in the
  issue's fix sketch).** Rejected: doesn't fix the cause; only
  narrows the race window. Adds API cost (one extra GET per role
  invocation). Eventual-consistency windows on GitHub vary; a
  fixed delay either over-spends latency budget or doesn't reliably
  close the race.

- **Skip the worker's self-notify re-enqueue and let the poller
  pick up the new labels (Option C).** Rejected: adds up to one
  full poll interval (default 30s) of latency between stage
  transitions. Undoes the daemon's fast-iteration property that
  makes the loop feel responsive. The architectural spec
  (`docs/superpowers/specs/foreman-v1-architectural-spec.md`)
  explicitly relies on self-notify for sub-second stage transitions.

- **Add a retry loop inside `_read_labels` that polls the host
  until the labels match the role's intent.** Rejected: same as
  Option B in spirit — papers over the race with API churn. The
  role already knows the intent; making the worker poll to confirm
  what the role just wrote is redundant and expensive.

- **Use the same client identity for both the role's writes and
  the post-write read (i.e., have the role itself call
  `_read_labels` using its own client).** Rejected: even
  same-client read-after-write on GitHub labels isn't strictly
  guaranteed atomic; this would narrow the race but not close it.
  And it spreads label-source-of-truth across the role/runner
  boundary, making the design harder to reason about.

- **Use a stale-snapshot guard in `next_action` that re-reads
  labels remotely before dispatching.** Rejected: same eventual-
  consistency hazard as `_read_labels`, just at a different point
  in the loop. Also breaks `next_action`'s "pure function, no I/O"
  contract — that's a core architectural property of the daemon
  (`packages/foreman/src/foreman/dispatcher.py:1-10`).

- **Distinguish the race-condition log signature from the
  foreman#79-family precondition-mismatch log signature by
  carrying a "dispatch context" structured field on the
  RuntimeError.** Acknowledged as adjacent and worth doing —
  explicitly out of scope here per the issue body ("Distinguishing
  ... follow-up"). Leaving as a separate ticket: the race goes
  away with this fix, so the log-disambiguation problem reduces
  to the single foreman#79 cause; that's a separate spec.

## Open questions

(none — the cause is precisely identified, the fix is mechanical,
all four roles share the same pattern, and the tests have clear
contract assertions. The merge-action label computation
(`merge_impl_pr` ending with the issue closed but the label still
in the queue's `new_labels`) is intentionally documented in the
approach section.)

## Out of scope

- **Distinguishing foreman#79-style real precondition mismatches
  from race-condition stale snapshots in the daemon log.** Per the
  issue body, that's a follow-up. After this fix, the race-condition
  source is gone, so any remaining
  `Issue #N does not carry the 'X' label` ERROR has a single
  cause (the foreman#79 family). If operators want a cleaner
  signature anyway, that's a separate ticket.

- **Re-architecting the worker iteration model.** The issue
  explicitly excludes this. The current iteration model
  (`packages/foreman/src/foreman/worker.py`) is sound; the bug is
  in the label-source decision at the seam between
  `DaemonRunners` and the worker. Fixing the seam is enough.

- **Removing the `get_issue_labels` method from the host adapter
  or `_HostLike` protocol.** It still has legitimate callers:
  `Daemon._reconcile_in_flight`
  (`packages/foreman/src/foreman/daemon.py:67-105`), the poller's
  search path
  (`packages/foreman/src/foreman/poller.py:52-76` — note: the
  poller reads labels via `search_foreman_labeled_issues`, not
  `get_issue_labels`, but the symmetry argument stands). Keep the
  method available; just stop calling it from
  `DaemonRunners.run_*`.

- **Reviewer Pydantic model unification with the other roles'
  `RunResult` shape (PRRef, attempt counters, etc.).** This spec
  adds `ReviewerRunResult` only to the extent needed for the bug
  fix. If the Reviewer should also surface attempt counts or PR
  refs, that's a separate spec — the foreman v1 architectural
  spec doesn't currently require parity here.

- **Auditing other role transitions for in-process determinism
  bugs.** The fix in this spec assumes each role's transition
  rules are correct as written today. If `roles/worker.py` has a
  buggy transition path (e.g., adds a label that the queue's
  `next_action` doesn't expect), that's a separate bug not
  caused by this race. The new `final_labels` tests will catch
  it if it exists, but fixing such a bug is out of scope here.

- **Persisting `final_labels` to the SQLite audit row separately
  from `from_labels` / `to_labels`.** The worker already writes
  `to_labels` to `storage.record_transition`
  (`packages/foreman/src/foreman/worker.py:179-185`); after this
  fix, the value comes from `result.final_labels` rather than
  `_read_labels`, but the SQLite shape is unchanged. No
  migration needed.
