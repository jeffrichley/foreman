"""Architectural guardrail: the ``claude_agent_sdk`` boundary.

After foreman#266, exactly ONE module under
``packages/foreman/src/foreman/`` is allowed to import from
``claude_agent_sdk`` — the concrete adapter
``foreman/providers/anthropic_sdk.py``. Every other module depends
on the ``ProviderProtocol``-shaped abstraction the adapter implements
(``foreman.provider.ProviderFacade``) and the domain exception types
in ``foreman.providers.exceptions``.

This test walks every ``.py`` file under
``packages/foreman/src/foreman/``, AST-parses it (NOT regex — comments
and docstrings must not register false positives), and asserts that
the only module containing a top-level ``from claude_agent_sdk`` or
``import claude_agent_sdk`` statement is the one allowed adapter.

If this test fails after a future change, the fix is to route the new
caller through ``foreman.providers`` (the public surface), NOT to add
another exception to the allowlist below.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SDK_MODULE = "claude_agent_sdk"
_ALLOWED_MODULE_RELPATH = Path("foreman/providers/anthropic_sdk.py")


def _foreman_src_root() -> Path:
    """Locate ``packages/foreman/src/foreman/`` relative to this file."""
    here = Path(__file__).resolve()
    # tests/test_provider_boundary.py -> packages/foreman/src/foreman
    candidate = here.parent.parent / "src" / "foreman"
    assert candidate.is_dir(), f"expected foreman src tree at {candidate}"
    return candidate


def _imports_claude_agent_sdk(path: Path) -> bool:
    """Return True iff ``path`` contains a top-level (or nested)
    ``import claude_agent_sdk`` / ``from claude_agent_sdk import ...``.

    AST-based so a docstring quoting ``from claude_agent_sdk`` does not
    register as an import.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _SDK_MODULE or alias.name.startswith(_SDK_MODULE + "."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == _SDK_MODULE or module.startswith(_SDK_MODULE + "."):
                return True
    return False


def test_no_module_outside_providers_imports_claude_agent_sdk() -> None:
    """At most one source module (the adapter) imports from
    ``claude_agent_sdk``. Lock the property going forward — any future
    PR that adds a second importer fails here loudly.
    """
    src_root = _foreman_src_root()
    offenders: list[Path] = []
    for path in src_root.rglob("*.py"):
        rel = path.relative_to(src_root.parent)
        if rel == _ALLOWED_MODULE_RELPATH:
            continue
        if _imports_claude_agent_sdk(path):
            offenders.append(rel)

    assert offenders == [], (
        f"these modules import claude_agent_sdk but only "
        f"{_ALLOWED_MODULE_RELPATH.as_posix()} is allowed to: "
        f"{[p.as_posix() for p in offenders]}"
    )
