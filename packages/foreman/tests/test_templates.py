"""Sanity tests for packaged templates.

The instructions template ships as a packaged resource (``importlib.resources``-
loaded markdown). These tests confirm the file is present in the wheel /
src layout and that its expected sections survive packaging — a missing
or corrupted template would silently break ``foreman init`` for every
new project, so we pin its shape here.
"""

from __future__ import annotations

from importlib import resources


def test_instructions_template_loads_from_packaged_resources() -> None:
    """The packaged template can be located + read without raising."""
    template = (
        resources.files("foreman.templates")
        .joinpath("instructions.md.template")
        .read_text(encoding="utf-8")
    )
    assert template, "template file must not be empty"


def test_instructions_template_contains_expected_sections() -> None:
    """All four section headers must be present so the rendered file
    actually carries the guidance the role bots expect to find."""
    template = (
        resources.files("foreman.templates")
        .joinpath("instructions.md.template")
        .read_text(encoding="utf-8")
    )
    expected_sections = [
        "## PR title rules",
        "## Branch naming",
        "## Quality gate",
        "## Project-specific notes",
    ]
    for section in expected_sections:
        assert section in template, f"template missing section: {section!r}"


def test_instructions_template_carries_substitution_markers() -> None:
    """Two angle-bracketed placeholders must be in the template so the
    init renderer has something to substitute. Without these the rendered
    file would still parse, but project-name + check-command would
    silently mismatch the operator's project."""
    template = (
        resources.files("foreman.templates")
        .joinpath("instructions.md.template")
        .read_text(encoding="utf-8")
    )
    assert "<repo-name>" in template
    assert "<configured_check_command>" in template
