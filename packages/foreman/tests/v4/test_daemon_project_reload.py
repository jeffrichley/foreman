"""Tests for Daemon hot-reload of the project list (issue #477).

Covers :meth:`~foreman.v4.daemon.Daemon._apply_project_reload`:
  - adds a project's Poller and config when the loader adds a project
  - removes a project's Poller and config when the loader drops a project
  - logs a no-changes message and leaves pollers unchanged when nothing changed
"""

from __future__ import annotations

import datetime as dt
import logging

import pytest

from foreman.v4.config import ProjectConfig
from foreman.v4.daemon import Daemon, DaemonConfig
from foreman.v4.git_provider import FakeGitProvider
from foreman.v4.poller import Poller
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.role_dispatcher import FakeRoleDispatcher

_PC1 = ProjectConfig(name="alpha", repo="owner/alpha", local_clone_path="/tmp/alpha")
_PC2 = ProjectConfig(name="beta", repo="owner/beta", local_clone_path="/tmp/beta")


def _make_daemon(
    *project_configs: ProjectConfig,
    loader_projects: list[ProjectConfig] | None = None,
) -> Daemon:
    """Build a test Daemon pre-loaded with ``project_configs``.

    ``loader_projects`` becomes the return value of ``_projects_loader``
    on the next reload call; defaults to the same list passed at construction
    (no-change scenario).
    """
    repo = InMemoryTicketRepository()
    clock = lambda: dt.datetime(2026, 7, 10, 12, 0, 0)  # noqa: E731

    project_configs_list = list(project_configs)
    pollers = [
        Poller(
            repo=repo,
            qm=None,
            git=FakeGitProvider(),
            project=pc.name,
            trigger_label=pc.trigger_label,
            clock=clock,
        )
        for pc in project_configs_list
    ]
    project_cfg_map = {pc.name: pc for pc in project_configs_list}

    target = loader_projects if loader_projects is not None else project_configs_list

    daemon = Daemon(
        repo=repo,
        git=FakeGitProvider(),
        dispatcher=FakeRoleDispatcher(responses={}),
        pollers=pollers,
        config=DaemonConfig(tick_seconds=0, max_in_flight=1),
        clock=clock,
        project_configs=project_cfg_map,
        projects_loader=lambda: list(target),
        git_provider_factory=lambda _repo: FakeGitProvider(),
    )
    return daemon


def test_apply_project_reload_adds_project():
    """After reload, a project newly returned by the loader has a Poller
    in ``_pollers`` and its config in ``_project_configs``."""
    daemon = _make_daemon(_PC1, loader_projects=[_PC1, _PC2])

    assert len(daemon._pollers) == 1
    assert "beta" not in daemon._project_configs

    daemon.request_project_reload()
    daemon.tick_once()

    poller_projects = {p._project for p in daemon._pollers}
    assert "beta" in poller_projects, f"expected 'beta' in pollers, got {poller_projects}"
    assert "beta" in daemon._project_configs


def test_apply_project_reload_removes_project():
    """After reload, a project dropped by the loader is absent from
    ``_pollers`` and ``_project_configs``."""
    daemon = _make_daemon(_PC1, _PC2, loader_projects=[_PC1])

    assert len(daemon._pollers) == 2
    assert "beta" in daemon._project_configs

    daemon.request_project_reload()
    daemon.tick_once()

    poller_projects = {p._project for p in daemon._pollers}
    assert "beta" not in poller_projects, f"'beta' should be removed; pollers={poller_projects}"
    assert "beta" not in daemon._project_configs
    # alpha must still be present
    assert "alpha" in poller_projects
    assert "alpha" in daemon._project_configs


def test_apply_project_reload_no_changes_logs_info(caplog: pytest.LogCaptureFixture):
    """When the loader returns the same projects, the daemon logs the
    no-changes message and leaves the pollers list unchanged."""
    daemon = _make_daemon(_PC1, loader_projects=[_PC1])

    initial_count = len(daemon._pollers)

    with caplog.at_level(logging.INFO, logger="foreman.v4.daemon"):
        daemon.request_project_reload()
        daemon.tick_once()

    assert len(daemon._pollers) == initial_count
    assert any("no changes" in rec.message.lower() for rec in caplog.records), (
        f"Expected 'no changes' log line; got: {[r.message for r in caplog.records]}"
    )
