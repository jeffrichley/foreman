# Spec: add `[operator]` identity to V4Config for DCO sign-off (issue #347)

## Goal
Add a top-level `[operator]` block to `V4Config` (`name` + `email`, both
required) plus an optional `[[projects.operator]]` per-project override,
and wire the resolved operator identity into every role-bot commit as a
`Signed-off-by:` trailer. This is the schema + plumbing prerequisite that
makes DCO enforcement (validated as a non-blocking CI gate in [PR #346])
mechanically possible: foreman-{planner,worker,fixer}-bot commits cannot
self-certify provenance, so the trailer carries the *operator* who
dispatched the run instead. Tracks
[foreman#347](https://github.com/jeffrichley/foreman/issues/347).

## Acceptance criteria
- [ ] `V4Config` parses a top-level `[operator]` block with required
  fields `name: str` (non-empty after `.strip()`) and `email: str`
  (matches a basic RFC-5321-ish regex: `^[^@\s]+@[^@\s]+\.[^@\s]+$`).
  Missing block, missing field, or empty `name`/`email` raises
  `pydantic.ValidationError` at `load_config()`. Same `extra="forbid"`
  discipline the rest of the schema uses.
- [ ] `ProjectConfig` gains an optional `operator: OperatorConfig | None`
  field (defaults to `None`). When set, validates with the same rules.
  When unset, the project inherits the top-level `[operator]`.
- [ ] A resolver function in `foreman.v4.config` —
  `resolve_operator(project: ProjectConfig, config: V4Config) -> OperatorConfig` —
  returns the project's override when present, else the top-level
  operator. Pure function, no I/O.
- [ ] `foreman.git_hosts.github.GitHubProvider.commit_files_to_worktree`
  accepts a new keyword-only parameter
  `signoff_trailer: str | None = None`. When provided, the `git commit`
  invocation gains `--trailer "Signed-off-by: <trailer>"`. When `None`,
  behavior is unchanged (backwards-compatible default).
- [ ] `foreman.roles.planner._run_planner_core` resolves the operator
  via `resolve_operator(project, config)` and passes
  `signoff_trailer=f"{op.name} <{op.email}>"` to
  `host.commit_files_to_worktree`. The spec doc commit lands with a
  `Signed-off-by:` trailer matching the resolved operator.
- [ ] `foreman.roles.worker._run_worker_core` passes the resolved
  operator identity to the LLM subprocess via two new env vars
  (`FOREMAN_OPERATOR_NAME`, `FOREMAN_OPERATOR_EMAIL`) on the
  `provider.run_agent` `env` dict, AND runs a new
  `_ensure_signoff_trailer(worktree_path, operator, commits_made_count, role_token)`
  helper after the LLM returns + before the existing
  `_sanitize_head_commit_auto_close` call. The helper:
  - On `commits_made_count == 0`: no-op, returns `False`.
  - On `commits_made_count == 1`: reads HEAD's message; if it does NOT
    already contain a `Signed-off-by: <name> <<email>>` line matching
    the resolved operator, amends HEAD via
    `git commit --amend -m '<orig>' --trailer "Signed-off-by: <name> <<email>>"`.
    Returns `True` if amended, `False` if already present.
  - On `commits_made_count > 1`: logs a warning naming the count and
    skips (mirrors `_sanitize_head_commit_auto_close`'s multi-commit
    safety guard). The prompt-side instruction is the primary defense
    in that shape; the Reviewer-on-impl is the backstop.
- [ ] `foreman.roles.fixer.run_fixer` (the impl-side path, since
  Fixer-on-spec doesn't touch commits) gets the same env injection +
  post-amend helper application as the Worker, so Fixer-on-impl
  commits also carry the trailer. (The Fixer-on-spec path doesn't
  commit; it edits the spec branch via the Planner-style API. Wire
  the trailer through whatever Python-side commit call it uses, if
  any — see Approach §3 for the read-the-code-first step.)
- [ ] `packages/foreman/src/foreman/prompts/worker.md` gains a new
  `<dco_signoff>` section (sibling of `<commit_message_guardrails>`)
  that documents the `--trailer "Signed-off-by: $FOREMAN_OPERATOR_NAME
  <$FOREMAN_OPERATOR_EMAIL>"` pattern as the prompt-side primary
  defense. Equivalent section added to `fixer.md` (impl-side).
- [ ] `docker/foreman/config.toml.template` gains an `[operator]`
  block immediately before `[[projects]]`, with
  `name = "${FOREMAN_OPERATOR_NAME}"` and
  `email = "${FOREMAN_OPERATOR_EMAIL}"` envsubst placeholders. No
  per-project override in the shipped template — projects opt in.
- [ ] Unit tests cover: top-level operator parse round-trip, missing
  operator raises ValidationError, missing `name` raises, missing
  `email` raises, malformed `email` raises, per-project override
  parses, `resolve_operator` returns project override when set,
  `resolve_operator` returns top-level when project override unset,
  `commit_files_to_worktree` adds trailer when `signoff_trailer` set
  (subprocess invocation pinned via `subprocess.run` mock or a real
  `git` invocation inside `tmp_path`), Worker
  `_ensure_signoff_trailer` no-ops at 0 commits, amends at 1 commit
  if missing, no-ops at 1 commit if already present, warn+skip at
  multi-commit. End-to-end test verifies a Planner spec commit on a
  real `tmp_path` worktree carries the `Signed-off-by:` trailer.
- [ ] `docs/RUNBOOK.md` gains a new "Operator identity (DCO sign-off)"
  section documenting the `[operator]` block, the per-project override
  shape, the `FOREMAN_OPERATOR_NAME` / `FOREMAN_OPERATOR_EMAIL` env
  vars consumed by `envsubst` at container start, and a one-line
  rationale linking to PR #346.
- [ ] `new_failures_count == 0` on `just check`.

## Approach

This spec introduces one new schema unit (`OperatorConfig`), one new pure
resolver function, and threads the resolved identity through every
existing role-bot commit pathway as a `Signed-off-by:` trailer. The DCO
gate validated in PR #346 checks every commit on every PR, so the
plumbing covers ALL three role bots that commit — Planner (spec doc),
Worker (impl), Fixer-on-impl (fix commits) — not just the Worker as
the issue body narrowly frames it. The originating issue's framing on
"Worker" is sufficient for the schema work, but a DCO gate is PR-wide
in practice, and shipping operator wiring that covers only one of the
three commit paths would leave bot PRs failing DCO from the Planner's
side.

**Schema (§1).** Add `OperatorConfig(BaseModel)` next to the existing
`ProjectConfig` / `AppCredentials` / `AppsConfig` blocks in
`packages/foreman/src/foreman/v4/config.py`. Required fields
`name: str` and `email: str`; both validated with `Field(...,
min_length=1)` and the email field constrained by a `field_validator`
matching the basic shape `[^@\s]+@[^@\s]+\.[^@\s]+`. `extra="forbid"`
matches the rest of the schema. Hang `operator: OperatorConfig` (no
default — required) on `V4Config`. Add optional `operator:
OperatorConfig | None = None` on `ProjectConfig`. Extend `load_config`
to forward `operator` from the parsed TOML the same way it forwards
`apps` / `orchestrator` today (only include the key in the payload if
it's present in the raw TOML, so the validation error names the
missing field cleanly instead of running an empty-dict against the
required-field schema).

Add a module-level pure function
`resolve_operator(project: ProjectConfig, config: V4Config) -> OperatorConfig`
that returns `project.operator or config.operator`. Lives next to
`load_config`. This is the single resolution surface every consumer
calls — no duplicated `project.operator or ...` ladders elsewhere in
the codebase.

**Pattern naming (per `CLAUDE.md` Decision 4):** This is the
Strategy-by-data pattern in its degenerate single-strategy form
(project override "is-a" replacement strategy). The clearer fit is the
"make the right thing easy" principle (Google §): the schema literally
requires an operator identity at config load, so the daemon cannot
boot without one, so every commit downstream can rely on the resolver
returning a real value. No optionality plumbed downstream → no
"forgot to set sign-off" failure mode.

**Planner (§2).** The Planner does its commit via the typed
`GitHostProvider.commit_files_to_worktree` Python API
(`packages/foreman/src/foreman/git_hosts/github.py:128`). Add a
keyword-only `signoff_trailer: str | None = None` parameter; when
provided, splice `"--trailer", f"Signed-off-by: {signoff_trailer}"`
into the `git commit` invocation at line 158. The protocol declaration
in `packages/foreman/src/foreman/git_host.py:117` gains the same
parameter. The Planner caller (`packages/foreman/src/foreman/roles/planner.py:372`)
resolves the operator via `resolve_operator(project, config)` and
passes the formatted `"<name> <<email>>"` string. No behavior change
on `signoff_trailer=None` callers (keeps the test seam clean).

**Worker (§3).** The Worker's commits are LLM-driven via Bash inside
the worktree (`packages/foreman/src/foreman/roles/worker.py:889-897`,
the `provider.run_agent` call). Two coordinated changes:

1. *Plumbing.* Extend the `env` dict on the `run_agent` call to include
   `FOREMAN_OPERATOR_NAME` and `FOREMAN_OPERATOR_EMAIL`, sourced from
   `resolve_operator(project, config)`. The LLM uses these via the
   prompt-side instruction documented in §6.
2. *Belt-and-suspenders.* Add
   `_ensure_signoff_trailer(worktree_path, operator, commits_made_count, role_token)`
   to `worker.py`, modeled on the existing
   `_sanitize_head_commit_auto_close` helper (same module, lines
   197-310). Run it on the `implemented` branch (~line 1018) BEFORE
   the existing auto-close strip and BEFORE `host.push_branch`. The
   helper reads HEAD's message via `git log -1 --pretty=%B`; if the
   message already contains the operator's `Signed-off-by:` line, no-op.
   Otherwise, amend HEAD with the same `--trailer` shape via
   `git commit --amend --no-edit --trailer "Signed-off-by: ..."`. Same
   single-commit safety guard as the existing helper (multi-commit
   warns and skips — `git rebase`-based history rewriting is too
   destructive for a backstop). The prompt is the primary defense in
   the multi-commit shape; the Reviewer-on-impl flags any slip.

The amend ordering matters: `_ensure_signoff_trailer` runs *before*
`_sanitize_head_commit_auto_close` so the auto-close strip's diff is
computed against the message that already has the trailer. Reversing
the order would mean the auto-close strip's amend overwrites the
trailer the previous amend just added. Both helpers no-op on the
clean path.

**Fixer (§3, cont.).** The Fixer-on-impl follows the same shape as
the Worker — read `packages/foreman/src/foreman/roles/fixer.py` to
locate its `provider.run_agent` call and the corresponding env-dict
construction, then apply the same env-injection + post-amend helper.
The Fixer-on-spec runs against the spec branch and uses
`commit_files_to_worktree`-style Python commits where applicable —
the same Planner-style `signoff_trailer` parameter pass-through
covers it. The Worker reads the file before drafting because the
v4 Fixer dispatcher details may differ from the Worker's shape; the
spec authorizes "mirror the Worker pattern using whichever commit
surface the Fixer actually uses" rather than prescribing exact line
numbers.

**Prompts (§6).** Add a `<dco_signoff>` section to
`packages/foreman/src/foreman/prompts/worker.md` (next to the existing
`<commit_message_guardrails>` block around line 274) instructing the
LLM to always pass
`--trailer "Signed-off-by: $FOREMAN_OPERATOR_NAME <$FOREMAN_OPERATOR_EMAIL>"`
to every `git commit` call. Equivalent section added to
`packages/foreman/src/foreman/prompts/fixer.md` (the impl-side body,
near its `<commit_discipline>` block around line 171). Frame both as:
"Python amends HEAD with the trailer after you return if missing; this
prompt instruction is the primary defense — the runtime amend is the
backstop." Mirrors the auto-close prompt+runtime layering already in
place.

**Container template (§7).** Add an `[operator]` block to
`docker/foreman/config.toml.template` between `[orchestrator]` (line
32-34) and `[[projects]]` (line 36+). Shape:

```toml
[operator]
name = "${FOREMAN_OPERATOR_NAME}"
email = "${FOREMAN_OPERATOR_EMAIL}"
```

The two env vars get expanded by the existing `envsubst` pipeline in
`docker/entrypoint.sh:74` ("envsubst < ... > $FOREMAN_V4_CONFIG").
Operator sets them in `.env` alongside `FOREMAN_PLANNER_APP_ID` etc.

**Docs (§8).** New section in `docs/RUNBOOK.md` near the existing
"Pre-commit hooks" / "Import-graph boundaries" sections, titled
"Operator identity (DCO sign-off)". Documents (a) the `[operator]`
schema, (b) the per-project override shape, (c) the
`FOREMAN_OPERATOR_NAME` / `FOREMAN_OPERATOR_EMAIL` env vars consumed
by `envsubst` at container start, (d) one-line rationale + a link to
PR #346 and the Linux kernel coding-assistants policy the pattern
follows.

## Sub-requests (topologically sorted)
1. Add `OperatorConfig(BaseModel)` to `packages/foreman/src/foreman/v4/config.py`
   with required `name: str` and `email: str`, `extra="forbid"`,
   email-shape `field_validator`.
2. Hang required `operator: OperatorConfig` on `V4Config` in the same
   file (no default — load-time validation enforces presence).
3. Add optional `operator: OperatorConfig | None = None` to
   `ProjectConfig` in the same file.
4. Extend `load_config` so the `operator` key flows from raw TOML to
   the `V4Config.model_validate` payload, mirroring how `apps` and
   `orchestrator` are forwarded today
   (`packages/foreman/src/foreman/v4/config.py:213-220`).
5. Add module-level
   `resolve_operator(project: ProjectConfig, config: V4Config) -> OperatorConfig`
   in the same file. Pure function: returns
   `project.operator or config.operator`.
6. Add unit tests in `packages/foreman/tests/v4/test_config.py`:
   top-level operator parse, missing operator raises, missing `name`
   raises, missing `email` raises, malformed `email` raises (e.g.,
   `"not-an-email"`), per-project override parses, resolver returns
   override when set, resolver returns top-level when unset. Extend
   the existing `_APPS_TOML` shared fixture with an `_OPERATOR_TOML`
   sibling so existing tests pass.
7. Add `signoff_trailer: str | None = None` keyword-only parameter
   to `GitHostProvider.commit_files_to_worktree` in
   `packages/foreman/src/foreman/git_host.py:117` (Protocol-level).
8. Implement the parameter in
   `packages/foreman/src/foreman/git_hosts/github.py:128`: when
   `signoff_trailer is not None`, splice
   `"--trailer", f"Signed-off-by: {signoff_trailer}"` into the
   `git commit` invocation at line 158 (right after `"-m", message`).
9. Wire the resolver into the Planner: in
   `packages/foreman/src/foreman/roles/planner.py:372`, call
   `op = resolve_operator(project, config)` (with `config` plumbed
   through if not already in scope — read the function signature
   first) and pass
   `signoff_trailer=f"{op.name} <{op.email}>"` to
   `host.commit_files_to_worktree`.
10. Test the Planner trailer end-to-end: create a real worktree in
    `tmp_path`, call `commit_files_to_worktree` with a signoff_trailer,
    `git log -1 --pretty=%B` against the resulting HEAD, assert the
    `Signed-off-by:` line is present.
11. Plumb `FOREMAN_OPERATOR_NAME` and `FOREMAN_OPERATOR_EMAIL` into
    the Worker's `provider.run_agent` env dict in
    `packages/foreman/src/foreman/roles/worker.py:896` (the
    `env={**os.environ, "GH_TOKEN": worker_token}` dict gets two new
    keys derived from `resolve_operator(project, config)`).
12. Implement `_ensure_signoff_trailer(worktree_path, operator,
    commits_made_count, role_token)` in `worker.py`. Same shape as
    `_sanitize_head_commit_auto_close` (lines 197-310): zero-commit
    no-op, single-commit amend, multi-commit warn+skip. Uses
    `git commit --amend --no-edit --trailer "Signed-off-by:
    <name> <<email>>"` when the trailer is missing.
13. Call `_ensure_signoff_trailer` at line ~1018 (the `implemented`
    branch, between the `pr_title`/`pr_body` asserts and the existing
    `_sanitize_head_commit_auto_close` call — trailer add BEFORE
    auto-close strip so the strip's amend works against the
    final-shape message).
14. Unit tests for the Worker helpers: `_ensure_signoff_trailer`
    zero-commits no-op, single-commit-missing amend, single-commit-
    present no-op, multi-commit warn+skip. Mirror the
    `_sanitize_head_commit_auto_close` test structure in
    `packages/foreman/tests/test_worker.py` (or whichever test file
    covers the existing helper — read first).
15. Mirror the env injection + helper for the Fixer-on-impl path:
    read `packages/foreman/src/foreman/roles/fixer.py`, locate the
    `provider.run_agent` env construction + the commit pathway, and
    apply the same two-layer plumbing (env vars + post-amend).
16. Add `<dco_signoff>` section to
    `packages/foreman/src/foreman/prompts/worker.md` (next to
    `<commit_message_guardrails>` around line 274). Documents the
    `--trailer` shape using the env vars, names Python's
    belt-and-suspenders amend as the backstop, and (for symmetry
    with the auto-close guardrails) explicitly says "you should
    still write the trailer correctly; the runtime amend is a
    backstop, not a license to be sloppy."
17. Add the equivalent `<dco_signoff>` section to
    `packages/foreman/src/foreman/prompts/fixer.md` (impl-side
    body), near `<commit_discipline>` around line 171.
18. Add the `[operator]` block to
    `docker/foreman/config.toml.template` between `[orchestrator]`
    (line 32-34) and the first `[[projects]]` block (line 36+).
    Uses `${FOREMAN_OPERATOR_NAME}` and `${FOREMAN_OPERATOR_EMAIL}`
    envsubst placeholders (no quotes around the name placeholder;
    `${FOO}` resolves to the literal value and TOML accepts both
    plain and quoted strings).
19. Document the new section in `docs/RUNBOOK.md`. Add a new
    "Operator identity (DCO sign-off)" H2 section between the
    existing "Pre-commit hooks" and "Import-graph boundaries"
    sections. Includes: the `[operator]` schema, per-project
    override shape, env-var names consumed by envsubst, a one-line
    rationale linking to PR #346 and the Linux kernel
    coding-assistants policy.
20. Run `just check` and confirm `new_failures_count == 0`.

## File-level changes
| File | Change |
|------|--------|
| `packages/foreman/src/foreman/v4/config.py` | Add `OperatorConfig` (required `name` + `email`, email regex validator); hang required `operator` on `V4Config`; add optional `operator` on `ProjectConfig`; extend `load_config` to forward the `operator` key; add `resolve_operator(project, config)` pure function. |
| `packages/foreman/tests/v4/test_config.py` | New tests for parse, missing-fields, per-project override, resolver. Extend the shared `_APPS_TOML` fixture with an `_OPERATOR_TOML` sibling so existing tests continue to validate the now-required block. |
| `packages/foreman/src/foreman/git_host.py` | Add `signoff_trailer: str \| None = None` to the `commit_files_to_worktree` Protocol. |
| `packages/foreman/src/foreman/git_hosts/github.py` | Implement the new parameter — splice `--trailer "Signed-off-by: ..."` into the `git commit` invocation when set. |
| `packages/foreman/src/foreman/roles/planner.py` | Resolve operator and pass `signoff_trailer=...` to `commit_files_to_worktree`. |
| `packages/foreman/src/foreman/roles/worker.py` | Add `_ensure_signoff_trailer` helper; call it on the `implemented` branch BEFORE `_sanitize_head_commit_auto_close`; plumb operator env vars into the `provider.run_agent` env dict. |
| `packages/foreman/src/foreman/roles/fixer.py` | Mirror the Worker's env plumbing + post-amend on the Fixer-on-impl path. |
| `packages/foreman/tests/test_worker.py` (or wherever the existing `_sanitize_head_commit_auto_close` tests live) | New tests for `_ensure_signoff_trailer` covering zero/one/multi-commit cases. |
| `packages/foreman/src/foreman/prompts/worker.md` | New `<dco_signoff>` section documenting the `--trailer` pattern + the runtime amend as backstop. |
| `packages/foreman/src/foreman/prompts/fixer.md` | Same `<dco_signoff>` section for the impl-side body. |
| `docker/foreman/config.toml.template` | Add `[operator]` block with `${FOREMAN_OPERATOR_NAME}` / `${FOREMAN_OPERATOR_EMAIL}` placeholders between `[orchestrator]` and `[[projects]]`. |
| `docs/RUNBOOK.md` | New "Operator identity (DCO sign-off)" section. |

## Alternatives considered
- **Replace the role-bot `GIT_AUTHOR_*` env vars with the operator's
  identity instead of layering a trailer on top.** Rejected per the
  issue body's explicit out-of-scope: the bot is the legitimate author
  of the code generation; the DCO trailer is a *certification of
  provenance* by the human operator who dispatched the run. The
  author/committer attribution belongs to the bot; the sign-off belongs
  to the operator. Replacing the env vars would mis-attribute every
  commit and break the existing per-role bot identity model the v4
  IdentityRegistry (`packages/foreman/src/foreman/v4/identity.py`) is
  built around.
- **Use `git commit -s` after temporarily exporting
  `GIT_COMMITTER_NAME` / `GIT_COMMITTER_EMAIL` to the operator
  identity, then restoring.** Rejected: `-s` reads `user.name` /
  `user.email` from `git config`, not env vars. Doing this correctly
  would require either (a) temporarily writing `.git/config` (forbidden
  per the foreman#53 leak fix at
  `packages/foreman/src/foreman/git_hosts/github.py:113`) or (b)
  invoking with `-c user.name=... -c user.email=... commit -s`, which
  doubles the surface area vs. the explicit `--trailer` form. The
  explicit `--trailer` form is what the issue body recommends and is
  what we ship.
- **Have only the Worker carry the trailer; let the Planner's spec
  doc commit stay unsigned and the DCO gate ignore spec PRs.**
  Rejected: DCO is a property of the merge target (the protected
  branch), not of the role. A protected-branch DCO check that ignores
  spec PRs is fragile (one bot commit category gets a per-PR-shape
  exemption baked into branch protection), and PR #346's test setup
  validates the gate against every bot PR. Plumbing operator through
  all three commit paths (Planner / Worker / Fixer) is the principled
  fix.
- **Make `[operator]` optional with a sentinel default (e.g.,
  `"foreman-default <bot@noreply.github.com>"`) so existing test
  fixtures don't all need updating.** Rejected: a default would let
  the daemon boot without a real operator identity, which is exactly
  the failure mode DCO is supposed to prevent. The shared `_APPS_TOML`
  fixture gets a sibling `_OPERATOR_TOML` block — one well-named
  constant, used everywhere — so the test churn is bounded.
- **Skip the per-project override entirely (top-level operator only).**
  Rejected: the issue body explicitly requires per-project override
  for the case where one of foreman's managed projects has a different
  maintainer than the rest. The override field is one optional pydantic
  field; ignoring the requirement would be cheaper to implement now
  but force a schema migration later.
- **Use the `commit-msg` hook to inject the trailer instead of
  Python-side amend.** Rejected: the worktree's `.git/hooks/` directory
  shares the parent repo's hookspath (foreman#53 leak family — same
  reason we don't write `user.name` to `.git/config`). Writing into
  the hooks directory would leak to subsequent human commits in the
  same worktree.

## Open questions
- None blocking. Two judgment calls are decided in the spec rather
  than left open: (a) the spec WIDENS the issue's stated scope from
  "Worker only" to "all three role bots that commit" because DCO
  gates are PR-wide; (b) the spec uses the explicit `--trailer` form
  (rather than `-s`) per the issue body's stated preference.

## Out of scope
- Adding DCO enforcement to branch protection — that's a separate
  decision once bot PRs reliably pass the non-blocking gate from
  PR #346 (per issue body).
- Refactoring how role-bot `GIT_AUTHOR_*` env injection works — that
  stays as-is (per issue body).
- Merging the unstaged `CONTRIBUTING.md` draft at repo root — that
  lands once the full DCO arc is verified (per issue body).
- Adding DCO enforcement to release-please / `release.yml`.
- Validating the operator email against any external system (LDAP,
  GitHub-account-exists, MX-record check). The schema validates shape
  only; semantic validity is the operator's responsibility.
- Carrying the operator identity into the Reviewer's PR-review comments
  (the Reviewer doesn't commit; nothing to sign).
- A `[[projects.operator]]` resolution priority order beyond
  "project override → top-level → fail loud". No env-var fallback,
  no per-issue override, no per-role override.
- Bumping any V4Config schema version field. The schema doesn't
  currently carry a version field, and the substrate cutover
  (foreman#333) just landed v4 with no in-flight production state
  to migrate, so the new required block is enforced at first load
  post-deploy (per issue body).
