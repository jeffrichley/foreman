# Foreman role-runtime speedup investigation (issue #162)

This report audits Jeff's hypothesis from issue #162 — that Foreman's
role subprocesses (Planner, Worker, Reviewer, Fixer) are dispatching
subagents via the claude-agent-sdk Task tool when they don't need to —
and triages the five adjacent speedup vectors the issue lists. The
deliverable is a citation-backed punch list of follow-up tickets, NOT
a prompt or code change. Every code-side remediation is intentionally
deferred to a follow-up ticket so a human can review the audit's
evidence before any prompt or code change lands.

## Subagent availability

The first question the audit must answer empirically is: can each
role's LLM actually reach the Task tool today? The answer is **yes,
for every role, by default** — and that's a configuration the
adapter never explicitly chose.

All four roles dispatch through the same provider call, which sets
`allowed_tools=...` but never sets `tools=...`:

```python
# packages/foreman/src/foreman/providers/anthropic_sdk.py:84-97
schema = output_model.model_json_schema()
hint = cwd.name or "role"
with _system_prompt_file(system_prompt, hint=hint) as sp_path:
    options_kwargs: dict[str, Any] = dict(
        system_prompt={"type": "file", "path": str(sp_path)},
        cwd=str(cwd),
        allowed_tools=allowed_tools,
        permission_mode="acceptEdits",
        max_turns=max_turns,
        output_format={"type": "json_schema", "schema": schema},
    )
    if env is not None:
        options_kwargs["env"] = env
    options = ClaudeAgentOptions(**options_kwargs)
```

Each role passes its own narrow `*_ALLOWED_TOOLS` list into that call:

- **Planner**: `PLANNER_ALLOWED_TOOLS = ["Read", "Glob", "Grep"]`
  (`packages/foreman/src/foreman/roles/planner.py:56`)
- **Worker**: `WORKER_ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Bash", "Edit", "Write"]`
  (`packages/foreman/src/foreman/roles/worker.py:105`)
- **Reviewer**: `REVIEWER_ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Bash"]`
  (`packages/foreman/src/foreman/roles/reviewer.py:60`)
- **Fixer**: `FIXER_ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Bash", "Edit", "Write"]`
  (`packages/foreman/src/foreman/roles/fixer.py:94`)

The SDK distinguishes `tools` from `allowed_tools` explicitly in the
`ClaudeAgentOptions` docstring:

```python
# .venv/lib/python3.12/site-packages/claude_agent_sdk/types.py:1582-1603
tools: list[str] | ToolsPreset | None = None
"""Specify the base set of available built-in tools.

- ``list[str]`` — Specific tool names (e.g. ``["Bash", "Read", "Edit"]``).
- ``[]`` (empty list) — Disable all built-in tools.
- ``{"type": "preset", "preset": "claude_code"}`` — Use all default Claude Code tools.

To restrict which tools the model may call without being prompted, use
``allowed_tools`` instead.
"""

allowed_tools: list[str] = field(default_factory=list)
"""Tool names that are auto-allowed without prompting for permission.

These tools execute automatically without asking the user for approval.
To restrict which tools are available at all, use ``tools``.
"""
```

The two parameters do different jobs. `tools` is the *base set of
available built-in tools*; `allowed_tools` is the *subset of those
that execute without a permission prompt*. Because Foreman's adapter
never sets `tools`, the SDK falls back to its default — the full
Claude Code tool surface, which includes the **Task** tool (the
subagent-dispatch primitive). The four narrow `*_ALLOWED_TOOLS`
lists only narrow the *auto-allowed* surface; they do **not** narrow
the *available* surface. Task is therefore reachable to every role
today; the LLM can call it, and since the adapter runs with
`permission_mode="acceptEdits"` and the daemon supplies no human at
the console, the SDK's permission gate is the only thing standing
between an LLM-initiated Task call and an actual subagent dispatch.

Net: the audit's reachability question collapses into the question
of whether the SDK auto-allows or prompts for Task. Empirically, the
Worker's pre-push hook recovery path at `worker.py:_verify_impl_branch_remote_state`
comments mention "the Claude SDK subagent" (`worker.py:343`), which
is consistent with Task being not just reachable but in active use
in production.

