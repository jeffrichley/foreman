"""Protocol for the GitHub-side surface the action executor needs.

Real impl wraps PyGithub + the subprocess spawner. Tests use a recording
fake. Keeping the protocol thin keeps the action layer pure-data-shaped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from foreman.config import MergeMechanism


@dataclass(frozen=True)
class PRMergeability:
    """Snapshot of a PR's live mergeability state, read at action-execution
    time via :meth:`ReconcilerHost.get_pr_mergeability`.

    ``state`` is one of GitHub's ``mergeStateStatus`` enum values, returned
    uppercase as GitHub sends it: ``CLEAN``, ``BEHIND``, ``BLOCKED``,
    ``UNSTABLE``, ``DIRTY``, ``UNKNOWN``, ``DRAFT``, ``HAS_HOOKS``. The
    new ``attempt_merge_*`` actions branch on this string per issue
    #165's state-machine table.

    ``failing_required_check_count`` counts required check runs whose
    ``conclusion`` is in ``{FAILURE, CANCELLED, TIMED_OUT, ACTION_REQUIRED,
    STALE}``. Only required checks count — non-required checks may be red
    on a healthy PR (linting, optional CI lanes).

    ``pending_required_check_count`` counts required check runs whose
    ``conclusion`` is ``None`` (still running) OR whose ``status`` is not
    ``COMPLETED``. The action handler uses this to distinguish
    ``BLOCKED + running CI`` (wait) from ``BLOCKED + at least one
    required check finished non-SUCCESS`` (needs-help).
    """

    state: str
    failing_required_check_count: int
    pending_required_check_count: int


class ReconcilerHost(Protocol):
    """The host side-effect surface for v3 actions."""

    def add_label(self, *, owner: str, repo: str, issue: int, label: str) -> None: ...
    def remove_label(self, *, owner: str, repo: str, issue: int, label: str) -> None: ...
    def post_comment(self, *, owner: str, repo: str, issue: int, body: str) -> None: ...
    def merge_pr(
        self,
        *,
        owner: str,
        repo: str,
        pr_number: int,
        mechanism: MergeMechanism,
    ) -> None:
        """Merge a PR via the project's configured mechanism.

        ``mechanism`` is the EFFECTIVE per-project value resolved upstream
        in the executor (``ActionContext.merge_mechanism``). The host
        dispatches on it:

        - ``direct``: synchronous ``pr.merge()`` against the GH API.
          Today's behavior; fails with the strict-status-checks-policy
          error when main has moved since the PR opened.
        - ``queue``: defer to GitHub MergeQueue. Requires the base
          branch's branch-protection ruleset to have MergeQueue enabled
          AND the orchestrator-bot App to hold the ``Merge queues:
          write`` permission. See foreman#158.

        Wiring landed in foreman#161 (this PR); the queue-specific
        implementation lands in a follow-up.
        """
        ...
    def get_pr_mergeability(
        self, *, owner: str, repo: str, pr_number: int
    ) -> PRMergeability:
        """Return the PR's live ``mergeStateStatus`` + required-check counts.

        Implementation note: GitHub computes ``mergeStateStatus`` lazily on
        direct PR-lookup. A bulk PR-list query (the observer's path) often
        returns ``UNKNOWN`` for every PR; reading it here at action-execution
        time forces the computation and gives the state machine an
        accurate decision input.

        Used by the ``attempt_merge_plan`` / ``attempt_merge_impl`` action
        handlers introduced for foreman#165's state-machine-driven merge
        phase. Raises on GitHub errors so the executor's catch path can
        record an ``error`` termination row; the next poll re-evaluates.
        """
        ...
    def update_branch(self, *, owner: str, repo: str, pr_number: int) -> None:
        """Call GitHub's ``updatePullRequestBranch`` GraphQL mutation.

        Server-side rebase of the PR's head ref onto the base branch's head.
        Used by the ``attempt_merge_*`` handlers when ``mergeStateStatus`` is
        ``BEHIND`` — GitHub then recomputes mergeability and the next poll
        re-reads the live state.

        Raises on GitHub errors (e.g., permission denied, branch protection
        blocks the bot, conflict surfaces during rebase) so the executor's
        catch path can record an ``error`` termination row; the next poll
        re-evaluates.
        """
        ...
    def dispatch_role(
        self,
        *,
        role: str,
        target: str | None,
        owner: str,
        repo: str,
        issue: int,
        pr_number: int | None,
        start_log_id: int,
        project: str,
    ) -> int:
        """Spawn the role subprocess (planner|reviewer|fixer|worker). Returns the PID.

        ``target`` is the per-dispatch target for roles that act on more than
        one artifact shape — ``"spec_pr"`` or ``"impl_pr"`` for the Reviewer
        and Fixer. Planner and Worker pass ``None``. The host plumbs ``target``
        into the subprocess argv via ``--target`` so the role's CLI dispatches
        the right prompt + entry-label check (foreman v3 rescue Stage 2).

        ``start_log_id`` is the id of the 'running' row the executor wrote
        immediately before calling this method. The host owns writing the
        termination row when the subprocess exits — see
        ``V3GitHubHost._track_subprocess_completion``.

        ``project`` is the project name the dispatched action belongs to —
        callers MUST pass the project of the snapshot that fired the rule,
        not a host-wide default. One host instance serves all registered
        projects; baking a single project_name into the host produces
        cross-project mis-dispatch (foreman v3 dogfood bug, 2026-06-04).
        Plumbed into the subprocess via ``--project``.
        """
        ...
