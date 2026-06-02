"""Tests for the prompts module — vendored superpowers + role-prompt composition.

These tests pin the contract between Foreman's role LLMs and the
discipline patterns we vendor from superpowers. If a refresh of the
vendored content drops a section the roles depend on, or if the
composition order silently regresses, these tests fail loudly.
"""

from __future__ import annotations

from importlib import resources

import pytest

from foreman.prompts import (
    compose_role_prompt,
    load_role_prompt,
    load_superpowers_skill,
)

# Vendored skills the role loaders bundle. Update this list when adding a
# new vendored skill so the packaged-resources test catches forgotten
# wheel inclusion.
_VENDORED_SKILLS: list[str] = [
    "writing-plans",
    "test-driven-development",
    "executing-plans",
    "verification-before-completion",
    "finishing-a-development-branch",
    "requesting-code-review",
    "receiving-code-review",
]

# Signature lines pulled verbatim from the upstream superpowers 5.1.0
# SKILL.md files. They appear early in each file and reflect that
# skill's load-bearing pattern. If a future refresh drops or renames
# these phrases, the role prompt has changed substantively and we want
# the suite to surface that for a deliberate review.
_SUPERPOWERS_SIGNATURES: dict[str, str] = {
    "writing-plans": "Writing Plans",
    "test-driven-development": "Test-Driven Development",
    "executing-plans": "Executing Plans",
    "verification-before-completion": "Verification Before Completion",
    "finishing-a-development-branch": "Finishing a Development Branch",
    "requesting-code-review": "Requesting Code Review",
    # upstream uses "Code Review Reception" as the H1, not "Receiving"
    "receiving-code-review": "Code Review Reception",
}


def test_version_marker_is_packaged() -> None:
    """The ``_VERSION.md`` marker MUST ride with the wheel so the
    source-version pin is discoverable at runtime (debugging, drift
    audit). Forgetting it on a refresh means the prompt is silently
    based on an unknown upstream version."""
    marker = (
        resources.files("foreman.prompts.superpowers")
        .joinpath("_VERSION.md")
        .read_text(encoding="utf-8")
    )
    assert "5.1.0" in marker, "version pin must reference the source version"
    assert "writing-plans" in marker, "marker should enumerate vendored skills"


@pytest.mark.parametrize("skill", _VENDORED_SKILLS)
def test_each_vendored_skill_loads(skill: str) -> None:
    content = load_superpowers_skill(skill)
    assert content.strip(), f"{skill}.md must not be empty"


@pytest.mark.parametrize("skill,signature", list(_SUPERPOWERS_SIGNATURES.items()))
def test_each_vendored_skill_carries_signature(skill: str, signature: str) -> None:
    """Pin a phrase from each upstream SKILL.md so refresh drift surfaces."""
    content = load_superpowers_skill(skill)
    assert signature in content, (
        f"vendored {skill}.md lost the signature {signature!r} — superpowers "
        "may have rewritten the skill; review and re-pin before merging the refresh"
    )


def test_unknown_skill_raises_filenotfound() -> None:
    """Asking for a skill we didn't vendor must fail loudly, not silently
    drop the discipline. The role startup is the right place for the
    failure (operator sees it immediately)."""
    with pytest.raises(FileNotFoundError):
        load_superpowers_skill("not-a-real-skill")


@pytest.mark.parametrize("role", ["planner", "reviewer", "fixer", "worker"])
def test_each_role_prompt_loads(role: str) -> None:
    """The Foreman-specific role.md files survive packaging."""
    content = load_role_prompt(role)
    assert content.strip(), f"{role}.md must not be empty"


def test_compose_role_prompt_includes_adapter_preamble_first() -> None:
    """The vendored SKILL.md files were written for interactive Claude
    Code (Skill tool, ``docs/superpowers/plans/`` defaults, etc.). Without
    the adapter preamble explaining the SDK environment, lines like
    ``REQUIRED SUB-SKILL: Use superpowers:executing-plans`` would either
    stall the role LLM or point it at paths the Foreman role contract
    does not own. The preamble MUST appear first so the LLM reads the
    interpretation rules before the discipline that needs interpreting.
    """
    composed = compose_role_prompt(
        role="planner", superpowers=["writing-plans"]
    )
    skill = load_superpowers_skill("writing-plans")
    preamble_idx = composed.index("Foreman role adapter")
    skill_idx = composed.index(skill)
    assert preamble_idx < skill_idx, "adapter preamble must precede vendored skills"
    # Pin the load-bearing assertions of the preamble. If a future edit
    # accidentally weakens these, the test surfaces it for review — the
    # SDK environment's missing affordances are the whole reason this
    # adapter exists.
    for required in (
        "Do NOT try to call a\n  Skill tool",
        "the role contract wins",
        "No `Skill` tool",
    ):
        assert required in composed, (
            f"adapter preamble lost required clause: {required!r}"
        )