## Prompt audit

A walk of every markdown file under
`packages/foreman/src/foreman/prompts/` plus the Python loader at
`prompts/__init__.py` surfaces every string that explicitly tells
the LLM to dispatch a subagent, use the Task tool, or invoke
`subagent-driven-development`. Hits live in three of the eight
vendored superpowers skills and zero of the six role-specific
prompt files (planner.md, worker.md, reviewer.md, reviewer_impl.md,
fixer.md, fixer_impl.md) — confirmed via
`grep -n -i "subagent|Task tool|dispatch|parallel|fresh subagent"`
across all of them.

### `superpowers/writing-plans.md`

**Line 52** (plan-document header that every Planner plan must
include):

> > **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

Applies to: **Planner** (writing-plans is the Planner's only
vendored superpower).

**Line 124** (Self-Review section):

> After writing the complete plan, look at the spec with fresh eyes
> and check the plan against it. This is a checklist you run
> yourself — not a subagent dispatch.

The phrasing is anti-subagent here, but the line still mentions
"subagent dispatch" as a contrast, normalising the idea that
dispatching one is a thing the LLM might otherwise do. Applies to:
**Planner**.

**Line 140** (Execution Handoff offer):

> **1. Subagent-Driven (recommended)** - I dispatch a fresh subagent
> per task, review between tasks, fast iteration

Applies to: **Planner**.

**Lines 147-148** (the chosen-Subagent-Driven branch):

> **If Subagent-Driven chosen:**
> - **REQUIRED SUB-SKILL:** Use superpowers:subagent-driven-development
> - Fresh subagent per task + two-stage review

Applies to: **Planner**.

### `superpowers/executing-plans.md`

**Line 14** (top-of-skill Note):

> **Note:** Tell your human partner that Superpowers works much
> better with access to subagents. The quality of its work will be
> significantly higher if run on a platform with subagent support
> (such as Claude Code or Codex). If subagents are available, use
> superpowers:subagent-driven-development instead of this skill.

Applies to: **Worker** (executing-plans is in the Worker's vendored
superpowers list at `worker.py:_load_worker_prompt`, lines 167-174).

### `superpowers/requesting-code-review.md`

**Line 8** (Overview opener):

> Dispatch a code reviewer subagent to catch issues before they
> cascade. The reviewer gets precisely crafted context for
> evaluation — never your session's history. This keeps the
> reviewer focused on the work product, not your thought process,
> and preserves your own context for continued work.

Applies to: **Reviewer (spec_pr)**, **Reviewer (impl_pr)** —
requesting-code-review is the only vendored superpower for the
spec-side Reviewer (`reviewer.py:_REVIEWER_SUPERPOWERS_BY_TARGET`,
lines 85-98) and is also included in the impl-side composition.

**Line 15** (When to Request Review, mandatory list):

> - After each task in subagent-driven development

Applies to: **Reviewer (spec_pr)**, **Reviewer (impl_pr)**.

**Line 32** (How to Request, step 2 heading):

> **2. Dispatch code reviewer subagent:**

Applies to: **Reviewer (spec_pr)**, **Reviewer (impl_pr)**.

**Line 34** (the literal Task-tool instruction):

> Use Task tool with `general-purpose` type, fill template at
> `code-reviewer.md`

Applies to: **Reviewer (spec_pr)**, **Reviewer (impl_pr)**.

**Line 58** (example block):

> [Dispatch code reviewer subagent]

Applies to: **Reviewer (spec_pr)**, **Reviewer (impl_pr)**.

### Other prompt files

A grep across the remaining vendored skills
(`test-driven-development.md`, `verification-before-completion.md`,
`finishing-a-development-branch.md`, `receiving-code-review.md`)
and the six role-specific prompt files (`planner.md`, `worker.md`,
`reviewer.md`, `reviewer_impl.md`, `fixer.md`, `fixer_impl.md`)
finds **zero** additional subagent-encouraging strings. The only
"dispatch" hit in `worker.md` is line 55 — a discussion of LLM
dispatch cycles in the `<library_research>` block, unrelated to
the Task tool. The audit therefore reaches every role through the
composed-prompt graph:

