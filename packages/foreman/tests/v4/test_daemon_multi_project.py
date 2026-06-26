"""Daemon with multiple Pollers — one per project, shared QM + WorkerPool.

Phase 6.5 built the Daemon to hold a `list[Poller]`. This test proves
the multi-project shape works end-to-end: two Pollers (one per project)
sharing one QueueManager + one WorkerPool advance independent tickets
through the tick loop.

Phase 8d.16 update: this test now wires TWO real provider instances —
one per project — routed through a real ``RoutingGitProvider`` for the
Daemon's cross-project consumers. The previous single-shared-FakeGitProvider
shape was the source of the F1 illusion: a Daemon that ignored the
``project=`` kwarg in its writes still passed because the lone Fake's
in-memory state was keyed by ``(project, ...)`` and routed correctly
within itself. With per-project providers, dispatch errors surface
immediately — calls keyed for project[i] hitting project[0]'s provider
raise ``PRNotFoundError`` (or worse, mis-label).
"""

from __future__ import annotations

import datetime as dt

from foreman.v4.daemon import Daemon, DaemonConfig
from foreman.v4.event_bus import EventBus
from foreman.v4.git_provider import FakeGitProvider
from foreman.v4.observers.label_observability import LabelObservabilityObserver
from foreman.v4.poller import Poller
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.role_dispatcher import FakeRoleDispatcher
from foreman.v4.routing_git_provider import RoutingGitProvider


def _canned(kind: str) -> str:
    return f'FOREMAN_OUTCOME:{{"kind":"{kind}","confidence":"high","summary":"x"}}'


def test_tick_polls_every_project_and_advances_each() -> None:
    """Two Pollers (voice + foreman) each surface one issue; the Daemon
    advances both past Queued in two ticks.

    Each project has its OWN ``FakeGitProvider``; the Daemon receives a
    ``RoutingGitProvider`` that dispatches per ``project=`` kwarg. If
    the Daemon or LabelObservabilityObserver ignored the project kwarg
    and routed everything to project[0]'s provider, the foreman ticket's
    label writes would land on the voice provider — the foreman ticket's
    Queued event would never be observed on the foreman provider, and
    the labeling assertions below would fail.
    """
    repo = InMemoryTicketRepository()

    # Per-project providers — the production shape after Phase 8d.16.
    git_voice = FakeGitProvider()
    git_foreman = FakeGitProvider()
    git_voice.set_open_issues_with_label(
        project="voice", label="foreman:plan", issue_numbers={1},
    )
    git_foreman.set_open_issues_with_label(
        project="foreman", label="foreman:plan", issue_numbers={2},
    )

    # Router for cross-project consumers (Daemon, label observer).
    router = RoutingGitProvider(
        providers={"voice": git_voice, "foreman": git_foreman},
    )

    # Wire the LabelObservabilityObserver against the router — this is
    # the consumer that bootstrap exposes to F1's failure mode. Without
    # the router, the observer would hold a single per-project provider
    # and stamp every project's labels onto that one provider.
    bus = EventBus()
    bus.subscribe(LabelObservabilityObserver(writer=router, repo=repo))

    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "voice", 1): _canned("clean"),
        ("planner", "foreman", 2): _canned("clean"),
    })

    def clock() -> dt.datetime:
        return dt.datetime(2026, 6, 13, 12, 0, 0)

    daemon = Daemon(
        repo=repo,
        git=router,
        dispatcher=dispatcher,
        pollers=[
            # Pollers operate on ONE project each — they take their own
            # per-project provider directly. No routing hop needed.
            Poller(
                repo=repo, qm=None, git=git_voice, project="voice",
                trigger_label="foreman:plan", clock=clock,
            ),
            Poller(
                repo=repo, qm=None, git=git_foreman, project="foreman",
                trigger_label="foreman:plan", clock=clock,
            ),
        ],
        config=DaemonConfig(tick_seconds=0, max_in_flight=4),
        clock=clock,
        bus=bus,
    )
    # Pollers built without QM get the QM wired by the Daemon constructor.
    # First tick adopts both new issues + enqueues Queued; second tick
    # runs Planning for each (clean → SpecReview).
    daemon.tick_once()
    daemon.tick_once()

    voice = repo.get_ticket_by_issue(project="voice", issue_number=1)
    foreman_t = repo.get_ticket_by_issue(project="foreman", issue_number=2)
    assert voice is not None
    assert foreman_t is not None
    assert voice.current_state != "Queued", (
        f"voice ticket did not advance — still in {voice.current_state}"
    )
    assert foreman_t.current_state != "Queued", (
        f"foreman ticket did not advance — still in {foreman_t.current_state}"
    )

    # Phase 8d.16 routing invariant: every label write the observer
    # issued during the run MUST have landed on the per-project provider
    # named in the event, never on a sibling provider. We assert this by
    # inspecting the FakeGitProvider's internal label dict — which
    # ``add_labels`` / ``remove_labels`` mutate in place keyed by
    # ``(project, issue_number)``. Even if Queued + Planning added-then-
    # removed every label (so the final label set is empty), the dict
    # itself records WHICH (project, issue_number) keys were touched.
    #
    # If the observer had been wired to a single per-project provider
    # (the F1 bug shape), foreman's writes would have landed on voice's
    # dict with key ('foreman', 2) — visible as a foreign key in the
    # voice provider, OR (if PyGithub) would 404 on the wrong repo and
    # the foreman key would never appear at all on either provider.
    voice_touched_keys = set(git_voice._issue_labels)
    foreman_touched_keys = set(git_foreman._issue_labels)
    assert voice_touched_keys == {("voice", 1)}, (
        f"voice provider should only have been touched for ('voice', 1); "
        f"got {voice_touched_keys}. Cross-project leak = F1."
    )
    assert foreman_touched_keys == {("foreman", 2)}, (
        f"foreman provider should only have been touched for "
        f"('foreman', 2); got {foreman_touched_keys}. Cross-project leak = F1."
    )
