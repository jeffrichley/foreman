"""Outcome — the role-to-daemon reporting contract.

Every role's CLI emits one terminal line on stdout shaped as

    FOREMAN_OUTCOME:{"schema_version":1,"kind":"...","confidence":"...",...}

The daemon's verify hook scans stdout in reverse for the marker, parses the
suffix, and validates against ``Outcome``. See the spec section
"Outcome JSON — role-side reporting contract" for the per-role kind matrix.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class OutcomeKind(str, Enum):
    CLEAN = "clean"
    NEEDS_FIX = "needs_fix"
    BLOCKED = "blocked"
    NEEDS_HELP = "needs_help"
    ERROR = "error"


class OutcomeConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Finding(BaseModel):
    severity: Literal["critical", "important", "minor"]
    location: str = Field(..., description="file:line or 'general'")
    description: str


class OutcomeArtifacts(BaseModel):
    pr_url: str | None = None
    pr_number: int | None = None
    commit_sha: str | None = None
    branch: str | None = None
    spec_doc_path: str | None = None


class Outcome(BaseModel):
    schema_version: Literal[1] = 1
    kind: OutcomeKind
    confidence: OutcomeConfidence
    summary: str = Field(..., max_length=500)
    findings: list[Finding] = Field(default_factory=list)
    artifacts: OutcomeArtifacts = Field(default_factory=OutcomeArtifacts)
    raw_role_output_path: str | None = None
