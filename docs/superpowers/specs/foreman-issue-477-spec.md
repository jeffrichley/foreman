# Spec: hot-reloadable host-mounted project list (issue #477)

## Goal

Move the `[[projects]]` list out of the baked-into-image envsubst template
(`docker/foreman/config.toml.template`) into a host-mounted, operator-edited
`projects.toml` file that the daemon reads at boot and re-reads on
`foreman daemon reload` — making repo adds, renames, and removals a one-file
data edit with no image rebuild or container restart.

Tracks issue [#477](https://github.com/jeffrichley/foreman/issues/477). This is
Half B of the "adding/renaming a repo = edit config + reload, never manual ops"
pair; see #476 for Half A (auto-clone missing checkouts).

## Acceptance criteria

- `load_projects(path: Path) -> list[ProjectConfig]` exists in
  `foreman.v4.config`; reads a standalone TOML file containing only
  `[[projects]]` tables and returns a validated `list[ProjectConfig]`.
- `docker/foreman/config.toml.template` ships zero `[[projects]]` tables.
- `docker/foreman/projects.toml.example` exists, documents the file format,
  and instructs operators how to use it.
- `$FOREMAN_PROJECTS_PATH` (default `/root/.foreman/projects.toml`, baked as
  `ENV` in the `Dockerfile`) is the sole runtime source of the project list.
- The daemon boots with projects from `$FOREMAN_PROJECTS_PATH`; the rendered
  `config.toml` carries zero projects.
- `.env` / envsubst still supplies secrets, App IDs, and operator identity
  (unchanged); only the project list moves.
- `foreman daemon reload` (existing SIGHUP command, unchanged) re-reads
  `$FOREMAN_PROJECTS_PATH`, diffs the result against the current registry,
  adds Pollers/GitProviders for new projects, and removes them for dropped
  projects — no container restart, no image rebuild.
- A renamed repo (name, `repo`, and `local_clone_path` all changed in
  `projects.toml`) is picked up as a remove + add on the next reload.
- Precedence is documented: mounted file is the sole source; template ships
  zero projects.
- `docs/runbooks/managing-projects.md` exists and instructs operators to edit
  `$FOREMAN_PROJECTS_PATH` and run `foreman daemon reload`.
- Tests cover: `load_projects` round-trips from a separate file; reload adds a
  project; reload removes a project.
- `just check` exits zero.

## Approach

**Design principle**: SRP ("single-responsibility") — `config.toml.template`
becomes a secrets/daemon-config file; `projects.toml` becomes the mutable
project registry. The Google "make the right thing easy" principle motivates
the host-mount: the operator's most common task (add/rename a repo) becomes a
local file edit + one command.

**No GoF pattern applies.** This is a config-source split: two concerns that
were conflated in one file are separated into two files with different
ownership models.

**Mount path**: `/root/.foreman/projects.toml` (host: `~/.foreman/projects.toml`).
The `${HOME}/.foreman:/root/.foreman` bind-mount already exists in
`docker-compose.yml` (added for the v5 `V5Config` path) — no new mount entry
needed. The daemon already sees `~/.foreman/` at `/root/.foreman/`.

**Config separation**:

`load_projects(path: Path) -> list[ProjectConfig]` reads a TOML file that
contains only `[[projects]]` tables (same `ProjectConfig` schema as today). It
is called separately from `load_config()`. In
`packages/foreman/src/foreman/v4/cli/__init__.py:main()`, projects are loaded
via `load_projects(projects_path)` and passed to
`bootstrap_cli_context(projects=..., projects_loader=...)`. The
`bootstrap_cli_context` signature gains two optional keyword parameters:
`projects: list[ProjectConfig] | None = None` (the boot-time list, used
instead of `config.projects`) and `projects_loader: Callable[[], list[ProjectConfig]] | None = None`
(a zero-arg callable stored on the Daemon for reload). `V4Config.projects`
field remains and continues to default to `[]`; it is simply not used when
`projects` is passed explicitly. This keeps the `V4Config` Pydantic model
unchanged and keeps existing tests green.

**`foreman init` fix**: `cli/init.py:cmd_init` reads `config.projects` to
look up the target project. After this change `config.projects` is always
empty, so `cmd_init` must also call `load_projects` from `$FOREMAN_PROJECTS_PATH`.

**Boot-time zero-projects guard**: The existing guard in `main()` (lines 206–
210 of `cli/__init__.py`) that refuses to boot with zero projects still applies
— it is updated to check the separately-loaded `projects` list instead of
`config.projects`. The daemon requires at least one project at boot for the
`V4IdentityRegistry.installation_repo` lookup.

**Missing projects file**: If `$FOREMAN_PROJECTS_PATH` does not exist at boot,
`Path.read_text()` raises `FileNotFoundError`, which surfaces as a startup
error. The operator must create the file before starting the daemon.
`projects.toml.example` shows the format.

**SIGHUP reload mechanism**: `cmd_daemon_reload` (unchanged) sends SIGHUP to
the daemon PID. The SIGHUP handler in `cmd_daemon_start` currently calls
`reset_logging() + configure_logging(...)`. For this issue, the handler is
extended to also call `daemon.request_project_reload()` — a new method that
sets a `threading.Event` flag (`_reload_projects_event`). `tick_once()` checks
this flag at the **start** of each invocation (before pollers run), clears it,
and calls `_apply_project_reload()`. Doing the work in `tick_once()` (not in
the signal handler itself) avoids network I/O inside a signal context.

**`_apply_project_reload()` — diff and apply**:

1. Call `self._projects_loader()` to get a fresh `list[ProjectConfig]`.
2. Build current project name set = `set(self._project_configs.keys())`.
3. Build new project name set from the loaded list.
4. **Added** = names in new but not in current, plus names whose `ProjectConfig`
   fields changed (same name, different values — treat as remove + re-add).
5. For each added project: call `self._git_provider_factory(pc.repo)` →
   `GitProvider`; if `self._routing_git is not None`, call
   `self._routing_git.register_provider(name, provider)`; create a `Poller`
   (same arguments as in `bootstrap_cli_context`); call `self._with_qm(new_poller)`
   to wire the shared QueueManager into the poller before appending it; append
   the wired poller to `self._pollers`;
   update `self._project_configs[name] = pc`; call
   `self._clone_refresher.register_project(name, Path(pc.local_clone_path))`.
6. **Removed** = current names not in the new set **plus** current names whose
   `ProjectConfig` differs from the loaded version (same remove-then-re-add
   treatment prescribed in step 4). For each: remove from
   `self._pollers` (filter by `poller._project != name`); if
   `self._routing_git is not None`, call
   `self._routing_git.unregister_provider(name)`; delete
   `self._project_configs[name]`; call
   `self._clone_refresher.unregister_project(name)`.
7. Log a structured INFO entry with added/removed name lists; no-op log
   (`"config reload: no changes"`) when both lists are empty.

**`RoutingGitProvider` mutability**: Add `register_provider(name, provider)` and
`unregister_provider(name)` public methods to `RoutingGitProvider`. The
"defensive copy at construction" semantics remain; the internal `_providers`
dict is now mutated by these two methods. Thread safety: `_apply_project_reload()`
runs on the Daemon's main tick thread; `RoutingGitProvider` is never written
from the WorkerPool threads (they only read via `_resolve`). No lock needed.

**Type narrowing for `RoutingGitProvider` calls**: `Daemon._git` is typed
`GitProvider` (a Protocol with no `register_provider` method); calling
`self._git.register_provider(...)` directly fails mypy (`just check` runs
mypy). Resolution: in `Daemon.__init__`, store a second attribute
`self._routing_git: RoutingGitProvider | None = git if isinstance(git, RoutingGitProvider) else None`
(import `RoutingGitProvider` from `foreman.v4.routing_git_provider`). Inside
`_apply_project_reload()`, call `self._routing_git.register_provider(name, provider)`
and `self._routing_git.unregister_provider(name)`, each guarded by
`if self._routing_git is not None`. When `_routing_git` is `None` (e.g., in
tests that supply a plain `GitProvider` stub), provider registration is a
no-op — this is acceptable because the Daemon boots with a `RoutingGitProvider`
in production, and the sentinel path is the test-isolation case only.

**`CloneRefresher` mutability**: Add `register_project(name, path)` and
`unregister_project(name)` public methods to `CloneRefresher`. The
`_DisabledCloneRefresher` sentinel (used when zero initial projects) should
provide no-op stubs for these methods so `_apply_project_reload()` doesn't need
a type branch. (Note: if the daemon booted with zero projects and thus received
a `_DisabledCloneRefresher`, adding a project via reload will call
`register_project` on the sentinel, which is a no-op — that project's clone
will not be auto-refreshed. This is an acceptable limitation for the zero-boot-
project edge case, which is not supported anyway: the daemon refuses to start
with zero projects.)

**`_build_sighup_handler` extension**: The function in
`packages/foreman/src/foreman/v4/cli/daemon.py` gains a new optional keyword
parameter `daemon: Daemon | None = None`. When provided, the returned handler
also calls `daemon.request_project_reload()`. The call site in
`cmd_daemon_start` passes `daemon=ctx.obj.daemon`. Backward-compatible: tests
and paths that don't supply `daemon=` continue to work unchanged.

## Sub-requests (topologically sorted)

1. Add `load_projects(path: Path) -> list[ProjectConfig]` to
   `packages/foreman/src/foreman/v4/config.py`. Parse the TOML, extract the
   `projects` array-of-tables key, validate each entry as `ProjectConfig` via
   `ProjectConfig.model_validate(...)`, and return the list. Raise
   `FileNotFoundError` (propagated from `Path.read_text`) if the path does
   not exist. Raise `pydantic.ValidationError` on invalid entries (same
   loud-fail contract as `load_config`).

2. Add tests in `packages/foreman/tests/v4/test_config.py`:
   - `test_load_projects_round_trip` — write a two-project TOML, assert both
     `ProjectConfig` instances parse correctly.
   - `test_load_projects_empty` — write an empty TOML (no `[[projects]]`),
     assert an empty list is returned.
   - `test_load_projects_missing_required_field_raises` — omit `name`, assert
     `ValidationError`.
   - `test_load_projects_extra_field_raises` — add unknown key, assert
     `ValidationError` (extra="forbid" propagates).

3. Strip all `[[projects]]` tables from
   `docker/foreman/config.toml.template`. The file should end after the
   `[operator.signer]` block. Add a comment noting that `[[projects]]` now
   lives in `$FOREMAN_PROJECTS_PATH` (default `/root/.foreman/projects.toml`).

4. Create `docker/foreman/projects.toml.example`:
   ```toml
   # Foreman managed projects.
   #
   # Edit this file and run `foreman daemon reload` to add, remove, or rename
   # a tracked repo. No image rebuild or daemon restart needed.
   #
   # Mount path (docker-compose.yml): ${HOME}/.foreman:/root/.foreman
   # Container reads: /root/.foreman/projects.toml  (FOREMAN_PROJECTS_PATH)
   #
   # Copy this file to ~/.foreman/projects.toml and fill in your repos.

   [[projects]]
   name = "myrepo"
   repo = "owner/myrepo"
   local_clone_path = "/foreman/repos/myrepo"

   [[projects]]
   name = "anotherone"
   repo = "owner/anotherone"
   local_clone_path = "/foreman/repos/anotherone"
   ```

5. Add `FOREMAN_PROJECTS_PATH=/root/.foreman/projects.toml` to the `ENV`
   block in `Dockerfile` (alongside the existing `FOREMAN_V4_CONFIG`,
   `FOREMAN_LOG_DIR`, `FOREMAN_STATE_DIR` line).

6. Add `FOREMAN_PROJECTS_PATH=/root/.foreman/projects.toml` to the daemon
   service's `environment:` block in `docker-compose.yml`. Update the
   existing `# v5: bind-mount ~/.foreman ...` comment to also mention
   `projects.toml`.

7. Add `register_provider(name: str, provider: GitProvider) -> None` and
   `unregister_provider(name: str) -> None` public methods to
   `packages/foreman/src/foreman/v4/routing_git_provider.py:RoutingGitProvider`.
   `unregister_provider` raises `UnknownProjectError` if name not found
   (matching the error raised by `RoutingGitProvider._resolve`).

8. Add `register_project(name: str, path: Path) -> None` and
   `unregister_project(name: str) -> None` public methods to
   `packages/foreman/src/foreman/v4/clone_refresh.py:CloneRefresher`. Add
   no-op stubs for both methods on `_DisabledCloneRefresher` so
   `_apply_project_reload()` doesn't need a type check.

9. Extend `packages/foreman/src/foreman/v4/daemon.py:Daemon.__init__` with
   two new keyword-only parameters:
   `projects_loader: Callable[[], list[ProjectConfig]] | None = None` and
   `git_provider_factory: Callable[[str], GitProvider] | None = None`.
   Add the following new instance attributes (after existing assignments):
   `self._reload_projects_event: threading.Event = threading.Event()`;
   `self._routing_git: RoutingGitProvider | None = git if isinstance(git, RoutingGitProvider) else None`
   (import `RoutingGitProvider` from `foreman.v4.routing_git_provider`; this
   stores a narrowed type for the mutating calls in `_apply_project_reload()`
   without widening the public `_git: GitProvider` attribute).
   Add `request_project_reload(self) -> None` (calls
   `self._reload_projects_event.set()`). Add `_apply_project_reload(self) ->
   None` (implements the diff logic from the Approach section above).

10. In `packages/foreman/src/foreman/v4/daemon.py:Daemon.tick_once()`, add at
    the **start** of the method (before pollers run): check
    `self._reload_projects_event.is_set()`; if so, clear it and call
    `self._apply_project_reload()`. The check **must** be in `tick_once()`, not
    in `run_forever()` — sub-request 15's tests call `tick_once()` directly and
    assert the reload has applied; placing the check in `run_forever()` would
    cause all three tests to fail because `tick_once()` would return without
    ever consulting the event.

11. Update `packages/foreman/src/foreman/v4/cli/daemon.py:_build_sighup_handler`
    signature to accept `daemon: Daemon | None = None`. When `daemon` is not
    None, the returned closure also calls `daemon.request_project_reload()`
    after resetting logging. Update the signal install call in
    `cmd_daemon_start` to pass `daemon=ctx.obj.daemon`.

12. Update `packages/foreman/src/foreman/v4/bootstrap.py:bootstrap_cli_context`:
    - Add `projects: list[ProjectConfig] | None = None` and
      `projects_loader: Callable[[], list[ProjectConfig]] | None = None` params.
    - Replace `config.projects` with `active_projects = projects if projects is
      not None else config.projects` at every iteration site (lines 86, 168,
      175).
    - Pass `projects_loader=projects_loader` and
      `git_provider_factory=git_provider_factory` to the `Daemon(...)` constructor.

13. Update `packages/foreman/src/foreman/v4/cli/__init__.py:main()`:
    - Add `_DEFAULT_PROJECTS_PATH = Path.home() / ".foreman" / "projects.toml"`
      alongside the existing `_DEFAULT_CONFIG`.
    - After `load_config(config_path)`: load `projects_path = Path(os.environ.get(
      "FOREMAN_PROJECTS_PATH", str(_DEFAULT_PROJECTS_PATH)))`;
      call `projects = load_projects(projects_path)` (import from
      `foreman.v4.config`).
    - Change the zero-projects guard from `config.projects` to `projects`.
    - Use `projects[0].repo` for `V4IdentityRegistry(installation_repo=...)`.
    - Pass `projects=projects` and
      `projects_loader=lambda: load_projects(projects_path)` to
      `bootstrap_cli_context(...)`.

14. Update `packages/foreman/src/foreman/v4/cli/init.py:cmd_init`:
    - Load `projects_path` from `FOREMAN_PROJECTS_PATH` env (same pattern as
      step 13).
    - Call `load_projects(projects_path)` to get the project list instead of
      `config.projects`.

15. Add tests for Daemon reload in a new file
    `packages/foreman/tests/v4/test_daemon_project_reload.py`:
    - `test_apply_project_reload_adds_project` — construct a Daemon with one
      project; set `_projects_loader` to return two projects; call
      `request_project_reload()` then `tick_once()`; assert the second project's
      Poller is now in `self._pollers` and its config is in `self._project_configs`.
    - `test_apply_project_reload_removes_project` — construct a Daemon with two
      projects; set loader to return only one; trigger reload; assert the removed
      project is absent from `self._pollers` and `self._project_configs`.
    - `test_apply_project_reload_no_changes_logs_info` — loader returns same
      projects; trigger reload; use `caplog` to assert the "no changes" log line;
      assert `_pollers` length unchanged.

16. Create `docs/runbooks/managing-projects.md` — operator runbook. Include:
    "to add a project, append a `[[projects]]` block to
    `~/.foreman/projects.toml` and run `foreman daemon reload`; to rename a
    repo, update `repo` and `local_clone_path` (and optionally `name`) in the
    file and reload; to remove a project, delete its block and reload. In-flight
    tickets for a removed project complete normally; future polls stop
    immediately."

17. Run `just check` and confirm exit zero.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/v4/config.py` | Add `load_projects(path: Path) -> list[ProjectConfig]` |
| `packages/foreman/tests/v4/test_config.py` | Add `test_load_projects_*` tests (4 tests) |
| `docker/foreman/config.toml.template` | Remove all `[[projects]]` tables; add comment pointing to `$FOREMAN_PROJECTS_PATH` |
| `docker/foreman/projects.toml.example` | **Create** — documented example projects file |
| `Dockerfile` | Add `FOREMAN_PROJECTS_PATH=/root/.foreman/projects.toml` to `ENV` block |
| `docker-compose.yml` | Add `FOREMAN_PROJECTS_PATH` to daemon `environment:`; update `.foreman` mount comment |
| `packages/foreman/src/foreman/v4/routing_git_provider.py` | Add `register_provider()` and `unregister_provider()` to `RoutingGitProvider` |
| `packages/foreman/src/foreman/v4/clone_refresh.py` | Add `register_project()` and `unregister_project()` to `CloneRefresher`; add no-op stubs on `_DisabledCloneRefresher` |
| `packages/foreman/src/foreman/v4/bootstrap.py` | Accept `projects:` and `projects_loader:` params; use `active_projects` instead of `config.projects`; pass both to `Daemon` |
| `packages/foreman/src/foreman/v4/daemon.py` | Add `_reload_projects_event`, `_projects_loader`, `_git_provider_factory`; add `request_project_reload()` and `_apply_project_reload()`; check event at tick start |
| `packages/foreman/src/foreman/v4/cli/daemon.py` | Add `daemon:` param to `_build_sighup_handler`; call `daemon.request_project_reload()` on SIGHUP; pass `daemon=ctx.obj.daemon` at install |
| `packages/foreman/src/foreman/v4/cli/__init__.py` | Load `projects` from `$FOREMAN_PROJECTS_PATH`; use for identity setup; pass to `bootstrap_cli_context` |
| `packages/foreman/src/foreman/v4/cli/init.py` | Load projects from `$FOREMAN_PROJECTS_PATH` for `project_cfg` lookup |
| `packages/foreman/tests/v4/test_daemon_project_reload.py` | **Create** — 3 tests for `_apply_project_reload` |
| `docs/runbooks/managing-projects.md` | **Create** — operator runbook |

## Alternatives considered

- **Keep projects in the template; add per-project env-var overrides**: rejected — combinatorial env-var explosion for every project field; does not scale and still requires image rebuild to add a project.
- **Docker secrets or Docker config files for the project list**: rejected — secrets are for credentials, not config; Docker config files carry the same "rebuild or redeploy" requirement as the baked template. The existing `~/.foreman` bind-mount already gives the needed semantics at zero additional infrastructure cost.
- **Reload by fully re-executing `bootstrap_cli_context`**: rejected — destroys in-flight ticket state, closes live role subprocesses, and tears down the EventBus subscriber list. The event-flag + diff approach preserves all live state while adding or removing only the changed projects.
- **Periodic `projects.toml` mtime polling** instead of SIGHUP-triggered reload: rejected — the established operator gesture is `foreman daemon reload` → SIGHUP (see #100 for the v3 precedent). A second polling interval tunable adds config surface for marginal gain. Periodic polling can be layered on top later if demanded.

## Open questions

(none — the issue specifies the accepted design direction, the repo already has the
`~/.foreman` bind-mount and the SIGHUP reload path, and the `ProjectConfig`
model is reused without modification.)

## Out of scope

- Auto-clone of missing `local_clone_path` checkouts on boot or reload — that is #476.
- Any change to how secrets, App IDs, or operator identity are supplied — envsubst + `.env` unchanged.
- Reloading non-project daemon config (tick_seconds, max_in_flight, etc.).
- Hot-reload when the daemon started with zero initial projects (unsupported: daemon refuses to boot with zero projects).
- POSIX permission hardening of `projects.toml` (it contains no secrets).
