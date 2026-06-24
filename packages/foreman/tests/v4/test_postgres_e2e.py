"""v4 ticket-lifecycle e2e parity on Postgres + the #403 resurrection guard.

Task 8 already proved the repository CONTRACT is identical across SQLite and
Postgres. This module proves the lifecycle WIRING — the state machine,
EventBus observers, and fake providers driving a ticket Queued -> ... -> Done
— behaves identically when the repo is a real pooled Postgres connection
instead of an in-process SQLite handle.

The happy-path scenario mirrors ``test_lifecycle.test_happy_path_queued_to_done``
exactly: same canned outcomes, same fakes, same driver, same terminal state and
state-instance journal. The only difference is the repository constructor.

It also carries an explicit guard for the #403 bug class (drop/terminal
resurrection): after a ticket is moved to a terminal state on one pooled
connection, a fresh read on a DIFFERENT pooled connection must see the
committed terminal state. SQLite's in-process cache broke that read-after-write;
Postgres must not.
"""

from __future__ import annotations

import datetime as dt

from foreman.v4.event_bus import EventBus
from foreman.v4.git_provider import FakeGitProvider, PRState
from foreman.v4.observers.event_archive import EventArchiveObserver
from foreman.v4.observers.structured_log import StructuredLogObserver
from foreman.v4.postgres_repository import PostgresTicketRepository
from foreman.v4.role_dispatcher import FakeRoleDispatcher

from .postgres_fixture import (  # noqa: F401  (fixture imports)
    clean_postgres_dsn,
    postgres_dsn,
)
from .test_lifecycle import _auto_merge_configs, _canned, _run_until_terminal


def test_happy_path_queued_to_done_on_postgres(clean_postgres_dsn):  # noqa: F811
    """The Phase 3 happy path, identical to the SQLite e2e, against Postgres."""
    repo = PostgresTicketRepository.from_dsn(clean_postgres_dsn, pool_min=1, pool_max=4)
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    git = FakeGitProvider()
    git.set_pr_state(
        project="p",
        pr_number=42,
        state=PRState(merged=False, mergeable=True, ci_passing=True, base_ref="main"),
    )
    dispatcher = FakeRoleDispatcher(
        responses={
            ("planner", "p", 1): _canned("clean", pr_number=42),
            ("reviewer-spec", "p", 1): _canned("clean", pr_number=42),
            ("worker", "p", 1): _canned("clean", pr_number=42),
            ("reviewer-impl", "p", 1): _canned("clean", pr_number=42),
        }
    )
    bus = EventBus()
    bus.subscribe(EventArchiveObserver(repo=repo))
    bus.subscribe(StructuredLogObserver())

    final = _run_until_terminal(
        repo, ticket.id, dispatcher=dispatcher, git=git, bus=bus,
        project_configs=_auto_merge_configs(),
    )
    assert final.current_state == "Done"

    # PR #42 ended merged — both the spec PR and impl PR merge through the
    # same merge_pr call (Phase 8d.19 collapsed those paths).
    assert git.get_pr_state(project="p", pr_number=42).merged is True
    assert ("p", 42) in git.merge_pr_calls

    # Journal records every state transition in order. The Postgres repo has
    # no ``_conn``; read the journal through the public API instead.
    instances = repo.list_state_instances_for_ticket(ticket.id)
    state_order = [inst.state_name for inst in instances]
    assert state_order == [
        "Queued",
        "Planning",
        "SpecReview",
        # foreman#416: spec-PR merge moved into the dedicated SpecMerging
        # state (out of SpecReviewState.verify) for the self-heal framework.
        "SpecMerging",
        "Implementing",
        "ImplReview",
        "Merging",
        "Done",
    ]

    # Events archived for each transition (terminal Done has no separate
    # StateEntered archive in the SQLite e2e either — same >= shape).
    archived_states = {ev["state_name"] for ev in repo.list_events_for_ticket(ticket.id)}
    assert archived_states >= {
        "Queued",
        "Planning",
        "SpecReview",
        "SpecMerging",
        "Implementing",
        "ImplReview",
        "Merging",
    }


def test_drop_then_poll_does_not_resurrect(clean_postgres_dsn):  # noqa: F811
    """#403 guard: cross-connection read-after-write on a terminal state.

    set_ticket_state(Failed) commits on one pooled connection;
    list_open_tickets and get_ticket each acquire a (possibly different)
    pooled connection. Both must observe the committed Failed state — the
    bug class #403 was an in-process cache returning the pre-terminal row,
    resurrecting a dropped/failed ticket into the open set.
    """
    repo = PostgresTicketRepository.from_dsn(clean_postgres_dsn, pool_min=1, pool_max=4)
    now = dt.datetime(2026, 6, 21, tzinfo=dt.UTC)
    t = repo.create_ticket(project="p", issue_number=1, now=now)
    repo.set_ticket_state(t.id, "Failed", now=now)
    # A fresh read on a DIFFERENT pooled connection sees the committed
    # Failed state — the cross-connection read-after-write that SQLite's
    # in-process cache broke (#403). list_open_tickets must NOT return it.
    assert all(ot.id != t.id for ot in repo.list_open_tickets())
    assert repo.get_ticket(t.id).current_state == "Failed"
