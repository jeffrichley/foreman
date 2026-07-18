"""End-to-end MergeCoordinator integration tests (foreman#550 Task 7).

The unit-level routing/bound/eviction/reconcile behaviors already covered in
``test_merge_coordinator.py`` are exercised there one entry at a time. This
file proves the whole per-repo ``merge_queue`` holds together across a
*multi-entry* queue on one project -- the shape the coordinator exists to
solve:

* serial draining -- two immediately-mergeable entries on the same repo
  never both merge in the same ``tick()`` (no batching, no race);
* head-blocking -- a legitimately stuck head entry keeps the second entry
  out of head-of-queue no matter how many ticks pass, until the first
  actually resolves and dequeues;
* the foreman#546 poison-PR bound unblocks the queue -- a head entry that
  never resolves escalates to NeedsHelp and dequeues after
  ``MAX_ATTEMPTS`` cycles, letting the second entry become head and merge.

Mirrors ``test_merge_coordinator.py``'s harness exactly: same
``InMemoryTicketRepository`` + ``FakeGitProvider`` construction, the same
``_seed_ticket_in_merge_queue`` shape (seed a ticket into ``MergeQueued`` +
one ``merge_queue`` row + a seeded ``PRState``/check state), and the same
``MergeCoordinator.tick()`` driving -- no new fakes invented.
"""

from __future__ import annotations

import datetime as dt

from foreman.v4.git_provider import FakeGitProvider, PRState, RequiredCheckState
from foreman.v4.merge_coordinator import MergeCoordinator
from foreman.v4.records import TicketRecord
from foreman.v4.repository import InMemoryTicketRepository

_NOW = dt.datetime(2026, 7, 17, tzinfo=dt.UTC)


def _clock() -> dt.datetime:
    return _NOW


def _seed_ticket_in_merge_queue(
    repo: InMemoryTicketRepository,
    git: FakeGitProvider,
    *,
    project: str = "p",
    issue_number: int,
    pr: int,
    kind: str,
    mergeable: bool = False,
    ci_passing: bool = False,
    mergeable_state: str = "blocked",
    ci: RequiredCheckState | None = None,
) -> TicketRecord:
    """Create a ticket parked in MergeQueued with one merge_queue entry for it.

    Mirrors ``test_merge_coordinator.py``'s helper of the same name exactly.
    """
    ticket = repo.create_ticket(project=project, issue_number=issue_number, now=_NOW)
    repo.set_ticket_state(ticket.id, "MergeQueued", now=_NOW)
    repo.enqueue_merge(project=project, ticket_id=ticket.id, pr_number=pr, kind=kind, now=_NOW)
    git.set_pr_state(
        project=project,
        pr_number=pr,
        state=PRState(
            merged=False,
            mergeable=mergeable,
            ci_passing=ci_passing,
            base_ref="main",
            mergeable_state=mergeable_state,
        ),
    )
    if ci is not None:
        git.seed_check_state(project, pr, ci)
    return ticket


def _fake() -> tuple[InMemoryTicketRepository, FakeGitProvider]:
    return InMemoryTicketRepository(), FakeGitProvider()


def _coordinator(
    repo: InMemoryTicketRepository, git: FakeGitProvider, *, project: str = "p"
) -> MergeCoordinator:
    return MergeCoordinator(repo=repo, git=git, projects=lambda: [project], clock=_clock)


def test_two_impl_prs_same_repo_merge_serially():
    """Two clean impl PRs queued on the same repo merge one tick at a time.

    Both entries are immediately mergeable (mergeable=True, ci_passing=True)
    -- the strongest race test, since nothing but the coordinator's own FIFO
    discipline stops both from merging in one pass. First tick(): only the
    head merges (ticket -> Done, entry dequeued) and the second entry is
    untouched. Second tick(): the second (now head) entry merges.
    """
    repo, git = _fake()
    first = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="impl", mergeable=True, ci_passing=True
    )
    second = _seed_ticket_in_merge_queue(
        repo, git, issue_number=2, pr=11, kind="impl", mergeable=True, ci_passing=True
    )
    coordinator = _coordinator(repo, git)

    coordinator.tick()

    # Only the head (first-enqueued) entry merged this tick -- the second
    # entry's trivially-mergeable PR was never touched.
    assert repo.get_ticket(first.id).current_state == "Done"
    assert repo.get_ticket(second.id).current_state == "MergeQueued"
    assert ("p", 10) in git.merge_pr_calls
    assert ("p", 11) not in git.merge_pr_calls
    entry = repo.head_merge_entry("p")
    assert entry is not None
    assert entry.ticket_id == second.id

    coordinator.tick()

    # The second tick drains the now-head second entry.
    assert repo.get_ticket(second.id).current_state == "Done"
    assert ("p", 11) in git.merge_pr_calls
    assert repo.head_merge_entry("p") is None


