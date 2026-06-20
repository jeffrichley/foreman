# Spec: add `foreman contrib` namespace with `sign-commits` + `check-signoff` (issue #349)

## Goal

Add a contributor-facing `foreman contrib` typer sub-app with two
commands — `sign-commits` (rewrites unsigned commits on the current
branch with a `Signed-off-by:` trailer) and `check-signoff` (dry-run
alias, suitable for CI / pre-push hooks). Collapses the three-step DCO
recovery snippet in `CONTRIBUTING.md` (read instructions → run
`git rebase --exec 'git commit --amend --no-edit -s'` → force-push)
into a single command, while adding safety checks the raw `git rebase`
snippet does not have. Establishes a `contrib` namespace so future
contributor helpers (lint-trailers, check-conventional-commit, etc.)
can land without polluting the operator command surface
(`ps`, `log`, `hold`, `reset`, etc.). Tracks issue
[#349](https://github.com/jeffrichley/foreman/issues/349).

## Acceptance criteria

- `packages/foreman/src/foreman/v4/cli/contrib/__init__.py` exists and
  declares `contrib_app = typer.Typer(name="contrib", help="Contributor
  helpers (sign-commits, check-signoff)", no_args_is_help=True)`.
- `packages/foreman/src/foreman/v4/cli/__init__.py` registers the
  sub-app via `app.add_typer(contrib_app)` next to the existing
  `daemon_app` registration at line 89, and binds the two commands
  via `contrib_app.command("sign-commits")(cmd_sign_commits)` and
  `contrib_app.command("check-signoff")(cmd_check_signoff)`.
- `foreman contrib --help` lists `sign-commits` and `check-signoff`.
  `foreman contrib` with no subcommand prints help (driven by
  `no_args_is_help=True`).
- `foreman contrib sign-commits` (no flags) walks the commit range
  `<base>..HEAD` (default `<base>=main`), detects which commits are
  missing a `Signed-off-by:` trailer matching `git config user.email`,
  prompts for confirmation, then rewrites the branch by running
  `git rebase <base> --exec 'git commit --amend --no-edit -s'` so each
  commit ends with a `Signed-off-by:` trailer derived from local
  `git config user.{name,email}`. After the rebase, reports a one-line
  summary ("Signed off N commits.") and exits 0.
- `foreman contrib sign-commits --check` is dry-run only: walks the
  commit range, prints each unsigned commit's short SHA + subject,
  exits 1 if any unsigned commit is found, exits 0 otherwise. Performs
  no rebase, no rewrite, no force-push.
- `foreman contrib check-signoff` is a thin alias that delegates to the
  same internal `_check_signoff(base=..., ...)` helper used by
  `sign-commits --check`. Exposes the same `--base` flag, accepts no
  other flags. Same exit codes.
- `foreman contrib sign-commits --base <branch>` walks `<branch>..HEAD`
  instead of `main..HEAD`. Test must verify this path independently of
  the default-branch path.
- `foreman contrib sign-commits --force` skips the interactive
  pushed-commit safety prompt (described below). Does not skip the
  dirty-tree or detached-HEAD checks.
- **Safety checks, all of which print a clear stderr message and exit
  non-zero (exit 2) before doing any work:**
  - Refuse to run if the working tree has uncommitted changes (run
    `git status --porcelain` — if stdout is non-empty, refuse with
    "working tree is dirty; commit or stash first").
  - Refuse to run if HEAD is detached (run
    `git symbolic-ref --short -q HEAD` — if it exits non-zero, refuse
    with "HEAD is detached; check out a branch first").
  - Refuse to run if `git config user.name` or `git config user.email`
    is unset (the `-s` trailer would be empty/invalid). Message:
    "git config user.name / user.email must be set; sign-off trailer
    would be invalid".
  - Refuse to run if the commit range `<base>..HEAD` contains any
    merge commits (`git log --merges <base>..HEAD` non-empty). Message:
    "range contains merge commits; rebase --exec would break history.
    Rewrite by hand or rebase out the merge first." (Without this
    guard, a `rebase --exec` over a merge will silently linearize
    history — a destructive surprise this command must not cause.)
