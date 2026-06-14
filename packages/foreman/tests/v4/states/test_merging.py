"""MergingState — artifact-check against MergeQueue verdict."""
from __future__ import annotations

import datetime as dt

from foreman.v4.git_provider import FakeGitProvider, MergeVerdict, PRState
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext
from foreman.v4.states.merging import MergingState


def _ctx_with_pr(pr_number: int = 99) -> tuple[StateContext, InMemoryTicketRepository, FakeGitProvider]:
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.set_ticket_state(ticket.id, "Merging", now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Merging", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=pr_number,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    ctx = StateContext(
        ticket=repo.get_ticket(ticket.id), instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        git=git,
    )
    return ctx, repo, git


def test_first_entry_enqueues_into_merge_queue(monkeypatch):
    ctx, repo, git = _ctx_with_pr(pr_number=99)
    # Stub the PR-number lookup; real impl reads from the most recent
    # ExecuteCompleted outcome on the ticket. Phase-3 test substitutes:
    monkeypatch.setattr(
        MergingState, "_pr_number_for", lambda self, ctx: 99,
    )
    MergingState().transition(ctx)
    assert ("p", 99) in git.merge_queue


def test_pending_verdict_routes_back_to_merging(monkeypatch):
    ctx, repo, git = _ctx_with_pr(pr_number=99)
    monkeypatch.setattr(MergingState, "_pr_number_for", lambda self, ctx: 99)
    git.enqueue_merge_queue(project="p", pr_number=99)  # already pending
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Merging"


def test_merged_verdict_routes_to_done(monkeypatch):
    ctx, repo, git = _ctx_with_pr(pr_number=99)
    monkeypatch.setattr(MergingState, "_pr_number_for", lambda self, ctx: 99)
    git.enqueue_merge_queue(project="p", pr_number=99)
    git.set_merge_verdict(project="p", pr_number=99, verdict=MergeVerdict.MERGED)
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Done"


def test_rejected_verdict_routes_to_impl_fix(monkeypatch):
    ctx, repo, git = _ctx_with_pr(pr_number=99)
    monkeypatch.setattr(MergingState, "_pr_number_for", lambda self, ctx: 99)
    git.enqueue_merge_queue(project="p", pr_number=99)
    git.set_merge_verdict(project="p", pr_number=99, verdict=MergeVerdict.REJECTED)
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "ImplFix"


def test_missing_git_provider_routes_through_execute_failure(monkeypatch):
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Merging", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        # git omitted
    )
    monkeypatch.setattr(MergingState, "_pr_number_for", lambda self, ctx: 99)
    MergingState().transition(ctx)
    closed = repo.get_state_instance(instance.id)
    assert closed.failure_phase in ("enter", "execute")
