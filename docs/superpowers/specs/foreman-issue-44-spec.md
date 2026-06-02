# Spec: rewire orchestrator-bot token through IdentityRegistry (issue #44)

## Goal

Eliminate the daemon's one-shot orchestrator-bot token by routing the
orchestrator through `IdentityRegistry` — the same per-role token cache
+ refresh path the planner / reviewer / fixer / worker bots use. After
this lands, all five Foreman App identities share one token-management
code path, the daemon survives past the 1-hour installation-token TTL,
and `daemon_host.py`'s parallel `build_orchestrator_github_client` mint
function is deleted.

Tracks issue [#44](https://github.com/jeffrichley/foreman/issues/44).

## Acceptance criteria

- `packages/foreman/src/foreman/identity.py`:
  - `IdentityRegistry.__init__` gains a keyword-only parameter
    `orchestrator: OrchestratorConfig | None = None`. Default `None`
    preserves the existing per-role-only call sites.
  - `IdentityRegistry._resolve_role_credentials` recognizes the
    `"orchestrator"` role and returns
    `(orchestrator.resolve_app_id(), orchestrator.resolve_private_key_path())`.
    Requesting the `"orchestrator"` role when `orchestrator is None`
    raises `ValueError` with a clear "registry was not constructed with
    orchestrator config" message.
  - The class's docstring (lines 7-22) is extended to list
    `orchestrator` alongside the four role bots and to note the
    orchestrator is global (one App installation, valid across every
    repo in the installation) while role bots are per-project.
  - Two convenience accessors mirror the role-bot accessor pair:
    - `get_orchestrator_client(self) -> Github`
    - `get_orchestrator_token(self) -> str`
- `packages/foreman/src/foreman/daemon_host.py`:
  - `GitHubDaemonHost.__init__` changes from
    `(*, github_client: Github)` to
    `(*, identity_registry: IdentityRegistry)`. The instance stores
    the registry on `self._registry`.
  - Every method on `GitHubDaemonHost` that previously used `self._gh`
    instead calls `self._registry.get_orchestrator_client()` once at
    the top of the method and uses the returned client.
    Methods touched: `search_foreman_labeled_issues`,
    `add_issue_label`, `remove_issue_label`, `post_issue_comment`,
    `get_issue_labels`, `close_issue`, `find_pr_for_branch`,
    `merge_pull_request`, `get_pr_base_ref`,
    `is_pr_merged_for_branch`, `retarget_pr_base`,
    `get_default_branch`. (Every method on the class.)
  - The module-level `build_orchestrator_github_client` function is
    deleted. The module docstring (lines 1-11) is updated to describe
    the registry-backed flow.
- `packages/foreman/src/foreman/cli.py:_resolve_host_and_runners`:
  - No longer calls `build_orchestrator_github_client` or imports it.
  - Constructs the registry once:
    `registry = IdentityRegistry(first_project, orchestrator=config.orchestrator)`
    where `first_project` is the first value in `config.projects`.
  - Constructs the host as
    `host = GitHubDaemonHost(identity_registry=registry)`.
  - The "orchestrator not configured" guard still raises before
    constructing the registry (using
    `config.orchestrator.resolve_app_id()` /
    `resolve_private_key_path()` to surface the same `RuntimeError`
    today's code catches). The fallback to `_build_null_host_and_runners()`
    on missing orchestrator config is preserved verbatim.
- `packages/foreman/tests/test_identity.py` gains an "Orchestrator role
  accessors" section mirroring the planner/reviewer/fixer/worker
  blocks, with these tests (all use the existing `monkeypatch`
  pattern + `_make_project()` helper, plus a new `_make_orchestrator()`
  helper that produces an `OrchestratorConfig` with
  `app_id=222222`, `private_key_path="/tmp/orchestrator.pem"`):
  - `test_get_orchestrator_client_returns_github_instance`.
  - `test_get_orchestrator_token_returns_installation_token_string`.
  - `test_get_orchestrator_mint_invoked_with_orchestrator_app_credentials`
    asserting `mint_installation_token` is called with the orchestrator
    App id, key path, and the project's repo slug (the first-repo
    convention for installation lookup).
  - `test_orchestrator_env_var_overrides_config_file_app_id`
    setting `FOREMAN_ORCHESTRATOR_APP_ID` and asserting the env value
    wins. (This re-uses `OrchestratorConfig.app_id_env`, the env-var
    precedence rule already shipped.)
  - `test_orchestrator_client_and_planner_client_share_no_cache`
    proving per-role cache isolation extends to the orchestrator —
    no cross-contamination of installation tokens.
  - `test_orchestrator_role_raises_when_no_orchestrator_config_passed`
    constructing an `IdentityRegistry(project)` (no orchestrator arg)
    and asserting `reg.get_orchestrator_client()` raises `ValueError`
    with a message mentioning "orchestrator config".
  - `test_orchestrator_mint_called_again_when_token_near_expiry` —
    this is the regression test the issue explicitly asks for. Use
    `side_effect=[near_expiry, fresh]` exactly as in the existing
    `test_mint_called_again_when_token_near_expiry` (line 86) but for
    the orchestrator role. Asserts the second `get_orchestrator_token`
    returns the refreshed token and `mint_installation_token` was
    called twice.
- `packages/foreman/tests/test_daemon_host.py`:
  - `_make_host_with_repo(repo)` is rewritten to construct a fake
    `IdentityRegistry` (a `MagicMock`) whose `get_orchestrator_client`
    returns a `Github` `MagicMock` whose `get_repo` returns the
    supplied `repo`. The function continues to return a
    `GitHubDaemonHost`. Every existing test that uses this helper
    continues to pass unchanged.
  - A new test
    `test_each_api_call_asks_registry_for_fresh_orchestrator_client`
    constructs a host with a `MagicMock` registry, makes two host API
    calls (e.g., `add_issue_label` then `close_issue`), and asserts
    `registry.get_orchestrator_client` was called at least twice —
    one per API call. This guards against accidental regressions to
    "cache the client on the host" and proves every method asks the
    registry, which is where the refresh logic lives.
  - A new test
    `test_host_uses_refreshed_client_after_registry_refresh` uses two
    distinct `Github` `MagicMock`s (`old_client`, `new_client`) on the
    registry's `get_orchestrator_client.side_effect`, calls one host
    method, then calls another, and asserts the second call used
    `new_client.get_repo` (not `old_client.get_repo`). This is the
    behavior-level proof that token rollover propagates to the
    daemon's next API call.
- `packages/foreman/src/foreman/__init__.py` and any other code that
  imports `build_orchestrator_github_client`: scan and remove the
  import + any remaining call site. Grep verifies zero hits for
  `build_orchestrator_github_client` after the change.
- `docs/superpowers/specs/2026-06-01-foreman-daemon-design.md` gains
  a short subsection — three or four paragraphs — describing the
  orchestrator's new token-management path: the registry is the
  single source of truth, the orchestrator role joins the four role
  bots in `_resolve_role_credentials`, and the daemon's host adapter
  asks the registry for a client per API call so refresh transparently
  applies. The subsection notes the 1-hour-daemon-death bug
  (2026-06-02) as the motivating failure.
- `just check` exits zero (lint + typecheck + tests).

## Approach

The fix is structural: the orchestrator IS a GitHub App identity, same
shape as the role bots, and the only reason it gets its own one-shot
mint code path is historical accident. The issue's framing — "all
GitHub App identities go through IdentityRegistry, period" — is the
right invariant. We make the registry the canonical token-management
seam, then delete the parallel mint path.

`IdentityRegistry` today lives in
`packages/foreman/src/foreman/identity.py`. Its constructor takes a
`ProjectConfig`, its `_resolve_role_credentials` dispatcher knows four
roles (`planner`, `reviewer`, `fixer`, `worker`), each backed by
`project.apps.resolve_<role>_app_id()` /
`resolve_<role>_private_key_path()`. The cache + refresh logic in
`_get_cached` (lines 147-157) — mint if missing, mint if within
`_REFRESH_SAFETY_SECONDS = 300` of expiry, otherwise return the
cached client — is exactly what the orchestrator needs but currently
doesn't get.

There is one structural difference between the orchestrator and the
role bots: the role bots are per-project (their credentials live on
`project.apps`), while the orchestrator is global (its credentials
live on `config.orchestrator`, top-level). The orchestrator's
installation token is valid for every repo in the App's installation;
the role bots' installation tokens are still scoped per-installation
but the registry semantics are per-project. We bridge by adding a
keyword-only `orchestrator: OrchestratorConfig | None = None`
parameter to `IdentityRegistry.__init__`, then teaching
`_resolve_role_credentials` to dispatch the `"orchestrator"` role
against it. The repo slug used for the GitHub installation-id lookup
is `project.repo` — the same "first repo" convention the CLI uses
today. The resulting installation token is valid across all repos in
the installation, so the host can call `get_repo("jeffrichley/voice")`
or `get_repo("jeffrichley/agent-core")` with the same token. This
matches the existing semantics of `build_orchestrator_github_client`
(which also uses one "first repo" for the lookup).

On the host side, `GitHubDaemonHost` (in `daemon_host.py`) currently
stores a pre-built `Github` client in `self._gh` and uses it for
every PyGithub call. The refresh logic lives in `IdentityRegistry`,
so the host's natural seam is "ask the registry for a fresh client on
every API call". We change `__init__` to take an
`IdentityRegistry` reference, store it as `self._registry`, and have
every public method call `self._registry.get_orchestrator_client()`
to retrieve the current cached-or-refreshed client. The PyGithub
client is cheap to call (the cache lives below it, in the registry),
and asking on every API call is the simplest invariant — the host
never holds stale state. This matches the role-side pattern: the
role functions also call `registry.get_<role>_client()` per role run
rather than holding the client across runs.

The CLI's `_resolve_host_and_runners` (cli.py:395-448) is the only
external constructor of `GitHubDaemonHost`. It gets a one-line
adjustment: build an `IdentityRegistry(first_project, orchestrator=config.orchestrator)`
and pass it to `GitHubDaemonHost(identity_registry=registry)`. The
existing guard for "orchestrator not configured" stays in place (we
call `config.orchestrator.resolve_app_id()` before constructing the
registry so the same `RuntimeError` surfaces at the same site, with
the same operator-facing warning). The `_build_null_host_and_runners()`
fallback is unchanged.

The registry's caching is process-lifetime: the daemon constructs one
registry at startup and uses it for the entire process. The cache
survives across host API calls and across role dispatches. This is
the existing behavior of `IdentityRegistry`; extending it to the
orchestrator gets us the same lifetime for free.

We don't take the alternative of "fix the orchestrator path
in-place" — adding refresh logic to `build_orchestrator_github_client`
or to `GitHubDaemonHost` directly. Doing so leaves two parallel
token-management code paths and re-introduces the same drift risk
that motivated this ticket. The architectural invariant the issue
asks for — one path for all App identities — only holds if there's
literally one path.

We also don't take the alternative of restructuring `IdentityRegistry`
to take a top-level `Config` instead of `ProjectConfig`. The role
bots are genuinely per-project (each project can have different App
credentials in principle), and changing the constructor surface
would force every existing call site
(`planner.py:143`, `reviewer.py:301`, `fixer.py:397`, `worker.py:464`,
plus the `test_identity.py` `_make_project()` helper) to change. The
keyword-only `orchestrator` parameter is additive and leaves the
per-role-only call sites untouched.

The regression test the issue asks for —
`test_orchestrator_mint_called_again_when_token_near_expiry` — sits
in `test_identity.py` because that's where the existing refresh test
(`test_mint_called_again_when_token_near_expiry`, line 86) lives.
We mock at the `mint_installation_token` boundary (the same boundary
the existing tests mock at), construct a near-expiry token followed
by a fresh one via `side_effect`, and assert the second call mints
a new token. The two `test_daemon_host.py` tests
(`test_each_api_call_asks_registry_for_fresh_orchestrator_client`
and `test_host_uses_refreshed_client_after_registry_refresh`) cover
the host-level invariant: every method routes through the registry,
and a registry refresh propagates to the next API call.

## Sub-requests (topologically sorted)

1. In `packages/foreman/src/foreman/identity.py`:
   - Import `OrchestratorConfig` from `foreman.config`.
   - Extend `IdentityRegistry.__init__` signature:
     ```python
     def __init__(
         self,
         project: ProjectConfig,
         *,
         orchestrator: OrchestratorConfig | None = None,
     ) -> None:
         self._project = project
         self._orchestrator = orchestrator
         self._cache: dict[str, _CachedClient] = {}
         self._app_meta_cache: dict[str, AppMetadata] = {}
     ```
   - Add an `"orchestrator"` branch to `_resolve_role_credentials`:
     ```python
     if role == "orchestrator":
         if self._orchestrator is None:
             raise ValueError(
                 "Orchestrator role requested but registry was not "
                 "constructed with orchestrator config. Pass "
                 "orchestrator=config.orchestrator at construction time."
             )
         return (
             self._orchestrator.resolve_app_id(),
             self._orchestrator.resolve_private_key_path(),
         )
     ```
   - Add the convenience accessors at the bottom of the role-accessor
     block:
     ```python
     def get_orchestrator_client(self) -> Github:
         """Return the PyGithub client authenticated as the
         orchestrator bot.

         The daemon's host adapter uses this client for every API
         call — label management, PR merging, polling search, issue
         close. Asking on every call lets the registry's 5-minute-
         pre-expiry refresh transparently propagate.
         """
         return self.get_client("orchestrator")

     def get_orchestrator_token(self) -> str:
         """Return the orchestrator bot's current installation token."""
         return self.get_token("orchestrator")
     ```
   - Update the class docstring (lines 7-22) and the "Walking skeleton
     wires" note (line 21) so the orchestrator is documented alongside
     the role bots, with a callout that it's global (one App
     installation, valid across every repo in the installation).
