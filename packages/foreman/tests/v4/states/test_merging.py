"""MergingState — guards the impl PR's base ref, then hands it to the merge queue.

foreman#550 moved the actual merge (``attempt_merge`` + the healer/
classifier machinery) out of this state and into the (forthcoming) per-repo
``MergeCoordinator``. ``MergingState.execute()`` now does two things only:
the foreman#357 base-ref guard (still enforced here — a wrong-base PR must
never even enter the FIFO) and the hand-off itself (``ctx.repo.enqueue_merge``
+ route to ``MergeQueued``). These tests pin that shape:

  - PR base mismatches the project's configured dev_base_branch → NEEDS_HELP,
    nothing enqueued (the base-ref guard test block below).
  - PR base matches → enqueue one ``impl`` merge_queue entry, route to
    MergeQueued.
  - A second execute() on an already-queued ticket does not double-enqueue
    (idempotent hand-off).

The merge classifier itself (BLOCKED wait-for-CI, the BehindBranchHealer,
NEEDS_FIX for a dirty/CI-failed PR) is no longer exercised at this layer —
that logic still lives in ``attempt_merge`` (``merge_helper.py``), reused by
the coordinator (a later foreman#550 task), not by MergingState directly.
"""

from __future__ import annotations

import datetime as dt
import logging

from foreman.v4.config import ProjectConfig
from foreman.v4.git_provider import FakeGitProvider, PRState
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext
from foreman.v4.states.merging import MergingState


def _project_config(
    *,
    name: str = "p",
    dev_base_branch: str | None = "main",
) -> ProjectConfig:
    """Build a minimal ProjectConfig for the merging tests."""
    return ProjectConfig(
        name=name,
        repo=f"o/{name}",
        local_clone_path=f"/tmp/{name}",
        dev_base_branch=dev_base_branch,
    )


def _seed_prior_outcome(repo: InMemoryTicketRepository, ticket_id: int, pr_number: int) -> None:
    """Seed a prior ExecuteCompleted state instance carrying the PR number.

    Mirrors what ImplReviewState would have written before the ticket reached
    Merging. The repository's `latest_pr_number_for_ticket` then resolves to
    `pr_number`.
    """
    prior = repo.open_state_instance(
        ticket_id=ticket_id,
        state_name="ImplReview",
        sequence=0,
        now=dt.datetime(2026, 6, 13),
    )
    repo.mark_execute_completed(
        prior.id,
        now=dt.datetime(2026, 6, 13),
        outcome_kind=OutcomeKind.CLEAN,
        outcome_payload={"artifacts": {"pr_number": pr_number}},
        next_state="Merging",
    )
    repo.close_state_instance(prior.id, now=dt.datetime(2026, 6, 13))


def _ctx_with_pr(
    pr_number: int = 99,
    *,
    base_ref: str = "main",
    project_configs: dict[str, ProjectConfig] | None = None,
) -> tuple[StateContext, InMemoryTicketRepository, FakeGitProvider]:
    """Build a StateContext where the ticket is in Merging with the named PR
    seeded against the FakeGitProvider at the given base ref.

    foreman#357: by default the PR's ``base_ref`` is ``"main"`` and the
    ``project_configs`` map contains a project ``"p"`` with
    ``dev_base_branch="main"`` so the base-ref guard passes by default.
    Tests exercising the guard pass an explicit ``base_ref`` and/or
    ``project_configs``.
    """
    if project_configs is None:
        project_configs = {"p": _project_config()}
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.set_ticket_state(ticket.id, "Merging", now=dt.datetime(2026, 6, 13))
    _seed_prior_outcome(repo, ticket.id, pr_number)
    instance = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="Merging",
        sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    git = FakeGitProvider()
    git.set_pr_state(
        project="p",
        pr_number=pr_number,
        state=PRState(merged=False, mergeable=True, ci_passing=True, base_ref=base_ref),
    )
    ctx = StateContext(
        ticket=repo.get_ticket(ticket.id),
        instance=instance,
        repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        git=git,
        project_configs=project_configs,
    )
    return ctx, repo, git