| Role | Vendored superpowers (per `roles/*.py`) | Subagent-encouraging hits |
| --- | --- | --- |
| Planner | `["writing-plans"]` | writing-plans:52,124,140,147-148 |
| Worker | `["test-driven-development", "executing-plans", "verification-before-completion", "finishing-a-development-branch"]` | executing-plans:14 |
| Reviewer (spec_pr) | `["requesting-code-review"]` | requesting-code-review:8,15,32,34,58 |
| Reviewer (impl_pr) | `["requesting-code-review", "verification-before-completion", "test-driven-development"]` | requesting-code-review:8,15,32,34,58 |
| Fixer (spec_pr) | `["receiving-code-review"]` | none |
| Fixer (impl_pr) | `["receiving-code-review", "verification-before-completion", "test-driven-development"]` | none |

Three of the four roles see at least one explicit subagent
instruction in their composed prompt; the Fixer is the lone
exception, and that's a function of the vendored skill it pulls
(`receiving-code-review`), not anything the adapter does.

## Adapter preamble gap

The adapter preamble at
`packages/foreman/src/foreman/prompts/__init__.py:42-95` is the
one place where Foreman patches interactive-Claude-Code assumptions
in the vendored skills before the role sees them. It currently reads:

```
# Foreman role adapter (read first)

You are an SDK-driven Foreman role agent — not interactive Claude
Code. The discipline patterns below were originally written for
interactive Claude Code; this preamble tells you how to interpret
them in your environment.

## Tools you have

- File ops: `Read`, `Glob`, `Grep`
- Mutation (some roles only): `Edit`, `Write`, `Bash`
- That is the entire tool surface.

## Things the discipline patterns mention that you DO NOT have

- **No `Skill` tool.** Cross-references that say "REQUIRED SUB-SKILL:
  Use superpowers:X" or "invoke the X skill" are informational only.
  …
- **No `TodoWrite` / `TaskCreate` tool.** When a skill says "create
  todos" or "track in TodoWrite", treat that as "decompose into a
  numbered checklist in your structured output". Your output schema
  is the audit trail.
- **No working directory ownership.** …
- **No conversation with a human.** …

## Order of authority

When the inlined discipline conflicts with the role contract below,
**the role contract wins.** …
```

The preamble closes three interactive-Claude-Code affordances
(Skill, TodoWrite, TaskCreate) and three operational assumptions
(working directory, human conversation, label-vocabulary precedence).
It says **nothing** about the Task tool or subagent dispatch — and
that omission lines up exactly with the prompt-audit hits above:
the LLM reads "REQUIRED SUB-SKILL: Use
superpowers:subagent-driven-development" in writing-plans.md, sees
no preamble note overriding it, and is left to its own judgement.
The preamble's pattern ("No `Skill` tool" / "No `TodoWrite` /
`TaskCreate` tool") is the natural shape a Task-tool clause would
take — but no such clause exists. Closing that gap is the most
direct prompt-side intervention the follow-up tickets can make.

## Tracing approach

The issue's "instrument or replay a few historical role runs and
count actual subagent dispatches per role per ticket" deliverable
cannot be satisfied from existing artifacts. The `/foreman/logs/`
tree contains daemon stderr, not LLM transcripts.

A sample from the worker log directory illustrates: the file header
of `/foreman/logs/worker/171__2026-06-07T00-40-46-941684Z.log`
begins:

```
[foreman.worktree] uv sync: Resolved 64 packages in 9ms
worker crashed before outcome; reverted entry labels for issue=171 (added back: ['foreman:plan-approved'], removed: ['foreman:impl-attempt-1'])
Traceback (most recent call last):
```

That is Python `print()` and `logging.warning` output from the
daemon-side orchestration (the `[foreman.worktree]` prefix is from
`worktree.py:_maybe_sync_worktree_deps`, line 777; the worker-crashed
line is the `_log.warning` in `worker.py:1072-1077`). There is no
record of which tools the LLM called, how many turns the session
took, or whether the Task tool fired. Historical Task dispatches
cannot be recovered post-hoc from this surface.

The principled instrumentation point lives in the claude-agent-sdk
hook taxonomy:

