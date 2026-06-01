"""Unit tests for ``foreman.instructions.load_project_instructions``.

Three shapes:
  * Happy path: file present → returns its content verbatim
  * Missing file: ``.foreman/INSTRUCTIONS.md`` absent → returns ``None``
  * Missing parent: no ``.foreman/`` dir at all → returns ``None``

Instructions are optional by design; a missing file is not an error.
The role dispatchers depend on this contract — they call the loader
unconditionally and only emit the prompt section when the return is
non-``None``.
"""

from __future__ import annotations

from pathlib import Path

from foreman.instructions import INSTRUCTIONS_RELPATH, load_project_instructions


def test_load_project_instructions_returns_file_content_when_present(
    tmp_path: Path,
) -> None:
    """Happy path: file at ``<clone>/.foreman/INSTRUCTIONS.md`` is read
    verbatim, including markdown formatting + trailing newline."""
    clone = tmp_path / "clone"
    (clone / ".foreman").mkdir(parents=True)
    content = (
        "# Foreman instructions for myproj\n\n## PR title rules\n\nUse `feat(scope): ...` format.\n"
    )
    (clone / INSTRUCTIONS_RELPATH).write_text(content, encoding="utf-8")

    result = load_project_instructions(clone)

    assert result == content


def test_load_project_instructions_returns_none_when_file_missing(
    tmp_path: Path,
) -> None:
    """The ``.foreman/`` directory exists but the file does not — the
    loader returns ``None`` rather than raising. Caller treats this as
    "no project nudge this run"."""
    clone = tmp_path / "clone"
    (clone / ".foreman").mkdir(parents=True)
    # Note: no INSTRUCTIONS.md written.

    assert load_project_instructions(clone) is None


def test_load_project_instructions_returns_none_when_parent_dir_missing(
    tmp_path: Path,
) -> None:
    """The ``.foreman/`` parent directory does not exist at all — same
    "absent" outcome, no exception. Covers the case where a project has
    not yet been onboarded via ``foreman init``."""
    clone = tmp_path / "clone"
    clone.mkdir()
    # No `.foreman/` directory at all.

    assert load_project_instructions(clone) is None


def test_load_project_instructions_returns_none_when_clone_path_missing(
    tmp_path: Path,
) -> None:
    """Even the clone path itself does not exist — still a clean ``None``,
    not a crash. Defensive: tests sometimes hand in synthesized paths."""
    nonexistent = tmp_path / "does-not-exist"

    assert load_project_instructions(nonexistent) is None
