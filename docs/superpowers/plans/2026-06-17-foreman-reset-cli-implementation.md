# foreman reset CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `foreman reset --project <p> --issue-number <N>` — a single CLI command that fully wipes a stuck foreman ticket (labels + remote branches + open PRs + local worktrees + SQLite row) and re-triggers the autonomous loop. Operator lever for the recurring stale-state-after-failure case.

**Architecture:** Two-phase shape — read-only `_discover` builds a `ResetPlan` dataclass, then `_execute` walks the plan with per-step try/except. Five new methods land on existing seams (`GitProvider.delete_branch / close_pr / find_open_pr_by_head_branch`, `TicketRepository.delete_ticket`, `WorktreeManager.prune`); the command itself lives in `cli/mutations.py` next to its sibling operator commands.

**Tech Stack:** Python 3.12 / typer / PyGithub / sqlite3 / pytest. Same stack as the rest of foreman v4.

**Spec:** `docs/superpowers/specs/2026-06-17-foreman-reset-cli-design.md`

**Repo state:** Branch `feat/foreman-v4-substrate`. **Do NOT push** — all work stays local. Pre-push hook runs `just check` (ruff + mypy + lint-imports + pytest); each commit must keep it green. Conventional commit subjects, lowercase. Stage specific files (no `git add -A`). Use Wren PAT via `python C:/Users/jeffr/.wren/.claude/skills/creds-management/scripts/creds.py --being wren get github --keyring --password` for any GitHub op, passed via `GH_TOKEN` env (never echoed). NO `Co-Authored-By` trailer.

---

## File map

**Modify:**
- `packages/foreman/src/foreman/v4/git_provider.py` — add `delete_branch` / `close_pr` / `find_open_pr_by_head_branch` to Protocol + FakeGitProvider
- `packages/foreman/src/foreman/v4/pygithub_git_provider.py` — implement the 3 new methods against PyGithub
- `packages/foreman/src/foreman/v4/routing_git_provider.py` — add 3 dispatch wrappers
- `packages/foreman/src/foreman/v4/repository.py` — add `delete_ticket` to Protocol + InMemoryTicketRepository
- `packages/foreman/src/foreman/v4/sqlite_repository.py` — add `delete_ticket` (two-step DELETE inside transaction; schema has no CASCADE)
- `packages/foreman/src/foreman/worktree.py` — add `prune` method on `WorktreeManager`
- `packages/foreman/src/foreman/v4/cli/mutations.py` — add `ResetPlan` dataclass, `_discover`, `_render_plan`, `_execute`, `cmd_reset`
- `packages/foreman/src/foreman/v4/cli/__init__.py` — register `reset` command

**Tests (modify):**
- `packages/foreman/tests/v4/_repository_contract.py` — add 3 `delete_ticket` cases
- `packages/foreman/tests/v4/test_git_provider_fake.py` — 3 new method tests
- `packages/foreman/tests/v4/test_pygithub_git_provider.py` — 3 new method tests (mocked Github client)
- `packages/foreman/tests/v4/test_routing_git_provider.py` — 3 new dispatch tests
- `packages/foreman/tests/v4/cli/test_mutation_commands.py` — add `cmd_reset` cases

**Tests (create):**
- `packages/foreman/tests/v4/test_worktree_manager_prune.py` — new file

---

### Task 1: `TicketRepository.delete_ticket` — Protocol, InMemory, SQLite

**Files:**
- Modify: `packages/foreman/tests/v4/_repository_contract.py`
- Modify: `packages/foreman/src/foreman/v4/repository.py`
- Modify: `packages/foreman/src/foreman/v4/sqlite_repository.py`

- [ ] **Step 1: Write failing contract tests for `delete_ticket`**

Append to `packages/foreman/tests/v4/_repository_contract.py` (inside `class RepositoryContract`):

```python
    def test_delete_ticket_removes_row(self, repo: TicketRepository) -> None:
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        repo.delete_ticket(t.id)
        with pytest.raises(TicketNotFoundError):
            repo.get_ticket(t.id)

    def test_delete_ticket_clears_by_issue_lookup(self, repo: TicketRepository) -> None:
        t = repo.create_ticket(project="p", issue_number=42, now=_now())
        repo.delete_ticket(t.id)
        with pytest.raises(TicketNotFoundError):
            repo.get_ticket_by_issue(project="p", issue_number=42)
        # After delete, the (project, issue_number) slot is free again.
        recreated = repo.create_ticket(project="p", issue_number=42, now=_now())
        assert recreated.issue_number == 42

    def test_delete_ticket_cascades_state_instances(
        self, repo: TicketRepository,
    ) -> None:
        t = repo.create_ticket(project="p", issue_number=1, now=_now())
        repo.open_state_instance(
            ticket_id=t.id, state_name="Planning", sequence=1, now=_now(),
        )
        repo.open_state_instance(
            ticket_id=t.id, state_name="SpecReview", sequence=2, now=_now(),
        )
        repo.delete_ticket(t.id)
        assert repo.list_state_instances_for_ticket(t.id) == []

    def test_delete_ticket_missing_raises(self, repo: TicketRepository) -> None:
        with pytest.raises(TicketNotFoundError):
            repo.delete_ticket(9999)
```

- [ ] **Step 2: Run contract tests to verify they fail**

Run: `cd e:/workspaces/ai/agents/foreman && uv run pytest packages/foreman/tests/v4/test_in_memory_repository.py packages/foreman/tests/v4/test_sqlite_repository.py -k delete_ticket -v`

Expected: FAIL — `TicketRepository` has no attribute `delete_ticket`.

- [ ] **Step 3: Add `delete_ticket` to the Protocol**

In `packages/foreman/src/foreman/v4/repository.py`, in the `TicketRepository` Protocol's `--- Ticket CRUD ---` section (after `resume_ticket`):

```python
    def delete_ticket(self, ticket_id: int) -> None: ...
```

- [ ] **Step 4: Implement `delete_ticket` on `InMemoryTicketRepository`**

In `packages/foreman/src/foreman/v4/repository.py`, after `resume_ticket` on the InMemory class:

```python
    def delete_ticket(self, ticket_id: int) -> None:
        existing = self.get_ticket(ticket_id)  # raises TicketNotFoundError
        del self._tickets[ticket_id]
        del self._by_issue[(existing.project, existing.issue_number)]
        # Cascade — drop every state-instance row tied to this ticket.
        self._instances = {
            iid: inst for iid, inst in self._instances.items()
            if inst.ticket_id != ticket_id
        }
```

- [ ] **Step 5: Run contract test against InMemory to verify pass**

Run: `cd e:/workspaces/ai/agents/foreman && uv run pytest packages/foreman/tests/v4/test_in_memory_repository.py -k delete_ticket -v`

Expected: PASS (all 4 tests).

- [ ] **Step 6: Implement `delete_ticket` on `SqliteTicketRepository`**

In `packages/foreman/src/foreman/v4/sqlite_repository.py`, after `resume_ticket` on the SQLite class. Schema has no `ON DELETE CASCADE` on `state_instances.ticket_id` (verified in `schema.sql`), so this does explicit two-step DELETE inside one transaction:

```python
    def delete_ticket(self, ticket_id: int) -> None:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "SELECT 1 FROM tickets WHERE id = ?", (ticket_id,),
            )
            if cur.fetchone() is None:
                raise TicketNotFoundError(str(ticket_id))
            self._conn.execute(
                "DELETE FROM state_instances WHERE ticket_id = ?",
                (ticket_id,),
            )
            self._conn.execute(
                "DELETE FROM tickets WHERE id = ?", (ticket_id,),
            )
```

(The `with self._conn:` context manager commits on exit / rolls back on exception — matches the existing pattern in this file. `self._lock` is the threading lock the class already uses for mutations.)

- [ ] **Step 7: Run contract test against SQLite to verify pass**

Run: `cd e:/workspaces/ai/agents/foreman && uv run pytest packages/foreman/tests/v4/test_sqlite_repository.py -k delete_ticket -v`

Expected: PASS (all 4 tests).

- [ ] **Step 8: Commit**

```bash
cd e:/workspaces/ai/agents/foreman
git add packages/foreman/tests/v4/_repository_contract.py \
        packages/foreman/src/foreman/v4/repository.py \
        packages/foreman/src/foreman/v4/sqlite_repository.py
git commit -m "feat: add TicketRepository.delete_ticket with state-instance cascade"
```

