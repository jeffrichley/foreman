"""Pin the foreman.labels Label StrEnum design.

D1 (architecture stability plan 2026-06-11): every member carries its
classification at the point of definition; the derived frozensets match
the membership; adding a label without classification is a TypeError at
module load. These tests pin the contract — if any of them fails on a
future refactor, that refactor is removing the structural guarantee
that foreman#194's original orphan was supposed to provide.
"""

from __future__ import annotations

from foreman.labels import (
    BLOCKING_LABELS,
    COUNTER_LABELS,
    IN_FLIGHT_LABELS,
    QUEUE_LABELS,
    TERMINAL_LABELS,
    Label,
    LabelClass,
)


def test_every_label_has_a_classification() -> None:
    """Forgetting classification is structurally impossible — Python's
    enum machinery raises TypeError at module load when a member is
    declared without the classification tuple. This test pins that the
    discipline is in place, so a future refactor that 'simplifies' the
    enum loses the structural guarantee loudly."""
    for member in Label:
        assert isinstance(member.classification, LabelClass), (
            f"{member.name} missing classification"
        )


def test_derived_frozensets_match_enum_membership() -> None:
    """Every label in the catalog appears in exactly ONE derived
    frozenset (the one matching its classification). No label is in
    two sets; no label is in zero sets."""
    all_labels = {m.value for m in Label}
    sets = [
        QUEUE_LABELS,
        IN_FLIGHT_LABELS,
        BLOCKING_LABELS,
        COUNTER_LABELS,
        TERMINAL_LABELS,
    ]
    union = set().union(*sets)
    assert union == all_labels, (
        f"Labels in enum but not in any frozenset: {all_labels - union}; "
        f"in frozenset but not enum: {union - all_labels}"
    )
    # Pairwise disjoint.
    for i, a in enumerate(sets):
        for b in sets[i + 1 :]:
            assert a.isdisjoint(b), f"Label appears in two buckets: {a & b}"


def test_blocking_labels_matches_known_blocking_states() -> None:
    """foreman#194 regression guard. ``_BLOCKING_LABELS`` was a
    hand-maintained tuple in ``rules.py`` pre-resurrection; if someone
    added a new BLOCKING label without updating the tuple, the
    rate-limiter silently treated the new state as non-blocking. With
    the StrEnum, ``BLOCKING_LABELS`` is derived from enum classification
    — so the only way to add a new blocking label without updating this
    set is to declare it with a non-BLOCKING classification, which the
    code review would catch."""
    assert BLOCKING_LABELS == frozenset(
        {
            "foreman:hold",
            "foreman:needs-help",
            "foreman:spec-fix",
            "foreman:impl-fix",
        }
    )


def test_queue_labels_matches_known_queue_states() -> None:
    """Queue labels drive ``next_action`` dispatch. The four states
    here are the labels a human (or the reconciler) adds to request a
    role run on a ticket."""
    assert QUEUE_LABELS == frozenset(
        {
            "foreman:plan",
            "foreman:plan-approved",
            "foreman:impl-review",
            "foreman:impl-approved",
        }
    )


def test_in_flight_labels_matches_known_in_flight_states() -> None:
    """In-flight labels mark a role as currently running on a ticket.
    The reconciler observes these to enforce one-role-at-a-time."""
    assert IN_FLIGHT_LABELS == frozenset(
        {
            "foreman:planning",
            "foreman:merging-plan",
            "foreman:merging-impl",
        }
    )


def test_counter_labels_matches_six_attempt_markers() -> None:
    """The 6 attempt-counter labels stamped by Worker (impl-attempt-N)
    and Fixer (fix-attempt-N) on each cycle. Currently three of each;
    if the max-attempts constant ever changes, this test must update."""
    assert COUNTER_LABELS == frozenset(
        {
            "foreman:impl-attempt-1",
            "foreman:impl-attempt-2",
            "foreman:impl-attempt-3",
            "foreman:fix-attempt-1",
            "foreman:fix-attempt-2",
            "foreman:fix-attempt-3",
        }
    )


def test_terminal_labels_matches_known_terminal_states() -> None:
    """``foreman:done`` clears the ticket cleanly; ``foreman:failed``
    marks abandoned after exhausting retries. No other label is
    terminal — every other state has a transition out."""
    assert TERMINAL_LABELS == frozenset(
        {
            "foreman:done",
            "foreman:failed",
        }
    )


def test_label_strenum_equality_with_string() -> None:
    """StrEnum invariant: members compare equal to their string values
    so PyGithub label add/remove calls receive the value verbatim and
    f-string interpolation produces the bare string. This is what lets
    the migration replace literal strings with enum members without
    breaking equality checks against ticket label sets."""
    assert Label.PLAN == "foreman:plan"
    assert "foreman:plan" == Label.PLAN
    assert f"{Label.NEEDS_HELP}" == "foreman:needs-help"


def test_catalog_has_exactly_nineteen_labels() -> None:
    """The 19-label catalog is the architecture-stability-plan baseline.
    If this count changes, the plan doc's classification table needs to
    be updated to match."""
    assert len(list(Label)) == 19
