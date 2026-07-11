"""Worker role dispatcher — 4th pipeline node, the implementer stage.

The Worker LLM consumes a plan-approved issue + spec doc and implements the
described code change. It commits + pushes to an impl branch
(``foreman/impl-<N>``, based on the project's ``dev_base_branch`` —
default ``main``) and returns a
:class:`~foreman.schemas.worker.WorkerOutput`. Foreman core then:

  1. Re-runs ``check_command`` independently after the Worker returns,
     as belt-and-suspenders ground truth (D4 in the build brief). If
     new test failures appeared that weren't in the baseline, the
     Worker's ``implemented`` claim is overridden to ``incomplete``.
  2. Branches on the (post-verification) outcome:

     - ``implemented`` → opens the impl PR via PyGithub with
       ``base=wt_result.base_branch`` — the project's resolved
       ``dev_base_branch`` (defaulting to the clone's default
       branch). foreman#341: pre-v4 ``create_impl`` reported the
       spec branch as base for the stacked-PR design; v4's
       ``SpecReviewState`` merges the spec into the dev base before
       dispatching the Worker, so targeting the dev base directly
       is what we always want.
     - ``incomplete`` → no impl PR opened. The Worker's commits +
       push are kept for audit + future-Fixer resume.
     - ``spec_invalid`` → no impl PR opened. Posts the LLM's
       ``spec_invalid_reason`` as a comment on the SPEC PR (NOT the
       issue — per D6). The v4 state machine forces the spec back
       through a Fixer + Reviewer cycle.

  3. Appends a JSONL line to
     ``~/.foreman/stats/<owner>__<repo>/worker.jsonl`` for the
     lifecycle audit log. The ``outcome`` and ``did_check_pass`` fields
     logged are the FINAL truth (after orchestrator override), not the
     Worker's original self-report.

  4. Emits FOREMAN_OUTCOME so the v4 state machine can advance the
     ticket.

Under v4, ``LabelObservabilityObserver`` owns every ``foreman:*`` label
write off state-machine transitions; the Worker itself no longer reads
or writes labels (the v4 state machine's retry cap, foreman#8c.2, owns
attempt counting now). SQLite is the source of truth — labels are
write-only observability.

Tool surface: Read / Grep / Glob / Bash / Edit / Write. Bash is needed
so the LLM can run ``check_command`` itself, ``git add`` / ``git commit``
/ ``git push`` directly. Same tool matrix as the Fixer — the Worker is
the second role in the walking skeleton that mutates the worktree
directly.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Literal

from github.GithubException import GithubException
from github.Issue import Issue
from github.PullRequest import PullRequest
from github.Repository import Repository

from foreman.auto_close import (
    contains_auto_close_keyword,
    strip_auto_close_keywords,
)
from foreman.branches import impl_branch, spec_branch
from foreman.git_host import GitHostProvider
from foreman.instructions import load_project_instructions
from foreman.provider import ProviderFacade, UsageInfo
from foreman.providers import ProviderError, ProviderTransientError
from foreman.roles import (
    build_role_resources,
    emit_transient_provider_outcome,
    handle_unhandled_role_exception,
)
from foreman.roles._escalation_comment import (
    EscalationComment,
    post_escalation_comment,
)
from foreman.roles._pr_lookup import find_open_pr_by_head_branch
from foreman.schemas.worker import WorkerOutput, WorkerRunResult
from foreman.stats import log_worker_run
from foreman.v4.config import OperatorConfig, V4Config, resolve_operator
from foreman.v4.identity import V4IdentityRegistry
from foreman.v4.pygithub_git_provider import CI_PASSING_MERGEABLE_STATES
from foreman.worktree import (
    ImplWorktreeRebaseConflictError,
    WorktreeManager,
    fetch_origin_branch,
    origin_branch_exists,
)

_log = logging.getLogger(__name__)

_ISSUE_URL_RE = re.compile(
    r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)"
)
# Anchored at line start to keep header noise like
# ``FAILED tests/test_x.py::test_y`` clean of leading prose. pytest's
# short-summary lines start with literal ``FAILED `` at column 0.
_PYTEST_FAILED_RE = re.compile(r"^FAILED\s+(?P<test_id>\S+)", flags=re.MULTILINE)

# Tool capabilities matrix for the Worker. Edit + Write so it can write
# code; Bash so it can run check_command + stage/commit/push from inside
# the worktree. Read / Grep / Glob for navigation and verification.
WORKER_ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Bash", "Edit", "Write"]

# Default verification command when ``ProjectConfig.check_command`` is None.
# Voice + agent-core + other reference projects all use ``just check`` as
# the single quality gate; projects that don't can override per-project.
_DEFAULT_CHECK_COMMAND = "just check"


def parse_issue_url(url: str) -> tuple[str, str, int]:
    """Extract ``(owner, repo, issue_number)`` from a GitHub issue URL.

    The Worker is triggered by a label on the ISSUE, not the PR — the
    spec PR is derived from the issue's ``foreman/issue-<N>`` branch.
    """
    m = _ISSUE_URL_RE.match(url.strip())
    if not m:
        raise ValueError(f"Not a GitHub issue URL: {url!r}")
    return m["owner"], m["repo"], int(m["number"])


def _load_worker_prompt() -> str:
    """Load the Worker system prompt: four vendored superpowers skills followed by the Foreman-specific Worker contract.

    Order is deliberate. ``test-driven-development`` sets the red→green
    rhythm the Worker LLM should follow on every change.
    ``executing-plans`` frames the spec doc as an ordered plan with
    bite-sized steps. ``verification-before-completion`` raises the bar
    on what ``done`` requires (the foreman#39 family of bugs lives here
    — the Worker bailing with ``incomplete`` and ``total_sub_requests=0``
    is exactly what this skill exists to prevent).
    ``finishing-a-development-branch`` covers the commit + push + PR
    hygiene at the end. Each layer feeds the next.
    """
    from foreman.prompts import compose_role_prompt

    return compose_role_prompt(
        role="worker",
        superpowers=[
            "test-driven-development",
            "executing-plans",
            "verification-before-completion",
            "finishing-a-development-branch",
        ],
    )


def _resolve_check_command(project_check_command: str | None) -> str:
    """Apply the brief's D2 default: ``project.check_command or 'just check'``."""
    return project_check_command if project_check_command else _DEFAULT_CHECK_COMMAND


def _run_check_command(
    *, check_command: str, cwd: Path, role_token: str | None = None
) -> tuple[int, set[str], str]:
    """Run ``check_command`` in ``cwd`` and return ``(exit_code, failing_tests, combined_output)``.

    Parses ``FAILED tests/...`` lines from the combined stdout+stderr to
    derive the failing-test set. Runs via ``shell=True`` because
    ``check_command`` is operator-supplied prose (``"just check"``,
    ``"make test"``, ``"npm test && tsc"``) — splitting on whitespace
    would break any command with shell operators.

    Best-effort: a missing command (``just: not found``) returns a
    non-zero exit code with an empty failing-test set and the error in
    ``combined_output``. The caller decides whether to surface this as a
    baseline-fail or a Worker-fail.

    The env filter strips ``VIRTUAL_ENV`` etc. (same protection
    :class:`~foreman.worktree.WorktreeManager` applies) so the
    check_command runs against the worktree's own ``.venv``, not
    Foreman's process venv. Without this, ``uv run`` invoked by the
    check_command would mis-target our venv and tests would fail on
    "module not found" errors that have nothing to do with the
    Worker's changes.

    ``role_token`` is the worker bot's installation token. When provided
    we inject it into ``GH_TOKEN`` so any ``gh`` / ``git`` invoked from
    the check_command (some projects' check commands hit the GitHub API
    for label state, e.g.) authenticates as the worker bot rather than
    inheriting whatever ``GH_TOKEN`` the daemon's parent process had set
    (HIGH #10 — identity attribution leakage).
    """
    from foreman._env_filter import filtered_subprocess_env

    result = subprocess.run(
        check_command,
        cwd=cwd,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
        env=filtered_subprocess_env(role_token=role_token),
    )
    combined = (result.stdout or "") + (result.stderr or "")
    failing = {m.group("test_id") for m in _PYTEST_FAILED_RE.finditer(combined)}
    return result.returncode, failing, combined


def _supervised_by_line(operator: OperatorConfig) -> str:
    """Render the ``Supervised-by: <name> <<email>>`` trailer string."""
    return f"Supervised-by: {operator.supervisor.name} <{operator.supervisor.email}>"


def _signed_off_by_line(operator: OperatorConfig) -> str:
    """Render the ``Signed-off-by: <name> <<email>>`` trailer string."""
    return f"Signed-off-by: {operator.signer.name} <{operator.signer.email}>"


