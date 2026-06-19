"""CliContext — single source of construction for per-invocation deps."""
from __future__ import annotations

import pytest

from foreman.v4.cli.context import CliContext, build_cli_context
from foreman.v4.queue_manager import QueueManager
from foreman.v4.sqlite_repository import SqliteTicketRepository


def test_build_returns_frozen_dataclass():
    repo = SqliteTicketRepository.in_memory()
    ctx = build_cli_context(repo=repo)
    assert isinstance(ctx, CliContext)
    with pytest.raises(AttributeError):
        ctx.repo = None  # frozen


def test_repo_is_required():
    with pytest.raises(TypeError):
        build_cli_context()  # missing repo


def test_optional_fields_default_to_none():
    ctx = build_cli_context(repo=SqliteTicketRepository.in_memory())
    assert ctx.qm is None
    assert ctx.daemon is None
    assert ctx.git is None
    assert ctx.dispatcher is None


def test_all_fields_passed_through():
    repo = SqliteTicketRepository.in_memory()
    qm = QueueManager(repo=repo, max_in_flight=2)
    ctx = build_cli_context(repo=repo, qm=qm)
    assert ctx.repo is repo
    assert ctx.qm is qm