def test_each_vendored_skill_gets_inline_header() -> None:
    """Each loaded SKILL.md is wrapped with a header that names it as the
    superpowers skill. The dangling ``Use superpowers:X`` references the
    upstream content contains can then resolve to the named block instead
    of stalling waiting for a Skill tool that doesn't exist in the SDK.
    Pin the load-bearing wording so a future "tidy up the header" edit
    doesn't accidentally remove the resolution hint.
    """
    composed = compose_role_prompt(
        role="worker",
        superpowers=[
            "test-driven-development",
            "executing-plans",
            "verification-before-completion",
            "finishing-a-development-branch",
        ],
    )
    for skill in (
        "test-driven-development",
        "executing-plans",
        "verification-before-completion",
        "finishing-a-development-branch",
    ):
        assert f"## superpowers:{skill} (inlined)" in composed
        assert (
            f"When any\nother section says ``Use superpowers:{skill}``"
            in composed
        ), f"{skill}: lost the cross-reference resolution clause"


def test_compose_role_prompt_orders_superpowers_before_role() -> None:
    """The composition puts vendored discipline FIRST (frames the
    Foreman-specific contract), then the role prompt. Inverting this
    order would let the role's narrow contract override the discipline
    in the LLM's attention budget — the opposite of the design."""
    composed = compose_role_prompt(
        role="planner", superpowers=["writing-plans"]
    )
    skill = load_superpowers_skill("writing-plans")
    role = load_role_prompt("planner")
    assert composed.index(skill) < composed.index(role), (
        "superpowers content must appear before the Foreman role prompt"
    )
    # Separator pins the visual boundary so the LLM sees the discipline
    # block clearly close before the role-specific contract opens.
    assert "\n\n---\n\n" in composed


def test_compose_role_prompt_concatenates_multiple_superpowers() -> None:
    """Worker bundles four skills in order. Each one appears once, the
    order matches the call, and they precede the role prompt."""
    composed = compose_role_prompt(
        role="worker",
        superpowers=[
            "test-driven-development",
            "executing-plans",
            "verification-before-completion",
            "finishing-a-development-branch",
        ],
    )
    role_marker = load_role_prompt("worker")[:200]  # first ~200 chars
    tdd_idx = composed.index(_SUPERPOWERS_SIGNATURES["test-driven-development"])
    exec_idx = composed.index(_SUPERPOWERS_SIGNATURES["executing-plans"])
    verify_idx = composed.index(_SUPERPOWERS_SIGNATURES["verification-before-completion"])
    finish_idx = composed.index(_SUPERPOWERS_SIGNATURES["finishing-a-development-branch"])
    role_idx = composed.index(role_marker)
    assert tdd_idx < exec_idx < verify_idx < finish_idx < role_idx


# ----------------------------------------------------------------------
# Per-role integration: the loaders the role modules actually call must
# return composed content that contains BOTH the vendored discipline and
# the role-specific contract. These tests catch wiring regressions where
# someone refactored a loader back to the pre-vendor shape.
# ----------------------------------------------------------------------


def test_planner_loader_includes_writing_plans() -> None:
    from foreman.roles.planner import _load_planner_prompt

    prompt = _load_planner_prompt()
    assert _SUPERPOWERS_SIGNATURES["writing-plans"] in prompt
    # First ~200 chars of the role file is a robust marker for "the role
    # prompt is in there too"
    assert load_role_prompt("planner")[:200] in prompt


def test_reviewer_loader_includes_requesting_code_review() -> None:
    from foreman.roles.reviewer import _load_reviewer_prompt

    prompt = _load_reviewer_prompt()
    assert _SUPERPOWERS_SIGNATURES["requesting-code-review"] in prompt
    assert load_role_prompt("reviewer")[:200] in prompt


def test_fixer_loader_includes_receiving_code_review() -> None:
    from foreman.roles.fixer import _load_fixer_prompt

    prompt = _load_fixer_prompt()
    assert _SUPERPOWERS_SIGNATURES["receiving-code-review"] in prompt
    assert load_role_prompt("fixer")[:200] in prompt


def test_worker_loader_includes_all_four_superpowers() -> None:
    from foreman.roles.worker import _load_worker_prompt

    prompt = _load_worker_prompt()
    for skill in (
        "test-driven-development",
        "executing-plans",
        "verification-before-completion",
        "finishing-a-development-branch",
    ):
        assert _SUPERPOWERS_SIGNATURES[skill] in prompt, (
            f"Worker prompt lost vendored skill {skill}"
        )
    assert load_role_prompt("worker")[:200] in prompt


# ----------------------------------------------------------------------
# Target-aware routing — foreman#78 + #79
#
# The loader gains an optional ``target`` kwarg that picks
# ``<role>_impl.md`` for ``target="impl_pr"`` when that file exists,
# and falls back to ``<role>.md`` otherwise. Roles without an impl
# variant (Planner, Worker) keep working unchanged.
# ----------------------------------------------------------------------


