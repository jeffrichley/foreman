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
from github.PullRequest import PullRequest
from github.Repository import Repository

from foreman.branches import impl_branch, spec_branch
from foreman.config import Config
from foreman.identity import IdentityRegistry
from foreman.instructions import load_project_instructions
from foreman.provider import ProviderFacade
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


def _run_check_command(*, check_command: str, cwd: Path) -> tuple[int, set[str], str]:
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
    """
    from foreman._env_filter import filtered_subprocess_env

    result = subprocess.run(
        check_command,
        cwd=cwd,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
        env=filtered_subprocess_env(),
    )
    combined = (result.stdout or "") + (result.stderr or "")
    failing = {m.group("test_id") for m in _PYTEST_FAILED_RE.finditer(combined)}
    return result.returncode, failing, combined


def _summarize_failures(failures: set[str], combined_output: str) -> str:
    """Compose a one-paragraph human summary for stats / logs.

    Caps at 8 listed failures to keep the JSONL row + stderr trace
    readable. The full output stays in ``combined_output`` for the
    caller to log separately if needed.
    """
    if not failures:
        return "no new failures"
    listed = sorted(failures)
    head = listed[:8]
    tail_note = f" (+{len(listed) - 8} more)" if len(listed) > 8 else ""
    return f"{len(listed)} new failures: " + ", ".join(head) + tail_note


def _read_spec_doc_from_branch(
    *, worktree_path: Path, spec_branch: str, issue_number: int
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
    """
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


def _clear_impl_attempt_and_help_labels(issue: object, all_known_labels: set[str]) -> None:
    """Per-episode counter reset on ``implemented`` outcome.

    Drops every ``foreman:impl-attempt-N`` label currently visible on
    the issue, plus ``foreman:needs-help`` if it was present. Mirrors
    the Fixer's per-episode reset: lifecycle stats JSONL preserves the
    cumulative audit trail; labels reflect current-cycle state only.

    Catches every individual remove and swallows — a label may already
    be gone on the GitHub side if a concurrent process or human
    interaction removed it. Logging the swallow would just create
    noise; the next read of the issue's labels is the source of truth.
    """
    for label_name in all_known_labels:
        if label_name.startswith("foreman:impl-attempt-") or label_name == _LABEL_NEEDS_HELP:
            try:
                issue.remove_from_labels(label_name)  # type: ignore[attr-defined]
            except Exception:
                pass  # label may already be absent on the GitHub side


