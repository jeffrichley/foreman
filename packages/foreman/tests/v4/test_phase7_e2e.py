"""Phase 7 e2e — TOML config in, live daemon out.

Closes Phase 7: writes a real V4Config TOML to disk, calls
``bootstrap_cli_context`` to materialize the production object graph,
runs the daemon tick loop with fakes wired in for the network seams,
and verifies that:

  - the journal advances a ticket all the way to Done,
  - StructuredLogObserver's JSON lines land in ``<log_dir>/transitions.jsonl``,
  - EventArchiveObserver's rows land in the Postgres ``events`` table.

The SubprocessRoleDispatcher is monkey-patched at the bootstrap import
site so no real subprocess fork happens; everything else is the production
wiring. Persistence is Postgres-only (v5 kill-sqlite), so the test runs
against the session-scoped Postgres testcontainer (``clean_postgres_dsn``)
and skips gracefully when Docker is unavailable.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from foreman.v4.bootstrap import bootstrap_cli_context
from foreman.v4.config import load_config
from foreman.v4.git_provider import FakeGitProvider, PRState
from foreman.v4.logging_config import reset_logging
from foreman.v4.role_dispatcher import FakeRoleDispatcher

from .postgres_fixture import (  # noqa: F401  (fixture imports)
    clean_postgres_dsn,
    postgres_dsn,
)


def _canned(kind: str, *, pr_number: int | None = None) -> str:
    art = f',"artifacts":{{"pr_number":{pr_number}}}' if pr_number else ""
    return f'FOREMAN_OUTCOME:{{"kind":"{kind}","confidence":"high","summary":"x"{art}}}'


def test_full_boot_from_toml_to_done(tmp_path: Path, monkeypatch, clean_postgres_dsn):  # noqa: F811
    reset_logging()
    log_dir = tmp_path / "logs"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"[daemon]\n"
        f'log_dir = "{log_dir.as_posix()}"\n'
        f"tick_seconds = 0\n"
        f"max_in_flight = 1\n"
        f"[storage]\n"
        f'engine = "postgres"\n'
        f'dsn = "{clean_postgres_dsn}"\n'
        # Task 8.3: [apps.*] is now required. Real PEM read happens in
        # main() at IdentityRegistry construction; the loader only
        # validates the string shape, so fake on-disk paths are fine
        # for this e2e (identity is a MagicMock below).
        f"[apps.planner]\n"
        f"app_id = 12345\n"
        f'private_key_path = "/tmp/fake-planner.pem"\n'
        f"[apps.reviewer]\n"
        f"app_id = 12346\n"
        f'private_key_path = "/tmp/fake-reviewer.pem"\n'
        f"[apps.fixer]\n"
        f"app_id = 12347\n"
        f'private_key_path = "/tmp/fake-fixer.pem"\n'
        f"[apps.worker]\n"
        f"app_id = 12348\n"
        f'private_key_path = "/tmp/fake-worker.pem"\n'
        # Task 8.4: [orchestrator] is now required too — App
        # installation creds, not env-var PAT. Same fake-on-disk-path
        # treatment as the per-role apps; identity is a MagicMock below
        # so no PEM read ever fires.
        f"[orchestrator]\n"
        f"app_id = 99999\n"
        f'private_key_path = "/tmp/fake-orchestrator.pem"\n'
        f"[operator.supervisor]\n"
        f'name = "Test Sup"\n'
        f'email = "sup@example.com"\n'
        f"[operator.signer]\n"
        f'name = "Test Sign"\n'
        f'email = "sign@example.com"\n'
        f"[[projects]]\n"
        f'name = "p"\n'
        f'repo = "owner/p"\n'
        f'local_clone_path = "{(tmp_path / "p").as_posix()}"\n'
        # foreman#418: opt in to auto-merge so the impl PR reaches Done
        # via MergingState (default parks at ImplApproved for human merge).
        f"auto_merge_impl = true\n",
        encoding="utf-8",
    )
    config = load_config(config_path)

    git = FakeGitProvider()
    git.set_open_issues_with_label(
        project="p",
        label="foreman:plan",
        issue_numbers={1},
    )
    git.set_pr_state(
        project="p",
        pr_number=42,
        # foreman#357: ``base_ref="main"`` matches the project's default
        # ``dev_base_branch`` (None → DEFAULT_DEV_BASE_BRANCH ("main")
        # per merging.py), so MergingState's base-ref guard passes
        # through and the ticket reaches Done as before.
        state=PRState(
            merged=False,
            mergeable=True,
            ci_passing=True,
            base_ref="main",
        ),
    )

    dispatcher = FakeRoleDispatcher(
        responses={
            ("planner", "p", 1): _canned("clean", pr_number=42),
            ("reviewer-spec", "p", 1): _canned("clean", pr_number=42),
            ("worker", "p", 1): _canned("clean", pr_number=42),
            ("reviewer-impl", "p", 1): _canned("clean", pr_number=42),
        }
    )
    # Bootstrap unconditionally constructs a SubprocessRoleDispatcher;
    # for the e2e smoke we replace that constructor with one that yields
    # our FakeRoleDispatcher. Everything else (the repository,
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
            config=config,
            identity=identity,
            git_provider_factory=lambda repo: git,
        )
        assert ctx.repo is not None
        assert ctx.daemon is not None

        # Bootstrap owns the EventBus + 4 standard observers as of
        # Task 8.1; this test no longer needs to wire them by hand.

        # Drive the loop. tick_seconds=0 + serial pool means one tick =
        # one transition; Queued -> Planning -> SpecReview -> Implementing
        # -> ImplReview -> Merging -> Done is 6 advances + adoption.
        # Phase 8d.19 dropped the MergeQueue polling so MergingState
        # advances in one pass when the PR is mergeable + CI green —
        # 30 ticks is still generous.
        for _ in range(30):
            ctx.daemon.tick_once()
            ticket = ctx.repo.get_ticket_by_issue(project="p", issue_number=1)
            if ticket.current_state in ("Done", "Failed", "NeedsHelp"):
                break
        else:
            raise AssertionError(
                f"ticket did not converge after 30 ticks: current_state={ticket.current_state}",
            )

        assert ticket.current_state == "Done"

        # JSON-lines log populated. configure_logging() inside bootstrap
        # attached a JsonLinesHandler to foreman.v4.transitions; the
        # StructuredLogObserver writes to that logger.
        jsonl = log_dir / "transitions.jsonl"
        assert jsonl.exists(), f"expected JSON-lines log at {jsonl}"
        content = jsonl.read_text(encoding="utf-8")
        state_entered_lines = [json.loads(line) for line in content.splitlines() if line.strip()]
        assert any(entry.get("event") == "state_entered" for entry in state_entered_lines), (
            "no state_entered event landed in the JSON-lines log"
        )

        # Events archive populated — proves EventArchiveObserver ran on
        # the same bus the Daemon used. Query through the Postgres pool.
        with ctx.repo._pool.connection() as conn:  # type: ignore[attr-defined]
            row = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        assert row["n"] > 0, "events table empty — archive observer never fired"
    finally:
        # Logging handlers are process-global; tear them down so later
        # tests don't see this run's JsonLinesHandler still attached.
        reset_logging()
        if "ctx" in locals() and ctx.daemon is not None:
            ctx.daemon.shutdown(wait=True)