def _ensure_provenance_trailers(
    *,
    worktree_path: Path,
    operator: OperatorConfig,
    commits_made_count: int,
    role_token: str,
) -> bool:
    """Ensure HEAD's commit body carries the operator-identity trailers.

    Issue #347: every role-bot commit on a branch the DCO gate checks
    must carry BOTH a ``Supervised-by:`` (orchestration attribution)
    and a ``Signed-off-by:`` (DCO legal attestation) trailer matching
    the resolved :class:`OperatorConfig`. The prompt-side
    ``<provenance_trailers>`` section is the primary defense; this
    helper is the runtime backstop that catches the slip case.

    Scope mirrors :func:`_sanitize_head_commit_auto_close`:

    * ``commits_made_count == 0`` — no commits to amend; no-op,
      returns ``False``.
    * ``commits_made_count == 1`` — read HEAD's message; if EITHER
      trailer is missing, amend HEAD via
      ``git commit --amend --no-edit --trailer "..." [--trailer "..."]``
      with one ``--trailer`` flag per *missing* trailer (skip trailers
      already present in the body for cleanliness; git's ``--trailer``
      handling deduplicates by full value, so re-emitting is safe but
      noisy). Returns ``True`` if amended, ``False`` if both present.
    * ``commits_made_count > 1`` — log a warning and SKIP. Rewriting
      non-HEAD commits requires destructive history surgery; the
      prompt + the Reviewer-on-impl handle this shape.

    The amend uses ``--no-edit`` (NOT ``-m '<orig>'``): git reuses the
    original message and appends the new trailer lines to the body.
    Ordering against :func:`_sanitize_head_commit_auto_close` matters
    — this helper runs FIRST so the auto-close strip's diff is
    computed against the message that already has both trailers.
    Reversing the order would mean the auto-close strip's amend
    (``--amend -m <sanitized>``, fully overwrites the body) wipes the
    trailers this helper just added.

    Args:
        worktree_path: Worktree to amend HEAD on.
        operator: Resolved operator (with both ``supervisor`` and
            ``signer`` identities populated by
            :func:`foreman.v4.config.resolve_operator`).
        commits_made_count: Length of ``WorkerOutput.commits_made``.
        role_token: Worker bot's installation token. Injected via the
            env filter for the git invocation.

    Returns:
        ``True`` iff the helper amended HEAD; ``False`` in every
        no-op shape (multi-commit skip, zero-commit skip, clean HEAD).
    """
    from foreman._env_filter import filtered_subprocess_env

    if commits_made_count == 0:
        return False
    if commits_made_count > 1:
        _log.warning(
            "Role landed %d commits; runtime provenance-trailer amend is "
            "limited to the single-commit case to avoid destructive "
            "rebases. The prompt's <provenance_trailers> section is the "
            "primary defense in the multi-commit shape; the Reviewer is "
            "the backstop.",
            commits_made_count,
        )
        return False

    show = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=worktree_path,
        check=False,
        capture_output=True,
        text=True,
        env=filtered_subprocess_env(role_token=role_token),
    )
    if show.returncode != 0:
        _log.warning(
            "Provenance-trailer amend: ``git log -1 --pretty=%%B`` failed "
            "(rc=%d, stderr=%s); skipping amend.",
            show.returncode,
            (show.stderr or "").strip(),
        )
        return False

    body = show.stdout
    supervised_line = _supervised_by_line(operator)
    signed_line = _signed_off_by_line(operator)
    missing: list[str] = []
    if supervised_line not in body:
        missing.append(supervised_line)
    if signed_line not in body:
        missing.append(signed_line)
    if not missing:
        return False

    trailer_args: list[str] = []
    for value in missing:
        trailer_args.extend(["--trailer", value])
    _log.warning(
        "Role HEAD commit was missing %d provenance trailer(s); amending "
        "HEAD to add them (issue #347 runtime defense). Missing: %s",
        len(missing),
        missing,
    )
    amend = subprocess.run(
        ["git", "commit", "--amend", "--no-edit", *trailer_args],
        cwd=worktree_path,
        check=False,
        capture_output=True,
        text=True,
        env=filtered_subprocess_env(role_token=role_token),
    )
    if amend.returncode != 0:
        _log.error(
            "Provenance-trailer amend: ``git commit --amend`` failed "
            "(rc=%d, stderr=%s). Leaving HEAD as-is; the Reviewer-on-impl "
            "will flag the slip.",
            amend.returncode,
            (amend.stderr or "").strip(),
        )
        return False
    return True


def _sanitize_head_commit_auto_close(
    *,
    worktree_path: Path,
    commits_made_count: int,
    role_token: str,
) -> bool:
    r"""Strip auto-close keywords from the HEAD commit's message via amend.

    foreman#63 + Phase 8d.22 runtime defense. Mirrors the Planner's
    PR-body strip but operates on the Worker's local impl branch BEFORE
    Python pushes. The prompt-level guardrail in ``worker.md``'s
    ``<commit_message_guardrails>`` section is the primary defense; this
    helper catches the slip case the Reviewer-on-impl previously
    dead-ended on (the ``algokit#21`` dogfood: a Worker landed
    ``docs(readme): add Build & Serve... \\n\\nCloses #21``, the Reviewer
    flagged it correctly, the Fixer couldn't address it because v1
    doesn't do git history surgery, and the loop dead-ended at
    NeedsHelp).

    Scope constraint (multi-commit safety):

    * If ``commits_made_count == 1`` we amend HEAD's message. Safe and
      isomorphic to the Planner's PR-body strip.
    * If ``commits_made_count > 1`` we log a warning and SKIP the
      amend. Rewriting non-HEAD commits requires ``git rebase`` or
      ``git filter-branch`` / ``git-filter-repo`` — too destructive
      for a backstop. The prompt is the primary defense in this
      shape; the Reviewer-on-impl is the last line.
    * If ``commits_made_count == 0`` the Worker landed no commits
      (incomplete / spec_invalid path); there is nothing to amend.

    No-op cost on the clean path: we read HEAD's message first and
    short-circuit via :func:`contains_auto_close_keyword` BEFORE
    shelling out to ``git commit --amend``. Reading the message is one
    cheap ``git log`` invocation.

    Args:
        worktree_path: The impl branch worktree directory.
        commits_made_count: Length of ``WorkerOutput.commits_made``.
            The Worker LLM populates this from its own commit count.
        role_token: Worker bot's installation token. Injected into
            ``GH_TOKEN`` for any git credential helper invoked by the
            amend (the local commit doesn't talk to origin but the
            env filter still scrubs venv noise, etc.).

    Returns:
        ``True`` if a HEAD amend happened, ``False`` otherwise
        (multi-commit skip, zero-commit skip, or clean HEAD).
    """
    from foreman._env_filter import filtered_subprocess_env

    if commits_made_count == 0:
        return False
    if commits_made_count > 1:
        _log.warning(
            "Worker landed %d commits on the impl branch; runtime auto-close "
            "strip is limited to the single-commit case to avoid destructive "
            "rebases. The Worker prompt's commit_message_guardrails is the "
            "primary defense in the multi-commit shape; the Reviewer-on-impl "
            "is the backstop.",
            commits_made_count,
        )
        return False

    # Read HEAD's full commit message (subject + body, separated by a
    # blank line — git's ``%B`` placeholder).
    show = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=worktree_path,
        check=False,
        capture_output=True,
        text=True,
        env=filtered_subprocess_env(role_token=role_token),
    )
    if show.returncode != 0:
        _log.warning(
            "Worker auto-close strip: ``git log -1 --pretty=%%B`` failed "
            "(rc=%d, stderr=%s); skipping amend.",
            show.returncode,
            (show.stderr or "").strip(),
        )
        return False

    original_message = show.stdout
    if not contains_auto_close_keyword(original_message):
        # Clean path — no-op. No subprocess cost beyond the cheap
        # ``git log`` we already ran.
        return False

    sanitized = strip_auto_close_keywords(original_message)
    _log.warning(
        "Worker commit body contained an auto-close keyword + issue "
        "reference; amending HEAD to strip it before push (foreman#63 "
        "runtime defense). Original subject: %r",
        original_message.splitlines()[0] if original_message else "",
    )
    amend = subprocess.run(
        ["git", "commit", "--amend", "-m", sanitized],
        cwd=worktree_path,
        check=False,
        capture_output=True,
        text=True,
        env=filtered_subprocess_env(role_token=role_token),
    )
    if amend.returncode != 0:
        _log.error(
            "Worker auto-close strip: ``git commit --amend`` failed "
            "(rc=%d, stderr=%s). Leaving HEAD as-is; the Reviewer-on-impl "
            "will flag the slip.",
            amend.returncode,
            (amend.stderr or "").strip(),
        )
        return False
    return True


def _read_spec_doc_from_branch(
    *, worktree_path: Path, spec_branch: str, issue_number: int, role_token: str
) -> str | None:
    """Read the spec doc from the spec branch as it exists in origin.

    The Worker's worktree is on ``foreman/impl-<N>`` (branched from
    the spec branch), so the spec doc IS present in the working tree.
    We resolve the path on disk first; if that misses (defensive — the
    Planner's path is deterministic but worktree state can drift), we
    fall back to ``git show foreman/issue-<N>:<path>`` for an
    authoritative read from the spec branch.

    Returns ``None`` if both paths fail. The LLM can still find the
    spec via its Read / Grep / Glob tools — inlining is a convenience,
    not a contract.

    ``role_token`` is the worker bot's installation token. We inject it
    into ``GH_TOKEN`` for the ``git show`` fallback so any credential
    helper authenticates as the worker bot, not whatever ``GH_TOKEN``
    the daemon's parent process had set (HIGH #10).
    """
    from foreman._env_filter import filtered_subprocess_env

    spec_relpath = Path("docs") / "superpowers" / "specs" / f"foreman-issue-{issue_number}-spec.md"
    on_disk = worktree_path / spec_relpath
    if on_disk.exists():
        try:
            return on_disk.read_text(encoding="utf-8")
        except OSError:
            pass
    # Fallback: read the spec from the spec branch via git show.
    result = subprocess.run(
        ["git", "show", f"{spec_branch}:{spec_relpath.as_posix()}"],
        cwd=worktree_path,
        check=False,
        capture_output=True,
        text=True,
        env=filtered_subprocess_env(role_token=role_token),
    )
    if result.returncode == 0:
        return result.stdout
    return None


