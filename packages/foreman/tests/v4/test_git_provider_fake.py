"""FakeGitProvider — in-memory implementation of the v4 GitProvider Protocol."""

from __future__ import annotations

import datetime as dt

import pytest

from foreman.git_host import CommentRef
from foreman.v4.git_provider import (
    FakeGitProvider,
    PRNotFoundError,
    PRState,
    RequiredCheckState,
)


def test_set_and_get_pr_state():
    git = FakeGitProvider()
    git.set_pr_state(
        project="p",
        pr_number=1,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    assert git.get_pr_state(project="p", pr_number=1).mergeable is True


def test_missing_pr_raises():
    git = FakeGitProvider()
    with pytest.raises(PRNotFoundError):
        git.get_pr_state(project="p", pr_number=999)


def test_merge_pr_marks_merged_and_records_call():
    """``merge_pr`` flips the PR state to ``merged=True`` AND records the
    call on ``merge_pr_calls`` so tests can distinguish "we merged it"
    from "already merged externally" (where merge_pr must NOT be called).
    """
    git = FakeGitProvider()
    git.set_pr_state(
        project="p",
        pr_number=1,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    git.merge_pr(project="p", pr_number=1)
    assert git.get_pr_state(project="p", pr_number=1).merged is True
    assert ("p", 1) in git.merge_pr_calls


def test_merge_pr_call_recorder_empty_by_default():
    """The recorder is empty until ``merge_pr`` is called — load-bearing
    for assertions that gate on "the state machine did NOT merge"."""
    git = FakeGitProvider()
    assert git.merge_pr_calls == set()


def test_merge_pr_preserves_mergeable_state():
    """foreman#416: merge_pr must carry the mergeable_state field through
    (like base_ref), not silently reset it to ""."""
    git = FakeGitProvider()
    git.set_pr_state(
        project="p",
        pr_number=1,
        state=PRState(
            merged=False,
            mergeable=True,
            ci_passing=True,
            base_ref="main",
            mergeable_state="clean",
        ),
    )
    git.merge_pr(project="p", pr_number=1)
    assert git.get_pr_state(project="p", pr_number=1).mergeable_state == "clean"


def test_update_branch_records_call_as_list():
    """foreman#416: update_branch appends to a LIST (not a set) so tests
    can assert call COUNT — the heal-loop bound depends on counting."""
    git = FakeGitProvider()
    assert git.update_branch_calls == []
    git.update_branch(project="p", pr_number=7)
    git.update_branch(project="p", pr_number=7)
    assert git.update_branch_calls == [("p", 7), ("p", 7)]


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
        project="p",
        issue_number=42,
        labels={"foreman:plan", "custom"},
    )
    git.add_labels(
        project="p",
        issue_number=42,
        labels={"foreman:state-planning"},
    )
    assert git.get_issue_labels(project="p", issue_number=42) == {
        "foreman:plan",
        "custom",
        "foreman:state-planning",
    }


def test_remove_labels_only_touches_specified():
    """Symmetric to add: removing a state label leaves trigger +
    operator labels intact."""
    git = FakeGitProvider()
    git.seed_issue_labels(
        project="p",
        issue_number=42,
        labels={"foreman:plan", "custom", "foreman:state-planning"},
    )
    git.remove_labels(
        project="p",
        issue_number=42,
        labels={"foreman:state-planning"},
    )
    assert git.get_issue_labels(project="p", issue_number=42) == {
        "foreman:plan",
        "custom",
    }


def test_remove_labels_idempotent_on_missing():
    """Removing a label that isn't on the issue is a silent no-op —
    matches the Protocol contract and the PyGithub impl's 404
    swallowing. Other labels unchanged."""
    git = FakeGitProvider()
    git.seed_issue_labels(
        project="p",
        issue_number=42,
        labels={"foreman:plan"},
    )
    # remove_labels with a never-applied label — must not raise.
    git.remove_labels(
        project="p",
        issue_number=42,
        labels={"foreman:state-nonexistent"},
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
        project="p",
        issue_number=999,
        labels={"foreman:state-planning"},
    )
    assert git.get_issue_labels(project="p", issue_number=999) == set()


def test_get_issue_labels_unseen_defaults_to_empty_set():
    git = FakeGitProvider()
    assert git.get_issue_labels(project="p", issue_number=999) == set()


def test_fake_close_issue_records_call():
    """``close_issue`` adds ``(project, issue_number)`` to ``closed_issues``.

    Phase 8d.20: MergingState calls close_issue after a successful merge so
    the originating GitHub issue closes. The recorder lets MergingState
    tests assert the close happened (and only in the merged branches —
    the BLOCKED branch must NOT call it).
    """
    git = FakeGitProvider()
    git.close_issue(project="p", issue_number=42)
    assert ("p", 42) in git.closed_issues


def test_fake_close_issue_idempotent():
    """Calling ``close_issue`` twice doesn't raise and the recorder set
    still has one entry — GitHub's REST API treats closing an
    already-closed issue as a no-op, and the Fake mirrors that contract."""
    git = FakeGitProvider()
    git.close_issue(project="p", issue_number=42)
    git.close_issue(project="p", issue_number=42)
    assert git.closed_issues == {("p", 42)}


def test_delete_branch_records_deletion():
    fake = FakeGitProvider()
    fake.seed_branch(project="p", branch_name="foreman/issue-1")
    fake.delete_branch(project="p", branch_name="foreman/issue-1")
    assert ("p", "foreman/issue-1") in fake.deleted_branches
    assert "foreman/issue-1" not in fake.get_branches(project="p")


def test_delete_branch_missing_is_noop():
    fake = FakeGitProvider()
    # No seed — branch doesn't exist. Must NOT raise.
    fake.delete_branch(project="p", branch_name="foreman/issue-99")
    assert ("p", "foreman/issue-99") in fake.deleted_branches


def test_close_pr_records_call_and_preserves_merged_state():
    """close_pr records the call. PRState.merged is preserved either way:
    closed-without-merge stays False; close on an already-merged PR does
    NOT undo the merge.
    """
    fake = FakeGitProvider()
    # Branch A: close-without-merge — merged stays False.
    fake.set_pr_state(
        project="p",
        pr_number=19,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    fake.close_pr(project="p", pr_number=19)
    assert ("p", 19) in fake.closed_prs
    assert fake.get_pr_state(project="p", pr_number=19).merged is False

    # Branch B: close on an already-merged PR — merged stays True.
    fake.set_pr_state(
        project="p",
        pr_number=20,
        state=PRState(merged=True, mergeable=True, ci_passing=True),
    )
    fake.close_pr(project="p", pr_number=20)
    assert ("p", 20) in fake.closed_prs
    assert fake.get_pr_state(project="p", pr_number=20).merged is True


def test_close_pr_idempotent_on_already_closed():
    fake = FakeGitProvider()
    fake.set_pr_state(
        project="p",
        pr_number=19,
        state=PRState(merged=False, mergeable=False, ci_passing=True),
    )
    fake.close_pr(project="p", pr_number=19)
    # Second call must not raise.
    fake.close_pr(project="p", pr_number=19)
    assert ("p", 19) in fake.closed_prs


def test_find_open_pr_by_head_branch_returns_pr_number():
    fake = FakeGitProvider()
    fake.set_pr_state(
        project="p",
        pr_number=19,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    fake.set_pr_head_branch(
        project="p",
        pr_number=19,
        branch_name="foreman/issue-180",
    )
    found = fake.find_open_pr_by_head_branch(
        project="p",
        branch_name="foreman/issue-180",
    )
    assert found == 19


def test_find_open_pr_by_head_branch_no_match_returns_none():
    fake = FakeGitProvider()
    found = fake.find_open_pr_by_head_branch(
        project="p",
        branch_name="foreman/issue-999",
    )
    assert found is None


def test_find_open_pr_by_head_branch_skips_closed_prs():
    fake = FakeGitProvider()
    fake.set_pr_state(
        project="p",
        pr_number=19,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    fake.set_pr_head_branch(
        project="p",
        pr_number=19,
        branch_name="foreman/issue-180",
    )
    fake.close_pr(project="p", pr_number=19)
    found = fake.find_open_pr_by_head_branch(
        project="p",
        branch_name="foreman/issue-180",
    )
    assert found is None


# ---------------------------------------------------------------------------
# Issue comments — seed_issue_comments, get_issue_comments, post_issue_comment
# (sub-request 1 of issue #410: add comment surface to FakeGitProvider)
# ---------------------------------------------------------------------------


def _comment(body: str, *, offset_seconds: int = 0) -> CommentRef:
    """Helper: build a CommentRef with a fixed base timestamp."""
    return CommentRef(
        author_login="foreman-bot",
        posted_at=dt.datetime(2026, 6, 20, 12, 0, offset_seconds, tzinfo=dt.UTC),
        body=body,
    )


def test_get_issue_comments_returns_empty_by_default() -> None:
    """get_issue_comments returns [] when no comments have been seeded."""
    fake = FakeGitProvider()
    result = fake.get_issue_comments(project="p", issue_number=42)
    assert result == []


def test_seed_issue_comments_populates_get_issue_comments() -> None:
    """seed_issue_comments makes comments retrievable via get_issue_comments."""
    fake = FakeGitProvider()
    comments = [_comment("first"), _comment("second", offset_seconds=1)]
    fake.seed_issue_comments(project="p", issue_number=42, comments=comments)
    result = fake.get_issue_comments(project="p", issue_number=42)
    assert result == comments


def test_get_issue_comments_is_per_issue() -> None:
    """Comments seeded for one (project, issue) don't bleed into another."""
    fake = FakeGitProvider()
    fake.seed_issue_comments(
        project="p",
        issue_number=1,
        comments=[_comment("only on 1")],
    )
    assert fake.get_issue_comments(project="p", issue_number=2) == []
    assert fake.get_issue_comments(project="other", issue_number=1) == []


def test_post_issue_comment_records_to_posted_comments() -> None:
    """post_issue_comment appends (project, issue_number, body) to posted_comments."""
    fake = FakeGitProvider()
    fake.post_issue_comment(project="p", issue_number=42, body="hello world")
    assert fake.posted_comments == [("p", 42, "hello world")]


def test_post_issue_comment_accumulates_in_order() -> None:
    """Multiple post_issue_comment calls accumulate in call order."""
    fake = FakeGitProvider()
    fake.post_issue_comment(project="p", issue_number=1, body="first")
    fake.post_issue_comment(project="p", issue_number=2, body="second")
    fake.post_issue_comment(project="p", issue_number=1, body="third")
    assert fake.posted_comments == [
        ("p", 1, "first"),
        ("p", 2, "second"),
        ("p", 1, "third"),
    ]


def test_posted_comments_empty_by_default() -> None:
    """The recorder starts empty so tests can assert no comments were posted."""
    fake = FakeGitProvider()
    assert fake.posted_comments == []


# ---------------------------------------------------------------------------
# get_issue_state_reason (foreman#524)
# ---------------------------------------------------------------------------


def test_get_issue_state_reason_defaults_none() -> None:
    """An unseeded issue has no state_reason — returns None."""
    p = FakeGitProvider()
    assert p.get_issue_state_reason(project="agent_core", issue_number=1) is None


def test_get_issue_state_reason_readback() -> None:
    """set_issue_state_reason seeds the value; get_issue_state_reason reads it back."""
    p = FakeGitProvider()
    p.set_issue_state_reason(project="agent_core", issue_number=1, reason="completed")
    assert p.get_issue_state_reason(project="agent_core", issue_number=1) == "completed"


# ---------------------------------------------------------------------------
# read_blocked_by (foreman#524)
# ---------------------------------------------------------------------------


def test_read_blocked_by_defaults_empty() -> None:
    """An unseeded issue has no blocked_by — returns empty list."""
    p = FakeGitProvider()
    assert p.read_blocked_by(project="agent_core", issue_number=291) == []


def test_read_blocked_by_readback() -> None:
    """set_blocked_by seeds the list; read_blocked_by reads it back."""
    p = FakeGitProvider()
    p.set_blocked_by(project="agent_core", issue_number=291, blocked_by=[290])
    assert p.read_blocked_by(project="agent_core", issue_number=291) == [290]


# ---------------------------------------------------------------------------
# required_check_state (foreman#317)
# ---------------------------------------------------------------------------


def test_fake_required_check_state_roundtrips():
    p = FakeGitProvider()
    p.seed_check_state("proj", 7, RequiredCheckState.FAILED)
    assert p.required_check_state(project="proj", pr_number=7) == RequiredCheckState.FAILED


def test_fake_required_check_state_defaults_pending_when_unseeded():
    # Mirror reality: a PR whose checks haven't registered yet reads PENDING
    # (C-CI guarantees CI exists), never a silent PASSED.
    p = FakeGitProvider()
    assert p.required_check_state(project="proj", pr_number=9) == RequiredCheckState.PENDING
