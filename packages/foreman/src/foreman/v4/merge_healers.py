"""merge_healers — pluggable self-heal registry for mergeable-blocked PRs.

foreman#416. When ``get_pr_state`` reports a PR that is neither merged nor
``mergeable + ci_passing``, the merge states (``MergingState`` for impl
PRs, ``SpecMerging`` for spec PRs) consult this registry BEFORE falling
back to the plain "wait for CI" BLOCKED behavior. The first healer whose
``applies(pr)`` is True gets to ``heal(...)`` the PR; its :class:`HealResult`
tells the caller what to do next.

The bug class this fixes
------------------------
A spec PR that is ``BEHIND`` (its base advanced while it sat in review)
used to make ``SpecReviewState.verify()`` call ``merge_pr`` → HTTP 405 →
escalate to NeedsHelp. That forced a manual operator rescue of
agent_core#190. An impl PR that is BEHIND looped ``BLOCKED`` forever in
``MergingState`` (it was never mergeable, so the merge gate never fired,
and nothing advanced the branch). The :class:`BehindBranchHealer` calls
``update_branch`` so the PR is no longer behind on the next poll.

Extension point — MERGE_HEALERS
-------------------------------
``MERGE_HEALERS`` is THE place to teach the daemon a new mergeable-blocked
corner case. To add one:

    1. Write a class satisfying :class:`MergeHealer` (``name``, ``applies``,
       ``heal``).
    2. ``applies`` should key off a specific ``pr.mergeable_state`` (or
       other ``PRState`` field) so only the intended case matches.
    3. ``heal`` performs the remediation via ``ctx.git`` and returns a
       :class:`HealResult` — ``RETRY`` (acted, re-poll), ``PROCEED`` (now
       mergeable, re-evaluate), or ``ESCALATE`` (needs a human).
    4. Append an instance to ``MERGE_HEALERS`` below.

That is the entire change surface — the merge states call into the
registry generically, so no state code changes for a new corner case.
Order matters: the FIRST applicable healer wins, so place more-specific
healers before broader ones.
"""
from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from foreman.v4.git_provider import GitProvider, PRState


class HealResult(Enum):
    """What a healer's action means for the merge state machine.

    * ``RETRY`` — the healer acted (e.g. issued ``update_branch``); the PR
      should be re-polled on the next tick. The caller maps this to a
      BLOCKED outcome. Crucially this self-loop must be retry-cap-exempt:
      BLOCKED rows are already skipped by
      ``count_consecutive_same_state`` (Phase 8d.18), so the runaway
      defense won't trip on a legitimate heal-then-poll cycle. The
      separate heal-action bound in ``attempt_merge`` catches pathological
      base-churn instead.
    * ``PROCEED`` — the healer determined the PR is now mergeable; the
      caller re-evaluates (mapped to BLOCKED today so the next poll runs
      the merge gate cleanly rather than racing a stale ``PRState``).
    * ``ESCALATE`` — the corner case needs a human; the caller maps this
      to a NEEDS_HELP outcome.
    """

    RETRY = auto()
    ESCALATE = auto()
    PROCEED = auto()


class _HealContext(Protocol):
    """Structural shape a healer needs from the merge state's context.

    Only the ``git`` provider is required today. Using a Protocol (rather
    than importing ``StateContext``) keeps ``merge_healers`` free of a
    state-layer import and lets the healer tests pass a tiny stand-in. The
    real :class:`~foreman.v4.state.StateContext` satisfies this structurally
    — it has a ``git: GitProvider | None`` attribute — and ``attempt_merge``
    passes it as the ``ctx`` argument to ``heal``. This IS the annotated
    param type below, so the shape is load-bearing, not decorative.
    """

    @property
    def git(self) -> GitProvider | None: ...


@runtime_checkable
class MergeHealer(Protocol):
    """One self-heal strategy for a mergeable-blocked PR.

    Implementations are stateless singletons registered in
    ``MERGE_HEALERS``.
    """

    name: str

    def applies(self, pr: PRState) -> bool:
        """True iff this healer handles the PR's current mergeable state."""
        ...

    def heal(
        self, ctx: _HealContext, *, project: str, pr_number: int, pr: PRState,
    ) -> HealResult:
        """Perform the remediation; return what the caller should do next."""
        ...


class BehindBranchHealer:
    """Heal a PR whose base advanced while it waited (``"behind"``).

    ``applies`` matches ``pr.mergeable_state == "behind"``. ``heal`` issues
    ``update_branch`` (GitHub's "Update branch" button) and returns
    ``RETRY`` — after the update lands, the PR is no longer behind, so the
    next poll runs the normal merge gate. A normal BEHIND therefore heals
    in 1–2 cycles; only pathological base-churn keeps it behind, which the
    heal-action bound in ``attempt_merge`` catches.
    """

    name = "behind-branch"

    def applies(self, pr: PRState) -> bool:
        """True when the PR's ``mergeable_state`` is exactly ``"behind"``."""
        return pr.mergeable_state == "behind"

    def heal(
        self, ctx: _HealContext, *, project: str, pr_number: int, pr: PRState,
    ) -> HealResult:
        """Issue GitHub's "Update branch" so the PR's base catches up.

        Returns ``RETRY`` so the caller re-polls next tick, by which point
        the update has landed and the PR is no longer behind.
        """
        # attempt_merge guarantees ctx.git is non-None before reaching any
        # healer (it raises otherwise); narrow for the type checker.
        assert ctx.git is not None
        ctx.git.update_branch(project=project, pr_number=pr_number)
        return HealResult.RETRY


#: The healer registry. THE extension point for new mergeable-blocked
#: corner cases — append a :class:`MergeHealer` here (see the module
#: docstring). First applicable healer wins, so order most-specific first.
MERGE_HEALERS: list[MergeHealer] = [BehindBranchHealer()]
