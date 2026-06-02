# Reviewer-on-impl and Fixer-on-impl prompt routing — design

**Status:** brainstormed 2026-06-02; awaiting Jeff approval before plan.

**Closes** (conceptually, NOT via auto-close keyword): foreman#78, foreman#79.

## Problem

Today's foreman#41 added Reviewer-on-impl support: the daemon's dispatcher
now emits `RUN_REVIEWER_IMPL` and `RUN_FIXER_IMPL` actions, branch
detection (`_parse_review_branch`) accepts both `foreman/issue-N` and
`foreman/impl-N`, and `run_reviewer` / `run_fixer` accept a `target`
kwarg.

What foreman#41 did NOT do: split the prompts or the entry-label gates
per target. The Reviewer and Fixer roles still load a single
target-agnostic prompt file (`reviewer.md`, `fixer.md`) and the Fixer's
precondition check hardcodes `foreman:spec-fix`.

This surfaced as two bugs during foreman#63's autonomous walk
(2026-06-02):

1. **foreman#78** — Reviewer dispatched against impl PR #77 reviewed it
   using the spec-review prompt. It explicitly diagnosed the routing
   issue ("this PR is the Worker's impl PR being routed to the spec
   reviewer") but had no escape, so it flagged the impl-shaped diff as
   critical "wrong shape" scope drift. Outcome: `needs_fix` on a
   clean impl PR.
2. **foreman#79** — Fixer dispatched with `target="impl_pr"` hard-failed
   at the entry-label gate:
   `Issue #63 does not carry the 'foreman:spec-fix' label (labels: foreman:impl-fix)`.
   The daemon then retried every poll cycle, never making progress.

Both are blockers for closing the autonomous loop end-to-end. They
share the same shape (the impl-side variant of a role doesn't have its
own prompt or precondition), so they get the same design.

## Architecture

Three coordinated changes covering both bugs:

### 1. New prompt files

- `packages/foreman/src/foreman/prompts/reviewer_impl.md` — Reviewer
  framed for reviewing IMPL PRs (code + tests) against the merged spec
  doc.
- `packages/foreman/src/foreman/prompts/fixer_impl.md` — Fixer framed
  for fixing IMPL PRs in response to Reviewer-on-impl findings.

Each composed with the SAME vendored superpowers as the spec-side
sibling PLUS two additions:

- `superpowers/verification-before-completion.md` — already vendored
  for Worker; what "complete" means when the artifact is code.
- `superpowers/test-driven-development.md` — already vendored for
  Worker; test-first discipline for impl edits.

No new superpowers vendoring; we're adding existing files to two new
role compositions. The composer infrastructure (adapter preamble +
per-skill wrappers from PR #43) already handles arbitrary skill lists.

### 2. Loader extension

`packages/foreman/src/foreman/prompts/__init__.py` —
`load_role_prompt` gains an optional `target` kwarg:

```python
def load_role_prompt(role: str, *, target: str | None = None) -> str:
    """Read the Foreman-specific role prompt by role name.

    When ``target == "impl_pr"`` AND a target-specific prompt file
    exists at ``<role>_impl.md``, return that. Otherwise fall back
    to the canonical ``<role>.md``.

    The mapping from ``(role, target)`` to filename is a fact about
    the prompt directory layout — it lives in the loader, not in
    each role's call site. Roles that don't have a target-specific
    variant (Planner, Worker today) automatically fall back; no
    branching at the role layer.
    """
    if target == "impl_pr":
        candidate = f"{role}_impl"
        if (
            resources.files(_PROMPTS_ROOT)
            .joinpath(f"{candidate}.md")
            .is_file()
        ):
            return _read(candidate)
    return _read(role)
```

The loader stays a path resolver; it does not know about role
semantics. The convention "impl-side prompts are named
`<role>_impl.md`" lives in exactly one place.

### 3. Role precondition gate + prompt selection

In `roles/reviewer.py` and `roles/fixer.py`, the existing `target`
kwarg drives both prompt loading and the entry-label gate:

```python
_REVIEWER_ENTRY_LABEL_BY_TARGET = {
    "spec_pr": "foreman:spec-review",
    "impl_pr": "foreman:impl-review",
}

_FIXER_ENTRY_LABEL_BY_TARGET = {
    "spec_pr": "foreman:spec-fix",
    "impl_pr": "foreman:impl-fix",
}

# Inside run_fixer (similar in run_reviewer):
expected_label = _FIXER_ENTRY_LABEL_BY_TARGET[target]
if expected_label not in ticket.labels:
    raise RuntimeError(
        f"Issue #{issue_number} does not carry the {expected_label!r} "
        f"label (labels: {','.join(ticket.labels)}). The Fixer only "
        f"acts on issues queued by the Reviewer for target={target}."
    )

prompt = load_role_prompt("fixer", target=target)
```

`target` becomes the single authority for "what kind of work am I
doing right now." Prompt selection and precondition check agree on
that authority by construction.

### Data flow

```
daemon (RUN_REVIEWER_IMPL action)
  → daemon_runners.run_reviewer(ticket, config, target="impl_pr")
    → roles/reviewer.py:run_reviewer(target="impl_pr")
      → check entry label is "foreman:impl-review"
      → load_role_prompt("reviewer", target="impl_pr")
        → reviewer_impl.md
      → compose with [requesting-code-review, verification-before-completion, test-driven-development]
      → provider.run_agent(system_prompt=composed, ...)
```

Same shape for Fixer. The path is mechanical; the policy lives in
the loader's one-line convention + the role's target-driven gates.

## Prompt content

The two new files retain the role-identity preamble and structured-
output schema from their spec-side siblings but reframe the
domain-specific guidance.

### reviewer_impl.md

- **Artifact being reviewed:** code, tests, and prompt/config files —
  NOT a spec doc. PR diff with `.py`/`.md`/`test_*.py` files is the
  EXPECTED shape, not a violation.
- **Reference for correctness:** the merged spec doc on `main` at
  `docs/superpowers/specs/foreman-issue-<N>-spec.md`. Read it first;
  that's the contract this PR must satisfy.
- **Acceptance gates:**
  - Each spec sub-request maps to a file change in the diff
  - Acceptance criteria from the spec are testable AND tested
  - No new test failures vs baseline (Worker stats:
    `baseline_failures_count == 0`, `new_failures_count == 0`)
  - PR title matches conventional commit + scope reflects the impl
  - No scope drift — files outside the spec's "File-level changes"
    section are flagged
- **What NOT to flag:** anything that's RIGHT for an impl PR (multiple
  source files, conventional `fix(scope):` title, "Implements #N"
  body, branch `foreman/impl-N`). The current spec-prompt's
  wrong-shape errors come from these false positives. The new prompt
  explicitly names them as valid.
- **Composition:** `requesting-code-review` +
  `verification-before-completion` + `test-driven-development`.

### fixer_impl.md

- **Artifact being fixed:** code, not spec. Fixes are source edits,
  possibly with new/changed tests.
- **Reference for what to fix:** the Reviewer-on-impl's structured
  findings JSON, already standardized at
  `<!-- foreman:findings:begin -->...<!-- foreman:findings:end -->`
  per foreman#41.
- **Discipline:**
  - For each finding, if the fix changes behavior: write a failing
    test first (TDD), then fix, then verify the test passes
  - NEVER delete or weaken tests to make CI pass — cardinal sin
  - Preserve scope: address what the Reviewer flagged, nothing more
    (no drive-by refactors)
  - Verification: run the full check after fixes; commit only when
    green
- **Failure mode handling:** if a finding can't be addressed (genuine
  ambiguity, finding looks wrong), surface in structured output rather
  than silently skipping
