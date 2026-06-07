# Spec: document the fixer's additive "fire and forget" attempt-counter pattern + asymmetry with Worker (issue #200)

## Goal

Add a focused code comment to `packages/foreman/src/foreman/roles/fixer.py`
above the `issue.add_to_labels(attempt_label)` call site (currently
line 484) that explains:

1. why the Fixer's pre-dispatch attempt-counter write is **additive**
   (`add_to_labels`) and intentionally NOT atomic via `set_labels`, and
2. why the Fixer intentionally has **no `finally` revert** of the
   counter, in contrast to Worker's `worker.py:1059-1083` revert block.

The change is a comment-only edit. Tracks issue
[#200](https://github.com/jeffrichley/foreman/issues/200).

## Acceptance criteria

- A new comment block is added in
  `packages/foreman/src/foreman/roles/fixer.py` such that it appears
  immediately above the `issue.add_to_labels(attempt_label)` call —
  i.e., directly preceding what is today line 484. The comment may
  either (a) extend / replace the existing 3-line "Stamp the new
  attempt label IMMEDIATELY..." block at lines 481-483, or (b) sit as
  a separate paragraph after that existing block but before line 484.
  The Worker chooses whichever reads more naturally; both shapes
  satisfy this spec.
- The new prose (excluding the existing 3 lines at 481-483 if those
  are preserved) is **under 12 lines** of comment text. "Lines" means
  rendered `# ...` lines in the source, not sentences. Don't write a
  thesis.
- The new prose explains **all three** of the following points, each
  in plain English with no jargon a future maintainer wouldn't grasp
  from the surrounding code:
  1. The write is **additive by design** (`add_to_labels`, not
     `set_labels`) because the Fixer's entry label
     (`foreman:spec-fix` for spec target, `foreman:impl-fix` for impl
     target — both registered in `_FIXER_ENTRY_LABEL_BY_TARGET` at
     `fixer.py:112`) **persists** across attempts and doesn't need
     atomic-transition protection.
  2. The asymmetry with Worker is **intentional**: Worker uses
     `set_labels` + a `finally` revert (`worker.py:722` for the
     atomic write; `worker.py:1059-1083` for the revert block)
     because Worker's entry label (`foreman:plan-approved`, registered
     in `_WORKER_ENTRY_LABELS`) is **consumed** by the dispatch and a
     pre-LLM crash would otherwise strand the issue half-transitioned.
     The Fixer has no entry-label transition to revert, so there is
     nothing for a `finally` block to protect.
  3. The **trade-off** is named explicitly: a Fixer that crashes
     **before producing any output** WILL burn an attempt against the
     `ProjectConfig.max_fix_attempts` budget (default 3, gated at
     `fixer.py:465-473`). This is the steady-state correct behavior —
     three real crashes mean the loop should escalate to humans via
     `foreman:failed`. Transient crashes (e.g., LLM 500) are an
     acceptable rare cost.
- The comment cites Worker's two call sites by file:line so a
  maintainer reading either role lands at the explanation:
  `worker.py:722` (the `set_labels` site) and `worker.py:1059-1083`
  (the `finally` revert block).
- **No code behavior change**: `git diff` shows ONLY added (or
  rephrased) comment lines in `fixer.py` around line 484. No other
  files modified. No statements added, removed, or reordered. No
  imports changed. No formatter-driven reflows outside the comment
  block.
- `just check` exits 0 — confirmed by the Worker before marking the
  impl PR ready. No test changes are expected; if `just check`
  surfaces unrelated drift, the Worker fixes the comment edit first
  and the drift is out of scope.

## Approach

The asymmetry between Worker and Fixer at the pre-LLM label write is
real, intentional, and currently undocumented in code. A reader of
`fixer.py:484` today sees an `add_to_labels` call and a 3-line
"stamp early in case of crash" comment, with no signal that the
neighboring Worker uses a structurally different pattern at the same
phase of its own lifecycle. The fix is a 5-12 line comment block, not
a code restructure.

Two reasons to keep this comment-only:

- **The current pattern is correct.** The Fixer's entry label
  (`foreman:spec-fix` / `foreman:impl-fix`) persists across attempts
  — the reconciler keeps the ticket in the fix-phase by virtue of
  that label until a successful Fixer outcome clears it. There's no
  half-transition state to protect against, so a `set_labels`-style
  atomic write would add zero safety while obscuring the simpler
  shape.
- **Burning an attempt on a crash is the desired behavior.** The
  3-attempt budget exists to escalate persistent failure to a human.
  Three crash-before-output retries means the LLM provider, the
  network, or the prompt is broken in a way the daemon can't fix on
  its own — at that point `foreman:failed` is the right escalation.
  Adding a `finally` revert would mask the crash signal under the
  appearance of a steady infinite-retry loop, which the
  `max_fix_attempts` gate at `fixer.py:465-473` is specifically there
  to prevent.

The Worker comment block to read for context is
`worker.py:682-693` (the rationale for the atomic `set_labels`) and
`worker.py:724-729` (the rationale for the `finally` block). The new
Fixer comment can be terser because the Fixer side is the "no
transition to protect" case — short to explain.

