"""Tests for ``foreman.init.run_init`` — the orchestrator behind
``foreman init``.

Verifies: arg validation, clone-path checking, refuses-without-force on
existing config, idempotent label creation, skips existing instructions
file, best-effort bot verification, correct config block writing,
summary content. Uses a fake admin GitHub client + fake AppsConfig so
the tests don't hit the network.

Phase 8d.9: ported off v3 ``Config`` / ``[projects.<name>]`` shape onto
v4 ``V4Config`` / ``[[projects]]`` shape. The label-set assertion now
pins the v4 ``foreman:state-*`` vocabulary instead of the legacy v3
catalog.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from github import GithubException

from foreman.init import (
    _FOREMAN_LABELS,
    BotVerification,
    InitConfig,
    InitResult,
    _format_project_block,
    _project_block_exists,
    _remote_matches_repo,
    _render_instructions_template,
    _validate_clone_path,
    _validate_repo_slug,
    _write_project_block_to_config,
    detect_matching_clone,
    run_init,
)
from foreman.v4.config import AppsConfig

# ----------------------------------------------------------------------
# Fake PyGithub admin surface
# ----------------------------------------------------------------------


class _FakeLabel:
    def __init__(self, name: str, color: str = "ffffff", description: str = "") -> None:
        self.name = name
        self.color = color
        self.description = description


class _FakeRepo:
    """In-memory stand-in for PyGithub's ``Repository`` (label surface only)."""

    def __init__(
        self,
        *,
        slug: str,
        existing_labels: list[_FakeLabel] | None = None,
        raise_on_create: dict[str, GithubException] | None = None,
    ) -> None:
        self.full_name = slug
        self._labels: list[_FakeLabel] = list(existing_labels or [])
        self._raise_on_create = raise_on_create or {}
        self.create_label_calls: list[dict[str, str]] = []

    def get_labels(self) -> list[_FakeLabel]:
        return list(self._labels)

    def create_label(self, *, name: str, color: str, description: str = "") -> _FakeLabel:
        self.create_label_calls.append({"name": name, "color": color, "description": description})
        if name in self._raise_on_create:
            raise self._raise_on_create[name]
        label = _FakeLabel(name=name, color=color, description=description)
        self._labels.append(label)
        return label


class _FakeAdminClient:
    def __init__(self, *, repo: _FakeRepo) -> None:
        self._repo = repo
        self.get_repo_calls: list[str] = []

    def get_repo(self, slug: str) -> _FakeRepo:
        self.get_repo_calls.append(slug)
        return self._repo


# ----------------------------------------------------------------------
# Helpers — seed a clone with an origin remote pointing at owner/repo
# ----------------------------------------------------------------------


def _seed_clone_with_origin(clone: Path, *, repo_slug: str) -> None:
    """Init a minimal git repo at ``clone`` with a fake origin pointing
    at ``https://github.com/<repo_slug>``.

    The remote URL doesn't need to be reachable — the init validator
    only does a string check. Tests pass a synthetic URL here so they
    never touch the network.
    """
    clone.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=clone, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=clone, check=True, capture_output=True
    )
    (clone / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=clone, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=clone, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", f"https://github.com/{repo_slug}.git"],
        cwd=clone,
        check=True,
        capture_output=True,
    )


# ----------------------------------------------------------------------
# Arg validation
# ----------------------------------------------------------------------


def test_validate_repo_slug_extracts_owner_and_name() -> None:
    owner, repo = _validate_repo_slug("jeffrichley/foreman")
    assert owner == "jeffrichley"
    assert repo == "foreman"


def test_validate_repo_slug_rejects_missing_slash() -> None:
    with pytest.raises(ValueError, match="owner/repo"):
        _validate_repo_slug("foreman")


def test_validate_repo_slug_rejects_extra_segments() -> None:
    with pytest.raises(ValueError, match="owner/repo"):
        _validate_repo_slug("jeffrichley/foreman/extra")


def test_validate_repo_slug_rejects_leading_dot() -> None:
    """GitHub repo names can't start with a dot — pattern requires
    alphanumeric leading char."""
    with pytest.raises(ValueError, match="owner/repo"):
        _validate_repo_slug(".hidden/foreman")


