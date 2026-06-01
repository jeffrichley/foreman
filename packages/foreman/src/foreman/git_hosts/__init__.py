"""Concrete :class:`~foreman.git_host.GitHostProvider` implementations.

Currently only :class:`~foreman.git_hosts.github.GitHubProvider`. A
``gitlab.GitLabProvider`` would live here too and plug in via the same
abstract interface.
"""

from __future__ import annotations

from foreman.git_hosts.github import GitHubProvider

__all__ = ["GitHubProvider"]
