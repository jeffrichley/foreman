# Spec: compose: pass IMAGE_SHA and ALLOW_DIRTY build args through to the image (issue #140)

## Goal

The `Dockerfile` declares two build args — `IMAGE_SHA` (short HEAD sha)
and `ALLOW_DIRTY` (whether the build bypassed the cleanliness gate) —
that surface as env vars inside the container so `docker/entrypoint.sh`
can stamp them into the `container_start` JSON banner for audit
(`Dockerfile:22-27`, `docker/entrypoint.sh:43-47`). Today only the
direct `scripts/build-docker.sh` invocation passes these via
`--build-arg`; a plain `docker compose build daemon` (the routine
daily-ops path) does not, so the banner records
`image_sha:"unknown"` and `allow_dirty:false` regardless of the actual
provenance. This spec makes the values flow through both build paths
by declaring `build.args:` in the `daemon` service of
`docker-compose.yml` and switching `scripts/build-docker.sh` to seed
them via environment instead of CLI flags. Addresses issue #140.

## Acceptance criteria

- `docker-compose.yml`'s `daemon` service declares a `build.args:`
  mapping under the existing `build:` key
  (`docker-compose.yml:13-15`) with two entries:
  `IMAGE_SHA: ${IMAGE_SHA:-unknown}` and
  `ALLOW_DIRTY: ${ALLOW_DIRTY:-false}`. The `:-` default form keeps
  parsing valid when the env vars are unset (e.g. operator running
  `docker compose build daemon` cold), at which point Compose passes
  the literal default strings as build args.
- `scripts/build-docker.sh` exports `IMAGE_SHA` and `ALLOW_DIRTY`
  before invoking `docker compose build daemon`, and no longer passes
  `--build-arg IMAGE_SHA=...` or `--build-arg ALLOW_DIRTY=...` on the
  Compose command line. The cleanliness gates above are unchanged.
- `tests/docker/test_build_check.sh` is updated to assert the env-var
  contract: case 1 (clean) records `ALLOW_DIRTY=false` in the
  recorded environment of the stubbed `docker` call, case 3
  (`--allow-dirty`) records `ALLOW_DIRTY=true`, and both cases record
  a non-empty `IMAGE_SHA`. The stub captures env via the docker
  wrapper rather than via positional args, because the values no
  longer appear on the command line.
- `tests/docker/test_compose_config.sh` (the resolved-config sanity
  check) is extended with two `grep -q` assertions verifying that the
  resolved Compose output for the `daemon` service contains both
  `IMAGE_SHA` and `ALLOW_DIRTY` as build args. This pins the
  `docker-compose.yml` change so a future edit can't silently drop
  the mapping.
- `just check` passes. (The shell-script tests are not currently
  invoked by `just check`; the existing repo convention is that
  authors run `tests/docker/*.sh` directly when touching Docker
  surface area. The plan should run them manually after the edits and
  confirm all PASS lines fire.)
- After: running `docker compose build daemon` directly (without
  `scripts/build-docker.sh`) and starting the container produces a
  `container_start` banner whose `image_sha` is the operator-set
  value or the literal string `unknown` when no `IMAGE_SHA` env was
  exported, and whose `allow_dirty` is the boolean string `false`
  when no `ALLOW_DIRTY` env was exported. Running via
  `scripts/build-docker.sh` produces the real short HEAD sha and the
  real flag.

## Approach

The current asymmetry lives at two layers. The `Dockerfile` correctly
declares `ARG IMAGE_SHA=unknown` / `ARG ALLOW_DIRTY=false` and pipes
them into `ENV` so the entrypoint can read them
(`Dockerfile:22-27`). The build orchestration, however, only fills
them in one of the two routes that reach `docker compose build`:

- `scripts/build-docker.sh` calls
  `docker compose build daemon --build-arg IMAGE_SHA="$image_sha"
  --build-arg ALLOW_DIRTY="$allow_dirty"`
  (`scripts/build-docker.sh:40-42`). These are positional flags on
  the CLI; they reach Docker because Compose forwards `--build-arg`
  through to the BuildKit builder.
- A plain `docker compose build daemon` invoked directly by an
  operator (Makefile hand-edit, a `docker compose up --build`
  refresh, an IDE button) carries no `--build-arg`, so Compose hands
  the builder nothing for those args, and the Dockerfile's
  declared defaults kick in. That is the documented bug.

The Compose v2 fix is the standard idiomatic one: declare
`services.<svc>.build.args` in `docker-compose.yml` and let Compose
interpolate values from its own environment. With the `${VAR:-fallback}`
form, Compose passes the value of the env var when set, and the
fallback string when unset, so the daemon service always receives a
defined build arg regardless of which build path triggered it. This
removes the dependence on the operator remembering to forward
`--build-arg` and centralises the wire-up in the Compose file where
the rest of the daemon's runtime contract already lives.

