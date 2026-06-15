"""Phase 7 e2e — TOML config in, live daemon out.

Closes Phase 7: writes a real V4Config TOML to disk, calls
``bootstrap_cli_context`` to materialize the production object graph,
runs the daemon tick loop with fakes wired in for the network seams,
and verifies that:

  - the journal advances a ticket all the way to Done,
  - StructuredLogObserver's JSON lines land in ``<log_dir>/transitions.jsonl``,
  - EventArchiveObserver's rows land in the SQLite ``events`` table.

The SubprocessRoleDispatcher is monkey-patched at the bootstrap import
site so no real subprocess fork happens; everything else is the production
wiring.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from foreman.v4.bootstrap import bootstrap_cli_context
from foreman.v4.config import load_config
from foreman.v4.git_provider import FakeGitProvider, MergeVerdict, PRState
from foreman.v4.logging_config import reset_logging
from foreman.v4.role_dispatcher import FakeRoleDispatcher


def _canned(kind: str, *, pr_number: int | None = None) -> str:
    art = f',"artifacts":{{"pr_number":{pr_number}}}' if pr_number else ""
    return (
        f'FOREMAN_OUTCOME:{{"kind":"{kind}","confidence":"high",'
        f'"summary":"x"{art}}}'
    )


def test_full_boot_from_toml_to_done(tmp_path: Path, monkeypatch):
    reset_logging()
    db_path = tmp_path / "v4.db"
    log_dir = tmp_path / "logs"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"[daemon]\n"
        f'db_path = "{db_path.as_posix()}"\n'
        f'log_dir = "{log_dir.as_posix()}"\n'
        f"tick_seconds = 0\n"
        f"max_in_flight = 1\n"
        f"[[projects]]\n"
        f'name = "p"\n'
        f'repo = "owner/p"\n'
        f'local_clone_path = "{(tmp_path / "p").as_posix()}"\n',
        encoding="utf-8",
    )
    config = load_config(config_path)

    git = FakeGitProvider()
    git.set_open_issues_with_label(
        project="p", label="foreman:plan", issue_numbers={1},
    )
    git.set_pr_state(
        project="p", pr_number=42,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    git.enqueue_merge_queue(project="p", pr_number=42)
    git.set_merge_verdict(project="p", pr_number=42, verdict=MergeVerdict.MERGED)

    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 1):       _canned("clean", pr_number=42),
        ("reviewer-spec", "p", 1): _canned("clean", pr_number=42),
        ("worker", "p", 1):        _canned("clean", pr_number=42),
        ("reviewer-impl", "p", 1): _canned("clean", pr_number=42),
    })
    # Bootstrap unconditionally constructs a SubprocessRoleDispatcher;
    # for the e2e smoke we replace that constructor with one that yields
    # our FakeRoleDispatcher. Everything else (SqliteTicketRepository,
    # Daemon, Poller, WorkerPool, QueueManager) is the real production
    # wiring — only the network seams (git + role dispatch) are faked.
    monkeypatch.setattr(
        "foreman.v4.bootstrap.SubprocessRoleDispatcher",
        lambda **_kwargs: dispatcher,
    )

    identity = MagicMock()
    identity.get_role_token.return_value = "ghp_TEST"
    try:
        ctx = bootstrap_cli_context(
            config=config, identity=identity,
            git_provider_factory=lambda repo: git,
        )
        assert ctx.repo is not None
        assert ctx.daemon is not None

        # Bootstrap owns the EventBus + 4 standard observers as of
        # Task 8.1; this test no longer needs to wire them by hand.

        # Drive the loop. tick_seconds=0 + serial pool means one tick =
        # one transition; Queued -> Planning -> SpecReview -> Implementing
        # -> ImplReview -> Merging -> Done is 6 advances + adoption + a
        # MergingState BLOCKED retry, so 30 ticks is generous.
        for _ in range(30):
            ctx.daemon.tick_once()
            ticket = ctx.repo.get_ticket_by_issue(project="p", issue_number=1)
            if ticket.current_state in ("Done", "Failed", "NeedsHelp"):
                break
        else:
            raise AssertionError(
                f"ticket did not converge after 30 ticks: "
                f"current_state={ticket.current_state}",
            )

        assert ticket.current_state == "Done"

        # JSON-lines log populated. configure_logging() inside bootstrap
        # attached a JsonLinesHandler to foreman.v4.transitions; the
        # StructuredLogObserver writes to that logger.
        jsonl = log_dir / "transitions.jsonl"
        assert jsonl.exists(), f"expected JSON-lines log at {jsonl}"
        content = jsonl.read_text(encoding="utf-8")
        state_entered_lines = [
            json.loads(line) for line in content.splitlines() if line.strip()
        ]
        assert any(
            entry.get("event") == "state_entered" for entry in state_entered_lines
        ), "no state_entered event landed in the JSON-lines log"

        # Events archive populated — proves EventArchiveObserver ran on
        # the same bus the Daemon used.
        rows = ctx.repo._conn.execute(  # type: ignore[attr-defined]
            "SELECT COUNT(*) AS n FROM events"
        ).fetchone()
        assert rows["n"] > 0, "events table empty — archive observer never fired"
    finally:
        # Logging handlers are process-global; tear them down so later
        # tests don't see this run's JsonLinesHandler still attached.
        reset_logging()
        if "ctx" in locals() and ctx.daemon is not None:
            ctx.daemon._pool.shutdown(wait=True)  # type: ignore[attr-defined]
