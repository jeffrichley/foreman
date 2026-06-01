"""Append-only JSONL lifecycle stats — proto for foreman#11.

Each role run produces one JSON line under
``~/.foreman/stats/<owner>__<repo>/<role>.jsonl``. Lines are append-only,
schema-stable, and one-per-run so downstream tooling can sum / group /
chart without re-parsing in-flight state.

This is a scoped proto: only Fixer logs today. Worker / Planner /
Reviewer will get their own ``log_<role>_run`` functions as those slices
land, all sharing this module's path discipline so an ``ls`` of
``~/.foreman/stats/`` mirrors the pipeline.

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
from typing import Literal

# Reasons that contribute to ``disagreed_count`` in fixer stats — kept
# in sync with the ``UnaddressedReason`` literal in
# :mod:`foreman.schemas.fixer`.
_DISAGREEMENT_REASON = "needed_remediation_wrong"


def _default_stats_root() -> Path:
    """Return ``~/.foreman/stats`` (env-overridable for tests via
    ``FOREMAN_STATS_ROOT``)."""
    override = os.environ.get("FOREMAN_STATS_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".foreman" / "stats"


def _repo_slug_to_dirname(repo_slug: str) -> str:
    """Convert ``owner/repo`` → ``owner__repo`` for filesystem use."""
    return repo_slug.replace("/", "__")


def log_fixer_run(
    *,
    repo_slug: str,
    issue_number: int,
    pr_number: int,
    attempt: int,
    outcome: Literal["fixed", "incomplete"],
    total_findings: int,
    addressed_count: int,
    unaddressed_count: int,
    unaddressed_by_reason: dict[str, int],
    disagreed_count: int,
    confidence: Literal["high", "medium", "low"],
    duration_seconds: float,
    stats_root: Path | None = None,
) -> Path:
    """Append one JSONL line to the Fixer stats file for ``repo_slug``.

    Args:
        repo_slug: ``"owner/repo"`` — used to derive the per-repo
            stats subdirectory.
        issue_number: Originating issue # (label-triggered the Fixer).
        pr_number: Spec PR # the Fixer edited.
        attempt: 1-based fix-attempt counter (matches the
            ``foreman:fix-attempt-N`` label set on entry).
        outcome: ``"fixed"`` or ``"incomplete"`` per
            :class:`~foreman.schemas.fixer.FixerOutput.outcome`.
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

    line = {
        "timestamp": datetime.now(UTC).isoformat(),
        "issue_number": issue_number,
        "pr_number": pr_number,
        "attempt": attempt,
        "outcome": outcome,
        "total_findings": total_findings,
        "addressed_count": addressed_count,
        "unaddressed_count": unaddressed_count,
        "unaddressed_by_reason": dict(unaddressed_by_reason),
        "disagreed_count": disagreed_count,
        "confidence": confidence,
        "duration_seconds": round(duration_seconds, 3),
    }
    with stats_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    return stats_file


__all__ = ["log_fixer_run", "_DISAGREEMENT_REASON"]
