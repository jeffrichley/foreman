"""Tests for the lifecycle-stats JSONL writer (proto for foreman#11)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foreman.stats import (
    _repo_slug_to_dirname,
    log_fixer_run,
    log_planner_run,
    log_reviewer_run,
    log_worker_run,
)


def _base_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "repo_slug": "jeffrichley/voice",
        "issue_number": 42,
        "pr_number": 99,
        "attempt": 1,
        "outcome": "fixed",
        "total_findings": 5,
        "addressed_count": 4,
        "unaddressed_count": 1,
        "unaddressed_by_reason": {"needs_info": 1},
        "disagreed_count": 0,
        "confidence": "high",
        "duration_seconds": 187.345,
    }
    base.update(overrides)
    return base


# ----------------------------------------------------------------------
# _repo_slug_to_dirname
# ----------------------------------------------------------------------


def test_repo_slug_to_dirname_swaps_slash_for_double_underscore() -> None:
    assert _repo_slug_to_dirname("jeffrichley/voice") == "jeffrichley__voice"


def test_repo_slug_to_dirname_preserves_unrelated_chars() -> None:
    assert _repo_slug_to_dirname("anthropics/claude-code") == "anthropics__claude-code"


# ----------------------------------------------------------------------
# log_fixer_run
# ----------------------------------------------------------------------


def test_log_fixer_run_writes_one_jsonl_line(tmp_path: Path) -> None:
    out_path = log_fixer_run(stats_root=tmp_path, **_base_kwargs())  # type: ignore[arg-type]
    assert out_path == tmp_path / "jeffrichley__voice" / "fixer.jsonl"
    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_log_fixer_run_creates_parent_dirs(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "stats"
    out_path = log_fixer_run(stats_root=nested, **_base_kwargs())  # type: ignore[arg-type]
    assert out_path.parent.is_dir()
    assert out_path.exists()


def test_log_fixer_run_appends_each_call(tmp_path: Path) -> None:
    log_fixer_run(stats_root=tmp_path, **_base_kwargs(attempt=1))  # type: ignore[arg-type]
    log_fixer_run(stats_root=tmp_path, **_base_kwargs(attempt=2))  # type: ignore[arg-type]
    log_fixer_run(stats_root=tmp_path, **_base_kwargs(attempt=3))  # type: ignore[arg-type]
    out_path = tmp_path / "jeffrichley__voice" / "fixer.jsonl"
    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    payloads = [json.loads(line) for line in lines]
    assert [p["attempt"] for p in payloads] == [1, 2, 3]


def test_log_fixer_run_schema_stable_fields(tmp_path: Path) -> None:
    """Pin the field set so downstream tooling can rely on the schema.

    foreman#227: the schema is additive — the per-call usage / cost
    fields land alongside the existing audit dimensions.
    foreman#233: the row now starts with the CommonEnvelope prefix
    (adds ``role``); cross-role order is asserted separately in
    ``test_common_envelope_uniform_across_roles``.
    """
    log_fixer_run(stats_root=tmp_path, **_base_kwargs())  # type: ignore[arg-type]
    out_path = tmp_path / "jeffrichley__voice" / "fixer.jsonl"
    payload = json.loads(out_path.read_text(encoding="utf-8").strip())
    expected_keys = {
        # foreman#233: CommonEnvelope prefix.
        "timestamp",
        "role",
        "issue_number",
        "pr_number",
        "outcome",
        "duration_seconds",
        # foreman#227: provider-reported usage + cost.
        "input_tokens",
        "output_tokens",
        # foreman#244: prompt-cache token counters.
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "total_cost_usd",
        "model_usage",
        "duration_ms",
        "num_turns",
        # Fixer-specific role fields.
        "attempt",
        "total_findings",
        "addressed_count",
        "unaddressed_count",
        "unaddressed_by_reason",
        "disagreed_count",
        "confidence",
    }
    assert set(payload.keys()) == expected_keys


def test_log_fixer_run_timestamp_is_iso_utc(tmp_path: Path) -> None:
    log_fixer_run(stats_root=tmp_path, **_base_kwargs())  # type: ignore[arg-type]
    out_path = tmp_path / "jeffrichley__voice" / "fixer.jsonl"
    payload = json.loads(out_path.read_text(encoding="utf-8").strip())
    ts = payload["timestamp"]
    # ISO-8601 with timezone — should parse cleanly and end in +00:00
    # (or contain it; Python's datetime.isoformat emits +00:00 for tz-aware UTC)
    assert "+00:00" in ts or ts.endswith("Z"), ts


def test_log_fixer_run_payload_carries_passed_values(tmp_path: Path) -> None:
    log_fixer_run(
        stats_root=tmp_path,
        **_base_kwargs(  # type: ignore[arg-type]
            attempt=2,
            outcome="incomplete",
            addressed_count=2,
            unaddressed_count=3,
            unaddressed_by_reason={"needs_info": 1, "needed_remediation_wrong": 2},
            disagreed_count=2,
            confidence="low",
        ),
    )
    out_path = tmp_path / "jeffrichley__voice" / "fixer.jsonl"
    payload = json.loads(out_path.read_text(encoding="utf-8").strip())
    assert payload["attempt"] == 2
    assert payload["outcome"] == "incomplete"
    assert payload["unaddressed_by_reason"] == {
        "needs_info": 1,
        "needed_remediation_wrong": 2,
    }
    assert payload["disagreed_count"] == 2
    assert payload["confidence"] == "low"


def test_log_fixer_run_env_var_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``FOREMAN_STATS_ROOT`` lets tests / ops point the stats elsewhere."""
    monkeypatch.setenv("FOREMAN_STATS_ROOT", str(tmp_path / "env-root"))
    # Pass stats_root=None to exercise the env-var path.
    out_path = log_fixer_run(**_base_kwargs())  # type: ignore[arg-type]
    assert tmp_path / "env-root" in out_path.parents


