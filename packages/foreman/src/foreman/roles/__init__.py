"""Per-role dispatch modules. Each role: build context, run agent, parse output, act.

This package also exposes the shared defensive-exception helper used by all
four role runners — see :func:`handle_unhandled_role_exception`.

It also exposes the v4 identity-resource helpers (:func:`build_v4_registry_from_v3_project`
and :func:`build_role_resources`) that let the legacy ``run_<role>`` async
bodies stay structurally identical while sourcing their token + host
provider from :class:`foreman.v4.identity.V4IdentityRegistry` instead of
v3's :class:`foreman.identity.IdentityRegistry`. Phase 8d.1 replaces the
v3 import; the v3-registry-shaped surface (``get_<role>_client`` /
``get_<role>_token`` / ``get_host_provider``) is built inline in this
package from the v4 token + ``fetch_app_metadata`` for the bot's
slug/user-id (still needed for v3-era commit attribution + push URL
construction in :class:`foreman.git_hosts.github.GitHubProvider`).
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from typing import Any

from github import Auth, Github

from foreman.auth import fetch_app_metadata
from foreman.config import ProjectConfig
from foreman.git_host import BotIdentity, GitHostProvider
from foreman.git_hosts.github import GitHubProvider
from foreman.v4.config import AppCredentials, AppsConfig, OrchestratorConfig
from foreman.v4.identity import V4IdentityRegistry

_log = logging.getLogger(__name__)

# foreman#229: runaway-burn defense — when an unhandled exception escapes a
# role runner's body, the in-flight ticket label was NOT being transitioned.
# The dispatcher's next poll then re-dispatched the SAME role on the SAME
# ticket, producing a runaway (foreman#227: 171 dispatches in 2h52m). This
# helper is the uniform "post traceback + transition to terminal blocking
# label" surface every role runner calls from its outermost ``except``.
TERMINAL_BLOCKING_LABEL = "foreman:needs-help"

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
        f"The ticket has been transitioned to `{TERMINAL_BLOCKING_LABEL}` to "
        "stop the daemon from re-dispatching on every poll (foreman#227, "
        "foreman#229). Remove the label to resume the autonomous flow once "
        "the underlying cause is addressed.\n\n"
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
    set_needs_help_label: Callable[[], Any],
) -> None:
    """Surface an unhandled role-runner exception to the ticket.

    Posts a comment carrying the exception type + message + truncated
    traceback, then transitions the originating ticket to
    :data:`TERMINAL_BLOCKING_LABEL` so the daemon stops re-dispatching
    on every poll (the runaway-burn pattern foreman#227 surfaced and
    foreman#229 fixes).

    Both side effects are wrapped in their own try/except so a GitHub
    5xx during the comment-post does NOT mask the failure to transition
    the label, and vice-versa. The original exception still propagates
    via the caller's bare ``raise`` — this helper never swallows.

    Args:
        role: ``"planner"`` / ``"reviewer"`` / ``"worker"`` / ``"fixer"``,
            used only in the human-facing comment prose.
        issue_number: For diagnostic logs when the helper's own writes
            fail. The label-transition callable already targets the
            right issue.
        exc: The exception that escaped the role's body. The caller is
            expected to ``raise`` after this helper returns so the
            dispatcher's error handling still sees it.
        post_comment: Closure that posts ``body`` as a GitHub comment on
            the originating ticket. Each role wires its own (the four
            roles use different PyGithub / GitHostProvider surfaces).
        set_needs_help_label: Closure that atomically transitions the
            originating ticket to :data:`TERMINAL_BLOCKING_LABEL`. Same
            rationale as ``post_comment``.
    """
    body = build_exception_comment(role=role, exc=exc)
    try:
        post_comment(body)
    except Exception:
        _log.exception(
            "foreman#229 %s exception-handler: post_comment failed for "
            "issue=%d; label transition will still be attempted",
            role,
            issue_number,
        )
    try:
        set_needs_help_label()
    except Exception:
        _log.exception(
            "foreman#229 %s exception-handler: set_needs_help_label "
            "failed for issue=%d; ticket will remain on the in-flight "
            "label and the daemon may re-dispatch",
            role,
            issue_number,
        )


# Sentinel orchestrator credentials used when constructing a
# :class:`V4IdentityRegistry` for a single per-role bot's needs. The
# role CLIs never resolve the ``"orchestrator"`` role through their
# registry (that's daemon-side concern), so the orchestrator slot stays
# unreachable. We point both fields at the planner's App so the
# Pydantic validators accept the construction without forcing every
# call site to thread a real orchestrator block down a code path that
# does not use it.
def build_v4_registry_from_v3_project(project: ProjectConfig) -> V4IdentityRegistry:
    """Build a :class:`V4IdentityRegistry` from a v3 :class:`ProjectConfig`.

    Used by the legacy ``run_<role>`` async functions as the default
    fallback when no ``identity_registry`` is injected. The v3
    ``ProjectConfig.apps`` block has resolvers per role (env-var or
    direct) that produce the same ``(app_id, private_key_path)`` tuple
    v4 :class:`AppCredentials` expects.

    The orchestrator slot is filled with the planner's credentials as a
    sentinel — role CLIs never resolve the ``orchestrator`` role through
    this registry. Phase 9 will delete the legacy entry points that use
    this helper alongside the v3 ``Config`` shape.
    """
    apps_block = project.apps
    planner_creds = AppCredentials(
        app_id=apps_block.resolve_planner_app_id(),
        private_key_path=str(apps_block.resolve_planner_private_key_path()),
    )
    reviewer_creds = AppCredentials(
        app_id=apps_block.resolve_reviewer_app_id(),
        private_key_path=str(apps_block.resolve_reviewer_private_key_path()),
    )
    fixer_creds = AppCredentials(
        app_id=apps_block.resolve_fixer_app_id(),
        private_key_path=str(apps_block.resolve_fixer_private_key_path()),
    )
    worker_creds = AppCredentials(
        app_id=apps_block.resolve_worker_app_id(),
        private_key_path=str(apps_block.resolve_worker_private_key_path()),
    )
    v4_apps = AppsConfig(
        planner=planner_creds,
        reviewer=reviewer_creds,
        fixer=fixer_creds,
        worker=worker_creds,
    )
    # Sentinel orchestrator — role CLIs never resolve the orchestrator
    # role, so reusing the planner's creds keeps the v4 registry's
    # Pydantic constructor happy without dragging a real orchestrator
    # block through the call site.
    sentinel_orchestrator = OrchestratorConfig(
        app_id=planner_creds.app_id,
        private_key_path=planner_creds.private_key_path,
    )
    return V4IdentityRegistry(
        apps=v4_apps,
        orchestrator=sentinel_orchestrator,
        installation_repo=project.repo,
    )


def build_role_resources(
    *,
    registry: Any,
    project: ProjectConfig,
    role: str,
) -> tuple[GitHostProvider, str, Github]:
    """Build the trio of role-side identity resources from a v4 registry.

    Returns ``(host, token, client)``:

    * ``host`` — v3-shape :class:`GitHostProvider` used by the role's
      commit/push/PR/comment side effects. The :class:`BotIdentity`
      carries the bot's slug + numeric id (still required for v3-era
      commit attribution via ``GIT_AUTHOR_*`` env vars and for the
      tokenized HTTPS push URL inside :class:`GitHubProvider`); these
      come from :func:`fetch_app_metadata` against the v3
      ``ProjectConfig.apps`` resolver block.
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
    app_id, key_path = _resolve_role_app_credentials(project, role)
    meta = fetch_app_metadata(app_id, key_path)
    identity = BotIdentity(slug=meta.slug, user_id=meta.app_id, token=token)
    host = GitHubProvider(identity=identity, client=client)
    return host, token, client


def _resolve_role_app_credentials(
    project: ProjectConfig, role: str
) -> tuple[int, Any]:
    """Return ``(app_id, private_key_path)`` for the requested role.

    Reads the v3 ``ProjectConfig.apps`` resolvers — the source of truth
    for per-role App credentials in the legacy role code path. The
    return type's second slot is :class:`pathlib.Path` at runtime; typed
    ``Any`` here because :func:`fetch_app_metadata` accepts either a
    ``Path`` or ``str``.
    """
    apps = project.apps
    if role == "planner":
        return apps.resolve_planner_app_id(), apps.resolve_planner_private_key_path()
    if role == "reviewer":
        return apps.resolve_reviewer_app_id(), apps.resolve_reviewer_private_key_path()
    if role == "fixer":
        return apps.resolve_fixer_app_id(), apps.resolve_fixer_private_key_path()
    if role == "worker":
        return apps.resolve_worker_app_id(), apps.resolve_worker_private_key_path()
    raise ValueError(
        f"Unknown role: {role!r}. build_role_resources supports "
        "'planner', 'reviewer', 'fixer', 'worker'."
    )
