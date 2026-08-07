"""V4Config — TOML-loaded settings."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from foreman.v4.config import (
    OperatorConfig,
    OperatorIdentity,
    ProjectConfig,
    ProjectOperatorOverride,
    SandboxConfig,
    StorageConfig,
    V4Config,
    load_config,
    resolve_operator,
)

# Shared `[apps.*]` + `[orchestrator]` blocks used by every existing
# test. As of Task 8.3 `apps` is REQUIRED on V4Config; Task 8.4 makes
# `orchestrator` REQUIRED too (pivoted from env-var PAT to App
# installation credentials — same shape as the per-role apps).
# Concatenating this helper into each test's TOML keeps the existing
# assertions focused on what they're actually testing (projects, etc.)
# instead of rewriting every TOML string inline.
_APPS_TOML = (
    "[apps.planner]\n"
    "app_id = 12345\n"
    'private_key_path = "/tmp/fake-planner.pem"\n'
    "[apps.reviewer]\n"
    "app_id = 12346\n"
    'private_key_path = "/tmp/fake-reviewer.pem"\n'
    "[apps.fixer]\n"
    "app_id = 12347\n"
    'private_key_path = "/tmp/fake-fixer.pem"\n'
    "[apps.worker]\n"
    "app_id = 12348\n"
    'private_key_path = "/tmp/fake-worker.pem"\n'
    "[orchestrator]\n"
    "app_id = 12349\n"
    'private_key_path = "/tmp/fake-orchestrator.pem"\n'
)

# Issue #347: ``[operator]`` is REQUIRED on V4Config. The shared TOML
# fixture below carries both sub-tables so every existing test that
# concatenates ``_APPS_TOML`` (now actually ``_APPS_TOML + _OPERATOR_TOML``)
# continues to validate. Operator-targeted tests further down build
# their own TOML to assert load-time validation of malformed inputs.
_OPERATOR_TOML = (
    "[operator.supervisor]\n"
    'name = "Wren Richley"\n'
    'email = "wren@example.com"\n'
    "[operator.signer]\n"
    'name = "Jeff Richley"\n'
    'email = "jeff@example.com"\n'
)
# v5 (kill-sqlite): StorageConfig is Postgres-only with a loud fail —
# every config without a valid [storage] block (engine=postgres + dsn)
# now raises at load time. The shared fixture carries a postgres block so
# every existing test that concatenates ``_APPS_TOML`` keeps loading.
# Storage-targeted tests further down build their own [storage] block to
# exercise the validator, so this shared block is sliced off for them
# (see ``_STORAGE_TOML`` length used in those tests).
_STORAGE_TOML = (
    '[storage]\nengine = "postgres"\ndsn = "postgresql://foreman:pw@postgres:5432/foreman"\n'
)
_APPS_TOML = _APPS_TOML + _OPERATOR_TOML + _STORAGE_TOML
# Same shared apps+operator boilerplate WITHOUT the storage block, for
# the storage-targeted tests that supply (or deliberately omit) their own
# [storage] block.
_APPS_TOML_NO_STORAGE = _APPS_TOML[: -len(_STORAGE_TOML)]


def test_daemon_defaults(tmp_path: Path):
    """Phase 8d.19 dropped merge_mechanism — daemon now uses direct
    pr.merge() for both spec and impl PRs, same on every project. The
    other daemon-level defaults stay."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n' + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.tick_seconds > 0
    assert config.max_in_flight > 0


