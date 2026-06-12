# Spec: per-poll clone auto-fetch refresh strategy (issue #291)

## Goal

The Foreman container's local clone at `/foreman/repos/<project>` never
fetches from origin between role dispatches. As external commits land on
origin, the local `origin/<default-branch>` ref grows stale until a role
dispatch happens to need that specific branch's ref refreshed. This spec
adds a per-poll fetch step in the v3 `Reconciler.tick()` loop and
introduces a `CloneRefreshStrategy` Protocol so the refresh policy is a
substitutable concern. The existing per-dispatch fetch in
`WorktreeManager.create()` / `create_impl()` is kept as defense-in-depth.
See issue [#291](https://github.com/jeffrichley/foreman/issues/291). Same
failure class as foreman#279 (D9 retarget guard): silent autonomous-loop
divergence from real upstream state.

## Acceptance criteria

- A new `CloneRefreshStrategy` `typing.Protocol` lives in a new file
  `packages/foreman/src/foreman/reconciler/clone_refresh.py`. It defines
  a single method:

  ```python
  def refresh(self, project: "ReconcilerProject") -> None: ...
  ```

  The method is best-effort and never raises into the reconciler (any
  exception is caught and logged at WARNING by the strategy
  implementations — the daemon must not crash on transient network
  errors).

- Two concrete strategies ship in the same file:
  - `OnPollFetch` (the new default behavior) — calls
    `foreman.worktree.fetch_origin_default_branch(clone_path)` per
    `refresh()` call. Best-effort: a non-zero return from the underlying
    fetch is logged at WARNING and swallowed.
  - `OnDispatchFetchOnly` (preserves today's behavior, kept for tests +
    explicit opt-out) — implements `refresh()` as a pure no-op.

- A new public helper
  `foreman.worktree.fetch_origin_default_branch(clone_path: pathlib.Path,
  *, role_token: str | None = None) -> None` is added to
  `packages/foreman/src/foreman/worktree.py`. It composes the existing
  private helpers (`_resolve_default_branch` to read
  `origin/HEAD`-derived default-branch name, then `_fetch_origin_branch`
  with `--prune` semantics for the resolved name). Same best-effort
  contract as `_fetch_origin_branch` — logs and returns on failure
  rather than raising. No existing private helper signatures are
  changed.

- `_fetch_origin_branch` (`worktree.py:657`) is updated to include
  `--prune` in its `git fetch` argv so refs deleted upstream are
  evicted locally. Existing tests for the helper's
  `couldn't-find-remote-ref` recovery path (`test_worktree.py:89`) stay
  green — the prune flag does not change rc=128-on-missing-ref
  behavior.

- `packages/foreman/src/foreman/reconciler/daemon.py`'s `ReconcilerProject`
  frozen dataclass gains a new optional field
  `local_clone_path: str = ""`. Empty string means "no clone path known
  for this project, skip refresh." Tests that construct
  `ReconcilerProject` without specifying `local_clone_path` keep
  passing — the field defaults are preserved.

- `Reconciler.__init__` accepts a new optional kwarg
  `clone_refresh_strategy: CloneRefreshStrategy | None = None`. When
  `None`, the default is `OnPollFetch()` (production-safe out of the
  box). When the caller passes an explicit strategy (tests, paranoid
  operators), it is used as-is.

- `Reconciler.tick()` calls
  `self._clone_refresh_strategy.refresh(project)` at the TOP of the
  per-project loop body, BEFORE the
  `fetch_project_state(project=project.name, ...)` call. Ordering
  rationale: the snapshot fetch is GraphQL-only, but role dispatches
  emitted later in the same tick may worktree-add from the clone — the
  fresh `origin/<default>` ref must be in place before any dispatch.

- A new config knob
  `ReconcilerConfig.auto_fetch_on_poll: bool = Field(default=True, ...)`
  is added to `packages/foreman/src/foreman/config.py`. CLI wiring in
  `_build_v3_gh_and_host` (or its caller in `daemon_v3_start`) reads
  the flag and constructs the strategy accordingly: `True` →
  `OnPollFetch()`, `False` → `OnDispatchFetchOnly()`. The selected
  strategy is then passed to `Reconciler(...)`.

- `_build_reconciler_projects` (`cli.py:527`) is updated so each
  `ReconcilerProject` carries `local_clone_path=proj_cfg.local_clone_path`
  from the corresponding `ProjectConfig`. The split-and-validate logic
  for `repo` is untouched.

- Regression test 1 (default strategy fires per tick):
  `packages/foreman/tests/reconciler/test_clone_refresh.py::test_on_poll_fetch_invokes_refresh_per_project_per_tick`.
  Construct a `Reconciler` with two `ReconcilerProject`s pointing at
  two on-disk bare-repo clones (created via `subprocess.run` in test
  setup, same pattern as `test_worktree.py:1737`). Inject a
  `_StubGHClient` returning an empty snapshot. Run `await
  reconciler.tick()` once. Assert `git fetch` ran once per project
  against the project's clone (captured via a stubbed
  `CloneRefreshStrategy` that records each `refresh(project)` call —
  the test asserts on the recorded project tuple). The empty snapshot
  ensures no dispatches happen so the only fetch is the per-poll one.

