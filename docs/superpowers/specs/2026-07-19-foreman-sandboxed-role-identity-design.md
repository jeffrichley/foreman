# Sandboxed-Role Identity — Design

**Status:** approved (Jeff delegated design decisions 2026-07-19)
**Author:** Wren
**Follows:** the 2026-07-19 bubblewrap sandbox canary, which proved the
mount/isolation layer works but surfaced this auth gap.

## Problem

When `config.sandbox.enabled = true`, the daemon runs each role
(`foreman plan|implement|review|fix ...`) inside a bubblewrap box. The box
gets the role's short-lived installation token injected as `GH_TOKEN`, and
the role/orchestrator PEM keys are deliberately withheld
(`DAEMON_NEVER_BIND` = `/run/secrets`, the vault, `/app/source`).

But the role subprocess runs the **daemon's full CLI bootstrap**, which reads
a PEM in two places:

1. `bootstrap_cli_context` (`bootstrap.py:96-97`) mints an **orchestrator**
   token to run a daemon-level *all-projects clone-maintenance loop* — work
   the daemon already did before dispatch (it preps the box's private clone).
2. `main()` builds `V4IdentityRegistry` (`cli/__init__.py:249`), the
   PEM-based `IdentityProvider`. `PyGithubGitProvider` calls
   `identity.get_role_token(...)` on **every GitHub API access** (issue read,
   comments, labels, PR), so the role's actual work also mints from a PEM.

The canary caught #1 first: the box crash-failed with
`FileNotFoundError: /run/secrets/orchestrator_pem`. #2 is the same class and
would fail the moment #1 is cleared.

The `GH_TOKEN` the box already has covers only the **git-CLI** path
(clone/push). The **PyGithub API** path never consumes it. This design closes
that gap: in the box, *all* GitHub auth — CLI and API — uses the injected
`GH_TOKEN`, and no PEM is ever read.

## Approach (Option A — finish the intended design)

Injecting `GH_TOKEN` was always meant to be "the box uses the role token for
everything"; it just never got wired on the API side. So we add a
token-from-env `IdentityProvider` and select it (plus skip the daemon clone
loop) when a `FOREMAN_SANDBOXED=1` marker is present. We do **not** bind a PEM
into the box (Option B — walks back the security goal) or build a token
broker (Option C — solves a threat we are not defending against yet; deferred
as future hardening alongside the planned Claude-creds proxy).

## Architecture

One env marker — `FOREMAN_SANDBOXED=1`, set by `SandboxLauncher.build_argv` —
flips three things in the role subprocess:

```
dispatcher (sandbox.enabled=true)
  └─ build_argv: --setenv GH_TOKEN <role token>
                 --setenv FOREMAN_SANDBOXED 1        ← new
       └─ foreman <role> …  (inside the box)
            main() sees FOREMAN_SANDBOXED=1
              ├─ identity = EnvTokenIdentity()        ← returns GH_TOKEN, no PEM
              └─ bootstrap_cli_context(run_startup_clone=False)  ← skip clone loop
                   └─ role work: git-CLI + PyGithub API both auth via GH_TOKEN
```

Non-sandboxed runs (marker absent) take the **exact current path** —
`V4IdentityRegistry` + the clone loop — byte-for-byte unchanged.

## Components

### 1. `EnvTokenIdentity` — `foreman/v4/identity.py`

Implements the one-method `IdentityProvider` protocol
(`bootstrap.py:41` — `get_role_token(self, role: str) -> str`).

```python
class EnvTokenIdentity:
    """IdentityProvider backed by a single injected GH_TOKEN.

    Used only inside the bubblewrap sandbox, where the daemon injects the
    dispatched role's short-lived installation token as GH_TOKEN and the
    PEM keys are deliberately absent. Returns that one token for ANY role
    argument: the box holds exactly one role's identity and nothing else,
    so the `role` parameter is inert here (documented, not asserted — the
    sandbox mount plan, not this class, is what guarantees single-role).
    Never reads a PEM. Fail-closed if GH_TOKEN is missing.
    """

    _ENV_VAR = "GH_TOKEN"

    def get_role_token(self, role: str) -> str:
        token = os.environ.get(self._ENV_VAR)
        if not token:
            raise SandboxIdentityError(
                "FOREMAN_SANDBOXED is set but GH_TOKEN is empty/unset; the "
                "sandboxed role has no injected token to authenticate with. "
                "The dispatcher must set --setenv GH_TOKEN <role token>. "
                "Refusing to run."
            )
        return token
```

`SandboxIdentityError` is a new exception in `identity.py` (subclass of
`RuntimeError`) so the failure is typed and greppable, matching the
fail-closed discipline of `SandboxUnavailableError` in `sandbox.py`.

### 2. `run_startup_clone` param — `bootstrap_cli_context`

Add `run_startup_clone: bool = True` to the signature. Guard the clone loop:

```python
if run_startup_clone and active_projects:
    orch_token = identity.get_role_token("orchestrator")
    for pc in active_projects:
        ...
```

Default `True` preserves every existing caller and all current behavior. The
rest of `bootstrap_cli_context` (building the object graph from
`active_projects`, git providers, etc.) is unchanged and still runs in
sandbox mode — the role needs its project config and git provider; it just
must not do orchestrator-level clone maintenance.

### 3. `main()` sandbox branch — `foreman/v4/cli/__init__.py`

After the existing `FOREMAN_DRY_RUN` short-circuit (`cli/__init__.py:180`,
the established env-branch precedent), and after config + projects are loaded,
select the identity and clone behavior:

```python
sandboxed = os.environ.get("FOREMAN_SANDBOXED") == "1"
identity, run_startup_clone = _select_identity(
    config=config, projects=projects, sandboxed=sandboxed
)
```

`_select_identity(...)` is a small module-level helper (so the branch is
unit-testable without invoking the full `main()`):

- **sandboxed:** `return EnvTokenIdentity(), False`
- **not sandboxed:** `return V4IdentityRegistry(apps=config.apps,
  orchestrator=config.orchestrator, installation_repo=projects[0].repo), True`

The `if not projects: raise` guard and `_git_factory` are unchanged.
`_git_factory` still constructs `PyGithubGitProvider(identity=identity,
role="orchestrator", repo_full_name=repo)`; under `EnvTokenIdentity` the
`role="orchestrator"` label is inert (the token returned is the injected role
token regardless), so no change is needed there — documented in the sandbox
branch. The final `bootstrap_cli_context(..., run_startup_clone=...)` +
`app(obj=ctx)` are shared across both paths.

### 4. `FOREMAN_SANDBOXED=1` marker — `SandboxLauncher.build_argv`

Add `"FOREMAN_SANDBOXED": "1"` to the `setenv` dict `build_argv` already
constructs (alongside `PATH`, `HOME`, `GH_TOKEN`, …), so every box gets it via
a `--setenv FOREMAN_SANDBOXED 1` triple. It is set on the box's cleared env
only — the daemon's own process never has it, so the daemon and any
unsandboxed run keep the PEM path.

## Error handling / fail-closed

- **Sandboxed + `GH_TOKEN` missing:** `EnvTokenIdentity.get_role_token` raises
  `SandboxIdentityError` with an actionable message. The role subprocess exits
  non-zero; the state machine escalates (NeedsHelp) rather than running
  unauthenticated. Mirrors the `SandboxUnavailableError` discipline.
- **Not sandboxed:** unchanged — the PEM registry, and its existing
  `RuntimeError` if a PEM path is missing.
- No silent fallback: the marker never causes an unsandboxed run to skip the
  PEM path, and a missing token never falls back to a PEM read.

## Testing

- **Unit — `EnvTokenIdentity`:** returns `GH_TOKEN` for any role
  (`"planner"`, `"orchestrator"`, arbitrary); raises `SandboxIdentityError`
  when `GH_TOKEN` is unset and when it is empty.
- **Unit — `bootstrap_cli_context`:** with a fake `IdentityProvider` that
  raises if `get_role_token` is called, `run_startup_clone=False` completes
  without calling it (clone loop skipped); `run_startup_clone=True` still
  invokes the loop (existing behavior preserved). Assert the returned
  `CliContext` is otherwise well-formed in both.
- **Unit — `_select_identity`:** `sandboxed=True` → `(EnvTokenIdentity, False)`
  and does not require `projects[0].repo`; `sandboxed=False` →
  `(V4IdentityRegistry, True)`.
- **Unit — `build_argv`:** argv contains the `--setenv FOREMAN_SANDBOXED 1`
  triple (pure argv assertion, runs everywhere).
- **Hermetic integration (real bwrap, self-skips off userns):** run a
  sandboxed `foreman` command with `FOREMAN_SANDBOXED=1`, a dummy `GH_TOKEN`,
  and **no PEM mounted**, and assert it gets **past bootstrap** without a
  `/run/secrets/*_pem` `FileNotFoundError`. This is the regression lock for
  the canary crash. (The live PyGithub API round-trip needs real GitHub and is
  covered by the canary keystone, not hermetically.)
- **Keystone (manual, final plan task):** rebuild the image, flip
  `FOREMAN_SANDBOXED`/`sandbox.enabled` on, re-run agent_core #408, and watch
  it advance **past Planning** — a real sandboxed ticket, not "tests pass."

## Non-goals / out of scope

- `foreman reset` reading projects from `config.projects` (empty since #477)
  rather than the host-mounted `projects.toml` — a separate CLI gap, tracked
  independently.
- Token broker / minting proxy (Option C) — future hardening, same shape as
  the planned LLM-proxy so Claude creds leave the box.
- Changing the scope/permissions of the role App installation token — assumed
  sufficient for the role's API ops (that is exactly what the daemon already
  uses per role); validated at the canary keystone, not changed here.

## Rollout

Lands behind the existing, default-off `config.sandbox.enabled` flag — no new
operator flag. When the sandbox is enabled, the marker and the env-token path
activate together. First real proof is the #408 canary re-run.
