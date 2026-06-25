"""Unit tests for :class:`TerminalLandingObserver`.

Migrated to use FakeGitProvider (sub-request 10 of issue #410): the
observer now takes ``git: GitProvider`` instead of a ``host_for_project``
callable, so tests seed comments via ``fake_git.seed_issue_comments`` and
assert posts via ``fake_git.posted_comments``.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest.mock import MagicMock

from foreman.git_host import CommentRef
from foreman.v4.events import StateEnteredEvent
from foreman.v4.git_provider import FakeGitProvider
from foreman.v4.observers.terminal_landing import (
    _STATE_NAME_TO_ROLE,
    TerminalLandingObserver,
)
from foreman.v4.records import StateInstanceRecord, TicketRecord
from foreman.v4.subprocess_dispatcher import _ROLE_TO_INVOCATION


def _record(
    *,
    seq: int,
    state_name: str = "Implementing",
    failure_phase: str | None = None,
    failure_reason: str | None = None,
    outcome_payload: dict | None = None,
) -> StateInstanceRecord:
    base = dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.UTC)
    return StateInstanceRecord(
        id=100 + seq,
        ticket_id=1,
        state_name=state_name,
        sequence=seq,
        entered_at=base,
        execute_started_at=base,
        execute_completed_at=base,
        exited_at=base,
        outcome_kind=None,
        outcome_payload=outcome_payload,
        next_state=None,
        failure_phase=failure_phase,
        failure_reason=failure_reason,
    )


def _event(
    *,
    state_name: str = "Failed",
    ticket_id: int = 1,
    instance_id: int = 99,
) -> StateEnteredEvent:
    return StateEnteredEvent(
        at=dt.datetime(2026, 6, 20, tzinfo=dt.UTC),
        ticket_id=ticket_id,
        instance_id=instance_id,
        state_name=state_name,
        sequence=10,
    )


def _ticket(*, project: str = "proj") -> TicketRecord:
    return TicketRecord(
        id=1, project=project, issue_number=42,
        current_state="Implementing",
        created_at=dt.datetime(2026, 6, 20, tzinfo=dt.UTC),
        updated_at=dt.datetime(2026, 6, 20, tzinfo=dt.UTC),
        held_by=None, held_at=None, held_reason=None,
        depends_on=[],
    )


def _repo(history: list[StateInstanceRecord]) -> MagicMock:
    repo = MagicMock()
    repo.list_state_instances_for_ticket.return_value = history
    repo.get_ticket.return_value = _ticket()
    return repo


# ---------------------------------------------------------------------------


def test_state_name_to_role_map_matches_dispatcher_invocation_keys() -> None:
    """Regression guard: drift between state_name and dispatcher role keys
    is caught at test-collection time.
    """
    # Every value in the map must be a valid dispatcher role key.
    for state_name, role in _STATE_NAME_TO_ROLE.items():
        assert role in _ROLE_TO_INVOCATION, (
            f"_STATE_NAME_TO_ROLE[{state_name!r}] = {role!r} but {role!r} "
            f"is not a key of _ROLE_TO_INVOCATION"
        )


def test_failed_landing_posts_comment_with_failure_reason(
    tmp_path: Path,
) -> None:
    history = [
        _record(
            seq=1,
            state_name="Implementing",
            failure_phase="execute",
            failure_reason="repr(subprocess crash with message Y)",
        ),
        _record(seq=2, state_name="Failed"),  # the landing row
    ]
    repo = _repo(history)
    fake_git = FakeGitProvider()
    obs = TerminalLandingObserver(
        repo=repo,
        git=fake_git,
        log_dir=tmp_path,
    )
    obs(_event())
    assert len(fake_git.posted_comments) == 1
    posted_body = fake_git.posted_comments[0][2]
    assert "subprocess crash with message Y" in posted_body


def test_recent_role_comment_suppresses_terminal_landing_post(
    tmp_path: Path,
) -> None:
    now = dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.UTC)
    history = [
        _record(
            seq=1,
            state_name="Implementing",
            failure_phase="execute",
            failure_reason="RoleSubprocessError('role=worker exited 137 ...')",
        ),
        _record(seq=2, state_name="Failed"),
    ]
    repo = _repo(history)
    fake_git = FakeGitProvider()
    # Seed a recent role:worker marker.
    fake_git.seed_issue_comments(
        project="proj",
        issue_number=42,
        comments=[
            CommentRef(
                author_login="foreman-worker-bot",
                posted_at=now - dt.timedelta(seconds=30),
                body=(
                    "<!-- foreman:escalation:begin ticket=proj#42:"
                    "source=role:worker:key=state-instance-99 -->\nbody\n"
                    "<!-- foreman:escalation:end -->"
                ),
            ),
        ],
    )
    obs = TerminalLandingObserver(
        repo=repo,
        git=fake_git,
        log_dir=tmp_path,
        clock=lambda: now,
    )
    obs(_event())
    assert fake_git.posted_comments == []


def test_same_key_dedup_skips_terminal_landing_repost(
    tmp_path: Path,
) -> None:
    history = [
        _record(
            seq=1,
            state_name="Implementing",
            failure_phase="execute",
            failure_reason="exited 1",
        ),
        _record(seq=2, state_name="Failed"),
    ]
    repo = _repo(history)
    fake_git = FakeGitProvider()
    fake_git.seed_issue_comments(
        project="proj",
        issue_number=42,
        comments=[
            CommentRef(
                author_login="foreman-bot",
                posted_at=dt.datetime(2025, 1, 1, tzinfo=dt.UTC),
                body=(
                    "<!-- foreman:escalation:begin ticket=proj#42:"
                    "source=terminal-landing:key=ticket-1-instance-99 -->\n"
                    "body\n<!-- foreman:escalation:end -->"
                ),
            ),
        ],
    )
    obs = TerminalLandingObserver(
        repo=repo,
        git=fake_git,
        log_dir=tmp_path,
    )
    obs(_event())
    assert fake_git.posted_comments == []


def test_needshelp_landing_includes_log_path_in_extra_context(
    tmp_path: Path,
) -> None:
    # Seed a real role log file under <log_dir>/worker/.
    role_dir = tmp_path / "worker"
    role_dir.mkdir(parents=True)
    log_file = role_dir / "1__2026-06-20-12-00-00-000Z.log"
    log_file.write_text("sample stderr line\n", encoding="utf-8")
    history = [
        _record(
            seq=1,
            state_name="Implementing",
            failure_phase="execute",
            failure_reason="exited 137",
        ),
        _record(seq=2, state_name="NeedsHelp"),
    ]
    repo = _repo(history)
    fake_git = FakeGitProvider()
    obs = TerminalLandingObserver(
        repo=repo,
        git=fake_git,
        log_dir=tmp_path,
    )
    obs(_event(state_name="NeedsHelp"))
    assert len(fake_git.posted_comments) == 1
    posted_body = fake_git.posted_comments[0][2]
    assert str(log_file) in posted_body


def test_failed_landing_renders_exit_code_and_log_tail_inline(
    tmp_path: Path,
) -> None:
    role_dir = tmp_path / "worker"
    role_dir.mkdir(parents=True)
    log_file = role_dir / "1__2026-06-20-12-00-00-000Z.log"
    log_file.write_text(
        "first line\nsecond line\nthird line with a long tail\n",
        encoding="utf-8",
    )
    history = [
        _record(
            seq=1,
            state_name="Implementing",
            failure_phase="execute",
            failure_reason=(
                "RoleSubprocessError('role=worker exited 137 without "
                "emitting an outcome; see log at /tmp/x.log')"
            ),
        ),
        _record(seq=2, state_name="Failed"),
    ]
    repo = _repo(history)
    fake_git = FakeGitProvider()
    obs = TerminalLandingObserver(
        repo=repo,
        git=fake_git,
        log_dir=tmp_path,
    )
    obs(_event())
    assert len(fake_git.posted_comments) == 1
    posted_body = fake_git.posted_comments[0][2]
    assert "exit code: 137" in posted_body
    assert "third line with a long tail" in posted_body
    assert "<details>" in posted_body


def test_recent_role_comment_path_skips_inline_exit_code_block(
    tmp_path: Path,
) -> None:
    """When the in-role comment posted recently, the terminal observer
    skips entirely — no inline exit-code block surfaces via the
    terminal post.
    """
    now = dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.UTC)
    history = [
        _record(
            seq=1,
            state_name="Implementing",
            failure_phase="execute",
            failure_reason=(
                "RoleSubprocessError('role=worker exited 137 ...')"
            ),
        ),
        _record(seq=2, state_name="Failed"),
    ]
    repo = _repo(history)
    fake_git = FakeGitProvider()
    role_marker = (
        "<!-- foreman:escalation:begin ticket=proj#42:"
        "source=role:worker:key=state-instance-99 -->\n"
        "body (no exit code surfaced)\n"
        "<!-- foreman:escalation:end -->"
    )
    fake_git.seed_issue_comments(
        project="proj",
        issue_number=42,
        comments=[
            CommentRef(
                author_login="foreman-worker-bot",
                posted_at=now - dt.timedelta(seconds=30),
                body=role_marker,
            ),
        ],
    )
    obs = TerminalLandingObserver(
        repo=repo,
        git=fake_git,
        log_dir=tmp_path,
        clock=lambda: now,
    )
    obs(_event())
    assert fake_git.posted_comments == []
    # The existing role:worker body does NOT contain the inline
    # ``exit code:`` literal — that surface is scoped to the
    # terminal-landing observer only.
    assert "exit code:" not in role_marker


def test_failed_landing_retry_cap_walks_back_to_crash_row(
    tmp_path: Path,
) -> None:
    history = [
        _record(
            seq=1,
            state_name="Implementing",
            failure_phase="execute",
            failure_reason="exited 1, see log...",
        ),
        _record(
            seq=2,
            state_name="Implementing",
            failure_phase="execute",
            failure_reason="exited 1, see log...",
        ),
        _record(
            seq=3,
            state_name="Implementing",
            failure_phase="state_instance",
            failure_reason="state Implementing failed 3 consecutive times",
        ),
        _record(seq=4, state_name="NeedsHelp"),
    ]
    repo = _repo(history)
    fake_git = FakeGitProvider()
    obs = TerminalLandingObserver(
        repo=repo,
        git=fake_git,
        log_dir=tmp_path,
    )
    obs(_event(state_name="NeedsHelp"))
    assert len(fake_git.posted_comments) == 1
    posted_body = fake_git.posted_comments[0][2]
    # Walks back from [-2] (the cap-trip row) to find the EARLIEST
    # ``exited \d+`` row → exit code 1, not the "consecutive times"
    # message.
    assert "exit code: 1" in posted_body


def test_failed_landing_post_failure_does_not_raise(tmp_path: Path) -> None:
    history = [
        _record(
            seq=1, state_name="Implementing",
            failure_phase="execute", failure_reason="exited 1",
        ),
        _record(seq=2, state_name="Failed"),
    ]
    repo = _repo(history)
    # Use a MagicMock that raises on post_issue_comment to simulate failure.
    fake_git = MagicMock()
    fake_git.get_issue_comments.return_value = []
    fake_git.post_issue_comment.side_effect = RuntimeError("github 5xx")
    obs = TerminalLandingObserver(
        repo=repo,
        git=fake_git,
        log_dir=tmp_path,
    )
    # Must not raise.
    obs(_event())


def test_non_terminal_state_ignored(tmp_path: Path) -> None:
    repo = _repo([])
    fake_git = FakeGitProvider()
    obs = TerminalLandingObserver(
        repo=repo,
        git=fake_git,
        log_dir=tmp_path,
    )
    obs(_event(state_name="Planning"))
    assert fake_git.posted_comments == []