- **Pushed-commit warning:** before the rebase, run
  `git for-each-ref --format='%(upstream:short)' refs/heads/$(git
  symbolic-ref --short HEAD)`. If an upstream is configured, run
  `git rev-list --count <base>..HEAD ^@{upstream}` to count commits in
  the rebase range that the upstream already has. If non-zero, print
  a warning: "N commits in `<base>..HEAD` have already been pushed to
  `<upstream>`. After rebase you'll need `git push --force-with-lease`
  to update the remote. Continue? [y/N]" — abort on `n`/default,
  proceed on `y`. The `--force` flag skips this prompt (and the
  warning still prints, so the contributor knows to force-push after).
- All git subprocess calls use the same env-handling discipline as
  `packages/foreman/src/foreman/worktree.py` (route through
  `foreman._env_filter.filtered_subprocess_env` so leaked
  `VIRTUAL_ENV` / `UV_PROJECT_ENVIRONMENT` etc. do not poison git
  hooks). `cwd` is resolved from the current working directory (these
  are contributor-machine commands, not bot-orchestrated worktree
  commands); we do NOT assume `cwd == repo_root` — instead, every git
  call uses `cwd=Path.cwd()` and relies on git's own upward repo-root
  search.
- Unit tests in `packages/foreman/tests/v4/cli/test_contrib_sign_commits.py`
  cover, using a `tmp_path`-bootstrapped fake git repo:
  - Fixture builds a repo with 3 commits on `main` → branch off →
    add 2 unsigned + 1 signed commit on the feature branch.
  - `sign-commits --check` lists the 2 unsigned by short SHA + subject,
    exits 1, does not modify history (verify HEAD SHA unchanged).
  - `sign-commits` (with `--force` to bypass any prompts in the test
    harness) rewrites the 2 unsigned with the `Signed-off-by:` trailer
    derived from the fixture's `git config user.{name,email}`, leaves
    the already-signed commit's trailer intact (no duplicate trailer
    line — `git commit --amend -s` is idempotent on an already-present
    trailer; assert via `git log --format=%B` parsing that the trailer
    appears exactly once per commit).
  - `sign-commits --base develop` walks `develop..HEAD` not
    `main..HEAD` (verify by setting up a `develop` branch in the
    fixture with a different ancestor).
  - Detached-HEAD path: check out a SHA directly, run `sign-commits`,
    assert exit 2 + stderr message.
  - Dirty-tree path: leave an unstaged change, run `sign-commits`,
    assert exit 2 + stderr message.
  - Merge-in-range path: create a merge commit in the range, run
    `sign-commits`, assert exit 2 + "range contains merge commits"
    in stderr.
  - Missing `user.email`: unset via `git config --unset user.email`,
    run `sign-commits`, assert exit 2 + "user.name / user.email must
    be set" in stderr.
- A separate test file
  `packages/foreman/tests/v4/cli/test_contrib_check_signoff.py`
  exercises the `check-signoff` alias path: same fixture, asserts the
  alias's exit code + stdout match `sign-commits --check`.
- A pushed-commit warning test sets up a bare-repo upstream in
  `tmp_path`, pushes one commit, adds one more local unsigned commit,
  invokes `sign-commits` (without `--force`) and confirms via input
  redirection (CliRunner `input="n\n"`) that the command aborts with
  the warning message present in stdout. A second test invokes with
  `--force` and confirms the rewrite proceeds (the warning still
  prints).
- `CONTRIBUTING.md`'s "Signing a commit" section (lines 88-90) is
  updated. The existing snippet:

  > `git commit -s` appends the `Signed-off-by:` trailer using the
  > `user.name` / `user.email` from your git config. To amend a commit
  > that's missing the trailer:
  > `git commit --amend -s --no-edit`.

  becomes:

  > `git commit -s` appends the `Signed-off-by:` trailer using the
  > `user.name` / `user.email` from your git config. If you forgot
  > `-s` on one or more commits already on your branch, the fastest
  > recovery is `foreman contrib sign-commits` (or
  > `foreman contrib check-signoff` to dry-run + see which commits
  > are missing the trailer). For a single-commit amend without
  > rebase: `git commit --amend -s --no-edit`.

