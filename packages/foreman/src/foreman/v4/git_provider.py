"""GitProvider — narrow seam over PyGithub for the v4 state machine.

States that need to look at GitHub artifact state (spec PR mergeable?
impl PR ready? MergeQueue verdict?) go through this Protocol. The
PyGithub concrete implementation lands in Phase 4; Phase 3 only needs
the shape + the fake.
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
    def write_labels(
        self, *, project: str, issue_number: int, labels: set[str]
    ) -> None:
        """Replace the labels on the given issue with the given set.

        Satisfies the ``LabelObservabilityObserver.LabelWriter`` Protocol
        so a GitProvider can be wired straight into bootstrap as the
        observer's writer — no separate adapter class.
        """
        ...


class FakeGitProvider:
    """In-memory GitProvider for unit + lifecycle tests."""

    def __init__(self) -> None:
        self._prs: dict[tuple[str, int], PRState] = {}
        self.merge_queue: set[tuple[str, int]] = set()
        self._verdicts: dict[tuple[str, int], MergeVerdict] = {}
        self._labeled_issues: dict[tuple[str, str], set[int]] = {}
        # write_labels call log — latest set per (project, issue_number).
        # Replace-semantics matches PyGithub's set_labels, so the most
        # recent call IS the current label state.
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

    def write_labels(
        self, *, project: str, issue_number: int, labels: set[str],
    ) -> None:
        """Replace-semantics matches PyGithub's ``issue.set_labels``."""
        self._issue_labels[(project, issue_number)] = set(labels)

    def set_issue_labels(
        self, *, project: str, issue_number: int,
    ) -> set[str]:
        """Inspect-only: the most recently written label set, or an
        empty set if nothing has been written for this issue."""
        return set(self._issue_labels.get((project, issue_number), set()))
