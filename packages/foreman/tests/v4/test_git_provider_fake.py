"""FakeGitProvider — in-memory implementation of the v4 GitProvider Protocol."""
from __future__ import annotations

import pytest

from foreman.v4.git_provider import (
    FakeGitProvider,
    MergeVerdict,
    PRNotFoundError,
    PRState,
)


def test_set_and_get_pr_state():
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=1,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    assert git.get_pr_state(project="p", pr_number=1).mergeable is True


def test_missing_pr_raises():
    git = FakeGitProvider()
    with pytest.raises(PRNotFoundError):
        git.get_pr_state(project="p", pr_number=999)


def test_enqueue_into_merge_queue_records_call():
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=1,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    git.enqueue_merge_queue(project="p", pr_number=1)
    assert ("p", 1) in git.merge_queue


def test_merge_verdict_default_is_pending():
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=1,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    git.enqueue_merge_queue(project="p", pr_number=1)
    assert git.merge_verdict(project="p", pr_number=1) is MergeVerdict.PENDING


def test_set_merge_verdict_advances():
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=1,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    git.enqueue_merge_queue(project="p", pr_number=1)
    git.set_merge_verdict(project="p", pr_number=1, verdict=MergeVerdict.MERGED)
    assert git.merge_verdict(project="p", pr_number=1) is MergeVerdict.MERGED


def test_merge_spec_pr_marks_merged():
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=1,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    git.merge_spec_pr(project="p", pr_number=1)
    assert git.get_pr_state(project="p", pr_number=1).merged is True


def test_add_labels_records_call_and_is_queryable():
    """A single add_labels call's labels are queryable via get_issue_labels."""
    git = FakeGitProvider()
    git.add_labels(project="p", issue_number=42, labels={"foreman:state-planning"})
    assert git.get_issue_labels(project="p", issue_number=42) == {
        "foreman:state-planning",
    }


def test_add_labels_preserves_existing_labels():
    """The defining preservation case: adding a state label MUST NOT
    drop the trigger label or operator-applied labels. This is the
    Phase 8c.4 fix — the morning dogfood wedged for ~40min because
    write_labels REPLACED the entire set, stripping foreman:plan."""
    git = FakeGitProvider()
    git.seed_issue_labels(
        project="p", issue_number=42, labels={"foreman:plan", "custom"},
    )
    git.add_labels(
        project="p", issue_number=42, labels={"foreman:state-planning"},
    )
    assert git.get_issue_labels(project="p", issue_number=42) == {
        "foreman:plan", "custom", "foreman:state-planning",
    }


def test_remove_labels_only_touches_specified():
    """Symmetric to add: removing a state label leaves trigger +
    operator labels intact."""
    git = FakeGitProvider()
    git.seed_issue_labels(
        project="p", issue_number=42,
        labels={"foreman:plan", "custom", "foreman:state-planning"},
    )
    git.remove_labels(
        project="p", issue_number=42, labels={"foreman:state-planning"},
    )
    assert git.get_issue_labels(project="p", issue_number=42) == {
        "foreman:plan", "custom",
    }


def test_remove_labels_idempotent_on_missing():
    """Removing a label that isn't on the issue is a silent no-op —
    matches the Protocol contract and the PyGithub impl's 404
    swallowing. Other labels unchanged."""
    git = FakeGitProvider()
    git.seed_issue_labels(
        project="p", issue_number=42, labels={"foreman:plan"},
    )
    # remove_labels with a never-applied label — must not raise.
    git.remove_labels(
        project="p", issue_number=42, labels={"foreman:state-nonexistent"},
    )
    assert git.get_issue_labels(project="p", issue_number=42) == {
        "foreman:plan",
    }


def test_remove_labels_on_unseen_issue_is_no_op():
    """Even when the FakeGitProvider has no entry for this issue
    (i.e., seed_issue_labels was never called), remove_labels must
    not raise."""
    git = FakeGitProvider()
    git.remove_labels(
        project="p", issue_number=999, labels={"foreman:state-planning"},
    )
    assert git.get_issue_labels(project="p", issue_number=999) == set()


def test_get_issue_labels_unseen_defaults_to_empty_set():
    git = FakeGitProvider()
    assert git.get_issue_labels(project="p", issue_number=999) == set()
