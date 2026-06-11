# Spec: introduce ProviderAdapter + RecoveryChain — GoF boundary for `claude_agent_sdk` (issue #266)

## Goal

Replace today's inline-special-case provider implementation
(`packages/foreman/src/foreman/providers/anthropic_sdk.py`) with a GoF
five-pattern boundary so SDK-specific exception shapes, known-bug
workarounds, and message-stream parsing live behind a single stable
internal interface. Concretely: introduce a pluggable
`RecoveryStrategy` + `RecoveryChain` for known SDK bug shapes
(starting with the foreman#230 `Exception("success")` shape), expose a
single `make_provider()` facade, translate SDK exception types into a
`ProviderError` family at the adapter boundary, and refactor the four
role runners to depend on the domain exceptions instead of the SDK's.
See issue [#266](https://github.com/jeffrichley/foreman/issues/266).

## Acceptance criteria

- A new module `packages/foreman/src/foreman/providers/recovery.py`
  defines the abstract `RecoveryStrategy` base class (with abstract
  `can_recover(exc, partial) -> bool` and `recover(exc, partial) -> tuple[T, UsageInfo]`),
  the `PartialResult` dataclass (carrying the most recent
  `ResultMessage | None` observed mid-iteration), and the
  `RecoveryChain` class (constructor takes `list[RecoveryStrategy]`;
  exposes `try_recover(exc, partial) -> tuple[T, UsageInfo] | None`).
- A new module `packages/foreman/src/foreman/providers/strategies/success_as_error.py`
  defines `SuccessAsErrorRecovery(RecoveryStrategy)`. Its
  `can_recover` returns `True` ONLY when ALL of these hold:
  (a) `type(exc) is Exception` (the raw bare `Exception`, NOT any
  subclass), (b) `exc.args == ("success",)` exactly, (c)
  `partial.result_message is not None`, (d) `getattr(partial.result_message, "subtype", None) == "success"`,
  and (e) `partial.result_message.structured_output is not None`.
  Its `recover` re-runs the same validation + `_build_usage_info`
  path the success branch uses today and returns the same
  `(validated, usage)` tuple shape.
- A new module `packages/foreman/src/foreman/providers/exceptions.py`
  defines a domain exception hierarchy: `ProviderError` (base),
  `ProviderInvalidResultError`, `ProviderTimeoutError`,
  `ProviderUnknownError`. The three pre-existing exceptions in
  `packages/foreman/src/foreman/provider.py` — `ProviderAuthError`,
  `StructuredOutputRetryError`, `StructuredOutputMissingError` — are
  re-rooted so they inherit from `ProviderError` (preserving their
  current `RuntimeError` lineage via multiple inheritance OR by
  changing the base class — Worker's choice, documented in the impl
  PR body). The existing import paths
  (`from foreman.provider import StructuredOutputRetryError`) MUST
  still work so no role-runner imports break.
- `AnthropicSDKProvider` in `packages/foreman/src/foreman/providers/anthropic_sdk.py`
  is refactored to: (1) accept a `RecoveryChain` constructor argument
  (default `RecoveryChain([])` so old call sites without DI still work
  during the transition); (2) accumulate a `PartialResult` while
  iterating the SDK message stream in `_iterate_query`; (3) in the
  outer `except Exception` of `run_agent`, first attempt
  `chain.try_recover(exc, partial)` BEFORE the existing
  `_SDK_AUTH_ERROR_PREFIX` retry branch — if `try_recover` returns a
  non-`None` result the adapter returns it normally and emits one
  `INFO`-level log line of the form
  `"provider recovery: <StrategyClass> handled <ExcType>"`; if it
  returns `None`, the existing auth-retry branch and the subsequent
  raise path run unchanged.
- A new module-level helper `_translate_sdk_exception(exc) -> ProviderError`
  in `anthropic_sdk.py` maps the SDK's `asyncio.TimeoutError` →
  `ProviderTimeoutError`, the existing `_SDK_AUTH_ERROR_PREFIX`-pattern
  path → `ProviderAuthError` (no behavior change for that branch
  beyond a typed re-raise), and any other unrecognized exception →
  `ProviderUnknownError(str(exc))`. The original is preserved via
  `raise ... from exc`. The translator is invoked at the SAME points
  the adapter currently raises — no new throw sites.
- A new module `packages/foreman/src/foreman/providers/factory.py` (or
  added to `packages/foreman/src/foreman/providers/__init__.py` — Worker's
  choice; either is acceptable provided the public name is stable)
  exposes `make_provider() -> ProviderFacade` returning
  `AnthropicSDKProvider(recovery=RecoveryChain([SuccessAsErrorRecovery()]))`.
  Strategy registration order is documented inline at the construction
  site (a comment naming each registered strategy + why); no
  auto-discovery, no entry-point plugins. The daemon's existing
  provider-construction call sites are migrated to call
  `make_provider()` instead of instantiating `AnthropicSDKProvider`
  directly.
- A new test file `packages/foreman/tests/providers/__init__.py` (empty)
  + `packages/foreman/tests/providers/strategies/__init__.py` (empty)
  + `packages/foreman/tests/providers/strategies/test_success_as_error_recovery.py`
  carries four unit tests for `SuccessAsErrorRecovery` (per the
  `Sub-requests` section below): the positive case, plus three
  specificity negatives.
- A new file `packages/foreman/tests/providers/test_recovery_chain.py`
  carries the `RecoveryChain` unit tests: order-mattering (two
  strategies, first one's `can_recover` is True → it handles;
  second one's `can_recover` is True but first is False → second
  handles), and pass-through (neither matches → `try_recover`
  returns `None`).
- A new file `packages/foreman/tests/providers/test_exception_translation.py`
  carries the translator's unit tests: each branch of
  `_translate_sdk_exception` (auth-prefix, timeout, unknown)
  produces the expected `ProviderError` subclass, and `__cause__`
  preserves the original SDK exception object.
- A new file `packages/foreman/tests/providers/test_adapter_integration.py`
  carries the adapter integration test: drives a fake message stream
  through `AnthropicSDKProvider` (using the same `_patch_query`
  helper pattern as
  `packages/foreman/tests/test_provider_anthropic_sdk.py`) covering
  (a) normal `subtype="success"` path returns the validated tuple,
  (b) the `Exception("success")` shape (after a valid ResultMessage
  was observed) is recovered via the chain and returns the same
  tuple a normal success would, (c) a translated SDK exception
  surfaces as the corresponding `ProviderError` subclass with
  `__cause__` set.
- A boundary-discipline test in `packages/foreman/tests/test_provider_boundary.py`
  asserts that no module under `packages/foreman/src/foreman/` other
  than `packages/foreman/src/foreman/providers/anthropic_sdk.py`
  contains a top-level `from claude_agent_sdk` or
  `import claude_agent_sdk` line. Implementation: walk all `.py`
  files under `packages/foreman/src/foreman/`, parse via `ast`
  (NOT regex — comments + string literals must not register), assert
  the SDK is imported by at most one source module. The test file
  itself is allowed to mention the string in comments since the
  scan is AST-based.
- The four role runners (`packages/foreman/src/foreman/roles/planner.py`,
  `roles/reviewer.py`, `roles/worker.py`, `roles/fixer.py`) each
  add a targeted `except ProviderError as exc:` clause UPSTREAM of
  their existing PR #255 `except Exception as exc:` clause. The
  typed catch is responsible for routing provider failures to
  `handle_unhandled_role_exception` (planner / reviewer / fixer) or
  the synthesized `WorkerOutput(outcome="incomplete")` (worker)
  EXACTLY as today's broad `except Exception` does. The broad
  `except Exception` block stays in place as belt-and-suspenders for
  everything else (worktree / git / GitHub I/O); it MUST remain
  reachable for non-provider exceptions. No behavior change for
  failing runs — the change is structural (typed catch vs untyped
  catch), not semantic.
- All four role runners' module-level imports DO NOT import
  `claude_agent_sdk` (verified by the boundary-discipline test
  above). Today none do; the spec pins this property going forward.
- The existing xfail tripwire test at
  `packages/foreman/tests/test_provider_anthropic_sdk.py:825-882`
  (`test_sdk_receive_messages_does_not_raise_on_success_subtype`) is
  REMOVED. Its replacement is the strategy-level unit tests + the
  adapter integration test (per the issue body's "Out of scope" note:
  "The xfail test becomes redundant"). The module-top `_SDKTransport`
  import at line 32 is also removed since it was only consumed by
  the deleted test. Code comments referencing foreman#230 elsewhere
  in the file may stay.
- The existing `_SDK_AUTH_ERROR_PREFIX` auth-retry guard at
  `packages/foreman/src/foreman/providers/anthropic_sdk.py:145-183`
  STAYS in place. The recovery chain handles the foreman#230
  `Exception("success")` shape; the auth-retry guard handles the
  `Exception("Claude Code returned an error result: ...")` shape.
  Different bugs, different defenses; both stay.
- The defensive `handle_unhandled_role_exception` helper at
  `packages/foreman/src/foreman/roles/__init__.py:107-163` stays
  unchanged. Same role: catches whatever the typed-catch upstream
  missed, transitions the label, posts the comment.
- The rate-limit config at
  `packages/foreman/src/foreman/config.py` (the PR #255 commit 3
  fields) is NOT touched. Different defense layer.
- `just check` exits zero on the impl worktree (lint + typecheck +
  full pytest suite green, including the new tests, with the xfail
  removed and the boundary test added).
- The impl PR body references issues #230, #262, #264 and PR #255
  as the historical investigation path that converged here, in
  prose only — NO GitHub closing-keyword references
  (`Closes #N` / `Fixes #N` / `Resolves #N`) to ANY of these
  numbers, per foreman#63. Reference plainly: "addresses #266",
  "consolidates the investigation from #230 / #262 / #264".

## Approach

This is a structural refactor at the provider boundary. The five GoF
patterns (Adapter, Strategy, Chain of Responsibility, Translator,
Facade) are introduced as a coordinated set so the boundary is
auditable in one place and each pattern earns its keep via a concrete
maintenance dividend the issue body documents (boundary isolation,
pluggable bug workarounds, observability, test isolation, future
provider swap).

**Reconciling the issue's named types with the existing codebase.**
The issue body uses illustrative names (`ProviderProtocol`,
`AgentResult`) that don't exactly match what's in the repo today.
Reality: `foreman/provider.py` already defines
`ProviderFacade(ABC)` as the abstract base and the `run_agent`
contract returns `tuple[T, UsageInfo]` not an `AgentResult` wrapper.
The spec respects the existing names — `ProviderFacade` stays as
the abstract base, the `run_agent` return type stays as
`tuple[T, UsageInfo]`, and the recovery chain returns the same
tuple shape so the adapter's exception → recovery → return path
type-checks without changing the public contract. The GoF patterns
the issue calls for are introduced as NEW types
(`RecoveryStrategy`, `RecoveryChain`, `PartialResult`,
`ProviderError` family); the EXISTING types stay backwards-compatible
so the change is purely additive at the seams. This keeps the blast
radius narrow and avoids a parallel "rename ProviderFacade →
ProviderProtocol" cleanup that the issue did not ask for.

**Module layout.** The provider boundary code lives under
`packages/foreman/src/foreman/providers/`:

- `__init__.py` — exports `make_provider`, `ProviderError` (and
  subclasses), `RecoveryStrategy`, `RecoveryChain`. This becomes the
  canonical import surface for role runners.
- `anthropic_sdk.py` — concrete adapter. Continues to be the ONLY
  module that imports from `claude_agent_sdk`.
- `recovery.py` — `RecoveryStrategy` ABC, `PartialResult` dataclass,
  `RecoveryChain` class.
- `strategies/__init__.py` — exports `SuccessAsErrorRecovery`.
- `strategies/success_as_error.py` — first concrete strategy.
- `exceptions.py` — `ProviderError` family.
- `factory.py` (or merged into `__init__.py`) — `make_provider()`.

Tests mirror the layout under `packages/foreman/tests/providers/`.

**Why explicit strategy registration, not auto-discovery.** The
issue body's "What we're NOT doing" list is explicit on this point
and the spec respects it: every deployed strategy MUST be visible
at one source location (`make_provider()`). When the next SDK bug
shape appears, adding a new strategy is one new file + one new line
in the factory. The reviewability comes from being able to read
`make_provider()` and see the entire chain in priority order.

**Why a single boundary-discipline test.** A test that asserts "no
module outside `providers/` imports `claude_agent_sdk`" prevents the
silent drift where some future PR adds `from claude_agent_sdk import
...` to a role runner because "the type was convenient". Implemented
as an AST walk over `packages/foreman/src/foreman/`. AST not grep so
comments and string literals don't false-positive (e.g. a docstring
that quotes a `from claude_agent_sdk` example). This is the architectural
guardrail; without it, the patterns degrade over time.

**Strategy semantics deliberately strict.** The
`SuccessAsErrorRecovery.can_recover` predicate uses `type(exc) is
Exception` not `isinstance(exc, Exception)`. The bug shape we're
catching is the SDK's raw `Exception("success")` raise — NOT any
subclass; subclasses indicate the SDK actually classified the error,
which we want to surface. The `args == ("success",)` equality is
also strict for the same reason — a message like
`"Claude Code returned an error result: success"` is a DIFFERENT
bug (auth failure) handled by the auth-retry branch, not this one.
The `partial.result_message` checks enforce that recovery only fires
when we have a logically-successful result to return; if we don't,
the strategy declines and the exception propagates normally. This
strictness is deliberately tight — broader matchers would mask
genuine SDK failures as fake successes.

**The auth-retry guard relationship.** Two separate bug shapes:
- foreman#230: SDK raises bare `Exception("success")` AFTER yielding
  a valid `ResultMessage(subtype="success")`. We recover.
- foreman#227: SDK raises `Exception("Claude Code returned an error
  result: <msg>")` because the token expired during a query. We
  refresh creds and retry once.

The order matters and is documented in code: the recovery chain
runs FIRST (after the SDK iteration completes), so foreman#230's
shape gets a proper recovery. THEN the auth-retry branch runs for
remaining exceptions. THEN translation. THEN raise. The branches
don't overlap — foreman#230 produces `exc.args == ("success",)` with
NO prefix; foreman#227 produces `str(exc).startswith("Claude Code
returned an error result")` with no args-tuple match. The
`can_recover` predicate's strictness keeps the two cleanly
separated.

**Role runner refactor is minimal-surface.** Each role gets one new
`except ProviderError as exc:` arm immediately above the existing
`except Exception as exc:` arm, with the SAME body (delegate to
`handle_unhandled_role_exception` for planner/reviewer/fixer, or
synthesize the incomplete WorkerOutput for worker). This is
syntactic — the work isn't re-routed, just typed. The reason to add
the typed clause anyway: it documents at the call site that
`ProviderError` is the expected failure shape, and future static
analysis (e.g. mypy strict mode) can see the typed branch separately
from the catch-all. Without the typed clause, a code reader has to
guess what exceptions can escape `provider.run_agent`.

**TDD discipline.** Each new module gets its red-first test before
the implementation. The strategy gets the four unit tests (one
positive, three specificity negatives) before
`SuccessAsErrorRecovery.can_recover` exists. The chain gets its
order + pass-through tests before `RecoveryChain.try_recover`
exists. The translator gets its branch tests before
`_translate_sdk_exception` exists. The boundary-discipline test
goes red first (today, `anthropic_sdk.py` is the only importer, so
the test passes from day 1 — the assertion shape MUST be written
such that the FIRST PR that adds a second importer fails the
suite). The adapter integration test goes red BEFORE the
adapter's recovery wiring lands.

**What this gives us, restated to keep the design honest:**
- Boundary isolation. After this lands, exactly one module imports
  from `claude_agent_sdk`. New PRs that violate this fail the
  boundary test loudly.
- Pluggable bug workarounds. New SDK bug = new strategy file (~30
  lines) + one line in `make_provider()`.
- Observability. Recovery firings emit a single log line naming the
  strategy class; downstream log-grep gives per-strategy fire counts.
- Test isolation. Each strategy gets its own test file; failures
  localize.
- Future provider swap. `make_provider()` is the single switch
  point; an `OpenAIProviderAdapter` would land alongside without
  role-runner changes.

## Sub-requests (topologically sorted)

1. **Create the new providers test package skeleton.** Add
   `packages/foreman/tests/providers/__init__.py` (empty) and
   `packages/foreman/tests/providers/strategies/__init__.py`
   (empty). Run pytest collection to confirm pytest sees them.

2. **Write the four red unit tests for `SuccessAsErrorRecovery`** at
   `packages/foreman/tests/providers/strategies/test_success_as_error_recovery.py`.
   Tests reference `from foreman.providers.strategies.success_as_error
   import SuccessAsErrorRecovery` and
   `from foreman.providers.recovery import PartialResult`. Tests fail
   with `ModuleNotFoundError` against current main (modules don't
   exist yet). Required tests:
   - `test_recovers_when_exception_success_and_valid_result_message`:
     `PartialResult` carrying a `ResultMessage(subtype="success",
     structured_output={...valid for _DemoOutput...})` + `exc =
     Exception("success")`. Assert `can_recover` returns True and
     `recover` returns the `(validated_DemoOutput_instance, UsageInfo)`
     tuple.
   - `test_declines_when_exception_message_is_different`: same
     partial state, `exc = Exception("error_max_turns")`. Assert
     `can_recover` returns False.
   - `test_declines_when_no_result_message_observed`:
     `PartialResult(result_message=None)`, `exc = Exception("success")`.
     Assert `can_recover` returns False.
   - `test_declines_when_result_message_subtype_is_error`:
     `PartialResult(result_message=ResultMessage(subtype="error",
     structured_output=None, ...))`, `exc = Exception("success")`.
     Assert `can_recover` returns False.

3. **Write the `RecoveryChain` red tests** at
   `packages/foreman/tests/providers/test_recovery_chain.py`:
   - `test_first_matching_strategy_handles`: chain of two fake
     strategies whose `can_recover` is `[True, True]`. Assert the
     first one's `recover` was called, second's was not.
   - `test_order_matters_when_only_second_matches`: chain of two
     fake strategies, `can_recover` is `[False, True]`. Assert the
     second's `recover` was called.
   - `test_returns_none_when_none_match`: chain of two fake
     strategies whose `can_recover` is `[False, False]`. Assert
     `try_recover` returns `None`.
   - `test_logs_strategy_class_name_on_recovery`: use
     `caplog.set_level(logging.INFO)`, fire a recovery, assert the
     emitted log record contains the strategy class name.

4. **Write the exception-translation red tests** at
   `packages/foreman/tests/providers/test_exception_translation.py`:
   - `test_translates_auth_prefix_to_provider_auth_error`: input
     `Exception("Claude Code returned an error result: foo")` →
     output is `ProviderAuthError`, `__cause__` is the original.
   - `test_translates_asyncio_timeout`: input
     `asyncio.TimeoutError()` → output is `ProviderTimeoutError`.
   - `test_translates_unknown_to_provider_unknown_error`: input
     `Exception("weird unknown")` (no prefix, no special shape) →
     output is `ProviderUnknownError`, `__cause__` is the original.

5. **Write the boundary-discipline red test** at
   `packages/foreman/tests/test_provider_boundary.py`:
   - `test_no_module_outside_providers_imports_claude_agent_sdk`:
     walk `packages/foreman/src/foreman/`, AST-parse each `.py`,
     collect modules with a top-level `from claude_agent_sdk` or
     `import claude_agent_sdk` statement. Assert the only such
     module is `foreman/providers/anthropic_sdk.py`. Today this
     passes; lock the property forward.

6. **Write the adapter integration red tests** at
   `packages/foreman/tests/providers/test_adapter_integration.py`.
   Use the same `_patch_query` style as the existing
   `test_provider_anthropic_sdk.py`. Tests:
   - `test_normal_success_path_returns_validated_tuple`: chain
     contains `SuccessAsErrorRecovery`; SDK yields a valid
     `ResultMessage(subtype="success", structured_output={...})`.
     Assert `run_agent` returns the expected `(model, usage)`.
   - `test_success_as_error_shape_is_recovered_by_chain`: chain
     contains `SuccessAsErrorRecovery`; SDK yields a valid
     `ResultMessage` then `raise Exception("success")`. Assert
     `run_agent` returns the same tuple the success path would,
     AND a log line of the form
     `"provider recovery: SuccessAsErrorRecovery handled Exception"`
     was emitted.
   - `test_unknown_sdk_exception_translates_to_provider_unknown_error`:
     chain is empty; SDK raises `Exception("totally weird")`.
     Assert the adapter raises `ProviderUnknownError` with the
     original as `__cause__`.
   - `test_auth_prefix_path_still_translates_to_provider_auth_error`:
     chain is empty; SDK raises `Exception("Claude Code returned
     an error result: ...")` BOTH calls (so the auth-retry guard
     surrenders). Assert the adapter raises `ProviderAuthError`.

7. **Create the providers module skeleton** —
   `packages/foreman/src/foreman/providers/exceptions.py` first:
   ```python
   """Domain exception hierarchy for the provider boundary."""
   from __future__ import annotations


   class ProviderError(Exception):
       """Base class for all provider-boundary errors."""


   class ProviderInvalidResultError(ProviderError):
       """The provider returned a structured result the adapter could not
       validate against the output model schema."""


   class ProviderTimeoutError(ProviderError):
       """The provider's underlying transport timed out."""


   class ProviderUnknownError(ProviderError):
       """The provider raised an exception the adapter could not classify.

       Carries the original exception as ``__cause__``.
       """
   ```
   Re-root the three pre-existing exceptions in
   `packages/foreman/src/foreman/provider.py` so they inherit from
   `ProviderError` (e.g.
   `class ProviderAuthError(ProviderError, RuntimeError): ...`,
   `class StructuredOutputRetryError(ProviderError, RuntimeError): ...`,
   `class StructuredOutputMissingError(ProviderError, RuntimeError): ...`).
   This keeps existing `isinstance(exc, RuntimeError)` checks (if any)
   working while adding `ProviderError` as a polymorphic root.

8. **Create `recovery.py`:**
   ```python
   """Recovery strategy pattern for SDK bug workarounds."""
   from __future__ import annotations

   import logging
   from abc import ABC, abstractmethod
   from dataclasses import dataclass
   from typing import Any, Generic, TypeVar

   from pydantic import BaseModel

   from foreman.provider import UsageInfo

   _log = logging.getLogger(__name__)
   T = TypeVar("T", bound=BaseModel)


   @dataclass
   class PartialResult(Generic[T]):
       """State captured mid-iteration that recovery strategies can inspect."""

       result_message: Any | None = None
       output_model: type[T] | None = None


   class RecoveryStrategy(ABC):
       """One known SDK bug shape we know how to recover from."""

       @abstractmethod
       def can_recover(
           self, exc: BaseException, partial: PartialResult[Any]
       ) -> bool: ...

       @abstractmethod
       def recover(
           self, exc: BaseException, partial: PartialResult[T]
       ) -> tuple[T, UsageInfo]: ...


   class RecoveryChain:
       """Try each registered strategy in order; first match handles."""

       def __init__(self, strategies: list[RecoveryStrategy]) -> None:
           self._strategies = tuple(strategies)

       def try_recover(
           self, exc: BaseException, partial: PartialResult[T]
       ) -> tuple[T, UsageInfo] | None:
           for s in self._strategies:
               if s.can_recover(exc, partial):
                   _log.info(
                       "provider recovery: %s handled %s",
                       s.__class__.__name__,
                       type(exc).__name__,
                   )
                   return s.recover(exc, partial)
           return None
   ```
   Run sub-request 3's tests; they should now go green.

9. **Create `strategies/success_as_error.py`:** the concrete
   strategy implementing `can_recover` per the strict predicate
   above and `recover` re-using `_build_usage_info` from the
   adapter (move `_build_usage_info` to a shared location like
   `providers/_usage.py` so the strategy can import it without a
   cycle, OR pass a callable into the strategy at construction time
   — Worker's call). Run sub-request 2's tests; they should go green.

10. **Wire the `_translate_sdk_exception` helper** into
    `packages/foreman/src/foreman/providers/anthropic_sdk.py`. New
    private module-level function; the three branches
    (auth-prefix → `ProviderAuthError`, `asyncio.TimeoutError` →
    `ProviderTimeoutError`, else → `ProviderUnknownError`). Use
    `raise translated from exc` semantics. Run sub-request 4's
    tests; they should go green.

11. **Refactor `AnthropicSDKProvider.run_agent` / `_iterate_query`**
    to (a) accept an optional `recovery: RecoveryChain | None = None`
    constructor arg (default `RecoveryChain([])`), (b) accumulate
    `PartialResult` while iterating (set `result_message` on each
    `ResultMessage` seen), (c) in the outer `except Exception`
    branch, call `chain.try_recover(exc, partial)` FIRST; if it
    returns a tuple, return it; if `None`, fall through to the
    existing auth-retry guard; if the auth-retry guard also gives
    up, call `_translate_sdk_exception(exc)` and raise the
    translated error. Run sub-request 6's tests; they should go
    green.

12. **Create `make_provider()`** in
    `packages/foreman/src/foreman/providers/__init__.py` (or a new
    `factory.py` exported from `__init__.py`):
    ```python
    from foreman.providers.anthropic_sdk import AnthropicSDKProvider
    from foreman.providers.recovery import RecoveryChain
    from foreman.providers.strategies.success_as_error import SuccessAsErrorRecovery
    from foreman.provider import ProviderFacade


    def make_provider() -> ProviderFacade:
        """Construct the production provider with its deployed recovery chain.

        Strategy registration order is significant — the first strategy
        whose ``can_recover`` returns True wins. Deployed order:

        1. ``SuccessAsErrorRecovery`` — foreman#230: bare
           Exception("success") after a valid ResultMessage. Catches
           the most-observed SDK bug.
        """
        recovery = RecoveryChain([
            SuccessAsErrorRecovery(),
        ])
        return AnthropicSDKProvider(recovery=recovery)
    ```
    Migrate every existing call site that instantiates
    `AnthropicSDKProvider` directly to call `make_provider()`
    instead. Use `grep -n 'AnthropicSDKProvider(' packages/foreman/`
    to find them.

13. **Run the boundary-discipline test** (sub-request 5). It
    should already pass since today's surface is correct; this
    sub-request just verifies the assertion shape catches the
    expected case. To smoke-test the assertion's negative path
    once, temporarily add a `from claude_agent_sdk import query`
    to `packages/foreman/src/foreman/roles/planner.py`, run the
    test, confirm it fails, revert the temp import. Do NOT commit
    the temp import.

14. **Refactor the role runners.** For each of
    `packages/foreman/src/foreman/roles/planner.py:348`,
    `roles/reviewer.py:620`, `roles/worker.py:886`,
    `roles/worker.py:1206`, and `roles/fixer.py:485+` (line
    numbers will drift slightly as the spec lands; use the
    `provider.run_agent`-adjacent `except Exception` clauses as
    the anchor), add a new `except ProviderError as exc:` arm
    IMMEDIATELY ABOVE the existing `except Exception as exc:`. The
    new arm's body is identical to the existing one's; the goal is
    type narrowing for readability + boundary documentation, not a
    behavior change.

    Concretely, for each call site, change:
    ```python
    except Exception as exc:
        # existing PR #255 commit 2 defensive handler body
    ```
    to:
    ```python
    except ProviderError as exc:
        # SAME body as below — typed catch for the documented provider
        # boundary failure mode. Belt-and-suspenders Exception catch
        # below stays in place for non-provider failures (worktree
        # ops, host I/O, etc.).
        ...
    except Exception as exc:
        # existing PR #255 commit 2 defensive handler body
    ```
    Each role module imports `ProviderError` from
    `foreman.providers` (NOT from `claude_agent_sdk`, which the
    boundary test would catch).

15. **Delete the xfail tripwire** at
    `packages/foreman/tests/test_provider_anthropic_sdk.py:825-882`
    (the test function + the `@pytest.mark.xfail(...)` block) AND
    the `_SuccessAsErrorTransport` fake at lines 765-822 (its only
    consumer was the deleted test) AND the now-unused module-top
    import `from claude_agent_sdk._internal.transport import
    Transport as _SDKTransport` at line 32. The strategy-level
    tests + adapter integration test now cover the same contract.

16. **Run the full quality gate:** `just check`. Expected: exit 0.
    All new tests green. The deleted xfail no longer appears in
    pytest's xfail summary. mypy + ruff clean.

17. **Live verification (optional, post-merge).** Queue a benign
    issue through the autonomous loop. If the SDK bug fires during
    the run, the dispatch completes via recovery instead of going
    to `foreman:needs-help`. Check the daemon log for the
    `"provider recovery: SuccessAsErrorRecovery handled Exception"`
    line emission. This is an operator-level smoke and not blocking
    on merge — the bug fires intermittently and the impl PR
    shouldn't sit waiting on a window where it fires.

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/src/foreman/providers/__init__.py` | Add `make_provider()` factory (or re-export from `factory.py`). Add re-exports of `ProviderError` family + `RecoveryChain` + `RecoveryStrategy` + `SuccessAsErrorRecovery` for role-runner imports. |
| `packages/foreman/src/foreman/providers/exceptions.py` | **New.** `ProviderError` (base), `ProviderInvalidResultError`, `ProviderTimeoutError`, `ProviderUnknownError`. |
| `packages/foreman/src/foreman/providers/recovery.py` | **New.** `PartialResult` dataclass + `RecoveryStrategy` ABC + `RecoveryChain`. |
| `packages/foreman/src/foreman/providers/strategies/__init__.py` | **New.** Re-exports `SuccessAsErrorRecovery`. |
| `packages/foreman/src/foreman/providers/strategies/success_as_error.py` | **New.** Concrete `SuccessAsErrorRecovery`. |
| `packages/foreman/src/foreman/providers/factory.py` | **New** (optional — may live in `__init__.py`). Houses `make_provider()`. |
| `packages/foreman/src/foreman/providers/anthropic_sdk.py` | Refactor: accept `recovery: RecoveryChain | None = None` in `__init__`; `_iterate_query` now also yields `PartialResult` state to the caller's exception handler; `run_agent` outer-except calls `chain.try_recover` first, then the existing auth-retry guard, then `_translate_sdk_exception` for unrecognized errors. Add `_translate_sdk_exception` helper. The `_build_usage_info` helper may move to `providers/_usage.py` so strategies can reuse it without a circular import. |
| `packages/foreman/src/foreman/provider.py` | Re-root `ProviderAuthError` / `StructuredOutputRetryError` / `StructuredOutputMissingError` to inherit from `ProviderError`. Pre-existing import paths (`from foreman.provider import StructuredOutputRetryError`) still work. |
| `packages/foreman/src/foreman/roles/planner.py` | Add `except ProviderError as exc:` arm immediately above the existing `except Exception as exc:` at line 348. Same handler body. Import `ProviderError` from `foreman.providers`. |
| `packages/foreman/src/foreman/roles/reviewer.py` | Same change at line 620. |
| `packages/foreman/src/foreman/roles/worker.py` | Same change at the `provider.run_agent`-adjacent except clauses (line 886 inner; line 1206 outer). |
| `packages/foreman/src/foreman/roles/fixer.py` | Same change at line 485 (and line 785 if it wraps `provider.run_agent`). |
| Daemon / CLI call sites instantiating `AnthropicSDKProvider` directly | Migrate to `make_provider()`. Find via `grep -n 'AnthropicSDKProvider(' packages/foreman/src/`. |
| `packages/foreman/tests/providers/__init__.py` | **New.** Empty marker file. |
| `packages/foreman/tests/providers/strategies/__init__.py` | **New.** Empty marker file. |
| `packages/foreman/tests/providers/strategies/test_success_as_error_recovery.py` | **New.** Four unit tests. |
| `packages/foreman/tests/providers/test_recovery_chain.py` | **New.** Chain unit tests. |
| `packages/foreman/tests/providers/test_exception_translation.py` | **New.** Translator unit tests. |
| `packages/foreman/tests/providers/test_adapter_integration.py` | **New.** End-to-end adapter tests with fake SDK message stream. |
| `packages/foreman/tests/test_provider_boundary.py` | **New.** AST scan asserting only `providers/anthropic_sdk.py` imports `claude_agent_sdk`. |
| `packages/foreman/tests/test_provider_anthropic_sdk.py` | DELETE lines 825-882 (xfail test) + 765-822 (`_SuccessAsErrorTransport` fake) + line 32 (`_SDKTransport` import). Comments referencing foreman#230 elsewhere may stay. |

No expected changes to:

- `packages/foreman/src/foreman/config.py` — PR #255 commit 3
  rate-limit config stays.
- `packages/foreman/src/foreman/roles/__init__.py` — the
  defensive `handle_unhandled_role_exception` helper stays.
- The `_SDK_AUTH_ERROR_PREFIX` constant + auth-retry wrapper at
  `anthropic_sdk.py:55,145-183` — the wrapper now raises a typed
  `ProviderAuthError` via the translator; the retry logic itself
  is unchanged.
- `Dockerfile` / `pyproject.toml` / `uv.lock`.

## Alternatives considered

- **Rename `ProviderFacade` → `ProviderProtocol` (PEP 544
  `typing.Protocol`).** Rejected — the issue body uses the name
  illustratively, not as a rename request, and changing the base
  type from `ABC` to `Protocol` would touch every concrete
  implementation's class declaration and most tests' assertion
  shapes. The maintenance dividend (slightly more idiomatic typing)
  does not pay for the diff size. The structural separation the
  issue actually asks for is achieved with the existing `ABC`.

- **Introduce an `AgentResult` wrapper dataclass.** Rejected —
  the existing public contract `tuple[T, UsageInfo]` is already in
  use by every role runner. Replacing it would propagate a
  ~30-line diff through each role runner's `provider.run_agent`
  call site, the type annotations, and several test helpers
  (`_make_result`, the `_patch_query` callers). The recovery
  chain returns the same tuple, so no internal type bridge is
  needed; the bug workaround doesn't require the wrapper to land.
  Re-evaluate when a second provider lands.

- **Auto-discover strategies via entry points / decorators.**
  Rejected per the issue body's "What we're NOT doing" list.
  Explicit registration in `make_provider()` is the audit
  boundary. Auto-discovery would make the deployed behavior
  depend on which strategies happened to be importable, which is
  exactly the property we don't want.

- **Generic "string in message" matcher.** Rejected per the issue
  body. Each strategy specifies a narrow `can_recover` predicate
  with type checks, args-tuple equality, and partial-state
  inspection. String sniffing has the wrong defaults — every
  near-miss fires recovery instead of failing loudly.

- **Remove the PR #255 commit 2 defensive helper after this
  lands.** Rejected — the defensive helper catches non-provider
  exceptions too (worktree creation, host I/O, GitHub 5xx). The
  recovery chain only handles known SDK bug shapes. Removing
  the defensive helper would re-open the runaway-burn window
  foreman#229 closed. Belt-and-suspenders is the discipline.

- **File an upstream issue at `anthropics/claude-agent-sdk-python`
  to fix the bare `Exception("success")` raise.** Rejected per
  the issue body ("deferred indefinitely"). Even if filed, we
  need the recovery in place to handle the production fleet in
  the meantime; the adapter pattern doesn't preclude the upstream
  fix — when it lands, the strategy stops firing (visible in the
  log) and can be deleted in a follow-up.

- **Vendor a forked `claude_agent_sdk`.** Rejected per the issue
  body. The adapter pattern is exactly the alternative to
  vendoring — same isolation, no maintenance burden on the
  vendored copy.

- **Add a generic recovery DSL (`StrategyA.and_(StrategyB)`,
  `StrategyA.or_(StrategyB)`).** Rejected per the issue body
  ("YAGNI today; revisit when we have 4+ strategies in the
  chain"). One strategy registered today.

- **Land the boundary-discipline test AFTER the refactor.**
  Rejected — the test goes red on the (hypothetical) PR that adds
  a second `from claude_agent_sdk` import. Landing the test
  upfront pins the invariant before it has a chance to drift.

## Open questions

- **Should `_build_usage_info` move out of `anthropic_sdk.py` into
  a shared `providers/_usage.py`?** The recovery strategy needs
  to call it (to reconstruct the `UsageInfo` from the captured
  `ResultMessage`). Two options: (a) move it to a shared module
  + import from both adapter and strategy, OR (b) pass it as a
  callable into the strategy at construction. Option (a) is
  simpler; option (b) is more testable. The spec leaves the
  choice to the Worker; either is acceptable. Recommend (a).

- **Should the new `make_provider()` live in `providers/__init__.py`
  or `providers/factory.py`?** Both are reasonable. Putting it in
  `__init__.py` makes the import path the shortest
  (`from foreman.providers import make_provider`). Putting it in
  `factory.py` keeps the module purpose more atomic. The spec
  leaves this to the Worker. Recommend `__init__.py` for the
  shorter import.

- **Does the `ProviderError` re-rooting break any external code
  that imports `StructuredOutputRetryError` and uses
  `isinstance(exc, RuntimeError)`?** Using multiple inheritance
  (`class StructuredOutputRetryError(ProviderError, RuntimeError):`)
  keeps both checks working. The Worker MUST verify by running
  the full test suite after the re-root and inspecting any
  `isinstance` assertions in the test files. If a test breaks,
  the re-root is wrong — adjust the base class order or revert
  the multiple inheritance.

Confidence: medium. The patterns are well-understood, the
file boundaries are clear, and the test discipline (red-first per
new module) catches drift. The two real risks are (1) the
`_build_usage_info` extraction (cycle-avoidance — sub-request 9
calls out a workaround) and (2) the role-runner refactor
introducing a typed-catch that accidentally masks a non-provider
exception (mitigated by keeping the broad `except Exception`
arm beneath as belt-and-suspenders).

## Out of scope

- **New recovery strategies beyond `SuccessAsErrorRecovery`.** File
  separately as concrete SDK bug shapes are observed in the
  daemon logs.
- **Refactoring `provider.py` (the higher-level facade ABC).** The
  boundary work here is at the adapter layer
  (`providers/anthropic_sdk.py`); `provider.py` only gains the
  `ProviderError` re-rooting on the three existing exception
  classes.
- **Adding metrics / Prometheus emission for recovery firings.**
  The single log line `"provider recovery: <Strategy> handled
  <ExcType>"` is enough for v1; downstream log analysis can graph
  it without code changes. File separately if metric scraping
  becomes operationally important.
- **Config knob to disable recovery strategies in production
  (env var / TOML field).** YAGNI. If a strategy ever needs to
  be hot-disabled, file separately.
- **Removing the auth-retry wrapper at
  `anthropic_sdk.py:145-183`** (PR #255 commit 2 path). It
  handles the foreman#227 token-expiry shape, not the
  foreman#230 success-as-error shape. Different bugs, different
  defenses.
- **Removing the per-ticket consecutive-failure rate-limit** at
  `config.py:109-145` (PR #255 commit 3). Belt-and-suspenders
  cascade defense; stays.
- **Removing the defensive `handle_unhandled_role_exception`
  helper** at `roles/__init__.py`. Stays — handles non-provider
  exceptions.
- **Filing an upstream issue at
  `anthropics/claude-agent-sdk-python`.** Deferred per the issue
  body.
- **Vendoring or forking `claude_agent_sdk`.** The adapter
  pattern is the alternative; this is exactly what's being
  built.
- **An async-iteration adapter for `query` (a generator yielding
  domain types instead of SDK types).** YAGNI — role runners only
  consume the final tuple.
- **Renaming `ProviderFacade` to `ProviderProtocol` or converting
  it to `typing.Protocol`.** Out of scope; see Alternatives.
- **Introducing an `AgentResult` wrapper dataclass.** Out of scope;
  see Alternatives.
- **Containerized live verification of the recovery firing.** The
  Worker subprocess runs inside the daemon container it cannot
  rebuild; live verification is operator-level post-merge work.
- **Adjusting the structured-output / mypy strict mode level.**
  Whatever mypy mode the project currently runs continues to
  apply.

## References

- foreman#266 — this ticket.
- foreman#230 — original SDK investigation. Subsumed by this work.
- foreman#262 — SDK bump attempt. Worker empirically proved no
  released SDK tag fixes the bug; log at
  `/foreman/logs/worker/262__2026-06-11T02-04-07-635603Z.log`.
- foreman#264 — narrow inline `Exception("success")` band-aid
  version. Replaced by this five-pattern design.
- PR #255 — the three reliability defenses (xfail tripwire test,
  role-runner defensive helper, per-ticket rate-limit). The
  xfail test from commit 1 is removed by this PR (replaced by
  strategy-level tests); the helper from commit 2 stays; the
  rate-limit from commit 3 stays.
- foreman#227 — the original 171-dispatch runaway that exposed
  the SDK bug.
- foreman#229 — the runaway-burn defense (defensive helper).
- foreman#228 — the rate-limit defense.
- GoF (Gamma, Helm, Johnson, Vlissides 1994): Adapter, Strategy,
  Chain of Responsibility, Facade — the four patterns directly
  named. Translator is the complementary domain-exception
  translation pattern at the boundary.
- Existing source code reference points:
  - `packages/foreman/src/foreman/providers/anthropic_sdk.py:30,99,202,210` — current SDK iteration loop + success path.
  - `packages/foreman/src/foreman/providers/anthropic_sdk.py:55,145-183` — current auth-retry guard.
  - `packages/foreman/src/foreman/provider.py:74-101` — current exception classes.
  - `packages/foreman/src/foreman/roles/planner.py:241,348` — call site + outer except.
  - `packages/foreman/src/foreman/roles/worker.py:877,886,1206` — call site + inner/outer excepts.
  - `packages/foreman/src/foreman/roles/__init__.py:107` — defensive helper.
  - `packages/foreman/tests/test_provider_anthropic_sdk.py:32,765-822,825-882` — xfail test + transport fake to delete.
