"""v4 isolation guard.

Phase 8 deletes v2/v3 by `git rm`. That's only safe if foreman.v4 never
imports from the kill set. This test AST-walks every v4 source file and
verifies the discipline holds.

If this test fails, the failing module reached into a legacy package.
Either move the dependency into foreman.v4 (correct) or reconsider whether
the legacy module belongs in the survival set instead (cite a reason).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

V4_ROOT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "foreman"
    / "v4"
)

# Modules whose entire purpose is the v2/v3 substrate. v4 must NOT import them.
KILL_SET = frozenset(
    {
        "foreman.reconciler",
        "foreman.daemon",
        "foreman.daemon_runners",
        "foreman.daemon_host",
        "foreman.daemon_lock",
        "foreman.dispatcher",
        "foreman.dispatch_recorder",
        "foreman.poller",
        "foreman.queue",
        "foreman.storage",
        "foreman.worker",
        "foreman.role_dispatch",
        "foreman.stats",
        "foreman.ps",
        "foreman.labels",
        "foreman.branches",
        "foreman.v3_bus_endpoint",
    }
)


def _iter_v4_files() -> list[Path]:
    assert V4_ROOT.is_dir(), f"v4 package missing at {V4_ROOT}"
    return sorted(V4_ROOT.rglob("*.py"))


def _imports_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None and node.level == 0:
                found.add(node.module)
    return found


@pytest.mark.parametrize("path", _iter_v4_files(), ids=lambda p: p.name)
def test_v4_module_does_not_import_kill_set(path: Path) -> None:
    imports = _imports_in(path)
    forbidden = {
        imp for imp in imports
        if any(imp == k or imp.startswith(k + ".") for k in KILL_SET)
    }
    assert not forbidden, (
        f"{path.relative_to(V4_ROOT)} imports from the kill set: {forbidden}. "
        "v4 modules must not depend on v2/v3 substrate. See the 'v4 isolation "
        "principle' section in the implementation plan."
    )


def test_kill_set_and_survival_set_are_disjoint() -> None:
    """Defensive: catch typos that would put a module on both lists."""
    survival_set = {
        "foreman.auth",
        "foreman.config",
        "foreman.identity",
        "foreman.init",
        "foreman.instructions",
        "foreman.locks",
        "foreman.git_host",
        "foreman.git_hosts",
        "foreman.provider",
        "foreman.providers",
        "foreman.roles",
        "foreman.prompts",
        "foreman.worktree",
        "foreman._env_filter",
        "foreman.logging_setup",
    }
    assert KILL_SET.isdisjoint(survival_set), KILL_SET & survival_set