def test_max_in_flight_above_one_accepted(tmp_path: Path):
    """Global ``max_in_flight > 1`` is now valid: it parallelises DIFFERENT
    repos (one foreman + one agent_core ticket at once) while each repo
    stays serial via the per-repo cap (pinned to 1). The old foreman#316
    global ``== 1`` pin moved to the per-repo grain where the merge race
    actually is (self-heal arch-review, 2026-07-17)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n'
        "max_in_flight = 4\n" + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.max_in_flight == 4


def test_max_in_flight_zero_rejected(tmp_path: Path):
    """0 would size the ThreadPoolExecutor at zero workers — no ticket ever
    runs. ``ge=1`` rejects it at load time."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n'
        "max_in_flight = 0\n" + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_max_in_flight_one_ok(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n'
        "max_in_flight = 1\n" + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.max_in_flight == 1


def test_projects_round_trip(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n' + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
        "[[projects]]\n"
        'name = "foreman"\n'
        'repo = "jeffrichley/foreman"\n'
        'local_clone_path = "/tmp/foreman"\n'
    )
    config = load_config(config_path)
    assert len(config.projects) == 2
    assert config.projects[0].name == "voice"
    assert config.projects[1].name == "foreman"


def test_missing_project_name_raises(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n' + _APPS_TOML + "[[projects]]\n"
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_project_config_default_trigger_label():
    p = ProjectConfig(
        name="voice",
        repo="jeffrichley/voice",
        local_clone_path="/tmp/voice",
    )
    assert p.trigger_label == "foreman:plan"


def test_role_timeout_seconds_default_600(tmp_path: Path):
    """Phase 5 carryover: SubprocessRoleDispatcher.timeout_seconds was hardcoded
    600s. V4Config now exposes it; Phase 7.5 will thread it through to the
    dispatcher constructor."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n' + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.role_timeout_seconds == 600


def test_role_timeout_seconds_override(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n'
        "role_timeout_seconds = 1200\n" + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.role_timeout_seconds == 1200


def test_role_inactivity_timeout_seconds_default_300(tmp_path: Path):
    """foreman#483: the no-output watchdog window defaults to 300s (5 min)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n' + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.role_inactivity_timeout_seconds == 300


def test_role_inactivity_timeout_seconds_override(tmp_path: Path):
    """foreman#483: the watchdog window is operator-tunable; 0 disables it."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n'
        "role_inactivity_timeout_seconds = 0\n" + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.role_inactivity_timeout_seconds == 0


def test_max_state_attempts_default_3(tmp_path: Path):
    """Phase 8c.2 retry cap defaults to 3 — matches max_fix_attempts /
    max_impl_attempts shape and is the smallest cap that still allows
    one retry after a transient failure."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n' + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.max_state_attempts == 3


def test_max_state_attempts_override(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n'
        "max_state_attempts = 5\n" + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.max_state_attempts == 5


def test_max_state_attempts_zero_raises(tmp_path: Path):
    """ge=1 — a value of 0 would dump every ticket to NeedsHelp on
    first entry, which is never a valid configuration."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n'
        "max_state_attempts = 0\n" + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    with pytest.raises(ValidationError):
        load_config(config_path)


# ---------------------------------------------------------------------------
# Task 8.3: [apps] + [orchestrator] sections
# ---------------------------------------------------------------------------


def test_missing_apps_block_raises(tmp_path: Path):
    """Task 8.3: ``[apps]`` is REQUIRED. A config without any app
    credentials cannot construct an IdentityRegistry, so the daemon
    refuses to start at load time. The error message should mention
    ``apps`` so the operator sees the gap immediately."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n' + _STORAGE_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    with pytest.raises(ValidationError) as exc_info:
        load_config(config_path)
    assert "apps" in str(exc_info.value)


def test_missing_role_app_raises(tmp_path: Path):
    """Task 8.3: each of the four roles needs its own credentials.
    Dropping a single role (worker, here) still raises — partial
    identity wiring is worse than none, because the daemon would crash
    halfway through processing a ticket."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n'
        "[apps.planner]\n"
        "app_id = 12345\n"
        'private_key_path = "/tmp/fake-planner.pem"\n'
        "[apps.reviewer]\n"
        "app_id = 12346\n"
        'private_key_path = "/tmp/fake-reviewer.pem"\n'
        "[apps.fixer]\n"
        "app_id = 12347\n"
        'private_key_path = "/tmp/fake-fixer.pem"\n' + _STORAGE_TOML + "[[projects]]\n"
        # No [apps.worker].
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    with pytest.raises(ValidationError) as exc_info:
        load_config(config_path)
    assert "worker" in str(exc_info.value)


def test_apps_orchestrator_round_trip(tmp_path: Path):
    """Task 8.4: full valid config round-trips through Pydantic. Each
    role's ``app_id`` + ``private_key_path`` is reachable from the
    loaded V4Config, and the ``[orchestrator]`` block carries its own
    App installation credentials (no env-var PAT)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n'
        "[apps.planner]\n"
        "app_id = 12345\n"
        'private_key_path = "/tmp/fake-planner.pem"\n'
        "[apps.reviewer]\n"
        "app_id = 12346\n"
        'private_key_path = "/tmp/fake-reviewer.pem"\n'
        "[apps.fixer]\n"
        "app_id = 12347\n"
        'private_key_path = "/tmp/fake-fixer.pem"\n'
        "[apps.worker]\n"
        "app_id = 12348\n"
        'private_key_path = "/tmp/fake-worker.pem"\n'
        "[orchestrator]\n"
        "app_id = 99999\n"
        'private_key_path = "/tmp/fake-orchestrator.pem"\n'
        + _OPERATOR_TOML
        + _STORAGE_TOML
        + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.apps.planner.app_id == 12345
    assert config.apps.planner.private_key_path == "/tmp/fake-planner.pem"
    assert config.apps.reviewer.app_id == 12346
    assert config.apps.reviewer.private_key_path == "/tmp/fake-reviewer.pem"
    assert config.apps.fixer.app_id == 12347
    assert config.apps.fixer.private_key_path == "/tmp/fake-fixer.pem"
    assert config.apps.worker.app_id == 12348
    assert config.apps.worker.private_key_path == "/tmp/fake-worker.pem"
    assert config.orchestrator.app_id == 99999
    assert config.orchestrator.private_key_path == "/tmp/fake-orchestrator.pem"


# ---------------------------------------------------------------------------
# Task 8b.2: ProjectConfig grows per-project fields the role CLIs need
# ---------------------------------------------------------------------------


def test_project_minimal_still_parses(tmp_path: Path):
    """Phase 8b.2: ProjectConfig keeps minimal-required shape.

    A TOML with only the historically-required fields (name/repo/
    local_clone_path) still loads, and all 4 new per-project fields take
    their sensible defaults. This proves existing v4 configs continue
    to work unchanged — operators do not have to touch their config to
    pick up the role-CLI rewire.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n' + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    proj = config.projects[0]
    assert proj.check_command is None
    assert proj.dev_base_branch is None
    assert proj.max_fix_attempts == 3
    assert proj.max_impl_attempts == 3


def test_project_check_command_override(tmp_path: Path):
    """Phase 8b.2: per-project ``check_command`` flows through.

    Worker uses this verification command before claiming done;
    projects that don't run ``just check`` set it here.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n' + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
        'check_command = "pytest"\n'
    )
    config = load_config(config_path)
    assert config.projects[0].check_command == "pytest"


def test_project_dev_base_branch_override(tmp_path: Path):
    """Phase 8b.2: per-project ``dev_base_branch`` flows through.

    Used for walking-skeleton phases where the active dev line lives
    on a feature branch rather than ``main``.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n' + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
        'dev_base_branch = "develop"\n'
    )
    config = load_config(config_path)
    assert config.projects[0].dev_base_branch == "develop"


def test_project_auto_merge_impl_defaults_false():
    """foreman#418: ``auto_merge_impl`` defaults to False — an approved
    impl PR parks at ImplApproved for human merge unless the project
    explicitly opts back in to the historic auto-merge behavior."""
    p = ProjectConfig(
        name="voice",
        repo="jeffrichley/voice",
        local_clone_path="/tmp/voice",
    )
    assert p.auto_merge_impl is False


def test_project_auto_merge_impl_round_trip(tmp_path: Path):
    """foreman#418: ``auto_merge_impl = true`` flows through TOML load.

    Opt-in restores the historic behavior where an approved impl PR
    auto-merges via MergingState.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n' + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
        "auto_merge_impl = true\n"
    )
    config = load_config(config_path)
    assert config.projects[0].auto_merge_impl is True


def test_project_max_fix_attempts_override(tmp_path: Path):
    """Phase 8b.2: per-project ``max_fix_attempts`` flows through.

    Fixer's preflight cap before NeedsHelp escalation.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n' + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
        "max_fix_attempts = 5\n"
    )
    config = load_config(config_path)
    assert config.projects[0].max_fix_attempts == 5


def test_project_max_impl_attempts_override(tmp_path: Path):
    """Phase 8b.2: per-project ``max_impl_attempts`` flows through.

    Worker's preflight cap before NeedsHelp escalation. Audit during
    8b.2 implementation found Worker reads this field on v3
    ProjectConfig (worker.py:702), so v4 must grow it too even though
    the plan's starting field list omitted it.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n' + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
        "max_impl_attempts = 5\n"
    )
    config = load_config(config_path)
    assert config.projects[0].max_impl_attempts == 5


