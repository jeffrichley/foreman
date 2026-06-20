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

import typer

# Foreman's own source-of-truth repo — the one whose ``main`` the
# daemon image is built from. Hardcoded module-level constant (not
# pulled from config) because the configured projects are what the
# daemon MANAGES, not the source of the daemon image. Matches the
# convention for ``_DEFAULT_CONFIG`` in ``cli/__init__.py``.
FOREMAN_SOURCE_REPO = "jeffrichley/foreman"


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


def cmd_doctor(ctx: typer.Context) -> None:
    """Run a sequence of deployment-health checks.

    Each check prints its own status line and contributes to the exit
    code. The command exits with code 1 if any check reported confirmed-
    bad state (e.g., STALE image); 0 otherwise (including SKIPPED and
    WARN conditions — see module docstring rationale).
    """
    # The ctx parameter is unused for this initial check, but every
    # future check that reads V4Config (e.g., GitHub Apps token expiry)
    # will need it. Keeping the signature stable now avoids a churn
    # cycle when the second check lands.
    del ctx  # explicit unused-arg marker

    overall_exit = 0
    overall_exit |= _check_image_fresh()

    if overall_exit != 0:
        raise typer.Exit(code=overall_exit)