`scripts/build-docker.sh` then becomes the env-seeding layer for the
"build cleanly with provenance" use case: it computes
`image_sha=$(git rev-parse --short HEAD)` and the `allow_dirty`
flag exactly as today, then `export`s both before invoking
`docker compose build daemon`. The `--build-arg` flags are removed
from the compose call — they would still work, but keeping them
would duplicate the wire-up and create a future trap where someone
edits one site and forgets the other. The cleanliness gates (Gate 1:
local-main-vs-origin; Gate 2: working-tree-clean) are unchanged; the
edit is local to the build-invocation line at the bottom of the
script.

The shell tests under `tests/docker/` need two small updates so they
keep passing and continue to pin the new contract.

`tests/docker/test_build_check.sh` currently stubs the `docker`
binary with a one-line shim that records `$*` (positional args)
into `$DOCKER_CALL_LOG`, then `grep`s the log for
`ALLOW_DIRTY=true|false` (`tests/docker/test_build_check.sh:32-38`,
`:44`, `:60`). After the refactor the build-arg values flow as env
vars instead of CLI flags, so the stub needs to capture env too. The
minimal change: extend the shim to also record `IMAGE_SHA=$IMAGE_SHA
ALLOW_DIRTY=$ALLOW_DIRTY` before the positional args, and update
the `grep -q "ALLOW_DIRTY=..."` assertions to match the new format.
The behavior the tests pin (gate 1, gate 2, `--allow-dirty` stamping)
is unchanged; only the capture path moves.

`tests/docker/test_compose_config.sh` resolves `docker compose
config` and greps for required pieces
(`tests/docker/test_compose_config.sh:40-57`). We add two parallel
`grep -q` lines for `IMAGE_SHA` and `ALLOW_DIRTY` in the resolved
output. This locks the `build.args` block in place and would catch a
future edit that drops it.

No change is needed in the Dockerfile or in `docker/entrypoint.sh` —
they already do the right thing with the values they receive. No
change is needed in `.env.example`: `IMAGE_SHA` / `ALLOW_DIRTY` are
build-time, not runtime, env vars, and operators don't want them
sticky in `.env`. The `${VAR:-fallback}` form in `docker-compose.yml`
covers the unset case, so there's nothing for `.env.example` to
document.

## Sub-requests (topologically sorted)

