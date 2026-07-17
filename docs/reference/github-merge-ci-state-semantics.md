# GitHub PR Merge-State & CI-Check Semantics — Authoritative Reference

> Compiled 2026-07-17 from authoritative GitHub sources (GraphQL schema, REST API docs,
> branch-protection docs) as the foundation for foreman#317 (granular mergeable_state
> handling: CI-failed → ImplFix, dirty → rebase, behind → update-branch). Facts only;
> routing decisions live in the companion routing-table doc.

Primary authoritative sources:
- GitHub public GraphQL schema (SDL, inline descriptions): https://docs.github.com/public/fpt/schema.docs.graphql
- GraphQL enums reference: https://docs.github.com/en/graphql/reference/enums
- REST pulls: https://docs.github.com/en/rest/pulls/pulls?apiVersion=2022-11-28
- REST check runs: https://docs.github.com/en/rest/checks/runs?apiVersion=2022-11-28
- REST commit statuses: https://docs.github.com/en/rest/commits/statuses?apiVersion=2022-11-28
- Branch protection: https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches
- Actions events: https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows

---

## 1. GraphQL `MergeStateStatus` enum (AUTHORITATIVE, documented)

`PullRequest.mergeStateStatus: MergeStateStatus!` — *"Represents the possible states of a
pull request from the point of view of merging."*

| Value | GitHub's verbatim description |
|-------|-------------------------------|
| `BEHIND` | "The head ref is out of date." |
| `BLOCKED` | "The merge is blocked." |
| `CLEAN` | "Mergeable and passing commit status." |
| `DIRTY` | "The merge commit cannot be cleanly created." |
| `DRAFT` | "The merge is blocked due to the pull request being a draft." — **DEPRECATED**, use `PullRequest.isDraft`. |
| `HAS_HOOKS` | "Mergeable with passing commit status and pre-receive hooks." (GHE pre-receive hooks) |
| `UNKNOWN` | "The state cannot currently be determined." |
| `UNSTABLE` | "Mergeable with non-passing commit status." |

Distinct coarse enum — `MergeableState` (returned by `PullRequest.mergeable`): `CONFLICTING`,
`MERGEABLE`, `UNKNOWN` ("still being calculated").

## 2. REST `mergeable_state` (UNDOCUMENTED) → GraphQL mapping

GitHub does **not** officially document `mergeable_state` values. Mapped to the documented
GraphQL enum (Section 1); meanings corroborated (UNOFFICIAL) from octokit/octokit.net#1763
and community discussion #24299 (GitHub staff confirm it's undocumented).

| REST `mergeable_state` | UNOFFICIAL meaning | GraphQL equiv | Documented meaning |
|------------------------|--------------------|---------------|--------------------|
| `clean` | No conflicts, mergeable (green) | `CLEAN` | Mergeable and passing commit status |
| `dirty` | Merge conflict; blocked | `DIRTY` | Merge commit cannot be cleanly created |
| `blocked` | Blocked by failing/missing required check* | `BLOCKED` | The merge is blocked |
| `behind` | Head behind base; only when required checks + strict (not loose) | `BEHIND` | The head ref is out of date |
| `unstable` | Non-required check failing/pending; mergeable (yellow) | `UNSTABLE` | Mergeable with non-passing commit status |
| `has_hooks` | GHE pre-receive hooks; mergeable | `HAS_HOOKS` | Mergeable + passing + pre-receive hooks |
| `unknown` | Not computed yet; blocked | `UNKNOWN` | State cannot currently be determined |
| `draft` | PR is a draft | `DRAFT` (deprecated) | Blocked due to draft |

\* The community gloss for `blocked` ("failing/missing required status check") is **too
narrow**. Documented `BLOCKED` = "The merge is blocked" covers ANY branch-protection block:
required checks not satisfied, required reviews missing, unresolved conversations, required
deployments, code-owner review, merge-base-changed re-approval. **The value never says why.**

## 3. Async computation of `mergeable` / `mergeable_state` (polling)

Verbatim (REST pulls): "If the value is null, then GitHub has started a background job to
compute the mergeability. After giving the job time to complete, resubmit the request."
- `mergeable` ∈ {true, false, null}; `null` ⇔ `mergeable_state: "unknown"` while computing.
- Official guidance: **poll** (give it time, resubmit). No fixed interval / max time documented.
- GraphQL analogue: `MergeableState.UNKNOWN` = "still being calculated."

## 4. Check runs — `status` and `conclusion`

### 4a. `status` (GraphQL `CheckStatusState`)
`queued`, `in_progress`, `completed`, `waiting`, `requested`, `pending`. A check carries a
`conclusion` **only** once `status == completed`. queued/in_progress/waiting/requested/pending
= no conclusion yet = **pending, not failed**.

