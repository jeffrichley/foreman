"""GitProvider — narrow seam over PyGithub for the v4 state machine.

States that need to look at GitHub artifact state (spec PR mergeable?
impl PR ready? impl PR merged?) go through this Protocol. The PyGithub
concrete implementation lands in Phase 4; Phase 3 only needs the shape
+ the fake.

Label-write surface
-------------------
The label-write side of this Protocol is intentionally granular:
:meth:`add_labels` adds a set of labels without touching anything else
on the issue, :meth:`remove_labels` removes them. The earlier
``write_labels(labels)`` shape (Phase 8.1) replaced the entire label
set via PyGithub's ``set_labels``, which stripped the trigger label
(``foreman:plan``) every time the daemon stamped a state-progress
label — a 40-minute dogfood wedge on the morning of 2026-06-15.
Granular writes preserve the trigger label AND any operator-applied
labels untouched. The old ``write_labels`` was dropped in Phase 8c.4
since it had no out-of-tree consumers.

PR-merge surface
----------------
:meth:`merge_pr` is the only merge entry point — both ``SpecReviewState``
(spec PR after Reviewer-on-spec approves) and ``MergingState`` (impl PR
after Reviewer-on-impl approves AND GitHub reports the PR mergeable +
CI green) call it. Phase 8d.19 collapsed the previous two paths
(``merge_spec_pr`` for spec PRs, ``enqueue_merge_queue`` + ``merge_verdict``
polling for impl PRs) into one direct ``pr.merge()`` call — most projects
don't have MergeQueue configured, and the polling path looped forever on
those that didn't. Granular ``mergeable_state`` handling (CI-failed →
ImplFix, dirty → rebase, etc.) is deferred to foreman#317.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class PRNotFoundError(LookupError):
    """No PR matching this (project, pr_number)."""


@dataclass(frozen=True, slots=True)
class PRState:
    merged: bool
    mergeable: bool
    ci_passing: bool
    # The PR's base branch ref (e.g. "main"). Populated by
    # ``PyGithubGitProvider.get_pr_state`` from ``pr.base.ref``.
    # MergingState compares this against
    # ``ProjectConfig.dev_base_branch`` before calling ``merge_pr`` so a
    # wrong-base impl PR (foreman#341 / #347 / #357) can't silently
    # merge into the spec branch. Default ``""`` keeps existing test
    # fixtures that construct ``PRState`` without the field compatible;
    # in production the PyGithub path always populates it.
    base_ref: str = ""
    # foreman#416: GitHub's raw ``pr.mergeable_state`` string (e.g.
    # "clean", "behind", "dirty", "blocked", "unstable"). Surfaced raw so
    # the merge-healer registry (``foreman.v4.merge_healers``) can dispatch
    # on specific corner cases — ``BehindBranchHealer`` keys off
    # ``mergeable_state == "behind"`` to self-heal a base-advanced PR via
    # ``update_branch`` instead of escalating to NeedsHelp. ``ci_passing``
    # stays as-is (derived from ``mergeable_state in
    # CI_PASSING_MERGEABLE_STATES``); this field is additive. Default ``""``
    # keeps existing fixtures that construct ``PRState`` without the field
    # compiling, same pattern as ``base_ref``; production PyGithub always
    # populates it.
    mergeable_state: str = ""


class GitProvider(Protocol):
    def list_open_issues_with_label(
        self, *, project: str, label: str,
    ) -> list[int]: ...
    def get_pr_state(self, *, project: str, pr_number: int) -> PRState: ...
    def merge_pr(self, *, project: str, pr_number: int) -> None: ...
    def update_branch(self, *, project: str, pr_number: int) -> None:
        """Update the PR's branch from its base (GitHub "Update branch").

        foreman#416: the merge-healer ``BehindBranchHealer`` calls this to
        self-heal a ``mergeable_state == "behind"`` PR — the base advanced
        while the PR waited, and merging a behind PR would 405 (spec PRs)
        or loop BLOCKED forever (impl PRs). PyGithub maps this to
        ``pr.update_branch()`` (``PUT .../update-branch``). After the
        update lands the PR is no longer "behind", so a normal BEHIND
        heals in one cycle; the healer registry's bound (see
        ``merge_healers`` + ``attempt_merge``) catches pathological
        base-churn.
        """
        ...
    def add_labels(
        self, *, project: str, issue_number: int, labels: set[str]
    ) -> None:
        """Add the given labels to the issue.

        Does NOT touch any other labels — the trigger label
        (``foreman:plan``) and any operator-applied labels survive.
        Idempotent: adding a label that's already on the issue is a
        no-op.
        """
        ...

    def remove_labels(
        self, *, project: str, issue_number: int, labels: set[str]
    ) -> None:
        """Remove the given labels from the issue, if present.

        Silently no-ops on labels that aren't on the issue — the
        observer doesn't track which state labels were ever stamped,
        so a remove on a never-applied label is expected behavior
        (e.g., a ticket transitioning directly from Queued to
        SpecReview never had ``foreman:state-planning`` to remove).
        """
        ...

    def close_issue(self, *, project: str, issue_number: int) -> None:
        """Close the GitHub issue.

        Called by MergingState after the impl PR merges. Idempotent at
        GitHub's REST API level — closing an already-closed issue is a
        no-op rather than an error, so this method must not raise on the
        already-closed case.
        """
        ...

    def delete_branch(
        self, *, project: str, branch_name: str,
    ) -> None:
        """Delete a remote branch.

        Idempotent: if the branch doesn't exist (404 / 422), this is a
        no-op rather than an error. Used by ``foreman reset`` to clear
        stale ``foreman/issue-N`` / ``foreman/impl-N`` debris.
        """
        ...

    def close_pr(self, *, project: str, pr_number: int) -> None:
        """Close a PR without merging.

        Distinct from ``close_issue`` — PyGithub treats issues and PRs
        as separate API surfaces. Idempotent: closing an already-closed
        PR is a no-op rather than an error. Used by ``foreman reset``
        to retire spec/impl PRs whose branches it's about to delete.
        """
        ...

    def find_open_pr_by_head_branch(
        self, *, project: str, branch_name: str,
    ) -> int | None:
        """Find an OPEN PR whose head branch matches ``branch_name``.

        Returns the PR number, or None if no open PR matches. Used by
        ``foreman reset`` to discover spec/impl PRs without depending
        on the SQLite ticket row (which may have already been deleted
        manually). PRs that are closed or merged are NOT returned —
        the discovery phase only cares about live debris that needs
        closing.
        """
        ...

    def get_issue_labels(
        self, *, project: str, issue_number: int,
    ) -> set[str]:
        """Return the current label set on this issue.

        Used by ``foreman reset`` to discover which ``foreman:*`` labels
        are currently on the issue (so the operator-facing plan can
        enumerate them by name).
        """
        ...


class FakeGitProvider:
    """In-memory GitProvider for unit + lifecycle tests."""

    def __init__(self) -> None:
        self._prs: dict[tuple[str, int], PRState] = {}
        # Recorder for merge_pr calls. Tests assert on this set instead
        # of (or in addition to) the PRState transition, since the
        # CLEAN-when-merged-externally case must NOT call merge_pr at all.
        self.merge_pr_calls: set[tuple[str, int]] = set()
        # foreman#416: recorder for update_branch calls. A LIST (not a set)
        # so tests can assert the call COUNT — the heal-loop bound in
        # ``attempt_merge`` cares how many times a perpetually-behind PR
        # got an update_branch, and the BehindBranchHealer must call it
        # exactly once per heal. Each entry is (project, pr_number).
        self.update_branch_calls: list[tuple[str, int]] = []
        self._labeled_issues: dict[tuple[str, str], set[int]] = {}
        # Current label set per (project, issue_number). add_labels
        # unions into this set; remove_labels differences from it.
        # get_issue_labels returns this set (or empty if untouched).
        self._issue_labels: dict[tuple[str, int], set[str]] = {}
        # Recorder for close_issue calls. MergingState (Phase 8d.20) closes
        # the originating GitHub issue after a successful impl-PR merge;
        # tests assert on this set to confirm close happened on the
        # merged-externally + just-merged branches and did NOT happen
        # on the BLOCKED branch. Set semantics give idempotency for free.
        self.closed_issues: set[tuple[str, int]] = set()
        # Recorder for delete_branch calls (mirrors closed_issues shape).
        self.deleted_branches: set[tuple[str, str]] = set()
        # Current branches per project. seed_branch populates; delete_branch
        # removes. Missing-branch delete records the call but is otherwise a no-op.
        self._branches: dict[str, set[str]] = {}
        # Recorder for close_pr calls.
        self.closed_prs: set[tuple[str, int]] = set()
        # Map of (project, pr_number) → head branch name. set_pr_head_branch
        # populates; find_open_pr_by_head_branch reverse-scans it.
        self._pr_head_branches: dict[tuple[str, int], str] = {}

    def set_open_issues_with_label(
        self, *, project: str, label: str, issue_numbers: set[int],
    ) -> None:
        self._labeled_issues[(project, label)] = set(issue_numbers)

    def list_open_issues_with_label(
        self, *, project: str, label: str,
    ) -> list[int]:
        return sorted(self._labeled_issues.get((project, label), set()))

    def set_pr_state(self, *, project: str, pr_number: int, state: PRState) -> None:
        self._prs[(project, pr_number)] = state

    def get_pr_state(self, *, project: str, pr_number: int) -> PRState:
        try:
            return self._prs[(project, pr_number)]
        except KeyError as exc:
            raise PRNotFoundError(f"{project}#{pr_number}") from exc

    def merge_pr(self, *, project: str, pr_number: int) -> None:
        """Mark the PR merged + record the call for test assertions.

        Mirrors what the real ``pr.merge()`` does observably from the
        state-machine's vantage point: subsequent ``get_pr_state``
        sees ``merged=True``. Recording the call separately lets tests
        distinguish "already-merged externally" (no merge_pr call) from
        "we merged it" (merge_pr call recorded).
        """
        existing = self.get_pr_state(project=project, pr_number=pr_number)
        self._prs[(project, pr_number)] = PRState(
            merged=True,
            mergeable=existing.mergeable,
            ci_passing=existing.ci_passing,
            base_ref=existing.base_ref,
            mergeable_state=existing.mergeable_state,
        )
        self.merge_pr_calls.add((project, pr_number))

    def update_branch(self, *, project: str, pr_number: int) -> None:
        """Record the update_branch call.

        foreman#416: mirrors what ``pr.update_branch()`` does observably
        from the state machine's vantage — the recorder is the sole
        side-effect. Tests that want a multi-cycle heal (BEHIND → still
        BEHIND → ... → escalate-on-bound) drive subsequent ``PRState`` via
        ``set_pr_state`` between transitions; a single update_branch on a
        Fake does NOT auto-clear "behind" because the Fake has no base to
        rebase onto. The call recorder lets tests assert exact heal counts.
        """
        self.update_branch_calls.append((project, pr_number))

    def seed_issue_labels(
        self, *, project: str, issue_number: int, labels: set[str],
    ) -> None:
        """Test helper: seed the issue's current label set.

        Used by tests that want to assert ``add_labels`` / ``remove_labels``
        preserve pre-existing labels (e.g. ``foreman:plan``,
        operator-applied custom labels).
        """
        self._issue_labels[(project, issue_number)] = set(labels)

    def add_labels(
        self, *, project: str, issue_number: int, labels: set[str],
    ) -> None:
        """Union the given labels into the issue's current label set."""
        current = self._issue_labels.setdefault((project, issue_number), set())
        current.update(labels)

    def remove_labels(
        self, *, project: str, issue_number: int, labels: set[str],
    ) -> None:
        """Remove the given labels from the issue's current set, if present.

        Idempotent: labels not on the issue are silently skipped — this
        mirrors the Protocol contract (and the PyGithub impl's
        404-swallowing behavior).
        """
        current = self._issue_labels.get((project, issue_number))
        if current is None:
            return
        current.difference_update(labels)

    def get_issue_labels(
        self, *, project: str, issue_number: int,
    ) -> set[str]:
        """Return the current label set on this issue."""
        return self._issue_labels.get((project, issue_number), set())

    def close_issue(self, *, project: str, issue_number: int) -> None:
        """Record that the issue was closed.

        Set-add is naturally idempotent, mirroring the real REST API's
        already-closed-is-no-op behavior.
        """
        self.closed_issues.add((project, issue_number))

    def seed_branch(self, *, project: str, branch_name: str) -> None:
        """Test helper: seed a branch into the fake's branch set."""
        self._branches.setdefault(project, set()).add(branch_name)

    def get_branches(self, *, project: str) -> set[str]:
        """Test helper: return the current branch set for a project."""
        return set(self._branches.get(project, set()))

    def delete_branch(
        self, *, project: str, branch_name: str,
    ) -> None:
        """Drop the branch from this fake's branch set + record the call."""
        self.deleted_branches.add((project, branch_name))
        current = self._branches.get(project)
        if current is not None:
            current.discard(branch_name)

    def close_pr(self, *, project: str, pr_number: int) -> None:
        """Record the close on ``closed_prs``. Idempotent.

        Does NOT mutate :class:`PRState` — there is no ``closed`` field, and
        ``merged`` is preserved either way (close-without-merge stays False;
        close on an already-merged PR does NOT undo the merge). The recorder
        is the sole observable side-effect; consumers that need "did we
        attempt to close" assert on ``closed_prs``.
        """
        self.closed_prs.add((project, pr_number))

    def set_pr_head_branch(
        self, *, project: str, pr_number: int, branch_name: str,
    ) -> None:
        """Test helper: seed the head branch for a PR."""
        self._pr_head_branches[(project, pr_number)] = branch_name

    def find_open_pr_by_head_branch(
        self, *, project: str, branch_name: str,
    ) -> int | None:
        """Linear-scan the PR head-branch map for an open PR on this branch."""
        for (proj, pr_num), head in self._pr_head_branches.items():
            if proj != project or head != branch_name:
                continue
            if (project, pr_num) in self.closed_prs:
                continue
            # An already-merged PR isn't "open" either; skip it.
            try:
                state = self.get_pr_state(project=project, pr_number=pr_num)
            except PRNotFoundError:
                continue
            if state.merged:
                continue
            return pr_num
        return None
