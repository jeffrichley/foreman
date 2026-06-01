"""Worker role's structured output schema.

The Worker LLM consumes a spec-ready issue + spec doc and implements the
described code change. It returns a :class:`WorkerOutput` carrying:

- the outcome (``implemented`` / ``incomplete`` / ``spec_invalid``),
- a human-readable comment (the impl PR body when implemented; the
  spec-PR comment body when spec_invalid),
- structured per-sub-request records of what was implemented or skipped,
- the commits the Worker made,
- a ``did_check_pass`` honesty gate (Worker's own claim — the
  orchestrator runs ``check_command`` independently and overrides if
  they disagree),
- a one-paragraph ``check_output_summary`` for incomplete-with-failures,
- a self-rated ``confidence``.

Foreman core consumes this to:
- open the impl PR (head=``foreman/impl-<N>``, base=``foreman/issue-<N>``)
  via PyGithub iff ``outcome == "implemented"``,
- post ``spec_invalid_reason`` as a comment on the spec PR iff
  ``outcome == "spec_invalid"``,
- advance the issue's label deterministically per the brief's outcome
  table (D6, D7),
- log a JSONL stats line per ``foreman.stats.log_worker_run``.

The Worker's ``commits_made`` shape mirrors
:class:`foreman.schemas.fixer.CommitMade` exactly. Reusing the Fixer
class wasn't worth the cross-module import in v1 — the shape is small
and the duplication keeps the Worker schema self-contained.

Outcome derivation rule (enforced in the prompt, mirrored by the
orchestrator's post-Worker verification):

- Worker reports ``check_command`` failed on tests it touched →
  ``incomplete``,
- Worker reports the spec contradicts the issue / itself / the codebase
  in a way no edit could fix → ``spec_invalid``,
- otherwise → ``implemented``.

The orchestrator runs ``check_command`` once before dispatch (baseline)
and once after (post). New failures (``post - baseline``) that the Worker
introduced override an ``implemented`` claim back to ``incomplete``;
that decision lives in the orchestrator, not the LLM.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

SkippedReason = Literal[
    "needs_info",
    "spec_unclear",
    "out_of_scope",
    "spec_invalid_partial",
]


class ImplementedSubRequest(BaseModel):
    """One spec sub-request the Worker implemented end-to-end."""

    spec_reference: str = Field(
        ...,
        description=(
            "Pointer back into the spec doc: 'Sub-request 3', 'Acceptance "
            "criterion 2', or the literal heading text. Specific enough that "
            "a reviewer can locate the item in the spec without grepping."
        ),
    )
    summary: str = Field(
        ...,
        description=(
            "One-line description of what the Worker did for this sub-request, "
            "concrete enough to verify by reading the diff."
        ),
    )
    files_touched: list[str] = Field(
        default_factory=list,
        description=(
            "Repo-relative paths of every file the Worker created or modified "
            "for this sub-request."
        ),
    )
    tests_added: list[str] = Field(
        default_factory=list,
        description=(
            "Names of tests the Worker added for this sub-request "
            "(e.g., 'test_x_returns_y'). Empty list is valid for structural "
            "changes that don't need new tests."
        ),
    )


class SkippedSubRequest(BaseModel):
    """One spec sub-request the Worker chose not to implement, with reason.

    ``reason`` discipline (mirrors the Fixer's unaddressed-reason rubric):

    - ``needs_info``: spec lacks context required to implement without
      inventing facts. Rationale must name what's missing.
    - ``spec_unclear``: spec is ambiguous in a way the Worker can't
      disambiguate from the codebase. Rationale must cite the ambiguous
      sentence / bullet.
    - ``out_of_scope``: the sub-request describes work clearly outside the
      named issue (Planner overreach). Rationale must point to the issue
      line that confirms this.
    - ``spec_invalid_partial``: the sub-request is internally consistent
      but a sibling sub-request makes it impossible to land cleanly.
      Rationale must name the conflicting sibling.

    A skip is NOT an outcome of ``spec_invalid``. A whole-spec
    contradiction → ``outcome: spec_invalid`` + ``spec_invalid_reason``.
    Per-sub-request skips coexist with ``outcome: implemented`` (when the
    skipped pieces are minor) or ``outcome: incomplete`` (when they aren't).
    """

    spec_reference: str = Field(
        ...,
        description=(
            "Pointer back into the spec doc; same format as "
            ":class:`ImplementedSubRequest.spec_reference`."
        ),
    )
    reason: SkippedReason = Field(
        ...,
        description=(
            "Why the sub-request was skipped. ``needs_info`` — spec missing "
            "context. ``spec_unclear`` — spec ambiguous. ``out_of_scope`` — "
            "describes work the issue did not request. ``spec_invalid_partial`` "
            "— blocked by sibling sub-request conflict."
        ),
    )
    rationale: str = Field(
        ...,
        description=(
            "1-paragraph explanation of why this sub-request is skipped. "
            "Required for every reason; cite the specific evidence "
            "(spec section, issue line, codebase fact) that justifies the skip."
        ),
    )


class CommitMade(BaseModel):
    """One git commit the Worker made while implementing the spec.

    Shape mirrors :class:`foreman.schemas.fixer.CommitMade`. Kept local
    so the Worker schema does not import from a sibling role.
    """

    sha: str = Field(
        ...,
        description="The commit SHA from the worktree after the commit landed.",
    )
    summary: str = Field(
        ...,
        description="One-line description of the commit (e.g. the commit title).",
    )
    files_changed: list[str] = Field(
        default_factory=list,
        description=(
            "Repo-relative paths of every file the commit touched. Lets the "
            "audit log join commit → sub-request without re-parsing the diff."
        ),
    )


class WorkerOutput(BaseModel):
    """What the Worker LLM produces.

    Foreman core consumes this to open the impl PR (``implemented``), post
    to the spec PR (``spec_invalid``), or keep the impl branch alive for
    human follow-up (``incomplete``); then advances the issue's label
    deterministically and writes a JSONL stats line.

    Required-iff-outcome rules (enforced by ``model_validator``):

    - ``implemented`` → ``pr_title`` and ``pr_body`` both non-empty;
      ``spec_invalid_reason`` must be ``None``,
    - ``spec_invalid`` → ``spec_invalid_reason`` non-empty;
      ``pr_title`` and ``pr_body`` must be ``None``,
    - ``incomplete`` → no PR opened either way; ``pr_title`` / ``pr_body``
      / ``spec_invalid_reason`` must all be ``None``.

    The Worker's ``did_check_pass`` is a self-report. The orchestrator
    runs ``check_command`` independently after the LLM returns and
    overrides ``outcome`` to ``incomplete`` when new failures appear that
    weren't in the baseline. Don't trust the LLM unconditionally; treat
    this field as the LLM's claim, not the ground truth.
    """

    outcome: Literal["implemented", "incomplete", "spec_invalid"] = Field(
        ...,
        description=(
            "Mechanically derived per the prompt's outcome-derivation rule. "
            "``implemented`` — work landed, ``check_command`` passed, ready "
            "for impl PR. ``incomplete`` — couldn't finish (check failures, "
            "blockers, SDK errors). ``spec_invalid`` — spec contradicts the "
            "issue / itself / codebase in a way no edit could fix."
        ),
    )
    work_comment: str = Field(
        ...,
        description=(
            "Human-readable markdown summary. When ``implemented``, this is "
            "audit-log prose (the impl PR body is in ``pr_body``). When "
            "``incomplete``, the rationale a human reads to triage. When "
            "``spec_invalid``, also-included context; the spec PR comment "
            "uses ``spec_invalid_reason`` as the body."
        ),
    )
    pr_title: str | None = Field(
        default=None,
        description=(
            "Conventional-commit-shaped subject for the impl PR. REQUIRED "
            "iff ``outcome == 'implemented'``; must be ``None`` otherwise."
        ),
    )
    pr_body: str | None = Field(
        default=None,
        description=(
            "Markdown body for the impl PR, following the prompt's PR body "
            "template (Implements #N, Spec ref, Spec PR ref, What was "
            "implemented, Acceptance criteria, Verification). REQUIRED "
            "iff ``outcome == 'implemented'``; must be ``None`` otherwise."
        ),
    )
    spec_invalid_reason: str | None = Field(
        default=None,
        description=(
            "1-2 paragraph explanation of why the spec is invalid, citing the "
            "specific contradiction (issue line / spec section / codebase fact). "
            "Posted verbatim as a comment on the spec PR. REQUIRED iff "
            "``outcome == 'spec_invalid'``; must be ``None`` otherwise."
        ),
    )
    commits_made: list[CommitMade] = Field(
        default_factory=list,
        description=(
            "Commits the Worker made in the worktree. Usually one bundled "
            "commit; multiple only when changes are genuinely orthogonal."
        ),
    )
    implemented_sub_requests: list[ImplementedSubRequest] = Field(
        default_factory=list,
        description=(
            "Sub-requests the Worker implemented end-to-end. Empty list is "
            "valid only when every sub-request was skipped or the spec was "
            "invalid."
        ),
    )
    skipped_sub_requests: list[SkippedSubRequest] = Field(
        default_factory=list,
        description=(
            "Sub-requests the Worker chose not to implement, with reason and "
            "rationale for each. A skip is per-sub-request; the whole-spec "
            "contradiction case uses ``outcome: spec_invalid`` instead."
        ),
    )
    did_check_pass: bool = Field(
        ...,
        description=(
            "Worker's self-report on whether ``check_command`` exited 0 on "
            "the worker's final tree. MANDATORY honesty gate. The "
            "orchestrator re-runs ``check_command`` after the Worker returns "
            "and overrides ``outcome`` if the truth disagrees with this field."
        ),
    )
    check_output_summary: str = Field(
        default="",
        description=(
            "One-paragraph summary of the final ``check_command`` run. "
            "Required content when ``did_check_pass`` is False (cite the "
            "failing test names); may be empty when pass."
        ),
    )
    confidence: Literal["high", "medium", "low"] = Field(
        default="medium",
        description="Worker's self-rated confidence in the implementation.",
    )

    @model_validator(mode="after")
    def _enforce_outcome_required_fields(self) -> WorkerOutput:
        """Enforce the required-iff-outcome contract.

        Centralized here (not in the prompt) because the contract is a
        wire-level invariant Foreman core relies on when opening / not
        opening the impl PR. A prompt slip that produced a ``spec_invalid``
        with missing ``spec_invalid_reason`` would otherwise reach the
        orchestrator and surface as a confusing post-hoc error.
        """
        if self.outcome == "implemented":
            if not self.pr_title:
                raise ValueError(
                    "outcome='implemented' requires non-empty pr_title"
                )
            if not self.pr_body:
                raise ValueError(
                    "outcome='implemented' requires non-empty pr_body"
                )
            if self.spec_invalid_reason is not None:
                raise ValueError(
                    "outcome='implemented' must not set spec_invalid_reason"
                )
        elif self.outcome == "spec_invalid":
            if not self.spec_invalid_reason:
                raise ValueError(
                    "outcome='spec_invalid' requires non-empty spec_invalid_reason"
                )
            if self.pr_title is not None or self.pr_body is not None:
                raise ValueError(
                    "outcome='spec_invalid' must not set pr_title or pr_body"
                )
        else:  # incomplete
            if (
                self.pr_title is not None
                or self.pr_body is not None
                or self.spec_invalid_reason is not None
            ):
                raise ValueError(
                    "outcome='incomplete' must not set pr_title, pr_body, "
                    "or spec_invalid_reason"
                )
        return self


class WorkerRunResult(BaseModel):
    """What :func:`foreman.roles.worker.run_worker` returns to its caller.

    Bundles the LLM's :class:`WorkerOutput` with orchestrator-side state
    (the impl-attempt counter, the opened impl PR's URL when applicable,
    and the orchestrator-verified ``did_check_pass``) so the CLI can
    render its one-line summary without re-querying the host.
    """

    llm_output: WorkerOutput = Field(
        ...,
        description="The structured output the Worker LLM produced.",
    )
    attempt: int = Field(
        ...,
        description=(
            "1-based impl-attempt counter for this run (matches the "
            "``foreman:impl-attempt-N`` label set on the issue at run start)."
        ),
    )
    pr_url: str | None = Field(
        default=None,
        description=(
            "URL of the impl PR opened by the orchestrator. Set iff "
            "``llm_output.outcome == 'implemented'`` AND the orchestrator's "
            "post-Worker verification confirmed no new test failures."
        ),
    )
    final_did_check_pass: bool = Field(
        ...,
        description=(
            "Orchestrator-verified ground truth for whether ``check_command`` "
            "passed after the Worker returned, minus baseline failures. May "
            "disagree with ``llm_output.did_check_pass`` if the Worker was "
            "wrong; the orchestrator's truth wins."
        ),
    )