### 4b. `conclusion` (GraphQL `CheckConclusionState`, once completed)
| Value | Verbatim |
|-------|----------|
| `success` | "has succeeded" |
| `failure` | "has failed" |
| `neutral` | "was neutral" (non-failing, non-success) |
| `cancelled` | "has been cancelled" |
| `timed_out` | "has timed out" |
| `action_required` | "requires action" (e.g. manual approval) |
| `skipped` | "was skipped" (non-failing, non-success) |
| `stale` | "marked stale by GitHub. Only GitHub can use this" |
| `startup_failure` | "has failed at startup" |

- `neutral`/`skipped` are non-failing but non-success; whether they satisfy a required check
  depends on protection config.
- `stale`/`startup_failure` are GitHub-set.
- List-check-runs-for-ref caps at the 1000 most recent check suites.

## 5. Combined Statuses (legacy) vs check-runs — LOAD-BEARING

- Legacy Statuses API states: `error`, `failure`, `pending`, `success`. Combined `state` =
  failure if any context error/failure; pending if none exist OR any pending; success if all
  latest success. **No statuses at all → `pending`** (not success).
- **GitHub Actions reports via check-runs, NOT the legacy Statuses API.** A combined-status
  query MISSES all Actions results. → query check-runs, or use `statusCheckRollup` (Section 6).
  [Flag: established structurally + universally observed; no single official sentence states it verbatim.]

## 6. GraphQL `statusCheckRollup` — the unified surface

`Commit.statusCheckRollup` / `PullRequest.statusCheckRollup`: *"Represents the rollup for both
the check runs and status for a commit."* `contexts` = union `CheckRun | StatusContext` — the
single surface that does NOT miss Actions. `state: StatusState` ∈ {`ERROR`, `EXPECTED`,
`FAILURE`, `PENDING`, `SUCCESS`}. The rollup `state` **collapses** the richer check-run
conclusion space — to know which/why, iterate `contexts`.

## 7. Branch protection: "Require branches to be up to date before merging" (STRICT)

- STRICT ON: when the base advances past the PR's merge base, the PR reads `BEHIND`/`behind`
  and **must** be updated before merge, even if all checks are green.
- STRICT OFF (loose): a stale PR whose base moved can still read `CLEAN` — staleness is NOT
  surfaced. → the switch that decides whether "base advanced" is visible (`behind`) or invisible.
- Separate: merge-base-changed re-approval (dismiss-stale-reviews) surfaces as `BLOCKED`, not `behind`.
- Same option exists in repository rulesets.

## 8. Where `pull_request` Actions execute — `refs/pull/N/merge`

- `pull_request` checks run against `refs/pull/N/merge` = **PR head merged into base** ("CI
  tests run against the merged result, not just the head alone").
- Trigger activity types include `synchronize` = "when a PR's head branch is updated" (head
  push). **Advancing the BASE is not a `pull_request` activity type** → does NOT dispatch a new
  run. Existing check runs do **not** auto-re-run on base movement; only head push / manual
  re-run / update-branch re-triggers them. → a green check can reflect a merge against a
  **stale** base. [Flag: documented-by-absence; corroborated by strict-up-to-date existing to force re-build.]
- `pull_request` workflows do NOT run while a merge conflict exists (`dirty`) — must resolve first.

## 9. Ambiguities the signals CANNOT self-disambiguate

1. **Required-check FAILED vs PENDING — both read `BLOCKED`.** Resolve via check-run status/conclusion.
2. **`BLOCKED` never says WHAT blocks** (check / review / conversation / deployment / re-approval).
3. **Genuine code failure vs stale-base failure** — a `failure` conclusion can be a real defect
   OR an artifact of a stale base (checks don't auto-re-run on base movement).
4. **`CLEAN` can be stale** — with strict OFF, a stale PR still reads clean+green.
5. **No-checks-yet vs no-CI vs in-flight** — combined status `pending` also means "no statuses
   exist"; zero check-runs can't distinguish "no CI" from "not started." `unstable` = optional
   check failing OR pending (can't tell which).
6. **`UNKNOWN` conflates "not computed yet" with "indeterminate"** — needs re-poll to tell apart.
7. **`neutral`/`skipped` vs `success`** — non-failing, but "counts as passing?" depends on config.
8. **`stale` conclusion** — result invalidated; neither pass nor fail; says nothing about the fresh result.
9. **Rollup collapses detail** — ERROR/EXPECTED/FAILURE/PENDING/SUCCESS can't name which context / why.
