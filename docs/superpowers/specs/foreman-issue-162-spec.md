# Spec: investigate foreman role-runtime speedup — subagent audit + adjacent vectors (issue #162)

## Goal

Produce a single investigation report committed at
`docs/superpowers/investigations/foreman-issue-162-role-runtime-speedup.md`
that audits Jeff's hypothesis — that Foreman's role subprocesses are
dispatching subagents (claude-agent-sdk's Task tool) when they don't
need to — and triages the adjacent speedup vectors the issue lists.
This is an INVESTIGATION ticket: the deliverable is a markdown report
plus a list of recommended follow-up tickets, NOT prompt or code
changes. See [#162](https://github.com/jeffrichley/foreman/issues/162).

## Acceptance criteria

- A new file exists at
  `docs/superpowers/investigations/foreman-issue-162-role-runtime-speedup.md`
  with the section structure described under `## Approach` below. The
  directory `docs/superpowers/investigations/` may be new; create it as
  part of the change.
- The report's **Subagent availability** section answers, with cited
  evidence (file path + line number), whether the Task tool is reachable
  by each role's LLM today. Specifically, the report must:
  - Note that all four roles call `provider.run_agent(...)` with
    `allowed_tools=[...]` but never set the SDK's `tools=...` parameter
    (cite `packages/foreman/src/foreman/providers/anthropic_sdk.py:84-97`
    and `packages/foreman/src/foreman/roles/{planner,worker,reviewer,fixer}.py`
    for each role's `*_ALLOWED_TOOLS` constant).
  - Quote the SDK's documented semantics for `tools` vs `allowed_tools`
    from `claude_agent_sdk/types.py` (around lines 1582-1603 — the
    docstring distinguishes "base set of available built-in tools" from
    "tool names auto-allowed without prompting"). The report must state
    explicitly whether Task is in the default Claude-Code tool set and
    therefore reachable to each role.
- The report's **Prompt audit** section walks every file under
  `packages/foreman/src/foreman/prompts/` (both the role contracts and
  every vendored superpowers skill under
  `packages/foreman/src/foreman/prompts/superpowers/`) and lists every
  string that explicitly tells the LLM to dispatch a subagent, use the
  Task tool, or invoke `subagent-driven-development`. For each hit,
  cite `<file>:<line>` and quote the exact instruction. At minimum the
  audit must surface the hits already known from
  `prompts/superpowers/writing-plans.md` (lines 52, 124, 140, 147-148),
  `prompts/superpowers/executing-plans.md` (line 14), and
  `prompts/superpowers/requesting-code-review.md` (lines 8, 15, 32, 34,
  58); the report must explicitly state whether any other prompt file
  contains additional encouragement.
- The report's **Adapter preamble gap** section quotes the existing
  preamble at `packages/foreman/src/foreman/prompts/__init__.py:42-95`
  and observes (one paragraph) that it tells the LLM not to call the
  Skill tool and not to call `TodoWrite`/`TaskCreate`, but says nothing
  about the Task tool / subagent dispatch — and pairs that observation
  with the prompt-audit hits above.
- The report's **Tracing approach** section names what tracing IS and
  IS NOT possible today, with citations:
  - States that `/foreman/logs/{role}/<issue>__*.log` files are daemon
    stderr (cite a sample log header such as
    `/foreman/logs/worker/171__2026-06-07T00-40-46-941684Z.log`), NOT
    LLM transcripts, so historical Task-tool dispatches cannot be
    replayed from logs.
  - Identifies the claude-agent-sdk hook taxonomy
    (`PreToolUseHookInput` + `_SubagentContextMixin` at
    `claude_agent_sdk/types.py:283-314`) as the principled
    instrumentation point and states (one sentence) that exposing
    those hooks via the provider facade is the
    follow-up-ticket-shaped change that would enable empirical tracing.
- The report's **Classification + estimated impact** section is
  honest about its evidence base: in the absence of live tracing it
  may produce only *estimates*, not measurements. For each role
  (Planner, Worker, Reviewer, Fixer) it must:
  - Cite the role's `*_ALLOWED_TOOLS` constant and prompt-audit hits
    that apply to it.
  - Classify the likely subagent-dispatch pattern as `justified`,
    `reflexive`, or `counterproductive` per the issue's rubric, with
    one paragraph of reasoning.
  - State an estimated latency floor + ceiling saved if reflexive +
    counterproductive dispatches were eliminated, with the reasoning
    behind each number. Round-trip cost numbers should reference the
    baseline timings the issue body provides (Planner ~3 min, Reviewer
    ~3 min, Worker ~14 min, Reviewer-impl ~3 min for #156).
  - Flag confidence as `low` for any role whose impact estimate cannot
    be defended without live tracing.
- The report's **Adjacent speedup vectors** section produces a
  one-paragraph triage for each of the five vectors the issue lists
  (polling latency, model choice, test re-runs, uv sync at dispatch
  time, Worker write-then-fix loops). Each triage names:
  - The relevant code path (cite file + line) — e.g., the reconciler's
    poll interval, `worker.py:_run_check_command`, `WorktreeManager.create`
    + its `uv sync` call, etc.
  - Rough size estimate of the potential win (`<1 min`, `~1-3 min`,
    `~3-10 min` per role run), with reasoning.
  - A one-line "follow-up ticket title" worded as a Conventional Commit
    that a human can copy-paste to open the next ticket.
- The report's **Recommended follow-up tickets** section lists
  Conventional-Commit-shaped titles for each follow-up the
  investigation surfaces. At minimum (if the subagent audit surfaces
  any reflexive/counterproductive dispatch pattern) it must include:
  - One title for "discourage subagent use in role prompts" (or
    equivalent).
  - One title for "hard-cap subagent dispatches per role".
  - One title for "log subagent dispatches per role-run for empirical
    tracking".
  Each ticket title is followed by a 1-2 sentence body suitable for
  pasting into the GitHub new-issue form. If the audit surfaces no
  meaningful waste, this section may instead state "no follow-up
  tickets recommended" with a one-paragraph rationale.
- The report is committed in a single Worker commit titled
  `docs(investigation): audit role-runtime subagent use and speedup vectors per issue #162`.
  No other files are modified. The Worker MUST NOT edit any file
  under `packages/foreman/src/foreman/prompts/` or
  `packages/foreman/src/foreman/roles/` — touching those is what the
  recommended follow-up tickets are for.
- `just check` exits zero. The investigation report is markdown only;
  the quality gate should run unchanged.

## Approach

This ticket explicitly inverts Foreman's usual rhythm: the Worker is
producing a *report*, not a code change. The Reviewer's job will be
to check the report's empirical claims (paths, line numbers, quoted
text) line by line — so the spec is structured to make every claim
in the report verifiable from a citation already grounded in the
worktree.

The investigation's three pillars are baked into the section list
above:

1. **Static audit.** Read what's actually in the prompts and role
   modules today. The vendored superpowers skills (under
   `packages/foreman/src/foreman/prompts/superpowers/`) were
   originally written for interactive Claude Code, where subagent
   dispatch is load-bearing for context isolation; the adapter
   preamble at `packages/foreman/src/foreman/prompts/__init__.py:42-95`
   patches a few interactive-Claude-Code assumptions (no Skill tool,
   no TodoWrite) but never addresses the Task tool. The audit must
   surface every instance of subagent-encouraging prose so the
   follow-up ticket knows what to edit.

2. **Reachability check.** Read the provider adapter and SDK source
   to determine whether the Task tool is reachable to each role.
   `AnthropicSDKProvider.run_agent` at
   `packages/foreman/src/foreman/providers/anthropic_sdk.py:73-97`
   sets only `allowed_tools=...`, not the SDK's `tools=...`
   parameter; per `claude_agent_sdk/types.py:1582-1603`, that
   means the default Claude-Code base tool set (which includes
   Task) is exposed and only auto-approval is narrowed. The
   report must state this fact and cite it; the Reviewer can
   then verify it without running anything.

3. **Tracing inventory.** Identify what tracing data exists vs.
   what would need to be added. The `/foreman/logs/{role}/`
   directory contains daemon stderr (sample headers visible in
   any file from `/foreman/logs/worker/`), NOT LLM transcripts —
   so the issue's "instrument or replay a few historical role
   runs and count actual subagent dispatches" deliverable
   cannot be satisfied from existing artifacts. The honest move
   is to flag this in the report and name
   `claude_agent_sdk.types._SubagentContextMixin` /
   `PreToolUseHookInput` (at
   `claude_agent_sdk/types.py:283-314`) as the principled
   instrumentation point for the follow-up.

The classification step (`justified` vs `reflexive` vs
`counterproductive`) then becomes an evidence-bounded estimate per
role, anchored to (a) which prompt-audit hits apply to that role's
composed prompt and (b) what work that role does inline today.
The report must be explicit when an estimate cannot be defended
without live tracing — that honesty is the point of the
`confidence: low` flag in the classification table.

The adjacent-speedup-vectors triage is shorter by design. Each
vector gets one paragraph: cite the relevant code path, estimate the
win, propose the follow-up ticket title. The issue body provides the
baseline numbers (`Planner ~3 min, Reviewer ~3 min, Worker ~14 min,
Reviewer-impl ~3 min, end-to-end ~40 min for #156`) — the report
anchors size estimates to those.

Finally, every "if we did X, we'd save Y" claim becomes a follow-up
ticket title. The investigation does NOT prescribe the fix's
implementation — that's a separate spec's job. The investigation's
job is to deliver a prioritized, citation-backed punch list a human
can act on.

## Sub-requests (topologically sorted)

1. Create the directory `docs/superpowers/investigations/` (no-op
   `git add` semantics — directories are tracked through their files;
   creating the report file in step 2 implicitly creates the dir).
2. Create the file
   `docs/superpowers/investigations/foreman-issue-162-role-runtime-speedup.md`
   with these top-level sections (in this order):
   - `# Foreman role-runtime speedup investigation (issue #162)`
   - `## Subagent availability` — answers "is the Task tool reachable
     by each role today?" Cite
     `packages/foreman/src/foreman/providers/anthropic_sdk.py:84-97`
     and each role's `*_ALLOWED_TOOLS` constant
     (`planner.py:56`, `worker.py:105`, `reviewer.py:60`,
     `fixer.py:94`). Quote the SDK's `tools` vs `allowed_tools`
     docstring from `claude_agent_sdk/types.py:1582-1603`.
   - `## Prompt audit` — every hit from a walk of
     `packages/foreman/src/foreman/prompts/**/*.md`. Format:
     `<file>:<line>` + a quoted block of the offending text +
     one-sentence "applies to which roles." Must include the known
     hits in `writing-plans.md` (lines 52, 124, 140, 147-148),
     `executing-plans.md` (line 14),
     `requesting-code-review.md` (lines 8, 15, 32, 34, 58). State
     explicitly whether any OTHER prompt file contains additional
     subagent-encouraging content.
   - `## Adapter preamble gap` — quote
     `packages/foreman/src/foreman/prompts/__init__.py:42-95`,
     observe that the preamble blocks Skill /
     TodoWrite / TaskCreate but says nothing about the Task tool
     / subagent dispatch.
   - `## Tracing approach` — distinguish what's available
     (daemon stderr at `/foreman/logs/{role}/*.log`, cite the
     header of any sample file) from what's NOT available (LLM
     transcripts with tool-call records). Name
     `_SubagentContextMixin` and `PreToolUseHookInput` at
     `claude_agent_sdk/types.py:283-314` as the
     principled instrumentation point.
   - `## Classification + estimated impact` — per role
     (Planner, Worker, Reviewer-on-spec, Reviewer-on-impl,
     Fixer-on-spec, Fixer-on-impl), a short paragraph with:
     (a) audit hits applicable to this role's composed
     prompt, (b) classification (`justified` /
     `reflexive` / `counterproductive`) with reasoning,
     (c) latency-save estimate floor + ceiling tied to the
     issue's baseline timings, (d) confidence
     (`high` / `medium` / `low`).
   - `## Adjacent speedup vectors` — one paragraph per
     vector for the five vectors listed in the issue body
     (polling latency, model choice, test re-runs, uv sync
     at dispatch, Worker write-then-fix loops). Each
     paragraph names a code path with citation, a rough
     size estimate, and a follow-up-ticket title.
   - `## Recommended follow-up tickets` — bulleted list of
     Conventional-Commit-shaped titles, each with a 1-2
     sentence body. At minimum include the three subagent-
     related tickets named in the acceptance criteria, OR
     the explicit "no follow-up tickets recommended"
     rationale.
   - `## Summary` — 3-5 sentences a busy human can read
     first: what the audit found, what the biggest estimated
     win is, what the highest-priority follow-up ticket is.
3. Stage only that single new file with `git add docs/superpowers/investigations/foreman-issue-162-role-runtime-speedup.md`.
4. Commit with title
   `docs(investigation): audit role-runtime subagent use and speedup vectors per issue #162`.
   Body bullets:
   one per top-level section landed.
5. `git push origin foreman/impl-162` to make the impl PR branch
   reflect the work.
6. Run `just check`. Expect pass (markdown-only change should not
   move the quality gate). Subtract the baseline failure set
   (handed in by the orchestrator) from the post-run failure set;
   confirm the new-failures set is empty.

## File-level changes

| File | Change |
| --- | --- |
| `docs/superpowers/investigations/foreman-issue-162-role-runtime-speedup.md` | New file. Investigation report with the eight required sections above. |

NO other files are modified. The Worker MUST NOT edit
`packages/foreman/src/foreman/prompts/*` or
`packages/foreman/src/foreman/roles/*` or
`packages/foreman/src/foreman/providers/anthropic_sdk.py` — every
recommended remediation is intentionally deferred to a follow-up
ticket (so a human gets to review the audit before any code or
prompt move actually lands).

## Alternatives considered

- **Ship a small prompt patch alongside the report** (e.g., extend
  the adapter preamble at
  `packages/foreman/src/foreman/prompts/__init__.py:42-95` to forbid
  the Task tool). Rejected: the issue explicitly states "INVESTIGATION
  ticket first, FIX ticket second" and that any prompt or code change
  should fall out as a follow-up ticket. Bundling a fix into the
  investigation conflates two review decisions — does the audit's
  evidence hold? AND does the proposed prompt edit cover it? — that
  the issue author wants kept separate.
- **Build the SubagentStart/Stop hook instrumentation in this PR**
  to produce real measurements. Rejected: the issue's "instrument or
  replay a few historical role runs" line is best read as a
  stretch goal, not the deliverable. The historical logs are stderr,
  not transcripts; making tracing real requires wiring hooks through
  `AnthropicSDKProvider.run_agent`, which is its own contract
  change. Naming that as the follow-up keeps the investigation
  cheap; the report still names the principled instrumentation
  point so the next ticket has a head start.
- **Skip the adjacent-speedup-vectors triage** and produce a
  subagent-only audit. Rejected: the issue lists five other vectors
  explicitly ("Other speedup vectors to consider while we're here")
  and the win from `uv sync` caching or polling latency may dominate
  the win from removing reflexive subagent dispatches. A
  one-paragraph triage per vector costs ~5 minutes of report-writing
  and converts each into a follow-up-ticket title — high
  leverage for low cost.

## Open questions

(none — the issue's acceptance criteria are concrete enough to
report against without further input; uncertainty in the
classification + impact estimate is surfaced inside the report via
the `confidence` field rather than as an open question for the
spec.)

## Out of scope

- Editing any file under `packages/foreman/src/foreman/prompts/` —
  including the adapter preamble at `__init__.py:42-95` and any
  vendored superpowers skill. All prompt changes are deferred to
  follow-up tickets surfaced by this report.
- Editing any role module under
  `packages/foreman/src/foreman/roles/` — `*_ALLOWED_TOOLS` lists,
  the prompt composition calls, the `run_agent` invocations all
  stay as-is. Hard-capping subagent dispatches per role and
  passing `tools=[...]` to narrow the SDK base tool set are both
  follow-up-ticket work.
- Wiring `SubagentStart` / `SubagentStop` hooks through the
  provider facade. The report names these as the principled
  instrumentation point but does not implement them.
- Producing live latency measurements against historical runs.
  Estimates in the report are anchored to the issue's stated
  baseline timings; live measurement requires instrumentation
  this ticket does not deliver.
- Triaging speedup vectors beyond the five named in the issue body.
  If the report-writer surfaces additional vectors during the audit,
  they belong in `## Recommended follow-up tickets` as bare
  ticket titles, not as full triage paragraphs.
- Changing `docs/superpowers/specs/` or `docs/superpowers/plans/`
  layout. The new `docs/superpowers/investigations/` directory is the
  only structural addition.
