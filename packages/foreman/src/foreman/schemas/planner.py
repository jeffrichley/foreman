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

from pydantic import BaseModel, Field

from foreman.git_host import PRRef


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


class PlannerRunResult(BaseModel):
    """Everything about a Planner run — what the LLM produced and what core did.

    Returned by :func:`foreman.roles.planner.run_planner` to its caller
    (CLI, daemon, replay tooling). Persisted to SQLite for audit.
    """

    llm_output: PlannerOutput
    pr: PRRef

    model_config = {"arbitrary_types_allowed": True}
