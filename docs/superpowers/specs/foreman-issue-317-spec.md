# foreman#317 — Granular merge-state routing (CI-failed → ImplFix, dirty → resolve, behind → update-branch)

**Status:** design approved 2026-07-17 (Jeff). Ready for an implementation plan.
**Foundation:** `docs/reference/github-merge-ci-state-semantics.md` (the cited, authoritative
GitHub merge-state + CI-check reference this design rests on).

## Problem

`MergingState` polls a PR until it can merge. Today `attempt_merge` collapses every
not-yet-mergeable state into `OutcomeKind.BLOCKED`, and `MergingState.next_state` maps
`BLOCKED` to a self-loop (re-poll next tick). So a PR whose **CI has failed** — a genuine,
terminal condition — polls `BLOCKED` **forever**: it is never routed to `ImplFix` to fix the
failure, and `BLOCKED` is retry-cap-exempt, so nothing escalates it either. This is finding
**C1** of the 2026-07-17 self-heal architecture review, and the root of the "everything stuck"
incidents. `states/merging.py` even carries a comment deferring "CI failed → ImplFix, dirty →
ImplFix, blocked by review" to this issue.

The root cause is signal imprecision: `mergeable_state == "blocked"` is GitHub's *generic*
"the merge is blocked" — it cannot, by itself, distinguish **CI-failed** (route to ImplFix)
from **CI-pending** (legitimately wait). See reference §9, ambiguity #1–2.

## Enabling constraints (already satisfied — verified 2026-07-17)

Two repo-config invariants collapse five of the nine ambiguities. Both are **already
configured** on every foreman-managed repo (verified via the live rulesets):

- **C-CI — every managed repo has CI.** ⇒ `blocked` + zero check-runs is unambiguously "CI
  hasn't registered yet" = *pending*, never "no CI → merge." No no-CI branch to design.
- **C-STRICT — every managed repo requires "branches up to date before merging"** (strict
  required status checks; foreman `main-gate` and agent_core `phase1-main-gate` both have
  `strict_required_status_checks_policy = true`). ⇒ staleness **always** surfaces as `behind`
  *first* and must be freshened before merge. So a `blocked`+`failure` can only occur when the
  PR is **not** behind = the base is current = **the failure is genuine**. C-STRICT is what
  makes "failure → ImplFix" a structural guarantee rather than a fragile base-SHA comparison
  (reference §7). This kills ambiguities #3 and #4 (stale-base, stale-clean).

A plan that changes these repos MUST preserve both invariants (loud-fail if a managed repo
lacks strict required checks, rather than silently routing on ambiguous signals).

## The ground-truth signal

`mergeable_state` is a rolled-up verdict; `blocked` never says *why*. To disambiguate,
read the **check-runs on the PR head SHA** (GitHub Actions reports via check-runs, NOT the
legacy Statuses API — reference §5; use check-runs or `statusCheckRollup.contexts`, never
combined-status alone).

