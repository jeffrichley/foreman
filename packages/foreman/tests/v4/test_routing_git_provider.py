"""RoutingGitProvider — dispatches every GitProvider call by ``project=``.

The fix for F1 (Phase 8d.16): ``PyGithubGitProvider`` is locked at
construction to ONE ``repo_full_name`` and ignores the ``project=`` kwarg
every Protocol method takes. In a multi-project daemon, bootstrap built
one provider per project but only threaded the FIRST into cross-project
consumers (LabelObservabilityObserver + state-machine ``merge_pr`` calls).
The router fixes that — every cross-project call gets dispatched to the
right per-project provider.

These tests verify EVERY method on the :class:`GitProvider` Protocol
dispatches to the named provider AND not to any other registered
provider. The asymmetry (other-not-called) is load-bearing: without it,
a router that broadcasts to every provider would also pass the dispatch
assertion, masking the original bug class.
"""

from __future__ import annotations

import datetime as dt

import pytest

from foreman.git_host import CommentRef
from foreman.v4.git_provider import FakeGitProvider, PRState, RequiredCheckState
from foreman.v4.routing_git_provider import (
    RoutingGitProvider,
    UnknownProjectError,
)


def _two_provider_router() -> tuple[FakeGitProvider, FakeGitProvider, RoutingGitProvider]:
    """Set up two real FakeGitProvider instances behind one router.

    Returns ``(foo, bar, router)`` so each test can call through the
    router and inspect both providers to confirm only one was touched.
    """
    foo = FakeGitProvider()
    bar = FakeGitProvider()
    router = RoutingGitProvider(providers={"foo": foo, "bar": bar})
    return foo, bar, router


def test_dispatches_get_pr_state_to_correct_provider() -> None:
    foo, bar, router = _two_provider_router()
    # Seed only bar with the PR — if the router mis-routes to foo,
    # foo's get_pr_state will raise PRNotFoundError.
    state = PRState(merged=False, mergeable=True, ci_passing=True)
    bar.set_pr_state(project="bar", pr_number=99, state=state)

    result = router.get_pr_state(project="bar", pr_number=99)

    assert result == state
    # And the inverse: routing to foo for a foo-pr that doesn't exist on
    # bar must raise PRNotFoundError from foo (not silently succeed via
    # cross-provider read).
    with pytest.raises(LookupError):
        router.get_pr_state(project="foo", pr_number=99)


def test_dispatches_merge_pr_to_correct_provider() -> None:
    foo, bar, router = _two_provider_router()
    bar.set_pr_state(
        project="bar",
        pr_number=42,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )

    router.merge_pr(project="bar", pr_number=42)

    assert bar.get_pr_state(project="bar", pr_number=42).merged is True
    assert ("bar", 42) in bar.merge_pr_calls
    # foo never had PR 42 set — if the router mis-routed, foo would have
    # raised PRNotFoundError; if it broadcast, foo would now have a
    # merged-PR-42 + recorded call. Neither happened.
    with pytest.raises(LookupError):
        foo.get_pr_state(project="foo", pr_number=42)
    assert ("bar", 42) not in foo.merge_pr_calls


def test_dispatches_add_labels_to_correct_provider() -> None:
    foo, bar, router = _two_provider_router()

    router.add_labels(
        project="bar",
        issue_number=7,
        labels={"foreman:state-planning"},
    )

    assert bar.get_issue_labels(project="bar", issue_number=7) == {
        "foreman:state-planning",
    }
    assert foo.get_issue_labels(project="foo", issue_number=7) == set()


def test_dispatches_remove_labels_to_correct_provider() -> None:
    foo, bar, router = _two_provider_router()
    # Seed both with the same label so we can prove the remove only
    # touched bar's copy.
    foo.seed_issue_labels(
        project="foo",
        issue_number=7,
        labels={"foreman:state-planning"},
    )
    bar.seed_issue_labels(
        project="bar",
        issue_number=7,
        labels={"foreman:state-planning"},
    )

    router.remove_labels(
        project="bar",
        issue_number=7,
        labels={"foreman:state-planning"},
    )

    assert bar.get_issue_labels(project="bar", issue_number=7) == set()
    # foo's label survives — router did not broadcast.
    assert foo.get_issue_labels(project="foo", issue_number=7) == {
        "foreman:state-planning",
    }