def test_merging_state_enqueues_impl_pr_and_routes_to_merge_queued():
    """Happy path: base ref matches → one 'impl' merge_queue entry, MergeQueued."""
    ctx, repo, git = _ctx_with_pr(pr_number=99, base_ref="main")
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "MergeQueued"

    entries = repo.merge_queue_for_project("p")
    assert len(entries) == 1
    assert entries[0].ticket_id == ctx.ticket.id
    assert entries[0].pr_number == 99
    assert entries[0].kind == "impl"
    # The merge itself doesn't happen here anymore — the coordinator does it.
    assert ("p", 99) not in git.merge_pr_calls
    assert git.closed_issues == set()


def test_merging_state_second_execute_does_not_double_enqueue():
    """A re-dispatched Merging execute() on an already-queued ticket must
    not insert a second merge_queue row for the same ticket."""
    ctx, repo, _git = _ctx_with_pr(pr_number=99, base_ref="main")
    MergingState().execute(ctx)
    MergingState().execute(ctx)
    entries = repo.merge_queue_for_project("p")
    assert len(entries) == 1


def test_merging_state_next_state_defensive_fallback_to_needs_help():
    """execute() only ever emits CLEAN or NEEDS_HELP now that the merge
    classifier lives in the coordinator, not here — but next_state() keeps
    a defensive fallback for any other outcome kind, so a future change to
    execute() can't silently strand a ticket instead of surfacing to a
    human."""
    ctx, _repo, _git = _ctx_with_pr(pr_number=99)
    outcome = Outcome(kind=OutcomeKind.NEEDS_FIX, confidence=OutcomeConfidence.HIGH, summary="x")
    next_state = MergingState().next_state(ctx, outcome)
    assert next_state is not None
    assert next_state.state_name == "NeedsHelp"


def test_missing_git_provider_routes_through_execute_failure():
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    _seed_prior_outcome(repo, ticket.id, 99)
    instance = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="Merging",
        sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    ctx = StateContext(
        ticket=ticket,
        instance=instance,
        repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        # git omitted
    )
    MergingState().transition(ctx)
    closed = repo.get_state_instance(instance.id)
    assert closed.failure_phase == "execute"


# ---------------------------------------------------------------------------
# Base-ref guard (foreman#357) — still enforced before the hand-off.
#
# Defense-in-depth: before enqueueing, MergingState compares the impl PR's
# base ref against the project's configured dev_base_branch. Mismatch →
# NEEDS_HELP, nothing enqueued. The bug class this catches was directly
# observed twice in production (foreman#341 logic bug; foreman#347
# stale-binary regression) and would silently move substrate state forward
# into the spec branch.
# ---------------------------------------------------------------------------


def _outcome_payload(repo: InMemoryTicketRepository, ticket_id: int) -> dict:
    """Read the latest Merging state instance's outcome payload from the
    in-memory repo so tests can assert on ``details`` carried into the
    NEEDS_HELP outcome.
    """
    rows = repo.list_state_instances_for_ticket(ticket_id)
    merging_rows = [r for r in rows if r.state_name == "Merging"]
    latest = merging_rows[-1]
    assert latest.outcome_payload is not None
    return latest.outcome_payload


def test_merging_state_refuses_to_enqueue_when_pr_base_diverges_from_dev_base_branch():
    """The bug class from foreman#341 / #347: Worker emitted the impl PR
    with ``base=foreman/issue-N`` (the spec branch) instead of the
    project's ``dev_base_branch``. Without the guard, MergingState would
    enqueue a merge into the spec branch. With the guard, it returns
    NEEDS_HELP and nothing is enqueued.
    """
    ctx, repo, git = _ctx_with_pr(
        pr_number=99,
        base_ref="foreman/issue-1",
        project_configs={"p": _project_config(dev_base_branch="main")},
    )
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "NeedsHelp"
    # CRITICAL invariants: nothing enqueued and the issue stays open.
    assert repo.merge_queue_for_project("p") == []
    assert ("p", 1) not in git.closed_issues
    # The outcome's details bag carries the diagnostic triplet so the
    # operator can see actual vs expected base directly.
    payload = _outcome_payload(repo, ctx.ticket.id)
    details = payload["details"]
    assert details["actual_base"] == "foreman/issue-1"
    assert details["expected_base"] == "main"
    assert details["pr_number"] == 99


