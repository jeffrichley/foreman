"""FOREMAN_PROJECTS_PATH fallback coverage for role CLI v4 entry-points.

Issue #477 moved ``[[projects]]`` out of ``config.toml`` into a separate
host-mounted projects file (``FOREMAN_PROJECTS_PATH``). The four role CLI
v4 entry-points — ``_run_planner_for_v4``, ``_run_reviewer_for_v4``,
``_run_fixer_for_v4``, ``_run_worker_for_v4`` — resolve the project by
checking ``cfg.projects`` first and only falling back to loading the
projects file when ``cfg.projects`` is empty (the production post-#477
shape).

The existing role-core tests all mock ``load_v4_config`` to return a cfg
whose ``.projects`` is already populated, so they exercise the
``cfg.projects`` branch. These tests deliberately drive the OTHER branch:
``cfg.projects == []`` so the code loads from ``FOREMAN_PROJECTS_PATH`` and
patches the resolved list back into ``cfg`` via ``model_copy`` before the
downstream ``_run_<role>_core`` runs.

Each role is covered twice:
  - the happy fallback: the project is present in the loaded file, the
    resolution succeeds, and the code reaches the ``V4IdentityRegistry``
    construction with the resolved project's repo (we intercept there with
    a sentinel so no real network / provider spin-up is needed).
  - the not-found fallback: the project is absent from the loaded file, so
    the fallback branch raises the clean ``project ... not found`` error.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from foreman.roles.fixer import _run_fixer_for_v4
from foreman.roles.planner import _run_planner_for_v4
from foreman.roles.reviewer import _run_reviewer_for_v4
from foreman.roles.worker import _run_worker_for_v4
from foreman.v4.config import (
    AppCredentials,
    AppsConfig,
    OperatorConfig,
    OperatorIdentity,
    OrchestratorConfig,
    ProjectConfig,
    StorageConfig,
    V4Config,
)


class _RegistrySentinel(Exception):
    """Raised by the patched ``V4IdentityRegistry`` to prove the role CLI
    reached identity construction — i.e., it passed through the
    ``FOREMAN_PROJECTS_PATH`` fallback + ``model_copy`` project-patch
    without raising the ``project ... not found`` error first."""


def _empty_projects_config() -> V4Config:
    """Build a valid V4Config whose ``projects`` list is EMPTY.

    Mirrors the production post-#477 config.toml shape: every required
    identity/operator/storage block is present, but ``[[projects]]`` is
    absent (they live in the host-mounted projects file now). Forces the
    role CLI down the ``FOREMAN_PROJECTS_PATH`` fallback branch.
    """
    placeholder_app = AppCredentials(app_id=1, private_key_path="/dev/null")
    return V4Config(
        storage=StorageConfig(engine="postgres", dsn="postgresql://test/test"),
        log_dir="/tmp/foreman-test-logs",
        apps=AppsConfig(
            planner=placeholder_app,
            reviewer=placeholder_app,
            fixer=placeholder_app,
            worker=placeholder_app,
        ),
        orchestrator=OrchestratorConfig(app_id=1, private_key_path="/dev/null"),
        operator=OperatorConfig(
            supervisor=OperatorIdentity(name="Test Supervisor", email="sup@example.com"),
            signer=OperatorIdentity(name="Test Signer", email="sign@example.com"),
        ),
        projects=[],
    )


def _loaded_projects() -> list[ProjectConfig]:
    """The project list a healthy ``FOREMAN_PROJECTS_PATH`` file yields."""
    return [
        ProjectConfig(
            name="proj-a",
            repo="testowner/proj-a",
            local_clone_path="/tmp/proj-a",
        ),
    ]


def _populated_projects_config() -> V4Config:
    """A V4Config whose ``projects`` is already populated (legacy shape).

    Tests that mock ``load_v4_config`` to return this take the
    ``cfg.projects`` branch of the role CLI resolution — the projects file
    is never consulted. Proves the pre-#477 code path stays intact.
    """
    cfg = _empty_projects_config()
    return cfg.model_copy(update={"projects": _loaded_projects()})


@pytest.fixture
def _projects_file(tmp_path: Path) -> Path:
    """An on-disk projects file so ``projects_path.exists()`` is True.

    Its CONTENT is irrelevant — the tests patch ``load_v4_projects`` — but
    the file must exist so the role CLI takes the ``load_v4_projects(...)``
    arm rather than the empty-list arm of the fallback branch.
    """
    path = tmp_path / "projects.toml"
    path.write_text(
        '[[projects]]\nname = "proj-a"\nrepo = "testowner/proj-a"\n'
        'local_clone_path = "/tmp/proj-a"\n',
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Happy-fallback: cfg.projects empty → load from file → resolve → model_copy →
# reach V4IdentityRegistry with the resolved project's repo.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module, entry, kwargs",
    [
        ("planner", _run_planner_for_v4, {"project": "proj-a", "issue_number": 7}),
        (
            "reviewer",
            _run_reviewer_for_v4,
            {"project": "proj-a", "issue_number": 7, "target": "spec"},
        ),
        (
            "fixer",
            _run_fixer_for_v4,
            {"project": "proj-a", "issue_number": 7, "target": "spec"},
        ),
        ("worker", _run_worker_for_v4, {"project": "proj-a", "issue_number": 7}),
    ],
    ids=["planner", "reviewer", "fixer", "worker"],
)
def test_role_v4_loads_project_from_projects_path_when_cfg_empty(
    module: str,
    entry: Callable[..., object],
    kwargs: dict[str, object],
    _projects_file: Path,
) -> None:
    """cfg.projects empty → the role CLI loads from FOREMAN_PROJECTS_PATH,
    resolves the project, patches cfg via model_copy, and reaches
    V4IdentityRegistry with the RESOLVED project's repo.

    We intercept at ``V4IdentityRegistry`` with a sentinel so the test
    proves the fallback + patch ran without spinning up any provider or
    network. The sentinel carries the ``installation_repo`` the role CLI
    passed, which is only correct if ``model_copy`` patched the loaded
    project into cfg and ``project_cfg.repo`` resolved from the file.
    """
    captured: dict[str, object] = {}

    def _fake_registry(**registry_kwargs: object) -> MagicMock:
        captured["installation_repo"] = registry_kwargs.get("installation_repo")
        raise _RegistrySentinel

    with (
        patch(f"foreman.roles.{module}.load_v4_config", return_value=_empty_projects_config()),
        patch(f"foreman.roles.{module}.load_v4_projects", return_value=_loaded_projects()),
        patch("foreman.v4.identity.V4IdentityRegistry", side_effect=_fake_registry),
        patch.dict("os.environ", {"FOREMAN_PROJECTS_PATH": str(_projects_file)}),
    ):
        with pytest.raises(_RegistrySentinel):
            entry(**kwargs)

    assert captured["installation_repo"] == "testowner/proj-a", (
        f"{module}: expected the resolved project's repo to reach "
        f"V4IdentityRegistry via the model_copy-patched cfg, got "
        f"{captured['installation_repo']!r}"
    )


# ---------------------------------------------------------------------------
# Not-found-fallback: cfg.projects empty → load from file → project absent →
# clean 'project ... not found' error (never reaches identity construction).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module, entry, kwargs",
    [
        ("planner", _run_planner_for_v4, {"project": "ghost", "issue_number": 7}),
        (
            "reviewer",
            _run_reviewer_for_v4,
            {"project": "ghost", "issue_number": 7, "target": "spec"},
        ),
        (
            "fixer",
            _run_fixer_for_v4,
            {"project": "ghost", "issue_number": 7, "target": "spec"},
        ),
        ("worker", _run_worker_for_v4, {"project": "ghost", "issue_number": 7}),
    ],
    ids=["planner", "reviewer", "fixer", "worker"],
)
def test_role_v4_project_missing_from_projects_file_raises_clean_error(
    module: str,
    entry: Callable[..., object],
    kwargs: dict[str, object],
    _projects_file: Path,
) -> None:
    """cfg.projects empty + requested project absent from the loaded file →
    the fallback branch raises the clean ``project ... not found`` error
    naming the known projects, before any identity/provider work."""
    with (
        patch(f"foreman.roles.{module}.load_v4_config", return_value=_empty_projects_config()),
        patch(f"foreman.roles.{module}.load_v4_projects", return_value=_loaded_projects()),
        patch.dict("os.environ", {"FOREMAN_PROJECTS_PATH": str(_projects_file)}),
    ):
        with pytest.raises(ValueError, match="project 'ghost' not found in V4Config"):
            entry(**kwargs)


# ---------------------------------------------------------------------------
# Legacy cfg.projects branch: when cfg.projects is already populated, the role
# CLI resolves from it directly and NEVER consults FOREMAN_PROJECTS_PATH.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module, entry, kwargs",
    [
        ("planner", _run_planner_for_v4, {"project": "proj-a", "issue_number": 7}),
        (
            "reviewer",
            _run_reviewer_for_v4,
            {"project": "proj-a", "issue_number": 7, "target": "spec"},
        ),
        (
            "fixer",
            _run_fixer_for_v4,
            {"project": "proj-a", "issue_number": 7, "target": "spec"},
        ),
    ],
    ids=["planner", "reviewer", "fixer"],
)
def test_role_v4_uses_cfg_projects_when_populated(
    module: str,
    entry: Callable[..., object],
    kwargs: dict[str, object],
) -> None:
    """cfg.projects populated → the role CLI resolves from it and never
    calls load_v4_projects. Intercept at V4IdentityRegistry with a sentinel
    and assert the resolved repo reached it AND load_v4_projects was never
    called (the projects-file path is skipped)."""
    captured: dict[str, object] = {}

    def _fake_registry(**registry_kwargs: object) -> MagicMock:
        captured["installation_repo"] = registry_kwargs.get("installation_repo")
        raise _RegistrySentinel

    load_projects_spy = MagicMock()

    with (
        patch(f"foreman.roles.{module}.load_v4_config", return_value=_populated_projects_config()),
        patch(f"foreman.roles.{module}.load_v4_projects", load_projects_spy),
        patch("foreman.v4.identity.V4IdentityRegistry", side_effect=_fake_registry),
    ):
        with pytest.raises(_RegistrySentinel):
            entry(**kwargs)

    assert captured["installation_repo"] == "testowner/proj-a"
    load_projects_spy.assert_not_called()
