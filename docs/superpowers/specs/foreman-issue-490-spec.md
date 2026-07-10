# Spec: freeze `_SIBLING_TERMINAL_LABELS` constant + widen `LabelWriter` params to `AbstractSet[str]` (issue #490)

## Goal

Two small type-hygiene fixes to the `LabelWriter` label-set API, surfaced by the `frozenset` constant introduced in #486. First, flip `_SIBLING_TERMINAL_LABELS` from `set[str]` to `frozenset[str]` to match the module's own `_FIRST_STATES` convention and prevent accidental mutation of a shared-state constant. Second, widen `add_labels` and `remove_labels` params from `labels: set[str]` to `labels: AbstractSet[str]` across all five protocol/implementation sites so that a `frozenset` caller (the `_SIBLING_TERMINAL_LABELS` scrub) satisfies mypy without overstating mutability requirements. See issue [#490](https://github.com/jeffrichley/foreman/issues/490).

## Acceptance criteria

- `_SIBLING_TERMINAL_LABELS` in `packages/foreman/src/foreman/v4/observers/label_observability.py` is annotated `frozenset[str]` (not `set[str]`).
- `add_labels` and `remove_labels` on `LabelWriter` (in `label_observability.py`), `GitProvider` Protocol, `FakeGitProvider`, `PyGithubGitProvider`, and `RoutingGitProvider` all accept `labels: AbstractSet[str]`.
- `_RecordingWriter` and `_BoomWriter` in `packages/foreman/tests/v4/observers/test_label_observability.py` mirror the widened param.
- `from collections.abc import Set as AbstractSet` is the import used in every file that touches the param type.
- `just check` (mypy + tests) exits zero after all changes.
- The `_SIBLING_TERMINAL_LABELS` constant remains the live caller that passes a `frozenset[str]` to `remove_labels` — this locks in the widening and prevents silent regression.
- (Optional) Module-level `dict`/`list` constants that are read-only (not extension points) are wrapped in `MappingProxyType`/`tuple` where cheap; `CLAUDE.md` gains a one-line note that module-level collection constants must be immutable.

## Approach

**Pattern (Decision 4):** No GoF pattern fits — this is a straightforward type annotation precision improvement. The applicable Google engineering principle is **DIP** ("depend on abstractions, not concretions"): `add_labels` and `remove_labels` only *iterate* their `labels` argument to make GitHub API calls — they never mutate it. Requiring `set[str]` overstates the interface; `AbstractSet[str]` (the read-only set ABC from `collections.abc`) is the honest minimal contract. Producers (`_SIBLING_TERMINAL_LABELS`, literal `{...}` set expressions) can hand out whichever concrete type they choose; consumers accept the abstract interface.

**Why `frozenset` for the constant.** `label_observability.py` already uses `frozenset` for `_FIRST_STATES` (line 86). Having a plain `set` on line 95 for `_SIBLING_TERMINAL_LABELS` is an inconsistency with the module's own established convention: module-level constants should be immutable so accidental `.add()`/`.discard()` anywhere corrupts state for every caller.

**Why `AbstractSet[str]` rather than `frozenset[str]`.** Flipping the param to `frozenset[str]` would break callers that pass a plain `set[str]` literal — e.g., the `{_state_label(event.state_name)}` expressions on lines 134 and 141–142 of `label_observability.py`, and many existing tests. `AbstractSet[str]` accepts both `set[str]` and `frozenset[str]` and any other `Set` ABC implementation, making zero callers break.

**The canonical import.** All other `collections.abc` imports in the codebase follow `from collections.abc import <Name>` (e.g., `Mapping` in `routing_git_provider.py`, `Callable` in `event_bus.py`). The widening should follow the same pattern: `from collections.abc import Set as AbstractSet`. The alias `AbstractSet` is conventional and avoids shadowing the built-in `set`.

**Where the signatures live.** There are six classes/protocols containing the two methods in five files:

