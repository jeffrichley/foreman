# Spec: add lowercase-subject rule to Planner and Worker prompts (issue #445)

## Goal

Add explicit "subject must begin with a lowercase letter" instructions to the Planner prompt, the Worker prompt, and the project's `.foreman/INSTRUCTIONS.md`, so that every spec and impl PR title foreman opens passes the repo's `pr-title-lint` gate. See issue [#445](https://github.com/jeffrichley/foreman/issues/445).

## Acceptance criteria

- `packages/foreman/src/foreman/prompts/planner.md` — the `<outputs>` section's `pr_title` item contains explicit lowercase-subject instruction plus rephrase guidance (e.g. "start with a lowercase verb if the natural subject would open with a class name").
- `packages/foreman/src/foreman/prompts/worker.md` — the `<output_schema>` section's `pr_title` bullet contains the same lowercase-subject rule and rephrase guidance; the existing "lowercase subject" wording in `<commit_discipline>` is kept (no removal).
- `.foreman/INSTRUCTIONS.md` — the "PR title rules" section states that subjects must begin with a lowercase letter and gives the rephrase guidance.
- `packages/foreman/tests/test_prompts.py` — a new parameterized test `test_pr_title_subject_lowercase_rule_in_prompt` asserts that the phrase `"lowercase letter"` appears in `planner.md` and in `worker.md`, failing if either file loses the instruction.
- `just check` exits zero; no new test failures.

## Approach

**Pattern**: No GoF pattern applies — this is a targeted addition of a missing constraint to three text files. The Google engineering principle at work is "make the right thing easy": placing the lowercase-subject rule where the model forms its `pr_title` field makes the correct behaviour automatic, without mechanical post-processing (which the issue explicitly rejected as option 2 because it would corrupt identifiers).

**Root cause**: `planner.md`'s `<outputs>` section describes `pr_title` with an example that happens to be lowercase but has no explicit rule. `worker.md`'s `<commit_discipline>` says "lowercase subject" (line 249) but this phrase is not visible in the `<output_schema>` section where the model forms the `pr_title` field. The `.foreman/INSTRUCTIONS.md` also lacks the constraint. When the most natural subject starts with a code identifier (`ImplApproved`, `PRState`), the models capitalize it because nothing told them not to.

**Fix surface**:

1. `planner.md` `<outputs>`, item 2 (`pr_title`): append two sentences — one naming the exact lint rule, one giving the rephrase guidance. Placing the rule here means the Planner sees it exactly when it is deciding what to write for `pr_title`.

2. `worker.md` `<output_schema>`, the `pr_title` bullet: append the same two sentences. The existing "lowercase subject" in `<commit_discipline>` is left in place (belt-and-suspenders for commit messages). Adding it to `<output_schema>` closes the gap where the output-field-level schema description has no lowercase rule.

3. `.foreman/INSTRUCTIONS.md` "PR title rules" section: add three lines below the existing type list so every role bot that reads the project instructions sees the same constraint at the project-configuration level.

4. `test_prompts.py`: add a pinning test (following the same pattern as `test_prompt_mentions_context7_tools`) so a future prompt edit that accidentally drops the instruction surfaces immediately in CI. Pin the phrase `"lowercase letter"`, which does not appear in either file today.

**Rephrase guidance wording** (same across all three files, for consistency):

> The subject (everything after `type(scope): `) MUST begin with a lowercase letter. The repo's `pr-title-lint` enforces `subjectPattern: ^(?![A-Z]).+$`. When the natural subject would start with a code identifier (class name, acronym, constant), open with a lowercase verb instead: e.g. `rework ImplApproved into a polling state` not `ImplApproved polling state`.

## Sub-requests (topologically sorted)

1. Edit `.foreman/INSTRUCTIONS.md`: in the "PR title rules" section, after the line `For impl PRs (produced by the Worker): use \`<type>(<scope>): ...\``, add a blank line and the three-sentence lowercase-subject rule with the rephrase example.

2. Edit `packages/foreman/src/foreman/prompts/planner.md`: in the `<outputs>` section, item 2 (`pr_title`), after the sentence `Do NOT invent new types like \`spec:\` — those will be rejected.`, add a blank line (not needed, just inline append) followed by two sentences: the lint-rule statement and the rephrase guidance.

3. Edit `packages/foreman/src/foreman/prompts/worker.md`: in the `<output_schema>` section, the `pr_title` bullet (currently ending `The Pydantic validator enforces this.`), append `The subject (after \`type(scope): \`) MUST begin with a lowercase letter — same \`pr-title-lint\` rule the commit discipline enforces. When the natural subject opens with a code identifier, open with a lowercase verb: \`rework ImplApproved into ...\` not \`ImplApproved ...\`.`

4. Add a parameterized test to `packages/foreman/tests/test_prompts.py` that asserts `"lowercase letter"` appears in `planner.md` and `worker.md`.

## File-level changes

| File | Change |
|------|--------|
| `.foreman/INSTRUCTIONS.md` | Add lowercase-subject rule + rephrase guidance to "PR title rules" section |
| `packages/foreman/src/foreman/prompts/planner.md` | Add lowercase-subject rule + rephrase guidance to `<outputs>` `pr_title` item |
| `packages/foreman/src/foreman/prompts/worker.md` | Add lowercase-subject rule + rephrase guidance to `<output_schema>` `pr_title` bullet |
| `packages/foreman/tests/test_prompts.py` | Add `test_pr_title_subject_lowercase_rule_in_prompt` parametrized over `planner.md` / `worker.md` |

## Alternatives considered

1. **Mechanical post-process (option 2 from issue)**: lowercase the first character of the generated subject before opening the PR. Rejected — the issue explicitly rules this out because `ImplApproved` → `implApproved` corrupts the identifier; the issue body documents this as the reason option 1 is preferred.

2. **Add the rule only to `.foreman/INSTRUCTIONS.md` (project instructions), not to the role prompts**: Rejected — the role prompts are the canonical contract the LLM reads; `INSTRUCTIONS.md` is supplemental project context loaded into the user prompt. Fixing only the project instructions leaves the core prompt unconstrained. The `planner.md`/`worker.md` changes are load-bearing; the `INSTRUCTIONS.md` change is complementary defense-in-depth.

3. **Add a runtime post-process that calls `str.lower()` only on the very first character before passing the title to `host.open_pull_request`**: Rejected — same corrupted-identifier problem as option 2, just deferred to Python rather than the prompt layer.

## Open questions

None. The fix is unambiguous: add the missing text to three files and add a pinning test.

## Out of scope

- Changing the `subjectPattern` regex or the types list in `.github/workflows/pr-title-lint.yml`.
- Adding a runtime character-casing guard anywhere in `planner.py` or `worker.py`.
- Backfilling the subject casing on any previously opened PRs.
- Updating `reviewer.md`, `reviewer_impl.md`, `fixer.md`, or `fixer_impl.md` — those roles do not produce `pr_title` fields.