async def run_worker(
    *,
    issue_url: str,
    config: Config,
    project_name: str,
    worktrees_root: Path,
    provider: ProviderFacade,
    identity_registry: IdentityRegistry | None = None,
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
    owner, repo_name, issue_number = parse_issue_url(issue_url)
    project = config.projects[project_name]
    expected_repo_slug = project.repo
    actual_repo_slug = f"{owner}/{repo_name}"
    if expected_repo_slug != actual_repo_slug:
        raise ValueError(
            f"Issue URL repo {actual_repo_slug!r} does not match project "
            f"{project_name!r} configured repo {expected_repo_slug!r}"
        )

    registry = identity_registry if identity_registry is not None else IdentityRegistry(project)
    worker_client: Github = registry.get_worker_client()
    worker_token: str = registry.get_worker_token()

    repo: Repository = worker_client.get_repo(actual_repo_slug)
    issue = repo.get_issue(issue_number)
    issue_labels = {label.name for label in issue.labels}

    # Pre-flight: refuse to run without the v3 Worker entry label.
    # ``foreman:plan-approved`` is the post-Reviewer-signoff queue marker;
    # the v3 reconciler dispatches the Worker once this label is on the
    # issue and the spec PR is merged (or stacked).
    if not (issue_labels & _WORKER_ENTRY_LABELS):
        raise RuntimeError(
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
        raise RuntimeError(
            f"Issue #{issue_number} has hit the max {max_impl_attempts} "
            "impl-attempts; needs human intervention via foreman:failed. "
            f"Existing attempts: {previous_attempts}."
        )

    # foreman#91: track the in-process label set parallel to each
    # remote add/remove so the role can return the authoritative
    # post-transition set in ``WorkerRunResult.final_labels``. Avoids
    # the eventual-consistency race of a post-mutation host re-read.
    current_labels: set[str] = set(issue_labels)

    # Stamp the new attempt label IMMEDIATELY so it's visible even if
    # the LLM dispatch crashes mid-run. Audit-trail before audit-loss.
    attempt_label = f"foreman:impl-attempt-{attempt}"
    issue.add_to_labels(attempt_label)
    current_labels.add(attempt_label)

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
    wt_mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt_result = wt_mgr.create_impl(
        clone_path=Path(project.local_clone_path),
        repo_slug=repo_name,
        ticket_id=issue_number,
    )
    wt_path = wt_result.path

    # Read the spec doc — on-disk first (it's checked out on this
    # branch), git-show fallback to authoritative spec-branch content.
    spec_doc_content = _read_spec_doc_from_branch(
        worktree_path=wt_path, spec_branch=spec_branch_name, issue_number=issue_number
    )

    # Baseline preflight (D4): capture the failing-tests set BEFORE the
    # LLM does anything. Anything in this set is "not the Worker's
    # problem" and gets subtracted from post-Worker failures to find
    # what the Worker actually introduced.
    _baseline_rc, baseline_failures, _baseline_output = _run_check_command(
        check_command=check_command, cwd=wt_path
    )

    # Advance label: clear the entry label so observers see in-flight
    # state via the ``foreman:impl-attempt-N`` marker (set above) and
    # the execution log. v3 does NOT use an explicit ``implementing``
    # label — the attempt counter + exec log are the in-flight signal,
    # and adding a transient label would re-dispatch on reconciler tick.
    for _entry_label in _WORKER_ENTRY_LABELS:
        try:
            issue.remove_from_labels(_entry_label)
        except Exception:
            pass
        current_labels.discard(_entry_label)

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

    start_time = time.monotonic()
    try:
        llm_output = await provider.run_agent(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            allowed_tools=WORKER_ALLOWED_TOOLS,
            output_model=WorkerOutput,
            cwd=wt_path,
            env={**os.environ, "GH_TOKEN": worker_token},
        )
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
        # Post-Worker verification still runs below — but since no LLM
        # work landed, post == baseline, so new_failures will be empty
        # and the outcome stays `incomplete`. The label transitions
        # branch normally.
    else:
        duration_seconds = time.monotonic() - start_time

    # Post-Worker verification (D4): re-run check_command and diff
    # against baseline to derive ground truth.
    post_rc, post_failures, post_output = _run_check_command(
        check_command=check_command, cwd=wt_path
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
    pr_url: str | None = None
    if final_outcome == "implemented":
        # Open the impl PR, stacked on the spec branch (D1). The
        # PyGithub call gives us the new PR's html_url to return.
        # ``pr_title`` and ``pr_body`` are guaranteed non-None by the
        # WorkerOutput validator; the override path above only flips
        # `implemented` → `incomplete`, never the reverse.
        assert llm_output.pr_title is not None
        assert llm_output.pr_body is not None
        impl_pr = repo.create_pull(
            title=llm_output.pr_title,
            body=llm_output.pr_body,
            base=wt_result.base_branch,
            head=impl_branch_name,
        )
        pr_url = impl_pr.html_url

        # Label transitions for the implemented outcome. Per-episode
        # counter reset: drop all impl-attempt-N labels + needs-help so
        # any future re-trigger starts with a fresh 3-attempt budget.
        # v3: no ``implementing`` label to clear — the entry label
        # ``foreman:plan-approved`` was already removed at dispatch.
        issue.add_to_labels(_LABEL_IMPL_REVIEW)
        current_labels.add(_LABEL_IMPL_REVIEW)
        all_known_labels = _label_names(issue_labels, attempt_label)
        _clear_impl_attempt_and_help_labels(issue, all_known_labels)
        for _label_name in all_known_labels:
            if (
                _label_name.startswith("foreman:impl-attempt-")
                or _label_name == _LABEL_NEEDS_HELP
            ):
                current_labels.discard(_label_name)
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
        # Entry label was removed at dispatch time; idempotent re-remove
        # so the ticket cleanly enters spec-fix regardless of how the
        # Worker came in (manual CLI vs daemon).
        for _entry_label in _WORKER_ENTRY_LABELS:
            try:
                issue.remove_from_labels(_entry_label)
            except Exception:
                pass
            current_labels.discard(_entry_label)
        issue.add_to_labels(_LABEL_SPEC_FIX)
        current_labels.add(_LABEL_SPEC_FIX)
        issue.add_to_labels(_LABEL_NEEDS_HELP)
        current_labels.add(_LABEL_NEEDS_HELP)
    else:
        # incomplete: add needs-help so observers know to look. v3 does
        # not use an in-flight ``implementing`` label — the impl-attempt-N
        # marker carries cycle state. On the last attempt, also stamp
        # foreman:failed so the queue surfaces it for human triage.
        issue.add_to_labels(_LABEL_NEEDS_HELP)
        current_labels.add(_LABEL_NEEDS_HELP)
        if attempt == max_impl_attempts:
            issue.add_to_labels(_LABEL_FAILED)
            current_labels.add(_LABEL_FAILED)

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