def test_validate_clone_path_accepts_matching_origin(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    _seed_clone_with_origin(clone, repo_slug="jeffrichley/foreman")
    _validate_clone_path(clone, "jeffrichley/foreman")  # no raise


def test_validate_clone_path_rejects_missing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        _validate_clone_path(tmp_path / "does-not-exist", "jeffrichley/foreman")


def test_validate_clone_path_rejects_non_git(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    with pytest.raises(ValueError, match="not a git repository"):
        _validate_clone_path(clone, "jeffrichley/foreman")


def test_validate_clone_path_rejects_mismatched_remote(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    _seed_clone_with_origin(clone, repo_slug="someone-else/other-repo")
    with pytest.raises(ValueError, match="does not match the target repo"):
        _validate_clone_path(clone, "jeffrichley/foreman")


# ----------------------------------------------------------------------
# Remote URL matcher — accept both HTTPS and SSH shapes
# ----------------------------------------------------------------------


def test_remote_matches_repo_accepts_https() -> None:
    assert _remote_matches_repo("https://github.com/jeffrichley/foreman", "jeffrichley/foreman")


def test_remote_matches_repo_accepts_https_with_dot_git() -> None:
    assert _remote_matches_repo("https://github.com/jeffrichley/foreman.git", "jeffrichley/foreman")


def test_remote_matches_repo_accepts_scp_style_ssh() -> None:
    assert _remote_matches_repo("git@github.com:jeffrichley/foreman.git", "jeffrichley/foreman")


def test_remote_matches_repo_accepts_ssh_url() -> None:
    assert _remote_matches_repo(
        "ssh://git@github.com/jeffrichley/foreman.git", "jeffrichley/foreman"
    )


def test_remote_matches_repo_case_insensitive() -> None:
    """GitHub URLs are case-insensitive — be lenient on matching."""
    assert _remote_matches_repo("https://github.com/JeffRichley/Foreman", "jeffrichley/foreman")


def test_remote_matches_repo_rejects_mismatch() -> None:
    assert not _remote_matches_repo("https://github.com/other/repo", "jeffrichley/foreman")


def test_remote_matches_repo_rejects_empty() -> None:
    assert not _remote_matches_repo("", "jeffrichley/foreman")


# ----------------------------------------------------------------------
# detect_matching_clone — used by CLI to default --clone-path
# ----------------------------------------------------------------------


def test_detect_matching_clone_returns_cwd_when_remote_matches(
    tmp_path: Path,
) -> None:
    clone = tmp_path / "clone"
    _seed_clone_with_origin(clone, repo_slug="jeffrichley/foreman")
    assert detect_matching_clone(clone, "jeffrichley/foreman") == clone


def test_detect_matching_clone_returns_none_when_remote_mismatches(
    tmp_path: Path,
) -> None:
    clone = tmp_path / "clone"
    _seed_clone_with_origin(clone, repo_slug="other/repo")
    assert detect_matching_clone(clone, "jeffrichley/foreman") is None


def test_detect_matching_clone_returns_none_for_non_repo(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()
    assert detect_matching_clone(not_a_repo, "jeffrichley/foreman") is None


# ----------------------------------------------------------------------
# Project block formatting + config writing
# ----------------------------------------------------------------------


def test_format_project_block_default_check_command_omitted(tmp_path: Path) -> None:
    """When ``check_command`` is the default (``just check``), it is NOT
    emitted in the block — Worker resolves None to that default."""
    clone = tmp_path / "clone"
    clone.mkdir()
    block = _format_project_block(
        name="foreman",
        repo="jeffrichley/foreman",
        clone_path=clone,
        check_command="just check",
    )
    assert "[[projects]]" in block
    assert 'name = "foreman"' in block
    assert 'repo = "jeffrichley/foreman"' in block
    assert "check_command" not in block  # default → omitted


def test_format_project_block_non_default_check_command_emitted(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    block = _format_project_block(
        name="myproj",
        repo="me/myproj",
        clone_path=clone,
        check_command="make test",
    )
    assert 'check_command = "make test"' in block


def test_format_project_block_uses_posix_path(tmp_path: Path) -> None:
    """Backslashes must be normalized to forward slashes so the TOML is
    portable across Windows + Unix."""
    clone = tmp_path / "clone"
    clone.mkdir()
    block = _format_project_block(
        name="x", repo="a/x", clone_path=clone, check_command="just check"
    )
    assert "\\" not in block  # no Windows-style separators


def test_write_project_block_creates_config_when_absent(tmp_path: Path) -> None:
    """When the config file doesn't exist, init writes a full v4
    skeleton (``[daemon]`` / ``[apps.*]`` / ``[orchestrator]``) plus
    the project block — so the file is daemon-loadable as a schema."""
    cfg = tmp_path / "config.toml"
    block = '[[projects]]\nname = "foreman"\nrepo = "jeffrichley/foreman"\n'
    _write_project_block_to_config(config_path=cfg, block_text=block, name="foreman", force=False)
    contents = cfg.read_text(encoding="utf-8")
    # Skeleton blocks are present.
    assert "[daemon]" in contents
    assert "[apps.planner]" in contents
    assert "[apps.reviewer]" in contents
    assert "[apps.fixer]" in contents
    assert "[apps.worker]" in contents
    assert "[orchestrator]" in contents
    # And the project block landed at the tail.
    assert block in contents


def test_write_project_block_appends_to_existing_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[[projects]]\nname = "voice"\nrepo = "jeffrichley/voice"\n',
        encoding="utf-8",
    )
    block = '[[projects]]\nname = "foreman"\nrepo = "jeffrichley/foreman"\n'
    _write_project_block_to_config(config_path=cfg, block_text=block, name="foreman", force=False)
    contents = cfg.read_text(encoding="utf-8")
    # Existing block survives unchanged.
    assert 'name = "voice"' in contents
    assert 'repo = "jeffrichley/voice"' in contents
    # New block appended.
    assert 'name = "foreman"' in contents
    assert 'repo = "jeffrichley/foreman"' in contents


def test_write_project_block_replaces_when_force(tmp_path: Path) -> None:
    """Force overwrite: the matching ``[[projects]]`` block is replaced;
    siblings stay put."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        (
            '[[projects]]\nname = "voice"\nrepo = "jeffrichley/voice"\n\n'
            '[[projects]]\nname = "foreman"\nrepo = "OLD_VALUE/foreman"\n'
            'local_clone_path = "/tmp/old"\n'
        ),
        encoding="utf-8",
    )
    new_block = (
        '[[projects]]\nname = "foreman"\nrepo = "jeffrichley/foreman"\n'
        'local_clone_path = "/new/path"\n'
    )
    _write_project_block_to_config(
        config_path=cfg, block_text=new_block, name="foreman", force=True
    )
    contents = cfg.read_text(encoding="utf-8")
    assert "OLD_VALUE" not in contents
    assert "/tmp/old" not in contents
    assert "/new/path" in contents
    # Sibling project block untouched.
    assert 'repo = "jeffrichley/voice"' in contents


def test_write_project_block_preserves_apps_block_on_force(tmp_path: Path) -> None:
    """The top-level ``[apps.<role>]`` / ``[orchestrator]`` blocks are
    operator-curated; a --force overwrite of one ``[[projects]]`` block
    MUST leave them intact."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        (
            "[apps.planner]\n"
            "app_id = 123456\n"
            'private_key_path = "/keys/planner.pem"\n\n'
            "[orchestrator]\n"
            "app_id = 99999\n"
            'private_key_path = "/keys/orchestrator.pem"\n\n'
            '[[projects]]\nname = "foreman"\nrepo = "OLD/foreman"\n'
            'local_clone_path = "/tmp/old"\n'
        ),
        encoding="utf-8",
    )
    new_block = (
        '[[projects]]\nname = "foreman"\nrepo = "jeffrichley/foreman"\nlocal_clone_path = "/new"\n'
    )
    _write_project_block_to_config(
        config_path=cfg, block_text=new_block, name="foreman", force=True
    )
    contents = cfg.read_text(encoding="utf-8")
    # apps + orchestrator blocks survive.
    assert "[apps.planner]" in contents
    assert "app_id = 123456" in contents
    assert "[orchestrator]" in contents
    assert "app_id = 99999" in contents
    # Project block was updated.
    assert 'repo = "jeffrichley/foreman"' in contents
    assert "OLD/foreman" not in contents


def test_project_block_exists_true_after_write(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[[projects]]\nname = "foreman"\nrepo = "jeffrichley/foreman"\n',
        encoding="utf-8",
    )
    assert _project_block_exists(cfg, "foreman") is True


def test_project_block_exists_false_for_other_project(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[[projects]]\nname = "voice"\nrepo = "jeffrichley/voice"\n',
        encoding="utf-8",
    )
    assert _project_block_exists(cfg, "foreman") is False


def test_project_block_exists_false_when_config_missing(tmp_path: Path) -> None:
    assert _project_block_exists(tmp_path / "no-config.toml", "foreman") is False


# ----------------------------------------------------------------------
# run_init — orchestrator end-to-end
# ----------------------------------------------------------------------


def _make_init_config(
    *,
    tmp_path: Path,
    repo: str = "jeffrichley/foreman",
    name: str = "foreman",
    check_command: str = "just check",
    force: bool = False,
) -> tuple[InitConfig, Path]:
    """Build a fully-validated InitConfig + return the seeded clone Path."""
    clone = tmp_path / "clone"
    _seed_clone_with_origin(clone, repo_slug=repo)
    cfg_path = tmp_path / "config.toml"
    init_config = InitConfig(
        repo=repo,
        name=name,
        clone_path=clone,
        check_command=check_command,
        force=force,
        config_path=cfg_path,
    )
    return init_config, clone


def _v4_skeleton_config() -> str:
    """Return a minimum-valid V4Config TOML — every required block,
    placeholder values. Used by tests that need ``_load_config_or_empty``
    to return a usable :class:`V4Config` so ``_verify_bot_installation``
    actually runs against an apps shape."""
    return (
        "[daemon]\n"
        'log_dir = "/tmp/logs"\n\n'
        "[storage]\n"
        'engine = "postgres"\n'
        'dsn = "postgresql://test/test"\n\n'
        "[apps.planner]\n"
        "app_id = 12345\n"
        'private_key_path = "/keys/planner.pem"\n\n'
        "[apps.reviewer]\n"
        "app_id = 12345\n"
        'private_key_path = "/keys/reviewer.pem"\n\n'
        "[apps.fixer]\n"
        "app_id = 12345\n"
        'private_key_path = "/keys/fixer.pem"\n\n'
        "[apps.worker]\n"
        "app_id = 12345\n"
        'private_key_path = "/keys/worker.pem"\n\n'
        "[orchestrator]\n"
        "app_id = 99999\n"
        'private_key_path = "/keys/orchestrator.pem"\n\n'
        "[operator.supervisor]\n"
        'name = "Test Sup"\n'
        'email = "sup@example.com"\n\n'
        "[operator.signer]\n"
        'name = "Test Sign"\n'
        'email = "sign@example.com"\n\n'
    )


def test_run_init_writes_config_block(tmp_path: Path) -> None:
    init_config, _clone = _make_init_config(tmp_path=tmp_path)
    fake_repo = _FakeRepo(slug=init_config.repo)
    admin = _FakeAdminClient(repo=fake_repo)

    result = run_init(init_config, admin_client=admin)

    contents = init_config.config_path.read_text(encoding="utf-8")
    assert "[[projects]]" in contents
    assert 'name = "foreman"' in contents
    assert 'repo = "jeffrichley/foreman"' in contents
    # Brand-new config gets the full v4 skeleton.
    assert "[daemon]" in contents
    assert "[apps.planner]" in contents
    assert "[orchestrator]" in contents
    assert result.config_path == init_config.config_path


def test_run_init_writes_instructions_template(tmp_path: Path) -> None:
    init_config, clone = _make_init_config(tmp_path=tmp_path)
    fake_repo = _FakeRepo(slug=init_config.repo)
    admin = _FakeAdminClient(repo=fake_repo)

    result = run_init(init_config, admin_client=admin)

    inst = clone / ".foreman" / "INSTRUCTIONS.md"
    assert inst.exists()
    assert result.instructions_written is True
    body = inst.read_text(encoding="utf-8")
    # The repo name + check_command substitutions landed.
    assert "Foreman instructions for foreman" in body
    assert "just check" in body


def test_run_init_skips_instructions_when_file_exists(tmp_path: Path) -> None:
    """Existing instructions are operator-curated; init must not
    overwrite them even with --force."""
    init_config, clone = _make_init_config(tmp_path=tmp_path, force=True)
    foreman_dir = clone / ".foreman"
    foreman_dir.mkdir()
    custom = "# operator-customized — do not overwrite\n"
    (foreman_dir / "INSTRUCTIONS.md").write_text(custom, encoding="utf-8")

    fake_repo = _FakeRepo(slug=init_config.repo)
    admin = _FakeAdminClient(repo=fake_repo)

    result = run_init(init_config, admin_client=admin)

    body = (clone / ".foreman" / "INSTRUCTIONS.md").read_text(encoding="utf-8")
    assert body == custom
    assert result.instructions_written is False


def test_run_init_creates_all_v4_labels_on_empty_repo(tmp_path: Path) -> None:
    """Phase 8d.9: ``foreman init`` writes the v4 label vocabulary —
    the ``foreman:plan`` trigger plus one ``foreman:state-*`` label per
    :data:`STATE_REGISTRY` entry. Pinning the exact set here protects
    the catalog from silently drifting back to v3 or growing a label
    the observer doesn't actually stamp."""
    init_config, _clone = _make_init_config(tmp_path=tmp_path)
    fake_repo = _FakeRepo(slug=init_config.repo)
    admin = _FakeAdminClient(repo=fake_repo)

    result = run_init(init_config, admin_client=admin)

    expected_names = [str(name) for name, _color, _desc in _FOREMAN_LABELS]
    assert len(result.labels_created) == len(expected_names)
    assert len(result.labels_existing) == 0
    created_names = [c["name"] for c in fake_repo.create_label_calls]
    assert sorted(created_names) == sorted(expected_names)
    # Pin the v4 vocabulary explicitly. The state-* labels are derived
    # from :data:`foreman.v4.states.registry.STATE_REGISTRY`; the
    # trigger label stays on the typed ``foreman.labels.Label`` enum.
    assert set(expected_names) == {
        "foreman:plan",
        "foreman:state-queued",
        "foreman:state-planning",
        "foreman:state-spec-review",
        "foreman:state-spec-fix",
        # foreman#416: SpecMerging merges the approved spec PR with the
        # self-heal framework (moved out of SpecReviewState.verify).
        "foreman:state-spec-merging",
        "foreman:state-implementing",
        "foreman:state-impl-review",
        "foreman:state-impl-fix",
        # foreman#418: parked state for an approved impl PR awaiting
        # human merge (when auto_merge_impl is False).
        "foreman:state-impl-approved",
        "foreman:state-merging",
        # foreman#550: parked state for a PR handed off to the per-repo
        # merge coordinator (Merging/SpecMerging enqueue → MergeQueued).
        "foreman:state-merge-queued",
        "foreman:state-done",
        "foreman:state-failed",
        "foreman:state-needs-help",
    }


def test_run_init_skips_existing_labels(tmp_path: Path) -> None:
    """Existing labels are left untouched — no update to color/description."""
    init_config, _clone = _make_init_config(tmp_path=tmp_path)
    fake_repo = _FakeRepo(
        slug=init_config.repo,
        existing_labels=[
            _FakeLabel(
                name="foreman:state-planning",
                color="CCCCCC",
                description="Custom operator description",
            ),
            _FakeLabel(name="foreman:state-done"),
        ],
    )
    admin = _FakeAdminClient(repo=fake_repo)

    result = run_init(init_config, admin_client=admin)

    total = len(_FOREMAN_LABELS)
    assert len(result.labels_created) == total - 2
    assert sorted(result.labels_existing) == ["foreman:state-done", "foreman:state-planning"]
    # The pre-existing label's color/description was NOT overwritten.
    plan_label = next(lbl for lbl in fake_repo._labels if lbl.name == "foreman:state-planning")
    assert plan_label.color == "CCCCCC"
    assert plan_label.description == "Custom operator description"


def test_run_init_treats_422_create_as_existing(tmp_path: Path) -> None:
    """422 on create_label means race-with-create — treat as already
    existed rather than failing the run."""
    init_config, _clone = _make_init_config(tmp_path=tmp_path)
    fake_repo = _FakeRepo(
        slug=init_config.repo,
        raise_on_create={
            "foreman:state-done": GithubException(
                status=422, data={"message": "Validation Failed"}, headers=None
            )
        },
    )
    admin = _FakeAdminClient(repo=fake_repo)

    result = run_init(init_config, admin_client=admin)

    assert "foreman:state-done" in result.labels_existing
    assert "foreman:state-done" not in result.labels_created


def test_run_init_refuses_overwrite_without_force(tmp_path: Path) -> None:
    init_config, _clone = _make_init_config(tmp_path=tmp_path)
    init_config.config_path.write_text(
        '[[projects]]\nname = "foreman"\nrepo = "someone/else"\n',
        encoding="utf-8",
    )
    fake_repo = _FakeRepo(slug=init_config.repo)
    admin = _FakeAdminClient(repo=fake_repo)

    with pytest.raises(FileExistsError, match="already configured"):
        run_init(init_config, admin_client=admin)

    # No labels created — refusal happens before any side effects.
    assert fake_repo.create_label_calls == []


def test_run_init_overwrites_with_force(tmp_path: Path) -> None:
    init_config, _clone = _make_init_config(tmp_path=tmp_path, force=True)
    init_config.config_path.write_text(
        '[[projects]]\nname = "foreman"\nrepo = "someone/else"\nlocal_clone_path = "/tmp/old"\n',
        encoding="utf-8",
    )
    fake_repo = _FakeRepo(slug=init_config.repo)
    admin = _FakeAdminClient(repo=fake_repo)

    run_init(init_config, admin_client=admin)

    contents = init_config.config_path.read_text(encoding="utf-8")
    assert 'repo = "jeffrichley/foreman"' in contents
    assert "someone/else" not in contents


def test_run_init_rejects_invalid_repo_slug(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    _seed_clone_with_origin(clone, repo_slug="jeffrichley/foreman")
    init_config = InitConfig(
        repo="not-a-slug",
        name="x",
        clone_path=clone,
        check_command="just check",
        force=False,
        config_path=tmp_path / "config.toml",
    )
    fake_repo = _FakeRepo(slug="not-a-slug")
    admin = _FakeAdminClient(repo=fake_repo)

    with pytest.raises(ValueError, match="owner/repo"):
        run_init(init_config, admin_client=admin)


def test_run_init_rejects_mismatched_clone_remote(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    _seed_clone_with_origin(clone, repo_slug="other/repo")
    init_config = InitConfig(
        repo="jeffrichley/foreman",
        name="foreman",
        clone_path=clone,
        check_command="just check",
        force=False,
        config_path=tmp_path / "config.toml",
    )
    fake_repo = _FakeRepo(slug="jeffrichley/foreman")
    admin = _FakeAdminClient(repo=fake_repo)

    with pytest.raises(ValueError, match="does not match"):
        run_init(init_config, admin_client=admin)


def test_run_init_bot_verification_skips_when_apps_unconfigured(
    tmp_path: Path,
) -> None:
    """No app IDs in config → each role's verification is 'skipped'
    rather than 'failed' — operator may set up apps later."""
    init_config, _clone = _make_init_config(tmp_path=tmp_path)
    fake_repo = _FakeRepo(slug=init_config.repo)
    admin = _FakeAdminClient(repo=fake_repo)

    result = run_init(init_config, admin_client=admin)

    assert len(result.bot_verifications) == 4
    roles_seen = {v.role for v in result.bot_verifications}
    assert roles_seen == {"planner", "reviewer", "fixer", "worker"}
    for v in result.bot_verifications:
        assert v.ok is False
        assert v.detail.startswith("skipped: ")


def test_run_init_bot_verification_records_failure_without_aborting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best-effort: a failed verification records the error but init
    still completes (labels created, config written)."""
    init_config, _clone = _make_init_config(tmp_path=tmp_path)
    fake_repo = _FakeRepo(slug=init_config.repo)
    admin = _FakeAdminClient(repo=fake_repo)

    # Seed an existing config so `_verify_bot_installation` is reached
    # (without v4 apps on disk, run_init reports every role as skipped
    # without consulting the verifier at all).
    init_config.config_path.parent.mkdir(parents=True, exist_ok=True)
    init_config.config_path.write_text(_v4_skeleton_config(), encoding="utf-8")

    # Stub _verify_bot_installation to simulate one fail + three skip.
    from foreman import init as init_mod

    def fake_verify(*, role: str, apps: AppsConfig, repo_slug: str) -> BotVerification:
        if role == "planner":
            return BotVerification(
                role=role,
                ok=False,
                detail="RuntimeError: installation not found",
            )
        return BotVerification(role=role, ok=False, detail="skipped: no app id")

    monkeypatch.setattr(init_mod, "_verify_bot_installation", fake_verify)

    result = run_init(init_config, admin_client=admin)

    planner = next(v for v in result.bot_verifications if v.role == "planner")
    assert planner.ok is False
    assert "installation not found" in planner.detail
    # Config still written + labels still created
    assert init_config.config_path.exists()
    assert len(result.labels_created) == len(_FOREMAN_LABELS)


def test_run_init_summary_contains_expected_fields(tmp_path: Path) -> None:
    init_config, _clone = _make_init_config(tmp_path=tmp_path)
    fake_repo = _FakeRepo(slug=init_config.repo)
    admin = _FakeAdminClient(repo=fake_repo)

    result = run_init(init_config, admin_client=admin)

    summary = result.summary
    assert "jeffrichley/foreman" in summary
    # v4: the summary names the [[projects]] block with the project's
    # ``name`` key.
    assert "[[projects]]" in summary
    assert "'foreman'" in summary
    assert "INSTRUCTIONS.md" in summary
    assert f"{len(_FOREMAN_LABELS)} labels" in summary
    assert "Next steps" in summary
    # v4: the next-steps prompt points operators at the v4 trigger
    # label (``foreman:plan``) + the daemon-start command, not the
    # legacy v3 ``foreman plan <url>`` invocation.
    assert "foreman:plan" in summary
    assert "foreman daemon start" in summary


def test_run_init_summary_notes_existing_instructions(tmp_path: Path) -> None:
    """When the instructions file already existed, the summary calls it
    out so the operator knows their content wasn't overwritten."""
    init_config, clone = _make_init_config(tmp_path=tmp_path)
    (clone / ".foreman").mkdir()
    (clone / ".foreman" / "INSTRUCTIONS.md").write_text("custom\n")
    fake_repo = _FakeRepo(slug=init_config.repo)
    admin = _FakeAdminClient(repo=fake_repo)

    result = run_init(init_config, admin_client=admin)

    assert "existing file preserved" in result.summary


def test_run_init_uses_check_command_override_in_template(
    tmp_path: Path,
) -> None:
    """The rendered instructions template substitutes the
    ``--check-command`` value into the quality-gate section."""
    init_config, clone = _make_init_config(tmp_path=tmp_path, check_command="make test")
    fake_repo = _FakeRepo(slug=init_config.repo)
    admin = _FakeAdminClient(repo=fake_repo)

    run_init(init_config, admin_client=admin)

    body = (clone / ".foreman" / "INSTRUCTIONS.md").read_text(encoding="utf-8")
    assert "make test" in body
    # And the placeholder was substituted, not left in place.
    assert "<configured_check_command>" not in body


def test_run_init_uses_check_command_override_in_config(tmp_path: Path) -> None:
    """Non-default ``check_command`` lands in the config block too."""
    init_config, _clone = _make_init_config(tmp_path=tmp_path, check_command="make test")
    fake_repo = _FakeRepo(slug=init_config.repo)
    admin = _FakeAdminClient(repo=fake_repo)

    run_init(init_config, admin_client=admin)

    contents = init_config.config_path.read_text(encoding="utf-8")
    assert 'check_command = "make test"' in contents


def test_run_init_returns_init_result_with_typed_fields(tmp_path: Path) -> None:
    """The orchestrator returns a structured result — CLI prints
    ``summary``; tests assert against the typed fields."""
    init_config, clone = _make_init_config(tmp_path=tmp_path)
    fake_repo = _FakeRepo(slug=init_config.repo)
    admin = _FakeAdminClient(repo=fake_repo)

    result = run_init(init_config, admin_client=admin)

    assert isinstance(result, InitResult)
    assert result.repo == "jeffrichley/foreman"
    assert result.name == "foreman"
    assert result.clone_path == clone
    assert result.instructions_path == clone / ".foreman" / "INSTRUCTIONS.md"
    assert isinstance(result.bot_verifications, list)
    assert len(result.bot_verifications) == 4


# ----------------------------------------------------------------------
# Instructions template — timestamp-stability invariant
# ----------------------------------------------------------------------


def test_run_init_warns_when_instructions_uncommitted(tmp_path: Path) -> None:
    """Fresh init on a seeded clone leaves ``.foreman/INSTRUCTIONS.md``
    as an untracked file. The warning must surface a copy-pasteable
    ``git add ... && git commit ...`` command, and the summary must
    render that command in the visible output."""
    init_config, clone = _make_init_config(tmp_path=tmp_path)
    fake_repo = _FakeRepo(slug=init_config.repo)
    admin = _FakeAdminClient(repo=fake_repo)

    result = run_init(init_config, admin_client=admin)

    # File was written but not committed → warning must be set.
    assert result.instructions_dirty_warning is not None
    # The command shape matches the acceptance criterion exactly.
    assert "git -C" in result.instructions_dirty_warning
    assert str(clone) in result.instructions_dirty_warning
    assert "add .foreman/INSTRUCTIONS.md" in result.instructions_dirty_warning
    assert "&&" in result.instructions_dirty_warning
    assert 'commit -m "chore: commit .foreman/INSTRUCTIONS.md"' in result.instructions_dirty_warning
    # And the rendered summary surfaces it.
    assert "Warning:" in result.summary
    assert result.instructions_dirty_warning in result.summary


def test_run_init_no_warning_when_instructions_committed(tmp_path: Path) -> None:
    """When ``.foreman/INSTRUCTIONS.md`` is already committed in the
    clone (and the file-exists short-circuit applies), the
    dirty-warning field is None and the summary contains no
    ``Warning:`` block."""
    init_config, clone = _make_init_config(tmp_path=tmp_path)
    foreman_dir = clone / ".foreman"
    foreman_dir.mkdir()
    (foreman_dir / "INSTRUCTIONS.md").write_text("# preexisting\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".foreman/INSTRUCTIONS.md"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "pre-commit instructions"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    fake_repo = _FakeRepo(slug=init_config.repo)
    admin = _FakeAdminClient(repo=fake_repo)

    result = run_init(init_config, admin_client=admin)

    assert result.instructions_dirty_warning is None
    assert "Warning:" not in result.summary


def test_run_init_no_warning_when_git_status_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subprocess failure during the porcelain check must not raise
    out of ``run_init``: the warning is suppressed (``None``) and init
    completes normally."""
    init_config, _clone = _make_init_config(tmp_path=tmp_path)
    fake_repo = _FakeRepo(slug=init_config.repo)
    admin = _FakeAdminClient(repo=fake_repo)

    from foreman import init as init_mod

    real_run = subprocess.run

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        # Only intercept the porcelain call. Other subprocess.run calls
        # (e.g. inside ``_validate_clone_path``) must keep working so
        # init can still reach the helper.
        argv = args[0] if args else kwargs.get("args", [])
        if (
            isinstance(argv, list)
            and len(argv) >= 3
            and argv[0] == "git"
            and argv[1] == "status"
            and argv[2] == "--porcelain"
        ):
            raise OSError("simulated git failure")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(init_mod.subprocess, "run", fake_run)

    result = run_init(init_config, admin_client=admin)

    # Init succeeded (no exception propagated) and no warning surfaced.
    assert result.instructions_dirty_warning is None
    assert "Warning:" not in result.summary


def test_template_render_is_timestamp_stable() -> None:
    """Rendering the instructions template twice with the same inputs
    must produce byte-identical output, and the template body must not
    contain any time-volatile placeholder markers.

    This pins the invariant that protects ``foreman init`` from
    daily "file changed" noise on the clean-tree gate: the template
    body has no timestamp / generated-at / now placeholders, so the
    rendered ``.foreman/INSTRUCTIONS.md`` is reproducible from
    ``(repo_name, check_command)`` alone.
    """
    first = _render_instructions_template("foreman", "just check")
    second = _render_instructions_template("foreman", "just check")
    assert first == second

    forbidden_markers = (
        "{timestamp}",
        "<timestamp>",
        "<date>",
        "<generated_at>",
        "{now}",
    )
    for marker in forbidden_markers:
        assert marker not in first, (
            f"Template contains time-volatile marker {marker!r}; "
            "rendered output will drift across runs."
        )


# ---------------------------------------------------------------------------
# Phase 8d.9: drift detection between the init catalog and the v4 state
# machine. Init owns the v4 label-creation metadata (color + description);
# :data:`foreman.v4.states.registry.STATE_REGISTRY` owns the state names.
# The two must enumerate the same set so an operator running ``foreman
# init`` ends up with one ``foreman:state-*`` label per state the
# observer might stamp.
# ---------------------------------------------------------------------------


def test_init_foreman_labels_covers_every_v4_state() -> None:
    """Every entry in :data:`STATE_REGISTRY` must have a matching
    ``foreman:state-<kebab>`` label in the init catalog.

    Drift detection: adding a new state to the registry without
    teaching init how to create the label would leave the observer
    trying to stamp a label the repo doesn't have. The 422 fallback in
    ``_ensure_labels`` would mask the create-as-needed, but the
    observer's ``add_to_labels`` would fail in production.
    """
    from foreman.v4.states.registry import STATE_REGISTRY

    init_names = {str(entry[0]) for entry in _FOREMAN_LABELS}
    expected_state_labels = {f"foreman:state-{_kebab(name)}" for name in STATE_REGISTRY}
    missing = expected_state_labels - init_names
    assert not missing, f"States in registry without an init-time label: {missing}"


def _kebab(state_name: str) -> str:
    """Test helper: mirror init's ``_state_label_name`` kebab transform."""
    import re

    return re.sub(r"(?<!^)([A-Z])", r"-\1", state_name).lower()


def test_init_foreman_labels_includes_trigger_label() -> None:
    """The ``foreman:plan`` trigger label must always be in the catalog.
    Without it an operator who runs ``foreman init`` then labels an
    issue with ``foreman:plan`` gets a 'label does not exist' GitHub
    API error."""
    from foreman.labels import Label

    init_names = {str(entry[0]) for entry in _FOREMAN_LABELS}
    assert Label.PLAN.value in init_names


def test_init_foreman_labels_has_no_duplicate_names() -> None:
    """Each label name must appear at most once in the init catalog. A
    duplicate would cause ``_ensure_labels`` to attempt to create the
    same label twice on a fresh repo, and the second create would 422
    — currently silently treated as 'already existed.' Better to catch
    duplicates in the catalog up front."""
    names = [str(entry[0]) for entry in _FOREMAN_LABELS]
    assert len(names) == len(set(names)), (
        f"Duplicate label in _FOREMAN_LABELS: {[n for n in names if names.count(n) > 1]}"
    )
