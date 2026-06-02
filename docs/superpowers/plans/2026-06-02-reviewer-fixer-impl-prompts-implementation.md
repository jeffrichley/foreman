# Reviewer-on-impl + Fixer-on-impl Prompt Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-target prompt routing so the Reviewer reviewing an impl PR uses an impl-framed prompt, and the Fixer fixing an impl PR uses an impl-framed prompt + accepts the `foreman:impl-fix` entry label. Closes (conceptually) foreman#78 and foreman#79.

**Architecture:** New `reviewer_impl.md` and `fixer_impl.md` prompt files. Loader extension `load_role_prompt(role, *, target=None)` owns the `(role, target) → filename` convention with graceful fallback. Role functions branch their precondition gates on the existing `target` kwarg using `_*_ENTRY_LABEL_BY_TARGET` constants.

**Tech Stack:** Python 3.12, pytest with parametrize, uv workspace, conventional commits.

**Repo state:** Branch `fix/reviewer-fixer-impl-prompts` already created from `main`. Spec doc committed at `docs/superpowers/specs/2026-06-02-reviewer-fixer-impl-prompts-design.md`. Working directory is `e:/workspaces/ai/agents/foreman/`. All 508 existing tests pass at branch creation.

**Commit attribution:** Local git config is `user.name=wrenrichley`, `user.email=288997060+wrenrichley@users.noreply.github.com` (carry over per the established convention).

---

## File Structure

Files this plan creates or modifies:

**Create:**
- `packages/foreman/src/foreman/prompts/reviewer_impl.md` — impl-PR-framed Reviewer system prompt
- `packages/foreman/src/foreman/prompts/fixer_impl.md` — impl-PR-framed Fixer system prompt

**Modify:**
- `packages/foreman/src/foreman/prompts/__init__.py` — extend `load_role_prompt` and `compose_role_prompt` with `target` kwarg + fallback logic
- `packages/foreman/src/foreman/roles/reviewer.py` — add `_REVIEWER_ENTRY_LABEL_BY_TARGET` constant + `_REVIEWER_SUPERPOWERS_BY_TARGET` constant; thread `target` through `_load_reviewer_prompt`; replace inline `_LABEL_*_REVIEW` branching with the constant lookup
- `packages/foreman/src/foreman/roles/fixer.py` — add `_FIXER_ENTRY_LABEL_BY_TARGET` constant + `_FIXER_SUPERPOWERS_BY_TARGET` constant; thread `target` through `_load_fixer_prompt`; replace `_LABEL_SPEC_FIX` precondition with target-driven check
- `packages/foreman/tests/test_prompts.py` — add tests for `target=` routing + fallback + impl-file presence
- `packages/foreman/tests/test_roles_reviewer.py` — add target/label parametrized precondition tests + assertion that impl-target loads `reviewer_impl.md`'s signature
- `packages/foreman/tests/test_roles_fixer.py` — add target/label parametrized precondition tests + assertion that impl-target loads `fixer_impl.md`'s signature

---

## Pre-flight verification

- [ ] **Step 0.1: Verify branch and baseline tests**

Run from `e:/workspaces/ai/agents/foreman/`:

```bash
git rev-parse --abbrev-ref HEAD
```

Expected: `fix/reviewer-fixer-impl-prompts`

```bash
uv run --no-sync pytest packages/foreman/tests -q
```

Expected: `508 passed` (or close to it — count may have drifted; the point is all green, zero failures).

If the count differs or anything fails, stop and resolve before proceeding.

---

## Task 1: Extend the prompt loader with target-aware routing

**Files:**
- Modify: `packages/foreman/src/foreman/prompts/__init__.py`
- Test: `packages/foreman/tests/test_prompts.py`

The loader owns the `(role, target) → filename` convention. When `target == "impl_pr"`, try `<role>_impl.md` first; fall back to `<role>.md` if the impl variant doesn't exist (so Planner / Worker without impl variants keep working). `compose_role_prompt` forwards the kwarg.

- [ ] **Step 1.1: Write the failing tests in test_prompts.py**

Append these test functions to the END of `packages/foreman/tests/test_prompts.py`:

```python
# ----------------------------------------------------------------------
# Target-aware routing — foreman#78 + #79
#
# The loader gains an optional ``target`` kwarg that picks
# ``<role>_impl.md`` for ``target="impl_pr"`` when that file exists,
# and falls back to ``<role>.md`` otherwise. Roles without an impl
# variant (Planner, Worker) keep working unchanged.
# ----------------------------------------------------------------------


def test_load_role_prompt_default_target_loads_canonical_file() -> None:
    """``target=None`` MUST keep today's behavior — the canonical
    ``<role>.md`` is loaded with no fallback consulted. This is the
    back-compat surface every existing call site relies on."""
    content = load_role_prompt("reviewer")
    assert content.strip(), "reviewer.md should load with default target"


def test_load_role_prompt_spec_target_loads_canonical_file() -> None:
    """``target="spec_pr"`` is equivalent to ``target=None`` — the
    canonical ``<role>.md`` is the spec-side prompt. The kwarg is
    informational at the call site, the loader treats spec as the
    default file name."""
    default_content = load_role_prompt("reviewer")
    spec_content = load_role_prompt("reviewer", target="spec_pr")
    assert spec_content == default_content


def test_load_role_prompt_impl_target_loads_impl_file_when_present() -> None:
    """``target="impl_pr"`` MUST load ``<role>_impl.md`` when that
    file exists. This is the routing the bug fix hinges on — without
    it, the Reviewer-on-impl reads the spec prompt."""
    impl_content = load_role_prompt("reviewer", target="impl_pr")
    assert impl_content.strip(), "reviewer_impl.md should load for impl_pr target"
    # Sanity: impl content differs from spec content (otherwise the
    # routing is a no-op and the bug is unfixed).
    spec_content = load_role_prompt("reviewer", target="spec_pr")
    assert impl_content != spec_content, (
        "reviewer_impl.md must differ from reviewer.md, or the routing "
        "isn't actually doing anything for the bug fix"
    )


def test_load_role_prompt_impl_target_falls_back_to_canonical_when_no_impl_file() -> None:
    """``target="impl_pr"`` MUST fall back gracefully to ``<role>.md``
    when ``<role>_impl.md`` doesn't exist. Roles without impl variants
    (Planner today) MUST keep working when callers pass target."""
    # Planner has no planner_impl.md; impl target should fall back.
    impl_content = load_role_prompt("planner", target="impl_pr")
    default_content = load_role_prompt("planner")
    assert impl_content == default_content


def test_load_role_prompt_unknown_target_falls_back_to_canonical() -> None:
    """An unrecognized target string (typo, future addition) MUST NOT
    raise — it falls back to the canonical role file. This keeps the
    routing forgiving at call sites that pass dynamic strings; the
    role-side precondition gate is where wrong-target errors should
    surface, not in the prompt loader."""
    content = load_role_prompt("reviewer", target="unknown_target")
    default_content = load_role_prompt("reviewer")
    assert content == default_content


def test_compose_role_prompt_forwards_target_to_loader() -> None:
    """``compose_role_prompt(target=...)`` MUST forward target to
    ``load_role_prompt``. Without this, the role's contract layer
    silently falls back to the spec prompt even when the caller asked
    for impl. This is the seam that bug-fixed roles consume."""
    composed = compose_role_prompt(
        role="reviewer", target="impl_pr", superpowers=[]
    )
    impl_role_content = load_role_prompt("reviewer", target="impl_pr")
    spec_role_content = load_role_prompt("reviewer", target="spec_pr")
    # The role-contract layer of the composed prompt should match the
    # impl variant, not the spec variant.
    assert impl_role_content in composed
    assert spec_role_content not in composed or impl_role_content == spec_role_content


@pytest.mark.parametrize("impl_role", ["reviewer", "fixer"])
def test_impl_prompt_file_exists_for_target_roles(impl_role: str) -> None:
    """Both Reviewer and Fixer MUST have an impl-variant prompt file.
    This is the resource-packaging guard: forgetting either file means
    the daemon's RUN_REVIEWER_IMPL / RUN_FIXER_IMPL silently falls
    back to the spec prompt — same bug, different symptom."""
    impl_path = (
        resources.files("foreman.prompts")
        .joinpath(f"{impl_role}_impl.md")
    )
    assert impl_path.is_file(), (
        f"{impl_role}_impl.md must exist in packages/foreman/src/foreman/"
        "prompts/ — the impl-target loader resolves to that path"
    )
    content = impl_path.read_text(encoding="utf-8")
    assert content.strip(), f"{impl_role}_impl.md must not be empty"
```

