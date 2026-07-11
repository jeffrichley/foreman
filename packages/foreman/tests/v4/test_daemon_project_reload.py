"""Tests for Daemon hot-reload of the project list (issue #477).

Covers :meth:`~foreman.v4.daemon.Daemon._apply_project_reload`:
  - adds a project's Poller and config when the loader adds a project
  - removes a project's Poller and config when the loader drops a project
  - logs a no-changes message and leaves pollers unchanged when nothing changed
  - logs-and-keeps-current on FileNotFoundError during reload (FIX 3)
  - logs-and-keeps-current on ValidationError during reload (FIX 3)
  - exercises the RoutingGitProvider register/unregister path (FIX 5)
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
from foreman.v4.routing_git_provider import RoutingGitProvider

_PC1 = ProjectConfig(name="alpha", repo="owner/alpha", local_clone_path="/tmp/alpha")
_PC2 = ProjectConfig(name="beta", repo="owner/beta", local_clone_path="/tmp/beta")


def _make_daemon(
    *project_configs: ProjectConfig,
    loader_projects: list[ProjectConfig] | None = None,
    loader_fn: object = None,
    git: FakeGitProvider | RoutingGitProvider | None = None,
) -> Daemon:
    """Build a test Daemon pre-loaded with ``project_configs``.

    ``loader_projects`` becomes the return value of ``_projects_loader``
    on the next reload call; defaults to the same list passed at construction
    (no-change scenario).

    ``loader_fn`` overrides ``loader_projects`` when a custom callable is
    needed (e.g., to raise on the first call).

    ``git`` lets callers supply a ``RoutingGitProvider`` to exercise the
    register/unregister path; defaults to a plain ``FakeGitProvider``.
    """
    from collections.abc import Callable

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

    if loader_fn is not None:
        actual_loader: Callable[[], list[ProjectConfig]] = loader_fn  # type: ignore[assignment]
    else:
        target = loader_projects if loader_projects is not None else project_configs_list
        actual_loader = lambda: list(target)  # noqa: E731

    daemon = Daemon(
        repo=repo,
        git=git or FakeGitProvider(),
        dispatcher=FakeRoleDispatcher(responses={}),
        pollers=pollers,
        config=DaemonConfig(tick_seconds=0, max_in_flight=1),
        clock=clock,
        project_configs=project_cfg_map,
        projects_loader=actual_loader,
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


# ---------------------------------------------------------------------------
# FIX 3: graceful degradation on reload failure (issue #503)
# ---------------------------------------------------------------------------


def test_apply_project_reload_file_not_found_keeps_current(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``FileNotFoundError`` during reload logs a warning and leaves the
    current project set unchanged — the daemon must NOT crash or drop
    any project."""
    daemon = _make_daemon(_PC1, loader_fn=lambda: (_ for _ in ()).throw(FileNotFoundError("gone")))

    initial_pollers = list(daemon._pollers)
    initial_configs = dict(daemon._project_configs)

    with caplog.at_level(logging.WARNING, logger="foreman.v4.daemon"):
        daemon.request_project_reload()
        daemon.tick_once()

    # Project set is unchanged.
    assert daemon._project_configs == initial_configs, (
        "project_configs changed despite reload failure"
    )
    assert {p._project for p in daemon._pollers} == {p._project for p in initial_pollers}, (
        "pollers changed despite reload failure"
    )
    # A warning was logged.
    assert any(
        "filenotfounderror" in rec.message.lower() or "failed to load" in rec.message.lower()
        for rec in caplog.records
    ), f"Expected warning log; got: {[r.message for r in caplog.records]}"


def test_apply_project_reload_validation_error_keeps_current(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``pydantic.ValidationError`` during reload logs a warning and
    leaves the current project set unchanged."""
    from pydantic import ValidationError as _VE

    # Fabricate a real ValidationError by trying to validate bad data.
    try:
        ProjectConfig.model_validate({"name": "x"})  # missing repo + local_clone_path
    except _VE as exc:
        val_err = exc
    else:
        pytest.fail("Expected ValidationError from bad ProjectConfig")  # pragma: no cover

    def _bad_loader() -> list[ProjectConfig]:
        raise val_err

    daemon = _make_daemon(_PC1, loader_fn=_bad_loader)

    initial_configs = dict(daemon._project_configs)

    with caplog.at_level(logging.WARNING, logger="foreman.v4.daemon"):
        daemon.request_project_reload()
        daemon.tick_once()

    assert daemon._project_configs == initial_configs, (
        "project_configs changed despite ValidationError during reload"
    )
    assert any(
        "validationerror" in rec.message.lower() or "failed to load" in rec.message.lower()
        for rec in caplog.records
    ), f"Expected warning log; got: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# FIX 5: RoutingGitProvider register/unregister coverage (issue #503)
# ---------------------------------------------------------------------------


def test_apply_project_reload_routing_git_registers_added_project() -> None:
    """When a new project is added via reload AND the Daemon's git is a
    ``RoutingGitProvider``, the new project gets a provider registered."""
    routing_git = RoutingGitProvider(providers={"alpha": FakeGitProvider()})
    daemon = _make_daemon(_PC1, loader_projects=[_PC1, _PC2], git=routing_git)

    # beta is not yet registered.
    assert "beta" not in routing_git._providers

    daemon.request_project_reload()
    daemon.tick_once()

    # After reload, beta's provider is registered.
    assert "beta" in routing_git._providers, (
        f"expected 'beta' in routing providers after reload; got {list(routing_git._providers)}"
    )


def test_apply_project_reload_routing_git_unregisters_removed_project() -> None:
    """When a project is removed via reload AND the Daemon's git is a
    ``RoutingGitProvider``, the dropped project's provider is unregistered."""
    routing_git = RoutingGitProvider(
        providers={"alpha": FakeGitProvider(), "beta": FakeGitProvider()}
    )
    daemon = _make_daemon(_PC1, _PC2, loader_projects=[_PC1], git=routing_git)

    assert "beta" in routing_git._providers

    daemon.request_project_reload()
    daemon.tick_once()

    assert "beta" not in routing_git._providers, (
        f"expected 'beta' removed from routing providers after reload; "
        f"got {list(routing_git._providers)}"
    )
