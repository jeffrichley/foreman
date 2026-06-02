# Spec: stop Planner from auto-closing the originating issue via spec PR body (issue #63)

## Goal

Eliminate the spec PR's ability to auto-close its originating issue at
spec-merge time. The Planner LLM currently writes `Closes #N` into the
spec PR's body (caught 2026-06-02: spec PR #60 → issue #41 auto-closed
before any implementation existed). Issue closure must route exclusively
through `daemon_runners.merge_impl_pr` → `close_issue`
(`packages/foreman/src/foreman/daemon_runners.py:250`), which fires only
after impl is merged AND Reviewer-on-impl approves (foreman#41).

Tracks issue [#63](https://github.com/jeffrichley/foreman/issues/63).

## Acceptance criteria

- `packages/foreman/src/foreman/prompts/planner.md` gains a new
  `<pr_body_guardrails>` section explicitly forbidding GitHub auto-close
  keywords in `pr_body`, listing the nine keyword forms (`close`,
  `closes`, `closed`, `fix`, `fixes`, `fixed`, `resolve`, `resolves`,
  `resolved`), naming the rationale (closure routes through
  `daemon_runners.merge_impl_pr`), and cross-referencing foreman#63.
- The `<outputs>` section's description of `pr_body` in
  `packages/foreman/src/foreman/prompts/planner.md` cross-references the
  new `<pr_body_guardrails>` section so the rule is discoverable from
  the schema-shape section as well.
- `packages/foreman/src/foreman/roles/planner.py` gains a
  module-private helper `_strip_auto_close_keywords(body: str) -> str`
  that removes GitHub auto-close keyword+issue-reference patterns from
  a text body (defense in depth — the runtime guarantee independent of
  LLM compliance).
- `run_planner` in
  `packages/foreman/src/foreman/roles/planner.py:178-184` invokes
  `_strip_auto_close_keywords` on `llm_output.pr_body` and passes the
  scrubbed result to `host.open_pull_request(..., body=...)`. The
  original `llm_output.pr_body` on the persisted `PlannerOutput` (which
  reaches `PlannerRunResult.llm_output`) is NOT mutated — keep the
  audit log faithful to what the LLM produced.
- Unit tests in `packages/foreman/tests/test_roles_planner.py`
  pin the strip helper's behavior across all nine keyword forms,
  uppercase/lowercase/titlecase variants, the `owner/repo#N`
  cross-repo form, the colon-separator form (`Closes: #42`), and
  multi-issue forms (`closes #42, fixes #43`). Plain `#42`
  references without a keyword survive unchanged.
- An integration test in
  `packages/foreman/tests/test_roles_planner.py` named
  `test_run_planner_strips_auto_close_keywords_from_pr_body` stubs
  the fake `ProviderFacade` to return a `PlannerOutput` whose
  `pr_body` contains `"Closes #42. See spec."`, runs `run_planner`,
  and asserts that the `body` kwarg passed to
  `_FakeHostProvider.open_pull_request` does NOT match
  `re.compile(r"(?i)\b(close[sd]?|fix(?:es|ed)?|resolve[sd]?)\b\s*:?\s+(?:[\w.-]+/[\w.-]+)?#\d+")`.
- A prompt-text regression test named
  `test_planner_prompt_forbids_auto_close_keywords` reads
  `packages/foreman/src/foreman/prompts/planner.md` via
  `foreman.prompts.load_role_prompt("planner")` and asserts that the
  rendered text contains the literal substrings `"pr_body_guardrails"`,
  `"Closes"`, `"Fixes"`, `"Resolves"`, and `"merge_impl_pr"`. This
  pins the prompt-side guardrail so a future copyedit can't silently
  strip it.
- `just check` exits zero.

## Approach

The bug has one cause and two layers of fix.

The cause: the Planner LLM produces `pr_body`, that value is passed
verbatim to `host.open_pull_request` at
`packages/foreman/src/foreman/roles/planner.py:178-184`, and GitHub
treats any merged PR whose body contains `Closes #N` (or any of the
nine keyword variants) as a signal to close issue #N. The Planner's
prompt currently does not warn against this, and the runtime does not
filter the LLM's output. Both gaps need to close — prompt as the
primary teach, runtime as deterministic backstop.

**Prompt layer (primary).** Add a `<pr_body_guardrails>` section to
`planner.md`. The section names the nine keyword forms verbatim, says
"MUST NOT include … in your `pr_body`", and explains the rationale by
referencing `daemon_runners.merge_impl_pr` as the one authorized
issue-closure path. Tone matches the existing `<anti_overengineering>`
section: short, declarative, with the rationale spelled out so the LLM
can generalize. We also touch the `<outputs>` section's bullet about
`pr_body` to cross-reference `<pr_body_guardrails>` so the rule is
findable from both directions.

**Runtime layer (defense in depth).** Add a small private helper
`_strip_auto_close_keywords` in `roles/planner.py`, immediately above
`run_planner`. The regex matches the nine keyword forms
case-insensitively, allows an optional colon (`Closes: #42`), allows
optional whitespace, allows an optional `owner/repo` cross-repo prefix,
and matches the bare `#<digits>` issue reference. The substitution
replaces the keyword+separator (`Closes ` or `Closes: `) with the empty
string while preserving the `#N` token — that keeps the human-readable
issue reference intact while neutralizing the auto-close semantics.

`run_planner` invokes the helper on `llm_output.pr_body` and passes
the scrubbed text into `host.open_pull_request`. Critically, we do not
mutate `llm_output.pr_body` itself — `PlannerRunResult` carries the
LLM's original output to the audit log, and an audit log that hides
what the LLM actually produced defeats its purpose. The scrub is
applied only to the kwarg flowing into the GitHub call.

This split matches what the issue body recommends ("Optionally:
post-generation, strip … defense in depth — same pattern as input
sanitization") and the broader Foreman discipline of putting
deterministic guardrails in core when LLM compliance can't be
trusted (cf. spec #46 §"Pydantic-first contract" and the read-only
tool surface at `PLANNER_ALLOWED_TOOLS`).

The regex covers GitHub's full closing-keywords syntax per
[GitHub docs on linking PRs to issues](https://docs.github.com/en/issues/tracking-your-work-with-issues/linking-a-pull-request-to-an-issue):
all nine verb forms (`close`/`closes`/`closed`, `fix`/`fixes`/`fixed`,
`resolve`/`resolves`/`resolved`), optional colon separator, optional
`owner/repo` cross-repo prefix, `#<N>` issue reference. The issue's
proposed regex
`(?i)\b(closes|fixes|resolves)\b\s+#\d+` is the minimum bar; we go one
notch further because the failure mode is silent and the cost is
trivial.

Tests live in `test_roles_planner.py` to keep the Planner's
guardrails in one file. The strip helper gets a small table-driven
unit test for each form. The integration test reuses the existing
`_FakeHostProvider` and `_make_llm_output` machinery
(`test_roles_planner.py:64-211`), overriding `pr_body` to include
`Closes #42` and asserting the scrubbed value arrives at
`_FakeHostProvider.open_pull_request`. The prompt-text test loads the
markdown via `foreman.prompts.load_role_prompt` (the existing public
helper at `packages/foreman/src/foreman/prompts/__init__.py:106-112`)
and pattern-matches for the new section's literal markers.

We deliberately do NOT update the Worker (`worker.md` /
`roles/worker.py`) per the issue's explicit out-of-scope statement —
the Worker already omits `Closes #N` from impl PR bodies today, and
that's the correct shape.

## Sub-requests (topologically sorted)

1. Add the `_strip_auto_close_keywords(body: str) -> str` helper to
   `packages/foreman/src/foreman/roles/planner.py`. Place it
   immediately above `run_planner` (after `_spec_doc_relpath`). Keep
   it module-private (underscore prefix) — it is Planner-specific
   sanitization, not a generally reusable utility.

   ```python
   _AUTO_CLOSE_KEYWORDS_RE = re.compile(
       r"(?i)\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\b\s*:?\s+"
       r"(?=(?:[\w.-]+/[\w.-]+)?#\d+)"
   )


   def _strip_auto_close_keywords(body: str) -> str:
       """Remove GitHub auto-close keyword+separator prefixes from ``body``.

       GitHub auto-closes the originating issue when a merged PR's body
       contains any of nine "closing keywords" (close/closes/closed,
       fix/fixes/fixed, resolve/resolves/resolved) followed by a `#N` or
       ``owner/repo#N`` reference. The Planner produces spec PR bodies;
       issue closure must route through ``daemon_runners.merge_impl_pr``
       (foreman#63). This helper strips the verb+separator while
       preserving the bare ``#N`` reference so the body still reads
       cleanly. The helper is idempotent and a no-op on bodies that
       contain no auto-close keywords.
       """
       return _AUTO_CLOSE_KEYWORDS_RE.sub("", body)
   ```

   The regex uses a lookahead (`(?=...#\d+)`) so the issue reference
   itself is preserved — only the keyword + optional colon + whitespace
   is consumed.

2. Add unit tests for `_strip_auto_close_keywords` in
   `packages/foreman/tests/test_roles_planner.py`. Place them under a
   new section header `# _strip_auto_close_keywords` near the top of
   the file (before the `# parse_issue_url` section, or right after
   it — either works; pick the spot that reads cleanly). Cover at
   minimum these inputs as a `pytest.mark.parametrize` table:

   | input | expected |
   |---|---|
   | `"Closes #42"` | `"#42"` |
   | `"closes #42"` | `"#42"` |
   | `"CLOSED #42"` | `"#42"` |
   | `"Fixes #42"` | `"#42"` |
   | `"fix #42"` | `"#42"` |
   | `"FIXED #42"` | `"#42"` |
   | `"Resolves #42"` | `"#42"` |
   | `"resolve #42"` | `"#42"` |
   | `"RESOLVED #42"` | `"#42"` |
   | `"Closes: #42"` | `"#42"` |
   | `"Closes jeffrichley/foreman#42"` | `"jeffrichley/foreman#42"` |
   | `"closes #42, fixes #43"` | `"#42, #43"` |
   | `"See #42 for context."` | `"See #42 for context."` |
   | `"foreclosed by #42"` | `"foreclosed by #42"` |
   | `""` | `""` |

   The "See #42" and "foreclosed" rows are negative tests: bare
   references and substrings-of-other-words must survive. (The `\b`
   word boundary in the regex handles the "foreclosed" case — there's
   no word boundary between `foreclose` and `d` followed by space,
   sorry that's wrong: `foreclosed by #42` — `closed` is at a word
   boundary because `e` and `d` are word chars, but `forecLOSED` ends
   with `d` followed by space, so `\bclosed\b` would NOT match because
   there's no word boundary between `fore` and `closed`. Verify by
   running the test; if the assertion fails, narrow the regex or drop
   the row.)

3. Add the integration test
   `test_run_planner_strips_auto_close_keywords_from_pr_body` to
   `packages/foreman/tests/test_roles_planner.py`. Model it on the
   existing `test_run_planner_dispatches_and_advances_label`
   (lines 219-275). Differences:

   - Override `pr_body` in `_make_llm_output`:
     ```python
     fake_provider.run_agent = AsyncMock(
         return_value=_make_llm_output(
             pr_body="Drafted SSML support spec. Closes #42. See spec doc."
         )
     )
     ```
   - Capture the `body` kwarg arriving at
     `_FakeHostProvider.open_pull_request` by extending the fake to
     record `last_open_pr_body: str | None = None` and assigning
     `self.last_open_pr_body = body` in `open_pull_request`. Alternative:
     wrap `open_pull_request` with a side-effect-only `MagicMock`. Pick
     the former — it stays consistent with the rest of `_FakeHostProvider`.
   - Assertion:
     ```python
     import re

     assert fake_host.last_open_pr_body is not None
     assert not re.search(
         r"(?i)\b(close[sd]?|fix(?:es|ed)?|resolve[sd]?)\b\s*:?\s+"
         r"(?:[\w.-]+/[\w.-]+)?#\d+",
         fake_host.last_open_pr_body,
     )
     # And the bare reference survives:
     assert "#42" in fake_host.last_open_pr_body
     # The audit-log copy still has the original LLM output:
     assert "Closes #42" in result.llm_output.pr_body
     ```

4. Add `test_planner_prompt_forbids_auto_close_keywords` to
   `packages/foreman/tests/test_roles_planner.py`. Implementation:

   ```python
   from foreman.prompts import load_role_prompt


   def test_planner_prompt_forbids_auto_close_keywords() -> None:
       """The Planner system prompt MUST explicitly forbid GitHub
       auto-close keywords in pr_body. Without this guardrail the LLM
       writes `Closes #N`, which auto-closes the originating issue at
       spec-merge time — short-circuiting the loop's close-out gate
       that lives in ``daemon_runners.merge_impl_pr`` (foreman#63).
       """
       prompt = load_role_prompt("planner")
       assert "pr_body_guardrails" in prompt
       # All three keyword families named, so the LLM can generalize:
       assert "Closes" in prompt
       assert "Fixes" in prompt
       assert "Resolves" in prompt
       # Rationale cites the daemon's authoritative close path:
       assert "merge_impl_pr" in prompt
   ```

5. Add the `<pr_body_guardrails>` section to
   `packages/foreman/src/foreman/prompts/planner.md`. Place it
   immediately after `<anti_overengineering>` (after line 78) and
   before `<spec_template>` (current line 80), so the rule frames the
   PR-body output before the spec-template scaffolding. Content:

   ```markdown
   <pr_body_guardrails>
   The `pr_body` you return is posted verbatim as the spec PR's GitHub
   body. GitHub auto-closes any issue referenced by a merged PR whose
   body contains a "closing keyword" + issue reference. The nine
   closing-keyword forms (case-insensitive) are:

   `close`, `closes`, `closed`,
   `fix`, `fixes`, `fixed`,
   `resolve`, `resolves`, `resolved`

   followed by `#<N>` or `owner/repo#<N>`, optionally with a colon
   separator (`Closes: #42`).

   **You MUST NOT include any of these keyword + issue-reference
   combinations in your `pr_body`.** Reference the issue plainly —
   "for issue #42", "addresses #42", "see issue #42" — never with a
   closing verb.

   Rationale: Foreman routes issue closure exclusively through the
   daemon's `merge_impl_pr` action
   (`packages/foreman/src/foreman/daemon_runners.py`), which closes
   the issue only AFTER (a) the spec PR merged, (b) the Worker
   implemented, (c) the Reviewer-on-impl approved, and (d) the impl
   PR merged. If your spec PR body contains `Closes #N`, merging the
   spec PR auto-closes the originating issue before the implementation
   exists — short-circuiting the loop's close-out gate (foreman#63).

   The Foreman runtime additionally strips matching keyword/reference
   patterns from your `pr_body` as defense in depth before opening the
   PR, but you should still write the body correctly: that strip is a
   backstop, not a license to be sloppy.
   </pr_body_guardrails>
   ```

6. Update the `<outputs>` section of
   `packages/foreman/src/foreman/prompts/planner.md` (lines 41-42) so
   the `pr_body` bullet cross-references the new section. Replace the
   current bullet:

   ```markdown
   3. **`pr_body`**: 2-4 sentences describing the spec for human PR reviewers.
      Foreman core posts this as the PR body.
   ```

   with:

   ```markdown
   3. **`pr_body`**: 2-4 sentences describing the spec for human PR
      reviewers. Foreman core posts this as the PR body. See
      `<pr_body_guardrails>` below — your `pr_body` MUST NOT contain
      GitHub auto-close keywords.
   ```

7. Wire `_strip_auto_close_keywords` into `run_planner` in
   `packages/foreman/src/foreman/roles/planner.py`. At the call site
   `host.open_pull_request(...)` (currently lines 178-184), replace
   `body=llm_output.pr_body` with
   `body=_strip_auto_close_keywords(llm_output.pr_body)`. Do NOT
   modify `llm_output.pr_body` in place — the `PlannerRunResult`
   returned at line 192 must preserve the LLM's original output for
   the audit log.

   Add a short comment above the call referencing foreman#63 so a
   future reader doesn't strip the strip as "redundant":

   ```python
   # foreman#63: strip GitHub auto-close keywords (Closes / Fixes /
   # Resolves + #N) from the PR body before opening. Issue closure
   # routes through daemon_runners.merge_impl_pr; an auto-close in the
   # spec PR's body would short-circuit that gate. Defense in depth —
   # the Planner prompt also forbids these keywords.
   pr = host.open_pull_request(
       repo_slug=actual_repo_slug,
       title=llm_output.pr_title,
       body=_strip_auto_close_keywords(llm_output.pr_body),
       base=default_branch,
       head=branch,
   )
   ```

8. Run only the new tests to confirm they pass:

   ```bash
   uv run pytest packages/foreman/tests/test_roles_planner.py -k "strip_auto_close or forbids_auto_close" -v
   ```

9. Run `just check` and confirm exit zero. Existing tests must
   continue to pass — the change is additive (one new helper call, one
   new prompt section, three new tests) and does not touch any code
   path other than the `body=` kwarg of `host.open_pull_request`.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/prompts/planner.md` | Insert a new `<pr_body_guardrails>` section after `<anti_overengineering>` listing the nine GitHub closing-keyword forms, forbidding them in `pr_body`, and explaining the foreman#63 rationale (closure routes through `daemon_runners.merge_impl_pr`). Update the `<outputs>` bullet for `pr_body` to cross-reference the new section. |
| `packages/foreman/src/foreman/roles/planner.py` | Add module-private `_AUTO_CLOSE_KEYWORDS_RE` and `_strip_auto_close_keywords(body: str) -> str` helper above `run_planner`. In `run_planner`, scrub `llm_output.pr_body` via the helper at the `host.open_pull_request` call site, without mutating `llm_output.pr_body` on the model (the audit log keeps the original). Short comment references foreman#63. |
| `packages/foreman/tests/test_roles_planner.py` | Add a parametrized unit-test table for `_strip_auto_close_keywords` covering all nine verb forms, casing variants, colon separator, cross-repo `owner/repo#N`, multi-issue lines, and negative cases (bare `#N`, substring-of-word). Add integration test `test_run_planner_strips_auto_close_keywords_from_pr_body` proving the scrub reaches `host.open_pull_request` and the audit-log copy is untouched. Extend `_FakeHostProvider` with `last_open_pr_body` capture. Add prompt-text test `test_planner_prompt_forbids_auto_close_keywords` that loads `planner.md` via `foreman.prompts.load_role_prompt` and asserts the literal markers `pr_body_guardrails`, `Closes`, `Fixes`, `Resolves`, `merge_impl_pr` are present. |

## Alternatives considered

- **Prompt-only fix: update `planner.md` with the guardrail, no
  runtime strip.** Rejected: LLM compliance with negative
  instructions is probabilistic, and the failure mode (silent issue
  closure mid-loop) is severe enough that the runtime needs a
  deterministic backstop. The issue body itself flags the strip as
  "defense in depth — same pattern as input sanitization", which
  reads as a recommendation, not a maybe.

- **Runtime-only fix: strip in `run_planner`, leave the prompt
  unchanged.** Rejected: the LLM would keep generating `Closes #N`
  bodies that the runtime then strips. The audit log still records
  the noncompliant output, downstream Reviewer prompts might see it
  in the persisted `PlannerOutput`, and future LLM upgrades would
  re-introduce the noise. Teach the LLM AND backstop it.

- **Mutate `llm_output.pr_body` in place before audit-log persistence,
  so the persisted record matches what GitHub saw.** Rejected:
  `PlannerRunResult.llm_output` is the LLM's raw output, and the
  whole point of the audit log is to record exactly what the model
  produced for replay/regression analysis. Hiding the scrub upstream
  defeats that. The scrub is a per-call transformation on the way
  out to GitHub.

- **Validate at the Pydantic layer — add a
  `@field_validator("pr_body")` to `PlannerOutput` that raises if
  auto-close keywords are present.** Rejected: this would crash the
  Planner run entirely on noncompliant output, losing the spec doc
  and the audit row. A silent-strip-with-warning is more robust for
  a pre-prod orchestrator; the prompt-text test ensures the LLM is
  taught not to produce these in the first place. Reconsider if the
  pre-prod loop stays noisy after this fix lands.

- **Lift the strip helper to a new
  `packages/foreman/src/foreman/sanitize.py` module from the start,
  on the theory other roles will eventually need it.** Rejected:
  YAGNI. The Worker's prompt doesn't write `Closes #N` today (per
  issue body) and there's no concrete need elsewhere. Co-locate the
  helper with `run_planner`; if a second caller materializes later,
  extract then.

- **Add a CI / workflow check that scans merged spec PR bodies for
  closing keywords and warns the operator.** Rejected: too late in
  the loop. By the time CI sees the merged PR, the issue is already
  closed. Prevention beats detection here.

- **Use a stricter regex tied to the specific issue number (e.g.,
  `Closes #<issue_number>` only).** Rejected: the LLM might also
  include `Fixes #<unrelated>` or close sibling issues, and the
  blanket "no auto-close keywords at all in spec PR bodies" rule is
  easier to teach, easier to test, and matches the issue's
  recommended regex shape.

## Open questions

(none — the failure mode is reproduced (spec PR #60), the fix is
mechanically simple (one prompt section, one helper, one call-site
edit), the regex covers the documented GitHub keyword set, the test
contracts are concrete, and the audit-log preservation policy follows
the existing `PlannerRunResult` design.)

## Out of scope

- Updating the Worker's prompt (`packages/foreman/src/foreman/prompts/worker.md`)
  or `worker.py` to add a parallel guardrail. The issue explicitly
  marks this out of scope: the Worker already omits `Closes #N` from
  impl PR bodies today, and we agreed that is the correct shape. If
  the Worker ever regresses, that's a separate ticket.

- Migrating already-auto-closed issues whose spec PRs landed with
  `Closes #N`. The issue says manual re-open is fine for the handful
  in flight; no scripted migration needed.

- Extending the strip to other GitHub keyword-driven behaviors
  (e.g., `Co-Authored-By` trailers, `Reviewed-by` lines). Only
  auto-close is in scope.

- Lifting `_strip_auto_close_keywords` into a shared sanitization
  module. Stay co-located with `run_planner` for now; extract if a
  second caller emerges (see Alternatives).

- Adding a real-engine integration test (Anthropic API + real GitHub)
  asserting the same. The existing `real_engine`-marked tests stay
  out of this ticket's scope; the integration test against the fake
  host provider is the right level for the regression pin.

- Reworking the daemon's close-out semantics or the
  `merge_impl_pr` → `close_issue` path. That path is correct today
  (`packages/foreman/src/foreman/daemon_runners.py:250`); this
  ticket only stops the Planner from racing it.

- Adding a Pydantic `field_validator` on `PlannerOutput.pr_body`.
  See Alternatives considered.

- Updating `docs/superpowers/specs/2026-06-01-foreman-daemon-design.md`
  or `foreman-v1-architectural-spec.md` to call out the strip step.
  The behavior is documented in the prompt and the `roles/planner.py`
  comment; doc-spec updates can fold in next time those files are
  edited.