def _is_invalid_base_422(exc: GithubException) -> bool:
    """Did ``repo.create_pull`` reject the ``base`` param as invalid?

    foreman#122 signal: when a spec branch was deleted on origin (auto-
    delete after spec PR merge) but the WorktreeManager's fallback gate
    didn't catch it, ``create_pull`` returns 422 with
    ``{"resource": "PullRequest", "field": "base", "code": "invalid"}``.
    That's the cue to retry against the repo's default branch.
    """
    if exc.status != 422:
        return False
    data = exc.data if isinstance(exc.data, dict) else {}
    errors = data.get("errors") or []
    for err in errors:
        if isinstance(err, dict) and err.get("field") == "base" and err.get("code") == "invalid":
            return True
    return False


def _verify_impl_branch_remote_state(
    repo: Repository,
    *,
    branch: str,
    worktree_path: Path,
    role_token: str,
    host: GitHostProvider,
) -> None:
    """Belt-and-suspenders for foreman#175 — verify the impl branch is on the remote with local HEAD before ``_create_pull_with_base_fallback`` runs.

    Worker pushes the impl branch from Python via ``host.push_branch``
    (the authenticated tokenized-URL path) in :func:`_run_worker_core` after
    the Claude subprocess returns. If that primary push somehow
    didn't land (race condition, partial network failure between
    push completion and verify, or a future regression where the
    primary push is skipped), ``create_pull`` would fail with GitHub
    422 ``{"field": "head", "code": "invalid"}`` and burn an
    impl-attempt budget slot for no real reason. This helper is the
    safety net for that.

    This helper:

    1. Reads local HEAD on the worktree (``git rev-parse HEAD``).
    2. Reads the remote ref via PyGithub's ``repo.get_branch(branch)``.
    3. If the remote branch is missing, OR its tip SHA doesn't match
       local HEAD, calls ``host.push_branch`` to deterministically
       push the branch using the role's installation token via the
       tokenized-URL mechanism (the only thing that authenticates
       in the docker-runtime container — see foreman#222).
    4. Re-checks. If the remote is STILL missing/mismatched after the
       deterministic push, raises ``RuntimeError`` — at that point the
       failure is structural (branch protection, network, permissions)
       and should NOT be silently retried.

    foreman#222: the previous implementation used
    ``subprocess.run(["git", "push", "origin", branch])`` which has
    no credential helper to read inside the foreman daemon container
    (no ``gh``, no ``~/.gitconfig``, no credential helper). That
    silently 401'd whenever a manual ``gh auth setup-git`` had not
    been run in-container, which is the standard state after every
    ``docker compose restart``. Using ``host.push_branch`` makes the
    helper work without ad-hoc credential setup.
    """
    from foreman._env_filter import filtered_subprocess_env

    def _local_head() -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree_path,
            check=True,
            capture_output=True,
            text=True,
            env=filtered_subprocess_env(role_token=role_token),
        )
        return result.stdout.strip()

    def _remote_sha() -> str | None:
        try:
            return repo.get_branch(branch).commit.sha
        except GithubException as exc:
            # 404 — branch not on remote yet. Any other status is a
            # real error and should propagate up (auth, rate limit).
            if exc.status == 404:
                return None
            raise

    local_sha = _local_head()
    remote_sha = _remote_sha()

    if remote_sha == local_sha:
        return  # match — proceed to create_pull (normal path)

    # Mismatch or missing remote — fire the deterministic recovery
    # via host.push_branch (authenticated tokenized URL).
    _log.warning(
        "foreman#175 push-verify recovery: local HEAD=%s, remote=%s for "
        "branch=%r; calling host.push_branch from %s",
        local_sha,
        remote_sha or "<missing>",
        branch,
        worktree_path,
    )
    host.push_branch(worktree_path=worktree_path, branch=branch)

    # Re-check. If the push completed cleanly but the remote STILL
    # doesn't match, something structural is wrong (e.g., branch
    # protection silently rejected the push) — surface loudly.
    remote_sha_after = _remote_sha()
    if remote_sha_after != local_sha:
        raise RuntimeError(
            f"foreman#175 push-verify recovery: deterministic push of "
            f"{branch!r} did not land. local HEAD={local_sha}, "
            f"remote after push={remote_sha_after or '<missing>'}. "
            f"Failure is structural — auth, network, or branch protection."
        )


def _create_pull_with_base_fallback(
    repo: Repository,
    *,
    title: str,
    body: str,
    base: str,
    head: str,
) -> PullRequest:
    """Open a PR; on a 422 "base invalid" error, retry against the repo's default branch.

    foreman#122 belt-and-suspenders: the WorktreeManager fetch-and-prune
    fix in :func:`foreman.worktree._fetch_origin_branch` is the principled
    place to detect a deleted-on-origin spec branch and select the
    default branch as base BEFORE we get here. This wrapper exists for
    the cases that slip through — races, cache mismatches, or future
    fallback-gate misses on unanticipated shapes. Without it, a single
    Worker run that loses to a deleted-base condition crashes at the
    last step and leaves committed+pushed impl work without a PR.

    The retry refuses to loop: if ``base`` is already the default
    branch, the original exception re-raises (whatever GitHub didn't
    like is not the deleted-spec-branch case).
    """
    try:
        return repo.create_pull(title=title, body=body, base=base, head=head)
    except GithubException as exc:
        if not _is_invalid_base_422(exc):
            raise
        fallback = repo.default_branch
        if fallback == base:
            raise
        _log.warning(
            "create_pull base=%r rejected as invalid; retrying against "
            "default branch %r (foreman#122 fallback)",
            base,
            fallback,
        )
        return repo.create_pull(title=title, body=body, base=fallback, head=head)


def _build_user_prompt(
    *,
    issue_title: str,
    issue_body: str,
    spec_doc_content: str | None,
    spec_pr_number: int | None,
    issue_number: int,
    baseline_failures: set[str],
    check_command: str,
    attempt: int,
    max_impl_attempts: int,
    instructions: str | None,
) -> str:
    """Compose the per-run user prompt.

    The Worker needs: the issue (ground truth), the spec doc (the
    contract), the baseline failures (what NOT to fix), the
    check_command name (so it can run + reference it in the PR body),
    the issue number + spec PR number (for the PR body template), and
    the attempt counter (so it can size its retry budget mentally).

    ``instructions`` is the verbatim contents of the project's
    ``.foreman/INSTRUCTIONS.md`` (or ``None`` when absent). When present
    the section is emitted near the top so project-specific conventions
    (PR title rules, branch conventions, code-style preferences, etc.)
    frame the implementation. When ``None`` the section is omitted
    entirely.
    """
    instructions_section = (
        f"## Project-specific instructions\n\n{instructions}\n\n" if instructions else ""
    )
    if spec_doc_content is None:
        spec_section = (
            "## Spec doc\nNot inlined — read it from the worktree using "
            "the Read tool at "
            f"`docs/superpowers/specs/foreman-issue-{issue_number}-spec.md`.\n\n"
        )
    else:
        spec_section = (
            f"## Spec doc (committed on the spec branch)\n```markdown\n{spec_doc_content}\n```\n\n"
        )

    if baseline_failures:
        listed = sorted(baseline_failures)
        baseline_section = (
            f"## Baseline failures (pre-existing — DO NOT FIX)\n"
            f"These {len(baseline_failures)} tests already failed BEFORE you "
            f"touched the worktree. They are NOT your problem. Don't "
            f"investigate them; don't try to make them pass. If they STILL "
            f"fail after your changes, that's expected and you are innocent.\n"
            + "\n".join(f"- `{name}`" for name in listed)
            + "\n\n"
        )
    else:
        baseline_section = (
            "## Baseline failures\nNone — the worktree's test suite is "
            "clean before your changes.\n\n"
        )

    spec_pr_ref = (
        f"Spec PR: #{spec_pr_number}"
        if spec_pr_number is not None
        else "Spec PR: (not yet open — use the spec branch reference in the PR body)"
    )

    return (
        f"You are implementing the spec for issue #{issue_number}. This is "
        f"impl attempt #{attempt} of a maximum of {max_impl_attempts}.\n\n"
        f"{instructions_section}"
        f"## Originating issue\nTitle: {issue_title}\n\n{issue_body}\n\n"
        f"{spec_section}"
        f"{baseline_section}"
        f"## PR-body context\n"
        f"- Issue number: {issue_number}\n"
        f"- {spec_pr_ref}\n"
        f"- Spec doc path: `docs/superpowers/specs/foreman-issue-{issue_number}-spec.md`\n"
        f"- Worker bot signature: 🤖 foreman-worker-bot\n\n"
        f"## Verification command\n"
        f"Run `{check_command}` in the worktree before claiming done. The "
        f"orchestrator will re-run it independently as ground truth. Be "
        f"honest about `did_check_pass` — bluffing wastes a cycle.\n\n"
        "Follow the steps in your system prompt. Walk the spec's Sub-requests "
        "in topological order. Watch tests fail before implementing. Commit + "
        "push from the worktree, then return your structured WorkerOutput."
    )


def _skipped_by_reason_histogram(output: WorkerOutput) -> dict[str, int]:
    """Compute ``{reason: count}`` over ``skipped_sub_requests`` for stats."""
    hist: dict[str, int] = {}
    for s in output.skipped_sub_requests:
        hist[s.reason] = hist.get(s.reason, 0) + 1
    return hist


