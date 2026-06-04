"""Reconciler — the v3 daemon's tick + run loop.

Composes observer + rules + actions. Per tick: fetch each project's
snapshot, evaluate the rule catalog per ticket, execute the action via the
host (or skip-and-log under dry_run). Fail-stop on observer outage with a
yellow alert after N consecutive failures.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

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
from foreman.reconciler.rules import RULES, evaluate
from foreman.reconciler.state import ProjectSnapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcilerProject:
    """One registered project the reconciler watches."""

    name: str
    owner: str
    repo: str


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
    ) -> None:
        self.projects = projects
        self.log = log
        self.gh = gh
        self.host = host
        self.dry_run = dry_run
        self.alert_after_n_failures = alert_after_n_failures
        self.poll_interval_seconds = poll_interval_seconds
        self._stop_event = asyncio.Event()
        self._consecutive_failures: dict[str, int] = {p.name: 0 for p in projects}

    async def tick(self) -> None:
        """Run one reconciliation pass over every project."""
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
            self._reconcile_project(snapshot)

    def _reconcile_project(self, snapshot: ProjectSnapshot) -> None:
        for issue in snapshot.issues:
            linked_prs = snapshot.prs_for_issue(issue.number)
            pr = linked_prs[0] if linked_prs else None
            ctx = ActionContext(snapshot=snapshot, issue=issue, pr=pr, log=self.log)
            action = evaluate(ctx, rules=RULES)
            if action is Action.NOOP:
                continue
            rule_name = _rule_that_fired(ctx, action)
            execute_action(
                action,
                ctx,
                host=self.host,
                rule_name=rule_name,
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


def _rule_that_fired(ctx: ActionContext, action: Action) -> str:
    """Reverse-lookup which rule emitted this action — for log attribution."""
    for rule in RULES:
        try:
            if rule.when(ctx) and rule.then is action:
                return rule.name
        except Exception:
            continue
    return "unknown"
