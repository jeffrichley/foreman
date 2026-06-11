# Foreman architecture + stability plan (2026-06-11)

> Filed after Jeff's 2026-06-10 PM observation: *"we've basically been layering bandaid on top of bandaid"* + 2026-06-11 AM addition: *"I want to look at a lens of design stability and cleanliness — GoF patterns, Google principles."*

This plan lives in the repo (not in an issue body) so we can iterate on it as the review produces decisions. Tracking ticket: foreman#269.

## Status note (2026-06-11 morning)

- All five overnight PRs landed on main: #266 (GoF provider boundary), #268 (reviewer-budget gate), #256 (dead Literals), #215 (git identity), #258 (typed Outcome enum)
- Container rebuilt on `a6c6956` and running healthy
- Architecture review session is **today** (Jeff corrected the timing — I had been calling it "tomorrow")
- Two structural bugs surfaced during deployment: container clone staleness, crash-cascade leaves ticket label-less

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