- Regression test 2 (per-dispatch fetch is preserved — defense in
  depth): `packages/foreman/tests/test_worktree.py::test_create_still_fetches_base_branch_per_dispatch`.
  Already implicitly covered by the existing
  `test_create_fetches_origin_before_branching` (grep
  `_fetch_origin_branch` in `test_worktree.py` for the existing
  fixture pattern). Add an explicit assertion that the per-poll path
  does NOT short-circuit the per-dispatch path — call
  `WorktreeManager.create(...)` and verify the `git fetch origin
  <base>` subprocess call still fires regardless of whether
  `OnPollFetch` is selected at reconciler scope.

- Regression test 3 (silent on network failure): construct an
  `OnPollFetch` strategy against a clone whose `origin` URL points at
  a non-existent local path. Call `strategy.refresh(project)`. Assert
  the call returns without raising AND that a WARNING-level log row
  was emitted (use `caplog` fixture with `level=logging.WARNING`).

- Regression test 4 (`OnDispatchFetchOnly` is a no-op): construct the
  strategy, call `refresh(project)` with a project whose clone path
  points at a non-existent directory. The call MUST NOT touch the
  filesystem, run any subprocess, or raise. This pins the
  opt-out-strategy contract so a future "improvement" to make it also
  fetch silently is impossible without breaking this test.

- Regression test 5 (`tick()` does not crash when strategy raises):
  inject a `CloneRefreshStrategy` whose `refresh()` raises
  `RuntimeError`. Call `await reconciler.tick()`. Assert the tick
  completes (does not propagate the error) AND that the per-project
  snapshot fetch still happens (the strategy failure must not block
  the rest of the tick). This pins the "best-effort, must not crash
  the daemon" contract at the reconciler boundary, separate from each
  strategy's internal try/except — so a future strategy that forgets
  to catch its own exception still doesn't take the daemon down.

- The CLI smoke test for `daemon v3-start --max-ticks=0`
  (`test_cli.py` — grep for `v3_start` / `v3-start`) continues to
  pass without modification. The new config knob has a default that
  keeps the smoke-test wiring exit-zero.

- `just check` exits 0 on the impl worktree: lint clean, mypy clean,
  full pytest suite green.

- The impl PR title uses a `feat(...)` or `fix(...)` conventional-commit
  type (e.g., `fix(reconciler): auto-fetch project clones per poll`).
  Subject must NOT start with an uppercase letter per `CLAUDE.md:36`.
  The impl PR body references issue #291 plainly (NO closing-keyword
  references per foreman#63 — use phrasing like "addresses #291" or
  "for issue #291").

## Approach

