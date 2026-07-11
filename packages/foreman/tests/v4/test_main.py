"""main() console-script entry point — config-load + identity-wire smoke.

The full ``foreman ...`` boot path runs through ``main()``: load the
TOML, build the V4IdentityRegistry, bootstrap the CLI context, hand off
to typer. This test exercises the construction half end-to-end (no
real subprocess, no real GitHub round-trip) and proves typer exits 0
on ``--help`` even with dummy PEM files — :func:`mint_installation_token`
is patched at the v4.identity import site so no real RSA parsing fires.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from foreman.auth import InstallationToken
from foreman.v4.repository import InMemoryTicketRepository


def _write_valid_config(tmp_path: Path) -> Path:
    """Write a minimal-valid config.toml (no ``[[projects]]``) + dummy PEMs.

    Post-#477 the ``[[projects]]`` tables live in the host-mounted projects
    file, so this config carries every required identity/operator/storage
    block but zero projects — the shape the startup-guard tests need so
    ``main()`` reaches ``load_projects`` (rather than failing earlier in
    ``load_config``). Returns the config.toml path.
    """
    log_dir = tmp_path / "logs"
    for role in ("planner", "reviewer", "fixer", "worker", "orchestrator"):
        (tmp_path / f"{role}.pem").write_text("dummy")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[daemon]
log_dir = "{log_dir.as_posix()}"

[storage]
engine = "postgres"
dsn = "postgresql://test/test"

[apps.planner]
app_id = 12345
private_key_path = "{(tmp_path / "planner.pem").as_posix()}"

[apps.reviewer]
app_id = 12346
private_key_path = "{(tmp_path / "reviewer.pem").as_posix()}"

[apps.fixer]
app_id = 12347
private_key_path = "{(tmp_path / "fixer.pem").as_posix()}"

[apps.worker]
app_id = 12348
private_key_path = "{(tmp_path / "worker.pem").as_posix()}"

[orchestrator]
app_id = 12349
private_key_path = "{(tmp_path / "orchestrator.pem").as_posix()}"

[operator.supervisor]
name = "Test Sup"
email = "sup@example.com"

[operator.signer]
name = "Test Sign"
email = "sign@example.com"
""",
        encoding="utf-8",
    )
    return config_path


def test_main_missing_projects_file_exits_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """issue #503 FIX 2: a missing projects file makes ``main()`` exit
    non-zero with a clean operator-facing message, NOT a raw
    ``FileNotFoundError`` traceback."""
    import typer

    from foreman.v4.cli import main

    config_path = _write_valid_config(tmp_path)
    monkeypatch.setenv("FOREMAN_V4_CONFIG", str(config_path))
    # Point at a projects file that deliberately does NOT exist.
    missing = tmp_path / "does-not-exist-projects.toml"
    monkeypatch.setenv("FOREMAN_PROJECTS_PATH", str(missing))
    monkeypatch.setattr("sys.argv", ["foreman", "daemon", "start"])

    # main() raises typer.Exit directly (before the typer app runs), so the
    # guard surfaces as typer.Exit(code=1), not SystemExit.
    with pytest.raises(typer.Exit) as excinfo:
        main()

    assert excinfo.value.exit_code == 1, "startup guard must exit non-zero on missing projects file"
    err = capsys.readouterr().err
    assert "projects file not found" in err, f"expected clean error text; got: {err!r}"
    assert str(missing) in err, "error should name the offending path"
    assert "Traceback" not in err, "must be a clean message, not a raw traceback"


def test_main_malformed_projects_file_exits_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """issue #503 FIX 2: a projects file that fails schema validation makes
    ``main()`` exit non-zero with a clean ``failed validation`` message,
    NOT a raw ``ValidationError`` traceback."""
    import typer

    from foreman.v4.cli import main

    config_path = _write_valid_config(tmp_path)
    monkeypatch.setenv("FOREMAN_V4_CONFIG", str(config_path))
    # A projects entry missing the required ``repo`` + ``local_clone_path``
    # fields → pydantic ValidationError at load_projects time.
    bad_projects = tmp_path / "projects.toml"
    bad_projects.write_text('[[projects]]\nname = "p"\n', encoding="utf-8")
    monkeypatch.setenv("FOREMAN_PROJECTS_PATH", str(bad_projects))
    monkeypatch.setattr("sys.argv", ["foreman", "daemon", "start"])

    with pytest.raises(typer.Exit) as excinfo:
        main()

    assert excinfo.value.exit_code == 1, (
        "startup guard must exit non-zero on malformed projects file"
    )
    err = capsys.readouterr().err
    assert "failed validation" in err, f"expected clean validation error text; got: {err!r}"
    assert str(bad_projects) in err, "error should name the offending path"
    assert "Traceback" not in err, "must be a clean message, not a raw traceback"


