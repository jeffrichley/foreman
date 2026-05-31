"""Custom exceptions for the git_hosts subpackage.

:class:`GitCommandError` exists to fix two real defects surfaced during the
2026-05-31 dogfood smoke test:

* **Defect B (error masking).** ``subprocess.CalledProcessError`` swallows
  the actual git ``stderr`` content in its default ``str()``, so callers see
  ``"non-zero exit status 1"`` with no diagnostic. We preserve ``stderr``
  on the exception and surface it in the message.

* **Defect C (install token leak).** ``CalledProcessError.cmd`` carries the
  full argv verbatim, which for ``push_branch`` includes the
  ``https://x-access-token:<TOKEN>@github.com/...`` URL. Python's default
  traceback formatting prints ``cmd`` (and therefore the live token) into
  logs, issue trackers, and chat transcripts. We sanitize the argv at the
  exception boundary, redacting any string matching a GitHub token prefix,
  so leaked-in-argv tokens never make it into ``str(exc)``, ``repr(exc)``,
  or any exception attribute. Defense-in-depth: even if future callers
  re-introduce a token in argv, the leak is closed at this seam.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

# GitHub token prefixes per
# https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/about-authentication-to-github#githubs-token-formats
# - ghs_*        installation access token (the one push_branch embeds)
# - ghp_*        classic personal access token
# - gho_*        OAuth access token
# - ghu_*        user-to-server OAuth token
# - ghr_*        refresh token
# - github_pat_* fine-grained personal access token
#
# Tokens are alphanumeric + underscore. Match the prefix and everything
# alnum/underscore that follows; replace with [REDACTED]. We intentionally
# do not try to identify "which arg is the URL" — string-level redaction
# closes the leak regardless of how the token was passed.
_TOKEN_PATTERN = re.compile(r"(?:ghs|ghp|gho|ghu|ghr|github_pat)_[A-Za-z0-9_]+")

_REDACTED = "[REDACTED]"


def _redact(value: str) -> str:
    """Replace any GitHub token-shaped substring with ``[REDACTED]``."""
    return _TOKEN_PATTERN.sub(_REDACTED, value)


def sanitize_cmd(cmd: Sequence[str] | str) -> list[str]:
    """Return a copy of ``cmd`` with any GitHub-token substrings redacted.

    Accepts both the list-form argv (typical for ``subprocess.run([...])``)
    and the string-form (for ``shell=True`` callers, defensive).
    """
    if isinstance(cmd, str):
        return [_redact(cmd)]
    return [_redact(arg) for arg in cmd]


class GitCommandError(RuntimeError):
    """A ``git`` subprocess invocation failed.

    Attributes:
        returncode: Exit code from the failed git invocation.
        cmd: Sanitized argv (tokens redacted). Safe to log.
        stderr: Captured stderr from the failed invocation (token-sanitized).
            This is the actual git error message — the whole point of Defect B.
    """

    def __init__(
        self,
        message: str,
        *,
        returncode: int,
        cmd: Iterable[str],
        stderr: str,
    ) -> None:
        sanitized_cmd = sanitize_cmd(list(cmd))
        sanitized_stderr = _redact(stderr)
        sanitized_message = _redact(message)
        super().__init__(
            f"{sanitized_message} "
            f"(returncode={returncode}, cmd={sanitized_cmd!r}): {sanitized_stderr}"
        )
        self.returncode = returncode
        self.cmd: list[str] = sanitized_cmd
        self.stderr = sanitized_stderr


__all__ = ["GitCommandError", "sanitize_cmd"]