The existing 3-line comment at `fixer.py:481-483` ("Stamp the new
attempt label IMMEDIATELY...") is **partially correct but
incomplete**: it explains the "stamp first" intent but not the
"additive + no revert" design decisions. The Worker may either keep
those three lines and append the new paragraph after them (cleanest
diff, preserves git blame for the existing intent), or rewrite them
into a single integrated block (cleaner final reading order). Either
shape satisfies the spec.

## Sub-requests (topologically sorted)

1. Open `packages/foreman/src/foreman/roles/fixer.py` and read the
   range around line 484 to confirm the current state matches what
   this spec describes. If lines have shifted (e.g., an intervening
   PR landed), locate the `issue.add_to_labels(attempt_label)` call
   by string — that's the anchor — and target the comment to that
   call site.
2. Read `packages/foreman/src/foreman/roles/worker.py` lines 682-693
   (atomic-write rationale), 700-722 (the `set_labels` site), and
   1059-1083 (the `finally` revert block) so the new Fixer comment
   cites the correct line numbers. If line numbers have shifted in
   Worker since this spec was written, use the current line numbers
   in the comment, not the ones in this spec.
3. Add the new comment block immediately above the
   `issue.add_to_labels(attempt_label)` call. The comment must cover
   the three points listed in Acceptance criteria item 3, cite Worker
   call sites by file:line, and stay under 12 lines of prose.
4. Run `just check`. Confirm zero failures. Confirm `git diff`
   touches only `fixer.py` and only the comment region.
5. Commit on `foreman/impl-200` with a conventional-commit message
   under the `docs(fixer):` scope (e.g.,
   `docs(fixer): explain additive attempt-counter pattern + worker asymmetry`).
   Single commit; no fix-up commits needed for a comment edit.

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/src/foreman/roles/fixer.py` | **Comment-only.** Add (or extend) a comment block immediately above `issue.add_to_labels(attempt_label)` near line 484 explaining the additive-by-design pattern, the intentional asymmetry with Worker's `set_labels` + `finally` revert (cite `worker.py:722` and `worker.py:1059-1083`), and the named trade-off that a pre-output Fixer crash burns an attempt against `ProjectConfig.max_fix_attempts`. Under 12 lines of new prose. No code change. |

No other files modified. Specifically: no edits to
`packages/foreman/src/foreman/roles/worker.py`, no edits to any test
file, no edits to any docstring on `run_fixer`, no edits to any
architectural doc.

## Alternatives considered

- **Change the Fixer's label-write pattern to atomic `set_labels` to
  match Worker.** Rejected: this would be a behavior change, and the
  issue body explicitly forbids it ("Do not change Fixer's
  label-write pattern from `add_to_labels` to `set_labels`. The
  current shape is correct."). The Fixer's entry label persists
  across attempts, so atomic-transition protection adds zero safety
  while obscuring the simpler design.
- **Add a `finally` revert to undo the attempt counter on crash.**
  Rejected: this would hide the failure signal that the 3-attempt
  budget gate (`fixer.py:465-473`) is designed to surface. A Fixer
  that crashes before producing output three times in a row should
  escalate via `foreman:failed`, not loop silently. Issue body
  explicitly forbids this ("The fire-and-forget IS the design.").
- **Write the explanation in the architecture doc
  `docs/architecture/v3-reconciler.md` instead of in `fixer.py`.**
  Rejected: that doc does not exist in the repo as of this spec
  (verified via `Glob`), and the issue body explicitly scopes that
  doc's update to a follow-up ticket ("the architecture doc... will
  be updated separately to reference the new comment"). The
  authoritative explanation needs to live next to the code so a
  future maintainer reading either Worker or Fixer can find it
  without crossing a doc boundary.
- **Add the explanation to the `run_fixer` docstring at the top of
  the function instead of inline above line 484.** Rejected: the
  issue body explicitly forbids this ("Do not change the docstring
  of `run_fixer` — the comment goes on the call site, not the
  function signature."). The rationale: a maintainer scanning the
  function body for the label-write reads the call site, not the
  function signature.
- **Add a more aggressive option-C "cap-skip" mechanism that detects
  a "didn't actually try" Fixer crash and skips the attempt
  decrement.** Rejected: explicitly named in the issue body's
  "Related → Future follow-up" section as a separate ticket gated on
  telemetry showing transient crashes are a real cost. Out of scope
  here.

## Open questions

None. The asymmetry is real and verified in code
(`fixer.py:484` is `add_to_labels`; `worker.py:722` is `set_labels`;
`worker.py:1059-1083` is the `finally` revert). The trade-off is
explicit in the issue body and matches the existing
`max_fix_attempts` gate's intent. The Worker has discretion on
whether to extend the existing 3-line comment at `fixer.py:481-483`
or write a separate paragraph after it — both shapes satisfy the
acceptance criteria.

## Out of scope

- Modifying any file other than `packages/foreman/src/foreman/roles/fixer.py`.
- Changing the `add_to_labels` call to `set_labels`. The additive
  shape is the design.
- Adding a `try` / `finally` block to revert the attempt counter on
  crash. The fire-and-forget shape is the design.
- Modifying Worker's pre-dispatch write or `finally` revert block.
- Refactoring any role's label-write call sites.
- Modifying or adding any test file.
- Changing the docstring of `run_fixer` at `fixer.py:42` or any other
  function-level docstring.
- Creating, editing, or referencing
  `docs/architecture/v3-reconciler.md`. That doc's update is a
  separate follow-up ticket per the issue body.
- Implementing the "option C cap-skip semantics" follow-up mentioned
  in the issue body's Related section. That is a separate ticket
  gated on telemetry data.