def test_dispatches_close_issue_to_correct_provider() -> None:
    """Phase 8d.20: ``close_issue`` is the newest method on the Protocol;
    same routing contract as ``merge_pr`` and the label methods.
    Mis-routing here would close the wrong issue in a multi-project
    daemon — directly user-visible bug in a no-undo space."""
    foo, bar, router = _two_provider_router()

    router.close_issue(project="bar", issue_number=7)

    assert ("bar", 7) in bar.closed_issues
    # The non-target provider must not see the call.
    assert ("bar", 7) not in foo.closed_issues
    assert ("foo", 7) not in foo.closed_issues


def test_dispatches_get_issue_state_to_correct_provider() -> None:
    """foreman#409: ``get_issue_state`` routes to the project's provider so the
    reset guard reads the right repo's issue in a multi-project daemon."""
    foo, bar, router = _two_provider_router()
    bar.seed_issue_state(project="bar", issue_number=7, state="closed")
    foo.seed_issue_state(project="foo", issue_number=7, state="open")

    # Routed to bar → returns bar's state, not foo's.
    assert router.get_issue_state(project="bar", issue_number=7) == "closed"
    assert router.get_issue_state(project="foo", issue_number=7) == "open"


def test_dispatches_reopen_issue_to_correct_provider() -> None:
    """foreman#409: ``reopen_issue`` routes to the project's provider —
    mis-routing would reopen the wrong repo's issue in a no-undo space."""
    foo, bar, router = _two_provider_router()
    bar.seed_issue_state(project="bar", issue_number=7, state="closed")
    foo.seed_issue_state(project="foo", issue_number=7, state="closed")

    router.reopen_issue(project="bar", issue_number=7)

    # Target reopened; non-target untouched (router did not broadcast).
    assert bar.get_issue_state(project="bar", issue_number=7) == "open"
    assert foo.get_issue_state(project="foo", issue_number=7) == "closed"


def test_dispatches_list_open_issues_with_label_to_correct_provider() -> None:
    foo, bar, router = _two_provider_router()
    foo.set_open_issues_with_label(
        project="foo",
        label="foreman:plan",
        issue_numbers={1, 2, 3},
    )
    bar.set_open_issues_with_label(
        project="bar",
        label="foreman:plan",
        issue_numbers={99},
    )

    result = router.list_open_issues_with_label(
        project="bar",
        label="foreman:plan",
    )

    assert result == [99]


def test_unknown_project_raises_with_known_list() -> None:
    _, _, router = _two_provider_router()

    with pytest.raises(UnknownProjectError) as exc_info:
        router.get_pr_state(project="never_registered", pr_number=1)

    # Error message names both the missing project and the known ones so
    # an operator can diff config vs. live state without digging into a
    # traceback.
    msg = str(exc_info.value)
    assert "never_registered" in msg
    assert "foo" in msg
    assert "bar" in msg


def test_unknown_project_is_lookuperror_subclass() -> None:
    """Callers that catch ``LookupError`` (the family PRNotFoundError
    already lives in) should also catch routing-level misses without
    needing to import the dedicated exception type."""
    _, _, router = _two_provider_router()

    with pytest.raises(LookupError):
        router.add_labels(
            project="never_registered",
            issue_number=1,
            labels={"x"},
        )


def test_constructor_takes_immutable_snapshot() -> None:
    """Mutating the caller's mapping after construction must not affect
    routing — the router holds a defensive copy."""
    foo = FakeGitProvider()
    bar = FakeGitProvider()
    providers: dict[str, FakeGitProvider] = {"foo": foo}
    router = RoutingGitProvider(providers=providers)

    # Add bar to the caller's dict AFTER construction. The router should
    # still treat bar as unknown.
    providers["bar"] = bar

    with pytest.raises(UnknownProjectError):
        router.get_pr_state(project="bar", pr_number=1)


