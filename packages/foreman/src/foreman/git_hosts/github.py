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
from foreman.git_host import BotIdentity, GitHostProvider, IssueRef, PRRef
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
        repo = self._client.get_repo(repo_slug)
        return repo.default_branch

    # ------------------------------------------------------------------
    # Worktree git operations
    # ------------------------------------------------------------------
    def _identity_env(self) -> dict[str, str]:
        """Build the env-var dict that scopes commit attribution to this
        provider's bot identity for a single ``git commit`` subprocess.

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
    ) -> str:
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
        self._git(worktree_path, "commit", "-m", message, env_extra=self._identity_env())
        result = self._git(worktree_path, "rev-parse", "HEAD")
        return result.stdout.strip()

    def push_branch(self, worktree_path: Path, branch: str) -> None:
        # Determine owner/repo from the existing remote, then rewrite the
        # remote URL to embed the installation token. Using -c http.extraheader
        # would leak the token into ~/.git config; the URL approach scopes the
        # secret to one subprocess call.
        remote_url = self._git(worktree_path, "config", "--get", "remote.origin.url").stdout.strip()
        repo_slug = _extract_repo_slug(remote_url)
        push_url = f"https://x-access-token:{self._identity.token}@github.com/{repo_slug}.git"
        self._git(worktree_path, "push", push_url, f"{branch}:{branch}")

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
        repo = self._client.get_repo(repo_slug)
        issue = repo.get_issue(issue_number)
        for label in remove:
            issue.remove_from_labels(label)
        for label in add:
            issue.add_to_labels(label)

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
