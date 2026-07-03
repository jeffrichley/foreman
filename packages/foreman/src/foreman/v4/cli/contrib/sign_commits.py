"""Implementation of ``foreman contrib sign-commits`` + ``check-signoff``.

These are contributor-machine commands. They do NOT consume
:class:`~foreman.v4.config.V4Config` or
:class:`~foreman.v4.config.OperatorConfig` — the sign-off identity
comes from ``git config user.{name,email}``, the same source
``git commit -s`` reads. This keeps the commands runnable on a fresh
clone with no GitHub App credentials configured.

The subprocess discipline matches :mod:`foreman.worktree`: every git
call routes through
:func:`foreman._env_filter.filtered_subprocess_env` so a leaked
``VIRTUAL_ENV`` / ``UV_PROJECT_ENVIRONMENT`` etc. cannot poison git
hooks fired by the rebase. ``cwd`` is resolved per call from
:func:`pathlib.Path.cwd` and we rely on git's own upward repo-root
search rather than assuming ``cwd == repo_root``.

Architecture: the read-side logic lives in
:func:`_list_unsigned_commits` and a thin
:func:`_check_signoff` wrapper that both the ``--check`` flag on
``sign-commits`` and the ``check-signoff`` alias call into.
:func:`_sign_commits` is the mutation entry point and runs all four
safety asserts before delegating to ``git rebase --exec``. Mirrors the
``_discover`` + ``_execute`` split already used in
:mod:`foreman.v4.cli.mutations` for ``cmd_reset``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer

from foreman._env_filter import filtered_subprocess_env

# --------------------------------------------------------------------- #
# Subprocess wrapper                                                    #
# --------------------------------------------------------------------- #


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Thin git wrapper using :func:`filtered_subprocess_env`.

    Mirrors the env discipline in :mod:`foreman.worktree`. ``check`` is
    forwarded straight to ``subprocess.run``; callers that want to read
    a non-zero exit code without raising must pass ``check=False``.
    """
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        env=filtered_subprocess_env(),
    )


# --------------------------------------------------------------------- #
# Safety asserts                                                        #
# --------------------------------------------------------------------- #


def _assert_clean_tree(cwd: Path) -> None:
    """Refuse if the working tree has uncommitted changes.

    Applies to the rewriting path only — ``git rebase`` would either
    refuse outright or carry the dirt forward as part of the rewrite,
    both confusing. We surface the dirt up front.
    """
    result = _run_git(["status", "--porcelain"], cwd=cwd)
    if result.stdout.strip():
        typer.echo(
            "error: working tree is dirty; commit or stash first.",
            err=True,
        )
        raise typer.Exit(code=2)


