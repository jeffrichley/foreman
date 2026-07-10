"""merge_healers — pluggable self-heal registry for mergeable-blocked PRs.

foreman#416. A spec PR that is ``BEHIND`` (base advanced while it waited)
used to make ``SpecReviewState.verify()`` call ``merge_pr`` → HTTP 405 →
escalate to NeedsHelp (the agent_core#190 manual-rescue incident). An impl
PR that is BEHIND looped ``BLOCKED`` forever in MergingState. The healer
framework lets the daemon self-heal BEHIND (and future corner cases)
before falling back to BLOCKED/NEEDS_HELP.

These tests pin down:

  - ``BehindBranchHealer.applies`` is True ONLY for
    ``mergeable_state == "behind"``.
  - ``BehindBranchHealer.heal`` calls ``update_branch`` exactly once and
    returns ``HealResult.RETRY``.
  - The ``MERGE_HEALERS`` registry contains the BehindBranchHealer (the
    documented extension point).
"""

from __future__ import annotations

from foreman.v4.git_provider import FakeGitProvider, PRState
from foreman.v4.merge_healers import (
    MERGE_HEALERS,
    BehindBranchHealer,
    HealResult,
    MergeHealer,
)


class _Ctx:
    """Minimal heal-context stand-in carrying a git provider."""

    def __init__(self, git: FakeGitProvider) -> None:
        self.git = git


def _pr(*, mergeable_state: str) -> PRState:
    return PRState(
        merged=False,
        mergeable=False,
        ci_passing=True,
        base_ref="main",
        mergeable_state=mergeable_state,
    )


def test_behind_healer_applies_only_when_behind():
    healer = BehindBranchHealer()
    assert healer.applies(_pr(mergeable_state="behind")) is True
    # Every other mergeable_state must NOT trigger this healer.
    for other in ("clean", "blocked", "dirty", "unstable", "unknown", ""):
        assert healer.applies(_pr(mergeable_state=other)) is False


def test_behind_healer_heal_calls_update_branch_once_and_returns_retry():
    git = FakeGitProvider()
    git.set_pr_state(
        project="p",
        pr_number=7,
        state=_pr(mergeable_state="behind"),
    )
    healer = BehindBranchHealer()
    result = healer.heal(
        _Ctx(git),
        project="p",
        pr_number=7,
        pr=git.get_pr_state(project="p", pr_number=7),
    )
    assert result is HealResult.RETRY
    assert git.update_branch_calls == [("p", 7)]


def test_behind_healer_has_a_name():
    assert BehindBranchHealer().name == "behind-branch"


def test_registry_contains_behind_healer():
    """MERGE_HEALERS is the documented extension point: append a healer
    here to teach the daemon a new mergeable-blocked corner case."""
    assert any(isinstance(h, BehindBranchHealer) for h in MERGE_HEALERS)


def test_behind_healer_is_a_merge_healer_structurally():
    """BehindBranchHealer satisfies the MergeHealer Protocol shape."""
    healer: MergeHealer = BehindBranchHealer()
    assert callable(healer.applies)
    assert callable(healer.heal)
    assert isinstance(healer.name, str)