def test_second_starts_only_after_first_dequeues():
    """A legitimately-stuck head entry never lets the second become head.

    The head PR polls BLOCKED (CI still PENDING) tick after tick -- a plain
    wait, not a heal action, so it never trips the attempts bound either
    (see ``test_plain_ci_pending_polls_do_not_count_toward_bound``). No
    matter how many ticks pass, the second (trivially mergeable) entry must
    stay put. Only once the first is resolved and actually dequeues does
    the second become head and get processed on the following tick.
    """
    repo, git = _fake()
    first = _seed_ticket_in_merge_queue(
        repo,
        git,
        issue_number=1,
        pr=10,
        kind="impl",
        mergeable_state="blocked",
        ci=RequiredCheckState.PENDING,
    )
    second = _seed_ticket_in_merge_queue(
        repo, git, issue_number=2, pr=11, kind="impl", mergeable=True, ci_passing=True
    )
    coordinator = _coordinator(repo, git)

    for _ in range(5):
        coordinator.tick()
        assert repo.get_ticket(first.id).current_state == "MergeQueued"
        assert repo.get_ticket(second.id).current_state == "MergeQueued"
        assert ("p", 11) not in git.merge_pr_calls
        entry = repo.head_merge_entry("p")
        assert entry is not None
        assert entry.ticket_id == first.id

    # Resolve the first entry -- CI goes green, the PR becomes mergeable.
    git.set_pr_state(
        project="p",
        pr_number=10,
        state=PRState(
            merged=False,
            mergeable=True,
            ci_passing=True,
            base_ref="main",
            mergeable_state="clean",
        ),
    )
    coordinator.tick()

    assert repo.get_ticket(first.id).current_state == "Done"
    head_after_first_dequeues = repo.head_merge_entry("p")
    assert head_after_first_dequeues is not None
    assert head_after_first_dequeues.ticket_id == second.id
    # It became head, but this tick already ran -- the merge itself only
    # happens on the NEXT tick, proving "starts only after dequeue" rather
    # than the same tick that dequeued the first.
    assert repo.get_ticket(second.id).current_state == "MergeQueued"
    assert ("p", 11) not in git.merge_pr_calls

    coordinator.tick()

    assert repo.get_ticket(second.id).current_state == "Done"
    assert ("p", 11) in git.merge_pr_calls
    assert repo.head_merge_entry("p") is None


def test_poison_pr_unblocks_queue_after_bound():
    """A poison head entry escalates and dequeues, unblocking the queue.

    The head PR is perpetually "behind" (the plain FakeGitProvider never
    auto-clears it) -- ``MergeCoordinator.MAX_ATTEMPTS`` real heal cycles
    without resolving escalates it to NeedsHelp and dequeues it. The second,
    trivially-mergeable entry must never move while the poison entry is
    still head; once the bound trips, the second becomes head on the very
    next tick and merges normally -- proving the bound exists specifically
    so one broken PR can't starve the rest of the repo's queue forever.
    """
    repo, git = _fake()
    poison = _seed_ticket_in_merge_queue(
        repo, git, issue_number=1, pr=10, kind="impl", mergeable_state="behind"
    )
    second = _seed_ticket_in_merge_queue(
        repo, git, issue_number=2, pr=11, kind="impl", mergeable=True, ci_passing=True
    )
    coordinator = _coordinator(repo, git)

    for _ in range(MergeCoordinator.MAX_ATTEMPTS):
        coordinator.tick()
        assert repo.get_ticket(second.id).current_state == "MergeQueued"
        assert ("p", 11) not in git.merge_pr_calls

    assert repo.get_ticket(poison.id).current_state == "NeedsHelp"
    unblocked_head = repo.head_merge_entry("p")
    assert unblocked_head is not None
    assert unblocked_head.ticket_id == second.id
    assert git.update_branch_calls == [("p", 10), ("p", 10), ("p", 10)]

    coordinator.tick()

    assert repo.get_ticket(second.id).current_state == "Done"
    assert ("p", 11) in git.merge_pr_calls
    assert repo.head_merge_entry("p") is None