| File | Class |
|---|---|
| `label_observability.py` | `LabelWriter` Protocol |
| `git_provider.py` | `GitProvider` Protocol |
| `git_provider.py` | `FakeGitProvider` concrete impl |
| `pygithub_git_provider.py` | `PyGithubGitProvider` concrete impl |
| `routing_git_provider.py` | `RoutingGitProvider` concrete impl |
| `test_label_observability.py` | `_RecordingWriter` test fake |
| `test_label_observability.py` | `_BoomWriter` test fake |

`FakeGitProvider.add_labels` calls `current.update(labels)` and `FakeGitProvider.remove_labels` calls `current.difference_update(labels)`. Both `set.update()` and `set.difference_update()` accept any iterable, so they work correctly with `AbstractSet[str]` without code changes beyond the annotation.

**Mypy as the gate.** The correctness signal for this change is `just check` (which runs mypy). There is no behavioral change to test at runtime — the fix is entirely a type annotation improvement. The live `_SIBLING_TERMINAL_LABELS` scrub in `_on_state_entered` already exercises the `frozenset → AbstractSet` path; the three regression tests added in #486 (`test_done_entry_scrubs_sibling_terminal_labels`, etc.) will continue to pass unchanged and also serve as the acceptance-test suite.

**Optional: other mutable module-level constants.** A sweep of the codebase found several additional `dict`/`list` module-level constants that are never mutated after definition. Excluding `MERGE_HEALERS` (explicitly documented as an extension point to which callers `append`), candidates for cheap freezing include:

- `_ROLE_TO_INVOCATION: dict[str, _Invocation]` in `subprocess_dispatcher.py` → `MappingProxyType`
- `_EVENT_NAMES: dict[type[Event], tuple[str, int]]` in `structured_log.py` → `MappingProxyType`
- `_STATE_NAME_TO_ROLE: dict[str, str]` in `terminal_landing.py` → `MappingProxyType`
- `_FORMATTERS: dict[str, type[OutputFormatter]]` in `cli/formatters.py` → `MappingProxyType`

These are not required; include them only if cheap (a single `MappingProxyType(...)` wrap each). `CLAUDE.md` should receive a one-line note under `## Conventions`: `"module-level collection constants must be immutable (frozenset/tuple/MappingProxyType)"`.

## Sub-requests (topologically sorted)

1. **Flip `_SIBLING_TERMINAL_LABELS` to `frozenset[str]`** in `packages/foreman/src/foreman/v4/observers/label_observability.py` (line 95):

   ```python
   _SIBLING_TERMINAL_LABELS: frozenset[str] = frozenset({
       _state_label("NeedsHelp"),
       _state_label("Failed"),
   })
   ```

2. **Widen `LabelWriter` Protocol** in `packages/foreman/src/foreman/v4/observers/label_observability.py`:

   Add `from collections.abc import Set as AbstractSet` to imports (alongside the existing `from typing import Protocol`). Change both method params:

   ```python
   from collections.abc import Set as AbstractSet
   from typing import Protocol

   class LabelWriter(Protocol):
       def add_labels(
           self, *, project: str, issue_number: int, labels: AbstractSet[str]
       ) -> None: ...

       def remove_labels(
           self, *, project: str, issue_number: int, labels: AbstractSet[str]
       ) -> None: ...
   ```

3. **Widen `GitProvider` Protocol** in `packages/foreman/src/foreman/v4/git_provider.py`:

   Add `from collections.abc import Set as AbstractSet` import. Change both Protocol methods:

   ```python
   def add_labels(
       self, *, project: str, issue_number: int, labels: AbstractSet[str]
   ) -> None: ...

   def remove_labels(
       self, *, project: str, issue_number: int, labels: AbstractSet[str]
   ) -> None: ...
   ```

4. **Widen `FakeGitProvider`** in the same file (`git_provider.py`). The import is already added in sub-request 3. Change the param annotations on `add_labels` and `remove_labels` (the body is unchanged — `set.update()` and `set.difference_update()` accept any iterable):

   ```python
   def add_labels(
       self, *, project: str, issue_number: int, labels: AbstractSet[str],
   ) -> None:
       current = self._issue_labels.setdefault((project, issue_number), set())
       current.update(labels)

   def remove_labels(
       self, *, project: str, issue_number: int, labels: AbstractSet[str],
   ) -> None:
       current = self._issue_labels.get((project, issue_number))
       if current is None:
           return
       current.difference_update(labels)
   ```

