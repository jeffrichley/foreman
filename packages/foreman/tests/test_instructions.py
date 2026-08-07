"""Unit tests for ``foreman.instructions.load_project_instructions``.

Shapes covered:
  * Happy path: file present → returns its content verbatim
  * Missing file: ``.foreman/INSTRUCTIONS.md`` absent → returns ``None``
  * Missing parent: no ``.foreman/`` dir at all → returns ``None``
  * Missing clone path entirely → returns ``None``
  * Present but unreadable → returns ``None`` **and warns** (issue #586)
  * Real bare mirror vs real linked worktree, against actual git

Instructions are optional by design; a missing file is not an error.
The role dispatchers depend on this contract — they call the loader
unconditionally and only emit the prompt section when the return is
non-``None``.

Absent and unreadable both return ``None`` so a role is never blocked,
but they are not the same event and must not produce the same record —
see the two ``#586`` tests at the bottom, which pin the warning on one
and the silence on the other.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from foreman.instructions import INSTRUCTIONS_RELPATH, load_project_instructions


def _git(repo: Path, *args: str) -> None:
    """Run a git command in repo, failing loudly."""
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


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


# ── absent vs unreachable (issue #586) ───────────────────────────────────


def test_an_unreadable_file_is_logged_not_silently_treated_as_absent(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """A file that exists but will not read must leave a trace.

    Absent and unreachable both return ``None`` — that part is deliberate,
    so a role is never blocked. What must differ is the record: folding the
    two together is what let #586 run its whole lifetime with every role
    silently unconfigured.
    """
    target = tmp_path / INSTRUCTIONS_RELPATH
    target.parent.mkdir(parents=True)
    target.write_text("# real content\n", encoding="utf-8")

    def _unreadable(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", _unreadable)

    with caplog.at_level(logging.WARNING, logger="foreman.instructions"):
        result = load_project_instructions(tmp_path)

    assert result is None, "roles must not be blocked by an unreadable file"
    assert caplog.records, "an unreadable file must not be silent"
    message = caplog.records[0].getMessage()
    assert str(target) in message
    assert "PermissionError" in message


def test_a_genuinely_absent_file_stays_silent(tmp_path: Path, caplog) -> None:
    """The normal case must not warn, or the warning becomes noise.

    A check that fires on the expected case trains readers to ignore it,
    which costs the distinction this test's sibling exists to create.
    """
    with caplog.at_level(logging.WARNING, logger="foreman.instructions"):
        result = load_project_instructions(tmp_path)

    assert result is None
    assert not caplog.records, "an absent file is expected and must stay quiet"


def test_instructions_are_readable_from_a_real_worktree_of_a_bare_mirror(
    tmp_path: Path,
) -> None:
    """Pins the two facts #586 turned on, against real git rather than mocks.

    Builds a real repo with the file committed, mirrors it the way the
    daemon does, adds a real linked worktree the way a role does, and
    asserts the loader reads the committed content from the worktree and
    reads nothing from the mirror.

    Scope, stated honestly: this does NOT catch the original bug and
    passes against the pre-fix source. The loader was always correct —
    the defect was which path the four role dispatchers handed it, and
    the call-arg assertions in ``test_prompt_injection.py`` are what guard
    that. What this adds is the ground truth those mocks assume: that a
    mirror really does yield nothing and a worktree really does yield the
    committed content. If that ever stops holding — sparse checkout, a
    changed base layout — the mocked tests would keep passing and this
    one would not.
    """
    body = "# Project instructions\n\nUse squash merges.\n"
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@example.com")
    _git(origin, "config", "user.name", "t")
    (origin / INSTRUCTIONS_RELPATH).parent.mkdir(parents=True)
    (origin / INSTRUCTIONS_RELPATH).write_text(body, encoding="utf-8")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "add instructions")

    # The daemon's base: a bare mirror. No working tree, ever.
    mirror = tmp_path / "mirror.git"
    subprocess.run(
        ["git", "clone", "--mirror", "-q", str(origin), str(mirror)],
        check=True,
        capture_output=True,
    )
    assert load_project_instructions(mirror) is None, (
        "a bare mirror has no working tree — this is the #586 failure"
    )

    # The role's worktree: checked out, so the file is on disk.
    wt_path = tmp_path / "wt"
    _git(mirror, "worktree", "add", "-q", "--detach", str(wt_path), "main")
    assert load_project_instructions(wt_path) == body
