"""V3GitHubHost — adapter implementing the v3 ReconcilerHost Protocol.

REST methods (add_label / remove_label / post_comment / merge_pr) delegate
to v2's GitHubDaemonHost (already battle-tested). dispatch_role() spawns
`uv run foreman <subcommand>` as a subprocess via subprocess.Popen,
returns the PID immediately, and registers a background asyncio.Task that
awaits subprocess completion and writes the termination row to ExecutionLog.

The executor writes the 'running' start row first and passes its id into
``dispatch_role`` as ``start_log_id``; the host carries that id straight
through to the background tracker so the termination row is written even
when the bus is silent. This avoids the indirection that previously left
dispatch rows in ``outcome='running'`` until daemon restart.

The subprocess runner is injectable for testability. Tests that drive the
host synchronously (no running asyncio loop) should call
``terminate_dispatch(start_log_id=..., outcome=...)`` themselves; the early
return path keeps that case working without scheduling a background task.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Protocol

from foreman.config import MergeMechanism
from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.host import PRMergeability

# foreman#251 (Phase 1): the DispatchRecorder lives in
# ``foreman.dispatch_recorder`` which imports ``ExecutionLog`` from
# ``foreman.reconciler.exec_log``. Importing it at module top here
# would trigger the ``foreman.reconciler`` package __init__ during
# dispatch_recorder's own import, completing a cycle. Pull it in via
# TYPE_CHECKING for annotations + a runtime import for the env-var
# constant inside ``dispatch_role`` to break the cycle.
if TYPE_CHECKING:
    from foreman.dispatch_recorder import DispatchRecorder
    from foreman.identity import IdentityRegistry

logger = logging.getLogger(__name__)


def resolve_log_dir() -> Path:
    """Resolve the per-dispatch log directory, honoring container env var.

    Container compose sets ``FOREMAN_LOG_DIR=/foreman/logs``. On the host,
    when the env var is unset, fall back to ``~/.foreman/logs`` so
    ``foreman daemon v3-start`` is still invokable for ad-hoc debug
    without containerization.
    """
    env_value = os.environ.get("FOREMAN_LOG_DIR")
    if env_value:
        return Path(env_value)
    return Path.home() / ".foreman" / "logs"


def resolve_state_dir() -> Path:
    """Resolve the daemon state directory, honoring container env var.

    Container compose sets ``FOREMAN_STATE_DIR=/foreman/state`` and mounts
    a named volume there so the daemon's state file (SQLite, sentinels,
    v3-daemon.log) survives ``docker compose down``. On the host, when
    the env var is unset, fall back to ``~/.foreman`` (the directory
    that already holds ``config.toml``, ``daemon.lock``, etc.).
    """
    env_value = os.environ.get("FOREMAN_STATE_DIR")
    if env_value:
        return Path(env_value)
    return Path.home() / ".foreman"

# Map v3 role names to the CLI subcommand the v2 daemon exposes.
_ROLE_TO_SUBCOMMAND = {
    "planner": "plan",
    "reviewer": "review",
    "fixer": "fix",
    "worker": "implement",
}


class _V2HostLike(Protocol):
    """The v2 surface V3 needs. Matches foreman.daemon_host.GitHubDaemonHost."""

    def add_issue_label(self, repo: str, issue_number: int, label: str) -> None: ...
    def remove_issue_label(self, repo: str, issue_number: int, label: str) -> None: ...
    def post_issue_comment(self, repo: str, issue_number: int, body: str) -> None: ...
    def merge_pull_request(self, repo: str, pr_number: int) -> None: ...


class _SubprocessLike(Protocol):
    """The minimal subprocess surface V3 needs."""

    pid: int

    async def wait(self) -> int: ...


class _GraphQLClientLike(Protocol):
    """The minimal GraphQL surface V3's queue-merge path needs.

    Matches ``HttpxGHGraphQLClient.graphql`` so the real client and any
    test fake can substitute interchangeably. Kept thin on purpose:
    the host only ever needs to POST a query/variables pair and read
    the JSON response.
    """

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]: ...


# GraphQL queries for the MergeQueue path. Hoisted to module scope so
# tests can grep for them, and so the queries don't sit inline making
# the dispatch method body harder to read.

_PR_NODE_ID_QUERY = """
query GetPRNodeId($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) { id }
  }
}
"""

_ENQUEUE_PR_MUTATION = """
mutation EnqueuePR($input: EnqueuePullRequestInput!) {
  enqueuePullRequest(input: $input) {
    mergeQueueEntry {
      id
      state
      position
      estimatedTimeToMerge
    }
  }
}
"""

# foreman#165: read the PR's live ``mergeStateStatus`` + the per-check
# ``conclusion`` / ``status`` / ``isRequired`` fields on the most recent
# commit's status check rollup so ``_handle_attempt_merge`` can compute
# failing- and pending-required-check counts in one round-trip.
#
# ``statusCheckRollup.contexts`` is a union of ``CheckRun`` and
# ``StatusContext``. ``isRequired`` is exposed on both; ``conclusion``
# / ``status`` only on ``CheckRun``; ``state`` only on ``StatusContext``.
# We branch in Python on the union member because GitHub's GraphQL
# requires the inline fragments to read the per-type fields.
_PR_MERGEABILITY_QUERY = """
query GetPRMergeability($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      mergeStateStatus
      commits(last: 1) {
        nodes {
          commit {
            statusCheckRollup {
              contexts(first: 100) {
                nodes {
                  __typename
                  ... on CheckRun {
                    isRequired(pullRequestNumber: $number)
                    conclusion
                    status
                  }
                  ... on StatusContext {
                    isRequired(pullRequestNumber: $number)
                    state
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

_UPDATE_BRANCH_MUTATION = """
mutation UpdatePRBranch($input: UpdatePullRequestBranchInput!) {
  updatePullRequestBranch(input: $input) {
    pullRequest { id }
  }
}
"""

# foreman#165: which CheckRun conclusions count as "this required check
# has terminated non-SUCCESS." Anything else either ran clean (SUCCESS,
# NEUTRAL, SKIPPED) or is still pending — the latter is counted as
# pending in ``_count_required_checks`` via the COMPLETED-status check.
_CHECK_FAILING_CONCLUSIONS = frozenset(
    {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STALE"}
)

# foreman#165: legacy commit-status states (``StatusContext`` union arm)
# that count as failing. ``StatusContext`` doesn't have a separate
# ``status`` field — only ``state``. ``EXPECTED`` and ``PENDING`` are
# still-running; ``SUCCESS`` is clean; everything else is failure.
_STATUS_FAILING_STATES = frozenset({"ERROR", "FAILURE"})
_STATUS_PENDING_STATES = frozenset({"EXPECTED", "PENDING"})

# Queue entry states the daemon treats as "successfully accepted into
# queue, nothing to retry." See foreman#158's empirical findings:
# QUEUED + AWAITING_CHECKS + MERGEABLE are healthy states the queue
# will process to merge over the next ~minutes. UNMERGEABLE + LOCKED
# require human attention — the daemon surfaces foreman:needs-help
# rather than retrying.
_QUEUE_HEALTHY_STATES = frozenset({"QUEUED", "AWAITING_CHECKS", "MERGEABLE"})
_QUEUE_NEEDS_HELP_STATES = frozenset({"UNMERGEABLE", "LOCKED"})


SubprocessRunner = Callable[..., _SubprocessLike]
"""Callable that spawns a subprocess and returns a ``_SubprocessLike`` wrapper.

Production runner accepts ``(argv, *, log_path: Path | None = None,
extra_env: dict[str, str] | None = None)``. Test fakes may accept the
same signature, but both ``log_path`` and ``extra_env`` are optional
from the caller's side: ``dispatch_role`` only passes ``log_path`` when
``V3GitHubHost`` was constructed with ``log_dir`` set, and only passes
``extra_env`` when forwarding the foreman#251 trace_id env var."""


class _PopenWrapper:
    """Adapter making subprocess.Popen match _SubprocessLike (pid + async wait()).

    Async wait() runs the blocking Popen.wait() on a thread executor so the
    daemon's running event loop isn't blocked.
    """

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc
        self.pid = proc.pid
        # log_path is None for the no-capture path; populated by
        # _PopenWithLog when output is being captured. The tracker reads
        # this off the wrapper to surface the path in the execution log.
        self.log_path: Path | None = None

    async def wait(self) -> int:
        return await asyncio.to_thread(self._proc.wait)


class _PopenWithLog(_PopenWrapper):
    """``_PopenWrapper`` that owns the log file handle and closes it on
    ``wait()`` completion.

    foreman#119: dispatched role subprocesses used to redirect stdout +
    stderr to ``DEVNULL``, so diagnosing a returncode=N failure required
    replaying the dispatch manually outside the daemon. The wrapper now
    keeps the per-dispatch log file open for the subprocess's lifetime
    and closes the parent-side handle once the child exits (the kernel
    keeps the file alive until the child's handle closes too, so output
    that flushes during shutdown is still captured).
    """

    def __init__(
        self,
        proc: subprocess.Popen[bytes],
        log_fh: IO[bytes],
        log_path: Path,
    ) -> None:
        super().__init__(proc)
        self._log_fh = log_fh
        self.log_path = log_path

    async def wait(self) -> int:
        try:
            return await super().wait()
        finally:
            # Best-effort close; even if it raises we don't want to mask
            # a non-zero returncode the caller wants to act on.
            try:
                self._log_fh.close()
            except Exception:
                logger.warning(
                    "failed to close log file %s for pid=%d",
                    self.log_path,
                    self.pid,
                    exc_info=True,
                )


# Constant sentinel paths handed to role subprocesses via env. They
# point OUTSIDE the daemon's polled state dir so a role subprocess
# that invokes ``foreman daemon stop`` (directly, via claude_agent_sdk
# bash, via inherited env in a child process, anywhere) writes its
# sentinel to a path the daemon does not watch — and reads the lock
# at a path that does not exist, falling through to the "no daemon"
# branch without writing anything at all.
#
# Constants (vs. per-dispatch tmp dirs) intentional: there's no
# cleanup obligation, no concurrency conflict (the paths only get
# touched if a role explicitly calls daemon stop, which it shouldn't),
# and the constant path is grep-able as evidence the scrub fired.
_ROLE_SUBPROCESS_SENTINEL_PATH = "/tmp/foreman-role-noop-shutdown-sentinel"
_ROLE_SUBPROCESS_RELOAD_PATH = "/tmp/foreman-role-noop-reload-sentinel"
_ROLE_SUBPROCESS_LOCK_PATH = "/tmp/foreman-role-noop-lock"


def _build_role_subprocess_env(base_env: Mapping[str, str]) -> dict[str, str]:
    """Compute the env a role subprocess should run with.

    Inherits everything from ``base_env`` (the daemon's env, including
    FOREMAN_CONFIG_PATH so the role can load its project config, and
    HOME so claude_agent_sdk finds ~/.claude/.credentials.json), then:

    - Sets ``FOREMAN_SHUTDOWN_SENTINEL_PATH`` / ``FOREMAN_RELOAD_SENTINEL_PATH``
      / ``FOREMAN_LOCK_PATH`` to constant paths OUTSIDE the daemon's
      state dir. A role subprocess invoking ``foreman daemon stop``
      (directly or through any inherited-env child) resolves the lock
      to a path that doesn't exist, falls through to the "no daemon"
      branch, and never reaches the sentinel-write path.
    - Drops ``FOREMAN_STATE_DIR`` / ``FOREMAN_LOG_DIR``. The role
      doesn't need them — they're for the daemon's own state + log
      directories — and inheriting them would let a role's pytest
      session resolve config-default sentinel paths to the prod
      ``/foreman/state/`` mount.

    Surfaced 2026-06-06: a role subprocess's inherited env let a
    ``foreman daemon stop`` invocation (or equivalent test code path)
    write the real ``/root/.foreman/shutdown-requested``, which the
    running daemon polled 30s later and gracefully shut down on.

    Defense in depth: the test suite also scrubs these vars per-test
    via the ``_isolate_foreman_env`` conftest fixture, closing the
    leak at the test layer. Both layers together make the bug
    structurally impossible.
    """
    env = dict(base_env)
    env["FOREMAN_SHUTDOWN_SENTINEL_PATH"] = _ROLE_SUBPROCESS_SENTINEL_PATH
    env["FOREMAN_RELOAD_SENTINEL_PATH"] = _ROLE_SUBPROCESS_RELOAD_PATH
    env["FOREMAN_LOCK_PATH"] = _ROLE_SUBPROCESS_LOCK_PATH
    env.pop("FOREMAN_STATE_DIR", None)
    env.pop("FOREMAN_LOG_DIR", None)
    return env


def _default_subprocess_runner(
    argv: list[str],
    *,
    log_path: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> _SubprocessLike:
    """Production runner. Spawn synchronously via subprocess.Popen.

    Using `asyncio.create_subprocess_exec` via `loop.run_until_complete` fails
    inside the daemon's tick (`RuntimeError: This event loop is already
    running`). subprocess.Popen is fully synchronous; we wrap it so callers
    can `await proc.wait()` without blocking the loop.

    When ``log_path`` is provided (foreman#119), the file is created (with
    parent dirs) and the subprocess's stdout + stderr are redirected to it,
    interleaved like a terminal (``stderr=subprocess.STDOUT``). The wrapper
    closes the parent-side file handle in ``wait()`` so we don't leak FDs.
    When ``log_path`` is ``None``, output is dropped to ``DEVNULL`` as
    before — preserves the existing behavior for code paths that haven't
    opted in.

    Every spawn passes an env computed by ``_build_role_subprocess_env``:
    the daemon's env minus state/log dir vars, with sentinel + lock paths
    rewritten to constant tmp paths the daemon does not watch. See that
    helper's docstring for the full rationale.

    foreman#251 (Phase 1): ``extra_env`` adds keys (typically the
    ``FOREMAN_DISPATCH_TRACE_ID`` env var) the daemon needs the role
    subprocess to see. Applied AFTER ``_build_role_subprocess_env`` so a
    deliberate override wins over the inherited env's scrubbed value.
    """
    env = _build_role_subprocess_env(os.environ)
    if extra_env:
        env.update(extra_env)
    if log_path is None:
        proc = subprocess.Popen(
            argv,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return _PopenWrapper(proc)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = log_path.open("ab")
    try:
        proc = subprocess.Popen(
            argv,
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
    except Exception:
        log_fh.close()
        raise
    return _PopenWithLog(proc, log_fh, log_path)


class V3GitHubHost:
    """v3 ReconcilerHost implementation."""

    def __init__(
        self,
        *,
        v2_host: _V2HostLike,
        log: ExecutionLog,
        subprocess_runner: SubprocessRunner | None = None,
        role_dispatch_timeout_seconds: int = 3600,
        max_concurrent_dispatches: int = 1,
        log_dir: Path | None = None,
        gh_queue_client: _GraphQLClientLike | None = None,
        dispatch_recorder: DispatchRecorder | None = None,
        identity_registry: IdentityRegistry | None = None,
    ) -> None:
        self._v2 = v2_host
        self._log = log
        self._runner = subprocess_runner if subprocess_runner is not None else _default_subprocess_runner
        self._timeout_seconds = role_dispatch_timeout_seconds
        # foreman#251 (Phase 1): the daemon-side DispatchRecorder. When
        # set, ``_track_subprocess_completion`` calls
        # ``record_subprocess_terminated`` on all four exit paths so
        # the Recorder can decide whether to write a
        # ``subprocess_killed`` cost row (subprocess died before
        # emitting its own completion) or skip (the subprocess's
        # in-band completion is already in the DB). Optional so that
        # the existing test fixtures that build a V3GitHubHost without
        # plumbing a Recorder stay green; production wiring in
        # ``cli.py`` always supplies one.
        self._recorder = dispatch_recorder
        # Global cap on concurrent dispatched role subprocesses. acquired() in
        # dispatch_role (non-blocking — raises when full), released in
        # _track_subprocess_completion's finally so the slot is held for the
        # entire subprocess lifecycle including timeout-termination.
        self._dispatch_capacity = threading.Semaphore(max_concurrent_dispatches)
        self._max_concurrent_dispatches = max_concurrent_dispatches
        # foreman#119: when ``log_dir`` is set, every dispatched role
        # subprocess gets its stdout + stderr captured to a per-dispatch
        # file under ``<log_dir>/<role>/<issue>__<iso-timestamp>.log``
        # and the path is recorded in the execution log so post-mortem
        # is just ``cat <path>``. When ``log_dir`` is ``None`` (test
        # default), output goes to DEVNULL as before — preserves the
        # existing behavior for any caller that hasn't opted in.
        self._log_dir = log_dir
        # foreman#158: when set, ``merge_pr`` with ``mechanism="queue"``
        # routes through GitHub's GraphQL ``enqueuePullRequest`` mutation
        # instead of the v2 REST ``pr.merge()``. The client should be
        # authenticated as the orchestrator-bot App (the same identity
        # that already mutates labels) — that's the App that needs
        # "Merge queues: write" permission on the target repo.
        #
        # When ``None``, ``merge_pr`` falls back to the v2 direct merge
        # regardless of ``mechanism``. This keeps tests and ad-hoc
        # invocations working without forcing a queue client through
        # every test fixture.
        self._gh_queue_client = gh_queue_client
        # foreman#215: optional IdentityRegistry for injecting GIT_AUTHOR_* /
        # GIT_COMMITTER_* env vars into role subprocesses so LLM-driven
        # git commit calls attribute to the correct bot regardless of
        # .git/config state in the worktree. When None (test default),
        # identity env vars are omitted --- the subprocess inherits whatever
        # identity the worktree's .git/config already has.
        self._identity_registry = identity_registry

    def add_label(self, *, owner: str, repo: str, issue: int, label: str) -> None:
        self._v2.add_issue_label(f"{owner}/{repo}", issue, label)

    def remove_label(self, *, owner: str, repo: str, issue: int, label: str) -> None:
        self._v2.remove_issue_label(f"{owner}/{repo}", issue, label)

    def post_comment(self, *, owner: str, repo: str, issue: int, body: str) -> None:
        self._v2.post_issue_comment(f"{owner}/{repo}", issue, body)

    def merge_pr(
        self,
        *,
        owner: str,
        repo: str,
        pr_number: int,
        mechanism: MergeMechanism,
    ) -> None:
        """Merge a PR via the project's configured mechanism (foreman#158).

        Today both branches call the same underlying ``pr.merge()`` —
        the queue-specific behavior (call GitHub MergeQueue endpoint
        explicitly, recognize 'queued' as success-not-failure, etc.)
        lands in a follow-up. The wiring being in place means the
        follow-up can implement just the queue branch without touching
        the executor or rules layer.
        """
        if mechanism == "queue":
            self._enqueue_pull_request(
                owner=owner, repo=repo, pr_number=pr_number
            )
        else:
            self._v2.merge_pull_request(f"{owner}/{repo}", pr_number)

    def _enqueue_pull_request(
        self, *, owner: str, repo: str, pr_number: int
    ) -> None:
        """Add a PR to GitHub's MergeQueue via the GraphQL mutation.

        Implementation grounded in the empirical schema probe from
        foreman#158: GitHub exposes the queue add as a GraphQL mutation
        named ``enqueuePullRequest`` (NOT a REST endpoint, and NOT
        ``addPullRequestToMergeQueue`` despite some docs). PyGithub does
        not wrap it, so we use our existing ``HttpxGHGraphQLClient``
        directly.

        Two-step call: GraphQL needs the PR's node ID, not the integer
        number, so we issue a tiny lookup query first. Both calls reuse
        the same client so the orchestrator-bot's token is minted once
        per invocation.

        Response handling — based on the probe's observed shapes:

        - Errors array non-empty (e.g., "Merge queues are not enabled
          for <repo>"): raise. Today's retry-loop fires, which is the
          right call for configuration errors that deserve operator
          attention.
        - ``mergeQueueEntry.state`` in QUEUED / AWAITING_CHECKS / MERGEABLE:
          success. Log the position + ETA and return. The daemon's next
          poll sees the PR eventually merged once the queue lands it,
          which triggers the normal ADVANCE_LABEL_TO_DONE path.
        - ``state`` in UNMERGEABLE / LOCKED: log + surface as ``foreman:
          needs-help`` (genuine human-attention case — likely conflict
          or branch-lock). Done via raising so the executor's catch
          path treats it as a needs-help-grade failure rather than a
          retryable error.
        """
        if self._gh_queue_client is None:
            raise RuntimeError(
                "merge_mechanism='queue' but V3GitHubHost was built "
                "without a queue-capable GraphQL client. Wire one "
                "through cli.py: gh_queue_client=HttpxGHGraphQLClient("
                "token_supplier=registry.get_orchestrator_token)."
            )

        # Step 1: resolve PR number → GraphQL node ID. We can't avoid
        # this round-trip because the enqueue mutation takes the node ID
        # form, and the daemon only has the integer PR number.
        node_resp = self._gh_queue_client.graphql(
            _PR_NODE_ID_QUERY,
            {"owner": owner, "repo": repo, "number": pr_number},
        )
        try:
            pr_node_id = node_resp["data"]["repository"]["pullRequest"]["id"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(
                f"Could not resolve node ID for {owner}/{repo}#{pr_number}: "
                f"response={node_resp!r}"
            ) from exc

        # Step 2: call enqueuePullRequest.
        resp = self._gh_queue_client.graphql(
            _ENQUEUE_PR_MUTATION,
            {
                "input": {
                    "pullRequestId": pr_node_id,
                    "clientMutationId": f"foreman-daemon-pr-{pr_number}",
                }
            },
        )

        # Errors array → propagate. Most common cause is "Merge queues
        # are not enabled for <repo>" (operator hasn't enabled MergeQueue
        # on the base branch); the operator should see the message and
        # either enable MergeQueue or set merge_mechanism="direct" for
        # this project.
        if resp.get("errors"):
            err = resp["errors"][0]
            raise RuntimeError(
                f"enqueuePullRequest failed for {owner}/{repo}#{pr_number}: "
                f"{err.get('type', 'UNKNOWN')}: {err.get('message', 'no message')}"
            )

        # Defensive: confirm the response shape matches what we observed
        # in the probe. If GitHub changes the shape, we want a loud
        # message, not a silent state inconsistency.
        try:
            entry = resp["data"]["enqueuePullRequest"]["mergeQueueEntry"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(
                f"enqueuePullRequest returned unexpected shape for "
                f"{owner}/{repo}#{pr_number}: {resp!r}"
            ) from exc

        if entry is None:
            raise RuntimeError(
                f"enqueuePullRequest returned null mergeQueueEntry for "
                f"{owner}/{repo}#{pr_number} (no error in response). "
                "GitHub semantics for this case are not documented; "
                "treating as failure."
            )

        state = entry.get("state")
        if state in _QUEUE_HEALTHY_STATES:
            logger.info(
                "PR %s/%s#%d added to merge queue: state=%s position=%s "
                "estimatedTimeToMerge=%ss",
                owner,
                repo,
                pr_number,
                state,
                entry.get("position"),
                entry.get("estimatedTimeToMerge"),
            )
            return

        if state in _QUEUE_NEEDS_HELP_STATES:
            raise RuntimeError(
                f"PR {owner}/{repo}#{pr_number} added to queue but "
                f"state={state} — needs human review (conflict, locked "
                "branch, or other unmergeable condition)."
            )

        # Unknown state — GitHub added a new value we haven't classified.
        # Raise loudly so we notice and update _QUEUE_HEALTHY_STATES /
        # _QUEUE_NEEDS_HELP_STATES.
        raise RuntimeError(
            f"PR {owner}/{repo}#{pr_number} added to queue but state="
            f"{state!r} is not in foreman's known MergeQueue states "
            f"({sorted(_QUEUE_HEALTHY_STATES | _QUEUE_NEEDS_HELP_STATES)}). "
            "GitHub may have added a new state; update v3_host.py."
        )

    def get_pr_mergeability(
        self, *, owner: str, repo: str, pr_number: int
    ) -> PRMergeability:
        """Read the PR's live ``mergeStateStatus`` + required-check counts.

        foreman#165: ``_handle_attempt_merge`` calls this every tick to
        decide between merge / wait / rebase / needs-help. The query
        forces GitHub to compute ``mergeStateStatus`` for this specific
        PR (the bulk PR-list query in the observer returns ``UNKNOWN``
        for many PRs because GitHub computes it lazily).
        """
        if self._gh_queue_client is None:
            raise RuntimeError(
                "get_pr_mergeability requires V3GitHubHost to be built "
                "with a GraphQL client. Wire one through cli.py: "
                "gh_queue_client=HttpxGHGraphQLClient("
                "token_supplier=registry.get_orchestrator_token)."
            )

        resp = self._gh_queue_client.graphql(
            _PR_MERGEABILITY_QUERY,
            {"owner": owner, "repo": repo, "number": pr_number},
        )

        if resp.get("errors"):
            err = resp["errors"][0]
            raise RuntimeError(
                f"getPRMergeability failed for {owner}/{repo}#{pr_number}: "
                f"{err.get('type', 'UNKNOWN')}: {err.get('message', 'no message')}"
            )

        try:
            pr_node = resp["data"]["repository"]["pullRequest"]
            state = pr_node["mergeStateStatus"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(
                f"getPRMergeability returned unexpected shape for "
                f"{owner}/{repo}#{pr_number}: {resp!r}"
            ) from exc

        failing, pending = self._count_required_checks(pr_node)

        return PRMergeability(
            state=str(state),
            failing_required_check_count=failing,
            pending_required_check_count=pending,
        )

    @staticmethod
    def _count_required_checks(pr_node: dict[str, Any]) -> tuple[int, int]:
        """Walk the PR's latest commit's statusCheckRollup and count
        required failing + pending checks.

        Returns ``(failing_count, pending_count)``. Each context is either
        a ``CheckRun`` (has ``conclusion``/``status``) or a legacy
        ``StatusContext`` (has ``state``). Non-required contexts are
        ignored — the spec's state machine only cares about required
        checks because GitHub's ``BLOCKED`` state already reflects
        required-only gating.
        """
        commits = ((pr_node.get("commits") or {}).get("nodes")) or []
        if not commits:
            return 0, 0
        rollup = (commits[0].get("commit") or {}).get("statusCheckRollup")
        if rollup is None:
            return 0, 0
        contexts = ((rollup.get("contexts") or {}).get("nodes")) or []
        failing = 0
        pending = 0
        for ctx in contexts:
            if not ctx.get("isRequired"):
                continue
            typename = ctx.get("__typename")
            if typename == "CheckRun":
                conclusion = ctx.get("conclusion")
                status = ctx.get("status")
                if conclusion is None or status != "COMPLETED":
                    pending += 1
                elif conclusion in _CHECK_FAILING_CONCLUSIONS:
                    failing += 1
            elif typename == "StatusContext":
                ctx_state = ctx.get("state")
                if ctx_state in _STATUS_PENDING_STATES:
                    pending += 1
                elif ctx_state in _STATUS_FAILING_STATES:
                    failing += 1
        return failing, pending

    def update_branch(
        self, *, owner: str, repo: str, pr_number: int
    ) -> None:
        """Call GitHub's ``updatePullRequestBranch`` GraphQL mutation
        (server-side rebase onto the base branch's head).

        foreman#165: ``_handle_attempt_merge`` calls this when
        ``mergeStateStatus`` is ``BEHIND``. GitHub recomputes mergeability
        server-side; the next poll re-enters the rule and re-reads the
        live state.
        """
        if self._gh_queue_client is None:
            raise RuntimeError(
                "update_branch requires V3GitHubHost to be built with a "
                "GraphQL client. Wire one through cli.py: "
                "gh_queue_client=HttpxGHGraphQLClient("
                "token_supplier=registry.get_orchestrator_token)."
            )

        # Two-step shape matching ``_enqueue_pull_request``: GraphQL needs
        # the PR's node ID, not the integer number.
        node_resp = self._gh_queue_client.graphql(
            _PR_NODE_ID_QUERY,
            {"owner": owner, "repo": repo, "number": pr_number},
        )
        try:
            pr_node_id = node_resp["data"]["repository"]["pullRequest"]["id"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(
                f"Could not resolve node ID for {owner}/{repo}#{pr_number}: "
                f"response={node_resp!r}"
            ) from exc

        resp = self._gh_queue_client.graphql(
            _UPDATE_BRANCH_MUTATION,
            {"input": {"pullRequestId": pr_node_id}},
        )

        if resp.get("errors"):
            err = resp["errors"][0]
            raise RuntimeError(
                f"updatePullRequestBranch failed for {owner}/{repo}#{pr_number}: "
                f"{err.get('type', 'UNKNOWN')}: {err.get('message', 'no message')}"
            )

    def dispatch_role(
        self,
        *,
        role: str,
        target: str | None,
        owner: str,
        repo: str,
        issue: int,
        pr_number: int | None,
        start_log_id: int,
        project: str,
    ) -> int | None:
        """Spawn `uv run foreman <subcommand>` as a subprocess.

        Returns the spawned subprocess's PID on success. Returns ``None``
        when the concurrency cap is full — the caller treats that as
        "skipped this poll, retry next." NEVER raises for cap-reached:
        cap-skip is a routine outcome of N tickets being ready in one
        poll, not an error condition. Raising would cascade through
        ``execute_action``'s except clause and write a terminate row
        with outcome ``error``, which both pollutes the log AND
        (per the 2026-06-06 production incident) corrupts the attempt-
        counting rules that key off recent terminations.

        Other failures still raise: unknown role (programmer error),
        missing pr_number for reviewer (programmer error), runner
        spawn failure (real environment problem). Capacity slot is
        released on those raise paths so they don't leak.

        ``target`` is ``"spec_pr"`` / ``"impl_pr"`` for Reviewer + Fixer (the
        target-ambiguous roles), and ``None`` for Planner + Worker. Plumbed
        into the subprocess via ``--target`` so the role CLI loads the right
        prompt and asserts the right entry label.

        ``start_log_id`` is the id of the 'running' row the executor wrote
        before calling this method. The background tracker carries it through
        and writes the termination row when the subprocess exits — so
        ``count_completed`` advances in production without depending on the
        worker's bus envelope landing. When this method returns ``None``
        (cap-skip), the caller is responsible for terminating the start row
        with outcome ``skipped_capacity`` so the row doesn't dangle.

        ``project`` is the project name the action is for. One V3GitHubHost
        instance is shared across all registered projects, so the project
        cannot be baked into ``__init__``; the executor passes
        ``ctx.snapshot.project`` per call. Plumbed into the subprocess via
        ``--project`` so the role CLI loads the right project config.
        """
        if not self._dispatch_capacity.acquire(blocking=False):
            logger.info(
                "dispatch skipped role=%s issue=%s — concurrency cap %d "
                "reached; will retry next poll",
                role,
                issue,
                self._max_concurrent_dispatches,
            )
            return None
        subcommand = _ROLE_TO_SUBCOMMAND.get(role)
        if subcommand is None:
            self._dispatch_capacity.release()
            raise ValueError(f"unknown role for dispatch: {role!r}")

        argv: list[str] = ["foreman", subcommand]

        if role == "reviewer":
            # `foreman review` takes a positional PR URL — no --issue-url flag.
            if pr_number is None:
                self._dispatch_capacity.release()
                raise ValueError("dispatch_role(role='reviewer') requires pr_number")
            pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
            argv.extend([pr_url, "--project", project])
            if target is not None:
                argv.extend(["--target", target])
        elif role in ("planner", "worker"):
            # `foreman plan` and `foreman implement` take positional ISSUE_URL.
            # They do not accept --pr-url; both roles OPEN the PR rather than
            # consume an existing one. Don't pass --target either — only the
            # target-ambiguous roles (reviewer, fixer) use it.
            issue_url = f"https://github.com/{owner}/{repo}/issues/{issue}"
            argv.extend([issue_url, "--project", project])
        elif role == "fixer":
            # `foreman fix` uses --issue-url + (optional) --pr-url + --target.
            issue_url = f"https://github.com/{owner}/{repo}/issues/{issue}"
            argv.extend(["--issue-url", issue_url, "--project", project])
            if pr_number is not None:
                pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
                argv.extend(["--pr-url", pr_url])
            if target is not None:
                argv.extend(["--target", target])
        else:
            # Defense-in-depth: the _ROLE_TO_SUBCOMMAND lookup above already
            # raises for unknown roles, but if a future role is added to the
            # map without a corresponding argv branch here, fail loudly rather
            # than spawn a malformed subprocess.
            self._dispatch_capacity.release()
            raise ValueError(f"unknown role for dispatch: {role!r}")

        # foreman#119: compute the per-dispatch log path before spawn so
        # the runner can open the file and wire stdout/stderr to it. When
        # log_dir was not configured (test default), log_path stays None
        # and the runner falls back to DEVNULL.
        log_path = self._build_dispatch_log_path(role=role, issue=issue)

        # foreman#251 (Phase 1): propagate the trace_id to the role
        # subprocess via env so the role's in-process DispatchRecorder
        # tags every event with the same trace_id the parent will use
        # in ``record_subprocess_terminated``. Without this, in-band
        # cost rows from the subprocess and the parent's subprocess-
        # killed row would target different trace_ids and the
        # CostSubscriber's DB-lookup dedup would never catch them.
        # Local import breaks the dispatch_recorder ⇄ reconciler.__init__
        # cycle (see the TYPE_CHECKING block above).
        extra_env: dict[str, str] | None = None
        if self._recorder is not None:
            from foreman.dispatch_recorder import TRACE_ID_ENV_VAR

            extra_env = {TRACE_ID_ENV_VAR: str(start_log_id)}

        # Wrap runner + task creation in try/except so the capacity slot is
        # released on any spawn-time failure (uv missing → FileNotFoundError,
        # fork failure → OSError, loop scheduling failure, etc.). Without
        # this, repeated spawn failures would permanently consume slots until
        # the daemon restart triggers recover_orphaned + slot reset.
        try:
            # foreman#215: inject GIT_AUTHOR_* / GIT_COMMITTER_* env vars so the
            # LLM-driven git commit calls inside the role subprocess attribute
            # to the correct bot rather than leaking the human identity from
            # .git/config in the worktree. Done inside the try/except so that
            # a metadata-fetch error (first-time App metadata call) still
            # releases the capacity slot instead of leaking it.
            if self._identity_registry is not None:
                git_identity_env = self._identity_registry.get_role_identity_env(role)
                if extra_env is None:
                    extra_env = git_identity_env
                else:
                    extra_env.update(git_identity_env)
            runner_kwargs: dict[str, Any] = {}
            if log_path is not None:
                runner_kwargs["log_path"] = log_path
            if extra_env is not None:
                runner_kwargs["extra_env"] = extra_env
            proc = self._runner(argv, **runner_kwargs) if runner_kwargs else self._runner(argv)
        except Exception:
            self._dispatch_capacity.release()
            raise
        logger.info(
            "dispatched role=%s pid=%d log=%s argv=%s",
            role,
            proc.pid,
            log_path,
            argv,
        )

        # Background task: wait for subprocess, write termination row.
        # If there's no running event loop (e.g., synchronous unit tests), the
        # caller is responsible for invoking terminate_dispatch directly. We
        # still need to release the capacity slot in that case to avoid
        # leaking it.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._dispatch_capacity.release()
            return proc.pid

        # foreman#251 (Phase 1): include role + ticket + project + issue
        # in the tracker so the parent's Recorder can build a complete
        # ``record_subprocess_terminated`` envelope. ``ticket_id`` and
        # ``project`` are needed because the Recorder's JSONL writer
        # fans out to ``log_<role>_run`` which keys on repo_slug +
        # issue_number.
        try:
            loop.create_task(
                self._track_subprocess_completion(
                    proc,
                    role,
                    start_log_id=start_log_id,
                    owner=owner,
                    repo=repo,
                    issue=issue,
                    project=project,
                )
            )
        except Exception:
            self._dispatch_capacity.release()
            raise
        return proc.pid

    async def _track_subprocess_completion(
        self,
        proc: _SubprocessLike,
        role: str,
        *,
        start_log_id: int,
        owner: str | None = None,
        repo: str | None = None,
        issue: int | None = None,
        project: str | None = None,
    ) -> None:
        """Await subprocess exit and write the termination row.

        The outer try/finally ensures the dispatch-capacity semaphore is
        released on every exit path (success, returncode!=0, timeout, error)
        so a stuck or crashed background task can never permanently consume
        a slot.

        foreman#251 (Phase 1): each of the four exit paths
        (timeout / error / returncode!=0 / returncode==0) ALSO calls
        ``self._recorder.record_subprocess_terminated`` so the
        Recorder can write a ``subprocess_killed`` cost row when the
        subprocess died before emitting its own ``dispatch_complete``.
        ``owner`` / ``repo`` / ``issue`` / ``project`` carry through
        so the Recorder can build the ticket_id + repo_slug it needs
        for the JSONL fan-out. Defaults are ``None`` for
        back-compatibility with the existing tests that drive
        ``_track_subprocess_completion`` directly without going
        through ``dispatch_role``.
        """
        # foreman#119: surface the per-dispatch log path in every
        # termination row so post-mortem doesn't need to grep the daemon
        # log to find which file to read. None for the no-capture path.
        log_path = getattr(proc, "log_path", None)
        log_path_str = str(log_path) if log_path is not None else None
        start_time = time.monotonic()
        try:
            try:
                returncode = await asyncio.wait_for(proc.wait(), timeout=self._timeout_seconds)
            except TimeoutError:
                logger.warning(
                    "subprocess for role=%s pid=%d timed out after %ds; terminating",
                    role,
                    proc.pid,
                    self._timeout_seconds,
                )
                # Attempt graceful termination via the wrapped Popen (best-effort).
                try:
                    inner = getattr(proc, "_proc", None)
                    if inner is not None and hasattr(inner, "terminate"):
                        inner.terminate()
                except Exception:
                    logger.exception("failed to terminate timed-out subprocess pid=%d", proc.pid)
                timeout_details: dict[str, Any] = {
                    "timeout_seconds": self._timeout_seconds,
                    "role": role,
                }
                if log_path_str is not None:
                    timeout_details["log_path"] = log_path_str
                self.terminate_dispatch(
                    start_log_id=start_log_id,
                    outcome="timeout",
                    details=timeout_details,
                )
                self._notify_recorder_terminated(
                    role=role,
                    owner=owner,
                    repo=repo,
                    issue=issue,
                    project=project,
                    start_log_id=start_log_id,
                    exit_outcome="subprocess_timeout",
                    duration_seconds=time.monotonic() - start_time,
                )
                return
            except Exception as exc:
                logger.exception("subprocess for role=%s pid=%d errored awaiting", role, proc.pid)
                err_details: dict[str, Any] = {"error": str(exc)}
                if log_path_str is not None:
                    err_details["log_path"] = log_path_str
                self.terminate_dispatch(
                    start_log_id=start_log_id,
                    outcome="error",
                    details=err_details,
                )
                self._notify_recorder_terminated(
                    role=role,
                    owner=owner,
                    repo=repo,
                    issue=issue,
                    project=project,
                    start_log_id=start_log_id,
                    exit_outcome="subprocess_error",
                    duration_seconds=time.monotonic() - start_time,
                )
                return

            outcome = "success" if returncode == 0 else "error"
            term_details: dict[str, Any] = {"returncode": returncode, "role": role}
            if log_path_str is not None:
                term_details["log_path"] = log_path_str
            self.terminate_dispatch(
                start_log_id=start_log_id,
                outcome=outcome,
                details=term_details,
            )
            self._notify_recorder_terminated(
                role=role,
                owner=owner,
                repo=repo,
                issue=issue,
                project=project,
                start_log_id=start_log_id,
                exit_outcome="success" if returncode == 0 else "subprocess_nonzero_exit",
                duration_seconds=time.monotonic() - start_time,
            )
        finally:
            self._dispatch_capacity.release()

    def _notify_recorder_terminated(
        self,
        *,
        role: str,
        owner: str | None,
        repo: str | None,
        issue: int | None,
        project: str | None,
        start_log_id: int,
        exit_outcome: str,
        duration_seconds: float,
    ) -> None:
        """Forward a subprocess-exit event to the Phase 1 DispatchRecorder.

        Best-effort: no-op when the host was built without a recorder
        (existing test fixtures) or when the dispatched context is
        incomplete (defensive guard for callers driving the host
        directly). A Recorder exception is logged and swallowed — the
        ``terminate_dispatch`` write above is the load-bearing row,
        not this one.
        """
        if self._recorder is None:
            return
        if owner is None or repo is None or issue is None or project is None:
            # Defensive: the dispatch_role path always passes all four;
            # this guard exists for direct ``_track_subprocess_completion``
            # callers (test seam) so partial-context calls don't crash.
            return
        repo_slug = f"{owner}/{repo}"
        ticket_id = f"{repo_slug}#{issue}"
        try:
            self._recorder.record_subprocess_terminated(
                trace_id=start_log_id,
                role=role,
                repo_slug=repo_slug,
                ticket_id=ticket_id,
                project=project,
                issue_number=issue,
                exit_outcome=exit_outcome,
                duration_seconds=duration_seconds,
            )
        except Exception:
            logger.exception(
                "DispatchRecorder.record_subprocess_terminated raised for "
                "trace_id=%d role=%s exit_outcome=%s; ignoring (telemetry-only)",
                start_log_id,
                role,
                exit_outcome,
            )

    def _build_dispatch_log_path(self, *, role: str, issue: int) -> Path | None:
        """Compute the per-dispatch log file path, or ``None`` when log
        capture is disabled (``log_dir`` not configured at host construction).

        foreman#119: file naming uses an ISO-8601 UTC timestamp with colons
        replaced by hyphens (Windows refuses ``:`` in path components):
        ``<log_dir>/<role>/<issue>__<YYYY-MM-DDTHH-MM-SS-microsecondsZ>.log``.
        The pid is recorded separately in the daemon log and the execution
        log's ``details``; including it in the filename would require a
        post-spawn rename that races with the subprocess.
        """
        if self._log_dir is None:
            return None
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S-%f")
        return self._log_dir / role / f"{issue}__{ts}Z.log"

    def terminate_dispatch(
        self,
        *,
        start_log_id: int,
        outcome: str,
        details: dict[str, Any],
    ) -> None:
        """Write the termination row for a dispatch_role start row.

        Called automatically by ``_track_subprocess_completion`` in production.
        Tests that drive the host without a running event loop call this
        directly to simulate subprocess exit.
        """
        self._log.terminate_action(
            parent_log_id=start_log_id, outcome=outcome, details=details
        )