1. Edit `docker-compose.yml`. Under `services.daemon.build`
   (currently lines 13-15: `context: .` + `dockerfile: Dockerfile`),
   add an `args:` mapping with two keys:

   ```yaml
       build:
         context: .
         dockerfile: Dockerfile
         args:
           IMAGE_SHA: ${IMAGE_SHA:-unknown}
           ALLOW_DIRTY: ${ALLOW_DIRTY:-false}
   ```

   Indentation: `args:` sits two spaces in from `build:`, matching
   the existing `context:` / `dockerfile:` indent (and the rest of
   the file's compose-v2 indentation style).

2. Edit `scripts/build-docker.sh`. Replace the final compose-build
   block (`scripts/build-docker.sh:36-42`) so that it `export`s
   `IMAGE_SHA` and `ALLOW_DIRTY` instead of passing them via
   `--build-arg`:

   ```bash
   image_sha=$(git rev-parse --short HEAD)
   export IMAGE_SHA="$image_sha"
   export ALLOW_DIRTY="$allow_dirty"

   docker compose build daemon
   ```

   The trailing-comment block above this section
   (`scripts/build-docker.sh:36-37`) should be kept and lightly
   updated to reflect that the values now propagate via env →
   Compose interpolation → build arg, rather than directly via
   `--build-arg`. Keep the wording short and audit-oriented.

3. Edit `tests/docker/test_build_check.sh`. In the docker stub
   (`tests/docker/test_build_check.sh:32-37`), change the body of
   `$tmp/stubs/docker` so it captures both env vars and positional
   args, e.g.:

   ```bash
   #!/usr/bin/env bash
   # noop stub — record the env vars + call
   echo "IMAGE_SHA=$IMAGE_SHA ALLOW_DIRTY=$ALLOW_DIRTY docker $*" >> "$DOCKER_CALL_LOG"
   ```

   Then update the three `ALLOW_DIRTY=...` assertions (lines 44, 60)
   so they grep for the env-formatted prefix that the new stub
   writes. Case 1's clean-build assertion stays
   `grep -q "ALLOW_DIRTY=false"` (still matches the new prefix);
   case 3's `--allow-dirty` assertion stays
   `grep -q "ALLOW_DIRTY=true"`. Add one new assertion to each of
   cases 1 and 3 that confirms `IMAGE_SHA=` is followed by a
   non-empty value (e.g.
   `grep -Eq 'IMAGE_SHA=[0-9a-f]+' "$DOCKER_CALL_LOG"`). Run the
   script and confirm all four PASS lines fire.

4. Edit `tests/docker/test_compose_config.sh`. After the existing
   `grep -q 'max-size'` assertion
   (`tests/docker/test_compose_config.sh:57`) and before the final
   `echo "PASS: ..."` (`:59`), add two lines:

   ```bash
   grep -q 'IMAGE_SHA' "$tmp/resolved.yml" || { echo "FAIL: IMAGE_SHA build arg missing"; exit 1; }
   grep -q 'ALLOW_DIRTY' "$tmp/resolved.yml" || { echo "FAIL: ALLOW_DIRTY build arg missing"; exit 1; }
   ```

   Run the script and confirm the PASS line still fires.

5. Run `just check` and the three `tests/docker/*.sh` scripts.
   Confirm all green.

## File-level changes

| Path | Change |
| --- | --- |
| `docker-compose.yml` | Add an `args:` mapping inside `services.daemon.build` with `IMAGE_SHA: ${IMAGE_SHA:-unknown}` and `ALLOW_DIRTY: ${ALLOW_DIRTY:-false}`. No other key in the file touched. |
| `scripts/build-docker.sh` | Export `IMAGE_SHA` and `ALLOW_DIRTY` before the compose build; drop the trailing `--build-arg IMAGE_SHA=... --build-arg ALLOW_DIRTY=...` flags on the `docker compose build daemon` line. Gates above unchanged. Lightly refresh the comment above this block. |
| `tests/docker/test_build_check.sh` | Extend the `docker` stub to record env vars in addition to args; tweak assertions so they grep the new prefix, and add one new `IMAGE_SHA=...` assertion to cases 1 and 3. |
| `tests/docker/test_compose_config.sh` | Add two `grep -q` assertions after the `max-size` line to pin `IMAGE_SHA` and `ALLOW_DIRTY` in the resolved compose output. |

No changes to `Dockerfile`, `docker/entrypoint.sh`, `.env.example`,
or any file under `packages/foreman/`. The Python codebase has no
visibility into these build-time values; the contract is purely
between Compose, BuildKit, and the in-container env that the
entrypoint reads.

## Alternatives considered

- **Keep `--build-arg` flags on the compose call in
  `scripts/build-docker.sh` AND add `build.args` in
  `docker-compose.yml`.** Ruled out: the CLI flag would override the
  Compose interpolation for the scripted path, so it would work, but
  the wire-up would be duplicated across two sites. The first time
  someone edits one and forgets the other (e.g. adds a third arg),
  the two paths diverge again and the bug recurs in a new shape.
  Single source of truth wins.
- **Push the env wiring into a Make/just target rather than into
  `docker-compose.yml`.** Ruled out: `docker compose build daemon`
  called directly (without going through justfile/Make) is exactly
  the day-to-day flow that breaks today. Centralising in Compose
  means every invocation route — `scripts/build-docker.sh`, plain
  `docker compose build`, `docker compose up --build`, IDE buttons —
  gets the same fallback values without coordination.
- **Stamp the SHA at runtime via the entrypoint instead of at build
  time.** Ruled out: the SHA the entrypoint would read at runtime is
  not the image's provenance — the container does not have access to
  the host's git working tree, only the source that was COPYed into
  the image at build time. The whole point of `IMAGE_SHA` is to
  record "which commit was on disk when this image was built", which
  is a build-time fact and belongs in a build arg.
- **Hardcode `ALLOW_DIRTY: false` in `docker-compose.yml` and rely
  on `scripts/build-docker.sh` to override.** Ruled out: this would
  break the audit purpose of `ALLOW_DIRTY`. The whole reason the
  flag exists (per the Docker design spec's
  "`--allow-dirty` visibility" item, surfaced in
  `docker/entrypoint.sh:8-10`) is that an operator who bypasses
  cleanliness gates should see a loud `allow_dirty:true` in the
  startup banner. Hardcoding the default to `false` everywhere would
  silently downgrade the flag the moment someone built without the
  scripted wrapper.

## Open questions

(none)

## Out of scope

- Adding `IMAGE_SHA` / `ALLOW_DIRTY` to `.env.example`. These are
  build-time-only inputs that should NOT be sticky in the operator's
  `.env`; the `${VAR:-fallback}` form in compose handles unset cases.
- Wiring `tests/docker/*.sh` into `just check`. The existing repo
  convention is that authors run these manually when touching the
  Docker surface area; promoting them to the gate is its own ticket
  (requires Docker-in-CI machinery).
- Migrating to BuildKit's `--load` / multi-stage build emit, or any
  other Dockerfile restructuring. The cache-layer ordering and
  layer responsibilities are correct as-is; this change is purely a
  build-arg plumbing fix.
- Surfacing the values to the Python code at runtime (e.g. exposing
  `IMAGE_SHA` as a CLI command output or `/health` payload). The
  entrypoint already prints them in the `container_start` banner;
  any further surfacing is its own design decision.
- Adding a CHANGELOG entry. Release-please synthesises CHANGELOG from
  conventional-commit messages on merged PRs (per `CLAUDE.md`); no
  manual CHANGELOG edit is required and adding one would conflict
  with the next release-please run.
- Cleaning up or audit-renaming the other build args that may grow
  in the future. The bug names exactly these two; broader build-arg
  hygiene is a separate ticket.