# Imports required by the helpers in this section. Lazy-named at this
# point in the file so the top-level imports stay minimal.
from foreman.v4.config import (  # noqa: E402
    AppCredentials,
    AppsConfig,
    OrchestratorConfig,
)


def test_project_attempt_caps_reject_zero(tmp_path: Path):
    """Phase 8b.2 ge=1 constraint: matches v3 ProjectConfig validation.

    A value of 0 (or negative) would silently skip the role's retry
    loop and dump every ticket to NeedsHelp on first dispatch — a
    confusing config-corruption mode. Reject at load time so the
    daemon refuses to start instead.
    """
    for field, bad in (("max_fix_attempts", 0), ("max_impl_attempts", 0)):
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[daemon]\n"
            'log_dir = "/tmp/foreman-logs"\n' + _APPS_TOML + "[[projects]]\n"
            'name = "voice"\n'
            'repo = "jeffrichley/voice"\n'
            'local_clone_path = "/tmp/voice"\n'
            f"{field} = {bad}\n"
        )
        with pytest.raises(ValidationError):
            load_config(config_path)


def test_missing_orchestrator_raises(tmp_path: Path):
    """Task 8.4: ``[orchestrator]`` is REQUIRED. The pivot from env-var
    PAT to App installation tokens means the daemon literally needs an
    ``app_id`` + ``private_key_path`` to mint orchestrator-level
    tokens — no default makes sense. A missing block raises
    ValidationError at load time, same as the per-role [apps.*] blocks."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n'
        "[apps.planner]\n"
        "app_id = 12345\n"
        'private_key_path = "/tmp/fake-planner.pem"\n'
        "[apps.reviewer]\n"
        "app_id = 12346\n"
        'private_key_path = "/tmp/fake-reviewer.pem"\n'
        "[apps.fixer]\n"
        "app_id = 12347\n"
        'private_key_path = "/tmp/fake-fixer.pem"\n'
        "[apps.worker]\n"
        "app_id = 12348\n"
        'private_key_path = "/tmp/fake-worker.pem"\n' + _STORAGE_TOML + "[[projects]]\n"
        # No [orchestrator] block.
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    with pytest.raises(ValidationError) as exc_info:
        load_config(config_path)
    assert "orchestrator" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Issue #347: [operator] block (supervisor + signer) + per-project override
# ---------------------------------------------------------------------------


def _config_text(operator_block: str | None, project_extra: str = "") -> str:
    """Helper to build a valid TOML string with a custom operator block.

    Pulls in the shared _APPS_TOML minus its embedded operator block,
    so each test can swap the operator section without rebuilding the
    boilerplate.
    """
    # _APPS_TOML now contains _OPERATOR_TOML + _STORAGE_TOML at the end
    # (see top-of-file fixture composition). Strip both for tests that
    # swap the operator block; we re-append a valid [storage] block below
    # so the Postgres-only loud-fail validator stays satisfied.
    base_apps_only = _APPS_TOML[: -(len(_OPERATOR_TOML) + len(_STORAGE_TOML))]
    body = '[daemon]\nlog_dir = "/tmp/foreman-logs"\n' + base_apps_only
    if operator_block is not None:
        body += operator_block
    body += _STORAGE_TOML
    body += (
        "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n' + project_extra
    )
    return body


def test_operator_parse_round_trip(tmp_path: Path):
    """Top-level [operator] with both sub-tables round-trips through pydantic."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(_config_text(_OPERATOR_TOML))
    config = load_config(config_path)
    assert config.operator.supervisor.name == "Wren Richley"
    assert config.operator.supervisor.email == "wren@example.com"
    assert config.operator.signer.name == "Jeff Richley"
    assert config.operator.signer.email == "jeff@example.com"


