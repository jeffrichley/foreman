"""V4 → V3 Config translation for the role CLIs.

Phase 8b.3 makes the role CLIs read v4 config (so a new project like
algokit is found via ``V4Config.projects``, not via the v3 config which
only has voice/foreman/agent_core hard-wired). The legacy ``run_<role>``
async functions still take a v3 ``Config`` object — this adapter builds
one from ``V4Config`` + the requested project's data so the legacy
bodies don't have to change.

Phase 8c or Phase 9 deletion can later refactor ``run_<role>`` to take
``V4Config`` directly; at that point this adapter becomes unused and
gets deleted.

Imports allowed:

- ``foreman.config`` (survival set per the isolation guard) — for v3
  shapes.
- ``foreman.v4.config`` — for ``V4Config`` + ``ProjectConfig``.
"""

from __future__ import annotations

import os
from pathlib import Path

from foreman.config import AdminConfig, ReconcilerConfig
from foreman.config import AppsConfig as V3AppsConfig
from foreman.config import Config as V3Config
from foreman.config import OrchestratorConfig as V3OrchestratorConfig
from foreman.config import ProjectConfig as V3ProjectConfig
from foreman.v4.config import ProjectConfig as V4ProjectConfig
from foreman.v4.config import V4Config
from foreman.v4.config import load_config as v4_load_config

_DEFAULT_V4_CONFIG = Path.home() / ".foreman" / "v4" / "config.toml"


def load_v3_config_for_project(project_name: str) -> V3Config:
    """Load v4 config from ``FOREMAN_V4_CONFIG``, look up ``project_name``,
    and return a v3 ``Config`` adapted for the legacy ``run_<role>`` async
    functions.

    Single helper shared by all 4 role CLIs (planner/reviewer/fixer/worker)
    — the per-role v4-emit blocks each collapse to one call. Raises
    ``ValueError`` with the list of known project names when the requested
    project is missing, so the role's outer exception handler can surface
    a meaningful FOREMAN_OUTCOME instead of a bare KeyError.
    """
    v4_cfg_path = Path(os.environ.get("FOREMAN_V4_CONFIG", _DEFAULT_V4_CONFIG))
    v4_cfg = v4_load_config(v4_cfg_path)
    v4_project = next(
        (p for p in v4_cfg.projects if p.name == project_name),
        None,
    )
    if v4_project is None:
        known = [p.name for p in v4_cfg.projects]
        raise ValueError(
            f"project {project_name!r} not found in V4Config at "
            f"{v4_cfg_path}. Known projects: {known}"
        )
    return v3_config_from_v4(v4_cfg, v4_project)


def v3_config_from_v4(v4_cfg: V4Config, project: V4ProjectConfig) -> V3Config:
    """Build a v3 ``Config`` from a v4 ``V4Config`` + the chosen project.

    Only the chosen project is included in the returned ``Config``; the
    legacy ``run_<role>`` functions only need the one project's data,
    and a partial projects dict keeps the adapter's seam narrow.

    Field mapping (v4 → v3):

    - ``v4_cfg.apps.<role>.app_id``           → v3 ``apps.<role>_app_id``
    - ``v4_cfg.apps.<role>.private_key_path`` → v3 ``apps.<role>_private_key_path``
    - ``v4_cfg.orchestrator.app_id``          → v3 ``orchestrator.app_id``
    - ``v4_cfg.orchestrator.private_key_path``→ v3 ``orchestrator.private_key_path``
    - ``v4_cfg.merge_mechanism``              → v3 ``reconciler.merge_mechanism``
      (only when the v4 value is one of the v3-supported literals;
      see below)
    - ``project.repo / local_clone_path / check_command / dev_base_branch /
      max_fix_attempts / max_impl_attempts / merge_mechanism`` → v3
      ``projects[<name>].*``

    v3's ``MergeMechanism`` Literal is ``"direct" | "queue"`` while v4's
    is ``"queue" | "merge" | "squash" | "rebase"``. The intersection is
    ``"queue"`` — for the only-mode the legacy code path knows. The
    adapter passes the value through when it matches; otherwise it
    falls back to the v3 default ``"direct"`` so the legacy
    ``ReconcilerConfig`` validator does not reject. The role CLIs never
    consume ``reconciler.merge_mechanism`` directly (the daemon does),
    so the fallback is purely defensive plumbing.
    """
    v3_apps = V3AppsConfig(
        planner_app_id=v4_cfg.apps.planner.app_id,
        planner_private_key_path=v4_cfg.apps.planner.private_key_path,
        reviewer_app_id=v4_cfg.apps.reviewer.app_id,
        reviewer_private_key_path=v4_cfg.apps.reviewer.private_key_path,
        fixer_app_id=v4_cfg.apps.fixer.app_id,
        fixer_private_key_path=v4_cfg.apps.fixer.private_key_path,
        worker_app_id=v4_cfg.apps.worker.app_id,
        worker_private_key_path=v4_cfg.apps.worker.private_key_path,
    )

    # v3 ``ProjectConfig.merge_mechanism`` accepts only ``"direct" | "queue"``;
    # v4 grew the literal to include ``"merge" | "squash" | "rebase"``. Only
    # ``"queue"`` (and ``None``) survive the round-trip; anything else
    # collapses to ``None`` (inherit from reconciler default) so the v3
    # validator does not reject.
    v3_project_merge = project.merge_mechanism if project.merge_mechanism == "queue" else None

    v3_project = V3ProjectConfig(
        repo=project.repo,
        local_clone_path=project.local_clone_path,
        apps=v3_apps,
        check_command=project.check_command,
        dev_base_branch=project.dev_base_branch,
        max_fix_attempts=project.max_fix_attempts,
        max_impl_attempts=project.max_impl_attempts,
        merge_mechanism=v3_project_merge,
    )
    v3_orchestrator = V3OrchestratorConfig(
        app_id=v4_cfg.orchestrator.app_id,
        private_key_path=v4_cfg.orchestrator.private_key_path,
    )

    # See merge_mechanism docstring above — same widening narrows back
    # to ``"direct" | "queue"`` here at the reconciler-default seam.
    v3_reconciler_merge = v4_cfg.merge_mechanism if v4_cfg.merge_mechanism == "queue" else "direct"
    reconciler = ReconcilerConfig(merge_mechanism=v3_reconciler_merge)

    return V3Config(
        admin=AdminConfig(),
        orchestrator=v3_orchestrator,
        reconciler=reconciler,
        projects={project.name: v3_project},
    )