2. Add orchestrator-section tests to
   `packages/foreman/tests/test_identity.py` after the worker block
   (around line 397):
   - Add `_make_orchestrator()` helper at the top of the file (near
     `_make_project()`):
     ```python
     from foreman.config import OrchestratorConfig

     def _make_orchestrator() -> OrchestratorConfig:
         return OrchestratorConfig(
             app_id_env="FOREMAN_ORCHESTRATOR_APP_ID",
             app_id=222222,
             private_key_path="/tmp/orchestrator.pem",
         )
     ```
   - Add the seven orchestrator tests listed in the acceptance
     criteria. Each test constructs the registry as
     `IdentityRegistry(_make_project(), orchestrator=_make_orchestrator())`
     and follows the same shape as the planner/reviewer/fixer/worker
     tests. The "raises when no orchestrator config" test omits the
     `orchestrator=` kwarg.
3. Run the new identity tests in isolation
   (`uv run pytest packages/foreman/tests/test_identity.py -k orchestrator -v`)
   and confirm they pass before touching the host.
4. In `packages/foreman/src/foreman/daemon_host.py`:
   - Update the module docstring (lines 1-11) to describe the
     registry-backed flow: the host holds an `IdentityRegistry`
     reference and asks for a fresh orchestrator client per API call;
     the registry handles caching + refresh.
   - Add the import: `from foreman.identity import IdentityRegistry`.
   - Change `GitHubDaemonHost.__init__`:
     ```python
     def __init__(self, *, identity_registry: IdentityRegistry) -> None:
         self._registry = identity_registry
     ```
   - In every public method, replace the opening line
     `repo_obj = self._gh.get_repo(repo)` (or equivalent) with:
     ```python
     gh = self._registry.get_orchestrator_client()
     repo_obj = gh.get_repo(repo)
     ```
     Methods to touch (each is a one- or two-line edit at the top):
     `search_foreman_labeled_issues`, `add_issue_label`,
     `remove_issue_label`, `post_issue_comment`, `get_issue_labels`,
     `close_issue`, `find_pr_for_branch`, `merge_pull_request`,
     `get_pr_base_ref`, `is_pr_merged_for_branch`, `retarget_pr_base`,
     `get_default_branch`.
   - Delete the module-level
     `build_orchestrator_github_client` function (lines 33-44) and
     the now-unused `mint_installation_token` import (line 21).
