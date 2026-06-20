"""Per-role dispatch modules. Each role: build context, run agent, parse output, act.

This package also exposes the shared defensive-exception helper used by all
four role runners — see :func:`handle_unhandled_role_exception`.

It also exposes :func:`build_role_resources` — the v4-native identity-
resource helper that mints a fresh installation token via
:class:`foreman.v4.identity.V4IdentityRegistry`, fetches the bot's
App metadata (still needed for v3-era commit attribution + push URL
construction in :class:`foreman.git_hosts.github.GitHubProvider`), and
returns the ``(host, token, client)`` trio the role bodies thread
through worktree creation + GitHub side effects.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

from github import Auth, Github

from foreman.auth import fetch_app_metadata
from foreman.git_host import BotIdentity, GitHostProvider
from foreman.git_hosts.github import GitHubProvider
from foreman.providers import ProviderTransientError
from foreman.v4.emit import emit_outcome
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind

# foreman#229 (v3): the runaway-burn defense in this helper used to
# transition the in-flight ticket to ``foreman:needs-help`` so the
# dispatcher's next poll wouldn't re-dispatch the same role on the same
# ticket. Under v4 the role subprocess dies on an unhandled exception,
# the :class:`SubprocessRoleDispatcher` reports failure, the state
# machine transitions to ``NeedsHelp``, and
# :class:`LabelObservabilityObserver` writes the v4-namespaced
# ``foreman:state-needs-help`` label. The role-side write was dropped
# in Phase 8d.7 — the helper now only posts a diagnostic comment so the
# operator sees the traceback on the issue.

# Cap the traceback shown in the GitHub comment so a runaway provider that
# fills its own stack with megabytes of context doesn't post a multi-MB
# comment (GitHub's hard limit is 65k chars; we want to stay well under and
# leave room for the prose preamble). 4000 chars OR 50 lines, whichever is
# smaller — same shape the spec specified.
_TRACEBACK_CHAR_LIMIT = 4000
_TRACEBACK_LINE_LIMIT = 50

# Truncation marker copy. Names the daemon's per-dispatch subprocess log
# (foreman#119: ``<log_dir>/<role>/<issue>__<iso-timestamp>.log``) so an
# operator reading the truncated comment knows exactly where to find the
# full traceback. The path itself is configurable via FOREMAN_LOG_DIR
# (compose sets ``/foreman/logs``; host default is ``~/.foreman/logs``),
# so the marker names the convention rather than a hard-coded path.
_TRUNCATION_MARKER = (
    "... (truncated; full traceback in the foreman daemon's per-dispatch "
    "subprocess log — `<log_dir>/<role>/<issue>__<timestamp>.log`, where "
    "`<log_dir>` is `$FOREMAN_LOG_DIR` or `~/.foreman/logs` by default)"
)


def _format_traceback(exc: BaseException) -> str:
    """Return a truncated traceback string for inclusion in the comment body.

    Truncated to at most :data:`_TRACEBACK_CHAR_LIMIT` characters OR
    :data:`_TRACEBACK_LINE_LIMIT` lines, whichever comes first. The
    truncation is signalled with a ``... (truncated)`` marker so a human
    reading the comment knows the tail was elided.
    """
    raw = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    # Line-based truncation first (preserves frame boundaries).
    lines = raw.splitlines()
    if len(lines) > _TRACEBACK_LINE_LIMIT:
        lines = lines[:_TRACEBACK_LINE_LIMIT] + [_TRUNCATION_MARKER]
    raw = "\n".join(lines)
    # Char-based truncation second (catches the long-single-frame case).
    if len(raw) > _TRACEBACK_CHAR_LIMIT:
        raw = raw[:_TRACEBACK_CHAR_LIMIT] + f"\n{_TRUNCATION_MARKER}"
    return raw


# Cap the inline ``**Exception:** type: msg`` line. The full message is
# in the traceback block below; the headline only needs to be readable on
# the issue's preview pane (a single GitHub list row).
_INLINE_MSG_LIMIT = 200


def _truncate_inline(msg: str, limit: int = _INLINE_MSG_LIMIT) -> str:
    if len(msg) <= limit:
        return msg
    # Short form for the inline ``**Exception:**`` line — keeps the
    # headline scannable. The full marker (with log path) lives on the
    # truncated traceback fence below.
    return msg[:limit] + "... (truncated)"


def build_exception_comment(role: str, exc: BaseException) -> str:
    """Compose the markdown comment body posted on the ticket.

    The body names the role that crashed, the exception type + (truncated)
    message, and the truncated traceback inside a fenced code block.
    Operators reading the issue see exactly what blew up without spelunking
    daemon logs. The inline message is capped at ~200 chars so a runaway
    provider that built a 10MB exception string doesn't post a 10MB GitHub
    comment; the full content (also truncated) is available in the
    traceback fence below.
    """
    tb = _format_traceback(exc)
    inline_msg = _truncate_inline(str(exc))
    return (
        f"foreman {role} runner crashed with an unhandled exception. "
        "v4 will transition the ticket to `NeedsHelp` once the role "
        "subprocess reports failure; the `foreman:state-needs-help` "
        "label will appear momentarily.\n\n"
        f"**Exception:** `{type(exc).__name__}: {inline_msg}`\n\n"
        "<details>\n"
        "<summary>Traceback</summary>\n\n"
        f"```\n{tb}\n```\n\n"
        "</details>"
    )


def handle_unhandled_role_exception(
    *,
    role: str,
    issue_number: int,
    exc: BaseException,
    post_comment: Callable[[str], Any],
) -> None:
    """Surface an unhandled role-runner exception to the ticket.

    Posts a diagnostic comment carrying the exception type + message +
    truncated traceback so the operator can read the crash on the issue
    page without spelunking daemon logs.

    Under v4, the role-side label transition that lived here in v3 has
    been dropped (Phase 8d.7): when the role subprocess dies the
    :class:`SubprocessRoleDispatcher` reports failure, the state machine
    transitions to ``NeedsHelp``, and
    :class:`LabelObservabilityObserver` writes
    ``foreman:state-needs-help``. The role-side write was the wrong
    namespace (literal ``foreman:needs-help``, no ``state-`` prefix) and
    redundant with the observer's correct one.

    Comment-post failures are swallowed silently — the original
    exception still propagates via the caller's bare ``raise``, the
    subprocess still dies, and v4's NeedsHelp transition still happens.
    The comment is diagnostic; losing it doesn't lose the defense.

    Args:
        role: ``"planner"`` / ``"reviewer"`` / ``"worker"`` / ``"fixer"``,
            used only in the human-facing comment prose.
        issue_number: Retained on the signature for diagnostic
            symmetry with the closure callbacks the role wires; not
            consumed by the body today.
        exc: The exception that escaped the role's body. The caller is
            expected to ``raise`` after this helper returns so the
            dispatcher's error handling still sees it.
        post_comment: Closure that posts ``body`` as a GitHub comment on
            the originating ticket. Each role wires its own (the four
            roles use different PyGithub / GitHostProvider surfaces).
    """
    del issue_number  # retained on signature for caller-side symmetry
    body = build_exception_comment(role=role, exc=exc)
    try:
        post_comment(body)
    except Exception:
        # Best-effort: a GitHub 5xx during the comment post must not
        # mask the original exception that triggered this helper.
        pass


def emit_transient_provider_outcome(exc: ProviderTransientError) -> int:
    """Emit the ``FOREMAN_OUTCOME`` for a transient provider failure.

    Shared by every role CLI's ``except ProviderTransientError`` arm
    (foreman#361) so the per-role bodies stay one line each and the
    ``details`` shape has exactly one definition. Returns exit code
    ``0`` deliberately — a non-zero exit would trip
    :class:`SubprocessRoleDispatcher`'s
    ``RoleSubprocessError`` and erase the
    ``TRANSIENT_PROVIDER_ERROR`` discriminator that
    :class:`RoleDispatchState` needs to schedule the
    exponential-backoff retry.

    ``exception_class`` walks ``exc.__cause__`` first so the
    classification in
    :func:`foreman.providers.anthropic_sdk._is_transient_sdk_error`
    surfaces the underlying SDK / transport exception type (e.g.
    ``RateLimitError``) rather than the wrapping
    ``ProviderTransientError`` itself — operators reading the
    structured log learn what actually went wrong.
    """
    cause = exc.__cause__
    exception_class = type(cause).__name__ if cause is not None else type(exc).__name__
    emit_outcome(
        Outcome(
            kind=OutcomeKind.TRANSIENT_PROVIDER_ERROR,
            confidence=OutcomeConfidence.HIGH,
            summary=f"provider transient failure: {exc}"[:500],
            details={
                "provider_status": str(exc),
                "exception_class": exception_class,
            },
        )
    )
    return 0


def build_role_resources(
    *,
    registry: Any,
    role: str,
    app_id: int,
    private_key_path: str,
) -> tuple[GitHostProvider, str, Github]:
    """Build the trio of role-side identity resources from a v4 registry.

    Returns ``(host, token, client)``:

    * ``host`` — v3-shape :class:`GitHostProvider` used by the role's
      commit/push/PR/comment side effects. The :class:`BotIdentity`
      carries the bot's slug + numeric id (still required for v3-era
      commit attribution via ``GIT_AUTHOR_*`` env vars and for the
      tokenized HTTPS push URL inside :class:`GitHubProvider`); these
      come from :func:`fetch_app_metadata` against the App credentials
      threaded in by the caller from ``V4Config.apps.<role>``.
    * ``token`` — the role's current installation-token string, used by
      :class:`~foreman.worktree.WorktreeManager` for ``GH_TOKEN`` env
      injection on its git subprocesses.
    * ``client`` — a fresh :class:`Github` client authenticated with the
      same token, used by the role for any direct PyGithub calls
      outside the ``host`` surface.

    ``registry`` is typed ``Any`` so tests can inject any duck-typed
    object that satisfies the production contract. Real callers pass a
    :class:`V4IdentityRegistry`.

    Production contract: ``registry.get_role_token(role) -> str``. The
    helper mints a fresh :class:`Github` client and uses
    :func:`fetch_app_metadata` (HTTP fetch to GitHub) to build the
    :class:`BotIdentity`.

    ``app_id`` + ``private_key_path`` come from
    ``V4Config.apps.<role>`` at the call site — v4's per-role App
    credentials are top-level (not nested under per-project blocks the
    way v3 stored them), so the role function reads them off the
    V4Config and threads them in here rather than this helper rooting
    through a project shape.

    Test-fake discipline (Wren's standing feedback on fakes mirroring
    real APIs strictly): tests should
    :func:`unittest.mock.patch.object` this helper or the per-role
    re-export to return a pre-baked ``(host, token, client)`` trio,
    rather than rely on ``MagicMock`` attribute auto-creation —
    auto-created attributes return ``MagicMock`` instances which would
    propagate into ``Github(token)`` where the real registry would
    have given a string.
    """
    token = registry.get_role_token(role)
    client = Github(auth=Auth.Token(token))
    meta = fetch_app_metadata(app_id, private_key_path)
    identity = BotIdentity(slug=meta.slug, user_id=meta.app_id, token=token)
    host = GitHubProvider(identity=identity, client=client)
    return host, token, client
