"""Planner role's structured output + run-result schemas.

The Planner LLM returns a :class:`PlannerOutput` (spec content + PR
metadata). Foreman core then performs the deterministic git/host operations
(commit, push, open PR, advance label) and returns a
:class:`PlannerRunResult` to its caller. Both types persist to SQLite for
audit + replay.

Per the B-strict forwarding rule, neither is automatically forwarded to
downstream nodes — the spec PR contents ARE the contract for the Reviewer.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from foreman.git_host import PRRef
from foreman.roles._escalation_comment import EscalationComment


class PlannerOutput(BaseModel):
    """What the Planner LLM produces.

    Foreman core consumes this to perform the deterministic host operations
    (write the spec file, commit, push, open the PR, advance the label).
    The LLM does NOT touch the filesystem or run git/host commands —
    those are core's responsibility.
    """

    spec_doc_content: str = Field(
        ...,
        description="Full markdown content of the spec doc",
    )
    pr_title: str = Field(
        ...,
        description="One-line PR title, conventional commit shape",
    )
    pr_body: str = Field(
        ...,
        description="2-4 sentence PR body + link to spec doc",
    )
    summary: str = Field(
        ...,
        description="One-line summary for audit log",
    )
    considered_alternatives: list[str] = Field(
        default_factory=list,
        description="Approaches considered and rejected, for the audit log",
    )
    confidence: Literal["high", "medium", "low"] = Field(
        default="medium",
        description="Planner's self-rated confidence in the spec approach",
    )
    escalation_comment: EscalationComment | None = Field(
        default=None,
        description=(
            "Required-iff ``confidence == 'low'``. The Planner LLM "
            "populates this when self-escalating; Foreman core renders "
            "+ posts it on the issue. A ``pydantic.model_validator`` "
            "enforces the requirement so a slip surfaces at "
            "schema-validation time rather than as a missing GitHub "
            "comment. Added by foreman#367."
        ),
    )

    @model_validator(mode="after")
    def _enforce_escalation_comment_required(self) -> PlannerOutput:
        """Require ``escalation_comment`` when ``confidence == 'low'``.

        Centralized here (not in the prompt) because the contract is a
        wire-level invariant Foreman core relies on when rendering the
        operator-visible escalation comment. A prompt slip that produced
        ``confidence='low'`` without ``escalation_comment`` would
        otherwise reach the orchestrator and fall through to the
        fallback shape that names the slip in the rendered body.
        """
        if self.confidence == "low" and self.escalation_comment is None:
            raise ValueError(
                "confidence='low' requires escalation_comment to be "
                "populated (why / what_tried / what_would_unblock); "
                "the foreman runtime posts this as an operator-visible "
                "comment on the issue."
            )
        return self


class PlannerRunResult(BaseModel):
    """Everything about a Planner run — what the LLM produced and what core did.

    Returned by :func:`foreman.roles.planner.run_planner` to its caller
    (CLI, daemon, replay tooling). Persisted to SQLite for audit.
    """

    llm_output: PlannerOutput
    pr: PRRef
    final_labels: list[str] = Field(
        ...,
        description=(
            "Sorted list of foreman labels on the originating issue "
            "after the role's transitions ran — the authoritative "
            "post-run label set, computed in-process from the role's "
            "known mutations, not via a post-mutation host re-read. "
            "Consumed by ``DaemonRunners.run_*`` to populate the "
            "worker's ``RoleResult.new_labels`` without the eventual-"
            "consistency hazard of a GitHub GET right after a write."
        ),
    )

    model_config = {"arbitrary_types_allowed": True}