5. **Widen `PyGithubGitProvider`** in `packages/foreman/src/foreman/v4/pygithub_git_provider.py`:

   Add `from collections.abc import Set as AbstractSet` import. Change both method params (bodies unchanged — `sorted(labels)` accepts any iterable):

   ```python
   def add_labels(
       self, *, project: str, issue_number: int, labels: AbstractSet[str],
   ) -> None: ...  # body unchanged

   def remove_labels(
       self, *, project: str, issue_number: int, labels: AbstractSet[str],
   ) -> None: ...  # body unchanged
   ```

6. **Widen `RoutingGitProvider`** in `packages/foreman/src/foreman/v4/routing_git_provider.py`:

   Add `from collections.abc import Set as AbstractSet` alongside the existing `from collections.abc import Mapping` import (merge into one statement). Change both method params (bodies pass `labels` straight through — no changes needed beyond annotation):

   ```python
   from collections.abc import Mapping, Set as AbstractSet

   # ...

   def add_labels(
       self, *, project: str, issue_number: int, labels: AbstractSet[str],
   ) -> None:
       self._resolve(project).add_labels(
           project=project, issue_number=issue_number, labels=labels,
       )

   def remove_labels(
       self, *, project: str, issue_number: int, labels: AbstractSet[str],
   ) -> None:
       self._resolve(project).remove_labels(
           project=project, issue_number=issue_number, labels=labels,
       )
   ```

7. **Widen `_RecordingWriter` and `_BoomWriter`** in `packages/foreman/tests/v4/observers/test_label_observability.py`:

   Add `from collections.abc import Set as AbstractSet` import. Change the param annotation on both methods of both classes (stored type in `add_calls` / `remove_calls` stays `set[str]` since each recording body explicitly calls `set(labels)` on the incoming value):

   ```python
   from collections.abc import Set as AbstractSet

   class _RecordingWriter:
       def add_labels(
           self, *, project: str, issue_number: int, labels: AbstractSet[str]
       ) -> None:
           self.add_calls.append((project, issue_number, set(labels)))
           self.all_calls.append(("add", project, issue_number, set(labels)))

       def remove_labels(
           self, *, project: str, issue_number: int, labels: AbstractSet[str]
       ) -> None:
           self.remove_calls.append((project, issue_number, set(labels)))
           self.all_calls.append(("remove", project, issue_number, set(labels)))


   class _BoomWriter:
       def add_labels(
           self, *, project: str, issue_number: int, labels: AbstractSet[str]
       ) -> None:
           raise RuntimeError("network down")

       def remove_labels(
           self, *, project: str, issue_number: int, labels: AbstractSet[str]
       ) -> None:
           raise RuntimeError("network down")
   ```

8. **Run `just check`** — mypy must report zero errors and all tests must pass. The three `test_done_entry_scrubs_sibling_terminal_labels`, `test_needshelp_terminal_does_not_scrub_siblings`, and `test_failed_terminal_does_not_scrub_siblings` tests added in #486 provide the frozenset-caller coverage.

