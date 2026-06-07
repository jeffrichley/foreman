# Spec: pass IMAGE_SHA and ALLOW_DIRTY build args through `docker compose build` (issue #140)

## Goal

The Dockerfile declares `ARG IMAGE_SHA=unknown` and `ARG ALLOW_DIRTY=false`
(`Dockerfile:22-23`) so the entrypoint can stamp them into the
`container_start` JSON banner (`docker/entrypoint.sh:43-47`). Today only
`scripts/build-docker.sh` populates them — and only when invoked
directly with `--build-arg` flags (`scripts/build-docker.sh:40-42`). The
daily-ops path `docker compose build daemon` (and the cold cache rebuild
that happens on `docker compose up -d daemon` after a Dockerfile edit)
bypasses the script entirely, so the banner reports
`image_sha: "unknown"` and `allow_dirty: false` regardless of the actual
build state. This spec teaches `docker-compose.yml` to read the two
values from env with sensible defaults and reworks `build-docker.sh` to
plumb them via env-var exports — so the banner reports real values no
matter which build path the operator uses. Addresses
[#140](https://github.com/jeffrichley/foreman/issues/140).

## Acceptance criteria

- `docker-compose.yml`'s `daemon` service contains a `build.args:` block
  with exactly two keys, in this shape:

  ```yaml
  build:
    context: .
    dockerfile: Dockerfile
    args:
      IMAGE_SHA: ${IMAGE_SHA:-unknown}
      ALLOW_DIRTY: ${ALLOW_DIRTY:-false}
  ```

  The `${VAR:-default}` interpolation form is required so a bare
  `docker compose build daemon` (no env, no script) still resolves to
  the Dockerfile's documented defaults (`unknown` / `false`) rather than
  erroring on an undefined variable.
- `scripts/build-docker.sh` no longer passes `--build-arg` flags to
  `docker compose build`. Instead it sets and exports the two env vars
  immediately before the compose invocation, with the same values it
  computes today: `IMAGE_SHA` from `git rev-parse --short HEAD`,
  `ALLOW_DIRTY` from the `--allow-dirty` flag. The pre-build gates
  (sections "Gate 1" and "Gate 2" in `scripts/build-docker.sh:19-34`)
  are unchanged.
- `tests/docker/test_build_check.sh` is updated so its docker stub
  captures the **environment** of the call (not just argv). The
  existing `grep -q "ALLOW_DIRTY=false" "$DOCKER_CALL_LOG"` (case 1)
  and `grep -q "ALLOW_DIRTY=true" "$DOCKER_CALL_LOG"` (case 3)
  assertions are reworked to assert against captured env, and a new
  case is added asserting `IMAGE_SHA` is exported with the script's
  computed short-sha value (the existing tests only check
  `ALLOW_DIRTY`; the spec extends coverage to the now-symmetric
  second variable). All other cases (case 2 dirty-tree refusal, case 4
  ahead-of-origin refusal) are unchanged.
- `tests/docker/test_compose_config.sh` gains a new assertion: after
  `docker compose config` resolves the YAML with no `IMAGE_SHA` /
  `ALLOW_DIRTY` in env, the resolved YAML's `daemon` service must
  contain `IMAGE_SHA: unknown` and `ALLOW_DIRTY: "false"` (or the
  unquoted equivalent — `docker compose config` may render scalar
  strings unquoted; the grep should accept both forms).
- `Dockerfile` and `docker/entrypoint.sh` are NOT modified. The two
  ARGs and the banner template are already correct; only the
  build-arg plumbing was broken.
- `just check` passes. The host-side docker shell tests are not in the
  `just check` gate (the `test` target runs `pytest` only — see
  `justfile:32-33`), so they are exercised manually as part of the
  Worker's verification step.

## Approach

The Dockerfile / entrypoint half of the build-arg pipeline is already
correct: line 22-23 declares the ARGs with the same defaults the
issue's "sensible defaults if unset" criterion calls for, lines 24-25
surface them as ENV, and the entrypoint at lines 43-47 interpolates
them into the `container_start` JSON banner. Nothing in that chain
needs to change.

The break is at the compose layer. Looking at the four ways the
foreman daemon image actually gets built:

1. `scripts/build-docker.sh` → invokes `docker compose build daemon
   --build-arg IMAGE_SHA=... --build-arg ALLOW_DIRTY=...`. This DOES
   plumb the args correctly today, but only because the script types
   them explicitly. Most operators don't reach for this path during
   day-to-day iteration.
2. `docker compose build daemon` → no `--build-arg` flags, no
   `build.args:` in YAML → Dockerfile ARGs fall back to their
   defaults (`unknown` / `false`). This is the daily-ops path and it's
   the one the issue is about.
3. `docker compose up -d daemon` after a Dockerfile edit → same as #2;
   compose's implicit rebuild does not pass build args.
4. `docker build .` (rare; no compose) → same as #2.

The minimum fix that closes paths #2 and #3 without regressing #1 is
to teach `docker-compose.yml` to **read the two values from env with
defaults**, then rework `build-docker.sh` to **set those env vars
before invoking compose build** instead of passing `--build-arg` flags.

The compose YAML interpolation form `${IMAGE_SHA:-unknown}` is the
standard way to inject env-derived build args with a fallback (it's
the same shape compose already uses for secrets at
`docker-compose.yml:64-75` via `${HOME}`). Operators running raw
`docker compose build daemon` get the documented defaults; operators
running `scripts/build-docker.sh` get the real sha + dirty flag, via
exports the script sets right before the compose call.

This consolidation also means the script becomes simpler: instead of
two `--build-arg` flags appended to the `docker compose build` line,
it sets two env vars and lets the YAML interpolation do the rest. The
single source of truth for "what build args does the daemon image
take" moves to the compose YAML, which is where someone reading the
project for the first time would look anyway.

The host-side tests need a small but real rework. `tests/docker/
test_build_check.sh:32-37` currently writes a stub `docker` script
that records its argv to `$DOCKER_CALL_LOG`:

```bash
cat > "$tmp/stubs/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$DOCKER_CALL_LOG"
STUB
```

After the change, `ALLOW_DIRTY` / `IMAGE_SHA` no longer appear in
argv — they appear in env. The stub needs to also dump the relevant
env-var values so the test's `grep` assertions can match. The
assertions stay logically identical ("after `--allow-dirty`,
ALLOW_DIRTY=true was passed to compose"; "after clean-build,
ALLOW_DIRTY=false was passed"); only the capture mechanism shifts
from argv-grep to env-grep.

`tests/docker/test_compose_config.sh` already runs `docker compose
config` and greps the resolved YAML for landmark keys
(`init: true`, `foreman-state`, etc.). Adding two more greps for
`IMAGE_SHA` and `ALLOW_DIRTY` in the resolved `daemon.build.args`
section is a one-line extension of the existing pattern.

## Sub-requests (topologically sorted)

1. Edit `docker-compose.yml`: inside the existing `services.daemon.build`
   block (currently `docker-compose.yml:13-15`), add an `args:` child
   with `IMAGE_SHA: ${IMAGE_SHA:-unknown}` and
   `ALLOW_DIRTY: ${ALLOW_DIRTY:-false}`. Final shape:

   ```yaml
   services:
     daemon:
       build:
         context: .
         dockerfile: Dockerfile
         args:
           IMAGE_SHA: ${IMAGE_SHA:-unknown}
           ALLOW_DIRTY: ${ALLOW_DIRTY:-false}
   ```

   No other compose keys touched.

2. Edit `scripts/build-docker.sh`: replace the trailing
   `docker compose build daemon --build-arg ... --build-arg ...`
   block (currently lines 36-42) with an export-and-invoke shape that
   keeps the same `image_sha` derivation:

   ```bash
   # Stamp the image SHA + allow-dirty flag as env vars; compose YAML
   # interpolates them into the daemon service's build.args, which
   # surface as Dockerfile ARGs and then as container env at runtime.
   export IMAGE_SHA=$(git rev-parse --short HEAD)
   export ALLOW_DIRTY="$allow_dirty"

   docker compose build daemon
   ```

   Pre-build gates (lines 19-34) and the `allow_dirty` parsing
   (lines 16-17) stay byte-identical.

3. Edit `tests/docker/test_build_check.sh`: rework the docker stub
   so it dumps the env values for the two relevant vars in addition
   to (or instead of) argv. Concrete shape:

   ```bash
   cat > "$tmp/stubs/docker" <<'STUB'
   #!/usr/bin/env bash
   # Record argv plus the env vars build-docker.sh is expected to export.
   echo "docker $*" >> "$DOCKER_CALL_LOG"
   echo "ENV IMAGE_SHA=${IMAGE_SHA:-<unset>}" >> "$DOCKER_CALL_LOG"
   echo "ENV ALLOW_DIRTY=${ALLOW_DIRTY:-<unset>}" >> "$DOCKER_CALL_LOG"
   STUB
   ```

   Then update the case-1 and case-3 assertions to grep against
   `ENV ALLOW_DIRTY=false` / `ENV ALLOW_DIRTY=true` instead of the
   `--build-arg ALLOW_DIRTY=...` argv form. Add a parallel assertion in
   case 1 that `ENV IMAGE_SHA=<short-sha>` was set — match against
   the regex `ENV IMAGE_SHA=[0-9a-f]\{7,\}` to avoid coupling the test
   to a specific commit sha. Cases 2 and 4 stay unchanged (they assert
   refusal before the docker stub gets a chance to run).

4. Edit `tests/docker/test_compose_config.sh`: after the existing
   landmark greps (lines 47-57), add two more:

   ```bash
   grep -qE 'IMAGE_SHA:[[:space:]]+"?unknown"?' "$tmp/resolved.yml" \
       || { echo "FAIL: IMAGE_SHA build arg missing or wrong default"; exit 1; }
   grep -qE 'ALLOW_DIRTY:[[:space:]]+"?false"?' "$tmp/resolved.yml" \
       || { echo "FAIL: ALLOW_DIRTY build arg missing or wrong default"; exit 1; }
   ```

   The regex tolerates both quoted and unquoted scalar rendering from
   `docker compose config`, since the docker version on the test host
   may render either form.

5. Run `bash tests/docker/test_build_check.sh` and `bash tests/docker/
   test_compose_config.sh` directly to confirm they pass against the
   updated shell + YAML.

6. Run `just check` to confirm lint / typecheck / pytest stay green
   (no Python code changes, so this is a sanity-only step).

## File-level changes

| Path | Change |
| --- | --- |
| `docker-compose.yml` | Add `build.args:` block under `services.daemon.build` with `IMAGE_SHA: ${IMAGE_SHA:-unknown}` and `ALLOW_DIRTY: ${ALLOW_DIRTY:-false}`. No other compose keys touched. |
| `scripts/build-docker.sh` | Replace the trailing `docker compose build daemon --build-arg IMAGE_SHA=... --build-arg ALLOW_DIRTY=...` invocation with two `export` lines (`IMAGE_SHA`, `ALLOW_DIRTY`) followed by a bare `docker compose build daemon`. Pre-build gates and arg parsing unchanged. |
| `tests/docker/test_build_check.sh` | Extend the docker stub to also record `IMAGE_SHA` / `ALLOW_DIRTY` from env; rework case-1 and case-3 assertions to grep `ENV ALLOW_DIRTY=...`; add a new case-1 assertion that `IMAGE_SHA` was exported as a 7+ hex-char short-sha. |
| `tests/docker/test_compose_config.sh` | Add two greps against the resolved compose YAML asserting `IMAGE_SHA: unknown` and `ALLOW_DIRTY: false` defaults landed when env was empty. |

`Dockerfile` and `docker/entrypoint.sh` are read for context only — they
are already correct and stay byte-identical.

## Alternatives considered

- **Keep `--build-arg` flags in `scripts/build-docker.sh` AND add
  `build.args:` to the YAML (belt-and-suspenders).** Ruled out: the
  issue explicitly asks for "`scripts/build-docker.sh` sets the env
  vars before invoking compose build," so the path of least surprise
  is to make env-var exports the single mechanism. Keeping both creates
  two places that can drift and obscures which is authoritative when
  reading the script.
- **Skip the `${VAR:-default}` interpolation and put literal defaults
  (`IMAGE_SHA: unknown`) in the YAML.** Ruled out: that would make
  `docker compose build daemon` always emit `unknown`, no matter what
  env says — defeating the whole point. Operators running the script
  set `IMAGE_SHA` in env; the YAML must read from env or those exports
  go nowhere.
- **Bake the short-sha into the image via an OCI label
  (`LABEL org.opencontainers.image.revision=...`) instead of a build
  arg.** Ruled out for this spec: the entrypoint already reads
  `IMAGE_SHA` from env into the `container_start` banner, and the
  banner is the consumer the issue calls out. Switching to OCI labels
  would require rewriting the banner-emission code AND querying labels
  at runtime (no convenient stdlib path) — strictly more work than the
  bug demands. A future ticket could harmonize with OCI labels, but
  that's not what this is.
- **Compute the short-sha inside the Dockerfile via `RUN git rev-parse`.**
  Ruled out: requires bringing the `.git/` directory into build
  context, which today is excluded by `.dockerignore` and would balloon
  build context size. Build-time computation also doesn't address the
  `ALLOW_DIRTY` half of the problem (which is a property of the
  invocation, not the source tree).
- **Do nothing; document that operators must always run
  `scripts/build-docker.sh`.** Ruled out: the issue describes a real
  silent-data-quality bug in the audit banner, the fix is two YAML
  lines plus a small script tweak, and `docker compose up -d daemon`
  is the documented day-to-day path
  (`docker-compose.yml:5-6` lists `docker compose up -d daemon` as the
  first lifecycle command). Forcing operators through a wrapper script
  for every iteration would be friction-creating, not friction-removing.

## Open questions

(none)

## Out of scope

- Changing the format of the `container_start` JSON banner in
  `docker/entrypoint.sh`. The two emitted keys (`image_sha`,
  `allow_dirty`) and their JSON types stay as-is; this spec only fixes
  what populates them.
- Adding OCI image labels (`org.opencontainers.image.*`) for
  Docker-native image metadata. Worth considering separately.
- Wiring the docker shell tests (`tests/docker/test_build_check.sh`,
  `tests/docker/test_compose_config.sh`, `tests/docker/test_entrypoint.sh`)
  into the `just check` gate. They're currently invoked manually; that
  CI-integration question is its own ticket.
- Touching `.env.example` to surface `IMAGE_SHA` / `ALLOW_DIRTY` as
  operator-configurable knobs. They're build-time values populated by
  the build script, not runtime values an operator would set in `.env`.
- Adding a fallback in `docker/entrypoint.sh` if either env var ends up
  empty at container start. The Dockerfile ARG defaults
  (`Dockerfile:22-23`) plus the YAML interpolation defaults make a
  truly unset value impossible to reach via the supported build paths,
  and the banner template at `entrypoint.sh:44-45` already has
  `${IMAGE_SHA:-unknown}` / `${ALLOW_DIRTY:-false}` defensive defaults
  as a third safety net.
- Modifying `Dockerfile` — the two `ARG` declarations and their default
  values are already correct.