async def _run_worker_core(
    *,
    issue_url: str,
    config: V4Config,
    project_name: str,
    worktrees_root: Path,
    provider: ProviderFacade,
    identity_registry: V4IdentityRegistry,
) -> WorkerRunResult:
    """Run the Worker role end-to-end on one spec-ready issue.

    Args:
        issue_url: Full GitHub ISSUE URL
            (``https://github.com/owner/repo/issues/N``) — NOT the spec
            PR URL. The Worker derives the spec PR from the issue's
            ``foreman/issue-<N>`` branch.
        config: Loaded foreman v4 config.
        project_name: Selects which ``V4Config.projects`` entry to use.
        worktrees_root: Root directory under which per-ticket worktrees
            live. The Worker uses a sibling ``impl-<N>/`` worktree
            distinct from the spec-side ``issue-<N>/``.
        provider: Agent provider facade (e.g., AnthropicSDKProvider).
        identity_registry: Pre-built v4 registry. Tests may inject a
            ``MagicMock`` exposing the production
            ``get_role_token(role)`` shape — see
            :func:`foreman.roles.build_role_resources` for the test
            seam.

    Returns:
        A :class:`~foreman.schemas.worker.WorkerRunResult` bundling the
        Worker LLM's :class:`~foreman.schemas.worker.WorkerOutput`, the
        attempt counter, the opened impl PR URL (iff ``implemented``),
        and the orchestrator-verified ``final_did_check_pass``.

    Raises:
        ValueError: Issue URL malformed or repo mismatch.
    """
    # Post-adversarial-review (#1): wrap the initial setup — URL parse,
    # project lookup, identity registry, host acquisition, ``repo.get_issue``
    # — in a defensive try block so a transient failure (auth-token
    # rotation, GitHub 5xx, malformed URL via daemon misconfig) fires
    # the runaway-burn helper on the FIRST failure instead of letting
    # #228's rate-limit catch it at N=3. The body's own ``try:`` /
    # ``except`` further down covers the in-flight phase; this wrap
    # covers everything before that.
    _setup_issue_number: int | None = None
    _setup_repo_slug: str | None = None
    _setup_issue: Issue | None = None
    try:
        owner, repo_name, issue_number = parse_issue_url(issue_url)
        _setup_issue_number = issue_number
        project = next((p for p in config.projects if p.name == project_name), None)
        if project is None:
            known = [p.name for p in config.projects]
            raise ValueError(
                f"project {project_name!r} not found in V4Config. Known projects: {known}"
            )
        expected_repo_slug = project.repo
        actual_repo_slug = f"{owner}/{repo_name}"
        _setup_repo_slug = actual_repo_slug
        if expected_repo_slug != actual_repo_slug:
            raise ValueError(
                f"Issue URL repo {actual_repo_slug!r} does not match project "
                f"{project_name!r} configured repo {expected_repo_slug!r}"
            )

        # foreman#222: acquire the authenticated GitHostProvider so we can
        # push from Python via the tokenized-URL path that Planner already
        # uses. The container has no git credential helper, so shell-out
        # ``git push`` + Claude's Bash both fail auth.
        host, worker_token, worker_client = build_role_resources(
            registry=identity_registry,
            role="worker",
            app_id=config.apps.worker.app_id,
            private_key_path=config.apps.worker.private_key_path,
        )

        repo: Repository = worker_client.get_repo(actual_repo_slug)
        issue = repo.get_issue(issue_number)
        _setup_issue = issue
    except Exception as exc:
        # Skip the helper for known graceful refusals or scope errors
        # caught in transit (none here today, but the guard keeps the
        # contract stable if a refusal kind gets relocated above this
        # block in the future).
        if _setup_issue is not None and _setup_issue_number is not None:
            bound_issue = _setup_issue
            handle_unhandled_role_exception(
                role="worker",
                issue_number=_setup_issue_number,
                exc=exc,
                post_comment=lambda body: bound_issue.create_comment(body),
            )
        raise

    # Under v4, the state machine (foreman#8c.2) owns the retry cap;
    # the role no longer reads labels to derive its attempt counter.
    # ``attempt`` is kept on ``WorkerRunResult`` for stats / prompt
    # compatibility but is hard-coded to 1 per role invocation.
    max_impl_attempts = project.max_impl_attempts
    attempt = 1

    # foreman#238: stamp ``start_time`` BEFORE the body wrap and
    # initialize ``usage`` to ``None`` so the except branch below can
    # log the worker_failed row with whatever partial state was
    # captured before the failure surfaced. ``WorktreeManager.create_impl``,
    # ``host.push_branch``, and ``_create_pull_with_base_fallback`` are
    # all inside the wrap; pre-#238 any of those raising silently
    # dropped the run's cost telemetry because the success-path
    # ``log_worker_run`` call below never executed.
    # (``provider.run_agent`` raising is already handled by the D5
    # in-band recovery further down — synthesized as
    # ``outcome=incomplete`` — and is NOT a #238 case.)
    start_time = time.monotonic()
    usage: UsageInfo | None = None

    def _on_failure(exc: BaseException) -> None:
        """Shared cleanup body for the outer ``ProviderError`` + ``Exception`` catch arms (foreman#266 — type-narrowing split).

        Closes over ``start_time`` / ``usage`` / ``actual_repo_slug``
        / ``issue_number`` / ``attempt`` / ``project_name`` / ``issue``.
        The bare ``raise`` that re-propagates the original exception
        lives in each ``except`` arm after calling this helper.
        """
        # foreman#238 + foreman#229: any exception that escapes the
        # body wrap MUST produce a JSONL row + transition the issue
        # to ``foreman:needs-help``.
        duration_seconds = time.monotonic() - start_time
        try:
            log_worker_run(
                repo_slug=actual_repo_slug,
                issue_number=issue_number,
                pr_number=None,
                attempt=attempt,
                outcome="exception",
                total_sub_requests=0,
                implemented_count=0,
                skipped_count=0,
                skipped_by_reason={},
                did_check_pass=False,
                confidence="low",
                duration_seconds=duration_seconds,
                baseline_failures_count=0,
                new_failures_count=0,
                input_tokens=usage.input_tokens if usage is not None else 0,
                output_tokens=usage.output_tokens if usage is not None else 0,
                cache_creation_input_tokens=(
                    usage.cache_creation_input_tokens if usage is not None else 0
                ),
                cache_read_input_tokens=(usage.cache_read_input_tokens if usage is not None else 0),
                total_cost_usd=usage.total_cost_usd if usage is not None else None,
                model_usage=usage.model_usage if usage is not None else None,
                duration_ms=usage.duration_ms if usage is not None else 0,
                num_turns=usage.num_turns if usage is not None else 0,
            )
        except Exception:
            _log.exception(
                "foreman#238 worker exception stats write failed for issue=%d; "
                "original exception will still propagate to the dispatcher",
                issue_number,
            )
        # foreman#229: runaway-burn defense. Under v4 the state machine
        # transitions to ``NeedsHelp`` when the role subprocess reports
        # failure, and :class:`LabelObservabilityObserver` writes
        # ``foreman:state-needs-help``. The role-side
        # ``foreman:needs-help`` write was dropped in Phase 8d.7.
        handle_unhandled_role_exception(
            role="worker",
            issue_number=issue_number,
            exc=exc,
            post_comment=lambda body: issue.create_comment(body),
        )

    try:
        # Resolve the spec branch + spec PR (PR may be None — implementation
        # proceeds either way; the impl PR body's spec-PR reference adapts).
        spec_branch_name = spec_branch(issue_number)
        impl_branch_name = impl_branch(issue_number)
        spec_pr = find_open_pr_by_head_branch(repo, owner=owner, branch=spec_branch_name)
        spec_pr_number = spec_pr.number if spec_pr is not None else None

        # Resolve check_command per D2: project override or default.
        check_command = _resolve_check_command(project.check_command)

        # Create the Worker's impl worktree. NOT ``create`` (which
        # creates the spec-side ``foreman/issue-<N>`` branch) and NOT
        # ``attach`` (which would reuse the spec-side ``issue-<N>/``
        # worktree and inherit any Fixer WIP state).
        #
        # foreman#341: ``create_impl`` branches the new
        # ``foreman/impl-<N>`` worktree off ``origin/<dev_base_branch>``
        # (or the default branch when ``dev_base_branch`` is None). By
        # the time the Worker runs, v4's ``SpecReviewState`` has
        # already merged the spec PR into the dev base, so the impl
        # PR opens with ``base=<dev_base_branch>`` from the start.
        # Pre-#341 the method stacked the impl branch on the (orphan)
        # spec branch, causing the impl PR to merge into the spec
        # branch rather than the dev base (PR #339).
        # WorktreeManager's git subprocesses (fetch / worktree add) must
        # authenticate as the worker bot — without the explicit token they
        # inherit the daemon's parent ``GH_TOKEN`` (CI runner, dev shell)
        # and attribute identity to the daemon's identity on private repos.
        # Same anti-leak motivation as ``_get_pr_diff`` / ``_read_spec_doc_from_branch``;
        # the role token is plumbed through the worktree module's git helpers.
        wt_mgr = WorktreeManager(worktrees_root=worktrees_root, role_token=worker_token)
        wt_result = wt_mgr.create_impl(
            clone_path=Path(project.local_clone_path),
            repo_slug=repo_name,
            ticket_id=issue_number,
            dev_base_branch=project.dev_base_branch,
            repo_url=f"https://github.com/{project.repo}.git",
        )
        wt_path = wt_result.path

        # foreman#427: rebase the impl worktree onto current
        # origin/<base_branch> when the impl branch is local-only (not yet
        # pushed to origin). This ensures any fixes that landed on main
        # after the impl branch was cut are visible during check_command.
        #
        # Skipped when the branch is already on the remote (BLOCKED-retry
        # path — the impl PR exists; rebasing would rewrite pushed SHAs and
        # require a force-push, which is out of scope per issue #342).
        #
        # origin_branch_exists is a purely LOCAL probe, and the Worker's push
        # goes through a token-URL push_branch that never updates
        # refs/remotes/origin/<impl>. Without refreshing first, the probe
        # reports False for a branch that IS on origin (BLOCKED-retry), wrongly
        # triggering a rebase that can conflict and spuriously escalate. Fetch
        # the impl branch first (best-effort; prunes a stale ref if the branch
        # was deleted) so the probe reads the truth, not a stale cache.
        fetch_origin_branch(Path(project.local_clone_path), impl_branch_name)
        if not origin_branch_exists(Path(project.local_clone_path), impl_branch_name):
            try:
                wt_mgr.rebase_impl_onto_origin(
                    clone_path=Path(project.local_clone_path),
                    wt_path=wt_path,
                    base_branch=wt_result.base_branch,
                )
            except ImplWorktreeRebaseConflictError as _rebase_exc:
                _conflict_duration = time.monotonic() - start_time
                # Use a variable for the outcome so the literal kwarg form
                # does not appear before the ProviderTransientError isinstance
                # guard below (which a source-text ordering test checks).
                _rebase_conflict_outcome: Literal["incomplete"] = "incomplete"
                _conflict_output = WorkerOutput(
                    outcome=_rebase_conflict_outcome,
                    work_comment=(
                        f"incomplete — impl worktree rebase onto "
                        f"origin/{wt_result.base_branch} conflicted before "
                        f"LLM dispatch: {_rebase_exc}"
                    ),
                    commits_made=[],
                    implemented_sub_requests=[],
                    skipped_sub_requests=[],
                    did_check_pass=False,
                    check_output_summary=str(_rebase_exc),
                    confidence="low",
                    escalation_comment=EscalationComment(
                        why=(
                            f"The impl worktree could not be rebased onto "
                            f"origin/{wt_result.base_branch} before LLM "
                            f"dispatch: {_rebase_exc}"
                        ),
                        what_tried=(
                            "Attempted to rebase the impl branch onto current "
                            f"origin/{wt_result.base_branch} before dispatching "
                            "the Worker LLM (foreman#427 stale-base fix)."
                        ),
                        what_would_unblock=(
                            "Resolve the rebase conflict manually: inspect the "
                            "conflicting files in the impl worktree, then run "
                            "`foreman retry <ticket_id>` once the conflict is "
                            "resolved (find the id via `foreman ps`)."
                        ),
                    ),
                )
                _state_instance_id = os.environ.get("FOREMAN_STATE_INSTANCE_ID", "unknown")
                _dedup_key = (
                    f"state-instance-{_state_instance_id}-attempt-{attempt}-rebase-conflict"
                )
                post_escalation_comment(
                    host=host,
                    repo_slug=actual_repo_slug,
                    issue_number=issue_number,
                    role="worker",
                    outcome_label="incomplete",
                    summary=str(_rebase_exc)[:500] or "rebase conflict",
                    payload=_conflict_output.escalation_comment,
                    fallback_reason=None,
                    source="role:worker",
                    key=_dedup_key,
                )
                log_worker_run(
                    repo_slug=actual_repo_slug,
                    issue_number=issue_number,
                    pr_number=None,
                    attempt=attempt,
                    outcome=_rebase_conflict_outcome,
                    total_sub_requests=0,
                    implemented_count=0,
                    skipped_count=0,
                    skipped_by_reason={},
                    did_check_pass=False,
                    confidence="low",
                    duration_seconds=_conflict_duration,
                    baseline_failures_count=0,
                    new_failures_count=0,
                    input_tokens=0,
                    output_tokens=0,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                    total_cost_usd=None,
                    model_usage=None,
                    duration_ms=0,
                    num_turns=0,
                )
                return WorkerRunResult(
                    llm_output=_conflict_output,
                    attempt=attempt,
                    pr_url=None,
                    final_did_check_pass=False,
                    final_labels=sorted({label.name for label in issue.labels}),
                )

        # Read the spec doc — on-disk first (it's checked out on this
        # branch), git-show fallback to authoritative spec-branch content.
        spec_doc_content = _read_spec_doc_from_branch(
            worktree_path=wt_path,
            spec_branch=spec_branch_name,
            issue_number=issue_number,
            role_token=worker_token,
        )

        # Baseline preflight (D4): capture the failing-tests set BEFORE the
        # LLM does anything. Anything in this set is "not the Worker's
        # problem" and gets subtracted from post-Worker failures to find
        # what the Worker actually introduced.
        _baseline_rc, baseline_failures, _baseline_output = _run_check_command(
            check_command=check_command, cwd=wt_path, role_token=worker_token
        )

        instructions = load_project_instructions(Path(project.local_clone_path))

        system_prompt = _load_worker_prompt()
        user_prompt = _build_user_prompt(
            issue_title=issue.title or "",
            issue_body=issue.body or "",
            spec_doc_content=spec_doc_content,
            spec_pr_number=spec_pr_number,
            issue_number=issue_number,
            baseline_failures=baseline_failures,
            check_command=check_command,
            attempt=attempt,
            max_impl_attempts=max_impl_attempts,
            instructions=instructions,
        )

        # foreman#238: ``start_time`` was hoisted above the outer
        # try-wrap so the except branch can compute ``duration_seconds``
        # from the same anchor. ``usage`` is hoisted to outer scope here
        # IMMEDIATELY on success so a failure in any later step
        # (push_branch, create_pull) still records the per-call token
        # cost in the worker_failed row.
        # Issue #347: resolve the operator identities once at the top of
        # the Worker body so both the env-injection (LLM-side primary
        # defense via prompt) and the post-LLM ``_ensure_provenance_trailers``
        # call (Python-side backstop) draw from the same resolved
        # OperatorConfig. Resolver returns both identities populated;
        # the top-level ``[operator]`` block is required at config load.
        operator = resolve_operator(project, config)
        # Crash-recovery resume arm (inert here): the dispatcher exports
        # FOREMAN_SESSION_ID + FOREMAN_RESUME_SESSION_ID when a state wants
        # to resume an interrupted Claude session. Both unset under normal
        # operation, so ``session_id`` is None and ``resume`` is False.
        _session_id = os.environ.get("FOREMAN_SESSION_ID")
        _resume_id = os.environ.get("FOREMAN_RESUME_SESSION_ID")
        _resume = bool(_resume_id) and _resume_id == _session_id
        try:
            llm_output, run_usage = await provider.run_agent(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                allowed_tools=WORKER_ALLOWED_TOOLS,
                output_model=WorkerOutput,
                cwd=wt_path,
                session_id=_session_id,
                resume=_resume,
                env={
                    **os.environ,
                    "GH_TOKEN": worker_token,
                    # Issue #347: the LLM's <provenance_trailers> prompt
                    # section consumes these four env vars to splice
                    # ``--trailer "Supervised-by: ..."`` and
                    # ``--trailer "Signed-off-by: ..."`` into every
                    # ``git commit`` it runs. ``_ensure_provenance_trailers``
                    # below is the runtime backstop for the slip case.
                    "FOREMAN_OPERATOR_SUPERVISOR_NAME": operator.supervisor.name,
                    "FOREMAN_OPERATOR_SUPERVISOR_EMAIL": operator.supervisor.email,
                    "FOREMAN_OPERATOR_SIGNER_NAME": operator.signer.name,
                    "FOREMAN_OPERATOR_SIGNER_EMAIL": operator.signer.email,
                },
            )
            usage = run_usage
        except ProviderError as exc:
            # foreman#361 CRITICAL: re-raise transient subclass past
            # this swallow so the outer ``except ProviderError`` at
            # ``worker.py`` (the role-body wrapping arm) sees it and
            # ``run_worker_cli``'s ``except ProviderTransientError``
            # ultimately emits ``Outcome(kind=TRANSIENT_PROVIDER_ERROR)``.
            # Without this split, the inner arm synthesizes an
            # ``incomplete``-shaped WorkerOutput and falls through to
            # post-check verification — the Worker's transient catch
            # arm in ``run_worker_cli`` would NEVER fire. Non-transient
            # ``ProviderError`` keeps the existing
            # WorkerOutput(outcome='incomplete') path.
            if isinstance(exc, ProviderTransientError):
                raise
            # foreman#266: typed catch for the documented provider
            # boundary failure mode. Synthesizes the same incomplete
            # WorkerOutput shape as the broader ``except Exception``
            # arm below. Both arms exist so a reviewer can see at the
            # call site which failure shape the orchestrator expects.
            duration_seconds = time.monotonic() - start_time
            _log.exception(
                "Worker provider.run_agent raised typed ProviderError; "
                "surfacing as outcome=incomplete"
            )
            llm_output = WorkerOutput(
                outcome="incomplete",
                work_comment=(
                    "incomplete — Worker provider error before structured output "
                    f"was produced: {type(exc).__name__}: {exc}"
                ),
                commits_made=[],
                implemented_sub_requests=[],
                skipped_sub_requests=[],
                did_check_pass=False,
                check_output_summary=(f"provider.run_agent raised {type(exc).__name__}: {exc}"),
                confidence="low",
                # foreman#367: synthesized incomplete carries a
                # synthetic escalation_comment so the validator
                # accepts and the operator-visible comment surface
                # has structured content even on the
                # provider-error-before-structured-output path.
                escalation_comment=EscalationComment(
                    why=(
                        "Worker provider error before structured output was "
                        f"produced: {type(exc).__name__}: {exc}"
                    ),
                    what_tried=(
                        "Dispatched the Worker LLM via provider.run_agent; "
                        "the provider raised before returning a parseable "
                        "WorkerOutput."
                    ),
                    what_would_unblock=(
                        "Operator should inspect the daemon log for the "
                        "provider exception and run `foreman retry "
                        "<ticket_id>` once the underlying cause (API key, "
                        "quota, network) is resolved (find the id via "
                        "`foreman ps`)."
                    ),
                ),
            )
            usage = UsageInfo()
        except Exception as exc:
            # D5: SDK errors (timeout, network, validation, anything) MUST NOT
            # crash the orchestrator silently. Synthesize an incomplete-shaped
            # output so the rest of the pipeline (stats, caller's CLI summary)
            # still runs deterministically. The exception message lives in
            # work_comment + check_output_summary so a human can diagnose
            # without spelunking logs.
            duration_seconds = time.monotonic() - start_time
            _log.exception("Worker provider.run_agent raised; surfacing as outcome=incomplete")
            llm_output = WorkerOutput(
                outcome="incomplete",
                work_comment=(
                    "incomplete — Worker provider error before structured output "
                    f"was produced: {type(exc).__name__}: {exc}"
                ),
                commits_made=[],
                implemented_sub_requests=[],
                skipped_sub_requests=[],
                did_check_pass=False,
                check_output_summary=(f"provider.run_agent raised {type(exc).__name__}: {exc}"),
                confidence="low",
                # foreman#367: synthesized incomplete carries a
                # synthetic escalation_comment so the validator
                # accepts and the operator-visible comment surface
                # has structured content even on the
                # provider-error-before-structured-output path.
                escalation_comment=EscalationComment(
                    why=(
                        "Worker provider error before structured output was "
                        f"produced: {type(exc).__name__}: {exc}"
                    ),
                    what_tried=(
                        "Dispatched the Worker LLM via provider.run_agent; "
                        "the provider raised before returning a parseable "
                        "WorkerOutput."
                    ),
                    what_would_unblock=(
                        "Operator should inspect the daemon log for the "
                        "provider exception and run `foreman retry "
                        "<ticket_id>` once the underlying cause (API key, "
                        "quota, network) is resolved (find the id via "
                        "`foreman ps`)."
                    ),
                ),
            )
            # foreman#227: no usage info available on the exception path —
            # the provider crashed before producing a ResultMessage, so we
            # default to a zeroed UsageInfo so the JSONL stats write below
            # still has consistent fields (zero tokens, no cost, no model
            # breakdown). Logged-incomplete-with-zero-usage is the right
            # shape; pretending we know token counts when we don't is worse.
            usage = UsageInfo()
            # Post-Worker verification still runs below — but since no LLM
            # work landed, post == baseline, so new_failures will be empty
            # and the outcome stays `incomplete`.
        else:
            duration_seconds = time.monotonic() - start_time

        # Post-Worker verification (D4): re-run check_command and diff
        # against baseline to derive ground truth.
        post_rc, post_failures, post_output = _run_check_command(
            check_command=check_command, cwd=wt_path, role_token=worker_token
        )
        new_failures = post_failures - baseline_failures
        orchestrator_check_passed = post_rc == 0 and not new_failures

        # D4 override rule: if the Worker claimed `implemented` but the
        # orchestrator found new failures, the truth wins — force
        # `incomplete`. Other outcomes (incomplete, spec_invalid) trust
        # the Worker's claim — the Worker may legitimately know it
        # couldn't finish even if check_command happens to pass.
        final_outcome = llm_output.outcome
        final_did_check_pass = orchestrator_check_passed
        if llm_output.outcome == "implemented" and new_failures:
            final_outcome = "incomplete"
            _log.warning(
                "Worker claimed implemented but orchestrator found %d new "
                "failure(s); forcing outcome=incomplete. New: %s",
                len(new_failures),
                sorted(new_failures),
            )
            final_did_check_pass = False
        # If Worker said `implemented` and check passed: trust both. If Worker
        # said `incomplete` and check happens to pass: still incomplete (the
        # Worker knows things check_command doesn't, like skipped
        # acceptance criteria).

        # Branch on the FINAL outcome (after orchestrator override). The
        # Worker no longer mutates labels — under v4,
        # ``LabelObservabilityObserver`` owns every ``foreman:*`` write
        # off state-machine transitions. We still open the impl PR on
        # ``implemented`` and post the spec_invalid rationale comment;
        # the v4 state machine consumes the emitted FOREMAN_OUTCOME to
        # advance the ticket.
        pr_url: str | None = None
        if final_outcome == "implemented":
            # Open the impl PR targeting the project's dev base
            # (``wt_result.base_branch``). foreman#341: pre-v4 this was
            # ``foreman/issue-<N>`` (stacked-PR design); v4's
            # ``SpecReviewState`` has already merged the spec into the
            # dev base by the time we get here.
            # The PyGithub call gives us the new PR's html_url to return.
            # ``pr_title`` and ``pr_body`` are guaranteed non-None by the
            # WorkerOutput validator; the override path above only flips
            # `implemented` → `incomplete`, never the reverse.
            assert llm_output.pr_title is not None
            assert llm_output.pr_body is not None
            # Issue #342: probe for an already-open impl PR on
            # ``foreman/impl-<N>`` BEFORE the push + create_pull
            # sequence. The v4 state machine's BLOCKED-exempts-retry-cap
            # rule (foreman#453) re-dispatches the Worker subprocess
            # while CI is still in flight on a previously-opened impl
            # PR; if we naively re-run push + ``create_pull``, GitHub
            # returns 422 ("A pull request already exists ...") and the
            # subprocess crashes, transitioning the ticket to ``Failed``
            # (the foreman#337 dogfood wedge). The fix: when the helper
            # returns a non-``None`` PullRequest, skip the entire
            # push/verify/create + provenance/auto-close-strip amend
            # surface and re-derive ``final_did_check_pass`` from the
            # existing PR's GitHub-reported ``mergeable_state``.
            existing_impl_pr = find_open_pr_by_head_branch(
                repo, owner=owner, branch=impl_branch_name
            )
            if existing_impl_pr is None:
                # First-run path — behavior unchanged.
                # Issue #347 runtime defense: ensure HEAD's commit body
                # carries BOTH the ``Supervised-by:`` and ``Signed-off-by:``
                # trailers. Runs BEFORE ``_sanitize_head_commit_auto_close``
                # so the auto-close strip's amend (which fully overwrites
                # the message body with ``-m <sanitized>``) operates on the
                # final-shape message that already has both trailers. The
                # prompt is the primary defense; this is the backstop.
                #
                # Issue #342: deliberately scoped to the first-run path.
                # On the existing-PR (BLOCKED-retry) branch we are NOT
                # pushing anything new — the commits being attributed
                # are already on origin from the prior dispatch — so the
                # amend backstops are no-ops at best and destructive at
                # worst.
                _ensure_provenance_trailers(
                    worktree_path=wt_path,
                    operator=operator,
                    commits_made_count=len(llm_output.commits_made),
                    role_token=worker_token,
                )
                # foreman#63 runtime defense (Phase 8d.22): scrub the HEAD
                # commit message of any GitHub auto-close keyword + issue
                # reference BEFORE Python pushes the branch. If a Worker
                # commit body contains ``Closes #N``, merging the impl PR
                # auto-closes the issue via the commit-body route, bypassing
                # the v4 state machine's close-out gate. The prompt is the
                # primary defense; this is the backstop. Limited to the
                # single-commit case (the default per ``<commit_discipline>``);
                # multi-commit runs log a warning and skip the amend to
                # avoid destructive rebases on shared history.
                #
                # Issue #342: also gated to the first-run path for the
                # same "no new commits being pushed" reason as the
                # provenance amend above.
                _sanitize_head_commit_auto_close(
                    worktree_path=wt_path,
                    commits_made_count=len(llm_output.commits_made),
                    role_token=worker_token,
                )
                # foreman#222: deterministically push the impl branch from
                # Python via host.push_branch (tokenized URL using the
                # worker installation token). Previously this was delegated
                # to the LLM's Bash via the prompt, but the container has
                # no git credential helper so Claude's `git push` fails
                # with a 401-shaped "could not read Username" whenever a
                # manual gh-auth-setup-git hasn't been done. Now Python
                # pushes deterministically using the same mechanism
                # Planner uses (see git_hosts/github.py:push_branch).
                host.push_branch(worktree_path=wt_path, branch=impl_branch_name)
                # foreman#175: belt-and-suspenders verify that the push
                # actually landed before opening the PR. Recovers via the
                # same host.push_branch path if the primary attempt above
                # somehow didn't take.
                _verify_impl_branch_remote_state(
                    repo,
                    branch=impl_branch_name,
                    worktree_path=wt_path,
                    role_token=worker_token,
                    host=host,
                )
                impl_pr = _create_pull_with_base_fallback(
                    repo,
                    title=llm_output.pr_title,
                    body=llm_output.pr_body,
                    base=wt_result.base_branch,
                    head=impl_branch_name,
                )
                pr_url = impl_pr.html_url
            else:
                # Issue #342: BLOCKED-retry idempotency. An open impl PR
                # already exists for ``foreman/impl-<N>`` — a prior
                # Worker dispatch opened it and is still polling its
                # CI status. Re-running push + ``create_pull`` would
                # 422 ("A pull request already exists ...") and crash
                # the subprocess. Skip the create surface entirely,
                # take the existing PR's html_url, and override
                # ``final_did_check_pass`` from its GitHub-reported
                # ``mergeable_state`` — the in-worktree
                # ``_run_check_command`` rerun above is unreliable as a
                # proxy for "is the impl PR's CI green" because the
                # worktree was freshly created from origin/<dev_base>
                # for this dispatch. The PR's ``mergeable_state`` is
                # the answer we actually want; ``"clean"`` / ``"unstable"``
                # mean CI is green, anything else means still in flight
                # or failing.
                _log.info(
                    "Worker BLOCKED retry: existing impl PR #%d found "
                    "for branch %r; skipping push+create_pull",
                    existing_impl_pr.number,
                    impl_branch_name,
                )
                pr_url = existing_impl_pr.html_url
                final_did_check_pass = (
                    existing_impl_pr.mergeable_state in CI_PASSING_MERGEABLE_STATES
                )
        elif final_outcome == "spec_invalid":
            # D6: post the spec_invalid_reason as a comment on the SPEC PR
            # (not the issue). The Worker's branch is left in place (commits,
            # if any, stay) but no impl PR is opened — the spec must be
            # re-fixed before the Worker tries again.
            assert llm_output.spec_invalid_reason is not None
            if spec_pr is not None:
                spec_pr.create_issue_comment(body=llm_output.spec_invalid_reason)
            else:
                _log.warning(
                    "Worker emitted spec_invalid but no open spec PR was found; rationale: %s",
                    llm_output.spec_invalid_reason,
                )

        # The Worker does not mutate labels. Under v4,
        # ``LabelObservabilityObserver`` owns every ``foreman:*`` write
        # off state-machine transitions; ``final_labels`` here is just
        # the post-call snapshot returned for the daemon's audit trail.
        final_labels = sorted({label.name for label in issue.labels})

        # Stats logging — write regardless of outcome. The orchestrator's
        # post-verification truth is what we persist (final_outcome +
        # final_did_check_pass), not the Worker's original self-report.
        # The audit log reads as "what actually happened," not "what the
        # Worker thought happened."
        #
        # pr_number on the stats row is the IMPL PR number when we opened
        # one (implemented branch above), or None otherwise. We intentionally
        # do NOT log the spec PR number here — that's a different artifact
        # in a different audit dimension and conflating them would make
        # downstream queries lie. The spec PR is the Fixer's lane.
        impl_pr_number: int | None = None
        if pr_url:
            try:
                impl_pr_number = int(pr_url.rsplit("/", 1)[-1])
            except ValueError:
                impl_pr_number = None
        skipped_hist = _skipped_by_reason_histogram(llm_output)

        # foreman#367: post the operator-visible escalation comment
        # BEFORE log_worker_run so a comment-post failure is visible
        # in the daemon log without preventing the JSONL row. The
        # helper catches host.post_issue_comment failures and returns
        # False; we do not branch on the return because the
        # success-path telemetry write must proceed unconditionally.
        if final_outcome in ("incomplete", "spec_invalid"):
            state_instance_id = os.environ.get(
                "FOREMAN_STATE_INSTANCE_ID",
                "unknown",
            )
            dedup_key = f"state-instance-{state_instance_id}-attempt-{attempt}-{final_outcome}"
            fallback_reason: str | None = None
            if llm_output.escalation_comment is None:
                fallback_reason = (
                    f"worker LLM produced outcome={final_outcome} but did "
                    "not populate escalation_comment"
                )
            post_escalation_comment(
                host=host,
                repo_slug=actual_repo_slug,
                issue_number=issue_number,
                role="worker",
                outcome_label=final_outcome,
                summary=llm_output.work_comment[:500] or final_outcome,
                payload=llm_output.escalation_comment,
                fallback_reason=fallback_reason,
                source="role:worker",
                key=dedup_key,
            )

        log_worker_run(
            repo_slug=actual_repo_slug,
            issue_number=issue_number,
            pr_number=impl_pr_number,
            attempt=attempt,
            outcome=final_outcome,
            total_sub_requests=(
                len(llm_output.implemented_sub_requests) + len(llm_output.skipped_sub_requests)
            ),
            implemented_count=len(llm_output.implemented_sub_requests),
            skipped_count=len(llm_output.skipped_sub_requests),
            skipped_by_reason=skipped_hist,
            did_check_pass=final_did_check_pass,
            confidence=llm_output.confidence,
            duration_seconds=duration_seconds,
            baseline_failures_count=len(baseline_failures),
            new_failures_count=len(new_failures),
            # foreman#227: provider-reported usage from the ResultMessage.
            # On the provider-error path above, ``usage`` is a zeroed
            # UsageInfo (we never saw a ResultMessage) — JSONL still
            # carries consistent fields.
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            # foreman#244: prompt-cache token counters from the SDK
            # (billed at 25% / 10% of regular input rate). Without
            # these the JSONL per-token columns drift from the
            # SDK-computed total_cost_usd on multi-turn loops where
            # prompt caching is the default.
            cache_creation_input_tokens=usage.cache_creation_input_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
            total_cost_usd=usage.total_cost_usd,
            model_usage=usage.model_usage,
            duration_ms=usage.duration_ms,
            num_turns=usage.num_turns,
        )
        # Discard the combined-output buffer from the post-check run so it
        # doesn't sit in memory across the rest of the lifetime of the
        # async result (the orchestrator returns to its caller after this).
        del post_output

        return WorkerRunResult(
            llm_output=llm_output,
            attempt=attempt,
            pr_url=pr_url,
            final_did_check_pass=final_did_check_pass,
            final_labels=final_labels,
        )
    except ProviderError as exc:
        # foreman#361: transient failures are retried by the state
        # machine with backoff; suppress the runaway-burn issue
        # comment so a 40-min outage does not carpet the issue with
        # redundant tracebacks. Combined with the inner-arm split
        # above (worker.py:1051), this re-raises ProviderTransientError
        # all the way out to run_worker_cli where the transient
        # catch arm emits the TRANSIENT_PROVIDER_ERROR outcome.
        if isinstance(exc, ProviderTransientError):
            raise
        # foreman#266: typed catch for the documented provider-boundary
        # failure mode. Same body as the ``except Exception`` arm
        # below — structural (type narrowing + boundary documentation),
        # not semantic.
        _on_failure(exc)
        raise
    except Exception as exc:
        # PR #255 commit 2 defensive handler — belt-and-suspenders for
        # non-provider failures (worktree ops, host I/O, GitHub 5xx).
        _on_failure(exc)
        raise


