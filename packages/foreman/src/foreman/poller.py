"""Poller — discovers ticket state changes by polling GitHub.

Compares each project's current foreman-labeled issues against the
last-known labels in SQLite. Returns the tickets whose labels changed,
and persists the new label snapshot for the next diff.

The poller is intentionally I/O-bound — it makes one search API call per
project per cycle. With 10 projects on a 30s poll, that's ~1200 API
calls/hour, well under GitHub's 5000/hour limit.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

from foreman.config import ProjectConfig
from foreman.dispatcher import Ticket
from foreman.queue import DaemonQueue
from foreman.storage import Storage

_log = logging.getLogger("foreman.daemon.poller")


class _IssueLike(Protocol):
    number: int
    labels: list[str]
    updated_at: datetime


class _HostLike(Protocol):
    def search_foreman_labeled_issues(self, repo: str) -> list[_IssueLike]: ...


def poll_project(
    *,
    project_name: str,
    project: ProjectConfig,
    host: _HostLike,
    storage: Storage,
    queue: DaemonQueue,
) -> list[Ticket]:
    """Poll one project. Return tickets whose labels changed since last poll.

    Side effects:
      - Persists the new label snapshot to ``labels_seen``.
      - Enqueues each changed ticket onto ``queue`` (dedup-by-key, so
        idempotent against the daemon's self-notify path).
    """
    now = datetime.now(UTC)
    issues = host.search_foreman_labeled_issues(project.repo)
    changed: list[Ticket] = []

    for issue in issues:
        seen = storage.get_labels_seen(project_name, issue.number)
        current = sorted(issue.labels)
        if seen != current:
            storage.upsert_labels_seen(project_name, issue.number, current, now)
            _log.info(
                "ticket labels changed",
                extra={
                    "project": project_name,
                    "issue": issue.number,
                    "previous_labels": seen or [],
                    "current_labels": current,
                },
            )
            ticket = Ticket(
                project_name=project_name,
                issue_number=issue.number,
                labels=frozenset(current),
                last_transition_at=issue.updated_at,
            )
            changed.append(ticket)
            queue.enqueue(ticket)

    _log.info(
        "polled project",
        extra={
            "project": project_name,
            "issues_seen": len(issues),
            "changed": len(changed),
            "queue_depth": len(queue),
        },
    )

    return changed
