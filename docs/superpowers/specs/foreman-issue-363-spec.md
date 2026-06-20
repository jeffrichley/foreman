# Spec: auto-rebuild `foreman:dev` image on every merge to main (issue #363)

## Goal
The `foreman:dev` Docker image is currently rebuilt manually via
`scripts/build-docker.sh`, so every PR that merges to main takes minutes-
to-hours to reach the running daemon container. Twice on 2026-06-19
(foreman#341 / #347) a stale container ran a known-buggy Worker against
real tickets because nobody had rebuilt yet. This spec automates the
build + redeploy: GitHub Actions builds + pushes `ghcr.io/jeffrichley/foreman`
on every push to main, a Watchtower sidecar on the host polls GHCR and
recreates the daemon container when a new digest lands, a `just
rebuild-daemon` target preserves the offline / dev path, and a new
`foreman doctor` CLI check warns when the running image's stamped SHA
lags `origin/main`. Tracks
[foreman#363](https://github.com/jeffrichley/foreman/issues/363).

## Acceptance criteria
- [ ] **NEW workflow `.github/workflows/image.yml`** that on push to main
  AND on `workflow_dispatch` (manual operator trigger):
  1. Logs into `ghcr.io` using `${{ github.actor }}` + `${{
     secrets.GITHUB_TOKEN }}` via `docker/login-action@v3` (pinned to a
     SHA — match the existing pin convention in `ci.yml` which pins
     actions by 40-char SHA + version comment).
  2. Builds `Dockerfile` via `docker/build-push-action@v6` with build
     args `IMAGE_SHA=${{ steps.short-sha.outputs.short_sha }}` and
     `ALLOW_DIRTY=false` (CI builds are never dirty — the working tree
     IS the merged commit). The short-sha step is a tiny `run:` block
     that emits `echo "short_sha=$(git rev-parse --short HEAD)" >>
     "$GITHUB_OUTPUT"`.
  3. Tags the resulting image with BOTH:
     * `ghcr.io/jeffrichley/foreman:dev` — the rolling pointer
       Watchtower watches.
     * `ghcr.io/jeffrichley/foreman:sha-<short>` — the immutable
       content-derived tag for traceability ("which commit is this
       container actually running?"). The short-sha matches the
       `IMAGE_SHA` build-arg already stamped into the container's
       env (`Dockerfile:22-25` → entrypoint banner JSON line) so a
       single grep across the daemon's logs reconciles container ↔
       tag ↔ source commit.
  4. Pushes both tags.
  5. Caches BuildKit layers via the `cache-from`/`cache-to`
     `type=gha,scope=foreman-dev` shape, so subsequent builds on top
     of unchanged dep layers complete in ~2 minutes instead of ~10.
  Workflow header is:
  ```yaml
  permissions:
    contents: read
    packages: write
  concurrency:
    group: image-${{ github.ref }}
    cancel-in-progress: true
  ```
  `concurrency: cancel-in-progress: true` is load-bearing because a
  fast burst of merges (e.g. release-please train) would otherwise
  queue N redundant builds that each push the same `:dev` tag in
  sequence — concurrency cancels the older runs so the most recent
  merge wins, and Watchtower polls converge on it.
- [ ] **Image timeout matches `ci.yml`**. Set `timeout-minutes: 25`
  on the build job (10% above `ci.yml`'s 20 — Docker builds dominated
  by network are noisier than `just check`).
- [ ] **First-merge package-visibility note**. The first push creates a
  PRIVATE package on GHCR (GitHub default). For Watchtower to pull
  without auth the operator MUST one-shot flip the package to public via
  https://github.com/jeffrichley/foreman/pkgs/container/foreman →
  "Package settings" → "Change visibility" → Public. This is operator
  one-shot setup; document it verbatim in the RUNBOOK section below.
  No secrets are stored in the image (`/run/secrets/*` is a tmpfs
  Compose mount; `IMAGE_SHA` and `ALLOW_DIRTY` env vars are not
  sensitive), so public visibility is safe.
- [ ] **`docker-compose.yml` adds a Watchtower service** scoped to only
  manage the daemon. Concrete service block (placed AFTER `daemon:`
  and BEFORE the top-level `volumes:` block — alphabetical-by-service
  is not the established convention here; the existing file has one
  service, so place Watchtower second and document with a leading
  comment block naming foreman#363):
  ```yaml
  watchtower:
    image: containrrr/watchtower:1.7.1
    container_name: foreman-watchtower
    restart: unless-stopped
    environment:
      # Poll GHCR every 2 minutes. The merge → image-published →
      # daemon-pulled wall-clock budget is ~5 min in the acceptance
      # criteria; CI build is ~2-3 min and the next poll lands
      # within 2 min after that. Tighter intervals burn rate-limit
      # quota on GHCR without operational gain.
      WATCHTOWER_POLL_INTERVAL: "120"
      # Remove the previous image layer after a successful update.
      # Without this the WSL2 disk fills with one stale image per
      # merge over time.
      WATCHTOWER_CLEANUP: "true"
      # Only manage containers labeled with this scope. Without a
      # scope, Watchtower would attempt to manage EVERY container
      # on the host (any voice/agent_core dev containers the
      # operator happens to be running) — see Out of scope below.
      WATCHTOWER_SCOPE: "foreman"
      # JSON-lines log output matches the daemon's structured log
      # convention; tail via `docker compose logs -f watchtower`.
      WATCHTOWER_LOG_FORMAT: "Json"
    volumes:
      # Docker socket is required for Watchtower to inspect/pull/
      # recreate containers. This is essentially root-on-host; the
      # scope label above keeps the blast radius limited to
      # foreman-daemon.
      - /var/run/docker.sock:/var/run/docker.sock
  ```
- [ ] **Daemon service gets the scope label AND image override**.
  Two edits on the existing `daemon:` block in `docker-compose.yml`:
  1. Add label
     `com.centurylinklabs.watchtower.scope=foreman` under a new
     top-level `labels:` key on the service. Watchtower's
     `WATCHTOWER_SCOPE=foreman` matches against this label.
  2. Change `image: foreman:dev` → `image:
     ghcr.io/jeffrichley/foreman:dev`. The `build:` block above it
     stays unchanged — so `docker compose build daemon`
     (operator-side, via `just rebuild-daemon`) still builds
     locally and TAGS the result as
     `ghcr.io/jeffrichley/foreman:dev` (Compose's behavior when
     `image:` is set alongside `build:` is to build, then tag with
     that image name). The operator's local working copy and
     Watchtower's pulled copy land on the same tag, so the daemon
     service's `image:` reference resolves identically either way.
- [ ] **`justfile` adds `rebuild-daemon` target** as the offline
  fallback (approach (c) from the issue, retained as a defense-in-
  depth path). Recipe shape:
  ```just
  # Rebuild + relaunch the daemon container from the current working
  # tree. Use this when CI/Watchtower are unavailable (offline dev,
  # GHCR outage) OR when you need to test an uncommitted change
  # without going through PR + merge. For the normal flow the daemon
  # auto-updates from GHCR via Watchtower (foreman#363).
  rebuild-daemon:
      ./scripts/build-docker.sh
      docker compose up -d daemon
  ```
  Place this BELOW the existing `build:` recipe (last in the file)
  with a blank line above. Do not add it to the composite `check`
  recipe — it's an operator action, not part of the quality gate.
- [ ] **NEW CLI subcommand `foreman doctor`** in a new file
  `packages/foreman/src/foreman/v4/cli/doctor.py` registered in
  `packages/foreman/src/foreman/v4/cli/__init__.py` via
  `app.command("doctor")(cmd_doctor)`. The command runs a sequence of
  checks; this ticket adds ONE check (image freshness). Future tickets
  add more (the file is structured to make adding a check a single new
  function). Signature:
  ```python
  def cmd_doctor(ctx: typer.Context) -> None: ...
  ```
  Behavior:
  1. Read `os.environ.get("IMAGE_SHA", "")`. If empty or `"unknown"`,
     print `[doctor] image-fresh: SKIPPED — IMAGE_SHA not set (running
     outside the foreman container?)` to stdout and continue with exit
     code 0 (this is the expected shape on host-direct runs where the
     env var is never set).
  2. Otherwise, shell out to
     `git ls-remote https://github.com/jeffrichley/foreman.git
     refs/heads/main` with `subprocess.run(..., capture_output=True,
     text=True, timeout=10, check=False)`. The `git` binary is in
     the container (`Dockerfile:33-35`) and `ls-remote` against a
     public repo needs no auth and is rate-limit-free.
  3. If the subprocess returncode is non-zero or the stdout doesn't
     match the expected `<40-char-sha>\trefs/heads/main\n` shape,
     print `[doctor] image-fresh: WARN — cannot reach github.com to
     compare image SHA vs. main HEAD (stderr: <truncated>)` and
     exit 0 (network failure should not be a hard failure of the
     check — Watchtower may also be temporarily blocked; the
     operator should see the warning but the daemon stays up).
  4. Otherwise extract the remote full SHA, take its leading
     `len(IMAGE_SHA)` characters (Dockerfile stamps the SHORT sha,
     typically 7 chars), and compare case-insensitively to
     `IMAGE_SHA`.
     * If they match: print
       `[doctor] image-fresh: OK — running sha-<short> matches main`
       and exit 0.
     * If they differ: print
       `[doctor] image-fresh: STALE — running sha-<image_sha>, main
       is at sha-<remote_short>. Watchtower normally closes this gap
       within ~5 min of merge; rerun in a few minutes or force a
       local rebuild with `just rebuild-daemon`.` Exit code 1.
  Exit code 1 ONLY on confirmed-stale; 0 on OK, on SKIPPED, and on
  network-failure WARN. This makes the exit code usable in `&&`-
  chained scripts ("if doctor is happy, run X") without false alarms
  from transient network blips.
- [ ] **`FOREMAN_SOURCE_REPO` constant in `cli/doctor.py`** holding
  `"jeffrichley/foreman"` — single module-level string for the
  Foreman repo whose `main` the image is built from. NOT pulled from
  config: the configured projects are what the daemon MANAGES, not
  the source of the daemon image. Hardcoding the source-of-truth
  matches the existing convention for `_DEFAULT_CONFIG` (`cli/__init__.py:151`,
  hardcoded `Path.home() / ".foreman" / "v4" / "config.toml"`).
- [ ] **New test file**
  `packages/foreman/tests/v4/cli/test_doctor.py` covering, at minimum:
  * `test_doctor_image_fresh_ok_when_shas_match`: monkeypatch
    `os.environ["IMAGE_SHA"] = "abc1234"`; monkeypatch
    `subprocess.run` to return a stub with returncode 0 and stdout
    `"abc1234deadbeef...feed\trefs/heads/main\n"`; invoke
    `cmd_doctor` via Typer's CliRunner; assert exit code 0 AND the
    output contains `image-fresh: OK`.
  * `test_doctor_image_fresh_stale_when_shas_differ`: same setup but
    stubbed `git ls-remote` returns a different prefix; assert exit
    code 1 AND output contains `image-fresh: STALE`.
  * `test_doctor_image_fresh_skipped_when_env_unset`: monkeypatch
    `IMAGE_SHA` out of the environment; invoke; assert exit code 0
    AND output contains `image-fresh: SKIPPED`.
  * `test_doctor_image_fresh_warn_on_subprocess_failure`: stub
    `subprocess.run` to return returncode 1 with a stderr message;
    assert exit code 0 AND output contains `image-fresh: WARN`.
  * `test_doctor_image_fresh_warn_on_subprocess_timeout`: stub
    `subprocess.run` to raise `subprocess.TimeoutExpired`; assert
    exit code 0 AND output contains `image-fresh: WARN` AND the
    daemon does not crash.
  Tests use Typer's `CliRunner` (existing precedent — search
  `packages/foreman/tests/v4/cli/` for `CliRunner` usage).
- [ ] **`docs/RUNBOOK.md` gains a new section** titled "Image lifecycle
  (auto-rebuild)" placed BETWEEN "Daily operations" and "Recovery:
  daemon won't start". The section documents:
  * One-shot operator setup: flip the GHCR package to public after
    the first CI build pushes (URL + click path verbatim — see the
    visibility acceptance criterion above).
  * The auto-update loop: merge to main → CI builds + pushes `:dev`
    + `:sha-<short>` to GHCR (~2-3 min) → Watchtower polls every 2
    min → pulls new digest → recreates the daemon container (~30s).
    Total wall-clock from merge to running daemon: typically ~5 min.
  * How to verify the running image is up-to-date:
    ```bash
    docker exec foreman-daemon foreman doctor
    # Should print: [doctor] image-fresh: OK — running sha-<X> matches main
    ```
  * Manual override (offline dev OR Watchtower / GHCR outage):
    ```bash
    cd e:/workspaces/ai/agents/foreman
    just rebuild-daemon
    ```
  * How to tail Watchtower's structured log:
    ```bash
    docker compose logs -f watchtower
    ```
  * How to pin a specific image temporarily (e.g. roll back to a
    known-good sha):
    ```bash
    # Edit docker-compose.yml: image: ghcr.io/jeffrichley/foreman:sha-<X>
    docker compose up -d daemon
    # Restore by reverting to image: ghcr.io/jeffrichley/foreman:dev
    ```
  * Tuning the poll interval (operators on a fast loop can drop
    `WATCHTOWER_POLL_INTERVAL` to 60s; operators who care about
    quota can raise to 300s; the env var lives in
    `docker-compose.yml`'s watchtower service block).
- [ ] **`docs/RUNBOOK.md` "Daily operations" → "Start daemon" subsection
  is updated**. Currently lines 174-189 instruct the operator to run
  `./scripts/build-docker.sh && docker compose up -d daemon` as the
  every-day flow. Replace with: "The recommended flow on a healthy
  setup is `docker compose up -d daemon` — Watchtower will pull the
  latest `:dev` image from GHCR within 2 minutes. Use `just
  rebuild-daemon` only for offline dev or when testing uncommitted
  changes." Keep the `--allow-dirty` paragraph (still relevant for
  the dev-iteration case).

## Approach
**Pattern naming (Decision 4 — calibrated lens).** No GoF pattern fits
cleanly. The shape is straightforward "CI publishes artifact + agent on
the host pulls latest". Two Google engineering principles apply:

1. **"Make the right thing easy."** Today the operator's mental model
   is "I have to remember to rebuild after every merge or the
   container drifts." After this ticket the mental model is "merge
   to main; the container catches up on its own; `foreman doctor`
   tells me if it hasn't." One workflow file + one sidecar + one
   doctor check + one fallback `just` recipe replace the manual
   ritual.
2. **Defense in depth ≠ duplication.** Three pull paths converge on
   the same `:dev` tag: (a) Watchtower auto-pull, (b) `just
   rebuild-daemon` for offline dev, (c) explicit `docker compose
   pull daemon && docker compose up -d daemon` for "I want the
   newest image right now without waiting for the next poll." All
   three resolve through the same Compose `image:` reference and the
   same Dockerfile build; there's no second build pipeline that can
   drift.

**Why approach (a) (GHCR + Watchtower) over (b) (push-side webhook) or
(c) (manual `just` target).** The issue body lays out the trade-offs
explicitly; this spec commits to (a) because:
- (b) depends on `agent_core#195` shipping first (the webhook
  receiver). Foreman should not block on a sibling project's roadmap.
- (c) is the status quo with a name — fully manual. The exact
  failure mode from foreman#347 (operator forgets to rebuild) recurs.
- (a) is fully automated, has zero per-operator setup beyond the
  one-shot GHCR visibility flip, survives any local working-tree
  state, and the operator can still fall back to (c) via `just
  rebuild-daemon` for offline dev. The cost is one new sidecar
  container and one ~30-line workflow file.

**Why a separate `image.yml` workflow rather than extending `ci.yml`.**
The two workflows have different gates, different timeouts, and
different permissions:
- `ci.yml` runs on `pull_request | merge_group | push:main |
  workflow_dispatch` with `permissions: contents: read` and runs the
  `just check` quality gate. Adding `packages: write` and a Docker
  build to it would couple "is this PR mergeable?" to "did the GHCR
  push succeed?" — a flaky GHCR would block PRs.
- `image.yml` runs ONLY on `push:main | workflow_dispatch` with
  `permissions: packages: write`. A failure here doesn't gate any PR;
  the worst case is "daemon stays on the previous `:dev` digest until
  the next successful build", which is strictly the status quo.

Separation of concerns: the existing `release.yml` is the precedent
(it's a separate file from `ci.yml` for the same reasons — different
trigger, different permission set, different failure semantics).

**Why Watchtower (vs. host cron polling, vs. a custom poller).** The
issue body names Watchtower as the standard tool for this shape, and
the alternatives all have downsides:
- *Host cron + `docker compose pull && docker compose up -d daemon`*:
  per-operator shell config (cron / Task Scheduler / launchd). Foreman
  targets Windows-WSL2 operators (per the README); three incompatible
  cron setups for one feature.
- *Custom poller (small Python script in `scripts/`)*: equivalent to
  re-implementing Watchtower. Adds code Foreman has to maintain. No
  benefit over the off-the-shelf tool.
- *Docker Compose's `pull_policy: always` + a separate timer*:
  `pull_policy: always` only takes effect on `docker compose up`,
  which is operator-initiated. No automatic re-pull on poll.

Watchtower is ~30MB, one container, configured entirely in
`docker-compose.yml`, scoped via a single label so it can't accidentally
manage other containers on the host.

**Why dual-tag `:dev` + `:sha-<short>`.** The rolling `:dev` tag is
what Watchtower watches; it's the operational pointer. The immutable
`:sha-<short>` tag is what makes "which exact code is the container
running?" answerable without timing-based reasoning. The
`IMAGE_SHA` env var already stamped into the container
(`Dockerfile:22-25` → `entrypoint.sh:84-88`) is the same short SHA, so
`docker exec foreman-daemon env | grep IMAGE_SHA` reconciles with
`docker inspect foreman-daemon | grep Image` against the GHCR tag.
Single source of truth, three places it surfaces.

**Why the doctor check uses `git ls-remote` instead of the GitHub API.**
`git ls-remote https://github.com/<owner>/<repo>.git refs/heads/main`
needs no authentication for a public repo, has no rate limit (it's a
plain Git Smart-HTTP protocol exchange against an unauthenticated
endpoint), and returns the canonical full SHA in a parseable single-
line format. The GitHub REST API alternative
(`/repos/<owner>/<repo>/commits/main`) would need either no auth
(60 reqs/hr/IP — fine for a single check, but the container shares
its IP with watchtower's polls on the same host) or a token (introduces
config). `git` is already in the image (`Dockerfile:33-35`), so the
implementation is one `subprocess.run` call with zero new deps.

**Why `foreman doctor` is a top-level command, not a flag on `daemon
status`.** `daemon status` answers "is the daemon process running?"
which is a fast local-PID-file check. `doctor` answers "is the
DEPLOYMENT healthy?" which is a multi-check probe over the network.
Conflating them would make `daemon status` slow and would force every
caller (including the existing `cmd_daemon_status` consumers like
foreman#360's restore-time PID check) to either wait for network
calls or thread a "fast mode" flag. Future doctor checks
(certificate expiry, GHCR reachability, GitHub Apps token expiry,
disk-space headroom) attach naturally to `doctor`; none of them
belong in `daemon status`. Precedent: `npm doctor`, `brew doctor`,
`nix doctor`.

**Why the doctor check exits 0 on SKIPPED and WARN, not 1.** The exit
code is the signal a monitoring script consumes (`foreman doctor &&
echo ok`). False positives on transient network failures or
host-direct (non-container) invocations would erode the signal's
value. Confirmed-stale is the only condition that warrants exit 1 —
that's a deterministic "something is wrong" the operator can act on.
SKIPPED ("you're running outside the container, this check doesn't
apply") and WARN ("can't reach GitHub right now, try again") are
informational.

**Why public-by-default for the GHCR package.** The image contains
no secrets: credentials flow in at runtime via Compose secrets
(`/run/secrets/*`, tmpfs-mounted at `docker-compose.yml:42-47`) and
the GitHub App IDs come from the operator's `.env`. The image is
just compiled Foreman + dependencies. Making the package public
eliminates the alternative — provisioning a GHCR read token on the
host, mounting it into Watchtower as a Docker config, rotating it
on a schedule — which is a meaningful ops surface for one operator
to maintain.

## Sub-requests (topologically sorted)
1. Add `.github/workflows/image.yml` per the workflow shape in the
   acceptance criteria (login → build → tag `:dev` + `:sha-<short>` →
   push, with BuildKit GHA cache, concurrency-cancellation, and
   `permissions: packages: write`).
2. Edit `docker-compose.yml`:
   * On the `daemon:` service, change `image: foreman:dev` →
     `image: ghcr.io/jeffrichley/foreman:dev` and add the
     `labels:` block with the Watchtower scope label.
   * Add the `watchtower:` service block below `daemon:`.
3. Add `rebuild-daemon` recipe to `justfile` at the bottom of the
   file.
4. Add `packages/foreman/src/foreman/v4/cli/doctor.py` with the
   `FOREMAN_SOURCE_REPO` constant and `cmd_doctor` function per the
   acceptance shape. Register
   `app.command("doctor")(cmd_doctor)` in
   `packages/foreman/src/foreman/v4/cli/__init__.py` (alongside the
   existing `app.command("restore")(cmd_restore)` registration at
   `cli/__init__.py:88`).
5. Add `packages/foreman/tests/v4/cli/test_doctor.py` with the five
   tests enumerated above.
6. Update `docs/RUNBOOK.md`:
   * Add new "Image lifecycle (auto-rebuild)" section between
     "Daily operations" and "Recovery: daemon won't start".
   * Edit the existing "Daily operations → Start daemon" subsection
     (lines 174-189) to describe the auto-update flow + offline
     fallback.

## File-level changes
- `.github/workflows/image.yml` — NEW: build + push to GHCR on
  push-to-main and on workflow_dispatch. Dual-tag `:dev` +
  `:sha-<short>`. BuildKit GHA cache, concurrency-cancellation,
  `permissions: packages: write`, `timeout-minutes: 25`.
- `docker-compose.yml` — change `daemon.image` to the GHCR pointer,
  add the `com.centurylinklabs.watchtower.scope=foreman` label on
  the daemon service, add the `watchtower:` service block.
- `justfile` — add `rebuild-daemon` target at the bottom of the file
  (calls `./scripts/build-docker.sh` then `docker compose up -d
  daemon`).
- `packages/foreman/src/foreman/v4/cli/doctor.py` — NEW: hosts
  `FOREMAN_SOURCE_REPO` constant and `cmd_doctor` function. The
  module is structured so future doctor checks attach as additional
  helper functions called from `cmd_doctor` in sequence.
- `packages/foreman/src/foreman/v4/cli/__init__.py` — add the import
  `from foreman.v4.cli.doctor import cmd_doctor` (alphabetically
  between `daemon` and `init` imports at lines 25-31), and the
  registration `app.command("doctor")(cmd_doctor)` alongside the
  existing `app.command("restore")(cmd_restore)` at line 88.
- `packages/foreman/tests/v4/cli/test_doctor.py` — NEW: five tests
  per the acceptance shape (OK, STALE, SKIPPED, WARN-on-failure,
  WARN-on-timeout).
- `docs/RUNBOOK.md` — new "Image lifecycle (auto-rebuild)" section;
  edit "Daily operations → Start daemon" subsection to describe the
  new flow.

## Alternatives considered
1. **Approach (b) from the issue: push-side trigger via the
   `agent_core#195` webhook receiver.** Rejected: depends on a
   sibling project's roadmap (`agent_core#195` hasn't shipped). If
   it does ship later, the host handler can be slotted in alongside
   Watchtower as a faster trigger — the GHCR push is the same artifact
   either way.
2. **Approach (c) from the issue: just add `just rebuild-daemon` and
   put it in the RUNBOOK as a post-merge ritual.** Rejected on its
   own: it's the status quo with a name. The exact failure mode the
   issue cites (operator forgets, container drifts, foreman#347
   repeats) is unaddressed. Retained as a SUPPLEMENT (the `just`
   target ships as the offline fallback) but not as the primary path.
3. **Extend `ci.yml` instead of adding `image.yml`.** Rejected:
   couples PR-mergeability ("did `just check` pass?") to image-publish
   success ("did GHCR push succeed?"). A flaky registry would gate PRs.
   `release.yml` is the precedent for separating publish concerns into
   their own file.
4. **Host cron instead of Watchtower.** Rejected: per-operator shell
   config (cron on Linux, Task Scheduler on Windows, launchd on
   macOS). Foreman targets Windows-WSL2 operators. One file in source
   control vs. N out-of-source-control cron entries.
5. **A custom poller (small Python script under `scripts/`)
   instead of Watchtower.** Rejected: re-implements Watchtower with
   no incremental value. Watchtower is ~30MB and configured entirely
   declaratively in `docker-compose.yml`.
6. **Skip the dual-tag scheme and only push `:dev`.** Rejected: the
   immutable `:sha-<short>` tag is what makes "roll back the daemon
   to last week's image" answerable without rebuilding from source.
   The marginal cost is one tag pointer per build (~no bytes — both
   tags point at the same digest).
7. **Skip `foreman doctor` and rely on `docker inspect` for image
   freshness.** Rejected: `docker inspect` doesn't compare against
   `main`. The whole point of the doctor check is to surface "is the
   container's source code actually current?" — and that's a
   network call to GitHub plus a comparison against the stamped
   `IMAGE_SHA`. `docker inspect` alone can't answer it.
8. **Multi-architecture builds (`linux/amd64` + `linux/arm64`).**
   Rejected per the issue's Out of scope — current operator target
   is WSL2 / amd64 only. Adding `linux/arm64` would roughly double
   the CI build time without serving any current operator.
9. **Image signing (cosign, sigstore).** Rejected per the issue's
   Out of scope — defensible later for multi-operator or public
   release; not required for single-operator dogfood.
10. **Adding a `foreman:vX.Y.Z` tagged release-image flow.** Rejected
    per the issue's Out of scope — this ticket is the dev-rolling
    case. Tagged release images are a separate epic that can build
    on the same `image.yml` workflow (different trigger:
    `release: published` instead of `push:main`) without conflict.

## Open questions
None. The workflow shape, the dual-tag scheme, the Watchtower service
configuration, the doctor-check exit-code semantics, and the RUNBOOK
section structure are all directly traceable to the issue body or to
in-repo precedents (`ci.yml` action pin convention, `release.yml`
job-separation precedent, foreman#360's `cmd_restore` placement of a
new sibling CLI command).

## Out of scope
- **Tagged-release images (`foreman:v0.X.Y`).** Explicitly out of
  scope per the issue body — dev-rolling case only. The same
  `image.yml` workflow can be extended later with a
  `release: published`-triggered job that publishes immutable
  version tags, without breaking the dev-rolling flow.
- **Image signing (cosign / sigstore).** Per the issue body — single
  operator, no signing needed yet.
- **Multi-architecture builds (`linux/arm64`).** Per the issue body —
  WSL2 / amd64 only is the target.
- **Removing `scripts/build-docker.sh` or the manual `docker compose
  build daemon` path.** Per the issue body — operators may still
  want a local build during development; the new auto-update path
  is additive, not replacing.
- **Watchtower's notification hooks (Slack/email/webhook).** Out of
  scope here; single operator, dashboard work tracked separately on
  foreman#352. The Watchtower service emits structured-log lines on
  every update; `docker compose logs -f watchtower` is the surface.
- **Running Watchtower against other foreman-host containers (e.g.
  voice, agent_core dev containers).** Explicit `WATCHTOWER_SCOPE`
  isolation prevents accidental scope creep; per-project auto-update
  is a separate decision each project should make on its own
  cadence.
- **Dashboard surfacing of the doctor check.** Lives with the
  foreman#352 dashboard epic; this ticket adds the CLI command but
  not the UI.
- **Cross-container Docker-aware liveness for the doctor check.**
  The check exits cleanly when run host-direct (`IMAGE_SHA` env
  unset → SKIPPED) and inside the container (env set → real check).
  A future doctor check for "is the daemon container actually
  running?" would shell out to `docker inspect`, which mirrors the
  same best-effort caveat already documented for foreman#360's
  `cmd_restore` — deferred to whichever ticket adds that check.