def test_delete_branch_dispatches_to_per_project_provider():
    a = FakeGitProvider()
    b = FakeGitProvider()
    router = RoutingGitProvider(providers={"a": a, "b": b})
    router.delete_branch(project="b", branch_name="foreman/issue-1")
    assert ("b", "foreman/issue-1") in b.deleted_branches
    assert a.deleted_branches == set()


def test_close_pr_dispatches_to_per_project_provider():
    a = FakeGitProvider()
    b = FakeGitProvider()
    a.set_pr_state(
        project="a",
        pr_number=5,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    router = RoutingGitProvider(providers={"a": a, "b": b})
    router.close_pr(project="a", pr_number=5)
    assert ("a", 5) in a.closed_prs
    assert b.closed_prs == set()


def test_find_open_pr_by_head_branch_dispatches_to_per_project_provider():
    a = FakeGitProvider()
    b = FakeGitProvider()
    b.set_pr_state(
        project="b",
        pr_number=42,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    b.set_pr_head_branch(
        project="b",
        pr_number=42,
        branch_name="foreman/issue-180",
    )
    router = RoutingGitProvider(providers={"a": a, "b": b})
    found = router.find_open_pr_by_head_branch(
        project="b",
        branch_name="foreman/issue-180",
    )
    assert found == 42
    # Same branch name on project "a" must NOT match — project routing
    # is the whole point.
    assert (
        router.find_open_pr_by_head_branch(
            project="a",
            branch_name="foreman/issue-180",
        )
        is None
    )


def test_update_branch_dispatches_to_per_project_provider():
    """foreman#416: update_branch routes by project like every other
    Protocol method. The asymmetry (other-not-called) is load-bearing."""
    a = FakeGitProvider()
    b = FakeGitProvider()
    b.set_pr_state(
        project="b",
        pr_number=42,
        state=PRState(merged=False, mergeable=False, ci_passing=True),
    )
    router = RoutingGitProvider(providers={"a": a, "b": b})
    router.update_branch(project="b", pr_number=42)
    assert b.update_branch_calls == [("b", 42)]
    assert a.update_branch_calls == []


def test_required_check_state_dispatches_to_per_project_provider():
    """foreman#317: required_check_state routes by project like every
    other Protocol method (closes a gap left by Task 1, which added the
    method to the Protocol/Fake/PyGithub trio but not the router)."""
    a = FakeGitProvider()
    b = FakeGitProvider()
    b.seed_check_state("b", 42, RequiredCheckState.FAILED)
    router = RoutingGitProvider(providers={"a": a, "b": b})
    result = router.required_check_state(project="b", pr_number=42)
    assert result == RequiredCheckState.FAILED
    # Asymmetry check: "a" was never seeded, so if the router silently
    # routed there instead it would surface the default PENDING, not
    # FAILED. Reading FAILED back proves the "b" provider was hit.


def test_rerun_failed_checks_dispatches_to_per_project_provider():
    """foreman#317: rerun_failed_checks routes by project like every
    other Protocol method. The asymmetry (other-not-called) is
    load-bearing."""
    a = FakeGitProvider()
    b = FakeGitProvider()
    router = RoutingGitProvider(providers={"a": a, "b": b})
    router.rerun_failed_checks(project="b", pr_number=42)
    assert b.rerun_failed_checks_calls == [("b", 42)]
    assert a.rerun_failed_checks_calls == []


def test_delete_branch_unknown_project_raises():
    router = RoutingGitProvider(providers={"a": FakeGitProvider()})
    with pytest.raises(UnknownProjectError):
        router.delete_branch(project="nope", branch_name="x")


def test_get_issue_labels_dispatches_to_per_project_provider():
    a = FakeGitProvider()
    b = FakeGitProvider()
    b.seed_issue_labels(
        project="b",
        issue_number=1,
        labels={"foreman:plan"},
    )
    router = RoutingGitProvider(providers={"a": a, "b": b})
    assert router.get_issue_labels(project="b", issue_number=1) == {"foreman:plan"}


# ---------------------------------------------------------------------------
# Issue-comment routing (sub-request 3 of issue #410)
# ---------------------------------------------------------------------------


def test_get_issue_comments_dispatches_to_correct_provider() -> None:
    """get_issue_comments routes by project — only the named provider is read."""
    a = FakeGitProvider()
    b = FakeGitProvider()
    comment = CommentRef(
        author_login="bot",
        posted_at=dt.datetime(2026, 6, 20, tzinfo=dt.UTC),
        body="hello",
    )
    b.seed_issue_comments(project="b", issue_number=5, comments=[comment])
    router = RoutingGitProvider(providers={"a": a, "b": b})

    result = router.get_issue_comments(project="b", issue_number=5)
    assert result == [comment]
    # Other project returns empty — no cross-project read.
    assert router.get_issue_comments(project="a", issue_number=5) == []


def test_post_issue_comment_dispatches_to_correct_provider() -> None:
    """post_issue_comment routes by project — only the named provider is written."""
    a = FakeGitProvider()
    b = FakeGitProvider()
    router = RoutingGitProvider(providers={"a": a, "b": b})

    router.post_issue_comment(project="b", issue_number=7, body="hi there")

    assert b.posted_comments == [("b", 7, "hi there")]
    # The other provider was NOT written to.
    assert a.posted_comments == []


def test_get_issue_comments_unknown_project_raises() -> None:
    router = RoutingGitProvider(providers={"a": FakeGitProvider()})
    with pytest.raises(UnknownProjectError):
        router.get_issue_comments(project="nope", issue_number=1)


def test_post_issue_comment_unknown_project_raises() -> None:
    router = RoutingGitProvider(providers={"a": FakeGitProvider()})
    with pytest.raises(UnknownProjectError):
        router.post_issue_comment(project="nope", issue_number=1, body="x")


# ---------------------------------------------------------------------------
# get_issue_state_reason routing (foreman#524)
# ---------------------------------------------------------------------------


def test_get_issue_state_reason_dispatches_to_correct_provider() -> None:
    """get_issue_state_reason routes by project — only the named provider is read."""
    foo, bar, router = _two_provider_router()
    bar.set_issue_state_reason(project="bar", issue_number=7, reason="completed")
    foo.set_issue_state_reason(project="foo", issue_number=7, reason="not_planned")

    # Routed to bar → returns bar's reason, not foo's.
    assert router.get_issue_state_reason(project="bar", issue_number=7) == "completed"
    # Routed to foo → returns foo's reason.
    assert router.get_issue_state_reason(project="foo", issue_number=7) == "not_planned"


def test_get_issue_state_reason_unknown_project_raises() -> None:
    router = RoutingGitProvider(providers={"a": FakeGitProvider()})
    with pytest.raises(UnknownProjectError):
        router.get_issue_state_reason(project="nope", issue_number=1)


# ---------------------------------------------------------------------------
# read_blocked_by routing (foreman#524)
# ---------------------------------------------------------------------------


def test_read_blocked_by_dispatches_to_correct_provider() -> None:
    """read_blocked_by routes by project — only the named provider is queried."""
    foo, bar, router = _two_provider_router()
    bar.set_blocked_by(project="bar", issue_number=291, blocked_by=[290])
    foo.set_blocked_by(project="foo", issue_number=291, blocked_by=[100, 101])

    # Routed to bar → returns bar's blockers, not foo's.
    assert router.read_blocked_by(project="bar", issue_number=291) == [290]
    # Routed to foo → returns foo's blockers.
    assert router.read_blocked_by(project="foo", issue_number=291) == [100, 101]


def test_read_blocked_by_unknown_project_raises() -> None:
    router = RoutingGitProvider(providers={"a": FakeGitProvider()})
    with pytest.raises(UnknownProjectError):
        router.read_blocked_by(project="nope", issue_number=1)
