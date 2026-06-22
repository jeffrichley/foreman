# Spec: Foreman dashboard v0 — operator visibility + light action surface

## Goal

Build a web UI for foreman that gives the operator (Jeff) at-a-glance visibility into what's happening across every project foreman is running, plus three operator verbs: queue a ticket, hold an in-flight ticket, reset a stuck ticket. Lean v0 — no alerts pane, no notifications, no triage cockpit — just the visibility shape with three buttons.

**Motivation.** Foreman runs autonomously, but Jeff still needs to know its state. Today's surfaces are `foreman ps` (terminal table), `docker logs foreman-daemon` (firehose), and GitHub-the-website (labels + PRs scattered across projects). None of these answer the question "what's foreman doing right now, and is anything stuck?" in one screen. The dashboard is that one screen.

The other half of the motivation is recovery latency. When a ticket gets stuck (today's `NeedsHelp` tickets that have been parked since 2026-06-19), Jeff currently has to know to go look at `foreman ps` to discover it. The dashboard pulls stuck tickets to the top with a visual signal (red border, sort precedence) so they surface without operator polling.

**Scope of this decision.** Establish the v0 dashboard architecture, the visual shape, the operator action surface, and the consumed HTTP API contract (defined by the sibling [foreman v5 spec](2026-06-21-foreman-v5-postgres-http-design.md)). The subsequent implementation plan (via writing-plans) decomposes into bite-sized tasks against this spec.

**Out of scope.**

- Authentication/authorization beyond local-bind or tailnet bind (matches the v5 HTTP control plane's threat model).
- An alerts/notifications panel. Deferred to v1+ — see "Deferred to v1+" below.
- Wren-to-Jeff or Pepper-to-Jeff communication surfaces. The dashboard is foreman's operator console, not a multi-being communication channel; those exist already (Discord, bus envelopes, vault notes).
- Direct GitHub mirroring (issue creation, PR comments, label management beyond what foreman already does). The dashboard observes foreman; it doesn't replace GitHub.
- Mobile/touch-first design. Desktop browser is the target form factor.
- A staging/dev environment separate from production. v0 runs on the operator's host (or a homelab box) under docker-compose alongside foreman-daemon. There's one deployment.

## Acceptance criteria

- **Portfolio page** at `GET /` renders a status grid of every project listed in `~/.foreman/v5/config.toml`. Each project tile shows name, repo, ticket counts, a status badge (one of `stalled` / `needs-you` / `idle` / `healthy`), last-activity timestamp.
- **Sort order on the portfolio page is precedence-driven**, not alphabetical: `stalled` first, then `needs-you`, then `healthy`, then `idle`. Within a category, sort by most-recent activity descending.
- **Tiles are clickable.** Clicking a tile navigates to `GET /projects/{name}` (the detail page).
- **Project detail page** at `GET /projects/{name}` renders for any project in the config. Layout follows Pepper's `project.html` mockup verbatim except the Alerts column of the Pulse bar — that column is removed for v0; Pulse becomes 2-column (Role health / Metrics).
- **Project detail page uses SSE for live updates.** Connects to `GET /projects/{name}/events` (foreman v5 HTTP API), drives tick counters, pulse-dot animation, role activity rows in real time. No client-side polling on the detail page.
- **Portfolio page polls** every 30 seconds via HTMX (`hx-trigger="every 30s"`) for the latest project tile snapshots. No SSE on the portfolio page — refresh cadence is bounded by ticks, not events.
- **Action surface — three buttons**:
  - **`+ Plan ⏎`** opens a small inline form (issue URL + brief reason). Submit calls `POST /tickets/enqueue`. On success, a toast confirms; the project tile (or detail page if currently open) refreshes.
  - **`Hold`** on each in-flight ticket row prompts for a reason, then calls `POST /tickets/{id}/hold`. The ticket row updates immediately.
  - **`Reset`** is hidden behind a confirmation modal (destructive — wipes labels, branches, worktrees, ticket row). Confirm calls `POST /tickets/{id}/reset` with `no_retrigger=true`.
- **No alerts pane.** Pulse bar is 2-column (Role health / Metrics). Alerts column from Pepper's mockup is dropped for v0.
- **Pepper's visual system is preserved verbatim.** Dark theme, accent `#EE9B33`, fonts, tabular-nums, status colors. CSS extracted from her mockups as-is, mounted as `static/dashboard.css`.
- **Daemon-version handshake.** On every page load, the dashboard fetches `GET /health` from the daemon. If `foreman_version` is not `v5`, render an error page asking the operator to upgrade foreman; refuse to render anything else.
- **Auto-discovery.** Any project added to `~/.foreman/v5/config.toml` appears on the dashboard at the next portfolio poll (no dashboard restart). The dashboard reads project scope from the daemon's `GET /projects` response, not from its own config.
- **Operator runbook.** README in the dashboard repo (or directory) documents the docker-compose-up flow + auth setup (localhost vs tailnet) + how to verify the dashboard sees the daemon.

## Approach

### Stack

- **FastAPI** for the HTTP server. Same framework foreman v5's control plane uses; sharing the framework reduces operator surface area.
- **Jinja2** for HTML templates. Macros provide React-component-style reuse (`metric_tile`, `role_health_row`, `project_tile`, `issue_row`, `repo_switcher`, `pulse_bar` — ~6 macros total for v0).
- **HTMX** (~14 KB) for partial-page swaps + live updates. Two modes are used:
  - **Polling**: `hx-trigger="every 30s"` on the portfolio page's project tile grid.
  - **SSE**: `hx-ext="sse"` + `hx-sse-connect="/projects/{name}/events"` on the project detail page. Driven by the foreman v5 SSE endpoint.
- **Alpine.js** (~7 KB) for genuine client-side state — the repo-switcher dropdown, the inline `+ Plan` form expansion, the reset-confirmation modal. Used sparingly; HTMX covers most of the interactivity.
- **No build pipeline.** Both HTMX and Alpine ship as `<script>` tags pinned to specific versions. No webpack, no esbuild, no Tailwind. The dashboard image is a slim Python container with the FastAPI app + a static-files directory; no Node toolchain.
- **`httpx`** (async) for talking to the foreman daemon. Wrapped in a tiny `ForemanClient` class that mirrors the v5 HTTP API. Pydantic models for responses to catch schema drift.

### Visual system (Pepper's CSS, verbatim)

Tokens (extracted from her 2026-06-19 mockups, delivered via zip 2026-06-21):

- **Theme**: dark — bg `#0B0F14`, panels `#11171F`, cards `#161D27`
- **Accent**: orange `#EE9B33` (Pepper's brand color — used for the brand mark + the primary action button)
- **Text**: `#E7ECF3` body, `#8A94A6` muted
- **Status**: ok `#5BD68B`, warn `#FFC857`, err `#FF6B6B`, bot `#6FB7FF`
- **Type**: 14 px base, mono for numerics + IDs (JetBrains Mono / SFMono / Menlo)
- **Tabular numbers** throughout (`font-variant-numeric: tabular-nums`)

These are not negotiated re-derivations; they're extracted from the source CSS as-is. The implementation copies Pepper's stylesheet to `static/dashboard.css` and references it from the base template.

### Page structure

**Portfolio page (`GET /`)**: header (brand + repo-switcher) + project tile grid + footer (daemon version + last-poll timestamp).

The tile grid is one HTMX-polled fragment (`<div id="project-grid" hx-get="/_partials/project-grid" hx-trigger="every 30s" hx-swap="innerHTML">`). The partial endpoint server-side renders one `project_tile` macro per project, sorted by status precedence.

**Project detail page (`GET /projects/{name}`)**: header + 2-column pulse bar (Role health / Metrics) + active-tickets table + recently-merged section + sidebar (`+ Plan`, `Hold`, `Reset` actions, repo metadata).

The page subscribes to `GET /projects/{name}/events` via SSE. Each event swaps a specific fragment:

- `state_transition` → swap the row for the affected ticket.
- `role_progress` → update the tick-N-of-M counter on the active-role row.
- `outcome` → flash a brief glow on the affected ticket's status cell + log the outcome to the "recent activity" sidebar.
- `heartbeat` → no visual change, used to detect connection loss.

If SSE disconnects, an HTMX `htmx:sseError` handler swaps the active-tickets table to a "stale data — reconnecting" banner + retries every 5 seconds.

**Project page is ONE template.** Pepper's mockup ships two HTML files (`project.html` for the healthy state, `project-agent-core.html` for the stalled state). These are two visual states of the same data — not two separate templates. v0 ships **one** `project.html.j2` served at `GET /projects/{name}`, parameterized by the daemon's response. The "stalled" treatment is a state of `status_badge` + a class on the active-ticket rows, not a separate page.

### Status badge rules (consumed from v5)

The dashboard does NOT compute status — it consumes the `status` field from the daemon's `GET /projects` and `GET /projects/{name}` responses. The rules live in foreman v5 (`packages/foreman/src/foreman/v5/status.py`), first-match-wins precedence:

1. **`stalled`** (red border) — any ticket in `NeedsHelp` state OR fixer.retry_count ≥ 3 on an active ticket.
2. **`needs-you`** (orange border) — inbox count > 0 OR any ticket labeled `foreman:needs-help`.
3. **`idle`** (muted border) — no state transitions in last 24h.
4. **`healthy`** (default border) — anything else.

This separation means the dashboard CSS encodes the *visual* mapping (which border color goes with which name) and the daemon encodes the *semantic* mapping (which conditions imply which name). Adding a new status badge in v1+ is a coordinated edit across both, with the daemon's `status.py` as the source of truth.

### Action surface (three operator verbs)

The dashboard is deliberately *not* a triage cockpit. The CLI handles retry/skip/set-state/drop/log inspection. Three verbs make it to the dashboard:

- **`+ Plan ⏎`** — primary action button on the portfolio header AND every project detail page header. Opens an inline form prompting for issue URL + a short reason. Submits to `POST /tickets/enqueue`. The form Alpine.js component validates the URL belongs to a configured project before submit; on success, displays a toast and refreshes the affected project tile.
- **`Hold`** — per-ticket affordance on each active-ticket row on the project detail page. Click → prompt for reason → `POST /tickets/{id}/hold`. The ticket row visually transitions to a "held" state immediately (no waiting for the next SSE event).
- **`Reset`** — destructive operator escape. Hidden behind a confirmation modal that explicitly lists what `reset` does (wipes labels, branches, worktrees, the ticket row) and requires typing the ticket id to confirm. Modal submit → `POST /tickets/{id}/reset` with `no_retrigger=true`.

The three buttons share a thin client-side helper (`actions.js`) that posts the request, handles the response, and triggers an HTMX `htmx:trigger` event for the relevant fragment to swap.

### Deferred to v1+ (Pepper's three additions)

During the 2026-06-21 brainstorm, Pepper proposed three surfaces beyond her own mockup:

1. **Adversarial-review queue**: impl PRs awaiting Wren's review (per the standing "Wren reviews every foreman impl PR" rule). Inverse of the "Recently merged" section; prevents Jeff from chasing PRs.
2. **Greenlight pile**: tickets Wren has authored that Jeff hasn't authorized for foreman to start (filed-without-`foreman:plan`). Surfaces "Wren has these queued for me to greenlight" without Jeff having to remember.
3. **Substrate-hot-loop watch**: per-project daemon-restart-count-last-24h + last-error surfacing. Pepper named this her highest-leverage incident-prevention add. The v5 spec adds the `daemon_health` table that this surface consumes; v0 of the dashboard doesn't render the watch yet, but the data is collected from day one.

All three are explicitly v1+, not dropped. The dashboard's macro structure is designed so adding a new sidebar/pulse-bar column is additive (new macro, new partial endpoint), not a refactor.

### Three-container topology (consumed from v5)

The dashboard runs as one of three sibling containers in `docker-compose.yml`, defined in the v5 spec. The dashboard container's responsibilities:

- Slim Python image (`python:3.13-slim` base) with the FastAPI app + `static/`.
- Reads `FOREMAN_API_URL` env (defaults to `http://foreman-daemon:8765` in compose).
- Reads `DASHBOARD_BIND` env (defaults to `0.0.0.0:8000` in compose; the docker-compose port-publish gates the external surface).
- No filesystem mount of the foreman SQLite/Postgres data — the dashboard's only persistence is the daemon's HTTP API.
- No GitHub access — the dashboard reads everything through the daemon.

### Auth model

v0 ships with three supported configurations:

1. **Localhost only** (default): dashboard binds `127.0.0.1:8000`. Operator SSHes or runs the browser on the same host.
2. **Tailnet**: dashboard binds `0.0.0.0:8000` behind Tailscale's network ACLs. The daemon's shared-secret config gates mutations; reads are unauthenticated if the operator opts.
3. **Tailscale Funnel** (public HTTPS): same shape as tailnet, but exposed via Tailscale Funnel for off-network access. **Not recommended for v0** — explicit warning in the runbook. Requires the daemon's `auth_reads_too = true` setting.

The dashboard reuses the daemon's auth model exactly. If a mutation request to the daemon requires `Authorization: Bearer <secret>`, the dashboard adds it from its own env (`FOREMAN_API_SECRET`).

### Repo layout

The dashboard lives as a new top-level package in the foreman monorepo:

```
packages/
  foreman/
    src/foreman/v5/...            # daemon + HTTP control plane
  foreman-dashboard/
    src/foreman_dashboard/
      __init__.py
      app.py                       # FastAPI app
      client.py                    # ForemanClient (typed httpx wrapper)
      routes/
        portfolio.py               # GET /, GET /_partials/project-grid
        project.py                 # GET /projects/{name}, partials
        actions.py                 # POST /actions/enqueue|hold|reset proxies
      templates/
        base.html.j2
        portfolio.html.j2
        project.html.j2
        partials/
          project_tile.html.j2
          issue_row.html.j2
          role_health_row.html.j2
          metric_tile.html.j2
          repo_switcher.html.j2
          pulse_bar.html.j2
      static/
        dashboard.css              # Pepper's CSS verbatim
        actions.js                 # 30-50 LOC HTMX/Alpine glue
        vendor/
          htmx-2.0.0.min.js
          alpine-3.13.0.min.js
    tests/
      ...
    Dockerfile
    pyproject.toml
```

The dashboard is a sibling package, not a sub-package of foreman. Rationale: it has different deps (`jinja2`, `httpx` async, no `asyncpg`), a different release cadence in principle, and shipping it as a separate wheel makes the docker layer caching cleaner.

### Implementation order

1. **Repo scaffolding** + Dockerfile + `pyproject.toml`.
2. **`ForemanClient`** typed wrapper around `httpx.AsyncClient`. Pydantic models match the v5 HTTP contract.
3. **Static assets**: copy Pepper's CSS into `static/dashboard.css`; vendor htmx + alpine.
4. **Base template + portfolio page** + project-grid partial. Tested with a fake `ForemanClient` returning fixture project data.
5. **Project detail page** (templated, single file) + partials. Tested with fixture data covering healthy + stalled states.
6. **SSE wiring** on the project detail page. Test with a stub SSE endpoint that emits scripted events.
7. **`+ Plan` form** + `Hold` button + `Reset` modal. Action POSTs go through the dashboard's `/actions/*` proxy routes (which add the daemon's shared secret) to the daemon's `/tickets/*` endpoints.
8. **Daemon-version handshake** on every page load. Refuse to render against non-v5.
9. **Auth wiring** — env-driven, three configurations.
10. **docker-compose integration** — sibling service to foreman-daemon and postgres (compose lives in the foreman v5 spec).
11. **Operator runbook** in `packages/foreman-dashboard/README.md`.
12. **`just check` clean** — `ruff`, `mypy`, `pytest`, coverage gate. Adversarial-review PR pass before merge.

## Sub-requests

1. **Scaffold `packages/foreman-dashboard/`** as a sibling package in the foreman monorepo. `pyproject.toml` with deps (`fastapi`, `jinja2`, `httpx`, `pydantic`, `uvicorn`, `sse-starlette` for the SSE *consumer* side if needed). Dockerfile based on `python:3.13-slim`.
2. **Extract Pepper's CSS verbatim** to `static/dashboard.css`. Source files: `dashboard.html` + `project.html` + `project-agent-core.html` from the 2026-06-21 zip delivery. Vendor htmx-2.0.0 and alpine-3.13.0 minified into `static/vendor/`.
3. **`ForemanClient`** — typed async wrapper around `httpx.AsyncClient`. One method per v5 endpoint. Pydantic models per response. Tests with `respx` for the HTTP mocking.
4. **Base template + brand header + repo-switcher macro.** Tested via `pytest` + `httpx` test client against the FastAPI app with a fake `ForemanClient`.
5. **Portfolio page (`GET /`)** + project-grid partial (`GET /_partials/project-grid`). The page is the shell; the partial is the HTMX swap target. Tested with fixture project data + status-precedence ordering.
6. **`project_tile` Jinja2 macro.** Status badge, ticket counts, last-activity timestamp. Tested with table-driven fixture data covering each status.
7. **Project detail page (`GET /projects/{name}`)** + partials: `pulse_bar` (2-column: role health + metrics), `role_health_row`, `metric_tile`, `issue_row`. Tested with fixture data covering healthy + stalled states from the same template.
8. **SSE consumer wiring** on the project detail page. Test plan: spin up the FastAPI dashboard, point it at a stub daemon that emits scripted SSE, verify the DOM updates as expected via Playwright (or a similar headless browser test).
9. **`+ Plan ⏎` inline form.** Alpine.js for client-side URL validation. POSTs to `/actions/enqueue` which proxies to the daemon. Tested with fixture daemon + a failing URL + a passing URL.
10. **`Hold` button per active-ticket row.** Click → reason prompt → POST. Tested.
11. **`Reset` modal.** Type-the-ticket-id confirmation. POST with `no_retrigger=true`. Tested.
12. **Daemon-version handshake.** On every page render, fetch `GET /health`; if `foreman_version != "v5"`, render an error template. Tested with daemon stubs returning v4 and v5.
13. **Auth wiring.** Env-driven (`FOREMAN_API_URL`, `FOREMAN_API_SECRET`, `DASHBOARD_BIND`). Tested with all three configurations (localhost, tailnet, funnel).
14. **docker-compose integration** documented in the dashboard README + verified end-to-end against a real local Postgres + foreman-daemon + foreman-dashboard stack.
15. **Operator runbook** at `packages/foreman-dashboard/README.md`. Covers cold start, auth configuration, version-mismatch error troubleshooting.
16. **Adversarial-review PR pass** before merge per the standing rule.

## Open questions for Jeff's spec review

Resolve before transition to writing-plans:

1. **Metric tile provenance.** Pepper's mockup lists four metric tiles (`PRs merged`, `Runs total`, `Fixer retries`, `Lead time p50`). The v5 spec adds aggregation in `packages/foreman/src/foreman/v5/metrics.py`. Confirm the four tiles are the right starting set; nothing in the brainstorm contradicted them, but I want to lock the list before the implementation plan codifies it.
2. **Repo switcher.** The dropdown in Pepper's header lets the operator jump to a project. v0 question: include every project in the config (3 today), or only projects with detail pages (also 3 today)? Recommend including all configured projects since the auto-discovery rule makes "configured but no activity" possible; the dropdown then naturally surfaces them.
3. **Hold-mid-tick semantics.** When the operator clicks `Hold` on a ticket whose current state is mid-execution (e.g. the Worker is actively running), what happens? The foreman v4 `Hold` behavior is "the next tick observes the hold and stops dispatching new work"; in-flight role processes complete normally. Confirm this is acceptable for v0 (the alternative is killing the role subprocess, which I'd defer to v1+ as a separate "Cancel" verb).
4. **Auth default for v0.** Default is `127.0.0.1` localhost. For the way Jeff actually uses this (one operator, one host, optional tailnet for remote check-ins from his laptop), is the default right, or should v0 ship with the tailnet config as the documented happy-path?
5. **Auto-deletion of completed tickets from the "recently merged" section.** No explicit retention rule today. Recommend showing the last 10 merged-in-the-last-7-days; older entries fall off. Confirm or override.

## Adversarial review (self)

Before transition to writing-plans, this spec gets the standing adversarial-review pass.

1. **SSE in HTMX is the right call, but operators behind reverse proxies may break it.** Tailscale Funnel does NOT support long-lived SSE on the free tier (per Tailscale docs as of mid-2026); the dashboard would fall back to polling. Mitigation: detect SSE failure via `htmx:sseError` and downgrade to polling automatically. Document the downgrade as a known v0 behavior.
2. **The dashboard is a third writer to the daemon (via HTTP).** If the daemon's HTTP API has a bug in mutation handling, the dashboard surfaces it before the CLI does. Mitigation: contract tests at the daemon side cover the surface; dashboard adds no new state semantics.
3. **The `+ Plan` form requires the operator to know the issue URL.** A v1+ improvement is autocomplete from GitHub. v0 punts — the URL paste flow is acceptable for Jeff's actual workflow (he files the issue in GitHub first, copies the URL, then opens the dashboard).
4. **No telemetry / page-view tracking.** v0 has no observability into operator UX. Mitigation: the daemon's `/audit-log.jsonl` already records every mutation request, with `metadata.source = "dashboard"` distinguishing dashboard calls from CLI calls. That's enough to answer "is the dashboard getting used."
5. **Pepper's mockup ships with multiple projects whose status is speculative** (gstack, pepper-discord, daku-press, niwc-jazz). These drop out of v0 by the config-driven scope rule. Confirm with Pepper that the speculation was illustrative, not load-bearing — already done in the 2026-06-21 closing envelope; her response acknowledged the trim.

## Acceptance criteria checklist

- [ ] `GET /` renders the portfolio page with one tile per configured project.
- [ ] Project tiles are sorted by status precedence (stalled → needs-you → healthy → idle), within-category by recent activity descending.
- [ ] Portfolio page polls every 30s via HTMX; swap is fragment-scoped, not full-page.
- [ ] Clicking a tile navigates to `GET /projects/{name}`.
- [ ] Project detail page renders for any configured project from a single `project.html.j2` template.
- [ ] Project detail page subscribes to the daemon's SSE stream; tick counters + role activity + ticket rows update live.
- [ ] On SSE disconnect, page shows a "reconnecting" banner and retries every 5s.
- [ ] `+ Plan ⏎` inline form submits to `POST /actions/enqueue`, which proxies to the daemon. On success, project tile refreshes with the new ticket.
- [ ] `Hold` button per active-ticket row submits to `POST /actions/hold`. Row updates immediately.
- [ ] `Reset` button is hidden behind a type-the-ticket-id confirmation modal. Submit calls the daemon's reset endpoint with `no_retrigger=true`.
- [ ] Daemon-version handshake: every page fetches `GET /health` and refuses to render against non-v5.
- [ ] Pepper's CSS is mounted verbatim as `static/dashboard.css`; no Tailwind, no build step.
- [ ] HTMX 2.0.x and Alpine 3.13.x are vendored as static files; no CDN dependency.
- [ ] docker-compose integrates the dashboard as a third service alongside foreman-daemon + postgres.
- [ ] Three auth configurations work end-to-end: localhost-only, tailnet, funnel (warning-documented).
- [ ] Operator runbook in `packages/foreman-dashboard/README.md` documents cold start.
- [ ] Full `just check` clean (ruff + mypy + pytest + coverage).
- [ ] Adversarial-review PR pass before merge.

## Out of scope (v0)

- Alerts pane / notifications panel.
- Adversarial-review queue surface (impl PRs awaiting Wren).
- Greenlight pile surface (filed-but-unlabeled tickets).
- Substrate-hot-loop watch surface (daemon restart count + last-error per project).
- Cancel-mid-tick verb (kill the role subprocess).
- Mobile / touch optimization.
- A staging environment.
- Cross-host operator (the dashboard talks to the daemon over HTTP; that's already cross-host capable, but no v0 acceptance criterion exercises it).
- Localization / i18n.

## References

- foreman v5 substrate + HTTP control plane (2026-06-21, sibling spec): `docs/superpowers/specs/2026-06-21-foreman-v5-postgres-http-design.md` — defines the HTTP API this dashboard consumes.
- foreman v4 substrate redesign (2026-06-13): `docs/superpowers/specs/2026-06-13-foreman-v4-substrate-redesign-design.md` — the state machine + observer fan-out the dashboard observes via SSE.
- Pepper's 2026-06-19 HTML mockups (delivered 2026-06-21 via zip): `dashboard.html`, `project.html`, `project-agent-core.html`. CSS extracted verbatim as `static/dashboard.css`.
- Brainstorm hub notes: `~/.wren/Memory/projects/foreman-dashboard/notes.md` — all locked decisions from 2026-06-21.
- Pepper's substrate-hot-loop watch flag (2026-06-21): deferred to v1+; data foundation lives in the v5 `daemon_health` table.