```python
# .venv/lib/python3.12/site-packages/claude_agent_sdk/types.py:283-314
class _SubagentContextMixin(TypedDict, total=False):
    """Optional sub-agent attribution fields for tool-lifecycle hooks.

    agent_id: Sub-agent identifier. Present only when the hook fires from
    inside a Task-spawned sub-agent; absent on the main thread. Matches the
    agent_id emitted by that sub-agent's SubagentStart/SubagentStop hooks.
    When multiple sub-agents run in parallel their tool-lifecycle hooks
    interleave over the same control channel — this is the only reliable
    way to attribute each one to the correct sub-agent.

    agent_type: Agent type name (e.g. "general-purpose", "code-reviewer").
    Present inside a sub-agent (alongside agent_id), or on the main thread
    of a session started with --agent (without agent_id).
    """
    agent_id: str
    agent_type: str


class PreToolUseHookInput(BaseHookInput, _SubagentContextMixin):
    """Input data for PreToolUse hook events."""
    hook_event_name: Literal["PreToolUse"]
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str
```

A `PreToolUseHookInput` that fires with `tool_name == "Task"` is the
explicit cue a subagent is being launched; the `agent_id` /
`agent_type` fields on `_SubagentContextMixin` are how
subsequent tool-lifecycle hooks attribute work back to that subagent.
Wiring these hooks through `AnthropicSDKProvider.run_agent` — adding
a `hooks=...` kwarg to `ClaudeAgentOptions` and persisting the
captured tool-use envelope to a per-dispatch JSONL — is the
contract-shaped change that would turn the LLM's tool-call surface
into something the audit can actually replay. That instrumentation
is intentionally deferred to a follow-up ticket (see "Recommended
follow-up tickets" below); without it, every classification in the
next section is estimate, not measurement.

## Classification + estimated impact

Estimates below classify the **likely** subagent-dispatch pattern
per role using the rubric the issue defines: `justified` (parallel
work or distinct-context sub-task), `reflexive` (would have been
just as cheap to inline), `counterproductive` (added latency
without isolating anything useful). Baseline timings from the
issue body — Planner ~3 min, Reviewer ~3 min, Worker ~14 min,
Reviewer-impl ~3 min for issue #156 — anchor the latency
estimates. Round-trip cost per Task dispatch is on the order of
~30 s of pure SDK overhead plus the subagent's own LLM time
(~60-180 s for a small subagent run), so a single avoided
dispatch saves ~1.5-3.5 min in the small-subagent case.

### Planner

- **Audit hits applicable**: writing-plans:52, 124, 140, 147-148
  (the entire Execution Handoff section pushes the Planner toward
  "I dispatch a fresh subagent per task").
- **`*_ALLOWED_TOOLS`**: `["Read", "Glob", "Grep"]` (`planner.py:56`).
  The Planner LLM is read-only; the only mutation surface is its
  structured output (the spec doc). A subagent dispatched from the
  Planner has the same read-only surface as the Planner itself, so
  it can't be doing exclusive work the Planner couldn't do inline.
- **Classification**: **reflexive**. The Execution Handoff section
  in writing-plans is a Planner→Worker handoff pattern from
  interactive Claude Code where the same session implements after
  planning. In Foreman, the Planner exits after producing
  `PlannerOutput` (see `planner.py:202-208`); there IS no
  "implement-now-or-later" choice to offer. Any subagent dispatched
  to "implement the plan task-by-task" is doing work that no Foreman
  pipeline stage reads.
- **Estimated savings**: floor ~0 min (Planner may already ignore
  the Execution Handoff because its role contract says "the
  PlannerOutput is your deliverable"), ceiling ~3-5 min if the
  Planner is reflexively dispatching one or more "planning-helper"
  subagents per run. Confidence: **low** — without tracing this
  could just as easily be a no-op.

### Worker

- **Audit hits applicable**: executing-plans:14 ("If subagents are
  available, use superpowers:subagent-driven-development instead of
  this skill"). The Worker's prompt composition at
  `worker.py:_load_worker_prompt` lines 167-174 puts executing-plans
  second, so the Note is one of the first instructions the Worker
  reads after the adapter preamble.
- **`*_ALLOWED_TOOLS`**: `["Read", "Grep", "Glob", "Bash", "Edit", "Write"]`
  (`worker.py:105`). The Worker's worktree-local work — read spec,
  edit code, run check_command — is fundamentally serial inside one
  branch. Parallel subagents editing the same worktree contend on
  the same files.
- **Classification**: **reflexive to counterproductive**. The
  Worker contract at `worker.md:<per_sub_request_loop>` says walk
  the spec's Sub-requests in topological order — a sequential walk
  that doesn't benefit from parallelism. A subagent dispatched to
  "implement sub-request 3 while I work on sub-request 4" would
  interleave commits and race the check_command run. The
  `verify_impl_branch_remote_state` comment at `worker.py:343` ("The
  Worker delegates `git push` to the Claude SDK subagent via Bash")
  suggests at least the push step routes through a subagent that
  doesn't need to.
- **Estimated savings**: floor ~2 min (eliminate one reflexive
  subagent dispatch per Worker run), ceiling ~8-10 min (if the
  Worker is dispatching one subagent per sub-request, and a typical
  spec has 4-6 sub-requests, removing them all shrinks the ~14 min
  Worker baseline materially). Confidence: **medium** — the
  reflexive pattern is consistent with both the prompt cue and the
  observed worker.py comments, but the dispatch count remains
  uncounted.

### Reviewer (spec_pr)

- **Audit hits applicable**: requesting-code-review:8, 15, 32, 34,
  58 — the entire skill IS subagent-dispatch instructions. Line 34
  is the most explicit: "Use Task tool with `general-purpose` type,
  fill template at `code-reviewer.md`".
- **`*_ALLOWED_TOOLS`**: `["Read", "Grep", "Glob", "Bash"]`
  (`reviewer.py:60`). The Reviewer's job is to read the spec PR and
  return structured findings; it does no mutation. A "fresh
  subagent for code review" inside an already-fresh Reviewer
  subprocess is a doubled context window with nothing to isolate.
- **Classification**: **counterproductive**. The Reviewer
  subprocess IS the code review; spawning a code-review subagent
  from inside it is essentially `for _ in range(2): review()`. Each
  dispatch round-trip adds latency without producing review
  findings the parent reviewer couldn't have produced inline. This
  is the strongest case in the audit for a prompt patch.
- **Estimated savings**: floor ~1.5 min (one avoided dispatch
  saves the round-trip + subagent LLM time), ceiling ~2.5 min
  (close to the entire ~3 min Reviewer baseline). Confidence:
  **medium-high** — the prompt cue is unambiguous; the only
  uncertainty is whether the SDK's permission gate is silently
  denying the dispatch in production (which the tracing follow-up
  would resolve).

### Reviewer (impl_pr)

- **Audit hits applicable**: requesting-code-review:8, 15, 32, 34,
  58 — same hits as spec_pr Reviewer since the same skill is in
  the composition list (`reviewer.py:_REVIEWER_SUPERPOWERS_BY_TARGET`
  lines 93-97).
- **`*_ALLOWED_TOOLS`**: identical to spec_pr Reviewer.
- **Classification**: **counterproductive**, same reasoning as
  spec_pr. Adding `verification-before-completion` and
  `test-driven-development` to the impl_pr composition doesn't
  add new subagent cues (those two skills had zero hits).
- **Estimated savings**: floor ~1.5 min, ceiling ~2.5 min.
  Confidence: **medium-high**.

### Fixer (spec_pr)

- **Audit hits applicable**: **none**. The Fixer's only vendored
  skill (`receiving-code-review`) contains zero subagent-encouraging
  strings.
- **`*_ALLOWED_TOOLS`**: `["Read", "Grep", "Glob", "Bash", "Edit", "Write"]`
  (`fixer.py:94`). Mutation surface, same as Worker.
- **Classification**: **justified or absent**. With no prompt cue
  pushing the Fixer toward Task, any dispatch that happens is the
  LLM's own initiative, and is most likely to be a "spawn a helper
  to grep for X" pattern that would have been just as cheap to do
  inline.
- **Estimated savings**: floor ~0 min (the Fixer may already not
  be dispatching), ceiling ~1 min. Confidence: **low** — the
  estimate is bounded only by the lack of prompt encouragement.

### Fixer (impl_pr)

- **Audit hits applicable**: **none**. The added skills
  (`verification-before-completion`, `test-driven-development`)
  contain zero hits.
- **Classification**: same as Fixer (spec_pr) — **justified or
  absent**.
- **Estimated savings**: floor ~0 min, ceiling ~1 min. Confidence:
  **low**.

### Roll-up

| Role | Class | Floor / ceiling | Confidence |
| --- | --- | --- | --- |
| Planner | reflexive | ~0 / ~5 min | low |
| Worker | reflexive→counterproductive | ~2 / ~10 min | medium |
| Reviewer (spec_pr) | counterproductive | ~1.5 / ~2.5 min | medium-high |
| Reviewer (impl_pr) | counterproductive | ~1.5 / ~2.5 min | medium-high |
| Fixer (spec_pr) | justified or absent | ~0 / ~1 min | low |
| Fixer (impl_pr) | justified or absent | ~0 / ~1 min | low |

If the issue #156 pipeline runs each role once (no impl Fixer in
that ticket), the rough end-to-end savings are floor ~5 min,
ceiling ~21 min — against the issue's stated 40 min baseline.
**These numbers are estimates, not measurements.** The Reviewer
findings are the highest-leverage and highest-confidence targets;
everything else needs the tracing follow-up to firm up.

## Adjacent speedup vectors

### 1. Polling latency

The reconciler dispatches the next role only on the next tick:

```python
# packages/foreman/src/foreman/reconciler/daemon.py:132
poll_interval_seconds: int = 60,
```

(Default 60 s; configurable per `config.py:127-130`.) For an 8-step
pipeline, the worst case is 8 ticks × 60 s = 8 min of pure poll
delay. Likely savings: ~3-7 min per end-to-end ticket if the role
exit signals the next dispatch directly (e.g., via a sentinel file
or a bus envelope the reconciler watches on a tighter interval).
**Follow-up ticket title**: `feat(reconciler): post-dispatch
fast-path that skips the next poll-interval wait`.

### 2. Model choice

The provider adapter never sets `ClaudeAgentOptions.model` (the
field's docstring at `claude_agent_sdk/types.py:1673-1677` —
"Defaults to the CLI default model" — applies). All four roles
therefore run on the same default model, including Reviewer which
does no mutation and could plausibly run on a smaller / faster
model. Likely savings: ~1-3 min per Reviewer run if a faster model
satisfies the structured-output schema reliably (uncertain — needs
a side-by-side accuracy check). **Follow-up ticket title**:
`feat(provider): plumb per-role model selection through
ClaudeAgentOptions`.

### 3. Test re-runs

The Worker's verification path runs `check_command` twice — once
to capture baseline failures pre-LLM, once post-LLM — at
`worker.py:_run_check_command` (lines 182-226). The orchestrator
then re-runs it a third time as ground truth (`worker.py:839-841`).
CI runs the same suite again on push. Likely savings: ~1-3 min per
Worker run if the orchestrator-side re-run is skipped when the
Worker's post-run already passed cleanly (the override rule at
`worker.py:850-860` is the only thing that needs the independent
check). **Follow-up ticket title**: `refactor(worker): skip
orchestrator-side check_command re-run when LLM-side run passed
cleanly`.

### 4. `uv sync` at dispatch time

`WorktreeManager` runs `uv sync --all-packages` on every dispatch
via `worktree.py:_maybe_sync_worktree_deps` (lines 746-786). The
log header `[foreman.worktree] uv sync: Resolved 64 packages in 9ms`
(seen in `/foreman/logs/worker/171__*.log`) shows that resolution
itself is fast, but a cold venv install (first dispatch on a new
worktree) is slower. Likely savings: <1 min per dispatch when the
venv is warm; ~3-5 min on the first dispatch per project per day
if a shared global cache pre-warms the venv. **Follow-up ticket
title**: `perf(worktree): cache uv sync result across dispatches
for the same project`.

### 5. Worker write-then-fix loops

`worker.md` already discourages "debug spirals" via the
`<check_failure_handling>` block (one retry, then surface
incomplete). The actual loop happens inside the LLM session and is
not visible to the daemon — same blind spot as the subagent
tracing question. Likely savings: ~2-5 min per Worker run that's
caught in a 2-3 cycle test-fail-fix loop, but this needs the same
PreToolUse hook instrumentation to see. **Follow-up ticket title**:
`feat(observability): record per-role tool-call counts and total
session turns to detect write-then-fix loops empirically`.

## Recommended follow-up tickets

Each title is a Conventional Commit shape suitable for pasting
into the GitHub new-issue form, followed by a 1-2 sentence body
the issue can lead with.

- **`docs(prompts): close adapter-preamble gap — forbid the Task
  tool / subagent dispatch in role subprocesses`**
  Extend `packages/foreman/src/foreman/prompts/__init__.py:42-95`
  with a "No Task tool / no subagent dispatch" clause matching the
  pattern of the existing "No `Skill` tool" and "No `TodoWrite` /
  `TaskCreate` tool" sections. The clause should reframe the
  writing-plans / executing-plans / requesting-code-review
  subagent prose as informational, the way the Skill clause does.

- **`feat(provider): pass `tools=[]` or a narrow `tools=[...]` list
  through `AnthropicSDKProvider.run_agent` to remove Task from the
  base tool surface`**
  Today `tools=...` is never set (see
  `anthropic_sdk.py:87-94`), so the SDK exposes the full Claude
  Code base tool set including Task. Plumb a per-role `tools`
  parameter through and have each role pass exactly its
  `*_ALLOWED_TOOLS` list (or a slightly broader set that still
  excludes Task) to hard-cap subagent reachability at the SDK
  surface.

- **`feat(observability): record subagent dispatches per role-run
  via PreToolUse / SubagentStart hooks`**
  Wire claude-agent-sdk hook callbacks (see
  `claude_agent_sdk/types.py:283-314` —
  `PreToolUseHookInput.tool_name == "Task"`,
  `_SubagentContextMixin.agent_id`) through
  `AnthropicSDKProvider.run_agent`, persist tool-call envelopes to
  a per-dispatch JSONL alongside the existing
  `~/.foreman/stats/<owner>__<repo>/worker.jsonl`, and surface a
  subagent-count column on the stats row. This is the
  measurement-not-estimate version of this report.

- **`feat(reconciler): post-dispatch fast-path that skips the next
  poll-interval wait`** — see "Adjacent speedup vectors, 1".

- **`feat(provider): plumb per-role model selection through
  ClaudeAgentOptions`** — see "Adjacent speedup vectors, 2".

- **`refactor(worker): skip orchestrator-side check_command re-run
  when LLM-side run passed cleanly`** — see "Adjacent speedup
  vectors, 3".

- **`perf(worktree): cache uv sync result across dispatches for the
  same project`** — see "Adjacent speedup vectors, 4".

## Summary

The audit confirms Jeff's hypothesis: the Task tool IS reachable to
every Foreman role today (the adapter sets `allowed_tools=...` but
never `tools=...`, leaving the SDK's default Claude Code base tool
set in place, see `anthropic_sdk.py:84-97` vs
`claude_agent_sdk/types.py:1582-1603`). Subagent-encouraging prose
appears in three of the eight vendored superpowers skills —
`writing-plans` (Planner), `executing-plans` (Worker), and
`requesting-code-review` (both Reviewer variants) — and the adapter
preamble at `prompts/__init__.py:42-95` patches Skill / TodoWrite /
TaskCreate but has no Task-tool clause. The Reviewer is the
highest-leverage and highest-confidence target: the entire
`requesting-code-review` skill IS Task-dispatch instructions, the
Reviewer subprocess IS the code review, so any dispatch from inside
it is counterproductive (estimated ~1.5-2.5 min saved per Reviewer
run, ~3-5 min across both Reviewer variants in a pipeline).
**Highest-priority follow-up: `docs(prompts): close adapter-preamble
gap — forbid the Task tool / subagent dispatch in role subprocesses`**.
Empirical confirmation needs the PreToolUse-hook instrumentation
ticket; the historical-log replay path the issue suggests cannot be
taken — `/foreman/logs/<role>/*.log` is daemon stderr, not LLM
transcripts.

🤖 foreman-worker-bot
