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

from pydantic import BaseModel, Field, model_validator

from foreman.roles._escalation_comment import EscalationComment


class Finding(BaseModel):
    """One issue the Reviewer found in the spec under review."""

    severity: Literal["critical", "important", "minor"] = Field(
        ...,
        description=(
            "Severity bucket per the prompt's severity rubric. critical: "
            "Worker cannot execute. important: Worker probably mis-builds. "
            "minor is accepted by the Literal for legacy compatibility, "
            "but ReviewerOutput's model_validator rejects minor entries — "
            "minor observations belong in review_comment prose, not in "
            "structured findings."
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
            "any important finding → needs_fix; otherwise clean. (Changed "
            "from 'two+ important' — any important now blocks, because "
            "'important but clean' accumulates spec-vs-impl drift.)"
        ),
    )
    review_comment: str = Field(
        ...,
        description=(
            "Human-readable review prose Foreman posts as the PR review "
            "comment. Opens with the outcome; cites specific evidence. "
            "Minor observations live here in prose, not in findings."
        ),
    )
    findings: list[Finding] = Field(
        default_factory=list,
        description=(
            "Structured findings. MUST contain ONLY critical and important "
            "entries (no minor — those go in review_comment prose). MUST be "
            "empty when outcome is clean. MUST be non-empty when outcome is "
            "needs_fix. The model_validator enforces all three rules."
        ),
    )
    confidence: Literal["high", "medium", "low"] = Field(
        default="medium",
        description="Reviewer's self-rated confidence in the outcome.",
    )
    escalation_comment: EscalationComment | None = Field(
        default=None,
        description=(
            "Required-iff ``confidence == 'low'``. The Reviewer LLM "
            "populates this when its confidence in the outcome is low; "
            "Foreman core renders + posts it on the issue. The Reviewer "
            "never self-escalates to NEEDS_HELP — its surface is "
            "low-confidence on CLEAN / NEEDS_FIX. Added by foreman#367."
        ),
    )

    @model_validator(mode="after")
    def _enforce_finding_contract(self) -> ReviewerOutput:
        """Reject shapes that contradict the Reviewer prompt contract.

        Three rules, all derived from the prompt's outcome rubric:

        1. No `minor` findings in the structured list — they belong in
           `review_comment` prose. Filing them creates unbounded
           per-ticket nit accumulation with no draining mechanism.
        2. `outcome: clean` ⇔ `findings` empty. A `clean` outcome with
           findings is a self-contradiction; an empty findings list with
           `needs_fix` leaves the Fixer with nothing to work on.
        3. `outcome: needs_fix` ⇔ `findings` non-empty (same axis).

        Raises ``ValueError`` (Pydantic re-raises as
        ``ValidationError``) so the SDK round-trips the LLM and asks it
        to fix the shape, rather than dispatching a contradictory
        review to the Fixer or the label transition.
        """
        bad_severities = [f.severity for f in self.findings if f.severity == "minor"]
        if bad_severities:
            raise ValueError(
                f"findings contains {len(bad_severities)} minor entry/entries; "
                "minor observations belong in review_comment prose, not "
                "structured findings"
            )
        if self.outcome == "clean" and self.findings:
            raise ValueError(
                f"outcome is 'clean' but findings is non-empty "
                f"({len(self.findings)} entries); a clean review has no "
                "structured findings — move them to review_comment or "
                "set outcome to 'needs_fix'"
            )
        if self.outcome == "needs_fix" and not self.findings:
            raise ValueError(
                "outcome is 'needs_fix' but findings is empty; the Fixer "
                "needs at least one structured finding to act on"
            )
        # foreman#367: low-confidence outcome must carry an
        # escalation_comment so Foreman core can render the
        # operator-visible comment.
        if self.confidence == "low" and self.escalation_comment is None:
            raise ValueError(
                "confidence='low' requires escalation_comment to be "
                "populated (why / what_tried / what_would_unblock); "
                "the foreman runtime posts this as an operator-visible "
                "comment on the issue."
            )
        return self


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
