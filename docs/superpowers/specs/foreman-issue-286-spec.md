# Spec: propagate Decision 4 GoF + Google pattern-naming calibration to CLAUDE.md, planner.md, reviewer.md (issue #286)

## Goal

Decision 4 of the architecture stability plan (`docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md`, lines 821-844) prescribes a calibrated prompt-level bias toward GoF patterns and Google engineering principles, encoded into three documents: the repo-root `CLAUDE.md` (human / Wren-driven work), `packages/foreman/src/foreman/prompts/planner.md` (autonomous-loop spec proposals), and `packages/foreman/src/foreman/prompts/reviewer.md` (autonomous-loop spec reviews). The clause is currently absent from all three (grep-confirmed: zero matches for "GoF", "Google", "SRP", "OCP", "DIP", "pattern-fishing" in any of the three files). This spec adds the verbatim Decision 4 clause to each of the three documents as a purely additive change. See issue [#286](https://github.com/jeffrichley/foreman/issues/286).

## Acceptance criteria

- `CLAUDE.md` (repo root, currently 43 lines) gains a new top-level markdown section that contains the verbatim Decision 4 clause as a blockquote. The clause text is exactly:
  > Before proposing a non-trivial design, name the GoF pattern and/or the Google engineering principle (SRP / OCP / DIP / "make the right thing easy") the design embodies. If neither applies cleanly, say so explicitly — "no pattern fits, this is straightforward X" is a legitimate output. Pattern-fishing produces worse code than no pattern at all.

  Recommended placement: insert a new `## Design` section between the existing `## Conventions` section (`CLAUDE.md:33-37`) and the existing `## Architecture` section (`CLAUDE.md:39-43`). The Worker MAY choose a different placement within `CLAUDE.md` so long as the clause is in the document and no existing content is removed or substantively reworded.
- `packages/foreman/src/foreman/prompts/planner.md` (currently 229 lines) gains a new XML-tag section that contains the verbatim Decision 4 clause as a blockquote (same exact wording as above).

  Recommended placement: insert a new `<pattern_naming>` section between the closing tag of `<anti_overengineering>` (`planner.md:98`) and the opening tag of `<pr_body_guardrails>` (`planner.md:100`). This sits the new guidance immediately after the existing scope-discipline guidance and before the PR-body mechanics, which is the natural place for "what to do when proposing the approach." The Worker MAY choose a different placement within `planner.md` so long as the clause is in the document and no existing content is removed or substantively reworded.
- `packages/foreman/src/foreman/prompts/reviewer.md` (currently 255 lines) gains a new XML-tag section that contains the verbatim Decision 4 clause as a blockquote (same exact wording as above).

  Recommended placement: insert a new `<pattern_naming_check>` section between the closing tag of `</what_to_look_for>` (`reviewer.md:108`) and the opening tag of `<verification_rules>` (`reviewer.md:110`). This sits the new criterion next to the three failure-mode axes the Reviewer already checks. The Worker MAY choose a different placement within `reviewer.md` so long as the clause is in the document and no existing content is removed or substantively reworded.
- The blockquote in all three files is BYTE-IDENTICAL to the text quoted above. In particular: the em-dash (`—`) is preserved; the straight quote characters around `"make the right thing easy"` and `"no pattern fits, this is straightforward X"` are preserved; the slash-spacing in `SRP / OCP / DIP` is preserved. No paraphrasing.
- The "or say it doesn't fit" sub-clause is present in all three documents. This is the load-bearing piece against pattern-fishing per Decision 4 (`docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md:827, 839, 844`). A Worker that drops or weakens "If neither applies cleanly, say so explicitly..." has broken the acceptance criterion.
- No existing content in any of the three files is removed, reordered, or substantively reworded. The change is purely additive. Verifiable by running `git diff main -- CLAUDE.md packages/foreman/src/foreman/prompts/planner.md packages/foreman/src/foreman/prompts/reviewer.md` and confirming every diff hunk is a pure addition.
- The Decision 4 clause is NOT added to `packages/foreman/src/foreman/prompts/fixer.md`, `packages/foreman/src/foreman/prompts/fixer_impl.md`, `packages/foreman/src/foreman/prompts/worker.md`, or `packages/foreman/src/foreman/prompts/reviewer_impl.md`. Decision 4 specifies the three documents above; the others are explicitly out of scope per the issue body.
- The architecture stability plan itself (`docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md`) is NOT modified. The plan stays the source of truth; this change propagates the calibration into the three prescribed locations.
- The impl PR title uses a `docs(...)` conventional-commit type (the issue's recommended scope is `docs(prompts)`). Subject must NOT start with an uppercase letter per `CLAUDE.md:36`. The impl PR title MUST pass `.github/workflows/pr-title-lint.yml`.
- The impl PR body references issue #286 plainly — NO GitHub closing-keyword references (`Closes #286` / `Fixes #286` / `Resolves #286`) per foreman#63. Use phrasing like "addresses #286" or "for issue #286".
- `just check` exits 0 on the impl worktree (lint + typecheck + full pytest suite green). `new_failures_count == 0` in CI. Note: this is a docs-only change and is not expected to interact with any test — the gate is included as a regression check that no formatter or test is sensitive to the prompt-file content.

## Approach

The change is three independent, additive markdown edits. There is no code change, no test change, and no schema change. Each edit inserts a small new section into one document; nothing else in any file is touched.

**Why the verbatim wording matters.** Decision 4's text is explicitly framed as "Calibrated wording (load-bearing — 'or say it doesn't fit' prevents pattern-fishing)" (`docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md:827`). The risks named in the plan (`stability-plan.md:838-841`) include "Pattern-fishing (Adapters everywhere) if the lens is uncalibrated — the 'or say it doesn't fit' clause is the defense." Paraphrasing or omitting the sub-clause silently breaks the calibration. Treating the clause as a verbatim blockquote (rather than inline prose the editor might be tempted to "polish") signals to future readers that the wording is fixed by design.

**Why a new section rather than splicing into an existing one.** Each of the three target docs has well-established sections with single-responsibility framing — `## Conventions` in `CLAUDE.md` is about commit / PR mechanics; `<anti_overengineering>` in `planner.md` is about scope discipline; `<what_to_look_for>` in `reviewer.md` is the three failure-mode axes. Splicing a pattern-naming clause into any of those dilutes the host section's focus. A new sibling section keeps each document's existing structure intact (satisfying the "purely additive" AC) and signals the new criterion as its own first-class concept.

**Why these three documents and only these three.** Decision 4 names exactly these three: `CLAUDE.md` for human/Wren-driven work in the foreman tree, and `planner.md` + `reviewer.md` for autonomous-loop output. The issue body's "Out of scope" section restates this explicitly: do NOT add to `fixer.md`, `worker.md`, `reviewer_impl.md`. The rationale (per Decision 4's framing in `stability-plan.md:826`) is that the planning-stage bias is where bandaid-ratio risk actually lives — Fixer and Worker execute against the already-planned spec, so adding the clause to their prompts would be a layer in the wrong place (calibration mid-execution rather than at initial design).

