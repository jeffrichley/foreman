"""Worker role dispatcher — 4th pipeline node, the implementer stage.

The Worker LLM consumes a plan-approved issue + spec doc and implements the
described code change. It commits + pushes to a stacked impl branch
(``foreman/impl-<N>``, based on ``foreman/issue-<N>``) and returns a
:class:`~foreman.schemas.worker.WorkerOutput`. Foreman core then:

  1. Re-runs ``check_command`` independently after the Worker returns,
     as belt-and-suspenders ground truth (D4 in the build brief). If
     new test failures appeared that weren't in the baseline, the
     Worker's ``implemented`` claim is overridden to ``incomplete``.
  2. Branches on the (post-verification) outcome:

     - ``implemented`` → opens the impl PR via PyGithub with
       ``base=wt_result.base_branch`` — either the spec branch (D1,
       stacked PR) or the default branch (fallback when the spec
       branch is gone, issue #48). Advances issue label: clears
       ``foreman:plan-approved`` (the entry label), adds
       ``foreman:impl-review``. Per-episode counter reset: drops all
       ``foreman:impl-attempt-N`` labels (the implementation episode
       closed cleanly; a future re-trigger gets a fresh 3-attempt
       budget). Clears ``needs-help`` if it was present.
     - ``incomplete`` → no impl PR opened. Adds ``needs-help`` to the
       issue. If this was the 3rd attempt, also adds ``failed``. The
       Worker's commits + push are kept for audit + future-Fixer
       resume.
     - ``spec_invalid`` → no impl PR opened. Posts the LLM's
       ``spec_invalid_reason`` as a comment on the SPEC PR (NOT the
       issue — per D6). Relabels the issue: remove
       ``foreman:plan-approved``, add ``foreman:spec-fix`` +
       ``foreman:needs-help``. This forces the spec back through a
       Fixer + Reviewer cycle.

  3. Appends a JSONL line to
     ``~/.foreman/stats/<owner>__<repo>/worker.jsonl`` for the
     lifecycle audit log. The ``outcome`` and ``did_check_pass`` fields
     logged are the FINAL truth (after orchestrator override), not the
     Worker's original self-report.

The impl-attempt counter is tracked via the issue's
``foreman:impl-attempt-N`` labels (set on entry, never removed
mid-episode). Max 3 attempts; the 4th raises before any LLM dispatch.

Tool surface: Read / Grep / Glob / Bash / Edit / Write. Bash is needed
so the LLM can run ``check_command`` itself, ``git add`` / ``git commit``
/ ``git push`` directly. Same tool matrix as the Fixer — the Worker is
the second role in the walking skeleton that mutates the worktree
directly.

Pre-flight guards: if the issue is missing ``foreman:plan-approved`` we
refuse to run; if the issue already has 3 ``foreman:impl-attempt-*``
labels, we refuse with a clear "needs human intervention" RuntimeError.
Both raise before any LLM dispatch — cheap deterministic checks.

Reserved (NOT yet honored, documented for the next ticket):
- ``foreman:auto-merge-spec`` label name — D7's future hook: when set
  on the issue, the Worker will merge the spec PR before opening the
  impl PR, breaking the stacked dependency.
- ``ProjectConfig.auto_merge_spec`` field — same purpose, project-wide
  default for the above label.

v3 Worker triggers on ``foreman:plan-approved`` regardless of spec PR
merge state. The stacked PR shape (D1) handles the dependency; a
follow-up ticket retargets the impl PR's base to the repo default
when the spec PR merges.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path

from github import Github
from github.GithubException import GithubException
from github.Issue import Issue
from github.PullRequest import PullRequest
from github.Repository import Repository

from foreman.branches import impl_branch, spec_branch
from foreman.config import Config
from foreman.dispatch_recorder import DispatchRecorder, emit_recorder_complete
from foreman.git_host import GitHostProvider
from foreman.identity import IdentityRegistry
from foreman.instructions import load_project_instructions
from foreman.provider import ProviderFacade, UsageInfo
from foreman.providers import ProviderError
from foreman.roles import TERMINAL_BLOCKING_LABEL, handle_unhandled_role_exception
from foreman.schemas.worker import WorkerOutput, WorkerRunResult
from foreman.stats import log_worker_run
from foreman.worktree import WorktreeManager

_log = logging.getLogger(__name__)

_ISSUE_URL_RE = re.compile(
    r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)"
)
_IMPL_ATTEMPT_RE = re.compile(r"^foreman:impl-attempt-(\d+)$")
# Anchored at line start to keep header noise like
# ``FAILED tests/test_x.py::test_y`` clean of leading prose. pytest's
# short-summary lines start with literal ``FAILED `` at column 0.
_PYTEST_FAILED_RE = re.compile(r"^FAILED\s+(?P<test_id>\S+)", flags=re.MULTILINE)

# Tool capabilities matrix for the Worker. Edit + Write so it can write
# code; Bash so it can run check_command + stage/commit/push from inside
# the worktree. Read / Grep / Glob for navigation and verification.
WORKER_ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Bash", "Edit", "Write"]

# Labels the Worker touches on the originating issue. v3 vocabulary:
# the Worker entry label is ``foreman:plan-approved`` (Reviewer signoff
# on the spec PR sets it). There is no in-flight ``implementing`` label
# in v3 — execution-log + impl-attempt-N labels carry that state.
_LABEL_PLAN_APPROVED = "foreman:plan-approved"
_WORKER_ENTRY_LABELS = frozenset({_LABEL_PLAN_APPROVED})


class _WorkerPreflightRefusal(RuntimeError):
    """Worker refused to proceed because either (a) the issue lacks the
    expected entry label (``foreman:plan-approved``), or (b) the
    impl-attempt counter has hit ``max_impl_attempts``.

    Subclass of :class:`RuntimeError` so existing callers'
    ``except RuntimeError`` clauses and ``pytest.raises(RuntimeError,
    match=...)`` patterns work unchanged. The distinguishing purpose is
    to tell the runaway-burn defense (#229 helper invocation) to SKIP
    firing: both refusals are deliberate (operator removed the label,
    or the attempt budget was the human-intervention escalation
    surface) — NOT a runaway signal. Firing the helper would override
    operator intent by re-adding ``foreman:needs-help``.
    """


_LABEL_IMPL_REVIEW = "foreman:impl-review"
_LABEL_SPEC_FIX = "foreman:spec-fix"
_LABEL_NEEDS_HELP = "foreman:needs-help"
_LABEL_FAILED = "foreman:failed"

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


def _count_impl_attempts(label_names: set[str]) -> int:
    """Return the highest existing impl-attempt counter (0 if none).

    Reads existing ``foreman:impl-attempt-N`` labels and returns the
    largest N. The new attempt is ``_count_impl_attempts(...) + 1``.
    """
    attempts: list[int] = []
    for name in label_names:
        m = _IMPL_ATTEMPT_RE.match(name)
        if m:
            attempts.append(int(m.group(1)))
    return max(attempts) if attempts else 0


def _load_worker_prompt() -> str:
    """Load the Worker system prompt: four vendored superpowers skills
    followed by the Foreman-specific Worker contract.

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


def _find_spec_pr(repo: Repository, owner: str, branch: str) -> PullRequest | None:
    """Locate the open spec PR whose head branch matches ``branch``.

    The Planner names spec branches ``foreman/issue-<N>``. We query
    open PRs by head (``owner:branch``) and take the first match.

    Returns ``None`` if no open spec PR is found. The Worker proceeds
    anyway in this case — the implementation can still happen against
    the spec doc on the spec branch; the only thing missing is the PR
    number for the impl PR body's "Spec PR: #<N>" reference (which we
    leave as a placeholder). Posting a spec_invalid_reason without a
    target PR is harmless (we skip the post and log a warning); the
    issue's label transition still happens.
    """
    head_qualifier = f"{owner}:{branch}"
    pulls = list(repo.get_pulls(state="open", head=head_qualifier))
    if not pulls:
        return None
    return pulls[0]


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
    """Belt-and-suspenders for foreman#175 — verify the impl branch is
    on the remote with local HEAD before ``_create_pull_with_base_fallback``
    runs.

    Worker pushes the impl branch from Python via ``host.push_branch``
    (the authenticated tokenized-URL path) in :func:`run_worker` after
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
    """Open a PR; on a 422 "base invalid" error, retry against the
    repo's default branch.

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


def _label_names(issue_labels: set[str], extra: str | None = None) -> set[str]:
    """Helper: union with an optional extra label, returning the result.

    Used to compose the "all labels known at this point" set without
    mutating the caller's view.
    """
    if extra is None:
        return set(issue_labels)
    return issue_labels | {extra}


async def run_worker(
    *,
    issue_url: str,
    config: Config,
    project_name: str,
    worktrees_root: Path,
    provider: ProviderFacade,
    identity_registry: IdentityRegistry | None = None,
    dispatch_recorder: DispatchRecorder | None = None,
    dispatch_trace_id: int | None = None,
) -> WorkerRunResult:
    """Run the Worker role end-to-end on one spec-ready issue.

    Args:
        issue_url: Full GitHub ISSUE URL
            (``https://github.com/owner/repo/issues/N``) — NOT the spec
            PR URL. The Worker is triggered by the
            ``foreman:plan-approved`` label on the issue and derives the
            spec PR from the issue's ``foreman/issue-<N>`` branch.
        config: Loaded foreman config.
        project_name: Key into ``config.projects``.
        worktrees_root: Root directory under which per-ticket worktrees
            live. The Worker uses a sibling ``impl-<N>/`` worktree
            distinct from the spec-side ``issue-<N>/``.
        provider: Agent provider facade (e.g., AnthropicSDKProvider).
        identity_registry: Optional pre-built registry; defaults to a
            fresh :class:`~foreman.identity.IdentityRegistry` for the
            project. Tests inject a fake registry to bypass real App
            auth.

    Returns:
        A :class:`~foreman.schemas.worker.WorkerRunResult` bundling the
        Worker LLM's :class:`~foreman.schemas.worker.WorkerOutput`, the
        attempt counter, the opened impl PR URL (iff ``implemented``),
        and the orchestrator-verified ``final_did_check_pass``.

    Raises:
        ValueError: Issue URL malformed or repo mismatch.
        RuntimeError: Issue missing ``foreman:plan-approved``, or max
            attempts (3) already reached.
    """
    # Post-adversarial-review (#1): wrap the initial setup — URL parse,
    # project lookup, identity registry, host acquisition, ``repo.get_issue``
    # — in a defensive try block so a transient failure (auth-token
    # rotation, GitHub 5xx, malformed URL via daemon misconfig) fires
    # the runaway-burn helper on the FIRST failure instead of letting
    # #228's rate-limit catch it at N=3. The body's own ``try:`` /
    # ``except`` / ``finally`` further down covers the in-flight phase
    # plus the label-revert invariant; this wrap covers everything
    # before that. Refusals (missing entry label, max-attempts) raise
    # ``_WorkerPreflightRefusal`` and are not caught here.
    _setup_issue_number: int | None = None
    _setup_repo_slug: str | None = None
    _setup_issue: Issue | None = None
    try:
        owner, repo_name, issue_number = parse_issue_url(issue_url)
        _setup_issue_number = issue_number
        project = config.projects[project_name]
        expected_repo_slug = project.repo
        actual_repo_slug = f"{owner}/{repo_name}"
        _setup_repo_slug = actual_repo_slug
        if expected_repo_slug != actual_repo_slug:
            raise ValueError(
                f"Issue URL repo {actual_repo_slug!r} does not match project "
                f"{project_name!r} configured repo {expected_repo_slug!r}"
            )

        registry = identity_registry if identity_registry is not None else IdentityRegistry(project)
        worker_client: Github = registry.get_worker_client()
        worker_token: str = registry.get_worker_token()
        # foreman#222: acquire the authenticated GitHostProvider so we can
        # push from Python via the tokenized-URL path that Planner already
        # uses. The container has no git credential helper, so shell-out
        # ``git push`` + Claude's Bash both fail auth.
        host: GitHostProvider = registry.get_host_provider("worker")

        repo: Repository = worker_client.get_repo(actual_repo_slug)
        issue = repo.get_issue(issue_number)
        _setup_issue = issue
        issue_labels = {label.name for label in issue.labels}
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
                set_needs_help_label=lambda: bound_issue.add_to_labels(TERMINAL_BLOCKING_LABEL),
            )
        raise

    # Pre-flight: refuse to run without the v3 Worker entry label.
    # ``foreman:plan-approved`` is the post-Reviewer-signoff queue marker;
    # the v3 reconciler dispatches the Worker once this label is on the
    # issue and the spec PR is merged (or stacked).
    if not (issue_labels & _WORKER_ENTRY_LABELS):
        # Graceful refusal — operator removed the entry label or it was
        # never set. Use the marker subclass so the body's except branch
        # (when wrapped via the helper-invocation guard) skips the
        # runaway-burn helper and respects the operator's intent.
        raise _WorkerPreflightRefusal(
            f"Issue #{issue_number} does not carry the Worker entry label "
            f"({_LABEL_PLAN_APPROVED!r}); "
            f"labels: "
            + ", ".join(sorted(issue_labels) or ["<none>"])
            + ". The Worker only acts on issues queued by the Reviewer "
            "(plan-approved set after spec-PR signoff)."
        )

    # Pre-flight: max-attempts gate. If max impl-attempt labels already
    # exist, refuse to run — this prevents infinite-loop drain and
    # forces human intervention via the foreman:failed escalation.
    # The cap is read from ``ProjectConfig.max_impl_attempts`` so each
    # project can size it to their own appetite (default 3).
    max_impl_attempts = project.max_impl_attempts
    previous_attempts = _count_impl_attempts(issue_labels)
    attempt = previous_attempts + 1
    if attempt > max_impl_attempts:
        # Graceful refusal — the max-attempts gate IS the escalation
        # surface to human intervention. Don't fire the runaway-burn
        # helper; the operator's audit trail (attempt-N labels) is
        # already the diagnostic.
        raise _WorkerPreflightRefusal(
            f"Issue #{issue_number} has hit the max {max_impl_attempts} "
            "impl-attempts; needs human intervention via foreman:failed. "
            f"Existing attempts: {previous_attempts}."
        )

    # foreman#91: track the in-process label set parallel to each
    # remote add/remove so the role can return the authoritative
    # post-transition set in ``WorkerRunResult.final_labels``. Avoids
    # the eventual-consistency race of a post-mutation host re-read.
    current_labels: set[str] = set(issue_labels)

    # Stamp the new attempt label + clear the entry label atomically
    # BEFORE the LLM dispatch. Audit-trail before audit-loss — the
    # attempt marker must be visible even if the LLM dispatch crashes
    # mid-run. Adversarial review MEDIUM #12: this is done as a single
    # ``set_labels(...)`` call (atomic PUT /issues/{N}/labels) so a
    # crash cannot leave the issue half-transitioned (e.g., entry label
    # cleared but attempt label not yet applied), which would drop it
    # out of the v3 observer's GraphQL ``filterBy.labels`` filter and
    # silently stall the pipeline. v3 does NOT use an explicit
    # ``implementing`` label — the attempt counter + exec log are the
    # in-flight signal, and adding a transient label would re-dispatch
    # on reconciler tick.
    #
    # Namespace-scoped merge (Pass 2 HIGH): re-read labels at the WRITE
    # site so any operator-added label (``priority:high``,
    # ``needs:design``) AND any foreman label the role isn't touching
    # (e.g., ``foreman:hold``) passes through. The role's verdict
    # explicitly names the foreman labels it is removing
    # (``removed_foreman`` = entry labels) and adding (``added_foreman``
    # = attempt counter); everything else survives.
    attempt_label = f"foreman:impl-attempt-{attempt}"
    current_labels.add(attempt_label)
    for _entry_label in _WORKER_ENTRY_LABELS:
        current_labels.discard(_entry_label)
    removed_foreman_pre = set(_WORKER_ENTRY_LABELS)
    added_foreman_pre = {attempt_label}
    # Pass 3 CRITICAL: PyGithub's ``Issue.labels`` is a cached property —
    # the snapshot taken at the top of ``run_worker`` (when we read
    # ``issue_labels`` for the pre-flight gate) would otherwise be reused
    # here. Call ``issue.update()`` (PyGithub's documented conditional
    # GET, see ``.venv/Lib/site-packages/github/GithubObject.py:638``)
    # to invalidate the cache so this read reflects the real remote
    # state. The pre-LLM site's race window is short, but the rule
    # — refresh BEFORE every namespace-scoped merge — is what makes the
    # post-LLM site below correct, and uniformity catches future drift.
    issue.update()
    current_label_names_pre = {label.name for label in issue.labels}
    pre_dispatch_labels = sorted(
        (current_label_names_pre - removed_foreman_pre) | added_foreman_pre
    )
    issue.set_labels(*pre_dispatch_labels)

    # Crash-safety: if anything below raises before the final outcome
    # set_labels at line ~876, the issue would be left with
    # foreman:impl-attempt-N but without foreman:plan-approved, and no rule
    # in the v3 catalog matches that state. Revert the entry-label transition
    # so the next reconciler tick can re-dispatch via _plan_approved_no_impl_pr,
    # advancing count_completed each crash until _impl_attempts_exhausted fires.
    outcome_labels_committed = False
    # foreman#238: stamp ``start_time`` BEFORE the body wrap and
    # initialize ``usage`` to ``None`` so the except branch below can
    # log the worker_failed row with whatever partial state was
    # captured before the failure surfaced. ``WorktreeManager.create_impl``,
    # ``host.push_branch``, ``_create_pull_with_base_fallback``, and
    # the final ``issue.set_labels`` are all inside the wrap; pre-#238
    # any of those raising silently dropped the run's cost telemetry
    # because the success-path ``log_worker_run`` call below never
    # executed. (``provider.run_agent`` raising is already handled by
    # the D5 in-band recovery further down — synthesized as
    # ``outcome=incomplete`` — and is NOT a #238 case.)
    start_time = time.monotonic()
    usage: UsageInfo | None = None

    def _on_failure(exc: BaseException) -> None:
        """Shared cleanup body for the outer ``ProviderError`` +
        ``Exception`` catch arms (foreman#266 — type-narrowing split).

        Closes over ``start_time`` / ``usage`` / ``actual_repo_slug``
        / ``issue_number`` / ``attempt`` / ``project_name`` /
        ``dispatch_recorder`` / ``dispatch_trace_id`` / ``issue``.
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
        # foreman#251 (Phase 1): mirror the failure-path dual-write.
        emit_recorder_complete(
            dispatch_recorder=dispatch_recorder,
            dispatch_trace_id=dispatch_trace_id,
            role="worker",
            repo_slug=actual_repo_slug,
            ticket_id=f"{actual_repo_slug}#{issue_number}",
            project=project_name,
            issue_number=issue_number,
            pr_number=None,
            outcome="exception",
            usage=usage if usage is not None else UsageInfo(),
            role_data={
                "attempt": attempt,
                "total_sub_requests": 0,
                "implemented_count": 0,
                "skipped_count": 0,
                "skipped_by_reason": {},
                "did_check_pass": False,
                "confidence": "low",
                "baseline_failures_count": 0,
                "new_failures_count": 0,
            },
            duration_seconds=duration_seconds,
        )
        # foreman#229: runaway-burn defense.
        handle_unhandled_role_exception(
            role="worker",
            issue_number=issue_number,
            exc=exc,
            post_comment=lambda body: issue.create_comment(body),
            set_needs_help_label=lambda: issue.add_to_labels(TERMINAL_BLOCKING_LABEL),
        )

    try:
        # Resolve the spec branch + spec PR (PR may be None — implementation
        # proceeds either way; the impl PR body's spec-PR reference adapts).
        spec_branch_name = spec_branch(issue_number)
        impl_branch_name = impl_branch(issue_number)
        spec_pr = _find_spec_pr(repo, owner=owner, branch=spec_branch_name)
        spec_pr_number = spec_pr.number if spec_pr is not None else None

        # Resolve check_command per D2: project override or default.
        check_command = _resolve_check_command(project.check_command)

        # Create the Worker's stacked impl worktree. NOT ``create`` (would
        # branch from main) and NOT ``attach`` (would reuse the spec-side
        # ``issue-<N>/`` worktree and inherit any Fixer WIP state).
        # ``create_impl`` returns the worktree path AND the branch the impl
        # PR should target — usually the spec branch (D1 stacked PR), or
        # the default branch when the spec branch is gone (issue #48
        # fallback).
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
            repo_url=f"https://github.com/{project.repo}.git",
        )
        wt_path = wt_result.path

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
        # (push_branch, create_pull, set_labels) still records the
        # per-call token cost in the worker_failed row.
        try:
            llm_output, run_usage = await provider.run_agent(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                allowed_tools=WORKER_ALLOWED_TOOLS,
                output_model=WorkerOutput,
                cwd=wt_path,
                env={**os.environ, "GH_TOKEN": worker_token},
            )
            usage = run_usage
        except ProviderError as exc:
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
            )
            usage = UsageInfo()
        except Exception as exc:
            # D5: SDK errors (timeout, network, validation, anything) MUST NOT
            # crash the orchestrator silently. Synthesize an incomplete-shaped
            # output so the rest of the pipeline (label transitions, stats,
            # caller's CLI summary) still runs deterministically. The
            # exception message lives in work_comment + check_output_summary
            # so a human can diagnose without spelunking logs.
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
            # and the outcome stays `incomplete`. The label transitions
            # branch normally.
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

        # Branch on the FINAL outcome (after orchestrator override).
        #
        # Per-branch declaration of which foreman labels the role is
        # MUTATING this transition (Pass 2 HIGH — namespace-scoped merge).
        # ``removed_foreman_post`` + ``added_foreman_post`` capture the
        # role's intent; everything NOT in those sets — non-foreman labels
        # (``priority:high``) AND foreman labels the role isn't touching
        # (``foreman:hold``) — passes through. The actual set_labels call
        # re-reads the label set just before writing to minimize the
        # race window for operator-added labels during the LLM call.
        pr_url: str | None = None
        removed_foreman_post: set[str] = set()
        added_foreman_post: set[str] = set()
        if final_outcome == "implemented":
            # Open the impl PR, stacked on the spec branch (D1). The
            # PyGithub call gives us the new PR's html_url to return.
            # ``pr_title`` and ``pr_body`` are guaranteed non-None by the
            # WorkerOutput validator; the override path above only flips
            # `implemented` → `incomplete`, never the reverse.
            assert llm_output.pr_title is not None
            assert llm_output.pr_body is not None
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

            # Label transitions for the implemented outcome. Per-episode
            # counter reset: drop all impl-attempt-N labels + needs-help +
            # foreman:failed so any future re-trigger starts with a fresh
            # 3-attempt budget and a clean dashboard. (Pass 2 MEDIUM: without
            # the foreman:failed drop, an operator re-triggering a previously-
            # failed ticket leaves stale failed state on the issue after a
            # successful run.)
            # v3: no ``implementing`` label to clear — the entry label
            # ``foreman:plan-approved`` was already removed at dispatch.
            current_labels.add(_LABEL_IMPL_REVIEW)
            all_known_labels = _label_names(issue_labels, attempt_label)
            for _label_name in all_known_labels:
                if (
                    _label_name.startswith("foreman:impl-attempt-")
                    or _label_name == _LABEL_NEEDS_HELP
                    or _label_name == _LABEL_FAILED
                ):
                    current_labels.discard(_label_name)
            added_foreman_post.add(_LABEL_IMPL_REVIEW)
            # ``removed_foreman_post`` is computed at the WRITE site from
            # the freshly-read label set (impl-attempt-N matching is
            # pattern-based, so we resolve it against CURRENT remote state,
            # not the pre-LLM snapshot).
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
            # Entry label was already cleared at dispatch time; idempotent
            # re-clear in the in-memory set so the final ``set_labels`` call
            # below cleanly enters spec-fix regardless of how the Worker
            # came in (manual CLI vs daemon).
            for _entry_label in _WORKER_ENTRY_LABELS:
                current_labels.discard(_entry_label)
            current_labels.add(_LABEL_SPEC_FIX)
            current_labels.add(_LABEL_NEEDS_HELP)
            removed_foreman_post |= set(_WORKER_ENTRY_LABELS)
            added_foreman_post.add(_LABEL_SPEC_FIX)
            added_foreman_post.add(_LABEL_NEEDS_HELP)
        else:
            # incomplete: add needs-help so observers know to look. v3 does
            # not use an in-flight ``implementing`` label — the impl-attempt-N
            # marker carries cycle state. On the last attempt, also stamp
            # foreman:failed so the queue surfaces it for human triage.
            current_labels.add(_LABEL_NEEDS_HELP)
            added_foreman_post.add(_LABEL_NEEDS_HELP)
            if attempt == max_impl_attempts:
                current_labels.add(_LABEL_FAILED)
                added_foreman_post.add(_LABEL_FAILED)

        # Atomic label transition (adversarial review MEDIUM #12): apply the
        # full final ``current_labels`` set in one ``issue.set_labels(...)``
        # call (PUT /issues/{N}/labels). Replaces sequential
        # ``remove_from_labels`` + ``add_to_labels`` calls that could leave
        # the issue half-transitioned on a subprocess crash — silently
        # dropping it out of the v3 observer's GraphQL ``filterBy.labels``
        # filter.
        #
        # Namespace-scoped merge (Pass 2 HIGH): re-read labels NOW (not
        # from the pre-LLM snapshot) so any operator-added label
        # (``priority:high``, ``needs:design``) AND any foreman label the
        # role isn't touching (e.g., ``foreman:hold``) passes through. The
        # role's verdict is encoded in ``removed_foreman_post`` /
        # ``added_foreman_post``; everything else survives. Race window
        # shrinks from minutes (LLM duration) to API round-trip.
        #
        # Pass 3 CRITICAL: ``issue.labels`` is a cached property in PyGithub
        # — without ``issue.update()`` (conditional GET, see
        # ``.venv/Lib/site-packages/github/GithubObject.py:638``) the read
        # below returns the SAME snapshot we took at the top of
        # ``run_worker`` minutes ago. The minute-long LLM call would silently
        # drop any operator-added label (``priority:high`` etc.) — the very
        # bug this namespace-scoped merge exists to prevent. ``update()``
        # invalidates the labels cache so the next access re-fetches.
        issue.update()
        current_label_names_post = {label.name for label in issue.labels}
        if final_outcome == "implemented":
            # Resolve the "drop all impl-attempt-N + needs-help + failed"
            # rule against the CURRENT remote label set, not the pre-LLM
            # snapshot (operators may have added/removed attempt labels
            # during the LLM call — unlikely but the semantic is "clean
            # the episode from what's actually there now").
            removed_foreman_post = {
                n
                for n in current_label_names_post
                if (
                    n.startswith("foreman:impl-attempt-")
                    or n == _LABEL_NEEDS_HELP
                    or n == _LABEL_FAILED
                )
            }
        final_label_set = (current_label_names_post - removed_foreman_post) | added_foreman_post
        issue.set_labels(*sorted(final_label_set))
        outcome_labels_committed = True
        # Keep the in-process tracking aligned with what was actually
        # applied so the WorkerRunResult.final_labels matches reality.
        current_labels = final_label_set

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
        # foreman#251 (Phase 1): dual-write through the Recorder.
        # ``role_data`` carries the Worker-specific JSONL fields so
        # :class:`RoleStatsSubscriber` can fan out to
        # ``log_worker_run`` with matching values. The success-path
        # ``log_worker_run`` above stays for Phase 1 (see ticket
        # acceptance criterion); Phase 2 will collapse it.
        emit_recorder_complete(
            dispatch_recorder=dispatch_recorder,
            dispatch_trace_id=dispatch_trace_id,
            role="worker",
            repo_slug=actual_repo_slug,
            ticket_id=f"{actual_repo_slug}#{issue_number}",
            project=project_name,
            issue_number=issue_number,
            pr_number=impl_pr_number,
            outcome=final_outcome,
            usage=usage,
            role_data={
                "attempt": attempt,
                "total_sub_requests": (
                    len(llm_output.implemented_sub_requests) + len(llm_output.skipped_sub_requests)
                ),
                "implemented_count": len(llm_output.implemented_sub_requests),
                "skipped_count": len(llm_output.skipped_sub_requests),
                "skipped_by_reason": skipped_hist,
                "did_check_pass": final_did_check_pass,
                "confidence": llm_output.confidence,
                "baseline_failures_count": len(baseline_failures),
                "new_failures_count": len(new_failures),
            },
            duration_seconds=duration_seconds,
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
            final_labels=sorted(current_labels),
        )
    except ProviderError as exc:
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
    finally:
        if not outcome_labels_committed:
            # Worker crashed before the outcome set_labels. Best-effort revert:
            # remove the impl-attempt-N we just added, restore plan-approved.
            # A revert failure leaves the issue stuck in impl-attempt-N — log loudly
            # but do not mask the original exception.
            #
            # foreman#229: the ``except Exception:`` above also added
            # ``foreman:needs-help`` which sits outside the
            # ``added_foreman_pre`` / ``removed_foreman_pre`` namespace
            # and survives this revert. The reconciler's
            # ``_needs_help_label`` safety rule then blocks
            # re-dispatch even though the entry label is back, which
            # is the actual runaway-burn defense.
            try:
                issue.update()
                current_after_crash = {label.name for label in issue.labels}
                revert_labels = sorted(
                    (current_after_crash - added_foreman_pre) | removed_foreman_pre
                )
                issue.set_labels(*revert_labels)
                _log.warning(
                    "worker crashed before outcome; reverted entry labels for issue=%d (added back: %s, removed: %s)",
                    issue_number,
                    sorted(removed_foreman_pre),
                    sorted(added_foreman_pre),
                )
            except Exception:
                _log.exception(
                    "worker entry-label revert FAILED for issue=%d; ticket stays stuck in impl-attempt-%d state",
                    issue_number,
                    attempt,
                )
