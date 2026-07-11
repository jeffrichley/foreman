"""RoutingGitProvider — per-project dispatch wrapper over GitProvider.

Why this exists
---------------
``PyGithubGitProvider`` is locked at construction to ONE ``repo_full_name``.
Every method takes a ``project=`` kwarg from the Protocol but the concrete
PyGithub impl ignores it — it always uses ``self._repo`` resolved from the
single hardcoded ``repo_full_name``. In a multi-project daemon, the
bootstrap created one PyGithubGitProvider per project but only the FIRST
was threaded into the Daemon's cross-project consumers (LabelObservability
observer + state-machine ``merge_pr`` calls). Result: any project ``i > 0``
would have its writes silently routed to project[0]'s repo, hitting 404
(best case) or mis-labeling the wrong issue (worst case, on issue-number
collision).

The fix is to wrap ``dict[project_name, GitProvider]`` in a dispatcher
that respects the ``project=`` kwarg every call carries. Cross-project
consumers (the Daemon's state machine, the LabelObservabilityObserver)
get a ``RoutingGitProvider``; per-project Pollers keep their own
GitProvider directly (they already operate on one project each, so the
dispatch hop would be redundant).

Why ``KeyError`` instead of a new exception class
--------------------------------------------------
The Protocol already publishes :class:`PRNotFoundError(LookupError)` for
its closest analog (unknown PR rather than unknown project). Routing-level
failures are a different concern (operator misconfiguration: a project
landed on a WorkItem that was never registered with the router), so
defining a dedicated :class:`UnknownProjectError` keeps the cause
distinguishable from PR-lookup misses while staying inside the
:class:`LookupError` family Python callers already catch.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Set as AbstractSet

from foreman.git_host import CommentRef
from foreman.v4.git_provider import GitProvider, PRState


class UnknownProjectError(LookupError):
    """No GitProvider registered for the named project.

    Raised at routing time, NOT at construction — the router accepts any
    mapping at __init__ and only fails when a call references a project
    that's missing. The error message names the known projects so an
    operator can diff config vs. live state without digging into a
    traceback.
    """


class RoutingGitProvider:
    """Dispatches every GitProvider call to the per-project provider.

    Implements the :class:`GitProvider` Protocol structurally. Holds an
    immutable snapshot of the ``{project_name: GitProvider}`` mapping
    taken at construction — mutating the dict the caller passed in does
    NOT mutate the router. Tests inject specific provider instances
    (typically :class:`FakeGitProvider`) keyed by project name and assert
    the right one was called.

    The class is intentionally stateless beyond the providers dict. All
    routing is by ``project`` kwarg; there is no fallback, no default,
    no implicit project-zero. Unknown projects raise
    :class:`UnknownProjectError` with a list of known names.
    """

    def __init__(self, *, providers: Mapping[str, GitProvider]) -> None:
        # Defensive copy: caller-side mutation after construction must
        # not affect routing. The dict is small (one entry per project),
        # so the copy is cheap.
        self._providers: dict[str, GitProvider] = dict(providers)

    def _resolve(self, project: str) -> GitProvider:
        try:
            return self._providers[project]
        except KeyError as exc:
            known = ", ".join(sorted(self._providers)) or "<none>"
            raise UnknownProjectError(
                f"No GitProvider registered for project {project!r}. Known: {known}.",
            ) from exc

    def list_open_issues_with_label(
        self,
        *,
        project: str,
        label: str,
    ) -> list[int]:
        """Route to ``project``'s provider to list open issues carrying ``label``."""
        return self._resolve(project).list_open_issues_with_label(
            project=project,
            label=label,
        )

    def get_pr_state(self, *, project: str, pr_number: int) -> PRState:
        """Route to ``project``'s provider to fetch the PR's merge state."""
        return self._resolve(project).get_pr_state(
            project=project,
            pr_number=pr_number,
        )

    def merge_pr(self, *, project: str, pr_number: int) -> None:
        """Route to ``project``'s provider to merge the PR."""
        self._resolve(project).merge_pr(
            project=project,
            pr_number=pr_number,
        )

    def update_branch(self, *, project: str, pr_number: int) -> None:
        """Route to ``project``'s provider to update the PR's branch from base."""
        self._resolve(project).update_branch(
            project=project,
            pr_number=pr_number,
        )

    def add_labels(
        self,
        *,
        project: str,
        issue_number: int,
        labels: AbstractSet[str],
    ) -> None:
        """Route to ``project``'s provider to add labels to the issue."""
        self._resolve(project).add_labels(
            project=project,
            issue_number=issue_number,
            labels=labels,
        )

    def remove_labels(
        self,
        *,
        project: str,
        issue_number: int,
        labels: AbstractSet[str],
    ) -> None:
        """Route to ``project``'s provider to remove labels from the issue."""
        self._resolve(project).remove_labels(
            project=project,
            issue_number=issue_number,
            labels=labels,
        )

    def close_issue(self, *, project: str, issue_number: int) -> None:
        """Route to ``project``'s provider to close the issue."""
        self._resolve(project).close_issue(
            project=project,
            issue_number=issue_number,
        )

    def delete_branch(
        self,
        *,
        project: str,
        branch_name: str,
    ) -> None:
        """Route to ``project``'s provider to delete the branch."""
        self._resolve(project).delete_branch(
            project=project,
            branch_name=branch_name,
        )

    def close_pr(self, *, project: str, pr_number: int) -> None:
        """Route to ``project``'s provider to close the PR without merging."""
        self._resolve(project).close_pr(
            project=project,
            pr_number=pr_number,
        )

    def find_open_pr_by_head_branch(
        self,
        *,
        project: str,
        branch_name: str,
    ) -> int | None:
        """Route to ``project``'s provider to find an open PR by head branch."""
        return self._resolve(project).find_open_pr_by_head_branch(
            project=project,
            branch_name=branch_name,
        )

    def get_issue_labels(
        self,
        *,
        project: str,
        issue_number: int,
    ) -> set[str]:
        """Route to ``project``'s provider to fetch the issue's current labels."""
        return self._resolve(project).get_issue_labels(
            project=project,
            issue_number=issue_number,
        )

    def get_issue_comments(
        self,
        *,
        project: str,
        issue_number: int,
    ) -> list[CommentRef]:
        """Route to ``project``'s provider to fetch the issue's comments."""
        return self._resolve(project).get_issue_comments(
            project=project,
            issue_number=issue_number,
        )

    def post_issue_comment(
        self,
        *,
        project: str,
        issue_number: int,
        body: str,
    ) -> None:
        """Route to ``project``'s provider to post a new comment on the issue."""
        self._resolve(project).post_issue_comment(
            project=project,
            issue_number=issue_number,
            body=body,
        )

    def register_provider(self, name: str, provider: GitProvider) -> None:
        """Add or replace the :class:`GitProvider` for ``name``.

        Called by :meth:`~foreman.v4.daemon.Daemon._apply_project_reload`
        when a new project is added (or an existing project's config
        changes — treated as a remove + re-add). Thread safety: this
        method runs on the Daemon's main tick thread; WorkerPool threads
        only read via :meth:`_resolve`, never write, so no lock is needed.
        """
        self._providers[name] = provider

    def unregister_provider(self, name: str) -> None:
        """Remove the :class:`GitProvider` for ``name``.

        Raises :class:`UnknownProjectError` if ``name`` is not registered,
        matching the error already raised by :meth:`_resolve` for consistency.
        Called by :meth:`~foreman.v4.daemon.Daemon._apply_project_reload`
        when a project is dropped. Thread safety: same as
        :meth:`register_provider`.
        """
        if name not in self._providers:
            known = ", ".join(sorted(self._providers)) or "<none>"
            raise UnknownProjectError(
                f"No GitProvider registered for project {name!r}. Known: {known}.",
            )
        del self._providers[name]
