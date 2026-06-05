# Foreman instructions for foreman

This file tells Foreman's bots about this project's specific conventions
and quirks. The 4 role bots (Planner, Reviewer, Fixer, Worker) read this
file and incorporate it into their context when working on your project.

Customize the sections below to match your project's needs. Foreman will
re-read this file on every role invocation, so changes take effect
immediately — no need to restart anything.

## PR title rules

Use standard conventional-commit types only:
`feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`,
`ci`, `chore`, `revert`

For spec PRs (produced by the Planner): use `docs(spec): ...`
For impl PRs (produced by the Worker): use `<type>(<scope>): ...`

## Branch naming

Foreman uses `foreman/issue-<N>` for spec branches and `foreman/impl-<N>`
for impl branches. These names are convention-bound; do not change them
in your `.github/workflows/` or branch-protection rules.

## Quality gate

Foreman's Worker runs the project's quality gate command before claiming
an implementation is done. This project uses: `just check`

Includes lint + typecheck + tests; should exit zero on success and
non-zero on any failure.

## Project-specific notes

<!-- Add anything project-specific Foreman should know:
  - Flaky tests to ignore
  - Deprecated modules to avoid
  - Code-style preferences
  - Merge strategy (squash / rebase / merge-commit)
  - Anything else that would help the bots be more useful
-->

(none yet)