- `just check` exits zero (ruff + mypy + lint-imports + pytest).
  `new_failures_count == 0` on a fresh run.
- The PR title for the Worker's impl PR follows the project's
  conventional-commit shape, e.g.
  `feat(cli): add 'foreman contrib' namespace with sign-commits +
  check-signoff helpers`.

## Approach

This is a thin contributor-facing CLI namespace; **no GoF pattern
applies cleanly** — this is the project's existing
"sub-typer + per-command function" CLI shape (already used by
`daemon_app` in `packages/foreman/src/foreman/v4/cli/__init__.py:88-93`)
extended one more time. The relevant principles are:

- **SRP** (Single Responsibility): the new `contrib` namespace
  separates contributor-facing helpers from operator-facing commands.
  Different audience, different command surface; mixing them at the
  top level would muddy `foreman --help`. This is why the issue
  insists on a sub-app rather than two more top-level commands.
- **"Make the right thing easy"** (the project's stated principle from
  Decision 4 of the architecture stability plan): the current DCO
  recovery story is three steps (read CONTRIBUTING → look up the
  rebase incantation → run it correctly without trashing the branch).
  Collapsing to one command, with safety rails the raw incantation
  does not have (dirty-tree guard, merge-commit guard, force-push
  warning, missing-user.email guard), is precisely the principle's
  application.

The Worker should structure the new code as a subpackage rather than a
flat module, leaving room for the future commands the issue calls out
(`foreman contrib lint-trailers`, `foreman contrib check-conventional-
commit`) without bloating one file:

```
packages/foreman/src/foreman/v4/cli/contrib/
  __init__.py         # contrib_app + command registrations
  sign_commits.py     # cmd_sign_commits + cmd_check_signoff +
                      # _check_signoff() / _sign_commits() helpers
```

Both `cmd_sign_commits` (with `--check`) and `cmd_check_signoff`
should call into a single `_check_signoff(base, cwd) -> list[str]`
helper that returns the list of unsigned commit SHAs. This is DRY —
the check path is the same logic from two entrypoints — and matches
the discovery/execution split already used by `cmd_reset`
(`packages/foreman/src/foreman/v4/cli/mutations.py:50-208`,
`_discover` + `_execute`): pure functions for the read side, an
imperative loop for the mutation side.

The subprocess discipline matches `packages/foreman/src/foreman/worktree.py`:
every `git` invocation goes through `subprocess.run([...], cwd=...,
check=True | False, capture_output=True, text=True,
env=filtered_subprocess_env())`. `filtered_subprocess_env` is the
existing env-leak guard from `foreman._env_filter`; this command runs
on the contributor's machine where their shell almost certainly has
`VIRTUAL_ENV` set, and we do not want that leaking into git hooks
(uv would otherwise mis-target the contributor's venv).

Crucially, this command does NOT consume `V4Config` or `OperatorConfig`.
This is a contributor-machine command — its identity comes from
`git config user.{name,email}`, the same source `git commit -s` reads.
The typer command bodies therefore do NOT call `ctx.obj.config` or
`ctx.obj.repo`; they take a typer `Context` only to satisfy the
existing CLI shape, and they consult only `git` itself for state.
This isolates the command from the daemon-bootstrap path
(`main()` at `packages/foreman/src/foreman/v4/cli/__init__.py:146-214`),
which is desirable: a contributor running `foreman contrib
sign-commits` should not need GitHub App credentials configured.

The dependency on foreman#347 is documentational, not structural:
foreman#347 makes DCO check-blocking (and therefore makes this command
useful), but this command's code does not import or call anything
foreman#347 adds. The `CONTRIBUTING.md` update in this PR can land
without waiting for foreman#347; if foreman#347 has not merged yet,
the docs reference is forward-looking but harmless.

## Sub-requests (topologically sorted)

