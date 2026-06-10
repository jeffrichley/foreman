"""Action catalog for v3 — what the reconciler can do, and the context it
needs to do it. The executor itself lands in Task 7; this module first
establishes the enum + context shape so rules (Task 5+) can reference them.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any, Literal

from foreman.config import MergeMechanism
from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.host import ReconcilerHost
from foreman.reconciler.state import IssueState, ProjectSnapshot, PRState

logger = logging.getLogger(__name__)


class Action(enum.Enum):
    """Catalog of every state-changing operation the reconciler can emit.

    Order matches the spec's transition flow for readability; the enum is
    NOT ordered (no value-comparison semantics intended).
    """

    NOOP = "noop"
    SURFACE_HELP = "surface_help"
    # foreman#228 (runaway-burn defense): per-ticket-per-role
    # consecutive-failure rate-limit trip. The reconciler emits this
    # when N same-role failures have landed on the same ticket within
    # W seconds. The executor removes the role's in-flight label, adds
    # ``foreman:needs-help``, posts a comment naming the role + last
    # failures, and writes a ``rate_limit_reset:<dispatch_action>``
    # sentinel so the human's subsequent re-queue starts a fresh
    # window. Separate enum value (not reusing ``SURFACE_HELP``) so the
    # comment copy + the reset-sentinel side effect are first-class in
    # the executor and so the existing ``surface_help`` cooldown
    # (``_safety_with_rate_limit`` 1h gate) doesn't suppress this trip
    # — the rate-limit predicate is its own gate.
    RATE_LIMIT_TRIP = "rate_limit_trip"
    # foreman#171: ``foreman:plan`` is the documented "queue for planning"
    # entry label, but no rule fires on it directly — ``dispatch_planner``
    # requires ``foreman:planning``. This action is the forward-progress
    # bridge: ``foreman:plan`` → ``foreman:planning`` on the next poll.
    # Without it, fresh tickets stall invisibly (the observer also has to
    # include ``foreman:plan`` in its filter for the rule to ever fire).
    ADVANCE_LABEL_TO_PLANNING = "advance_label_to_planning"
    DISPATCH_PLANNER = "dispatch_planner"
    # foreman#165: the old ``MERGE_SPEC_PR`` / ``MERGE_IMPL_PR`` enum
    # values are gone. The flow is now: a ``foreman:*-approved`` issue
    # gets a ``foreman:merging-*`` label added by
    # ``ADVANCE_LABEL_TO_MERGING_*``, which makes the
    # ``ATTEMPT_MERGE_*`` action eligible on subsequent ticks. The
    # ``host.merge_pr`` call lives inside that handler when the PR's
    # ``mergeStateStatus`` is ``CLEAN``.
    ADVANCE_LABEL_TO_MERGING_PLAN = "advance_label_to_merging_plan"
    ADVANCE_LABEL_TO_MERGING_IMPL = "advance_label_to_merging_impl"
    ATTEMPT_MERGE_PLAN = "attempt_merge_plan"
    ATTEMPT_MERGE_IMPL = "attempt_merge_impl"
    ADVANCE_LABEL_TO_PLAN_APPROVED = "advance_label_to_plan_approved"
    DISPATCH_WORKER = "dispatch_worker"
    # foreman v3 rescue Stage 2: split Reviewer + Fixer dispatch actions
    # by target. Pre-split, both spec-side and impl-side dispatch_reviewer
    # rules shared the action key ``dispatch_reviewer``, which made the
    # spec-side ``count_completed`` idempotence gate flip permanently
    # False once an impl-side dispatch landed (HIGH #7). Same shape for
    # the Fixer: ``foreman fix --target spec_pr|impl_pr`` requires per-
    # target action so the executor can plumb the right flag (CRITICAL #4).
    # foreman#165 reuses the same shape for ``ATTEMPT_MERGE_PLAN`` /
    # ``ATTEMPT_MERGE_IMPL`` so per-target ``count_completed`` rows stay
    # attributed correctly.
    DISPATCH_REVIEWER_SPEC = "dispatch_reviewer_spec"
    DISPATCH_REVIEWER_IMPL = "dispatch_reviewer_impl"
    DISPATCH_FIXER_SPEC = "dispatch_fixer_spec"
    DISPATCH_FIXER_IMPL = "dispatch_fixer_impl"
    ADVANCE_LABEL_TO_DONE = "advance_label_to_done"


@dataclass(frozen=True)
class ActionContext:
    """Everything a rule + executor need to evaluate or apply an action.

    `snapshot` is the full project view. `issue` is the focal ticket.
    `pr` is the linked PR if one exists (None for tickets pre-PR or after merge).
    `log` is the execution log — rules consult it for idempotence; executor
    writes through it.

    `auto_merge_spec` and `auto_merge_impl` are the EFFECTIVE per-project
    auto-merge flags (after global+project resolution via
    ``ReconcilerConfig.effective_auto_merge_*``). Rules consult these to
    decide between MERGE_SPEC_PR / MERGE_IMPL_PR and a "park for human
    review" transition. Defaults match the global ``ReconcilerConfig``
    defaults (spec=True, impl=False) so legacy callers that omit the flags
    keep the same behavior.

    `merge_mechanism` is the EFFECTIVE per-project merge mechanism (after
    global+project resolution via ``ReconcilerConfig.effective_merge_mechanism``).
    The executor passes it to ``host.merge_pr`` so the host can dispatch
    between ``direct`` (call ``pr.merge()``) and ``queue`` (defer to GitHub
    MergeQueue). Default is ``"direct"`` matching the global config default;
    see foreman#158.
    """

    snapshot: ProjectSnapshot
    issue: IssueState
    pr: PRState | None
    log: ExecutionLog
    auto_merge_spec: bool = True
    auto_merge_impl: bool = False
    merge_mechanism: MergeMechanism = "direct"
    # foreman#228: per-ticket-per-role consecutive-failure rate-limit
    # knobs. Defaults mirror ``DaemonConfig`` so tests + reconciler
    # paths that omit them keep the same behavior. Production resolves
    # the effective values via the per-tick context-build path in the
    # reconciler daemon (which reads ``ReconcilerConfig``).
    rate_limit_max_consecutive_failures: int = 3
    rate_limit_window_seconds: int = 1800

    @property
    def ticket_id(self) -> str:
        return self.snapshot.ticket_id_for(self.issue.number)


_DISPATCH_ROLE_FOR_ACTION: dict[Action, tuple[str, str | None]] = {
    Action.DISPATCH_PLANNER: ("planner", None),
    Action.DISPATCH_WORKER: ("worker", None),
    Action.DISPATCH_REVIEWER_SPEC: ("reviewer", "spec_pr"),
    Action.DISPATCH_REVIEWER_IMPL: ("reviewer", "impl_pr"),
    Action.DISPATCH_FIXER_SPEC: ("fixer", "spec_pr"),
    Action.DISPATCH_FIXER_IMPL: ("fixer", "impl_pr"),
}


# foreman#165: the merging-* label the corresponding ATTEMPT_MERGE_*
# handler operates under. Reads from the focal issue's labels in the
# rule; used here only for surface_help comment attribution.
_MERGING_LABEL_FOR_TARGET: dict[str, str] = {
    "plan": "foreman:merging-plan",
    "impl": "foreman:merging-impl",
}


# foreman#228: terminal blocking label name. Matches the role-runner
# helper from foreman#229 so the operator always sees the same name in
# failure comments regardless of which path surfaced them.
TERMINAL_NEEDS_HELP_LABEL = "foreman:needs-help"


# foreman#228: the rate-limit rule name → (role, dispatch_action,
# in_flight_label) mapping the RATE_LIMIT_TRIP executor uses to figure
# out which label to remove and which dispatch-action key to write the
# reset sentinel against. The rule layer creates one rate-limit rule
# per dispatch-action key (planner / worker / reviewer-spec /
# reviewer-impl / fixer-spec / fixer-impl), each named with the prefix
# below. The executor parses the rule name to recover the per-action
# context.
_RATE_LIMIT_RULE_PREFIX = "rate_limit_consecutive_failures:"

# (dispatch_action → (role_name, in_flight_label)). The role_name is
# used only in the human-facing comment prose. The in_flight_label is
# what the executor removes; the dispatch_action is what the reset
# sentinel is keyed against AND what ``count_recent_failures`` /
# ``recent_failure_details`` query for. Keeping the three together in
# ONE structure means a future role addition touches one place.
_RATE_LIMIT_TARGETS: dict[str, tuple[str, str]] = {
    "dispatch_planner": ("planner", "foreman:planning"),
    "dispatch_worker": ("worker", "foreman:plan-approved"),
    "dispatch_reviewer_spec": ("reviewer", "foreman:planning"),
    "dispatch_reviewer_impl": ("reviewer", "foreman:impl-review"),
    "dispatch_fixer_spec": ("fixer", "foreman:spec-fix"),
    "dispatch_fixer_impl": ("fixer", "foreman:impl-fix"),
}


def _parse_rate_limit_rule_name(rule_name: str) -> str | None:
    """Extract the dispatch-action key from a rate-limit rule name.

    Returns ``"dispatch_planner"`` (etc.) for
    ``"rate_limit_consecutive_failures:dispatch_planner"``, or ``None``
    when the rule_name doesn't match the rate-limit shape.
    """
    if not rule_name.startswith(_RATE_LIMIT_RULE_PREFIX):
        return None
    suffix = rule_name[len(_RATE_LIMIT_RULE_PREFIX):]
    return suffix if suffix in _RATE_LIMIT_TARGETS else None


def _format_rate_limit_comment(
    *,
    role: str,
    dispatch_action: str,
    failure_count: int,
    window_seconds: int,
    recent: list[dict[str, Any]],
) -> str:
    """Render the GitHub comment body posted on RATE_LIMIT_TRIP.

    Names the role + dispatch action that tripped, the configured N/W
    knobs, and a digest of the last failures (newest first). Each
    failure line is one bullet capped at ~200 chars so a runaway
    provider that filled details with megabytes doesn't post a multi-MB
    comment. The full per-dispatch subprocess log naming convention
    (foreman#119) is referenced so an operator can spelunk further;
    pairs with the truncation-marker shape from foreman#229.
    """
    window_minutes = window_seconds // 60
    lines = [
        f"foreman {role} dispatch ({dispatch_action}) hit the per-ticket "
        f"per-role consecutive-failure rate-limit "
        f"(foreman#228): {failure_count} failures within the last "
        f"{window_minutes} minutes.",
        "",
        f"The ticket has been transitioned to `{TERMINAL_NEEDS_HELP_LABEL}` "
        "to stop the daemon from re-dispatching on every poll cycle "
        "(yesterday's runaway: foreman#227 spun 171 consecutive "
        "Planner dispatches in 2h52m before this defense existed). "
        f"Remove `{TERMINAL_NEEDS_HELP_LABEL}` to resume the autonomous "
        "flow once the underlying cause is addressed; the counter has "
        "been reset so the next failure starts a fresh window.",
    ]
    if recent:
        lines.append("")
        lines.append("Recent failures (newest first):")
        for entry in recent:
            ts = entry.get("ts", "?")
            outcome = entry.get("outcome", "?")
            details = entry.get("details") or {}
            err = str(details.get("error", "")).strip()
            if len(err) > 200:
                err = err[:200] + "... (truncated)"
            issue_no = details.get("issue", "?")
            head = f"- {ts} UTC · outcome=`{outcome}` · issue={issue_no}"
            if err:
                head += f" · `{err}`"
            lines.append(head)
        lines.append("")
        lines.append(
            "Full tracebacks live in the daemon's per-dispatch subprocess "
            "log convention `<log_dir>/<role>/<issue>__<timestamp>.log` "
            "(`<log_dir>` = `$FOREMAN_LOG_DIR` or `~/.foreman/logs`)."
        )
    return "\n".join(lines)


def _handle_rate_limit_trip(
    ctx: ActionContext,
    host: ReconcilerHost,
    *,
    rule_name: str,
) -> None:
    """Side effects for ``Action.RATE_LIMIT_TRIP`` (foreman#228).

    Per the locked contract from Pepper's criterion-check:

    - Remove the role's in-flight label (e.g., ``foreman:planning`` for
      Planner) so a stale label doesn't mis-route future polls.
    - Add ``foreman:needs-help`` so safety-tier rules at higher
      precedence keep firing NOOP / SURFACE_HELP rather than re-routing
      forward-progress dispatch.
    - Post a comment naming the role, the trip count + window, and the
      last few failures so the operator has actionable context without
      having to spelunk the daemon logs.
    - Write a ``rate_limit_reset:<dispatch_action>`` sentinel so the
      counter clears for the (action, ticket_id) — when the human then
      removes ``foreman:needs-help`` and re-adds the entry-point label,
      the next failure starts a fresh window deterministic regardless
      of how much of W has elapsed.

    The dispatch-action key the trip relates to is encoded in
    ``rule_name`` via the :data:`_RATE_LIMIT_RULE_PREFIX` convention
    (e.g., ``rate_limit_consecutive_failures:dispatch_planner``).

    All four side effects are independently try-wrapped (mirroring
    ``foreman.roles.handle_unhandled_role_exception``): a transient
    failure of any one (GitHub 5xx, race-window 404, sentinel-write
    hiccup) is logged and the remaining steps still run. The order is
    chosen so the most-load-bearing step (``needs-help`` label add)
    runs first.
    """
    dispatch_action = _parse_rate_limit_rule_name(rule_name)
    if dispatch_action is None:
        # Defensive: a future caller that fires RATE_LIMIT_TRIP under
        # an unknown rule_name shape — log loudly + skip the structured
        # side effects rather than papering over with an arbitrary
        # default that could clear the wrong counter or remove the
        # wrong label.
        logger.error(
            "RATE_LIMIT_TRIP fired under unrecognized rule_name=%r; "
            "skipping side effects for ticket %s",
            rule_name,
            ctx.ticket_id,
        )
        return
    role, in_flight_label = _RATE_LIMIT_TARGETS[dispatch_action]
    recent = ctx.log.recent_failure_details(
        action=dispatch_action,
        ticket_id=ctx.ticket_id,
        within_seconds=ctx.rate_limit_window_seconds,
        limit=ctx.rate_limit_max_consecutive_failures,
    )
    failure_count = ctx.log.count_recent_failures(
        action=dispatch_action,
        ticket_id=ctx.ticket_id,
        within_seconds=ctx.rate_limit_window_seconds,
    )
    body = _format_rate_limit_comment(
        role=role,
        dispatch_action=dispatch_action,
        failure_count=failure_count,
        window_seconds=ctx.rate_limit_window_seconds,
        recent=recent,
    )
    # Each side effect is wrapped INDEPENDENTLY so a single host hiccup
    # (GitHub 5xx on the comment, race-window 404 on remove_label, disk
    # write hiccup on the sentinel) cannot leave the others undone. This
    # mirrors the pattern from ``handle_unhandled_role_exception`` in
    # ``foreman.roles`` (#229) — Pepper called the independent wrapping
    # load-bearing because the rate-limit's contract depends on each
    # of these landing.
    #
    # The semantic priorities, in order:
    #   1. ``add_label(needs-help)`` — the load-bearing safety move.
    #      Without it the rate-limit rule predicate's `needs-help in
    #      labels: return False` short-circuit cannot fire and the
    #      trip's protection is broken. Wrap and log loudly, but do
    #      NOT abort the remaining steps — a label-add failure today
    #      is far more recoverable than a permanently-stuck loop.
    #   2. ``remove_label(in_flight)`` — hygiene. A failed remove
    #      leaves the ticket double-labeled (in-flight + needs-help);
    #      the higher-precedence ``needs_help_label`` rule wins, so
    #      forward-progress dispatch stays gated.
    #   3. ``post_comment`` — diagnostic. Operator loses the last-N
    #      failure summary if this fails; the label transition (and
    #      the structural protection) still holds.
    #   4. ``write_rate_limit_reset`` — load-bearing for the contract.
    #      Without the sentinel, a same-window human re-queue trips
    #      immediately at N=1 (the re-trip whiplash Pepper warned
    #      about). Log loudly on failure: this is the one to wake
    #      somebody for.
    try:
        host.add_label(
            owner=ctx.snapshot.owner,
            repo=ctx.snapshot.repo,
            issue=ctx.issue.number,
            label=TERMINAL_NEEDS_HELP_LABEL,
        )
    except Exception:
        logger.exception(
            "RATE_LIMIT_TRIP: failed to add %s on issue %d (rule=%s); "
            "continuing with remaining trip side effects but the rate-limit "
            "predicate's short-circuit may not fire on the next poll",
            TERMINAL_NEEDS_HELP_LABEL,
            ctx.issue.number,
            rule_name,
        )
    if in_flight_label in ctx.issue.labels:
        try:
            host.remove_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label=in_flight_label,
            )
        except Exception:
            logger.exception(
                "RATE_LIMIT_TRIP: failed to remove %s on issue %d; ticket "
                "will carry both labels until the next reconcile cycle",
                in_flight_label,
                ctx.issue.number,
            )
    try:
        host.post_comment(
            owner=ctx.snapshot.owner,
            repo=ctx.snapshot.repo,
            issue=ctx.issue.number,
            body=body,
        )
    except Exception:
        logger.exception(
            "RATE_LIMIT_TRIP: failed to post diagnostic comment on issue %d "
            "(rule=%s); operator will see needs-help label but no comment",
            ctx.issue.number,
            rule_name,
        )
    try:
        ctx.log.write_rate_limit_reset(
            action=dispatch_action,
            ticket_id=ctx.ticket_id,
            details={
                "tripped_by_rule": rule_name,
                "failure_count_at_trip": failure_count,
                "window_seconds": ctx.rate_limit_window_seconds,
                "issue": ctx.issue.number,
            },
        )
    except Exception:
        logger.exception(
            "RATE_LIMIT_TRIP: failed to write reset sentinel for "
            "(action=%s, ticket=%s) — human re-queue may re-trip "
            "immediately at N=1 within W. Investigate the execution log.",
            dispatch_action,
            ctx.ticket_id,
        )


def _handle_attempt_merge(
    ctx: ActionContext,
    host: ReconcilerHost,
    *,
    target: Literal["plan", "impl"],
) -> None:
    """Drive GitHub's ``mergeStateStatus`` machine for one PR.

    Shared body for ``ATTEMPT_MERGE_PLAN`` and ``ATTEMPT_MERGE_IMPL``.
    The two enum values exist so per-target ``count_completed`` rows
    stay attributed correctly (same shape as the Stage-2 Reviewer +
    Fixer split). ``target`` selects the head-ref expectation in the
    rule predicate; it's preserved here for log attribution only.

    State table (foreman#165 issue body):

    - ``CLEAN``: all gates green → call ``host.merge_pr`` with the
      project's configured ``merge_mechanism``.
    - ``BEHIND``: out-of-date with base, no conflict → server-side
      rebase via ``host.update_branch``. GitHub recomputes
      ``mergeStateStatus``; the next poll re-enters this handler.
    - ``BLOCKED`` + pending required checks → no-op (wait for CI).
    - ``BLOCKED`` + no pending required checks → at least one
      required check terminated non-SUCCESS → surface needs-help.
    - ``UNSTABLE`` or ``DIRTY`` → surface needs-help (conservative
      defaults per the issue body's "anti-patterns to watch" note).
    - ``UNKNOWN`` → no-op. GitHub computes lazily; the next poll's
      re-read will have an actionable value.
    - ``DRAFT`` or ``HAS_HOOKS`` → no-op + log at INFO. Shouldn't
      occur on foreman PRs; documenting the no-op makes it explicit
      rather than fall-through-to-needs-help via AttributeError.
    """
    if ctx.pr is None:
        raise RuntimeError(
            f"attempt_merge_{target} requires a PR in context"
        )
    mergeability = host.get_pr_mergeability(
        owner=ctx.snapshot.owner,
        repo=ctx.snapshot.repo,
        pr_number=ctx.pr.number,
    )
    state = mergeability.state

    if state == "CLEAN":
        host.merge_pr(
            owner=ctx.snapshot.owner,
            repo=ctx.snapshot.repo,
            pr_number=ctx.pr.number,
            mechanism=ctx.merge_mechanism,
        )
        return

    if state == "BEHIND":
        host.update_branch(
            owner=ctx.snapshot.owner,
            repo=ctx.snapshot.repo,
            pr_number=ctx.pr.number,
        )
        return

    if state == "BLOCKED":
        if mergeability.pending_required_check_count > 0:
            logger.info(
                "attempt_merge_%s: BLOCKED with %d pending required check(s) "
                "for %s/%s#%d — waiting for CI, will re-evaluate next poll",
                target,
                mergeability.pending_required_check_count,
                ctx.snapshot.owner,
                ctx.snapshot.repo,
                ctx.pr.number,
            )
            return
        # No pending required checks → at least one required check has
        # terminated non-SUCCESS → needs human review.
        _surface_attempt_merge_needs_help(ctx, host, target=target, reason="blocked")
        return

    if state in ("UNSTABLE", "DIRTY"):
        _surface_attempt_merge_needs_help(ctx, host, target=target, reason=state.lower())
        return

    if state == "UNKNOWN":
        logger.info(
            "attempt_merge_%s: mergeStateStatus=UNKNOWN for %s/%s#%d — "
            "GitHub still computing, will re-evaluate next poll",
            target,
            ctx.snapshot.owner,
            ctx.snapshot.repo,
            ctx.pr.number,
        )
        return

    if state in ("DRAFT", "HAS_HOOKS"):
        # Should not occur on foreman PRs (no Draft PRs, no
        # branch-protection hooks the bot can't pass). Documented no-op
        # so the handler doesn't fall through to needs-help by accident.
        logger.info(
            "attempt_merge_%s: unexpected mergeStateStatus=%s for "
            "%s/%s#%d — leaving inert; operator may need to inspect",
            target,
            state,
            ctx.snapshot.owner,
            ctx.snapshot.repo,
            ctx.pr.number,
        )
        return

    # Unknown state value — GitHub introduced something new. Log and
    # treat as needs-help so it surfaces to a human rather than silently
    # dropping a merge attempt.
    logger.warning(
        "attempt_merge_%s: unrecognized mergeStateStatus=%r for %s/%s#%d — "
        "surfacing needs-help",
        target,
        state,
        ctx.snapshot.owner,
        ctx.snapshot.repo,
        ctx.pr.number,
    )
    _surface_attempt_merge_needs_help(
        ctx, host, target=target, reason=f"unrecognized_state:{state}"
    )


def _surface_attempt_merge_needs_help(
    ctx: ActionContext,
    host: ReconcilerHost,
    *,
    target: Literal["plan", "impl"],
    reason: str,
) -> None:
    """Add ``foreman:needs-help`` + post the same surfacing copy as
    ``Action.SURFACE_HELP`` so operators get a single, consistent
    surface across both the safety-rule path and the attempt-merge
    needs-help branches."""
    merging_label = _MERGING_LABEL_FOR_TARGET[target]
    host.add_label(
        owner=ctx.snapshot.owner,
        repo=ctx.snapshot.repo,
        issue=ctx.issue.number,
        label="foreman:needs-help",
    )
    host.post_comment(
        owner=ctx.snapshot.owner,
        repo=ctx.snapshot.repo,
        issue=ctx.issue.number,
        body=(
            "Foreman v3 surfaced this ticket for human attention "
            f"(attempt_merge_{target}: {reason}; label {merging_label}). "
            "Investigate the PR's mergeable state and either fix or "
            "remove the `foreman:needs-help` label to resume autonomous "
            "flow."
        ),
    )


def execute_action(
    action: Action,
    ctx: ActionContext,
    *,
    host: ReconcilerHost,
    rule_name: str,
    dry_run: bool,
) -> None:
    """Execute one action with single-writer log + dry-run support.

    Sequence: write start row -> call host -> write termination row (success
    or error). On exception the start row is terminated with outcome='error'
    and the exception is logged; the executor never re-raises (one bad action
    must not crash the reconciler loop).

    For dry_run: skip host entirely, write a single row with outcome='dry_run'.
    """
    if action is Action.NOOP:
        return

    if dry_run:
        ctx.log.write_action(
            ticket_id=ctx.ticket_id,
            project=ctx.snapshot.project,
            rule_name=rule_name,
            action=action.value,
            outcome="dry_run",
            details={
                "issue": ctx.issue.number,
                "pr": ctx.pr.number if ctx.pr else None,
            },
        )
        return

    start_id = ctx.log.write_action(
        ticket_id=ctx.ticket_id,
        project=ctx.snapshot.project,
        rule_name=rule_name,
        action=action.value,
        outcome="running",
        details={
            "issue": ctx.issue.number,
            "pr": ctx.pr.number if ctx.pr else None,
        },
    )

    try:
        if action is Action.RATE_LIMIT_TRIP:
            _handle_rate_limit_trip(ctx, host, rule_name=rule_name)
        elif action is Action.SURFACE_HELP:
            host.add_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:needs-help",
            )
            host.post_comment(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                body=(
                    "Foreman v3 surfaced this ticket for human attention. "
                    "Investigate state and either fix or remove the "
                    "`foreman:needs-help` label to resume autonomous flow."
                ),
            )
        elif action in _DISPATCH_ROLE_FOR_ACTION:
            role, target = _DISPATCH_ROLE_FOR_ACTION[action]
            pid = host.dispatch_role(
                role=role,
                target=target,
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                pr_number=ctx.pr.number if ctx.pr else None,
                start_log_id=start_id,
                project=ctx.snapshot.project,
            )
            # ``pid is None`` means the concurrency cap was full and the
            # host opted to skip this dispatch — see V3GitHubHost.dispatch_role
            # for the rationale. Terminate the start row with a
            # ``skipped_capacity`` outcome so it doesn't dangle in
            # ``running`` state forever, and return early so the success
            # path below doesn't write a second termination row. The
            # attempt-counting rules in rules.py count ``success`` only
            # (see ``_planning_pr_needs_review``'s docstring), so a
            # ``skipped_capacity`` row does NOT burn budget — the
            # cap-skip is rule-neutral, which is what we want.
            if pid is None:
                ctx.log.terminate_action(
                    parent_log_id=start_id,
                    outcome="skipped_capacity",
                    details={
                        "reason": "concurrency cap reached at dispatch time",
                    },
                )
                return
        elif action is Action.ADVANCE_LABEL_TO_MERGING_PLAN:
            host.add_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:merging-plan",
            )
        elif action is Action.ADVANCE_LABEL_TO_MERGING_IMPL:
            host.add_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:merging-impl",
            )
        elif action is Action.ATTEMPT_MERGE_PLAN:
            _handle_attempt_merge(ctx, host, target="plan")
        elif action is Action.ATTEMPT_MERGE_IMPL:
            _handle_attempt_merge(ctx, host, target="impl")
        elif action is Action.ADVANCE_LABEL_TO_PLANNING:
            # foreman#171: ``foreman:plan`` is the queue label; advance to
            # ``foreman:planning`` so ``dispatch_planner`` fires on the
            # next poll. The predicate (_plan_label_only) gates this to
            # truly-fresh tickets — no phase labels, no hold/needs-help,
            # no impl-attempt-N/fix-attempt-N counters.
            host.remove_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:plan",
            )
            host.add_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:planning",
            )
        elif action is Action.ADVANCE_LABEL_TO_PLAN_APPROVED:
            host.remove_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:planning",
            )
            host.add_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:plan-approved",
            )
        elif action is Action.ADVANCE_LABEL_TO_DONE:
            host.remove_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:impl-approved",
            )
            host.add_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:done",
            )

        # Some actions complete synchronously (label changes, merges,
        # surface_help) and we write the success termination here. Subprocess
        # dispatches return from this branch already — the host's background
        # tracker (or terminate_dispatch in synchronous tests) writes the
        # termination row when the subprocess exits, so we must NOT write a
        # success row here or count_completed would double-count.
        if action in _DISPATCH_ROLE_FOR_ACTION:
            return

        ctx.log.terminate_action(parent_log_id=start_id, outcome="success", details={})

    except Exception as exc:
        logger.exception("action %s failed for ticket %s", action.name, ctx.ticket_id)
        ctx.log.terminate_action(
            parent_log_id=start_id,
            outcome="error",
            details={"error": str(exc)},
        )
