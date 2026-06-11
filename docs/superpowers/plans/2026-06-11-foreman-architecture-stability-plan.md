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
