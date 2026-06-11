# Spec: catch Exception("success") at provider layer and recover (issue #264)

## Goal

Wrap the SDK iteration loop in
`AnthropicSDKProvider._iterate_query` with a narrow exception filter
that recognizes a bare `Exception("success")` raise — the upstream
`claude_agent_sdk` mis-signal documented in foreman#230 — as a
*successful* run, returns the `ResultMessage` already received during
the iteration, and lets the role runner see a normal success instead
of a discarded result. See issue #264. Today (after PR #255 commit 2 /
foreman#229) the upstream SDK bug forces the defensive role-runner
helper to transition successful work to `foreman:needs-help` and
write `outcome="exception"`; the agent's actual `ResultMessage` is
thrown away. This fix recovers the agent's captured output at the
provider boundary so the damage container only fires for *genuine*
failures.

## Acceptance criteria

- **TDD red baseline first.** Add a new test
  `test_run_agent_recovers_from_sdk_exception_success_with_captured_result`
  to `packages/foreman/tests/test_provider_anthropic_sdk.py` that
  drives `AnthropicSDKProvider.run_agent` against a fake `query()`
  whose async generator yields a `ResultMessage(subtype="success",
  structured_output={...})` and then raises
  `Exception("success")` during the SAME iteration / on the iterator's
  cleanup path (see "Open questions" for fake-generator shape
  options). The test asserts the provider returns a validated
  `_DemoOutput` instance whose fields match the captured
  `ResultMessage`'s `structured_output`, and that the returned
  `UsageInfo` reflects that same `ResultMessage`'s usage fields.
  The test MUST FAIL on the pre-edit branch (provider re-raises the
  Exception, no `_DemoOutput` returned) and PASS after the provider
  change lands. Quote BOTH the pre-edit FAIL pytest line and the
  post-edit PASS pytest line in the impl PR body as the TDD
  red/green evidence.

- **Specificity test 1 (narrow string match).** Add
  `test_run_agent_re_raises_non_success_exception_after_result_message`:
  the fake yields no captured `ResultMessage` (or one whose subtype
  is not "success") and raises `RuntimeError("some other transport
  failure")` from the iterator. The provider MUST re-raise the
  `RuntimeError` — the new filter must NOT widen to "any exception
  after iteration". This pins the exact-string-match discipline.

