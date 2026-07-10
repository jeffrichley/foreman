"""GitHub concrete implementation of :class:`~foreman.git_host.GitHostProvider`.

Combines PyGithub (for issue/PR/label API calls) and ``subprocess`` git
(for worktree operations). Each instance carries a single role's
:class:`~foreman.git_host.BotIdentity`; rebuild when the underlying
installation token is refreshed.

Git push auth uses the GitHub-Apps convention of an HTTPS remote URL
embedding the installation token:

    https://x-access-token:<token>@github.com/<owner>/<repo>.git

Commit attribution uses the App's slug + numeric id, injected via env
vars on the ``git commit`` subprocess (not via ``.git/config``):

    GIT_AUTHOR_NAME      = "<slug>[bot]"
    GIT_AUTHOR_EMAIL     = "<app_id>+<slug>[bot]@users.noreply.github.com"
    GIT_COMMITTER_NAME   = same as author
    GIT_COMMITTER_EMAIL  = same as author

The env-var route (foreman#53) is required because git worktrees share
``.git/config`` with the parent repo — persisting ``user.name`` /
``user.email`` there leaks the bot identity into subsequent human
commits in the same checkout. Env vars scope identity to the one
subprocess call.

Both conventions are verified end-to-end in
``~/.wren/scratch/foreman-app-auth-spike.py`` (steps 7-8).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from github import Github

from foreman._env_filter import filtered_subprocess_env as _filtered_subprocess_env
from foreman.git_host import BotIdentity, CommentRef, GitHostProvider, IssueRef, PRRef
from foreman.git_hosts._errors import GitCommandError

# The same filter is applied by :class:`~foreman.worktree.WorktreeManager`
# when running ``uv sync`` in a newly-created worktree — see the
# :mod:`foreman._env_filter` module docstring for the issue #10 history.


class GitHubProvider(GitHostProvider):
    """:class:`~foreman.git_host.GitHostProvider` backed by PyGithub + git CLI."""

    def __init__(self, *, identity: BotIdentity, client: Github) -> None:
        """Construct a provider bound to one role's bot identity.

        Args:
            identity: BotIdentity for the role (slug, user_id, token).
                The token is used for both PyGithub API calls and git push
                URL construction.
            client: PyGithub client. Caller (typically
                :class:`~foreman.identity.IdentityRegistry`) is responsible
                for authenticating it with the same installation token
                referenced by ``identity.token`` — this keeps token refresh
                centralized in one place.
        """
        self._identity = identity
        self._client = client

    # ------------------------------------------------------------------
    # Issue + repo queries
    # ------------------------------------------------------------------
    def get_issue(self, repo_slug: str, issue_number: int) -> IssueRef:
        """Fetch an issue via PyGithub and normalize it into an :class:`IssueRef`.

        ``title``/``body`` are coerced from PyGithub's ``None`` (which it
        returns for empty fields) to ``""`` so callers never have to
        null-check, and labels are flattened from ``Label`` objects to
        their plain names.
        """
        repo = self._client.get_repo(repo_slug)
        issue = repo.get_issue(issue_number)
        return IssueRef(
            number=issue.number,
            title=issue.title or "",
            body=issue.body or "",
            labels=[label.name for label in issue.labels],
            repo_slug=repo_slug,
        )

    def get_default_branch(self, repo_slug: str) -> str:
        """Look up the repository via PyGithub and return its default branch name."""
        repo = self._client.get_repo(repo_slug)
        return repo.default_branch

    def get_issue_comments(self, repo_slug: str, issue_number: int) -> list[CommentRef]:
        """Fetch the issue's comments in chronological order (oldest first).

        PyGithub returns ``Issue.get_comments()`` in chronological order
        by default; we ``sorted(...)`` defensively so callers can rely
        on the ordering even if PyGithub's default ever shifts. No
        filtering happens here — the foreman role-bot self-comments are
        filtered one layer up (see
        :func:`foreman.roles._prompt_helpers.filter_bot_self_comments`).
        """
        repo = self._client.get_repo(repo_slug)
        issue = repo.get_issue(issue_number)
        refs = [
            CommentRef(
                author_login=c.user.login,
                posted_at=c.created_at,
                body=c.body or "",
            )
            for c in issue.get_comments()
        ]
        return sorted(refs, key=lambda r: r.posted_at)

    # ------------------------------------------------------------------
    # Worktree git operations
    # ------------------------------------------------------------------
    def _identity_env(self) -> dict[str, str]:
        """Build env vars that scope commit attribution to this provider's bot identity.

        foreman#53: ``git config user.name`` writes ``.git/config`` which
        a worktree shares with its parent repo, so the bot identity leaks
        into every subsequent human commit in the same checkout. Env vars
        ``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*`` scope identity to one
        subprocess call without touching any persistent config.
        """
        bot_name = f"{self._identity.slug}[bot]"
        bot_email = f"{self._identity.user_id}+{self._identity.slug}[bot]@users.noreply.github.com"
        return {
            "GIT_AUTHOR_NAME": bot_name,
            "GIT_AUTHOR_EMAIL": bot_email,
            "GIT_COMMITTER_NAME": bot_name,
            "GIT_COMMITTER_EMAIL": bot_email,
        }

    def commit_files_to_worktree(
        self,
        worktree_path: Path,
        files: dict[str, str],
        message: str,
        *,
        provenance_trailers: list[str] | None = None,
    ) -> str:
        """Write ``files`` into the worktree, stage them, and commit under the bot identity.

        Handles the foreman#117 retry case: if a prior run already
        committed these exact contents (killed after commit but before
        push), staging is a no-op and a fresh ``git commit`` would fail
        with "nothing to commit" — this detects that empty-diff state via
        ``git diff --cached --quiet`` and returns the existing HEAD instead
        of erroring, so the caller can retry the push idempotently.
        ``provenance_trailers`` (issue #347), when given, are appended as
        ``--trailer`` flags in order (e.g. ``Supervised-by:`` then
        ``Signed-off-by:``).
        """
        for relpath, content in files.items():
            target = worktree_path / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            self._git(worktree_path, "add", "--", relpath)
        # foreman#117: when a prior Planner run on this issue was killed
        # AFTER committing but BEFORE pushing, the worktree HEAD already
        # carries the previous commit. The files we just wrote match
        # HEAD, `git add` is a no-op, and `git commit` would exit 1
        # ("nothing to commit, working tree clean") — bricking the
        # Planner on every retry. Detect that empty-staged state and
        # short-circuit: return the existing HEAD so the caller pushes
        # the prior commit instead of crashing on a no-op commit.
        diff_check = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=worktree_path,
            check=False,
            capture_output=True,
            text=True,
            env=_filtered_subprocess_env(),
        )
        if diff_check.returncode == 0:
            head = self._git(worktree_path, "rev-parse", "HEAD")
            return head.stdout.strip()
        # Issue #347: splice ``--trailer "<value>"`` per entry. Order is
        # preserved so the commit body carries the same trailer order
        # the caller specified (Supervised-by: then Signed-off-by: per
        # the Planner / Worker / Fixer wire-up).
        trailer_args: list[str] = []
        if provenance_trailers:
            for value in provenance_trailers:
                trailer_args.extend(["--trailer", value])
        self._git(
            worktree_path,
            "commit",
            "-m",
            message,
            *trailer_args,
            env_extra=self._identity_env(),
        )
        result = self._git(worktree_path, "rev-parse", "HEAD")
        return result.stdout.strip()

    def push_branch(self, worktree_path: Path, branch: str) -> None:
        """Push ``branch`` to origin, authenticating via an installation-token URL.

        Uses ``--force-with-lease`` so the push succeeds after a history-rewriting
        rebase or amend (e.g. the Fixer rebasing its impl branch onto origin/main).
        The flag is safe on bot-owned single-writer branches (``foreman/issue-*`` /
        ``foreman/impl-*``) and refuses to overwrite if the remote moved unexpectedly
        (unlike bare ``--force``).

        Reads the existing ``remote.origin.url`` to recover the owner/repo slug,
        then constructs an ``https://x-access-token:<token>@...`` push URL rather
        than using ``-c http.extraheader`` — the latter would leak the token into
        persistent git config, whereas the URL form scopes it to this one subprocess
        call.
        """
        remote_url = self._git(worktree_path, "config", "--get", "remote.origin.url").stdout.strip()
        repo_slug = _extract_repo_slug(remote_url)
        push_url = f"https://x-access-token:{self._identity.token}@github.com/{repo_slug}.git"
        self._git(worktree_path, "push", "--force-with-lease", push_url, f"{branch}:{branch}")

    # ------------------------------------------------------------------
    # PR + label API operations
    # ------------------------------------------------------------------
    def open_pull_request(
        self,
        repo_slug: str,
        title: str,
        body: str,
        base: str,
        head: str,
    ) -> PRRef:
        """Create the pull request via PyGithub and wrap the response in a :class:`PRRef`."""
        repo = self._client.get_repo(repo_slug)
        pr = repo.create_pull(title=title, body=body, base=base, head=head)
        return PRRef(
            number=pr.number,
            url=pr.html_url,
            title=pr.title,
            body=pr.body or "",
            branch=head,
            base_branch=base,
            repo_slug=repo_slug,
        )

    def update_issue_labels(
        self,
        repo_slug: str,
        issue_number: int,
        add: list[str],
        remove: list[str],
    ) -> None:
        """Remove ``remove`` labels then apply ``add`` labels to the issue, via PyGithub.

        Removals run before additions so a label present in both lists
        ends up applied (remove-then-add, not add-then-remove).
        """
        repo = self._client.get_repo(repo_slug)
        issue = repo.get_issue(issue_number)
        for label in remove:
            issue.remove_from_labels(label)
        for label in add:
            issue.add_to_labels(label)

    def post_issue_comment(
        self,
        repo_slug: str,
        issue_number: int,
        body: str,
    ) -> None:
        """Post ``body`` as a new comment on the issue via PyGithub."""
        repo = self._client.get_repo(repo_slug)
        issue = repo.get_issue(issue_number)
        issue.create_comment(body)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _git(
        cwd: Path,
        *args: str,
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a ``git`` subprocess, surfacing stderr and redacting tokens on failure.

        On ``CalledProcessError`` we raise :class:`GitCommandError` so callers
        see git's actual stderr (Defect B) and so any token-shaped strings in
        the argv or stderr are redacted before they hit logs or tracebacks
        (Defect C).

        ``env_extra`` merges into the filtered env for this one call —
        used by ``commit_files_to_worktree`` to scope ``GIT_AUTHOR_*`` /
        ``GIT_COMMITTER_*`` to the commit subprocess (foreman#53).
        """
        env = _filtered_subprocess_env()
        if env_extra:
            env = {**env, **env_extra}
        try:
            return subprocess.run(
                ["git", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
        except subprocess.CalledProcessError as exc:
            raise GitCommandError(
                f"git {args[0] if args else ''} failed",
                returncode=exc.returncode,
                cmd=exc.cmd if isinstance(exc.cmd, list) else [str(exc.cmd)],
                stderr=exc.stderr or "",
            ) from None


def _extract_repo_slug(remote_url: str) -> str:
    """Pull ``owner/name`` out of an HTTPS or SSH GitHub remote URL.

    Tolerates:
      * ``https://github.com/owner/name.git``
      * ``https://github.com/owner/name``
      * ``git@github.com:owner/name.git``
      * Existing ``x-access-token:...`` HTTPS form (rewritten between pushes)
    """
    url = remote_url.strip()
    if url.endswith(".git"):
        url = url[: -len(".git")]
    if url.startswith("git@github.com:"):
        return url[len("git@github.com:") :]
    marker = "github.com/"
    idx = url.find(marker)
    if idx == -1:
        raise ValueError(f"Cannot parse GitHub repo slug from remote URL: {remote_url!r}")
    return url[idx + len(marker) :]
