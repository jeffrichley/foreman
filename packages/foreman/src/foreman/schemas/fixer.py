"""Fixer role's structured output schema.

The Fixer LLM consumes the Reviewer's findings on a spec PR, applies
addressable edits to the spec doc, and returns a :class:`FixerOutput`
carrying its outcome, a human-readable PR comment summary, a structured
record of what was addressed and what was deferred, and a self-rated
confidence.

Foreman core consumes this to:
- post ``fix_comment`` as a PR **comment** (``pr.create_issue_comment``),
  NOT a review — the Fixer is acting on the Reviewer's feedback, not
  re-reviewing the spec,
- advance the issue's label (back to ``foreman:spec-review`` on
  ``fixed``, or attach ``foreman:needs-help`` on ``incomplete``),
- log a JSONL stats line for proto-version of foreman#11 lifecycle stats.

Per the B-strict forwarding rule, only the artifacts the next node
actually needs travel forward; the structured ``addressed_findings`` /
``unaddressed_findings`` lists are for the audit log + stats, not the
Reviewer's second pass (which re-derives findings from scratch).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

UnaddressedReason = Literal[
    "needs_info",
    "needed_remediation_wrong",
    "requires_git_surgery",
    "requires_worker_codebase_access",
]


class AddressedFinding(BaseModel):
    """One Reviewer finding the Fixer applied an edit for."""

    target: str = Field(
        ...,
        description=(
            "The Reviewer finding's target field, copied so the audit log can "
            "join the addressed item back to the original finding."
        ),
    )
    summary: str = Field(
        ...,
        description=(
            "One-line description of what the Fixer changed in the spec doc, "
            "concrete enough to verify by reading the diff."
        ),
    )


class UnaddressedFinding(BaseModel):
    """One Reviewer finding the Fixer chose not to address, with reason."""

    target: str = Field(
        ...,
        description=("The Reviewer finding's target field, copied for audit-log join."),
    )
    severity: Literal["critical", "important", "minor"] = Field(
        ...,
        description=(
            "Severity of the original Reviewer finding. Used by Foreman core "
            "and downstream stats to compute the outcome."
        ),
    )
    reason: UnaddressedReason = Field(
        ...,
        description=(
            "Why the Fixer did not address this finding. ``needs_info`` — "
            "spec lacks the context to fix it. ``needed_remediation_wrong`` "
            "— the Reviewer's ``needed`` field would make the spec worse "
            "(REQUIRES a 1-paragraph rationale). ``requires_git_surgery`` — "
            "drift findings (files outside the spec doc); v1 does not attempt. "
            "``requires_worker_codebase_access`` — the actual fix lives in "
            "the code, not the spec doc."
        ),
    )
    rationale: str = Field(
        ...,
        description=(
            "1-paragraph explanation of why this finding is unaddressed. "
            "Required for every reason, but especially load-bearing for "
            "``needed_remediation_wrong`` — cite the conflicting evidence "
            "(issue line, spec section, or codebase fact)."
        ),
    )


class CommitMade(BaseModel):
    """One git commit the Fixer made while applying findings."""

    sha: str = Field(
        ...,
        description="The commit SHA from the worktree after the commit landed.",
    )
    summary: str = Field(
        ...,
        description="One-line description of the commit (e.g. the commit title).",
    )
    findings_addressed: list[str] = Field(
        default_factory=list,
        description=(
            "Targets of the findings this commit addressed. Lets the audit "
            "log join commit→finding without re-parsing the commit body."
        ),
    )


class FixerOutput(BaseModel):
    """What the Fixer LLM produces.

    Foreman core consumes this to post the ``fix_comment`` as a PR comment
    (not a review), advance the issue label based on ``outcome``, and log
    a JSONL stats line.

    Outcome derivation rule (enforced in the prompt, mirrored in stats):
    - Any unresolved critical → ``incomplete``
    - Any unresolved important → ``incomplete``
    - All critical + important resolved (regardless of skipped minors) →
      ``fixed``
    """

    outcome: Literal["fixed", "incomplete"] = Field(
        ...,
        description=(
            "Derived mechanically from unaddressed_findings: any unresolved "
            "critical or important → ``incomplete``; otherwise ``fixed``. "
            "Skipped minor findings never block ``fixed``."
        ),
    )
    fix_comment: str = Field(
        ...,
        description=(
            "Human-readable markdown summary Foreman posts as a PR comment "
            "(not a review). Opens with the outcome; lists what was fixed "
            "and what was deferred with one-sentence reasons."
        ),
    )
    commits_made: list[CommitMade] = Field(
        default_factory=list,
        description=(
            "Commits the Fixer made in the worktree. Usually one bundled "
            "commit; multiple only when changes are genuinely orthogonal."
        ),
    )
    addressed_findings: list[AddressedFinding] = Field(
        default_factory=list,
        description=(
            "Reviewer findings the Fixer applied an edit for. Empty list is "
            "valid only when every finding was unaddressable."
        ),
    )
    unaddressed_findings: list[UnaddressedFinding] = Field(
        default_factory=list,
        description=(
            "Reviewer findings the Fixer chose not to address, with the "
            "reason and rationale for each. MUST contain at least one entry "
            "with severity ``critical`` or ``important`` when outcome is "
            "``incomplete``."
        ),
    )
    confidence: Literal["high", "medium", "low"] = Field(
        default="medium",
        description="Fixer's self-rated confidence in the edits applied.",
    )


class FixerRunResult(BaseModel):
    """What :func:`foreman.roles.fixer.run_fixer` returns to its caller.

    Bundles the LLM's :class:`FixerOutput` with orchestrator-side state
    (the fix-attempt counter) so the CLI can render
    ``{outcome}: {attempt}/3 attempt, ...`` without re-querying the host.
    Mirrors the Planner's :class:`~foreman.schemas.planner.PlannerRunResult`
    pattern.
    """

    llm_output: FixerOutput = Field(
        ...,
        description="The structured output the Fixer LLM produced.",
    )
    attempt: int = Field(
        ...,
        description=(
            "1-based fix-attempt counter for this run (matches the "
            "``foreman:fix-attempt-N`` label set on the issue at run start)."
        ),
    )
