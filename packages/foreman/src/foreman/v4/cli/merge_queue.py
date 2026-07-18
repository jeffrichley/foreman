"""merge-queue — inspect each repo's merge_queue (foreman#550, observability).

Read-only: the per-repo :class:`~foreman.v4.merge_coordinator.MergeCoordinator`
(Tasks 1-5) is the only writer of ``merge_queue`` rows. This command answers
the operator question "what's queued, and why is the active merge stuck" in
one shot, without a live GitHub round-trip — the CLI context carries no
GitProvider, so the "why waiting" detail is rendered from the entry's
persisted fields only (``status`` + ``attempts``), not from a live
``required_check_state`` call.
"""

from __future__ import annotations

import typer

from foreman.v4.cli.formatters import get_formatter
from foreman.v4.merge_coordinator import MergeCoordinator
from foreman.v4.repository import MergeQueueEntry


def _detail(entry: MergeQueueEntry) -> str:
    """Render the "why waiting" column for one entry from persisted fields only.

    ``merging`` is the one active entry per project — it shows its attempt
    count against ``MergeCoordinator.MAX_ATTEMPTS``. ``queued`` entries have
    nothing to report yet, so the column is blank.
    """
    if entry.status == "merging":
        return f"merging · attempt {entry.attempts}/{MergeCoordinator.MAX_ATTEMPTS}"
    return ""


def _row(pos: int, entry: MergeQueueEntry) -> dict[str, object]:
    """Shape one merge_queue entry into the command's row dict."""
    return {
        "pos": pos,
        "project": entry.project,
        "ticket": entry.ticket_id,
        "pr": entry.pr_number,
        "kind": entry.kind,
        "status": entry.status,
        "attempts": f"{entry.attempts}/{MergeCoordinator.MAX_ATTEMPTS}",
        "detail": _detail(entry),
    }


def cmd_merge_queue(
    ctx: typer.Context,
    project: str = typer.Option(
        None,
        "--project",
        help="Limit to one project's merge queue (default: every project)",
    ),
    format: str = typer.Option(
        "table",
        "--format",
        help="table | json | yaml",
    ),
) -> None:
    """Show each project's merge queue, FIFO-ordered, with the active entry's status."""
    repo = ctx.obj.repo
    projects = (
        [project] if project is not None else sorted({t.project for t in repo.list_all_tickets()})
    )
    rows: list[dict[str, object]] = []
    for proj in projects:
        entries = repo.merge_queue_for_project(proj)
        rows.extend(_row(pos, entry) for pos, entry in enumerate(entries, start=1))
    typer.echo(get_formatter(format).format(rows), nl=False)
