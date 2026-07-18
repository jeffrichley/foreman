"""state_label() — the single kebab-case label-name helper.

Pins the shared transform both ``foreman.init`` and
``foreman.v4.observers.label_observability`` delegate to. Before this
module existed the two call sites disagreed for multi-word states
(kebab-case vs. naive lowercase); these tests exercise every
multi-word state name in the registry so a future regression on
either side is caught here first.
"""

from __future__ import annotations

import pytest

from foreman.v4.state_labels import state_label
from foreman.v4.states.registry import STATE_REGISTRY

#: Every registry state paired with its expected label. The expected
#: values are hardcoded (not re-derived from ``state_label``) so a
#: regression in the transform is caught here rather than masked by a
#: tautology. ``test_cases_cover_every_registry_state`` guards that this
#: list stays complete as new states are added.
_LABEL_CASES: list[tuple[str, str]] = [
    ("Queued", "foreman:state-queued"),
    ("Planning", "foreman:state-planning"),
    ("SpecReview", "foreman:state-spec-review"),
    ("SpecFix", "foreman:state-spec-fix"),
    ("SpecMerging", "foreman:state-spec-merging"),
    ("Implementing", "foreman:state-implementing"),
    ("ImplReview", "foreman:state-impl-review"),
    ("ImplFix", "foreman:state-impl-fix"),
    ("ImplApproved", "foreman:state-impl-approved"),
    ("Merging", "foreman:state-merging"),
    ("MergeQueued", "foreman:state-merge-queued"),
    ("Done", "foreman:state-done"),
    ("Failed", "foreman:state-failed"),
    ("NeedsHelp", "foreman:state-needs-help"),
]


@pytest.mark.parametrize(("state_name", "expected"), _LABEL_CASES)
def test_state_label_kebab_cases_every_registry_state(state_name: str, expected: str) -> None:
    assert state_label(state_name) == expected


def test_cases_cover_every_registry_state() -> None:
    """A new STATE_REGISTRY entry must add a pinned label case above.

    This is the anti-drift guard: adding a state without pinning its
    expected label here fails immediately, so the kebab transform can
    never silently go unverified for a new (possibly multi-word) state.
    """
    assert {name for name, _ in _LABEL_CASES} == set(STATE_REGISTRY)


def test_state_label_single_word_state_has_no_dash() -> None:
    """A single-word state must not grow a spurious leading dash."""
    assert state_label("Queued") == "foreman:state-queued"