5. Update `packages/foreman/tests/test_daemon_host.py`:
   - Rewrite `_make_host_with_repo(repo)`:
     ```python
     def _make_host_with_repo(repo) -> GitHubDaemonHost:
         gh_client = MagicMock()
         gh_client.get_repo = MagicMock(return_value=repo)
         registry = MagicMock()
         registry.get_orchestrator_client = MagicMock(return_value=gh_client)
         return GitHubDaemonHost(identity_registry=registry)
     ```
     All existing tests pass without further changes — they only
     interact through the host's public API.
   - Add the two new host-level tests
     (`test_each_api_call_asks_registry_for_fresh_orchestrator_client`
     and `test_host_uses_refreshed_client_after_registry_refresh`)
     per the acceptance criteria. Both construct a `MagicMock`
     registry directly (no `_make_host_with_repo` helper) so they
     can assert on `registry.get_orchestrator_client` call counts
     and `side_effect` behavior.
6. Run the `daemon_host` tests in isolation
   (`uv run pytest packages/foreman/tests/test_daemon_host.py -v`)
   and confirm they pass.
7. Update `packages/foreman/src/foreman/cli.py:_resolve_host_and_runners`
   (lines 395-448):
   - Remove `build_orchestrator_github_client` from the import line
     (line 411).
   - Add `from foreman.identity import IdentityRegistry` to the
     function-scoped imports (the existing imports are function-
     scoped to keep the CLI module import-light).
   - Keep the `try/except RuntimeError` guard that calls
     `config.orchestrator.resolve_app_id()` /
     `resolve_private_key_path()` so the same "orchestrator not
     configured" warning fires at the same site. (The registry won't
     mint until the first API call, so without this guard the
     daemon would start "successfully" and only fail later. We want
     to fail fast at startup.)
   - Build the registry and host:
     ```python
     first_project = next(iter(config.projects.values()))
     registry = IdentityRegistry(
         first_project,
         orchestrator=config.orchestrator,
     )
     host = GitHubDaemonHost(identity_registry=registry)
     ```
   - Delete the `app_id`, `key_path`, `first_repo`, and `gh_client`
     locals that were only used to feed `build_orchestrator_github_client`.
   - Leave the `worktrees_root` + `DaemonRunners` construction
     untouched.
