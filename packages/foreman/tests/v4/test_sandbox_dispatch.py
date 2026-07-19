"""Dispatcher wiring: flag off = unchanged; flag on = bwrap-wrapped. Pure."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

from foreman.v4.sandbox import SandboxLauncher
from foreman.v4.subprocess_dispatcher import _redact_cmd


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

    identity = MagicMock()
    identity.get_role_token.return_value = "ghs_TOK"
    scratch_root = tmp_path / "scratch"
    d = sd.SubprocessRoleDispatcher(
        foreman_cli=["foreman"],
        identity=identity,
        log_dir=tmp_path / "logs",
        sandbox=SandboxLauncher(cache_dir="/root/.cache/uv", bwrap_path="bwrap"),
        sandbox_scratch_root=scratch_root,
    )
    out = d.dispatch(role="worker", project="foreman", issue_number=537, ticket_id=99)
    assert "FOREMAN_OUTCOME:" in out
    cmd = captured["cmd"]
    assert cmd[0] == "bwrap"
    assert "foreman" in cmd and "implement" in cmd
    # scratch dir created and mounted RW at /scratch
    expected_scratch = scratch_root / "foreman" / "worker-537"
    assert expected_scratch.exists()
    # NOTE (brief bug fix): build_argv emits TWO "--bind" pairs — the
    # shared cache first, then the job scratch dir — so cmd.index("--bind")
    # lands on the cache pair, not the scratch one. Look up the second
    # occurrence to target the scratch bind specifically.
    dd = cmd.index("--bind", cmd.index("--bind") + 1)
    assert cmd[dd + 1] == str(expected_scratch)
    assert cmd[dd + 2] == "/scratch"


class _StubStream:
    def __init__(self, text: str) -> None:
        self._lines = text.splitlines(keepends=True)

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass
