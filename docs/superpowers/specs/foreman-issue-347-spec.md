# Spec: add `[operator]` identities to V4Config for DCO sign-off + supervision attribution (issue #347)

## Goal
Add a top-level `[operator]` block to `V4Config` carrying TWO nested
identities — `[operator.supervisor]` (human who actively orchestrated
the run) and `[operator.signer]` (human who legally attests the DCO
sign-off) — each with required `name` + `email`. Allow per-project
override of either identity independently
(`[[projects.operator.supervisor]]` and/or `[[projects.operator.signer]]`).
Wire the resolved identities into every role-bot commit so each commit
body carries BOTH a `Supervised-by:` trailer (from supervisor) AND a
`Signed-off-by:` trailer (from signer). This is the schema + plumbing
prerequisite that makes DCO enforcement (validated as a non-blocking
CI gate in [PR #346]) mechanically possible — and the same plumbing
also lays down the new `Supervised-by:` attribution trailer foreman
introduces for AI-PR-automation. Tracks
[foreman#347](https://github.com/jeffrichley/foreman/issues/347).

**Authoritative direction.** The two-identity shape comes from
[@wrenrichley's 2026-06-19T17:17:36 comment on issue
#347](https://github.com/jeffrichley/foreman/issues/347) (headed
"**Trailer policy refinement — please factor into review.**", sub-section
"**Spec changes needed:**", stated "After discussion with Jeff"). That
comment supersedes the original issue body's flat `name + email`
framing; this spec encodes the refined design.

**Trailer policy (from the comment).** Every foreman-driven bot commit
emits up to four trailers in the message body:
- `Co-Authored-By: foreman-<role>[bot] <...>` — existing recognition
  trailer, unchanged behavior; surfaces the role bot avatar in
  GitHub's UI.
- `Co-Authored-By: <model name> <noreply@<provider>>` — existing
  model-attribution trailer, unchanged behavior.
- `Supervised-by: <name> <<email>>` — NEW trailer this spec
  introduces. Names the human who actively orchestrated this
  dispatch (managed the queue, watched transitions, made
  merge-readiness calls, did adversarial review). foreman invents
  this trailer; CONTRIBUTING.md (out of scope here) will become the
  reference doc.
- `Signed-off-by: <name> <<email>>` — legal DCO attestation. Only
  humans can sign. This is the ONLY trailer the DCO CI gate
  enforces; the other three are recommended-but-not-required per
  Jeff's direction in the comment ("we should only require the sign
  off attesting").

This spec covers wiring the new `Supervised-by:` and the existing
`Signed-off-by:` (both from new operator-identity config). The two
`Co-Authored-By:` trailers are unchanged from today's behavior — no
edits there.

## Acceptance criteria
- [ ] `V4Config` parses a top-level `[operator]` block containing two
  required nested sub-tables `[operator.supervisor]` and
  `[operator.signer]`, each with required fields `name: str` (non-empty
  after `.strip()`) and `email: str` (matches a basic RFC-5321-ish
  regex: `^[^@\s]+@[^@\s]+\.[^@\s]+$`). Missing block, missing
  sub-table, missing field, or empty `name`/`email` raises
  `pydantic.ValidationError` at `load_config()`. Same `extra="forbid"`
  discipline the rest of the schema uses. Supervisor and signer
  may legitimately resolve to the same identity (common case); the
  schema does not enforce uniqueness.
- [ ] `ProjectConfig` gains an optional
  `operator: ProjectOperatorOverride | None` field (defaults to
  `None`). `ProjectOperatorOverride` has two optional fields
  `supervisor: OperatorIdentity | None = None` and
  `signer: OperatorIdentity | None = None` — each validated with the
  same rules as the top-level identities when present. When unset
  on either field, the project inherits that identity from the
  top-level `[operator]`.
- [ ] A resolver function in `foreman.v4.config` —
  `resolve_operator(project: ProjectConfig, config: V4Config) -> OperatorConfig` —
  returns an `OperatorConfig` whose `supervisor` is
  `project.operator.supervisor` if set, else `config.operator.supervisor`,
  and whose `signer` is resolved the same way independently. Pure
  function, no I/O.
- [ ] `foreman.git_hosts.github.GitHubProvider.commit_files_to_worktree`
  accepts a new keyword-only parameter
  `provenance_trailers: list[str] | None = None`. When provided
  (non-empty list), the `git commit` invocation gains one
  `--trailer "<value>"` flag per list entry, in list order. When
  `None` or empty, behavior is unchanged (backwards-compatible
  default).
- [ ] `foreman.roles.planner._run_planner_core` resolves the operator
  via `resolve_operator(project, config)` and passes
  `provenance_trailers=[f"Supervised-by: {op.supervisor.name} <{op.supervisor.email}>", f"Signed-off-by: {op.signer.name} <{op.signer.email}>"]`
  to `host.commit_files_to_worktree`. The spec doc commit lands with
  BOTH trailers in the commit body.
- [ ] `foreman.roles.worker._run_worker_core` passes the resolved
  operator identities to the LLM subprocess via four new env vars
  (`FOREMAN_OPERATOR_SUPERVISOR_NAME`,
  `FOREMAN_OPERATOR_SUPERVISOR_EMAIL`,
  `FOREMAN_OPERATOR_SIGNER_NAME`,
  `FOREMAN_OPERATOR_SIGNER_EMAIL`) on the `provider.run_agent` `env`
  dict, AND runs a new
  `_ensure_provenance_trailers(worktree_path, operator, commits_made_count, role_token)`
  helper after the LLM returns + before the existing
  `_sanitize_head_commit_auto_close` call. The helper:
  - On `commits_made_count == 0`: no-op, returns `False`.
  - On `commits_made_count == 1`: reads HEAD's message; checks for
    BOTH a `Supervised-by: <supervisor.name> <<supervisor.email>>`
    line AND a `Signed-off-by: <signer.name> <<signer.email>>` line.
    If EITHER is missing, amends HEAD via
    `git commit --amend --no-edit --trailer "Supervised-by: ..." --trailer "Signed-off-by: ..."`
    (one `--trailer` flag per missing trailer; git's `--trailer`
    handling deduplicates by full value, so re-emitting an
    already-present trailer is safe but we skip the present ones
    for cleanliness). Returns `True` if amended, `False` if both
    already present. The amend invocation uses `--no-edit` (NOT
    `-m '<orig>'`) — message is reused in place; trailers are
    appended by git.
  - On `commits_made_count > 1`: logs a warning naming the count and
    skips (mirrors `_sanitize_head_commit_auto_close`'s multi-commit
    safety guard). The prompt-side instruction is the primary defense
    in that shape; the Reviewer-on-impl is the backstop.
- [ ] `foreman.roles.fixer.run_fixer` gets the same env injection +
  post-amend helper application as the Worker on BOTH commit paths
  (`spec_pr` AND `impl_pr`), so every Fixer commit — regardless of
  target — carries BOTH trailers on the REMOTE branch the DCO gate
  checks. The contract requires three coupled changes:
  (a) the LLM-side `git push` instruction is removed from
  `fixer.md` (line 186) so the spec-side LLM commits but does NOT
  push; `fixer_impl.md` already has no explicit push instruction,
  so no prompt-side push edit is needed there;
  (b) after the LLM returns, `_ensure_provenance_trailers` runs
  against the worktree on whichever branch the Fixer target writes
  to, then `fixer.py` calls `host.push_branch(worktree_path=...,
  branch=...)` to deterministic-push the (possibly amended) HEAD
  to the remote — mirroring the Worker's foreman#222 flow at
  `worker.py:1032`;
  (c) `fixer.md`'s no-force-push wording (lines 189-192) is
  narrowed to scope only LLM-side pushes the LLM might attempt;
  the Python-side `host.push_branch` on a bot-owned branch is not
  subject to the same rail.
  Both Fixer targets commit via the LLM's Bash invocations (per
  `fixer.md:186` for the spec-side and the implicit-push
  expectation in the `fixer.py` docstring at line 24 for the
  impl-side); neither path calls `commit_files_to_worktree`. There
  is a single shared `provider.run_agent` call site at
  `packages/foreman/src/foreman/roles/fixer.py:647-653` that
  handles both targets, so one env-dict extension covers both. The
  contract is: every Fixer commit ships with BOTH the
  `Supervised-by:` and `Signed-off-by:` trailers on the REMOTE
  branch, regardless of target.
- [ ] `packages/foreman/src/foreman/prompts/worker.md` gains a new
  `<provenance_trailers>` section (sibling of
  `<commit_message_guardrails>`) that documents the
  `--trailer "Supervised-by: $FOREMAN_OPERATOR_SUPERVISOR_NAME <$FOREMAN_OPERATOR_SUPERVISOR_EMAIL>"`
  and `--trailer "Signed-off-by: $FOREMAN_OPERATOR_SIGNER_NAME <$FOREMAN_OPERATOR_SIGNER_EMAIL>"`
  pattern as the prompt-side primary defense. Equivalent sections
  added to BOTH `packages/foreman/src/foreman/prompts/fixer.md` (the
  spec-side Fixer body — `fixer.md` IS the `spec_pr` prompt per
  `roles/fixer.py:123-128`; place the section near
  `<commit_discipline>` around line 171) AND
  `packages/foreman/src/foreman/prompts/fixer_impl.md` (the impl-side
  Fixer body). `fixer_impl.md` has no `<commit_discipline>` anchor —
  the Worker MUST read the file first and pick the right adjacent
  block (e.g., near the commit-instruction lines around 73-82). The
  goal is the same in all three prompts: instruct the LLM to write
  both trailers correctly so the runtime amend never has to fire.
- [ ] `docker/foreman/config.toml.template` gains an `[operator]`
  block immediately before `[[projects]]`, with nested
  `[operator.supervisor]` and `[operator.signer]` sub-tables, each
  using `name = "${FOREMAN_OPERATOR_SUPERVISOR_NAME}"` /
  `email = "${FOREMAN_OPERATOR_SUPERVISOR_EMAIL}"` and the analogous
  signer envsubst placeholders. No per-project override in the
  shipped template — projects opt in.
- [ ] Unit tests cover: top-level operator parse round-trip
  (both sub-tables), missing operator block raises ValidationError,
  missing `[operator.supervisor]` raises, missing `[operator.signer]`
  raises, missing `name` in either sub-table raises, missing `email`
  in either sub-table raises, whitespace-only `name` raises
  (e.g., `name = " "` raises ValidationError — pins AC bullet 1's
  "non-empty after `.strip()`" invariant against
  `Field(..., min_length=1)` drift), whitespace-only `email` raises,
  malformed `email` raises, per-project override parses with
  supervisor-only set (signer inherited), per-project override
  parses with signer-only set (supervisor inherited), per-project
  override parses with both set, `resolve_operator` returns project
  supervisor when project supervisor set, returns top-level
  supervisor when project supervisor unset, returns project signer
  when project signer set, returns top-level signer when project
  signer unset, `commit_files_to_worktree` adds N trailers when
  given an N-length `provenance_trailers` list (subprocess
  invocation pinned via `subprocess.run` mock or a real `git`
  invocation inside `tmp_path`), Worker `_ensure_provenance_trailers`
  no-ops at 0 commits, amends at 1 commit if EITHER trailer
  missing, no-ops at 1 commit if BOTH already present, warn+skip
  at multi-commit. End-to-end test verifies a Planner spec commit
  on a real `tmp_path` worktree carries BOTH the `Supervised-by:`
  and `Signed-off-by:` trailers.
- [ ] Bot commits produced by Planner / Worker / Fixer-on-impl /
  Fixer-on-spec all include BOTH a `Supervised-by:` line and a
  `Signed-off-by:` line in the commit body, visible to
  `git log --format=%B` and to the DCO gate from PR #346.
- [ ] `docs/RUNBOOK.md` gains a new "Operator identities (DCO
  sign-off + supervision attribution)" section documenting the
  `[operator]` block with its two sub-tables, the per-project
  per-identity override shape, the four
  `FOREMAN_OPERATOR_{SUPERVISOR,SIGNER}_{NAME,EMAIL}` env vars
  consumed by `envsubst` at container start, the trailer policy
  (which trailers are emitted, which is enforced by CI), and a
  one-line rationale linking to PR #346 and to the @wrenrichley
  comment on issue #347.
- [ ] `new_failures_count == 0` on `just check`.

## Approach

This spec introduces two new schema units (`OperatorIdentity` + the
two-identity `OperatorConfig`, plus a `ProjectOperatorOverride`
sibling for per-project override), one new pure resolver function,
and threads the resolved identities through every existing role-bot
commit pathway as a pair of trailers — `Supervised-by:` (from
supervisor) and `Signed-off-by:` (from signer). The DCO gate
validated in PR #346 checks every commit on every PR, so the
plumbing covers ALL three role bots that commit — Planner (spec
doc), Worker (impl), Fixer (spec PR and impl PR) — not just the
Worker as the original issue body narrowly frames it. The
originating issue's framing on "Worker" is sufficient for the
schema work, but a DCO gate is PR-wide in practice, and shipping
operator wiring that covers only one of the commit paths would
leave bot PRs failing DCO from the other paths.

**Schema (§1).** Add three new pydantic models next to the existing
`ProjectConfig` / `AppCredentials` / `AppsConfig` blocks in
`packages/foreman/src/foreman/v4/config.py`:

1. `OperatorIdentity(BaseModel)` — atomic identity unit. Required
   fields `name: str` and `email: str`; both validated with an
   explicit `field_validator('name', 'email', mode='before')` that
   calls `.strip()` on the input and raises `ValueError` if the
   result is empty. This is the canonical pydantic shape for
   "non-empty after `.strip()`" per AC bullet 1 —
   `Field(..., min_length=1)` counts pre-strip characters and
   would accept `" "`, which AC bullet 1 explicitly forbids and
   the test list explicitly pins. The email field is further
   constrained by a second `field_validator('email')` matching
   `[^@\s]+@[^@\s]+\.[^@\s]+`. `extra="forbid"`.
2. `OperatorConfig(BaseModel)` — top-level required block with
   nested `supervisor: OperatorIdentity` and
   `signer: OperatorIdentity` (no defaults — both required).
   `extra="forbid"`.
3. `ProjectOperatorOverride(BaseModel)` — per-project override
   block with optional `supervisor: OperatorIdentity | None = None`
   and `signer: OperatorIdentity | None = None`. Either, both, or
   neither may be set. `extra="forbid"`.

Hang `operator: OperatorConfig` (no default — required) on
`V4Config`. Add optional
`operator: ProjectOperatorOverride | None = None` on
`ProjectConfig`. Extend `load_config` to forward `operator` from
the parsed TOML the same way it forwards `apps` / `orchestrator`
today (only include the key in the payload if it's present in
the raw TOML, so the validation error names the missing field
cleanly instead of running an empty-dict against the
required-field schema).

Add a module-level pure function
`resolve_operator(project: ProjectConfig, config: V4Config) -> OperatorConfig`
that constructs and returns a fresh `OperatorConfig` whose
`supervisor` is `project.operator.supervisor if (project.operator and project.operator.supervisor) else config.operator.supervisor`,
and whose `signer` is resolved independently the same way. Lives
next to `load_config`. This is the single resolution surface every
consumer calls — no duplicated `project.operator.X or ...` ladders
elsewhere in the codebase.

**Pattern naming (per `CLAUDE.md` Decision 4):** This is the
Strategy-by-data pattern in its degenerate single-strategy form
(project override "is-a" replacement strategy) — but applied
independently to two fields. The clearer fit is the "make the
right thing easy" principle (Google §): the schema literally
requires both operator identities at config load, so the daemon
cannot boot without both, so every commit downstream can rely on
the resolver returning a real `OperatorConfig` with both fields
populated. No optionality plumbed downstream → no "forgot to set
sign-off" or "forgot to set supervisor" failure mode.

**Planner (§2).** The Planner does its commit via the typed
`GitHostProvider.commit_files_to_worktree` Python API
(`packages/foreman/src/foreman/git_hosts/github.py:128`). Add a
keyword-only `provenance_trailers: list[str] | None = None`
parameter; when provided (non-empty list), splice one
`"--trailer", value` pair per list entry into the `git commit`
invocation at line 158 (after `"-m", message`). The protocol
declaration in `packages/foreman/src/foreman/git_host.py:117`
gains the same parameter. The Planner caller
(`packages/foreman/src/foreman/roles/planner.py:372`) resolves
the operator via `resolve_operator(project, config)` and passes:

```python
op = resolve_operator(project, config)
provenance_trailers = [
    f"Supervised-by: {op.supervisor.name} <{op.supervisor.email}>",
    f"Signed-off-by: {op.signer.name} <{op.signer.email}>",
]
host.commit_files_to_worktree(..., provenance_trailers=provenance_trailers)
```

No behavior change on `provenance_trailers=None` or empty-list
callers (keeps the test seam clean).

**Worker (§3).** The Worker's commits are LLM-driven via Bash inside
the worktree (`packages/foreman/src/foreman/roles/worker.py:889-897`,
the `provider.run_agent` call). Two coordinated changes:

1. *Plumbing.* Extend the `env` dict on the `run_agent` call to
   include all four operator env vars:
   `FOREMAN_OPERATOR_SUPERVISOR_NAME`,
   `FOREMAN_OPERATOR_SUPERVISOR_EMAIL`,
   `FOREMAN_OPERATOR_SIGNER_NAME`,
   `FOREMAN_OPERATOR_SIGNER_EMAIL`, sourced from
   `resolve_operator(project, config)`. The LLM uses these via the
   prompt-side instruction documented in §6 (one `--trailer` flag
   per env-var pair).
2. *Belt-and-suspenders.* Add
   `_ensure_provenance_trailers(worktree_path, operator, commits_made_count, role_token)`
   to `worker.py`, modeled on the existing
   `_sanitize_head_commit_auto_close` helper (same module, lines
   197-310). Run it on the `implemented` branch (~line 1018) BEFORE
   the existing auto-close strip and BEFORE `host.push_branch`. The
   helper reads HEAD's message via `git log -1 --pretty=%B`;
   checks for BOTH the supervisor's `Supervised-by:` line AND the
   signer's `Signed-off-by:` line matching the resolved operator.
   If both are present, no-op. Otherwise amend HEAD adding the
   missing trailer(s) — one `--trailer` flag per missing trailer
   — via `git commit --amend --no-edit --trailer "..." [--trailer "..."]`.
   The amend uses `--no-edit` (NOT `-m '<orig>'`): the original
   message is reused in place; git appends the new trailer lines
   to the body. Same single-commit safety guard as the existing
   helper (multi-commit warns and skips — `git rebase`-based
   history rewriting is too destructive for a backstop). The
   prompt is the primary defense in the multi-commit shape; the
   Reviewer-on-impl flags any slip.

The amend ordering matters: `_ensure_provenance_trailers` runs
*before* `_sanitize_head_commit_auto_close` so the auto-close
strip's diff is computed against the message that already has
both trailers. Reversing the order would mean the auto-close
strip's amend (which currently uses `--amend -m <sanitized>`,
fully overwriting the message body) wipes the trailers the
previous amend just added. Both helpers no-op on the clean path.

