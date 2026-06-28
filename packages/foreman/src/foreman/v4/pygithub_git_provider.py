"""PyGithubGitProvider — production GitProvider backed by PyGithub.

Tests use FakeGitProvider (Task 3.2); production uses this. The seam
matches the Protocol from foreman.v4.git_provider.

PyGithubGitProvider rebuilds its cached Github client iff
identity.get_role_token(role) returns a different string than the last
cached token. V4IdentityRegistry is the sole arbiter of when a new
installation token is minted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from github import Github, GithubException

from foreman.git_host import CommentRef
from foreman.v4.git_provider import PRNotFoundError, PRState
from foreman.v4.identity import V4IdentityRegistry

if TYPE_CHECKING:
    from github.Repository import Repository


def _resolve_merge_method(
    repo: Repository, preferred: str | None = None,
) -> str:
    """Pick the merge method to pass to ``pr.merge(merge_method=...)``.

    Reads the three repo-level allow booleans (``allow_squash_merge``,
    ``allow_rebase_merge``, ``allow_merge_commit``) and selects a method
    that the target repo will accept. Preference order when ``preferred``
    is not given (or given but disallowed): squash → rebase → merge.

    Parameters
    ----------
    repo:
        PyGithub :class:`~github.Repository.Repository` handle for the
        target repo. Must expose the three ``allow_*`` boolean attributes.
    preferred:
        Caller-supplied preference (e.g. from ``ProjectConfig.merge_method``
        in a future follow-up). If the method is allowed on this repo the
        preferred value is returned; otherwise the function falls back to
        the first allowed method in the preference order.

    Raises
    ------
    ValueError
        If none of the three repo flags are ``True`` — which should not
        happen on a real GitHub repo, but raises a descriptive error rather
        than letting ``pr.merge()`` produce a cryptic 405.
    """
    allowed: dict[str, bool] = {
        "squash": bool(repo.allow_squash_merge),
        "rebase": bool(repo.allow_rebase_merge),
        "merge": bool(repo.allow_merge_commit),
    }
    if preferred is not None and allowed.get(preferred):
        return preferred
    for method in ("squash", "rebase", "merge"):
        if allowed[method]:
            return method
    raise ValueError(
        f"repo has no allowed merge method "
        f"(allow_squash_merge={allowed['squash']}, "
        f"allow_rebase_merge={allowed['rebase']}, "
        f"allow_merge_commit={allowed['merge']}); "
        f"cannot call pr.merge()"
    )


#: GitHub PR ``mergeable_state`` values that we treat as "CI passing"
#: for the purposes of v4 routing decisions. Public (no underscore) so
#: other modules — notably ``foreman.roles.worker`` for the BLOCKED-retry
#: existing-impl-PR idempotency path (issue #342) — can import this set
#: instead of redefining it. ``"clean"`` is GitHub's "all green" state;
#: ``"unstable"`` means required checks passed but at least one optional
#: check failed, which is still a green-light for our purposes.
CI_PASSING_MERGEABLE_STATES = frozenset({"clean", "unstable"})


class PyGithubGitProvider:
    def __init__(
        self,
        *,
        identity: V4IdentityRegistry,
        role: str,
        repo_full_name: str,
    ) -> None:
        """Construct the provider.

        Parameters
        ----------
        identity:
            :class:`~foreman.v4.identity.V4IdentityRegistry` that owns
            token freshness. ``_gh`` delegates to
            ``identity.get_role_token(role)`` on every access; the
            registry decides when to mint a fresh installation token.
        role:
            Role name whose token is used for GitHub API calls, e.g.
            ``"orchestrator"``.
        repo_full_name:
            ``owner/name`` slug, e.g. ``"jeffrichley/algokit"``.
        """
        self._identity = identity
        self._role = role
        self._repo_full_name = repo_full_name
        self._cached_token: str | None = None
        self._cached_github: Github | None = None
        self._cached_repo: Repository | None = None

    @property
    def _gh(self) -> Github:
        """Return a ``Github`` client backed by the current installation token.

        Delegates token-freshness to the identity registry: calls
        ``self._identity.get_role_token(self._role)`` on every access
        and rebuilds the ``Github`` client iff the returned token string
        differs from the last cached token. Lazy: the registry is NOT
        queried at construction time — only on first ``_gh`` access.

        Callers MUST go through ``self._gh`` for every API access; do
        not cache a ``Github`` reference outside this property — a
        stashed reference clings to the old token and silently 401s
        once it expires.
        """
        current_token = self._identity.get_role_token(self._role)
        if self._cached_token != current_token:
            self._cached_github = Github(current_token)
            self._cached_token = current_token
            # Repository handle is scoped to the OLD Github client's
            # requester; invalidate so the next ``_repo`` access
            # re-fetches via the freshly-built client.
            self._cached_repo = None
        # mypy can't track the in-lockstep assignment invariant above;
        # _cached_github is non-None after the if-branch runs OR if we
        # fell through (cache hit). assert keeps the type narrowed and
        # documents the invariant for readers.
        assert self._cached_github is not None
        return self._cached_github

    @property
    def _repo(self) -> Repository:
        """Return a :class:`Repository` handle for the configured repo.

        Cached alongside ``_gh`` — when ``_gh`` detects a new token and
        rebuilds the client, ``_cached_repo`` is invalidated and the
        next access re-fetches via the new client.

        Always invokes ``self._gh`` first so its token-equality check
        fires on every ``_repo`` access. Otherwise the Poller's hot path
        (``self._repo.get_issues(...)`` every tick) would short-circuit
        on the cached Repository handle and the rebuild path inside
        ``_gh`` would never trigger.
        """
        gh = self._gh
        if self._cached_repo is None:
            self._cached_repo = gh.get_repo(self._repo_full_name)
        return self._cached_repo

    def get_pr_state(self, *, project: str, pr_number: int) -> PRState:
        try:
            pr = self._repo.get_pull(pr_number)
        except GithubException as exc:
            if exc.status == 404:
                raise PRNotFoundError(f"{project}#{pr_number}") from exc
            raise
        return PRState(
            merged=bool(pr.merged),
            mergeable=bool(pr.mergeable),
            ci_passing=(pr.mergeable_state in CI_PASSING_MERGEABLE_STATES),
            base_ref=pr.base.ref,
            # foreman#416: surface the raw mergeable_state so the
            # merge-healer registry can dispatch on "behind" / future
            # corner cases. ci_passing above stays derived from it.
            mergeable_state=pr.mergeable_state or "",
            # foreman#443: GitHub sets pr.state == "closed" for both
            # merged PRs and PRs closed-without-merge. ImplApprovedState
            # uses this to distinguish "closed without merge" (NeedsHelp)
            # from "still open" (BLOCKED self-loop). A merged PR has both
            # merged=True and closed=True; callers must check merged first.
            closed=(pr.state == "closed"),
        )

    def merge_pr(self, *, project: str, pr_number: int) -> None:
        """Merge the PR via GitHub's REST API.

        Same mechanism for spec PRs (SpecReviewState) and impl PRs
        (MergingState). Phase 8d.19 dropped the MergeQueue-based path
        because most projects don't have MergeQueue configured —
        ``pr.merge()`` is the universal fallback that works everywhere.
        Callers (specifically MergingState) gate this call on a
        ``get_pr_state`` check that confirms the PR is mergeable + CI
        passing, so the failure modes are limited.

        The merge method is resolved automatically from the repo's allowed
        merge methods via ``_resolve_merge_method`` (preference order:
        squash → rebase → merge). This avoids HTTP 405 on squash-only
        repos like ``jeffrichley/agent_core`` where PyGithub's default
        ``"merge"`` method is disabled.
        """
        repo = self._repo
        pr = repo.get_pull(pr_number)
        pr.merge(merge_method=_resolve_merge_method(repo))

    def update_branch(self, *, project: str, pr_number: int) -> None:
        """Update the PR's branch from base via ``pr.update_branch()``.

        foreman#416: ``BehindBranchHealer`` calls this to self-heal a
        ``mergeable_state == "behind"`` PR. PyGithub maps it to
        ``PUT /repos/{owner}/{repo}/pulls/{n}/update-branch``. ``project``
        is accepted for Protocol-shape symmetry but unused — this provider
        is locked to one repo at construction (same pattern as
        ``merge_pr`` / ``close_issue``).
        """
        pr = self._repo.get_pull(pr_number)
        pr.update_branch()

    def list_open_issues_with_label(
        self, *, project: str, label: str,
    ) -> list[int]:
        issues = self._repo.get_issues(state="open", labels=[label])
        return [issue.number for issue in issues if issue.pull_request is None]

    def add_labels(
        self, *, project: str, issue_number: int, labels: set[str],
    ) -> None:
        """Add the given labels to the issue without touching others.

        PyGithub's ``add_to_labels`` takes label names as positional
        args. The underlying REST endpoint is additive — it does NOT
        replace the existing label set, so the trigger label and any
        operator-applied labels survive. Sort for deterministic call
        shape (useful for log assertions and snapshot tests).
        """
        if not labels:
            # PyGithub's add_to_labels raises with no args; skip the
            # call entirely on an empty set to make the impl
            # robust for callers that may pass {} as a no-op.
            return
        issue = self._repo.get_issue(issue_number)
        issue.add_to_labels(*sorted(labels))

    def close_issue(self, *, project: str, issue_number: int) -> None:
        """Close the GitHub issue, idempotently.

        Called by MergingState after the impl PR merges so the
        originating issue reaches "closed" alongside the PR's "merged"
        — the algokit#23 dogfood at 17:00 UTC 2026-06-16 reached Done
        via pr.merge() but left the issue OPEN; this method is the
        v4 plumbing that closes that loop.

        ``project`` is accepted for Protocol-shape symmetry (every
        method on :class:`GitProvider` takes it for ``RoutingGitProvider``)
        but unused here — this provider is locked to one repo at
        construction. Same pattern as ``merge_pr``.

        The ``state == "closed"`` guard makes the call idempotent at the
        caller level too: PyGithub's ``edit(state="closed")`` on an
        already-closed issue would still send a PATCH (wasting a
        request and creating a tiny reopen race window). The guard
        sidesteps both.
        """
        issue = self._repo.get_issue(issue_number)
        if issue.state != "closed":
            issue.edit(state="closed")

    def remove_labels(
        self, *, project: str, issue_number: int, labels: set[str],
    ) -> None:
        """Remove the given labels from the issue, swallowing 404s.

        PyGithub exposes ``remove_from_labels`` per-label (the REST API
        has no bulk-remove endpoint). When the label isn't on the
        issue, the DELETE returns 404 and PyGithub raises
        :class:`UnknownObjectException`. The Protocol contract is
        idempotent on missing labels, so we swallow that exception and
        continue with the rest. Sort for deterministic call ordering.
        """
        if not labels:
            return
        # Local import to avoid widening the module-level import surface
        # — UnknownObjectException is only needed in this one path.
        from github import UnknownObjectException

        issue = self._repo.get_issue(issue_number)
        for label_name in sorted(labels):
            try:
                issue.remove_from_labels(label_name)
            except UnknownObjectException:
                # Label not on the issue — idempotent no-op.
                continue

    def delete_branch(
        self, *, project: str, branch_name: str,
    ) -> None:
        """Delete the branch, idempotently.

        Reset CLI calls this to scrub foreman/issue-N branches on a
        ticket reset. The Protocol contract is idempotent — if the ref
        is already gone, that's the success state.

        GitHub returns 404 when the ref doesn't exist; some races
        surface 422 instead (ref gone but cached). Both translate to
        "already absent" from reset's perspective, so we swallow them.
        Any other GithubException re-raises.
        """
        try:
            ref = self._repo.get_git_ref(f"heads/{branch_name}")
            ref.delete()
        except GithubException as exc:
            if exc.status in (404, 422):
                return
            raise

    def close_pr(self, *, project: str, pr_number: int) -> None:
        """Close the PR without merging, idempotently.

        Reset CLI calls this to drop in-flight PRs on a ticket reset.
        PyGithub raises ``GithubException`` with status 422
        ("Validation Failed") when ``edit(state="closed")`` is called
        on an already-closed PR, and 404 if the PR doesn't exist —
        both map to "already gone" for reset, so we swallow them.
        Any other GithubException re-raises.
        """
        try:
            pr = self._repo.get_pull(pr_number)
            pr.edit(state="closed")
        except GithubException as exc:
            if exc.status in (404, 422):
                return
            raise

    def find_open_pr_by_head_branch(
        self, *, project: str, branch_name: str,
    ) -> int | None:
        """Return the number of the open PR with the given head branch.

        Reset CLI calls this to discover the impl PR attached to a
        foreman/issue-N branch (no separate ticket record links them).
        GitHub's PR search wants ``head`` as ``"owner:branch"`` — bare
        branch names silently match nothing. Returns the first match's
        number, or ``None`` when no open PR has that head.
        """
        repo = self._repo
        head_filter = f"{repo.owner.login}:{branch_name}"
        for pr in repo.get_pulls(state="open", head=head_filter):
            return pr.number
        return None

    def get_issue_labels(
        self, *, project: str, issue_number: int,
    ) -> set[str]:
        """Return the set of label names currently on the issue.

        Reset CLI's discovery phase uses this to enumerate which
        ``foreman:*`` labels are on the issue so the operator-facing
        plan can name them explicitly. ``project`` is accepted for
        Protocol-shape symmetry but unused — this provider is locked
        to one repo at construction, same pattern as ``merge_pr``.
        """
        issue = self._repo.get_issue(issue_number)
        return {label.name for label in issue.labels}

    def get_issue_comments(
        self, *, project: str, issue_number: int,
    ) -> list[CommentRef]:
        """Fetch the issue's comments in chronological order (oldest first).

        Goes through ``self._repo`` → ``self._gh``, which triggers the
        token-equality check. This is the seam that fixes the original
        401 bug: the legacy ``GitHostProvider`` took a static client;
        this method always uses the current token-delegated client.

        ``project`` is accepted for Protocol-shape symmetry but unused —
        the provider is locked to one repo at construction.
        """
        issue = self._repo.get_issue(issue_number)
        return [
            CommentRef(
                author_login=c.user.login,
                posted_at=c.created_at,
                body=c.body,
            )
            for c in issue.get_comments()
        ]

    def post_issue_comment(
        self, *, project: str, issue_number: int, body: str,
    ) -> None:
        """Post a new comment on the issue via PyGithub.

        Goes through ``self._repo`` → ``self._gh`` (refresh seam).
        ``project`` is accepted for Protocol-shape symmetry but unused.
        """
        issue = self._repo.get_issue(issue_number)
        issue.create_comment(body)
