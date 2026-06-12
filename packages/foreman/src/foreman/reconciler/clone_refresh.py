"""Refresh strategy for project clones between role dispatches.

foreman#291: the container's clone at /foreman/repos/<project> never
auto-fetches, so ``origin/<default-branch>`` goes stale between role
dispatches. :class:`OnPollFetch` (the default) refreshes it once per
poll cycle. :class:`OnDispatchFetchOnly` preserves the pre-#291
behavior for paranoid environments / tests.

Strategy GoF pattern, per ``CLAUDE.md``'s Decision-4 calibrated lens:
the refresh policy is a substitutable concern. Google's "make the
right thing easy" also applies — the default produces the safer
behavior (no silent drift) out of the box.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from foreman.worktree import fetch_origin_default_branch

if TYPE_CHECKING:
    from foreman.reconciler.daemon import ReconcilerProject

logger = logging.getLogger(__name__)


class CloneRefreshStrategy(Protocol):
    """Policy hook for refreshing a project's clone between role dispatches.

    Implementations MUST be best-effort: any exception raised inside
    ``refresh`` is swallowed by the reconciler boundary
    (:meth:`foreman.reconciler.daemon.Reconciler.tick`) so a misbehaving
    strategy never crashes the daemon. Implementations are still
    expected to catch their own expected failure modes (network
    timeouts, missing-clone-path, etc.) and log them at WARNING — the
    reconciler-level catch is defense in depth, not the primary
    contract.
    """

    def refresh(self, project: ReconcilerProject) -> None: ...


class OnPollFetch:
    """Refresh ``origin/<default>`` once per poll cycle, per project.

    Best-effort: catches and logs (WARNING) every exception so the
    Reconciler's tick continues even on transient network or
    filesystem trouble. A missing ``local_clone_path`` on
    :class:`~foreman.reconciler.daemon.ReconcilerProject` is treated
    as "no clone known; skip" (logged at DEBUG, since this is the
    expected case for test fixtures that don't wire a clone path).
    """

    def refresh(self, project: ReconcilerProject) -> None:
        if not project.local_clone_path:
            logger.debug(
                "clone refresh skipped for project=%s: no local_clone_path",
                project.name,
            )
            return
        try:
            fetch_origin_default_branch(Path(project.local_clone_path))
        except Exception as exc:
            logger.warning(
                "clone refresh failed for project=%s clone=%s: %s",
                project.name,
                project.local_clone_path,
                exc,
            )


class OnDispatchFetchOnly:
    """No-op per-poll refresh. Defense-in-depth + paranoid opt-out.

    Selecting this strategy preserves the pre-#291 behavior: the
    per-dispatch fetch in
    :class:`~foreman.worktree.WorktreeManager` is the ONLY source of
    clone refresh. Use this when the operator wants strict
    on-dispatch-only network activity (air-gapped CI, throttled
    bandwidth, etc.).
    """

    def refresh(self, project: ReconcilerProject) -> None:
        return