**Fixer (§3, cont.).** Both Fixer targets (`spec_pr` AND `impl_pr`)
follow the same shape as the Worker — both commit via LLM-driven
Bash inside the worktree. The spec-side `fixer.md:186` instructs
the LLM to `git push origin foreman/issue-<N>` after committing.
The impl-side `fixer_impl.md` has NO explicit `git push`
instruction and NO `<commit_discipline>` anchor; the commit/push
behavior is implicit via the `fixer.py` docstring at line 24,
which names "`git add` + `git commit` + `git push` directly" as
the expected flow for both targets. (This means the impl-side
prompt edit is a pure addition near the "Hard rules" block at
lines 73-82 — sibling-add the `<provenance_trailers>` section
and leave the implicit-commit-and-push pattern intact; do NOT
add a new `<commit_discipline>` block mirroring `fixer.md`'s
shape, because Python now owns the push and the LLM-side push
instruction is intentionally absent.) Neither path calls
`commit_files_to_worktree` — `roles/fixer.py` never imports or
invokes that API on either target.

Two coordinated changes mirror the Worker pattern:

1. *Plumbing.* `packages/foreman/src/foreman/roles/fixer.py` has a
   single shared `provider.run_agent` call site at lines 647-653
   that both targets flow through. Extend the `env` dict there to
   include all four operator env vars (the same set the Worker
   gets), sourced from `resolve_operator(project, config)` (with
   `config` plumbed through if not already in scope — read the
   function signature first). One env-dict edit covers both
   targets.
