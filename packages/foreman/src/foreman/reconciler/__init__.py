"""Foreman v3 declarative reconciler.

GitHub is the source of truth for ticket + PR state. The execution log is
the daemon's only cross-poll memory. See:
docs/superpowers/specs/foreman-issue-106-spec.md
"""

from foreman.reconciler.exec_log import ExecutionLog

__all__ = ["ExecutionLog"]
