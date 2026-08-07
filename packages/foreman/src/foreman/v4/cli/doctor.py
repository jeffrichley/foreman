"""``foreman doctor`` — health probes for the running foreman deployment.

Distinct from ``foreman daemon status`` (which answers "is the daemon
process running?" via a fast local PID-file check). ``doctor`` answers
"is the DEPLOYMENT healthy?" via a sequence of multi-source probes that
may require the network. Conflating the two would make ``daemon
status`` slow and force callers (incl. foreman#360's restore-time PID
check) to thread a fast-mode flag.

Current checks (one — image freshness). Adding a check is a single new
helper function called from :func:`cmd_doctor` in sequence. Future
candidates: certificate expiry, GHCR reachability, GitHub Apps token
expiry, disk-space headroom. Precedent: ``npm doctor`` / ``brew
doctor`` / ``nix doctor``.

Image-freshness check (foreman#363):

1. If ``IMAGE_SHA`` env var is unset or ``"unknown"`` — running outside
   the container; print SKIPPED and exit 0. The env var is stamped at
   build time by the Dockerfile (``ARG IMAGE_SHA``); only the daemon
   container has it set.
2. Otherwise, ``git ls-remote`` the Foreman repo's ``refs/heads/main``
   over plain HTTPS (no auth, no rate limit for public repos) and
   compare the leading ``len(IMAGE_SHA)`` chars of the remote SHA to
   the stamped ``IMAGE_SHA``.
3. Network failure → WARN + exit 0 (Watchtower may also be temporarily
   blocked; the daemon stays up; the operator sees the warning).
4. Match → OK + exit 0. Mismatch → STALE + exit 1.

Exit 1 ONLY on confirmed-stale; 0 on OK / SKIPPED / WARN. This makes
the exit code usable in ``&&``-chained scripts without false alarms
from transient network blips.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import typer
from github import Auth, Github, GithubException

from foreman.v4.config import ProjectConfig, V4Config

# Foreman's own source-of-truth repo — the one whose ``main`` the
# daemon image is built from. Hardcoded module-level constant (not
# pulled from config) because the configured projects are what the
# daemon MANAGES, not the source of the daemon image. Matches the
# convention for ``_DEFAULT_CONFIG`` in ``cli/__init__.py``.
FOREMAN_SOURCE_REPO = "jeffrichley/foreman"

# Default paths — mirrors the constants in foreman.v4.cli.init to avoid
# circular imports.
_DEFAULT_CONFIG = Path.home() / ".foreman" / "v4" / "config.toml"
_DEFAULT_PROJECTS_PATH = Path.home() / ".foreman" / "projects.toml"

# GitHub statuses that will not clear on a retry: the token lacks the
# scope, or the repo does not resolve. Both mean webhook coverage is
# unknown indefinitely, which must not be reported as healthy. Everything
# else (network, 5xx) is treated as transient — see the WARN branch in
# ``_check_webhook_coverage``.
_UNRECOVERABLE_STATUSES = frozenset({401, 403, 404})


def _check_image_fresh() -> int:
    """Compare the stamped IMAGE_SHA env var against ``origin/main``.

    Returns the exit code contribution for this check (0 on OK,
    SKIPPED, or WARN; 1 on confirmed-stale).
    """
    image_sha = os.environ.get("IMAGE_SHA", "")
    if not image_sha or image_sha == "unknown":
        typer.echo(
            "[doctor] image-fresh: SKIPPED — IMAGE_SHA not set "
            "(running outside the foreman container?)",
        )
        return 0

    remote_url = f"https://github.com/{FOREMAN_SOURCE_REPO}.git"
    try:
        proc = subprocess.run(
            ["git", "ls-remote", remote_url, "refs/heads/main"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # Timeout is a network-failure variant; classify as WARN so a
        # transient blip doesn't crash the daemon's monitoring chain.
        typer.echo(
            f"[doctor] image-fresh: WARN — cannot reach github.com to "
            f"compare image SHA vs. main HEAD (timeout after {exc.timeout}s)",
        )
        return 0
    except OSError as exc:
        # `git` binary missing (host-direct run on a system without
        # git on PATH) — surface as WARN, not stale.
        typer.echo(
            f"[doctor] image-fresh: WARN — cannot reach github.com to "
            f"compare image SHA vs. main HEAD (stderr: {exc})",
        )
        return 0

    if proc.returncode != 0:
        stderr_snippet = (proc.stderr or "").strip().splitlines()
        stderr_truncated = stderr_snippet[-1] if stderr_snippet else "(no stderr)"
        typer.echo(
            f"[doctor] image-fresh: WARN — cannot reach github.com to "
            f"compare image SHA vs. main HEAD (stderr: {stderr_truncated})",
        )
        return 0

    # Expected single-line output:
    #   <40-char-sha>\trefs/heads/main\n
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        typer.echo(
            "[doctor] image-fresh: WARN — cannot reach github.com to "
            "compare image SHA vs. main HEAD (stderr: empty stdout)",
        )
        return 0
    first = line[0]
    parts = first.split("\t")
    if len(parts) != 2 or parts[1] != "refs/heads/main" or len(parts[0]) < 7:
        typer.echo(
            f"[doctor] image-fresh: WARN — cannot reach github.com to "
            f"compare image SHA vs. main HEAD "
            f"(stderr: unexpected ls-remote output: {first!r})",
        )
        return 0

    remote_full = parts[0]
    remote_short = remote_full[: len(image_sha)]

    if remote_short.lower() == image_sha.lower():
        typer.echo(
            f"[doctor] image-fresh: OK — running sha-{image_sha} matches main",
        )
        return 0

    typer.echo(
        f"[doctor] image-fresh: STALE — running sha-{image_sha}, main is "
        f"at sha-{remote_short}. Watchtower normally closes this gap "
        f"within ~5 min of merge; rerun in a few minutes or force a "
        f"local rebuild with `just rebuild-daemon`.",
    )
    return 1


def _check_webhook_coverage(
    config: V4Config,
    projects: list[ProjectConfig],
    *,
    github_factory: Callable[[], Github] | None = None,
) -> int:
    """Check that every project in ``projects`` has an active GitHub webhook.

    Returns 0 on SKIPPED/OK/WARN; 1 on confirmed-missing hook.

    The ``github_factory`` is a zero-arg callable that returns a
    ``Github`` client pre-authenticated with an appropriate token.
    ``cmd_doctor`` always passes a closure that captures a pre-minted
    orchestrator token; tests pass their own fake factory. The parameter
    accepts ``None`` as a sentinel so callers can also omit it — the
    function returns SKIPPED immediately before using it when
    ``config.inbound`` is absent or ``receiver_url`` is empty.

    SKIPPED conditions (exit 0):
      - ``config.inbound is None`` or ``config.inbound.receiver_url`` is
        empty (webhook-coverage is unconfigured).
      - ``projects`` is empty (nothing to check).

    WARN condition (exit 0):
      - A transient ``GithubException`` (network error, 5xx) while
        fetching hooks. It clears on the next run.

    UNVERIFIABLE condition (exit 1):
      - A ``GithubException`` with a status in
        ``_UNRECOVERABLE_STATUSES`` (401/403 insufficient scope, 404 repo
        not found). Coverage cannot be determined and will stay that way
        until someone acts, so it must not read as healthy.

    MISSING condition (exit 1):
      - A project has no active hook whose ``config.url`` matches
        ``receiver_url``.

    OK condition (exit 0):
      - All projects have at least one active matching hook.
    """
    # SKIPPED: inbound block absent or receiver_url empty.
    if not config.inbound or not config.inbound.receiver_url:
        typer.echo(
            "[doctor] webhook-coverage: SKIPPED — [inbound].receiver_url not configured",
        )
        return 0

    # SKIPPED: no projects to check.
    if not projects:
        typer.echo(
            "[doctor] webhook-coverage: SKIPPED — no projects configured",
        )
        return 0

    receiver_url = config.inbound.receiver_url
    # github_factory is guaranteed non-None here (caller always provides
    # one when inbound is configured).
    assert github_factory is not None, "github_factory must be provided when inbound is set"
    gh = github_factory()

    exit_code = 0
    for pc in projects:
        try:
            repo = gh.get_repo(pc.repo)
            hooks = repo.get_hooks()
            has_match = any(h.active and h.config.get("url") == receiver_url for h in hooks)
        except GithubException as exc:
            if exc.status in _UNRECOVERABLE_STATUSES:
                # A permissions or not-found failure does not clear on its
                # own: coverage stays unknown for every future run. Exiting
                # 0 here would make "coverage confirmed" and "coverage
                # unknown forever" the same machine-readable signal — which
                # is the invisible-gap defect this check exists to close,
                # moved one layer up. Listing hooks needs repo-admin scope,
                # so this is the most likely real failure, not a corner case.
                typer.echo(
                    f"[doctor] webhook-coverage: UNVERIFIABLE — cannot check "
                    f"{pc.repo} ({exc.status} {exc.data})",
                )
                exit_code = 1
            else:
                # Transient (network, 5xx): retry next run, do not fail.
                typer.echo(
                    f"[doctor] webhook-coverage: WARN — could not fetch hooks for "
                    f"{pc.repo} ({exc.status} {exc.data})",
                )
            continue

        if has_match:
            typer.echo(
                f"[doctor] webhook-coverage: OK — {pc.name} ({pc.repo}) has active hook",
            )
        else:
            typer.echo(
                f"[doctor] webhook-coverage: MISSING — {pc.name} ({pc.repo}) "
                f"has no active webhook pointing at {receiver_url}",
            )
            exit_code = 1

    return exit_code


def cmd_doctor(ctx: typer.Context) -> None:
    """Run a sequence of deployment-health checks.

    Each check prints its own status line and contributes to the exit
    code. The command exits with code 1 if any check reported confirmed-
    bad state (e.g., STALE image) or a state it could not verify and
    never will without intervention (UNVERIFIABLE webhook coverage);
    0 otherwise (including SKIPPED and WARN — see module docstring).
    """
    # The ctx parameter is unused for this initial check, but every
    # future check that reads V4Config (e.g., GitHub Apps token expiry)
    # will need it. Keeping the signature stable now avoids a churn
    # cycle when the second check lands.
    del ctx  # explicit unused-arg marker

    overall_exit = 0
    overall_exit |= _check_image_fresh()

    # Webhook-coverage check (issue #590): load config + projects, mint
    # orchestrator token, and check each project for an active hook.
    config_path = Path(os.environ.get("FOREMAN_V4_CONFIG", str(_DEFAULT_CONFIG)))
    config: V4Config | None = None
    if config_path.exists():
        try:
            from foreman.v4.config import load_config

            config = load_config(config_path)
        except Exception:
            typer.echo(
                "[doctor] webhook-coverage: WARN — could not load V4 config; skipping check",
            )
    else:
        # Say so. Without this the check emits NOTHING when the config is
        # absent — not a skip, not a warning, no line at all — and a silent
        # check is indistinguishable from a passing one.
        typer.echo(
            f"[doctor] webhook-coverage: SKIPPED — no V4 config at {config_path}",
        )

    if config is not None:
        projects_path = Path(os.environ.get("FOREMAN_PROJECTS_PATH", str(_DEFAULT_PROJECTS_PATH)))
        projects: list[ProjectConfig] | None = None
        try:
            from foreman.v4.config import load_projects

            projects = load_projects(projects_path)
        except Exception:
            typer.echo(
                "[doctor] webhook-coverage: WARN — could not load projects; skipping check",
            )

        if projects is not None:
            if not projects:
                typer.echo(
                    "[doctor] webhook-coverage: WARN — no projects configured; skipping check",
                )
            else:
                # Mint the orchestrator token using the first project's repo
                # (valid in the single-org deployment model documented in
                # identity.py — any project's repo works for the
                # installation-id lookup).
                try:
                    from foreman.v4.identity import V4IdentityRegistry

                    identity = V4IdentityRegistry(
                        apps=config.apps,
                        orchestrator=config.orchestrator,
                        installation_repo=projects[0].repo,
                    )
                    orch_token = identity.get_role_token("orchestrator")
                    github_factory = lambda: Github(auth=Auth.Token(orch_token))  # noqa: E731
                    overall_exit |= _check_webhook_coverage(
                        config, projects, github_factory=github_factory
                    )
                except Exception as exc:
                    # Any failure minting the orchestrator token (GithubException,
                    # FileNotFoundError for missing PEM, network error, etc.)
                    # degrades gracefully to WARN — the daemon is not blocked.
                    typer.echo(
                        f"[doctor] webhook-coverage: WARN — could not mint orchestrator "
                        f"token ({type(exc).__name__}: {exc}); skipping check",
                    )

    if overall_exit != 0:
        raise typer.Exit(code=overall_exit)