- [ ] **Step 1.2: Run the new tests to verify they fail**

Run:

```bash
uv run --no-sync pytest packages/foreman/tests/test_prompts.py -v -k "target or impl_prompt_file_exists"
```

Expected: 7 tests collected (the 6 explicit tests + 2 parametrize cases for `test_impl_prompt_file_exists_for_target_roles`). Most FAIL with `TypeError: load_role_prompt() got an unexpected keyword argument 'target'` — except `test_load_role_prompt_default_target_loads_canonical_file` which should PASS even now. Several others fail because the impl prompt files don't exist yet. This is correct: failing tests prove the feature isn't built yet.

- [ ] **Step 1.3: Extend load_role_prompt and compose_role_prompt**

Open `packages/foreman/src/foreman/prompts/__init__.py`. Replace the existing `load_role_prompt` function with the version below. Also update `compose_role_prompt` to accept and forward `target`.

Replace this block:

```python
def load_role_prompt(role: str) -> str:
    """Read the Foreman-specific role prompt by role name."""
    return (
        resources.files(_PROMPTS_ROOT)
        .joinpath(f"{role}.md")
        .read_text(encoding="utf-8")
    )
```

with:

```python
def load_role_prompt(role: str, *, target: str | None = None) -> str:
    """Read the Foreman-specific role prompt by role name.

    When ``target == "impl_pr"`` AND a target-specific prompt file
    exists at ``<role>_impl.md``, return that. Otherwise fall back
    to the canonical ``<role>.md``.

    The mapping from ``(role, target)`` to filename is a fact about
    the prompt directory layout — it lives in this loader, not in
    each role's call site. Roles that don't have a target-specific
    variant (Planner, Worker today) automatically fall back; no
    branching at the role layer.

    Unrecognized target values fall back to ``<role>.md`` rather than
    raising — wrong-target errors should surface at the role's
    precondition gate, not silently here.
    """
    if target == "impl_pr":
        impl_path = resources.files(_PROMPTS_ROOT).joinpath(f"{role}_impl.md")
        if impl_path.is_file():
            return impl_path.read_text(encoding="utf-8")
    return (
        resources.files(_PROMPTS_ROOT)
        .joinpath(f"{role}.md")
        .read_text(encoding="utf-8")
    )
```

Then replace the existing `compose_role_prompt` signature and body. Find:

```python
def compose_role_prompt(*, role: str, superpowers: list[str]) -> str:
    """Compose a role's full system prompt with three layers:

    1. **Adapter preamble** (:data:`_ADAPTER_PREAMBLE`) — read first,
       tells the LLM how to interpret cross-references and missing
       tools in the layers that follow.
    2. **Vendored superpowers skills**, each wrapped with a header
       (:func:`_wrap_skill`) that explicitly names the block as that
       skill so downstream references resolve to the in-prompt content
       instead of stalling on a missing Skill tool.
    3. **Foreman role contract** — the role's labels, branch
       conventions, structured output schema.

    ``superpowers`` is the ordered list of vendored skill names to inline
    between the preamble and the role contract. Order matters — the role
    LLM reads top-to-bottom, so put the most foundational skill first
    (e.g. ``test-driven-development`` before ``executing-plans``).

    Layers are separated by ``---`` so the LLM sees clear section
    boundaries.
    """
    parts: list[str] = [_ADAPTER_PREAMBLE]
    parts.extend(_wrap_skill(name) for name in superpowers)
    parts.append(load_role_prompt(role))
    return "\n\n---\n\n".join(parts)
```

with:

```python
def compose_role_prompt(
    *,
    role: str,
    superpowers: list[str],
    target: str | None = None,
) -> str:
    """Compose a role's full system prompt with three layers:

    1. **Adapter preamble** (:data:`_ADAPTER_PREAMBLE`) — read first,
       tells the LLM how to interpret cross-references and missing
       tools in the layers that follow.
    2. **Vendored superpowers skills**, each wrapped with a header
       (:func:`_wrap_skill`) that explicitly names the block as that
       skill so downstream references resolve to the in-prompt content
       instead of stalling on a missing Skill tool.
    3. **Foreman role contract** — the role's labels, branch
       conventions, structured output schema. ``target`` is forwarded
       to :func:`load_role_prompt` so target-specific role prompts
       (e.g. ``reviewer_impl.md``) are loaded for the impl-side
       variants — see foreman#78 / foreman#79.

    ``superpowers`` is the ordered list of vendored skill names to inline
    between the preamble and the role contract. Order matters — the role
    LLM reads top-to-bottom, so put the most foundational skill first
    (e.g. ``test-driven-development`` before ``executing-plans``).

    Layers are separated by ``---`` so the LLM sees clear section
    boundaries.
    """
    parts: list[str] = [_ADAPTER_PREAMBLE]
    parts.extend(_wrap_skill(name) for name in superpowers)
    parts.append(load_role_prompt(role, target=target))
    return "\n\n---\n\n".join(parts)
```

- [ ] **Step 1.4: Run the loader-only tests (impl file tests will still fail until Tasks 2/3 land)**

Run:

```bash
uv run --no-sync pytest packages/foreman/tests/test_prompts.py -v -k "target and not impl_prompt_file_exists"
```

Expected: All 6 `*_target_*` tests PASS — `test_load_role_prompt_impl_target_loads_impl_file_when_present` should FAIL with `FileNotFoundError` until Task 2 lands the `reviewer_impl.md` file, but it MAY pass if the test only inspects content equality with the fallback. Read carefully: if it fails because the impl content equals spec content, that's expected at this point — Task 2/3 fix it.

Specifically:
- `test_load_role_prompt_default_target_loads_canonical_file`: PASS
- `test_load_role_prompt_spec_target_loads_canonical_file`: PASS
- `test_load_role_prompt_impl_target_loads_impl_file_when_present`: FAIL (impl file missing → falls back → equals spec → assert fires). EXPECTED.
- `test_load_role_prompt_impl_target_falls_back_to_canonical_when_no_impl_file`: PASS (planner has no impl, fallback works)
- `test_load_role_prompt_unknown_target_falls_back_to_canonical`: PASS
- `test_compose_role_prompt_forwards_target_to_loader`: FAIL (same reason as above). EXPECTED.

