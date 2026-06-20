"""Contributor-facing helpers — not operator mutations.

This sub-app groups commands a human contributor runs on their own
machine to make their PR pass project gates (DCO sign-off being the
first such gate). Distinct from the operator command surface
(``ps``, ``log``, ``hold``, ``reset``, ...) which manage the
daemon-owned ticket state machine.

Future contrib commands (``lint-trailers``,
``check-conventional-commit``, ...) will register here without
polluting the top-level ``foreman --help``.
"""

from __future__ import annotations

import typer

from foreman.v4.cli.contrib.sign_commits import (
    cmd_check_signoff,
    cmd_sign_commits,
)

contrib_app = typer.Typer(
    name="contrib",
    help="Contributor helpers (sign-commits, check-signoff)",
    no_args_is_help=True,
)

contrib_app.command("sign-commits")(cmd_sign_commits)
contrib_app.command("check-signoff")(cmd_check_signoff)


__all__ = ["contrib_app"]
