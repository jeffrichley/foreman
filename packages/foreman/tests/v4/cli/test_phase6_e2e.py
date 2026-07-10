"""Phase 6 e2e — operator commands work against a live repo+QM."""

from __future__ import annotations

import datetime as dt

from typer.testing import CliRunner

from foreman.v4.cli import app
from foreman.v4.cli.context import build_cli_context
from foreman.v4.queue_manager import QueueManager
from foreman.v4.repository import InMemoryTicketRepository


def test_hold_ps_resume_retry_queue_workflow():
    repo = InMemoryTicketRepository()
    t = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.set_ticket_state(t.id, "Planning", now=dt.datetime(2026, 6, 13))
    qm = QueueManager(repo=repo, max_in_flight=4)
    runner = CliRunner()
    ctx = build_cli_context(repo=repo, qm=qm)

    # 1. hold
    r1 = runner.invoke(app, ["hold", str(t.id), "--reason", "test"], obj=ctx)
    assert r1.exit_code == 0

    # 2. ps — held column populated
    r2 = runner.invoke(app, ["ps"], obj=ctx)
    assert "yes" in r2.output  # the "held" column shows "yes"

    # 3. resume
    r3 = runner.invoke(app, ["resume", str(t.id)], obj=ctx)
    assert r3.exit_code == 0
    assert not repo.get_ticket(t.id).is_held

    # 4. retry → enqueues
    r4 = runner.invoke(app, ["retry", str(t.id)], obj=ctx)
    assert r4.exit_code == 0
    assert qm.queue_depth() == 1

    # 5. queue reports the depth
    r5 = runner.invoke(app, ["queue"], obj=ctx)
    assert "1" in r5.output
