"""RoutingGitProvider — dispatches every GitProvider call by ``project=``.

The fix for F1 (Phase 8d.16): ``PyGithubGitProvider`` is locked at
construction to ONE ``repo_full_name`` and ignores the ``project=`` kwarg
every Protocol method takes. In a multi-project daemon, bootstrap built
one provider per project but only threaded the FIRST into cross-project
consumers (LabelObservabilityObserver + state-machine spec-merge /
merge-queue calls). The router fixes that — every cross-project call gets
dispatched to the right per-project provider.

These tests verify EVERY method on the :class:`GitProvider` Protocol
dispatches to the named provider AND not to any other registered
provider. The asymmetry (other-not-called) is load-bearing: without it,
a router that broadcasts to every provider would also pass the dispatch
assertion, masking the original bug class.
"""

from __future__ import annotations

import pytest

from foreman.v4.git_provider import FakeGitProvider, MergeVerdict, PRState
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


def test_dispatches_merge_spec_pr_to_correct_provider() -> None:
    foo, bar, router = _two_provider_router()
    bar.set_pr_state(
        project="bar", pr_number=42,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )

    router.merge_spec_pr(project="bar", pr_number=42)

    assert bar.get_pr_state(project="bar", pr_number=42).merged is True
    # foo never had PR 42 set — if the router mis-routed, foo would have
    # raised PRNotFoundError; if it broadcast, foo would now have a
    # merged-PR-42. Neither happened.
    with pytest.raises(LookupError):
        foo.get_pr_state(project="foo", pr_number=42)


def test_dispatches_add_labels_to_correct_provider() -> None:
    foo, bar, router = _two_provider_router()

    router.add_labels(
        project="bar", issue_number=7, labels={"foreman:state-planning"},
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
        project="foo", issue_number=7, labels={"foreman:state-planning"},
    )
    bar.seed_issue_labels(
        project="bar", issue_number=7, labels={"foreman:state-planning"},
    )

    router.remove_labels(
        project="bar", issue_number=7, labels={"foreman:state-planning"},
    )

    assert bar.get_issue_labels(project="bar", issue_number=7) == set()
    # foo's label survives — router did not broadcast.
    assert foo.get_issue_labels(project="foo", issue_number=7) == {
        "foreman:state-planning",
    }


def test_dispatches_list_open_issues_with_label_to_correct_provider() -> None:
    foo, bar, router = _two_provider_router()
    foo.set_open_issues_with_label(
        project="foo", label="foreman:plan", issue_numbers={1, 2, 3},
    )
    bar.set_open_issues_with_label(
        project="bar", label="foreman:plan", issue_numbers={99},
    )

    result = router.list_open_issues_with_label(
        project="bar", label="foreman:plan",
    )

    assert result == [99]


def test_dispatches_enqueue_merge_queue_to_correct_provider() -> None:
    foo, bar, router = _two_provider_router()
    bar.set_pr_state(
        project="bar", pr_number=15,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )

    router.enqueue_merge_queue(project="bar", pr_number=15)

    assert ("bar", 15) in bar.merge_queue
    # foo's queue never touched.
    assert ("foo", 15) not in foo.merge_queue
    assert ("bar", 15) not in foo.merge_queue


def test_dispatches_merge_verdict_to_correct_provider() -> None:
    foo, bar, router = _two_provider_router()
    bar.set_pr_state(
        project="bar", pr_number=20,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    bar.set_merge_verdict(
        project="bar", pr_number=20, verdict=MergeVerdict.MERGED,
    )

    verdict = router.merge_verdict(project="bar", pr_number=20)

    assert verdict is MergeVerdict.MERGED
    # foo has no verdict registered for (foo, 20) so it returns the
    # default PENDING — proving the call did NOT cross over to bar.
    assert foo.merge_verdict(project="foo", pr_number=20) is MergeVerdict.PENDING


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
            project="never_registered", issue_number=1, labels={"x"},
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
