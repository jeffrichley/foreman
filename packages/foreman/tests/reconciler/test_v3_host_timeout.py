"""Test that V3GitHubHost terminates and logs timeout for stuck subprocesses."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.v3_host import V3GitHubHost


class _HangingProcess:
    """A subprocess-like that never exits unless terminate() is called."""

    def __init__(self) -> None:
        self.pid = 99999
        self._terminated = False
        # `proc._proc.terminate()` is the path v3_host uses to nudge a hung child.
        self._proc = self

    def terminate(self) -> None:
        self._terminated = True

    async def wait(self) -> int:
        while not self._terminated:
            await asyncio.sleep(0.01)
        return -15  # conventional return code for SIGTERM


@pytest.mark.asyncio
async def test_subprocess_timeout_terminates_and_logs(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    proc = _HangingProcess()

    def _runner(_argv: list[str]) -> _HangingProcess:
        return proc

    host = V3GitHubHost(
        v2_host=None,  # type: ignore[arg-type]  # not exercised in dispatch path
        log=log,
        subprocess_runner=_runner,
        role_dispatch_timeout_seconds=1,
    )

    # Pre-create a start row so the timeout-termination path has a parent_log_id.
    start_id = log.write_action(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        rule_name="dispatch_planner",
        action="dispatch_planner",
        outcome="running",
        details={},
    )

    await host._track_subprocess_completion(proc, "planner", start_log_id=start_id)

    # The subprocess was terminated by the timeout path.
    assert proc._terminated is True

    # The log row was finalized with outcome='timeout'.
    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        outcome = conn.execute(
            "SELECT outcome FROM execution_log WHERE parent_log_id = ?",
            (start_id,),
        ).fetchone()[0]
    assert outcome == "timeout"
