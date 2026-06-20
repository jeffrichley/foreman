# Spec: require GitHub issue comments on NeedsHelp / Failed / sustained-BLOCKED outcomes (issue #367)

## Goal
Make every operator-significant state transition observable on the GitHub
issue's comment stream — so "the issue page" remains the single answer to
"why did this ticket stop?". Today a role can emit `NEEDS_HELP`, the
state machine can land on `Failed` after a subprocess crash, or a Worker
ticket can sit in `Blocked` for half an hour, and the issue's comment
stream stays empty (the only signal is a label change + a row in
`transitions.jsonl` buried in the container). This spec adds three
narrowly-scoped comment surfaces (in-role escalation, sustained-BLOCKED
observer, terminal-landing observer) plus a Fixer pre-dispatch "received
rejection" comment, all with marker-fenced idempotency so retries / poll
ticks / daemon restarts never duplicate. Tracks
[foreman#367](https://github.com/jeffrichley/foreman/issues/367) and
addresses the concrete instance [foreman#357](https://github.com/jeffrichley/foreman/issues/357)
(NeedsHelp landing with no operator-visible explanation).

## Acceptance criteria

### Shared helper module (single source of truth)

- [ ] NEW file `packages/foreman/src/foreman/roles/_escalation_comment.py`
  exports:
  * `EscalationComment` Pydantic model with three required string fields
    (`why`, `what_tried`, `what_would_unblock`) and one optional field
    `extra_context: str | None = None`. Used as a `BaseModel` nested under
    each role's structured-output schema.
  * Marker constants
    `ESCALATION_MARKER_BEGIN = "<!-- foreman:escalation:begin -->"` and
    `ESCALATION_MARKER_END = "<!-- foreman:escalation:end -->"`. Idempotency
    keys ride inside the begin marker as `ticket=<id>:source=<source>:key=<key>`
    so the existence check is a plain substring scan. Precedent:
    `FINDINGS_BEGIN_MARKER` / `FINDINGS_END_MARKER` in
    `packages/foreman/src/foreman/roles/reviewer.py:105-106`.
  * `build_escalation_comment_body(*, role: str, outcome_label: str,
    summary: str, at: dt.datetime, payload: EscalationComment | None,
    fallback_reason: str | None = None) -> str` — pure function returning
    the Markdown skeleton from the issue body. When `payload is None`
    OR `fallback_reason is not None`, the body explicitly names that
    the role-side prompt did NOT populate the structured field and
    falls back to `summary` as the only available signal.
  * `already_posted_for_key(comments: list[CommentRef], *, source: str,
    key: str) -> bool` — scans the issue's existing comments for the
    `source=<source>:key=<key>` substring in the begin marker; returns
    True iff at least one match is found.
  * `post_escalation_comment(*, host: GitHostProvider, repo_slug: str,
    issue_number: int, role: str, outcome_label: str, summary: str,
    payload: EscalationComment | None, source: str, key: str,
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)) -> bool`
    — fetches the issue's comments via `host.get_issue_comments`,
    short-circuits via `already_posted_for_key`, otherwise builds the
    body via `build_escalation_comment_body` (with the begin marker
    carrying `ticket=<repo>#<N>:source=<source>:key=<key>`) and posts
    via `host.post_issue_comment`. Returns True iff it actually posted.

- [ ] The skeleton produced by `build_escalation_comment_body` matches
  the shape the issue body specifies (with the begin/end markers
  wrapping the entire body):
  ```markdown
  <!-- foreman:escalation:begin ticket=<repo>#<N>:source=<source>:key=<key> -->
  **[<role>] · [<outcome_label>] · [<iso8601 timestamp>]**

  > <summary in one line>

  ## Why
  <payload.why OR "(role-side prompt did not populate; fallback) — <fallback_reason>">

  ## What I tried
  <payload.what_tried OR fallback bullet>

  ## What would unblock this
  <payload.what_would_unblock OR fallback line>

  <payload.extra_context block, omitted when None>

  ---
  *Auto-posted by foreman-<role>-bot. Do not edit; apply the
  `foreman:retry` label on the issue to re-dispatch.*
  <!-- foreman:escalation:end -->
  ```
  The `extra_context` block is rendered only when non-None. Forbidden:
  any `Closes #N` / `Fixes #N` / `Resolves #N` keyword forms inside the
  body — same constraint as `foreman.auto_close.strip_auto_close_keywords`
  (the helper does NOT need to call the stripper because the template is
  controlled; a unit test asserts the rendered template contains none of
  the nine closing keywords as standalone tokens).

### Role output schemas

- [ ] `PlannerOutput` in
  `packages/foreman/src/foreman/schemas/planner.py` gains an optional
  field `escalation_comment: EscalationComment | None = None`. Docstring
  states: "Required-iff-`confidence == 'low'`. The Planner LLM populates
  this when self-escalating; Foreman core renders + posts it on the issue.
  A `pydantic.model_validator` enforces the requirement so a slip surfaces
  at schema-validation time rather than as a missing GitHub comment."
- [ ] `WorkerOutput` in
  `packages/foreman/src/foreman/schemas/worker.py` gains the same
  `escalation_comment: EscalationComment | None = None`. Docstring states:
  "Required-iff-`outcome in {'incomplete', 'spec_invalid'}`. Core
  renders + posts when the Worker self-reports it could not finish."
  The existing `_enforce_outcome_required_fields` validator gains the
  matching presence check.
- [ ] `FixerOutput` in
  `packages/foreman/src/foreman/schemas/fixer.py` gains the same field.
  Docstring states: "Required-iff-`outcome == 'incomplete'` OR
  `confidence == 'low'`. Core renders + posts."
- [ ] `ReviewerOutput` in
  `packages/foreman/src/foreman/schemas/reviewer.py` gains the same
  field, required iff `confidence == 'low'` (Reviewer never
  self-escalates to NEEDS_HELP; its surface is low-confidence on
  CLEAN / NEEDS_FIX). Validator enforces presence in that case.

### Role prompts (six files)

- [ ] `packages/foreman/src/foreman/prompts/planner.md` gains an
  `<escalation_comment>` section between `<process>` and `<self_review>`
  (planner.md uniquely places `<self_review>` BEFORE `<output_schema>`
  — verify by `grep -nE '^<(process|self_review|output_schema)>' planner.md`
  before inserting; current line numbers are 186 / 204 / 220) instructing
  the LLM to populate `escalation_comment` with `why` / `what_tried` /
  `what_would_unblock` whenever `confidence == 'low'`. The section names
  the three-field content requirements from the issue's table verbatim
  ("Why I escalated · What I attempted · What an operator would need
  to do to unblock") and forbids posting via Bash directly (the LLM
  cannot — its tool surface is read-only — but the prompt section
  states this for symmetry with the Worker / Fixer prompts).
- [ ] `packages/foreman/src/foreman/prompts/reviewer.md` gains the same
  `<escalation_comment>` section, gated on `confidence == 'low'`. Content
  requirement: "Why my confidence is low · What additional context would
  help · What scope guardrails would unblock". The Reviewer's tool
  surface includes Bash but only for read-only recon; the section
  forbids direct comment posting via Bash.
- [ ] `packages/foreman/src/foreman/prompts/reviewer_impl.md` gains the
  same section (impl-side mirror).
- [ ] `packages/foreman/src/foreman/prompts/fixer.md` gains an
  `<escalation_comment>` section gated on
  `outcome == 'incomplete'` OR `confidence == 'low'`. Content requirement
  per the issue's table for "Fixer receiving Reviewer rejection": "What
  the rejection said (one-line) · What fix I'm attempting · Scope
  guardrails I'm applying". The Fixer's Bash tool MUST NOT be used to
  call `gh issue comment` — comments are routed via the structured
  field; the section names this explicitly.
- [ ] `packages/foreman/src/foreman/prompts/fixer_impl.md` gains the
  same section (impl-side mirror).
- [ ] `packages/foreman/src/foreman/prompts/worker.md` gains an
  `<escalation_comment>` section gated on
  `outcome in {'incomplete', 'spec_invalid'}`. Content requirement:
  "Why I could not finish · What sub-requests landed and which I
  skipped · What an operator would need to do to unblock". Same Bash
  prohibition as Fixer.

### Role core wiring (Python-side post + fallback)

- [ ] `_run_planner_core` in
  `packages/foreman/src/foreman/roles/planner.py` calls
  `post_escalation_comment` whenever `llm_output.confidence == 'low'`
  (i.e., the run is about to emit a `NEEDS_HELP` outcome from
  `run_planner_cli`). `source="role:planner"`,
  `key=f"state-instance-{<see below>}"`. Because the role subprocess
  does not know its `state_instance_id`, the per-run key uses the spec
  PR number when available and falls back to
  `f"run-{int(start_time*1000)}"` (a deterministic-within-run number) so
  the marker is unique per role run. If `escalation_comment is None`
  on the LLM's output (slip), the helper posts the fallback shape with
  `fallback_reason="planner LLM produced confidence=low but did not
  populate escalation_comment"`. The post happens BEFORE
  `log_planner_run` so a comment-post failure is visible in the daemon
  log without preventing the JSONL row.
- [ ] `_run_reviewer_core` in
  `packages/foreman/src/foreman/roles/reviewer.py` calls
  `post_escalation_comment` whenever `llm_output.confidence == 'low'`.
  `source="role:reviewer-<target>"` (`spec_pr` or `impl_pr`),
  `key=f"pr-{pr_number}-{llm_output.outcome}"`. Fallback applies
  identically.
- [ ] `_run_fixer_core` in
  `packages/foreman/src/foreman/roles/fixer.py` calls
  `post_escalation_comment` whenever
  `llm_output.outcome == 'incomplete'` OR
  `llm_output.confidence == 'low'`.
  `source="role:fixer-<target>"`,
  `key=f"pr-{pr_number}-{llm_output.outcome}"`.
  **Additionally**, BEFORE invoking `provider.run_agent`, the Fixer's
  core posts a pre-dispatch "received rejection" comment with
  `source="fixer-received-rejection"`,
  `key=f"pr-{pr_number}-instance-{reviewer_findings_hash}"` (where
  `reviewer_findings_hash` is a short
  `hashlib.sha1(findings_json.encode()).hexdigest()[:8]` over the
  Reviewer-findings JSON the Fixer extracted). The pre-dispatch comment
  body names the rejection summary (the first 200 chars of the
  Reviewer's `review_comment` prose), the count + severity breakdown
  of findings, and that the Fixer is about to attempt edits. The
  per-findings-hash key guarantees no duplicate posting when the
  Fixer state re-enters on a poll tick before the LLM run completes.
- [ ] `_run_worker_core` in
  `packages/foreman/src/foreman/roles/worker.py` calls
  `post_escalation_comment` whenever
  `final_outcome in {'incomplete', 'spec_invalid'}`.
  `source="role:worker"`,
  `key=f"attempt-{attempt}-{final_outcome}"`. Fallback applies
  identically.

### Sustained-BLOCKED observer (new module)

- [ ] NEW file
  `packages/foreman/src/foreman/v4/observers/sustained_blocked.py`
  defines `SustainedBlockedObserver` consuming `ExecuteCompletedEvent`
  (the only event whose payload carries the `Outcome`). On every
  `BLOCKED`-outcome event:
  1. Compute the "blocked-reason signal" — for v1 this is
     `outcome.summary` truncated to its first 80 chars
     (`outcome.summary` is the human-readable BLOCKED cause; existing
     emitters produce stable strings like "impl PR open, CI still in
     flight" / "impl PR not yet mergeable (CI pending or merge
     conflict)").
  2. Walk the ticket's `state_instances` via
     `repo.list_state_instances_for_ticket(ticket_id)` in reverse
     sequence; collect the contiguous suffix whose
     `outcome_kind == OutcomeKind.BLOCKED` AND whose
     blocked-reason-signal matches step 1. Take the
     `execute_completed_at` of the EARLIEST row in that suffix as
     `first_blocked_at`.
  3. If `event.at - first_blocked_at > timedelta(minutes=15)`:
     a. Fetch issue comments via the project's
        `GitHostProvider.get_issue_comments`.
     b. Build the dedup key
        `f"ticket-{ticket_id}-state-{state_name}-reason-{reason_hash}"`
        where `reason_hash = hashlib.sha1(signal.encode()).hexdigest()[:8]`.
        Call `already_posted_for_key(comments, source="sustained-blocked",
        key=key)`. If True, do nothing.
     c. Otherwise call `post_escalation_comment` with
        `source="sustained-blocked"`, `key=<as above>`,
        `payload=EscalationComment(why=f"State {state_name} has been
        BLOCKED for ≥15 minutes on the same async signal: {signal!r}",
        what_tried="Polling the BLOCKED state on each daemon tick;
        the BLOCKED-exempt retry-cap is by-design and not consuming
        max_state_attempts.", what_would_unblock="Check the external
        async signal (CI status, merge-queue verdict, etc.) and apply
        `foreman:retry` once it has converged, OR triage the BLOCKED
        cause if it is genuinely stuck.")`.
  4. Comment-post failures are caught and logged via
     `logger.exception` so an isolated GitHub 5xx doesn't propagate
     into the EventBus (precedent: `LabelObservabilityObserver`'s
     deliberate no-catch policy is overridden here because the
     observer's job IS the post; losing the post on transient host
     failure should not crash the daemon).
- [ ] The observer accepts a `host_for_project: Callable[[str],
  GitHostProvider]` (resolves the per-project GitHostProvider — the
  project name is on `repo.get_ticket(ticket_id).project`) at
  construction. Same shape as the wiring for
  `LabelObservabilityObserver`'s `writer` parameter.
- [ ] The 15-minute threshold is a module-level constant
  `SUSTAINED_BLOCKED_THRESHOLD = dt.timedelta(minutes=15)` and the
  observer's constructor accepts an override
  `threshold: dt.timedelta = SUSTAINED_BLOCKED_THRESHOLD` so tests can
  pass `timedelta(seconds=0.001)` without monkey-patching.

### Terminal-landing observer (subprocess-crash + unexpected-FAILED fallback)

- [ ] NEW file
  `packages/foreman/src/foreman/v4/observers/terminal_landing.py`
  defines `TerminalLandingObserver` consuming `StateEnteredEvent`.
  On entry to a state whose name is in `{"NeedsHelp", "Failed"}`:
  1. Fetch the issue's existing comments.
  2. Build dedup key `f"ticket-{ticket_id}-instance-{instance_id}"`.
  3. If `already_posted_for_key(comments, source="terminal-landing",
     key=key)` — short-circuit. (The in-role escalation comment carries
     `source="role:<role>"`, so the dedup keys are DISJOINT — an
     in-role comment does NOT suppress the terminal-landing comment,
     and vice versa. This is intentional: if the role-side path
     posted, the terminal observer still posts a "ticket landed on
     <terminal>" confirmation; for sustained-BLOCKED → NeedsHelp via
     cap exhaustion, the in-role path never ran, so the terminal
     observer is the only signal source.)
     **Correction (avoid double-post in the happy path):** the
     terminal observer ALSO checks for any comment whose marker
     carries `source^="role:"` AND has been posted within the last
     5 minutes; if found, it SKIPS the post — the role-side path
     already produced operator-visible context. The 5-minute window
     is a defensive heuristic; document it in the observer's
     docstring.
  4. Reads the ticket's most recent `state_instances` row via
     `repo.list_state_instances_for_ticket(ticket_id)[-2]` (the
     LANDING row is `[-1]` from the just-fired event; the cause is
     the row before). Posts via `post_escalation_comment` with
     `source="terminal-landing"`, `payload=EscalationComment` built
     from the prior row's `failure_phase` / `failure_reason` /
     `outcome_payload` so the comment names WHY the state machine
     landed terminally (subprocess crash, retry cap, base-ref guard,
     etc.). When the prior row's `outcome_payload` exists and carries
     a non-empty `summary`, that summary becomes the comment's
     `> summary in one line`.
- [ ] The observer accepts the same
  `host_for_project: Callable[[str], GitHostProvider]` as
  `SustainedBlockedObserver` plus a
  `clock: Callable[[], dt.datetime]` (defaults to
  `lambda: dt.datetime.now(dt.UTC)`) for the 5-minute-window check.

### Bootstrap wiring

- [ ] `packages/foreman/src/foreman/v4/bootstrap.py`'s
  `bootstrap_cli_context` (the existing function that builds the
  per-project providers and constructs the `Daemon`) constructs both
  new observers, wires them onto the same EventBus that already
  receives `LabelObservabilityObserver` /
  `StructuredLogObserver` / `EventArchiveObserver` /
  `MetricsObserver`. The `host_for_project` callable is built from
  the existing `per_project_providers` map (each value is already a
  built `PyGithubGitProvider`; we need its sibling
  `GitHostProvider` — the bootstrap currently constructs the v4
  `GitProvider` only, so add a parallel
  `per_project_git_hosts: dict[str, GitHostProvider]` map keyed by
  project name, built by calling
  `build_role_resources(role="orchestrator",
  app_id=config.orchestrator.app_id,
  private_key_path=config.orchestrator.private_key_path, ...)`
  per project. NOTE: unlike the four role cores (Planner / Reviewer /
  Fixer / Worker), which read their App credentials from
  `config.apps.<role>.app_id` / `config.apps.<role>.private_key_path`,
  the orchestrator's credentials live at the TOP-LEVEL `config.orchestrator`
  block — there is no `config.apps.orchestrator` (see
  `v4/config.py:308` and `v4/identity.py:247-249`). Do NOT copy the
  role-core call shape verbatim or it will crash at startup.
  If the orchestrator identity is not configured for a given project,
  the helper logs a warning and skips wiring the observer for that
  project; this preserves the existing "additive change" discipline.

### Subprocess crash / timeout fallback (Python-side, NOT in-role)

- [ ] `SubprocessRoleDispatcher.dispatch` in
  `packages/foreman/src/foreman/v4/subprocess_dispatcher.py` already
  raises `RoleSubprocessError` on TIMEOUT / non-zero-exit-without-outcome.
  This spec does NOT change the dispatcher. The `TerminalLandingObserver`
  (above) is the unified surface that fires for both subprocess crash
  AND in-state failure — the dispatcher's exception propagates up
  through `WorkerPool._run_transition`, the state machine records the
  failure, transitions to FailedState, fires `StateEnteredEvent(Failed)`,
  and the observer posts. The comment body includes the exception
  message (truncated to 500 chars) via the prior state-instance row's
  `failure_reason` field — which `state.py:303-307` already populates
  with `repr(exc)`.
- [ ] The subprocess log path
  (`<log_dir>/<role-base>/<ticket_id>__<iso>.log`) is mentioned in
  the terminal-landing comment's `extra_context` so operators can
  pull the full stderr without spelunking. The path is computed
  identically to `subprocess_dispatcher._fs_safe_iso_utc` —
  reusing the constants ensures the comment names the actual log file.

### Idempotency invariants

- [ ] All four marker `source` strings are disjoint by construction:
  `role:planner`, `role:reviewer-spec_pr`, `role:reviewer-impl_pr`,
  `role:fixer-spec_pr`, `role:fixer-impl_pr`, `role:worker`,
  `fixer-received-rejection`, `sustained-blocked`, `terminal-landing`.
  A test asserts the disjoint set.
- [ ] `already_posted_for_key` performs a literal substring search on
  each comment body (looking for `source=<source>:key=<key>` inside the
  begin marker). It does NOT parse comment metadata or query GitHub for
  bot identity — the marker IS the contract. Foreman role-bot
  self-comments are NOT filtered out at the lookup step (we WANT to
  see them — they're our own past posts).
- [ ] When `host.get_issue_comments` itself fails (GitHub 5xx, rate
  limit), the helper logs `logger.exception` and PROCEEDS to post —
  a duplicate comment is preferable to a missing comment under the
  "issue is the answer" invariant.

### Tests

- [ ] NEW test
  `packages/foreman/tests/v4/roles/test_escalation_comment.py` exercising
  `_escalation_comment.py`:
  * `test_build_body_with_payload_renders_all_three_sections`
  * `test_build_body_fallback_names_missing_field`
  * `test_already_posted_for_key_matches_substring`
  * `test_already_posted_for_key_misses_when_source_differs`
  * `test_post_escalation_short_circuits_when_already_posted`
  * `test_post_escalation_proceeds_on_get_comments_failure` (asserts
    the post still fires when the fetch raises)
  * `test_body_contains_no_github_closing_keywords` (regression guard
    for foreman#63 mirror)
- [ ] NEW test
  `packages/foreman/tests/v4/observers/test_sustained_blocked_observer.py`:
  * `test_first_blocked_event_below_threshold_posts_nothing` — fakes
    a single BLOCKED row whose `execute_completed_at` is `now - 5min`;
    asserts the observer's host-mock was NOT called.
  * `test_sustained_blocked_above_threshold_posts_once` — 32 BLOCKED
    events 30s apart, total span 16 min; asserts the observer posts
    exactly ONE comment (regression guard for the issue's "not 32
    ticks worth" requirement).
  * `test_distinct_reasons_each_get_one_comment` — same ticket, two
    different `outcome.summary` strings, each crossing the threshold;
    asserts two comments, one per (ticket, reason).
  * `test_clean_outcome_resets_the_run` — BLOCKED → BLOCKED → CLEAN
    → BLOCKED; asserts the second BLOCKED run's `first_blocked_at`
    is the third event, not the first.
- [ ] NEW test
  `packages/foreman/tests/v4/observers/test_terminal_landing_observer.py`:
  * `test_failed_landing_posts_comment_with_failure_reason` — seeds
    a `StateEnteredEvent("Failed")` after a prior row whose
    `failure_phase="execute"`, `failure_reason="repr(subprocess crash)"`;
    asserts the posted body contains the failure_reason.
  * `test_recent_role_comment_suppresses_terminal_landing_post` —
    seeds the issue's comment list with a `source=role:worker` marker
    posted 30 seconds ago; asserts the observer SKIPS the post.
  * `test_needshelp_landing_includes_log_path_in_extra_context` —
    asserts the rendered comment body names
    `<log_dir>/<role>/<ticket_id>__<iso>.log` (or the convention
    string).
- [ ] NEW test
  `packages/foreman/tests/v4/roles/test_planner_low_confidence_posts_comment.py`:
  patches `host.post_issue_comment` on a `MagicMock`; runs
  `_run_planner_core` with a forced-low-confidence `PlannerOutput`
  fixture; asserts the mock was called once with a body containing
  the begin marker + `source=role:planner`.
- [ ] NEW test
  `packages/foreman/tests/v4/roles/test_planner_fallback_when_field_missing.py`:
  same but the `PlannerOutput` has
  `confidence='low'` AND `escalation_comment=None` — asserts the
  posted body contains the fallback prose
  ("role-side prompt did not populate").
- [ ] NEW tests in the equivalent files for Reviewer, Fixer, Worker
  (one happy-path + one fallback path per role; 6 new test functions
  total across three new files alongside the Planner pair).
- [ ] NEW test
  `packages/foreman/tests/v4/roles/test_fixer_received_rejection_pre_dispatch.py`:
  asserts the Fixer's core posts the "received rejection" comment
  BEFORE invoking `provider.run_agent` (use a `MagicMock` order
  assertion via `mock_calls`). A second test asserts the
  per-`findings-hash` key prevents a duplicate on re-dispatch.
- [ ] MODIFY
  `packages/foreman/tests/v4/test_subprocess_dispatcher.py` (existing
  file) — append `test_subprocess_timeout_routes_to_terminal_landing_comment`
  that drives a TIMEOUT through the full WorkerPool → state machine →
  observer chain and asserts the terminal-landing observer's mock host
  was called once with the timeout marker.
- [ ] MODIFY `packages/foreman/tests/v4/test_bootstrap.py` — assert
  both new observers are wired onto the EventBus after
  `bootstrap_cli_context` runs.

### Documentation

- [ ] `docs/RUNBOOK.md` gains a new section "Operator-visible
  escalation comments" between the existing "Provider transient
  failures and backoff suspension" section and "Recovery: daemon
  won't start". The section names: the four `source` namespaces,
  how to read the marker for triage, the 15-minute sustained-BLOCKED
  threshold, and `foreman:retry` as the standard re-dispatch verb.

## Approach
**Pattern naming (Decision 4 — calibrated lens).** Three patterns
apply, plus one Google principle:

1. **Observer Pattern (GoF)** — `SustainedBlockedObserver` and
   `TerminalLandingObserver` are new observers on the existing
   `EventBus`, sibling to `LabelObservabilityObserver` and
   `StructuredLogObserver`. The mechanism is already in place; we're
   adding two new subscribers, not inventing a notification surface.
2. **Marker-fenced idempotency** — every posted comment carries an
   HTML-comment marker (`<!-- foreman:escalation:begin
   ticket=...:source=...:key=... -->`). Existence-check is a literal
   substring scan over `host.get_issue_comments(...)`. Direct precedent:
   the Reviewer's `FINDINGS_BEGIN_MARKER` / `FINDINGS_END_MARKER`
   handshake in `roles/reviewer.py:105-106` and
   `roles/fixer.py:75-80`. The substring shape generalizes the
   Reviewer's pattern (the Reviewer's markers are static; ours embed
   the dedup key) without inventing a new mechanism.
3. **"Make the right thing easy" (Google SRE)** — comment
   construction lives in ONE module
   (`roles/_escalation_comment.py`); the four role cores + two
   observers + Fixer pre-dispatch all call
   `post_escalation_comment` with their own `source` / `key` /
   `payload`. A future operator who wants to tune the body skeleton,
   the marker shape, or the post-then-skip semantics edits one file.

**Why the LLM populates `escalation_comment` as structured output
rather than posting via Bash directly.** The Planner and Reviewer
have READ-ONLY tool surfaces — they cannot post comments via the
LLM. The Worker and Fixer have Bash, but routing comments through
structured-output → Python core has three benefits over
LLM-direct-post: (a) idempotency is enforceable in one place (the
helper's marker scan), (b) tests can assert on the post by mocking
one host call rather than the LLM's tool-use, (c) the role's
existing structured-output schema is the natural contract slot —
adding a sibling field is symmetric with how `pr_body` /
`work_comment` / `review_comment` already work. The trade-off:
the LLM cannot post a comment partway through its run; it must
populate the field and exit. That's the right shape — partial-run
posts would mean partial-run state, and v4 explicitly avoids that.

**Why dedup keys are per-source (not global) and per-state-instance
(not per-ticket).** A ticket can legitimately land on NeedsHelp once
per Anthropic outage and once per genuine spec ambiguity; collapsing
those to one comment loses operator-visible signal. The marker key
includes the state-instance signature so retries WITHIN the same
state-instance no-op, but a fresh dispatch posts fresh signal. The
`sustained-blocked` source is the exception: the key is per
(ticket, blocked-reason-signal) so the issue body stays clean even
across a 4-hour CI wait. Both shapes appear in the test matrix.

**Why a separate `TerminalLandingObserver` rather than folding into
the in-role escalation path.** Subprocess crash, TIMEOUT, and
retry-cap exhaustion all transition to FailedState / NeedsHelpState
WITHOUT the role's structured-output path running. The in-role
`post_escalation_comment` cannot fire in those cases — the role
subprocess died before producing structured output. The
terminal-landing observer is the unified backstop: it fires on
EVERY transition into a terminal state and posts a comment iff the
in-role path didn't (checked via the 5-minute window heuristic on
recent `source^="role:"` markers). The double-source design keeps
the operational invariant "every terminal landing has a comment"
true even when the role-side path is dead.

**Why `outcome.summary` as the sustained-BLOCKED reason signal
rather than `outcome.kind` alone.** `OutcomeKind.BLOCKED` is one
value; the SAME `BLOCKED` outcome means different things in
different states (Implementing-BLOCKED → "CI in flight on impl
PR"; Merging-BLOCKED → "PR not mergeable, polling"). The
threshold-crossing semantics differ — operators care about whether
the underlying signal is making progress, not whether the state
machine is polling. The `summary` field is the human-readable
signal; existing emitters produce stable strings (verified in
`states/implementing.py` + `states/merging.py`); hashing the
first 80 chars gives a stable per-reason key without coupling to
the `details` dict's per-state shape.

**Why both observers wire from `bootstrap_cli_context` rather than
auto-registering at module load.** The observers need per-project
`GitHostProvider` instances built from
`V4Config.apps.orchestrator` — that config is read in the
bootstrap path; module-level auto-register would force the
import to load config, which couples the observer module to the
runtime. Wiring at bootstrap mirrors how
`LabelObservabilityObserver` and the `MetricsObserver` are
attached today.

## Sub-requests (topologically sorted)
1. Add `_escalation_comment.py` (model + markers + body builder +
   already-posted scanner + post helper). Pure module, no role-core
   imports yet.
2. Extend the four role schemas (`PlannerOutput` / `ReviewerOutput` /
   `FixerOutput` / `WorkerOutput`) with the new
   `escalation_comment: EscalationComment | None` field plus the
   validator presence check.
3. Update the six role prompts (`planner.md` / `reviewer.md` /
   `reviewer_impl.md` / `fixer.md` / `fixer_impl.md` / `worker.md`)
   with the new `<escalation_comment>` section.
4. Wire `_run_planner_core` to call `post_escalation_comment` on the
   low-confidence path. Add the fallback shape for the missing-field
   slip.
5. Wire `_run_reviewer_core` identically on its low-confidence path.
6. Wire `_run_fixer_core` on incomplete / low-confidence, AND add the
   pre-dispatch "received rejection" post.
7. Wire `_run_worker_core` on incomplete / spec_invalid.
8. Add `SustainedBlockedObserver` consuming `ExecuteCompletedEvent`
   with the per-reason scan + 15-minute threshold + marker dedup.
9. Add `TerminalLandingObserver` consuming `StateEnteredEvent` for
   `NeedsHelp` / `Failed` with the 5-minute role-comment window
   heuristic.
10. Extend `bootstrap_cli_context` to construct + wire both
    observers (and the `per_project_git_hosts` map they need).
11. Write the unit + integration tests enumerated in Acceptance.
12. Add the RUNBOOK section.

## File-level changes
- `packages/foreman/src/foreman/roles/_escalation_comment.py` —
  NEW: `EscalationComment` model + marker constants + body builder +
  already-posted scanner + `post_escalation_comment` orchestrator.
- `packages/foreman/src/foreman/schemas/planner.py` — add the
  optional `escalation_comment` field + the validator presence check
  gated on `confidence`.
- `packages/foreman/src/foreman/schemas/reviewer.py` — same field +
  validator (low-confidence gate).
- `packages/foreman/src/foreman/schemas/fixer.py` — same field +
  validator (`incomplete` OR low-confidence gate).
- `packages/foreman/src/foreman/schemas/worker.py` — same field +
  extend `_enforce_outcome_required_fields` to require it on
  `incomplete` / `spec_invalid`.
- `packages/foreman/src/foreman/prompts/planner.md` — new
  `<escalation_comment>` section.
- `packages/foreman/src/foreman/prompts/reviewer.md` — same.
- `packages/foreman/src/foreman/prompts/reviewer_impl.md` — same.
- `packages/foreman/src/foreman/prompts/fixer.md` — same.
- `packages/foreman/src/foreman/prompts/fixer_impl.md` — same.
- `packages/foreman/src/foreman/prompts/worker.md` — same.
- `packages/foreman/src/foreman/roles/planner.py` — `_run_planner_core`
  posts via the helper when `llm_output.confidence == 'low'`.
- `packages/foreman/src/foreman/roles/reviewer.py` —
  `_run_reviewer_core` posts via the helper on low-confidence.
- `packages/foreman/src/foreman/roles/fixer.py` — `_run_fixer_core`
  posts the pre-dispatch "received rejection" comment AND posts the
  escalation comment on incomplete / low-confidence.
- `packages/foreman/src/foreman/roles/worker.py` —
  `_run_worker_core` posts via the helper on incomplete /
  spec_invalid.
- `packages/foreman/src/foreman/v4/observers/sustained_blocked.py` —
  NEW: `SustainedBlockedObserver` + `SUSTAINED_BLOCKED_THRESHOLD`
  constant.
- `packages/foreman/src/foreman/v4/observers/terminal_landing.py` —
  NEW: `TerminalLandingObserver` + 5-minute window constant.
- `packages/foreman/src/foreman/v4/observers/__init__.py` — re-export.
- `packages/foreman/src/foreman/v4/bootstrap.py` — construct +
  wire both observers; build `per_project_git_hosts` map.
- `packages/foreman/tests/v4/roles/test_escalation_comment.py` —
  NEW: helper unit tests.
- `packages/foreman/tests/v4/observers/test_sustained_blocked_observer.py`
  — NEW: observer behavior tests.
- `packages/foreman/tests/v4/observers/test_terminal_landing_observer.py`
  — NEW: observer behavior tests.
- `packages/foreman/tests/v4/roles/test_planner_low_confidence_posts_comment.py`
  — NEW: per-role escalation test (Planner).
- `packages/foreman/tests/v4/roles/test_planner_fallback_when_field_missing.py`
  — NEW: per-role fallback test (Planner).
- `packages/foreman/tests/v4/roles/test_reviewer_low_confidence_posts_comment.py`
  — NEW (one happy + one fallback in same file).
- `packages/foreman/tests/v4/roles/test_fixer_escalation_paths.py`
  — NEW (escalation + fallback).
- `packages/foreman/tests/v4/roles/test_fixer_received_rejection_pre_dispatch.py`
  — NEW.
- `packages/foreman/tests/v4/roles/test_worker_escalation_paths.py`
  — NEW (escalation + fallback).
- `packages/foreman/tests/v4/test_subprocess_dispatcher.py` — EXTEND
  with `test_subprocess_timeout_routes_to_terminal_landing_comment`.
- `packages/foreman/tests/v4/test_bootstrap.py` — EXTEND with the
  "both observers wired" assertion.
- `docs/RUNBOOK.md` — new "Operator-visible escalation comments"
  section.

## Alternatives considered
1. **Let the LLM call `gh issue comment` via Bash directly (Worker /
   Fixer only).** Rejected: the Planner and Reviewer have read-only
   tool surfaces, so this couldn't be the unified approach for all
   roles. Half-Python-half-LLM splits the contract across two layers
   and makes idempotency hard to enforce (the marker scan would have
   to live in both layers). Single-source-of-truth in Python is
   cleaner.
2. **Post comments from the state-machine `transition()` boundary
   instead of from observers.** Rejected: `transition()` is already
   the orchestration backbone for the five lifecycle hooks; adding
   GitHub I/O there violates SRP (the state machine knows nothing
   about hosts today — `GitProvider` and `GitHostProvider` are
   passed in via `StateContext` and only consumed by `MergingState`).
   Observers are the existing seam for cross-cutting concerns.
3. **Persist "comment already posted" state in SQLite.** Rejected:
   the marker-comment scan on `host.get_issue_comments` already
   provides persistence (survives daemon restart, no schema change,
   uses GitHub itself as the source of truth). A new SQLite table
   would duplicate state and risk drift. Cost: one extra GitHub API
   call per post (cheap; observers run on tick boundaries, not in
   a hot loop).
4. **Skip the in-role post entirely; rely on `TerminalLandingObserver`
   alone.** Rejected: low-confidence Planner / Reviewer don't
   terminally land — they post a spec PR / a `needs_fix` review and
   the loop continues with reduced confidence the operator should
   see NOW, not when (if) the ticket later lands on NeedsHelp. The
   in-role path captures these mid-loop signals that the
   terminal-landing observer misses by design.
5. **Threshold the sustained-BLOCKED comment on `count_state_instances`
   rather than wall-clock duration.** Rejected: poll cadence varies
   (the daemon's tick interval is configurable; tickets can be
   suspended via `next_action_at` and skip ticks). Wall-clock is the
   operator-meaningful axis. Foreman #361's
   `next_action_at` already proves the daemon reasons about
   wall-clock cleanly.

## Open questions
None — every acceptance criterion traces to a file path and a
named pattern in the worktree. One design choice deliberately
left to the implementing Reviewer's judgment: whether the
5-minute "recent role-comment window" in `TerminalLandingObserver`
should be configurable (currently a constant). For v1 a constant
matches the SUSTAINED_BLOCKED_THRESHOLD shape; the threshold can
be promoted to constructor parameter cheaply if a future ticket
needs it.

## Out of scope
- Threading comments onto PR review threads. Per the issue body —
  PR comments are a separate concern; this spec only touches the
  ISSUE comment stream.
- Old-comment pruning. The issue explicitly accepts unbounded
  comment growth.
- Cross-project escalation routing (Slack / webhook). Sibling to
  jeffrichley/agent_core#195; explicitly out of scope per the
  issue body.
- Comment moderation / spam protection. Single-operator setup;
  explicitly out of scope.
- Removing or refactoring the existing
  `handle_unhandled_role_exception` helper in
  `foreman/roles/__init__.py`. That helper posts on the
  exception path BEFORE the subprocess dies; this spec adds the
  terminal-landing observer as the AFTER-the-fact backstop. The
  two surfaces co-exist intentionally — the existing helper
  catches in-process Python-side crashes (the runaway-burn
  defense in foreman#229); the new observer catches the cases
  the existing helper missed (TIMEOUT, retry-cap, dispatcher-side
  failure).
- Migrating existing legacy `handle_unhandled_role_exception` posts
  to use the new marker shape. The legacy helper's comment shape
  is unchanged; the new comment shape co-exists. Unifying them is
  a follow-up.
- Changing the `OutcomeKind` enum or the role outcome contract.
  This spec adds plumbing around the existing contract; it does
  not redefine BLOCKED / NEEDS_HELP / etc.
