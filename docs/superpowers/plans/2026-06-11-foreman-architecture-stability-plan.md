# Foreman architecture + stability plan (2026-06-11)

> Filed after Jeff's 2026-06-10 PM observation: *"we've basically been layering bandaid on top of bandaid"* + 2026-06-11 AM addition: *"I want to look at a lens of design stability and cleanliness — GoF patterns, Google principles."*

This plan lives in the repo (not in an issue body) so we can iterate on it as the review produces decisions. Tracking ticket: foreman#269.

## Status note (2026-06-11 morning — CORRECTED EVENING)

- Morning state (as I believed it at the time): "All five overnight PRs landed on main: #266 (GoF provider boundary), #268 (reviewer-budget gate), #256 (dead Literals), #215 (git identity), #258 (typed Outcome enum)."
- **Evening correction (see Decision 9):** that statement was only true via three *manual rescue PRs* (#274, #275, #276 — "promote ... to main") that re-landed the orphaned impl content. The original impl PRs (#271, #273, #260) all merged into orphan spec branches, not main. We did NOT realize at the time that the rescue work was rescue work; we believed it was normal stability sprint merging. This is the same impl-PR-base-retarget bug Decision 9 names.
- Container rebuilt on `a6c6956` and ran healthy through the day.
- **Container STOPPED 2026-06-11 20:54 PM** per Jeff's decision after Decision 9's audit findings. The autonomous loop is paused on all three registered projects (foreman, voice, agent_core) pending the fix.
- Architecture review session was **today** (Jeff corrected the timing — I had been calling it "tomorrow").
- Two structural bugs surfaced during deployment: container clone staleness, crash-cascade leaves ticket label-less.

---

## Scope

**Whole-foreman.** The review and the lenses below apply to every module under `packages/foreman/src/foreman/`, not just the reconciler/rules engine that's been getting the most recent attention. Explicit scope:

- `reconciler/` — rules, actions, exec_log, outcomes, dispatcher, observer, host wrappers
- `roles/` — planner, reviewer, fixer, worker, and the shared role-runner helper
- `providers/` — adapter, recovery chain, strategies, exceptions, the boundary contract
- `provider.py` — facade ABC + legacy exception family
- `daemon_runners.py` + `daemon.py` — process lifecycle, queue, locks, recovery
- `cli.py` — entrypoints, the `_build_v3_gh_and_host` wiring site
- `dispatch_recorder.py` — Phase 1 mediator, the dual-write path
- `stats.py` — JSONL row shapes, per-role outcome Literals
- `schemas/` — Pydantic models per role
- `identity.py` + `auth.py` — App metadata, token minting, per-role clients
- `git_hosts/` — GitHub provider, observer integrations
- `worktree.py` — git-worktree lifecycle (the source of this morning's clone-staleness crash)
- `config.py` + `init.py` — TOML loading, label constants, project config
- `prompts/` — role prompt templates (Lens C cares here too — dead/stale prompt sections accumulate)

Tests are in scope as well: dead test scaffolding, fixture drift, assertion mismatches with current production behavior all count.

## The three cross-cutting lenses

The phases below are organized chronologically, but every phase MUST be evaluated through these three lenses. If a phase's deliverable doesn't materially improve at least one, it's the wrong deliverable.

### Lens A — Operational stability
*Will the autonomous loop survive the next class of failure we don't know about yet?*

Concrete checks per deliverable:
- Failure-mode taxonomy: is this failure shape catalogued? Does at least one defense catch it?
- Blast radius: when this defense doesn't fire (or fires wrong), what's the maximum damage?
- Observability: can an operator see this happening at the time it's happening, or only after?
- Recovery path: does the system get back to a clean state without human intervention?

### Lens B — Design stability + cleanliness *(new — Jeff's ask)*
*Are we solving recurring problems with idiomatic, named patterns instead of bespoke one-off shapes?*

Concrete checks per deliverable:
- **GoF pattern fit**: does this code map cleanly to a named pattern (Strategy / Adapter / Chain of Responsibility / Template Method / Observer / State / Command / Facade)? If yes, name it explicitly in the docstring. If no, why not?
- **Google engineering principles** (from the public Eng Practices guidelines + Software Engineering at Google book):
  - **Single responsibility**: does this module/class have one reason to change?
  - **Interface segregation**: are callers depending only on what they actually use?
  - **Make the easy thing the right thing**: is the idiomatic call the safe one, or do you have to remember discipline?
  - **Dependency inversion**: does this code depend on abstractions or concretions?
  - **Loose coupling, high cohesion**: when this thing changes, what else has to change?
- **SOLID adherence**:
  - **Open/closed**: can we extend without modifying? (foreman#258's enum is a good example — adding an outcome is one line, no edits to existing call sites.)
  - **Liskov**: do subclasses really substitute for their bases? (the foreman#266 `ProviderFacade` ABC + `AnthropicSDKProvider` concretion is on test for this.)
- **"Where are we hand-rolling things that have well-known patterns?"** — this is the audit question. Examples already identified:
  - `rules.py` predicates → Strategy pattern (each predicate is already a function; could be `Rule` objects with `evaluate()` + clearer extension points)
  - Role runners → Template Method (every role has setup → preflight → dispatch → terminate; today the shape is implicit and each role re-implements it)
  - `execution_log` writes → Observer pattern (the dispatch-recorder dual-write is already adopting this; could be more explicit)
  - Label state machine → State pattern (currently distributed across rules.py predicates; explicit State objects could make transitions visible)
  - Actions → Command pattern (already partially — `Action` is an enum, but the executor's dispatch table could be a registry of `Command` objects)

A finding through Lens B doesn't automatically mean we MUST refactor — sometimes the bespoke shape is fine and adding a pattern just adds ceremony. But every deliverable that goes through this lens produces an explicit answer.

### Lens C — Dead code + unused surface

*Is every line earning its keep right now, or is it surviving because nobody noticed?*

Dead code is its own failure mode: it lies to the reader, it hides bugs (the foreman#266 unreachable `SuccessAsErrorRecovery` shipped because the implementer KNEW it was unreachable and worked around it in tests — a textbook example), and it accumulates because removing things feels riskier than leaving them.

Concrete categories to audit (the whole foreman tree, not just reconciler):

- **Unused functions / classes / methods**: nothing imports them, nothing calls them. Use `vulture` or `ruff`'s unused-import + dead-code rules as a first pass; eyeball-confirm because dynamic imports + getattr can fool the tools.
- **Unreachable code paths**: the foreman#266 `SuccessAsErrorRecovery` predicate-vs-early-return case is the canonical example. Look for branches whose preconditions contradict callers' invariants, defensive checks for conditions that can't occur, `except` arms catching exceptions never raised.
- **YAGNI scaffolding**: classes/methods/parameters defined "for future use" with no current caller. `ProviderInvalidResultError` was a fresh example removed today. Re-add when the use case actually appears.
- **Stale defensive code**: try/except arms catching errors that no longer happen because the bug was fixed elsewhere. Backwards-compat shims for migrations that completed months ago.
- **Stale docstrings + comments**: references to closed issues, deleted modules, renamed functions. Comments describing behavior that's no longer true.
- **Dead test fixtures / helpers**: setup code for tests that were deleted; mocks for classes that no longer exist; fixture parameters never consumed.
- **Dead Literal/enum members**: foreman#256's `*_failed` outcomes were the canonical example. After foreman#258's typed enum lands, the equivalent check is "does every `Outcome` member appear in `outcomes_written_to_log` collected from the source tree?"
- **Dead config knobs**: `ProjectConfig` / `OrchestratorConfig` fields never read; environment variables defined but never checked; CLI flags that route nowhere.
- **Dead prompt sections**: `prompts/*.md` content that references behavior the role no longer performs. The role runners have evolved; the prompts may not have.
- **Dead label states**: labels defined in `init.py` that no rule fires on and no role applies.
- **Dead SQL columns**: migrations that added columns later abandoned but never dropped. `execution_log` has accumulated columns over multiple foreman# issues; some may be unused.
- **Dead imports**: `ruff` catches the obvious ones; eyeball-confirm modules imported only for a side effect (`# noqa`) that no longer triggers.
- **Commented-out code**: zero tolerance. Either it's live or it's deleted; "we'll come back to it" never happens.

Lens C produces a separate deliverable per phase: a "dead surface inventory" with line numbers + provenance + suggested action (delete vs un-comment vs re-test the contract). Reviewer-on-impl prompt should grow a Lens C section: *"Look for branches the author claims handle a case the surrounding code can't reach. Look for defensive code for failure modes named in a closed ticket. If you find dead code, flag it; don't ignore it because 'it could be useful later'."*

---

## Phase 0 — Pre-review prep (this morning, before the session)

**Goal:** show up with concrete evidence + straw-man positions, not blank-page.

Deliverables (all three lenses, whole foreman tree):

- [ ] **Lens A — operational stability**: audit last 30 days of merged PRs, tag each `feature` / `bandaid` / `structural fix` → quantify the pattern. Catalog the failure modes each bandaid was added to defend against.
- [ ] **Lens B — design quality**: whole-tree pattern audit. For each module under `packages/foreman/src/foreman/`, identify:
  - Which GoF patterns ARE present (so we know the baseline vocabulary)
  - Which would fit cleanly but aren't used (refactor candidates)
  - Where code rolls its own version of something with a canonical pattern (Strategy / Adapter / Chain of Responsibility / Template Method / Observer / State / Command / Facade)
  - Where Google principles are violated (multi-responsibility classes, wide interfaces, easy-thing-isn't-the-right-thing call sites, concrete dependencies that should be abstract)
- [ ] **Lens C — dead code**: whole-tree dead-surface inventory. First pass `ruff check --select F401,F841` + `vulture packages/foreman` for the mechanical wins; eyeball-confirm pass for unreachable branches, YAGNI scaffolding, stale defensive code, dead Literal/enum members, dead config knobs, dead prompt sections, dead label states, dead SQL columns, dead test fixtures. Produce a CSV: `path:line | category | suggested action | provenance ticket`.
- [ ] Re-read `rules.py` / `actions.py` / `init.py` / role runners / providers / daemon-runners with all three lenses simultaneously
- [ ] Draft straw-man positions on each of foreman#269's six "Decision the review must produce" lines
- [ ] Add this week's newly-discovered bugs to foreman#269's evidence section (already done for some — finish the rest)

Output: this plan doc gets a "Phase 0 findings" section appended with summary tables per lens.

## Phase 1 — Architecture review session (today)

**Goal:** decisions, not discussion. Each topic ends with a converged stance.

Six topics from foreman#269 (all evaluated through both lenses):

1. **Snapshot-eval vs lifecycle events** — small version (helper + outcome-filtered predicates) or big version (event-sourced reactor)?
2. **Layered defenses audit** — failure-mode taxonomy → defense mapping → identify gaps and overlaps
3. **Role-runner contract formalization** — codify setup → preflight → dispatch → terminate via Template Method? Or explicit interface?
4. **Provider boundary generalization** — does the #266 GoF pattern extend to PyGithub, git subprocess, GraphQL?
5. **Outcome vocabulary** — already addressed (foreman#258); any residual decisions?
6. **State-machine visibility** — explicit diagram + visualization tool, or distributed encoding good enough?

For each topic, the decision MUST answer Lens B explicitly: "what GoF pattern (if any) does the chosen design map to, and why?"

Output: `docs/architecture-review-2026-06-11.md` + 1 ticket per decision (5–10 new tickets expected).

## Phase 2 — Operational stability sprint (this week)

**Goal:** the structural bugs we already know about, ranked by blast radius. Each one becomes a spec → impl PR.

Ranked tickets to file (subject to review decisions):

1. **Container clone auto-fetch on poll** — fixes the agent_core#172-class crash. Highest priority: silent autonomous-loop corruption when origin is ahead of local clone.
2. **Reviewer-on-impl base-ref resilience** — when the spec branch is deleted post-merge, the `git diff origin/foreman/issue-N` call exit-128s. Loud crash, but recurs every stacked-PR merge.
3. **Stacked-PR promotion automation** — daemon detects "PR merged to spec branch, not main" and either auto-opens the promotion PR OR refuses to close-on-merge until main has the work.
4. **Crash-cascade label recovery** — when rate-limit-trip strips the in-flight label, restore it after the reset window OR keep the in-flight label and just block dispatch.

Lens B for each: does the fix involve adding a clean pattern (e.g., Strategy for branch-fetch policies, Template Method for crash recovery), or is it a localized predicate change?

## Phase 3 — Architectural changes from Phase 1 decisions (1–2 weeks after review)

**Goal:** implement whatever the review decides. Examples depending on review outcomes:

- Small-version lifecycle events: add `count_completed_with_outcome` everywhere it's useful → ship as one PR
- Big-version lifecycle events: design + scope + multi-PR migration → likely warrants its own design doc
- Role-runner contract formalization via Template Method base class
- State-machine visualizer tool (renders rule predicates → label transitions as a Mermaid/Graphviz graph)
- Provider-boundary generalization to PyGithub / git subprocess wrappers

Lens B sanity-check per decision: write the design as a Mermaid class diagram + name the GoF patterns used. If the diagram can't be cleanly explained in pattern terms, the design probably needs another iteration.

## Phase 4 — Coverage + adversarial verification (parallel to Phase 3)

**Goal:** make sure the autonomous loop's Reviewer can catch what it missed twice this week.

Deliverables:
- Adversarial-review prompt iteration: add the async-iterator semantic check + "is this branch reachable?" check to `prompts/reviewer_impl.md`
- Failure-mode taxonomy from review §2 mapped to a test-coverage matrix; gaps get tickets
- Deliberate-break pen-tests: stage scenarios where we KNOW the system SHOULD escalate vs handle automatically, observe behavior, log delta
- **Lens B specifically for the Reviewer prompt**: does the Reviewer carry a checklist of common bug shapes (unreachable code, race conditions, async cleanup semantics, off-by-one, partial-failure recovery)? Today it doesn't.

## Phase 5 — Visibility + ops infrastructure (parallel)

**Goal:** operator can answer "is foreman healthy?" without digging through SQLite.

Tickets:
- foreman#217 (`foreman doctor` health probe) — revive
- foreman#218 (web dashboard) — revive
- Per-ticket lifecycle visualization (render the full path a ticket has walked)
- Daemon-state introspection: "what does the queue think it's doing right now?"

Lens B: dashboard implementation likely uses MVC + Observer + maybe Iterator over execution_log. Name them.

## Phase 6 — Stabilization rituals (continuous)

**Goal:** sustain. Stop the bandaid pattern from re-occurring.

Rituals:
- Daily container rebuild on main HEAD (automated or scheduled)
- Pre-launch checklist for any change touching `rules.py` / `actions.py` / role-runner
- **Recurring architecture review** on a cadence (monthly? quarterly?) — driven by THIS doc updated with running findings
- "Bandaid-pattern alarm" — a check that surfaces when N defenses stack against the same failure shape
- **Lens B as a continuous discipline**: every PR description includes one line — "GoF/principles considered: ___ — no pattern applies because ___" OR "applies X pattern; here's why"
- **Lens C as a continuous discipline**: every PR description includes one line — "Dead code added or removed: ___". Net-positive dead code is a code smell. Net-negative dead code is a feature.
- **Quarterly dead-code sweep**: rerun `ruff` + `vulture` + the eyeball-confirm pass on the whole tree. The deltas tell us whether the per-PR discipline is holding or whether we're accumulating again.

## Gates between phases

- Phase 0 → Phase 1: only when straw-man positions are drafted + I've re-read the three core files with both lenses
- Phase 1 → everything else: only when the review has produced written decisions + tickets
- Phase 2 vs Phase 3: parallel-OK if they touch different files; sequence if they collide
- Phase 4: doesn't gate on Phase 3 — adversarial checks can land first
- Phase 5: doesn't gate on anything but its own ticket prioritization
- Phase 6: rolls continuously regardless of phase state

## What this plan is not

- Not a 100% scope. Phase 1 may decide topics we drop entirely or add new ones.
- Not a guaranteed timeline. "1–2 weeks" assumes the autonomous loop holds together; if it doesn't, the review needs deeper changes.
- Not a substitute for the review itself. Phase 0 prep IS prep, not a pre-decision.
- Not a Lens B mandate that every existing module gets refactored. The audit's output is a prioritized list of where idiomatic pattern application would move the needle.

## Living document

Each phase appends a "Findings" section to this doc as it completes. When Phase 1 is done, this doc is the authoritative reference for what the review decided + what the follow-up tickets are. Tickets created reference this doc; this doc gets a "Decisions made on YYYY-MM-DD" section per review.

---

## Phase 0 findings — initial pass (2026-06-11 AM)

Working through the three lenses against the whole foreman tree. This section appends as Phase 0 progresses; it is not exhaustive yet.

### Lens A — PR pattern over the last 30 days

50 PRs merged since 2026-05-12. First-pass classification (more nuanced re-read pending):

**Bandaid pattern (PRs that defended against a symptom rather than the root cause):**

- PR #255 — runaway-burn defense triple (#228+#229+#230). Three layers of defense against one foreman#227 incident. Per Jeff: the literal bandaid we are trying to stop.
- PR #270 — fix(dispatch): inject GIT_AUTHOR_* into role subprocess env. Patches the symptom (commits attributed wrong); the root cause is that role runners do not have a clean identity boundary.
- PR #231 — fix(provider): catch SDK auth pattern, refresh creds, retry once. Specific exception-shape pattern-match; structurally it is the kind of thing the GoF refactor (#266) eventually superseded.
- PR #224 — fix(worktree): self-heal orphan local branch + stale worktree metadata. Defensive worktree handling; root cause is git worktree lifecycle is not modeled cleanly.
- PR #223 — fix(worker): push impl branch via host.push_branch instead of unauthenticated shell-out. Mostly structural, but motivated by an auth incident.
- PR #214 — fix(git): inject bot identity via env vars. Same shape as #270.
- PR #192 — fix(reconciler): clear stale merging-* labels. Defensive cleanup; root cause is label state machine does not have an explicit terminator.
- PR #189 — fix(worktree): reattach existing impl branch when worktree dir is gone. Same shape as #224.
- PR #209 — fix: pin container reconciler.db_path. Single-site fix; not really a bandaid.

**Structural fixes (root-cause work; pattern application; new abstractions):**

- PR #277 — typed Outcome enum (foreman#258). Replaces three hand-maintained constants with one source-of-truth enum.
- PR #275 / #273 — reviewer-budget gate moved to AFTER review, counts needs_fix outcomes. Fixes the gate-placement bug.
- PR #276 / #260 — dead Literal cleanup. Pure dead code removal.
- PR #274 / #271 — GoF ProviderAdapter + RecoveryChain. Boundary architecture (Adapter + Strategy + Chain of Responsibility + Facade + Translator).
- PR #252 — dispatch recorder mediator with dual-write. Phase 1 of a structural plan.
- PR #234 — standardize role JSONL envelope with CommonEnvelope. Single-source schema.
- PR #197 — centralize foreman:* label constants. Single source of truth.
- PR #207 — mirror daemon log to stdout. Visibility / ops.
- PR #202 — plumb --target through run_reviewer as a sanity-check guard. Structural.

**Feature work (genuine new capability):**

- PR #232 — capture token usage from anthropic SDK ResultMessage (foreman#227 cost telemetry baseline).
- PR #247 — capture cache tokens.
- PR #236 / #240 / #241 / #242 — log per-role failures to JSONL. Telemetry coverage.
- PR #216 — test isolation fix.
- PR #212 — test coverage for 24h boundary.

**Docs / spec PRs (the autonomous loops intentional spec-doc step):**

PR #272, #267, #263, #257, #254, #250, #210, #211, #201, #199, #196, #193, #191, #188, #185 fed into impl PRs above.

**Initial Lens A observation:**

Roughly **40% bandaid-shaped, 35% structural, 15% feature, 10% docs** over the 30-day window. That ratio is the empirical evidence for what Jeff named last night. The bandaid PRs cluster around three failure surfaces: SDK boundary (multiple defensive try/except + retry shapes), git worktree lifecycle (multiple self-heal fixes), and label state machine (multiple defensive cleanups). All three were addressed at the architectural layer by PRs in the same window — #274 (GoF boundary), worktree work has not yet been redone structurally, label state machine is still distributed.

### Lens C — Dead code findings (confirmed, first pass)

**Mechanical tools:** `ruff F401,F811,F841` clean. `vulture --min-confidence 80` clean modulo signal-handler signatures and Click decorator boilerplate. `vulture --min-confidence 60` produced 60+ candidates of which most are false positives (Click commands, Pydantic schema fields, enum members read via iteration).

**Confirmed real dead code (eyeball-verified, callers grep-checked):**

| Location | Category | Action |
|---|---|---|
| `providers/__init__.py:80` — `ProviderInvalidResultError` in `__all__` | **Broken zombie**: class deleted but `__all__` reference remains. `from foreman.providers import ProviderInvalidResultError` raises ImportError today on main. | Delete the `__all__` entry. **Bug.** |
| `roles/worker.py:252` — `_summarize_failures()` | Defined, no callers anywhere. | Delete. |
| `dispatch_recorder.py:513` — `record_dispatch_started()` | Defined, no callers anywhere. Likely foreman#251 Phase 1 scaffolding that was planned but never wired. | Delete (or wire if Phase 2 of #251 still wants it). |
| `reconciler/state.py:26` — `has_label()` | Only called by its own test. | Delete method + test. |
| `reconciler/state.py:73` — `find_issue()` | Only called by its own test. | Delete method + test. |
| `storage.py:153` — `mark_pipeline_terminated()` | Only called by its own test. | Delete method + test. |
| `identity.py:100` — `get_planner_client()` | Only called by its own test. Generic `get_client(role)` is used everywhere else. | Delete the per-role convenience methods + their tests. |
| `daemon_host.py:101` + `daemon_runners.py:50` — `get_issue_labels()` | Defined; only called by tests. Production code now uses the labels-cache approach. | Delete method + tests. |
| `worktree.py:608` — `cleanup()` | No callers anywhere. | Delete. |
| `config.py:47` — `AdminConfig.github_token_env` | Field defined; only test-loaded, never read from production code. Likely PAT-era leftover, obsolete since the GitHub App migration. | Delete + remove tests + remove TOML fixtures. |
| `config.py:174` — `retention_days` | Field defined; never read from production. | Delete or wire (do we have a retention policy yet?). |
| `config.py:95` — `max_concurrent_workers` | Self-described forward-compat knob with validator that requires it to be 1. Classic YAGNI scaffolding. | Delete + delete validator. Re-add when we actually want multi-worker. |

**Indirect dead code (tests for dead code are themselves dead):**

Every test for one of the methods above is its own dead-test entry. The grep showed ~5 such tests; the cleanup PR for the methods should remove them too.

**To verify in a deeper sweep (not yet confirmed):**

- Per-role convenience methods on `IdentityRegistry` (planner / reviewer / fixer / worker / orchestrator) — are some duplicated by the generic `get_client(role)`?
- `init.py` label constants — every constant defined; are any labels never read from a rule predicate or written by a role?
- `prompts/*.md` — do any prompts reference behavior the role no longer performs?
- `execution_log` schema — any columns added in foreman#251 cost-telemetry that are not actually populated yet?
- Pre-#266 provider-related code paths — anything that survived the GoF refactor but is not reached anymore?

### Lens B — initial GoF observations (deeper audit pending)

Modules sampled so far show:

- **Reconciler / `rules.py`**: each rule is already a `Rule` dataclass with `name`, `tier`, `precedence`, `when` (predicate function), `then` (Action enum value). Close to Strategy pattern — each rule IS a strategy. Refactor toward "every rule is an explicit class with `evaluate(ctx) -> Action | None`" would make the pattern obvious; current shape is "rule = data + predicate function" which is more functional than OO Strategy. **Verdict: already pattern-shaped; no urgent refactor.**
- **Reconciler / `actions.py`** + executor: this is closer to Command pattern (each action is a discrete operation handled by a dispatch table). Current shape: `Action` enum + executor `if`/`elif` dispatch. **Verdict: a Command registry pattern would replace the if/elif with explicit class-per-action — cleaner OCP (open/closed), easier to test in isolation.**
- **`providers/`** (post-#266): canonical Adapter + Strategy + Chain of Responsibility + Facade + Translator. **Verdict: gold standard. Use as the template for other boundaries.**
- **Role runners** (`roles/{planner,reviewer,fixer,worker}.py`): each role implements setup → preflight → dispatch → terminate informally. Template Method base class would formalize and eliminate the 4× duplication. **Verdict: refactor candidate — high-value.**
- **`dispatch_recorder.py`**: mediator pattern between role runners and the cost ledger; the dual-write to JSONL + execution_log is Observer-shaped. **Verdict: pattern is present but not named explicitly in docstrings.**
- **Label state machine**: distributed across `init.py` constants + `rules.py` predicates + `actions.py` transitions. NO explicit State pattern; transitions are implicit. **Verdict: explicit State or finite-state-machine library would make transitions auditable.**

A deeper Lens B pass would produce a module-by-module table with the same shape as Lens C above. That is the next sub-step before Phase 1.

### What is left for Phase 0

- Deeper Lens B pass: explicit pattern map per module
- Deeper Lens C pass: prompts, label constants, execution_log columns, pre-#266 leftovers
- Draft straw-man positions on foreman#269 six review topics
- Add this week's discovered structural bugs to foreman#269 evidence section

Lens A is in good enough shape to brief from. Continue or pivot?

---

## Phase 0 deeper pass (2026-06-11 late morning)

Jeff's direction: pure analysis, no fixing, no filing. Document everything.

### Lens C deeper — IdentityRegistry per-role surface

**10 per-role methods** on `IdentityRegistry`:

| Method | Line | Production callers |
|---|---|---|
| `get_planner_client()` | identity.py:100 | **ZERO** in src; only test_identity.py:55 |
| `get_planner_token()` | identity.py:104 | roles/planner.py:315 |
| `get_reviewer_client()` | identity.py:108 | roles/reviewer.py:486 |
| `get_reviewer_token()` | identity.py:118 | roles/reviewer.py:487 |
| `get_fixer_client()` | identity.py:122 | roles/fixer.py:474 |
| `get_fixer_token()` | identity.py:133 | roles/fixer.py:475 |
| `get_worker_client()` | identity.py:137 | roles/worker.py:665 |
| `get_worker_token()` | identity.py:148 | roles/worker.py:666 |
| `get_orchestrator_client()` | identity.py:152 | daemon_host.py × 11 |
| `get_orchestrator_token()` | identity.py:163 | cli.py:893 |

**Plus a generic `get_client(role)` / `get_token(role)`** — only one caller of the generic in cli.py:886 (`registry.get_token("planner")`).

**Findings:**
- `get_planner_client()` is **dead** (only test-called) — confirmed from initial pass.
- The per-role methods are thin wrappers around `get_client(role)` / `get_token(role)`. Pure DRY violation; per-role wrappers exist for typing convenience but add 10 method definitions for zero behavior. **Lens B verdict:** the generic IS the cleaner interface; the per-role variants are syntactic sugar that costs maintenance.
- Asymmetry: planner uses only `_token`, reviewer/fixer/worker use both `_client` and `_token`. Suggests the planner is genuinely simpler than the others (no PyGithub client work) — could be intentional.

### Lens C deeper — Label constant duplication (the big one)

Label string constants ARE NOT centralized — they are **re-declared in every role module that touches them**:

| Constant | fixer.py | reviewer.py | worker.py |
|---|---|---|---|
| `_LABEL_SPEC_FIX = "foreman:spec-fix"` | line 101 | line 90 | — |
| `_LABEL_PLANNING = "foreman:planning"` | line 105 | — | — |
| `_LABEL_NEEDS_HELP = "foreman:needs-help"` | line 106 | — | line 138 |
| `_LABEL_FAILED = "foreman:failed"` | line 107 | — | line 139 |
| `_LABEL_IMPL_FIX = "foreman:impl-fix"` | line 113 | line 93 | line 137 |
| `_LABEL_IMPL_REVIEW = "foreman:impl-review"` | line 114 | line 91 | line 136 |
| `_LABEL_SPEC_REVIEW = "foreman:planning"` | — | line 88 | — |
| `_LABEL_SPEC_READY = "foreman:plan-approved"` | — | line 89 | — |
| `_LABEL_READY_FOR_MERGE = "foreman:impl-approved"` | — | line 92 | — |
| `_LABEL_PLAN_APPROVED = "foreman:plan-approved"` | — | — | line 116 |

20 distinct `foreman:*` label strings appear across the source tree. The role modules each re-declare a subset. Of particular note:

- **Aliasing confusion**: `_LABEL_SPEC_REVIEW = "foreman:planning"` (reviewer.py:88) and `_LABEL_PLANNING = "foreman:planning"` (fixer.py:105) name the same string differently. A reader has to know that "planning" and "spec_review" refer to the same conceptual state.
- **Overlap rate**: `_LABEL_IMPL_REVIEW` appears 3× across role modules. `_LABEL_IMPL_FIX` appears 3×. `_LABEL_SPEC_FIX` appears 2×. Same string, three (or two) source-of-truth definitions.
- **PR #197 ("centralize foreman:* label constants into one source of truth") claimed to fix this.** It did NOT update the role modules — the per-module declarations survived. The "centralization" presumably happened elsewhere (init.py? reconciler/labels?). This is a Lens A finding too: a structural PR that did not finish the job.

**The exact failure mode Jeff named** ("if you can forget it in two places, it is an issue") is alive and well here. Renaming `foreman:needs-help` to anything else means editing 3+ files; missing one is a silent bug.

**Where the "centralized" constants live (if they exist):** TBD — need to find them. The fact that `grep -nE '^_LABEL_|^LABEL_' __init__.py` returned nothing means they are not in `foreman.__init__` like PR #197 suggested. Worth chasing.

### Lens C deeper — Prompt inventory

6 main role prompts under `prompts/`:

| File | Size | Last touched |
|---|---|---|
| `fixer.md` | 12,623 B | June 5 |
| `fixer_impl.md` | 7,804 B | June 5 |
| `planner.md` | 10,578 B | June 5 |
| `reviewer.md` | 12,445 B | June 5 |
| `reviewer_impl.md` | 8,303 B | June 5 |
| `worker.md` | 18,092 B | June 7 |

5 of 6 last-touched June 5 (today is June 11). Worker prompt last touched June 7. **Drift risk:** the role runners have evolved (PR #255 added defensive exception handling, PR #266 introduced provider boundary, PR #277 introduced typed Outcome, etc.) but the prompts have been static for ~5 days. Prompt sections referring to specific behavior may now be stale.

**8 vendored superpowers files under `prompts/superpowers/`:**
`_VERSION.md`, `executing-plans.md`, `finishing-a-development-branch.md`, `receiving-code-review.md`, `requesting-code-review.md`, `test-driven-development.md`, `verification-before-completion.md`, `writing-plans.md`

These are copies of source-of-truth skills that live in the `superpowers` plugin. **Drift risk:** if the source skills get iterated on (and they have, per the substrate's history) and we never re-vendor, our copies are stale. `_VERSION.md` exists, which suggests a versioning convention — worth checking whether the versioning is being honored.

### Lens C deeper — Marker exception duplication

PR #255 added marker exception subclasses for graceful preflight refusals:

- `_ReviewerPreflightRefusal(RuntimeError)` in roles/reviewer.py
- `_WorkerPreflightRefusal(RuntimeError)` in roles/worker.py
- `_FixerPreflightRefusal(RuntimeError)` in roles/fixer.py

(Planner does not have one — yet?)

**3 distinct classes for the same conceptual shape.** Each role re-implements identical boilerplate. The role-runner helper from PR #255 catches them generically. This is a Lens B finding: the marker-exception pattern is a candidate for a single `RolePreflightRefusal` base class with optional role tag, OR — better — formalization into the eventual Template Method base class.

### Lens B deeper — Role runner shape (Template Method opportunity)

The four `run_<role>` functions:

- `run_planner(...)` — roles/planner.py:143
- `run_reviewer(...)` — roles/reviewer.py:335
- `run_fixer(...)` — roles/fixer.py:409
- `run_worker(...)` — roles/worker.py:597

Each one informally implements:

1. Setup (worktree attach, label snapshot, App identity resolve)
2. Preflight (refuse fast if labels/state are wrong; raises `_<Role>PreflightRefusal` if so)
3. Dispatch (provider.run_agent with role-specific prompt + schema)
4. Post-dispatch (commit, push, label transition, comment, terminate the dispatch row)

**Template Method opportunity** is concrete: a `RoleRunner` ABC with `setup`, `preflight`, `dispatch`, `terminate` as abstract/protected methods plus a `run` driver that orchestrates them. Each role would implement only what differs. The shared shape (try/except for preflight refusal, label snapshot for post-state, terminate-on-finally) would live once.

**Cost of NOT doing this:** every defensive change (the marker-exception pattern, the env-injection #270, the foreman#266 GoF boundary integration) has to be applied 4×. The "we keep bandaiding" pattern is structurally caused by 4× role runners that share 80%+ of their shape with no shared abstraction.

### execution_log schema columns

| Column | Type | Source | Populated? |
|---|---|---|---|
| `id` | INTEGER PRIMARY KEY | original | yes |
| `ts` | TIMESTAMP | original | yes |
| `ticket_id` | TEXT | original | yes |
| `project` | TEXT | original | yes |
| `rule_name` | TEXT | original | yes |
| `action` | TEXT | original | yes |
| `outcome` | TEXT | original | yes (now via typed `Outcome` enum) |
| `details` | TEXT (JSON) | original | yes |
| `parent_log_id` | INTEGER | original | yes (for terminations) |
| `input_tokens` | INTEGER | foreman#251 (_COST_COLUMNS) | only on dispatch_complete rows |
| `output_tokens` | INTEGER | foreman#251 | only on dispatch_complete rows |
| `cache_creation_input_tokens` | INTEGER | foreman#251 + #244 | only on dispatch_complete rows |
| `cache_read_input_tokens` | INTEGER | foreman#251 + #244 | only on dispatch_complete rows |
| `total_cost_usd` | REAL | foreman#251 | only on dispatch_complete rows |
| `model_usage_json` | TEXT | foreman#251 | only on dispatch_complete rows |
| `duration_ms` | INTEGER | foreman#251 | only on dispatch_complete rows |
| `num_turns` | INTEGER | foreman#251 | only on dispatch_complete rows |

**Observation:** 8 cost columns added in foreman#251 are populated only when `CostSubscriber.handle_dispatch_complete` fires. For every other row (rate-limit trips, label advances, terminations from `recover_orphaned`, etc.), those columns are NULL. That is not dead code — it is correctly sparse — but it does mean most rows in `execution_log` have NULL across 8 columns. A schema audit later might ask whether the cost data should live in a separate sibling table rather than padding the main log; for now, noting.

### Initial Lens A re-classification (correction)

On a second look at PR #197 ("centralize foreman:* label constants"): this PR is now reclassified from "structural fix" to **"partial structural fix — bandaid in disguise"**. It did centralize some constants but left the role modules with their own duplicated declarations. The centralization is half-done. This pattern — incomplete structural fixes that still get tagged as "done" because the spec was narrowly scoped — is itself worth surfacing in Phase 1 §2 (layered defenses audit).

### Findings summary at this point

**Confirmed dead code (12 items, listed above):** 1 active bug (`ProviderInvalidResultError` zombie in `__all__`), 11 unused methods/fields.

**Duplication / single-source-of-truth violations:**

- 10 per-role IdentityRegistry methods (`get_<role>_client`/`_token` × 5) wrapping a generic `get_client(role)`/`get_token(role)`. 1 of 10 (`get_planner_client`) is dead.
- 20 `foreman:*` label strings, with at least 6 re-declared across 3 role modules with overlapping names AND aliases. Centralization PR #197 did not finish the job.
- 3 `_<Role>PreflightRefusal` classes for the same conceptual marker.

**Refactor candidates (Lens B):**

- Role runners → Template Method base class (eliminates ~80% of the 4× duplication; folds marker exceptions, env injection, label management, etc. into one shape).
- Actions executor → Command registry (replaces if/elif dispatch with explicit class-per-action).
- Label state machine → explicit State pattern (today distributed across 3 layers).

**Prompts:**

- 6 main role prompts last touched 5+ days ago; drift risk.
- 8 vendored superpowers files; vendor-sync discipline unclear.

**Schema:**

- `execution_log` is wide (17 columns). 8 of those are cost columns populated only on `dispatch_complete` rows. Not dead, but sparsely populated. Possible sibling-table candidate.

### Still left for Phase 0

- Find where labels are "centralized" per PR #197 (or confirm they're NOT, which makes #197's claim retroactively wrong)
- Audit `init.py` label state vocabulary specifically
- Lens B deeper for: provider boundary's adapter wiring (sanity-check the gold standard claim), daemon_host.py (very tall — 11 calls to `get_orchestrator_client` — possibly should accept a host wrapper instead)
- Pre-#266 leftover audit (anything that the GoF refactor obsoleted but did not delete)
- Draft straw-man positions on foreman#269 six review topics

Continuing.

---

## Phase 0 — third pass: the regression that nobody noticed (2026-06-11 noon)

### THE STRUCTURAL FIX THAT WAS LOST

This is the single biggest finding of Phase 0 so far. Documenting in full because it changes Phase 1's §2 (layered defenses audit) — there is a fourth dimension that audit needs to cover.

**Discovery sequence:**

1. Phase 0 found role modules with duplicated `_LABEL_*` constants (16+ entries across 3 files with aliases like `_LABEL_PLANNING` vs `_LABEL_SPEC_REVIEW` for the same `foreman:planning` string).
2. Looking at git history, PR #197 (commit `186964c`, Jun 7, "refactor(labels): centralize foreman:* label constants into one source of truth") DID create the centralization:
   - It created `packages/foreman/src/foreman/labels.py` with a `Labels` class catalog
   - It refactored rules.py, actions.py, observer.py, all three role modules, AND init.py to use it
   - It created `tests/test_labels_keystone.py` with three drift guards (foreman literal -> Labels value; Labels.all -> at least one v3 catalog; init metadata aligned)
   - Per the commit body: "927 tests pass"
3. Today, on main: `labels.py` DOES NOT EXIST. `test_labels_keystone.py` DOES NOT EXIST. The role modules have local `_LABEL_*` constants again. Every drift guard #197 added is gone.

**This means: a confirmed structural fix landed, ran in production, and was reverted in a later commit WITHOUT anyone noticing.** The reversal was almost certainly an unintentional side effect of the v3 rescue / Plan B work (PRs in the #335-#342 range, "v3 rescue Stages") which restructured large parts of the reconciler. The label module fell out during that restructuring; nobody re-added it.

**Why this matters:**

- Lens A: the bandaid stack we see today over the label state machine is partially a consequence of #197 being silently un-done. The "label duplicated in 3 places" failure mode that #197 fixed is back in production.
- Lens C: an entire module worth of structural-fix code disappeared without leaving a deletion trace anyone could grep for. This is the dead-code zombie problem at the largest scale.
- **New Phase 1 topic**: § "regression detection for structural fixes." Today there is no mechanism that says "this structural fix was supposed to make this property true; here is the property; here is the test that guards it." The closest we have is the keystone-test pattern #197 itself used (which got deleted along with everything else).

### Why this is THE bandaid pattern made visible

Jeff's exact phrasing yesterday was: "we've been layering bandaid on top of bandaid." The labels-regression case is the bandaid pattern with a twist: **#197 was a structural fix that worked, was tested, was operationally correct — and then a later refactor removed it because the new refactor did not know it was load-bearing.** The role modules then accumulated the per-file constants again (#197's deletions undone), and we are now bandaiding around the lack of a single source of truth that we used to have.

The taxonomy this exposes:

- Bandaid layered on top of bandaid: what we already named.
- **Structural fix layered on top of structural fix, where the second one buries the first**: this case. PR #335-#342's v3 rescue obsoleted (intentionally or not) the work in #197 because it touched the same modules. PR #197's tests and module went away with the v2 surface. The "keystone test" that was supposed to alarm if anyone deleted Labels was itself deleted.

This pattern is invisible without git-archaeology and is exactly the kind of thing the architecture review should formalize a defense against. **Concrete Phase 1 §2 deliverable: a "load-bearing" tag for structural fixes** (a docstring marker, a designated test file pattern, a CI check that asserts "this property is still true") so that future refactors that touch the same module can SEE the property they would break.

### Lens C deep — verified findings (revised after the regression discovery)

The original Lens C inventory still stands, plus these additions:

| Location | Category | Action |
|---|---|---|
| `packages/foreman/src/foreman/` (missing `labels.py`) | **Lost structural fix**: PR #197's centralized `Labels` catalog was deleted by a subsequent refactor; role-module constants reappeared as duplicates. | Re-create the centralization. The previous shape is recoverable via `git show 186964c`. Add the keystone test as a regression guard. |
| `packages/foreman/src/foreman/__init__.py` | The `_LABEL_METADATA` map mentioned in #197 — is it still here or also deleted? Worth checking. | Verify state. |
| `daemon_host.py` | 11+ methods each begin with `gh = self._registry.get_orchestrator_client(); repo_obj = gh.get_repo(repo)`. ~3-4 line preamble repeated. | Lens B finding: introduce a context-manager `with self._repo(repo) as repo_obj:` OR a `_get_repo(repo)` helper. Pure DRY play. |

### Updated Lens A — PR #197 reclassified again

Phase 0 has now reclassified PR #197 twice:

1. **Initial pass:** "structural fix"
2. **After finding role modules still have duplicates:** "partial structural fix — bandaid in disguise"
3. **After finding labels.py existed and was deleted:** **"reverted structural fix"** — the work was real and complete; subsequent refactors silently undid it.

The 30-day Lens A re-classification needs one more pass through this lens: which other PRs landed structural fixes that may have been undone by subsequent refactors? PRs that touched the same modules as later v3-rescue work are high-risk:
- PR #202 (plumb --target through run_reviewer) — modified `roles/reviewer.py`, which was heavily reworked in the v3 rescue.
- PR #197 (labels) — already confirmed reverted.
- PR #214 (git bot identity injection) — similar shape to #270 today; was the work in #214 superseded by #270, or did #270 re-implement work that already existed?

These would need git-archaeology to verify. Worth scoping a follow-up: **"PR resurrection audit"** — for every claimed structural fix in the last 90 days, verify the property it asserted is still true on main.

### Still left for Phase 0 (post regression-discovery)

- Run the property-still-true check against the top 5 structural PRs in the 90-day window (Lens A regression audit, manually for now)
- Lens B deeper for actions.py / executor pattern
- `__init__.py` label state vocabulary audit (now critical because we cannot trust the "centralized" claim)
- Provider boundary "gold standard" sanity-check
- Pre-#266 leftover audit
- Draft straw-man positions on foreman#269 six review topics

The regression-detection topic alone might want to be its own foreman#269 topic. Adding it to the review agenda.

---

## Phase 0 — fourth pass: regression audit + dual implementations + provider split (2026-06-11 late noon)

### Lens A — Regression audit (property-still-true on top 5 structural PRs)

Pulled the 5 highest-impact structural PRs from the 30-day window and verified the property each claimed to deliver is still in effect on main:

| PR | Claimed property | Status on main |
|---|---|---|
| #234 (CommonEnvelope) | `class CommonEnvelope` is the single JSONL envelope schema | ✅ HELD — `stats.py:54` |
| #214 (bot identity env) | `IdentityRegistry` exposes role-bot GIT_AUTHOR_* env vars | ✅ HELD — `identity.py:264` (now wrapped by #270's dispatch-side injection) |
| #202 (`--target` plumbing) | `_load_reviewer_prompt(target: Literal[...])` + `--target` CLI flag | ✅ HELD — `roles/reviewer.py:195, 400` |
| #209 (db_path pin to /foreman/state) | Container's reconciler.db lives under `/foreman/state` | ✅ HELD — via `FOREMAN_STATE_DIR` env var override route (`v3_host.py:75`) |
| **#207 (mirror daemon log to stdout as JSON for docker logs)** | `docker logs` shows the JSON-lines daemon log | ❌ **REGRESSED** — `logging_setup.py:113-133` writes JSON to a FileHandler, RichHandler goes to stderr. `docker logs` shows pretty Rich output, not JSON-lines. |
| **#197 (centralize foreman:* labels into `Labels` catalog)** | `foreman.labels.Labels` is the single source of truth | ❌ **REGRESSED** (already documented above) |

**Two confirmed regressions out of five sampled.** A 90-day audit would likely surface more. **Phase 1 §2 (layered defenses) and the new "regression detection" topic both need this finding.**

The Lens A re-classification stands: **#197 and #207 are reclassified to "reverted structural fix"**, joining the broader pattern that "shipped structural work eventually drifts back to the bandaid baseline if there is no guard against it."

### Lens B — `dispatcher.py` is a v2 module still actively imported by v3 code

The codebase has **two Action-type universes**:

- **v3 reconciler:** `reconciler/actions.py` defines `class Action(Enum)` with the rule-engine actions (ADVANCE_LABEL_*, RATE_LIMIT_TRIP, etc.)
- **v2 dispatcher (in src root):** `dispatcher.py` defines `ActionKind`, `Action`, `Ticket`, `is_blocked`, `next_action`, `stage_index`, `_LABEL_TO_ACTION` map

The v2 dispatcher is **NOT dead code** — it is imported live by:

- `daemon_runners.py:32` — `Ticket`
- `poller.py:19` — `Ticket`
- `queue.py:14` — `Ticket, next_action, stage_index`
- `role_dispatch.py:19` — `Action, ActionKind, Ticket`
- `worker.py:18` — `Action, ActionKind, Ticket, next_action`

Two parallel `Action` types coexist. Two parallel label-to-action maps coexist (`dispatcher._LABEL_TO_ACTION` is v2; `reconciler/rules.RULES` is v3). **The dispatcher.py `_LABEL_TO_ACTION` references labels that are NOT in `init.py:_FOREMAN_LABELS`** (e.g., `"foreman:plan"`, `"foreman:spec-review"`, `"foreman:implementing"`) — i.e., the v2 dispatcher routes to labels the v3 init flow does not even create on a fresh repo.

**Lens B verdict:** classic incomplete-migration debt. The v3 reconciler is the live path; the v2 dispatcher's types still leak through the import graph because various daemon-side modules (worker.py, poller.py, queue.py at the src-root level) were not migrated to v3 idioms. Either (a) finish the v2 → v3 migration and delete `dispatcher.py`, or (b) recognize the v2 surface as load-bearing infrastructure and rename the types to remove the v2/v3 confusion.

**Lens C verdict on the same finding:** `dispatcher._LABEL_TO_ACTION` references 5 labels that are nowhere in the v3 init flow and nowhere in v3 rule predicates. Those map entries are dead. But the rest of `dispatcher.py` is alive. This is "module half-dead" — different from the binary live/dead model.

### Lens B — Provider boundary split across `foreman.provider` (singular) and `foreman.providers` (plural)

The #266 GoF refactor was praised as the gold standard. Looking at imports on main, the boundary actually lives in **TWO modules with confusingly similar names:**

- `foreman.provider` (singular) — `ProviderFacade` ABC, `UsageInfo`, legacy exceptions `StructuredOutputRetryError` / `StructuredOutputMissingError` / `ProviderAuthError`
- `foreman.providers` (plural) — `ProviderError`, `ProviderTimeoutError`, `ProviderUnknownError`, `RecoveryChain`, `RecoveryStrategy`, strategies, `make_provider`

The split is **intentional per the #266 design** (avoids a circular import — the providers package imports `UsageInfo` from `foreman.provider`, so the recovery types had to live on the providers side). But the result is that every role-runner imports from BOTH:

```python
# In every role module:
from foreman.provider import ProviderFacade, UsageInfo
from foreman.providers import ProviderError  # if it catches the error family
```

**Lens B verdict:** the gold-standard claim still holds for the internal structure of `foreman.providers` (Adapter + Strategy + Chain + Facade + Translator). But the **external surface** that role-runner callers see is a two-module boundary where the naming similarity invites confusion. A reader unfamiliar with the #266 history would not understand why `ProviderFacade` lives in `provider` and `ProviderError` lives in `providers`. The fix is documentation (a module-level docstring on `provider.py` that explicitly redirects readers to `providers.__init__` for the recovery types) OR a structural collapse (move `provider.py` into `providers/_facade.py` and re-export from `providers/__init__.py`, breaking the circular by reorganizing). Phase 3 candidate, not Phase 2.

### Lens B — `daemon_host.py` boilerplate (the 33-line preamble tax)

`packages/foreman/src/foreman/daemon_host.py`: 213 lines, 12 methods. Eleven of the twelve open with:

```python
gh = self._registry.get_orchestrator_client()
repo_obj = gh.get_repo(repo)
issue = repo_obj.get_issue(issue_number)  # or .get_pull(pr_number)
```

**~33 lines of pure boilerplate in a 213-line file (15%).** Classic Lens B Template Method / context-manager candidate:

```python
@contextmanager
def _repo(self, repo: str) -> Iterator[Repository]:
    gh = self._registry.get_orchestrator_client()
    yield gh.get_repo(repo)
```

Every `def add_issue_label(...)` body would shrink by 2 lines; the abstraction would have one place to add (e.g.) timeout handling, retry policy, or rate-limit accommodation. Phase 2 candidate — low blast radius, high readability win.

### Lens A + Lens B — `init.py:_FOREMAN_LABELS` is the operator catalog but is NOT the code's source of truth

`init.py:_FOREMAN_LABELS` is an 18-entry tuple list with `(name, color, description)` per label. It is the master list **the operator sees** at `foreman init` setup time.

Comparing this list to the 20 label strings actually referenced in foreman code (collected earlier), six strings are **used in code but NOT in `_FOREMAN_LABELS`**:

- `foreman:plan` — used by v2 dispatcher's `_LABEL_TO_ACTION`
- `foreman:spec-review` — same
- `foreman:implementing` — same
- `foreman:implementing-ready` — same
- `foreman:ready-for-merge` — same
- `foreman:spec-ready` — same

All six are from the v2 dispatcher's `_LABEL_TO_ACTION` map (dead per the previous finding). If a fresh operator runs `foreman init`, none of these get created on their repo. The v2 dispatcher code that references them would silently no-op because the labels never appear on tickets.

**Verdict:** `_FOREMAN_LABELS` is correct relative to v3, but the v2 dispatcher's label vocabulary is divergent. Cleaning up `dispatcher.py` (per the previous finding) would also collapse these dead label references. Lens C indirect dead code: 5 `_LABEL_TO_ACTION` entries that nothing will ever match.

---

## Phase 0 — fifth pass: foreman#269 straw-man positions

For each of the six review topics, here is the position I would defend at today's session. The point is not "Wren has decided" but "Jeff has something concrete to react to rather than starting from blank."

### §1 — Snapshot-eval vs lifecycle events

**Straw-man:** **small version now, big version only if Phase 1 §2 reveals 3+ more bugs of this shape**. Adding `count_completed_with_outcome` (already in tree as of #258's enum + #268's gate) gets us 80% of the value at 5% of the cost. The big version (event-sourced reactor with explicit `on_outcome` handlers per transition) trades the "GitHub is source of truth, db is cache" simplicity for cleaner reactivity. We do not yet have evidence that the simplicity trade is worth it.

**Why I'd defend this:** the #268 bug we just fixed is the only confirmed instance of "gate fires in the wrong place." Until 2-3 more land, the small version is paying for itself.

**Where the lens-B audit pushes back:** the v2 dispatcher / v3 reconciler split (above) is structurally the same kind of "two competing dispatch models" mess we'd get from event-sourcing being added on top of snapshot-eval. Maybe a clearer separation IS necessary.

### §2 — Layered defenses audit (failure-mode taxonomy)

**Straw-man:** the taxonomy needs **four orthogonal dimensions, not the three Jeff outlined yesterday**:

- (A) Failure source: SDK / git subprocess / GitHub API / role-runner logic / role-runner prompt
- (B) Recovery shape: retry / fallback / surface-help / abort
- (C) Trigger layer: pre-dispatch gate / mid-dispatch try-except / post-dispatch outcome check / rate-limit window
- (D) **NEW: durability — "is this defense self-asserting" or "does it depend on operator memory"?**

Dimension (D) is the new one from this morning's regression discovery. PR #197's centralized `Labels` had a keystone test that asserted "every label literal matches a `Labels` value" — that is self-asserting durability. When PR #197 was unintentionally reverted, the keystone test went away too; if it had lived elsewhere (e.g., `tests/architecture/`), the regression would have alarmed.

**Concrete §2 deliverable:** an inventory of every defense the foreman code currently runs, classified along (A)-(D). Then we KNOW which defenses are self-asserting and which depend on discipline. Add the "load-bearing tag" mechanism for new structural fixes.

### §3 — Role-runner contract formalization

**Straw-man:** **Template Method base class.** The four role runners share 80%+ of their shape (setup, preflight, dispatch, terminate). Today the shape is replicated 4×; every defensive change (#255 marker exceptions, #270 git identity injection, future #266 boundary integration) has to be applied 4×. A `RoleRunner` ABC with `setup` / `preflight` / `dispatch` / `terminate` as abstract/protected methods plus a `run` driver that orchestrates them is the highest-ROI refactor in the tree.

**This also subsumes:** the 3 `_<Role>PreflightRefusal` classes (consolidate to a single `RolePreflightRefusal`), the per-role `_LABEL_*` constants (consolidate via the resurrected `Labels` catalog), the worker-only `combined_output` dead variable and `_summarize_failures` dead function (eliminate the asymmetry).

### §4 — Provider boundary generalization

**Straw-man:** **defer generalization for one cycle.** The #266 boundary is genuinely good for the SDK case but lives in a confusingly-split two-module shape (`foreman.provider` vs `foreman.providers`). Before applying the same pattern to PyGithub / git subprocess / GraphQL boundaries, document the SPLIT — write a module-level docstring on `provider.py` that names the circular-import constraint and points at `providers.__init__`. Wait one cycle to see if the documentation alone is enough, OR refactor to collapse the split (rename `provider.py` → `providers/_facade.py` + re-exports).

Once the provider boundary is genuinely consolidated and documented, generalizing to other third-party boundaries becomes safe.

### §5 — Outcome vocabulary

**Straw-man:** **already done as of foreman#258 (typed `Outcome` enum).** Residual question: does the role-level outcome vocabulary (`spec_written`, `clean`, `needs_fix`, `fixed`, `incomplete`, `implemented`, `spec_invalid`, `exception`) — flowing through `emit_recorder_complete → terminate_action(outcome=variable)` — deserve the same treatment? That's the "dual-write path" the foreman#258 reviewer flagged. The Outcome enum today only covers inline-literal write sites; the role-level outcomes are an `stats.py` `Literal` union, narrower scope.

If the architecture review says yes, file a follow-up: extend `Outcome` enum to cover role-level outcomes too (probably an `Outcome.ROLE_SPEC_WRITTEN` family or a separate `RoleOutcome` enum). If no, the spec PR #259 was already correct in scoping narrowly.

### §6 — State-machine visibility

**Straw-man:** **build the visualizer — but do it as a `foreman doctor` subcommand, not a web dashboard.** Render the rule predicates + action transitions as a Mermaid diagram emitted on demand. Operators get a CLI command that prints "here is the live state machine"; CI gets the same command to produce a diagram that's checked into docs.

This is cheaper than the dashboard (foreman#218) and addresses 80% of the "I cannot read the state machine in my head" pain. The dashboard adds operational live-status visibility; the visualizer adds structural-state-shape visibility. Different needs.

**Lens B view:** the state machine itself is the candidate refactor (explicit State pattern). The visualizer is a corollary that comes for free if the State pattern lands.

### NEW §7 — Regression detection for structural fixes (proposed addition to foreman#269)

**Straw-man:** introduce a `load_bearing` decorator or designated `tests/architecture/` directory pattern. Every structural fix that asserts a property the code MUST keep gets a regression-guard test. PR template gets a checkbox: "Did this PR introduce a load-bearing property? If yes, name the guard." Optionally, CI fails if a load-bearing test gets deleted without an accompanying ADR justifying it.

The PR #197 lost-fix case is the canonical motivator. Today there is zero mechanism to alarm when a deliberate structural fix gets buried by a subsequent refactor.

---

## Phase 0 — synthesis (ready for Phase 1)

### What we learned

**Three lenses applied to the whole foreman tree produced 9 categories of finding:**

1. **30-day PR pattern (Lens A):** ~40% bandaid / 35% structural / 15% feature / 10% docs. Bandaid clusters around SDK boundary (now addressed by #266/#274), worktree lifecycle (still bandaiding), label state machine (still bandaiding).
2. **Reverted structural fixes (Lens A regression audit):** **2 of 5** sampled top-impact PRs have regressed silently on main (#197 labels, #207 stdout-mirror). Suggests a broader 90-day sweep would find more.
3. **Dead code (Lens C mechanical + eyeball):** 12 confirmed entries. 1 active import bug (`ProviderInvalidResultError` in `__all__`). 11 unused methods/fields.
4. **Duplication / DRY violations:** 10 per-role IdentityRegistry methods over a generic. 16+ per-role `_LABEL_*` constants (re-introducing the failure mode #197 fixed). 3 per-role `_<Role>PreflightRefusal` classes for the same shape.
5. **Half-dead modules:** v2 `dispatcher.py` still actively imported by 5 v3-era modules. 5 of its `_LABEL_TO_ACTION` map entries reference labels nothing creates.
6. **Boilerplate tax:** ~33 lines / 15% of `daemon_host.py` is repeated 3-line preamble.
7. **Module-split confusion:** `foreman.provider` (singular) vs `foreman.providers` (plural) — intentional but undocumented.
8. **Prompt drift risk:** all 6 role prompts last touched 5+ days ago; code has evolved.
9. **Refactor candidates (Lens B):** Template Method for role runners (highest ROI), Command registry for actions, explicit State for label state machine, context-manager for daemon_host repo access.

### Phase 1 agenda (refined)

The original foreman#269 six topics + the new §7 are:

1. Snapshot-eval vs lifecycle events
2. Layered defenses audit (with new dimension D — durability)
3. Role-runner contract formalization (Template Method)
4. Provider boundary generalization (or "document the split first")
5. Outcome vocabulary (already done; residual decision on role-level outcomes)
6. State-machine visibility (build visualizer as `foreman doctor` subcommand)
7. **NEW: Regression detection for structural fixes**

Plus operational follow-ups Phase 0 surfaced:

- 2 confirmed regressions to resurrect (#197 labels, #207 stdout-mirror)
- 12 dead-code entries to delete
- 1 active import bug to fix (`ProviderInvalidResultError` in `__all__`)
- v2 `dispatcher.py` migration (finish or rename)
- daemon_host.py preamble refactor (small, do it)

**Phase 0 is complete.** Ready for Phase 1.

---

## Phase 1 — Decisions log

Each finding gets one decision row. Format: finding summary, decision, rationale, next-step ticket placeholder.

### Decision 1 — Restore PR #197 Labels as a `Labels` StrEnum following the `Outcome` pattern

- **Finding:** PR #197's centralized `Labels` catalog was silently reverted by subsequent refactors. Today the 16+ `_LABEL_*` constants are duplicated across role modules.
- **Decision:** Resurrect as a **`Labels` StrEnum** (NOT the original class-attribute pattern). Follow the foreman#258 `Outcome` enum design: StrEnum members with classification metadata via custom `__new__`. One source of truth, type-system enforced.
- **Rationale:** consistency with the canonical pattern we landed today. The Outcome enum from #258 is now THE shape for "centralized vocabulary with metadata" in foreman. Doing labels the same way means we have one shape, not two.
- **Cost:** ~half-day implementation vs ~half-hour for verbatim resurrection.
- **Benefit:** consistent pattern with #258; if anyone tries to add a label without classification, Python's enum machinery raises `TypeError` at module load (same structural guarantee).
- **Open question deferred:** the durability mechanism (how to prevent THIS from being silently reverted next time) — that is its own architectural topic for a later decision round.
- **Next-step ticket:** TBD after Phase 1 closes; will reference this decision.

### Decision 2 — Role-runner `RoleRunner` ABC via strangler-fig migration, executed back-to-back

- **Finding:** four role runners (`planner.py`, `reviewer.py`, `fixer.py`, `worker.py`) share 80%+ of their shape (setup → preflight → dispatch → terminate) but have no shared abstraction. Every defensive change (#255 marker exceptions, #270 git identity injection, future #266 boundary integration) is applied 4× with drift risk. Lens B finding flagged Template Method as highest-ROI structural refactor.
- **Decision:** **Option B (strangler-fig)** — introduce `RoleRunner` ABC + `run()` template, then migrate one role at a time. **Sequencing constraint: back-to-back as a single work sprint, not spread over days/weeks.** Order: Planner first (simplest, no PreflightRefusal yet) → Reviewer → Fixer → Worker. Each migration is its own PR for review granularity; all four land sequentially without long gaps.
- **Rationale (against the alternatives):**
  - Option A (big bang) felt too risky given recent stability work — single PR with 2000 LOC delta and 4× module touch is the kind of change that masks regressions.
  - Option C (skeleton-first) too cautious — without actual migrations we cannot catch cases the template does not handle. The Planner migration is also the validation of the ABC design.
- **Rationale (back-to-back sequencing):** spreading the migrations over multiple weeks means the codebase lives in a partial-migration state (some roles using the ABC, others not) for an extended window. That partial state is itself a Lens C/B finding (`dispatcher.py` is exactly this — the v2 module that was supposed to be migrated to v3 but the work stalled half-done). Back-to-back avoids becoming the next `dispatcher.py`.
- **Sequencing dependency:** Decision 1 (Labels StrEnum) lands first; role migrations consume it from the start so the migrated roles use `Labels.SPEC_FIX` instead of importing per-module `_LABEL_*`.
- **Integration opportunities** that land alongside the migration:
  - Marker exceptions consolidate to a single `RolePreflightRefusal` base class
  - Per-role identity-resolution boilerplate consolidates via the `setup()` template slot
  - foreman#266 provider boundary consumed by `run()` template's dispatch step
  - foreman#258 typed `Outcome` enum consumed by `terminate_*` template slots
- **Next-step ticket:** TBD after Phase 1 closes. Should reference both Decision 1 (Labels first) and the back-to-back sequencing constraint so a future operator does not partially-migrate and walk away.

### Decision 3 — v2 layer is dead code; excise via `vulture` + reachability cross-check

- **Finding correction:** I initially framed this as Lens B "dual Action universes in active use" based on `dispatcher.py` still being importable by 5 modules. Jeff pushed back ("I thought we were completely off of v2 at this point and that old code was dead code"); verification confirmed his read. Production runtime entrypoint is `foreman daemon v3-start` (per `docker/entrypoint.sh:75`), which routes entirely through `foreman.reconciler.*` — no imports of `dispatcher`, `daemon`, `worker`, `poller`, `queue`, `daemon_runners`, or `role_dispatch`. The v2 surface is a **closed dead island** of 7 modules + the `daemon start` CLI subcommand + 3 helper functions in `cli.py`, reached from no live path. The reason I misread it was that the modules still pass static `import` resolution, which is itself evidence of the cognitive cost of leaving dead code on disk — it actively fooled the analysis two minutes ago.
- **Reclassification:** Lens C (dead code), not Lens B (dual universes in active use).
- **Decision:** **Smart Option A — excise the dead set via tooling**, not by manual grep. Two-tool stack:
  - **`vulture`** at `--min-confidence 80` against `packages/foreman/src/foreman/`, with a committed `vulture_whitelist.py` for the predictable Python false-positives in this codebase (Click commands, Pydantic validators, pytest fixtures, hookspec callbacks, `__all__` exports, `__new__` metadata enums).
  - **Reachability AST-walk script** starting from the live CLI entry points (`daemon v3-start`, `init run`, `ps`, `pipeline-detail`) plus the test-suite entry points, marking every transitively-imported module. Complement = unreachable modules. The v2 island shows up as a closed component automatically; anything else dead in the tree (e.g. `ProviderInvalidResultError` in `providers/__init__.__all__` — already flagged by Lens C) surfaces too.
  - High-confidence dead = (vulture-flagged at ≥80) ∪ (modules not reachable from entry points).
- **Rationale (against the alternatives):**
  - Manual-grep A (the version I originally proposed): brittle. I just misidentified the v2 surface as live by eyeballing imports; I could miss other dead code I'm not specifically looking for.
  - Option B (quarantine + telemetry): "harmless if nobody runs it" is exactly what enabled the v2 island to live since the Docker cutover; leaving it longer prolongs the cognitive-cost finding.
  - Option C (keep): no operational justification — v2 has been off the production path since the Docker cutover (#343).
- **Why tooling-driven is "smart":** outputs an auditable list, catches dead code in corners I have not looked at, and the whitelist is the artifact that prevents the dead set from growing back. The same `vulture` invocation can later become a CI gate — but the gate decision belongs to execution planning, not analysis.
- **Execution-phase questions deferred** (to the implementation plan, not here):
  - One-shot audit vs CI-regression-gate
  - Single PR vs staged-by-confidence-tier excise
  - Whether to delete the `daemon start` CLI subcommand at the same time as the modules it transitively uses, or in a follow-up
- **Next-step ticket:** TBD after Phase 1 closes. Should reference both the tooling stack (vulture + reachability) and Decision 2's back-to-back constraint (the v2 island is itself a partial-migration carcass — exactly the failure mode Decision 2 is trying to prevent for the role-runner ABC).
- **Cross-reference (Topic 7 — `ProviderInvalidResultError` zombie):** the active import bug in `foreman.providers.__all__` is the textbook case this tooling is designed to catch. Recording it here so the relevance is obvious: if a `__all__` export references a class that does not exist, the reachability sweep's symbol resolution step trips, and `vulture --confidence=100` flags the dangling string. Both signals would have surfaced it months ago.
- **Cross-reference (Topic 8 dissolution — Phase 0's 12-item dead-code catalogue):** Phase 0 manually catalogued ~12 specific dead-code items (the v2 island, `ProviderInvalidResultError`, ~10 smaller orphans — unused helpers, dead test fixtures, prompt-template fragments referenced nowhere). Rather than file Topic 8 as a separate decision, the catalogue is absorbed into Decision 3 as the **calibration baseline for the first tooling run**: when vulture + reachability execute, the result is compared against this 12-item list. If the tool misses items from the catalogue, the vulture whitelist / reachability entry-points need tuning. If the tool finds more, that's signal that the manual sweep undercounted. The catalogue becomes the calibration set; it does not itself need a decision entry.

### Decision 4 — Bandaid-ratio guardrail: artifact discipline (B) + prompt-level bias toward structural patterns

- **Finding:** Phase 0 Lens A tallied ~40% of post-Docker-cutover merges as defensive patches against symptoms rather than structural fixes. Without a guardrail, the ratio drifts back up after every stability sprint.
- **Decision:** **Combined intervention — artifact discipline AND prompt-level bias.** Two complementary layers:
  - **Layer 1 (Option B from the three-option set) — artifact discipline.** When a defensive patch is the right call (hot fire, structural fix too expensive right now), the patch ships with (a) a regression test that pins the bandaid in place and (b) a tracking ticket for the eventual structural fix. The test + ticket are the durable signal that survives memory drift, where social process (Option A) and calendar-driven sweeps (Option C) do not.
  - **Layer 2 (Jeff's twist) — prompt-level bias toward GoF + Google principles.** Encode a calibrated lens into both `foreman/CLAUDE.md` (for human/Wren-driven work in the tree) and `prompts/planner.md` + `prompts/reviewer.md` (for autonomous-loop output, where most bandaid-ratio risk actually lives). Empirical observation: "what would Google do + which GoF pattern applies" pulls LLM output into a higher-quality sample neighborhood than "what is the best way." Calibration matters — see wording below.
- **Calibrated wording** (load-bearing — "or say it doesn't fit" prevents pattern-fishing):
  > Before proposing a non-trivial design, name the GoF pattern and/or the Google engineering principle (SRP / OCP / DIP / "make the right thing easy") the design embodies. If neither applies cleanly, say so explicitly — "no pattern fits, this is straightforward X" is a legitimate output. Pattern-fishing produces worse code than no pattern at all.
- **Why the two layers compose** (rather than one replacing the other):
  - Layer 2 biases the *initial* Planner output away from bandaids → fewer bandaid PRs even filed
  - Layer 1 catches the bandaids that still ship under fire → no untracked bandaid survives
  - Two-stage filter: lower bandaid input rate; bandaids that ship are tracked + tested
- **Rationale (against alternatives):**
  - Option A (PR-template process gate) alone: social, easy to drift, no artifact
  - Option C (quarterly de-bandaid sweep) alone: calendar-dependent, retrospective only
  - B alone: catches bandaids after the fact but doesn't reduce the input rate
  - Twist alone: improves initial proposals but provides no audit trail for bandaids that do ship
- **Risks named:**
  - Pattern-fishing (Adapters everywhere) if the lens is uncalibrated — the "or say it doesn't fit" clause is the defense
  - Subagent-prompt bloat slowing simple work — kept to 3-4 lines
  - Layer 1 producing test-as-bandaid-monument (the regression test memorializes the bug shape instead of fixing it) — addressed by the tracking ticket linkage
- **Tracking metric:** subsequent architecture audits compute bandaid-ratio; if not lower after both layers are in place, the lens didn't bite.
- **Sequencing dependency:** none with Decisions 1-3 — Decision 4 is process/prompt-level, not code-level. Can land any time.
- **Next-step ticket:** TBD after Phase 1 closes. Should reference both layers (artifact discipline + prompt bias) and the calibration wording above so a future operator does not strip the "or say it doesn't fit" clause as redundant.

### Decision 5 — Merge `foreman.provider` (singular) + `foreman.providers` (plural) into one `foreman.provider` package

- **Finding:** Two sibling packages with confusingly similar names doing different things — `foreman.provider` (singular) holds the ABC + `UsageInfo` + legacy exception family; `foreman.providers` (plural) holds concrete adapters + the recovery chain + Strategy implementations and additionally exports `ProviderInvalidResultError` in `__all__` (the zombie symbol from Lens C — class doesn't exist, would crash on import-by-name). The split is a Java/C# "interface package vs implementation package" convention that does not translate to Python idiom; it has already misled reviewers (see the #266 GoF refactor PR comment thread).
- **Decision:** **Option B — merge into one package, `foreman.provider`, with internal sub-modules:**
  - `provider/__init__.py` — public surface
  - `provider/api.py` — ABC + `UsageInfo` + protocol types
  - `provider/adapters/` — concrete adapters (Anthropic SDK, future others)
  - `provider/recovery.py` — recovery chain + Strategy implementations
  - `provider/exceptions.py` — single source of truth for the exception family (the zombie `ProviderInvalidResultError` either gets defined here or removed from `__all__`)
- **Rationale (against the alternatives):**
  - Option A (rename + keep split, e.g. `provider` → `provider_api`): cheapest but preserves the underlying confusion. The Java convention is wrong for Python; renaming hides the smell rather than removing it.
  - Option C (three-way split — `provider` for ABC, `provider_recovery` for chain, `adapters` for impls): same number of confusing boundaries as the current bad split, just rearranged. Three packages each with "one SRP-clean responsibility" produces three cross-cutting import chains where one would do.
- **Rationale (for B):** Python idiom is "one package per coherent concept, internal sub-modules for separation." The provider concept is coherent — ABC, adapters, recovery, exceptions all belong to the same domain. Single package + sub-modules is the boring-correct answer that nobody is confused by six months from now. Also surfaces the zombie `ProviderInvalidResultError` for resolution (either define it or remove it) as part of the merge.
- **Integration opportunities:**
  - Resolves Lens C zombie `ProviderInvalidResultError`
  - Eliminates one of the cross-module-import knot that the recent #266 refactor partly addressed but left half-resolved
  - Pairs naturally with Decision 2's `RoleRunner` ABC migration — the role-runner template's dispatch step consumes the provider; cleaner provider package = cleaner dispatch slot
- **Sequencing dependency:** none with Decisions 1-4. Independent of role-runner work, label work, dead-code excise, and the bandaid guardrail.
- **Next-step ticket:** TBD after Phase 1 closes. Should reference the zombie symbol resolution as part of the merge so it doesn't slip into a separate cleanup ticket.
- **Cross-reference (Topic 7 dissolution):** the active import bug `ProviderInvalidResultError` (the zombie symbol from Lens C) is absorbed into this decision's scope rather than being filed as a separate decision. It's exactly the shape Decision 3's vulture + reachability sweep is designed to catch, and exactly the shape Decision 7's import-boundary CI is designed to prevent reappearing.

### Decision 6 — `daemon_host.py` boilerplate: constructor injection (C) + verify-and-pin the dependency's lifecycle invariant

- **Finding:** `daemon_host.py` is 213 lines, 12 methods. 11 of the 12 begin with the same ~3-line preamble (`gh = self._registry.get_orchestrator_client()` + repo lookup + try-block) — roughly 33 lines (~15%) of repeated setup. Three SOLID smells stacked: repetition, hidden coupling to the registry concrete shape, DIP violation (the host knows the exact registry → orchestrator client → PyGithub `Repo` chain).
- **Decision:** **Option C — constructor injection.** `GitHubDaemonHost.__init__` receives the already-resolved orchestrator client (or a `RepoLookup` callable). Methods call `self._lookup(repo_full)` directly. Boilerplate disappears at the source rather than being factored into a helper. Registry becomes injectable, tests get a fake without monkeypatching, the DIP smell goes away.
- **Rationale (against alternatives):**
  - Option A (helper method `_gh_repo`): cuts 33 lines to 11 single-line calls, but the dependency is still re-resolved per call and the DIP smell remains. Cheapest, structurally weakest.
  - Option B (decorator): same effect as A with extra Python cleverness; signature-rewriting decorators are reviewer-confusers; gain is marginal.
  - Option C: fixes the symptom AND the underlying smell. Pairs naturally with Decision 2 (the `RoleRunner` ABC will use the same lift-deps-to-`__init__` pattern).
- **Required verification before the refactor lands** (Jeff's explicit ask 2026-06-11 PM): the decision's correctness depends on `IdentityRegistry.get_orchestrator_client()` returning a client whose underlying GitHub-App installation token is **refreshed automatically** as the token approaches expiry. If the registry hands out a once-resolved-and-cached client that silently expires, constructor injection caches that expiry into `GitHubDaemonHost`'s lifetime — bug buried deeper instead of fixed. **Verify before refactor; pin the verification in a test, not just a comment.** Concrete shape: a test that constructs `GitHubDaemonHost`, advances time past the installation-token TTL (mock the clock), and asserts the next call to `_lookup()` still succeeds because the registry refreshed under the hood.
- **Generalizable sub-rule (Jeff's "I don't want that kind of thing leaking into the rest of the system"):** every constructor-injection refactor in this codebase MUST verify-and-pin the lifecycle invariant of the lifted dependency. The verification lives as a test (not just a code comment) so that a future operator who changes the dependency's TTL/refresh behavior trips the assertion rather than silently expiring tokens in production. This rule applies again to Decision 2 (the `RoleRunner` ABC will lift per-role identity clients to `__init__` and inherits the same verification requirement).
- **Why pin-as-test, not pin-as-comment:** Decision 4 codified the principle that artifacts survive memory drift better than process. A docstring saying "this assumes auto-refresh" rots when the registry's refresh policy changes silently; a test fails loudly.
- **Sequencing dependency:** no hard sequencing with Decisions 1, 3, 5 (independent surface areas). Decision 2 shares the generalizable sub-rule — when 2 lands, it inherits the verify-and-pin discipline.
- **Next-step ticket:** TBD after Phase 1 closes. Should reference (a) Option C as the implementation shape, (b) the token-TTL refresh test as a precondition of the refactor merging, (c) the generalizable sub-rule so it propagates to future constructor-injection refactors (Decision 2 included).

### Decision 7 — Adopt `import-linter` CI rules with the discipline "only when a decision creates the boundary"

- **Finding:** Phase 0 surfaced multiple cases where soft architectural boundaries silently rotted. The v2 dispatcher module remained importable from anywhere in the tree for months after v3 was supposed to replace it; the `ProviderInvalidResultError` zombie sat in `__all__` undetected because nothing exercised the import; the singular-vs-plural provider package split misled even reviewers actively editing it (#266 comment thread). Jeff's question raised the canonical fix shape from another repo: CI-enforced import boundaries so that only specific packages may import from named other packages (or from external code).
- **Decision:** **Yes — adopt `import-linter` as the boundary-enforcement layer**, with three rules tied directly to decisions already made in this Phase 1 round. Add new rules only as future decisions create the boundaries they enforce. Do NOT pre-populate the rule set with hypothetical layers.
- **Three initial rules** (each tied to an in-flight decision):
  - **R1 — Provider-adapter boundary (Decision 5).** Only modules under `foreman.provider.adapters.*` may import third-party provider SDKs (`anthropic`, future `openai`, etc.). All other consumers must talk to the `provider.api` ABC. Prevents the provider boundary from silently re-splitting.
  - **R2 — Role-runner boundary (Decision 2).** `foreman.reconciler.*` may not import from `foreman.roles.*`. Only the role-dispatch seam (`reconciler/role_dispatch.py`) bridges the two; reconciler does not reach into role internals. Prevents the v3 reconciler from sneaking back into role-runner shape the way the v2 dispatcher did.
  - **R3 — Dead-island prevention (Decision 3).** Modules on the reachability complement (unreachable from the live entry-point graph computed by Decision 3's tooling) fail CI. This is what permanently closes the v2 island once excised and catches the next dead-island shape *before* it accumulates. Implementation note: the import-linter contract type is `forbidden` against any module flagged by the reachability sweep.
- **Tool: `import-linter`** — mature, declarative config (`importlinter.cfg`), integrates with pre-commit and the existing `just check` gate. The three rule shapes above are all standard contracts it supports (`forbidden`, `layered`, custom-with-helper-script).
- **Rule-set discipline (the load-bearing piece):** add a rule **only when a decision creates the boundary it enforces.** No hypothetical-future layers; no "let's also enforce X just in case." Reason: the failure mode of boundary-rule CI is config drift — every new package needs a rule, every refactor needs the rule updated, and the moment the config falls behind the code the rules stop biting. Keeping the rule set small enough to inspect at a glance is the only sustainable defense against this drift.
- **Layered defense framing** (why this composes with Decisions 3, 4, 6):
  - Decision 4 = soft guardrail (prompt-level bias toward GoF + Google patterns during initial design)
  - Decision 6 = test-as-truth (verify-and-pin invariants as tests, not comments)
  - Decision 3 = sweep tooling (vulture + reachability find dead code AFTER it forms)
  - Decision 7 = boundary CI (import-linter prevents specific boundary violations from forming AT ALL)
  - The four together: bias the initial design toward good patterns → pin lifecycle assumptions as tests → catch dead code retrospectively → block boundary violations at CI. Defense in depth; each layer catches what the others miss.
- **Why this is "worth it" for foreman specifically, not for every project:**
  - ~30-module codebase is the threshold where humans CAN still hold all boundaries in their heads but won't reliably do so under deadline pressure
  - We're explicitly hardening boundaries this sprint (Decisions 2, 3, 5) — adding the enforcement layer alongside is cheaper than adding it later
  - A 5-module project would be pure overhead; a 100-module project would make this non-negotiable; we're in the band where the discipline of "only when a decision creates the boundary" keeps it from tipping into overhead
- **Rationale (against the do-nothing alternative):**
  - Without CI enforcement, every one of Decisions 2/3/5 relies on reviewer vigilance during PR review. Reviewer vigilance has already been demonstrated insufficient — see the v2 dispatcher dead island, the provider-package split confusion, the zombie `__all__` export.
  - Soft conventions in CLAUDE.md / prompts (Decision 4) bias initial proposals but do not catch the case where someone explicitly chooses to violate the convention "just this once."
- **Sequencing dependency:** R1, R2, R3 land *with* their respective parent decisions (5, 2, 3) — the import-linter rule and the boundary it enforces ship in the same PR so the rule is testing the new shape, not retrofitting the old. The `import-linter` framework itself can land independently first.
- **Next-step ticket:** TBD after Phase 1 closes. Should reference (a) `import-linter` adoption as the framework PR (independent), (b) the rule-set discipline ("only when a decision creates the boundary"), and (c) the three initial rules with their parent-decision linkage so the rule provenance survives memory drift.

### Decision 8 — Library-boundary audit with tier table (extends Decision 7's rule-source discipline)

- **Finding:** Decision 7 specified three import-boundary rules (R1 provider-adapter, R2 role-runner, R3 dead-island) tied to specific decisions in this Phase 1 round. But several third-party libraries currently imported across `foreman.*` are high blast-radius if misplaced (network surface, process spawn, DB layer, crypto) and don't yet have boundaries enforced. The "only when a decision creates the boundary" discipline was protecting against pre-building rules for hypothetical layers, not against discovering boundaries that already exist implicitly. An audit IS a decision-making activity — done in batch instead of piecewise — and each resulting rule still traces back to a documented justification.
- **Decision:** **Run a library-boundary audit** as part of Phase 1's execution work. The audit produces a tier table of every third-party library imported in `foreman.*`, ranked by *blast-radius if imported from the wrong place*. Tier 1 libraries get import-linter rules immediately; Tiers 2 and 3 stay documented but unbound. Decision 8 amends Decision 7's rule-source discipline from "only when an in-flight decision creates the boundary" to "rules come from documented decisions, **including** the explicit library-boundary audit." Same load-bearing principle (each rule has documented justification), broader sourcing.
- **Straw-man tier table** (subject to verification when the audit actually runs against the current tree):
  - **Tier 1 — bind now (high blast-radius if misplaced):**
    - `anthropic` — LLM SDK; only `provider.adapters.*` (this is Decision 7's R1 absorbed)
    - `github` (PyGithub) — GitHub REST; only `identity.*`, `auth.*`, `git_hosts.*`, `daemon_host.*`
    - `httpx` — HTTP client; only `git_hosts.*`, `reconciler.gh_graphql`, future provider adapters
    - `subprocess` / `asyncio.create_subprocess_*` — process spawn surface; only `worktree.*`, `reconciler.v3_host` (for role-dispatch subprocess)
    - `sqlite3` / `aiosqlite` — DB surface; only `reconciler.exec_log`, `storage.*`
    - `pyjwt`, `cryptography` — JWT minting; only `auth.*`, `identity.*`
  - **Tier 2 — bind when a second-consumer threatens (don't pre-build the rule):**
    - `click` — CLI framework; only matters if non-CLI code starts importing it
    - `rich` — pretty-print; only matters if business logic starts touching terminal output
  - **Tier 3 — explicitly NOT bound, with reason:**
    - `pydantic` — intentionally used everywhere; binding would create overhead with no payoff
    - `pathlib`, `os`, `sys`, stdlib basics — universally needed
    - `tomllib` / `tomli` — used wherever config loads; no leakage concern
- **The Tier 3 column is load-bearing** (anti-pattern defense): the audit's failure mode is bind-everything. By explicitly naming the libraries we are *not* binding *and why*, we prevent a future operator from reflexively adding boundaries to libraries that should stay unconstrained. The phrase "intentionally everywhere" must appear in the doc for `pydantic` (and any future library that crosses module boundaries by design).
- **Rationale (against do-nothing):** without the audit, the rule set is whatever R1/R2/R3 from Decision 7 happened to surface — `anthropic` covered, but `httpx`/`subprocess`/`sqlite3`/`pyjwt` left ungoverned. Those are higher blast-radius than the v2 dispatcher import we just spent a decision on. The audit is the cheapest way to know whether other dispatcher-shaped problems are hiding.
- **Rationale (against expanding without audit):** "add 6 more rules" without naming why each one is bound (versus Tier 2/3) is exactly the bind-everything failure mode. The tier table forces the justification onto the record so future operators can re-tier as the codebase evolves.
- **Composes with prior decisions:**
  - Absorbs Decision 7's R1 (anthropic) into the Tier 1 set
  - R2 (role-runner) and R3 (dead-island) remain as documented in Decision 7 — they are intra-codebase boundaries, not library boundaries; they are orthogonal to the tier table
  - Decision 4's prompt-bias toward GoF/Google patterns naturally drives Tier-1-shaped thinking ("which libraries are at boundaries Google would harden?")
- **Sequencing dependency:** Decision 7 framework lands first (need `import-linter` configured before rules can land); audit runs as a Phase 2 task; Tier 1 rules ship in the framework PR or a fast-follow PR.
- **Next-step ticket:** TBD after Phase 1 closes. Should reference (a) the audit as a Phase 2 task that produces the tier table as an artifact in the repo (e.g. `docs/architecture/library-boundaries.md`), (b) Tier 1 rules ship at audit-completion time, (c) the Tier 3 explicit-non-bind discipline so future operators don't reflexively add rules.

### Decision 9 — Impl-PR-base-retarget bug in the autonomous loop (the actual durability problem from Decision 1's §7)

- **Empirical root cause** (verified 2026-06-11 PM):
  - Issue #194 ("centralize foreman:* label constants") was closed today with `foreman:done` label.
  - **PR #196 (spec):** `base=main`, `head=foreman/issue-194`. Files: ONLY `docs/superpowers/specs/foreman-issue-194-spec.md (+490/-0)`. Merged to main 2026-06-07 18:06.
  - **PR #197 (impl):** `base=foreman/issue-194`, `head=foreman/impl-194`. Files: `labels.py (+169/-0)`, `test_labels_keystone.py (+285/-0)`, plus 7 modified consumers across `init/reconciler/roles`. Merged into `foreman/issue-194` at 19:30 — **NOT into main.**
  - Branch protection on main is `main-gate` ruleset with `strict_required_status_checks_policy=True` (verified). So the strict-up-to-date rebase cycle Jeff suspected as Hypothesis 2 IS real-but-separate; it did not cause Labels loss.
  - Verified via `git show main:packages/foreman/src/foreman/labels.py` → "fatal: path does not exist." The file lives only on the orphan branches `foreman/issue-194` and `foreman/impl-194`.
- **Diagnosis:** **The autonomous loop's PR-base-retarget step is missing or broken.** Expected lifecycle:
  1. Planner creates spec PR (base=main, head=foreman/issue-N) and impl PR (base=foreman/issue-N, head=foreman/impl-N)
  2. Spec PR merges to main → spec branch should auto-delete OR impl PR's base should be retargeted to main
  3. Impl PR merges to main
  4. Daemon marks issue `foreman:done`
  - In the observed sequence: step 2's retarget did not happen; step 3 became "impl PR merges into the orphan spec branch"; step 4 fired anyway because it only checked PR-merged, not merge-target.
- **Neither hypothesis from this morning was right.** Worth recording explicitly so the diagnostic dead-ends don't get re-walked:
  - NOT Hypothesis 1 (merge-flurry overwrite) — git log shows no later commit deleted labels.py from main; it was never there.
  - NOT Hypothesis 2 (rebase cycle loses content) — strict-up-to-date IS enforced; the rebase cycle is a real smell but not the cause here.
  - Actual cause: structural bug in the autonomous loop's state machine.
- **Decision:** **File this as a high-priority Phase 2 investigation with three specific questions to answer.** No code change here in Phase 1; we don't know enough yet to specify the fix. Phase 1 records the finding and the investigation questions.
  - **Q1 — Scope: how many other "done" tickets are orphaned?** Audit: for every `foreman:done` issue in foreman/voice/agent_core, check whether the impl PR's mergeCommit is reachable from `origin/main`. If not, the work is orphaned. Cheap script (~20 lines via `gh pr list` + `git merge-base --is-ancestor`). Kicked off in parallel with this Decision being recorded.
  - **Q2 — Code: where in the autonomous loop is the retarget supposed to happen?** Suspected sites: `daemon_runners.merge_spec_pr` (should issue `gh pr edit <impl-pr> --base main` for the corresponding impl PR after the spec merges); or `dispatcher`/`reconciler` action sequencing where the "spec merged, retarget impl" state transition lives. Verify in code.
  - **Q3 — Test: what's the regression test that would have caught this?** Shape: after `merge_spec_pr(spec_pr)`, assert the corresponding impl PR's `baseRefName == "main"`. AND after `merge_impl_pr(impl_pr)`, assert the merge commit is reachable from main. The second assertion is what would have flagged #194 specifically.
- **Why this is the real §7 from Decision 1:**
  - Decision 1 deferred "the durability mechanism" — how to prevent the Labels-pattern regression from recurring silently.
  - I previously thought the four-layer defense (Decisions 3/4/6/7) answered §7 in aggregate. It does NOT. The four layers protect against *human/Wren errors* (bias, drift, dead code, boundary violations). They don't catch a *foreman autonomous-loop bug* that lies about completion.
  - The autonomous loop reporting "done" on content that never landed is the canonical untrustworthy-machinery problem. Tests-green + dispatch-clean + `foreman:done`-label all said success; main never had the code.
  - This finding therefore *re-opens* §7 specifically: durability against the autonomous loop's own failure modes. The audit (Q1) is the empirical scoping; the code change (Q2) and the test (Q3) are the durability artifact.
- **Composes with prior decisions:**
  - Decision 3 (vulture + reachability) — if labels.py had been referenced from main code as if it existed, the reachability sweep would have flagged the missing module. (Did the dependency exist? Verify in Phase 2.)
  - Decision 4 (artifact discipline) — the artifact here would be the Q3 test: post-merge-target assertion.
  - Decision 6 (verify-and-pin lifecycle invariants) — Q3 is a verify-and-pin of the merge-target invariant.
  - Decision 7 (import-linter R3 dead-island prevention) — would have flagged `labels.py` callers on main as importing-from-missing-module.
- **Sequencing dependency:** none for Phase 1 closure. Q1 audit ran in parallel with this entry being written; results below.
- **Q1 RESULTS — orphan-content audit (2026-06-11 20:50 PM):**
  - **15 merged PRs are orphaned** (mergeCommit not reachable from `origin/main`). Every single one has `base=foreman/issue-N` instead of `base=main`. Dates range from 2026-06-02 (#58) to 2026-06-11 (#273).
  - **3 of the 15 were manually rescued today** via "promote ... to main" PRs:
    - #266 GoF provider boundary → rescued by **#274** "refactor(provider): promote ProviderAdapter + RecoveryChain boundary to main"
    - #268 reviewer-budget gate → rescued by **#275** "fix(reconciler): promote reviewer-budget gate fix to main (foreman#268)"
    - #256 dead Literal cleanup → rescued by **#276** "chore(stats): promote dead *_failed Literal cleanup to main (foreman#256)"
  - **The morning's stability sprint was actually a rescue sprint.** The status note at the top of this plan claimed "All five overnight PRs landed on main" — true only because three of the five required manual `promote ... to main` PRs. The original impl PRs (#271, #273, #260) all merged into orphan branches.
  - **At least 2 orphans are confirmed still missing from main:**
    - #207 (`feat(logging): mirror daemon log to stdout as JSON for docker logs`) — verified by `git grep` on `logging_setup.py`; this is exactly the regression Phase 0's Lens A flagged about logging.
    - #197 (`refactor(labels): centralize foreman:* label constants`) — the original Decision 1 motivating case.
  - **~10 more orphans remain unaudited:** #58, #159, #168, #186, #189, #192, #202, #206, #212, #225. Need per-PR check: was the content rescued by a later "promote" PR, by a separate refactor that re-implemented it, or is it still missing? (#168's `attempt_merge` symbol DOES appear on main, suggesting it was implicitly rescued by a later commit.)
- **Updated diagnosis** (in light of audit results): the bug is NOT "newly discovered today." It has been silently failing the autonomous loop for at least 9 days (since 2026-06-02 #58). The team has been working around it manually via "promote ... to main" rescue PRs — but the workaround is incomplete. The team also has not realized this is a single systemic bug; each rescue has been treated as a one-off "weird, the merge went sideways" fix.
- **Updated severity:** **CRITICAL.** The autonomous loop has been reporting `foreman:done` on content that never reached main for over a week. Every "completed" ticket needs verification. The container that's been running in production is built off a main that's missing ~12 of the last month's structural fixes. We've been building today's architecture review on the implicit assumption that those fixes are in place; the assumption is FALSE.
- **Next-step ticket:** **HIGH-PRIORITY** foreman issue with the three investigation questions, the empirical evidence above, the audit results, and a fix-the-loop scope. Title: `bug: autonomous loop merges impl PR into orphan spec branch; content never reaches main; daemon labels done anyway`. Must be addressed before any further autonomous-loop tickets run (otherwise the orphan list keeps growing). Pair with: a rescue sprint that audits the remaining ~10 orphans and either re-promotes their content or filed-as-known-dropped tickets.
- **Operational action taken 2026-06-11 20:54 PM:** foreman daemon container stopped (`docker compose stop daemon`). The autonomous loop is paused on all three registered projects (foreman, voice, agent_core) until Decision 9's fix lands. Per Jeff's decision: "First stop foreman. Second document that we need to do this but don't do it yet. Finish the analysis and get all documented."

#### D9 Phase 2 analysis findings (2026-06-11 21:00–21:15 PM, read-only)

##### Q1 TAIL — Per-PR triage of the 10 untriaged orphans (DEFINITIVE):

- **3 still ORPHAN (code content):**
  - **#197** Labels keystone — `packages/foreman/src/foreman/labels.py` (+169) + `tests/test_labels_keystone.py` (+285). Original Decision 1 motivating case.
  - **#207** stdout JSON logging — `feat(logging): mirror daemon log to stdout as JSON for docker logs`. **This is the Phase 0 Lens A logging regression** — it's not a regression, the fix never landed.
  - **#159** `--pr-url` in fixer — verified 0 matches on main for `pr_url` symbol in `roles/fixer.py`.
- **2 still ORPHAN (non-code content):**
  - **#212** 24h `has_recent` boundary test for lagging label rules.
  - **#186** role-runtime speedup investigation doc.
- **7 RESCUED implicitly** by later code paths re-implementing the same content (verified by symbol presence on main): #58 (branches module), #168 (attempt_merge action), #189 (worktree reattach), #192 (clear stale merging-* labels), #202 (reviewer target plumbing), #206 (fixer docs), #225 (init.py merging docstring).
- **3 RESCUED explicitly** by today's promote PRs (#274, #275, #276 covering #266, #268, #256).

**Total rescue scope for Phase 2: 5 PRs** (#197, #207, #159, #212, #186). Each is recoverable from its orphan branch via a "promote ... to main" PR.

##### Q2 — Code-path investigation (the retarget step exists but in the wrong place):

**Critical discovery: the retarget logic IS in the repo — and lives on the v2 dead-code island Decision 3 wants to excise.**

- **`packages/foreman/src/foreman/daemon_runners.py:209-255`** — v1/v2 `merge_impl_pr()` has a working retarget conditional at lines 234-243: reads current base via `get_pr_base_ref()`, checks if base == spec_branch AND spec PR merged, calls `retarget_pr_base(pr_number, default_branch)` before merging. The docstring at lines 210-227 explicitly explains the orphan-commit failure mode this guards against (foreman#49 + recovery PR #61 from 2026-06-02 — this exact bug has been "fixed" once before).
- **`packages/foreman/src/foreman/daemon_host.py:195-207`** — `retarget_pr_base()` method exists and works. Calls `pr.edit(base=new_base)` via PyGithub. Production-ready.
- **`packages/foreman/src/foreman/reconciler/actions.py:408-530`** — v3 `_handle_attempt_merge()` reads PR mergeability and calls `host.merge_pr()` directly. **No retarget step.** No predicate check on current base before merge.
- **`packages/foreman/src/foreman/reconciler/rules.py:557-570`** — `_impl_pr_merged_label_lagging` checks `ctx.pr.is_merged` to fire ADVANCE_LABEL_TO_DONE. **`is_merged` is True regardless of merge target.** No verification that the merge landed on main.

**Root cause:** the v3 reconciler migration replaced `daemon_runners.merge_impl_pr()` (which had the retarget) with `ATTEMPT_MERGE_IMPL` action (which doesn't). The retarget logic was simply never ported. The host-layer method (`retarget_pr_base`) was preserved; the call site disappeared.

**This is the same shape as the v2-dispatcher-dead-island finding** from Decision 3, but with sharper teeth: it's not just that v2 code is dead — it's that v2 code contains the ONLY working implementation of a critical correctness invariant the v3 path lacks. The v2 island is load-bearing in a way our pre-D9 analysis didn't appreciate.

##### CRITICAL CROSS-DECISION SEQUENCING (amends D3):

**Port retarget v2→v3 BEFORE excising v2 (Decision 3).** Otherwise we delete the only working implementation of the very logic D9 needs to land. Updated Phase 2 ordering: D9 retarget port → D9 regression test → D9 rescue sprint → D3 dead-code excise. D3's pre-flight check now reads "no v2 module contains code that is uniquely correct vs the v3 equivalent" — the retarget logic must be lifted first.

##### Q3 — Regression test sketch (for after the port lands):

Two assertions, written as one integration test against the autonomous-loop simulator:

```python
def test_impl_pr_base_is_retargeted_to_main_after_spec_merge():
    # Setup: simulate spec PR merged
    spec_pr = make_spec_pr(issue_n=999, base="main", head="foreman/issue-999")
    impl_pr = make_impl_pr(issue_n=999, base="foreman/issue-999", head="foreman/impl-999")
    fake_host.merge_pr(spec_pr.number)  # spec lands on main

    # Action: drive the reconciler one tick with impl-approved label
    fake_host.add_label(issue=999, label="foreman:impl-approved")
    reconciler.tick()

    # Assertion 1 (the retarget check itself)
    impl_pr_now = fake_host.get_pr(impl_pr.number)
    assert impl_pr_now.base_ref == "main", \
        f"Impl PR base should be retargeted to main after spec merge, was {impl_pr_now.base_ref}"

def test_impl_pr_merge_target_is_main_not_orphan_branch():
    # Drive through full spec→impl autonomous-loop cycle
    drive_full_cycle(issue_n=999)

    # Assertion 2 (the merge-target check — would have caught #197 directly)
    impl_pr_now = fake_host.get_pr(impl_pr.number)
    assert impl_pr_now.merged
    assert fake_host.is_commit_in_main(impl_pr_now.merge_commit_oid), \
        "Impl PR merge commit must be reachable from main; otherwise content is orphaned"
```

The second assertion is the one that would have flagged #197 specifically the moment it ran. It belongs in the reconciler e2e test suite, not as a unit test — it specifically checks the integration of merge + label-transition + main-reachability.

##### Phase 2 D9 execution sequence (when work resumes):

1. **Port retarget into v3** — add retarget-conditional inside `_handle_attempt_merge()` at `actions.py` before the existing merge call. Mirror the daemon_runners.py:234-243 logic. Reuse `host.retarget_pr_base()` unchanged.
2. **Add regression test** (the Q3 sketch above) — guard the new path.
3. **Update `_impl_pr_merged_label_lagging`** to also verify merge target is main before firing ADVANCE_LABEL_TO_DONE. Belt-and-suspenders defense.
4. **Rescue sprint** — 5 "promote ... to main" PRs for #197, #207, #159, #212, #186.
5. **Verify on dogfood** — file a test ticket, run it through the loop, confirm impl PR base is retargeted and merge commit lands on main.
6. **THEN** D3 dead-code excise can proceed — at this point `daemon_runners.merge_impl_pr` has no unique correctness over the v3 path.

#### D9 spec sketch — exact code-level changes (Worker lift-and-paste, 2026-06-11 21:30 PM)

Read-only investigation produced this concrete spec. Five code locations, all in `packages/foreman/src/foreman/reconciler/` and `packages/foreman/tests/reconciler/`. The Worker doing D9 can lift these blocks verbatim.

**Architectural note on host wrappers:** `V3GitHubHost` (in `reconciler/v3_host.py`) is a thin delegation wrapper around `self._v2` (the v1/v2 `GitHubDaemonHost` in `daemon_host.py`). The four host methods D9 needs already exist on `_v2`. The Worker only writes delegation wrappers, not new implementations. **When Decision 3 excises v2 later, those four wrappers will need to be flattened into V3GitHubHost — replacing `self._v2.foo(...)` with direct implementations.** This sequencing is acceptable: D9 ships fast with delegation, D3's excise pass does the flatten as part of its scope.

**Site 1 — `packages/foreman/src/foreman/reconciler/host.py:43` — extend `ReconcilerHost` Protocol with four method declarations:**

Insert after the existing `update_branch(...)` declaration (around line 105), keeping protocol-stub style (`...` body):

```python
def retarget_pr_base(
    self, *, owner: str, repo: str, pr_number: int, new_base: str
) -> None:
    """Retarget an open PR's base branch via the GitHub API.

    Used by ``attempt_merge_impl`` to point an impl PR at the default
    branch before merge when the spec PR has already merged. Without
    this guard, the impl PR's squash commit lands on the
    about-to-be-deleted spec branch — an orphan commit unreachable
    from main (issue #62, recurrence diagnosed in foreman#XXX).
    """
    ...

def get_pr_base_ref(
    self, *, owner: str, repo: str, pr_number: int
) -> str:
    """Return the PR's current base branch ref.

    Used by ``attempt_merge_impl`` to decide whether retargeting is
    needed (idempotency: skip the retarget if base is already the
    default branch).
    """
    ...

def is_pr_merged_for_branch(
    self, *, owner: str, repo: str, branch: str
) -> bool:
    """Return True iff a closed, merged PR exists whose head == ``branch``.

    Used by ``attempt_merge_impl`` as the spec-PR-merged predicate:
    only retarget the impl PR if the spec PR has actually landed,
    otherwise we'd merge impl content depending on un-landed spec.
    """
    ...

def get_default_branch(self, *, owner: str, repo: str) -> str:
    """Return the repo's default branch name (typically ``main``).

    Used by ``attempt_merge_impl`` as the retarget destination so the
    impl PR's squash commit lands on a reachable ref.
    """
    ...
```

**Site 2 — `packages/foreman/src/foreman/reconciler/v3_host.py:474` — add four delegation wrappers to `V3GitHubHost`:**

Insert after the existing `merge_pr(...)` method (around line 497, before `_enqueue_pull_request`):

```python
def retarget_pr_base(
    self, *, owner: str, repo: str, pr_number: int, new_base: str
) -> None:
    self._v2.retarget_pr_base(f"{owner}/{repo}", pr_number, new_base)

def get_pr_base_ref(
    self, *, owner: str, repo: str, pr_number: int
) -> str:
    return self._v2.get_pr_base_ref(f"{owner}/{repo}", pr_number)

def is_pr_merged_for_branch(
    self, *, owner: str, repo: str, branch: str
) -> bool:
    return self._v2.is_pr_merged_for_branch(f"{owner}/{repo}", branch)

def get_default_branch(self, *, owner: str, repo: str) -> str:
    return self._v2.get_default_branch(f"{owner}/{repo}")
```

**Site 3 — `packages/foreman/src/foreman/reconciler/actions.py` — add import and retarget guard:**

3a. Add import near the top of the file (with the other foreman imports):

```python
from foreman.branches import spec_branch
```

3b. Modify `_handle_attempt_merge` at line 408. Insert the retarget guard between the existing `if ctx.pr is None:` check (line 440-443) and the `mergeability = host.get_pr_mergeability(...)` call (line 444):

```python
    # foreman#XXX retarget guard: when handling an impl PR whose base
    # still points at the spec branch AND the spec PR has merged, the
    # impl PR must be retargeted to the default branch before merge.
    # Without this, the squash commit lands on the about-to-be-deleted
    # spec branch — an orphan commit unreachable from main. Mirrors
    # the v1 daemon_runners.merge_impl_pr retarget step (issue #62);
    # the v3 reconciler migration did not port this guard.
    #
    # Conditional on two checks:
    # 1. impl PR's current base IS the spec branch — skip if already
    #    retargeted (idempotency under crash re-enqueue).
    # 2. the spec PR has merged — skip if the spec is still pending,
    #    since retargeting to main and merging would land impl changes
    #    that depend on un-landed spec changes.
    if target == "impl":
        spec_branch_name = spec_branch(ctx.issue.number)
        current_base = host.get_pr_base_ref(
            owner=ctx.snapshot.owner,
            repo=ctx.snapshot.repo,
            pr_number=ctx.pr.number,
        )
        if current_base == spec_branch_name and host.is_pr_merged_for_branch(
            owner=ctx.snapshot.owner,
            repo=ctx.snapshot.repo,
            branch=spec_branch_name,
        ):
            default_branch = host.get_default_branch(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
            )
            host.retarget_pr_base(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                pr_number=ctx.pr.number,
                new_base=default_branch,
            )
            logger.info(
                "attempt_merge_impl: retargeted PR %s/%s#%d base %s → %s "
                "(spec PR for issue #%d has merged; preventing orphan-on-spec-branch)",
                ctx.snapshot.owner,
                ctx.snapshot.repo,
                ctx.pr.number,
                spec_branch_name,
                default_branch,
                ctx.issue.number,
            )
```

**Important:** the retarget happens BEFORE `host.get_pr_mergeability(...)` because retargeting changes GitHub's `mergeStateStatus` computation. Reading mergeability after the retarget gives the correct state for the next branch of the state machine.

**Site 4 — `packages/foreman/src/foreman/reconciler/rules.py:557-570` — strengthen `_impl_pr_merged_label_lagging` (belt-and-suspenders defense):**

Currently the rule advances to `foreman:done` on `ctx.pr.is_merged`. Add a merge-target verification so even if the retarget guard at Site 3 ever fails, the loop refuses to label a ticket done with orphaned content:

```python
# (After the existing is_merged check, before returning True)
# foreman#XXX defense-in-depth: even if attempt_merge_impl's retarget
# guard fires correctly, we double-check here that the impl PR's
# merge commit is reachable from the default branch before marking
# the issue done. Catches: (a) any future regression of the retarget
# guard, (b) any direct-API merge that bypasses the guard.
if not host.is_commit_in_default_branch(
    owner=ctx.snapshot.owner,
    repo=ctx.snapshot.repo,
    commit_oid=ctx.pr.merge_commit_oid,
):
    return False  # orphan; will surface via different rule path
return True
```

This requires a fifth host method, `is_commit_in_default_branch`, added at the same three sites (Protocol declaration, V3GitHubHost delegation, daemon_host.py implementation — the v1/v2 host doesn't currently have this method, so a real implementation is needed there). **Workable but expanding scope.** Worker call: implement Site 4 only if the dogfood test at step 5 passes cleanly without it; otherwise defer to a follow-up PR. The retarget guard at Site 3 is the primary fix; Site 4 is defense-in-depth.

**Site 5 — `packages/foreman/tests/reconciler/test_reconciler_e2e.py:28` — update `_StubHost` fake:**

Add four (or five, if Site 4 lands) stub methods to `_StubHost` so existing tests still satisfy the extended Protocol. Default behavior: return values that DON'T trigger the retarget (current_base != spec_branch, is_pr_merged_for_branch returns False) so existing tests' merge paths don't change. New test (Q3) drives the retarget path explicitly.

Same treatment for `tests/test_roles_worker.py:370:_StubHost` if and only if the worker test exercises `_handle_attempt_merge` (probably doesn't — worker tests focus on the role, not the reconciler's merge handler — but worth a 30-second check).

**Site 6 — `packages/foreman/tests/reconciler/test_reconciler_e2e.py` — add the Q3 regression test:**

Two new tests, both targeting the retarget path. Concrete shape, mostly setup boilerplate:

```python
def test_attempt_merge_impl_retargets_base_to_main_when_spec_merged():
    """foreman#XXX: impl PR's base must be retargeted to main after
    spec PR merges, otherwise the squash commit orphans on the
    about-to-be-deleted spec branch."""
    host = _StubHost()
    host.set_pr_base_ref("o", "r", 200, "foreman/issue-100")  # impl on spec branch
    host.set_spec_pr_merged("o", "r", "foreman/issue-100", True)
    host.set_default_branch("o", "r", "main")
    host.set_pr_mergeability("o", "r", 200, state="CLEAN")
    ctx = make_action_context(issue=100, pr_number=200, snapshot_owner="o", snapshot_repo="r")

    _handle_attempt_merge(ctx, host, target="impl")

    assert host.retarget_calls == [
        {"owner": "o", "repo": "r", "pr_number": 200, "new_base": "main"}
    ], "Impl PR base must be retargeted to default branch before merge"
    assert host.merge_calls == [
        {"owner": "o", "repo": "r", "pr_number": 200, "mechanism": ...}
    ], "Merge must happen after retarget"

def test_attempt_merge_impl_skips_retarget_when_already_on_main():
    """Idempotency: if impl PR was already retargeted (e.g., a previous
    handler invocation succeeded but the dispatcher re-enqueued), the
    retarget step must skip — we read current_base each tick."""
    host = _StubHost()
    host.set_pr_base_ref("o", "r", 200, "main")  # already retargeted
    host.set_default_branch("o", "r", "main")
    host.set_pr_mergeability("o", "r", 200, state="CLEAN")
    ctx = make_action_context(issue=100, pr_number=200, snapshot_owner="o", snapshot_repo="r")

    _handle_attempt_merge(ctx, host, target="impl")

    assert host.retarget_calls == [], "Idempotent: no retarget when base is already default"

def test_attempt_merge_impl_skips_retarget_when_spec_pr_unmerged():
    """Safety: do NOT retarget the impl PR if the spec PR has not
    merged yet — merging impl-on-main with unlanded spec dependencies
    would corrupt main."""
    host = _StubHost()
    host.set_pr_base_ref("o", "r", 200, "foreman/issue-100")  # impl on spec branch
    host.set_spec_pr_merged("o", "r", "foreman/issue-100", False)  # spec PR NOT merged
    ctx = make_action_context(issue=100, pr_number=200, snapshot_owner="o", snapshot_repo="r")

    _handle_attempt_merge(ctx, host, target="impl")

    assert host.retarget_calls == [], "Safety: no retarget if spec PR unmerged"
    # The merge attempt should also be skipped or surface needs-help —
    # impl PR on a still-open spec branch cannot CLEAN merge.
```

The first test is the **single most important regression guard.** It directly tests the scenario that produced 15+ orphan PRs. If this test ever fails on a future change, that change is reintroducing the orphan bug.

##### Scope-of-D9 summary for the Worker

Touching 4 files, 6 named code locations:
- `reconciler/host.py` — Protocol additions (Site 1)
- `reconciler/v3_host.py` — delegation wrappers (Site 2)
- `reconciler/actions.py` — import + retarget guard in `_handle_attempt_merge` (Site 3)
- `reconciler/rules.py` — defense-in-depth check (Site 4, conditional)
- `tests/reconciler/test_reconciler_e2e.py` — stub extensions (Site 5) + new tests (Site 6)
- Optional: `tests/test_roles_worker.py:_StubHost` if it tests the merge handler

Estimated effort: 2-4 hours of focused work (mostly mechanical given the spec). Should fit one TDD Worker session.

#### D1 spec sketch — Labels StrEnum module + classification taxonomy (2026-06-11 21:40 PM)

Same lift-and-paste pattern as D9. Decision 1 specified the shape (follow foreman#258 `Outcome` pattern); this sketch grounds it in the canonical 19-label catalogue currently in `init.py:_FOREMAN_LABELS` and identifies the 9 consumer files (skipping the 3 v2-dead-island files that D3 will excise).

##### The canonical 19-label catalogue (read from `init.py:_FOREMAN_LABELS`)

| Label | Bucket | Existing color | Description |
|---|---|---|---|
| `foreman:plan` | QUEUE | green | queued for planning |
| `foreman:plan-approved` | QUEUE | green | spec approved, queued for Worker |
| `foreman:impl-review` | QUEUE | yellow | impl PR ready for Reviewer |
| `foreman:impl-approved` | QUEUE | green | impl approved, queued for merge |
| `foreman:planning` | IN_FLIGHT | yellow | spec phase running |
| `foreman:merging-plan` | IN_FLIGHT | yellow | attempting to merge spec PR |
| `foreman:merging-impl` | IN_FLIGHT | yellow | attempting to merge impl PR |
| `foreman:hold` | BLOCKING | blue | manual pause (blocks all rules) |
| `foreman:needs-help` | BLOCKING | yellow | surfaced for human intervention |
| `foreman:spec-fix` | BLOCKING | red | spec PR needs human follow-up |
| `foreman:impl-fix` | BLOCKING | red | impl PR needs Fixer follow-up |
| `foreman:impl-attempt-1` | COUNTER | blue | impl cycle attempt 1 of 3 |
| `foreman:impl-attempt-2` | COUNTER | blue | impl cycle attempt 2 of 3 |
| `foreman:impl-attempt-3` | COUNTER | blue | impl cycle attempt 3 of 3 |
| `foreman:fix-attempt-1` | COUNTER | blue | fix cycle attempt 1 of 3 |
| `foreman:fix-attempt-2` | COUNTER | blue | fix cycle attempt 2 of 3 |
| `foreman:fix-attempt-3` | COUNTER | blue | fix cycle attempt 3 of 3 |
| `foreman:done` | TERMINAL | purple | ticket complete |
| `foreman:failed` | TERMINAL | dark-red | ticket exhausted retries |

Bucket rationale (these are the ones consumers actually need to query, not just visual groupings):
- **QUEUE** — "this ticket is queued for role X." `next_action` reads these to dispatch.
- **IN_FLIGHT** — "role X is running." Reconciler observes to enforce "one role at a time per ticket."
- **BLOCKING** — pauses the loop. Currently maintained as `_BLOCKING_LABELS` tuple in `rules.py`. Migrating to enum-classification eliminates the manual sync.
- **COUNTER** — attempt-N markers. Stats/retry-limit code currently regex-matches; with enum membership it becomes set-membership.
- **TERMINAL** — `foreman:done` clears the ticket from active processing; `foreman:failed` clears + marks abandoned.

##### Site 1 — Create `packages/foreman/src/foreman/labels.py` (NEW FILE):

Mirrors `reconciler/outcomes.py` structurally — `LabelClass` enum + `Label` StrEnum with custom `__new__` requiring classification:

```python
"""Typed catalog of every ``foreman:*`` label written to GitHub.

foreman#194 / foreman#XXX (D1 resurrection): "If we forget to update
_BLOCKING_LABELS when adding a new label, the rate limiter silently
treats the new state as non-blocking. The StrEnum machinery makes
forgetting the classification impossible — Python raises TypeError
at module load before any test runs."

This module is the single source of truth for foreman label strings.
The :class:`Label` StrEnum is a ``StrEnum`` so members compare equal
to their string values — GitHub API calls receive the value verbatim,
f-string interpolation produces the bare string, and SQL bind sites
work without ``.value``.

Each member carries its own :class:`LabelClass` classification at the
point of definition; a contributor who adds a new member as a bare
string (``NEW = "new"`` instead of ``NEW = ("new", LabelClass.QUEUE)``)
trips a ``TypeError`` at module load — Python's enum machinery raises
before any test gets a chance to run. The "forgot to update
_BLOCKING_LABELS" failure mode that bit us on foreman#194's
original (orphaned) implementation is now structurally impossible.

The five derived frozensets (:data:`QUEUE_LABELS`,
:data:`IN_FLIGHT_LABELS`, :data:`BLOCKING_LABELS`,
:data:`COUNTER_LABELS`, :data:`TERMINAL_LABELS`) are computed at
module load by filtering the enum on the ``classification`` attribute.

Adding a new label:

1. Add one new line to :class:`Label`:
   ``NEW = ("new", LabelClass.<bucket>)``.
2. Mirror the entry into :data:`_FOREMAN_LABELS` in ``init.py`` with
   color + description (init still owns CREATION metadata).
3. That's it. Both the enum membership AND the derived frozenset are
   updated; no separate constant to keep in sync.
"""

from __future__ import annotations

from enum import Enum, StrEnum


class LabelClass(Enum):
    """The five classification buckets a :class:`Label` can belong to.

    QUEUE: ticket is queued for role X to act on. ``next_action`` reads
    these to dispatch.

    IN_FLIGHT: a role is currently running. Reconciler observes to
    enforce "one role at a time per ticket."

    BLOCKING: pauses the loop. Includes manual holds (``foreman:hold``),
    surfacings to human (``foreman:needs-help``), and fix-needed
    states (``foreman:spec-fix``, ``foreman:impl-fix``). Was
    ``_BLOCKING_LABELS`` tuple in rules.py pre-foreman#XXX.

    COUNTER: attempt-N markers. Stats/retry-limit code uses these to
    count cycles. Membership check replaces regex-matching against
    label names.

    TERMINAL: ``foreman:done`` clears the ticket from active
    processing; ``foreman:failed`` clears + marks abandoned after
    exhausting retries.
    """

    QUEUE = "queue"
    IN_FLIGHT = "in_flight"
    BLOCKING = "blocking"
    COUNTER = "counter"
    TERMINAL = "terminal"


class Label(StrEnum):
    """Every ``foreman:*`` label string written to a GitHub issue or PR.

    The enum is a ``StrEnum`` so members compare equal to their string
    values — PyGithub's ``add_to_labels`` and ``remove_from_labels``
    receive the value verbatim, and f-string interpolation produces
    the bare string. Use ``.value`` explicitly when interpolating into
    SQL DDL (where the result is parsed by SQLite without going
    through the binder) so the format cannot accidentally produce
    ``"Label.PLAN"`` instead of ``"foreman:plan"``.

    Each member's right-hand side is a tuple of
    ``(value: str, classification: LabelClass)``. The custom
    :meth:`__new__` requires the classification — omitting it raises
    a ``TypeError`` at class-creation time, before module load
    completes.

    Adding a new label is one line below. Forgetting the
    classification is impossible.
    """

    classification: LabelClass

    def __new__(cls, value: str, classification: LabelClass) -> Label:
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.classification = classification
        return obj

    # QUEUE — ticket queued for role X
    PLAN = ("foreman:plan", LabelClass.QUEUE)
    PLAN_APPROVED = ("foreman:plan-approved", LabelClass.QUEUE)
    IMPL_REVIEW = ("foreman:impl-review", LabelClass.QUEUE)
    IMPL_APPROVED = ("foreman:impl-approved", LabelClass.QUEUE)

    # IN_FLIGHT — role currently running on this ticket
    PLANNING = ("foreman:planning", LabelClass.IN_FLIGHT)
    MERGING_PLAN = ("foreman:merging-plan", LabelClass.IN_FLIGHT)
    MERGING_IMPL = ("foreman:merging-impl", LabelClass.IN_FLIGHT)

    # BLOCKING — pauses the loop
    HOLD = ("foreman:hold", LabelClass.BLOCKING)
    NEEDS_HELP = ("foreman:needs-help", LabelClass.BLOCKING)
    SPEC_FIX = ("foreman:spec-fix", LabelClass.BLOCKING)
    IMPL_FIX = ("foreman:impl-fix", LabelClass.BLOCKING)

    # COUNTER — attempt markers
    IMPL_ATTEMPT_1 = ("foreman:impl-attempt-1", LabelClass.COUNTER)
    IMPL_ATTEMPT_2 = ("foreman:impl-attempt-2", LabelClass.COUNTER)
    IMPL_ATTEMPT_3 = ("foreman:impl-attempt-3", LabelClass.COUNTER)
    FIX_ATTEMPT_1 = ("foreman:fix-attempt-1", LabelClass.COUNTER)
    FIX_ATTEMPT_2 = ("foreman:fix-attempt-2", LabelClass.COUNTER)
    FIX_ATTEMPT_3 = ("foreman:fix-attempt-3", LabelClass.COUNTER)

    # TERMINAL — final state
    DONE = ("foreman:done", LabelClass.TERMINAL)
    FAILED = ("foreman:failed", LabelClass.TERMINAL)


QUEUE_LABELS: frozenset[str] = frozenset(
    m.value for m in Label if m.classification is LabelClass.QUEUE
)
"""String values whose classification is :attr:`LabelClass.QUEUE`."""

IN_FLIGHT_LABELS: frozenset[str] = frozenset(
    m.value for m in Label if m.classification is LabelClass.IN_FLIGHT
)
"""String values whose classification is :attr:`LabelClass.IN_FLIGHT`."""

BLOCKING_LABELS: frozenset[str] = frozenset(
    m.value for m in Label if m.classification is LabelClass.BLOCKING
)
"""String values whose classification is :attr:`LabelClass.BLOCKING`.

Replaces the hand-maintained ``_BLOCKING_LABELS`` tuple in
``reconciler/rules.py``. Any rate-limit / blocked-state check reads
this directly.
"""

COUNTER_LABELS: frozenset[str] = frozenset(
    m.value for m in Label if m.classification is LabelClass.COUNTER
)
"""String values whose classification is :attr:`LabelClass.COUNTER`."""

TERMINAL_LABELS: frozenset[str] = frozenset(
    m.value for m in Label if m.classification is LabelClass.TERMINAL
)
"""String values whose classification is :attr:`LabelClass.TERMINAL`."""


__all__ = [
    "BLOCKING_LABELS",
    "COUNTER_LABELS",
    "IN_FLIGHT_LABELS",
    "Label",
    "LabelClass",
    "QUEUE_LABELS",
    "TERMINAL_LABELS",
]
```

##### Site 2 — Update `init.py` to consume `Label` instead of hardcoded strings:

`_FOREMAN_LABELS` stays (it's the creation-metadata catalogue — color, description), but the label name in each tuple becomes `Label.PLAN`, `Label.PLANNING`, etc. instead of the hardcoded string. Same data shape, just the name source-of-truth flips. Example diff:

```python
# BEFORE
_FOREMAN_LABELS: list[tuple[str, str, str]] = [
    ("foreman:plan", "0E8A16", "Foreman: queue for planning ..."),
    ...
]

# AFTER
from foreman.labels import Label
_FOREMAN_LABELS: list[tuple[Label, str, str]] = [
    (Label.PLAN, "0E8A16", "Foreman: queue for planning ..."),
    ...
]
```

Add a regression test that asserts every `Label.*` member appears exactly once in `_FOREMAN_LABELS`, and that `_FOREMAN_LABELS` introduces no labels not in `Label.*`. Closes the "init catalogue + Label enum drift" failure mode.

##### Site 3 — Migrate 9 consumer files to import from `foreman.labels`:

In-scope (per Grep — files with `"foreman:..."` string literals):
- `packages/foreman/src/foreman/roles/fixer.py`
- `packages/foreman/src/foreman/roles/reviewer.py`
- `packages/foreman/src/foreman/roles/worker.py`
- `packages/foreman/src/foreman/roles/__init__.py`
- `packages/foreman/src/foreman/reconciler/rules.py`
- `packages/foreman/src/foreman/reconciler/actions.py`
- `packages/foreman/src/foreman/reconciler/observer.py`
- `packages/foreman/src/foreman/reconciler/daemon.py`
- `packages/foreman/src/foreman/init.py` (Site 2)

**Out of scope (v2 dead island — leave alone; D3 will delete):**
- `packages/foreman/src/foreman/dispatcher.py`
- `packages/foreman/src/foreman/daemon.py` (top-level v2 module, not reconciler/daemon.py)
- `packages/foreman/src/foreman/daemon_runners.py`

This is a deliberate non-port: migrating dead modules to a new pattern just to delete them next sprint is wasted work. Explicit reference back to the D3 dead-island finding so a future operator doesn't wonder why these three files weren't touched.

Migration pattern per consumer file: replace hardcoded `"foreman:plan"` with `Label.PLAN` (etc.). The hardcoded string and the enum member compare equal (StrEnum), so equality checks against ticket label sets still work. The most-important transformation is in `rules.py`, where the hand-maintained `_BLOCKING_LABELS` tuple gets replaced:

```python
# BEFORE — rules.py
_BLOCKING_LABELS: tuple[str, ...] = (
    "foreman:hold",
    "foreman:needs-help",
    "foreman:spec-fix",
    "foreman:impl-fix",
)

# AFTER
from foreman.labels import BLOCKING_LABELS  # frozenset[str], populated by enum
# (delete the local _BLOCKING_LABELS tuple)
```

##### Site 4 — Regression tests at `packages/foreman/tests/test_labels.py` (NEW FILE):

Mirror `tests/reconciler/test_outcomes.py` (the foreman#258 reference test that pins the Outcome enum design — same shape applies here):

```python
"""Pin the foreman.labels Label StrEnum design — every member carries
its classification at the point of definition; the derived frozensets
match the membership; adding a label without classification is a
TypeError at module load."""

import pytest
from foreman.labels import (
    BLOCKING_LABELS, COUNTER_LABELS, IN_FLIGHT_LABELS,
    Label, LabelClass, QUEUE_LABELS, TERMINAL_LABELS,
)

def test_every_label_has_a_classification():
    """Forgetting classification is structurally impossible —
    Python's enum machinery raises TypeError at module load. This
    test pins that the discipline is in place, so a future refactor
    that 'simplifies' the enum loses the structural guarantee
    loudly."""
    for member in Label:
        assert isinstance(member.classification, LabelClass), \
            f"{member.name} missing classification"

def test_derived_frozensets_match_enum_membership():
    """Every label in the catalogue appears in exactly ONE derived
    frozenset (the one matching its classification). No label is in
    two sets; no label is in zero sets."""
    all_labels = {m.value for m in Label}
    union = QUEUE_LABELS | IN_FLIGHT_LABELS | BLOCKING_LABELS | COUNTER_LABELS | TERMINAL_LABELS
    assert union == all_labels, \
        f"Labels in enum but not in any frozenset: {all_labels - union}; " \
        f"in frozenset but not enum: {union - all_labels}"
    # Pairwise disjoint
    sets = [QUEUE_LABELS, IN_FLIGHT_LABELS, BLOCKING_LABELS, COUNTER_LABELS, TERMINAL_LABELS]
    for i, a in enumerate(sets):
        for b in sets[i+1:]:
            assert a.isdisjoint(b), f"Label appears in two buckets: {a & b}"

def test_blocking_labels_includes_all_four_known_blocking_states():
    """foreman#194 regression: _BLOCKING_LABELS was a hand-maintained
    tuple in rules.py; if someone added a new BLOCKING label without
    updating the tuple, the rate-limiter silently treated the new
    state as non-blocking. This test pins the contract."""
    assert BLOCKING_LABELS == frozenset({
        "foreman:hold",
        "foreman:needs-help",
        "foreman:spec-fix",
        "foreman:impl-fix",
    })

def test_label_strenum_equality_with_string():
    """StrEnum invariant: members compare equal to their string values
    so PyGithub label add/remove calls work transparently."""
    assert Label.PLAN == "foreman:plan"
    assert "foreman:plan" == Label.PLAN
    assert f"{Label.NEEDS_HELP}" == "foreman:needs-help"
```

##### Site 5 — Init-catalogue sync test in `tests/test_init.py`:

```python
def test_init_foreman_labels_matches_label_enum():
    """The init catalogue (_FOREMAN_LABELS, owns color + description)
    and the Label enum (owns name + classification) must enumerate
    the same set. Drift is the failure mode foreman#194 was supposed
    to prevent the first time; this is the durable artifact that
    catches it next time."""
    from foreman.init import _FOREMAN_LABELS
    from foreman.labels import Label
    init_names = {entry[0] for entry in _FOREMAN_LABELS}
    enum_names = {m.value for m in Label}
    assert init_names == enum_names, \
        f"In init but not enum: {init_names - enum_names}; " \
        f"in enum but not init: {enum_names - init_names}"
```

##### Sequencing within Phase 2

D1 can land in parallel with D9 — they touch completely different code surfaces. Recommended order if a single Worker handles both: D9 first (more critical — the loop is broken without it), D1 second (mechanical migration once the loop fix is verified).

Estimated effort: 3-5 hours. The new module + tests is fast (~1 hour with the spec above); the consumer migration is the bulk of the work (~2-3 hours for 9 files, mostly find-and-replace with import-add).

---

## Phase 1 closure (2026-06-11 evening)

**Status: CLOSED.** Nine decisions recorded covering the original 8-topic finding list from Phase 0 plus three emergent decisions (D7 import-linter, D8 library-boundary audit, D9 autonomous-loop bug — the largest finding of the day).

### Decision summary

| # | Topic | Verdict | Layer |
|---|---|---|---|
| 1 | Labels regression resurrection | Restore as `StrEnum` following `Outcome` pattern from foreman#258 | Code structure |
| 2 | Role-runner duplication | `RoleRunner` ABC via strangler-fig, executed back-to-back | Code structure |
| 3 | Dead-code surface | `vulture` + reachability AST-walk tooling (smart Option A) | Tooling |
| 4 | Bandaid-ratio guardrail | Artifact discipline (test + ticket) + prompt-level GoF/Google lens with "or say it doesn't fit" calibration | Process + prompt |
| 5 | Provider package split (`provider` vs `providers`) | Merge into one `foreman.provider` package with sub-modules | Code structure |
| 6 | `daemon_host.py` boilerplate | Constructor injection + verify-and-pin token-TTL invariant as a test (generalizable sub-rule for all constructor-injection refactors) | Code structure + discipline |
| 7 | Import-boundary CI (emergent topic) | `import-linter` with R1/R2/R3 rules tied to D5/D2/D3; rule-set discipline "only when a decision creates the boundary" | Tooling |
| 8 | Library-boundary audit (emergent topic) | Tier-table audit of third-party imports; Tier 1 binds now, Tier 2 binds on second-consumer, Tier 3 explicitly NOT bound with reason | Tooling + amendment to D7 |
| 9 | Impl-PR-base-retarget bug (emergent — the critical finding) | Investigate + fix; orphan-content audit identifies scope; the actual durability mechanism Decision 1 §7 was reaching for | Code (daemon) + audit |

### The four-layer defense framing (D3 + D4 + D6 + D7)

D9 is its own layer because it addresses autonomous-loop machinery failure, not human/Wren judgment failure. The original four-layer defense (bias → pin → sweep → CI) protects against drift in human work; D9 protects against the loop itself silently failing to ship work.

### Topics that dissolved without standalone decisions

- **Topic 7** (`ProviderInvalidResultError` zombie symbol): absorbed into D5 (resolved by the package merge) and cross-referenced from D3 (exemplar of what reachability tooling catches) and D7 (exemplar of what dead-island CI prevents).
- **Topic 8** (Phase 0's 12-item manual dead-code catalogue): absorbed into D3 as the calibration baseline for the first tooling run.

### Phase 2 priority order (recommended)

1. **D9 fix first** — without it, the autonomous loop continues orphaning work. Everything downstream is wasted if D9 isn't first. Includes: investigate the retarget code path (Q2), write the post-merge-target assertion test (Q3), audit + rescue the remaining ~10 orphan PRs (Q1 tail).
2. **D1 + D5** — Labels resurrection and provider package merge land alongside the D9 rescue work; both are referenced by other decisions and unblock them.
3. **D7 framework (import-linter scaffolding)** — independent infrastructure; cheap to land; enables D2/D3/D8 rule-checking.
4. **D3 tooling** — runs the dead-code sweep; produces input for D7 R3 (dead-island rule).
5. **D8 library-boundary audit** — produces Tier 1 rules; these ship as additions to D7.
6. **D2 RoleRunner ABC** — back-to-back strangler-fig across all four roles. Depends on D1 (labels), D6 sub-rule (verify-pin lifecycle invariants), and consumes D5 (provider boundary).
7. **D6 daemon_host constructor injection** — independent of the loop work; can land any time after D6's verify-pin test for token-TTL invariant is written.
8. **D4 process/prompt guardrail** — process layer; lowest urgency; updates `CLAUDE.md` + `prompts/{planner,reviewer}.md` at end of Phase 2.

### Explicit non-decisions (recorded for traceability)

- **Decision NOT taken:** "structural-fix-regression-detection annotation" (the original §7 of D1 as I'd framed it — a comment marker / registry file marking load-bearing fixes). Reason: D9 superseded the framing entirely. The annotation would have been paperwork solving a problem that didn't exist; the real problem was the loop lying about completion.
- **Decision NOT taken:** binding `pydantic`, `pathlib`, `tomllib`, stdlib basics under D7/D8. Recorded in D8's Tier 3 with the phrase "intentionally everywhere" to prevent a future operator from reflexively binding them.

### Open follow-ups not requiring Phase 1 decisions

- The remaining ~10 orphan PRs from D9's audit (#58, #159, #168, #186, #189, #192, #202, #206, #212, #225) need per-PR triage during Phase 2's rescue sprint. Each needs the same check: is the content on main (rescued by later commit) or genuinely still missing?
- The "BEHIND merges + parallel-rebase cycle" smell Jeff named as Hypothesis 2 is real but separate from D9. It is exacerbated by strict-up-to-date branch protection. Worth filing as a Phase 2 followup once D9's fix is in: does the autonomous loop's PR-base-retarget step also handle the stale-rebase-loop cleanly?