def test_missing_operator_block_raises(tmp_path: Path):
    """Issue #347: ``[operator]`` is REQUIRED — refuse to boot without it."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(_config_text(None))
    with pytest.raises(ValidationError) as exc_info:
        load_config(config_path)
    assert "operator" in str(exc_info.value)


def test_missing_operator_supervisor_raises(tmp_path: Path):
    """Issue #347: ``[operator.supervisor]`` is REQUIRED."""
    operator_block = '[operator.signer]\nname = "Jeff Richley"\nemail = "jeff@example.com"\n'
    config_path = tmp_path / "config.toml"
    config_path.write_text(_config_text(operator_block))
    with pytest.raises(ValidationError) as exc_info:
        load_config(config_path)
    assert "supervisor" in str(exc_info.value)


def test_missing_operator_signer_raises(tmp_path: Path):
    """Issue #347: ``[operator.signer]`` is REQUIRED."""
    operator_block = '[operator.supervisor]\nname = "Wren Richley"\nemail = "wren@example.com"\n'
    config_path = tmp_path / "config.toml"
    config_path.write_text(_config_text(operator_block))
    with pytest.raises(ValidationError) as exc_info:
        load_config(config_path)
    assert "signer" in str(exc_info.value)


def test_missing_supervisor_name_raises(tmp_path: Path):
    """Each identity requires ``name``."""
    operator_block = (
        "[operator.supervisor]\n"
        'email = "wren@example.com"\n'
        "[operator.signer]\n"
        'name = "Jeff Richley"\n'
        'email = "jeff@example.com"\n'
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(_config_text(operator_block))
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_missing_signer_email_raises(tmp_path: Path):
    """Each identity requires ``email``."""
    operator_block = (
        "[operator.supervisor]\n"
        'name = "Wren Richley"\n'
        'email = "wren@example.com"\n'
        "[operator.signer]\n"
        'name = "Jeff Richley"\n'
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(_config_text(operator_block))
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_whitespace_only_name_raises(tmp_path: Path):
    """Issue #347 AC bullet 1: name must be non-empty after .strip()."""
    operator_block = (
        "[operator.supervisor]\n"
        'name = " "\n'
        'email = "wren@example.com"\n'
        "[operator.signer]\n"
        'name = "Jeff Richley"\n'
        'email = "jeff@example.com"\n'
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(_config_text(operator_block))
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_whitespace_only_email_raises(tmp_path: Path):
    """Issue #347 AC bullet 1: email must be non-empty after .strip()."""
    operator_block = (
        "[operator.supervisor]\n"
        'name = "Wren Richley"\n'
        'email = "   "\n'
        "[operator.signer]\n"
        'name = "Jeff Richley"\n'
        'email = "jeff@example.com"\n'
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(_config_text(operator_block))
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_malformed_email_raises(tmp_path: Path):
    """Issue #347: email must match RFC-5321-ish shape regex."""
    operator_block = (
        "[operator.supervisor]\n"
        'name = "Wren Richley"\n'
        'email = "not-an-email"\n'
        "[operator.signer]\n"
        'name = "Jeff Richley"\n'
        'email = "jeff@example.com"\n'
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(_config_text(operator_block))
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_project_operator_override_supervisor_only(tmp_path: Path):
    """Per-project override may set only supervisor; signer inherits."""
    project_extra = (
        "[projects.operator.supervisor]\n"
        'name = "Other Supervisor"\n'
        'email = "other-sup@example.com"\n'
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(_config_text(_OPERATOR_TOML, project_extra=project_extra))
    config = load_config(config_path)
    proj = config.projects[0]
    assert proj.operator is not None
    assert proj.operator.supervisor is not None
    assert proj.operator.supervisor.name == "Other Supervisor"
    assert proj.operator.signer is None


def test_project_operator_override_signer_only(tmp_path: Path):
    """Per-project override may set only signer; supervisor inherits."""
    project_extra = (
        '[projects.operator.signer]\nname = "Other Signer"\nemail = "other-sign@example.com"\n'
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(_config_text(_OPERATOR_TOML, project_extra=project_extra))
    config = load_config(config_path)
    proj = config.projects[0]
    assert proj.operator is not None
    assert proj.operator.signer is not None
    assert proj.operator.signer.name == "Other Signer"
    assert proj.operator.supervisor is None


def test_project_operator_override_both(tmp_path: Path):
    """Per-project override may set both identities."""
    project_extra = (
        "[projects.operator.supervisor]\n"
        'name = "Project Supervisor"\n'
        'email = "psup@example.com"\n'
        "[projects.operator.signer]\n"
        'name = "Project Signer"\n'
        'email = "psign@example.com"\n'
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(_config_text(_OPERATOR_TOML, project_extra=project_extra))
    config = load_config(config_path)
    proj = config.projects[0]
    assert proj.operator is not None
    assert proj.operator.supervisor is not None
    assert proj.operator.supervisor.name == "Project Supervisor"
    assert proj.operator.signer is not None
    assert proj.operator.signer.name == "Project Signer"


def _build_config_with_override(
    override: ProjectOperatorOverride | None,
) -> tuple[ProjectConfig, V4Config]:
    top = OperatorConfig(
        supervisor=OperatorIdentity(name="Top Supervisor", email="topsup@example.com"),
        signer=OperatorIdentity(name="Top Signer", email="topsign@example.com"),
    )
    project = ProjectConfig(
        name="p",
        repo="o/r",
        local_clone_path="/tmp/r",
        operator=override,
    )
    config = V4Config(
        log_dir="/tmp/foreman-logs",
        apps=AppsConfig(
            planner=AppCredentials(app_id=1, private_key_path="/dev/null"),
            reviewer=AppCredentials(app_id=2, private_key_path="/dev/null"),
            fixer=AppCredentials(app_id=3, private_key_path="/dev/null"),
            worker=AppCredentials(app_id=4, private_key_path="/dev/null"),
        ),
        orchestrator=OrchestratorConfig(app_id=5, private_key_path="/dev/null"),
        operator=top,
        storage=StorageConfig(
            engine="postgres",
            dsn="postgresql://foreman:pw@postgres:5432/foreman",
        ),
        projects=[project],
    )
    return project, config


def test_resolve_operator_no_override(tmp_path: Path):
    project, config = _build_config_with_override(None)
    op = resolve_operator(project, config)
    assert op.supervisor.name == "Top Supervisor"
    assert op.signer.name == "Top Signer"


def test_resolve_operator_supervisor_override_only():
    override = ProjectOperatorOverride(
        supervisor=OperatorIdentity(name="Proj Sup", email="psup@example.com"),
    )
    project, config = _build_config_with_override(override)
    op = resolve_operator(project, config)
    assert op.supervisor.name == "Proj Sup"
    assert op.signer.name == "Top Signer"  # inherited from top-level


def test_resolve_operator_signer_override_only():
    override = ProjectOperatorOverride(
        signer=OperatorIdentity(name="Proj Sign", email="psign@example.com"),
    )
    project, config = _build_config_with_override(override)
    op = resolve_operator(project, config)
    assert op.supervisor.name == "Top Supervisor"  # inherited
    assert op.signer.name == "Proj Sign"


def test_resolve_operator_both_overridden():
    override = ProjectOperatorOverride(
        supervisor=OperatorIdentity(name="Proj Sup", email="psup@example.com"),
        signer=OperatorIdentity(name="Proj Sign", email="psign@example.com"),
    )
    project, config = _build_config_with_override(override)
    op = resolve_operator(project, config)
    assert op.supervisor.name == "Proj Sup"
    assert op.signer.name == "Proj Sign"


# ---------------------------------------------------------------------------
# Issue #360: [backup] block — periodic SQLite snapshot scheduler config.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Task 2 (v5): [storage] block — Postgres-only with loud-fail validation.
# ---------------------------------------------------------------------------


def test_storage_unset_dsn_loud_fails():
    """v5 (kill-sqlite): StorageConfig is Postgres-only. Constructing it
    with no ``dsn`` MUST raise — the default engine is now "postgres",
    which requires a dsn. This is Jeff's "always loud fail" directive: a
    misconfigured storage block refuses at validation time rather than
    silently falling back to a non-Postgres engine."""
    with pytest.raises(ValidationError, match="dsn is required"):
        StorageConfig()


def test_storage_defaults_to_postgres_loud_fail_when_section_absent(
    tmp_path: Path,
):
    """v5 (kill-sqlite): A config with no [storage] block no longer keeps
    any historical sqlite behavior — the default engine is "postgres" and
    no dsn is supplied, so loading MUST raise rather than silently fall
    back."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n' + _APPS_TOML_NO_STORAGE + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    with pytest.raises(ValidationError, match="dsn is required"):
        load_config(config_path)


def test_storage_postgres_requires_dsn(tmp_path: Path):
    """Task 2 (v5): engine = "postgres" without a dsn MUST raise. The
    daemon cannot construct a connection pool without a connection string,
    so refusing at load time is correct."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n' + _APPS_TOML_NO_STORAGE + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
        "\n[storage]\n"
        'engine = "postgres"\n'
    )
    with pytest.raises(ValidationError, match="dsn is required when engine"):
        load_config(config_path)


def test_storage_postgres_accepts_dsn_and_pool_sizes(tmp_path: Path):
    """Task 2 (v5): A full postgres storage block round-trips through
    pydantic — engine, dsn, pool_min, pool_max all readable."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n' + _APPS_TOML_NO_STORAGE + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
        "\n[storage]\n"
        'engine = "postgres"\n'
        'dsn = "postgresql://foreman:pw@postgres:5432/foreman"\n'
        "pool_min = 2\n"
        "pool_max = 10\n"
    )
    cfg = load_config(config_path)
    assert cfg.storage.engine == "postgres"
    assert cfg.storage.dsn == "postgresql://foreman:pw@postgres:5432/foreman"
    assert cfg.storage.pool_min == 2
    assert cfg.storage.pool_max == 10


# --- Issue #434: BackupConfig tests ------------------------------------


def test_backup_block_defaulted_when_absent(tmp_path: Path):
    """A TOML without a [backup] block defaults to enabled=True, 3600s interval."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n' + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
    )
    config = load_config(config_path)
    assert config.backup.enabled is True
    assert config.backup.interval_seconds == 3600
    assert config.backup.dir == "/foreman/backups"
    assert config.backup.retention_hourly == 24
    assert config.backup.retention_daily == 7
    assert config.backup.retention_weekly == 4


def test_backup_block_interval_too_small(tmp_path: Path):
    """[backup] with interval_seconds = 30 (< ge=60) raises ValidationError."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n' + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
        "\n[backup]\n"
        "interval_seconds = 30\n"
    )
    with pytest.raises(ValidationError, match="interval_seconds"):
        load_config(config_path)


def test_backup_block_extras_forbidden(tmp_path: Path):
    """[backup] with an unknown key raises ValidationError (extra='forbid')."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[daemon]\n"
        'log_dir = "/tmp/foreman-logs"\n' + _APPS_TOML + "[[projects]]\n"
        'name = "voice"\n'
        'repo = "jeffrichley/voice"\n'
        'local_clone_path = "/tmp/voice"\n'
        "\n[backup]\n"
        "unknown_key = true\n"
    )
    with pytest.raises(ValidationError):
        load_config(config_path)


# ---------------------------------------------------------------------------
# issue #472: ProjectConfig gains per-project max_in_flight cap
# ---------------------------------------------------------------------------


def test_project_config_max_in_flight_defaults_to_one():
    """No per-repo cap set → defaults to 1 (the same-repo serial invariant).
    Every repo is capped whether or not projects.toml mentions it — this is
    what makes a global cap > 1 safe (self-heal arch-review, 2026-07-17)."""
    p = ProjectConfig(name="p", repo="o/r", local_clone_path="/tmp/r")
    assert p.max_in_flight == 1


def test_project_config_max_in_flight_zero_rejected():
    """A cap of 0 would block all tickets — reject at validation time (ge=1)."""
    with pytest.raises(ValidationError):
        ProjectConfig(name="p", repo="o/r", local_clone_path="/tmp/r", max_in_flight=0)


def test_project_config_max_in_flight_one_accepted():
    """1 is the pinned per-repo cap — accepted and stored."""
    p = ProjectConfig(name="p", repo="o/r", local_clone_path="/tmp/r", max_in_flight=1)
    assert p.max_in_flight == 1


def test_project_config_max_in_flight_above_one_accepted():
    """foreman#550: the merge coordinator makes intra-repo concurrency safe,
    so a per-repo cap > 1 is now allowed. Default stays 1 (opt-in)."""
    p = ProjectConfig(name="p", repo="o/r", local_clone_path="/tmp/r", max_in_flight=3)
    assert p.max_in_flight == 3


# ---------------------------------------------------------------------------
# issue #477: load_projects — standalone TOML file for [[projects]]
# ---------------------------------------------------------------------------

from foreman.v4.config import load_projects  # noqa: E402


def test_load_projects_round_trip(tmp_path: Path):
    """load_projects parses a two-project standalone TOML file correctly."""
    projects_path = tmp_path / "projects.toml"
    projects_path.write_text(
        "[[projects]]\n"
        'name = "alpha"\n'
        'repo = "owner/alpha"\n'
        'local_clone_path = "/foreman/repos/alpha"\n'
        "\n[[projects]]\n"
        'name = "beta"\n'
        'repo = "owner/beta"\n'
        'local_clone_path = "/foreman/repos/beta"\n'
    )
    result = load_projects(projects_path)
    assert len(result) == 2
    assert result[0].name == "alpha"
    assert result[0].repo == "owner/alpha"
    assert result[0].local_clone_path == "/foreman/repos/alpha"
    assert result[1].name == "beta"
    assert result[1].repo == "owner/beta"


def test_load_projects_empty(tmp_path: Path):
    """load_projects returns an empty list for a TOML file with no [[projects]]."""
    projects_path = tmp_path / "projects.toml"
    projects_path.write_text("# no projects defined yet\n")
    result = load_projects(projects_path)
    assert result == []


def test_load_projects_missing_required_field_raises(tmp_path: Path):
    """load_projects raises ValidationError when a required field (name) is absent."""
    projects_path = tmp_path / "projects.toml"
    projects_path.write_text(
        "[[projects]]\n"
        'repo = "owner/missing-name"\n'
        'local_clone_path = "/foreman/repos/missing-name"\n'
    )
    with pytest.raises(ValidationError):
        load_projects(projects_path)


def test_load_projects_extra_field_raises(tmp_path: Path):
    """load_projects raises ValidationError for an unrecognised key (extra='forbid')."""
    projects_path = tmp_path / "projects.toml"
    projects_path.write_text(
        "[[projects]]\n"
        'name = "gamma"\n'
        'repo = "owner/gamma"\n'
        'local_clone_path = "/foreman/repos/gamma"\n'
        'unknown_key = "surprise"\n'
    )
    with pytest.raises(ValidationError):
        load_projects(projects_path)


# ---------------------------------------------------------------------------
# foreman#job-sandbox-isolation Task 1: [sandbox] config block
# ---------------------------------------------------------------------------


def test_sandbox_defaults_off_when_block_absent(tmp_path: Path) -> None:
    """A config without a [sandbox] block loads with the sandbox disabled
    and every other field at its documented default — existing operator
    configs continue to run role subprocesses unwrapped."""
    toml = "[daemon]\n" + 'log_dir = "/tmp/logs"\n' + _APPS_TOML
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(toml, encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.sandbox.enabled is False
    assert cfg.sandbox.allow_unsandboxed is False
    assert cfg.sandbox.cache_dir == "/root/.cache/uv"
    assert cfg.sandbox.scratch_root == "/foreman/repos/.scratch"
    assert cfg.sandbox.bwrap_path == "bwrap"


def test_sandbox_block_parses_and_rejects_unknown_key(tmp_path: Path) -> None:
    """A [sandbox] block round-trips its values through load_config, and an
    unrecognised key inside it raises ValidationError (extra='forbid')."""
    toml = (
        "[daemon]\n"
        'log_dir = "/tmp/logs"\n' + _APPS_TOML + "[sandbox]\n"
        "enabled = true\n"
        "allow_unsandboxed = true\n"
        'cache_dir = "/mnt/uv"\n'
        'scratch_root = "/mnt/scratch"\n'
    )
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(toml, encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.sandbox.enabled is True
    assert cfg.sandbox.allow_unsandboxed is True
    assert cfg.sandbox.cache_dir == "/mnt/uv"
    assert cfg.sandbox.scratch_root == "/mnt/scratch"

    bad_toml = "[daemon]\n" + 'log_dir = "/tmp/logs"\n' + _APPS_TOML + "[sandbox]\nbogus = 1\n"
    bad_path = cfg_path.with_name("bad.toml")
    bad_path.write_text(bad_toml, encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(bad_path)


def test_sandbox_config_rejects_extra_field_directly() -> None:
    """SandboxConfig itself sets extra='forbid', independent of the
    TOML-loading path."""
    with pytest.raises(ValidationError):
        SandboxConfig(bogus=1)  # type: ignore[call-arg]


def test_sandbox_scratch_root_defaults_onto_repos_volume() -> None:
    """The per-job scratch defaults under the repos volume so ``git clone --local`` hardlinks.

    A scratch dir on a different filesystem than the base repo makes the
    local clone fail with "Invalid cross-device link" (hardlinks cannot
    cross devices). Co-locating under /foreman/repos guarantees the free clone.
    """
    cfg = SandboxConfig()
    assert cfg.scratch_root == "/foreman/repos/.scratch"


# ---------------------------------------------------------------------------
# issue #590: [inbound] config block — webhook receiver URL
# ---------------------------------------------------------------------------


def test_inbound_block_absent_loads_with_none(tmp_path: Path) -> None:
    """A config without an [inbound] block loads cleanly with inbound=None.

    Backward-compatible: every existing operator config that lacks [inbound]
    continues to load without any change — the field defaults to None.
    """
    toml = "[daemon]\n" + 'log_dir = "/tmp/logs"\n' + _APPS_TOML
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(toml, encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.inbound is None


def test_inbound_block_receiver_url_round_trips(tmp_path: Path) -> None:
    """A config with [inbound].receiver_url populates V4Config.inbound.receiver_url."""
    toml = (
        "[daemon]\n"
        'log_dir = "/tmp/logs"\n' + _APPS_TOML + "[inbound]\n"
        'receiver_url = "https://my.funnel.example.com/webhook"\n'
    )
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(toml, encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.inbound is not None
    assert cfg.inbound.receiver_url == "https://my.funnel.example.com/webhook"