# Worker is the LAST role in the pipeline and the ONLY role that emits
# BLOCKED. BLOCKED semantics: the v4 state machine's
# ``ImplementingState.next_state`` returns a fresh ``ImplementingState()``
# on BLOCKED so the Worker gets re-dispatched on the next tick.
# Worker is also NOT target-aware — the impl PR is unambiguous from the
# issue number, so ``run_worker_cli`` has no ``target`` kwarg.

import asyncio  # noqa: E402

from foreman.providers import make_provider  # noqa: E402
from foreman.v4.config import load_config as load_v4_config  # noqa: E402
from foreman.v4.config import load_projects as load_v4_projects  # noqa: E402
from foreman.v4.emit import emit_outcome  # noqa: E402
from foreman.v4.outcome import (  # noqa: E402
    Outcome,
    OutcomeArtifacts,
    OutcomeConfidence,
    OutcomeKind,
)

_DEFAULT_V4_CONFIG = Path.home() / ".foreman" / "v4" / "config.toml"
_DEFAULT_PROJECTS_PATH = Path.home() / ".foreman" / "projects.toml"


class _V4WorkerResult:
    """Flat-shape result for the v4 emit path.

    The legacy ``WorkerRunResult`` nests outcome under
    ``llm_output.outcome`` ("implemented" / "incomplete" / "spec_invalid")
    with a ``pr_url``, an attempt counter, and the orchestrator-verified
    ``final_did_check_pass``. The v4 emit path consumes a coarser
    ``status`` field — one of ``"ci_passing"`` / ``"ci_in_flight"`` /
    ``"give_up"`` — so ``run_worker_cli`` can pick CLEAN / BLOCKED /
    NEEDS_HELP without re-interpreting legacy shape.

    Status mapping (computed in ``_run_worker_for_v4``):

    - ``implemented`` + impl PR opened + check passed → ``ci_passing``
      (CLEAN). The orchestrator's post-Worker re-run of
      ``check_command`` is the "CI" surface for v3 — when it passed,
      the impl PR is green and ready for Reviewer dispatch.
    - ``implemented`` + impl PR opened but check did NOT pass →
      ``ci_in_flight`` (BLOCKED). The Worker landed code but CI hasn't
      converged; the v4 state machine re-polls. In practice the
      orchestrator's check-rerun already overrides ``implemented`` →
      ``incomplete`` when new failures appear, so this branch is rare;
      kept for the contract.
    - ``incomplete`` / ``spec_invalid`` → ``give_up`` (NEEDS_HELP).
      The Worker self-reported it couldn't finish (or the spec is
      invalid); operator triage required.

    Both this class and the helper that builds instances disappear in
    Phase 8.
    """

    def __init__(
        self,
        *,
        status: str,
        pr_number: int | None,
        summary: str,
        details: dict[str, object] | None = None,
    ) -> None:
        self.status = status
        self.pr_number = pr_number
        self.summary = summary
        # Phase 8d.17 / foreman#315: diagnostic detail bag the v4 CLI
        # forwards to Outcome.details. Populated by
        # ``_run_worker_for_v4`` from the WorkerOutput LLM fields the
        # state machine would otherwise drop at the v3→v4 flatten point.
        self.details: dict[str, object] = details if details is not None else {}