1. Create the subpackage skeleton:
   - `packages/foreman/src/foreman/v4/cli/contrib/__init__.py` —
     module docstring explaining "contributor helpers, not operator
     mutations"; declares `contrib_app` typer instance with
     `no_args_is_help=True`; imports `cmd_sign_commits` +
     `cmd_check_signoff` from `.sign_commits` and binds them via
     `contrib_app.command(...)(...)`.
   - `packages/foreman/src/foreman/v4/cli/contrib/sign_commits.py` —
     skeleton with the two command functions + the two private helpers
     stubbed.

2. Implement the read-side helpers in `sign_commits.py`:
   - `_run_git(args: list[str], *, cwd: Path, check: bool = True) ->
     subprocess.CompletedProcess[str]` — thin wrapper using
     `filtered_subprocess_env()`. Matches the env discipline in
     `worktree.py`.
   - `_assert_clean_tree(cwd: Path) -> None` — runs
     `git status --porcelain`; raises `typer.Exit(code=2)` after
     stderr-echoing if non-empty.
   - `_assert_branch_not_detached(cwd: Path) -> str` — runs
     `git symbolic-ref --short -q HEAD`; on non-zero exit, raises
     `typer.Exit(code=2)`. Returns the current branch name.
   - `_assert_signoff_identity(cwd: Path) -> tuple[str, str]` —
     reads `git config user.name` + `git config user.email`; raises
     `typer.Exit(code=2)` if either missing/empty.
   - `_assert_no_merge_commits(cwd: Path, base: str) -> None` — runs
     `git log --merges --format=%H <base>..HEAD`; raises
     `typer.Exit(code=2)` if non-empty.
   - `_list_unsigned_commits(cwd: Path, base: str, signoff_email:
     str) -> list[tuple[str, str]]` — runs
     `git log --format=%H%x00%s%x00%(trailers:key=Signed-off-by,
     valueonly=true,separator=|)) <base>..HEAD`. For each commit,
     checks whether any trailer's email part matches `signoff_email`.
     Returns `[(short_sha, subject), ...]` for unsigned ones (the
     `--check` path prints this list).
   - `_count_pushed_commits_in_range(cwd: Path, base: str) -> int |
     None` — resolves `@{upstream}`; if no upstream, returns `None`.
     Otherwise returns the count of commits in `<base>..HEAD`
     reachable from `@{upstream}` (i.e. already pushed).

3. Implement `_check_signoff(base: str, cwd: Path) -> int`:
   - Runs `_assert_branch_not_detached`,
     `_assert_signoff_identity`, then `_list_unsigned_commits`.
   - If list is empty: prints "All commits in `<base>..HEAD` are
     signed off." and returns 0.
   - Otherwise: prints "Unsigned commits in `<base>..HEAD`:" + one
     line per `(short_sha, subject)` to stdout, returns 1.

4. Implement `_sign_commits(base: str, cwd: Path, force: bool) -> int`:
   - Runs all four `_assert_*` checks (dirty-tree, detached HEAD,
     signoff identity, no merges in range).
   - Calls `_list_unsigned_commits`. If empty: print "Nothing to do
     — all commits already signed." Return 0.
   - Calls `_count_pushed_commits_in_range`. If non-zero, print the
     force-push warning. If `not force`, run
     `typer.confirm("Continue?", default=False)`; abort on no.
   - Runs `git rebase <base> --exec 'git commit --amend --no-edit
     -s'` via `_run_git`. On non-zero exit, prints the rebase's
     stderr and returns the rebase's exit code (contributor will
     likely need to `git rebase --abort` themselves; we surface
     git's error rather than try to recover).
   - On success, prints "Signed off N commits." Return 0.

5. Implement the typer commands in `sign_commits.py`:

   ```python
   def cmd_sign_commits(
       ctx: typer.Context,
       base: str = typer.Option("main", "--base"),
       check: bool = typer.Option(False, "--check"),
       force: bool = typer.Option(False, "--force"),
   ) -> None:
       cwd = Path.cwd()
       if check:
           raise typer.Exit(code=_check_signoff(base=base, cwd=cwd))
       raise typer.Exit(code=_sign_commits(
           base=base, cwd=cwd, force=force,
       ))

   def cmd_check_signoff(
       ctx: typer.Context,
       base: str = typer.Option("main", "--base"),
   ) -> None:
       cwd = Path.cwd()
       raise typer.Exit(code=_check_signoff(base=base, cwd=cwd))
   ```

