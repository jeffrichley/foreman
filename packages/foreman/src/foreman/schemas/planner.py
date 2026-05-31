"""Planner role's structured output schema.

The Planner returns this after writing the spec PR. It is persisted to
SQLite for audit + replay. Per the B-strict forwarding rule, this is NOT
automatically forwarded to downstream nodes — the spec PR contents ARE
the contract for the Reviewer.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PlannerOutput(BaseModel):
    """Structured result from the Planner role."""

    pr_url: str = Field(..., description="Full GitHub URL of the created spec PR")
    pr_number: int = Field(..., description="PR number (integer)")
    branch_name: str = Field(..., description="Branch name the PR is on")
    summary: str = Field(..., description="One-paragraph summary of the spec approach")
    considered_alternatives: list[str] = Field(
        default_factory=list,
        description="Approaches considered and rejected, for the audit log",
    )
    confidence: Literal["high", "medium", "low"] = Field(
        default="medium",
        description="Planner's self-rated confidence in the spec approach",
    )
