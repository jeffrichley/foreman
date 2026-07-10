"""Shared PR-lookup helper for role dispatchers.

Both the Worker and the Planner need to probe for an already-open PR on a
given head branch — the Worker for spec-PR lookup + impl-PR
idempotency (issue #342), the Planner for spec-PR idempotency on crash
re-run (Stage 1b). Keeping the probe in one place avoids the
divergent-duplication smell of two copies drifting apart.
"""

from __future__ import annotations

from github.PullRequest import PullRequest
from github.Repository import Repository


def find_open_pr_by_head_branch(repo: Repository, owner: str, branch: str) -> PullRequest | None:
    """Locate the open PR whose head branch matches ``branch``.

    Generalized from the original spec-PR-only ``_find_spec_pr`` helper
    so both callers in :func:`_run_worker_core` can reuse it (issue
    #342):

    - **Spec PR lookup** (the original use): caller passes
      ``branch=foreman/issue-<N>``. ``None`` means the spec PR has
      already merged + auto-deleted (the v4 normal path after
      ``SpecReviewState``), which is fine — the implementation still
      proceeds against the spec doc on disk; only the impl PR body's
      "Spec PR: #<N>" reference is omitted. Posting a
      ``spec_invalid_reason`` without a target PR is harmless (we skip
      the post and log a warning); the v4 state machine transition
      still fires.
    - **Existing impl PR detection** (issue #342, BLOCKED-retry
      idempotency): caller passes ``branch=foreman/impl-<N>``. A
      non-``None`` return means a previous Worker dispatch already
      opened the impl PR and is still polling its CI status; the
      Python-side push + ``create_pull`` MUST be skipped to avoid a
      GitHub 422 ("A pull request already exists") which would crash
      the Worker subprocess and transition the ticket to ``Failed``.

    The helper is a thin wrapper over
    ``repo.get_pulls(state="open", head=f"{owner}:{branch}")``. The
    query is stable: GitHub's REST search returns the open PRs whose
    head ref qualifier matches; for our branch-name conventions
    (``foreman/issue-<N>`` and ``foreman/impl-<N>``) at most one open
    PR can match in practice, so taking the first hit is safe.
    """
    head_qualifier = f"{owner}:{branch}"
    pulls = list(repo.get_pulls(state="open", head=head_qualifier))
    if not pulls:
        return None
    return pulls[0]