This spec maps cleanly to the Strategy GoF pattern (named explicitly
per `CLAUDE.md`'s Decision-4 calibrated lens): the "when does the
local clone refresh" decision is a substitutable policy, with two
concrete implementations on day one (`OnPollFetch` and
`OnDispatchFetchOnly`) and a clear extension seam for future
strategies (e.g., `EventDrivenFetch` triggered by GitHub webhooks, an
`AdaptiveFetch` that backs off on observed quiescence). Choosing
Strategy over a localized-predicate change is justified by two
properties present here: (a) the refresh behavior is a CONFIGURABLE
policy decision (operators in air-gapped environments may want the
on-dispatch-only behavior); (b) the test surface is materially
cleaner when the policy is a substitutable Protocol — we test
"strategy fires per tick" and "strategy is best-effort" as separate
contracts rather than baking both into a single test against the
reconciler loop.

Decision 4's "make the right thing easy" Google principle also fits:
the DEFAULT `OnPollFetch` means a fresh `foreman daemon v3-start` is
safe out of the box. The paranoid operator opts INTO the older
`OnDispatchFetchOnly` shape (matching the cap=1 max-concurrent-
dispatches principle elsewhere in `ReconcilerConfig`).

The new public helper `fetch_origin_default_branch` in
`worktree.py` reuses the existing `_resolve_default_branch` (which
already handles the `origin/HEAD`-missing fallback with the right
warning logs) and the existing `_fetch_origin_branch` (which already
has the `couldn't-find-remote-ref` self-heal path from foreman#122).
Keeping these private and composing them in a thin public wrapper
preserves the existing helper contracts — the strategy is a
client of the existing fetch infrastructure, not a parallel
implementation.

The `--prune` flag on `_fetch_origin_branch` is a small additive
change: today it fetches a specific branch and tolerates rc=128;
with `--prune`, it ALSO evicts upstream-deleted refs from the
local clone, which is the contract the issue asks for ("at most
one poll cycle behind"). The existing rc=128-on-missing-ref
recovery path is unaffected — `git fetch --prune origin <branch>`
still returns 128 with `couldn't find remote ref` for a deleted
upstream branch, and the existing `update-ref -d` self-heal still
fires.

The `ReconcilerProject` field addition (`local_clone_path: str =
""`) is purely additive — the empty-string default means existing
tests that construct `ReconcilerProject(name=..., owner=..., repo=...)`
keep passing untouched, and `OnPollFetch.refresh(project)` treats
the empty string as "no clone path → skip" (logged at DEBUG, not
WARNING, because in test setups that's the expected case).

Wiring in `cli.py`: `_build_reconciler_projects` (which is already
the single source of truth for the ReconcilerProject tuple, called
both at startup AND from the reload callback) gets one new line
threading `local_clone_path` through from `ProjectConfig`. The
reload path inherits the change for free.

`Reconciler.tick()` calls the strategy at the TOP of the per-project
loop body BEFORE `fetch_project_state`. The strategy is wrapped in
a try/except at the reconciler boundary (defense-in-depth: even if
a future strategy forgets its own try/except, the daemon doesn't
crash). A logged WARNING + continue-to-next-step is the contract.

Per the issue's "Out of scope" list, this spec deliberately does
NOT:
- Refresh feature branches beyond the default branch
  (`fetch_origin_default_branch` resolves one ref; the per-dispatch
  path in `WorktreeManager` still handles the explicit base-branch
  case).
- Replace GraphQL polling with git-based polling — the strategy
  fires alongside the existing GraphQL observer, not as a
  replacement.
- `git pull` anything — fetch-only is the contract.

## Sub-requests (topologically sorted)

1. **Add `--prune` to `_fetch_origin_branch` in `worktree.py:674-681`.**
   The existing rc=0 / rc=128 / rc=other branches are unaffected
   semantically — `--prune` only ADDS upstream-deleted-ref eviction.
   Confirm the existing
   `test_fetch_origin_branch_prunes_stale_ref_when_remote_branch_is_gone`
   (`test_worktree.py:89`) still passes — the new prune flag is
   complementary to the manual `update-ref -d` self-heal there.

2. **Add public helper `fetch_origin_default_branch` to
   `worktree.py`.** Signature:

   ```python
   def fetch_origin_default_branch(
       clone_path: Path,
       *,
       role_token: str | None = None,
   ) -> None:
       """Best-effort refresh of ``origin/<default-branch>``.

       Resolves the default-branch name via
       :func:`_resolve_default_branch` (which handles ``origin/HEAD``
       missing by falling back to ``"main"``), then calls
       :func:`_fetch_origin_branch` against it. Same best-effort
       contract as the underlying helper: network failures are
       logged as warnings and swallowed.

       Used by :class:`OnPollFetch` (foreman#291) to keep the
       container clone's ``origin/<default>`` ref fresh between role
       dispatches.
       """
       default = _resolve_default_branch(clone_path, role_token=role_token)
       _fetch_origin_branch(clone_path, default, role_token=role_token)
   ```

3. **Create
   `packages/foreman/src/foreman/reconciler/clone_refresh.py`** with the
   `CloneRefreshStrategy` Protocol + `OnPollFetch` +
   `OnDispatchFetchOnly` classes. Concrete code:

   ```python
   """Refresh strategy for project clones between role dispatches.

   foreman#291: the container's clone at /foreman/repos/<project>
   never auto-fetches, so ``origin/<default-branch>`` goes stale
   between role dispatches. ``OnPollFetch`` (the default) refreshes
   it once per poll cycle. ``OnDispatchFetchOnly`` preserves the
   pre-#291 behavior for paranoid environments / tests.

   Strategy GoF pattern, per CLAUDE.md's Decision-4 calibrated lens:
   the refresh policy is a substitutable concern. Google's "make the
   right thing easy" also applies — the default produces the safer
   behavior out of the box.
   """

   from __future__ import annotations

   import logging
   from pathlib import Path
   from typing import TYPE_CHECKING, Protocol

   from foreman.worktree import fetch_origin_default_branch

   if TYPE_CHECKING:
       from foreman.reconciler.daemon import ReconcilerProject

   logger = logging.getLogger(__name__)


   class CloneRefreshStrategy(Protocol):
       def refresh(self, project: "ReconcilerProject") -> None: ...


   class OnPollFetch:
       """Refresh ``origin/<default>`` once per poll cycle, per project.

       Best-effort: catches and logs (WARNING) every exception so the
       Reconciler's tick continues even on transient network or
       filesystem trouble.
       """

       def refresh(self, project: "ReconcilerProject") -> None:
           if not project.local_clone_path:
               logger.debug(
                   "clone refresh skipped for project=%s: no local_clone_path",
                   project.name,
               )
               return
           try:
               fetch_origin_default_branch(Path(project.local_clone_path))
           except Exception as exc:
               logger.warning(
                   "clone refresh failed for project=%s clone=%s: %s",
                   project.name,
                   project.local_clone_path,
                   exc,
               )


   class OnDispatchFetchOnly:
       """No-op per-poll refresh. Defense-in-depth + paranoid opt-out.

       Selecting this strategy preserves the pre-#291 behavior: the
       per-dispatch fetch in :class:`WorktreeManager` is the ONLY
       source of clone refresh.
       """

       def refresh(self, project: "ReconcilerProject") -> None:  # noqa: ARG002
           return
   ```

4. **Add `local_clone_path: str = ""` to `ReconcilerProject`** in
   `packages/foreman/src/foreman/reconciler/daemon.py:114`. Preserve
   the existing field order (`name`, `owner`, `repo` first); new
   field comes after `merge_mechanism` with the default
   so positional construction in tests stays valid. Docstring picks
   up a note: empty string is "no clone known; skip per-poll
   refresh."

5. **Wire the strategy into `Reconciler`** in
   `packages/foreman/src/foreman/reconciler/daemon.py:142`:

   - `__init__` gains
     `clone_refresh_strategy: CloneRefreshStrategy | None = None`.
   - Default fallback in `__init__` body:
     `self._clone_refresh_strategy = clone_refresh_strategy or OnPollFetch()`.
   - Import at the top of `daemon.py`:
     `from foreman.reconciler.clone_refresh import (CloneRefreshStrategy, OnPollFetch)`.
   - In `tick()`'s per-project loop, BEFORE the
     `fetch_project_state` call, add a try/except-wrapped call:

     ```python
     try:
         self._clone_refresh_strategy.refresh(project)
     except Exception:
         logger.exception(
             "clone_refresh_strategy raised for project=%s; "
             "continuing tick (best-effort contract)",
             project.name,
         )
     ```

     This is the reconciler-boundary defense: even a misbehaving
     strategy must not crash the daemon.

6. **Add `auto_fetch_on_poll: bool = Field(default=True, ...)`** to
   `ReconcilerConfig` in `packages/foreman/src/foreman/config.py:162`.
   Description should reference foreman#291 and explain: True (the
   default) → per-poll fetch refreshes `origin/<default>` for each
   registered project; False → skip per-poll fetch and rely on the
   per-dispatch fetch only. Same "operator opts out for paranoid /
   air-gapped environments" framing as `max_concurrent_dispatches=1`.

7. **Thread `local_clone_path` into `_build_reconciler_projects`** in
   `packages/foreman/src/foreman/cli.py:540`. One new kwarg added to
   the `ReconcilerProject(...)` constructor inside the loop:
   `local_clone_path=proj_cfg.local_clone_path`. No other changes to
   the function.

8. **Construct + pass the strategy in `daemon_v3_start`** in
   `packages/foreman/src/foreman/cli.py:750`. Before constructing
   the `Reconciler`, add:

   ```python
   from foreman.reconciler.clone_refresh import OnDispatchFetchOnly, OnPollFetch
   clone_refresh_strategy = (
       OnPollFetch() if config.reconciler.auto_fetch_on_poll else OnDispatchFetchOnly()
   )
   ```

   Then pass `clone_refresh_strategy=clone_refresh_strategy` to
   `Reconciler(...)`.

9. **Write the five regression tests** named in Acceptance criteria,
   landing in
   `packages/foreman/tests/reconciler/test_clone_refresh.py` (new
   file) and an additional assertion in `tests/test_worktree.py`. Use
   the `subprocess`-based bare-repo fixture pattern from
   `test_worktree.py:1737-1774` for tests that need a real on-disk
   clone; use a fake `CloneRefreshStrategy` (a small dataclass that
   records each `refresh()` call) for tests that just need to
   observe scheduling, not actual git work.

10. **Run `just check`.** Lint, mypy, full pytest suite green.

11. **Verify the dogfood path manually after merge** (out of band of
    the Worker's automation): once landed, rebuild the container,
    land an external commit on origin/main, and confirm via `docker
    compose exec daemon bash -c "cd /foreman/repos/foreman && git
    log origin/main --oneline -1 && git ls-remote origin main"` that
    local `origin/main` matches actual upstream within one poll
    cycle. This step is operator-driven, not Worker-automatable.

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/src/foreman/worktree.py` | Add `--prune` to `_fetch_origin_branch`'s argv (line 675). Add new public helper `fetch_origin_default_branch(clone_path, *, role_token=None)` near `_fetch_origin_branch`. No other helpers modified. |
| `packages/foreman/src/foreman/reconciler/clone_refresh.py` | NEW FILE. Defines `CloneRefreshStrategy` Protocol + `OnPollFetch` + `OnDispatchFetchOnly` concrete strategies. |
| `packages/foreman/src/foreman/reconciler/daemon.py` | (a) Add `local_clone_path: str = ""` to `ReconcilerProject`. (b) Import `CloneRefreshStrategy` + `OnPollFetch` from `clone_refresh`. (c) `Reconciler.__init__` accepts `clone_refresh_strategy` kwarg, defaults to `OnPollFetch()`. (d) `tick()`'s per-project loop calls `self._clone_refresh_strategy.refresh(project)` (wrapped in try/except + logger.exception) BEFORE the `fetch_project_state` call. |
| `packages/foreman/src/foreman/config.py` | Add `auto_fetch_on_poll: bool = Field(default=True, ...)` to `ReconcilerConfig`. Description references foreman#291. |
| `packages/foreman/src/foreman/cli.py` | (a) `_build_reconciler_projects`: pass `local_clone_path=proj_cfg.local_clone_path` to `ReconcilerProject(...)`. (b) `daemon_v3_start`: construct `OnPollFetch()` or `OnDispatchFetchOnly()` based on `config.reconciler.auto_fetch_on_poll`, pass it as `clone_refresh_strategy=` to `Reconciler(...)`. |
| `packages/foreman/src/foreman/reconciler/__init__.py` | Export `CloneRefreshStrategy`, `OnPollFetch`, `OnDispatchFetchOnly` at the package boundary so tests + CLI can import from `foreman.reconciler` directly. Add the names to `__all__`. |
| `packages/foreman/tests/reconciler/test_clone_refresh.py` | NEW FILE. Five regression tests per Acceptance criteria. |
| `packages/foreman/tests/test_worktree.py` | Optional additive assertion confirming the per-dispatch fetch path still fires (defense-in-depth coverage). |

No expected changes to:

- `packages/foreman/src/foreman/reconciler/actions.py`, `rules.py`,
  `exec_log.py`, `outcomes.py`, `observer.py`, `state.py`, `host.py`,
  `v3_host.py`. The strategy is invoked at the reconciler-tick layer
  and does not interact with the action / rule / host surfaces.
- `packages/foreman/src/foreman/roles/`. Role subprocesses are
  downstream consumers of the refreshed clone; they don't see the
  strategy directly.
- `docker-compose.yml`, `docker/foreman/config.toml.container`. The
  default-True knob means the container picks up the new behavior
  with no config change.
- `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md`.
  This spec is the Phase 2 #1 ticket the plan ranks; the plan itself
  stays the source of truth and is not modified by the impl PR.

## Alternatives considered

- **Localized predicate (no Strategy): add `git fetch origin --prune`
  inline at the top of `Reconciler.tick()`'s per-project loop.**
  Smaller diff (~10 lines vs ~150 lines), one fewer module, no
  Protocol. Rejected — the issue body explicitly calls out the
  refresh policy as a configurable concern (`auto_fetch_on_poll`
  flag), which collapses to "if/else around two predicate calls"
  without the Strategy. That branch lives inside the reconciler's
  hottest loop and creates a localized hook point that future
  refresh strategies (event-driven, adaptive) would have to retrofit
  AROUND. The Strategy seam now is the cheaper-future-cost answer
  per Lens B.

- **Filesystem watcher / inotify on the clone.** Out of scope: this
  watches the LOCAL clone's filesystem, not upstream GitHub. The
  failure mode is "upstream advanced; local hasn't fetched" — a
  filesystem watcher catches nothing here.

- **GitHub webhook → daemon push notification.** Architecturally
  cleaner (sub-second freshness, no polling at all), but requires:
  webhook receiver wiring in the daemon, a public-internet endpoint
  or smee.io tunnel for development, secret management for webhook
  signature verification, and a fallback for ad-hoc CLI invocations
  where no webhook fires. Two orders of magnitude more scope than
  this issue asks for. Filed mentally as a Phase 5 visibility
  upgrade; not blocking on the operational fix this spec ships.

- **Drop the per-dispatch fetch in `WorktreeManager.create`** once
  the per-poll path is in place. Tempting (DRY), but rejected per
  the issue's AC ("Existing per-dispatch fetch in
  `WorktreeManager.create()` continues to fire (defense-in-depth;
  not removed)"). The per-dispatch path catches the case where a
  role dispatch is requested between two ticks AND the in-tick poll
  fetch happened to miss the relevant ref (e.g., a dev_base_branch
  override pointing at a non-default branch the per-poll fetch
  doesn't touch).

- **Refresh ALL refs (`git fetch --all --prune`) instead of just the
  default branch.** Rejected per the issue's "Out of scope" line:
  "Refresh of project-specific feature branches (only the default
  branch + the per-dispatch base-branch needed)". A `--all` fetch
  also incurs proportional cost on repos with many feature branches
  (voice, agent_core), so the cheaper targeted refresh wins on cost
  too.

- **Make `OnDispatchFetchOnly` the default and `OnPollFetch` opt-in.**
  Rejected — directly contradicts the "make the right thing easy"
  Google principle. The failure mode this issue exists to fix is
  the DEFAULT producing silent drift; the safer default must be the
  new shipped behavior. Operators with reasons to opt out (no
  outbound network, throttled CI, etc.) can flip the flag.

- **Do nothing; rely on daily container rebuild** (Phase 6 ritual in
  the architecture stability plan). Rejected — daily rebuild
  doesn't bound the staleness window below 24h, and the empirical
  evidence in the issue body is 4h of drift in a freshly-rebuilt
  container. The autonomous loop cannot rely on the operator's
  rebuild cadence to maintain correctness.

## Open questions

(None — the issue body specifies the contract (60s freshness, prune,
fetch-only, default-branch-only), the codebase has clear extension
seams at `ReconcilerProject` + `Reconciler.__init__`, the
worktree-helper composition is straightforward, and the Strategy
pattern fit is the calibrated answer per CLAUDE.md's Decision-4
lens.)

## Out of scope

- **D9-style retarget logic.** Separate concern; shipped as
  foreman#279 / PR #280. This spec is the Phase 2 #1 ticket per the
  architecture stability plan; D9 was the Phase 2 #2 ticket and is
  done.
- **Refreshing project-specific feature branches.** Only the default
  branch + the per-dispatch base-branch (via the kept
  `WorktreeManager` fetch) are refreshed. Feature-branch staleness
  is not the failure mode this issue addresses.
- **Replacing GraphQL polling with git-based polling.** The observer
  layer is untouched.
- **Adding `git pull` or any working-tree mutation** to the strategy.
  Fetch-only is the contract — `git pull` would mutate the clone's
  working tree and could collide with in-flight worktree adds.
- **Refresh strategies beyond `OnPollFetch` / `OnDispatchFetchOnly`.**
  The Protocol is designed to admit them (e.g., `EventDrivenFetch`,
  `AdaptiveFetch`), but day-one only ships the two concrete
  strategies the issue names.
- **Removing the per-dispatch fetch in `WorktreeManager.create()`.**
  Kept as defense-in-depth per the issue's explicit AC.
- **Modifying `docker-compose.yml` or
  `docker/foreman/config.toml.container`.** The new config knob
  defaults to `True` so the container picks up the new behavior
  with no config change.
- **Updating the architecture stability plan doc.** The plan stays
  the source of truth; merged-PR + CHANGELOG carry the propagation
  status (same pattern as foreman#286 / D4 propagation).

## References

- foreman#291 — this ticket. Surfaces the silent drift failure mode.
- foreman#279 / PR #280 — D9 retarget guard, same failure class
  (silent autonomous-loop divergence from real upstream state).
- foreman#122 — origin-PR-merged-and-deleted self-heal in
  `_fetch_origin_branch`; the `--prune` flag added here is
  complementary to that path.
- `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md`
  line 150 — Phase 2 #1 ticket, which this spec lands.
- `CLAUDE.md` (repo root) — Decision-4 calibrated lens; this spec
  names Strategy + "make the right thing easy" per the calibration.
- Source pointers used by this spec:
  - `packages/foreman/src/foreman/worktree.py:43` — `ensure_clone`
    (idempotent first-run helper; not modified by this spec).
  - `packages/foreman/src/foreman/worktree.py:222-254` — existing
    per-dispatch fetch in `WorktreeManager.create()`; preserved as
    defense-in-depth.
  - `packages/foreman/src/foreman/worktree.py:657-718` — existing
    `_fetch_origin_branch` helper; gains `--prune` and is composed
    into the new public `fetch_origin_default_branch`.
  - `packages/foreman/src/foreman/reconciler/daemon.py:114-137` —
    `ReconcilerProject` dataclass; gains one new field.
  - `packages/foreman/src/foreman/reconciler/daemon.py:142-460` —
    `Reconciler`; gains a strategy field + per-tick refresh call.
  - `packages/foreman/src/foreman/reconciler/v3_host.py` — host
    layer is NOT the injection point (per `V3GitHubHost`'s
    project-agnostic design); the strategy lives at the
    Reconciler scope instead.
  - `packages/foreman/src/foreman/cli.py:527-560` —
    `_build_reconciler_projects` (the single source of truth for
    `ReconcilerProject` construction, called both at startup AND
    from the reload callback); gains one new kwarg.
  - `packages/foreman/src/foreman/cli.py:750-768` — `Reconciler`
    construction site; gains the strategy kwarg.
  - `packages/foreman/tests/test_worktree.py:89` — existing test
    for the `couldn't-find-remote-ref` recovery path; the
    `--prune` addition must keep this green.
  - `packages/foreman/tests/test_worktree.py:1737-1774` —
    bare-repo fixture pattern reused in the new
    `test_clone_refresh.py`.
