"""CloneRefresher — per-poll, interval-throttled project clone refresh.

foreman#407: the v4 Poller dropped the per-poll ``origin/<default>``
auto-fetch that #291 shipped for v3 (the v3 ``OnPollFetch`` reconciler
was not ported in the #333 v4 cutover). This re-introduces it as a
daemon-loop component: injected, interval-throttled, swallows
per-project failures.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from foreman.v4.clone_refresh import CloneRefresher


class _RecordingFetch:
    """Records every ``(clone_path, role_token)`` it was called with, so
    a test can assert which projects were refreshed on which tick."""

    def __init__(self, *, fail_for: set[Path] | None = None) -> None:
        self.calls: list[Path] = []
        self._fail_for = fail_for or set()

    def __call__(self, clone_path: Path, *, role_token: str | None = None) -> None:
        self.calls.append(clone_path)
        if clone_path in self._fail_for:
            raise RuntimeError(f"boom for {clone_path}")


def _refresher(
    *,
    clone_paths: dict[str, Path],
    interval_seconds: float,
    fetch: _RecordingFetch,
    clock,
) -> CloneRefresher:
    return CloneRefresher(
        clone_paths=clone_paths,
        interval_seconds=interval_seconds,
        fetch=fetch,
        clock=clock,
    )


def test_first_tick_fetches_every_project() -> None:
    """First ever tick is never throttled — every project is refreshed."""
    fetch = _RecordingFetch()
    now = dt.datetime(2026, 6, 24, 12, 0, 0)
    refresher = _refresher(
        clone_paths={"a": Path("/clones/a"), "b": Path("/clones/b")},
        interval_seconds=300,
        fetch=fetch,
        clock=lambda: now,
    )
    refresher.tick()
    assert set(fetch.calls) == {Path("/clones/a"), Path("/clones/b")}
    assert len(fetch.calls) == 2


def test_throttle_suppresses_repeat_within_interval() -> None:
    """A second tick inside the interval must NOT re-fetch."""
    fetch = _RecordingFetch()
    times = iter(
        [
            dt.datetime(2026, 6, 24, 12, 0, 0),  # tick 1 → fetch
            dt.datetime(2026, 6, 24, 12, 2, 0),  # tick 2 (+120s < 300s) → skip
        ]
    )
    refresher = _refresher(
        clone_paths={"a": Path("/clones/a")},
        interval_seconds=300,
        fetch=fetch,
        clock=lambda: next(times),
    )
    refresher.tick()
    refresher.tick()
    assert fetch.calls == [Path("/clones/a")], "second tick within the interval must be throttled"


def test_throttle_fires_again_after_interval() -> None:
    """Once the interval elapses, the next tick refreshes again."""
    fetch = _RecordingFetch()
    times = iter(
        [
            dt.datetime(2026, 6, 24, 12, 0, 0),  # tick 1 → fetch
            dt.datetime(2026, 6, 24, 12, 2, 0),  # tick 2 (+120s) → skip
            dt.datetime(2026, 6, 24, 12, 6, 0),  # tick 3 (+360s > 300s) → fetch
        ]
    )
    refresher = _refresher(
        clone_paths={"a": Path("/clones/a")},
        interval_seconds=300,
        fetch=fetch,
        clock=lambda: next(times),
    )
    refresher.tick()
    refresher.tick()
    refresher.tick()
    assert fetch.calls == [Path("/clones/a"), Path("/clones/a")], (
        "third tick after the interval elapsed must re-fetch"
    )


def test_per_project_throttle_is_independent() -> None:
    """Throttle is tracked per-project: each project's last-fetch time is
    independent, so one project being fresh doesn't suppress another."""
    fetch = _RecordingFetch()
    times = iter(
        [
            dt.datetime(2026, 6, 24, 12, 0, 0),  # tick 1 → both a + b fetched
            dt.datetime(2026, 6, 24, 12, 2, 0),  # tick 2 → both throttled
        ]
    )
    refresher = _refresher(
        clone_paths={"a": Path("/clones/a"), "b": Path("/clones/b")},
        interval_seconds=300,
        fetch=fetch,
        clock=lambda: next(times),
    )
    refresher.tick()
    refresher.tick()
    assert fetch.calls.count(Path("/clones/a")) == 1
    assert fetch.calls.count(Path("/clones/b")) == 1


def test_one_project_failure_does_not_abort_others() -> None:
    """A fetch that raises for one project must not prevent the others
    from being refreshed, and must not propagate out of tick()."""
    fetch = _RecordingFetch(fail_for={Path("/clones/a")})
    now = dt.datetime(2026, 6, 24, 12, 0, 0)
    refresher = _refresher(
        clone_paths={"a": Path("/clones/a"), "b": Path("/clones/b")},
        interval_seconds=300,
        fetch=fetch,
        clock=lambda: now,
    )
    # Must not raise even though project "a" fetch raises.
    refresher.tick()
    assert Path("/clones/b") in fetch.calls, (
        "project b must still be refreshed after project a's fetch raised"
    )


def test_failed_fetch_does_not_mark_project_fresh() -> None:
    """A project whose fetch RAISED must be retried on the next tick (its
    throttle timestamp is only advanced on success)."""
    fetch = _RecordingFetch(fail_for={Path("/clones/a")})
    times = iter(
        [
            dt.datetime(2026, 6, 24, 12, 0, 0),  # tick 1 → fetch raises
            dt.datetime(2026, 6, 24, 12, 0, 30),  # tick 2 (+30s, well inside) → retry
        ]
    )
    refresher = _refresher(
        clone_paths={"a": Path("/clones/a")},
        interval_seconds=300,
        fetch=fetch,
        clock=lambda: next(times),
    )
    refresher.tick()
    refresher.tick()
    assert fetch.calls == [Path("/clones/a"), Path("/clones/a")], (
        "a failed fetch must not start the throttle clock; retry next tick"
    )
