"""Phase 4 completion — 3 concurrent tickets including one dep-blocked."""
from __future__ import annotations

import datetime as dt
import time

from foreman.v4.git_provider import FakeGitProvider, MergeVerdict, PRState
from foreman.v4.poller import Poller
from foreman.v4.queue_manager import QueueManager
from foreman.v4.role_dispatcher import FakeRoleDispatcher
from foreman.v4.sqlite_repository import SqliteTicketRepository
from foreman.v4.worker_pool import WorkerPool


def _canned(kind: str, *, pr_number: int | None = None) -> str:
    artifacts = f',"artifacts":{{"pr_number":{pr_number}}}' if pr_number else ""
    return (
        f'FOREMAN_OUTCOME:{{"kind":"{kind}","confidence":"high",'
        f'"summary":"x"{artifacts}}}'
    )


def _wait_idle(qm: QueueManager, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while qm.in_flight_count() > 0 and time.time() < deadline:
        time.sleep(0.01)
    if qm.in_flight_count() > 0:
        raise AssertionError("worker pool did not drain in time")


def test_three_concurrent_tickets_with_one_dep_blocked():
    repo = SqliteTicketRepository.in_memory()
    git = FakeGitProvider()
    # 3 fresh labeled issues
    git.set_open_issues_with_label(
        project="p", label="foreman:plan", issue_numbers={1, 2, 3},
    )
    # Each ticket gets its own PR + MergeQueue verdict MERGED
    for pr in (101, 102, 103):
        git.set_pr_state(
            project="p", pr_number=pr,
            state=PRState(merged=False, mergeable=True, ci_passing=True),
        )
        git.enqueue_merge_queue(project="p", pr_number=pr)
        git.set_merge_verdict(project="p", pr_number=pr, verdict=MergeVerdict.MERGED)

    # FakeRoleDispatcher: every role+issue tuple returns CLEAN. The PR number
    # per issue is the canonical "100 + issue" so each ticket carries its own.
    dispatcher_responses = {}
    for issue, pr in ((1, 101), (2, 102), (3, 103)):
        for role in ("planner", "reviewer-spec", "worker", "reviewer-impl"):
            dispatcher_responses[(role, "p", issue)] = _canned("clean", pr_number=pr)
    dispatcher = FakeRoleDispatcher(responses=dispatcher_responses)

    # max_in_flight=4 sizes both the QM cap and the WorkerPool thread pool.
    qm = QueueManager(repo=repo, max_in_flight=4)
    poller = Poller(
        repo=repo, qm=qm, git=git,
        project="p", trigger_label="foreman:plan",
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    pool = WorkerPool(
        repo=repo, qm=qm, dispatcher=dispatcher, git=git, bus=None,
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    try:
        # First tick adopts the 3 tickets. Set ticket 3's dep AFTER adoption.
        poller.tick()
        pool.tick()
        _wait_idle(qm)
        t1 = repo.get_ticket_by_issue(project="p", issue_number=1)
        t3 = repo.get_ticket_by_issue(project="p", issue_number=3)
        repo.set_ticket_dependencies(t3.id, deps=[t1.id])

        # Loop the runtime triad until all three tickets terminal.
        for _ in range(50):
            poller.tick()
            pool.tick()
            _wait_idle(qm)
            tickets = [
                repo.get_ticket_by_issue(project="p", issue_number=i)
                for i in (1, 2, 3)
            ]
            if all(t.current_state in ("Done", "Failed", "NeedsHelp") for t in tickets):
                break
        else:
            raise AssertionError("not all tickets converged")

        # All three reached Done
        for issue in (1, 2, 3):
            t = repo.get_ticket_by_issue(project="p", issue_number=issue)
            assert t.current_state == "Done", f"ticket {issue} = {t.current_state}"
        # Spec PRs all merged
        for pr in (101, 102, 103):
            assert git.get_pr_state(project="p", pr_number=pr).merged is True

        # Ticket 3 walked through >= 6 state transitions (Queued -> Planning ->
        # SpecReview -> Implementing -> ImplReview -> Merging -> Done) — proving
        # the dep filter didn't short-circuit it past the pipeline.
        ticket3 = repo.get_ticket_by_issue(project="p", issue_number=3)
        assert repo.count_state_instances_for_ticket(ticket3.id) >= 6
    finally:
        pool.shutdown(wait=True)
