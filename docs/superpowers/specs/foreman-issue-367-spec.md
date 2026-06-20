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
  * **POST-failure semantics (load-bearing for the "non-fatal comment
    post" claim in the role-core wiring section).**
    `host.post_issue_comment` failures (any `Exception` subclass — GitHub
    5xx, rate limit, network drop) are caught inside the helper and
    logged via `logger.exception`; the helper returns `False` rather
    than re-raising. The four role-core call sites and both observers
    treat a `False` return as a non-fatal comment-post failure and
    proceed with their normal success-path telemetry write (e.g.,
    `log_planner_run` / `log_reviewer_run` / etc.). This contract is
    the regression guard against foreman#235 — a transient GitHub 5xx
    on the comment post MUST NOT skip the JSONL telemetry write or
    kill the role subprocess. Rationale: the comment is a
    nice-to-have; the role's structured outcome is the contract.
    Crashing the role on a comment-post 5xx escalates a survivable
    failure into the NeedsHelp pipeline, which is strictly worse than
    a missing comment.

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
- [ ] **Validator placement guidance.** Where the role's
  schema file already has a `model_validator(mode="after")`
  method that enforces outcome-driven required fields, EXTEND
  the existing method rather than adding a new one. The
  existing methods are:
  * `ReviewerOutput._enforce_finding_contract` at
    `packages/foreman/src/foreman/schemas/reviewer.py:97`
  * `WorkerOutput._enforce_outcome_required_fields` at
    `packages/foreman/src/foreman/schemas/worker.py:282`
  The Planner and Fixer schemas do NOT have an existing combined
  validator; add a new `model_validator(mode="after")` method
  named `_enforce_escalation_comment_required` for those two.

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
- [ ] `packages/foreman/src/foreman/prompts/reviewer_impl.md` gains
  the same content requirement as `reviewer.md` BUT uses a Markdown
  heading rather than an XML tag — `reviewer_impl.md` has zero
  `<process>` / `<self_review>` / `<output_schema>` tag sections
  (verify with `grep -c "^<process>\|^<self_review>\|^<output_schema>"
  reviewer_impl.md` — returns 0). Insertion site: a new
  `## Escalation comment` Markdown section inserted between the
  existing `## Output` heading (line 143) and the existing
  `## Identity` heading (line 178). The body's content is identical
  to the XML-tag version in `reviewer.md` minus the angle-bracket
  wrappers (the body text is identical Markdown either way; only the
  heading style changes to match each file's existing convention).
- [ ] `packages/foreman/src/foreman/prompts/fixer.md` gains an
  `<escalation_comment>` section gated on
  `outcome == 'incomplete'` OR `confidence == 'low'`. Content requirement
  per the issue's table for "Fixer receiving Reviewer rejection": "What
  the rejection said (one-line) · What fix I'm attempting · Scope
  guardrails I'm applying". The Fixer's Bash tool MUST NOT be used to
  call `gh issue comment` — comments are routed via the structured
  field; the section names this explicitly.
- [ ] `packages/foreman/src/foreman/prompts/fixer_impl.md` gains
  the same content requirement as `fixer.md` BUT uses a Markdown
  heading rather than an XML tag — `fixer_impl.md` has zero
  `<process>` / `<self_review>` / `<output_schema>` tag sections
  (verify with `grep -c "^<process>\|^<self_review>\|^<output_schema>"
  fixer_impl.md` — returns 0). Insertion site: a new
  `## Escalation comment` Markdown section inserted between the
  existing `## Output` heading (line 169) and the existing
  `## Identity` heading (line 221). The body is identical to the
  XML-tag version in `fixer.md` minus the angle-bracket wrappers.
- [ ] `packages/foreman/src/foreman/prompts/worker.md` gains an
  `<escalation_comment>` section gated on
  `outcome in {'incomplete', 'spec_invalid'}`. Content requirement:
  "Why I could not finish · What sub-requests landed and which I
  skipped · What an operator would need to do to unblock". Same Bash
  prohibition as Fixer.

### Role core wiring (Python-side post + fallback)

- [ ] **State-instance id plumbing (prerequisite for the per-run
  dedup key).** Before the per-role wiring below can satisfy the
  spec's idempotency invariant ("re-emitting the same outcome on the
  same ticket does NOT post a duplicate comment within the same
  state-attempt sequence"), the dispatcher MUST thread the current
  state-instance id into the role subprocess. Concrete contract:
  * `RoleDispatchState.execute` in
    `packages/foreman/src/foreman/v4/states/role_dispatch.py:30-41`
    is the call site that invokes
    `ctx.role_dispatcher.dispatch(...)`. The `StateInstanceRecord`
    is already on `ctx.instance` (built upstream by
    `WorkerPool._run_transition` via `repo.open_state_instance`
    at `worker_pool.py:125` — no edits to `worker_pool.py` are
    required because `ctx.instance` already carries the field).
    Extend the existing call at `role_dispatch.py:35-40` to pass
    `state_instance_id=ctx.instance.id` as a new keyword arg into
    `SubprocessRoleDispatcher.dispatch`.
  * `SubprocessRoleDispatcher.dispatch` injects it into the
    subprocess environment as `FOREMAN_STATE_INSTANCE_ID=<id>`
    (alongside the existing `GH_TOKEN` env var set at
    `subprocess_dispatcher.py:238`). Env var route — not a CLI
    flag — because the existing role CLIs (`run_planner_cli`,
    `run_reviewer_cli`, `run_fixer_cli`, `run_worker_cli`) all
    take only `project: str` + `issue_number: int` keyword args
    today; adding a flag means touching every CLI signature.
    Env var keeps the change additive.
  * Each role core (`_run_planner_core` / `_run_reviewer_core` /
    `_run_fixer_core` / `_run_worker_core`) reads the env var via
    `state_instance_id = os.environ.get("FOREMAN_STATE_INSTANCE_ID")`
    at the top of the function. When unset (legacy / direct CLI
    invocation outside the dispatcher), falls back to the literal
    string `"unknown"` and the per-source dedup key becomes
    `f"state-instance-unknown-{<role-specific suffix>}"`. The
    fallback is intentionally NOT random: under direct-CLI
    invocation an operator IS running the role once, and a stable
    fallback prevents accidental double-post if they invoke twice.
- [ ] `_run_planner_core` in
  `packages/foreman/src/foreman/roles/planner.py` calls
  `post_escalation_comment` whenever `llm_output.confidence == 'low'`
  (i.e., the run is about to emit a `NEEDS_HELP` outcome from
  `run_planner_cli`). `source="role:planner"`,
  `key=f"state-instance-{state_instance_id}"` where
  `state_instance_id` is read from the
  `FOREMAN_STATE_INSTANCE_ID` env var per the plumbing contract
  above. If `escalation_comment is None` on the LLM's output
  (slip), the helper posts the fallback shape with
  `fallback_reason="planner LLM produced confidence=low but did not
  populate escalation_comment"`. The post happens BEFORE
  `log_planner_run` so a comment-post failure is visible in the daemon
  log without preventing the JSONL row.
- [ ] `_run_reviewer_core` in
  `packages/foreman/src/foreman/roles/reviewer.py` calls
  `post_escalation_comment` whenever `llm_output.confidence == 'low'`.
  `source="role:reviewer-<target>"` (`spec_pr` or `impl_pr`),
  `key=f"state-instance-{state_instance_id}-pr-{pr_number}-{llm_output.outcome}"`
  (state-instance prefix keeps the dedup contract uniform across
  roles; the PR number + outcome suffix preserves the per-attempt
  fingerprint the Reviewer needs). Fallback applies identically.
- [ ] `_run_fixer_core` in
  `packages/foreman/src/foreman/roles/fixer.py` calls
  `post_escalation_comment` whenever
  `llm_output.outcome == 'incomplete'` OR
  `llm_output.confidence == 'low'`.
  `source="role:fixer-<target>"`,
  `key=f"state-instance-{state_instance_id}-pr-{pr_number}-{llm_output.outcome}"`.
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
  `key=f"state-instance-{state_instance_id}-attempt-{attempt}-{final_outcome}"`.
  Fallback applies identically.

### Sustained-BLOCKED observer (new module)

- [ ] NEW file
  `packages/foreman/src/foreman/v4/observers/sustained_blocked.py`
  defines `SustainedBlockedObserver` consuming `ExecuteCompletedEvent`
  (the only event whose payload carries the `Outcome`). On every
  `BLOCKED`-outcome event:
  1. Compute the "blocked-reason signal" — for v1 this is
     `outcome.summary` truncated to its first 80 chars. The stability
     contract holds because there are exactly TWO `OutcomeKind.BLOCKED`
     emit sites in the codebase (verify with
     `grep -rn "OutcomeKind.BLOCKED" packages/foreman/src/ | grep -v "outcome_kind ==" | grep -v "OutcomeKind.BLOCKED.value"`),
     both of which emit stable per-cause strings:
     * `merging.py:164-165` →
       `"impl PR not yet mergeable (CI pending or merge conflict)"`
     * `worker.py:1666-1668` →
       `"impl PR open, CI in flight"` (default) OR the upstream
       `getattr(result, "summary", None)` value, which on the
       BLOCKED path is the stable string built at `worker.py:1560`
       (`"impl PR open, check still in flight"`).
     The per-attempt-varying summary at `worker.py:1563`
     (`f"{llm.outcome} (attempt {core_result.attempt})"`) belongs
     to the `give_up` status path, which emits `OutcomeKind.NEEDS_HELP`
     at `worker.py:1675-1677` — NOT BLOCKED — so it never reaches
     the SustainedBlockedObserver. **Forward-compatibility rule**:
     new BLOCKED emitters introduced by future tickets MUST emit a
     summary that is stable across poll ticks for the same logical
     cause (the SustainedBlockedObserver's contract requires this).
     A regression test in
     `packages/foreman/tests/v4/observers/test_sustained_blocked_observer.py`
     asserts that the two current emitters produce stable strings
     across two simulated ticks; future emitters should be added
     to the same test as they land.
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
     failure should not crash the daemon). Operationally, the
     post-failure catch is delegated to
     `post_escalation_comment` (which catches and returns `False`
     per the helper's POST-failure contract); the observer treats
     `False` as a no-op and proceeds. The observer's own
     `try/except` is the belt to the helper's suspenders: an
     unexpected exception path (e.g., key-derivation crash, host
     resolver crash) is also swallowed at the observer boundary so
     EventBus dispatch never raises.
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
     **Consequence for the inline exit-code + log-tail block.** The
     `extra_context` block defined under "Inline exit code + last
     500 chars of stderr" below is, by design, ONLY rendered on
     subprocess-crash / TIMEOUT / retry-cap-trip paths — i.e., the
     paths where the role subprocess died WITHOUT producing
     structured output, and therefore the in-role
     `post_escalation_comment` could not run. On the common
     Worker-self-reports-`incomplete` / `spec_invalid` path the
     in-role helper posts `source="role:worker"`, the terminal
     observer's 5-minute heuristic skips, and no inline exit code
     is rendered (correctly — the Worker subprocess exited 0 with
     a structured "I couldn't finish" outcome; there is no crash
     exit code to surface). This matches the issue body's table,
     which scopes the "Role · timestamp · exit code · last 500
     chars of stderr" requirement to the
     "All roles: subprocess crash / timeout (Python-side fallback)"
     row, distinct from the self-escalation rows that only require
     "Why · What tried · What would unblock".
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
- [ ] **POST-failure semantics (shared rule).** Like
  `SustainedBlockedObserver`, the `TerminalLandingObserver`
  delegates comment-post catch to `post_escalation_comment` (which
  returns `False` on `host.post_issue_comment` failure per the
  helper's POST-failure contract); the observer treats `False` as a
  no-op and proceeds. The observer additionally wraps the entire
  `handle()` body in a top-level `try/except Exception` that logs
  via `logger.exception` and swallows — EventBus dispatch must
  never raise out of this observer. Same belt-and-suspenders
  rationale as `SustainedBlockedObserver`.

### Bootstrap wiring

- [ ] `packages/foreman/src/foreman/v4/bootstrap.py`'s
  `bootstrap_cli_context` (the existing function that builds the
  per-project providers and constructs the `Daemon`) constructs both
  new observers, wires them onto the same EventBus that already
  receives `LabelObservabilityObserver` /
  `StructuredLogObserver` / `EventArchiveObserver` /
  `MetricsObserver`. The `host_for_project` callable is built from
  a NEW parallel
  `per_project_git_hosts: dict[str, GitHostProvider]` map keyed by
  project name. **Type correction**: the existing
  `per_project_providers` map at `bootstrap.py:61` holds v4
  `foreman.v4.git_provider.GitProvider` instances, NOT v3
  `PyGithubGitProvider` (the `PyGithubGitProvider` class lives at
  `packages/foreman/src/foreman/v4/pygithub_git_provider.py` and is
  the concrete factory the production
  `git_provider_factory` happens to construct, but the dict's
  declared element type is the v4 `GitProvider` Protocol). The
  v4 `GitProvider` Protocol does NOT include the
  `get_issue_comments` / `post_issue_comment` methods we need;
  those live on the v3-shape `GitHostProvider`. So the new map is
  genuinely a separate construction, not a downcast.
  * **Identity plumbing for `build_role_resources`.** The helper
    signature at `foreman/roles/__init__.py:210-216` is
    `build_role_resources(*, registry: Any, role: str, app_id: int,
    private_key_path: str)`. The `registry` parameter expects an
    object with `get_role_token(role) -> str`. The
    `bootstrap_cli_context` signature accepts an `IdentityProvider`
    Protocol (`bootstrap.py:32-33`) whose only declared method is
    `get_role_token(role: str) -> str` — structurally compatible
    with the `registry` shape `build_role_resources` requires. The
    spec change: pass the existing `identity` parameter through to
    `build_role_resources` as `registry=identity` directly; the
    Protocol contract is already satisfied by duck typing (and by
    production's actual `V4IdentityRegistry`, which is the canonical
    implementor of the same one-method contract). No widening of
    the Protocol is required.
  * **Orchestrator credentials location.** Per-project iteration
    calls `build_role_resources(registry=identity, role="orchestrator",
    app_id=config.orchestrator.app_id,
    private_key_path=config.orchestrator.private_key_path)`. NOTE:
    unlike the four role cores (Planner / Reviewer / Fixer / Worker),
    which read their App credentials from
    `config.apps.<role>.app_id` / `config.apps.<role>.private_key_path`,
    the orchestrator's credentials live at the TOP-LEVEL
    `config.orchestrator` block — there is no
    `config.apps.orchestrator` (see `v4/config.py:308` and
    `v4/identity.py:247-249`). Do NOT copy the role-core call
    shape verbatim or it will crash at startup.
  * **Optional configuration.** If `config.orchestrator` is not
    configured (e.g., zero-orchestrator-Apps deployments used for
    integration tests), the helper logs a warning, leaves
    `per_project_git_hosts` empty for that project, and the
    observers' `host_for_project` callable returns `None` for that
    project. Both observers MUST treat a `None` host as a no-op
    (logger.warning + skip) so the deployment continues without the
    comment surface; this preserves the existing "additive change"
    discipline.

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
- [ ] **Inline exit code + last 500 chars of stderr (issue table
  requirement).** The issue body's table names "Role · timestamp ·
  exit code · last 500 chars of stderr" as the REQUIRED minimum
  content for the subprocess-crash / timeout event. Role + timestamp
  are already in the body skeleton; the observer MUST additionally
  surface exit code AND log-tail inline. Concrete contract:
  * NEW helper `extract_subprocess_failure_signals(*, failure_reason:
    str | None, log_path: pathlib.Path | None) -> tuple[int | None,
    str | None]` in `_escalation_comment.py`. Returns
    `(exit_code, log_tail)`.
  * `exit_code` is extracted by regex `r"exited (\d+)"` (or
    `r"exit code: (\d+)"` against the on-disk footer
    `--- exit code: <N> ---` written at
    `subprocess_dispatcher.py:449-451`) against
    `failure_reason`; returns `None` when neither pattern matches
    (e.g., TIMEOUT path, generic retry-cap failure_reason).
  * `log_tail` is the last 500 chars of the role's log file at
    `<log_dir>/<role-base>/<ticket_id>__<iso>.log` — read via
    `log_path.read_bytes()[-500:].decode("utf-8", errors="replace")`
    (bytes-then-decode because the file is mixed stdout/stderr UTF-8
    with no length guarantee). When the file is missing, unreadable,
    or empty, returns `None` and the observer logs a
    `logger.warning`; the comment is still posted without the
    log_tail block. Read failures MUST NOT raise into the observer.
  * The `TerminalLandingObserver` calls this helper, then renders
    the `extra_context` block as:
    ```markdown
    ## Subprocess signals
    - exit code: <exit_code OR "(unknown — TIMEOUT or generic
      cap-trip; see log)">
    - log path: <log_path>

    <details>
    <summary>last 500 chars of role log</summary>

    ```
    <log_tail OR "(log file missing or unreadable)">
    ```

    </details>
    ```
  * The `<details>` fold prevents the inline tail from dominating
    the issue body when stderr is verbose; the operator clicks to
    expand. Precedent: the Reviewer's findings JSON uses the same
    `<details><summary>` fold in `roles/reviewer.py` (the
    `FINDINGS_BEGIN_MARKER` block).
  * Log path is computed in the observer by GLOBBING the role's log
    directory and picking the most-recently-modified file for the
    ticket. Concrete contract:
    1. The role-base directory is `<log_dir>/<role-base>` where
       `<role-base>` is the suffix-stripped role name
       (`reviewer-spec` / `reviewer-impl` → `reviewer`,
       `fixer-spec` / `fixer-impl` → `fixer`, etc.). The observer
       imports `_base_role` from
       `foreman.v4.subprocess_dispatcher` — the same helper the
       dispatcher uses at `subprocess_dispatcher.py:241` — so the
       observer's directory naming and the dispatcher's directory
       naming cannot drift.
    2. The role name comes from the prior `state_instances` row's
       `state_name` (e.g., `Planning` → `planner`,
       `SpecReview` → `reviewer-spec`, `Fixing` → `fixer-spec`,
       `Implementing` → `worker`) via a new module-level
       `_STATE_NAME_TO_ROLE` map in
       `terminal_landing.py` that mirrors the inverse of
       `subprocess_dispatcher._ROLE_TO_INVOCATION`.
    3. Glob is `role_log_dir.glob(f"{ticket_id}__*.log")`; pick the
       file with the highest `Path.stat().st_mtime`. The dispatcher
       writes log lines as the role runs (`buffering=1` per
       `subprocess_dispatcher.py:253-255`), so the most-recently
       modified file is the one whose role just crashed.
    4. When the glob returns zero matches (e.g., the role crashed
       before opening the log file, or the log directory was rotated
       out of band), the observer logs `logger.warning("no log file
       found for ticket %s under %s", ticket_id, role_log_dir)` and
       sets `log_path = None`. The comment is still posted; the
       `extra_context` block renders `(log file not found)` for the
       path and `(log file missing or unreadable)` for the tail.
    5. The glob-by-mtime strategy is preferred over reconstructing
       the iso timestamp from `state_instances` because
       `StateInstanceRecord` (`packages/foreman/src/foreman/v4/records.py:45-58`)
       has NO `dispatched_at` field — only `entered_at` /
       `execute_started_at` / `execute_completed_at` / `exited_at` —
       AND because the dispatcher captures its OWN
       `started_at = dt.datetime.now(dt.UTC)` at
       `subprocess_dispatcher.py:240` (never persisted) while the
       state-machine clock wired in `bootstrap.py:78` is
       `dt.datetime.now` (naive local time). Even if a future
       schema migration adds `dispatched_at` to
       `state_instances`, the two clocks would still differ; the
       glob-by-mtime strategy is robust against this mismatch by
       construction. The trade-off: when two ticket attempts dispatch
       within the same daemon tick (vanishingly rare; the dispatcher
       holds the SQLite write lock between attempts), the glob may
       resolve to a sibling attempt's log. We accept that — the
       sibling attempt's log is still SOMETHING the operator can
       triage, vs. an exact-match strategy that hands the operator
       a `FileNotFoundError`.
- [ ] The subprocess log path resolved per the strategy above is
  mentioned in the terminal-landing comment's `extra_context` so
  operators can pull the full stderr without spelunking. When the
  glob returns no match, the comment names the role's log directory
  (`<log_dir>/<role-base>`) instead so operators have a starting
  point.
- [ ] **Retry-cap-trip recovery (failure_reason is generic on this
  path).** On the subprocess-crash → retry-cap → NeedsHelp path,
  `state_instances[-2]`'s `failure_reason` is the generic "state X
  failed N consecutive times" message — the original crash's exit
  code is no longer present in `[-2]`. The observer MUST walk
  backward from `[-2]` through prior `state_instances` rows of the
  SAME state name to find the EARLIEST row whose `failure_phase ==
  "execute"` AND whose `failure_reason` matches the regex
  `r"exited \d+"` (i.e., the actual crash row, not the cap-trip
  row). Use that crash row's `failure_reason` as the input to
  `extract_subprocess_failure_signals`. **Log path on this fallback
  path**: the observer names the log file resolved by the
  glob-by-mtime strategy above — which is the MOST RECENT log file
  for the ticket under the role directory — NOT the earliest
  crash row's log. The most-recent log is the operator's best
  starting point because each attempt's crash signal is logged
  fresh; the earliest crash's exit code is informational for
  triage, but the operator typically wants to pull the most
  recent attempt's output. When no `r"exited \d+"` row is found
  (e.g., the cap was tripped by some other failure phase),
  `exit_code` falls back to `None` and the comment names "(retry
  cap exhausted; original crash exit code not recoverable — see
  log for the most recent attempt)".

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
- [ ] When `host.post_issue_comment` itself fails (GitHub 5xx, rate
  limit, network drop), the helper logs `logger.exception` and
  returns `False` rather than re-raising. The four role-core call
  sites and both observers treat `False` as a non-fatal comment-post
  failure: they MUST NOT abort their normal success-path telemetry
  write (`log_planner_run` / `log_reviewer_run` / `log_fixer_run` /
  `log_worker_run`), MUST NOT change their outcome label, and MUST
  NOT propagate the failure into the EventBus. The single contract
  point is the helper; callers are spared `try/except` ceremony
  around every call.

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
  * `test_post_escalation_returns_false_on_post_failure` — patches
    `host.post_issue_comment` to raise; asserts the helper does NOT
    re-raise, asserts the helper logs via `logger.exception`, and
    asserts the helper returns `False`.
  * `test_extract_subprocess_failure_signals_parses_exit_code` —
    feeds `failure_reason="RoleSubprocessError('role=worker exited
    137 without emitting an outcome; see log at ...')"` and asserts
    the returned exit code is `137`.
  * `test_extract_subprocess_failure_signals_returns_none_on_timeout` —
    feeds a generic TIMEOUT failure_reason that doesn't carry an
    `exited <N>` substring; asserts the returned exit code is
    `None`.
  * `test_extract_subprocess_failure_signals_reads_last_500_chars` —
    writes a 10 KB log file to a temp path, asserts the returned
    `log_tail` is the LAST 500 bytes decoded with `errors="replace"`.
  * `test_extract_subprocess_failure_signals_handles_missing_log` —
    points at a nonexistent path; asserts the helper returns
    `(exit_code, None)` and does NOT raise.
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
  * `test_failed_landing_renders_exit_code_and_log_tail_inline` —
    seeds a prior `state_instances` row with
    `failure_reason="RoleSubprocessError('role=worker exited 137
    without emitting an outcome; see log at /tmp/x.log')"` and
    writes a real log file to the resolved path; asserts the
    posted comment body contains the literal `exit code: 137` AND
    contains the log file's last 500 chars inside the
    `<details>` fold. The test seeds an EMPTY comment list (i.e.,
    no recent `source^="role:"` marker) so the 5-minute heuristic
    does NOT skip — the test scope is the subprocess-crash path
    where the in-role helper never ran.
  * `test_recent_role_comment_path_skips_inline_exit_code_block` —
    regression guard for the scoping decision in the terminal
    observer's docstring. Seeds the issue's comment list with a
    `source=role:worker` marker posted 30 seconds ago AND a prior
    `state_instances` row whose `failure_reason` carries an
    `exited 137` substring; asserts the observer's host-mock was
    NOT called (terminal post is suppressed by the 5-minute
    heuristic) AND that the existing `source=role:worker` comment
    body does NOT contain the inline `exit code: ` literal. This
    locks in the documented scoping: the inline exit-code +
    log-tail block lives on the terminal-landing surface only;
    the in-role self-report carries `why` / `what_tried` /
    `what_would_unblock` and nothing else.
  * `test_failed_landing_retry_cap_walks_back_to_crash_row` —
    seeds a sequence of `state_instances`:
    `(execute, "exited 1, see log...")` → `(execute, "exited 1,
    see log...")` → `(state_instance, "state Implementing failed
    3 consecutive times")` → terminal NeedsHelp landing. Asserts
    the observer walks back from `[-2]` to find the EARLIEST
    crash row and renders `exit code: 1` (not `None`).
  * `test_failed_landing_post_failure_does_not_raise` — patches
    `host.post_issue_comment` to raise; asserts the observer
    swallows (via the helper's `False` return) and the EventBus
    dispatch completes normally.
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
**Pattern naming (Decision 4 — calibrated lens).** One GoF pattern,
one Google principle, plus one project-local idiom that is
explicitly NOT a pattern:

1. **Observer Pattern (GoF)** — `SustainedBlockedObserver` and
   `TerminalLandingObserver` are new observers on the existing
   `EventBus`, sibling to `LabelObservabilityObserver` and
   `StructuredLogObserver`. The mechanism is already in place; we're
   adding two new subscribers, not inventing a notification surface.
2. **"Make the right thing easy" (Google SRE)** — comment
   construction lives in ONE module
   (`roles/_escalation_comment.py`); the four role cores + two
   observers + Fixer pre-dispatch all call
   `post_escalation_comment` with their own `source` / `key` /
   `payload`. A future operator who wants to tune the body skeleton,
   the marker shape, or the post-then-skip semantics edits one file.
3. **No GoF pattern applies for the HTML-comment-marker dedup
   mechanism — this is a project-local idiom.** Per `CLAUDE.md`'s
   calibration rule ("if neither GoF nor a Google principle applies
   cleanly, say so explicitly — pattern-fishing produces worse code
   than no pattern at all"), the marker-fenced dedup mechanism is
   reused from the existing precedent in the Foreman codebase:
   the Reviewer's `FINDINGS_BEGIN_MARKER` / `FINDINGS_END_MARKER`
   handshake in `roles/reviewer.py:105-106` and
   `roles/fixer.py:75-80`. Our shape generalizes the Reviewer's
   markers (which are static) by embedding the dedup key inside the
   begin marker (`<!-- foreman:escalation:begin
   ticket=...:source=...:key=... -->`) so the existence check
   remains a literal substring scan over
   `host.get_issue_comments(...)`. Reusing the local convention
   beats inventing a new one, even though it does not map to a
   canonical pattern name.

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
different states (Implementing-BLOCKED → "impl PR open, CI in
flight"; Merging-BLOCKED → "impl PR not yet mergeable (CI pending
or merge conflict)"). The threshold-crossing semantics differ —
operators care about whether the underlying signal is making
progress, not whether the state machine is polling. The `summary`
field is the human-readable signal; only TWO BLOCKED emitters
exist today (enumerated in the SustainedBlockedObserver
acceptance criterion above, both with stable strings); hashing
the first 80 chars gives a stable per-reason key without coupling
to the `details` dict's per-state shape. The forward-compatibility
contract requires new BLOCKED emitters to also produce stable
summaries; the SustainedBlockedObserver regression test catches
drift.

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
   with the new `<escalation_comment>` section / `## Escalation
   comment` Markdown section per each file's existing convention.
4. **State-instance id plumbing.** Extend
   `SubprocessRoleDispatcher.dispatch` to accept
   `state_instance_id: int` and inject it as
   `FOREMAN_STATE_INSTANCE_ID` in the subprocess env. Extend
   `RoleDispatchState.execute` (the actual dispatch call site at
   `packages/foreman/src/foreman/v4/states/role_dispatch.py:35`)
   to forward `ctx.instance.id` into the dispatcher call.
   `WorkerPool._run_transition` does NOT need editing —
   `ctx.instance` already carries the `StateInstanceRecord` built
   via `repo.open_state_instance` at `worker_pool.py:125`. This
   step MUST land before the role-core wiring so the env var is
   available at every dedup-key construction site.
5. Wire `_run_planner_core` to call `post_escalation_comment` on the
   low-confidence path, reading
   `FOREMAN_STATE_INSTANCE_ID` per the plumbing above. Add the
   fallback shape for the missing-field slip.
6. Wire `_run_reviewer_core` identically on its low-confidence path.
7. Wire `_run_fixer_core` on incomplete / low-confidence, AND add the
   pre-dispatch "received rejection" post.
8. Wire `_run_worker_core` on incomplete / spec_invalid.
9. Add `SustainedBlockedObserver` consuming `ExecuteCompletedEvent`
   with the per-reason scan + 15-minute threshold + marker dedup.
10. Add `TerminalLandingObserver` consuming `StateEnteredEvent` for
    `NeedsHelp` / `Failed` with the 5-minute role-comment window
    heuristic and the glob-by-mtime log-path resolution.
11. Extend `bootstrap_cli_context` to construct + wire both
    observers (and the `per_project_git_hosts` map they need).
12. Write the unit + integration tests enumerated in Acceptance.
13. Add the RUNBOOK section.

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
- `packages/foreman/src/foreman/v4/subprocess_dispatcher.py` — extend
  `SubprocessRoleDispatcher.dispatch` signature with a new
  `state_instance_id: int` keyword arg; inject
  `FOREMAN_STATE_INSTANCE_ID=<id>` into the subprocess env alongside
  the existing `GH_TOKEN`.
- `packages/foreman/src/foreman/v4/states/role_dispatch.py` — extend
  `RoleDispatchState.execute` to forward `ctx.instance.id` to
  `ctx.role_dispatcher.dispatch(...)` as the new
  `state_instance_id` arg. (This is the actual dispatch call site;
  `WorkerPool._run_transition` does NOT call `dispatcher.dispatch`
  and does NOT need editing — `ctx.instance` already carries the
  `StateInstanceRecord` built by `repo.open_state_instance` at
  `worker_pool.py:125`.)
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
