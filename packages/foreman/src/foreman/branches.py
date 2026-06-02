"""Single source of truth for Foreman branch-name conventions.

All Foreman roles and the daemon's merge runners construct branch names
from issue numbers in exactly two shapes:

- spec branch (Planner / Reviewer / Fixer share this worktree):
  ``foreman/issue-<N>``
- impl branch (Worker stacks its impl PR on the spec branch):
  ``foreman/impl-<N>``

Both helpers live here so the convention has one home and cannot drift
across sites. Issue #49 surfaced a real drift bug where the daemon's
``merge_impl_pr`` looked up the impl PR by ``foreman/issue-<N>-impl``
while the Worker actually opened it as ``foreman/impl-<N>`` — the
autonomous loop's merge step would have raised ``RuntimeError`` the
moment ``auto_merge_impl=True`` fired. Promoting these helpers to a
shared module makes that class of mismatch impossible.

These functions are intentionally minimal: pure ``int -> str``, no
caching, no side effects. The *value* — the literal branch name — is
the contract.
"""

from __future__ import annotations


def spec_branch(issue_number: int) -> str:
    """Return the spec PR's head branch name for ``issue_number``."""
    return f"foreman/issue-{issue_number}"


def impl_branch(issue_number: int) -> str:
    """Return the impl PR's head branch name for ``issue_number``."""
    return f"foreman/impl-{issue_number}"
