"""Tests for the poller — discovers ticket state changes via GitHub search."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from foreman.config import AppsConfig, ProjectConfig
from foreman.poller import poll_project
from foreman.storage import Storage


@dataclass
class _FakeIssue:
    number: int
    labels: list[str]
    updated_at: datetime


@dataclass
class _FakeGitHostProvider:
    """Stand-in for the real GitHostProvider's search-issues capability."""

    issues_by_query: dict[str, list[_FakeIssue]] = field(default_factory=dict)

    def search_foreman_labeled_issues(self, repo: str) -> list[_FakeIssue]:
        return list(self.issues_by_query.get(repo, []))


def test_poll_project_enqueues_new_issue(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    host = _FakeGitHostProvider(
        issues_by_query={
            "jeffrichley/voice": [
                _FakeIssue(
                    number=42,
                    labels=["foreman:plan"],
                    updated_at=datetime(2026, 6, 1, tzinfo=UTC),
                )
            ]
        }
    )
    project = ProjectConfig(
        repo="jeffrichley/voice", local_clone_path="/tmp/voice", apps=AppsConfig()
    )

    changed = poll_project(project_name="voice", project=project, host=host, storage=storage)

    assert len(changed) == 1
    assert changed[0].issue_number == 42
    assert changed[0].labels == frozenset({"foreman:plan"})


def test_poll_project_returns_nothing_when_labels_unchanged(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    storage.upsert_labels_seen("voice", 42, ["foreman:plan"], datetime(2026, 6, 1, tzinfo=UTC))

    host = _FakeGitHostProvider(
        issues_by_query={
            "jeffrichley/voice": [
                _FakeIssue(
                    number=42,
                    labels=["foreman:plan"],
                    updated_at=datetime(2026, 6, 1, tzinfo=UTC),
                )
            ]
        }
    )
    project = ProjectConfig(
        repo="jeffrichley/voice", local_clone_path="/tmp/voice", apps=AppsConfig()
    )

    changed = poll_project(project_name="voice", project=project, host=host, storage=storage)
    assert changed == []


def test_poll_project_detects_label_changes(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    storage.upsert_labels_seen("voice", 42, ["foreman:plan"], datetime(2026, 6, 1, tzinfo=UTC))

    host = _FakeGitHostProvider(
        issues_by_query={
            "jeffrichley/voice": [
                _FakeIssue(
                    number=42,
                    labels=["foreman:spec-review"],
                    updated_at=datetime(2026, 6, 1, 1, 0, tzinfo=UTC),
                )
            ]
        }
    )
    project = ProjectConfig(
        repo="jeffrichley/voice", local_clone_path="/tmp/voice", apps=AppsConfig()
    )

    changed = poll_project(project_name="voice", project=project, host=host, storage=storage)

    assert len(changed) == 1
    assert changed[0].labels == frozenset({"foreman:spec-review"})


def test_poll_project_persists_new_labels_seen(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()

    host = _FakeGitHostProvider(
        issues_by_query={
            "jeffrichley/voice": [
                _FakeIssue(
                    number=42,
                    labels=["foreman:plan"],
                    updated_at=datetime(2026, 6, 1, tzinfo=UTC),
                )
            ]
        }
    )
    project = ProjectConfig(
        repo="jeffrichley/voice", local_clone_path="/tmp/voice", apps=AppsConfig()
    )

    poll_project(project_name="voice", project=project, host=host, storage=storage)

    assert storage.get_labels_seen("voice", 42) == ["foreman:plan"]