2. *Backstop.* Run `_ensure_provenance_trailers` against the
   worktree on BOTH branches after the LLM returns. Mirror the
   Worker's helper structure: zero-commit no-op, single-commit
   amend-if-either-missing, multi-commit warn+skip. Factor the
   helper into a shared module so the Worker and both Fixer paths
   share one implementation (preferred), or duplicate per existing
   role conventions if the Worker stage hits an import-graph wall
   — both options satisfy the AC tests pinning trailer presence
   on every commit.

   **Push-flow reconciliation (mandated).** The runtime amend only
   rewrites local HEAD; the DCO gate from PR #346 checks REMOTE
   commits. Today the LLM pushes from inside its Bash session
   (`fixer.md:186` for the spec-side and implicitly via the
   `fixer.py` docstring at line 24 for the impl-side), so by the
   time Python regains control to run
   `_ensure_provenance_trailers`, an un-amended commit is already
   on the remote and the local amend is invisible to CI.
   "Amending before the LLM's push lands by sequencing the helper
   inside the same role-run boundary" is mechanically impossible —
   Python regains control only after the LLM's Bash session ends.
   The Worker stage MUST therefore:

   (a) Remove the LLM-side push instruction from `fixer.md` (line
       186 — delete the "After committing, `git push origin
       foreman/issue-<N>`" sentence) so the spec-side LLM commits
       but does NOT push. `fixer_impl.md` has no explicit
       `git push` instruction to remove (per the framing above);
       the implicit-push expectation from `fixer.py` docstring
       line 24 is what changes, not a prompt edit.
   (b) Add a Python-side `host.push_branch(worktree_path=...,
       branch=...)` call in `fixer.py` AFTER
       `_ensure_provenance_trailers` runs and BEFORE control
       returns to Foreman core — mirroring the Worker's
       foreman#222 flow at `worker.py:1032`. The branch is
       `foreman/issue-<N>` on the `spec_pr` target and
       `foreman/impl-<N>` on the `impl_pr` target; both are
       bot-owned and safe to deterministic-push.
   (c) Narrow `fixer.md`'s no-force-push wording (lines 189-192,
       the "If `git push` fails… do NOT attempt `--force`"
       paragraph) so it explicitly scopes to LLM-side pushes the
       LLM might attempt — the Python-side `host.push_branch` of
       an amended commit on a bot-owned branch is a non-issue
       (the branch only has bot history) and `host.push_branch`
       is deterministic, not LLM-driven, so the existing safety
       rail still applies wherever it matters.

   This mandate makes the runtime amend a real backstop: even
   when the LLM forgets a trailer, Python's amend lands on local
   HEAD and Python's subsequent `host.push_branch` carries that
   amended HEAD to the remote, where the DCO gate sees it. The
   prompt-side `<provenance_trailers>` guard is still the primary
   defense — in the clean path the LLM writes both trailers
   correctly and the helper no-ops without amending — but the
   not-clean path also produces a DCO-passing remote commit.

**Prompts (§6).** Add a `<provenance_trailers>` section to
`packages/foreman/src/foreman/prompts/worker.md` (next to the
existing `<commit_message_guardrails>` block around line 274)
instructing the LLM to always pass BOTH

```
--trailer "Supervised-by: $FOREMAN_OPERATOR_SUPERVISOR_NAME <$FOREMAN_OPERATOR_SUPERVISOR_EMAIL>"
--trailer "Signed-off-by: $FOREMAN_OPERATOR_SIGNER_NAME <$FOREMAN_OPERATOR_SIGNER_EMAIL>"
```

to every `git commit` call. Equivalent sections added to BOTH
`packages/foreman/src/foreman/prompts/fixer.md` (the spec-side
Fixer body — `fixer.md` IS the `spec_pr` prompt per
`roles/fixer.py:123-128`, with `<commit_discipline>` at line 171
as the right adjacent anchor) AND
`packages/foreman/src/foreman/prompts/fixer_impl.md` (the
impl-side Fixer body). `fixer_impl.md` does NOT contain a
`<commit_discipline>` anchor; the Worker MUST read
`fixer_impl.md` first and pick the right adjacent block (e.g.,
near the commit-instruction lines around 73-82). Frame all three
sections as: "Python amends HEAD with the missing trailer(s)
after you return if either is missing; this prompt instruction
is the primary defense — the runtime amend is the backstop."
Mirrors the auto-close prompt+runtime layering already in place.

**Container template (§7).** Add an `[operator]` block to
`docker/foreman/config.toml.template` between `[orchestrator]`
(line 32-34) and `[[projects]]` (line 36+). Shape:

```toml
[operator.supervisor]
name = "${FOREMAN_OPERATOR_SUPERVISOR_NAME}"
email = "${FOREMAN_OPERATOR_SUPERVISOR_EMAIL}"

[operator.signer]
name = "${FOREMAN_OPERATOR_SIGNER_NAME}"
email = "${FOREMAN_OPERATOR_SIGNER_EMAIL}"
```

The four env vars get expanded by the existing `envsubst` pipeline
in `docker/entrypoint.sh:74` ("envsubst < ... > $FOREMAN_V4_CONFIG").
Operator sets them in `.env` alongside `FOREMAN_PLANNER_APP_ID` etc.
In the common case where supervisor and signer are the same person,
set both env-var pairs to the same values; the schema does not
enforce uniqueness.

**Docs (§8).** New section in `docs/RUNBOOK.md` near the existing
"Pre-commit hooks" / "Import-graph boundaries" sections, titled
"Operator identities (DCO sign-off + supervision attribution)".
Documents (a) the `[operator]` schema with both sub-tables, (b)
the per-identity per-project override shape, (c) the four
`FOREMAN_OPERATOR_{SUPERVISOR,SIGNER}_{NAME,EMAIL}` env vars
consumed by `envsubst` at container start, (d) the trailer
policy — which trailers foreman emits on every bot commit and
which is the only one the DCO CI gate enforces, (e) one-line
rationale + a link to PR #346, to the @wrenrichley 2026-06-19
comment on issue #347 (the authoritative direction for the
two-identity shape), and to the Linux kernel coding-assistants
policy the `Signed-off-by:` pattern follows.

## Sub-requests (topologically sorted)
1. Add `OperatorIdentity(BaseModel)` to
   `packages/foreman/src/foreman/v4/config.py` with required
   `name: str` and `email: str`, `extra="forbid"`, email-shape
   `field_validator`, and the `mode="before"` `.strip()`/empty-check
   validator for `name` + `email`.
2. Add `OperatorConfig(BaseModel)` in the same file with required
   nested `supervisor: OperatorIdentity` and `signer: OperatorIdentity`
   (no defaults), `extra="forbid"`.
3. Add `ProjectOperatorOverride(BaseModel)` in the same file with
   optional `supervisor: OperatorIdentity | None = None` and
   `signer: OperatorIdentity | None = None`, `extra="forbid"`.
4. Hang required `operator: OperatorConfig` on `V4Config` in the
   same file (no default — load-time validation enforces presence).
5. Add optional
   `operator: ProjectOperatorOverride | None = None` to
   `ProjectConfig` in the same file.
6. Extend `load_config` so the `operator` key flows from raw TOML
   to the `V4Config.model_validate` payload, mirroring how `apps`
   and `orchestrator` are forwarded today
   (`packages/foreman/src/foreman/v4/config.py:213-220`).
7. Add module-level
   `resolve_operator(project: ProjectConfig, config: V4Config) -> OperatorConfig`
   in the same file. Pure function: builds a fresh `OperatorConfig`
   by resolving `supervisor` and `signer` independently
   (`project.operator.X or config.operator.X` per field, with the
   project-side null-check for the outer `operator` field).
8. Add unit tests in
   `packages/foreman/tests/v4/test_config.py`: top-level operator
   parse round-trip (both sub-tables populated), missing operator
   raises, missing `[operator.supervisor]` raises, missing
   `[operator.signer]` raises, missing `name` in either sub-table
   raises, missing `email` in either sub-table raises,
   whitespace-only `name` raises (e.g., `name = " "` raises
   ValidationError — pins the "non-empty after `.strip()`"
   invariant from AC bullet 1), whitespace-only `email` raises,
   malformed `email` raises (e.g., `"not-an-email"`), per-project
   override parses with supervisor-only set, per-project override
   parses with signer-only set, per-project override parses with
   both set, resolver returns project supervisor when project
   supervisor set + top-level supervisor when project unset
   (independent per-field), resolver returns project signer when
   project signer set + top-level signer when project unset.
   Extend the existing `_APPS_TOML` shared fixture with an
   `_OPERATOR_TOML` sibling (containing both sub-tables) so
   existing tests pass.
9. Add `provenance_trailers: list[str] | None = None`
   keyword-only parameter to
   `GitHostProvider.commit_files_to_worktree` in
   `packages/foreman/src/foreman/git_host.py:117` (Protocol-level).
10. Implement the parameter in
    `packages/foreman/src/foreman/git_hosts/github.py:128`: when
    `provenance_trailers` is a non-empty list, splice one
    `"--trailer", value` pair per list entry into the `git commit`
    invocation at line 158 (right after `"-m", message`), in list
    order.
11. Wire the resolver into the Planner: in
    `packages/foreman/src/foreman/roles/planner.py:372`, call
    `op = resolve_operator(project, config)` (with `config`
    plumbed through if not already in scope — read the function
    signature first) and pass
    `provenance_trailers=[f"Supervised-by: {op.supervisor.name} <{op.supervisor.email}>", f"Signed-off-by: {op.signer.name} <{op.signer.email}>"]`
    to `host.commit_files_to_worktree`.
12. Test the Planner trailer end-to-end: create a real worktree in
    `tmp_path`, call `commit_files_to_worktree` with a
    two-entry `provenance_trailers` list, `git log -1 --pretty=%B`
    against the resulting HEAD, assert BOTH the `Supervised-by:`
    and `Signed-off-by:` lines are present.
13. Plumb all four operator env vars
    (`FOREMAN_OPERATOR_SUPERVISOR_NAME`,
    `FOREMAN_OPERATOR_SUPERVISOR_EMAIL`,
    `FOREMAN_OPERATOR_SIGNER_NAME`,
    `FOREMAN_OPERATOR_SIGNER_EMAIL`) into the Worker's
    `provider.run_agent` env dict in
    `packages/foreman/src/foreman/roles/worker.py:896` (the
    `env={**os.environ, "GH_TOKEN": worker_token}` dict gets four
    new keys derived from `resolve_operator(project, config)`).
14. Implement
    `_ensure_provenance_trailers(worktree_path, operator,
    commits_made_count, role_token)` in `worker.py`. Same overall
    shape as `_sanitize_head_commit_auto_close` (lines 197-310):
    zero-commit no-op, single-commit amend-if-either-missing,
    multi-commit warn+skip. Uses
    `git commit --amend --no-edit --trailer "Supervised-by: ..." --trailer "Signed-off-by: ..."`
    with one `--trailer` flag per *missing* trailer (skip
    trailers already present in HEAD's message to keep the
    invocation cleaner; either is safe because git's `--trailer`
    handling deduplicates by full value).
15. Call `_ensure_provenance_trailers` at line ~1018 (the
    `implemented` branch, between the `pr_title`/`pr_body`
    asserts and the existing `_sanitize_head_commit_auto_close`
    call — trailer add BEFORE auto-close strip so the strip's
    amend works against the final-shape message).
16. Unit tests for the Worker helper:
    `_ensure_provenance_trailers` zero-commits no-op,
    single-commit-both-missing amend (verify both trailers land),
    single-commit-only-supervisor-missing amend (verify only
    supervisor trailer is added), single-commit-only-signer-missing
    amend, single-commit-both-present no-op, multi-commit
    warn+skip. Mirror the `_sanitize_head_commit_auto_close` test
    structure in `packages/foreman/tests/test_worker.py` (or
    whichever test file covers the existing helper — read first).
17. Mirror the env injection + helper for BOTH Fixer targets
    (`spec_pr` AND `impl_pr`). In
    `packages/foreman/src/foreman/roles/fixer.py`, the shared
    `provider.run_agent` call at lines 647-653 covers both
    targets, so one env-dict extension (adding the same four
    operator env vars from `resolve_operator(project, config)`)
    threads through both. After the LLM returns, run
    `_ensure_provenance_trailers` against the worktree on
    whichever branch the Fixer target writes to, then
    immediately call `host.push_branch(worktree_path=...,
    branch=...)` to deterministic-push the (possibly amended)
    HEAD to the remote — mirroring the Worker's foreman#222
    flow at `worker.py:1032`. The branch is `foreman/issue-<N>`
    on the `spec_pr` target and `foreman/impl-<N>` on the
    `impl_pr` target. Both paths commit via LLM-driven Bash
    (per `fixer.md:186` for the spec-side and the implicit-push
    expectation in the `fixer.py` docstring at line 24 for the
    impl-side); the LLM-side push instruction in `fixer.md:186`
    is removed in sub-request 19 so Python owns the push for
    both targets. Factor the helper into a shared module so the
    Worker and both Fixer paths share one implementation
    (preferred), or duplicate per existing role conventions —
    both leave the contract the AC tests pin.
18. Add `<provenance_trailers>` section to
    `packages/foreman/src/foreman/prompts/worker.md` (next to
    `<commit_message_guardrails>` around line 274). Documents
    the dual `--trailer` shape using the four env vars, names
    Python's belt-and-suspenders amend as the backstop, and
    (for symmetry with the auto-close guardrails) explicitly
    says "you should still write both trailers correctly; the
    runtime amend is a backstop, not a license to be sloppy."
19. Add the equivalent `<provenance_trailers>` section to
    `packages/foreman/src/foreman/prompts/fixer.md` (the
    **spec-side** Fixer body — `fixer.md` IS the `spec_pr`
    prompt per `roles/fixer.py:123-128`), near
    `<commit_discipline>` around line 171. In the SAME edit
    pass on `fixer.md`:
    (a) delete the "After committing, `git push origin
    foreman/issue-<N>` so the PR branch reflects your work"
    sentence at line 186 — Python now owns the push via
    `host.push_branch` (sub-request 17); the LLM must commit
    but not push;
    (b) narrow the `<commit_discipline>` no-force-push
    paragraph at lines 189-192 so its scope is explicitly
    limited to LLM-side push attempts (e.g., reframe as "If
    you somehow attempt a `git push` and it fails…"), since
    the Python-side `host.push_branch` of an amended commit on
    a bot-owned branch is not subject to the same rail.
    ALSO add the same `<provenance_trailers>` section to
    `packages/foreman/src/foreman/prompts/fixer_impl.md` (the
    **impl-side** Fixer body). `fixer_impl.md` has no
    `<commit_discipline>` anchor and no explicit `git push`
    instruction (so there is no prompt-side push to delete on
    the impl-side — Python's push handles both targets). Pick
    the sibling-add-only shape: add a new
    `<provenance_trailers>` section near the
    commit-instruction lines around 73-82, leaving the
    implicit-commit-and-push pattern in the existing "Hard
    rules" intact. Do NOT add a new `<commit_discipline>`
    block mirroring `fixer.md`'s shape, because Python now
    owns the push and the LLM-side push instruction is
    intentionally absent.
20. Add the `[operator]` block (with both
    `[operator.supervisor]` and `[operator.signer]`
    sub-tables) to `docker/foreman/config.toml.template`
    between `[orchestrator]` (line 32-34) and the first
    `[[projects]]` block (line 36+). Uses the four
    `${FOREMAN_OPERATOR_SUPERVISOR_NAME}` /
    `${FOREMAN_OPERATOR_SUPERVISOR_EMAIL}` /
    `${FOREMAN_OPERATOR_SIGNER_NAME}` /
    `${FOREMAN_OPERATOR_SIGNER_EMAIL}` envsubst placeholders
    (no quotes around the name placeholders are required;
    `${FOO}` resolves to the literal value and TOML accepts
    both plain and quoted strings — match the surrounding
    template style).
21. Document the new section in `docs/RUNBOOK.md`. Add a new
    "Operator identities (DCO sign-off + supervision
    attribution)" H2 section between the existing
    "Pre-commit hooks" and "Import-graph boundaries"
    sections. Includes: the `[operator]` schema with both
    sub-tables, per-identity per-project override shape, the
    four env-var names consumed by envsubst, the trailer
    policy (which trailers foreman emits and which is the
    only one the DCO gate enforces), one-line rationale +
    links to PR #346, the @wrenrichley 2026-06-19 comment on
    issue #347, and the Linux kernel coding-assistants
    policy.
22. Run `just check` and confirm `new_failures_count == 0`.

## File-level changes
| File | Change |
|------|--------|
| `packages/foreman/src/foreman/v4/config.py` | Add `OperatorIdentity` (required `name` + `email`, both validated for non-empty-after-strip + email regex on `email`); add `OperatorConfig` (required nested `supervisor` + `signer`); add `ProjectOperatorOverride` (optional `supervisor` + `signer`); hang required `operator: OperatorConfig` on `V4Config`; add optional `operator: ProjectOperatorOverride \| None = None` on `ProjectConfig`; extend `load_config` to forward the `operator` key; add `resolve_operator(project, config)` pure function returning a fresh `OperatorConfig` with both identities resolved independently. |
| `packages/foreman/tests/v4/test_config.py` | New tests for parse, missing-fields (per sub-table), per-identity per-project override, resolver. Extend the shared `_APPS_TOML` fixture with an `_OPERATOR_TOML` sibling (containing both sub-tables) so existing tests continue to validate the now-required block. |
| `packages/foreman/src/foreman/git_host.py` | Add `provenance_trailers: list[str] \| None = None` to the `commit_files_to_worktree` Protocol. |
| `packages/foreman/src/foreman/git_hosts/github.py` | Implement the new parameter — splice one `--trailer "<value>"` pair per list entry into the `git commit` invocation. |
| `packages/foreman/src/foreman/roles/planner.py` | Resolve operator and pass a two-entry `provenance_trailers=[Supervised-by..., Signed-off-by...]` list to `commit_files_to_worktree`. |
| `packages/foreman/src/foreman/roles/worker.py` | Add `_ensure_provenance_trailers` helper (checks both trailers, amends with one `--trailer` flag per missing trailer via `--amend --no-edit`); call it on the `implemented` branch BEFORE `_sanitize_head_commit_auto_close`; plumb all four operator env vars into the `provider.run_agent` env dict. |
| `packages/foreman/src/foreman/roles/fixer.py` | Mirror the Worker's env plumbing + post-amend on BOTH Fixer targets (`spec_pr` AND `impl_pr`). One env-dict extension at the shared `provider.run_agent` call site (lines 647-653) covers both targets; the post-amend helper runs on each branch after the LLM returns, then `host.push_branch` is called immediately after (mirroring `worker.py:1032`) so the amended HEAD lands on the remote where the DCO gate checks. |
| `packages/foreman/tests/test_worker.py` (or wherever the existing `_sanitize_head_commit_auto_close` tests live) | New tests for `_ensure_provenance_trailers` covering zero/one (both-missing, only-supervisor-missing, only-signer-missing, both-present)/multi-commit cases. |
| `packages/foreman/src/foreman/prompts/worker.md` | New `<provenance_trailers>` section documenting the dual `--trailer` pattern + the runtime amend as backstop. |
| `packages/foreman/src/foreman/prompts/fixer.md` | New `<provenance_trailers>` section for the **spec-side** Fixer body, near `<commit_discipline>` around line 171. Also: (a) delete the "After committing, `git push origin foreman/issue-<N>`" sentence at line 186 (Python now owns the push via `host.push_branch`); (b) narrow the no-force-push paragraph at lines 189-192 so its scope explicitly limits to LLM-side push attempts. (`fixer.md` is the `spec_pr` prompt per `roles/fixer.py:123-128`.) |
| `packages/foreman/src/foreman/prompts/fixer_impl.md` | New `<provenance_trailers>` section for the **impl-side** Fixer body. This file has no `<commit_discipline>` anchor and no explicit `git push` instruction (commit/push behavior is implicit via the `fixer.py` docstring at line 24); the edit is a pure addition — read the file first and pick the right adjacent block (e.g., near the commit-instruction lines around 73-82). No prompt-side `git push` to remove, since Python now owns the push for both targets (sub-request 17). Sibling-add only `<provenance_trailers>`; do NOT add a new `<commit_discipline>` block. |
| `docker/foreman/config.toml.template` | Add `[operator.supervisor]` + `[operator.signer]` blocks with `${FOREMAN_OPERATOR_SUPERVISOR_NAME}` / `${FOREMAN_OPERATOR_SUPERVISOR_EMAIL}` / `${FOREMAN_OPERATOR_SIGNER_NAME}` / `${FOREMAN_OPERATOR_SIGNER_EMAIL}` placeholders between `[orchestrator]` and `[[projects]]`. |
| `docs/RUNBOOK.md` | New "Operator identities (DCO sign-off + supervision attribution)" section. |

## Alternatives considered
- **Keep the flat single-identity `[operator]` block (just `name` +
  `email`) from the original issue body, emitting only a
  `Signed-off-by:` trailer.** Rejected: @wrenrichley's 2026-06-19
  comment on the issue explicitly supersedes the original issue
  body's flat framing with a two-identity policy plus a second
  `Supervised-by:` trailer. The comment is the authoritative
  direction; a spec that ignored it would ship the wrong shape.
  The flat shape also conflates two distinct concerns (legal DCO
  attestation by the signer vs. orchestration attribution by the
  supervisor) into one field, which would force a schema migration
  the moment we wanted distinct people for the two roles (the
  foreman-driven Wren-orchestrates-Jeff-signs case the comment
  spells out).
- **Replace the role-bot `GIT_AUTHOR_*` env vars with the operator's
  identity instead of layering trailers on top.** Rejected per the
  issue body's explicit out-of-scope: the bot is the legitimate
  author of the code generation; the DCO trailer is a
  *certification of provenance* by the human, and the
  `Supervised-by:` trailer is an *orchestration attribution* by
  the human. The author/committer attribution belongs to the bot;
  the human identities belong on trailers. Replacing the env vars
  would mis-attribute every commit and break the existing
  per-role bot identity model the v4 IdentityRegistry
  (`packages/foreman/src/foreman/v4/identity.py`) is built around.
- **Use `git commit -s` after temporarily exporting
  `GIT_COMMITTER_NAME` / `GIT_COMMITTER_EMAIL` to the signer
  identity, then restoring.** Rejected: `-s` reads `user.name` /
  `user.email` from `git config`, not env vars, and produces only
  `Signed-off-by:` (no analogue for `Supervised-by:`). Doing this
  correctly would require either (a) temporarily writing
  `.git/config` (forbidden per the foreman#53 leak fix at
  `packages/foreman/src/foreman/git_hosts/github.py:113`) or (b)
  invoking with `-c user.name=... -c user.email=... commit -s`,
  which doubles the surface area vs. the explicit `--trailer`
  form. The explicit `--trailer` form is what the issue body
  recommends, what the @wrenrichley comment's two-trailer policy
  needs, and what we ship.
- **Have only the Worker carry the trailers; let the Planner's
  spec doc commit stay unsigned and the DCO gate ignore spec
  PRs.** Rejected: DCO is a property of the merge target (the
  protected branch), not of the role. A protected-branch DCO
  check that ignores spec PRs is fragile (one bot commit
  category gets a per-PR-shape exemption baked into branch
  protection), and PR #346's test setup validates the gate
  against every bot PR. Plumbing operator through all role-bot
  commit paths is the principled fix.
- **Make `[operator]` optional with a sentinel default (e.g.,
  `"foreman-default <bot@noreply.github.com>"`) so existing
  test fixtures don't all need updating.** Rejected: a default
  would let the daemon boot without a real signer identity,
  which is exactly the failure mode DCO is supposed to prevent
  (and the supervisor identity has no sensible default either).
  The shared `_APPS_TOML` fixture gets a sibling `_OPERATOR_TOML`
  block — one well-named constant, used everywhere — so the
  test churn is bounded.
- **Make the per-project override a flat
  `OperatorConfig | None` (override both or neither) rather
  than an independent-per-identity
  `ProjectOperatorOverride`.** Rejected: the @wrenrichley
  comment specifies `[[projects.operator.supervisor]]` and
  `[[projects.operator.signer]]` as independently overridable.
  A flat override-both-or-neither shape would force projects to
  duplicate the inherited identity just to override the other
  one. The cost of `ProjectOperatorOverride` over plain
  `OperatorConfig | None` is one extra pydantic model — bounded.
- **Skip the per-project override entirely (top-level operator
  only).** Rejected: the original issue body explicitly requires
  per-project override for the case where one of foreman's
  managed projects has a different maintainer than the rest;
  the @wrenrichley comment preserves this requirement while
  refining it to per-identity. The override field is one
  optional pydantic model on `ProjectConfig`; ignoring the
  requirement would be cheaper to implement now but force a
  schema migration later.
- **Use the `commit-msg` hook to inject the trailers instead of
  Python-side amend.** Rejected: the worktree's `.git/hooks/`
  directory shares the parent repo's hookspath (foreman#53 leak
  family — same reason we don't write `user.name` to
  `.git/config`). Writing into the hooks directory would leak
  to subsequent human commits in the same worktree.
- **Two separate resolvers `resolve_operator_supervisor` and
  `resolve_operator_signer` instead of one
  `resolve_operator` returning a fresh `OperatorConfig`.**
  Considered. Rejected on simplicity: every consumer needs both
  identities (Planner builds two trailers in one call; Worker
  exports four env vars; the helper checks both trailers). One
  resolver that returns both keeps the call sites short. The
  internal implementation still resolves the two fields
  independently — the API surface is the only thing that's
  unified.

## Open questions
- None blocking. Three judgment calls are decided in the spec
  rather than left open: (a) the spec WIDENS the original issue's
  stated scope from "Worker only" to "all three role bots that
  commit" because DCO gates are PR-wide; (b) the spec adopts the
  two-identity / two-trailer policy from @wrenrichley's
  2026-06-19 comment on the issue (which supersedes the
  original issue body's flat framing); (c) the spec uses the
  explicit `--trailer` form (rather than `-s`) per the issue
  body's stated preference and the comment's two-trailer
  requirement.

## Out of scope
- Adding DCO enforcement to branch protection — that's a
  separate decision once bot PRs reliably pass the
  non-blocking gate from PR #346 (per issue body).
- Refactoring how role-bot `GIT_AUTHOR_*` env injection works —
  that stays as-is (per issue body).
- Merging or editing the unstaged `CONTRIBUTING.md` draft at
  repo root — that lands once the full DCO arc is verified
  (per issue body). The comment's note that CONTRIBUTING.md
  becomes the reference for all three trailers
  (`Co-Authored-By:` recognition, `Supervised-by:`
  orchestration, `Signed-off-by:` DCO) is acknowledged here
  but the actual prose update happens in a follow-up.
- Adding the `Supervised-by:` trailer (or any
  `Supervised-by:`-related CI gate) to release-please /
  `release.yml`.
- Validating either operator email against any external system
  (LDAP, GitHub-account-exists, MX-record check). The schema
  validates shape only; semantic validity is the operator's
  responsibility.
- Carrying either operator identity into the Reviewer's
  PR-review comments (the Reviewer doesn't commit; nothing to
  sign or supervise).
- A `[[projects.operator]]` resolution priority order beyond
  "per-identity project override → per-identity top-level →
  fail loud". No env-var fallback, no per-issue override, no
  per-role override.
- Adjusting the two existing `Co-Authored-By:` trailers
  (foreman-{role}-bot and model). The comment names them as
  unchanged-behavior recognition trailers; this spec leaves
  them alone.
- Bumping any V4Config schema version field. The schema
  doesn't currently carry a version field, and the substrate
  cutover (foreman#333) just landed v4 with no in-flight
  production state to migrate, so the new required block is
  enforced at first load post-deploy (per issue body).