def _assert_branch_not_detached(cwd: Path) -> str:
    """Refuse if HEAD is detached. Returns the current branch name.

    Applies to the rewriting path only. The read-only ``--check`` path
    deliberately does NOT call this — CI checkouts (GitHub Actions et al.)
    are detached by default, and ``<base>..HEAD`` resolves fine from a
    detached HEAD; firing the guard there would break the
    advertised pre-push / CI use case.
    """
    result = _run_git(
        ["symbolic-ref", "--short", "-q", "HEAD"],
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0:
        typer.echo(
            "error: HEAD is detached; check out a branch first.",
            err=True,
        )
        raise typer.Exit(code=2)
    return result.stdout.strip()


def _assert_signoff_identity(cwd: Path) -> tuple[str, str]:
    """Refuse if ``git config user.{name,email}`` is unset.

    Applies to both paths. On the read-only path we need ``user.email``
    to match against trailer email values; on the rewrite path the ``-s``
    trailer would be empty/invalid without it.
    """
    name_res = _run_git(
        ["config", "--get", "user.name"],
        cwd=cwd,
        check=False,
    )
    email_res = _run_git(
        ["config", "--get", "user.email"],
        cwd=cwd,
        check=False,
    )
    name = name_res.stdout.strip() if name_res.returncode == 0 else ""
    email = email_res.stdout.strip() if email_res.returncode == 0 else ""
    if not name or not email:
        typer.echo(
            "error: git config user.name / user.email must be set; "
            "sign-off trailer would be invalid.",
            err=True,
        )
        raise typer.Exit(code=2)
    return name, email


def _assert_no_merge_commits(cwd: Path, base: str) -> None:
    """Refuse if ``<base>..HEAD`` contains any merge commits.

    A ``rebase --exec`` over a merge silently linearizes history — a
    destructive surprise this command must not cause. Read-only paths
    don't rebase, so this check is not run there.
    """
    result = _run_git(
        ["log", "--merges", "--format=%H", f"{base}..HEAD"],
        cwd=cwd,
    )
    if result.stdout.strip():
        typer.echo(
            "error: range contains merge commits; rebase --exec would "
            "break history. Rewrite by hand or rebase out the merge "
            "first.",
            err=True,
        )
        raise typer.Exit(code=2)


# --------------------------------------------------------------------- #
# Read-side: list unsigned commits in <base>..HEAD                      #
# --------------------------------------------------------------------- #


def _list_unsigned_commits(
    cwd: Path,
    base: str,
    signoff_email: str,
) -> list[tuple[str, str]]:
    r"""Return ``[(short_sha, subject), ...]`` for unsigned commits.

    A commit counts as signed when at least one ``Signed-off-by:``
    trailer's email part matches ``signoff_email`` (case-insensitive).
    The match is on the contributor's own email so re-running the
    command twice produces no duplicate trailers.

    Uses NUL-byte field separators in the format string to survive
    commit subjects that contain colons / newlines. Each commit record
    is parsed as ``<short>\\x00<subject>\\x00<trailers>``.
    """
    # %x00 is git's NUL byte; %(trailers:...) gives us the trailer
    # block as a string we can scan for Signed-off-by email matches.
    fmt = "%h%x00%s%x00%(trailers:key=Signed-off-by,valueonly=true,separator=|)"
    result = _run_git(
        ["log", f"--format={fmt}", f"{base}..HEAD"],
        cwd=cwd,
    )
    unsigned: list[tuple[str, str]] = []
    needle = signoff_email.strip().lower()
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x00")
        if len(parts) < 3:
            # Defensive: malformed line — treat as unsigned so the
            # human sees it and can investigate.
            short = parts[0] if parts else "????"
            subject = parts[1] if len(parts) > 1 else "(unknown)"
            unsigned.append((short, subject))
            continue
        short, subject, trailers = parts[0], parts[1], parts[2]
        if not _trailers_match_email(trailers, needle):
            unsigned.append((short, subject))
    return unsigned


def _trailers_match_email(trailers: str, needle: str) -> bool:
    """Return True if any Signed-off-by trailer value contains ``needle``.

    ``trailers`` is the ``%(trailers:...separator=|)`` output: a
    pipe-separated list of trailer values, each looking like
    ``"Name <email@host>"``. We do a substring match on the lowercased
    needle — exact rfc822 parsing is overkill given that contributors
    use plain ``Name <email>`` format and ``git config user.email`` is
    the source of truth on both sides.
    """
    if not trailers.strip():
        return False
    for value in trailers.split("|"):
        if needle and needle in value.lower():
            return True
    return False


# --------------------------------------------------------------------- #
# Pushed-commit counting                                                #
# --------------------------------------------------------------------- #


def _count_pushed_commits_in_range(
    cwd: Path,
    base: str,
) -> int | None:
    """Count commits in ``<base>..HEAD`` that the upstream already has.

    Returns ``None`` if no upstream is configured for the current
    branch. Otherwise returns the intersection size.

    Implementation:
      - If ``@{upstream}`` is an ancestor of HEAD (the common linear
        case), the intersection is exactly ``<base>..@{upstream}``.
      - Otherwise (divergent: local rebase + remote moved), compute the
        merge-base of ``@{upstream}`` and HEAD, then count
        ``<base>..<merge-base>``.

    We deliberately do NOT use ``rev-list --count <base>..HEAD
    ^@{upstream}`` — that counts the inverse (local-only commits not
    yet pushed) and would fire the warning backwards.
    """
    # Is an upstream configured for HEAD?
    upstream_res = _run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=cwd,
        check=False,
    )
    if upstream_res.returncode != 0:
        return None
    upstream = upstream_res.stdout.strip()
    if not upstream:
        return None

    # Is the upstream an ancestor of HEAD?
    ancestor_res = _run_git(
        ["merge-base", "--is-ancestor", upstream, "HEAD"],
        cwd=cwd,
        check=False,
    )
    if ancestor_res.returncode == 0:
        # Linear case.
        count_res = _run_git(
            ["rev-list", "--count", f"{base}..{upstream}"],
            cwd=cwd,
        )
        return int(count_res.stdout.strip() or "0")

    # Divergent case: walk via the merge-base.
    mb_res = _run_git(
        ["merge-base", upstream, "HEAD"],
        cwd=cwd,
        check=False,
    )
    if mb_res.returncode != 0:
        # No common ancestor — pathological history. Don't warn; let
        # the rebase surface whatever git would say next.
        return 0
    mb = mb_res.stdout.strip()
    if not mb:
        return 0
    count_res = _run_git(
        ["rev-list", "--count", f"{base}..{mb}"],
        cwd=cwd,
    )
    return int(count_res.stdout.strip() or "0")


# --------------------------------------------------------------------- #
# Read-side entry: list + report                                        #
# --------------------------------------------------------------------- #


