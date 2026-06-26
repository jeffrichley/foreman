# Spec: watchtower idle-gate via `foreman gate-update` pre-update hook (issue #412)

## Goal

Prevent watchtower from restarting the foreman daemon while in-flight tickets are being processed. A new CLI command `foreman gate-update` acts as a watchtower pre-update lifecycle hook: it exits 75 (EX_TEMPFAIL) when the board is busy so watchtower defers the restart, exits 0 (allow) when the board is idle, and exits 0 (fail-open) on any error. Two `docker-compose.yml` env/label changes wire the hook to watchtower. A new RUNBOOK subsection documents the gate semantics and the operator escape hatch. Tracks [foreman#412](https://github.com/jeffrichley/foreman/issues/412).

## Acceptance criteria

- [ ] `foreman gate-update` exists and:
  - Exits **75** (EX_TEMPFAIL) when `repo.list_open_tickets()` returns ≥1 ticket.
  - Exits **0** when `repo.list_open_tickets()` returns an empty list.
  - Exits **0** (fail-open) when `repo.list_open_tickets()` raises, AND emits a line containing `"WARNING"` to stderr.
- [ ] Unit tests in `packages/foreman/tests/v4/cli/test_gate_update.py` cover all three exit paths using `InMemoryTicketRepository` (busy and idle cases) and a subclass that raises (error case).
- [ ] `docker-compose.yml` `foreman-daemon` labels block gains: `com.centurylinklabs.watchtower.lifecycle.pre-update: "foreman gate-update"`.
- [ ] `docker-compose.yml` `foreman-watchtower` environment block gains: `WATCHTOWER_LIFECYCLE_HOOKS: "true"`.
- [ ] `docs/RUNBOOK.md` gains a "Watchtower idle-gate" subsection documenting the gate behavior, exit-75 semantics, and the operator escape hatch.
- [ ] `just check` passes (`new_failures_count == 0`).

## Approach

**Pattern naming (per CLAUDE.md Decision 4):** No GoF pattern fits. This is straightforward SRP: `cmd_gate_update` has exactly one responsibility — query the board state and signal to watchtower via exit code. DIP is exercised by depending on the `TicketRepository` Protocol (already used by every other CLI command) rather than on any concrete database driver.

**The hook flow.** Watchtower reads the `com.centurylinklabs.watchtower.lifecycle.pre-update` container label and executes the value as a shell command inside the daemon container before stopping it for a redeploy. Exit-code semantics (verified 2026-06-23 against official docs): only 75 (EX_TEMPFAIL) defers the update and causes watchtower to retry on the next poll cycle. Exit 0 allows the update. Any other non-zero exit (including uncaught crash → exit 1) also allows the update, so the system is fail-open even if `cmd_gate_update` itself crashes before reaching `sys.exit`.

**`cmd_gate_update` structure.** The command mirrors `cmd_ps` in `ps.py`: it reads `ctx.obj.repo` and calls `repo.list_open_tickets()`. No new context fields are needed. The try/except wraps only the repository call so that `typer.Exit(code=75)` (which is itself an `Exception` subclass) is never accidentally caught:

```python
def cmd_gate_update(ctx: typer.Context) -> None:
    try:
        open_tickets = ctx.obj.repo.list_open_tickets()
    except Exception as exc:
        typer.echo(f"WARNING: gate-update check failed ({exc!r}); allowing update (fail-open)", err=True)
        sys.exit(0)
    if open_tickets:
        raise typer.Exit(code=75)
    # Idle — fall through; typer exits 0
```

`sys.exit(0)` is used in the except handler (not `raise typer.Exit`) to guarantee a clean zero exit regardless of typer's exception-handling machinery. When the function returns normally, typer exits 0.

**Registration.** `cmd_gate_update` is imported into `foreman.v4.cli` and registered with `app.command("gate-update")(cmd_gate_update)` alongside the existing query commands (`ps`, `show`, `queue`).

**`docker-compose.yml` changes.** Two minimal, surgical additions:
1. `foreman-daemon` labels block (currently line 59): add `com.centurylinklabs.watchtower.lifecycle.pre-update: "foreman gate-update"` alongside the existing scope label.
2. `foreman-watchtower` environment block: add `WATCHTOWER_LIFECYCLE_HOOKS: "true"`. Without this env var, watchtower ignores all lifecycle labels.

**RUNBOOK addition.** A new "Watchtower idle-gate" subsection under "Image lifecycle (auto-rebuild)" documents: what the hook does, the exit-75/0 semantics, and the operator escape hatch (remove the `pre-update` label from the running container via `docker label rm` or, simplest, `docker compose stop daemon && docker compose up -d daemon` after temporarily removing the label from `docker-compose.yml`).

## Sub-requests (topologically sorted)

1. **Create `packages/foreman/src/foreman/v4/cli/gate_update.py`**: implement `cmd_gate_update` with the three exit paths described in Approach. Import `sys` and `typer`. Access `ctx.obj.repo`. Wrap only `repo.list_open_tickets()` in try/except. Emit `typer.echo(..., err=True)` on error. Use `sys.exit(0)` in except handler; `raise typer.Exit(code=75)` when busy; fall through (implicit exit 0) when idle.

2. **Modify `packages/foreman/src/foreman/v4/cli/__init__.py`**: add `from foreman.v4.cli.gate_update import cmd_gate_update` to the import block (alongside `from foreman.v4.cli.ps import cmd_ps`); add `app.command("gate-update")(cmd_gate_update)` to the query-command registration block (alongside `app.command("ps")(cmd_ps)`).

3. **Modify `docker-compose.yml`**: in the `daemon:` service's `labels:` block, add `com.centurylinklabs.watchtower.lifecycle.pre-update: "foreman gate-update"` below the existing `com.centurylinklabs.watchtower.scope: foreman` label. In the `watchtower:` service's `environment:` block, add `WATCHTOWER_LIFECYCLE_HOOKS: "true"` below `WATCHTOWER_SCOPE: "foreman"`.

4. **Modify `docs/RUNBOOK.md`**: add a "### Watchtower idle-gate" subsection at the end of the "Image lifecycle (auto-rebuild)" section, before the horizontal rule that opens the next section.

5. **Create `packages/foreman/tests/v4/cli/test_gate_update.py`**: three tests — `test_gate_update_busy_exits_75`, `test_gate_update_idle_exits_0`, `test_gate_update_error_exits_0_and_warns`.

## File-level changes

| File | Change |
|------|--------|
| `packages/foreman/src/foreman/v4/cli/gate_update.py` | **Create.** `cmd_gate_update` — three exit-code paths as described in Approach. |
| `packages/foreman/src/foreman/v4/cli/__init__.py` | **Modify.** Import `cmd_gate_update`; register `app.command("gate-update")(cmd_gate_update)`. |
| `docker-compose.yml` | **Modify.** Add `pre-update` label to `daemon` service; add `WATCHTOWER_LIFECYCLE_HOOKS` env to `watchtower` service. |
| `docs/RUNBOOK.md` | **Modify.** Add "Watchtower idle-gate" subsection under "Image lifecycle (auto-rebuild)". |
| `packages/foreman/tests/v4/cli/test_gate_update.py` | **Create.** Three unit tests covering busy=75, idle=0, error=0+warning. |

## Alternatives considered

- **Use `list_in_flight_state_instances()` instead of `list_open_tickets()`.** `list_in_flight_state_instances()` is the narrower "a role subprocess is actively dispatched" signal the issue mentions at line ~83 of `repository.py`. Ruled out in favor of `list_open_tickets()`: the issue's Approach section explicitly specifies `list_open_tickets()` as the gate condition ("non-empty (any in-flight/non-terminal ticket)"), which is the broader and safer signal — a ticket in `Planning` or `Implementing` state but not yet actively dispatched still represents queued work that would be disrupted by a mid-run restart.
- **Graceful drain / SIGTERM handler.** Wire the daemon to finish in-flight work on SIGTERM before allowing watchtower to replace it. Ruled out: explicitly listed as "Out of scope" in the issue (separate defense-in-depth follow-up ticket). The idle-gate is the MVP solution.
- **Shell script hook instead of a CLI command.** Add a standalone `/foreman/scripts/gate-update.sh` shell script that queries postgres directly. Ruled out: coupling the hook to postgres schema bypasses the `TicketRepository` abstraction, creates a second place to maintain the "what counts as non-terminal" logic, and cannot be tested with the existing `InMemoryTicketRepository` infrastructure.

## Open questions

None. The approach is fully grounded in verified code: `repository.py` (Protocol + `list_open_tickets`), `ps.py` (access pattern for `ctx.obj.repo`), `__init__.py` (registration pattern), `docker-compose.yml` (exact line numbers for labels + env), and the existing CLI test pattern in `test_query_commands.py`.

## Out of scope

- Changing `image.yml` or the every-push-to-main build trigger.
- Graceful-drain / SIGTERM-checkpoint / resumable in-flight attempts (separate follow-up).
- Deploying the change (`docker compose up -d` to apply the new label + watchtower env) — operator-driven post-merge.
- Changing the role-subprocess or state-machine code paths.
- Adding a `--verbose` flag or structured log output to `gate-update` (a simple WARNING line is sufficient for the fail-open path).