8. Run the CLI tests
   (`uv run pytest packages/foreman/tests/test_cli.py -v`) and confirm
   nothing regresses. If a CLI test mocks
   `build_orchestrator_github_client`, update the mock target to
   `foreman.identity.IdentityRegistry` or to
   `foreman.identity.mint_installation_token` (whichever boundary the
   test was probing). Grep first to find any such mock.
9. Grep the entire repo for
   `build_orchestrator_github_client` and confirm zero hits:
   `uv run rg build_orchestrator_github_client packages/`. If any hit
   remains (including in test files or docs), remove or update it.
10. Update
    `docs/superpowers/specs/2026-06-01-foreman-daemon-design.md` with
    a short subsection (three or four paragraphs) titled "Orchestrator
    token management" describing: the registry-as-canonical-path
    framing, the 1-hour-daemon-death bug (2026-06-02) that motivated
    it, the orchestrator role joining the four role bots in
    `_resolve_role_credentials`, and the host's
    "ask-the-registry-per-call" invariant.
11. Run `just check` and confirm exit zero. Fix any lint /
    typecheck / test failures inline. Common spots to watch:
    - The `IdentityRegistry` import in `daemon_host.py` creates a
      new dependency edge that mypy may flag; verify no cycle.
    - Ruff may complain about the unused `mint_installation_token`
      import in `daemon_host.py` after the function deletion — that's
      the deletion's job to clean up.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/identity.py` | Extend `IdentityRegistry.__init__` with keyword-only `orchestrator: OrchestratorConfig \| None = None`. Add `"orchestrator"` branch in `_resolve_role_credentials` raising `ValueError` if the orchestrator config is absent. Add `get_orchestrator_client()` + `get_orchestrator_token()` convenience methods. Update class docstring to document the orchestrator alongside the role bots and note its global (cross-repo) scope. Import `OrchestratorConfig`. |
