"""MergingState — direct pr.merge() against the impl PR.

Phase 8d.19 collapsed the MergeQueue enqueue-then-poll path into one
direct merge call. These tests pin down the 3-branch shape of
``MergingState.execute``:

  - PR already merged externally → CLEAN (no merge_pr call).
  - PR mergeable + CI passing → call merge_pr → CLEAN.
  - Anything else → BLOCKED (Poller picks it up next tick).

foreman#357 added a fourth branch in front of all three: the base-ref
guard returns NEEDS_HELP when ``pr.base.ref`` doesn't match the
project's configured ``dev_base_branch``. The guard tests live below
the original three-branch tests, after the helper.

Granular ``mergeable_state`` handling (CI failed → ImplFix, dirty →
ImplFix, etc.) is deferred to foreman#317. The BLOCKED branch is the
catch-all today.
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
    pr_state: PRState | None = None,
    base_ref: str = "main",
    project_configs: dict[str, ProjectConfig] | None = None,
) -> tuple[StateContext, InMemoryTicketRepository, FakeGitProvider]:
    """Build a StateContext where the ticket is in Merging with the
    named PR seeded against the FakeGitProvider in the given state.

    The default ``pr_state`` is mergeable + CI passing + not yet merged
    — the "execute() should call merge_pr" happy path. Tests override
    when they want to exercise the merged-externally or BLOCKED branches.

    foreman#357: by default the PR's ``base_ref`` is ``"main"`` and the
    ``project_configs`` map contains a project ``"p"`` with
    ``dev_base_branch="main"`` so existing tests pass through the new
    base-ref guard unchanged. Tests overriding the guard pass an
    explicit ``base_ref`` and/or ``project_configs``.
    """
    if pr_state is None:
        pr_state = PRState(
            merged=False,
            mergeable=True,
            ci_passing=True,
            base_ref=base_ref,
        )
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
    git.set_pr_state(project="p", pr_number=pr_number, state=pr_state)
    ctx = StateContext(
        ticket=repo.get_ticket(ticket.id),
        instance=instance,
        repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        git=git,
        project_configs=project_configs,
    )
    return ctx, repo, git


def test_merging_state_returns_clean_when_pr_merged_externally():
    """If the PR is already merged by something outside the daemon
    (operator click-merge, GitHub's own merge-queue, an earlier daemon
    instance), execute() returns CLEAN without calling merge_pr again.

    The recorder asymmetry is load-bearing: a naive implementation that
    always calls merge_pr would set merged=True via the fake's state
    machine and "pass" a merged-state assertion, masking the bug class.

    Phase 8d.20 extension: the originating issue must STILL get closed
    on the already-merged branch — otherwise an operator click-merge
    leaves the issue OPEN forever despite the loop reaching Done.
    """
    ctx, _repo, git = _ctx_with_pr(
        pr_number=99,
        pr_state=PRState(
            merged=True,
            mergeable=True,
            ci_passing=True,
            base_ref="main",
        ),
    )
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Done"
    # NO merge_pr call — the PR was already merged.
    assert ("p", 99) not in git.merge_pr_calls
    # But the issue MUST still be closed (Phase 8d.20).
    assert ("p", 1) in git.closed_issues


def test_merging_state_calls_merge_pr_when_mergeable_and_ci_passing():
    """The happy path: GitHub reports the PR mergeable + CI green,
    execute() calls merge_pr and returns CLEAN → Done.

    Phase 8d.20 extension: the originating issue is also closed after
    the merge — same call-site, gated on the same CLEAN outcome.
    """
    ctx, _repo, git = _ctx_with_pr(
        pr_number=99,
        pr_state=PRState(
            merged=False,
            mergeable=True,
            ci_passing=True,
            base_ref="main",
        ),
    )
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Done"
    assert ("p", 99) in git.merge_pr_calls
    assert git.get_pr_state(project="p", pr_number=99).merged is True
    # Phase 8d.20: the originating issue is closed after the merge.
    assert ("p", 1) in git.closed_issues


def test_merging_state_returns_blocked_when_ci_pending():
    """CI hasn't passed yet — execute() returns BLOCKED so the Poller
    tries again next tick. merge_pr MUST NOT be called: merging while
    CI is still running would defeat the whole point of the gate."""
    ctx, _repo, git = _ctx_with_pr(
        pr_number=99,
        pr_state=PRState(
            merged=False,
            mergeable=True,
            ci_passing=False,
            base_ref="main",
        ),
    )
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Merging"
    assert ("p", 99) not in git.merge_pr_calls
    # And the PR is still un-merged from the daemon's perspective.
    assert git.get_pr_state(project="p", pr_number=99).merged is False


def test_merging_state_returns_blocked_when_not_mergeable():
    """The PR isn't mergeable (conflict, blocked-by-review, etc.) —
    execute() returns BLOCKED. Granular dispatch on the underlying
    cause (rebase needed, review missing, etc.) is foreman#317; this
    task ships the minimum-shape that lets the happy path reach Done."""
    ctx, _repo, git = _ctx_with_pr(
        pr_number=99,
        pr_state=PRState(
            merged=False,
            mergeable=False,
            ci_passing=True,
            base_ref="main",
        ),
    )
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Merging"
    assert ("p", 99) not in git.merge_pr_calls


def test_merging_state_blocked_does_not_close_issue():
    """BLOCKED branch (CI pending OR not mergeable) MUST NOT close the
    issue. Closing on every poll tick would prematurely close issues
    whose impl PR isn't actually merged yet — directly user-visible.
    """
    ctx, _repo, git = _ctx_with_pr(
        pr_number=99,
        pr_state=PRState(
            merged=False,
            mergeable=False,
            ci_passing=False,
            base_ref="main",
        ),
    )
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Merging"
    assert ("p", 99) not in git.merge_pr_calls
    # Crucial: BLOCKED leaves the issue OPEN.
    assert git.closed_issues == set()


def test_merging_state_behind_impl_pr_updates_branch_and_blocks():
    """foreman#416: a BEHIND impl PR (base advanced while it waited) used
    to loop BLOCKED forever — it was never mergeable, so the merge gate
    never fired and nothing advanced the branch. Now the BehindBranchHealer
    issues update_branch and the state stays BLOCKED for the next poll.
    merge_pr MUST NOT be called; the issue stays open.
    """
    ctx, _repo, git = _ctx_with_pr(
        pr_number=99,
        pr_state=PRState(
            merged=False,
            mergeable=False,
            ci_passing=True,
            base_ref="main",
            mergeable_state="behind",
        ),
    )
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Merging"
    assert git.update_branch_calls == [("p", 99)]
    assert ("p", 99) not in git.merge_pr_calls
    assert git.closed_issues == set()


def test_merging_state_behind_then_healed_merges_and_closes_issue():
    """Once the behind impl PR heals (next poll reports mergeable + CI
    green), the normal merge path fires: merge_pr + close issue + Done."""
    ctx, _repo, git = _ctx_with_pr(
        pr_number=99,
        pr_state=PRState(
            merged=False,
            mergeable=True,
            ci_passing=True,
            base_ref="main",
            mergeable_state="clean",
        ),
    )
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Done"
    assert ("p", 99) in git.merge_pr_calls
    assert ("p", 1) in git.closed_issues
    # The healed path doesn't touch update_branch.
    assert git.update_branch_calls == []


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


def test_needs_fix_routes_merging_to_impl_fix():
    """foreman#317 C1: attempt_merge (Task 2) now emits NEEDS_FIX for a
    CI-failed or dirty impl PR. Before this branch existed, next_state()
    fell through to the defensive NeedsHelp fallback, stranding a
    genuinely fixable PR in a human queue instead of routing it back to
    the Fixer. This pins the new Merging -> ImplFix edge.
    """
    from foreman.v4.states.impl_fix import ImplFixState

    ctx, _repo, _git = _ctx_with_pr(pr_number=99)
    outcome = Outcome(kind=OutcomeKind.NEEDS_FIX, confidence=OutcomeConfidence.HIGH, summary="x")
    next_state = MergingState().next_state(ctx, outcome)
    assert isinstance(next_state, ImplFixState)


# ---------------------------------------------------------------------------
# Base-ref guard (foreman#357)
#
# Defense-in-depth: before either the already-merged short-circuit or the
# merge call, MergingState compares the impl PR's base ref against the
# project's configured dev_base_branch. Mismatch → NEEDS_HELP. The bug
# class this catches was directly observed twice in production
# (foreman#341 logic bug; foreman#347 stale-binary regression) and would
# silently move substrate state forward into the spec branch.
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


def test_merging_state_refuses_to_merge_when_pr_base_diverges_from_dev_base_branch():
    """The bug class from foreman#341 / #347: Worker emitted the impl PR
    with ``base=foreman/issue-N`` (the spec branch) instead of the
    project's ``dev_base_branch``. Without the guard, MergingState
    cheerfully merges into the spec branch and the impl never reaches
    main. With the guard, MergingState returns NEEDS_HELP and the
    operator gets a structured diagnostic.
    """
    ctx, repo, git = _ctx_with_pr(
        pr_number=99,
        pr_state=PRState(
            merged=False,
            mergeable=True,
            ci_passing=True,
            base_ref="foreman/issue-1",
        ),
        project_configs={"p": _project_config(dev_base_branch="main")},
    )
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "NeedsHelp"
    # CRITICAL invariants: no merge happened and the issue stays open.
    assert ("p", 99) not in git.merge_pr_calls
    assert ("p", 1) not in git.closed_issues
    # The outcome's details bag carries the diagnostic triplet so the
    # operator can see actual vs expected base directly.
    payload = _outcome_payload(repo, ctx.ticket.id)
    details = payload["details"]
    assert details["actual_base"] == "foreman/issue-1"
    assert details["expected_base"] == "main"
    assert details["pr_number"] == 99


def test_merging_state_merges_when_pr_base_matches_dev_base_branch():
    """Happy path regression: with the new guard in place, the
    plain-vanilla matching-base flow still reaches Done + closes the
    issue. If this fails, the guard accidentally blocks every merge.
    """
    ctx, _repo, git = _ctx_with_pr(
        pr_number=99,
        pr_state=PRState(
            merged=False,
            mergeable=True,
            ci_passing=True,
            base_ref="main",
        ),
        project_configs={"p": _project_config(dev_base_branch="main")},
    )
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Done"
    assert ("p", 99) in git.merge_pr_calls
    assert ("p", 1) in git.closed_issues


def test_merging_state_base_ref_comparison_is_case_insensitive():
    """GitHub returns refs lower-case for some legacy repos; the
    comparison normalizes both sides via casefold so the guard isn't
    accidentally case-fragile.
    """
    # Config Main, PR main → match → Done.
    ctx_a, _repo_a, git_a = _ctx_with_pr(
        pr_number=99,
        pr_state=PRState(
            merged=False,
            mergeable=True,
            ci_passing=True,
            base_ref="main",
        ),
        project_configs={"p": _project_config(dev_base_branch="Main")},
    )
    assert MergingState().transition(ctx_a).state_name == "Done"
    assert ("p", 99) in git_a.merge_pr_calls

    # Config main, PR MAIN → match → Done.
    ctx_b, _repo_b, git_b = _ctx_with_pr(
        pr_number=99,
        pr_state=PRState(
            merged=False,
            mergeable=True,
            ci_passing=True,
            base_ref="MAIN",
        ),
        project_configs={"p": _project_config(dev_base_branch="main")},
    )
    assert MergingState().transition(ctx_b).state_name == "Done"
    assert ("p", 99) in git_b.merge_pr_calls


def test_merging_state_falls_back_to_main_when_dev_base_branch_unset():
    """``ProjectConfig.dev_base_branch=None`` falls back to
    ``DEFAULT_DEV_BASE_BRANCH = "main"``. The Worker resolves None to
    origin's actual default branch by probing a clone; MergingState
    has no clone to probe, so the documented constant fallback is the
    smallest correct shape.
    """
    # Happy: PR base "main" matches the fallback → Done.
    ctx_ok, _repo_ok, git_ok = _ctx_with_pr(
        pr_number=99,
        pr_state=PRState(
            merged=False,
            mergeable=True,
            ci_passing=True,
            base_ref="main",
        ),
        project_configs={"p": _project_config(dev_base_branch=None)},
    )
    assert MergingState().transition(ctx_ok).state_name == "Done"
    assert ("p", 99) in git_ok.merge_pr_calls

    # Refusal: PR base "foreman/issue-1" diverges from the fallback →
    # NeedsHelp.
    ctx_bad, _repo_bad, git_bad = _ctx_with_pr(
        pr_number=99,
        pr_state=PRState(
            merged=False,
            mergeable=True,
            ci_passing=True,
            base_ref="foreman/issue-1",
        ),
        project_configs={"p": _project_config(dev_base_branch=None)},
    )
    assert MergingState().transition(ctx_bad).state_name == "NeedsHelp"
    assert ("p", 99) not in git_bad.merge_pr_calls


def test_merging_state_refuses_when_base_ref_empty():
    """``base_ref=""`` means "couldn't read it" — the production
    PyGithub path always populates it, so an empty value signals a
    config-shape problem or a Fake provider that wasn't seeded.
    Treat the unknown as a refusal — never silently merge.
    """
    ctx, _repo, git = _ctx_with_pr(
        pr_number=99,
        pr_state=PRState(
            merged=False,
            mergeable=True,
            ci_passing=True,
            base_ref="",
        ),
        project_configs={"p": _project_config(dev_base_branch="main")},
    )
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "NeedsHelp"
    assert ("p", 99) not in git.merge_pr_calls
    assert ("p", 1) not in git.closed_issues


def test_merging_state_skips_guard_when_project_config_missing(caplog):
    """Legacy test shape: when ``project_configs`` is empty (or doesn't
    contain ``ticket.project``), the guard short-circuits with a
    warning log line and the existing merge path proceeds unchanged.
    Keeps the change additive — older tests don't have to thread the
    map through.
    """
    ctx, _repo, git = _ctx_with_pr(
        pr_number=99,
        pr_state=PRState(
            merged=False,
            mergeable=True,
            ci_passing=True,
            base_ref="anything",
        ),
        project_configs={},
    )
    with caplog.at_level(logging.WARNING, logger="foreman.v4.states.merging"):
        next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Done"
    assert ("p", 99) in git.merge_pr_calls
    # The skip must be operator-visible.
    assert any("no project_config for project=p" in rec.message for rec in caplog.records), (
        caplog.records
    )
