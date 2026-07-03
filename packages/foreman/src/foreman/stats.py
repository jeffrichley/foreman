"""Append-only JSONL lifecycle stats — proto for foreman#11.

Each role run produces one JSON line under
``~/.foreman/stats/<owner>__<repo>/<role>.jsonl``. Lines are append-only,
schema-stable, and one-per-run so downstream tooling can sum / group /
chart without re-parsing in-flight state.

Cross-role envelope (foreman#233)
---------------------------------
Every role's JSONL row starts with the same 12-key
:class:`CommonEnvelope` prefix, in CommonEnvelope's declared field
order, followed by role-specific fields. The shared prefix lets
operators ``cat *.jsonl | jq ...`` across all four files without
deriving ``role`` from the filename, and lets ``head``-style queries
peek at the load-bearing fields (role / outcome / cost) in a
byte-identical position.

The ``role`` field is hard-coded inside each ``log_*_run`` function —
callers cannot pass the wrong role for a given file.

Path slug
---------
``<owner>/<repo>`` doesn't map cleanly to a single directory name on all
filesystems (Windows treats ``/`` as a separator; some bash tooling
treats ``__`` as a token boundary). We pick ``<owner>__<repo>`` as the
slug because:
  * it round-trips losslessly (no info dropped from the repo slug),
  * it stays one directory deep so ``ls ~/.foreman/stats`` shows every
    repo at a glance,
  * it avoids the cross-OS path quirks of a literal ``owner/repo`` dir.

If ``~/.foreman/stats/`` or the per-repo subdirectory doesn't exist,
we create them (parents=True, exist_ok=True). Best-effort: if the
filesystem refuses (e.g., the home dir is read-only in some test
sandboxes), the caller decides whether to propagate or swallow.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

# Reasons that contribute to ``disagreed_count`` in fixer stats — kept
# in sync with the ``UnaddressedReason`` literal in
# :mod:`foreman.schemas.fixer`.
_DISAGREEMENT_REASON = "needed_remediation_wrong"


class CommonEnvelope(BaseModel):
    """The 12-field prefix that every role's JSONL row starts with.

    foreman#233: previously each role's JSONL had its own bespoke key
    order, and Planner had no ``outcome`` field at all. Downstream
    tooling that wants to ``cat *.jsonl | jq '.outcome'`` across all
    four files needs a uniform shape; deriving ``role`` from the
    filename is fragile (file gets renamed, gets concatenated into a
    bigger log, etc.).

    The field order declared on this model IS the on-disk key order —
    every ``log_*_run`` function emits these keys first, in this order,
    before any role-specific fields. The ordering is asserted in
    ``test_common_envelope_uniform_across_roles``.

    Fields
    ------
    timestamp:
        ISO-8601 UTC instant the row was written (``datetime.now(UTC).isoformat()``).
    role:
        One of ``planner`` / ``reviewer`` / ``worker`` / ``fixer`` —
        hard-coded inside the matching ``log_*_run`` function so the
        caller cannot get it wrong.
    issue_number:
        Originating issue # the run was attached to.
    pr_number:
        The PR the run produced / reviewed / edited. ``None`` when the
        run produced no PR (Planner exception, Worker incomplete, etc.).
    outcome:
        Per-role enum value — see each ``log_*_run`` docstring for the
        allowed literals.
    duration_seconds:
        Wall-clock time the role run took, end to end, rounded to 3
        decimals at write time.
    input_tokens / output_tokens / total_cost_usd / model_usage /
    duration_ms / num_turns:
        Provider-reported per-call usage + SDK-computed cost. Defaults
        match what AnthropicSDKProvider emits when no ResultMessage
        arrived (zeros for ints, ``None`` for cost / model_usage).
    cache_creation_input_tokens / cache_read_input_tokens:
        Prompt-cache counters from ``ResultMessage.usage`` (foreman#244).
        The Anthropic API bills these at 25% and 10% of the regular
        input rate respectively. Sit between ``output_tokens`` and
        ``total_cost_usd`` so the four token counters cluster together
        in the on-disk JSONL row — operator ``jq`` selecting per-token
        columns gets all four in one peek. Default ``0`` (first turn
        of a fresh agent loop, or older SDK that didn't surface the
        fields).
    """

    timestamp: str
    role: Literal["planner", "reviewer", "worker", "fixer"]
    issue_number: int
    pr_number: int | None
    outcome: str
    duration_seconds: float
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    total_cost_usd: float | None
    model_usage: dict[str, Any] | None
    duration_ms: int
    num_turns: int


# Frozen tuple of CommonEnvelope keys in declaration order. Used to
# construct the on-disk JSONL line so the prefix shape is single-sourced
# from the Pydantic model — if the model gains / loses / reorders a
# field, the JSONL writers move with it.
_COMMON_ENVELOPE_FIELDS: tuple[str, ...] = tuple(CommonEnvelope.model_fields.keys())


def _default_stats_root() -> Path:
    """Return ``~/.foreman/stats`` (env-overridable for tests via
    ``FOREMAN_STATS_ROOT``).
    """
    override = os.environ.get("FOREMAN_STATS_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".foreman" / "stats"


def _repo_slug_to_dirname(repo_slug: str) -> str:
    """Convert ``owner/repo`` → ``owner__repo`` for filesystem use."""
    return repo_slug.replace("/", "__")


def _envelope_dict(
    *,
    role: Literal["planner", "reviewer", "worker", "fixer"],
    issue_number: int,
    pr_number: int | None,
    outcome: str,
    duration_seconds: float,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    total_cost_usd: float | None,
    model_usage: dict[str, Any] | None,
    duration_ms: int,
    num_turns: int,
) -> dict[str, Any]:
    """Build the CommonEnvelope key/value dict in declared order.

    Returns a plain ``dict`` rather than a Pydantic model so the caller
    can ``{**envelope, **role_specific}`` without going through
    ``model_dump`` on every write. Python 3.7+ dicts preserve insertion
    order, so the key sequence here is the on-disk JSONL key sequence.

    foreman#244: ``cache_creation_input_tokens`` and
    ``cache_read_input_tokens`` sit between ``output_tokens`` and
    ``total_cost_usd`` so the four token counters cluster together on
    disk.
    """
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "role": role,
        "issue_number": issue_number,
        "pr_number": pr_number,
        "outcome": outcome,
        "duration_seconds": round(duration_seconds, 3),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "total_cost_usd": total_cost_usd,
        "model_usage": model_usage,
        "duration_ms": duration_ms,
        "num_turns": num_turns,
    }


def log_fixer_run(
    *,
    repo_slug: str,
    issue_number: int,
    pr_number: int | None,
    attempt: int,
    outcome: Literal["fixed", "incomplete", "exception"],
    total_findings: int,
    addressed_count: int,
    unaddressed_count: int,
    unaddressed_by_reason: dict[str, int],
    disagreed_count: int,
    confidence: Literal["high", "medium", "low"],
    duration_seconds: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    total_cost_usd: float | None = None,
    model_usage: dict[str, Any] | None = None,
    duration_ms: int = 0,
    num_turns: int = 0,
    stats_root: Path | None = None,
) -> Path:
    """Append one JSONL line to the Fixer stats file for ``repo_slug``.

    Args:
        repo_slug: ``"owner/repo"`` — used to derive the per-repo
            stats subdirectory.
        issue_number: Originating issue # (label-triggered the Fixer).
        pr_number: Spec PR # the Fixer edited. ``None`` on
            ``"exception"`` runs that crashed before the spec PR
            could be resolved.
        attempt: 1-based fix-attempt counter (matches the
            ``foreman:fix-attempt-N`` label set on entry).
        outcome: ``"fixed"`` or ``"incomplete"`` per
            :class:`~foreman.schemas.fixer.FixerOutput.outcome`, or
            ``"exception"`` (foreman#239 / unified across all four roles
            in PR #255) when ``run_fixer`` itself raised an exception
            before producing a FixerOutput. ``"incomplete"`` is the
            Fixer's SELF-REPORTED outcome (LLM said it didn't finish);
            ``"exception"`` is a different shape — an uncaught
            exception in the role runner. Keeping them distinct lets
            cost-rollup queries answer "how many Fixer runs crashed vs
            how many the LLM gave up on?".
        total_findings: Sum of addressed + unaddressed; matches the
            Reviewer's finding count.
        addressed_count: ``len(addressed_findings)``.
        unaddressed_count: ``len(unaddressed_findings)``.
        unaddressed_by_reason: Histogram of unaddressed reasons
            (``{"needs_info": N, ...}``). Reasons not present are
            omitted; downstream tooling should treat missing keys as 0.
        disagreed_count: Count of unaddressed findings with reason
            ``needed_remediation_wrong``. Tracked separately because
            disagreements are a sharper signal than other skip reasons.
        confidence: Fixer's self-rated confidence in the run.
        duration_seconds: Wall-clock time the Fixer run took, end to
            end (orchestrator handle the timing — the LLM doesn't
            know how to time itself).
        stats_root: Override for the stats root directory; defaults to
            ``$FOREMAN_STATS_ROOT`` or ``~/.foreman/stats``. Test-only
            knob.

    Returns:
        The absolute path of the JSONL file the line was appended to.
        Useful for the caller to log / surface where the line landed.
    """
    root = stats_root if stats_root is not None else _default_stats_root()
    repo_dir = root / _repo_slug_to_dirname(repo_slug)
    repo_dir.mkdir(parents=True, exist_ok=True)
    stats_file = repo_dir / "fixer.jsonl"

    envelope = _envelope_dict(
        role="fixer",
        issue_number=issue_number,
        pr_number=pr_number,
        outcome=outcome,
        duration_seconds=duration_seconds,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        total_cost_usd=total_cost_usd,
        model_usage=model_usage,
        duration_ms=duration_ms,
        num_turns=num_turns,
    )
    role_specific = {
        "attempt": attempt,
        "total_findings": total_findings,
        "addressed_count": addressed_count,
        "unaddressed_count": unaddressed_count,
        "unaddressed_by_reason": dict(unaddressed_by_reason),
        "disagreed_count": disagreed_count,
        "confidence": confidence,
    }
    line = {**envelope, **role_specific}
    with stats_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    return stats_file


def log_worker_run(
    *,
    repo_slug: str,
    issue_number: int,
    pr_number: int | None,
    attempt: int,
    outcome: Literal["implemented", "incomplete", "spec_invalid", "exception"],
    total_sub_requests: int,
    implemented_count: int,
    skipped_count: int,
    skipped_by_reason: dict[str, int],
    did_check_pass: bool,
    confidence: Literal["high", "medium", "low"],
    duration_seconds: float,
    baseline_failures_count: int,
    new_failures_count: int,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    total_cost_usd: float | None = None,
    model_usage: dict[str, Any] | None = None,
    duration_ms: int = 0,
    num_turns: int = 0,
    stats_root: Path | None = None,
) -> Path:
    """Append one JSONL line to the Worker stats file for ``repo_slug``.

    Args:
        repo_slug: ``"owner/repo"`` — used to derive the per-repo
            stats subdirectory.
        issue_number: Originating issue # (label-triggered the Worker).
        pr_number: Impl PR number if opened (None for incomplete /
            spec_invalid runs that don't open one).
        attempt: 1-based impl-attempt counter (matches the
            ``foreman:impl-attempt-N`` label set on entry).
        outcome: One of ``implemented`` / ``incomplete`` /
            ``spec_invalid`` per :class:`~foreman.schemas.worker.WorkerOutput.outcome`,
            OR ``exception`` (foreman#238 / unified across all four
            roles in PR #255) when an uncaught exception escaped
            ``run_worker``'s body wrap. ``exception`` is distinct from
            ``incomplete`` (the Worker's self-reported "I couldn't
            finish") and ``spec_invalid`` (the Worker's self-reported
            "the spec is wrong") — both of those are LLM outputs.
            ``exception`` means the run never produced a structured
            output at all.
            The non-failed outcomes are the FINAL value after
            orchestrator-side override: if the Worker claimed
            ``implemented`` but the orchestrator's post-Worker
            verification found new failures, log ``incomplete`` here
            (not the Worker's original claim).
        total_sub_requests: ``len(implemented_sub_requests) +
            len(skipped_sub_requests)``. Matches the spec doc's
            Sub-requests count when no silent drops occurred.
        implemented_count: ``len(implemented_sub_requests)``.
        skipped_count: ``len(skipped_sub_requests)``.
        skipped_by_reason: Histogram of skip reasons
            (``{"needs_info": N, "out_of_scope": M, ...}``). Missing
            keys downstream should be read as 0.
        did_check_pass: Orchestrator-verified ground truth. NOT the
            Worker's self-report — the orchestrator runs ``check_command``
            after the Worker returns and uses that as the audit-log
            field. The Worker's own claim lives in-memory and is not
            persisted here (only the truth is).
        confidence: Worker's self-rated confidence.
        duration_seconds: Wall-clock time the Worker run took, end to
            end (orchestrator times it — the LLM doesn't know how to
            time itself).
        baseline_failures_count: Number of tests that already failed
            before the Worker LLM dispatched. Used to compute the
            "Worker innocent / Worker broke it" split downstream.
        new_failures_count: Number of failing tests after the Worker
            returned MINUS the baseline. Positive → Worker introduced
            regressions; zero → Worker's net effect on the suite was
            non-negative.
        stats_root: Override for the stats root directory; defaults to
            ``$FOREMAN_STATS_ROOT`` or ``~/.foreman/stats``. Test-only
            knob.

    Returns:
        The absolute path of the JSONL file the line was appended to.
    """
    root = stats_root if stats_root is not None else _default_stats_root()
    repo_dir = root / _repo_slug_to_dirname(repo_slug)
    repo_dir.mkdir(parents=True, exist_ok=True)
    stats_file = repo_dir / "worker.jsonl"

    envelope = _envelope_dict(
        role="worker",
        issue_number=issue_number,
        pr_number=pr_number,
        outcome=outcome,
        duration_seconds=duration_seconds,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        total_cost_usd=total_cost_usd,
        model_usage=model_usage,
        duration_ms=duration_ms,
        num_turns=num_turns,
    )
    role_specific = {
        "attempt": attempt,
        "total_sub_requests": total_sub_requests,
        "implemented_count": implemented_count,
        "skipped_count": skipped_count,
        "skipped_by_reason": dict(skipped_by_reason),
        "did_check_pass": did_check_pass,
        "confidence": confidence,
        "baseline_failures_count": baseline_failures_count,
        "new_failures_count": new_failures_count,
    }
    line = {**envelope, **role_specific}
    with stats_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    return stats_file


def log_planner_run(
    *,
    repo_slug: str,
    issue_number: int,
    pr_number: int | None,
    outcome: Literal["spec_written", "exception"],
    duration_seconds: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    total_cost_usd: float | None = None,
    model_usage: dict[str, Any] | None = None,
    duration_ms: int = 0,
    num_turns: int = 0,
    stats_root: Path | None = None,
) -> Path:
    """Append one JSONL line to the Planner stats file for ``repo_slug``.

    foreman#227 added the file; foreman#233 standardized its envelope
    against the other three roles and added the ``outcome`` field
    (Planner previously had no per-row outcome; cross-role queries like
    "how many runs failed today" couldn't be written).

    Args:
        repo_slug: ``"owner/repo"`` — used to derive the per-repo
            stats subdirectory.
        issue_number: Originating issue # (the Planner runs on issues).
        pr_number: Spec PR # opened by the Planner (None on failed runs
            that returned before opening a PR).
        outcome: ``"spec_written"`` when the run produced a spec PR;
            ``"exception"`` when the role runner caught an uncaught
            exception before producing structured output (foreman#239
            unified across all four roles in PR #255). Required
            kwarg — foreman#233 deliberately does NOT default this, so
            callers can't silently regress by omitting it.
        duration_seconds: Wall-clock time the Planner run took.
        input_tokens: Provider-reported input token count.
        output_tokens: Provider-reported output token count.
        cache_creation_input_tokens: Provider-reported prompt-cache
            creation tokens (foreman#244, billed at 25% of input rate).
        cache_read_input_tokens: Provider-reported prompt-cache read
            tokens (foreman#244, billed at 10% of input rate).
        total_cost_usd: Provider-computed cost for the call.
        model_usage: Per-model breakdown when the provider supplies one.
        duration_ms: Provider-reported wall-clock duration.
        num_turns: Number of agent-loop turns the provider executed.
        stats_root: Override for the stats root directory.

    Returns:
        The absolute path of the JSONL file the line was appended to.
    """
    root = stats_root if stats_root is not None else _default_stats_root()
    repo_dir = root / _repo_slug_to_dirname(repo_slug)
    repo_dir.mkdir(parents=True, exist_ok=True)
    stats_file = repo_dir / "planner.jsonl"

    envelope = _envelope_dict(
        role="planner",
        issue_number=issue_number,
        pr_number=pr_number,
        outcome=outcome,
        duration_seconds=duration_seconds,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        total_cost_usd=total_cost_usd,
        model_usage=model_usage,
        duration_ms=duration_ms,
        num_turns=num_turns,
    )
    # Planner has no role-specific fields yet; rich per-role telemetry
    # (spec_doc_length, considered_alternatives_count, ...) is a
    # deliberately-deferred follow-up.
    line = envelope
    with stats_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    return stats_file


def log_reviewer_run(
    *,
    repo_slug: str,
    issue_number: int,
    pr_number: int | None,
    target: Literal["spec_pr", "impl_pr"],
    outcome: Literal["clean", "needs_fix", "exception"],
    duration_seconds: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    total_cost_usd: float | None = None,
    model_usage: dict[str, Any] | None = None,
    duration_ms: int = 0,
    num_turns: int = 0,
    stats_root: Path | None = None,
) -> Path:
    """Append one JSONL line to the Reviewer stats file for ``repo_slug``.

    foreman#227 added the file; foreman#233 standardized its envelope
    against the other three roles. ``target`` (``spec_pr`` vs
    ``impl_pr``, per foreman#78/#79 target-aware routing) lands after
    the CommonEnvelope prefix as a Reviewer-specific field.

    Args:
        repo_slug: ``"owner/repo"`` — used to derive the per-repo
            stats subdirectory.
        issue_number: Originating issue # (the source of the PR).
        pr_number: PR # the Reviewer reviewed. ``None`` is permitted
            for failed-run rows where the exception fired before the
            Reviewer resolved the PR number (defensive — today the PR
            number is resolved from the URL before any failure-prone
            step, but the type allows the partial-state pattern that
            foreman#235 / PR #236 established for the Planner).
        target: ``"spec_pr"`` or ``"impl_pr"`` per foreman#78/#79
            target-aware routing.
        outcome: ``"clean"`` / ``"needs_fix"`` per
            :class:`~foreman.schemas.reviewer.ReviewerOutput.outcome`
            on success, or ``"exception"`` when ``run_reviewer``
            caught an uncaught exception mid-run (foreman#237 / unified
            across all four roles in PR #255). The exception path
            ensures failed Reviewer runs no longer silently vanish
            from ``reviewer.jsonl``.
        duration_seconds: Wall-clock time the Reviewer run took.
        input_tokens: Provider-reported input token count.
        output_tokens: Provider-reported output token count.
        cache_creation_input_tokens: Provider-reported prompt-cache
            creation tokens (foreman#244, billed at 25% of input rate).
        cache_read_input_tokens: Provider-reported prompt-cache read
            tokens (foreman#244, billed at 10% of input rate).
        total_cost_usd: Provider-computed cost for the call.
        model_usage: Per-model breakdown when the provider supplies one.
        duration_ms: Provider-reported wall-clock duration.
        num_turns: Number of agent-loop turns the provider executed.
        stats_root: Override for the stats root directory.

    Returns:
        The absolute path of the JSONL file the line was appended to.
    """
    root = stats_root if stats_root is not None else _default_stats_root()
    repo_dir = root / _repo_slug_to_dirname(repo_slug)
    repo_dir.mkdir(parents=True, exist_ok=True)
    stats_file = repo_dir / "reviewer.jsonl"

    envelope = _envelope_dict(
        role="reviewer",
        issue_number=issue_number,
        pr_number=pr_number,
        outcome=outcome,
        duration_seconds=duration_seconds,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        total_cost_usd=total_cost_usd,
        model_usage=model_usage,
        duration_ms=duration_ms,
        num_turns=num_turns,
    )
    role_specific = {
        "target": target,
    }
    line = {**envelope, **role_specific}
    with stats_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    return stats_file


__all__ = [
    "CommonEnvelope",
    "log_fixer_run",
    "log_planner_run",
    "log_reviewer_run",
    "log_worker_run",
    "_DISAGREEMENT_REASON",
]
