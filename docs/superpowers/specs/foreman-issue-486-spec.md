# Spec: scrub sibling terminal state labels on Done completion (issue #486)

## Goal

When a ticket reaches the `Done` terminal, `LabelObservabilityObserver` should strip the sibling terminal state labels (`foreman:state-needshelp`, `foreman:state-failed`) that accumulate without cleanup during detours through those intermediate terminal states. `NeedsHelp` and `Failed` terminal landings must continue to preserve their labels unchanged. See issue [#486](https://github.com/jeffrichley/foreman/issues/486).

## Acceptance criteria

- A ticket that transited through `NeedsHelp` (and/or `Failed`) and then reached `Done` carries only `foreman:state-done` — `foreman:state-needshelp` and `foreman:state-failed` are removed on `Done` entry.
- A ticket that terminally parks in `NeedsHelp` retains `foreman:state-needshelp` (no scrub on NeedsHelp entry).
- A ticket that terminally parks in `Failed` retains `foreman:state-failed` (no scrub on Failed entry).
- On the Done-entry path, `remove_labels` is called with exactly `{"foreman:state-needshelp", "foreman:state-failed"}`; the writer's existing 404-swallowing handles labels not present without extra reads.
- Three regression tests added to `packages/foreman/tests/v4/observers/test_label_observability.py`:
  - Done-after-needshelp scrubs both sibling terminal labels.
  - NeedsHelp terminal preserves its own label (no sibling scrub).
  - Failed terminal preserves its own label (no sibling scrub).
- `just check` exits zero.

## Approach

**Pattern (Decision 4):** No GoF pattern fits — this is a straightforward conditional-branch extension to an existing single-responsibility handler. The Google principle is **SRP** ("make the right thing easy"): `LabelObservabilityObserver` already owns the entire label surface; adding the Done-entry scrub here is the only place it must live.

**Why the residue accumulates.** `_enter_terminal()` in `packages/foreman/src/foreman/v4/state.py` (lines 124–145) emits `StateEnteredEvent` for the terminal state — which causes the observer to add `foreman:state-needshelp` or `foreman:state-failed` — but deliberately skips `StateExitedEvent`:

> "We deliberately skip StateExitedEvent so the observer keeps the terminal label visible on the issue."

When an operator later runs `foreman retry`, `cmd_retry` in `packages/foreman/src/foreman/v4/cli/mutations.py` (lines 374–439) calls `repo.set_ticket_state()` directly without event publication. No `StateExitedEvent(NeedsHelp)` fires, so the observer never removes `foreman:state-needshelp`. The ticket resumes, eventually reaches `Done`, and the sibling label lingers alongside `foreman:state-done`.

**Why only `needshelp` and `failed` labels need scrubbing.** Non-terminal state labels (Planning, SpecReview, Implementing, etc.) are always removed by `StateExitedEvent` when those states are exited — this is the existing `_on_state_exited` path in the observer (label_observability.py line 132–147). Only labels added by `_enter_terminal()` can linger, because `_enter_terminal` is the only path that emits `StateEnteredEvent` without a paired `StateExitedEvent`. `_enter_terminal` is called for `NeedsHelp` (via `escalate_to_needs_help`) and `Failed` (via the normal terminal-advancement branch in `transition()`). `Done` also goes through `_enter_terminal`, but that is the target state; we strip the *other* two.

**The fix.** In `LabelObservabilityObserver._on_state_entered()`, add a branch immediately after the existing `_FIRST_STATES` guard: when `event.state_name == _COMPLETION_TERMINAL` (= `"Done"`), call `remove_labels` with `_SIBLING_TERMINAL_LABELS` (= `frozenset({"foreman:state-needshelp", "foreman:state-failed"})`).

Two new module-level constants, placed after `_FIRST_STATES`:

```python
#: The completion terminal: the only terminal whose entry triggers scrubbing
#: of sibling terminal state labels.
_COMPLETION_TERMINAL = "Done"

#: Labels for the two non-completion terminals. These are stripped when the
#: ticket enters Done so completed issues don't carry misleading residue.
#: Computed via ``_state_label()`` to stay consistent with the naming helper.
_SIBLING_TERMINAL_LABELS: frozenset[str] = frozenset({
    _state_label("NeedsHelp"),
    _state_label("Failed"),
})
```

The extension to `_on_state_entered`, inserted after the `_FIRST_STATES` block:

```python
if event.state_name == _COMPLETION_TERMINAL:
    self._writer.remove_labels(
        project=ticket.project,
        issue_number=ticket.issue_number,
        labels=_SIBLING_TERMINAL_LABELS,
    )
```

**Remains write-only.** No `get_issue_labels` read is added. The `remove_labels` call passes a fixed, known set; `PyGithubGitProvider.remove_labels` 404-swallows labels not on the issue (see `pygithub_git_provider.py` lines 292–316), preserving the observer's write-only invariant.

**NeedsHelp and Failed paths are untouched.** The new branch fires only when `event.state_name == _COMPLETION_TERMINAL`. Entries to `NeedsHelp` and `Failed` follow the existing path — they add their label only — which correctly preserves those labels for human and Poller visibility.

## Sub-requests (topologically sorted)

1. **Add two module-level constants to `packages/foreman/src/foreman/v4/observers/label_observability.py`** — insert after `_FIRST_STATES` (line 86):

   ```python
   _COMPLETION_TERMINAL = "Done"
   _SIBLING_TERMINAL_LABELS: frozenset[str] = frozenset({
       _state_label("NeedsHelp"),
       _state_label("Failed"),
   })
   ```

2. **Extend `LabelObservabilityObserver._on_state_entered()`** — after the `if event.state_name in _FIRST_STATES:` block (lines 125–130), insert:

   ```python
   if event.state_name == _COMPLETION_TERMINAL:
       self._writer.remove_labels(
           project=ticket.project,
           issue_number=ticket.issue_number,
           labels=_SIBLING_TERMINAL_LABELS,
       )
   ```

3. **Add three tests to `packages/foreman/tests/v4/observers/test_label_observability.py`**:

   ```python
   def test_done_entry_scrubs_sibling_terminal_labels() -> None:
       """StateEntered(Done) removes foreman:state-needshelp + foreman:state-failed.

       Regression for the class of residue found 2026-07-10: tickets that
       transited through NeedsHelp (and/or Failed) accumulated sibling terminal
       labels that were never stripped on the Done completion landing."""
       repo, ticket = _make_repo_and_ticket("Done")
       writer = _RecordingWriter()
       obs = LabelObservabilityObserver(writer=writer, repo=repo)
       obs(
           StateEnteredEvent(
               ticket_id=ticket.id,
               instance_id=99,
               state_name="Done",
               sequence=5,
               at=_T0,
           )
       )
       # Exactly one add_labels call for foreman:state-done.
       assert writer.add_calls == [("foreman", 42, {"foreman:state-done"})]
       # remove_labels called with the two sibling terminal labels.
       remove_label_sets = [labels for _proj, _issue, labels in writer.remove_calls]
       assert {"foreman:state-needshelp", "foreman:state-failed"} in remove_label_sets


   def test_needshelp_terminal_does_not_scrub_siblings() -> None:
       """StateEntered(NeedsHelp) must NOT remove sibling terminal labels.

       NeedsHelp is a holding pen: the label must stay visible for humans and
       the state-sweep so operators can find and retry the ticket."""
       repo, ticket = _make_repo_and_ticket("NeedsHelp")
       writer = _RecordingWriter()
       obs = LabelObservabilityObserver(writer=writer, repo=repo)
       obs(
           StateEnteredEvent(
               ticket_id=ticket.id,
               instance_id=99,
               state_name="NeedsHelp",
               sequence=4,
               at=_T0,
           )
       )
       assert writer.add_calls == [("foreman", 42, {"foreman:state-needshelp"})]
       # No remove call for sibling terminal labels.
       sibling_scrub_calls = [
           labels for _proj, _issue, labels in writer.remove_calls
           if labels & {"foreman:state-needshelp", "foreman:state-failed"}
       ]
       assert sibling_scrub_calls == [], (
           f"NeedsHelp entry must not scrub sibling labels; got: {sibling_scrub_calls!r}"
       )


   def test_failed_terminal_does_not_scrub_siblings() -> None:
       """StateEntered(Failed) must NOT remove sibling terminal labels.

       Failed is a terminal with human-actionable context; the label must stay
       visible for operators reviewing the issue."""
       repo, ticket = _make_repo_and_ticket("Failed")
       writer = _RecordingWriter()
       obs = LabelObservabilityObserver(writer=writer, repo=repo)
       obs(
           StateEnteredEvent(
               ticket_id=ticket.id,
               instance_id=99,
               state_name="Failed",
               sequence=4,
               at=_T0,
           )
       )
       assert writer.add_calls == [("foreman", 42, {"foreman:state-failed"})]
       sibling_scrub_calls = [
           labels for _proj, _issue, labels in writer.remove_calls
           if labels & {"foreman:state-needshelp", "foreman:state-failed"}
       ]
       assert sibling_scrub_calls == [], (
           f"Failed entry must not scrub sibling labels; got: {sibling_scrub_calls!r}"
       )
   ```

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/v4/observers/label_observability.py` | Add `_COMPLETION_TERMINAL` and `_SIBLING_TERMINAL_LABELS` constants after `_FIRST_STATES`; extend `_on_state_entered()` with Done-entry scrub branch |
| `packages/foreman/tests/v4/observers/test_label_observability.py` | Add three regression tests: Done-scrubs, NeedsHelp-preserves, Failed-preserves |

## Alternatives considered

1. **Strip ALL non-Done `foreman:state-*` labels on Done entry.** Enumerate all registered state names from `STATE_REGISTRY` (or a hardcoded list) and pass the full set minus `foreman:state-done` to `remove_labels`. Rejected: YAGNI — non-terminal state labels are always removed by the existing `StateExitedEvent` path; the extra DELETE calls (one per label, 404-swallowed by `PyGithubGitProvider.remove_labels`) are wasted on the happy path; and importing `STATE_REGISTRY` would widen the module's dependency surface without benefit.

2. **Emit `StateExitedEvent(NeedsHelp)` from `cmd_retry` when resuming.** The `cmd_retry` handler in `mutations.py` calls `repo.set_ticket_state()` without publishing events. Adding an `EventBus` publish there would trigger the existing `_on_state_exited` path to remove the NeedsHelp label at resume time. Rejected: `cmd_retry` today has no `EventBus` access (it operates on `repo` + `qm`); threading the bus through the CLI context would require broader architectural changes. The targeted Done-entry scrub in the observer is more surgical.

3. **Emit `StateExitedEvent` inside `_enter_terminal()` and let the observer remove it later.** Modify `_enter_terminal` to emit `StateExitedEvent` immediately after `StateEnteredEvent` for terminal states. Rejected: contradicts the explicit design comment in `_enter_terminal` ("We deliberately skip StateExitedEvent so the observer keeps the terminal label visible on the issue"). The label visibility during the `NeedsHelp` parking period is a feature, not a bug.

## Open questions

None. The affected file, the exact constants, and the exact set of labels to strip are all unambiguous from the issue and the codebase.

## Out of scope

- Retroactively scrubbing labels on tickets that were manually cleaned (done 2026-07-10).
- Stripping non-terminal transient state labels (Planning, SpecReview, etc.) — those are handled correctly by existing `StateExitedEvent` emissions.
- Changes to NeedsHelp or Failed terminal entry behavior.
- Threading `EventBus` through `cmd_retry` to emit `StateExitedEvent(NeedsHelp)` on resume.