9. **(Optional)** Wrap `_ROLE_TO_INVOCATION`, `_EVENT_NAMES`, `_STATE_NAME_TO_ROLE`, and `_FORMATTERS` in `MappingProxyType`. Add one-line note to `CLAUDE.md` under `## Conventions`: `"module-level collection constants must be immutable (frozenset/tuple/MappingProxyType)"`. **Do NOT freeze `MERGE_HEALERS`** — its module docstring explicitly documents it as an extension point where callers `append` new healers.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/v4/observers/label_observability.py` | Add `from collections.abc import Set as AbstractSet`; change `_SIBLING_TERMINAL_LABELS` annotation to `frozenset[str]` and value to `frozenset({...})`; widen `LabelWriter.add_labels` and `LabelWriter.remove_labels` params |
| `packages/foreman/src/foreman/v4/git_provider.py` | Add `from collections.abc import Set as AbstractSet`; widen `GitProvider.add_labels`, `GitProvider.remove_labels`, `FakeGitProvider.add_labels`, `FakeGitProvider.remove_labels` params |
| `packages/foreman/src/foreman/v4/pygithub_git_provider.py` | Add `from collections.abc import Set as AbstractSet`; widen `PyGithubGitProvider.add_labels` and `PyGithubGitProvider.remove_labels` params |
| `packages/foreman/src/foreman/v4/routing_git_provider.py` | Merge `Set as AbstractSet` into existing `collections.abc` import; widen `RoutingGitProvider.add_labels` and `RoutingGitProvider.remove_labels` params |
| `packages/foreman/tests/v4/observers/test_label_observability.py` | Add `from collections.abc import Set as AbstractSet`; widen `_RecordingWriter.add_labels`, `_RecordingWriter.remove_labels`, `_BoomWriter.add_labels`, `_BoomWriter.remove_labels` params |
| `CLAUDE.md` *(optional)* | Add one-line convention note under `## Conventions` |
| `packages/foreman/src/foreman/v4/subprocess_dispatcher.py` *(optional)* | Wrap `_ROLE_TO_INVOCATION` in `MappingProxyType` |
| `packages/foreman/src/foreman/v4/observers/structured_log.py` *(optional)* | Wrap `_EVENT_NAMES` in `MappingProxyType` |
| `packages/foreman/src/foreman/v4/observers/terminal_landing.py` *(optional)* | Wrap `_STATE_NAME_TO_ROLE` in `MappingProxyType` |
| `packages/foreman/src/foreman/v4/cli/formatters.py` *(optional)* | Wrap `_FORMATTERS` in `MappingProxyType` |

## Alternatives considered

1. **Flip the param to `frozenset[str]` instead of `AbstractSet[str]`.** Would require changing every caller that currently passes a plain `set[str]` literal (e.g., `{_state_label(event.state_name)}` on lines 134, 141–142 of `label_observability.py`, plus many tests). Net code-churn is higher with no semantic gain — the writer only iterates the set. Rejected.

2. **Leave `_SIBLING_TERMINAL_LABELS` as `set[str]` and widen params to `AbstractSet[str]` separately.** The widening is still correct, and mypy stops complaining even without flipping the constant. But the original inconsistency with `_FIRST_STATES` remains, and the mutable shared-state footgun lives on. Rejected — doing both together is exactly the scope the issue asks for.

3. **Do nothing; accept the `set[str]` / `frozenset[str]` inconsistency.** The existing `_FIRST_STATES = frozenset(...)` convention makes the inconsistency visible every time a reader sees both constants side-by-side. The mypy error is real (not hypothetical — it fires today if you flip the constant). Rejected.

## Open questions

None. The affected files, exact annotation to use (`AbstractSet[str]`), the import form (`from collections.abc import Set as AbstractSet`), and which constants to freeze are all unambiguous from the issue and the codebase. The only judgment call is whether to include the optional `MappingProxyType` sweep; both choices are acceptable and the issue explicitly labels that part as optional.

## Out of scope

- `seed_issue_labels` helper method on `FakeGitProvider` — it takes `labels: set[str]` as a test-seeding helper, not a Protocol method. Callers always pass a plain `set` literal. No widening needed.
- `get_issue_labels` return type on `GitProvider` and `FakeGitProvider` — returns a mutable `set[str]` so callers can compare with set operations; no change.
- `MERGE_HEALERS: list[MergeHealer]` — explicitly designed as an extension point to which operators `append` new healers (per module docstring). Must remain mutable.
- Any behavioral change to `LabelObservabilityObserver`, `PyGithubGitProvider`, or `RoutingGitProvider`.
- Threading `EventBus` through `cmd_retry`, adding `StateExitedEvent` emissions, or any other architectural change.
