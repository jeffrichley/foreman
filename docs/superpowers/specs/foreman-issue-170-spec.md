# Spec: document the merging phase in `init.py` module docstring (issue #170)

## Goal

Add a one-or-two-sentence note to the `packages/foreman/src/foreman/init.py`
module docstring describing the new `foreman:merging-plan` /
`foreman:merging-impl` "trying-to-merge" phase that #169 (the
`attempt_merge` state machine) introduced. The docstring's
"create Foreman state + modifier + attempt labels" line is currently
silent about the merging-phase additions, so an engineer reading the
module top-of-file no longer gets a complete picture of what `init`
creates.

This is intentionally a tiny ticket — its second purpose is to act as
an end-to-end smoke test of the `ATTEMPT_MERGE_PLAN` /
`ATTEMPT_MERGE_IMPL` actions delivered by #169. See
[#170](https://github.com/jeffrichley/foreman/issues/170) and the prior
spec at `docs/superpowers/specs/foreman-issue-165-spec.md` for the full
context on what the merging phase represents.

## Acceptance criteria

- The module-level docstring at the top of
  `packages/foreman/src/foreman/init.py` (currently lines 1-37) mentions
  the merging phase in one or two sentences. The note must:
  - Name both labels explicitly: `foreman:merging-plan` and
    `foreman:merging-impl`.
  - State that this phase sits between Reviewer-approval and final
    merge.
  - Reference the `ATTEMPT_MERGE_*` actions as the runtime drivers
    that evaluate the PR's `mergeStateStatus` during that phase.
- The note lives in the module docstring (lines 1-37). No other file
  is touched. No code changes. No new tests.
- The numbered init-flow steps and the existing "Design notes" bullets
  retain their current shape — the addition extends step 4's bullet,
  it does not renumber or reorder the rest of the docstring.
- `just check` passes (lint + typecheck + tests). Because nothing
  changes outside a docstring, the gate is a no-op in terms of code
  semantics; it exists to confirm no incidental drift.

## Approach

The module docstring (`packages/foreman/src/foreman/init.py:1-37`)
describes the seven steps `foreman init` performs. Step 4 is the
label-creation step:

```
  4. Creates the Foreman state + modifier + attempt labels on the
     target repo (idempotent: existing labels are left alone)
```

The `_FOREMAN_LABELS` constant below the docstring
(`init.py:78-118`) already lists the two new labels —
`foreman:merging-plan` at lines 87-90 and `foreman:merging-impl` at
lines 99-102 — but the docstring has no equivalent acknowledgement.
The minimal, correct fix is to extend step 4's bullet with one
clarifying sentence naming the merging phase, so a reader of the
docstring gets a complete inventory of what the labels cover.

Why extend step 4 rather than add a new Design-notes bullet:

- Step 4 is the docstring's existing surface for "what labels does
  init create". The merging labels ARE state labels, so this is
  semantically the right home.
- The "Design notes" section (`init.py:18-37`) is reserved for
  rationale that explains why the implementation looks the way it
  does — placement of business logic, the idempotence stance, the
  TOML-write strategy. The merging phase is not a design choice
  `init` made; it is a feature of the broader pipeline. Putting it
  in Design notes would be a category error.
- The note must be short. Two sentences is the upper bound named by
  the issue body. Step 4 currently fits on two lines; appending one
  short sentence keeps the docstring's existing rhythm.

Suggested wording for the Worker (one acceptable rendering; others
that meet the acceptance criteria are equally fine):

```
  4. Creates the Foreman state + modifier + attempt labels on the
     target repo (idempotent: existing labels are left alone). The
     state labels include a "merging" phase
     (``foreman:merging-plan`` / ``foreman:merging-impl``) that
     surfaces between Reviewer-approval and final merge, while the
     ``ATTEMPT_MERGE_*`` actions evaluate the PR's
     ``mergeStateStatus``.
```

This stays within step 4's bullet (preserving the numbered-list
shape), names both labels, locates the phase between
"Reviewer-approval" and "final merge" (matching the issue body's
framing), and names the `ATTEMPT_MERGE_*` actions as the drivers.
Worker may choose alternate phrasing as long as the three required
elements above are present.

## Sub-requests (topologically sorted)

1. In `packages/foreman/src/foreman/init.py`, extend the step-4 bullet
   of the module docstring (currently lines 10-11) with a
   one-or-two-sentence clause that names both `foreman:merging-plan`
   and `foreman:merging-impl`, locates the phase between
   Reviewer-approval and final merge, and references the
   `ATTEMPT_MERGE_*` actions as the runtime drivers that evaluate the
   PR's `mergeStateStatus`.
2. Run `just check` and confirm it exits zero.

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/src/foreman/init.py` | Append a one-or-two-sentence clause to the step-4 bullet of the module docstring describing the merging phase. No code, no tests, no other files touched. |

## Alternatives considered

- **Add the note as a new bullet in the "Design notes" section of the
  docstring (`init.py:18-37`).** Rejected: the Design-notes section
  documents `init`-specific implementation choices (label-creation
  idempotence, bot-verification best-effort posture, TOML
  string-rewrite strategy). The merging phase is a pipeline-level
  concept, not an `init`-level design choice. Putting it there would
  mis-categorize the note and force a reader looking up "what labels
  are created" to read two separate sections.
- **Also update the inline `_FOREMAN_LABELS` comment at
  `init.py:73-77`** ("state labels first... then modifier labels...
  then attempt counters") to call out the merging phase too.
  Rejected: the issue scope is explicit — "module docstring only, no
  code changes". Comments adjacent to a constant are borderline, and
  the inline comment is already accurate (the merging labels ARE
  state labels, listed first in pipeline order). No drift; no edit
  needed.
- **Do nothing (close as won't-fix).** Rejected: the issue body
  explicitly requests the docstring update, and the secondary
  motivation (end-to-end smoke test of #169's new state machine
  through the full Planner → Worker → Reviewer → merge loop) cannot
  be exercised without a real code-change ticket. A no-op spec PR
  would not produce a downstream impl PR for the merging-impl phase
  to act on.

## Open questions

None. The required content is fully specified by the issue body; the
suggested phrasing in Approach satisfies every acceptance criterion;
the location (step 4's bullet) is unambiguous.

## Out of scope

- **Acceptance items 3 and 4 in the issue body** ("Loop fires
  ADVANCE_LABEL_TO_MERGING_PLAN, ATTEMPT_MERGE_PLAN,
  ADVANCE_LABEL_TO_MERGING_IMPL, ATTEMPT_MERGE_IMPL actions visible
  in reconciler.sqlite execution_log" and "PR auto-merges cleanly via
  the new state machine"). These are runtime observations of the
  foreman daemon driving this very ticket through the pipeline. They
  cannot be implemented or verified by code changes inside this PR;
  they are observed by the operator on the running daemon as this
  spec PR (and its implementation PR) flow through the merging
  phases. The Worker must NOT add code, tests, or assertions
  attempting to verify them. If they fail to occur, the operator
  diagnoses the daemon and files follow-up tickets — that is the
  smoke-test feedback loop the issue exists to exercise.
- Cleaning up stale `foreman:merging-plan` / `foreman:merging-impl`
  labels after merge. Documented as out-of-scope in
  `docs/superpowers/specs/foreman-issue-165-spec.md` (Approach
  section, "Why no in-action label cleanup").
- Touching any of the other `init.py` constants, helpers, or the
  `_FOREMAN_LABELS` list itself. The merging labels are already
  present in the constant from #169.
- Touching any file other than `packages/foreman/src/foreman/init.py`.
