"""Tests for the lifecycle-stats JSONL writer (proto for foreman#11)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foreman.stats import _repo_slug_to_dirname, log_fixer_run


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
    """
    log_fixer_run(stats_root=tmp_path, **_base_kwargs())  # type: ignore[arg-type]
    out_path = tmp_path / "jeffrichley__voice" / "fixer.jsonl"
    payload = json.loads(out_path.read_text(encoding="utf-8").strip())
    expected_keys = {
        "timestamp",
        "issue_number",
        "pr_number",
        "attempt",
        "outcome",
        "total_findings",
        "addressed_count",
        "unaddressed_count",
        "unaddressed_by_reason",
        "disagreed_count",
        "confidence",
        "duration_seconds",
        # foreman#227: provider-reported usage + cost.
        "input_tokens",
        "output_tokens",
        "total_cost_usd",
        "model_usage",
        "duration_ms",
        "num_turns",
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
