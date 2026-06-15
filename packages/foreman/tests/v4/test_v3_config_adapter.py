"""Tests for ``foreman.v4._v3_config_adapter.v3_config_from_v4``.

The adapter is the seam between the v4-config-reading role CLI outer
skin and the v3-Config-consuming legacy ``run_<role>`` async bodies. A
mis-mapped field here would silently corrupt the legacy code path; the
tests below pin each field-flow individually so a future v4-config
schema change shows up as a test failure pointing at the missed seam.
"""
from __future__ import annotations

from foreman.v4._v3_config_adapter import v3_config_from_v4
from foreman.v4.config import (
    AppCredentials,
    AppsConfig,
    OrchestratorConfig,
    ProjectConfig,
    V4Config,
)


def _make_v4_config(
    *,
    projects: list[ProjectConfig],
    merge_mechanism: str = "queue",
) -> V4Config:
    """Build a minimal V4Config for adapter exercise.

    Apps + orchestrator use distinct app_ids per role so credentials-
    threading tests can pin each slot independently.
    """
    return V4Config(
        db_path="/tmp/v4.db",
        log_dir="/tmp/v4-logs",
        merge_mechanism=merge_mechanism,  # type: ignore[arg-type]
        apps=AppsConfig(
            planner=AppCredentials(app_id=1001, private_key_path="/tmp/planner.pem"),
            reviewer=AppCredentials(app_id=1002, private_key_path="/tmp/reviewer.pem"),
            fixer=AppCredentials(app_id=1003, private_key_path="/tmp/fixer.pem"),
            worker=AppCredentials(app_id=1004, private_key_path="/tmp/worker.pem"),
        ),
        orchestrator=OrchestratorConfig(
            app_id=9999, private_key_path="/tmp/orch.pem",
        ),
        projects=projects,
    )


def test_adapter_round_trip_basic_fields() -> None:
    """Adapter threads name / repo / local_clone_path into v3 ``Config``."""
    project = ProjectConfig(
        name="algokit",
        repo="jeffrichley/algokit",
        local_clone_path="/home/jeff/code/algokit",
    )
    v4_cfg = _make_v4_config(projects=[project])

    v3_cfg = v3_config_from_v4(v4_cfg, project)

    assert "algokit" in v3_cfg.projects
    v3_project = v3_cfg.projects["algokit"]
    assert v3_project.repo == "jeffrichley/algokit"
    assert v3_project.local_clone_path == "/home/jeff/code/algokit"


def test_adapter_threads_v4_project_overrides() -> None:
    """V4 per-project overrides flow into v3 ProjectConfig."""
    project = ProjectConfig(
        name="algokit",
        repo="jeffrichley/algokit",
        local_clone_path="/home/jeff/code/algokit",
        check_command="uv run pytest",
        dev_base_branch="feat/walking-skeleton",
        max_fix_attempts=5,
    )
    v4_cfg = _make_v4_config(projects=[project])

    v3_cfg = v3_config_from_v4(v4_cfg, project)

    v3_project = v3_cfg.projects["algokit"]
    assert v3_project.check_command == "uv run pytest"
    assert v3_project.dev_base_branch == "feat/walking-skeleton"
    assert v3_project.max_fix_attempts == 5


def test_adapter_threads_orchestrator_credentials() -> None:
    """V4 ``orchestrator`` block flows into v3 ``OrchestratorConfig``."""
    project = ProjectConfig(
        name="algokit",
        repo="jeffrichley/algokit",
        local_clone_path="/home/jeff/code/algokit",
    )
    v4_cfg = _make_v4_config(projects=[project])

    v3_cfg = v3_config_from_v4(v4_cfg, project)

    assert v3_cfg.orchestrator.app_id == 9999
    assert v3_cfg.orchestrator.private_key_path == "/tmp/orch.pem"


def test_adapter_threads_apps_credentials_for_all_4_roles() -> None:
    """Each per-role v4 App credential lands in the right v3 ``AppsConfig`` slot."""
    project = ProjectConfig(
        name="algokit",
        repo="jeffrichley/algokit",
        local_clone_path="/home/jeff/code/algokit",
    )
    v4_cfg = _make_v4_config(projects=[project])

    v3_cfg = v3_config_from_v4(v4_cfg, project)

    apps = v3_cfg.projects["algokit"].apps
    assert apps.planner_app_id == 1001
    assert apps.planner_private_key_path == "/tmp/planner.pem"
    assert apps.reviewer_app_id == 1002
    assert apps.reviewer_private_key_path == "/tmp/reviewer.pem"
    assert apps.fixer_app_id == 1003
    assert apps.fixer_private_key_path == "/tmp/fixer.pem"
    assert apps.worker_app_id == 1004
    assert apps.worker_private_key_path == "/tmp/worker.pem"


def test_adapter_picks_only_requested_project() -> None:
    """V4Config with two projects + adapter called with one → v3 has only that one."""
    project_a = ProjectConfig(
        name="algokit",
        repo="jeffrichley/algokit",
        local_clone_path="/home/jeff/code/algokit",
    )
    project_b = ProjectConfig(
        name="voice",
        repo="jeffrichley/voice",
        local_clone_path="/home/jeff/code/voice",
    )
    v4_cfg = _make_v4_config(projects=[project_a, project_b])

    v3_cfg = v3_config_from_v4(v4_cfg, project_a)

    assert set(v3_cfg.projects.keys()) == {"algokit"}


def test_adapter_v4_only_merge_mechanism_collapses_to_v3_default() -> None:
    """V4 merge_mechanism values v3 doesn't know (``merge``/``squash``/``rebase``) collapse to v3 default.

    v3's literal is ``"direct" | "queue"``; v4 grew it to four values.
    The adapter narrows to the intersection — ``"queue"`` passes through;
    anything else falls back so the v3 Pydantic validator does not
    reject. Role CLIs never read ``reconciler.merge_mechanism`` directly,
    so the fallback is defensive plumbing for the daemon-level field.
    """
    project = ProjectConfig(
        name="algokit",
        repo="jeffrichley/algokit",
        local_clone_path="/home/jeff/code/algokit",
        merge_mechanism="squash",
    )
    v4_cfg = _make_v4_config(projects=[project], merge_mechanism="rebase")

    v3_cfg = v3_config_from_v4(v4_cfg, project)

    # Per-project: collapses to None (inherit) when v4 value isn't ``"queue"``.
    assert v3_cfg.projects["algokit"].merge_mechanism is None
    # Reconciler-level: collapses to ``"direct"`` (the v3 default).
    assert v3_cfg.reconciler.merge_mechanism == "direct"


def test_adapter_v4_queue_merge_mechanism_passes_through() -> None:
    """V4 ``"queue"`` survives the round-trip at both per-project + reconciler levels."""
    project = ProjectConfig(
        name="algokit",
        repo="jeffrichley/algokit",
        local_clone_path="/home/jeff/code/algokit",
        merge_mechanism="queue",
    )
    v4_cfg = _make_v4_config(projects=[project], merge_mechanism="queue")

    v3_cfg = v3_config_from_v4(v4_cfg, project)

    assert v3_cfg.projects["algokit"].merge_mechanism == "queue"
    assert v3_cfg.reconciler.merge_mechanism == "queue"