| `packages/foreman/src/foreman/daemon_host.py` | Change `GitHubDaemonHost.__init__` to accept `identity_registry: IdentityRegistry` instead of `github_client: Github`. Every public method asks `self._registry.get_orchestrator_client()` for a fresh client at the top of the method. Delete the module-level `build_orchestrator_github_client` function and remove the `mint_installation_token` import. Update the module docstring to describe the registry-backed flow. |
| `packages/foreman/src/foreman/cli.py` | In `_resolve_host_and_runners`: remove the `build_orchestrator_github_client` import + call, build an `IdentityRegistry(first_project, orchestrator=config.orchestrator)` and pass it to `GitHubDaemonHost(identity_registry=registry)`. Preserve the existing "orchestrator not configured" guard + null-host fallback at the same site. |
| `packages/foreman/tests/test_identity.py` | Add `_make_orchestrator()` helper, add an "Orchestrator role accessors" test section with seven tests covering the existence + caching + refresh + env-var-precedence + cache-isolation + missing-config-raises invariants. The refresh test (`test_orchestrator_mint_called_again_when_token_near_expiry`) is the regression test the issue asks for. |
| `packages/foreman/tests/test_daemon_host.py` | Rewrite `_make_host_with_repo` to construct a fake `IdentityRegistry` whose `get_orchestrator_client` returns the Github client. Existing tests pass unchanged. Add two new tests: one asserting every host API call asks the registry for a fresh client, one asserting a refreshed client (via `side_effect`) propagates to the next host call. |
| `docs/superpowers/specs/2026-06-01-foreman-daemon-design.md` | Append a short "Orchestrator token management" subsection (three or four paragraphs) describing the registry-as-canonical-path invariant, the 1-hour-death bug that motivated it (2026-06-02), and the host's per-call refresh pattern. |

