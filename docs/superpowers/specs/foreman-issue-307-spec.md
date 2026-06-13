# Spec: introduce LabelManager — single owner for `foreman:*` label lifecycles (issue #307)

## Goal

Build the runtime layer the D1 typed-label catalog (`packages/foreman/src/foreman/labels.py`) was waiting for: a single `LabelManager` module that owns every `foreman:*` label write, enforces classification-level invariants (writing a `QUEUE` label strips `IN_FLIGHT` labels; writing `TERMINAL` strips everything non-terminal), and emits a structured log line per transition. This eliminates the recurrent failure class where transitional labels (`merging-plan`, `merging-impl`) outlive the operation that wrote them and silently disable downstream rule predicates that gate on `"foreman:merging-plan" not in ctx.issue.labels`. See issue [#307](https://github.com/jeffrichley/foreman/issues/307); the empirical case study is issue #303's 30-minute autonomous-loop stall on 2026-06-12.

## Acceptance criteria

- **New module exists.** `packages/foreman/src/foreman/label_manager.py` defines a `LabelManager` class (or callable surface — name negotiable, contract not) exposing at minimum a `transition(...)` entry point that takes:
  - a `LabelWriter` (small protocol: `read_current() -> set[str]`, `replace_labels(final_set: set[str]) -> None`),
  - a set of `Label` (D1 enum) members to add,
  - a set of `Label` members (or class-based shorthands) to remove,
  - a `reason: str` for the audit log,
  - and returns the final label set actually applied.
- **Invariants are enforced inside the manager, not by callers.** The following are unit-tested in `packages/foreman/tests/test_label_manager.py`:
  - Writing any `LabelClass.QUEUE` label (e.g. `Label.IMPL_APPROVED`) strips every `LabelClass.IN_FLIGHT` label in the SAME transition. This is the #303 regression — writing `IMPL_APPROVED` MUST remove `MERGING_PLAN` automatically without the caller naming it.
  - Writing any `LabelClass.TERMINAL` label (e.g. `Label.DONE`, `Label.FAILED`) strips every non-terminal `foreman:*` label in the same transition.
  - Re-adding a label that is already present is a no-op (idempotence) — does NOT raise, but also does NOT add a duplicate write or duplicate log line.
  - Removing a label that is already absent is a no-op — does NOT raise (this is the bug PyGithub's `remove_from_labels` has: it 404s; the manager swallows that case at the API boundary).
  - Non-`foreman:*` labels (`priority:high`, `needs:design`, plain `bug`) pass through untouched — the manager only mutates the `foreman:` namespace.
- **Namespace-scoped merge is internalized.** The pattern currently duplicated in `roles/{worker,fixer,reviewer}.py` (`removed_foreman` / `added_foreman` sets + `issue.update()` + read current + `(current - removed) | added` + `issue.set_labels(*sorted(final))`) is collapsed into the manager. Each role's call site becomes a single `label_manager.transition(...)` call.
- **Single import point.** A new `packages/foreman/tests/test_label_manager.py::test_label_writes_only_go_through_label_manager` enforces that the only modules referencing PyGithub's `Issue.add_to_labels` / `Issue.remove_from_labels` / `Issue.set_labels` symbols (by literal grep against `packages/foreman/src/foreman/`, with Python comments and docstrings stripped — see Sub-request 2 for the implementation) are: `foreman/label_manager.py`, the two writer adapters (`foreman/daemon_host.py` lines 87/93 and `foreman/git_hosts/github.py` lines 183/185 — the thin PyGithub wrappers). The scanner does not walk `packages/foreman/tests/` (test fixtures legitimately call PyGithub label methods on their fakes), so no test-file allowlist is needed. Every role module (`roles/planner.py`, `roles/reviewer.py`, `roles/worker.py`, `roles/fixer.py`) and `reconciler/actions.py` are excluded — they MUST route through the manager.
- **Host-side `update_issue_labels` is removed.** The `GitHost.update_issue_labels` abstract method (`GitHost` is an `ABC` with `@abstractmethod`, not a `typing.Protocol`; `packages/foreman/src/foreman/git_host.py:127`) and its `GitHubProvider.update_issue_labels` implementation (`packages/foreman/src/foreman/git_hosts/github.py:173-185`) are deleted as part of this PR. After the Planner migration (Sub-request 10), the only caller in `packages/foreman/src/foreman/` is gone; leaving the method in place would be a second label-write surface that bypasses the grep-fence (the test scans for PyGithub method tokens, not the host Protocol token). Test doubles in `tests/test_git_host.py`, `tests/test_roles_exception_handler.py`, and `tests/test_roles_planner.py` lose their stub implementations; `tests/test_git_hosts_github.py::test_update_issue_labels_removes_then_adds` is deleted (the behavior it exercised no longer exists). The `host.add_label` / `host.remove_label` removal is a separate question (see "Out of scope").
- **Reconciler action handlers route through the manager.** Each label-mutating action handler in `packages/foreman/src/foreman/reconciler/actions.py` is converted to a single `label_manager.transition(...)` call. The specific sites:
  - `Action.ADVANCE_LABEL_TO_MERGING_PLAN` (lines 744-749) — writes `Label.MERGING_PLAN`; the manager's IN_FLIGHT invariant does NOT strip it (writing IN_FLIGHT alongside an existing `plan-approved` QUEUE is the documented legal transition; QUEUE→IN_FLIGHT additivity is intentional, see Open questions).
  - `Action.ADVANCE_LABEL_TO_MERGING_IMPL` (lines 751-757) — symmetric.
  - `Action.ADVANCE_LABEL_TO_PLANNING` (lines 775-792) — was sequential `remove(PLAN) + add(PLANNING)`; becomes one `transition(add={PLANNING}, remove={PLAN})`.
  - `Action.ADVANCE_LABEL_TO_PLAN_APPROVED` (lines 793-805) — was sequential `remove(PLANNING) + add(PLAN_APPROVED)`; becomes one transition. Writing `PLAN_APPROVED` (QUEUE) WILL strip any `MERGING_*` (IN_FLIGHT) labels via the invariant. NOTE: in the empirical #303 timeline, `PLAN_APPROVED` was written BEFORE `MERGING_PLAN` arrived, so this invariant fires DEFENSIVELY rather than as the empirical #303 closer. The actual write path that fixes the #303 rot is the Reviewer's `IMPL_APPROVED` write (per the Acceptance criterion's `test_writing_impl_approved_strips_merging_plan` regression). The QUEUE-strips-IN_FLIGHT invariant fires at every QUEUE-write site as defense-in-depth.
  - `Action.ADVANCE_LABEL_TO_DONE` (lines 806-818) — was sequential `remove(IMPL_APPROVED) + add(DONE)`; becomes one transition with the TERMINAL invariant stripping all non-terminal labels.
  - `SURFACE_HELP` (line 697) + `_surface_attempt_merge_needs_help` (line 627) + `RATE_LIMIT_TRIP` (line 365) — three sites that add `foreman:needs-help` via `host.add_label(...)`. Convert each to `transition(add={Label.NEEDS_HELP}, ...)`. `needs-help` is BLOCKING, not QUEUE/TERMINAL, so the invariants leave the in-flight label alone (operationally desirable per actions.py:350-363 — the in-flight label IS the diagnostic of "tripped while planning").
- **Role modules route through the manager.** Each `issue.set_labels(*sorted(final_label_set))` site in `roles/{worker,fixer,reviewer}.py` is replaced with one `label_manager.transition(...)` call. The role's outcome branch still computes the `add` / `remove` sets (the role's verdict — `removed_foreman` / `added_foreman` in today's code) and passes them in; the manager handles the read-update-write cycle and invariants. Sites:
  - `roles/worker.py:761` (pre-LLM attempt-label stamp) and `:1188` (post-LLM outcome).
  - `roles/worker.py:1323` (crash-revert path) — uses the manager too so the revert observes the same invariants and emits a `reason="crash_revert"` audit line.
  - `roles/fixer.py:790` (outcome).
  - `roles/reviewer.py:674` (clean / needs_fix verdict).
  - `roles/planner.py:294` (exception-callback label write — `bound_host.update_issue_labels(bound_repo_slug, bound_issue_number, add=[TERMINAL_BLOCKING_LABEL], remove=[])`). The Planner exception handler does NOT have a PyGithub `Issue` object in scope (only `bound_host: GitHost` + `bound_repo_slug` + `bound_issue_number`). Resolution: inside the exception handler, fetch the issue once with `bound_issue = bound_host.get_issue(bound_repo_slug, bound_issue_number)` and pass `IssueLabelWriter(bound_issue)` to the manager. This is one extra GET on an already-rare exception path; equivalent to what `GitHost.update_issue_labels` does internally (`repo.get_issue(issue_number)`).
  - The six `lambda: bound_issue.add_to_labels(TERMINAL_BLOCKING_LABEL)` (or equivalent) callbacks for `handle_unhandled_role_exception` — at `reviewer.py:510`, `fixer.py:489`, `fixer.py:623`, `worker.py:674`, `worker.py:862`, and `planner.py:294` (the Planner site uses `bound_host.update_issue_labels(...)` rather than a PyGithub `add_to_labels`, but the lifecycle role is identical and it migrates the same way). Convert each to invoke the manager. The exception path is the one place where atomicity is most fragile; the manager call here is the same shape (`add={Label.NEEDS_HELP}`) but goes through the standard transition surface for logging consistency.
  - `roles/fixer.py:531` (`issue.add_to_labels(attempt_label)` for the `fix-attempt-N` counter) — this is a literal string `f"foreman:fix-attempt-{n}"` not in the `Label` enum but classified as `COUNTER` via the `COUNTER_LABELS` frozenset. Decision: the manager's `transition()` accepts a `str` for the dynamic attempt label (typed as `Label | str`) and resolves the classification by membership lookup against `COUNTER_LABELS`. Same shape for the parallel `issue.set_labels(*pre_dispatch_labels)` worker stamp.
- **Structured logging per transition.** Each manager call emits exactly ONE log line at INFO level via `logger = logging.getLogger("foreman.label_manager")`. Fields (logger `extra=`, NOT f-string interpolation): `issue_url` (sourced from `writer.issue_url`, see Sub-request 1's `LabelWriter` protocol), `from_labels` (sorted list of strings), `to_labels` (sorted list of strings), `added` (sorted list), `removed` (sorted list), `reason`. The pre-transition + post-transition sets are computed inside the manager so a caller cannot lie about either. A no-op transition (`final == current` AFTER invariants are applied — the SAME check Sub-request 1 uses to decide whether to call `replace_labels`) logs at DEBUG instead of INFO so idempotent retries don't spam. **CRITICALLY this MUST be the post-invariant check, not the surface-level `add ∩ current == add AND remove ∩ current == ∅` check.** In the #303 idempotent-retry scenario (`current = {foreman:impl-approved, foreman:merging-plan, priority:high}`, `add = {Label.IMPL_APPROVED}`), the surface check classifies as no-op but the QUEUE-strips-IN_FLIGHT invariant strips `merging-plan`, so `final != current` and the write MUST fire (at INFO, with the WARNING for the stale-strip). Both the DEBUG-vs-INFO log decision and the skip-the-write decision use the same `final == current` check, computed post-invariants.
- **Stale-label guard fires WARNING.** When the manager about to write a `QUEUE` or `TERMINAL` label observes an `IN_FLIGHT` label present in `from_labels` that the invariant is about to strip, it logs a WARNING with the same `extra=` shape plus `stale_label_stripped: list[str]`. This is the diagnostic surface — once #303-class rot happens, the operator sees a loud line, not silent absorption.
- **Regression test for #303.** `packages/foreman/tests/test_label_manager.py::test_writing_impl_approved_strips_merging_plan` asserts that a `LabelManager` whose `LabelWriter` starts with `{foreman:planning, foreman:merging-plan}` (the #303 leak state) and is asked to `transition(add={Label.IMPL_APPROVED}, remove=set(), reason="reviewer_clean")` ends with `{foreman:impl-approved}` — `merging-plan` automatically stripped by the QUEUE-vs-IN_FLIGHT invariant. The same test asserts the WARNING log line is emitted with `stale_label_stripped=["foreman:merging-plan"]`.
- **Existing test suite unchanged.** All currently-passing tests in `packages/foreman/tests/` continue to pass (issue body cites 1086; the Worker should record actual pre-edit count + confirm post-edit count is unchanged). Tests that previously inspected `_FakeHost.calls` for `("add_label", ...)` + `("remove_label", ...)` tuples (`tests/reconciler/test_actions.py:135-144` and similar) MAY be updated to assert the new manager-mediated shape (`replace_labels` calls instead, or a recorded `transition()` call) — those updates are mechanical and part of the migration.
- **Quality gate clean.** `just check` exits 0 on the impl worktree: ruff clean, mypy clean, full pytest green.
- **PR conventions.** Impl PR uses `feat(labels):` prefix per `CLAUDE.md` (standard conventional-commit). Subject MUST NOT start with an uppercase letter. PR body references #307 plainly (no closing keywords) per the foreman#63 close-out-gate rule.

## Approach

Per `CLAUDE.md`'s Decision-4 calibrated bias: this design embodies two principles plainly and one GoF pattern weakly.

- **SRP (Single Responsibility Principle).** Today the `foreman:*` label namespace is mutated by 6+ modules (`reconciler/actions.py`, each of the four role modules, `daemon_host.py` / `git_hosts/github.py` as PyGithub wrappers). Each is also responsible for unrelated work (role logic, action dispatch, PyGithub plumbing). The `LabelManager`'s ONE job is `foreman:*` label transitions — class invariants, atomic writes, audit logging. This is a textbook SRP extract.
- **DIP (Dependency Inversion Principle).** Today callers depend on PyGithub's `Issue.set_labels` (roles) or the `ReconcilerHost.add_label`/`remove_label` protocol (actions). The manager inverts that: callers depend on the `LabelManager`'s narrow `transition()` API; the manager depends on a small `LabelWriter` protocol (`read_current` / `replace_labels`) that EITHER the PyGithub Issue OR a wrapped `ReconcilerHost` can satisfy. The Worker, Fixer, Reviewer, and reconciler action executor become writer-agnostic — the same manager code drives both surfaces.
- **GoF Facade (weak fit).** The manager is a thin facade over PyGithub's label-write API: it hides the sequence (refresh cache, read live state, apply invariants, atomic PUT). This is the weakest of the three framings — the manager isn't aggregating a complex subsystem behind a simplified API so much as constraining a single API — but the shape rhymes with Facade enough to call out.

**What the manager does NOT do.** It is NOT a state machine; the state machine still lives in `reconciler/rules.py`. It is NOT a label catalog; the D1 catalog (`labels.py`) stays the source of truth for string + classification. It is NOT a fix for the rule predicates that gate on `"foreman:merging-plan" not in ctx.issue.labels` — those become correct AS A CONSEQUENCE of the rot being gone (per "Out of scope"). It is NOT a new label or label class.

**The writer-adapter shape.** Two trivial adapters land alongside the manager:

```python
class IssueLabelWriter:
    """Adapts a PyGithub Issue to LabelWriter."""
    def __init__(self, issue: github.Issue.Issue) -> None: ...
    @property
    def issue_url(self) -> str:
        return self._issue.html_url
    def read_current(self) -> set[str]:
        self._issue.update()  # invalidate PyGithub label cache
        return {lbl.name for lbl in self._issue.labels}
    def replace_labels(self, final: set[str]) -> None:
        self._issue.set_labels(*sorted(final))

class ReconcilerHostLabelWriter:
    """Adapts ReconcilerHost (+ owner/repo/issue) to LabelWriter."""
    def __init__(self, host: ReconcilerHost, *, owner: str, repo: str, issue: int) -> None: ...
    @property
    def issue_url(self) -> str:
        return f"https://github.com/{self._owner}/{self._repo}/issues/{self._issue}"
    def read_current(self) -> set[str]: ...  # adds a new ReconcilerHost.get_labels method
    def replace_labels(self, final: set[str]) -> None: ...  # adds a new ReconcilerHost.set_labels method
```

Adding `get_labels` + `set_labels` to `ReconcilerHost` (and `v3_host.py`'s impl + `daemon_host.py`'s underlying `get_issue_labels` already exists, plus a new atomic `set_issue_labels` method) is part of this PR. The existing `host.add_label` / `host.remove_label` methods on `ReconcilerHost` are KEPT as dead code for one PR — after the Sub-request 6 migration they have ZERO callers in `packages/foreman/src/foreman/`, but removing them is left as a one-line follow-up PR to keep this diff scoped (see "Out of scope").

**Why a writer protocol rather than the manager calling PyGithub directly.** Two reasons. (1) The reconciler-side context (`ActionContext.snapshot.owner` / `.repo`, no PyGithub `Issue` object) is shaped differently from the role-side context (a PyGithub `Issue` already in hand). Forcing one through the other adds API noise. (2) Unit tests for the manager become trivial: a `FakeLabelWriter` with a single `set[str]` field is all the test infrastructure needed to exhaustively cover the invariants, no PyGithub fixtures.

**Topological dependency.** The manager module + tests land first (no callers, fully testable in isolation). Then the writer adapters. Then per-call-site migration, one site at a time, with `just check` between each site. The grep-fence test lands LAST — it cannot pass until every site is migrated, and once it does pass, future PRs can't regress without explicitly opting out.

## Sub-requests (topologically sorted)

1. **Create `packages/foreman/src/foreman/label_manager.py`** with:
   - `LabelWriter` Protocol (`read_current() -> set[str]`, `replace_labels(final: set[str]) -> None`, `issue_url: str` (property)). The `issue_url` property is required so the manager can populate the `extra={"issue_url": ...}` log payload without the caller threading it through — it keeps the "manager computes everything" contract intact. Both writer adapters source the URL from data they already hold (PyGithub `Issue.html_url`; reconciler `owner`/`repo`/`issue` → `https://github.com/{owner}/{repo}/issues/{issue}`).
   - `LabelManager.transition(writer, *, add, remove, reason) -> set[str]` method. Accepts `add: set[Label | str]` and `remove: set[Label | str]` (str case is for the dynamic `foreman:impl-attempt-N` / `foreman:fix-attempt-N` counters).
   - Internal `_apply_invariants(current, add, remove) -> (final, stripped_in_flight, stripped_non_terminal)` pure helper. The pure helper is the reusable seam tests target directly.
   - Module-level `logger = logging.getLogger("foreman.label_manager")`.
   - Invariant logic:
     - For each label in `add` that resolves to `LabelClass.QUEUE`: every label in `current` that resolves to `LabelClass.IN_FLIGHT` is added to `stripped_in_flight` and removed from `final`.
     - For each label in `add` that resolves to `LabelClass.TERMINAL`: every label in `current` whose classification is NOT TERMINAL AND that starts with `foreman:` is added to `stripped_non_terminal` and removed from `final`.
     - Idempotence: `final = (current ∪ add) − remove` (after invariants); writes only happen if `final != current`.
     - Non-`foreman:` labels in `current` are PRESERVED throughout (filtered by `.startswith("foreman:")` in invariant logic).
   - Classification resolver helper `_classify(label_str) -> LabelClass | None`. Uses the D1 frozensets (`QUEUE_LABELS`, `IN_FLIGHT_LABELS`, `BLOCKING_LABELS`, `COUNTER_LABELS`, `TERMINAL_LABELS`); returns `None` for `foreman:*` strings not in any frozenset (defensive — should never happen with the D1 catalog complete, but the manager logs WARNING if it does).
   - Adapter classes `IssueLabelWriter` and `ReconcilerHostLabelWriter` as described in Approach.

2. **Write `packages/foreman/tests/test_label_manager.py`** with FakeLabelWriter + unit tests covering:
   - Each invariant individually (QUEUE-strips-IN_FLIGHT, TERMINAL-strips-non-terminal, idempotence, non-foreman passthrough).
   - The #303 regression test: writer starts with `{foreman:planning, foreman:merging-plan, priority:high}`, transition adds `IMPL_APPROVED`, final is `{foreman:impl-approved, priority:high}` (planning stripped because it's IN_FLIGHT and IMPL_APPROVED is QUEUE; merging-plan stripped same way; priority:high preserved).
   - **Post-invariant idempotence guard**: writer starts with `{foreman:impl-approved, foreman:merging-plan}`, transition adds `IMPL_APPROVED` (already present), final is `{foreman:impl-approved}` — i.e. the QUEUE-strips-IN_FLIGHT invariant fires EVEN THOUGH the surface-level `add ∩ current == add` check would classify this as no-op. `replace_labels` IS called (one write), the INFO log line IS emitted, and the WARNING for the stale strip IS emitted. This test pins the post-invariant `final == current` semantic explicitly.
   - WARNING log emitted when stale IN_FLIGHT labels are stripped (use `caplog` fixture).
   - No-op transitions (truly: `final == current` after invariants, e.g. `current = {foreman:impl-approved}` + add `IMPL_APPROVED` with no stale labels present) log at DEBUG, not INFO, and call `replace_labels` zero times.
   - The grep-fence test `test_label_writes_only_go_through_label_manager` that walks `packages/foreman/src/foreman/` and asserts ONLY the allowlisted files contain the literal substrings `add_to_labels`, `remove_from_labels`, `set_labels`. Allowlist: `label_manager.py`, `daemon_host.py`, `git_hosts/github.py`. **Comment- and docstring-stripping**: the scanner uses the stdlib `tokenize` module to skip `tokenize.COMMENT` and `tokenize.STRING` tokens whose value matches a triple-quoted docstring (or any string token at a position where Python's grammar treats it as a docstring) before searching for the forbidden substrings. The intent is to fail only on real code references. Implementation sketch: open the file with `tokenize.open(path)`, iterate `tokenize.generate_tokens`, accumulate non-COMMENT non-STRING tokens' `string` field into a buffer, and search the buffer; raise an `AssertionError` listing offending files and the matching token if any forbidden token appears outside the allowlist. The three known-stale comment references (see Sub-requests 7/8/9 below) are independently scrubbed as part of the per-call-site migration so this scanner's job is "future-proofing", not "compensating for known leftover comments".

3. **Add `get_labels` + `set_labels` methods to `ReconcilerHost` Protocol** (`packages/foreman/src/foreman/reconciler/host.py:43-105`):
   ```python
   def get_labels(self, *, owner: str, repo: str, issue: int) -> set[str]: ...
   def set_labels(self, *, owner: str, repo: str, issue: int, labels: set[str]) -> None: ...
   ```

4. **Implement the new methods in `v3_host.py`** (`packages/foreman/src/foreman/reconciler/v3_host.py:473-477` neighborhood):
   - `get_labels` delegates to a NEW `daemon_host.get_issue_labels` (which already exists at `daemon_host.py:101-105` — reuse verbatim).
   - `set_labels` delegates to a NEW `daemon_host.set_issue_labels` method (add it at `daemon_host.py` next to lines 83-93; calls `issue.set_labels(*sorted(labels))`).

5. **Update test fixtures** (`packages/foreman/tests/reconciler/test_actions.py:135-200` `_FakeHost` class and any other recording fakes) to add `get_labels` (returning a per-test-injectable label set, default empty) and `set_labels` (recording the call into `calls`).

6. **Migrate reconciler action handlers** to route through the manager. Touch only:
   - `Action.ADVANCE_LABEL_TO_MERGING_PLAN` (actions.py:744-749).
   - `Action.ADVANCE_LABEL_TO_MERGING_IMPL` (actions.py:751-757).
   - `Action.ADVANCE_LABEL_TO_PLANNING` (actions.py:775-792).
   - `Action.ADVANCE_LABEL_TO_PLAN_APPROVED` (actions.py:793-805).
   - `Action.ADVANCE_LABEL_TO_DONE` (actions.py:806-818).
   - `SURFACE_HELP` block at actions.py:697-712.
   - `_surface_attempt_merge_needs_help` at actions.py:615-644.
   - `_handle_rate_limit_trip` `add_label(needs-help)` call at actions.py:364-379 (the load-bearing one — keep the same try/except wrap; just swap the call inside).
   Each call site constructs `ReconcilerHostLabelWriter(host=host, owner=..., repo=..., issue=...)` and calls `LabelManager().transition(writer, add=..., remove=..., reason="<action_name>")`.

7. **Migrate `roles/worker.py`** sites: lines 761 (pre-LLM), 1188 (post-LLM outcome), 1323 (crash-revert). Construct `IssueLabelWriter(issue)`; call `transition(...)`. The role's existing `removed_foreman` / `added_foreman` sets become the `remove=` / `add=` kwargs. **Keep** the `issue.update()` call at line 1170 and the `current_label_names_post = {label.name for label in issue.labels}` read at line 1171 — those are NOT redundant: the Worker enumerates `foreman:impl-attempt-N` labels from the live remote state (`removed_foreman_post = {n for n in current_label_names_post if n.startswith("foreman:impl-attempt-") ...}` at lines 1178-1186), and the manager's `read_current()` cannot infer that pattern-enumeration. Trade-off documented: the role pays one extra `issue.update()` round-trip on top of the manager's `read_current()`. Alternative considered: extend `transition()` with a `remove_class=set[LabelClass]` or `remove_matching=Callable[[str], bool]` kwarg that lets the manager enumerate inside its own read. Rejected for v1 — the role still needs to know `final_outcome == "implemented"` to decide whether to enumerate at all, and the kwarg adds API surface without removing the conditional. v2 may revisit. Same call-site shape applies for the worker's pre-LLM stamp (line 761) and crash-revert (line 1323) — those write static label sets and do not need the enumeration read; they still construct an `IssueLabelWriter(issue)` and call `transition(...)`. The `lambda: issue.add_to_labels(TERMINAL_BLOCKING_LABEL)` callbacks at lines 674 and 862 become `lambda: LabelManager().transition(IssueLabelWriter(issue), add={Label.NEEDS_HELP}, remove=set(), reason="worker_exception")`. **Comment scrub**: update the stale docstring at lines 725, 764, 775, 935, 1044, 1125, 1147, 1149, 1305 that reference `issue.set_labels(...)` / `set_labels` / `remove_from_labels` / `add_to_labels` — those comments are obsolete once the calls are gone; rewrite them to reference `LabelManager().transition(...)` or delete them. The grep-fence test (Sub-request 2) strips comments via `tokenize`, but the comments are also factually misleading post-migration and should not be left to mislead future readers.

8. **Migrate `roles/fixer.py`** sites: line 531 (attempt label add — pass `str(f"foreman:fix-attempt-{attempt}")` as a member of `add`), line 790 (outcome). Callbacks at lines 489 and 623 → manager call. **Keep** the `issue.update()` call at line 777 AND the `current_label_names = {label.name for label in issue.labels}` read at line 778 — same reasoning as Sub-request 7: the Fixer enumerates `foreman:fix-attempt-N` labels from live remote state (lines 784-788). The role pays one extra round-trip; the trade-off mirrors the Worker. **Comment scrub**: update the stale docstring/comment references at lines 540, 633, 695, 708, 710, 721 that mention `issue.set_labels(...)` / `set_labels` / `remove_from_labels` / `add_to_labels` — rewrite to reference the LabelManager or delete.

9. **Migrate `roles/reviewer.py`** sites: line 674 (outcome). Callback at line 510 → manager call. **Remove** the now-redundant `issue.update()` at line 671 — Reviewer's `removed_foreman = {in_review_label}` is a static singleton (no pattern enumeration), so the writer's `read_current()` is the only label-read the role needs. **Comment scrub**: update the stale docstring/comment references at lines 420, 640, 641, 645, 649 that mention `issue.set_labels(...)` / `set_labels` / `remove_from_labels` / `add_to_labels` — rewrite to reference the LabelManager or delete.

10. **Migrate `roles/planner.py`** site: line 294 (the `set_needs_help_label=lambda: bound_host.update_issue_labels(bound_repo_slug, bound_issue_number, add=[TERMINAL_BLOCKING_LABEL], remove=[])` callback inside `handle_unhandled_role_exception`). **The PyGithub fetch MUST stay INSIDE the callback to preserve the runaway-burn recovery boundary.** `handle_unhandled_role_exception` (`roles/__init__.py:154-159`) wraps `set_needs_help_label()` in its own try/except, so a PyGithub-side failure during the recovery path is currently caught and `post_comment` still fires. If the `bound_host.get_issue(...)` fetch were hoisted BEFORE lambda construction (e.g. resolved at `_on_failure` entry), a `get_issue` failure would raise OUT of `_on_failure` BEFORE the helper is invoked, and `post_comment` would also be skipped — degrading the runaway-burn defense from "label fails, comment still posts" to "both fail silently." Resolution: define a small named closure that performs BOTH the fetch and the manager transition together, and pass it as `set_needs_help_label`:

   ```python
   def _set_needs_help_label() -> None:
       bound_issue = bound_host.get_issue(bound_repo_slug, bound_issue_number)
       LabelManager().transition(
           IssueLabelWriter(bound_issue),
           add={Label.NEEDS_HELP},
           remove=set(),
           reason="planner_exception",
       )

   handle_unhandled_role_exception(..., set_needs_help_label=_set_needs_help_label)
   ```

   A PyGithub `get_issue` failure now raises INSIDE the helper's try/except (`roles/__init__.py:154-159`), preserving the documented contract that `post_comment` attempts to fire even when the label write fails. The happy path of `run_planner` is unchanged — the comment at lines 384-398 documenting "Planner writes zero labels on the success path" stays correct; only the exception callback is touched. Planner does NOT need its own `bound_issue` resolved on the happy path.

11. **Update existing role unit tests** to use the new manager-mediated shape. Most role tests today inspect `issue.set_labels(...)` call args via PyGithub mocks; they continue to work since the writer adapter still calls `issue.set_labels` — the test doubles don't need to change. The `tests/reconciler/test_actions.py` fakes DO need the `get_labels` / `set_labels` additions from Sub-request 5.

12. **Add the grep-fence test** `test_label_writes_only_go_through_label_manager` (already drafted in Sub-request 2). This test now passes because every migration is complete.

13. **Run `just check`**. Confirm ruff clean, mypy clean, full pytest green. Record pre-edit + post-edit test counts in the impl PR body. Existing-test pass count MUST remain unchanged. Expected net delta to total test count: `(new tests in test_label_manager.py) − 1` (the `−1` is the explicit deletion of `tests/test_git_hosts_github.py::test_update_issue_labels_removes_then_adds` listed in Sub-request 5 / File-level changes; the migration to `set_labels`-shape fakes is mechanical and adds no net tests). Worker should compute and report the expected delta in the impl PR body, not just assert "match."

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/src/foreman/label_manager.py` | NEW. `LabelWriter` protocol, `LabelManager` class with `transition()`, `_apply_invariants()` pure helper, `IssueLabelWriter` + `ReconcilerHostLabelWriter` adapters, module logger. |
| `packages/foreman/tests/test_label_manager.py` | NEW. Unit tests for invariants, the #303 regression test, the WARNING log assertion, the grep-fence test. |
| `packages/foreman/src/foreman/reconciler/host.py` | Add `get_labels` + `set_labels` to `ReconcilerHost` Protocol (~lines 43-105). |
| `packages/foreman/src/foreman/reconciler/v3_host.py` | Implement `get_labels` + `set_labels` (~lines 473-477 neighborhood). |
| `packages/foreman/src/foreman/daemon_host.py` | Add `set_issue_labels(repo, issue_number, labels: set[str])` method next to lines 83-93 (delegates to `issue.set_labels(*sorted(labels))`). `add_issue_label` / `remove_issue_label` STAY — `daemon.py:99` writes `foreman:failed` via `add_issue_label` directly (separate axis from the reconciler-action label-lifecycle work) and `daemon_runners.py:193-194` uses both for the vestigial v1 paths. Both call paths are explicitly out of scope. |
| `packages/foreman/src/foreman/reconciler/actions.py` | Migrate 8 call sites listed in Sub-request 6 from `host.add_label` / `host.remove_label` to `LabelManager().transition(ReconcilerHostLabelWriter(...), ...)`. |
| `packages/foreman/src/foreman/roles/worker.py` | Migrate 3 `issue.set_labels` sites + 2 exception-callback `issue.add_to_labels` sites to the manager. KEEP the `issue.update()` + `issue.labels` reads at lines 1170-1171 (Worker enumerates `impl-attempt-N` labels from live state). Scrub stale comments referencing `set_labels` / `add_to_labels` / `remove_from_labels`. |
| `packages/foreman/src/foreman/roles/fixer.py` | Migrate 1 `issue.set_labels` site + 1 `issue.add_to_labels(attempt_label)` + 2 exception-callback sites to the manager. KEEP the `issue.update()` + `issue.labels` reads at lines 777-778 (Fixer enumerates `fix-attempt-N` labels from live state). Scrub stale comments. |
| `packages/foreman/src/foreman/roles/reviewer.py` | Migrate 1 `issue.set_labels` site + 1 exception-callback site to the manager. Remove 1 redundant `issue.update()` at line 671 (Reviewer's `removed_foreman` is static — no pattern enumeration). Scrub stale comments. |
| `packages/foreman/src/foreman/roles/planner.py` | Migrate 1 exception-callback site at line 294. Resolve `bound_issue = bound_host.get_issue(bound_repo_slug, bound_issue_number)` inside the exception handler and pass `IssueLabelWriter(bound_issue)` to `LabelManager().transition(add={Label.NEEDS_HELP}, remove=set(), reason="planner_exception")`. Happy path (which writes ZERO labels) unchanged. |
| `packages/foreman/src/foreman/git_host.py` | DELETE the `update_issue_labels` abstract method on `GitHost` (lines 126-134). The Planner is the last caller and migrates to LabelManager. |
| `packages/foreman/src/foreman/git_hosts/github.py` | DELETE the `update_issue_labels` implementation (lines 173-185). |
| `packages/foreman/tests/test_git_host.py` + `packages/foreman/tests/test_roles_exception_handler.py` + `packages/foreman/tests/test_roles_planner.py` | Drop `update_issue_labels` stub methods from each test fake. |
| `packages/foreman/tests/test_git_hosts_github.py` | Delete `test_update_issue_labels_removes_then_adds` (the behavior it exercised is removed). |
| `packages/foreman/tests/reconciler/test_actions.py` | Extend `_FakeHost` (and the `_ProjectCapturingHost`/`_BoomHost` variants) to implement the new `get_labels` / `set_labels` methods. Update tests that asserted on `("add_label", ...)` / `("remove_label", ...)` tuples to assert on `("set_labels", ...)` instead. |

No expected changes to: `packages/foreman/src/foreman/labels.py` (D1 catalog stays as-is); `reconciler/rules.py` (predicates stay; they become correct as a consequence of the rot being gone); `daemon_runners.py` (the legacy v1 `merge_spec_pr`/`merge_impl_pr` paths use vestigial labels `foreman:spec-ready` / `foreman:implementing-ready` / `foreman:ready-for-merge` not in the D1 catalog — those are dead code per the D1 introduction and untouched here per "Out of scope"); `daemon.py:99`'s direct `daemon_host.add_issue_label(repo, n, "foreman:failed")` call (separate axis from the label-lifecycle work — it writes a TERMINAL label outside the reconciler-action surface and is left for a follow-up cleanup PR).

## Alternatives considered

1. **Patch only the leak: add `host.remove_label("foreman:merging-plan")` to every action handler that completes a merge.** Rejected. This fixes #303 specifically but does not eliminate the failure CLASS — the next transitional label added (or the next refactor that introduces one) will reintroduce the same rot. Issue body explicitly cites three instances of the same failure class in two weeks; patching the third without changing the surface area guarantees a fourth.

2. **Make the manager a free function (`transition_label(...)`) instead of a class.** Rejected weakly. The class shape mirrors the D1 catalog (one class, methods that take callers' inputs) and lets the manager hold per-call configuration (e.g. an injected logger or test-mode flag in future PRs). The free-function shape would be functionally equivalent today but harder to extend. The class is one extra line of boilerplate per call site (`LabelManager().transition(...)`); acceptable.

3. **Replace the rule predicates in `rules.py` instead** (e.g. switch `"foreman:merging-plan" not in ctx.issue.labels` to a `not has_stale_in_flight(ctx)` helper that's tolerant of rot). Rejected. This treats the symptom; it's also a state-machine change which the issue body explicitly disallows. The predicates are correctly written — they assume the label set is clean. Make the set clean; don't make the predicates tolerant.

4. **Use import-linter (`importlinter`) for the grep-fence rule instead of a literal-grep pytest.** Rejected for v1. import-linter is a new project dependency and configures via a separate `.importlinter` file; the literal-grep test is 20 lines of Python with zero new deps. If the grep-fence test becomes a maintenance burden or starts catching false positives (e.g. comments mentioning `add_to_labels`), upgrading to import-linter is a one-PR follow-up. v1 keeps it cheap.

5. **Do nothing; document the rot in a runbook.** Rejected. Issue body documents the failure class is recurring on a ~weekly cadence; the autonomous loop spent 30 minutes silent on #303 with no operator-visible diagnostic. The current diagnostic surface IS the problem.

## Open questions

- **QUEUE→IN_FLIGHT additivity.** The invariant table says "writing a QUEUE label strips IN_FLIGHT". But the existing `ADVANCE_LABEL_TO_MERGING_PLAN` flow writes `MERGING_PLAN` (IN_FLIGHT) into an issue that already carries `PLAN_APPROVED` (QUEUE) — the QUEUE label SHOULD survive. Resolution embedded in this spec: the invariant is **"writing QUEUE strips IN_FLIGHT,"** NOT the reverse. Writing IN_FLIGHT next to an existing QUEUE label is the normal transition path and leaves the QUEUE label alone. Worker should verify this asymmetry in the unit tests; if it surfaces a real conflict during implementation, label `foreman:spec-fix`.
- **BLOCKING invariants.** Should writing `BLOCKING` (e.g. `NEEDS_HELP`) strip anything? Today's behavior (actions.py:350-363): no — the in-flight label is the diagnostic. This spec preserves that explicitly: BLOCKING is purely additive, no invariants fire. If the Worker discovers a test that depended on the old strip behavior (there shouldn't be one — actions.py:350 says the strip was removed pre-#307), label spec-fix.

Setting `confidence: medium` per the open questions above being design-level rather than blocking.

## Out of scope

- Changing the D1 catalog (`labels.py`). Classifications + StrEnum shape are correct.
- Changing rule predicates in `rules.py` that read `"foreman:merging-plan" not in ctx.issue.labels`. They become correct as a consequence of the rot fix.
- Redesigning the state machine or adding new label states / label classes.
- Removing the `ReconcilerHost.add_label` / `host.remove_label` methods (after the Sub-request 6 migration these have zero callers in `packages/foreman/src/foreman/`; they become dead code, NOT "still used"). They are kept for one PR to bound the diff scope; removal is a one-line follow-up. The underlying `daemon_host.add_issue_label` / `daemon_host.remove_issue_label` continue to have non-reconciler callers (`daemon.py:99` writes `foreman:failed` directly, and `daemon_runners.py:193-194` uses the vestigial v1 labels), so those remain untouched on a separate axis — see the `daemon_runners.py` "Out of scope" bullet below.
- Cleaning up the vestigial v1 labels (`foreman:spec-ready` / `foreman:implementing-ready` / `foreman:ready-for-merge`) used in `daemon_runners.py:185-255` — those are dead code per the D1 introduction and untouched here.
- Replacing the literal-grep fence test with `import-linter`.
- Touching GitHub App identity routing (`daemon_host.py` identity logic).
- Changing PR-merge logic, dispatch-role logic, or any non-label side effect.

## References

- D1 catalog: `packages/foreman/src/foreman/labels.py`.
- Architecture stability plan: `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md` (D1 is the catalog; this ticket is the runtime that D1 was waiting for).
- Issue #303 — empirical case study, 2026-06-12 stall (`foreman:merging-plan` outliving the merge operation).
- Issue #170 — earlier instance of the same failure class (`plan-approved + needs-help` coexistence).
- Issue #160 — earlier instance (epic + foreman labels overlap).
- Issue #63 — close-out gate (motivates the PR-body-no-closing-keywords rule).