6. Register the sub-app in
   `packages/foreman/src/foreman/v4/cli/__init__.py`:
   - Add `from foreman.v4.cli.contrib import contrib_app` to the
     existing import block at lines 24-44.
   - Add `app.add_typer(contrib_app)` immediately after the existing
     `daemon_app` block (after line 93).

7. Add `packages/foreman/tests/v4/cli/test_contrib_sign_commits.py`
   with the fixture + scenarios listed in acceptance criteria. The
   fixture pattern:

   ```python
   @pytest.fixture
   def fake_repo(tmp_path: Path) -> Path:
       repo = tmp_path / "repo"
       repo.mkdir()
       subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
       subprocess.run(["git", "config", "user.name", "Alice"], cwd=repo, check=True)
       subprocess.run(["git", "config", "user.email", "a@example.com"], cwd=repo, check=True)
       # ... build commits ...
       return repo
   ```

   Each test changes into the fake-repo dir via `monkeypatch.chdir(repo)`
   before invoking `CliRunner().invoke(app, ["contrib", "sign-commits",
   ...])`. CliRunner's `obj` argument can be a minimal
   `build_cli_context()` since these commands ignore `ctx.obj`, but
   we still provide one so typer's `Context` parameter resolves.

8. Add `packages/foreman/tests/v4/cli/test_contrib_check_signoff.py`
   with the alias-path tests.

9. Update `CONTRIBUTING.md` per the acceptance criterion's wording.
   No structural change to neighboring sections.