def test_merging_state_enqueues_when_pr_base_matches_dev_base_branch():
    """Happy path regression: with the guard in place, the plain-vanilla
    matching-base flow still enqueues and routes to MergeQueued. If this
    fails, the guard accidentally blocks every hand-off.
    """
    ctx, repo, _git = _ctx_with_pr(
        pr_number=99,
        base_ref="main",
        project_configs={"p": _project_config(dev_base_branch="main")},
    )
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "MergeQueued"
    assert len(repo.merge_queue_for_project("p")) == 1


def test_merging_state_base_ref_comparison_is_case_insensitive():
    """GitHub returns refs lower-case for some legacy repos; the
    comparison normalizes both sides via casefold so the guard isn't
    accidentally case-fragile.
    """
    # Config Main, PR main → match → MergeQueued.
    ctx_a, repo_a, _git_a = _ctx_with_pr(
        pr_number=99,
        base_ref="main",
        project_configs={"p": _project_config(dev_base_branch="Main")},
    )
    next_a = MergingState().transition(ctx_a)
    assert next_a is not None
    assert next_a.state_name == "MergeQueued"
    assert len(repo_a.merge_queue_for_project("p")) == 1

    # Config main, PR MAIN → match → MergeQueued.
    ctx_b, repo_b, _git_b = _ctx_with_pr(
        pr_number=99,
        base_ref="MAIN",
        project_configs={"p": _project_config(dev_base_branch="main")},
    )
    next_b = MergingState().transition(ctx_b)
    assert next_b is not None
    assert next_b.state_name == "MergeQueued"
    assert len(repo_b.merge_queue_for_project("p")) == 1


def test_merging_state_falls_back_to_main_when_dev_base_branch_unset():
    """``ProjectConfig.dev_base_branch=None`` falls back to
    ``DEFAULT_DEV_BASE_BRANCH = "main"``. The Worker resolves None to
    origin's actual default branch by probing a clone; MergingState has no
    clone to probe, so the documented constant fallback is the smallest
    correct shape.
    """
    # Happy: PR base "main" matches the fallback → MergeQueued.
    ctx_ok, repo_ok, _git_ok = _ctx_with_pr(
        pr_number=99,
        base_ref="main",
        project_configs={"p": _project_config(dev_base_branch=None)},
    )
    next_ok = MergingState().transition(ctx_ok)
    assert next_ok is not None
    assert next_ok.state_name == "MergeQueued"
    assert len(repo_ok.merge_queue_for_project("p")) == 1

    # Refusal: PR base "foreman/issue-1" diverges from the fallback →
    # NeedsHelp.
    ctx_bad, repo_bad, _git_bad = _ctx_with_pr(
        pr_number=99,
        base_ref="foreman/issue-1",
        project_configs={"p": _project_config(dev_base_branch=None)},
    )
    next_bad = MergingState().transition(ctx_bad)
    assert next_bad is not None
    assert next_bad.state_name == "NeedsHelp"
    assert repo_bad.merge_queue_for_project("p") == []


def test_merging_state_refuses_when_base_ref_empty():
    """``base_ref=""`` means "couldn't read it" — the production PyGithub
    path always populates it, so an empty value signals a config-shape
    problem or a Fake provider that wasn't seeded. Treat the unknown as a
    refusal — never silently enqueue.
    """
    ctx, repo, git = _ctx_with_pr(
        pr_number=99,
        base_ref="",
        project_configs={"p": _project_config(dev_base_branch="main")},
    )
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "NeedsHelp"
    assert repo.merge_queue_for_project("p") == []
    assert ("p", 1) not in git.closed_issues


def test_merging_state_skips_guard_when_project_config_missing(caplog):
    """Legacy test shape: when ``project_configs`` is empty (or doesn't
    contain ``ticket.project``), the guard short-circuits with a warning
    log line and the hand-off proceeds unchanged. Keeps the change
    additive — older tests don't have to thread the map through.
    """
    ctx, repo, _git = _ctx_with_pr(
        pr_number=99,
        base_ref="anything",
        project_configs={},
    )
    with caplog.at_level(logging.WARNING, logger="foreman.v4.states.merging"):
        next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "MergeQueued"
    assert len(repo.merge_queue_for_project("p")) == 1
    # The skip must be operator-visible.
    assert any("no project_config for project=p" in rec.message for rec in caplog.records), (
        caplog.records
    )