**Why the Worker has flexibility on placement.** The recommended insertion points above are the Planner's grounded recommendation, but the exact placement inside each file is reviewer-judgable and doesn't change the spec's correctness. The acceptance criterion is "clause is in the document and purely additive"; the placement guidance below is a reasonable default. If the Worker finds a clearly better adjacent location (e.g., the `## Architecture` section in `CLAUDE.md` for the human/Wren doc), they may use it.

**No interaction with the architecture stability plan doc.** The plan stays as-is. This is a one-way propagation: plan → three prescribed locations. Anyone reading any of the three target docs can find the calibration without having to read the plan; anyone reading the plan can verify the calibration was propagated correctly by grep-checking the three target files.

## Sub-requests (topologically sorted)

1. **Edit `CLAUDE.md` to add the Decision 4 clause.** Insert a new section between the existing `## Conventions` (ends `CLAUDE.md:37`) and `## Architecture` (starts `CLAUDE.md:39`). Recommended exact text to insert (two blank lines preserved around the new section so existing section spacing is unchanged):

   ```markdown

   ## Design

   Calibrated bias toward structural patterns (per Decision 4 of `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md`):

   > Before proposing a non-trivial design, name the GoF pattern and/or the Google engineering principle (SRP / OCP / DIP / "make the right thing easy") the design embodies. If neither applies cleanly, say so explicitly — "no pattern fits, this is straightforward X" is a legitimate output. Pattern-fishing produces worse code than no pattern at all.

   ```

   The Worker MAY adjust the intro sentence ("Calibrated bias toward structural patterns...") for tone, but the blockquote (everything from `> Before` through `no pattern at all.`) is byte-identical and not negotiable.