def test_log_fixer_run_separate_repos_separate_files(tmp_path: Path) -> None:
    log_fixer_run(stats_root=tmp_path, **_base_kwargs(repo_slug="jeffrichley/voice"))  # type: ignore[arg-type]
    log_fixer_run(stats_root=tmp_path, **_base_kwargs(repo_slug="jeffrichley/foreman"))  # type: ignore[arg-type]
    voice = tmp_path / "jeffrichley__voice" / "fixer.jsonl"
    foreman = tmp_path / "jeffrichley__foreman" / "fixer.jsonl"
    assert voice.exists()
    assert foreman.exists()


# ----------------------------------------------------------------------
# Cross-role CommonEnvelope (foreman#233)
# ----------------------------------------------------------------------

# Authoritative order — must match foreman.stats.CommonEnvelope field order.
# Downstream `head`-style queries rely on this ordering being identical across
# every role's JSONL file.
_COMMON_ENVELOPE_KEYS: tuple[str, ...] = (
    "timestamp",
    "role",
    "issue_number",
    "pr_number",
    "outcome",
    "duration_seconds",
    "input_tokens",
    "output_tokens",
    # foreman#244: prompt-cache token counters sit between
    # ``output_tokens`` and ``total_cost_usd`` so the four token
    # counters cluster together in the on-disk row.
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "total_cost_usd",
    "model_usage",
    "duration_ms",
    "num_turns",
)