## Alternatives considered

- **Patch refresh into `build_orchestrator_github_client` / `GitHubDaemonHost` in place**, keeping the two parallel token-management code paths. Rejected: this directly contradicts the issue's architectural framing ("one code path for all App identities") and re-introduces the same drift risk that produced the bug. The whole point of the fix is the deduplication; a local patch defeats it.
- **Restructure `IdentityRegistry` to take a top-level `Config` instead of `ProjectConfig`.** Rejected: this is a larger refactor that touches every existing call site (`planner.py:143`, `reviewer.py:301`, `fixer.py:397`, `worker.py:464`, plus four test setup helpers in `test_identity.py`). The additive `orchestrator: OrchestratorConfig | None = None` parameter delivers the same end-state with no churn on the role-bot path.
- **Construct a second `IdentityRegistry` instance per-project for the orchestrator** (one orchestrator-registry per project). Rejected: the orchestrator's installation token is global (one App installation, valid across every repo in the installation). Per-project registries would mint redundant tokens and waste rate limit. One registry, one orchestrator cache entry, used across every project the daemon serves.
- **Move the host's caching invariant from "ask the registry per call" to "ask once at host construction and trust the registry to mutate the client in place".** Rejected: `IdentityRegistry` constructs a *new* `Github` client when it refreshes (line 154), it doesn't mutate the old one. Holding the client on the host would freeze the original (expired) instance even after the registry refreshed. Asking per call is the cheap, correct invariant and matches how role functions use the registry (each role run calls `registry.get_<role>_client()` afresh).
- **Add token-rotation-on-401-retry inside `IdentityRegistry`** alongside the expiry-based refresh. Out of scope — the issue explicitly defers it ("Token rotation on revocation (separate concern; current refresh handles expiry only)") and the existing 5-minute safety margin handles ordinary expiry.
- **Do nothing — operator restarts the daemon every hour.** Rejected: the failure mode is silent (the daemon doesn't crash; it goes inert with auth errors that look like transient GitHub flakes), the recovery is manual, and the bug exists in this codebase right now. The issue was caught by accident (overnight test); a production deployment would be worse.

## Open questions

(none — the fix is structural and the code paths are well-mapped. The registry's existing per-role caching pattern, the role-bot convenience accessors, and the existing refresh test (`test_mint_called_again_when_token_near_expiry`) all transfer cleanly to the orchestrator role. The host adapter's per-call registry lookup matches how role functions already use the registry. The CLI's "first project's repo" convention for installation lookup is preserved verbatim.)

## Out of scope

- Per-project bot identity overrides (foreman#17). The orchestrator stays global to its single configured App installation; this ticket does not introduce per-project orchestrator credentials.
- Token rotation on revocation. The 5-minute pre-expiry refresh handles expiry; webhook-driven rotation on App credential revocation is a separate concern.
- Daemon process-level resilience: memory leaks, file-descriptor leaks, watchdog timers, etc. The issue is scoped to identity/auth only.
- Switching the role bots' registries to also share one process-wide instance (today each role function constructs its own `IdentityRegistry(project)` per role run). The role-bot path is already correct under refresh because each role run is short-lived; daemon-process-wide pooling is a separate optimization.
- Migrating `IdentityRegistry` to take a top-level `Config` instead of `ProjectConfig`. Additive-parameter approach delivers the same end-state without churning the role-bot call sites.
- Introducing a metric / log event for token mint count or refresh frequency. The existing test coverage on `mint_installation_token` call count is sufficient for v1; production telemetry is a follow-up.
- Updating `foreman init` to provision the orchestrator App credentials (currently a manual one-time setup step). Adjacent concern; covered by the existing init code path that already reads `config.orchestrator`.
- Refactoring or relocating `_HostLike` Protocol declarations. The host's public method signatures are unchanged; the Protocol shapes in `daemon.py:23-26` and `daemon_runners.py:49-63` continue to match.
- Changing the merge-action semantics in `daemon_runners.py` (`merge_spec_pr` / `merge_impl_pr`). They continue to call `self._host.merge_pull_request(...)` exactly as today; the host's internal client lookup is the only change.
