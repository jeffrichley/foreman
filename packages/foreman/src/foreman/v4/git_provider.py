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


class GitProvider(Protocol):
    def list_open_issues_with_label(
        self, *, project: str, label: str,
    ) -> list[int]: ...
    def get_pr_state(self, *, project: str, pr_number: int) -> PRState: ...
    def merge_pr(self, *, project: str, pr_number: int) -> None: ...
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


class FakeGitProvider:
    """In-memory GitProvider for unit + lifecycle tests."""

    def __init__(self) -> None:
        self._prs: dict[tuple[str, int], PRState] = {}
        # Recorder for merge_pr calls. Tests assert on this set instead
        # of (or in addition to) the PRState transition, since the
        # CLEAN-when-merged-externally case must NOT call merge_pr at all.
        self.merge_pr_calls: set[tuple[str, int]] = set()
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
            merged=True, mergeable=existing.mergeable, ci_passing=existing.ci_passing,
        )
        self.merge_pr_calls.add((project, pr_number))

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
        """Record the close + ensure subsequent get_pr_state shows it
        as not merged. Idempotent on repeat calls.
        """
        self.closed_prs.add((project, pr_number))
        # Best-effort: if the PR is in our state map, leave merged as-is
        # (closed-without-merge stays merged=False; already-merged stays
        # merged=True — close on a merged PR shouldn't undo the merge).
        # No state mutation needed beyond the recorder.