def _check_signoff(base: str, cwd: Path) -> int:
    """Read-only signoff check. Returns 0 if all signed, 1 if any unsigned.

    Only runs :func:`_assert_signoff_identity` — the dirty-tree,
    detached-HEAD, and merge-in-range guards deliberately do not fire
    on this path so the command works in CI detached-HEAD checkouts
    and pre-push hooks on dirty trees.
    """
    _name, email = _assert_signoff_identity(cwd)
    unsigned = _list_unsigned_commits(cwd, base, signoff_email=email)
    if not unsigned:
        typer.echo(f"All commits in `{base}..HEAD` are signed off.")
        return 0
    typer.echo(f"Unsigned commits in `{base}..HEAD`:")
    for short, subject in unsigned:
        typer.echo(f"  {short}  {subject}")
    return 1


# --------------------------------------------------------------------- #
# Mutation entry: full guard set + rebase --exec                        #
# --------------------------------------------------------------------- #


def _sign_commits(base: str, cwd: Path, force: bool) -> int:
    """Run all four safety asserts then ``git rebase --exec`` the range.

    Returns the rebase's exit code on rebase failure, or 0 on success.
    On rebase failure we surface git's stderr verbatim and leave any
    in-progress rebase state for the contributor to ``git rebase
    --abort`` — recovering from a partial rebase is the contributor's
    call, not ours.
    """
    _assert_clean_tree(cwd)
    _assert_branch_not_detached(cwd)
    _name, email = _assert_signoff_identity(cwd)
    _assert_no_merge_commits(cwd, base)

    unsigned = _list_unsigned_commits(cwd, base, signoff_email=email)
    if not unsigned:
        typer.echo("Nothing to do — all commits already signed.")
        return 0

    pushed = _count_pushed_commits_in_range(cwd, base)
    if pushed is not None and pushed > 0:
        # Resolve the upstream label for the warning message.
        upstream_res = _run_git(
            [
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                "@{upstream}",
            ],
            cwd=cwd,
            check=False,
        )
        upstream = upstream_res.stdout.strip() or "@{upstream}"
        typer.echo(
            f"warning: {pushed} commits in `{base}..HEAD` have already "
            f"been pushed to `{upstream}`. After rebase you'll need "
            f"`git push --force-with-lease` to update the remote.",
        )
        if not force:
            try:
                proceed = typer.confirm("Continue?", default=False)
            except typer.Abort:
                proceed = False
            if not proceed:
                typer.echo("Aborted.")
                raise typer.Abort()

    rebase_res = _run_git(
        [
            "rebase",
            base,
            "--exec",
            "git commit --amend --no-edit -s",
        ],
        cwd=cwd,
        check=False,
    )
    if rebase_res.returncode != 0:
        typer.echo(
            f"error: git rebase failed (rc={rebase_res.returncode}). "
            f"You may need to `git rebase --abort` to recover.\n"
            f"{rebase_res.stderr}",
            err=True,
        )
        return rebase_res.returncode

    typer.echo(f"Signed off {len(unsigned)} commits.")
    return 0


# --------------------------------------------------------------------- #
# Typer commands                                                        #
# --------------------------------------------------------------------- #


def cmd_sign_commits(
    ctx: typer.Context,
    base: str = typer.Option(
        "main",
        "--base",
        help="Base branch to walk from (default: main).",
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help=(
            "Dry-run: list unsigned commits, exit 1 if any, exit 0 "
            "otherwise. Does not rewrite history."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Skip the pushed-commit safety prompt (the warning still "
            "prints so you know to `git push --force-with-lease` "
            "afterward)."
        ),
    ),
) -> None:
    """Rewrite unsigned commits on the current branch with a Signed-off-by trailer.

    Walks ``<base>..HEAD`` and rebases with ``git commit --amend
    --no-edit -s`` per commit. Refuses if the working tree is dirty,
    HEAD is detached, ``git config user.{name,email}`` is unset, or
    the range contains merge commits.
    """
    cwd = Path.cwd()
    if check:
        raise typer.Exit(code=_check_signoff(base=base, cwd=cwd))
    raise typer.Exit(code=_sign_commits(base=base, cwd=cwd, force=force))


def cmd_check_signoff(
    ctx: typer.Context,
    base: str = typer.Option(
        "main",
        "--base",
        help="Base branch to walk from (default: main).",
    ),
) -> None:
    """Dry-run alias for ``sign-commits --check``. Suitable for CI / pre-push hooks.

    Lists unsigned commits in ``<base>..HEAD`` and exits 1 if any are
    found, 0 otherwise. Does not modify history.
    """
    cwd = Path.cwd()
    raise typer.Exit(code=_check_signoff(base=base, cwd=cwd))
