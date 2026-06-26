"""Outcome — the role-to-daemon reporting contract.

Every role's CLI emits one terminal line on stdout shaped as

    FOREMAN_OUTCOME:{"schema_version":1,"kind":"...","confidence":"...",...}

The daemon's verify hook scans stdout in reverse for the marker, parses the
suffix, and validates against ``Outcome``. See the spec section
"Outcome JSON — role-side reporting contract" for the per-role kind matrix.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class OutcomeKind(StrEnum):
    CLEAN = "clean"
    NEEDS_FIX = "needs_fix"
    BLOCKED = "blocked"
    NEEDS_HELP = "needs_help"
    ERROR = "error"
    # foreman#361: emitted by the role CLIs when the failure is
    # classified as an Anthropic-side transient transport blip
    # (5xx / 429 / connection refused / transport-level timeout).
    # The state machine treats this kind specially — see the
    # ``RoleDispatchState`` Template Method override and the
    # exempted skip in ``count_consecutive_same_state``. Per-role
    # matrix is documented in
    # ``docs/superpowers/specs/2026-06-13-foreman-v4-substrate-redesign-design.md``
    # (section ``Per-role kind matrix``); every role-dispatch state
    # may emit it.
    TRANSIENT_PROVIDER_ERROR = "transient_provider_error"


class OutcomeConfidence(StrEnum):
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
    # Phase 8d.17 / foreman#315: freeform diagnostic detail bag.
    # Roles populate with their own legacy-output fields (Worker:
    # work_comment / check_output_summary / did_check_pass / etc.;
    # Reviewer: pr_review_comment + outcome + confidence; Fixer:
    # fix_comment + addressed/unaddressed breakdown). State-machine
    # consumers continue reading kind/summary/artifacts — details is
    # purely additive observability so operators don't have to spelunk
    # worktrees + stats jsonl to find the actual cause of a NEEDS_HELP
    # / NEEDS_FIX outcome (the algokit#21 dogfood needed a 3-step
    # expedition to find the cause was "Justfile has no `check`
    # recipe" — now that detail rides on the Outcome). V0: freeform
    # dict; V1 may tighten to a per-role discriminated union (stretch
    # goal in foreman#315, deferred).
    details: dict[str, Any] = Field(default_factory=dict)


OUTCOME_MARKER = "FOREMAN_OUTCOME:"


class OutcomeMissingError(Exception):
    """Stdout did not contain the FOREMAN_OUTCOME: marker."""


class OutcomeMalformedError(Exception):
    """FOREMAN_OUTCOME: marker present but the suffix is not parseable JSON."""

    def __init__(self, raw: str) -> None:
        super().__init__(f"malformed outcome JSON: {raw!r}")
        self.raw = raw


class OutcomeInvalidError(Exception):
    """FOREMAN_OUTCOME: JSON parsed but failed schema validation."""

    def __init__(self, raw: str, pydantic_errors: object) -> None:
        super().__init__(f"invalid outcome: {pydantic_errors}")
        self.raw = raw
        self.pydantic_errors = pydantic_errors


def is_runaway_exempt(failure_phase: str | None, outcome_kind: OutcomeKind | None) -> bool:
    """Return True if a state-instance row should be skipped by the runaway-cap counter.

    Codifies the four exempt conditions for ``count_consecutive_same_state``:

    - ``failure_phase == "can_run"``: the ticket was held and the state never
      actually executed; not runaway-defense signal (Phase 8d.15 Bug F4).
    - ``failure_phase == "crash_recovery"``: a daemon restart closed this row as
      an orphan; not runaway-defense signal (C1 crash-recovery reconciliation).
    - ``outcome_kind == OutcomeKind.BLOCKED``: a legitimate async-polling
      self-loop; not runaway-defense signal (Phase 8d.18).
    - ``outcome_kind == OutcomeKind.TRANSIENT_PROVIDER_ERROR``: an Anthropic-side
      transient blip handled by the backoff scheduler; not runaway-defense
      signal (foreman#361).

    Single source of truth for issue #455 — both InMemoryTicketRepository and
    PostgresTicketRepository delegate to this predicate.
    """
    if failure_phase == "can_run":
        return True
    if failure_phase == "crash_recovery":
        return True
    if outcome_kind == OutcomeKind.BLOCKED:
        return True
    if outcome_kind == OutcomeKind.TRANSIENT_PROVIDER_ERROR:
        return True
    return False


def is_transient_error_exempt(failure_phase: str | None, outcome_kind: OutcomeKind | None) -> bool:
    """Return True if a state-instance row should be skipped by the transient-provider-error counter.

    Codifies the two exempt conditions for ``count_consecutive_transient_provider_errors``:

    - ``failure_phase == "can_run"``: same precedent as ``is_runaway_exempt`` —
      can_run failures don't count and don't break the run.
    - ``outcome_kind is None``: the in-flight row (``mark_execute_completed``
      has not yet been called) — skipped so the counter returns prior completed
      transient attempts, not "current row, NULL outcome, break → 0" (foreman#361).

    Single source of truth for issue #455 — both InMemoryTicketRepository and
    PostgresTicketRepository delegate to this predicate.
    """
    if failure_phase == "can_run":
        return True
    if outcome_kind is None:
        return True
    return False


def parse_outcome_from_stdout(stdout: str) -> Outcome:
    """Scan stdout in reverse for the FOREMAN_OUTCOME: marker; parse + validate.

    Reverse scan so log lines that happen to contain JSON earlier in stdout
    cannot poison the parse. If multiple marker lines are present, the last
    one wins — roles that re-emit on retry overwrite the earlier value.
    """
    import json

    from pydantic import ValidationError

    for line in reversed(stdout.splitlines()):
        idx = line.find(OUTCOME_MARKER)
        if idx == -1:
            continue
        suffix = line[idx + len(OUTCOME_MARKER):].strip()
        try:
            payload = json.loads(suffix)
        except json.JSONDecodeError as exc:
            raise OutcomeMalformedError(suffix) from exc
        try:
            return Outcome.model_validate(payload)
        except ValidationError as exc:
            raise OutcomeInvalidError(suffix, exc.errors()) from exc
    raise OutcomeMissingError("no FOREMAN_OUTCOME: marker found in stdout")
