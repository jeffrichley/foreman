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
