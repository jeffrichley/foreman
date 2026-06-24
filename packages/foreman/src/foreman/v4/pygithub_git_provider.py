"""PyGithubGitProvider — production GitProvider backed by PyGithub.

Tests use FakeGitProvider (Task 3.2); production uses this. The seam
matches the Protocol from foreman.v4.git_provider.

Token-refresh seam
------------------
GitHub App installation tokens expire after 1 hour. v4's
:class:`~foreman.v4.identity.V4IdentityRegistry` mints + caches them
with a pre-expiry refresh window, but that refresh is only useful if
the consumer ASKS for a fresh token. ``PyGithub.Github(token)`` stores
the token at construction and never reaches back for a new one — so a
long-running daemon that constructs the client once at bootstrap dies
at minute ~60 with ``BadCredentialsException: 401``.

The fix is a factory seam: ``PyGithubGitProvider`` takes a callable
that mints a fresh ``Github`` client (which internally calls
``identity.get_role_token(...)``), and rebuilds the cached client when
it's older than ``refresh_after_seconds`` (default 50 min = 3000s — well
inside the 1-hour TTL with safety margin). The :class:`Repository`
handle lives INSIDE each ``Github`` instance, so rebuilding the client
invalidates it too.

Cooperation with V4IdentityRegistry's pre-expiry safety window
--------------------------------------------------------------
A subtle gotcha bit dogfood at minute 60 on 2026-06-15: rebuilding the
``Github`` client only helps if the factory closure pulls a FRESH
installation token from the identity registry. The registry's
``get_role_token`` will reuse its cached token until the token enters
its pre-expiry safety window. So the rebuild interval here and the
identity registry's safety window must cooperate:

    safety_window > (token_lifetime - refresh_after_seconds)

With token_lifetime = 3600s and refresh_after_seconds = 3000s, the
registry's ``_REFRESH_SAFETY_SECONDS`` must be > 600s. It is currently
900s — so when we rebuild here at t=3000s, the cached token (minted at
t=0, expiring at t=3600) has 600s left, which is INSIDE the 900s safety
window, so the registry mints a fresh one. Every PyGithub rebuild
reliably gets a brand-new installation token.

Do NOT raise ``_DEFAULT_REFRESH_AFTER_SECONDS`` above ~3300s without
raising ``V4IdentityRegistry._REFRESH_SAFETY_SECONDS`` in lockstep, or
the cooperation breaks and the daemon will 401 at minute ~60.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from github import GithubException

from foreman.v4.git_provider import PRNotFoundError, PRState

if TYPE_CHECKING:
    from github import Github
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

# Default refresh window: 50 minutes (3000 seconds). Load-bearing — the
# GitHub App installation token TTL is 1 hour (3600s); rebuilding the
# client at 50 min keeps us comfortably inside that window even if the
# next API call happens a few minutes later. See the module-level
# "Cooperation with V4IdentityRegistry's pre-expiry safety window"
# docstring for why this constant and V4IdentityRegistry's
# _REFRESH_SAFETY_SECONDS (currently 900s) must change in lockstep.
_DEFAULT_REFRESH_AFTER_SECONDS = 3000.0


class PyGithubGitProvider:
    def __init__(
        self,
        *,
        github_factory: Callable[[], Github],
        repo_full_name: str,
        clock: Callable[[], float] = time.time,
        refresh_after_seconds: float = _DEFAULT_REFRESH_AFTER_SECONDS,
    ) -> None:
        """Construct the provider.

        Parameters
        ----------
        github_factory:
            Zero-arg callable that returns a fresh ``Github`` client.
            Called lazily on first ``_gh`` access AND on each refresh
            past ``refresh_after_seconds``. Production wires this to
            ``lambda: Github(identity.get_role_token("orchestrator"))``
            so every rebuild pulls a fresh installation token from the
            identity registry.
        repo_full_name:
            ``owner/name`` slug, e.g. ``"jeffrichley/algokit"``.
        clock:
            Injectable seconds-since-epoch source. Defaults to
            :func:`time.time`. Tests monkey-patch this to simulate
            clock advance without sleeping.
        refresh_after_seconds:
            How long a cached ``Github`` client can live before the
            next ``_gh`` access rebuilds it. Default 50 min = 3000s; see the
            module-level docstring for why this is load-bearing.
        """
        self._github_factory = github_factory
        self._repo_full_name = repo_full_name
        self._clock = clock
        self._refresh_after = refresh_after_seconds
        self._cached_github: Github | None = None
        self._cached_at: float | None = None
        self._cached_repo: Repository | None = None

    @property
    def _gh(self) -> Github:
        """Return a ``Github`` client with a non-expired token.

        Rebuilds the cached client (and invalidates the cached
        :class:`Repository` handle) when the cached client is older
        than ``refresh_after_seconds``. Lazy: the factory is NOT
        invoked at construction time — only on first access.

        Callers MUST go through ``self._gh`` for every API access; do
        not cache a ``Github`` reference outside this property — a
        stashed reference clings to the old client across refresh and
        silently 401s once its token expires.
        """
        now = self._clock()
        # _cached_github and _cached_at are set together; check _cached_at
        # alone — its None state proves the cache is uninitialized.
        if self._cached_at is None or (now - self._cached_at) > self._refresh_after:
            self._cached_github = self._github_factory()
            self._cached_at = now
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

        Cached alongside ``_gh`` — when ``_gh`` refreshes, the
        ``_cached_repo`` is dropped and the next access re-fetches via
        the new client. Cheap call (one extra GET) per refresh window.

        Always invoke ``self._gh`` first so its time-based rebuild check
        fires on every ``_repo`` access. Otherwise the Poller's hot path
        (``self._repo.get_issues(...)`` every tick) would short-circuit
        on the cached Repository handle and the rebuild path inside
        ``_gh`` would never trigger — which is the bug that killed the
        v4 daemon at minute 60 on 2026-06-15 dogfood, despite 8c.3's
        rebuild logic being correct in isolation.
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
