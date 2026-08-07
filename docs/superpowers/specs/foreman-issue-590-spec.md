# Spec: derive GitHub webhook coverage from `projects.toml` and flag gaps (issue #590)

## Goal

Close the silent coverage gap documented in [#590](https://github.com/jeffrichley/foreman/issues/590): every project in `~/.foreman/projects.toml` is daemon-managed but the inbound GitHub webhook set is a separate, hand-maintained registry that drifts silently. The fix has two parts: (1) add an `[inbound]` config block so foreman knows the canonical webhook receiver URL, and (2) surface any project-without-webhook as a loud, actionable signal at daemon startup and via `foreman doctor`.

## Acceptance criteria

- `V4Config` accepts an optional `[inbound]` TOML block with a `receiver_url` string field; configs without the block load cleanly (backward-compatible).
- `foreman doctor` includes a `webhook-coverage` check that, for each project in `projects.toml`, verifies an active GitHub webhook exists whose `config.url` matches `config.inbound.receiver_url`.
- When `[inbound]` is absent or `receiver_url` is empty, the `webhook-coverage` check prints `SKIPPED` and exits 0 (same pattern as `image-fresh` / no `IMAGE_SHA`).
- When any project lacks an active matching webhook, `foreman doctor` prints a `MISSING` line naming the project and repo, exits 1.
- When all projects have an active matching webhook, prints `OK` for each, exits 0.
- On `GithubException` (network error, 403 insufficient permissions, 404 repo not found), the check prints `WARN` and exits 0 (not 1) — consistent with the `image-fresh` network-failure policy.
- `bootstrap_cli_context` logs `ERROR`-level for each project missing the inbound webhook (when `config.inbound` is set), using the orchestrator token already minted for the clone loop.
- `foreman init <project>` installs the webhook when `config.inbound.receiver_url` is set — idempotent: if an active matching webhook already exists it reports "existed"; if created it reports "created".
- `docker/foreman/config.toml.template` gains a commented-out `[inbound]` example block so operators know the key exists.
- `just check` exits zero.
- No existing tests regress.

## Approach

**Pattern (Decision 4):** No GoF pattern fits. The applicable Google engineering principle is **"make the right thing easy"** — specifically its corollary, **"make the wrong thing loud"**. The root cause in the issue is that a hand-maintained copy of a registry always drifts silently; the fix derives coverage from the single authoritative source (`projects.toml`) on every startup and `doctor` invocation, so absence becomes a visible signal rather than an invisible gap.

### New config block: `InboundConfig`

Add a new Pydantic model `InboundConfig` to `packages/foreman/src/foreman/v4/config.py`, parallel to the existing optional `SandboxConfig`. It carries a single field:

```toml
[inbound]
receiver_url = "https://your.tailscale-funnel-or-public-url/webhook"
```

`V4Config` gains `inbound: InboundConfig | None = Field(default=None)`. The `load_config` parser adds `if "inbound" in raw: payload["inbound"] = raw["inbound"]` — same pattern as `[sandbox]` and `[backup]`.

### `foreman doctor` check

`packages/foreman/src/foreman/v4/cli/doctor.py` already has the `_check_image_fresh` pattern: a pure function, zero arguments, reads the environment, prints a status line, returns an exit code. The new `_check_webhook_coverage(config, projects, *, github_factory)` follows the same shape but takes the config and project list as parameters (for testability) rather than loading them internally. `cmd_doctor` is extended to load config + projects (mirroring `cmd_init`'s load pattern), catch load failures gracefully, and pass the results to the new check. If the config load itself fails, the check is SKIPPED with WARN.

For each project, the function calls `Github(auth=Auth.Token(token)).get_repo(slug).get_hooks()` and looks for a hook with `hook.active and hook.config.get("url") == receiver_url`. Network / auth errors produce WARN + exit 0; a confirmed absent hook produces MISSING + exit 1.

### Daemon startup log

`bootstrap_cli_context` in `packages/foreman/src/foreman/v4/bootstrap.py` already mints `orch_token` for the clone loop. After the clone loop completes, if `config.inbound` is set, a new helper checks each project's webhooks using the same `orch_token` and calls `logger.error(...)` for any project missing the inbound hook. This matches the "report loudly" directive — an ERROR-level log entry is visible in the daemon's structured log stream and in `docker compose logs`.

### Webhook installation in `foreman init`

`packages/foreman/src/foreman/v4/cli/init.py` already has `admin_client` (an orchestrator PyGithub client). If `config.inbound` is set, `cmd_init` calls `_ensure_webhook(client, repo_slug, receiver_url)` after the label step. This is idempotent: scan existing hooks for a matching URL and skip creation if found, creating only if absent. The summary printout gains a "Webhook:" line (installed / existed / skipped — no inbound URL configured).

### Permission requirement (open question)

The orchestrator GitHub App needs **`administration: read`** permission to call `GET /repos/{owner}/{repo}/hooks`, and **`administration: write`** to call `POST /repos/{owner}/{repo}/hooks`. These permissions are not currently granted (the orchestrator App uses `issues`, `pull_requests`, and `metadata` scopes). An operator must update the App's permissions in GitHub's UI before the webhook check and installation steps will work. The check degrades gracefully to WARN on 403, so the daemon is not blocked by this gap; it merely reports WARN instead of MISSING until the permission is added.

## Sub-requests (topologically sorted)

1. **Add `InboundConfig` to `config.py`:** add a new `InboundConfig(BaseModel)` with `model_config = ConfigDict(extra="forbid")` and `receiver_url: str`. Add `inbound: InboundConfig | None = Field(default=None)` to `V4Config`. Extend `load_config` to splice `raw["inbound"]` into the payload when present (same pattern as `sandbox`).

2. **Update `load_config` TOML parser** (`config.py:load_config`): add `if "inbound" in raw: payload["inbound"] = raw["inbound"]`.

3. **Add `_check_webhook_coverage` to `doctor.py`:** pure function taking `(config: V4Config, projects: list[ProjectConfig], *, github_factory: Callable[[str], Github] | None = None) -> int`. Returns 0 on SKIPPED/OK/WARN, 1 on MISSING. The `github_factory` parameter is injectable for tests (default: `lambda token: Github(auth=Auth.Token(token))`).

4. **Extend `cmd_doctor` to load config + projects and call the new check:** load config from `FOREMAN_V4_CONFIG` env var; if load fails, print WARN + skip the check. Load projects from `FOREMAN_PROJECTS_PATH`; if load fails, print WARN + skip. Mint orchestrator token via `V4IdentityRegistry`. Pass config + projects to `_check_webhook_coverage`.

5. **Add startup webhook log to `bootstrap_cli_context`** (`bootstrap.py`): after the clone loop, if `config.inbound` is set, build a `Github(auth=Auth.Token(orch_token))` client and check each project's hooks. Log `logger.error(...)` per missing project, `logger.info(...)` per covered project.

6. **Add `_ensure_webhook` helper and call it from `cmd_init`** (`cli/init.py`): `_ensure_webhook(client: Github, repo_slug: str, receiver_url: str) -> tuple[bool, bool]` returns `(created, existed)`. Call it from `cmd_init` when `config.inbound` is set; add a "Webhook:" row to the summary output.

7. **Add `[inbound]` commented-out block to `docker/foreman/config.toml.template`**: a comment block explaining the field and an example `receiver_url` placeholder.

8. **Add tests for `InboundConfig`** to `packages/foreman/tests/v4/test_config.py`: (a) config without `[inbound]` loads cleanly (backward compat), (b) config with `[inbound].receiver_url` populates `V4Config.inbound.receiver_url`.

9. **Add tests for `_check_webhook_coverage`** to `packages/foreman/tests/v4/cli/test_doctor.py`: (a) SKIPPED when `config.inbound is None`, (b) OK when all projects have an active matching webhook, (c) MISSING + exit 1 when a project's hooks don't match, (d) WARN + exit 0 on `GithubException`.

10. **Run `just check`** and verify exit zero.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/v4/config.py` | Add `InboundConfig` model; add `inbound: InboundConfig \| None` field to `V4Config`; update `load_config` to parse `[inbound]` block. |
| `packages/foreman/src/foreman/v4/cli/doctor.py` | Add `_check_webhook_coverage(config, projects, *, github_factory)` function; extend `cmd_doctor` to load config + projects + mint token and pass to new check. |
| `packages/foreman/src/foreman/v4/bootstrap.py` | Add startup webhook coverage log after the clone loop, gated on `config.inbound` being set. |
| `packages/foreman/src/foreman/v4/cli/init.py` | Add `_ensure_webhook` helper; call it from `cmd_init` when `config.inbound` is set; add "Webhook:" row to summary. |
| `docker/foreman/config.toml.template` | Add commented-out `[inbound]` example block. |
| `packages/foreman/tests/v4/test_config.py` | Add `InboundConfig` load tests (optional block, populated block). |
| `packages/foreman/tests/v4/cli/test_doctor.py` | Add `_check_webhook_coverage` tests (SKIPPED, OK, MISSING, WARN). |

## Alternatives considered

1. **Log-only approach (no `foreman doctor` check):** Add only a startup log warning, not a `doctor` check. Easier to implement but the operator has no manual, on-demand way to interrogate coverage without tailing the log. The `foreman doctor` pattern (foreman#363) is explicitly designed for this class of "is my deployment correctly configured?" check — reusing it costs little and provides the ad-hoc probing story. Rejected.

2. **Separate `foreman webhook check` subcommand:** Instead of extending `doctor`, add a new first-class subcommand. Cleaner surface area but breaks the consolidation principle that `foreman doctor` is the health-probe entrypoint. Adding a new command for this creates a second inconsistent probing surface. Rejected — extend `doctor`.

3. **Derive only from `projects.toml`, no installation in `foreman init`:** Spec only the check (points 1–2 in the issue), leave point 3 (init install) for a follow-up. The issue explicitly says "consider making hook installation part of the onboarding path" — including it here makes the onboarding flow self-consistent (labels + bots + webhook in one `foreman init` call) and costs one sub-request. The check is the priority; the installation is incremental. Included with lower priority.

## Open questions

1. **GitHub App permissions:** The orchestrator App requires `administration: read` to list webhooks and `administration: write` to install them. It is unknown whether these permissions are currently granted. The WARN path in `_check_webhook_coverage` handles the 403 gracefully, so absence of these permissions does not break the daemon — but the check degrades to WARN rather than MISSING until the permission is granted. An operator must update the App's permissions in GitHub's UI to get the full signal. This spec cannot resolve this without access to the App's configuration.

## Out of scope

- Periodic (every-tick) webhook coverage checks in the daemon's main loop — startup logging is sufficient; on-demand `foreman doctor` covers the operator's manual check need.
- Auto-creating webhooks during daemon startup or reload — that is an infrastructure mutation that should require an explicit operator action (`foreman init`), not run silently.
- Managing the webhook secret (HMAC signing) — the issue is specifically about coverage (active vs. inactive hook), not about webhook authentication. HMAC is a separate concern.
- Removing stale webhooks that point at old URLs — the issue asks for flagging gaps, not cleanup of orphaned hooks.
- Changes to the inbound webhook receiver itself (the "wren" system) — that is external to this codebase.