- **Specificity test 2 (no captured ResultMessage).** Add
  `test_run_agent_re_raises_exception_success_when_no_result_message_captured`:
  the fake yields zero `ResultMessage`s (only non-ResultMessage
  message instances filtered out by the existing
  `isinstance(message, ResultMessage)` guard, or simply an empty
  stream) and then raises `Exception("success")`. The provider MUST
  re-raise the `Exception` — recovery requires a captured
  success-shaped `ResultMessage`. The dispatcher's existing
  defensive helper (foreman#229) handles the propagated Exception
  from there.

- **Specificity test 3 (captured ResultMessage with wrong subtype).**
  Add
  `test_run_agent_re_raises_exception_success_when_captured_result_is_not_success_subtype`:
  the fake yields a `ResultMessage(subtype="error_max_structured_output_retries",
  errors=[...])` then raises `Exception("success")`. In production
  the existing retry-error branch
  (`anthropic_sdk.py:214-219`) would raise
  `StructuredOutputRetryError` before the Exception is ever observed,
  but this test pins the belt-and-suspenders second guard:
  even IF an exception path bypassed that branch, the captured
  ResultMessage's `subtype != "success"` MUST block recovery and
  re-raise instead. Document this expected behavior in the test
  docstring.

- **Existing xfail tripwire stays in place.** The test
  `test_sdk_receive_messages_does_not_raise_on_success_subtype`
  at `packages/foreman/tests/test_provider_anthropic_sdk.py:825-882`
  is UNCHANGED in terms of marker (`@pytest.mark.xfail(strict=False,
  ...)`), body, or fake `_SuccessAsErrorTransport` definition. The
  test pins the UPSTREAM SDK contract; foreman's new
  provider-level recovery is a separate concern. Add ONE paragraph
  to the leading docstring/comment block at
  `packages/foreman/tests/test_provider_anthropic_sdk.py:727-763`
  noting that foreman now has its own provider-level recovery
  (issue #264), so this xfail test functions as a tripwire for the
  upstream SDK bug rather than as a gate on foreman's resilience.
  No marker change, no test-body change, no `_SuccessAsErrorTransport`
  change.

- **Existing auth-retry wrapper untouched.** The wrapper at
  `packages/foreman/src/foreman/providers/anthropic_sdk.py:155-183`
  and the `_SDK_AUTH_ERROR_PREFIX = "Claude Code returned an error
  result"` pattern at lines 45-55 remain UNCHANGED. The auth-pattern
  filter detects a *prefix-match* Exception
  (`"Claude Code returned an error result: success"`); the new
  filter detects bare `Exception("success")`. Distinct shapes;
  both stay.

- **Defensive role-runner helper untouched.** The
  `handle_unhandled_role_exception` helper at
  `packages/foreman/src/foreman/roles/__init__.py:107-163` and the
  per-ticket consecutive-failure rate-limit at
  `packages/foreman/src/foreman/config.py:109-145` are NOT
  modified. Belt-and-suspenders for everything OTHER than the new
  recovery path.

- **No regression in existing tests.** All 20+ existing tests in
  `packages/foreman/tests/test_provider_anthropic_sdk.py` continue
  to pass (or XFAIL for the upstream tripwire, as before). In
  particular the existing happy-path tests at lines 128-159 and the
  auth-retry tests at lines 389-505 must remain green without
  modification.

- **Quality gate.** `just check` exits zero on the post-fix worktree
  (lint + typecheck + tests). `new_failures_count == 0`.

- **Impl PR body discipline.** The impl PR body
  - Quotes the pre-edit FAIL and post-edit PASS pytest lines for
    the new
    `test_run_agent_recovers_from_sdk_exception_success_with_captured_result`
    test as TDD evidence.
  - Cites foreman#262 as the verification ticket proving an SDK
    bump cannot fix this bug (Worker log preserved in container at
    `/foreman/logs/worker/262__2026-06-11T02-04-07-635603Z.log`).
  - References #264 plainly ("addresses #264") and #230 plainly
    ("retires the root cause documented in #230").
  - MUST NOT contain `Closes #264`, `Fixes #230`, or any other
    GitHub closing-keyword + reference combination — per
    foreman#63, the Foreman daemon's `merge_impl_pr` action owns
    issue closure; PR-body closing keywords short-circuit the
    pipeline's close-out gate. Reference plainly only.

## Approach

The bug is in upstream
`claude_agent_sdk._internal.query.receive_messages` (lines 845-852
per the issue body, byte-identical between SDK 0.2.87 and 0.2.96 per
foreman#262's Worker verification). When the CLI emits a raw
`{"type": "error", "error": "success"}` envelope (the protocol-level
subtype "success" leaked into the `error` field),
`receive_messages` raises a bare `Exception("success")` — even
though the underlying run was logically a success. Upstream PR #918
fixes the `error_max_turns` / `error_during_execution` path in
`_read_messages`, NOT the `{"type": "error", "error": "success"}`
shape foreman#230 cares about. No current tagged release satisfies
the original tripwire criterion. **The fix has to land at our layer.**

By the issue author's empirical observation (PR #255 / foreman#229
production traces, 2026-06-08), by the time the Exception fires the
consumer (our `async for` loop at
`packages/foreman/src/foreman/providers/anthropic_sdk.py:202`) has
already received the agent's real `ResultMessage` with
`structured_output`. The work succeeded; only the terminal
end-of-stream is broken. Today the Exception propagates →
`run_agent` re-raises → the role runner's defensive helper
(`handle_unhandled_role_exception`, foreman#229) catches → ticket
transitions to `foreman:needs-help` → operator burden + discarded
successful output.

The fix wraps the iteration loop in `_iterate_query` (the method
split out in PR #255 commit 2 specifically so the auth-retry
wrapper could call it twice) with a narrow `try/except Exception`
filter keyed on two guards:

1. **Exception narrowness.** Match `str(exc) == "success"`
   (exact-string equality, NOT `startswith` and NOT `in`). The
   SDK's bare raise is literally `raise Exception("success")` →
   `str(exc) == "success"`. Anything broader would silently
   swallow unrelated failures.

2. **Captured-result soundness.** Track the latest `ResultMessage`
   seen during iteration in a local `last_result: ResultMessage |
   None = None`. The captured message must have
   `last_result.subtype == "success"` AND
   `last_result.structured_output is not None` — i.e., the same
   shape the existing happy-path branch at line 210-213 already
   acts on. If we have one, the agent's work is intact and the
   Exception is a bogus end-of-stream signal; we synthesize the
   normal `(output_model.model_validate(...), _build_usage_info(...))`
   return using the captured message. If either guard fails, the
   Exception propagates unchanged.

**Why wrap inside `_iterate_query`, not `run_agent`.** The
success-recovery is about salvaging a captured-mid-iteration
`ResultMessage`; the auth-retry concerns in `run_agent` (lines
155-183) are about restarting the whole call after a credential
refresh. Wrapping at `run_agent` would either need to thread
`last_result` out of `_iterate_query` (awkward API change) or do
its own iteration (duplicating the loop). Wrapping inside
`_iterate_query` keeps the captured-result bookkeeping local. The
auth-retry wrapper at `run_agent` is unaffected: a bare
`Exception("success")` with no captured success ResultMessage will
propagate to the auth-retry wrapper, fail its prefix-match
(`"Claude Code returned an error result"` ≠ `"success"`),
re-propagate to the role runner's defensive helper, and end at
`foreman:needs-help` — the existing damage-container behavior for
that shape.

**Why the trailing `StructuredOutputMissingError` raise stays
outside the try block.** That raise represents an empty-stream
condition (the for loop completed without a usable result), not an
exception caught from the iterator. Keeping it outside the
`try` makes the control flow unambiguous.

**House-style.** The new filter mirrors the auth-retry wrapper's
defensive pattern (exact substring match, narrow `Exception`
catch — not `BaseException`, preserved typed exception subtypes).
A 6-8 line comment block above the new `except` references
foreman#230 (upstream investigation), foreman#262 (no-fix
verification), and foreman#264 (this fix), in the same shape the
auth-retry wrapper's comments use (lines 45-55, 145-155).

## Sub-requests (topologically sorted)

1. **TDD red baseline.** Add the new test
   `test_run_agent_recovers_from_sdk_exception_success_with_captured_result`
   to `packages/foreman/tests/test_provider_anthropic_sdk.py` after
   the foreman#244 prompt-cache test block (after line ~725, before
   the foreman#230 xfail block at line 727). Add a new patch helper
   `_patch_query_yields_then_raises(monkeypatch, *, messages,
   exception_to_raise)` near `_patch_query_sequence` (line 356).
   The helper returns an async generator that yields each message
   in `messages` and then raises `exception_to_raise` from the
   iterator's cleanup path (see "Open questions" for the
   `try/finally` shape needed to make the raise observable to the
   consumer). Run on the pre-edit branch:
   ```bash
   uv run --no-sync pytest \
     packages/foreman/tests/test_provider_anthropic_sdk.py::test_run_agent_recovers_from_sdk_exception_success_with_captured_result \
     -v
   ```
   Expected: FAILED with `Exception: success` propagating out of
   `run_agent`. Record the pytest output line for the PR body.

2. **Edit
   `packages/foreman/src/foreman/providers/anthropic_sdk.py`** —
   modify `_iterate_query` (lines 185-224). Wrap the existing
   `async for message in query(...)` block in a `try/except
   Exception` and track the latest captured `ResultMessage`. The
   minimal target shape (Worker fills in the comment text inline):

   ```python
   async def _iterate_query(
       self,
       *,
       user_prompt: str,
       options: ClaudeAgentOptions,
       output_model: type[T],
   ) -> tuple[T, UsageInfo]:
       last_result: ResultMessage | None = None
       try:
           async for message in query(prompt=user_prompt, options=options):
               if not isinstance(message, ResultMessage):
                   continue
               last_result = message
               if message.subtype == "success" and message.structured_output is not None:
                   validated = output_model.model_validate(message.structured_output)
                   usage = _build_usage_info(message)
                   return validated, usage
               if message.subtype == "error_max_structured_output_retries":
                   raise StructuredOutputRetryError(
                       "Anthropic Agent SDK exhausted its retry budget trying to "
                       f"satisfy the schema for {output_model.__name__}. "
                       f"Errors reported: {message.errors!r}"
                   )
       except Exception as exc:
           # foreman#264: upstream claude_agent_sdk receive_messages raises
           # bare Exception("success") when a logically-successful result is
           # wrapped in a {"type": "error", "error": "success"} envelope.
           # By the time this fires we've already captured the real
           # ResultMessage with structured_output — salvage it. See foreman
           # #230 for the upstream investigation, foreman#262 for the
           # confirmed-no-fix verification, and the xfail tripwire in this
           # module's tests for the upstream contract. Distinct from the
           # auth-pattern match at run_agent lines 155-183: that wrapper
           # detects a prefix-match Exception; this one detects the bare
           # "success" literal. Both stay as belt-and-suspenders.
           if (
               str(exc) == "success"
               and last_result is not None
               and last_result.subtype == "success"
               and last_result.structured_output is not None
           ):
               validated = output_model.model_validate(last_result.structured_output)
               usage = _build_usage_info(last_result)
               return validated, usage
           raise

       raise StructuredOutputMissingError(
           "Anthropic Agent SDK did not return a successful ResultMessage "
           f"carrying structured_output for {output_model.__name__}"
       )
   ```

   Worker discipline: the only structural changes are the
   `last_result` local, the outer `try:` / `except Exception as
   exc:` block, the inner `last_result = message` line after the
   `isinstance` filter, and the recovery branch in the except.
   The existing happy-path return, the retry-error branch, and
   the trailing `StructuredOutputMissingError` raise are unchanged.

3. **Re-run the red test:**
   ```bash
   uv run --no-sync pytest \
     packages/foreman/tests/test_provider_anthropic_sdk.py::test_run_agent_recovers_from_sdk_exception_success_with_captured_result \
     -v
   ```
   Expected: PASS. Record the pytest output line for the PR body
   as the post-fix GREEN evidence.

4. **Add specificity test 1** — the narrow string match:
   ```python
   @pytest.mark.asyncio
   async def test_run_agent_re_raises_non_success_exception_after_result_message(
       monkeypatch: pytest.MonkeyPatch, tmp_path: Path
   ) -> None:
       """Pin the narrow string-match filter: an exception whose message
       is NOT exactly "success" must re-raise, even after a captured
       success-shaped ResultMessage. Guards the filter against widening
       to "any exception after iteration"."""
       _patch_query_yields_then_raises(
           monkeypatch,
           messages=[
               # Note: this Result won't trigger early return because
               # the test fake's cleanup raise fires before the loop
               # body sees structured_output is not None. See the
               # red-test fake shape in sub-request 1 for details.
               _make_result(subtype="success", structured_output={"name": "ok", "count": 1}),
           ],
           exception_to_raise=RuntimeError("not a success signal"),
       )
       provider = AnthropicSDKProvider()
       with pytest.raises(RuntimeError, match="not a success signal"):
           await provider.run_agent(
               system_prompt="sys", user_prompt="usr",
               allowed_tools=["Read"], output_model=_DemoOutput,
               cwd=tmp_path,
           )
   ```

5. **Add specificity test 2** — no captured ResultMessage:
   ```python
   @pytest.mark.asyncio
   async def test_run_agent_re_raises_exception_success_when_no_result_message_captured(
       monkeypatch: pytest.MonkeyPatch, tmp_path: Path
   ) -> None:
       """Belt-and-suspenders second guard: without a captured
       success-shaped ResultMessage, an Exception('success') is
       suspicious (no work was actually received). Re-raise; the
       dispatcher's defensive helper (foreman#229) handles it from
       there."""
       _patch_query_yields_then_raises(
           monkeypatch,
           messages=[],
           exception_to_raise=Exception("success"),
       )
       provider = AnthropicSDKProvider()
       with pytest.raises(Exception, match="^success$"):
           await provider.run_agent(
               system_prompt="sys", user_prompt="usr",
               allowed_tools=["Read"], output_model=_DemoOutput,
               cwd=tmp_path,
           )
   ```

6. **Add specificity test 3** — captured ResultMessage with wrong
   subtype:
   ```python
   @pytest.mark.asyncio
   async def test_run_agent_re_raises_exception_success_when_captured_result_is_not_success_subtype(
       monkeypatch: pytest.MonkeyPatch, tmp_path: Path
   ) -> None:
       """Belt-and-suspenders third guard: the captured ResultMessage's
       subtype must equal "success" for recovery to fire. A
       success-string Exception arriving alongside a non-success
       ResultMessage is a contradictory SDK shape — don't recover.

       In production the retry-error branch at anthropic_sdk.py:214-219
       would raise StructuredOutputRetryError before this Exception
       is observed, but this test pins the recovery's subtype guard
       in case a future SDK shape bypasses that branch."""
       # Use a non-retry-error non-success subtype so the existing
       # error branch doesn't fire either; bare "info" or similar.
       _patch_query_yields_then_raises(
           monkeypatch,
           messages=[_make_result(subtype="info", structured_output={"name": "ok", "count": 1})],
           exception_to_raise=Exception("success"),
       )
       provider = AnthropicSDKProvider()
       with pytest.raises(Exception, match="^success$"):
           await provider.run_agent(
               system_prompt="sys", user_prompt="usr",
               allowed_tools=["Read"], output_model=_DemoOutput,
               cwd=tmp_path,
           )
   ```

7. **Update the xfail-block leading docstring** at
   `packages/foreman/tests/test_provider_anthropic_sdk.py:727-763`.
   Add one paragraph after the existing prose noting:
   ```
   foreman#264 (2026-06-11): foreman's AnthropicSDKProvider now has
   its OWN provider-level recovery for this upstream bug — see
   _iterate_query's narrow Exception("success") + captured-success-
   ResultMessage filter, and the recover-from-* tests above. This
   xfail test continues to pin the upstream SDK CONTRACT (a separate
   concern); it converts XFAIL → XPASS only when the SDK itself
   ships a fix. The provider-level recovery means foreman is
   resilient to the bug TODAY regardless of the SDK's fix timeline.
   ```
   No marker change. No test-body change. No
   `_SuccessAsErrorTransport` change.

8. **Run the targeted test module:**
   ```bash
   uv run --no-sync pytest packages/foreman/tests/test_provider_anthropic_sdk.py -v
   ```
   Expected: every existing test still passes; the four new tests
   PASS; the upstream-contract xfail tripwire still reports XFAIL
   (no flip, since we haven't bumped the SDK). If anything else
   regresses, STOP and investigate before staging.

9. **Run the full quality gate:** `just check`. Expected: exit 0.
   `new_failures_count == 0`. The typecheck step
   (`mypy packages/foreman/src`) must continue to type-check
   `_iterate_query` cleanly; the new `last_result: ResultMessage |
   None = None` local + the typed match in the except branch are
   the only signature-relevant additions.

10. **Stage and commit:**
    ```bash
    git add packages/foreman/src/foreman/providers/anthropic_sdk.py \
            packages/foreman/tests/test_provider_anthropic_sdk.py
    git commit -m "fix(provider): recover from upstream Exception('success') when a valid ResultMessage was captured"
    ```
    Exactly two files staged. If `git status` shows any other
    modified files in the staged set, unstage them — this PR
    touches the provider and its tests only.

11. **Draft the impl PR body.** Required elements (no GitHub
    closing-keyword + #N combinations — see foreman#63):
    - Cite foreman#230 (upstream investigation) and foreman#262
      (no-fix verification) plainly: "addresses #264", "retires
      the root cause documented in #230".
    - Quote the pre-edit FAIL pytest line and post-edit PASS
      pytest line for
      `test_run_agent_recovers_from_sdk_exception_success_with_captured_result`
      as the TDD red-then-green evidence.
    - One-line note that the existing xfail tripwire stays in
      place as the upstream-contract signal; foreman's
      resilience is now provider-level (no SDK bump required).
    - One-line acknowledgment that live verification of the bug
      firing in production is post-merge operator concern (the
      SDK bug fires intermittently; may take days/weeks before it
      naturally exercises the new recovery path).

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/src/foreman/providers/anthropic_sdk.py` | Modify `_iterate_query` (lines 185-224). Add a local `last_result: ResultMessage | None = None`. Wrap the existing `async for message in query(...)` block in a `try/except Exception` filter. Track `last_result = message` after the `isinstance(message, ResultMessage)` guard. In the new `except` branch, if `str(exc) == "success"` AND `last_result is not None` AND `last_result.subtype == "success"` AND `last_result.structured_output is not None`, synthesize the success return using `last_result` (re-using the existing `model_validate` + `_build_usage_info` calls). Otherwise re-raise unchanged. Leave the trailing `StructuredOutputMissingError` raise outside the try block. Add a 6-8 line comment above the recovery branch referencing foreman#230 / #262 / #264 and distinguishing this filter from the auth-pattern wrapper at `run_agent` lines 155-183. No other changes to the file. |
| `packages/foreman/tests/test_provider_anthropic_sdk.py` | (1) Add `_patch_query_yields_then_raises(monkeypatch, *, messages, exception_to_raise)` helper near `_patch_query_sequence` (line 356). The fake's async generator must yield each message in order and then raise `exception_to_raise` in a way that propagates to the consumer's `async for` site (likely via `try: yield ...; finally: raise` or by yielding all messages and then raising on the next `__anext__` call — Worker determines the exact shape during impl; see "Open questions"). (2) Add four new tests after the existing foreman#244 prompt-cache block (~line 725) and before the existing foreman#230 xfail block (line 727): `test_run_agent_recovers_from_sdk_exception_success_with_captured_result` (the recovery happy-path), `test_run_agent_re_raises_non_success_exception_after_result_message` (narrow string match), `test_run_agent_re_raises_exception_success_when_no_result_message_captured` (captured-result-required guard), `test_run_agent_re_raises_exception_success_when_captured_result_is_not_success_subtype` (subtype guard). (3) Append one paragraph to the leading docstring/comment block at lines 727-763 noting foreman's provider-level recovery (issue #264) — the xfail test is now an upstream-contract tripwire, not a foreman-resilience gate. NO marker change, NO test-body change for the xfail test, NO change to `_SuccessAsErrorTransport`. |

No expected changes to:

- `packages/foreman/src/foreman/provider.py` — `UsageInfo`,
  `ProviderFacade`, `ProviderAuthError`,
  `StructuredOutputMissingError`, `StructuredOutputRetryError` all
  untouched. No new exception type needed; recovery is a successful
  return, not a new error class.
- `packages/foreman/src/foreman/roles/__init__.py` — the
  defensive `handle_unhandled_role_exception` helper (lines
  107-163) is untouched. It still fires for every exception OTHER
  than the new recovery path's narrow match. Belt-and-suspenders.
- `packages/foreman/src/foreman/config.py` — the
  `rate_limit_max_consecutive_failures` /
  `rate_limit_window_seconds` fields (lines 109-145) are untouched.
  Belt-and-suspenders against cascades.
- `packages/foreman/pyproject.toml` / `uv.lock` — no dependency
  changes. foreman#262 empirically confirmed no current
  `claude-agent-sdk` tagged release fixes the bug; the fix is at
  our layer.
- `packages/foreman/tests/test_provider_anthropic_sdk.py:825-833`
  (the xfail marker block on the upstream-contract test) — the
  `@pytest.mark.xfail(strict=False, ...)` marker stays as the
  long-tail signal.
- All other foreman source / test files.

## Alternatives considered

- **Wrap inside `run_agent` instead of `_iterate_query`.** Rejected
  — the success-recovery is about salvaging a captured-mid-iteration
  `ResultMessage`; the auth-retry concerns in `run_agent` are about
  restarting the whole call after a credential refresh. Wrapping at
  `run_agent` would either need to thread `last_result` out of
  `_iterate_query` (awkward API surface change) or duplicate the
  iteration loop. Wrapping inside `_iterate_query` keeps the
  captured-result bookkeeping local and the two concerns layered
  cleanly.

- **Detect via `exc.args == ("success",)` instead of `str(exc) ==
  "success"`.** Rejected — minor stylistic preference; both produce
  the same match for the SDK's actual raise shape `raise
  Exception("success")` (single-arg Exception → `args = ("success",)`
  and `str(exc) = "success"`). `str(exc) == "success"` is the same
  shape the auth-retry wrapper uses (`str(e).startswith(...)`), so
  it keeps the pattern uniform.

- **Match `"success" in str(exc)` (substring).** Rejected — too
  loose. An unrelated future SDK error like `"upstream
  success-rate API timed out"` would silently recover. Exact-string-
  equality is the discipline.

- **Catch `BaseException` instead of `Exception`.** Rejected —
  keyboard-interrupt / system-exit / cancelled-task signals must
  propagate. The SDK never raises a BaseException-not-Exception for
  this shape.

- **Monkey-patch
  `claude_agent_sdk._internal.query.receive_messages` directly** to
  fix the upstream classification. Rejected per the issue body:
  would couple foreman to SDK internals beyond the public `query()`
  / `ResultMessage` surface; brittle to upstream refactors; expanding
  maintenance burden every time the SDK changes internally. Our
  code is the boundary.

- **Delete the existing xfail tripwire test entirely** since foreman
  has fixed its own resilience. Rejected — the xfail test pins the
  upstream SDK CONTRACT (a separate concern from foreman's
  resilience). When the SDK eventually ships a real fix, the
  xfail flips XPASS as a visible signal that we can re-evaluate the
  defensive layers (this provider-level recovery, the auth-retry
  wrapper, the rate-limit, the role-runner helper). Losing that
  signal trades one-time test deletion for ongoing operator
  confusion when an upstream fix lands silently.

- **Bump `claude-agent-sdk` past 0.2.87 to pick up an upstream fix.**
  Rejected — foreman#262's Worker empirically proved this doesn't
  work. `receive_messages` is byte-identical between 0.2.87 and
  0.2.96; upstream PR #918 fixes the `error_max_turns` /
  `error_during_execution` path in `_read_messages`, NOT the
  `{"type": "error", "error": "success"}` shape foreman#230 cares
  about. Separate ticket if/when upstream ships an actual fix —
  we'll see it via the xfail tripwire flipping.

- **Do nothing — keep PR #255's damage container as the only
  defense.** Rejected per the issue body: the damage container
  prevents the runaway but throws away the agent's actual output
  and forces the operator to investigate. Recovery at the provider
  boundary preserves the agent's work; the damage container only
  fires for genuine failures going forward.

- **Vendor the SDK or pin to a fork.** Rejected — excessive given
  the targeted nature of the fix. A few-line provider-level filter
  is far cheaper to maintain than carrying our own SDK fork.

## Open questions

- **Exact shape of the fake `query()` generator that triggers the
  recovery path in a unit test.** The production observation (issue
  body) is that by the time `Exception("success")` fires, the
  loop's consumer has already received a valid success-shaped
  `ResultMessage`. But the current loop body returns immediately on
  `subtype == "success" and structured_output is not None`, so in a
  naive fake (`async def gen(): yield result; raise Exception("success")`)
  the loop returns on the first iteration and the generator's raise
  never fires. Two candidate fake shapes for the Worker to evaluate
  at impl time:
  1. **`try / finally` cleanup raise:**
     ```python
     async def gen():
         try:
             yield success_result
         finally:
             raise Exception("success")
     ```
     If Python's `async for` cleanup path (implicit `aclose()` on
     return) propagates the `finally` raise to the consumer's
     frame, the wrap catches it.
  2. **Two-stage yield with `last_result` capture before return-eligible message:**
     yield an intermediate ResultMessage that doesn't satisfy the
     early-return condition (e.g. `subtype="success",
     structured_output=None` — captured but no return), then yield
     a success message that does satisfy it but with the SDK's
     raise interleaving on the iterator's next step.
  The Worker MUST verify which shape (or some third) actually
  exercises the recovery path during the red-baseline run (sub-
  request 1). If neither shape works, escalate via the Reviewer /
  Fixer cycle — the bug's empirical observation in production may
  hinge on async-generator cleanup semantics not exposed cleanly in
  a mock. Confidence: medium on this question specifically.

- **Whether `claude_agent_sdk.query()` is an `async generator` or a
  wrapper that explicitly awaits cleanup.** If the former, Python's
  GC-triggered `aclose()` happens AFTER the calling function returns
  — the recovery branch wouldn't fire deterministically. If the
  latter, cleanup is observable at the consumer's frame and the
  recovery works as specified. The Worker can answer this by reading
  the SDK source at impl time (`claude_agent_sdk/__init__.py` or
  `_internal/client.py`); this also informs the fake-generator shape
  above.

## Out of scope

- **Removing the auth-retry wrapper** at
  `packages/foreman/src/foreman/providers/anthropic_sdk.py:155-183`.
  Distinct exception-message shape (`"Claude Code returned an error
  result: ..."` prefix-match vs. bare `"success"` exact-match).
  Both stay as belt-and-suspenders. Per issue body.

- **Removing the defensive role-runner helper**
  `handle_unhandled_role_exception` at
  `packages/foreman/src/foreman/roles/__init__.py:107-163`. Still
  fires for everything outside the narrow recovery path. Per
  issue body.

- **Removing the per-ticket consecutive-failure rate-limit** at
  `packages/foreman/src/foreman/config.py:109-145`. Belt-and-
  suspenders against cascades. Per issue body.

- **Adding telemetry around how often the SDK bug fires.**
  Interesting but explicitly out of scope per issue body;
  foreman#251's dispatch recorder already captures run outcomes.

- **Refactoring `anthropic_sdk.py` more broadly.** Targeted change
  only — only `_iterate_query`'s loop body gets the wrap. Per issue
  body.

- **Vendoring the SDK or pinning to a fork.** Excessive. Per issue
  body.

- **Bumping `claude-agent-sdk`.** foreman#262 closed because no
  current tagged release fixes the bug; reopening is a separate
  ticket if/when upstream ships an actual fix. The xfail tripwire
  is the long-tail signal for that.

- **Removing or flipping `strict=True` on the xfail marker.** Same
  long-tail-signal carve-out as the foreman#262 spec; out of scope
  here. (foreman#262's spec has its own open question about this
  marker's behavior with `strict=False`; that's a separate
  follow-up.)

- **Closing issue #230 or #264 from the impl PR body.** Per
  foreman#63, the Foreman daemon's `merge_impl_pr` action owns
  issue closure; PR-body GitHub closing-keyword references would
  short-circuit the gate. The Worker references plainly.

- **Live verification of the fix on a production ticket.** Per
  issue body: may take days/weeks of activity before the SDK bug
  fires naturally; not required to land the PR. Post-merge
  operator concern.

- **Editing any role runner**
  (`packages/foreman/src/foreman/roles/{planner,reviewer,worker,fixer}.py`).
  The role runners' outermost `except Exception` blocks continue to
  call `handle_unhandled_role_exception` unchanged; the recovery
  happens BELOW them at the provider boundary, so role-runner code
  doesn't need to know about it.
