"""GitHub concrete implementation of :class:`~foreman.git_host.GitHostProvider`.

Combines PyGithub (for issue/PR/label API calls) and ``subprocess`` git
(for worktree operations). Each instance carries a single role's
:class:`~foreman.git_host.BotIdentity`; rebuild when the underlying
installation token is refreshed.

Git push auth uses the GitHub-Apps convention of an HTTPS remote URL
embedding the installation token:

    https://x-access-token:<token>@github.com/<owner>/<repo>.git

Commit attribution uses the App's slug + numeric id:

    git config user.name  "<slug>[bot]"
    git config user.email "<app_id>+<slug>[bot]@users.noreply.github.com"

Both conventions are verified end-to-end in
``~/.wren/scratch/foreman-app-auth-spike.py`` (steps 7-8).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from github import Github

from foreman.git_host import BotIdentity, GitHostProvider, IssueRef, PRRef
from foreman.git_hosts._errors import GitCommandError

# Env vars that would mis-direct any Python / uv tool running inside a
# DIFFERENT repo's worktree (e.g. a target repo's pre-push hook calling
# ``uv run --no-sync just check``).
#
# When Foreman runs ``git push`` via subprocess, the target repo's hook
# inherits the parent's environment. If VIRTUAL_ENV (or any of the vars
# below) point at Foreman's venv, ``uv run --no-sync`` detects the
# mismatch with the target worktree, ignores the env var, and creates a
# fresh empty ``.venv`` in the target repo — subsequent mypy/pytest then
# fail because the new venv has no deps installed.
#
# Issue #10 surfaced this on 3 consecutive Foreman dogfood runs on
# 2026-05-31. Workaround was ``env -u VIRTUAL_ENV git push`` by hand.
#
# UV_CACHE_DIR is intentionally NOT on the blocklist — cache sharing
# across repos is safe and desirable (faster syncs).
_FOREIGN_WORKTREE_BLOCKED_ENV_VARS = frozenset(
    {
        # --- Python interpreter / module path ---
        "VIRTUAL_ENV",  # load-bearing — the one that triggered #10
        "PYTHONHOME",  # would mis-direct python interpreter selection
        "PYTHONPATH",  # would pull foreman modules into hook imports
        "CONDA_PREFIX",  # uv also detects this as an "active env"
        # --- uv project / interpreter selection ---
        # Sourced from https://docs.astral.sh/uv/reference/environment/
        "UV_PROJECT",  # equivalent to --project
        "UV_PROJECT_ENVIRONMENT",  # path to project venv dir
        "UV_WORKING_DIR",  # equivalent to --directory
        "UV_PYTHON",  # path to interpreter for all ops
        "UV_SYSTEM_PYTHON",  # use first python on PATH
        "UV_PYTHON_PREFERENCE",  # system vs managed
        "UV_PYTHON_SEARCH_PATH",  # PATH override for python discovery
        "UV_MANAGED_PYTHON",  # force use of uv-managed python
        "UV_NO_MANAGED_PYTHON",  # forbid uv-managed python
    }
)


def _filtered_subprocess_env() -> dict[str, str]:
    """Return ``os.environ`` minus env vars that would mis-direct a foreign worktree.

    See :data:`_FOREIGN_WORKTREE_BLOCKED_ENV_VARS` for rationale.

    Returns:
        A copy of the current process environment with the blocklist removed.
        Everything else (PATH, HOME, USERPROFILE, GIT_*, GH_TOKEN, LANG,
        UV_CACHE_DIR, ...) is preserved so the target repo's hooks behave
        the same as they would for a human invocation.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if k not in _FOREIGN_WORKTREE_BLOCKED_ENV_VARS
    }


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
    def configure_worktree_identity(self, worktree_path: Path) -> None:
        user_name = f"{self._identity.slug}[bot]"
        user_email = (
            f"{self._identity.user_id}+{self._identity.slug}[bot]"
            "@users.noreply.github.com"
        )
        self._git(worktree_path, "config", "user.name", user_name)
        self._git(worktree_path, "config", "user.email", user_email)

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
        self._git(worktree_path, "commit", "-m", message)
        result = self._git(worktree_path, "rev-parse", "HEAD")
        return result.stdout.strip()

    def push_branch(self, worktree_path: Path, branch: str) -> None:
        # Determine owner/repo from the existing remote, then rewrite the
        # remote URL to embed the installation token. Using -c http.extraheader
        # would leak the token into ~/.git config; the URL approach scopes the
        # secret to one subprocess call.
        remote_url = self._git(
            worktree_path, "config", "--get", "remote.origin.url"
        ).stdout.strip()
        repo_slug = _extract_repo_slug(remote_url)
        push_url = (
            f"https://x-access-token:{self._identity.token}"
            f"@github.com/{repo_slug}.git"
        )
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
    def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        """Run a ``git`` subprocess, surfacing stderr and redacting tokens on failure.

        On ``CalledProcessError`` we raise :class:`GitCommandError` so callers
        see git's actual stderr (Defect B) and so any token-shaped strings in
        the argv or stderr are redacted before they hit logs or tracebacks
        (Defect C).
        """
        try:
            return subprocess.run(
                ["git", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
                env=_filtered_subprocess_env(),
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