def test_load_role_prompt_default_target_loads_canonical_file() -> None:
    """``target=None`` MUST keep today's behavior — the canonical
    ``<role>.md`` is loaded with no fallback consulted. This is the
    back-compat surface every existing call site relies on."""
    content = load_role_prompt("reviewer")
    assert content.strip(), "reviewer.md should load with default target"


def test_load_role_prompt_spec_target_loads_canonical_file() -> None:
    """``target="spec_pr"`` is equivalent to ``target=None`` — the
    canonical ``<role>.md`` is the spec-side prompt. The kwarg is
    informational at the call site, the loader treats spec as the
    default file name."""
    default_content = load_role_prompt("reviewer")
    spec_content = load_role_prompt("reviewer", target="spec_pr")
    assert spec_content == default_content


def test_load_role_prompt_impl_target_loads_impl_file_when_present() -> None:
    """``target="impl_pr"`` MUST load ``<role>_impl.md`` when that
    file exists. This is the routing the bug fix hinges on — without
    it, the Reviewer-on-impl reads the spec prompt."""
    impl_content = load_role_prompt("reviewer", target="impl_pr")
    assert impl_content.strip(), "reviewer_impl.md should load for impl_pr target"
    # Sanity: impl content differs from spec content (otherwise the
    # routing is a no-op and the bug is unfixed).
    spec_content = load_role_prompt("reviewer", target="spec_pr")
    assert impl_content != spec_content, (
        "reviewer_impl.md must differ from reviewer.md, or the routing "
        "isn't actually doing anything for the bug fix"
    )


def test_load_role_prompt_impl_target_falls_back_to_canonical_when_no_impl_file() -> None:
    """``target="impl_pr"`` MUST fall back gracefully to ``<role>.md``
    when ``<role>_impl.md`` doesn't exist. Roles without impl variants
    (Planner today) MUST keep working when callers pass target."""
    # Planner has no planner_impl.md; impl target should fall back.
    impl_content = load_role_prompt("planner", target="impl_pr")
    default_content = load_role_prompt("planner")
    assert impl_content == default_content


def test_load_role_prompt_unknown_target_falls_back_to_canonical() -> None:
    """An unrecognized target string (typo, future addition) MUST NOT
    raise — it falls back to the canonical role file. This keeps the
    routing forgiving at call sites that pass dynamic strings; the
    role-side precondition gate is where wrong-target errors should
    surface, not in the prompt loader."""
    content = load_role_prompt("reviewer", target="unknown_target")
    default_content = load_role_prompt("reviewer")
    assert content == default_content


def test_compose_role_prompt_forwards_target_to_loader() -> None:
    """``compose_role_prompt(target=...)`` MUST forward target to
    ``load_role_prompt``. Without this, the role's contract layer
    silently falls back to the spec prompt even when the caller asked
    for impl. This is the seam that bug-fixed roles consume.

    Compares the composed prompt against both targets directly. When
    Tasks 2/3 land the impl files, ``composed_impl`` and
    ``composed_spec`` will differ — proving the forwarding works.
    Until then, the tail assertion (composed_impl ends with the impl-
    loaded role content) catches forwarding regressions even while
    the two compose outputs are byte-identical.
    """
    composed_impl = compose_role_prompt(
        role="reviewer", target="impl_pr", superpowers=[]
    )
    composed_spec = compose_role_prompt(
        role="reviewer", target="spec_pr", superpowers=[]
    )
    # The composed prompt must end with whatever load_role_prompt
    # returned for this target. If compose silently dropped target,
    # composed_impl would end with the spec content even when the
    # impl file exists (after Task 2/3 land). This assertion catches
    # the forwarding regression directly.
    assert composed_impl.endswith(
        load_role_prompt("reviewer", target="impl_pr")
    )
    assert composed_spec.endswith(
        load_role_prompt("reviewer", target="spec_pr")
    )


@pytest.mark.parametrize("impl_role", ["reviewer", "fixer"])
def test_impl_prompt_file_exists_for_target_roles(impl_role: str) -> None:
    """Both Reviewer and Fixer MUST have an impl-variant prompt file.
    This is the resource-packaging guard: forgetting either file means
    the daemon's RUN_REVIEWER_IMPL / RUN_FIXER_IMPL silently falls
    back to the spec prompt — same bug, different symptom."""
    impl_path = (
        resources.files("foreman.prompts")
        .joinpath(f"{impl_role}_impl.md")
    )
    assert impl_path.is_file(), (
        f"{impl_role}_impl.md must exist in packages/foreman/src/foreman/"
        "prompts/ — the impl-target loader resolves to that path"
    )
    content = impl_path.read_text(encoding="utf-8")
    assert content.strip(), f"{impl_role}_impl.md must not be empty"
