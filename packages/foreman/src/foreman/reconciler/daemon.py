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
      → spec PR (``foreman/issue-N``)
    - ``foreman:impl-review`` / ``foreman:impl-approved`` /
      ``foreman:impl-fix`` → impl PR (``foreman/impl-N``)

    If the label set spans both phases (transient state during a label
    swap), prefer the impl PR — the later stage wins. Falls back to
    ``linked_prs[0]`` when no shape filter matches (legacy or
    operator-created PRs not following the foreman branch conventions).
    """
    if not linked_prs:
        return None

    spec_phase = {"foreman:planning", "foreman:plan-approved", "foreman:spec-fix"}
    impl_phase = {"foreman:impl-review", "foreman:impl-approved", "foreman:impl-fix"}

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
    return linked_prs[0]


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
    """

    name: str
    owner: str
    repo: str
    auto_merge_spec: bool = True
    auto_merge_impl: bool = False


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
            self._reconcile_project(snapshot, project)

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