2. **Edit `packages/foreman/src/foreman/prompts/planner.md` to add the Decision 4 clause.** Insert a new XML section between the closing `</anti_overengineering>` (`planner.md:98`) and the opening `<pr_body_guardrails>` (`planner.md:100`). Recommended exact text to insert (preserving the existing blank-line spacing pattern between top-level XML sections):

   ```markdown

   <pattern_naming>
   Before sketching the spec's Approach section, apply Decision 4's calibrated lens (`docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md`):

   > Before proposing a non-trivial design, name the GoF pattern and/or the Google engineering principle (SRP / OCP / DIP / "make the right thing easy") the design embodies. If neither applies cleanly, say so explicitly — "no pattern fits, this is straightforward X" is a legitimate output. Pattern-fishing produces worse code than no pattern at all.
   </pattern_naming>

   ```

   The Worker MAY adjust the intro sentence ("Before sketching the spec's Approach section...") for tone, but the blockquote is byte-identical.

3. **Edit `packages/foreman/src/foreman/prompts/reviewer.md` to add the Decision 4 clause.** Insert a new XML section between the closing `</what_to_look_for>` (`reviewer.md:108`) and the opening `<verification_rules>` (`reviewer.md:110`). Recommended exact text to insert:

   ```markdown

   <pattern_naming_check>
   When the spec proposes a non-trivial design, check that the Planner applied Decision 4's calibrated lens (`docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md`):

   > Before proposing a non-trivial design, name the GoF pattern and/or the Google engineering principle (SRP / OCP / DIP / "make the right thing easy") the design embodies. If neither applies cleanly, say so explicitly — "no pattern fits, this is straightforward X" is a legitimate output. Pattern-fishing produces worse code than no pattern at all.

   A spec proposing a non-trivial design that names neither a GoF pattern nor a Google principle AND does not explicitly say "no pattern fits" is missing the calibration. Treat as `important`. A spec that pattern-fishes (names a pattern that doesn't actually fit, e.g. "Adapter" for code that is just a thin wrapper) is also missing the calibration — flag with the same severity. Reviewer-bot does NOT penalize a spec for explicitly stating "no pattern fits, this is straightforward X" — that is a legitimate output per the calibration.
   </pattern_naming_check>

   ```

   The Worker MAY adjust the surrounding prose for tone, but the blockquote (the four-sentence Decision 4 wording) is byte-identical.

4. **Verify byte-identity of the blockquote across all three files.** Run `grep -c "Pattern-fishing produces worse code than no pattern at all\." CLAUDE.md packages/foreman/src/foreman/prompts/planner.md packages/foreman/src/foreman/prompts/reviewer.md`. Expected: each file reports 1 match. Run also `grep -c "or say it doesn't fit" docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md` to confirm the plan's framing line is untouched (expected: 3 matches — the original three references at lines 827, 839, 844). If either grep returns the wrong count, the edit is incorrect; revisit.

5. **Confirm out-of-scope files were not touched.** Run `git diff main -- packages/foreman/src/foreman/prompts/fixer.md packages/foreman/src/foreman/prompts/fixer_impl.md packages/foreman/src/foreman/prompts/worker.md packages/foreman/src/foreman/prompts/reviewer_impl.md docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md`. Expected: empty diff.

6. **Run the full quality gate.** `just check`. Expected: exit 0. The three edits are docs-only and are not expected to interact with lint, typecheck, or tests — the gate is included as a regression check.

## File-level changes

| File | Change |
| --- | --- |
| `CLAUDE.md` | Add a new `## Design` section between existing `## Conventions` (line 37) and `## Architecture` (line 39). Section contains one intro sentence + the verbatim Decision 4 clause as a blockquote. |
| `packages/foreman/src/foreman/prompts/planner.md` | Add a new `<pattern_naming>` XML section between existing `</anti_overengineering>` (line 98) and `<pr_body_guardrails>` (line 100). Section contains one intro sentence + the verbatim Decision 4 clause as a blockquote. |
| `packages/foreman/src/foreman/prompts/reviewer.md` | Add a new `<pattern_naming_check>` XML section between existing `</what_to_look_for>` (line 108) and `<verification_rules>` (line 110). Section contains one intro sentence + the verbatim Decision 4 clause as a blockquote + one short paragraph framing the Reviewer's check on it. |

No expected changes to:

- `packages/foreman/src/foreman/prompts/fixer.md`, `fixer_impl.md`, `worker.md`, `reviewer_impl.md` (explicitly out of scope per the issue).
- `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md` (the plan stays the source of truth).
- Any code in `packages/foreman/src/foreman/`. This is a docs-only change.
- Any test file. The prompts are loaded as static strings at role-dispatch time; no test asserts their content shape.

