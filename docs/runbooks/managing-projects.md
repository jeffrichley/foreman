# Managing projects (add, rename, remove)

**Issue #477** — the `[[projects]]` list is no longer baked into the
envsubst template. It lives in a host-mounted file that the daemon reads at
boot and re-reads on `foreman daemon reload`, so repo adds, renames, and
removals are a one-file edit with no image rebuild or container restart.

## File location

| Context | Path |
|---|---|
| Host | `~/.foreman/projects.toml` |
| Container (via bind-mount) | `/root/.foreman/projects.toml` |
| Env var | `FOREMAN_PROJECTS_PATH` (default `/root/.foreman/projects.toml`) |

The `${HOME}/.foreman:/root/.foreman` bind-mount in `docker-compose.yml`
makes host edits immediately visible to the running container.

See `docker/foreman/projects.toml.example` for the file format.

## Adding a project

1. Append a `[[projects]]` block to `~/.foreman/projects.toml`:

   ```toml
   [[projects]]
   name = "newrepo"
   repo = "owner/newrepo"
   local_clone_path = "/foreman/repos/newrepo"
   ```

2. Run `foreman daemon reload`.

The daemon reads the updated file on the next tick and creates a Poller +
GitProvider for the new project. Combined with issue #476 (auto-clone
missing checkouts), the new checkout is cloned automatically if
`local_clone_path` does not exist yet.

## Renaming a repo

When a GitHub repo is renamed (e.g. `owner/old` → `owner/new`):

1. Update `repo` and `local_clone_path` (and optionally `name`) in
   `~/.foreman/projects.toml`.
2. Run `foreman daemon reload`.

The daemon treats a changed `ProjectConfig` as a remove + re-add: the old
Poller is stopped and a new one is created for the updated config. The
2026-07-02 `voice` → `madrigal` rename would have been a 30-second edit
with this mechanism in place.

## Removing a project

1. Delete the project's `[[projects]]` block from
   `~/.foreman/projects.toml`.
2. Run `foreman daemon reload`.

> **Warning for removals**: `_apply_project_reload()` calls
> `routing_git.unregister_provider(name)` synchronously. Any WorkerPool
> thread concurrently executing a state-machine transition for the removed
> project will raise `UnknownProjectError` on its next GitHub API call
> (`add_labels`, `merge_pr`, etc.), causing that ticket to fail rather than
> complete normally. For safe removal, wait until no tickets for the project
> are in-flight before reloading (check the dashboard or allow the current
> tick to finish). Future polls stop immediately after reload.

## How `foreman daemon reload` works

`foreman daemon reload` sends `SIGHUP` to the daemon process. The SIGHUP
handler:

1. Resets and reconfigures logging (unchanged behavior).
2. Calls `daemon.request_project_reload()`, which sets a
   `threading.Event` flag.

On the next `tick_once()`, the daemon clears the flag and calls
`_apply_project_reload()`:

1. Reads a fresh `list[ProjectConfig]` from `$FOREMAN_PROJECTS_PATH`.
2. Diffs the new list against the current registry.
3. Removes Pollers and GitProviders for dropped projects.
4. Adds Pollers and GitProviders for new projects.
5. Logs the added/removed names (or "no changes" when identical).

The reload is non-destructive: in-flight ticket transitions complete
normally. Only future polls are affected.