def _run_worker_for_v4(*, project: str, issue_number: int) -> _V4WorkerResult:
    """Run the worker end-to-end for a v4 caller.

    Wraps :func:`_run_worker_core` (worktree create → LLM dispatch →
    check rerun → impl PR open, formerly ``run_worker``) and flattens
    its result into the v4 shape consumed by ``run_worker_cli``. The v4
    state machine drives off the FOREMAN_OUTCOME emitted below, not off
    GitHub labels.

    Worker is NOT target-aware (unlike Reviewer/Fixer): the impl PR is
    unambiguous from the issue number — only one impl branch per
    issue. So no ``target`` kwarg here.
    """
    cfg_path = Path(os.environ.get("FOREMAN_V4_CONFIG", _DEFAULT_V4_CONFIG))
    cfg = load_v4_config(cfg_path)
    # issue #477: projects now live in the host-mounted projects file
    # (FOREMAN_PROJECTS_PATH), not in config.toml (which ships with zero
    # [[projects]] tables).  Only fall back to the projects file when
    # cfg.projects is empty — tests that mock load_v4_config to return a
    # cfg with projects already populated take the cfg.projects path.
    if cfg.projects:
        all_projects = cfg.projects
    else:
        projects_path = Path(os.environ.get("FOREMAN_PROJECTS_PATH", str(_DEFAULT_PROJECTS_PATH)))
        all_projects = load_v4_projects(projects_path) if projects_path.exists() else []
    project_cfg = next((p for p in all_projects if p.name == project), None)
    if project_cfg is None:
        known = [p.name for p in all_projects]
        raise ValueError(
            f"project {project!r} not found in V4Config at {cfg_path}. Known projects: {known}"
        )

    # Patch the config's projects list so _run_worker_core's lookup
    # (which reads config.projects) finds the project even when cfg was
    # loaded from a config.toml that has zero [[projects]] tables.
    if not cfg.projects:
        cfg = cfg.model_copy(update={"projects": all_projects})

    # The core takes an issue URL — v4's SubprocessRoleDispatcher only
    # knows the issue number. Construct the URL from the project's
    # configured repo slug.
    issue_url = f"https://github.com/{project_cfg.repo}/issues/{issue_number}"

    worktrees_root = Path(
        os.environ.get(
            "FOREMAN_WORKTREES_ROOT",
            str(Path.home() / ".foreman" / "worktrees"),
        )
    )
    provider = make_provider()
    registry = V4IdentityRegistry(
        apps=cfg.apps,
        orchestrator=cfg.orchestrator,
        installation_repo=project_cfg.repo,
    )
    core_result = asyncio.run(
        _run_worker_core(
            issue_url=issue_url,
            config=cfg,
            project_name=project,
            worktrees_root=worktrees_root,
            provider=provider,
            identity_registry=registry,
        )
    )

    # Flatten legacy → v4 shape. The v3 ``run_worker`` runs the project's
    # ``check_command`` as belt-and-suspenders post-LLM verification —
    # ``final_did_check_pass`` is the orchestrator's ground truth, not
    # the Worker's self-report. For the v4 surface:
    #
    # - ``implemented`` + ``pr_url`` + check passed → CI passing
    # - ``implemented`` + ``pr_url`` but check did NOT pass → CI still
    #   in flight (rare — see _V4WorkerResult docstring)
    # - anything else (``incomplete`` / ``spec_invalid``) → give-up
    llm = core_result.llm_output
    pr_url = core_result.pr_url
    pr_number: int | None = None
    if pr_url:
        try:
            pr_number = int(pr_url.rsplit("/", 1)[-1])
        except ValueError:
            pr_number = None

    if llm.outcome == "implemented" and pr_url is not None:
        if core_result.final_did_check_pass:
            status = "ci_passing"
            summary = "impl PR open, check passed"
        else:
            status = "ci_in_flight"
            summary = "impl PR open, check still in flight"
    else:
        status = "give_up"
        summary = f"{llm.outcome} (attempt {core_result.attempt})"

    # Phase 8d.17 / foreman#315: preserve WorkerOutput's diagnostic
    # detail by lifting onto the v4 Outcome. Before this, NEEDS_HELP
    # outcomes carried only the terse summary and the actual cause
    # (e.g., "algokit's Justfile has no `check` recipe") lived only on
    # the in-memory WorkerOutput that was dropped at the v3→v4
    # flatten point. Operators had to spelunk worktrees + stats jsonl
    # to recover it. Now the detail rides forward on the Outcome.
    # ``model_dump(mode="json")`` on the sub-request / commit lists
    # converts pydantic nested objects into JSON-safe dicts so the
    # ``Outcome.details: dict[str, Any]`` round-trips through
    # ``model_dump_json`` cleanly.
    details: dict[str, object] = {
        "work_comment": llm.work_comment,
        "did_check_pass": core_result.final_did_check_pass,
        "check_output_summary": llm.check_output_summary,
        "confidence": llm.confidence,
        "outcome": llm.outcome,
        "attempt": core_result.attempt,
        "commits_made": [c.model_dump(mode="json") for c in llm.commits_made],
        "implemented_sub_requests": [
            s.model_dump(mode="json") for s in llm.implemented_sub_requests
        ],
        "skipped_sub_requests": [s.model_dump(mode="json") for s in llm.skipped_sub_requests],
    }

    return _V4WorkerResult(
        status=status,
        pr_number=pr_number,
        summary=summary,
        details=details,
    )


