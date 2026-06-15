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


def test_main_help_exits_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
        "foreman.v4.identity.mint_installation_token", _fake_mint,
    )
    monkeypatch.setattr(
        "foreman.v4.pygithub_git_provider.PyGithubGitProvider",
        lambda **_kwargs: MagicMock(),
    )

    # Build a minimal valid config + dummy PEM files (never read at --help).
    log_dir = tmp_path / "logs"
    db_path = tmp_path / "v4.db"
    for role in ("planner", "reviewer", "fixer", "worker", "orchestrator"):
        (tmp_path / f"{role}.pem").write_text("dummy")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[daemon]
db_path = "{db_path.as_posix()}"
log_dir = "{log_dir.as_posix()}"

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

[[projects]]
name = "p"
repo = "owner/p"
local_clone_path = "{(tmp_path / "p").as_posix()}"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("FOREMAN_V4_CONFIG", str(config_path))
    monkeypatch.setattr("sys.argv", ["foreman", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        main()
    # typer exits 0 on --help.
    assert excinfo.value.code == 0