Add a provider capability that returns, for a PR, the classified state of its **required**
checks (required = the repo's ruleset `required_status_checks` contexts):

- `PENDING` — at least one required check has `status ∈ {queued, in_progress, waiting,
  requested, pending}` (no conclusion yet).
- `FAILED` — at least one required check `conclusion ∈ {failure, startup_failure}` (and none
  pending). Carries which context(s) failed.
- `TIMED_OUT_OR_CANCELLED` — a required check `conclusion ∈ {timed_out, cancelled}`.
- `ACTION_REQUIRED` — a required check `conclusion == action_required`.
- `PASSED` — all required checks `conclusion ∈ {success, neutral, skipped}`.

Design notes:
- New provider method on the `GitHostProvider` protocol (e.g.
  `required_check_state(project, pr_number) -> RequiredCheckState`), backed by PyGithub
  `commit.get_check_runs()` on the PR head SHA, filtered to the ruleset's required contexts.
- **The fake provider MUST mirror the real one strictly** (refuse shapes the real lib refuses)
  per the test-fakes rule — a fake that always returns PASSED would hide every routing bug.
- Precedence when multiple states co-occur: `PENDING` > `ACTION_REQUIRED` >
  `TIMED_OUT_OR_CANCELLED` > `FAILED` > `PASSED` is NOT correct — pending must not mask a
  concluded failure on a *different* required check. Precedence is: any FAILED/action_required/
  timed_out present AND no required check still pending ⇒ classify the concluded problem; if
  ANY required check is still pending ⇒ `PENDING` (we haven't heard the full verdict yet).
  (Resolve the exact precedence in the plan with explicit test cases.)

## Routing table (decisions resolved 2026-07-17)

Read `mergeable_state`; for `blocked`, additionally read `required_check_state`.

| mergeable_state | Condition | Outcome → next state | Notes |
|---|---|---|---|
| `clean` / `has_hooks` | mergeable + passing (fresh by C-STRICT) | **CLEAN → Done** (merge) | the green path |
| `unstable` | required green, optional check failing/pending | **CLEAN → Done** (merge) | *Decision U*: optional is optional; already in `CI_PASSING_MERGEABLE_STATES` |
| `behind` | base advanced (visible under C-STRICT) | **FRESHEN** (update-branch healer) → BLOCKED re-poll | existing healer; re-runs checks on fresh base |
| `dirty` | textual merge conflict; CI won't run | **NEEDS_FIX → ImplFix** *(with a "resolve the merge conflict against base" directive)* | *Decision D*: only the Fixer can resolve; update-branch/rebase just fails on a real conflict |
| `unknown` | mergeability not computed (async) | **BLOCKED** re-poll | reference §3; bound retries → NEEDS_HELP via convergence budget (C3) |
| `draft` | PR is a draft (anomalous for a foreman PR) | **NEEDS_HELP** | should not happen |
| `blocked` + required `PENDING` | CI still running | **BLOCKED** re-poll | the legitimate wait — NOT a failure |
| `blocked` + required `FAILED` | genuine failure (fresh by C-STRICT) | **NEEDS_FIX → ImplFix** | the C1 fix |
| `blocked` + required `TIMED_OUT_OR_CANCELLED` | likely infra flake | **re-run once → then NEEDS_HELP** | *Decision T*: not a clear defect for the Fixer; human decides if it recurs; bounded by C3 |
| `blocked` + required `ACTION_REQUIRED` | manual approval / human gate | **NEEDS_HELP** | ImplFix can't fix a human gate |

**Cells that dropped out** (no routing needed):
- **Review gate** (`blocked` + all checks green): neither managed repo requires human PR review,
  so this does not occur from GitHub's side (foreman's own Reviewer runs earlier, in ImplReview).
- **Stale-base**: eliminated by C-STRICT (surfaces as `behind`).
- **`Validate PR title`**: prevented at the source by #541 (PR-title casing normalized at
  creation), so this required check can no longer fail on casing.

## State-machine changes

The routing Outcome kinds mostly exist already:
- `OutcomeKind.NEEDS_FIX` already **routes to ImplFix** (used today from Implementing/ImplReview).
  #317 **reuses** it for the merge-time CI-failed and dirty cases.
- **New edge required:** `MergingState.next_state` currently handles only `CLEAN → Done`,
  `BLOCKED → self-loop`, else `→ NeedsHelp`. It must gain **`NEEDS_FIX → ImplFixState()`** — the
  missing Merging→ImplFix transition that is the core of C1. (`ImplFixState` is reachable today
  only from `ImplReviewState`; this adds Merging as a second entry.)
- `attempt_merge` becomes the classifier: it reads `mergeable_state` + `required_check_state`
  and returns the Outcome per the table (CLEAN / BLOCKED / NEEDS_FIX / NEEDS_HELP), plus the
  existing behind-healer path.

## Convergence-budget interplay (C3)

Two cells rely on bounded retries the current retry-cap-exempt `BLOCKED` self-loop does not
provide: `unknown` re-poll and `timed_out/cancelled` re-run-once. This design assumes the
convergence budget (review finding C3 — a per-ticket transition/time bound that escalates a
too-long-polling ticket to NEEDS_HELP) lands alongside or before #317. Until it does, cap the
re-run/re-poll counts locally (mirroring `MAX_HEAL_ACTIONS`) so no cell can loop unbounded.
The plan must not ship a new self-loop without a bound.

## Testing approach

- Unit-test the classifier (`attempt_merge` / `required_check_state`) against every routing
  cell, with the fake provider returning each `RequiredCheckState` — the fake mirrors the real
  check-run shapes strictly.
- Test the new `MergingState.next_state` edge: `NEEDS_FIX → ImplFixState`.
- Explicit precedence tests for the multi-check cases (one failed + one pending → PENDING;
  all concluded, one failed → FAILED; etc.).
- Regression: a `blocked`+failed PR reaches ImplFix (not an infinite BLOCKED loop) — the exact
  incident this closes.

## Out of scope

- The convergence budget itself (C3) — separate issue; this spec only assumes/uses it.
- The merge coordinator (ADR-0) — the structural end-state that re-tests at merge against the
  real base; #317 is the correct behavior for the current single-PR-at-a-time merge path and
  remains valid under a future coordinator.
- Per-check-*name* routing beyond conclusion type — with `Validate PR title` prevented (#541),
  a required `failure` conclusion is treated uniformly as ImplFix-able; refine only if a
  specific required check proves to need different handling.