- **Composition:** `receiving-code-review` +
  `verification-before-completion` + `test-driven-development`.

The "what's different from spec-side" is the framing, the reference
target, and the test-discipline emphasis. Same role identity,
same authority, different domain.

## Test plan

Four layers, no LLM mocking required. We test the wire-up; we
validate prompt content via the autonomous dogfood at the end.

### Layer 1: Loader unit tests (`tests/test_prompts.py`)

Pin the convention:

- `load_role_prompt("reviewer", target="impl_pr")` returns
  `reviewer_impl.md` content.
- `load_role_prompt("reviewer", target="spec_pr")` returns
  `reviewer.md` content (same as `target=None`).
- `load_role_prompt("planner", target="impl_pr")` falls back to
  `planner.md` (graceful for roles without impl variants).
- `load_role_prompt("reviewer", target=None)` returns `reviewer.md`
  (back-compat for call sites that don't yet pass target).
- Parametrized signature test extends to pin one identifying line in
  each new impl prompt so refresh drift surfaces.

### Layer 2: Role unit tests

In `tests/test_roles_reviewer.py` and `tests/test_roles_fixer.py`,
pin the precondition gate + prompt selection:

- `run_reviewer(target="impl_pr")` with the wrong label
  (`foreman:spec-review`) raises `RuntimeError` mentioning
  `foreman:impl-review`.
- Symmetric: `run_reviewer(target="spec_pr")` with the wrong label
  (`foreman:impl-review`) raises mentioning `foreman:spec-review`.
- Same parametrization for Fixer: `target="impl_pr"` expects
  `foreman:impl-fix`, `target="spec_pr"` expects `foreman:spec-fix`.
- One happy-path test per role per target: with the correct label,
  runs through to the SDK call (mocked) and the mock receives the
  impl-side composed prompt. Assertion: composed prompt body contains
  the signature lines of `"requesting-code-review"` AND
  `"verification-before-completion"` AND `"test-driven-development"`
  for Reviewer-on-impl. (Symmetric for Fixer.)

### Layer 3: Integration tests (`tests/test_daemon_runners.py`)

Pin the wiring through the daemon:

- `RealRoleDispatcher.dispatch(action=RUN_REVIEWER_IMPL)` calls
  `run_reviewer(target="impl_pr")` — existing test infrastructure from
  foreman#41 already covers `target=` plumbing; verify it routes
  correctly with the new prompt machinery.
- Same for `RUN_FIXER_IMPL` → `run_fixer(target="impl_pr")`.

### Layer 4: End-to-end validation (the proof)

After both fixes are on `main`:

1. Restart the daemon (so it picks up the new code).
2. Label a small fresh ticket with `foreman:plan`.
3. Watch the daemon run the full loop autonomously:
   Planner → Reviewer-on-spec → merge_spec → Worker →
   **Reviewer-on-impl** (now correct prompt) → **Fixer-on-impl** if
   findings (now correct label gate + prompt) → merge_impl →
   close_issue.
4. Success criteria:
   - Issue closes autonomously without manual intervention.
   - No `RuntimeError` in daemon log.
   - No false-positive "wrong shape" findings.

What we are NOT testing:

- LLM behavior on the new prompt content. Prompt quality is validated
  by the dogfood run, not by unit tests.
- The vendored superpowers' content itself (already pinned via
  signature tests).