The remaining 2 `test_impl_prompt_file_exists_for_target_roles` cases also FAIL (files don't exist). EXPECTED — Tasks 2 and 3 close them.

- [ ] **Step 1.5: Run the existing prompts tests to confirm no regression**

Run:

```bash
uv run --no-sync pytest packages/foreman/tests/test_prompts.py -v -k "not target and not impl_prompt_file_exists"
```

Expected: all existing tests still PASS (the function-signature change is backwards-compatible — `target` defaults to `None` and the kwarg-only marker prevents positional confusion).

- [ ] **Step 1.6: Commit**

```bash
git add packages/foreman/src/foreman/prompts/__init__.py packages/foreman/tests/test_prompts.py
git commit -m "feat(prompts): add target kwarg to load_role_prompt for per-target routing (#78, #79)

Adds optional target='impl_pr' kwarg to load_role_prompt and
compose_role_prompt. When set, the loader tries <role>_impl.md
first and falls back to <role>.md if the impl variant doesn't
exist. Unrecognized target values fall back silently — wrong-target
errors should surface at the role's precondition gate, not in the
loader. Tests pin: default behavior, spec_pr equivalence, impl_pr
fallback when no impl file, unknown-target fallback, and
compose_role_prompt forwarding. Impl-file-exists tests intentionally
fail at this commit — Tasks 2 and 3 land the files."
```

---

## Task 2: Write reviewer_impl.md

**Files:**
- Create: `packages/foreman/src/foreman/prompts/reviewer_impl.md`

The impl-side Reviewer prompt frames the role for reviewing CODE (the Worker's output) against the merged spec doc — not for reviewing spec docs. The structured output schema is the same; the WHAT-TO-REVIEW and WHAT-TO-FLAG differ.

- [ ] **Step 2.1: Read the existing reviewer.md to understand its shape**

Run:

```bash
wc -l packages/foreman/src/foreman/prompts/reviewer.md
```

Note the line count for reference. Open the file to read the section structure (role identity, output schema, what-to-flag rules). The impl variant keeps the same identity + schema but rewrites the domain-specific guidance.

- [ ] **Step 2.2: Create reviewer_impl.md**

Create `packages/foreman/src/foreman/prompts/reviewer_impl.md` with this content:

```markdown
# Foreman Reviewer role — impl-PR variant

You are the Foreman Reviewer reviewing an **implementation pull
request** opened by the Worker. Your job: judge whether the impl
delivers what the merged spec doc on `main` promised, and whether
the code is healthy enough to land.

This is the impl-side variant. You are NOT reviewing a spec doc.
The PR diff containing source files, tests, and prompt/config
files is the EXPECTED shape — not a violation.

## What the PR diff should look like

Impl PRs from the Worker typically contain:

- Source files (`packages/foreman/src/foreman/**/*.py`, prompt
  markdown, role config) — the actual implementation.
- Test files (`packages/foreman/tests/test_*.py`) — coverage for
  the implementation.
- Sometimes documentation files when the spec called for them.

Branch name: `foreman/impl-<N>`. PR title: conventional commit
(`fix(scope):`, `feat(scope):`, etc.) reflecting the impl
content. PR body opens with phrasing like `Implements #<N>` or
`Addresses #<N>` (NEVER `Closes #<N>` — that auto-closes the
issue and short-circuits the merge_impl_pr close-out gate per
foreman#63).

A PR diff containing ONLY a spec doc is a violation. A PR diff
containing source files and tests is CORRECT for an impl PR.
The spec-side Reviewer's "spec PR should contain only the spec
doc" rule does NOT apply here.

## The reference contract

The spec doc the Worker is implementing already merged to `main`
before the Worker ran. Read it first:

`docs/superpowers/specs/foreman-issue-<N>-spec.md`

That document is the contract this PR must satisfy. Specifically:

- **Sub-requests** section: each enumerated sub-request should
  map to a file change in this PR's diff. Missing sub-requests
  are critical; extra sub-requests beyond what the spec enumerated
  are scope drift.
- **File-level changes** section: the spec lists which files
  should change. Files in this PR outside that list are scope
  drift unless they're tests for the listed files.
- **Acceptance criteria** section: each criterion should be
  testable AND tested in this PR's diff.

## What to flag

**Critical (blocking):**

- Missing implementation for a spec sub-request.
- Files changed outside the spec's "File-level changes" section
  (unless they're tests for in-scope files).
- Failing tests introduced by this PR (the Worker's stats should
  show `new_failures_count == 0`). If `new_failures_count > 0`,
  the impl regressed something.
- Tests deleted or weakened to make CI green — cardinal sin.
- PR title doesn't match the conventional commit convention.
- PR body contains a GitHub auto-close keyword (`Closes`/`Fixes`/
  `Resolves`/etc. + `#<N>` reference) — issue closure routes
  through `daemon_runners.merge_impl_pr`, not via PR body
  auto-close.

**Important (non-blocking):**

- Sub-request implemented but missing test coverage for an
  acceptance criterion.
- Code that diverges from the spec's documented approach without
  rationale in the PR body or commit message.
- Conventional-commit scope doesn't match the impl content
  (e.g., `feat(planner):` on a PR that only touches the Reviewer).

**NOT findings (do not flag):**

- "PR diff has the wrong shape for a spec PR" — this is an IMPL
  PR. Source files and tests in the diff are correct.
- "PR title uses `fix(scope):` instead of `docs(spec):`" — the
  impl convention uses standard commit scopes, not `docs(spec):`.
- "PR body uses `Implements #N` instead of pointing at the spec
  doc" — `Implements` is correct for impl PRs and does NOT
  auto-close the issue.
- "Branch is `foreman/impl-<N>` instead of `foreman/issue-<N>`" —
  impl PRs use the `impl-` prefix per the worktree convention.

## How to verify your claims

Before raising any finding, do the empirical work:

1. **Read the spec doc** at `docs/superpowers/specs/foreman-issue-<N>-spec.md`
   on the worktree. That's the contract.
2. **Read the changed files** in the PR diff. Look at the actual
   code, not just the file names.
3. **Trace each sub-request** in the spec to its implementation
   location in the diff. If a sub-request has no corresponding
   diff change, that's a missing-implementation finding.
4. **Inspect the tests** added in the diff. For each spec
   acceptance criterion, identify the test(s) that pin it. If a
   criterion has no test, that's a missing-test finding.
5. **Check the worker stats line** in the PR description or
   commit body for `baseline_failures_count` and
   `new_failures_count`. Non-zero `new_failures_count` is a
   regression finding.

Empirical verification, not vibes. The Reviewer that says "this
file looks wrong" without naming the line + the spec rule it
violates is the Reviewer the Fixer cannot act on.

## Output

Same structured output schema as the spec-side Reviewer
(`ReviewerOutput`): `outcome` (`"clean"` or `"needs_fix"`),
`confidence`, `findings` list. Each finding has severity,
target (file + line range), issue description, and `needed`
prescription. Same marker-fenced JSON block for the Fixer to
recover from your posted review body.

For impl PRs, set `outcome="clean"` when:

- All spec sub-requests have corresponding diff changes
- All acceptance criteria have tests
- `new_failures_count == 0`
- No critical findings exist (important findings allowed; they
  go to the Fixer but don't block landing)

Set `outcome="needs_fix"` when any critical finding exists.

## Identity

You are the Foreman Reviewer bot
(`<your-installation-bot>`). The Foreman role contract applies:
label vocabulary (`foreman:impl-review`, `foreman:ready-for-merge`,
`foreman:impl-fix`), branch conventions
(`foreman/impl-<N>`), structured output schema, and identity
model are not negotiable.
```

- [ ] **Step 2.3: Verify the impl-content-exists test now passes**

Run:

```bash
uv run --no-sync pytest packages/foreman/tests/test_prompts.py::test_impl_prompt_file_exists_for_target_roles -v
```

Expected: the `reviewer` parametrize case PASSES; the `fixer` case still FAILS (Task 3 lands fixer_impl.md). The other target tests for the reviewer impl path should now pass:

```bash
uv run --no-sync pytest packages/foreman/tests/test_prompts.py::test_load_role_prompt_impl_target_loads_impl_file_when_present -v
uv run --no-sync pytest packages/foreman/tests/test_prompts.py::test_compose_role_prompt_forwards_target_to_loader -v
```

Both should now PASS.

- [ ] **Step 2.4: Commit**

```bash
git add packages/foreman/src/foreman/prompts/reviewer_impl.md
git commit -m "feat(prompts): add reviewer_impl.md for Reviewer-on-impl PRs (#78)

Reviewer-on-impl now has its own framing prompt: expects impl-shaped
PR diffs (source + tests, not a spec doc), references the merged
spec doc as the correctness contract, explicitly names what NOT to
flag (Implements #N body, fix(scope) title, foreman/impl-N branch
— the false-positive 'wrong shape' findings the spec prompt
produced on PR #77 today). Routes via load_role_prompt(target='impl_pr')
once roles/reviewer.py is wired in Task 4."
```

---

## Task 3: Write fixer_impl.md

**Files:**
- Create: `packages/foreman/src/foreman/prompts/fixer_impl.md`

The impl-side Fixer prompt frames the role for fixing CODE in an impl PR in response to Reviewer-on-impl findings. Same structured output schema as the spec-side Fixer; the WHAT-TO-FIX differs.

- [ ] **Step 3.1: Create fixer_impl.md**

Create `packages/foreman/src/foreman/prompts/fixer_impl.md` with this content:

```markdown
# Foreman Fixer role — impl-PR variant

You are the Foreman Fixer applying fixes to an **implementation
pull request** in response to Reviewer-on-impl findings.

This is the impl-side variant. The artifact you are fixing is
CODE (source files, tests, configuration), NOT a spec doc. Your
edits go on the impl branch (`foreman/impl-<N>`) — never the spec
branch.

## What you are fixing

The Reviewer-on-impl posted a review with structured findings.
Each finding identifies:

- **severity** (`critical`, `important`, `minor`)
- **target** (file + line range)
- **issue** (what's wrong)
- **needed** (what to do about it)

The findings are embedded as a marker-fenced JSON block in the
review body:

```
<!-- foreman:findings:begin -->
<details>
<summary>Structured findings (for Fixer)</summary>

```json
[ { ... finding ... }, ... ]
```

</details>
<!-- foreman:findings:end -->
```

For each finding, decide:

- If the fix changes runtime behavior (most cases): **write a
  failing test first**, then change the code, then verify the
  test passes. This is non-negotiable for impl-side fixes.
- If the fix is purely structural (move a function, rename a
  variable that's not externally observable, fix a typo in a
  comment): tests aren't required — but verify nothing was broken.

## Hard rules

1. **Never delete or weaken tests to make CI pass.** This is
   the cardinal sin of impl Fixing. If a test you didn't write
   is failing, the right answer is to fix the code so the test
   passes — not delete the test. If you genuinely believe the
   test is wrong, surface that as a `skipped_fix` in your
   structured output with a clear explanation; do NOT silently
   remove it.

2. **Preserve scope.** Fix only what the Reviewer flagged. The
   Reviewer's findings are the authoritative list. If you notice
   an unrelated problem while fixing, surface it as a follow-up
   note in structured output — do not extend the PR with
   drive-by changes.

3. **Verify before committing.** After each fix, run the project's
   check command (`just check` or whatever the project config
   specifies). If the check fails, fix it BEFORE committing. A
   commit with red CI is a worse state than no commit.

4. **One commit per finding when practical.** Small atomic commits
   make the Reviewer's re-review cheaper. If multiple findings
   share a single fix surface (e.g., the same function needs two
   adjustments), one commit is fine — note both findings in the
   commit message.

## Failure mode handling

If a finding cannot be addressed:

- Reviewer's finding looks empirically wrong (e.g., it flags a
  line that doesn't exist, or claims a bug you can't reproduce):
  surface as `skipped_fix` with `reason="reviewer_finding_invalid"`
  and a brief explanation. Do NOT silently skip.

- Finding is genuinely ambiguous (e.g., "add validation" without
  saying what to validate): surface as `skipped_fix` with
  `reason="finding_too_vague"`. Do NOT guess.

- Finding requires changes outside the impl branch (e.g., needs
  a spec amendment): surface as `skipped_fix` with
  `reason="needs_spec_change"`. The autonomous loop's escalation
  path handles this via `foreman:needs-help`.

In all these cases, you continue addressing the remaining
findings; one un-addressable finding doesn't stop the rest.

## Output

Same structured output schema as the spec-side Fixer
(`FixerOutput`): `outcome` (`"fixed"`, `"partial"`, `"failed"`),
`addressed_findings` list, `skipped_fixes` list with reasons,
`confidence`. Same per-episode counter discipline — the Fixer
gets up to `project.max_fix_attempts` tries.

For impl PRs, set `outcome="fixed"` when EVERY critical finding
is addressed AND the project check command passes. Important and
minor findings can be skipped with explanations and still produce
`outcome="fixed"` if all criticals are handled and CI is green.

Set `outcome="partial"` when some criticals are skipped (with
reasons in `skipped_fixes`).

Set `outcome="failed"` when you can't make the project check
pass — that's an escalation signal.

## Identity

You are the Foreman Fixer bot. The Foreman role contract applies:
label vocabulary (`foreman:impl-fix`, `foreman:impl-review`),
branch conventions (`foreman/impl-<N>`), structured output schema,
and identity model are not negotiable.
```

- [ ] **Step 3.2: Verify the impl-content-exists test now passes for fixer too**

Run:

```bash
uv run --no-sync pytest packages/foreman/tests/test_prompts.py::test_impl_prompt_file_exists_for_target_roles -v
```

Expected: both `reviewer` and `fixer` parametrize cases PASS.

- [ ] **Step 3.3: Run all prompts tests to confirm full green**

Run:

```bash
uv run --no-sync pytest packages/foreman/tests/test_prompts.py -v
```

Expected: all tests PASS. The `*_target_*` tests are green now that both impl files exist.

- [ ] **Step 3.4: Commit**

```bash
git add packages/foreman/src/foreman/prompts/fixer_impl.md
git commit -m "feat(prompts): add fixer_impl.md for Fixer-on-impl PRs (#79)

Fixer-on-impl now has its own framing prompt: fixes CODE on the
impl branch (foreman/impl-N), not spec docs. Hard rules: never
delete/weaken tests, preserve scope, verify before committing,
one commit per finding. Failure-mode handling with structured
skipped_fix reasons (invalid finding, vague finding, needs spec
change). Routes via load_role_prompt(target='impl_pr') once
roles/fixer.py is wired in Task 5."
```

---

## Task 4: Wire roles/reviewer.py to target-aware prompt + label

**Files:**
- Modify: `packages/foreman/src/foreman/roles/reviewer.py`
- Test: `packages/foreman/tests/test_roles_reviewer.py`

The Reviewer already accepts a `target` kwarg (per foreman#41). Today it uses the kwarg to choose label-transition behavior; we extend it to also pick the prompt file via a target-keyed constant and a target-aware `_load_reviewer_prompt`. The existing in-place `if target == "impl_pr"` branch for label selection (lines 313-320) gets refactored to a constant lookup for symmetry.

- [ ] **Step 4.1: Read the current reviewer.py prompt loader to understand its shape**

Look at `packages/foreman/src/foreman/roles/reviewer.py` lines 144-160 — `_load_reviewer_prompt()`. It currently calls `compose_role_prompt(role="reviewer", superpowers=["requesting-code-review"])`. We extend it to take `target` and choose the superpowers list accordingly.

- [ ] **Step 4.2: Write the failing tests in test_roles_reviewer.py**

Append to `packages/foreman/tests/test_roles_reviewer.py`:

```python
# ----------------------------------------------------------------------
# Per-target prompt routing — foreman#78
#
# The Reviewer's ``target`` kwarg now drives both the entry-label
# precondition AND the prompt loaded. Today's bug (foreman#78) was
# that ``target="impl_pr"`` correctly drove label transitions but
# silently kept the spec prompt — so impl PRs got reviewed as if
# they were spec PRs. These tests pin both halves of the routing.
# ----------------------------------------------------------------------


def test_reviewer_entry_label_by_target_mapping_is_complete() -> None:
    """The mapping covers the two valid targets and nothing else.
    A future target string (e.g. ``"docs_pr"``) must require an
    explicit addition to the mapping — not silently fall back to
    spec behavior."""
    from foreman.roles.reviewer import _REVIEWER_ENTRY_LABEL_BY_TARGET

    assert _REVIEWER_ENTRY_LABEL_BY_TARGET == {
        "spec_pr": "foreman:spec-review",
        "impl_pr": "foreman:impl-review",
    }


def test_reviewer_superpowers_by_target_mapping_is_complete() -> None:
    """Each target gets its own superpowers composition list. The
    impl variant adds verification-before-completion and
    test-driven-development (Worker's discipline patterns the
    impl-PR Reviewer needs to enforce)."""
    from foreman.roles.reviewer import _REVIEWER_SUPERPOWERS_BY_TARGET

    assert _REVIEWER_SUPERPOWERS_BY_TARGET == {
        "spec_pr": ["requesting-code-review"],
        "impl_pr": [
            "requesting-code-review",
            "verification-before-completion",
            "test-driven-development",
        ],
    }


def test_load_reviewer_prompt_default_uses_spec_composition() -> None:
    """The legacy zero-arg call MUST keep working — many tests and
    the spec-side dispatcher rely on it. It returns the spec
    composition."""
    from foreman.roles.reviewer import _load_reviewer_prompt
    from foreman.prompts import compose_role_prompt

    actual = _load_reviewer_prompt()
    expected = compose_role_prompt(
        role="reviewer",
        superpowers=["requesting-code-review"],
        target="spec_pr",
    )
    assert actual == expected


def test_load_reviewer_prompt_impl_target_loads_impl_composition() -> None:
    """``target="impl_pr"`` loads ``reviewer_impl.md`` with the
    impl superpowers list. Without this, the bug fix is incomplete —
    the role function reads spec content while claiming to do impl
    review."""
    from foreman.roles.reviewer import _load_reviewer_prompt
    from foreman.prompts import compose_role_prompt

    actual = _load_reviewer_prompt(target="impl_pr")
    expected = compose_role_prompt(
        role="reviewer",
        superpowers=[
            "requesting-code-review",
            "verification-before-completion",
            "test-driven-development",
        ],
        target="impl_pr",
    )
    assert actual == expected
    # Sanity: ensure the impl composition contains impl-file content.
    # If the loader silently fell back to reviewer.md, this would fail.
    assert "impl pull request" in actual.lower() or "impl-pr variant" in actual.lower()
```

- [ ] **Step 4.3: Run the new tests to verify they fail**

Run:

```bash
uv run --no-sync pytest packages/foreman/tests/test_roles_reviewer.py -v -k "target or superpowers_by_target"
```

Expected: all 4 new tests FAIL — the constants don't exist yet (`ImportError`) and `_load_reviewer_prompt` doesn't accept `target`.

- [ ] **Step 4.4: Add the constants + extend _load_reviewer_prompt**

Open `packages/foreman/src/foreman/roles/reviewer.py`. Find the existing `_LABEL_*` constants block near the top (around line 65-72). After those constants, ADD:

```python
# foreman#78: per-target routing for the Reviewer. The role accepts
# a ``target`` kwarg (added by foreman#41) that distinguishes spec
# PRs (``foreman/issue-<N>``) from impl PRs (``foreman/impl-<N>``).
# Each target gets its own entry-label precondition and its own
# prompt composition. The mappings are intentionally explicit
# rather than computed — adding a new target later (``docs_pr``,
# ``release_pr``) requires updating the mappings deliberately, not
# silently falling back to spec behavior.
_REVIEWER_ENTRY_LABEL_BY_TARGET: dict[str, str] = {
    "spec_pr": _LABEL_SPEC_REVIEW,
    "impl_pr": _LABEL_IMPL_REVIEW,
}

_REVIEWER_SUPERPOWERS_BY_TARGET: dict[str, list[str]] = {
    # Spec-side: today's discipline — empirical code review.
    "spec_pr": ["requesting-code-review"],
    # Impl-side: same code-review discipline PLUS what-counts-as-done
    # (verification-before-completion) and TDD-shape checking
    # (test-driven-development). The impl Reviewer judges whether the
    # Worker did right, so it needs the Worker's discipline patterns
    # to know what right looks like.
    "impl_pr": [
        "requesting-code-review",
        "verification-before-completion",
        "test-driven-development",
    ],
}
```

Then replace the existing `_load_reviewer_prompt` function. Find:

```python
def _load_reviewer_prompt() -> str:
    """Load the Reviewer system prompt: vendored ``requesting-code-review``
    followed by the Foreman-specific Reviewer contract.

    Composed via :func:`foreman.prompts.compose_role_prompt` so the
    adapter preamble + per-skill wrappers from PR #43 are applied
    consistently with the other roles.
    """
    from foreman.prompts import compose_role_prompt

    return compose_role_prompt(
        role="reviewer",
        superpowers=["requesting-code-review"],
    )
```

Replace with:

```python
def _load_reviewer_prompt(target: str = "spec_pr") -> str:
    """Load the Reviewer system prompt for the given ``target``.

    ``target="spec_pr"`` (default for back-compat with existing call
    sites) loads ``reviewer.md`` composed with
    ``requesting-code-review``. ``target="impl_pr"`` loads
    ``reviewer_impl.md`` composed with the impl-side superpowers list
    (adds ``verification-before-completion`` + ``test-driven-development``).
    See ``_REVIEWER_SUPERPOWERS_BY_TARGET`` for the exact composition.

    Unknown target values fall back to the spec composition — the
    role's precondition gate is the right place to surface
    wrong-target errors, not this loader.

    Composed via :func:`foreman.prompts.compose_role_prompt` so the
    adapter preamble + per-skill wrappers from PR #43 are applied
    consistently with the other roles.
    """
    from foreman.prompts import compose_role_prompt

    superpowers = _REVIEWER_SUPERPOWERS_BY_TARGET.get(
        target, _REVIEWER_SUPERPOWERS_BY_TARGET["spec_pr"]
    )
    # Map unknown target to "spec_pr" for the prompt loader call too,
    # so the file resolution stays consistent with the superpowers list.
    safe_target = target if target in _REVIEWER_SUPERPOWERS_BY_TARGET else "spec_pr"
    return compose_role_prompt(
        role="reviewer",
        superpowers=superpowers,
        target=safe_target,
    )
```

Then refactor the existing in-place label branching (lines 313-320 in the current file) to use the constant. Find:

```python
    if target == "impl_pr":
        in_review_label = _LABEL_IMPL_REVIEW
        clean_label = _LABEL_READY_FOR_MERGE
        fix_label = _LABEL_IMPL_FIX
    else:
        in_review_label = _LABEL_SPEC_REVIEW
        clean_label = _LABEL_SPEC_READY
        fix_label = _LABEL_SPEC_FIX
```

Replace with:

```python
    in_review_label = _REVIEWER_ENTRY_LABEL_BY_TARGET[target]
    if target == "impl_pr":
        clean_label = _LABEL_READY_FOR_MERGE
        fix_label = _LABEL_IMPL_FIX
    else:
        clean_label = _LABEL_SPEC_READY
        fix_label = _LABEL_SPEC_FIX
```

(We keep the clean/fix labels inline because their target mapping has more than one value per target and isn't worth pulling into a separate constant for two cases.)

Finally, find the `system_prompt = _load_reviewer_prompt()` call (line 353) and change it to thread `target`:

```python
    system_prompt = _load_reviewer_prompt(target=target)
```

- [ ] **Step 4.5: Run the new reviewer tests to verify they pass**

Run:

```bash
uv run --no-sync pytest packages/foreman/tests/test_roles_reviewer.py -v -k "target or superpowers_by_target"
```

Expected: all 4 new tests PASS.

- [ ] **Step 4.6: Run all reviewer tests to confirm no regression**

Run:

```bash
uv run --no-sync pytest packages/foreman/tests/test_roles_reviewer.py -v
```

Expected: all tests PASS — including the existing label-transition tests. If any existing test fails because it stubbed `_load_reviewer_prompt()` with the old zero-arg signature, update it to pass `target=...` explicitly OR accept the default `"spec_pr"`.

- [ ] **Step 4.7: Commit**

```bash
git add packages/foreman/src/foreman/roles/reviewer.py packages/foreman/tests/test_roles_reviewer.py
git commit -m "fix(reviewer): per-target prompt + label routing (#78)

Adds _REVIEWER_ENTRY_LABEL_BY_TARGET and _REVIEWER_SUPERPOWERS_BY_TARGET
constants. _load_reviewer_prompt(target=...) loads reviewer_impl.md
with [requesting-code-review, verification-before-completion,
test-driven-development] for target='impl_pr', falls back to
reviewer.md with [requesting-code-review] otherwise. Refactors the
inline in_review_label branching to use the constant. Threads target
through to the prompt loader call. Closes the actual bug from
foreman#78: PR #77 was reviewed against the spec prompt's expectations
('wrong shape, where's the spec doc?') because nothing routed the
prompt selection on target."
```

---

## Task 5: Wire roles/fixer.py to target-aware prompt + label

**Files:**
- Modify: `packages/foreman/src/foreman/roles/fixer.py`
- Test: `packages/foreman/tests/test_roles_fixer.py`

Mirror of Task 4 for the Fixer. The Fixer accepts `target` (per foreman#41 — `daemon_runners.run_fixer` already passes `target="impl_pr"` for `run_fixer_impl`). Today the role function ignores it for both the precondition gate (hardcoded `_LABEL_SPEC_FIX`) and the prompt (hardcoded `_load_fixer_prompt()`).

- [ ] **Step 5.1: Write the failing tests in test_roles_fixer.py**

Append to `packages/foreman/tests/test_roles_fixer.py`:

```python
# ----------------------------------------------------------------------
# Per-target prompt + precondition routing — foreman#79
#
# The Fixer's ``target`` kwarg now drives both the entry-label
# precondition and the prompt loaded. Today's bug (foreman#79) was
# that ``target="impl_pr"`` got plumbed through DaemonRunners but
# the role function rejected the issue because ``foreman:impl-fix``
# was not in its hardcoded acceptance set (only ``foreman:spec-fix``).
# These tests pin both halves of the routing.
# ----------------------------------------------------------------------


def test_fixer_entry_label_by_target_mapping_is_complete() -> None:
    """The mapping covers the two valid targets and nothing else."""
    from foreman.roles.fixer import _FIXER_ENTRY_LABEL_BY_TARGET

    assert _FIXER_ENTRY_LABEL_BY_TARGET == {
        "spec_pr": "foreman:spec-fix",
        "impl_pr": "foreman:impl-fix",
    }


def test_fixer_superpowers_by_target_mapping_is_complete() -> None:
    """Each target gets its own superpowers composition list. The
    impl variant adds verification-before-completion and
    test-driven-development."""
    from foreman.roles.fixer import _FIXER_SUPERPOWERS_BY_TARGET

    assert _FIXER_SUPERPOWERS_BY_TARGET == {
        "spec_pr": ["receiving-code-review"],
        "impl_pr": [
            "receiving-code-review",
            "verification-before-completion",
            "test-driven-development",
        ],
    }


def test_load_fixer_prompt_default_uses_spec_composition() -> None:
    """Zero-arg call returns the spec composition — back-compat for
    existing call sites and tests."""
    from foreman.roles.fixer import _load_fixer_prompt
    from foreman.prompts import compose_role_prompt

    actual = _load_fixer_prompt()
    expected = compose_role_prompt(
        role="fixer",
        superpowers=["receiving-code-review"],
        target="spec_pr",
    )
    assert actual == expected


def test_load_fixer_prompt_impl_target_loads_impl_composition() -> None:
    """``target="impl_pr"`` loads ``fixer_impl.md`` with the impl
    superpowers list. Without this, the Fixer reads spec-fix content
    while trying to fix impl-PR code."""
    from foreman.roles.fixer import _load_fixer_prompt
    from foreman.prompts import compose_role_prompt

    actual = _load_fixer_prompt(target="impl_pr")
    expected = compose_role_prompt(
        role="fixer",
        superpowers=[
            "receiving-code-review",
            "verification-before-completion",
            "test-driven-development",
        ],
        target="impl_pr",
    )
    assert actual == expected
    # Sanity: ensure the impl composition contains impl-file content.
    assert "implementation pull request" in actual.lower() or "impl-pr variant" in actual.lower()
```

- [ ] **Step 5.2: Run the new tests to verify they fail**

Run:

```bash
uv run --no-sync pytest packages/foreman/tests/test_roles_fixer.py -v -k "target or superpowers_by_target"
```

Expected: all 4 new tests FAIL — constants and target-aware loader don't exist yet.

- [ ] **Step 5.3: Add the constants + extend _load_fixer_prompt**

Open `packages/foreman/src/foreman/roles/fixer.py`. Find the existing `_LABEL_SPEC_FIX = "foreman:spec-fix"` line and the `_LABEL_SPEC_REVIEW` constant near it (around line 93-94). After those, ADD:

```python
# foreman#79: per-target routing for the Fixer. The role accepts a
# ``target`` kwarg (added by foreman#41 via DaemonRunners) that
# distinguishes spec-PR fixes from impl-PR fixes. Each target gets
# its own entry-label precondition and its own prompt composition.
_LABEL_IMPL_FIX = "foreman:impl-fix"

_FIXER_ENTRY_LABEL_BY_TARGET: dict[str, str] = {
    "spec_pr": _LABEL_SPEC_FIX,
    "impl_pr": _LABEL_IMPL_FIX,
}

_FIXER_SUPERPOWERS_BY_TARGET: dict[str, list[str]] = {
    # Spec-side: today's discipline — receiving review feedback.
    "spec_pr": ["receiving-code-review"],
    # Impl-side: same feedback-reception discipline PLUS the Worker's
    # what-counts-as-done (verification-before-completion) and TDD
    # discipline (test-driven-development). Impl fixes change code, so
    # the test-first + verify-before-commit patterns apply.
    "impl_pr": [
        "receiving-code-review",
        "verification-before-completion",
        "test-driven-development",
    ],
}
```

Then replace the existing `_load_fixer_prompt` function. Find:

```python
def _load_fixer_prompt() -> str:
    """Load the Fixer system prompt: vendored ``receiving-code-review``
    followed by the Foreman-specific Fixer contract.

    Composed via :func:`foreman.prompts.compose_role_prompt` so the
    adapter preamble + per-skill wrappers from PR #43 are applied
    consistently with the other roles.
    """
    from foreman.prompts import compose_role_prompt

    return compose_role_prompt(
        role="fixer",
        superpowers=["receiving-code-review"],
    )
```

Replace with:

```python
def _load_fixer_prompt(target: str = "spec_pr") -> str:
    """Load the Fixer system prompt for the given ``target``.

    ``target="spec_pr"`` (default for back-compat) loads ``fixer.md``
    composed with ``receiving-code-review``. ``target="impl_pr"``
    loads ``fixer_impl.md`` composed with the impl-side superpowers
    list (adds ``verification-before-completion`` +
    ``test-driven-development``). See ``_FIXER_SUPERPOWERS_BY_TARGET``
    for the exact composition.

    Unknown target values fall back to the spec composition — the
    role's precondition gate is the right place to surface
    wrong-target errors, not this loader.
    """
    from foreman.prompts import compose_role_prompt

    superpowers = _FIXER_SUPERPOWERS_BY_TARGET.get(
        target, _FIXER_SUPERPOWERS_BY_TARGET["spec_pr"]
    )
    safe_target = target if target in _FIXER_SUPERPOWERS_BY_TARGET else "spec_pr"
    return compose_role_prompt(
        role="fixer",
        superpowers=superpowers,
        target=safe_target,
    )
```

Now find the precondition gate at line 406:

```python
    if _LABEL_SPEC_FIX not in issue_labels:
        raise RuntimeError(
            f"Issue #{issue_number} does not carry the {_LABEL_SPEC_FIX!r} "
            f"label (labels: "
            + ", ".join(sorted(issue_labels) or ["<none>"])
            + "). The Fixer only acts on issues queued by the Reviewer."
        )
```

Replace with:

```python
    expected_label = _FIXER_ENTRY_LABEL_BY_TARGET[target]
    if expected_label not in issue_labels:
        raise RuntimeError(
            f"Issue #{issue_number} does not carry the {expected_label!r} "
            f"label (labels: "
            + ", ".join(sorted(issue_labels) or ["<none>"])
            + f"). The Fixer only acts on issues queued by the Reviewer "
            f"for target={target!r}."
        )
```

Now find the label-transition block at lines 500-501 (the spec-side flow that removes `_LABEL_SPEC_FIX` and adds `_LABEL_SPEC_REVIEW`). For the impl-side flow, removing `_LABEL_IMPL_FIX` and adding `_LABEL_IMPL_REVIEW` is the symmetric transition. Find the block:

```python
        issue.remove_from_labels(_LABEL_SPEC_FIX)
        issue.add_to_labels(_LABEL_SPEC_REVIEW)
```

Replace with:

```python
        if target == "impl_pr":
            issue.remove_from_labels(_LABEL_IMPL_FIX)
            issue.add_to_labels("foreman:impl-review")
        else:
            issue.remove_from_labels(_LABEL_SPEC_FIX)
            issue.add_to_labels(_LABEL_SPEC_REVIEW)
```

Finally, find the `system_prompt = _load_fixer_prompt()` call (line 463) and thread `target`:

```python
    system_prompt = _load_fixer_prompt(target=target)
```

Also: find the `run_fixer` function signature and verify it accepts `target` as a kwarg. Per the existing daemon_runners wiring, it should already. If the signature is missing `target`, add it: `target: str = "spec_pr"` to keep back-compat.

- [ ] **Step 5.4: Run the new fixer tests to verify they pass**

Run:

```bash
uv run --no-sync pytest packages/foreman/tests/test_roles_fixer.py -v -k "target or superpowers_by_target"
```

Expected: all 4 new tests PASS.

- [ ] **Step 5.5: Run all fixer tests to confirm no regression**

Run:

```bash
uv run --no-sync pytest packages/foreman/tests/test_roles_fixer.py -v
```

Expected: all tests PASS — including existing label-transition tests. If an existing test fails because it stubbed `_load_fixer_prompt()` with the old zero-arg signature, update it to pass `target="spec_pr"` explicitly OR accept the default.

- [ ] **Step 5.6: Run the FULL test suite to confirm zero regression across the package**

Run:

```bash
uv run --no-sync pytest packages/foreman/tests -q
```

Expected: ~518 passed (508 baseline + 10 new from this plan), 0 failed. If anything red, fix before committing.

- [ ] **Step 5.7: Commit**

```bash
git add packages/foreman/src/foreman/roles/fixer.py packages/foreman/tests/test_roles_fixer.py
git commit -m "fix(fixer): per-target prompt + label routing (#79)

Adds _FIXER_ENTRY_LABEL_BY_TARGET and _FIXER_SUPERPOWERS_BY_TARGET
constants. _load_fixer_prompt(target=...) loads fixer_impl.md with
[receiving-code-review, verification-before-completion,
test-driven-development] for target='impl_pr', falls back to fixer.md
with [receiving-code-review] otherwise. Precondition gate now reads
from _FIXER_ENTRY_LABEL_BY_TARGET[target] — accepts foreman:impl-fix
when target='impl_pr', preserves foreman:spec-fix acceptance when
target='spec_pr'. Label-transition block on outcome='fixed' branches
on target (impl_pr → impl-review, spec_pr → spec-review). Threads
target through to the prompt loader. Closes the actual bug from
foreman#79: foreman#63 stalled at impl-fix because the daemon
retried the Fixer dispatch every 30s and got rejected every time."
```

---

## Task 6: Push branch + open PR

**Files:** (no file changes — this is the integration step)

- [ ] **Step 6.1: Verify branch state**

Run:

```bash
git log --oneline main..HEAD
```

Expected: 5 commits on this branch:

1. `docs(spec): reviewer-on-impl and fixer-on-impl prompt routing design` (already there from brainstorming)
2. `feat(prompts): add target kwarg to load_role_prompt for per-target routing (#78, #79)`
3. `feat(prompts): add reviewer_impl.md for Reviewer-on-impl PRs (#78)`
4. `feat(prompts): add fixer_impl.md for Fixer-on-impl PRs (#79)`
5. `fix(reviewer): per-target prompt + label routing (#78)`
6. `fix(fixer): per-target prompt + label routing (#79)`

(That's 6 commits — the spec doc commit + 5 implementation commits.)

- [ ] **Step 6.2: Push the branch using a temp-credential file (no GH_TOKEN in env)**

The pre-push hook runs the full test suite; if `GH_TOKEN` is in the shell env, role tests that assert on parent-env passthrough fail. Use a temp credential helper instead.

Run:

```bash
cd e:/workspaces/ai/agents/foreman
PAT=$(python C:/Users/jeffr/.wren/.claude/skills/creds-management/scripts/creds.py --being wren get github --keyring --password)
CRED_FILE=$(mktemp --suffix=.creds)
chmod 600 "$CRED_FILE"
echo "https://wrenrichley:$PAT@github.com" > "$CRED_FILE"
git -c credential.helper="store --file=$CRED_FILE" push --set-upstream origin fix/reviewer-fixer-impl-prompts
rm -f "$CRED_FILE"
```

Expected: pre-push hook runs the full pytest suite (~2 min), all tests pass, branch is pushed to origin.

- [ ] **Step 6.3: Open the PR**

The PR body explicitly says "Implements #78 and #79" — NOT `Closes`. Issue closure happens manually after merge.

```bash
PAT=$(python C:/Users/jeffr/.wren/.claude/skills/creds-management/scripts/creds.py --being wren get github --keyring --password)
GH_TOKEN="$PAT" gh pr create --repo jeffrichley/foreman --base main --head fix/reviewer-fixer-impl-prompts \
  --title "fix(roles): per-target prompt routing for Reviewer and Fixer" \
  --body "$(cat <<'EOF'
## Summary

Implements #78 and #79 (using "Implements" instead of "Closes" per the convention foreman#63 just landed — issue closure routes through manual close after merge, not via PR body auto-close keywords).

- Adds `target` kwarg to `load_role_prompt` and `compose_role_prompt` with graceful fallback when the impl-variant file doesn't exist (Planner / Worker keep working).
- New `prompts/reviewer_impl.md` framed for reviewing impl PRs (code + tests, not spec docs).
- New `prompts/fixer_impl.md` framed for fixing impl PRs with hard rules on test discipline and scope preservation.
- `roles/reviewer.py`: `_REVIEWER_ENTRY_LABEL_BY_TARGET` + `_REVIEWER_SUPERPOWERS_BY_TARGET` constants drive precondition and composition; `_load_reviewer_prompt(target=...)` threads the kwarg through.
- `roles/fixer.py`: symmetric — `_FIXER_ENTRY_LABEL_BY_TARGET` and `_FIXER_SUPERPOWERS_BY_TARGET`; precondition accepts `foreman:impl-fix` when `target="impl_pr"`; label-transition branches on target.

## Why this matters

Today's foreman#63 autonomous walk surfaced both bugs:

- The Reviewer-on-impl false-flagged PR #77 as "wrong shape, where's the spec doc?" — it was reviewing an impl PR with the spec-review prompt (foreman#78).
- The Fixer-on-impl hard-failed every poll cycle because its precondition required `foreman:spec-fix` and the issue had `foreman:impl-fix` (foreman#79).

After this PR, the autonomous loop can close end-to-end on impl-side findings.

## Test plan

- [x] `pytest packages/foreman/tests/test_prompts.py -v` — loader routing tests
- [x] `pytest packages/foreman/tests/test_roles_reviewer.py -v` — Reviewer per-target tests
- [x] `pytest packages/foreman/tests/test_roles_fixer.py -v` — Fixer per-target tests
- [x] `pytest packages/foreman/tests -q` — full package suite (~518 passed)
- [ ] After merge: restart daemon, label a small dogfood ticket with `foreman:plan`, watch the autonomous loop close end-to-end including any impl-side Reviewer findings → Fixer-on-impl cycle.

## References

- Design: docs/superpowers/specs/2026-06-02-reviewer-fixer-impl-prompts-design.md
- foreman#78: Reviewer-on-impl uses spec prompt
- foreman#79: Fixer rejects foreman:impl-fix label
- foreman#41: original Reviewer-on-impl branch detection (this PR completes the work)
EOF
)"
```

Expected: PR URL printed (e.g., `https://github.com/jeffrichley/foreman/pull/<N>`).

- [ ] **Step 6.4: Watch CI**

CI runs ubuntu-latest + windows-latest + pr-title-lint. Use:

```bash
GH_TOKEN="$PAT" gh pr view <PR_NUMBER> --repo jeffrichley/foreman --json statusCheckRollup --jq '[.statusCheckRollup[]?|{name,status,conclusion}]'
```

Wait until all three are `conclusion: SUCCESS`.

- [ ] **Step 6.5: Merge with retarget discipline**

After all CI green, squash-merge with `--delete-branch`. The base is already `main` (no stacked-PR pattern in play for this PR), so no retarget needed.

```bash
GH_TOKEN="$PAT" gh pr merge <PR_NUMBER> --repo jeffrichley/foreman --squash --delete-branch
```

- [ ] **Step 6.6: Manually close foreman#78 and foreman#79 with audit comments**

```bash
GH_TOKEN="$PAT" gh issue close 78 --repo jeffrichley/foreman --comment "Shipped via PR <PR_NUMBER>. Reviewer-on-impl now loads reviewer_impl.md (impl-PR framing) when target='impl_pr', composed with [requesting-code-review, verification-before-completion, test-driven-development]. _REVIEWER_ENTRY_LABEL_BY_TARGET + _REVIEWER_SUPERPOWERS_BY_TARGET constants make the routing explicit at the role layer."

GH_TOKEN="$PAT" gh issue close 79 --repo jeffrichley/foreman --comment "Shipped via PR <PR_NUMBER>. Fixer-on-impl now accepts foreman:impl-fix as an entry label when target='impl_pr' (via _FIXER_ENTRY_LABEL_BY_TARGET) AND loads fixer_impl.md composed with the impl superpowers list. Label-transition on outcome='fixed' is target-aware (impl_pr → foreman:impl-review)."
```

- [ ] **Step 6.7: Cleanup**

```bash
cd e:/workspaces/ai/agents/foreman
git checkout main
git pull --ff-only origin main
git branch -d fix/reviewer-fixer-impl-prompts
```

Expected: local branch removed cleanly. `git log -1` shows the squash-merge commit on main.

---

## Post-implementation validation (out of plan, in spec)

After all tasks land:

1. Restart the foreman daemon so it picks up the new code.
2. Label a fresh small ticket (or any open foreman ticket without a current pipeline) with `foreman:plan`.
3. Watch the autonomous loop run end-to-end. Success criteria:
   - Reviewer-on-impl does NOT flag the impl PR as "wrong shape."
   - If Reviewer-on-impl returns clean: daemon merges + closes issue autonomously.
   - If Reviewer-on-impl returns needs_fix: Fixer-on-impl accepts the `foreman:impl-fix` label, runs, and reverts the labels on `outcome="fixed"`.

That validation is NOT a task in this plan because it requires the daemon to be running with merged code. It's the proof-of-fix step.

---

## Self-review notes

Before handing off:

- **Spec coverage:** Each section of the design doc maps to at least one task. Architecture (loader, prompt files, role gates) → Tasks 1-5. Test plan layers 1-3 → Tasks 1, 4, 5 tests. Test plan layer 4 (autonomous dogfood) → out-of-plan post-validation.
- **Placeholder scan:** No "TBD" / "TODO" / "fill in later" anywhere. Each step has the actual code or command.
- **Type consistency:** `target: str | None` in the loader, `target: str = "spec_pr"` default in role loaders, dict-typed `_*_ENTRY_LABEL_BY_TARGET` and `_*_SUPERPOWERS_BY_TARGET`. Consistent across all tasks.
- **Bite-sized:** Each step is one action (~2-5 min). Tests come before implementation per TDD.
- **Frequent commits:** One commit per task. Six commits total on the branch (including the spec-doc commit). Each commit message references the foreman ticket(s) it addresses.
