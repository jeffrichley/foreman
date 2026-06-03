"""Reviewer role's structured output schema.

The Reviewer LLM reads a spec PR opened by the Planner and returns a
:class:`ReviewerOutput` carrying its outcome (clean / needs_fix), a
human-readable PR review comment, a list of structured findings, and a
self-rated confidence.

Foreman core consumes this to:
- post ``review_comment`` on the spec PR (as foreman-reviewer-bot),
- advance the label deterministically (``foreman:spec-review`` →
  ``foreman:ready-for-worker`` or ``foreman:fix``),
- if ``outcome == "needs_fix"``, hand ``findings`` off to the Fixer role.

Per the B-strict forwarding rule, only the artifacts the next node actually
needs travel forward; ``confidence`` is for the audit log, not the Fixer.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Finding(BaseModel):
    """One issue the Reviewer found in the spec under review."""

    severity: Literal["critical", "important", "minor"] = Field(
        ...,
        description=(
            "Severity bucket per the prompt's severity rubric. critical: "
            "Worker cannot execute. important: Worker probably mis-builds. "
            "minor: spec is rough but executable."
        ),
    )
    target: str = Field(
        ...,
        description=(
            "Spec section, file path, or acceptance criterion the finding "
            "applies to. Must be specific (e.g. 'Acceptance criteria bullet 3' "
            "or 'packages/foo/src/foo/bar.py'), not 'the spec'."
        ),
    )
    issue: str = Field(
        ...,
        description="What is wrong, concretely. One or two sentences.",
    )
    needed: str = Field(
        ...,
        description="What would fix it, concretely. One or two sentences.",
    )


class ReviewerOutput(BaseModel):
    """What the Reviewer LLM produces.

    Foreman core consumes this to post the review comment, advance the
    label, and (if ``outcome == "needs_fix"``) dispatch the Fixer with
    ``findings`` as the work list.
    """

    outcome: Literal["clean", "needs_fix"] = Field(
        ...,
        description=(
            "Derived mechanically from severity counts: any critical OR "
            "two+ important findings → needs_fix; otherwise clean."
        ),
    )
    review_comment: str = Field(
        ...,
        description=(
            "Human-readable review prose Foreman posts as the PR review "
            "comment. Opens with the outcome; cites specific evidence."
        ),
    )
    findings: list[Finding] = Field(
        default_factory=list,
        description=(
            "Structured findings. MAY be non-empty when outcome is clean "
            "(minor-only findings don't block). MUST be non-empty when "
            "outcome is needs_fix."
        ),
    )
    confidence: Literal["high", "medium", "low"] = Field(
        default="medium",
        description="Reviewer's self-rated confidence in the outcome.",
    )


class ReviewerRunResult(BaseModel):
    """What :func:`foreman.roles.reviewer.run_reviewer` returns.

    Bundles the LLM's :class:`ReviewerOutput` with the
    deterministic post-run label set computed in-process from
    the role's known transitions. Mirrors
    :class:`~foreman.schemas.planner.PlannerRunResult` /
    :class:`~foreman.schemas.fixer.FixerRunResult` /
    :class:`~foreman.schemas.worker.WorkerRunResult`.

    The ``final_labels`` field is the fix for foreman#91:
    ``DaemonRunners.run_reviewer`` used to populate the
    worker's ``RoleResult.new_labels`` via a fresh
    ``host.get_issue_labels`` GET, which raced GitHub's
    eventual-consistency window and produced stale-snapshot
    re-dispatches at the next worker iteration.
    """

    llm_output: ReviewerOutput = Field(
        ...,
        description="The structured output the Reviewer LLM produced.",
    )
    final_labels: list[str] = Field(
        ...,
        description=(
            "Sorted list of foreman labels on the originating issue "
            "after the Reviewer's clean→spec-ready/ready-for-merge "
            "or needs_fix→spec-fix/impl-fix transition ran. The "
            "authoritative post-run label set, computed in-process; "
            "not a remote re-read."
        ),
    )
