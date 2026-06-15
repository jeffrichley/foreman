"""CliContext — the one-and-only builder for per-invocation deps.

Production startup (Phase 7 daemon entry) calls build_cli_context()
with concretes. Tests call it with fakes. There is no other call site
that assembles these fields — adding a new dep means adding a field
here and updating both call sites once.

Why frozen + typed: ad-hoc dict construction (``obj={"repo": r}``) is
how drift sneaks in — a test forgets the new field, production forgets
the rename. Frozen dataclass makes the shape a single point of edit;
the type checker flags every site that hasn't migrated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from foreman.v4.git_provider import GitProvider
    from foreman.v4.queue_manager import QueueManager
    from foreman.v4.repository import TicketRepository
    from foreman.v4.role_dispatcher import RoleDispatcher

    # Phase 6.5 introduces foreman.v4.daemon.Daemon. Until then, keep the
    # field type-erased (Any) so the dataclass shape exists without
    # forward-referencing a module that hasn't landed yet.
    Daemon = Any


@dataclass(frozen=True, slots=True)
class CliContext:
    """Per-invocation context passed via typer's ctx.obj."""

    repo: TicketRepository
    qm: QueueManager | None = None
    daemon: Daemon | None = None
    git: GitProvider | None = None
    dispatcher: RoleDispatcher | None = None


def build_cli_context(
    *,
    repo: TicketRepository,
    qm: QueueManager | None = None,
    daemon: Daemon | None = None,
    git: GitProvider | None = None,
    dispatcher: RoleDispatcher | None = None,
) -> CliContext:
    """The single point of construction for CliContext.

    Do NOT instantiate CliContext directly. Do NOT pass raw dicts as
    ``obj=`` to runner.invoke / typer. Both paths route through here.
    """
    return CliContext(
        repo=repo, qm=qm, daemon=daemon, git=git, dispatcher=dispatcher,
    )