## Alternatives considered

- **Inline the clause into existing sections rather than adding new ones** (e.g., extend `## Conventions` in `CLAUDE.md`, or splice into `<anti_overengineering>` in `planner.md`). Rejected — each host section has a well-defined single responsibility and splicing in pattern-naming guidance dilutes its focus. The "purely additive, no substantive alteration" AC also strongly favors a new sibling section over editing existing prose.
- **Add the clause to all six prompt files (fixer, fixer_impl, worker, reviewer_impl included)** for symmetry. Rejected — explicitly out of scope per the issue body and per Decision 4's framing. The calibration is a planning-stage lens; Fixer and Worker execute against the already-planned spec, so the bias belongs at the design proposal layer, not at execution. Adding it elsewhere would be calibration in the wrong place.
- **Paraphrase the clause to match each document's tone** (e.g., a terser version for `CLAUDE.md`, a more LLM-directive version for the prompts). Rejected — Decision 4 explicitly flags the wording as "Calibrated (load-bearing — 'or say it doesn't fit' prevents pattern-fishing)" (`stability-plan.md:827`). Paraphrasing risks dropping or softening the load-bearing sub-clause, which the issue Out-of-Scope section also explicitly forbids.
- **Edit the architecture stability plan to inline the propagation status** (e.g., add "propagated to X, Y, Z" notes to Decision 4). Rejected — the plan is the source of truth; mutating it on every propagation creates a churn pattern that diverges from how other Decision-tied tickets are landed (see foreman#280 D9 fix, foreman#285 D1 Labels resurrection — neither modified the plan). Propagation status lives in the merged PR + CHANGELOG, not in the plan body.
- **Do nothing** (rely on the plan doc as sufficient documentation). Rejected — Decision 4 explicitly prescribes propagation into the three operational documents. The plan is too far from the operational paths (planner / reviewer dispatch, contributor onboarding) to bite at the moment of design proposal; the bias must live where the design actually gets proposed.

## Open questions

(None — the wording is fixed by Decision 4, the target files are named by the issue, the placement guidance is grounded in the current file structure of each, and the edit is purely additive.)

## Out of scope

- **Adding the Decision 4 clause to `fixer.md`, `fixer_impl.md`, `worker.md`, or `reviewer_impl.md`.** Decision 4 specifies the three documents this spec targets; the others are not in scope per the issue body's Out of Scope section.
- **Modifying `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md`.** The plan stays as the source of truth; this PR propagates the calibration into the prescribed locations without mutating the plan.
- **Removing or weakening the "or say it doesn't fit" sub-clause** in the clause text. That sub-clause is the explicit guard against pattern-fishing per Decision 4's rationale (`stability-plan.md:827, 839, 844`).
- **Rewording, restructuring, or "polishing" existing content in any of the three target files.** This change is purely additive — every existing line in each file stays as-is.
- **Adding tests that lint the prompt files for the presence of the clause.** A test that pins the wording could be a natural follow-up under Decision 6's "verify-and-pin as a test" discipline, but Decision 4 itself does not require it and the issue does not request it. File separately if desired.
- **Wiring the calibration into impl-side review** (e.g., updating `reviewer_impl.md`'s check criteria to flag impl PRs whose code doesn't name a pattern). Out of scope per the issue's explicit list of three target documents.

## References

- foreman#286 — this ticket. Surfaces the missing Decision 4 propagation.
- foreman#278 — the architecture stability plan PR with the full Decision 4 framing.
- foreman#280 — the D9 autonomous-loop fix that makes this dogfood meaningful (without D9, this ticket's impl PR would orphan onto the spec branch).
- foreman#285 — D1 Labels resurrection, the previous Decision-stamped execution.
- Source pointers used by this spec:
  - `CLAUDE.md:33-43` — existing Conventions + Architecture sections; the insertion point sits between them.
  - `packages/foreman/src/foreman/prompts/planner.md:81-98` — existing `<anti_overengineering>` section; the new `<pattern_naming>` sits immediately after.
  - `packages/foreman/src/foreman/prompts/reviewer.md:84-108` — existing `<what_to_look_for>` section; the new `<pattern_naming_check>` sits immediately after.
  - `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md:821-844` — Decision 4 in full, including the calibrated wording at line 828 and the "load-bearing" framing at line 827.