10. Run `just check` and confirm green. If `lint-imports` complains
    about the new subpackage, add it under the same allowed-paths
    block the rest of `foreman.v4.cli.*` uses in
    `pyproject.toml`'s `[tool.importlinter]` config (verify whether
    a new entry is needed before assuming).

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/v4/cli/contrib/__init__.py` | New file. Declares `contrib_app = typer.Typer(name="contrib", help="...", no_args_is_help=True)`; imports `cmd_sign_commits` + `cmd_check_signoff` from `.sign_commits`; binds them via `contrib_app.command(...)`. |
| `packages/foreman/src/foreman/v4/cli/contrib/sign_commits.py` | New file. Contains the two typer command functions (`cmd_sign_commits`, `cmd_check_signoff`), the two internal entry helpers (`_check_signoff`, `_sign_commits`), and the read-side helpers (`_run_git`, `_assert_*`, `_list_unsigned_commits`, `_count_pushed_commits_in_range`). |
| `packages/foreman/src/foreman/v4/cli/__init__.py` | Import `contrib_app`; register via `app.add_typer(contrib_app)` after the existing `daemon_app` block at line 93. |
| `packages/foreman/tests/v4/cli/test_contrib_sign_commits.py` | New file. Fake-repo fixture + the seven scenarios in acceptance criteria (check / sign / --base / detached / dirty / merge-in-range / missing-email) + the two pushed-warning scenarios. |
| `packages/foreman/tests/v4/cli/test_contrib_check_signoff.py` | New file. Alias-path tests asserting `check-signoff` matches `sign-commits --check` exit code + stdout shape. |
| `CONTRIBUTING.md` | Update "Signing a commit" section (lines 88-90) to point at `foreman contrib sign-commits` for multi-commit recovery; preserve the `git commit --amend -s --no-edit` snippet for the single-commit case. |

## Alternatives considered

- **Implement as a `foreman sign-commits` top-level command (no `contrib`
  namespace).** Rejected: the issue explicitly establishes the
  namespace ("gives us room to grow contributor-facing helpers without
  polluting the operator command surface"). Top-level `foreman
  sign-commits` would put it next to `ps`/`log`/`hold`/`reset`, mixing
  audiences. The 1-time cost of `contrib_app` is small; the recurring
  benefit is a clean operator surface.

- **Use a flat `foreman/v4/cli/contrib.py` module instead of a
  subpackage.** Rejected on the issue's explicit "subpackage"
  requirement and the named future commands (`lint-trailers`,
  `check-conventional-commit`). A subpackage costs one extra
  `__init__.py` today and avoids one rename later when the second
  module lands.

- **Implement `sign-commits` as a shell script in `scripts/` rather
  than a `foreman` subcommand.** Rejected: that's the current
  CONTRIBUTING.md state (a copy-pasteable rebase incantation). The
  whole point of the ticket is to add a discoverable, safe,
  testable command. A shell script lacks the safety rails we want
  (Python subprocess error handling, structured exit codes,
  testability via `CliRunner`).

- **Walk + rewrite commits in Python (via `git commit-tree` /
  manual ref rewriting) rather than `git rebase --exec`.** Rejected:
  significantly more code, more failure modes, and the existing
  CONTRIBUTING.md recovery snippet uses `rebase --exec` — keeping
  the same underlying mechanic means contributors who already know
  the manual incantation see exactly the operation they expected.
  We add the safety rails around the same call, not in place of it.

- **Make `sign-commits` re-sign only the commits where the trailer's
  email differs from `git config user.email` (rather than re-sign
  every commit in range).** Rejected on simplicity grounds:
  `git commit --amend -s` is idempotent on an already-signed commit
  with a matching trailer (it does not append a duplicate). The
  rebase walks every commit in range either way; making the
  predicate smarter buys nothing.

- **Make `check-signoff` a hidden `--check` subcommand of
  `sign-commits` (no separate top-level command).** Rejected on the
  CI-ergonomics argument the issue makes: `foreman contrib
  check-signoff` is a cleaner shape than `foreman contrib sign-commits
  --check` to wire into a pre-push hook or a CI job. The cost (one
  extra typer command body, four lines) is trivial.

## Open questions

(none — the issue is precise about the command surface, flag set,
safety checks, and test scenarios; the existing CLI patterns
(daemon_app sub-typer, mutations.py discover/execute split,
worktree.py subprocess discipline) supply every needed convention.
One judgment call — refusing to rebase a range containing merge
commits — is documented in the approach section as an additional
safety rail; flagging here in case the Reviewer disagrees, but the
default direction is the safe one and the Worker can implement as
specified.)

## Out of scope

- **Pre-push git hook installation.** Per the issue body: "we don't
  ship a hook installer in this ticket." Contributors can wire
  `foreman contrib check-signoff` into their own `.git/hooks/pre-push`
  manually; the existing CONTRIBUTING.md already references the
  pre-commit hook tooling for other gates.
- **Additional `contrib` commands** (`lint-trailers`,
  `check-conventional-commit`, etc.). The issue names them but
  scopes them to future tickets. This PR establishes the namespace
  with two commands; future PRs add more.
- **Changing DCO enforcement state** (blocking vs warning). The
  issue body explicitly excludes this — "the gate's blocking-vs-
  warning state is a separate decision."
- **Reading `[operator.signer]` from `~/.foreman/config.toml`** to
  source the sign-off identity. This command reads `git config
  user.{name,email}` — the same source `git commit -s` reads —
  because contributors run it on their own machine, with their own
  git identity. The operator config is for bot-driven runs (Planner
  / Reviewer / Fixer / Worker), not contributor tooling.
- **Auto-detecting the project's "main" branch name** (e.g.
  master vs main vs trunk). The `--base` flag with default `main`
  matches the repo's existing convention (CONTRIBUTING.md and
  branch-protection rules both assume `main`). Contributors with
  a non-`main` default can pass `--base`. Auto-detection via
  `git symbolic-ref refs/remotes/origin/HEAD` is straightforward
  but adds one more network-sensitive code path; deferred unless
  the Reviewer asks for it.
- **GPG-signing the rewritten commits.** Sign-off (a trailer) and
  GPG signing (a commit-object signature) are independent. The
  `--amend -s` rewrite respects the contributor's local
  `commit.gpgsign` config — if they have it enabled, the rebased
  commits get re-signed; if not, they don't. We do not change the
  GPG-signing state.
