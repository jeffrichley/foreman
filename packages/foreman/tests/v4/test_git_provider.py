"""RequiredCheckState signal — enum roundtrip through FakeGitProvider (foreman#317)."""

from __future__ import annotations

from foreman.v4.git_provider import FakeGitProvider, RequiredCheckState


def test_fake_required_check_state_roundtrips():
    p = FakeGitProvider()
    p.seed_check_state("proj", 7, RequiredCheckState.FAILED)
    assert p.required_check_state(project="proj", pr_number=7) == RequiredCheckState.FAILED


def test_fake_required_check_state_defaults_pending_when_unseeded():
    # Mirror reality: a PR whose checks haven't registered yet reads PENDING
    # (C-CI guarantees CI exists), never a silent PASSED.
    p = FakeGitProvider()
    assert p.required_check_state(project="proj", pr_number=9) == RequiredCheckState.PENDING