def test_main_broken_toml_projects_file_exits_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """issue #509: a syntactically-broken projects.toml makes main() exit
    non-zero with a clean 'invalid TOML' message, NOT a raw
    TOMLDecodeError traceback."""
    import typer

    from foreman.v4.cli import main

    config_path = _write_valid_config(tmp_path)
    monkeypatch.setenv("FOREMAN_V4_CONFIG", str(config_path))
    # A projects file with invalid TOML syntax → tomllib.TOMLDecodeError.
    broken_projects = tmp_path / "projects.toml"
    broken_projects.write_text("[[projects\n", encoding="utf-8")  # missing closing bracket
    monkeypatch.setenv("FOREMAN_PROJECTS_PATH", str(broken_projects))
    monkeypatch.setattr("sys.argv", ["foreman", "daemon", "start"])

    with pytest.raises(typer.Exit) as excinfo:
        main()

    assert excinfo.value.exit_code == 1, "startup guard must exit non-zero on broken TOML"
    err = capsys.readouterr().err
    assert "invalid toml" in err.lower(), f"expected TOML error text; got: {err!r}"
    assert str(broken_projects) in err, "error should name the offending path"
    assert "Traceback" not in err, "must be a clean message, not a raw traceback"


def test_main_help_exits_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``foreman --help`` exits 0 via typer's SystemExit, proving the
    full import graph + V4Config load + V4IdentityRegistry construction
    + bootstrap path succeeds before typer dispatches to the help
    handler. The test patches network seams (token mint + PyGithub
    client) so dummy on-disk PEMs are enough; everything else is
    production wiring."""
    from foreman.v4.cli import main

    # bootstrap_cli_context iterates projects and constructs a
    # PyGithubGitProvider per project, which (a) mints an orchestrator
    # installation token via foreman.auth, and (b) immediately calls
    # ``Github.get_repo(...)`` which is NOT lazy in PyGithub — it fires
    # a real HTTP round-trip on construction. The boundary the test
    # cares about is "main() builds the V4IdentityRegistry from V4Config
    # cleanly," not the downstream network surface, so stub both:
    #   - patch mint_installation_token at the v4.identity import site
    #     so dummy PEM bytes never reach the JWT signer.
    #   - patch PyGithubGitProvider at the cli import site so no real
    #     PyGithub client construction happens.
    def _fake_mint(app_id, private_key_path, repo_slug):
        return InstallationToken(token="ghs_FAKE_TOKEN", expires_at=2_000_000_000)

    monkeypatch.setattr(
        "foreman.v4.identity.mint_installation_token",
        _fake_mint,
    )
    monkeypatch.setattr(
        "foreman.v4.pygithub_git_provider.PyGithubGitProvider",
        lambda **_kwargs: MagicMock(),
    )
    # v5 (kill-sqlite): bootstrap now builds a PostgresTicketRepository,
    # whose ``from_dsn`` opens a real connection pool eagerly. The test
    # uses a dummy DSN, so stub the constructor to return an in-memory
    # repo — no Postgres connect happens. The boundary under test is the
    # config-load + bootstrap wiring, not real persistence.
    monkeypatch.setattr(
        "foreman.v4.postgres_repository.PostgresTicketRepository.from_dsn",
        classmethod(lambda cls, dsn, **_kwargs: InMemoryTicketRepository()),
    )
    # foreman#476: bootstrap now calls ensure_clone for each project whose
    # local_clone_path doesn't exist. Stub it so the --help smoke test
    # doesn't attempt a real git clone to a non-existent GitHub repo.
    monkeypatch.setattr("foreman.v4.bootstrap.ensure_clone", MagicMock())

    # Build a minimal valid config + dummy PEM files (never read at --help).
    log_dir = tmp_path / "logs"
    for role in ("planner", "reviewer", "fixer", "worker", "orchestrator"):
        (tmp_path / f"{role}.pem").write_text("dummy")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[daemon]
log_dir = "{log_dir.as_posix()}"

[storage]
engine = "postgres"
dsn = "postgresql://test/test"

[apps.planner]
app_id = 12345
private_key_path = "{(tmp_path / "planner.pem").as_posix()}"

[apps.reviewer]
app_id = 12346
private_key_path = "{(tmp_path / "reviewer.pem").as_posix()}"

[apps.fixer]
app_id = 12347
private_key_path = "{(tmp_path / "fixer.pem").as_posix()}"

[apps.worker]
app_id = 12348
private_key_path = "{(tmp_path / "worker.pem").as_posix()}"

[orchestrator]
app_id = 12349
private_key_path = "{(tmp_path / "orchestrator.pem").as_posix()}"

[operator.supervisor]
name = "Test Sup"
email = "sup@example.com"

[operator.signer]
name = "Test Sign"
email = "sign@example.com"

[[projects]]
name = "p"
repo = "owner/p"
local_clone_path = "{(tmp_path / "p").as_posix()}"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("FOREMAN_V4_CONFIG", str(config_path))
    # issue #477: main() now loads [[projects]] from FOREMAN_PROJECTS_PATH
    # instead of from config.toml. Write a minimal projects.toml and point
    # the daemon at it.
    projects_path = tmp_path / "projects.toml"
    projects_path.write_text(
        '[[projects]]\nname = "p"\nrepo = "owner/p"\n'
        f'local_clone_path = "{(tmp_path / "p").as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("FOREMAN_PROJECTS_PATH", str(projects_path))
    monkeypatch.setattr("sys.argv", ["foreman", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        main()
    # typer exits 0 on --help.
    assert excinfo.value.code == 0
