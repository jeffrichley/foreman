"""Dispatcher wiring: flag off = unchanged; flag on = bwrap-wrapped. Pure."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from foreman.v4.sandbox import SandboxLauncher
from foreman.v4.subprocess_dispatcher import BotMetadata, _redact_cmd, _reject_never_bind_paths

_BOT_METADATA = BotMetadata(
    slug_by_role={"worker": "worker-bot", "planner": "planner-bot"},
    bot_logins=frozenset(
        {"planner-bot[bot]", "reviewer-bot[bot]", "fixer-bot[bot]", "worker-bot[bot]"}
    ),
)


def test_redact_masks_gh_token_after_setenv() -> None:
    cmd = ["bwrap", "--setenv", "GH_TOKEN", "ghs_SECRET", "--", "foreman", "plan"]
    red = _redact_cmd(cmd)
    assert "ghs_SECRET" not in red
    assert red[red.index("GH_TOKEN") + 1] == "***"
    # non-token args untouched
    assert red[-2:] == ["foreman", "plan"]


def test_redact_is_noop_without_gh_token() -> None:
    cmd = ["foreman", "implement", "--project", "foreman"]
    assert _redact_cmd(cmd) == cmd


def test_dispatch_flag_off_runs_unwrapped(tmp_path: Path) -> None:
    """With no sandbox, the stub role runs directly (no bwrap prefix)."""
    from foreman.v4.subprocess_dispatcher import SubprocessRoleDispatcher

    stub = tmp_path / "stub.py"
    stub.write_text(
        "import sys\n"
        'print(\'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}\')\n'
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    identity = MagicMock()
    identity.get_role_token.return_value = "ghs_X"
    d = SubprocessRoleDispatcher(
        foreman_cli=[sys.executable, str(stub)],
        identity=identity,
        log_dir=tmp_path,
    )
    out = d.dispatch(role="planner", project="foreman", issue_number=1, ticket_id=1)
    assert "FOREMAN_OUTCOME:" in out


def test_dispatch_flag_on_creates_scratch_and_wraps(tmp_path: Path, monkeypatch) -> None:
    """With a sandbox launcher, dispatch creates the per-job scratch dir and
    Popens a bwrap-prefixed argv. We intercept Popen to assert the argv shape
    without needing real bwrap."""
    from foreman.v4 import subprocess_dispatcher as sd

    captured: dict[str, object] = {}

    class FakeProc:
        def __init__(self, cmd, **kw):
            captured["cmd"] = cmd
            self.stdout = _StubStream(
                'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}\n'
            )
            self.stderr = _StubStream("")
            self.pid = 4321
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(sd.subprocess, "Popen", FakeProc)

    from foreman.v4.config import ProjectConfig

    identity = MagicMock()
    identity.get_role_token.return_value = "ghs_TOK"
    scratch_root = tmp_path / "scratch"
    projects = {
        "foreman": ProjectConfig(
            name="foreman", repo="jeffrichley/foreman", local_clone_path="/foreman/repos/foreman"
        )
    }
    d = sd.SubprocessRoleDispatcher(
        foreman_cli=["foreman"],
        identity=identity,
        log_dir=tmp_path / "logs",
        sandbox=SandboxLauncher(cache_dir="/root/.cache/uv", bwrap_path="bwrap"),
        sandbox_scratch_root=scratch_root,
        sandbox_projects=projects,
        sandbox_clone_prep=lambda **kw: None,
        bot_metadata=_BOT_METADATA,
    )
    out = d.dispatch(role="worker", project="foreman", issue_number=537, ticket_id=99)
    assert "FOREMAN_OUTCOME:" in out
    cmd = captured["cmd"]
    assert cmd[0] == "bwrap"
    assert "foreman" in cmd and "implement" in cmd
    # foreman#556: the worktree ROOT now lives under a `wt/` sub-layout
    # (sibling of `clone/`), not directly at the job dir.
    expected_scratch = scratch_root / "foreman" / "worker-537" / "wt"
    assert expected_scratch.exists()
    # NOTE (brief bug fix): build_argv emits TWO "--bind" pairs — the
    # shared cache first, then the job scratch dir — so cmd.index("--bind")
    # lands on the cache pair, not the scratch one. Look up the second
    # occurrence to target the scratch bind specifically.
    dd = cmd.index("--bind", cmd.index("--bind") + 1)
    assert cmd[dd + 1] == str(expected_scratch)
    assert cmd[dd + 2] == "/scratch"


def test_dispatch_flag_on_preps_clone_and_binds_repo_and_config(
    tmp_path: Path, monkeypatch
) -> None:
    """Enabled path: clone-prep runs, private clone RW-bound at local_clone_path,
    config + projects RO-bound, worktree root at /scratch."""
    from foreman.v4 import subprocess_dispatcher as sd
    from foreman.v4.config import ProjectConfig

    captured: dict[str, object] = {}

    class FakeProc:
        def __init__(self, cmd, **kw):
            captured["cmd"] = cmd
            self.stdout = _StubStream(
                'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}\n'
            )
            self.stderr = _StubStream("")
            self.pid = 4321
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(sd.subprocess, "Popen", FakeProc)
    monkeypatch.setenv("FOREMAN_V4_CONFIG", "/foreman/state/config.toml")
    monkeypatch.setenv("FOREMAN_PROJECTS_PATH", "/root/.foreman/projects.toml")

    prep_calls: list[dict[str, object]] = []

    def fake_prep(*, base_clone_path, dest_clone_path, repo_url, role_token):
        prep_calls.append(
            {
                "base": str(base_clone_path),
                "dest": str(dest_clone_path),
                "url": repo_url,
                "token": role_token,
            }
        )

    identity = MagicMock()
    identity.get_role_token.return_value = "ghs_TOK"
    scratch_root = tmp_path / "repos" / ".scratch"
    projects = {
        "foreman": ProjectConfig(
            name="foreman", repo="jeffrichley/foreman", local_clone_path="/foreman/repos/foreman"
        )
    }
    d = sd.SubprocessRoleDispatcher(
        foreman_cli=["foreman"],
        identity=identity,
        log_dir=tmp_path / "logs",
        sandbox=SandboxLauncher(cache_dir="/root/.cache/uv", bwrap_path="bwrap"),
        sandbox_scratch_root=scratch_root,
        sandbox_projects=projects,
        sandbox_clone_prep=fake_prep,
        bot_metadata=_BOT_METADATA,
    )
    out = d.dispatch(role="worker", project="foreman", issue_number=537, ticket_id=99)
    assert "FOREMAN_OUTCOME:" in out

    # clone-prep called with base=local_clone_path, dest under scratch, github url + token
    assert len(prep_calls) == 1
    job = scratch_root / "foreman" / "worker-537"
    # Brief-snippet fix (same class of bug as test_sandbox_clone.py's
    # test_clone_argv_is_local_clone): the dispatcher wraps
    # project_cfg.local_clone_path in Path(...) before handing it to
    # prepare_sandbox_clone (its signature requires a Path), and
    # str(Path("/foreman/...")) renders with backslashes on a Windows
    # dev box. Derive the expected string from the same Path(...) call
    # instead of a hardcoded POSIX literal so the assertion is
    # platform-consistent rather than Linux-only.
    assert prep_calls[0]["base"] == str(Path("/foreman/repos/foreman"))
    assert prep_calls[0]["dest"] == str(job / "clone")
    assert prep_calls[0]["url"] == "https://github.com/jeffrichley/foreman.git"
    assert prep_calls[0]["token"] == "ghs_TOK"

    cmd = captured["cmd"]
    assert cmd[0] == "bwrap"
    # worktree root (wt/) bound at /scratch
    assert str(job / "wt") in cmd
    assert (job / "wt").exists()
    # private clone RW-bound at the in-box repo path
    i = cmd.index("--bind", cmd.index("--bind", cmd.index("--bind") + 1) + 1)  # 3rd --bind
    assert cmd[i + 1] == str(job / "clone")
    assert cmd[i + 2] == "/foreman/repos/foreman"
    # config + projects RO-bound
    assert "/foreman/state/config.toml" in cmd
    assert "/root/.foreman/projects.toml" in cmd
    # carry-forward B: FOREMAN_PROJECTS_PATH also reaches the box as an
    # env var (not just the RO file bind) so the role's own
    # `load_projects` call resolves it.
    key_idx = cmd.index("FOREMAN_PROJECTS_PATH")
    assert cmd[key_idx - 1] == "--setenv"
    assert cmd[key_idx + 1] == "/root/.foreman/projects.toml"


def test_dispatch_flag_on_injects_bot_slug_and_logins(tmp_path: Path, monkeypatch) -> None:
    """foreman#role-identity: the box's SandboxIdentityRegistry reads its
    bot slug/logins from the env — the sandboxed argv must carry
    ``--setenv FOREMAN_BOT_SLUG <role's slug>`` and
    ``--setenv FOREMAN_BOT_LOGINS "<space-separated logins>"``."""
    from foreman.v4 import subprocess_dispatcher as sd
    from foreman.v4.config import ProjectConfig

    captured: dict[str, object] = {}

    class FakeProc:
        def __init__(self, cmd, **kw):
            captured["cmd"] = cmd
            self.stdout = _StubStream(
                'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}\n'
            )
            self.stderr = _StubStream("")
            self.pid = 4321
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(sd.subprocess, "Popen", FakeProc)

    identity = MagicMock()
    identity.get_role_token.return_value = "ghs_TOK"
    scratch_root = tmp_path / "scratch"
    projects = {
        "foreman": ProjectConfig(
            name="foreman", repo="jeffrichley/foreman", local_clone_path="/foreman/repos/foreman"
        )
    }
    bot_metadata = sd.BotMetadata(
        slug_by_role={"planner": "my-slug"},
        bot_logins=frozenset({"b[bot]", "a[bot]"}),
    )
    d = sd.SubprocessRoleDispatcher(
        foreman_cli=["foreman"],
        identity=identity,
        log_dir=tmp_path / "logs",
        sandbox=SandboxLauncher(cache_dir="/root/.cache/uv", bwrap_path="bwrap"),
        sandbox_scratch_root=scratch_root,
        sandbox_projects=projects,
        sandbox_clone_prep=lambda **kw: None,
        bot_metadata=bot_metadata,
    )
    out = d.dispatch(role="planner", project="foreman", issue_number=1, ticket_id=1)
    assert "FOREMAN_OUTCOME:" in out
    cmd = captured["cmd"]
    slug_idx = cmd.index("FOREMAN_BOT_SLUG")
    assert cmd[slug_idx - 1] == "--setenv"
    assert cmd[slug_idx + 1] == "my-slug"
    logins_idx = cmd.index("FOREMAN_BOT_LOGINS")
    assert cmd[logins_idx - 1] == "--setenv"
    assert cmd[logins_idx + 1] == "a[bot] b[bot]"


def test_dispatch_flag_on_target_aware_role_injects_base_role_slug(
    tmp_path: Path, monkeypatch
) -> None:
    """foreman#role-identity regression: target-aware roles (``reviewer-spec``,
    ``reviewer-impl``, ``fixer-spec``, ``fixer-impl``) must resolve
    ``FOREMAN_BOT_SLUG`` via the BASE role, not the target-aware role — the
    ``slug_by_role`` map built in bootstrap.py is keyed by the four base
    roles only, so looking a target-aware role up directly raises
    ``KeyError``."""
    from foreman.v4 import subprocess_dispatcher as sd
    from foreman.v4.config import ProjectConfig

    captured: dict[str, object] = {}

    class FakeProc:
        def __init__(self, cmd, **kw):
            captured["cmd"] = cmd
            self.stdout = _StubStream(
                'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}\n'
            )
            self.stderr = _StubStream("")
            self.pid = 4321
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(sd.subprocess, "Popen", FakeProc)

    identity = MagicMock()
    identity.get_role_token.return_value = "ghs_TOK"
    scratch_root = tmp_path / "scratch"
    projects = {
        "foreman": ProjectConfig(
            name="foreman", repo="jeffrichley/foreman", local_clone_path="/foreman/repos/foreman"
        )
    }
    bot_metadata = sd.BotMetadata(
        slug_by_role={
            "planner": "plan-slug",
            "reviewer": "rev-slug",
            "fixer": "fix-slug",
            "worker": "work-slug",
        },
        bot_logins=frozenset({"x[bot]"}),
    )
    d = sd.SubprocessRoleDispatcher(
        foreman_cli=["foreman"],
        identity=identity,
        log_dir=tmp_path / "logs",
        sandbox=SandboxLauncher(cache_dir="/root/.cache/uv", bwrap_path="bwrap"),
        sandbox_scratch_root=scratch_root,
        sandbox_projects=projects,
        sandbox_clone_prep=lambda **kw: None,
        bot_metadata=bot_metadata,
    )
    out = d.dispatch(role="reviewer-spec", project="foreman", issue_number=1, ticket_id=1)
    assert "FOREMAN_OUTCOME:" in out
    cmd = captured["cmd"]
    slug_idx = cmd.index("FOREMAN_BOT_SLUG")
    assert cmd[slug_idx - 1] == "--setenv"
    assert cmd[slug_idx + 1] == "rev-slug"


def test_dispatch_flag_on_without_bot_metadata_raises(tmp_path: Path) -> None:
    """foreman#role-identity: sandbox enabled but no ``bot_metadata`` configured
    is a required-config error, mirroring the sibling ``sandbox_projects`` /
    ``sandbox_scratch_root`` guards."""
    from foreman.v4.config import ProjectConfig
    from foreman.v4.subprocess_dispatcher import RoleSubprocessError, SubprocessRoleDispatcher

    identity = MagicMock()
    identity.get_role_token.return_value = "ghs_X"
    projects = {
        "foreman": ProjectConfig(
            name="foreman", repo="jeffrichley/foreman", local_clone_path="/foreman/repos/foreman"
        )
    }
    d = SubprocessRoleDispatcher(
        foreman_cli=["foreman"],
        identity=identity,
        log_dir=tmp_path,
        sandbox=SandboxLauncher(cache_dir="/c"),
        sandbox_scratch_root=tmp_path / "s",
        sandbox_projects=projects,
    )
    with pytest.raises(RoleSubprocessError, match="bot metadata"):
        d.dispatch(role="planner", project="foreman", issue_number=1, ticket_id=1)


def test_dispatch_flag_on_without_project_map_raises(tmp_path: Path) -> None:
    from foreman.v4.subprocess_dispatcher import RoleSubprocessError, SubprocessRoleDispatcher

    identity = MagicMock()
    identity.get_role_token.return_value = "ghs_X"
    d = SubprocessRoleDispatcher(
        foreman_cli=["foreman"],
        identity=identity,
        log_dir=tmp_path,
        sandbox=SandboxLauncher(cache_dir="/c"),
        sandbox_scratch_root=tmp_path / "s",
        sandbox_projects=None,
    )
    with pytest.raises(RoleSubprocessError, match="project map"):
        d.dispatch(role="worker", project="foreman", issue_number=1, ticket_id=1)


def test_reject_never_bind_paths_allows_the_projects_toml_exception() -> None:
    """Carry-forward (Task 2 review): the single named exception passes."""
    _reject_never_bind_paths(
        [("/root/.foreman/projects.toml", "/root/.foreman/projects.toml")]
    )  # no raise


def test_reject_never_bind_paths_rejects_prefix_violation() -> None:
    """Carry-forward (Task 2 review): PREFIX match, not the old test-only
    substring check — a stray file under a never-bind root is caught even
    though it is not the allow-listed projects.toml itself."""
    from foreman.v4.subprocess_dispatcher import RoleSubprocessError

    with pytest.raises(RoleSubprocessError, match="never-bind"):
        _reject_never_bind_paths([("/root/.foreman/keys/x.pem", "/root/.foreman/keys/x.pem")])


def test_reject_never_bind_paths_does_not_false_positive_on_lookalike() -> None:
    """PREFIX (not substring) match: a sibling path that merely starts with
    the same characters as a never-bind root must NOT be rejected."""
    _reject_never_bind_paths(
        [("/root/.foreman-lookalike/x.toml", "/root/.foreman-lookalike/x.toml")]
    )  # no raise


def test_dispatch_flag_on_rejects_forbidden_path_before_reaching_bwrap(
    tmp_path: Path, monkeypatch
) -> None:
    """Carry-forward (Task 2 review), wired end-to-end: a projects path
    under a never-bind root that ISN'T the allow-listed projects.toml
    exception is rejected before dispatch ever builds the bwrap argv."""
    from foreman.v4 import subprocess_dispatcher as sd
    from foreman.v4.config import ProjectConfig

    monkeypatch.setenv("FOREMAN_V4_CONFIG", "/foreman/state/config.toml")
    # Not the allow-listed projects.toml -- a stray file under the same
    # never-bind root.
    monkeypatch.setenv("FOREMAN_PROJECTS_PATH", "/root/.foreman/keys/x.pem")

    identity = MagicMock()
    identity.get_role_token.return_value = "ghs_TOK"
    projects = {
        "foreman": ProjectConfig(
            name="foreman", repo="jeffrichley/foreman", local_clone_path="/foreman/repos/foreman"
        )
    }
    d = sd.SubprocessRoleDispatcher(
        foreman_cli=["foreman"],
        identity=identity,
        log_dir=tmp_path / "logs",
        sandbox=SandboxLauncher(cache_dir="/root/.cache/uv", bwrap_path="bwrap"),
        sandbox_scratch_root=tmp_path / "repos" / ".scratch",
        sandbox_projects=projects,
        sandbox_clone_prep=lambda **kw: None,
        bot_metadata=_BOT_METADATA,
    )
    with pytest.raises(sd.RoleSubprocessError, match="never-bind"):
        d.dispatch(role="worker", project="foreman", issue_number=537, ticket_id=99)


class _StubStream:
    def __init__(self, text: str) -> None:
        self._lines = text.splitlines(keepends=True)

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass
