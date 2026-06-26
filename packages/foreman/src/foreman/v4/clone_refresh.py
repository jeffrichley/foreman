"""Per-poll project clone refresh (issue #407).

The v4 Poller is the only producer of WorkItems, but it does NOT refresh
the local project clones between polls. v3 shipped a per-poll
``origin/<default-branch>`` auto-fetch (foreman#291 — the ``OnPollFetch``
reconciler) so a role dispatched between worktree creations always saw a
fresh base ref; that reconciler was not ported in the #333 v4 cutover, so
the local ``refs/remotes/origin/<default>`` ref went stale between polls.
Consequence: a role could be dispatched against an out-of-date base until
the next worktree creation re-fetched.

This module re-introduces the refresh as a daemon-loop component: it is
injected into the :class:`~foreman.v4.daemon.Daemon`, ``tick()``-ed once
per ``tick_once()`` call, and reuses the surviving
:func:`foreman.worktree.fetch_origin_default_branch` helper rather than
re-implementing fetch logic.

Two resilience properties matter:

* **Throttled.** ``tick_seconds`` defaults to 30s; fetching every clone on
  every tick would hammer ``git fetch`` (and the remote) far more often
  than the base ref actually changes. The refresher tracks a per-project
  last-success timestamp and skips a project until
  ``clone_refresh_seconds`` (default 300s = 5 min) have elapsed.
* **Per-project isolation.** One project's fetch failure (network blip,
  auth, a deleted clone dir) must not abort the others or crash the poll
  loop. Each project is fetched in its own ``try``/``except``; failures are
  structured-logged and the throttle clock is NOT advanced (so the failed
  project is retried on the next tick rather than parked for the full
  interval).
"""
from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from pathlib import Path

from foreman.worktree import fetch_origin_default_branch

# Routes through the ``foreman.v4`` JSON-lines handler wired by
# ``configure_logging`` (see ``foreman.v4.logging_config``).
_log = logging.getLogger(__name__)

# Type of the fetch callable the refresher invokes per project. Matches
# the signature of ``foreman.worktree.fetch_origin_default_branch`` so the
# real helper is the production default and a fake can be injected in tests.
FetchFn = Callable[..., None]


class _DisabledCloneRefresher:
    """No-op refresher sentinel for configs with no projects to refresh.

    Same ``tick()`` shape as the real :class:`CloneRefresher` so the
    daemon's call site stays unconditional (``self._clone_refresher.tick()``).
    Stateless, so one instance can serve as the default kwarg on
    ``Daemon.__init__`` and as the construction-time default in tests.
    """

    def tick(self) -> None:
        return None


class CloneRefresher:
    """Interval-throttled per-poll refresh of each project's clone.

    Runs inside the daemon's ``tick_once()`` loop. On each ``tick()`` it
    walks every registered project and, for any whose last successful
    refresh was longer than ``interval_seconds`` ago (or never), calls
    ``fetch`` against that project's clone path. The default ``fetch`` is
    :func:`foreman.worktree.fetch_origin_default_branch`; tests inject a
    fake.
    """

    def __init__(
        self,
        *,
        clone_paths: dict[str, Path],
        interval_seconds: float,
        clock: Callable[[], dt.datetime],
        fetch: FetchFn = fetch_origin_default_branch,
    ) -> None:
        self._clone_paths = clone_paths
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._fetch = fetch
        # Per-project last-successful-refresh wall-clock time. A project
        # missing from the map has never been (successfully) refreshed, so
        # its first eligible tick always fetches.
        self._last_refresh_at: dict[str, dt.datetime] = {}

    @classmethod
    def from_projects(
        cls,
        projects: dict[str, Path],
        *,
        interval_seconds: float,
        clock: Callable[[], dt.datetime],
        fetch: FetchFn = fetch_origin_default_branch,
    ) -> CloneRefresher | _DisabledCloneRefresher:
        """Build a refresher from a ``project name -> clone path`` map.

        Returns the no-op :class:`_DisabledCloneRefresher` sentinel when
        there are no projects (nothing to refresh), else a live
        :class:`CloneRefresher`. This factory is the bootstrap entry point;
        tests that want a hand-tuned interval / clock / fetch construct
        :class:`CloneRefresher` directly.
        """
        if not projects:
            return _DisabledCloneRefresher()
        return cls(
            clone_paths=projects,
            interval_seconds=interval_seconds,
            clock=clock,
            fetch=fetch,
        )

    def tick(self) -> None:
        """Refresh every project whose throttle interval has elapsed.

        Per-project ``try``/``except``: one project's fetch failure is
        structured-logged and swallowed so it cannot abort the others or
        crash the poll loop. The throttle clock advances only on success,
        so a failed fetch is retried next tick.
        """
        now = self._clock()
        for project, clone_path in self._clone_paths.items():
            last = self._last_refresh_at.get(project)
            if last is not None:
                elapsed = (now - last).total_seconds()
                if elapsed < self._interval_seconds:
                    continue
            try:
                self._fetch(clone_path)
            except Exception:
                # fetch_origin_default_branch is itself best-effort
                # (network failures are logged + swallowed inside), so an
                # exception escaping here is something more structural — a
                # missing clone dir, a bad path. Log per-project and keep
                # going; do NOT advance the throttle clock so the next tick
                # retries instead of parking the project for the full
                # interval.
                _log.warning(
                    "clone refresh failed for project=%s clone_path=%s; "
                    "other projects continue, will retry next tick",
                    project,
                    clone_path,
                    exc_info=True,
                )
                continue
            self._last_refresh_at[project] = now


# Type alias for "the daemon's clone-refresher attribute" — either a real
# :class:`CloneRefresher` or the no-op :class:`_DisabledCloneRefresher`
# sentinel. Structural typing (not a formal Protocol); both share
# ``tick() -> None``.
CloneRefresherLike = CloneRefresher | _DisabledCloneRefresher