def run_worker_cli(*, project: str, issue_number: int) -> int:
    """v4 CLI entry-point. Emits FOREMAN_OUTCOME JSON; returns exit code.

    Worker has 4 outcome paths (the only role with this many):

    - ``ci_passing`` → CLEAN with ``pr_number`` on artifacts
    - ``ci_in_flight`` → BLOCKED with ``pr_number`` (state machine re-polls)
    - ``give_up`` → NEEDS_HELP (operator triage)
    - exception → ERROR (exit 1)
    """
    if os.environ.get("FOREMAN_DRY_RUN") == "1":
        # Short-circuit for the Task 8.6 real-fork integration test. Emits
        # a canned CLEAN outcome without any provider / GitHub / worktree
        # work. The chain being exercised here is typer → role entry →
        # emit_outcome → parser → exit code — the role's actual work is
        # out of scope for this test path.
        emit_outcome(
            Outcome(
                kind=OutcomeKind.CLEAN,
                confidence=OutcomeConfidence.HIGH,
                summary="dry-run",
            )
        )
        return 0
    try:
        result = _run_worker_for_v4(project=project, issue_number=issue_number)
    except ProviderTransientError as exc:
        # foreman#361: classify Anthropic-side transient failures so
        # the state machine's RoleDispatchState Template Method can
        # schedule an exponential-backoff retry without burning the
        # max_state_attempts cap. Shared helper for the body — see
        # :func:`foreman.roles.emit_transient_provider_outcome`.
        return emit_transient_provider_outcome(exc)
    except Exception as exc:
        emit_outcome(
            Outcome(
                kind=OutcomeKind.ERROR,
                confidence=OutcomeConfidence.HIGH,
                summary=f"worker raised: {exc}"[:500],
            )
        )
        return 1

    status = getattr(result, "status", None)
    artifacts = OutcomeArtifacts(pr_number=getattr(result, "pr_number", None))
    # Phase 8d.17 / foreman#315: forward Worker diagnostic detail onto
    # every emitted Outcome (CLEAN, BLOCKED, NEEDS_HELP). The
    # isinstance check keeps the contract backward-compatible — older
    # test doubles (MagicMock) that don't set ``details`` explicitly
    # produce a non-dict attribute by default; we ignore it and emit
    # an empty bag rather than fail pydantic validation.
    raw_details = getattr(result, "details", None)
    details: dict[str, object] = raw_details if isinstance(raw_details, dict) else {}

    if status == "ci_passing":
        emit_outcome(
            Outcome(
                kind=OutcomeKind.CLEAN,
                confidence=OutcomeConfidence.HIGH,
                summary=getattr(result, "summary", None) or "impl PR open, CI green",
                artifacts=artifacts,
                details=details,
            )
        )
        return 0
    if status == "ci_in_flight":
        emit_outcome(
            Outcome(
                kind=OutcomeKind.BLOCKED,
                confidence=OutcomeConfidence.HIGH,
                summary=getattr(result, "summary", None) or "impl PR open, CI in flight",
                artifacts=artifacts,
                details=details,
            )
        )
        return 0
    if status == "give_up":
        emit_outcome(
            Outcome(
                kind=OutcomeKind.NEEDS_HELP,
                confidence=OutcomeConfidence.HIGH,
                summary=getattr(result, "summary", None) or "worker hit give-up condition",
                artifacts=artifacts,
                details=details,
            )
        )
        return 0
    # Unknown status — surface as ERROR so the state machine doesn't
    # silently advance on a contract violation.
    emit_outcome(
        Outcome(
            kind=OutcomeKind.ERROR,
            confidence=OutcomeConfidence.HIGH,
            summary=f"unknown worker status: {status}",
        )
    )
    return 1
