"""GitProvider — narrow seam over PyGithub for the v4 state machine.

States that need to look at GitHub artifact state (spec PR mergeable?
impl PR ready? MergeQueue verdict?) go through this Protocol. The
PyGithub concrete implementation lands in Phase 4; Phase 3 only needs
the shape + the fake.

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
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class PRNotFoundError(LookupError):
    """No PR matching this (project, pr_number)."""


@dataclass(frozen=True, slots=True)
class PRState:
    merged: bool
    mergeable: bool
    ci_passing: bool


class MergeVerdict(StrEnum):
    PENDING = "pending"     # in MergeQueue, no decision yet
    MERGED = "merged"       # MergeQueue completed the merge
    REJECTED = "rejected"   # MergeQueue rejected (CI fail, conflict)


class GitProvider(Protocol):
    def list_open_issues_with_label(
        self, *, project: str, label: str,
    ) -> list[int]: ...
    def get_pr_state(self, *, project: str, pr_number: int) -> PRState: ...
    def merge_spec_pr(self, *, project: str, pr_number: int) -> None: ...
    def enqueue_merge_queue(self, *, project: str, pr_number: int) -> None: ...
    def merge_verdict(self, *, project: str, pr_number: int) -> MergeVerdict: ...
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


class FakeGitProvider:
    """In-memory GitProvider for unit + lifecycle tests."""

    def __init__(self) -> None:
        self._prs: dict[tuple[str, int], PRState] = {}
        self.merge_queue: set[tuple[str, int]] = set()
        self._verdicts: dict[tuple[str, int], MergeVerdict] = {}
        self._labeled_issues: dict[tuple[str, str], set[int]] = {}
        # Current label set per (project, issue_number). add_labels
        # unions into this set; remove_labels differences from it.
        # get_issue_labels returns this set (or empty if untouched).
        self._issue_labels: dict[tuple[str, int], set[str]] = {}

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

    def merge_spec_pr(self, *, project: str, pr_number: int) -> None:
        existing = self.get_pr_state(project=project, pr_number=pr_number)
        self._prs[(project, pr_number)] = PRState(
            merged=True, mergeable=existing.mergeable, ci_passing=existing.ci_passing,
        )

    def enqueue_merge_queue(self, *, project: str, pr_number: int) -> None:
        self.get_pr_state(project=project, pr_number=pr_number)  # raise if missing
        self.merge_queue.add((project, pr_number))
        self._verdicts.setdefault((project, pr_number), MergeVerdict.PENDING)

    def merge_verdict(self, *, project: str, pr_number: int) -> MergeVerdict:
        return self._verdicts.get((project, pr_number), MergeVerdict.PENDING)

    def set_merge_verdict(
        self, *, project: str, pr_number: int, verdict: MergeVerdict,
    ) -> None:
        self._verdicts[(project, pr_number)] = verdict
        if verdict is MergeVerdict.MERGED:
            existing = self.get_pr_state(project=project, pr_number=pr_number)
            self._prs[(project, pr_number)] = PRState(
                merged=True, mergeable=existing.mergeable,
                ci_passing=existing.ci_passing,
            )

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
