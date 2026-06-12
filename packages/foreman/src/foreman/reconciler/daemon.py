"""Reconciler — the v3 daemon's tick + run loop.

Composes observer + rules + actions. Per tick: fetch each project's
snapshot, evaluate the rule catalog per ticket, execute the action via the
host (or skip-and-log under dry_run). Fail-stop on observer outage with a
yellow alert after N consecutive failures.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from foreman.config import MergeMechanism
from foreman.reconciler.actions import Action, ActionContext, execute_action
from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.host import ReconcilerHost
from foreman.reconciler.observer import (
    GHGraphQLClient,
    ObserverError,
    ObserverRateLimited,
    ObserverUnreachable,
    fetch_project_state,
)
from foreman.reconciler.rules import evaluate_with_rule
from foreman.reconciler.state import IssueState, ProjectSnapshot, PRState

logger = logging.getLogger(__name__)


def _pick_pr_for_ticket(
    issue: IssueState, linked_prs: list[PRState]
) -> PRState | None:
    """Pick the right linked PR for this ticket based on the issue's current
    label state.

    During the brief stacked-PR window where both a spec PR
    (``foreman/issue-N``) and an impl PR (``foreman/impl-N``) carry
    ``closingIssuesReferences`` pointing to the same issue, GraphQL result
    ordering is undefined. ``linked_prs[0]`` would arbitrarily pick one,
    which can land the wrong PR into ``ctx.pr`` and let
    ``merge_spec_pr`` / ``merge_impl_pr`` fire on a shape-mismatched PR
    (adversarial review MEDIUM #11).

    Routing by label phase:

    - ``foreman:planning`` / ``foreman:plan-approved`` / ``foreman:spec-fix``
      / ``foreman:merging-plan`` → spec PR (``foreman/issue-N``)
    - ``foreman:impl-review`` / ``foreman:impl-approved`` /
      ``foreman:impl-fix`` / ``foreman:merging-impl`` → impl PR
      (``foreman/impl-N``)

    The transitional ``foreman:merging-*`` labels are intentionally in their
    side's phase set even though ``ADVANCE_LABEL_TO_MERGING_*`` keeps the
    prior ``*-approved`` label alongside in production (so the picker would
    have routed correctly via that label). Carrying the merging-* labels in
    the phase set removes the fragile coupling — surfaced 2026-06-12 when
    a manual relabel that dropped the approved label silently stalled
    ``attempt_merge_impl`` for ~75 minutes (D9 dogfood verification).

    If the label set spans both phases (transient state during a label
    swap), prefer the impl PR — the later stage wins.

    Returns ``None`` when no shape filter matches (Pass 2 MEDIUM): the issue
    carries no foreman phase label (e.g., only ``foreman:hold`` or
    ``foreman:needs-help``) or the linked PR's head_ref doesn't follow the
    foreman branch convention. Some safety rules don't filter by shape and
    would otherwise fire against an arbitrarily-picked off-shape PR;
    returning None makes those rules safely no-op via the existing
    ``ctx.pr is None`` guards.
    """
    if not linked_prs:
        return None

    spec_phase = {
        "foreman:planning",
        "foreman:plan-approved",
        "foreman:spec-fix",
        "foreman:merging-plan",
    }
    impl_phase = {
        "foreman:impl-review",
        "foreman:impl-approved",
        "foreman:impl-fix",
        "foreman:merging-impl",
    }

    labels = set(issue.labels)
    prefer_spec = bool(labels & spec_phase)
    prefer_impl = bool(labels & impl_phase)

    if prefer_spec and not prefer_impl:
        for pr in linked_prs:
            if pr.head_ref.startswith("foreman/issue-"):
                return pr
    if prefer_impl and not prefer_spec:
        for pr in linked_prs:
            if pr.head_ref.startswith("foreman/impl-"):
                return pr
    if prefer_spec and prefer_impl:
        # Transient state spans both phases — later stage wins.
        for pr in linked_prs:
            if pr.head_ref.startswith("foreman/impl-"):
                return pr
        for pr in linked_prs:
            if pr.head_ref.startswith("foreman/issue-"):
                return pr
    return None


@dataclass(frozen=True)
class ReconcilerProject:
    """One registered project the reconciler watches.

    ``auto_merge_spec`` / ``auto_merge_impl`` are the EFFECTIVE flags after
    resolving per-project overrides against ``ReconcilerConfig`` defaults
    (via ``ReconcilerConfig.effective_auto_merge_*(project_cfg)``). They
    travel into each per-tick ``ActionContext`` so the rule catalog can
    decide between auto-merge and park-for-human transitions. Defaults
    match the global ``ReconcilerConfig`` defaults so tests that omit them
    keep the same behavior as production with no project-level overrides.

    ``merge_mechanism`` similarly carries the EFFECTIVE per-project merge
    mechanism — see ``ReconcilerConfig.effective_merge_mechanism`` and
    foreman#158. Default is ``"direct"`` (matches global default).
    """

    name: str
    owner: str
    repo: str
    auto_merge_spec: bool = True
    auto_merge_impl: bool = False
    merge_mechanism: MergeMechanism = "direct"


class Reconciler:
    """The v3 daemon's main loop."""

    def __init__(
        self,
        *,
        projects: tuple[ReconcilerProject, ...],
        log: ExecutionLog,
        gh: GHGraphQLClient,
        host: ReconcilerHost,
        dry_run: bool,
        alert_after_n_failures: int = 3,
        poll_interval_seconds: int = 60,
        shutdown_sentinel_path: Path | str | None = None,
        reload_callback: Callable[[], tuple[ReconcilerProject, ...]] | None = None,
        reload_sentinel_path: Path | str | None = None,
        rate_limit_max_consecutive_failures: int = 3,
        rate_limit_window_seconds: int = 1800,
    ) -> None:
        self.projects = projects
        self.log = log
        self.gh = gh
        self.host = host
        self.dry_run = dry_run
        self.alert_after_n_failures = alert_after_n_failures
        self.poll_interval_seconds = poll_interval_seconds
        # foreman#228: rate-limit knobs threaded into every
        # ActionContext built per-tick — the rules + executor read from
        # the context (single source of truth at the per-tick boundary).
        self.rate_limit_max_consecutive_failures = rate_limit_max_consecutive_failures
        self.rate_limit_window_seconds = rate_limit_window_seconds
        self._stop_event = asyncio.Event()
        self._consecutive_failures: dict[str, int] = {p.name: 0 for p in projects}
        # Sentinel-file-based graceful-shutdown signal. ``foreman daemon stop``
        # writes this file; we poll it each tick and trigger shutdown when
        # present. ``None`` disables the mechanism (used by tests that don't
        # need it — the existing signal-handler path still works).
        self._shutdown_sentinel_path: Path | None = (
            Path(shutdown_sentinel_path).expanduser()
            if shutdown_sentinel_path is not None
            else None
        )
        # Sentinel-file-based config-reload signal. ``foreman daemon reload``
        # writes this file; we poll it at the TOP of each tick (before the
        # per-project loop) so a newly-added project is reconciled in the
        # SAME tick. ``None`` callback disables the mechanism — tests that
        # don't exercise reload omit both kwargs.
        self._reload_callback = reload_callback
        self._reload_sentinel_path: Path | None = (
            Path(reload_sentinel_path).expanduser()
            if reload_sentinel_path is not None
            else None
        )

    async def tick(self) -> None:
        """Run one reconciliation pass over every project."""
        # Config-reload sentinel runs BEFORE the project loop so an
        # operator's ``foreman daemon reload`` applied moments before
        # this tick lets the newly-added project be reconciled in the
        # SAME tick rather than waiting a full poll cycle. Shutdown
        # sentinel still runs AFTER the loop (graceful: in-flight tick
        # completes first).
        if self._reload_sentinel_present():
            self._consume_reload_sentinel()
            self._apply_reload()

        for project in self.projects:
            try:
                snapshot = fetch_project_state(
                    project=project.name,
                    owner=project.owner,
                    repo=project.repo,
                    gh=self.gh,
                )
            except (ObserverRateLimited, ObserverUnreachable, ObserverError) as exc:
                self._consecutive_failures[project.name] += 1
                logger.warning(
                    "observer failed for project=%s (%d/%d): %s",
                    project.name,
                    self._consecutive_failures[project.name],
                    self.alert_after_n_failures,
                    exc,
                )
                if self._consecutive_failures[project.name] == self.alert_after_n_failures:
                    # Single alert row — log once per breach, not every poll.
                    self.log.write_action(
                        ticket_id=f"project:{project.name}",
                        project=project.name,
                        rule_name=None,
                        action="observer_failure_alert",
                        outcome="alert",
                        details={
                            "error_class": type(exc).__name__,
                            "error_message": str(exc),
                            "consecutive_failures": self._consecutive_failures[project.name],
                        },
                    )
                continue

            self._consecutive_failures[project.name] = 0
            self._reconcile_project(snapshot, project)

        # Sentinel-file-based shutdown check — runs once per tick after
        # all projects have been reconciled so an in-flight tick completes
        # before we set the stop event. On Windows this is the ONLY way
        # ``foreman daemon stop`` can request graceful shutdown (os.kill
        # there maps to TerminateProcess, which delivers no signal); on
        # POSIX it is additive to the SIGTERM-handler path installed by
        # the CLI.
        if self._shutdown_sentinel_present():
            logger.info(
                "shutdown sentinel detected at %s; initiating graceful shutdown",
                self._shutdown_sentinel_path,
            )
            self._consume_shutdown_sentinel()
            self._stop_event.set()

    def _shutdown_sentinel_present(self) -> bool:
        """Return True iff a configured sentinel file exists on disk."""
        if self._shutdown_sentinel_path is None:
            return False
        try:
            return self._shutdown_sentinel_path.exists()
        except OSError:
            # Filesystem hiccup — don't crash the tick over a missing volume.
            return False

    def _consume_shutdown_sentinel(self) -> None:
        """Delete the sentinel after detection.

        Cleanup so the next ``daemon start`` does not immediately shut
        down. ``FileNotFoundError`` is harmless (the file might have been
        removed externally between presence-check and unlink). Other
        errors are logged but don't block the shutdown — the stop event
        is set either way.
        """
        if self._shutdown_sentinel_path is None:
            return
        try:
            self._shutdown_sentinel_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception(
                "failed to delete shutdown sentinel at %s; continuing shutdown",
                self._shutdown_sentinel_path,
            )

    def _reload_sentinel_present(self) -> bool:
        """Return True iff a configured reload sentinel file exists on disk.

        Disabled (returns False) when ``reload_callback`` was not provided
        OR ``reload_sentinel_path`` is None — the reload mechanism only
        activates when both are wired by the CLI.
        """
        if self._reload_callback is None:
            return False
        if self._reload_sentinel_path is None:
            return False
        try:
            return self._reload_sentinel_path.exists()
        except OSError:
            # Filesystem hiccup — don't crash the tick over a missing volume.
            return False

    def _consume_reload_sentinel(self) -> None:
        """Delete the reload sentinel after detection.

        Always called regardless of whether the reload itself succeeds —
        a broken-config reload is the operator's bug, and we don't want a
        retry loop on every tick. ``FileNotFoundError`` is harmless (the
        file may have been removed externally between presence-check and
        unlink). Other errors are logged but don't block the apply step.
        """
        if self._reload_sentinel_path is None:
            return
        try:
            self._reload_sentinel_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception(
                "failed to delete reload sentinel at %s; continuing reload",
                self._reload_sentinel_path,
            )

    def _apply_reload(self) -> None:
        """Re-read the project tuple from the callback and diff against current.

        Idempotent: when the callback returns a tuple identical to
        ``self.projects`` (same names, same resolved auto-merge flags,
        same owner/repo), logs INFO and returns without writing an
        exec_log row. Otherwise installs the new tuple, initializes
        failure counters for added projects, drops counters for removed
        projects, logs INFO with the diff, and writes one
        ``config_reload`` exec_log row.

        Failure-tolerant: if the callback raises (e.g., TOML parse error
        from the operator mid-editing the file, or pydantic validation
        error from a missing required field), logs an ERROR, writes one
        ``config_reload`` row with outcome ``failed``, and leaves
        ``self.projects`` + ``self._consecutive_failures`` unchanged. The
        daemon keeps running. The sentinel is consumed regardless (see
        ``_consume_reload_sentinel``) so the operator's next reload
        attempt is the canonical retry.
        """
        # Type narrowing: we only reach here when both the callback and
        # the sentinel path were provided (see ``_reload_sentinel_present``).
        assert self._reload_callback is not None

        try:
            new_projects = self._reload_callback()
        except Exception as exc:
            logger.error(
                "config reload failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            self.log.write_action(
                ticket_id="daemon:reload",
                project="",
                rule_name=None,
                action="config_reload",
                outcome="failed",
                details={
                    "error_class": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            return

        # Compare by name — auto_merge flags may have been edited on a
        # surviving project without renaming it.
        current_by_name = {p.name: p for p in self.projects}
        new_by_name = {p.name: p for p in new_projects}
        added = sorted(new_by_name.keys() - current_by_name.keys())
        removed = sorted(current_by_name.keys() - new_by_name.keys())

        # No-change check: every surviving project's resolved fields
        # must also match (auto_merge_*, owner, repo). If any field
        # changed on a surviving project, treat as a change.
        survivors_unchanged = all(
            new_by_name[name] == current_by_name[name]
            for name in new_by_name.keys() & current_by_name.keys()
        )
        if not added and not removed and survivors_unchanged:
            logger.info("config reload: no changes")
            return

        self.projects = tuple(new_projects)
        for name in added:
            self._consecutive_failures[name] = 0
        for name in removed:
            self._consecutive_failures.pop(name, None)
        logger.info(
            "config reload: %d added, %d removed",
            len(added),
            len(removed),
            extra={"added": added, "removed": removed},
        )
        self.log.write_action(
            ticket_id="daemon:reload",
            project="",
            rule_name=None,
            action="config_reload",
            outcome="executed",
            details={"added": added, "removed": removed},
        )

    def _reconcile_project(
        self,
        snapshot: ProjectSnapshot,
        project: ReconcilerProject,
    ) -> None:
        for issue in snapshot.issues:
            linked_prs = snapshot.prs_for_issue(issue.number)
            pr = _pick_pr_for_ticket(issue, list(linked_prs))
            ctx = ActionContext(
                snapshot=snapshot,
                issue=issue,
                pr=pr,
                log=self.log,
                auto_merge_spec=project.auto_merge_spec,
                auto_merge_impl=project.auto_merge_impl,
                merge_mechanism=project.merge_mechanism,
                rate_limit_max_consecutive_failures=(
                    self.rate_limit_max_consecutive_failures
                ),
                rate_limit_window_seconds=self.rate_limit_window_seconds,
            )
            action, rule_name = evaluate_with_rule(ctx)
            if action is Action.NOOP:
                continue
            # ``evaluate_with_rule`` returns a non-None rule name whenever
            # action is not NOOP; the ``or "unknown"`` is a defensive fallback
            # mypy can verify against the union without runtime-narrowing logic.
            execute_action(
                action,
                ctx,
                host=self.host,
                rule_name=rule_name or "unknown",
                dry_run=self.dry_run,
            )

    async def run(self) -> None:
        """Forever loop. Stops cleanly when shutdown() is called."""
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception:
                logger.exception("reconciler tick raised; continuing")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.poll_interval_seconds,
                )
            except TimeoutError:
                pass

    async def shutdown(self) -> None:
        self._stop_event.set()