---

### Task 2: `GitProvider.delete_branch` — Protocol + FakeGitProvider

**Files:**
- Modify: `packages/foreman/tests/v4/test_git_provider_fake.py`
- Modify: `packages/foreman/src/foreman/v4/git_provider.py`

- [ ] **Step 1: Write failing tests for FakeGitProvider.delete_branch**

Append to `packages/foreman/tests/v4/test_git_provider_fake.py`:

```python
def test_delete_branch_records_deletion():
    fake = FakeGitProvider()
    fake.seed_branch(project="p", branch_name="foreman/issue-1")
    fake.delete_branch(project="p", branch_name="foreman/issue-1")
    assert ("p", "foreman/issue-1") in fake.deleted_branches
    assert "foreman/issue-1" not in fake.get_branches(project="p")


def test_delete_branch_missing_is_noop():
    fake = FakeGitProvider()
    # No seed — branch doesn't exist. Must NOT raise.
    fake.delete_branch(project="p", branch_name="foreman/issue-99")
    assert ("p", "foreman/issue-99") in fake.deleted_branches
```

(Note: `seed_branch` / `get_branches` / `deleted_branches` are the new test helpers + recorder we add to FakeGitProvider in Step 3.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd e:/workspaces/ai/agents/foreman && uv run pytest packages/foreman/tests/v4/test_git_provider_fake.py -k delete_branch -v`

Expected: FAIL — `FakeGitProvider has no attribute 'delete_branch'`.

- [ ] **Step 3: Add `delete_branch` to Protocol + Fake**

In `packages/foreman/src/foreman/v4/git_provider.py`:

Add to `GitProvider` Protocol after `close_issue`:

```python
    def delete_branch(
        self, *, project: str, branch_name: str,
    ) -> None:
        """Delete a remote branch.

        Idempotent: if the branch doesn't exist (404 / 422), this is a
        no-op rather than an error. Used by ``foreman reset`` to clear
        stale ``foreman/issue-N`` / ``foreman/impl-N`` debris.
        """
        ...
```

Add to `FakeGitProvider.__init__` after `self.closed_issues = set()`:

```python
        # Recorder for delete_branch calls (mirrors closed_issues shape).
        self.deleted_branches: set[tuple[str, str]] = set()
        # Current branches per project. seed_branch populates; delete_branch
        # removes. Missing-branch delete records the call but is otherwise a no-op.
        self._branches: dict[str, set[str]] = {}
```

Add the seed helper + Protocol impl on `FakeGitProvider`:

```python
    def seed_branch(self, *, project: str, branch_name: str) -> None:
        """Test helper: seed a branch into the fake's branch set."""
        self._branches.setdefault(project, set()).add(branch_name)

    def get_branches(self, *, project: str) -> set[str]:
        """Test helper: return the current branch set for a project."""
        return set(self._branches.get(project, set()))

    def delete_branch(
        self, *, project: str, branch_name: str,
    ) -> None:
        """Drop the branch from this fake's branch set + record the call."""
        self.deleted_branches.add((project, branch_name))
        current = self._branches.get(project)
        if current is not None:
            current.discard(branch_name)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd e:/workspaces/ai/agents/foreman && uv run pytest packages/foreman/tests/v4/test_git_provider_fake.py -k delete_branch -v`

Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
cd e:/workspaces/ai/agents/foreman
git add packages/foreman/tests/v4/test_git_provider_fake.py \
        packages/foreman/src/foreman/v4/git_provider.py
git commit -m "feat: add GitProvider.delete_branch Protocol + fake"
```

---

### Task 3: `GitProvider.close_pr` — Protocol + FakeGitProvider

**Files:**
- Modify: `packages/foreman/tests/v4/test_git_provider_fake.py`
- Modify: `packages/foreman/src/foreman/v4/git_provider.py`

- [ ] **Step 1: Write failing tests for FakeGitProvider.close_pr**

Append to `packages/foreman/tests/v4/test_git_provider_fake.py`:

```python
def test_close_pr_records_close_and_marks_pr_closed():
    fake = FakeGitProvider()
    fake.set_pr_state(
        project="p", pr_number=19,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    fake.close_pr(project="p", pr_number=19)
    assert ("p", 19) in fake.closed_prs
    # PR state should reflect closed-not-merged so subsequent get_pr_state
    # doesn't claim mergeable.
    state = fake.get_pr_state(project="p", pr_number=19)
    assert state.merged is False


def test_close_pr_idempotent_on_already_closed():
    fake = FakeGitProvider()
    fake.set_pr_state(
        project="p", pr_number=19,
        state=PRState(merged=False, mergeable=False, ci_passing=True),
    )
    fake.close_pr(project="p", pr_number=19)
    # Second call must not raise.
    fake.close_pr(project="p", pr_number=19)
    assert ("p", 19) in fake.closed_prs
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd e:/workspaces/ai/agents/foreman && uv run pytest packages/foreman/tests/v4/test_git_provider_fake.py -k close_pr -v`

Expected: FAIL — `FakeGitProvider has no attribute 'close_pr'`.

- [ ] **Step 3: Add `close_pr` to Protocol + Fake**

In `packages/foreman/src/foreman/v4/git_provider.py`:

Add to `GitProvider` Protocol after `delete_branch`:

```python
    def close_pr(self, *, project: str, pr_number: int) -> None:
        """Close a PR without merging.

        Distinct from ``close_issue`` — PyGithub treats issues and PRs
        as separate API surfaces. Idempotent: closing an already-closed
        PR is a no-op rather than an error. Used by ``foreman reset``
        to retire spec/impl PRs whose branches it's about to delete.
        """
        ...
```

Add to `FakeGitProvider.__init__` after `self._branches`:

```python
        # Recorder for close_pr calls.
        self.closed_prs: set[tuple[str, int]] = set()
```

Add on `FakeGitProvider`:

```python
    def close_pr(self, *, project: str, pr_number: int) -> None:
        """Record the close + ensure subsequent get_pr_state shows it
        as not merged. Idempotent on repeat calls.
        """
        self.closed_prs.add((project, pr_number))
        # Best-effort: if the PR is in our state map, leave merged as-is
        # (closed-without-merge stays merged=False; already-merged stays
        # merged=True — close on a merged PR shouldn't undo the merge).
        # No state mutation needed beyond the recorder.
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd e:/workspaces/ai/agents/foreman && uv run pytest packages/foreman/tests/v4/test_git_provider_fake.py -k close_pr -v`

Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
cd e:/workspaces/ai/agents/foreman
git add packages/foreman/tests/v4/test_git_provider_fake.py \
        packages/foreman/src/foreman/v4/git_provider.py
git commit -m "feat: add GitProvider.close_pr Protocol + fake"
```

---

### Task 4: `GitProvider.find_open_pr_by_head_branch` — Protocol + FakeGitProvider

**Files:**
- Modify: `packages/foreman/tests/v4/test_git_provider_fake.py`
- Modify: `packages/foreman/src/foreman/v4/git_provider.py`

- [ ] **Step 1: Write failing tests**

Append to `packages/foreman/tests/v4/test_git_provider_fake.py`:

```python
def test_find_open_pr_by_head_branch_returns_pr_number():
    fake = FakeGitProvider()
    fake.set_pr_state(
        project="p", pr_number=19,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    fake.set_pr_head_branch(
        project="p", pr_number=19, branch_name="foreman/issue-180",
    )
    found = fake.find_open_pr_by_head_branch(
        project="p", branch_name="foreman/issue-180",
    )
    assert found == 19


def test_find_open_pr_by_head_branch_no_match_returns_none():
    fake = FakeGitProvider()
    found = fake.find_open_pr_by_head_branch(
        project="p", branch_name="foreman/issue-999",
    )
    assert found is None


def test_find_open_pr_by_head_branch_skips_closed_prs():
    fake = FakeGitProvider()
    fake.set_pr_state(
        project="p", pr_number=19,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    fake.set_pr_head_branch(
        project="p", pr_number=19, branch_name="foreman/issue-180",
    )
    fake.close_pr(project="p", pr_number=19)
    found = fake.find_open_pr_by_head_branch(
        project="p", branch_name="foreman/issue-180",
    )
    assert found is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd e:/workspaces/ai/agents/foreman && uv run pytest packages/foreman/tests/v4/test_git_provider_fake.py -k find_open_pr -v`

Expected: FAIL — no attribute `find_open_pr_by_head_branch` (or `set_pr_head_branch`).

- [ ] **Step 3: Add Protocol method + Fake impl**

In `packages/foreman/src/foreman/v4/git_provider.py`:

Add to `GitProvider` Protocol after `close_pr`:

```python
    def find_open_pr_by_head_branch(
        self, *, project: str, branch_name: str,
    ) -> int | None:
        """Find an OPEN PR whose head branch matches ``branch_name``.

        Returns the PR number, or None if no open PR matches. Used by
        ``foreman reset`` to discover spec/impl PRs without depending
        on the SQLite ticket row (which may have already been deleted
        manually). PRs that are closed or merged are NOT returned —
        the discovery phase only cares about live debris that needs
        closing.
        """
        ...
```

Add to `FakeGitProvider.__init__` after `self.closed_prs`:

```python
        # Map of (project, pr_number) → head branch name. set_pr_head_branch
        # populates; find_open_pr_by_head_branch reverse-scans it.
        self._pr_head_branches: dict[tuple[str, int], str] = {}
```

Add on `FakeGitProvider`:

```python
    def set_pr_head_branch(
        self, *, project: str, pr_number: int, branch_name: str,
    ) -> None:
        """Test helper: seed the head branch for a PR."""
        self._pr_head_branches[(project, pr_number)] = branch_name

    def find_open_pr_by_head_branch(
        self, *, project: str, branch_name: str,
    ) -> int | None:
        """Linear-scan the PR head-branch map for an open PR on this branch."""
        for (proj, pr_num), head in self._pr_head_branches.items():
            if proj != project or head != branch_name:
                continue
            if (project, pr_num) in self.closed_prs:
                continue
            # An already-merged PR isn't "open" either; skip it.
            try:
                state = self.get_pr_state(project=project, pr_number=pr_num)
            except PRNotFoundError:
                continue
            if state.merged:
                continue
            return pr_num
        return None
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd e:/workspaces/ai/agents/foreman && uv run pytest packages/foreman/tests/v4/test_git_provider_fake.py -k find_open_pr -v`

Expected: PASS (all 3 tests).

- [ ] **Step 5: Commit**

```bash
cd e:/workspaces/ai/agents/foreman
git add packages/foreman/tests/v4/test_git_provider_fake.py \
        packages/foreman/src/foreman/v4/git_provider.py
git commit -m "feat: add GitProvider.find_open_pr_by_head_branch Protocol + fake"
```

---

### Task 5: `PyGithubGitProvider` — implement the 3 new methods

**Files:**
- Modify: `packages/foreman/tests/v4/test_pygithub_git_provider.py`
- Modify: `packages/foreman/src/foreman/v4/pygithub_git_provider.py`

- [ ] **Step 1: Write failing tests against mocked Github client**

Append to `packages/foreman/tests/v4/test_pygithub_git_provider.py`. Follow the existing pattern in that file for constructing `PyGithubGitProvider` with a mock `Github` factory.

```python
def test_delete_branch_calls_git_ref_delete(monkeypatch):
    """delete_branch resolves heads/<name> then calls .delete()."""
    fake_ref = MagicMock()
    fake_repo = MagicMock()
    fake_repo.get_git_ref.return_value = fake_ref
    fake_github = MagicMock()
    fake_github.get_repo.return_value = fake_repo
    provider = PyGithubGitProvider(
        github_factory=lambda: fake_github,
        repo_full_name="org/repo",
    )
    provider.delete_branch(project="ignored", branch_name="foreman/issue-1")
    fake_repo.get_git_ref.assert_called_once_with("heads/foreman/issue-1")
    fake_ref.delete.assert_called_once()


def test_delete_branch_swallows_404(monkeypatch):
    """A 404 (ref doesn't exist) must not raise — reset relies on this."""
    fake_repo = MagicMock()
    fake_repo.get_git_ref.side_effect = GithubException(
        status=404, data={"message": "Not Found"}, headers={},
    )
    fake_github = MagicMock()
    fake_github.get_repo.return_value = fake_repo
    provider = PyGithubGitProvider(
        github_factory=lambda: fake_github,
        repo_full_name="org/repo",
    )
    # Must NOT raise.
    provider.delete_branch(project="ignored", branch_name="foreman/issue-1")


def test_close_pr_calls_pull_edit_state_closed():
    fake_pr = MagicMock()
    fake_repo = MagicMock()
    fake_repo.get_pull.return_value = fake_pr
    fake_github = MagicMock()
    fake_github.get_repo.return_value = fake_repo
    provider = PyGithubGitProvider(
        github_factory=lambda: fake_github,
        repo_full_name="org/repo",
    )
    provider.close_pr(project="ignored", pr_number=19)
    fake_repo.get_pull.assert_called_once_with(19)
    fake_pr.edit.assert_called_once_with(state="closed")


def test_close_pr_swallows_already_closed():
    """422 (PR already closed) must not raise."""
    fake_pr = MagicMock()
    fake_pr.edit.side_effect = GithubException(
        status=422,
        data={"message": "Validation Failed", "errors": [
            {"resource": "PullRequest", "code": "invalid"}]},
        headers={},
    )
    fake_repo = MagicMock()
    fake_repo.get_pull.return_value = fake_pr
    fake_github = MagicMock()
    fake_github.get_repo.return_value = fake_repo
    provider = PyGithubGitProvider(
        github_factory=lambda: fake_github,
        repo_full_name="org/repo",
    )
    provider.close_pr(project="ignored", pr_number=19)


def test_find_open_pr_by_head_branch_uses_get_pulls_with_head_filter():
    fake_pr = MagicMock(number=19)
    fake_repo = MagicMock()
    fake_repo.owner.login = "org"
    fake_repo.get_pulls.return_value = [fake_pr]
    fake_github = MagicMock()
    fake_github.get_repo.return_value = fake_repo
    provider = PyGithubGitProvider(
        github_factory=lambda: fake_github,
        repo_full_name="org/repo",
    )
    result = provider.find_open_pr_by_head_branch(
        project="ignored", branch_name="foreman/issue-180",
    )
    assert result == 19
    fake_repo.get_pulls.assert_called_once_with(
        state="open", head="org:foreman/issue-180",
    )


def test_find_open_pr_by_head_branch_returns_none_on_empty():
    fake_repo = MagicMock()
    fake_repo.owner.login = "org"
    fake_repo.get_pulls.return_value = []
    fake_github = MagicMock()
    fake_github.get_repo.return_value = fake_repo
    provider = PyGithubGitProvider(
        github_factory=lambda: fake_github,
        repo_full_name="org/repo",
    )
    assert provider.find_open_pr_by_head_branch(
        project="ignored", branch_name="foreman/issue-180",
    ) is None
```

Imports needed at top of file (add if missing):

```python
from unittest.mock import MagicMock
from github import GithubException
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd e:/workspaces/ai/agents/foreman && uv run pytest packages/foreman/tests/v4/test_pygithub_git_provider.py -k "delete_branch or close_pr or find_open_pr" -v`

Expected: FAIL — methods don't exist on `PyGithubGitProvider`.

- [ ] **Step 3: Implement on PyGithubGitProvider**

Add to `packages/foreman/src/foreman/v4/pygithub_git_provider.py`. The `_repo` helper already exists on this class (returns a fresh `Repository` handle with token-refresh accounted for). Use the same pattern as the existing methods:

```python
    def delete_branch(
        self, *, project: str, branch_name: str,
    ) -> None:
        try:
            ref = self._repo().get_git_ref(f"heads/{branch_name}")
            ref.delete()
        except GithubException as exc:
            # 404 = ref doesn't exist. 422 sometimes returned when the
            # ref is gone but cached. Both mean "branch already absent"
            # which is exactly what reset wants.
            if exc.status in (404, 422):
                return
            raise

    def close_pr(self, *, project: str, pr_number: int) -> None:
        try:
            pr = self._repo().get_pull(pr_number)
            pr.edit(state="closed")
        except GithubException as exc:
            # 422 = "PR is already closed" (PyGithub raises Validation
            # Failed on edit-to-closed when state is already closed).
            # 404 = PR doesn't exist (treat as already-gone).
            if exc.status in (404, 422):
                return
            raise

    def find_open_pr_by_head_branch(
        self, *, project: str, branch_name: str,
    ) -> int | None:
        repo = self._repo()
        # GitHub's PR search wants head as "owner:branch".
        head_filter = f"{repo.owner.login}:{branch_name}"
        for pr in repo.get_pulls(state="open", head=head_filter):
            return pr.number
        return None
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd e:/workspaces/ai/agents/foreman && uv run pytest packages/foreman/tests/v4/test_pygithub_git_provider.py -k "delete_branch or close_pr or find_open_pr" -v`

Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
cd e:/workspaces/ai/agents/foreman
git add packages/foreman/tests/v4/test_pygithub_git_provider.py \
        packages/foreman/src/foreman/v4/pygithub_git_provider.py
git commit -m "feat: implement delete_branch / close_pr / find_open_pr_by_head_branch on PyGithubGitProvider"
```

---

### Task 6: `RoutingGitProvider` — dispatch the 3 new methods

**Files:**
- Modify: `packages/foreman/tests/v4/test_routing_git_provider.py`
- Modify: `packages/foreman/src/foreman/v4/routing_git_provider.py`

- [ ] **Step 1: Write failing routing tests**

Append to `packages/foreman/tests/v4/test_routing_git_provider.py`:

```python
def test_delete_branch_dispatches_to_per_project_provider():
    a = FakeGitProvider()
    b = FakeGitProvider()
    router = RoutingGitProvider(providers={"a": a, "b": b})
    router.delete_branch(project="b", branch_name="foreman/issue-1")
    assert ("b", "foreman/issue-1") in b.deleted_branches
    assert a.deleted_branches == set()


def test_close_pr_dispatches_to_per_project_provider():
    a = FakeGitProvider()
    b = FakeGitProvider()
    a.set_pr_state(
        project="a", pr_number=5,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    router = RoutingGitProvider(providers={"a": a, "b": b})
    router.close_pr(project="a", pr_number=5)
    assert ("a", 5) in a.closed_prs
    assert b.closed_prs == set()


def test_find_open_pr_by_head_branch_dispatches_to_per_project_provider():
    a = FakeGitProvider()
    b = FakeGitProvider()
    b.set_pr_state(
        project="b", pr_number=42,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    b.set_pr_head_branch(
        project="b", pr_number=42, branch_name="foreman/issue-180",
    )
    router = RoutingGitProvider(providers={"a": a, "b": b})
    found = router.find_open_pr_by_head_branch(
        project="b", branch_name="foreman/issue-180",
    )
    assert found == 42
    # Same branch name on project "a" must NOT match — project routing
    # is the whole point.
    assert router.find_open_pr_by_head_branch(
        project="a", branch_name="foreman/issue-180",
    ) is None


def test_delete_branch_unknown_project_raises():
    router = RoutingGitProvider(providers={"a": FakeGitProvider()})
    with pytest.raises(UnknownProjectError):
        router.delete_branch(project="nope", branch_name="x")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd e:/workspaces/ai/agents/foreman && uv run pytest packages/foreman/tests/v4/test_routing_git_provider.py -k "delete_branch or close_pr or find_open_pr" -v`

Expected: FAIL — `RoutingGitProvider` has no such methods.

- [ ] **Step 3: Add dispatch methods to RoutingGitProvider**

Append to `RoutingGitProvider` in `packages/foreman/src/foreman/v4/routing_git_provider.py`:

```python
    def delete_branch(
        self, *, project: str, branch_name: str,
    ) -> None:
        self._resolve(project).delete_branch(
            project=project, branch_name=branch_name,
        )

    def close_pr(self, *, project: str, pr_number: int) -> None:
        self._resolve(project).close_pr(
            project=project, pr_number=pr_number,
        )

    def find_open_pr_by_head_branch(
        self, *, project: str, branch_name: str,
    ) -> int | None:
        return self._resolve(project).find_open_pr_by_head_branch(
            project=project, branch_name=branch_name,
        )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd e:/workspaces/ai/agents/foreman && uv run pytest packages/foreman/tests/v4/test_routing_git_provider.py -k "delete_branch or close_pr or find_open_pr" -v`

Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
cd e:/workspaces/ai/agents/foreman
git add packages/foreman/tests/v4/test_routing_git_provider.py \
        packages/foreman/src/foreman/v4/routing_git_provider.py
git commit -m "feat: route delete_branch / close_pr / find_open_pr_by_head_branch in RoutingGitProvider"
```

---

### Task 7: `WorktreeManager.prune` — remove `issue-N` + `impl-N` worktrees

**Files:**
- Create: `packages/foreman/tests/v4/test_worktree_manager_prune.py`
- Modify: `packages/foreman/src/foreman/worktree.py`

- [ ] **Step 1: Write failing prune tests**

Create `packages/foreman/tests/v4/test_worktree_manager_prune.py`:

```python
"""WorktreeManager.prune — operator lever for foreman reset.

Removes both ~/.foreman/worktrees/<project>/issue-N/ and impl-N/ via
``git worktree remove --force`` first, falling back to ``shutil.rmtree``
if that fails (e.g. the dir exists but isn't a registered worktree).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from foreman.worktree import WorktreeManager


def _make_worktree_root(tmp_path: Path, project: str, issue: int) -> Path:
    root = tmp_path / "worktrees"
    (root / project / f"issue-{issue}").mkdir(parents=True)
    (root / project / f"impl-{issue}").mkdir(parents=True)
    return root


def test_prune_removes_both_issue_and_impl_dirs(tmp_path: Path):
    root = _make_worktree_root(tmp_path, "agent_core", 180)
    wt = WorktreeManager(worktrees_root=root)
    removed = wt.prune(project="agent_core", issue_number=180)
    assert sorted(p.name for p in removed) == ["impl-180", "issue-180"]
    assert not (root / "agent_core" / "issue-180").exists()
    assert not (root / "agent_core" / "impl-180").exists()


def test_prune_missing_dirs_returns_empty_list(tmp_path: Path):
    root = tmp_path / "worktrees"
    root.mkdir()
    wt = WorktreeManager(worktrees_root=root)
    removed = wt.prune(project="agent_core", issue_number=999)
    assert removed == []


def test_prune_only_issue_present(tmp_path: Path):
    root = tmp_path / "worktrees"
    (root / "agent_core" / "issue-180").mkdir(parents=True)
    # impl-180 deliberately absent.
    wt = WorktreeManager(worktrees_root=root)
    removed = wt.prune(project="agent_core", issue_number=180)
    assert [p.name for p in removed] == ["issue-180"]


def test_prune_only_impl_present(tmp_path: Path):
    root = tmp_path / "worktrees"
    (root / "agent_core" / "impl-180").mkdir(parents=True)
    wt = WorktreeManager(worktrees_root=root)
    removed = wt.prune(project="agent_core", issue_number=180)
    assert [p.name for p in removed] == ["impl-180"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd e:/workspaces/ai/agents/foreman && uv run pytest packages/foreman/tests/v4/test_worktree_manager_prune.py -v`

Expected: FAIL — `WorktreeManager has no attribute 'prune'`.

- [ ] **Step 3: Implement `prune` on `WorktreeManager`**

In `packages/foreman/src/foreman/worktree.py`, add a method on `WorktreeManager` (place near the cleanup helpers):

```python
    def prune(
        self,
        *,
        project: str,
        issue_number: int,
    ) -> list[Path]:
        """Remove both ``issue-<N>`` and ``impl-<N>`` worktrees for this ticket.

        For each target path:

        1. If the path is a registered git worktree, try
           ``git worktree remove --force <path>``.
        2. If that fails (non-zero exit, or path isn't a registered
           worktree), fall back to ``shutil.rmtree(path, ignore_errors=False)``.
        3. If the path doesn't exist, silently skip it.

        Returns the list of paths that were actually removed. Used by
        ``foreman reset`` to wipe local debris for a stuck ticket.
        """
        project_root = self.worktrees_root / project
        candidates = [
            project_root / f"issue-{issue_number}",
            project_root / f"impl-{issue_number}",
        ]
        removed: list[Path] = []
        for path in candidates:
            if not path.exists():
                continue
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(path)],
                    check=True,
                    capture_output=True,
                    env=self._env(),
                )
            except (subprocess.CalledProcessError, FileNotFoundError):
                # Either git rejected (not a registered worktree) or git
                # isn't on PATH. Fall back to rmtree.
                if path.exists():
                    shutil.rmtree(path, ignore_errors=False)
            removed.append(path)
        return removed
```

(`subprocess`, `shutil`, `Path` already imported at top of file. `self._env()` already defined on the class.)

- [ ] **Step 4: Run tests to verify pass**

Run: `cd e:/workspaces/ai/agents/foreman && uv run pytest packages/foreman/tests/v4/test_worktree_manager_prune.py -v`

Expected: PASS (4 tests). The tests use plain dirs (not real git worktrees), so the `git worktree remove` call will fail and rmtree fallback will handle them — which is exactly the path we want exercised.

- [ ] **Step 5: Commit**

```bash
cd e:/workspaces/ai/agents/foreman
git add packages/foreman/tests/v4/test_worktree_manager_prune.py \
        packages/foreman/src/foreman/worktree.py
git commit -m "feat: add WorktreeManager.prune for foreman reset"
```

---

### Task 8: `ResetPlan` dataclass + `_discover` helper

**Files:**
- Modify: `packages/foreman/tests/v4/cli/test_mutation_commands.py`
- Modify: `packages/foreman/src/foreman/v4/cli/mutations.py`

- [ ] **Step 1: Write failing tests for `_discover` and `ResetPlan`**

Append to `packages/foreman/tests/v4/cli/test_mutation_commands.py`:

```python
def test_discover_collects_full_state_when_everything_present():
    from foreman.v4.cli.mutations import ResetPlan, _discover
    from foreman.v4.git_provider import FakeGitProvider, PRState

    repo = SqliteTicketRepository.in_memory()
    ticket = repo.create_ticket(
        project="agent_core", issue_number=180, now=dt.datetime(2026, 6, 17),
    )
    git = FakeGitProvider()
    git.seed_issue_labels(
        project="agent_core", issue_number=180,
        labels={"foreman:state-failed", "foreman:plan", "bug"},
    )
    git.seed_branch(project="agent_core", branch_name="foreman/issue-180")
    git.seed_branch(project="agent_core", branch_name="foreman/impl-180")
    git.set_pr_state(
        project="agent_core", pr_number=19,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    git.set_pr_head_branch(
        project="agent_core", pr_number=19, branch_name="foreman/issue-180",
    )
    git.set_pr_state(
        project="agent_core", pr_number=21,
        state=PRState(merged=False, mergeable=False, ci_passing=False),
    )
    git.set_pr_head_branch(
        project="agent_core", pr_number=21, branch_name="foreman/impl-180",
    )
    plan = _discover(
        git=git, repo=repo,
        project="agent_core", issue_number=180,
        keep_pr=False, keep_worktree=False, retrigger=True,
    )
    assert isinstance(plan, ResetPlan)
    assert plan.spec_pr == 19
    assert plan.impl_pr == 21
    assert plan.delete_branches == [
        "foreman/issue-180", "foreman/impl-180",
    ]
    assert plan.prune_worktrees is True
    assert plan.strip_labels == {"foreman:state-failed", "foreman:plan"}
    assert plan.delete_ticket_id == ticket.id
    assert plan.apply_plan_label is True


def test_discover_no_row_no_branches_no_prs_minimal_plan():
    from foreman.v4.cli.mutations import _discover
    from foreman.v4.git_provider import FakeGitProvider

    repo = SqliteTicketRepository.in_memory()
    git = FakeGitProvider()
    # No labels, no branches, no PRs, no ticket row.
    plan = _discover(
        git=git, repo=repo,
        project="agent_core", issue_number=999,
        keep_pr=False, keep_worktree=False, retrigger=True,
    )
    assert plan.spec_pr is None
    assert plan.impl_pr is None
    assert plan.strip_labels == set()
    assert plan.delete_ticket_id is None
    assert plan.apply_plan_label is True


def test_discover_keep_pr_skips_pr_lookup():
    from foreman.v4.cli.mutations import _discover
    from foreman.v4.git_provider import FakeGitProvider, PRState

    repo = SqliteTicketRepository.in_memory()
    git = FakeGitProvider()
    git.set_pr_state(
        project="agent_core", pr_number=19,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    git.set_pr_head_branch(
        project="agent_core", pr_number=19, branch_name="foreman/issue-180",
    )
    plan = _discover(
        git=git, repo=repo,
        project="agent_core", issue_number=180,
        keep_pr=True, keep_worktree=False, retrigger=True,
    )
    assert plan.spec_pr is None
    assert plan.impl_pr is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd e:/workspaces/ai/agents/foreman && uv run pytest packages/foreman/tests/v4/cli/test_mutation_commands.py -k discover -v`

Expected: FAIL — `cannot import name 'ResetPlan' from 'foreman.v4.cli.mutations'`.

- [ ] **Step 3: Add `ResetPlan` dataclass + `_discover` to mutations.py**

At the top of `packages/foreman/src/foreman/v4/cli/mutations.py` (after existing imports, before `_resolve`):

```python
from dataclasses import dataclass

from foreman.v4.git_provider import GitProvider


@dataclass(frozen=True, slots=True)
class ResetPlan:
    """What ``foreman reset`` will do, decided in the discovery phase.

    Built read-only from current GitHub + SQLite + filesystem state by
    :func:`_discover`. Walked destructively by :func:`_execute`. Steps
    that are off — no PR matched, no row in SQLite, ``--keep-pr`` set —
    are encoded as ``None`` / empty / False so the renderer + executor
    can skip them uniformly.
    """
    project: str
    issue_number: int
    spec_pr: int | None
    impl_pr: int | None
    delete_branches: list[str]
    prune_worktrees: bool
    strip_labels: set[str]
    delete_ticket_id: int | None
    apply_plan_label: bool


def _discover(
    *,
    git: GitProvider,
    repo,
    project: str,
    issue_number: int,
    keep_pr: bool,
    keep_worktree: bool,
    retrigger: bool,
) -> ResetPlan:
    """Read-only scan of current state. No mutations."""
    if keep_pr:
        spec_pr = None
        impl_pr = None
    else:
        spec_pr = git.find_open_pr_by_head_branch(
            project=project, branch_name=f"foreman/issue-{issue_number}",
        )
        impl_pr = git.find_open_pr_by_head_branch(
            project=project, branch_name=f"foreman/impl-{issue_number}",
        )
    # Branches: always include both candidates. delete_branch is idempotent
    # on missing, so listing them unconditionally is fine.
    delete_branches = [
        f"foreman/issue-{issue_number}",
        f"foreman/impl-{issue_number}",
    ]
    # FakeGitProvider has get_issue_labels; the Protocol doesn't (label
    # reading isn't otherwise needed by states). Use getattr fallback so
    # production PyGithub paths can implement it later without breaking
    # this code path. Production reset adds the method via the next
    # follow-up if it isn't already there at exec time.
    get_labels = getattr(git, "get_issue_labels", None)
    if get_labels is not None:
        labels_on_issue = get_labels(project=project, issue_number=issue_number)
    else:
        labels_on_issue = set()
    strip = {lbl for lbl in labels_on_issue if lbl.startswith("foreman:")}
    try:
        ticket = repo.get_ticket_by_issue(
            project=project, issue_number=issue_number,
        )
        delete_ticket_id = ticket.id
    except TicketNotFoundError:
        delete_ticket_id = None
    return ResetPlan(
        project=project,
        issue_number=issue_number,
        spec_pr=spec_pr,
        impl_pr=impl_pr,
        delete_branches=delete_branches,
        prune_worktrees=not keep_worktree,
        strip_labels=strip,
        delete_ticket_id=delete_ticket_id,
        apply_plan_label=retrigger,
    )
```

(Note on `get_issue_labels`: it's already on FakeGitProvider per `git_provider.py:180`. Promote it to the GitProvider Protocol — and add it to RoutingGitProvider + PyGithubGitProvider — as a follow-up task in the same commit.)

- [ ] **Step 4: Promote `get_issue_labels` to the GitProvider Protocol**

In `packages/foreman/src/foreman/v4/git_provider.py`, add to `GitProvider` Protocol after `find_open_pr_by_head_branch`:

```python
    def get_issue_labels(
        self, *, project: str, issue_number: int,
    ) -> set[str]:
        """Return the current label set on this issue.

        Used by ``foreman reset`` to discover which ``foreman:*`` labels
        are currently on the issue (so the operator-facing plan can
        enumerate them by name).
        """
        ...
```

In `packages/foreman/src/foreman/v4/pygithub_git_provider.py`, add:

```python
    def get_issue_labels(
        self, *, project: str, issue_number: int,
    ) -> set[str]:
        issue = self._repo().get_issue(issue_number)
        return {label.name for label in issue.labels}
```

In `packages/foreman/src/foreman/v4/routing_git_provider.py`, add:

```python
    def get_issue_labels(
        self, *, project: str, issue_number: int,
    ) -> set[str]:
        return self._resolve(project).get_issue_labels(
            project=project, issue_number=issue_number,
        )
```

- [ ] **Step 5: Add a PyGithub test for `get_issue_labels`**

In `packages/foreman/tests/v4/test_pygithub_git_provider.py`:

```python
def test_get_issue_labels_returns_label_names():
    fake_label_a = MagicMock(name="foreman:state-failed")
    fake_label_a.name = "foreman:state-failed"
    fake_label_b = MagicMock(name="bug")
    fake_label_b.name = "bug"
    fake_issue = MagicMock(labels=[fake_label_a, fake_label_b])
    fake_repo = MagicMock()
    fake_repo.get_issue.return_value = fake_issue
    fake_github = MagicMock()
    fake_github.get_repo.return_value = fake_repo
    provider = PyGithubGitProvider(
        github_factory=lambda: fake_github,
        repo_full_name="org/repo",
    )
    labels = provider.get_issue_labels(project="ignored", issue_number=180)
    assert labels == {"foreman:state-failed", "bug"}
```

And in `packages/foreman/tests/v4/test_routing_git_provider.py`:

```python
def test_get_issue_labels_dispatches_to_per_project_provider():
    a = FakeGitProvider()
    b = FakeGitProvider()
    b.seed_issue_labels(
        project="b", issue_number=1, labels={"foreman:plan"},
    )
    router = RoutingGitProvider(providers={"a": a, "b": b})
    assert router.get_issue_labels(project="b", issue_number=1) == {"foreman:plan"}
```

- [ ] **Step 6: Update `_discover` to drop the getattr fallback**

In `packages/foreman/src/foreman/v4/cli/mutations.py`, replace the `getattr(git, "get_issue_labels", None)` block with the direct call:

```python
    labels_on_issue = git.get_issue_labels(
        project=project, issue_number=issue_number,
    )
    strip = {lbl for lbl in labels_on_issue if lbl.startswith("foreman:")}
```

- [ ] **Step 7: Run all new tests to verify pass**

Run: `cd e:/workspaces/ai/agents/foreman && uv run pytest packages/foreman/tests/v4/cli/test_mutation_commands.py -k discover packages/foreman/tests/v4/test_pygithub_git_provider.py::test_get_issue_labels_returns_label_names packages/foreman/tests/v4/test_routing_git_provider.py::test_get_issue_labels_dispatches_to_per_project_provider -v`

Expected: PASS (3 discover tests + 2 get_issue_labels tests = 5 tests).

- [ ] **Step 8: Commit**

```bash
cd e:/workspaces/ai/agents/foreman
git add packages/foreman/src/foreman/v4/cli/mutations.py \
        packages/foreman/src/foreman/v4/git_provider.py \
        packages/foreman/src/foreman/v4/pygithub_git_provider.py \
        packages/foreman/src/foreman/v4/routing_git_provider.py \
        packages/foreman/tests/v4/cli/test_mutation_commands.py \
        packages/foreman/tests/v4/test_pygithub_git_provider.py \
        packages/foreman/tests/v4/test_routing_git_provider.py
git commit -m "feat: add ResetPlan dataclass + _discover helper + promote get_issue_labels to Protocol"
```

---

### Task 9: Plan renderer + execute phase + `cmd_reset` typer wire-up

**Files:**
- Modify: `packages/foreman/tests/v4/cli/test_mutation_commands.py`
- Modify: `packages/foreman/src/foreman/v4/cli/mutations.py`
- Modify: `packages/foreman/src/foreman/v4/cli/__init__.py`

- [ ] **Step 1: Write failing tests for the full command**

Append to `packages/foreman/tests/v4/cli/test_mutation_commands.py`:

```python
def _full_reset_ctx(tmp_path):
    """Build a CliContext with all the deps cmd_reset reads."""
    from foreman.v4.git_provider import FakeGitProvider, PRState
    from foreman.worktree import WorktreeManager

    repo = SqliteTicketRepository.in_memory()
    repo.create_ticket(
        project="agent_core", issue_number=180, now=dt.datetime(2026, 6, 17),
    )
    git = FakeGitProvider()
    git.seed_issue_labels(
        project="agent_core", issue_number=180,
        labels={"foreman:state-failed", "bug"},
    )
    git.seed_branch(project="agent_core", branch_name="foreman/issue-180")
    git.set_pr_state(
        project="agent_core", pr_number=19,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    git.set_pr_head_branch(
        project="agent_core", pr_number=19, branch_name="foreman/issue-180",
    )
    # Seed both worktrees.
    wt_root = tmp_path / "worktrees"
    (wt_root / "agent_core" / "issue-180").mkdir(parents=True)
    (wt_root / "agent_core" / "impl-180").mkdir(parents=True)
    wt = WorktreeManager(worktrees_root=wt_root)
    ctx = build_cli_context(repo=repo, git=git)
    return ctx, repo, git, wt, wt_root


def test_reset_dry_run_prints_plan_no_mutations(tmp_path):
    ctx, repo, git, wt, wt_root = _full_reset_ctx(tmp_path)
    runner = CliRunner()
    # cmd_reset reads WorktreeManager from a module-level seam so tests
    # can inject the temp-rooted one. See cmd_reset implementation.
    result = runner.invoke(
        app,
        [
            "reset",
            "--project", "agent_core",
            "--issue-number", "180",
            "--dry-run",
            "--worktrees-root", str(wt_root),
        ],
        obj=ctx,
    )
    assert result.exit_code == 0
    # Plan should mention each action by name.
    assert "Close PR #19" in result.stdout
    assert "Delete remote branch foreman/issue-180" in result.stdout
    assert "Prune local worktree" in result.stdout
    assert "Delete ticket row" in result.stdout
    assert "Apply foreman:plan" in result.stdout
    # No mutations occurred.
    assert git.closed_prs == set()
    assert git.deleted_branches == set()
    assert (wt_root / "agent_core" / "issue-180").exists()
    # Ticket row still there.
    repo.get_ticket_by_issue(project="agent_core", issue_number=180)


def test_reset_yes_executes_full_plan(tmp_path):
    ctx, repo, git, wt, wt_root = _full_reset_ctx(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reset",
            "--project", "agent_core",
            "--issue-number", "180",
            "--yes",
            "--worktrees-root", str(wt_root),
        ],
        obj=ctx,
    )
    assert result.exit_code == 0, result.stdout
    assert ("agent_core", 19) in git.closed_prs
    assert ("agent_core", "foreman/issue-180") in git.deleted_branches
    assert ("agent_core", "foreman/impl-180") in git.deleted_branches
    assert not (wt_root / "agent_core" / "issue-180").exists()
    assert not (wt_root / "agent_core" / "impl-180").exists()
    # Foreman labels stripped.
    remaining = git.get_issue_labels(project="agent_core", issue_number=180)
    assert "foreman:state-failed" not in remaining
    # foreman:plan re-applied.
    assert "foreman:plan" in remaining
    # Ticket row gone.
    with pytest.raises(TicketNotFoundError):
        repo.get_ticket_by_issue(project="agent_core", issue_number=180)


def test_reset_prompt_declined_aborts(tmp_path):
    """Default (no --yes) prompts; declining must abort with no mutations."""
    ctx, repo, git, wt, wt_root = _full_reset_ctx(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reset",
            "--project", "agent_core",
            "--issue-number", "180",
            "--worktrees-root", str(wt_root),
        ],
        obj=ctx,
        input="n\n",
    )
    # Decline → exit with non-zero per typer.Abort convention.
    assert result.exit_code != 0
    assert git.closed_prs == set()
    assert (wt_root / "agent_core" / "issue-180").exists()


def test_reset_keep_pr_skips_pr_close(tmp_path):
    ctx, repo, git, wt, wt_root = _full_reset_ctx(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reset",
            "--project", "agent_core",
            "--issue-number", "180",
            "--yes",
            "--keep-pr",
            "--worktrees-root", str(wt_root),
        ],
        obj=ctx,
    )
    assert result.exit_code == 0
    assert git.closed_prs == set()  # PR untouched.
    assert ("agent_core", "foreman/issue-180") in git.deleted_branches  # but branch still went.


def test_reset_keep_worktree_skips_worktree_prune(tmp_path):
    ctx, repo, git, wt, wt_root = _full_reset_ctx(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reset",
            "--project", "agent_core",
            "--issue-number", "180",
            "--yes",
            "--keep-worktree",
            "--worktrees-root", str(wt_root),
        ],
        obj=ctx,
    )
    assert result.exit_code == 0
    assert (wt_root / "agent_core" / "issue-180").exists()


def test_reset_no_retrigger_skips_apply_plan_label(tmp_path):
    ctx, repo, git, wt, wt_root = _full_reset_ctx(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "reset",
            "--project", "agent_core",
            "--issue-number", "180",
            "--yes",
            "--no-retrigger",
            "--worktrees-root", str(wt_root),
        ],
        obj=ctx,
    )
    assert result.exit_code == 0
    remaining = git.get_issue_labels(project="agent_core", issue_number=180)
    assert "foreman:plan" not in remaining


def test_reset_idempotent_second_run_minimal(tmp_path):
    """Run reset twice in a row — the second run finds nothing to do."""
    ctx, repo, git, wt, wt_root = _full_reset_ctx(tmp_path)
    runner = CliRunner()
    first = runner.invoke(
        app, ["reset", "--project", "agent_core", "--issue-number", "180",
              "--yes", "--worktrees-root", str(wt_root)],
        obj=ctx,
    )
    assert first.exit_code == 0
    second = runner.invoke(
        app, ["reset", "--project", "agent_core", "--issue-number", "180",
              "--yes", "--worktrees-root", str(wt_root)],
        obj=ctx,
    )
    assert second.exit_code == 0
    # Second run plan should ONLY contain the foreman:plan re-apply step
    # (everything else is already clean). The label re-apply is idempotent
    # at the GitProvider layer.
    assert "Apply foreman:plan" in second.stdout
    # Branches: delete_branch is idempotent, so both calls record again.
    # That's fine — test asserts the command succeeded twice.


def test_reset_no_row_still_cleans_debris(tmp_path):
    """The B payoff: row already gone, debris still cleaned."""
    ctx, repo, git, wt, wt_root = _full_reset_ctx(tmp_path)
    # Manually delete the row to simulate "operator nuked it earlier."
    ticket = repo.get_ticket_by_issue(project="agent_core", issue_number=180)
    repo.delete_ticket(ticket.id)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["reset", "--project", "agent_core", "--issue-number", "180",
         "--yes", "--worktrees-root", str(wt_root)],
        obj=ctx,
    )
    assert result.exit_code == 0
    assert ("agent_core", "foreman/issue-180") in git.deleted_branches
    assert not (wt_root / "agent_core" / "issue-180").exists()
    # foreman:plan re-applied.
    assert "foreman:plan" in git.get_issue_labels(
        project="agent_core", issue_number=180,
    )


def test_reset_partial_failure_continues_and_exits_nonzero(tmp_path, monkeypatch):
    """One step fails → other steps still run, exit code is 1."""
    ctx, repo, git, wt, wt_root = _full_reset_ctx(tmp_path)

    # Force close_pr to explode on PR 19 — every other step must still execute.
    original_close_pr = git.close_pr
    def boom_close_pr(*, project, pr_number):
        if pr_number == 19:
            raise RuntimeError("synthetic PR close failure")
        return original_close_pr(project=project, pr_number=pr_number)
    monkeypatch.setattr(git, "close_pr", boom_close_pr)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["reset", "--project", "agent_core", "--issue-number", "180",
         "--yes", "--worktrees-root", str(wt_root)],
        obj=ctx,
    )
    assert result.exit_code == 1
    assert "fail" in result.stdout.lower()
    # But the branch / worktree / row / label steps still ran.
    assert ("agent_core", "foreman/issue-180") in git.deleted_branches
    assert not (wt_root / "agent_core" / "issue-180").exists()
```

Also add the required import at the top of the test file:

```python
from foreman.v4.repository import TicketNotFoundError
```

- [ ] **Step 2: Run new tests to verify they fail**

Run: `cd e:/workspaces/ai/agents/foreman && uv run pytest packages/foreman/tests/v4/cli/test_mutation_commands.py -k reset -v`

Expected: FAIL — no `reset` command registered on `app`.

- [ ] **Step 3: Implement `_render_plan`, `_execute`, and `cmd_reset` in mutations.py**

Add these to `packages/foreman/src/foreman/v4/cli/mutations.py` after `_discover`:

```python
import shutil  # add to existing imports section
from pathlib import Path

from foreman.worktree import WorktreeManager


def _plan_steps(plan: ResetPlan) -> list[tuple[str, str]]:
    """Return ordered (label, kind) tuples for the plan's actionable steps.

    ``kind`` is a stable token the executor dispatches on. Steps that
    are off (no PR found, no row, --keep-* set) are filtered here so the
    renderer + executor walk the same list.
    """
    steps: list[tuple[str, str]] = []
    if plan.spec_pr is not None:
        steps.append((f"Close PR #{plan.spec_pr} (spec)", "close_spec_pr"))
    if plan.impl_pr is not None:
        steps.append((f"Close PR #{plan.impl_pr} (impl)", "close_impl_pr"))
    for branch in plan.delete_branches:
        steps.append((f"Delete remote branch {branch}", f"delete_branch:{branch}"))
    if plan.prune_worktrees:
        steps.append((
            f"Prune local worktree {plan.project}/issue-{plan.issue_number}/",
            "prune_worktrees",
        ))
    if plan.strip_labels:
        steps.append((
            f"Strip {len(plan.strip_labels)} foreman:* labels "
            f"({', '.join(sorted(plan.strip_labels))})",
            "strip_labels",
        ))
    if plan.delete_ticket_id is not None:
        steps.append((
            f"Delete ticket row id={plan.delete_ticket_id} from state.db",
            "delete_ticket",
        ))
    if plan.apply_plan_label:
        steps.append(("Apply foreman:plan label", "apply_plan_label"))
    return steps


def _render_plan(plan: ResetPlan, steps: list[tuple[str, str]]) -> str:
    """Format the discovery plan for the operator. Pure string-builder."""
    if not steps:
        return f"Nothing to do for {plan.project}#{plan.issue_number}.\n"
    lines = [f"Resetting {plan.project}#{plan.issue_number}:", ""]
    for n, (label, _) in enumerate(steps, 1):
        lines.append(f"  {n}. {label}")
    lines.append("")
    return "\n".join(lines)


def _execute(
    plan: ResetPlan,
    steps: list[tuple[str, str]],
    *,
    git: GitProvider,
    repo,
    wt: WorktreeManager,
) -> int:
    """Walk the plan, printing per-step status. Returns count of failures."""
    failures = 0
    total = len(steps)
    for n, (label, kind) in enumerate(steps, 1):
        prefix = f"  [{n}/{total}] {label}"
        try:
            if kind == "close_spec_pr":
                assert plan.spec_pr is not None
                git.close_pr(project=plan.project, pr_number=plan.spec_pr)
            elif kind == "close_impl_pr":
                assert plan.impl_pr is not None
                git.close_pr(project=plan.project, pr_number=plan.impl_pr)
            elif kind.startswith("delete_branch:"):
                branch = kind.split(":", 1)[1]
                git.delete_branch(project=plan.project, branch_name=branch)
            elif kind == "prune_worktrees":
                wt.prune(project=plan.project, issue_number=plan.issue_number)
            elif kind == "strip_labels":
                git.remove_labels(
                    project=plan.project,
                    issue_number=plan.issue_number,
                    labels=plan.strip_labels,
                )
            elif kind == "delete_ticket":
                assert plan.delete_ticket_id is not None
                repo.delete_ticket(plan.delete_ticket_id)
            elif kind == "apply_plan_label":
                git.add_labels(
                    project=plan.project,
                    issue_number=plan.issue_number,
                    labels={"foreman:plan"},
                )
            else:
                raise AssertionError(f"unknown step kind: {kind}")
            typer.echo(f"{prefix} ... ok")
        except Exception as exc:  # noqa: BLE001 — operator-facing tool wants
            # every step's failure visible without aborting the rest.
            failures += 1
            typer.echo(f"{prefix} ... fail: {exc}")
    return failures


def cmd_reset(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project"),
    issue_number: int = typer.Option(..., "--issue-number", min=1),
    keep_pr: bool = typer.Option(
        False, "--keep-pr", help="Don't close open spec/impl PRs.",
    ),
    keep_worktree: bool = typer.Option(
        False, "--keep-worktree", help="Don't rmtree local worktrees.",
    ),
    no_retrigger: bool = typer.Option(
        False, "--no-retrigger", help="Don't re-apply foreman:plan at end.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print plan, exit. No prompt, no execution.",
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Skip the interactive confirmation.",
    ),
    worktrees_root: Path = typer.Option(
        Path.home() / ".foreman" / "worktrees",
        "--worktrees-root",
        help="Override worktree root path (test seam + alt-install support).",
    ),
) -> None:
    """Fully reset a foreman ticket: labels + branches + PRs + worktrees + row.

    Discovery phase reads current state. Confirmation prompts the operator
    (skip with --yes). Execute walks the plan; per-step failures are
    surfaced but don't halt subsequent steps.
    """
    repo = ctx.obj.repo
    git = ctx.obj.git
    if git is None:
        typer.echo("reset requires a GitProvider in the CLI context", err=True)
        raise typer.Exit(code=1)
    wt = WorktreeManager(worktrees_root=worktrees_root)
    plan = _discover(
        git=git, repo=repo,
        project=project, issue_number=issue_number,
        keep_pr=keep_pr, keep_worktree=keep_worktree,
        retrigger=not no_retrigger,
    )
    steps = _plan_steps(plan)
    typer.echo(_render_plan(plan, steps))
    if dry_run:
        return
    if not yes and steps:
        typer.confirm("Proceed?", abort=True)
    failures = _execute(plan, steps, git=git, repo=repo, wt=wt)
    total = len(steps)
    if failures:
        typer.echo(
            f"\ncompleted {total - failures}/{total} steps; {failures} failed",
        )
        raise typer.Exit(code=1)
    typer.echo(
        f"\nDone. {project}#{issue_number} reset. "
        f"Daemon will pick up on next poll.",
    )
```

- [ ] **Step 4: Register `reset` in the typer app**

In `packages/foreman/src/foreman/v4/cli/__init__.py`, in the imports block:

```python
from foreman.v4.cli.mutations import (
    cmd_drop,
    cmd_enqueue,
    cmd_hold,
    cmd_reset,    # NEW
    cmd_resume,
    cmd_retry,
    cmd_set_state,
    cmd_skip,
)
```

And in the registration block (alongside the other `app.command(...)` calls):

```python
app.command("reset")(cmd_reset)
```

- [ ] **Step 5: Run all new tests to verify pass**

Run: `cd e:/workspaces/ai/agents/foreman && uv run pytest packages/foreman/tests/v4/cli/test_mutation_commands.py -k reset -v`

Expected: PASS (all 9 new tests).

- [ ] **Step 6: Run full v4 suite to catch regressions**

Run: `cd e:/workspaces/ai/agents/foreman && uv run pytest packages/foreman/tests/v4/ -q`

Expected: All v4 tests pass. If anything elsewhere broke, fix it before moving on.

- [ ] **Step 7: Run `just check` for the pre-push gate**

Run: `cd e:/workspaces/ai/agents/foreman && just check`

Expected: PASS — ruff clean, mypy clean, lint-imports clean, pytest green.

If `mypy` complains about `repo` lacking a type annotation in `_discover` / `_execute`, narrow it: `from foreman.v4.repository import TicketRepository` and use `repo: TicketRepository`.

- [ ] **Step 8: Commit**

```bash
cd e:/workspaces/ai/agents/foreman
git add packages/foreman/src/foreman/v4/cli/mutations.py \
        packages/foreman/src/foreman/v4/cli/__init__.py \
        packages/foreman/tests/v4/cli/test_mutation_commands.py
git commit -m "feat: add foreman reset CLI command"
```

---

### Task 10: Dogfood the new command on `agent_core#180`

This task is operator-facing and not test-encoded. Run the command for real against the stuck ticket to confirm the autonomous loop picks it up with the broader Discord-event-gate scope (the spec was extended by Wren's comment on the issue).

- [ ] **Step 1: Confirm daemon is running**

Run: `tasklist | findstr foreman.exe`

Expected: one `foreman.exe` process. If not running, start the daemon with `foreman daemon start` first.

- [ ] **Step 2: Dry-run reset on agent_core#180**

```bash
cd e:/workspaces/ai/agents/foreman
foreman reset --project agent_core --issue-number 180 --dry-run
```

Expected output: a numbered plan listing the spec PR (#19 or whatever exists), the branches, the worktrees, the labels currently on the issue, the ticket row id, and the foreman:plan apply step. No mutations occur.

- [ ] **Step 3: Execute the reset**

```bash
foreman reset --project agent_core --issue-number 180 --yes
```

Expected: each step prints `ok`, final line "Done. agent_core#180 reset. Daemon will pick up on next poll."

- [ ] **Step 4: Watch the daemon log for Planner pickup**

Run (in a separate shell): `Get-Content C:/Users/jeffr/.foreman/v4/logs/transitions.jsonl -Wait -Tail 0`

Expected (within ~60s of reset): `state_entered: Planning ticket_id=<new>`. Planner runs; its summary should reference message_delete in addition to reaction_add (proving the broader-scope comment was absorbed).

- [ ] **Step 5: No commit**

This is a dogfood-only task. No code changes. If anything's busted, file a fix ticket — do NOT commit hacks.

---

## Self-review

**Spec coverage:**

| Spec section | Plan task(s) | Notes |
|---|---|---|
| CLI surface (5 flags) | Task 9 | All 5 flags wired in `cmd_reset` |
| Plan-and-confirm flow | Task 9 | `_render_plan` + `typer.confirm` |
| GitProvider.delete_branch | Tasks 2, 5, 6 | Protocol + Fake + PyGithub + Routing |
| GitProvider.close_pr | Tasks 3, 5, 6 | Same fan-out |
| GitProvider.find_open_pr_by_head_branch | Tasks 4, 5, 6 | Same fan-out |
| TicketRepository.delete_ticket | Task 1 | Contract + InMemory + SQLite |
| WorktreeManager.prune | Task 7 | Test file + implementation |
| ResetPlan + _discover | Task 8 | Includes `get_issue_labels` promotion |
| cmd_reset orchestration | Task 9 | Render + execute + per-step try/except |
| Test strategy (substrate + cmd_reset) | All tasks | Each substrate addition has tests; cmd_reset has 9 cases |
| Out of scope (bulk, audit, GC, substrate bugs) | n/a | Explicitly skipped |

**Placeholder scan:** No TBDs / TODOs / "handle edge cases" placeholders found. Every step has concrete code or commands.

**Type consistency:** `ResetPlan` field names used identically in `_discover`, `_plan_steps`, `_render_plan`, `_execute`. Method signatures (`delete_branch`, `close_pr`, `find_open_pr_by_head_branch`) consistent across Protocol, Fake, Routing, PyGithub, and the three test files.

**Notable mid-plan addition:** Task 8 promotes `get_issue_labels` from FakeGitProvider-only to the GitProvider Protocol (so the production path works too). This was implied by the spec's "discovery phase ... labels currently on issue" but not explicitly called out. Caught during plan-writing; baked into Task 8.

---

## Execution

After implementation is complete, the `foreman reset` command can be used immediately on `agent_core#180` (Task 10) to retrigger the autonomous loop with the broader Discord-event-gate scope.