## Rollout

**One PR** covering both foreman#78 and foreman#79. The loader
extension is shared infrastructure; the role-side changes are
symmetric and only validate together. Two PRs would leave one half
broken between merges.

PR body explicitly says "Implements #78 and #79" — NOT `Closes #N` or
any GitHub auto-close keyword (we just shipped foreman#63 to forbid
those). Issue closure happens manually after the merge with
audit-trail comments.

**Hand-implement, NOT Foreman-driven.** Dogfooding the implementation
of these fixes through the currently-broken Foreman pipeline would be
silly. We brainstormed (this doc), we'll hand-code, we'll hand-merge.
Foreman's job is to validate the result by autonomously closing the
NEXT dogfood ticket end-to-end after the fixes land.

### Task order within the PR (per writing-plans output)

Anticipated decomposition (final breakdown comes from the plan):

1. Loader extension + tests (`prompts/__init__.py` + `test_prompts.py`).
2. New `reviewer_impl.md` (write content per "Prompt content" above).
3. New `fixer_impl.md` (write content per "Prompt content" above).
4. `roles/reviewer.py`: precondition gate via
   `_REVIEWER_ENTRY_LABEL_BY_TARGET` + `load_role_prompt(..., target=...)`
   + tests.
5. `roles/fixer.py`: same shape — gate via
   `_FIXER_ENTRY_LABEL_BY_TARGET` + prompt selection + tests.

### Branch / merge mechanics

- Single feature branch off `main` (e.g.
  `fix/reviewer-fixer-impl-prompts`).
- Squash-merge to `main` when CI green.
- After merge: manually close foreman#78 and foreman#79 with
  cross-references to the PR.

## Out of scope

- **Separate GitHub Apps per target** (e.g., distinct
  `foreman-reviewer-spec` and `foreman-reviewer-impl` identities).
  Shared App, two prompts is the cleaner v1; revisit when per-App
  permission tightening becomes useful (pairs with foreman#59).
- **Planner / Worker target-specific prompts.** Today only Reviewer
  and Fixer have meaningful target distinction. The loader's
  fallback-to-canonical behavior handles target-less roles cleanly;
  no work needed on Planner / Worker side.
- **Renaming `<role>.md` to `<role>_spec.md` for explicit symmetry.**
  Tempting (symmetric naming) but breaks every existing reference and
  every audit-log row. Keep the canonical `<role>.md` as the
  spec-default; impl is the variant.
- **Vendoring new superpowers** (foreman#80 `systematic-debugging`,
  foreman#81 `dispatching-parallel-agents`). Filed separately; not on
  the critical path for tonight.
- **Reviewer/Fixer prompt content beyond the impl-review framing.**
  Iterate from real dogfood data after this lands.

## References

- foreman#78 — Reviewer-on-impl uses spec prompt.
- foreman#79 — Fixer rejects `foreman:impl-fix` label.
- foreman#41 — added Reviewer-on-impl support (this design completes
  the work).
- foreman#63 — Planner `Closes #N` anti-pattern (already shipped;
  spec PR convention this design respects).
- foreman#80, foreman#81 — adjacent vendoring tickets, deferred.
- foreman#59 — per-role App permissions, future identity-separation
  hardening.
