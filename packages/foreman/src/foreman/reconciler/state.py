"""Immutable per-poll state of a project, derived from one GraphQL fetch.

These dataclasses are the reconciler's only view of GitHub state during a
poll. They are NOT persisted — they exist for the duration of one tick and
are discarded. The execution log persists facts the daemon decides;
GitHub IS the truth for everything in these objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class IssueState:
    """One issue's relevant state at the time of fetch."""

    number: int
    title: str
    labels: tuple[str, ...]
    assignees: tuple[str, ...]
    body: str
    updated_at: datetime

    def has_label(self, label: str) -> bool:
        return label in self.labels


@dataclass(frozen=True, slots=True)
class PRState:
    """One PR's relevant state at the time of fetch.

    `mergeable` mirrors GitHub's MergeableState string: "MERGEABLE" |
    "CONFLICTING" | "UNKNOWN". `ci_status` mirrors statusCheckRollup's
    rollupState: "SUCCESS" | "PENDING" | "FAILURE" | "ERROR" | None.
    `linked_issue_numbers` comes from GraphQL `closingIssuesReferences`.
    """

    number: int
    head_ref: str
    mergeable: str
    ci_status: str | None
    body: str
    linked_issue_numbers: tuple[int, ...]
    is_merged: bool

    def closes_issue(self, issue_number: int) -> bool:
        return issue_number in self.linked_issue_numbers


@dataclass(frozen=True, slots=True)
class ProjectSnapshot:
    """One poll's full view of a project.

    `project` is the local config name ("foreman", "voice", "agent_core").
    `owner` / `repo` are the GitHub coords. `issues` and `prs` are tuples for
    immutability + hashability.
    """

    project: str
    owner: str
    repo: str
    issues: tuple[IssueState, ...]
    prs: tuple[PRState, ...]
    fetched_at: datetime

    def find_issue(self, number: int) -> IssueState | None:
        for issue in self.issues:
            if issue.number == number:
                return issue
        return None

    def prs_for_issue(self, issue_number: int) -> tuple[PRState, ...]:
        return tuple(pr for pr in self.prs if pr.closes_issue(issue_number))

    def ticket_id_for(self, issue_number: int) -> str:
        return f"{self.owner}/{self.repo}#{issue_number}"
