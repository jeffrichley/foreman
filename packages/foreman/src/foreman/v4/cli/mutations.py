"""hold/resume/retry/skip/drop/set-state/enqueue — operator mutations.

Each command resolves the ticket via repo + applies the change. retry
enqueues a WorkItem (needs the QueueManager from ctx); enqueue inserts
a new ticket row at state ``Queued`` (bypassing the Poller's GitHub
label scan); the rest are repository-only.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path

import typer

from foreman.v4.git_provider import GitProvider
from foreman.v4.records import TicketRecord
from foreman.v4.repository import (
    TicketAlreadyExistsError,
    TicketNotFoundError,
    TicketRepository,
)

# Single source of truth for which states are terminal (machine-done).
# Imported rather than duplicated so changes to the terminal set stay in
# sync here automatically — duplicating the set is how past drift bugs
# crept in. foreman#443: ImplApproved was removed from _TERMINAL_STATE_NAMES
# (it now polls for the human merge rather than parking as a dead-end).
from foreman.v4.state import _TERMINAL_STATE_NAMES
from foreman.v4.states.registry import STATE_REGISTRY
from foreman.v4.work import WorkItem
from foreman.worktree import WorktreeManager

# foreman#414: terminal states an operator can recover via ``retry`` — the
# command re-dispatches the role that escalated. ``Done`` is a happy
# terminal (nothing to retry). foreman#443: ``ImplApproved`` is no longer
# terminal; a ticket polling at ImplApproved is simply re-enqueued by the
# Poller each tick without any special retry handling.
_RETRYABLE_TERMINALS = frozenset({"NeedsHelp", "Failed"})


@dataclass(frozen=True, slots=True)
class ResetPlan:
    """What ``foreman reset`` will do, decided in the discovery phase.

    Built read-only from current GitHub + Postgres + filesystem state by
    :func:`_discover`. Walked destructively by :func:`_execute`. Steps
    that are off — no PR matched, no row in Postgres, ``--keep-pr`` set —
    are encoded as ``None`` / empty / False so the renderer + executor
    can skip them uniformly.
    """
    project: str
    issue_number: int
    spec_pr: int | None
    impl_pr: int | None
    delete_branches: list[str]
    prune_worktrees: bool
    strip_labels: set[str]
    delete_ticket_id: int | None
    apply_plan_label: bool


def _discover(
    *,
    git: GitProvider,
    repo: TicketRepository,
    project: str,
    issue_number: int,
    keep_pr: bool,
    keep_worktree: bool,
    retrigger: bool,
) -> ResetPlan:
    """Read-only scan of current state. No mutations."""
    if keep_pr:
        spec_pr = None
        impl_pr = None
    else:
        spec_pr = git.find_open_pr_by_head_branch(
            project=project, branch_name=f"foreman/issue-{issue_number}",
        )
        impl_pr = git.find_open_pr_by_head_branch(
            project=project, branch_name=f"foreman/impl-{issue_number}",
        )
    # Branches: always include both candidates. delete_branch is idempotent
    # on missing, so listing them unconditionally is fine.
    delete_branches = [
        f"foreman/issue-{issue_number}",
        f"foreman/impl-{issue_number}",
    ]
    labels_on_issue = git.get_issue_labels(
        project=project, issue_number=issue_number,
    )
    strip = {lbl for lbl in labels_on_issue if lbl.startswith("foreman:")}
    try:
        ticket = repo.get_ticket_by_issue(
            project=project, issue_number=issue_number,
        )
        delete_ticket_id = ticket.id
    except TicketNotFoundError:
        delete_ticket_id = None
    return ResetPlan(
        project=project,
        issue_number=issue_number,
        spec_pr=spec_pr,
        impl_pr=impl_pr,
        delete_branches=delete_branches,
        prune_worktrees=not keep_worktree,
        strip_labels=strip,
        delete_ticket_id=delete_ticket_id,
        apply_plan_label=retrigger,
    )


def _plan_steps(plan: ResetPlan) -> list[tuple[str, str]]:
    """Return ordered (label, kind) tuples for the plan's actionable steps.

    ``kind`` is a stable token the executor dispatches on. Steps that
    are off (no PR found, no row, --keep-* set) are filtered here so the
    renderer + executor walk the same list.
    """
    steps: list[tuple[str, str]] = []
    if plan.spec_pr is not None:
        steps.append((f"Close PR #{plan.spec_pr} (spec)", "close_spec_pr"))
    if plan.impl_pr is not None:
        steps.append((f"Close PR #{plan.impl_pr} (impl)", "close_impl_pr"))
    for branch in plan.delete_branches:
        steps.append((f"Delete remote branch {branch}", f"delete_branch:{branch}"))
    if plan.prune_worktrees:
        steps.append((
            f"Prune local worktree {plan.project}/issue-{plan.issue_number}/",
            "prune_worktrees",
        ))
    if plan.strip_labels:
        steps.append((
            f"Strip {len(plan.strip_labels)} foreman:* labels "
            f"({', '.join(sorted(plan.strip_labels))})",
            "strip_labels",
        ))
    if plan.delete_ticket_id is not None:
        steps.append((
            f"Delete ticket row id={plan.delete_ticket_id} from the database",
            "delete_ticket",
        ))
    if plan.apply_plan_label:
        steps.append(("Apply foreman:plan label", "apply_plan_label"))
    return steps


def _render_plan(plan: ResetPlan, steps: list[tuple[str, str]]) -> str:
    """Format the discovery plan for the operator. Pure string-builder."""
    if not steps:
        return f"Nothing to do for {plan.project}#{plan.issue_number}.\n"
    lines = [f"Resetting {plan.project}#{plan.issue_number}:", ""]
    for n, (label, _) in enumerate(steps, 1):
        lines.append(f"  {n}. {label}")
    lines.append("")
    return "\n".join(lines)


def _execute(
    plan: ResetPlan,
    steps: list[tuple[str, str]],
    *,
    git: GitProvider,
    repo: TicketRepository,
    wt: WorktreeManager,
    clone_path: Path,
) -> int:
    """Walk the plan, printing per-step status. Returns count of failures.

    ``clone_path`` is required by :meth:`WorktreeManager.prune` so
    ``git worktree remove`` consults the right ``.git/worktrees/``
    registry — without it, git either errors (cwd isn't a repo) or
    hits the wrong registry. Comes from
    :attr:`ProjectConfig.local_clone_path` in production; tests seed
    a tmp_path dir.
    """
    failures = 0
    total = len(steps)
    for n, (label, kind) in enumerate(steps, 1):
        prefix = f"  [{n}/{total}] {label}"
        try:
            if kind == "close_spec_pr":
                assert plan.spec_pr is not None
                git.close_pr(project=plan.project, pr_number=plan.spec_pr)
            elif kind == "close_impl_pr":
                assert plan.impl_pr is not None
                git.close_pr(project=plan.project, pr_number=plan.impl_pr)
            elif kind.startswith("delete_branch:"):
                branch = kind.split(":", 1)[1]
                git.delete_branch(project=plan.project, branch_name=branch)
            elif kind == "prune_worktrees":
                wt.prune(
                    project=plan.project,
                    issue_number=plan.issue_number,
                    clone_path=clone_path,
                )
            elif kind == "strip_labels":
                git.remove_labels(
                    project=plan.project,
                    issue_number=plan.issue_number,
                    labels=plan.strip_labels,
                )
            elif kind == "delete_ticket":
                assert plan.delete_ticket_id is not None
                repo.delete_ticket(plan.delete_ticket_id)
            elif kind == "apply_plan_label":
                git.add_labels(
                    project=plan.project,
                    issue_number=plan.issue_number,
                    labels={"foreman:plan"},
                )
            else:
                raise AssertionError(f"unknown step kind: {kind}")
            typer.echo(f"{prefix} ... ok")
        except Exception as exc:
            # Operator-facing tool: every step's failure must be
            # visible without aborting the rest.
            failures += 1
            typer.echo(f"{prefix} ... fail: {exc}")
    return failures


def _default_worktrees_root() -> Path:
    """Resolve the default worktrees root at call-time (avoids B008)."""
    return Path.home() / ".foreman" / "worktrees"


def cmd_reset(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project"),
    issue_number: int = typer.Option(..., "--issue-number", min=1),
    keep_pr: bool = typer.Option(
        False, "--keep-pr", help="Don't close open spec/impl PRs.",
    ),
    keep_worktree: bool = typer.Option(
        False, "--keep-worktree", help="Don't rmtree local worktrees.",
    ),
    no_retrigger: bool = typer.Option(
        False, "--no-retrigger", help="Don't re-apply foreman:plan at end.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print plan, exit. No prompt, no execution.",
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Skip the interactive confirmation.",
    ),
    worktrees_root: str | None = typer.Option(
        None,
        "--worktrees-root",
        help="Override worktree root path (test seam + alt-install support).",
    ),
) -> None:
    """Fully reset a foreman ticket: labels + branches + PRs + worktrees + row."""
    wt_root_path = (
        Path(worktrees_root) if worktrees_root else _default_worktrees_root()
    )
    repo = ctx.obj.repo
    git = ctx.obj.git
    config = ctx.obj.config
    if git is None:
        typer.echo("reset requires a GitProvider in the CLI context", err=True)
        raise typer.Exit(code=1)
    if config is None:
        typer.echo("reset requires V4Config", err=True)
        raise typer.Exit(code=1)
    project_config = next(
        (p for p in config.projects if p.name == project), None,
    )
    if project_config is None:
        typer.echo(
            f"unknown project: {project!r}. "
            f"Configured: {[p.name for p in config.projects]}",
            err=True,
        )
        raise typer.Exit(code=1)
    # ProjectConfig.local_clone_path is a str (TOML-loaded); coerce to
    # Path so WorktreeManager.prune sees the type it expects.
    clone_path = Path(project_config.local_clone_path)
    wt = WorktreeManager(worktrees_root=wt_root_path)
    plan = _discover(
        git=git, repo=repo,
        project=project, issue_number=issue_number,
        keep_pr=keep_pr, keep_worktree=keep_worktree,
        retrigger=not no_retrigger,
    )
    steps = _plan_steps(plan)
    typer.echo(_render_plan(plan, steps))
    if dry_run:
        return
    if not yes and steps:
        typer.confirm("Proceed?", abort=True)
    failures = _execute(
        plan, steps, git=git, repo=repo, wt=wt, clone_path=clone_path,
    )
    total = len(steps)
    if failures:
        typer.echo(
            f"\ncompleted {total - failures}/{total} steps; {failures} failed",
        )
        raise typer.Exit(code=1)
    typer.echo(
        f"\nDone. {project}#{issue_number} reset. "
        f"Daemon will pick up on next poll.",
    )


def _resolve(ctx: typer.Context, ticket_id: int) -> tuple[TicketRepository, TicketRecord]:
    repo = ctx.obj.repo
    try:
        ticket = repo.get_ticket(ticket_id)
    except TicketNotFoundError as exc:
        typer.echo(f"ticket {ticket_id} not found", err=True)
        raise typer.Exit(code=1) from exc
    return repo, ticket


def cmd_hold(
    ctx: typer.Context,
    ticket_id: int = typer.Argument(...),
    reason: str = typer.Option(..., "--reason"),
    by: str | None = typer.Option(None, "--by", help="Operator name (defaults to $USER)"),
) -> None:
    """Mark a ticket held, recording who held it and why.

    Held tickets are excluded from the daemon's normal processing until
    an operator runs ``resume``; ``--reason`` is stored on the ticket
    row for later audit.
    """
    repo, _ = _resolve(ctx, ticket_id)
    repo.hold_ticket(
        ticket_id,
        held_by=by or os.environ.get("USER", "operator"),
        reason=reason,
        now=dt.datetime.now(dt.UTC),
    )
    typer.echo(f"ticket {ticket_id} held")


def cmd_resume(
    ctx: typer.Context,
    ticket_id: int = typer.Argument(...),
) -> None:
    """Clear a ticket's held status so the daemon resumes normal processing.

    Undoes ``hold``; does not otherwise change the ticket's state or
    re-enqueue any work.
    """
    repo, _ = _resolve(ctx, ticket_id)
    repo.resume_ticket(ticket_id, now=dt.datetime.now(dt.UTC))
    typer.echo(f"ticket {ticket_id} resumed")


def _resolve_resume_state(
    repo: TicketRepository, ticket_id: int
) -> str | None:
    """Return the role-state a terminal ticket should resume into.

    foreman#414: a ticket parked in ``NeedsHelp``/``Failed`` carries no
    actionable work in its *current* state. The state that actually
    escalated is the most recent non-terminal ``state_instances`` row —
    walk the trail newest-first and return the first one that isn't
    terminal. ``None`` when the ticket never ran a non-terminal state
    (caller refuses rather than enqueuing a no-op).
    """
    for instance in reversed(repo.list_state_instances_for_ticket(ticket_id)):
        if instance.state_name not in _TERMINAL_STATE_NAMES:
            return instance.state_name
    return None


def cmd_retry(
    ctx: typer.Context,
    ticket_id: int = typer.Argument(...),
) -> None:
    """Re-enqueue a stuck ticket, resolving it out of a terminal state first.

    If the ticket sits in a retryable terminal (``NeedsHelp``/``Failed``),
    walks its state-instance history to find the role-state that
    escalated and moves it back there before enqueuing — retrying a
    terminal in place would be a no-op since terminals don't dispatch a
    role. Also clears any active ``next_action_at`` provider-error
    suspension so the retry isn't silently deferred. Refuses (exit 1)
    for a happy terminal (``Done``) or a terminal with no prior
    role-dispatch state to resume into.
    """
    repo, ticket = _resolve(ctx, ticket_id)
    qm = ctx.obj.qm
    if qm is None:
        typer.echo("retry requires a queue manager", err=True)
        raise typer.Exit(code=1)

    # foreman#414: a terminal ticket (NeedsHelp/Failed) re-enqueued in its
    # *current* state is a no-op — terminals don't dispatch a role. Resolve
    # the role-state that escalated and move the ticket back there first so
    # the requeued WorkItem actually re-runs the failed role.
    current = ticket.current_state
    resume_state = current
    resumed_from: str | None = None
    if current in _TERMINAL_STATE_NAMES:
        if current not in _RETRYABLE_TERMINALS:
            typer.echo(
                f"ticket {ticket_id} is in terminal {current}; nothing to retry",
                err=True,
            )
            raise typer.Exit(code=1)
        resolved = _resolve_resume_state(repo, ticket_id)
        if resolved is None:
            typer.echo(
                f"ticket {ticket_id} is in {current} with no prior role-dispatch "
                f"state to resume; use 'foreman set-state {ticket_id} <State>' "
                f"then retry",
                err=True,
            )
            raise typer.Exit(code=1)
        resume_state = resolved
        repo.set_ticket_state(ticket_id, resume_state, now=dt.datetime.now(dt.UTC))
        resumed_from = current

    # foreman#361: an operator-forced retry MUST bypass any active
    # transient-provider-error suspension. Without this clear, the
    # Poller would skip the enqueue + the requeued WorkItem until
    # next_action_at, which defeats the point of ``foreman retry``.
    cleared_suspension = ticket.next_action_at is not None
    if cleared_suspension:
        repo.clear_next_action_at(ticket_id)
    qm.enqueue(WorkItem(ticket_id=ticket_id, state_name=resume_state, project=ticket.project))
    suspension_note = " (cleared next_action_at)" if cleared_suspension else ""
    if resumed_from is not None:
        typer.echo(
            f"ticket {ticket_id} resumed {resumed_from} -> {resume_state} "
            f"and re-enqueued{suspension_note}"
        )
    else:
        typer.echo(
            f"ticket {ticket_id} re-enqueued in {resume_state}{suspension_note}"
        )


def cmd_set_state(
    ctx: typer.Context,
    ticket_id: int = typer.Argument(...),
    state: str = typer.Argument(...),
) -> None:
    """Force a ticket directly to an arbitrary registered state.

    An operator escape hatch for correcting a ticket that's stuck or
    was moved incorrectly — bypasses the normal state-machine
    transition rules entirely. Refuses (exit 1) if ``state`` isn't in
    :data:`~foreman.v4.states.registry.STATE_REGISTRY`.
    """
    repo, ticket = _resolve(ctx, ticket_id)
    if state not in STATE_REGISTRY:
        typer.echo(f"unknown state: {state}", err=True)
        raise typer.Exit(code=1)
    repo.set_ticket_state(ticket_id, state, now=dt.datetime.now(dt.UTC))
    typer.echo(f"ticket {ticket_id} moved {ticket.current_state} -> {state}")


def cmd_drop(
    ctx: typer.Context,
    ticket_id: int = typer.Argument(...),
) -> None:
    """Force a ticket straight to the ``Failed`` terminal state.

    An operator giving up on a ticket rather than letting it keep
    retrying/escalating; ``Failed`` is one of the terminals ``retry``
    can later resume out of, so this isn't destructive.
    """
    repo, _ = _resolve(ctx, ticket_id)
    repo.set_ticket_state(ticket_id, "Failed", now=dt.datetime.now(dt.UTC))
    typer.echo(f"ticket {ticket_id} dropped (-> Failed)")


def cmd_skip(
    ctx: typer.Context,
    ticket_id: int = typer.Argument(...),
    next_state: str = typer.Argument(...),
) -> None:
    """Jump a ticket directly to ``next_state``, bypassing intermediate transitions.

    Unlike ``set-state`` this is framed as forward progress (skipping
    steps an operator has already handled out-of-band) rather than an
    arbitrary correction, but the mechanics are identical: refuses
    (exit 1) if ``next_state`` isn't registered.
    """
    repo, _ = _resolve(ctx, ticket_id)
    if next_state not in STATE_REGISTRY:
        typer.echo(f"unknown state: {next_state}", err=True)
        raise typer.Exit(code=1)
    repo.set_ticket_state(ticket_id, next_state, now=dt.datetime.now(dt.UTC))
    typer.echo(f"ticket {ticket_id} skipped to {next_state}")


def cmd_enqueue(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project name from V4Config"),
    issue_number: int = typer.Option(
        ..., "--issue-number", min=1, help="GitHub issue number",
    ),
) -> None:
    """Insert a ticket directly into Postgres at state ``Queued``.

    Bypasses the Poller's GitHub label scan. Useful for dogfood +
    recovery scenarios where round-tripping through ``gh issue edit``
    + waiting for the next Poller tick is friction. The next worker
    poll picks the row up like any other Queued ticket.

    No GitHub API calls are made; this is a pure Postgres mutation.
    """
    repo = ctx.obj.repo
    config = ctx.obj.config

    # Unknown-project check has to happen before the create call —
    # without a V4Config we can't validate, so refuse rather than
    # silently allowing typos.
    if config is None:
        typer.echo(
            "enqueue requires a V4Config (cannot validate --project)",
            err=True,
        )
        raise typer.Exit(code=1)

    known = [p.name for p in config.projects]
    if project not in known:
        typer.echo(
            f"unknown project: {project!r}. "
            f"Configured projects: {known}",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        ticket = repo.create_ticket(
            project=project,
            issue_number=issue_number,
            now=dt.datetime.now(dt.UTC),
        )
    except TicketAlreadyExistsError:
        existing = repo.get_ticket_by_issue(
            project=project, issue_number=issue_number,
        )
        typer.echo(
            f"ticket already exists for {project}#{issue_number}: "
            f"id={existing.id} state={existing.current_state}",
            err=True,
        )
        raise typer.Exit(code=1) from None

    typer.echo(str(ticket.id))
