# Spec: warn-uncommitted on generated `.foreman/INSTRUCTIONS.md` (issue #137)

## Goal

After `foreman init` writes `.foreman/INSTRUCTIONS.md`, detect whether the
file is untracked or dirty in the clone and surface a copy-pasteable
`git add ... && git commit ...` command in the init summary so the operator
doesn't get a surprise dirty-tree gate trip on the next build. Also pin the
template's "no timestamp / no per-run-volatile content" invariant with a
regression test so the file stays stable across re-inits. Closes
[#137](https://github.com/jeffrichley/foreman/issues/137).

## Acceptance criteria

- After `_write_instructions_template` returns inside `run_init`, init runs
  `git status --porcelain -- .foreman/INSTRUCTIONS.md` in the clone and
  records the result.
- `InitResult` exposes a new field `instructions_dirty_warning: str | None`
  populated by that check.
- When the file is untracked or modified: the warning string names the
  EXACT command an operator can paste, of the shape
  `git -C <clone_path> add .foreman/INSTRUCTIONS.md && git -C <clone_path> commit -m "chore: commit .foreman/INSTRUCTIONS.md"`.
- When the file is clean (committed, no diff): the field is `None` and the
  summary contains no warning lines.
- The summary printed by `foreman init` (via `_format_summary`) renders the
  warning when present and omits it cleanly when absent.
- A new regression test pins `instructions.md.template` as timestamp-stable:
  rendering it twice with the same `(repo_name, check_command)` inputs
  produces byte-identical output, and the template body contains no
  `{timestamp}` / `<timestamp>` / `<date>` / `<generated_at>` / `{now}`
  placeholder markers.
- The git-status check failing (subprocess error, non-zero exit, missing
  git binary) does NOT raise from `run_init`; it silently treats the file
  as clean (no warning). Init continues to succeed.

## Approach

The dirty-tree noise the issue describes happens on the FIRST `foreman init`
of a fresh repo: the template lands in the working tree as an untracked
file, and the next build's clean-check gate trips. The existing template
contains no timestamp and `_write_instructions_template` already skips when
the file exists (`packages/foreman/src/foreman/init.py:332-333`), so
subsequent re-inits do NOT regenerate the file — meaning we only need to
solve the first-init untracked case and pin the "no timestamp" invariant
that protects us from regressing the rest.

The issue's acceptance criteria offer two approaches: (A) auto-commit when
the index is otherwise clean, falling back to (B) warn; or (B) just warn.
We pick (B). Rationale:

- `foreman init` already has many side effects (label creation on GitHub,
  config-file mutation, instruction-template write). Silently making a git
  commit on the operator's clone — even a "clean" one — is the most
  surprising side effect of all and the hardest to undo.
- (A) requires defining "index is otherwise clean" precisely
  (untracked-but-unrelated files? staged? worktree mods?) and then implementing
  BOTH (A) and the (B) fallback. (B) alone is one helper, one new field,
  one summary line.
- The warning is fully copy-pasteable and gives the operator agency over
  when and how to commit — including in CI containers and Docker runtimes
  where surprise commits could fire unexpectedly.

Implementation lives entirely in `packages/foreman/src/foreman/init.py`:

1. Add a helper `_check_instructions_committed(clone_path: Path) -> str | None`
   that runs `git status --porcelain -- .foreman/INSTRUCTIONS.md` in
   `clone_path` (subprocess pattern already used by `_validate_clone_path`
   at `init.py:213-219`). Returns `None` when porcelain output is empty
   (clean) or when the subprocess fails for any reason (defensive: a git
   problem shouldn't block init). Returns the formatted warning string
   when porcelain output is non-empty.
2. Extend the `InitResult` dataclass with
   `instructions_dirty_warning: str | None = None`. Default `None` keeps
   constructor calls in tests backwards-compatible.
3. In `run_init`, immediately after the `_write_instructions_template`
   call (currently `init.py:613-617`), call the helper and store the
   result on the `InitResult`.
4. Update `_format_summary` to append a `Warning:` block before the
   `Next steps` block when `instructions_dirty_warning` is non-None;
   keep the rest of the summary identical when it's None so existing
   summary assertions in `test_init.py` continue to pass.

The timestamp-stability test goes in `packages/foreman/tests/test_init.py`
alongside the existing `test_run_init_writes_instructions_template` test;
it calls `_render_instructions_template` (already exists at
`init.py:301-313`) twice and asserts equality, then scans the result for
the forbidden placeholder substrings. This pins both the runtime template
AND the source template (since the runtime template is produced from the
source template via `_load_instructions_template`).

## Sub-requests (topologically sorted)

1. Add the timestamp-stability regression test in
   `packages/foreman/tests/test_init.py`. Verifies the current state and
   pins it before any production changes. Pure test — no source change.
2. Add `_check_instructions_committed(clone_path: Path) -> str | None`
   helper in `packages/foreman/src/foreman/init.py` (near the other
   instructions helpers around lines 287-336). Defensive: catches subprocess
   failure and returns `None`. Helper has no callers yet — pure addition.
3. Extend `InitResult` in `packages/foreman/src/foreman/init.py:160-180`
   with `instructions_dirty_warning: str | None = None`. Default `None`
   keeps existing test constructors valid.
4. Wire `_check_instructions_committed` into `run_init` immediately after
   the `_write_instructions_template` call at `init.py:613-617`. Populate
   `result.instructions_dirty_warning` before `_format_summary` runs.
5. Update `_format_summary` at `init.py:535-566` to render the warning
   block when `instructions_dirty_warning` is non-None and omit it
   otherwise. The clean-path summary text must be byte-identical to the
   pre-change output so existing assertions in
   `test_run_init_summary_contains_expected_fields` and
   `test_run_init_summary_notes_existing_instructions` continue to pass.
6. Add test `test_run_init_warns_when_instructions_uncommitted` covering
   the untracked-file case (fresh init on a seeded clone produces an
   untracked file → warning present, summary contains the command).
7. Add test `test_run_init_no_warning_when_instructions_committed`
   covering the clean case (test pre-commits the file inside the seeded
   clone → warning is `None`, summary contains no `Warning:` line).
8. Add test `test_run_init_no_warning_when_git_status_fails` covering
   defensive behavior (mock `subprocess.run` for the porcelain call to
   raise → `instructions_dirty_warning` is `None`, init still succeeds).

## File-level changes

| Path | Change |
| --- | --- |
| `packages/foreman/src/foreman/init.py` | Add `_check_instructions_committed` helper; add `instructions_dirty_warning: str \| None = None` field to `InitResult`; call helper inside `run_init` after instructions write; render warning in `_format_summary` when present. |
| `packages/foreman/tests/test_init.py` | Add `test_template_render_is_timestamp_stable`; add `test_run_init_warns_when_instructions_uncommitted`; add `test_run_init_no_warning_when_instructions_committed`; add `test_run_init_no_warning_when_git_status_fails`. |

No other source files change. The template itself (`packages/foreman/src/foreman/templates/instructions.md.template`) is NOT modified — its current timestamp-stable state is the invariant we are pinning.

## Alternatives considered

- **Approach A from the issue (auto-commit when clean, warn fallback).**
  Ruled out: requires defining "clean" precisely (untracked-but-unrelated
  files? staged? worktree mods elsewhere?) and implementing BOTH branches.
  Silent commits during init are also the most surprising side effect we
  could add — operators using Docker or CI runtimes could see unintended
  commits. Picking just (B) is simpler, safer, and the issue explicitly
  permits it.
- **Gitignore `.foreman/INSTRUCTIONS.md` and skip the commit entirely.**
  Ruled out: PR #134 deliberately committed the file so the project-specific
  instructions are version-controlled and reviewable. Gitignoring it would
  undo that decision and leave the file invisible to project history.
- **Add the warning to stderr only, not the summary.** Ruled out: `foreman
  init` currently prints exactly one block to stdout via `click.echo(result.summary)`.
  Splitting output across streams complicates tests and breaks the "one
  summary block tells you everything" CLI surface. Embedding the warning
  in the summary keeps the operator-facing contract simple.
- **Do nothing — let operators commit the file manually after init.**
  Ruled out: the issue documents this exact problem hitting twice
  (PR #134 + cutover-day dirty-tree gate trip), so the operator-facing UX
  needs to actively prevent it rather than rely on prior knowledge.

## Open questions

(none)

## Out of scope

- Implementing the auto-commit branch from issue's Approach A. We
  explicitly picked Approach B.
- Modifying the `.foreman/INSTRUCTIONS.md` file already committed in
  the repo (the existing content is operator-curated; init never
  overwrites it).
- Adding `foreman label sync` semantics or any other init feature.
- Refactoring the existing `_validate_clone_path` subprocess pattern.
- Gitignoring `.foreman/INSTRUCTIONS.md`.
- Any change to the four role bots' behavior, prompts, or instruction
  loading code in `instructions.py`.
- Adding a `--commit` / `--no-commit` flag to `foreman init` (the warning
  is unconditional; if operators want a flag later that's a separate
  ticket).