def test_common_envelope_uniform_across_roles(tmp_path: Path) -> None:
    """Every role's JSONL row starts with the same 12 envelope keys in the
    same order, then role-specific fields after.

    foreman#233: locks the CommonEnvelope contract. Cross-role queries
    (``cat *.jsonl | jq ...``) and ``head -c`` peeks depend on the prefix
    being byte-identical in shape across planner / reviewer / worker /
    fixer files.
    """
    # Planner — minimal envelope, no role-specific fields.
    log_planner_run(
        stats_root=tmp_path,
        repo_slug="acme/widgets",
        issue_number=1,
        pr_number=10,
        outcome="spec_written",
        duration_seconds=1.5,
    )
    # Reviewer — adds ``target`` after the envelope.
    log_reviewer_run(
        stats_root=tmp_path,
        repo_slug="acme/widgets",
        issue_number=2,
        pr_number=20,
        target="spec_pr",
        outcome="clean",
        duration_seconds=2.5,
    )
    # Worker — adds attempt + sub-request histogram + check-pass + baselines.
    log_worker_run(
        stats_root=tmp_path,
        repo_slug="acme/widgets",
        issue_number=3,
        pr_number=30,
        attempt=1,
        outcome="implemented",
        total_sub_requests=2,
        implemented_count=2,
        skipped_count=0,
        skipped_by_reason={},
        did_check_pass=True,
        confidence="high",
        duration_seconds=3.5,
        baseline_failures_count=0,
        new_failures_count=0,
    )
    # Fixer — adds finding histogram + disagreement count.
    log_fixer_run(
        stats_root=tmp_path,
        repo_slug="acme/widgets",
        issue_number=4,
        pr_number=40,
        attempt=1,
        outcome="fixed",
        total_findings=1,
        addressed_count=1,
        unaddressed_count=0,
        unaddressed_by_reason={},
        disagreed_count=0,
        confidence="high",
        duration_seconds=4.5,
    )

    repo_dir = tmp_path / "acme__widgets"
    files = {
        "planner": repo_dir / "planner.jsonl",
        "reviewer": repo_dir / "reviewer.jsonl",
        "worker": repo_dir / "worker.jsonl",
        "fixer": repo_dir / "fixer.jsonl",
    }
    for role, path in files.items():
        assert path.exists(), f"{role} jsonl was not written"
        payload = json.loads(path.read_text(encoding="utf-8").strip())
        actual_keys = tuple(payload.keys())
        # The first 12 keys MUST be the CommonEnvelope keys, in CommonEnvelope
        # order. Anything after is role-specific and is not constrained here.
        assert actual_keys[: len(_COMMON_ENVELOPE_KEYS)] == _COMMON_ENVELOPE_KEYS, (
            f"{role} envelope prefix drifted: {actual_keys[: len(_COMMON_ENVELOPE_KEYS)]!r} "
            f"vs expected {_COMMON_ENVELOPE_KEYS!r}"
        )
        # And every row self-identifies its role via the literal hard-coded
        # in the writer — callers can't get it wrong.
        assert payload["role"] == role, (
            f"{role} jsonl row carried role={payload['role']!r}; the writer "
            f"must hard-code its own role literal"
        )


def test_log_planner_run_requires_outcome(tmp_path: Path) -> None:
    """foreman#233: ``outcome`` is a required kwarg on log_planner_run."""
    with pytest.raises(TypeError):
        log_planner_run(  # type: ignore[call-arg]
            stats_root=tmp_path,
            repo_slug="acme/widgets",
            issue_number=1,
            pr_number=10,
            duration_seconds=1.0,
        )


def test_log_planner_run_emits_role_planner(tmp_path: Path) -> None:
    log_planner_run(
        stats_root=tmp_path,
        repo_slug="acme/widgets",
        issue_number=1,
        pr_number=10,
        outcome="spec_written",
        duration_seconds=1.0,
    )
    payload = json.loads(
        (tmp_path / "acme__widgets" / "planner.jsonl").read_text(encoding="utf-8").strip()
    )
    assert payload["role"] == "planner"
    assert payload["outcome"] == "spec_written"


def test_log_outcome_literals_exclude_dead_failure_values() -> None:
    """foreman#256: PR #255 (commit b18a683) unified all four role
    runners on ``outcome="exception"``. The legacy ``<role>_failed``
    values are dead vocabulary — accepted by the type system but
    never emitted. This test pins the cleanup so they can't be
    silently re-added."""
    from typing import get_args, get_type_hints

    cases = [
        (log_planner_run, "spec_failed"),
        (log_reviewer_run, "review_failed"),
        (log_worker_run, "worker_failed"),
        (log_fixer_run, "fixer_failed"),
    ]
    for fn, dead_value in cases:
        allowed = set(get_args(get_type_hints(fn)["outcome"]))
        assert dead_value not in allowed, (
            f"{fn.__name__}: dead Literal value {dead_value!r} re-introduced"
        )
        # And the replacement is still in the vocabulary.
        assert "exception" in allowed, (
            f"{fn.__name__}: 'exception' missing from outcome Literal"
        )
