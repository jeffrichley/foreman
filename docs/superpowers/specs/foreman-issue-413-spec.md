# Spec: replace dead `foreman:retry` label instruction with `foreman retry` CLI command in escalation text (issue #413)

## Goal

Fix every operator-visible escalation comment that tells operators to apply a `foreman:retry` label — a label that does not exist and is not watched by the v4 daemon — replacing it with the correct v4 operator path: `foreman retry <ticket_id>` (with guidance to find the id via `foreman ps`). Tracks [foreman#413](https://github.com/jeffrichley/foreman/issues/413).

## Acceptance criteria

- [ ] No runtime escalation text in `packages/foreman/src/` references a `` `foreman:retry` `` label.
- [ ] Every escalation `what_would_unblock` text that previously named the label now names the CLI command `foreman retry <ticket_id>` and includes the note "find the id via `foreman ps`".
- [ ] The shared footer in `build_escalation_comment_body` is updated from the dead-label form to the CLI command form.
- [ ] A test asserts the rendered escalation body contains the string `foreman retry` (CLI command) and does NOT contain the string `foreman:retry` (dead label).
- [ ] `docs/RUNBOOK.md` section "Re-dispatch via `foreman:retry`" is updated to describe the `foreman retry <ticket_id>` path and remove the label reference.
- [ ] `just check` passes with `new_failures_count == 0`.

## Approach

**Pattern naming (per CLAUDE.md Decision 4):** No GoF pattern fits — this is a straightforward text-content correction across five source sites and one test file. "No pattern fits, this is straightforward search-and-replace of stale operator guidance."

The `foreman:retry` label string appears in six places in runtime code:

1. `packages/foreman/src/foreman/roles/_escalation_comment.py:238` — fallback `what_would_unblock` (when no structured payload was provided).
2. `packages/foreman/src/foreman/roles/_escalation_comment.py:267` — shared footer line appended to every rendered escalation comment body.
3. `packages/foreman/src/foreman/v4/observers/sustained_blocked.py:179` — `what_would_unblock` text for the sustained-BLOCKED observer.
4. `packages/foreman/src/foreman/v4/observers/terminal_landing.py:267` — `what_would_unblock` text for the terminal-landing observer.
5. `packages/foreman/src/foreman/roles/worker.py:1099` — `what_would_unblock` in the first provider-error except block (ProviderError).
6. `packages/foreman/src/foreman/roles/worker.py:1144` — `what_would_unblock` in the second provider-error except block (generic Exception).

Sites 5 and 6 produce identical text; they must both be updated to keep their synthesized `WorkerOutput` payloads accurate.

The correct v4 operator path is confirmed by reading `packages/foreman/src/foreman/v4/cli/mutations.py:360-414`: `cmd_retry` accepts a positional `ticket_id` (internal numeric id from `foreman ps`, not the GitHub issue number), and since foreman#414, it already handles `NeedsHelp` tickets by auto-resolving the prior role-dispatch state and re-enqueueing — no manual `foreman set-state` step is needed. The replacement text is therefore: `foreman retry <ticket_id>` with a parenthetical "(find the id via `foreman ps`)".

The existing test `test_body_contains_no_github_closing_keywords` has a docstring that says "The body intentionally mentions the literal label `foreman:retry`". After the fix, the body will no longer mention that label at all. The docstring must be updated and a new assertion added (body must contain `foreman retry` and must not contain `foreman:retry`).

Historical spec docs (`docs/superpowers/specs/foreman-issue-367-spec.md`) that reference the dead label are planning artifacts, not operator-facing runtime text. They are left unchanged per the "out of scope" section.

## Sub-requests (topologically sorted)

1. **`_escalation_comment.py` — fallback `what_would_unblock`** (line 235–238): Replace `"apply the \`foreman:retry\` label once unblocked."` with `"run \`foreman retry <ticket_id>\` to re-dispatch (find the id via \`foreman ps\`)."`.

2. **`_escalation_comment.py` — shared footer** (line 267): Replace the `"Do not edit; apply the \`foreman:retry\` label on the issue to re-dispatch."` suffix with `"Do not edit; to re-dispatch, run \`foreman retry <ticket_id>\` (find the id via \`foreman ps\`)."`.

3. **`sustained_blocked.py` — `what_would_unblock`** (lines 177–180): Replace `"and apply the \`foreman:retry\` label once it has converged"` with `"and run \`foreman retry <ticket_id>\` once it has converged (find the id via \`foreman ps\`)"`.

4. **`terminal_landing.py` — `what_would_unblock`** (lines 266–269): Replace `"When ready, apply the \`foreman:retry\` label on the issue to re-dispatch the ticket from the prior state."` with `"When ready, run \`foreman retry <ticket_id>\` to re-dispatch (find the id via \`foreman ps\`)."`.

5. **`worker.py` — both `what_would_unblock` blocks** (lines 1097–1101 and 1142–1146): Replace `"apply the \`foreman:retry\` label once the underlying cause (API key, quota, network) is resolved."` with `"run \`foreman retry <ticket_id>\` once the underlying cause (API key, quota, network) is resolved (find the id via \`foreman ps\`)."` in both identical blocks.

6. **`tests/v4/roles/test_escalation_comment.py`**: Update `test_body_contains_no_github_closing_keywords` docstring to remove the statement "The body intentionally mentions the literal label `foreman:retry`" (it will no longer be true). Add a new test `test_body_footer_names_cli_command_not_dead_label` that builds a body and asserts `"foreman retry"` is present and `"foreman:retry"` is absent.

7. **`docs/RUNBOOK.md`**: Update the "Re-dispatch via `foreman:retry`" section (lines 546–551) to describe the `foreman retry <ticket_id>` path (listing `foreman ps` to find the id) and remove all references to the `foreman:retry` label.

## File-level changes

| File | Change |
|------|--------|
| `packages/foreman/src/foreman/roles/_escalation_comment.py` | Update fallback `what_would_unblock` string (line 238) and shared footer string (line 267) to use `foreman retry <ticket_id>` instead of `` `foreman:retry` `` label. |
| `packages/foreman/src/foreman/v4/observers/sustained_blocked.py` | Update `what_would_unblock` string (line 179) to use `foreman retry <ticket_id>`. |
| `packages/foreman/src/foreman/v4/observers/terminal_landing.py` | Update `what_would_unblock` string (line 267) to use `foreman retry <ticket_id>`. |
| `packages/foreman/src/foreman/roles/worker.py` | Update `what_would_unblock` strings in both provider-error except blocks (lines 1099 and 1144) to use `foreman retry <ticket_id>`. |
| `packages/foreman/tests/v4/roles/test_escalation_comment.py` | Update docstring in `test_body_contains_no_github_closing_keywords`; add `test_body_footer_names_cli_command_not_dead_label`. |
| `docs/RUNBOOK.md` | Rewrite "Re-dispatch via `foreman:retry`" section to reference `foreman retry <ticket_id>` and `foreman ps`. |

## Alternatives considered

- **Also update historical spec docs** (`docs/superpowers/specs/foreman-issue-367-spec.md`). Ruled out: these are planning artifacts, not operator-facing runtime text. Changing them provides no operator benefit and risks introducing review noise unrelated to the bug.
- **Add a `foreman:retry` label to registered repos** so the v3-era workflow becomes functional again. Ruled out: the v4 daemon doesn't watch for this label, so creating it would require re-wiring the v4 Poller. The correct long-term path is the CLI command; adding a dead label to repos would be misleading and mask the missing Poller wiring.

## Open questions

None. The correct operator command (`foreman retry <ticket_id>`, with auto-resume of NeedsHelp since foreman#414) is confirmed by reading `packages/foreman/src/foreman/v4/cli/mutations.py:360-414` directly. All source sites of the dead label are enumerated by a codebase-wide grep and verified by file reads.

## Out of scope

- The cmd_retry-on-needs-help no-op behavior (addressed in foreman#414, already fixed in the current codebase based on `_RETRYABLE_TERMINALS = frozenset({"NeedsHelp", "Failed"})` in `mutations.py`).
- Historical spec docs (`docs/superpowers/specs/foreman-issue-367-spec.md`) that reference the dead label — planning artifacts, not operator-facing text.
- Adding a `foreman:retry` label to any registered repo's label set.
- Any change to the v4 Poller's label-watching logic.
