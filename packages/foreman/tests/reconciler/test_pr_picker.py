"""Tests for ``_pick_pr_for_ticket`` — the daemon's shape-aware PR selector.

When both a spec PR (``foreman/issue-N``) and an impl PR (``foreman/impl-N``)
carry ``closingIssuesReferences`` pointing to the same issue (transient
stacked-PR window), GraphQL result ordering is undefined. The picker must
select the PR whose shape matches the issue's current label phase so that
``ctx.pr`` always lines up with the rules that will fire against it
(adversarial review MEDIUM #11).
"""

from __future__ import annotations

from datetime import UTC, datetime

from foreman.reconciler.daemon import _pick_pr_for_ticket
from foreman.reconciler.state import IssueState, PRState


def _issue(labels: tuple[str, ...]) -> IssueState:
    return IssueState(
        number=42,
        title="t",
        labels=labels,
        assignees=(),
        body="",
        updated_at=datetime(2026, 6, 4, tzinfo=UTC),
    )


def _pr(head_ref: str, *, number: int = 99) -> PRState:
    return PRState(
        number=number,
        head_ref=head_ref,
        mergeable="MERGEABLE",
        ci_status="SUCCESS",
        body="",
        linked_issue_numbers=(42,),
        is_merged=False,
        review_decision=None,
    )


def test_picker_prefers_spec_pr_when_issue_in_planning() -> None:
    """``foreman:planning`` → spec PR even if impl PR is listed first."""
    issue = _issue(("foreman:planning",))
    spec = _pr("foreman/issue-42", number=100)
    impl = _pr("foreman/impl-42", number=200)
    # Impl first to ensure the picker isn't just returning linked_prs[0].
    assert _pick_pr_for_ticket(issue, [impl, spec]).head_ref == "foreman/issue-42"


def test_picker_prefers_impl_pr_when_issue_in_impl_review() -> None:
    """``foreman:impl-review`` → impl PR even if spec PR is listed first."""
    issue = _issue(("foreman:impl-review",))
    spec = _pr("foreman/issue-42", number=100)
    impl = _pr("foreman/impl-42", number=200)
    assert _pick_pr_for_ticket(issue, [spec, impl]).head_ref == "foreman/impl-42"


def test_picker_returns_none_when_no_prs() -> None:
    """No linked PRs → ``None`` (preserves the pre-fix sentinel behavior)."""
    issue = _issue(("foreman:planning",))
    assert _pick_pr_for_ticket(issue, []) is None


def test_picker_prefers_spec_pr_on_plan_approved() -> None:
    """``foreman:plan-approved`` is still spec-phase — picks spec PR."""
    issue = _issue(("foreman:plan-approved",))
    spec = _pr("foreman/issue-42", number=100)
    impl = _pr("foreman/impl-42", number=200)
    assert _pick_pr_for_ticket(issue, [impl, spec]).head_ref == "foreman/issue-42"


def test_picker_prefers_spec_pr_on_spec_fix() -> None:
    """``foreman:spec-fix`` is spec-phase (the spec-side Fixer loop)."""
    issue = _issue(("foreman:spec-fix",))
    spec = _pr("foreman/issue-42", number=100)
    impl = _pr("foreman/impl-42", number=200)
    assert _pick_pr_for_ticket(issue, [impl, spec]).head_ref == "foreman/issue-42"


def test_picker_prefers_impl_pr_on_impl_fix() -> None:
    """``foreman:impl-fix`` is impl-phase."""
    issue = _issue(("foreman:impl-fix",))
    spec = _pr("foreman/issue-42", number=100)
    impl = _pr("foreman/impl-42", number=200)
    assert _pick_pr_for_ticket(issue, [spec, impl]).head_ref == "foreman/impl-42"


def test_picker_prefers_impl_pr_on_impl_approved() -> None:
    """``foreman:impl-approved`` is impl-phase."""
    issue = _issue(("foreman:impl-approved",))
    spec = _pr("foreman/issue-42", number=100)
    impl = _pr("foreman/impl-42", number=200)
    assert _pick_pr_for_ticket(issue, [spec, impl]).head_ref == "foreman/impl-42"


def test_picker_prefers_impl_when_both_phases_present() -> None:
    """Transient label state spans both phases — later stage wins (impl)."""
    issue = _issue(("foreman:planning", "foreman:impl-review"))
    spec = _pr("foreman/issue-42", number=100)
    impl = _pr("foreman/impl-42", number=200)
    assert _pick_pr_for_ticket(issue, [spec, impl]).head_ref == "foreman/impl-42"


def test_picker_falls_back_to_first_when_no_phase_match() -> None:
    """No foreman phase labels → return the first linked PR (legacy/operator
    PRs that don't follow foreman branch conventions still get a PR
    assigned; rules will filter by head_ref anyway)."""
    issue = _issue(("some-other-label",))
    pr_a = _pr("custom-branch-a", number=100)
    pr_b = _pr("custom-branch-b", number=200)
    assert _pick_pr_for_ticket(issue, [pr_a, pr_b]) is pr_a


def test_picker_falls_back_when_phase_set_but_no_shape_match() -> None:
    """If the issue is in spec phase but only an off-shape PR is linked,
    fall back to the first PR rather than returning None — rules will
    still refuse to fire via their own head_ref filters."""
    issue = _issue(("foreman:planning",))
    odd = _pr("custom/branch", number=100)
    assert _pick_pr_for_ticket(issue, [odd]) is odd
