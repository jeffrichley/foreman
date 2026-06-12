# Spec: D3 catalogue — delete vestigial AdminConfig (issue #303)

## Goal

`AdminConfig` (defined at `packages/foreman/src/foreman/config.py:44-47`) carries one field — `github_token_env: str = "FOREMAN_ADMIN_TOKEN"` — that no production code reads. It's a leftover from the pre-GitHub-App era; today's admin-PAT lookup in `cli.py:421-425` hard-codes the env-var name directly. Delete the class, the `Config.admin` field that holds it, every test fixture that mentions it, and the lone comment that name-drops it. Cleanup-only; no behavior change. See issue [#303](https://github.com/jeffrichley/foreman/issues/303) and Decision 3 catalogue line 291 of `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md`. Same family as the companion D3 deletions already shipped today: #299 (`_summarize_failures`), #300 (vulture wiring), #302 (`WorktreeManager.cleanup`).

## Acceptance criteria

- `packages/foreman/src/foreman/config.py` no longer defines `class AdminConfig` (currently lines 44-47). The class is removed entirely.
- `packages/foreman/src/foreman/config.py:559` — the line `admin: AdminConfig = Field(default_factory=AdminConfig)` on `class Config` — is removed. No replacement field; pydantic's default `extra='ignore'` policy (no `model_config = ConfigDict(extra="forbid")` is set on `Config`, verified) means any operator who left `[admin]` in their existing `~/.foreman/config.toml` will see it silently ignored at load time, not a validation error.
- `packages/foreman/src/foreman/cli.py:417-420` — the four-line comment block above the `admin_token = ...` lookup that name-drops `AdminConfig` — is reworded to drop the dangling reference while keeping the env-var fallback explanation. Suggested rewrite (preserves the operator-facing semantics — `FOREMAN_ADMIN_TOKEN` is still the documented default, with `GH_TOKEN` / `GITHUB_TOKEN` as `gh`-parity fallbacks):
  ```python
  # Admin client uses the operator's PAT for label creation. The
  # default env var is ``FOREMAN_ADMIN_TOKEN``; operators can still
  # set ``GH_TOKEN`` / ``GITHUB_TOKEN`` for parity with ``gh``.
  ```
- `packages/foreman/tests/test_config.py` (line 17-18 inside the module docstring's example, lines 525, 538, 551, 564, 576, 590, 874, 884, 897, 909, 919, 930, 942-943): all 13 fixture sites + the 1 docstring reference are scrubbed. Per-site treatment:
  - **Standalone-`[admin]` fixtures** (lines 525, 874, 909, 930): `config_path.write_text('[admin]\ngithub_token_env = "..."\n')` becomes `config_path.write_text("")`. An empty TOML file parses to `{}` via `tomllib.load`, which `Config.model_validate({})` accepts using `default_factory` for every field. The tests' assertions (which target defaults, NOT the `admin` field) stay green.
  - **Concat-with-other-sections fixtures** (lines 538, 551, 564, 576, 590, 884, 897, 919, 942-943): the leading `'[admin]\ngithub_token_env = "X"\n'` line is dropped from the implicit-string-concat tuple. The remaining `[daemon]` / `[orchestrator]` / `[projects.*]` sections — which are what the test is actually exercising — are preserved verbatim.
  - **Triple-quoted-block docstring example at lines 17-18** (the module docstring's worked example, NOT a runtime fixture): the two lines `[admin]\ngithub_token_env = "FOREMAN_ADMIN_TOKEN"` are deleted from the docstring's example TOML block. The blank line after them is also deleted so the example reads cleanly straight into `[projects.voice]`.
- `packages/foreman/tests/test_cli.py` (lines 548, 566, 593, 625, 664, 714, 766, 846, 873, 906, 1084, 1130, 1161, 1195, 1225, 1315, 1347, 1437, 1473, 1508, 1573, 1593, 1629): all 23 fixture sites are scrubbed. Each is the same shape — an f-string `f'[admin]\ngithub_token_env = "X"\n'` line at the head of a `config_path.write_text(...)` implicit-string-concat block. Drop that ONE line from each block; preserve every subsequent `f'[daemon]\n...'` / `f'[projects.*]\n...'` line untouched. After the drop the f-prefix on the next line stays (it carries `{tmp_path}`-style interpolations). No test assertions reference the `admin` field, so no further changes per test are needed.
- `packages/foreman/tests/test_daemon.py`: drop `AdminConfig` from the import at line 12 (becomes `from foreman.config import AppsConfig, Config, DaemonConfig, ProjectConfig`); drop the `admin=AdminConfig(),` line from the `_config()` helper at line 48.
- `packages/foreman/tests/test_daemon_e2e.py`: drop `AdminConfig` from the import at line 17; drop the `admin=AdminConfig(),` line from the `_config()` helper at line 83.
- `packages/foreman/tests/test_daemon_runners.py`: drop `AdminConfig` from the import at line 11; drop the `admin=AdminConfig(),` line from the `_config()` helper at line 27.
- `packages/foreman/tests/test_role_dispatch.py`: drop `AdminConfig` from the import at line 10; drop the `admin=AdminConfig(),` line from the `_config()` helper at line 26.
- A repo-wide post-edit grep for `AdminConfig` returns ZERO matches across `packages/foreman/src/` and `packages/foreman/tests/`. A repo-wide grep for the literal substring `github_token_env` returns ZERO matches across `packages/foreman/src/`, `packages/foreman/tests/`, AND `docs/` (the issue body confirms no production reads; this AC pins that no doc still references the removed field either — Worker should grep `docs/` and silently update any stale references it finds, but per "Out of scope" no doc-tone rewrites beyond the literal-string removal).
- `just check` exits 0 on the impl worktree: ruff clean, mypy clean, full pytest suite green. The full-suite test count stays exactly where it was on `main` at the time the Worker branches (the issue body states 1088; the Worker should record the actual pre-edit count in the impl PR body and confirm it's unchanged post-edit — only fixture stanzas are removed, no test functions deleted).
- The impl PR uses a `chore(config):` conventional-commit prefix (matches sibling D3 PRs #299 / #300 / #302). Suggested title: `chore(config): delete vestigial AdminConfig + tests`. Subject must NOT start with an uppercase letter per `CLAUDE.md:36`. The PR body references issue #303 plainly — NO closing-keyword references (per foreman#63; the merge gate lives in the daemon's close-out, not in PR auto-close).

## Approach

Per `CLAUDE.md`'s Decision-4 calibrated bias toward structural patterns: **no GoF pattern applies and no Google engineering principle (SRP / OCP / DIP / "make the right thing easy") meaningfully fits.** This is straightforward dead-code removal — a class whose ONE field is never read, plus the test fixtures that exist only to make the obsolete class parse-able. Naming it as a pattern would be pattern-fishing.

The mechanical shape: one class deletion (config.py:44-47), one field deletion (config.py:559), one comment reword (cli.py:417-420), 41 test fixture sites scrubbed across six test files, plus four `from foreman.config import ...` lines that need `AdminConfig` removed from their import list. Sub-requests are ordered for safe stepwise verification: src first (the deletion that makes the field unreachable), then each test file (in order of how cheap they are to verify in isolation), then a full `just check` at the end. Each step keeps the suite green — the field had `default_factory=AdminConfig`, so removing it is a strict subset of the model and pydantic's `extra='ignore'` default means leftover `[admin]` TOML doesn't fail validation; removing the fixtures is independent housekeeping.

The issue body's scope description undercounted by missing four test files (`test_daemon.py`, `test_daemon_e2e.py`, `test_daemon_runners.py`, `test_role_dispatch.py`) that explicitly `import AdminConfig` and pass `admin=AdminConfig()` to `Config(...)`. These four MUST be cleaned up because deleting the `Config.admin` field removes the kwarg — pydantic accepts the extra-kwarg call silently (per `extra='ignore'`) but the dangling `AdminConfig` symbol in the import becomes an `ImportError` at module-load time and the test file fails to even collect. The Acceptance criteria above name each of the four explicitly so the Worker can't miss them.

The "1088 tests stay at 1088" check is the ground-truth signal that nothing was over-deleted. If the count drops, the Worker accidentally removed a test function instead of a fixture stanza — escalate to `foreman:needs-help` rather than amending the AC.

## Sub-requests (topologically sorted)

1. **Delete `class AdminConfig`** from `packages/foreman/src/foreman/config.py:44-47` (the entire 4-line block including the docstring). Preserve the surrounding blank lines so the file's spacing stays clean.

2. **Delete the `admin: AdminConfig = Field(default_factory=AdminConfig)` field** on `class Config` at `packages/foreman/src/foreman/config.py:559`. Single line.

3. **Reword the comment block** at `packages/foreman/src/foreman/cli.py:417-420` to drop the `AdminConfig` name-drop. Use the suggested rewrite from the Acceptance criteria (three lines instead of four).

4. **Scrub `packages/foreman/tests/test_config.py`**:
   - Lines 17-18 (inside the module docstring's example): delete the two lines starting `[admin]` and `github_token_env = "FOREMAN_ADMIN_TOKEN"` plus the trailing blank line so the example reads from the file's docstring straight into `[projects.voice]`.
   - Standalone-`[admin]` fixture lines (525, 874, 909, 930): replace `config_path.write_text('[admin]\ngithub_token_env = "..."\n')` with `config_path.write_text("")`. Empty file → `tomllib.load` returns `{}` → `Config.model_validate({})` uses every field's `default_factory`. The tests' assertions (daemon defaults at 527-532; orchestrator defaults at 876-878; orchestrator-resolve-raises path at 911-913; orchestrator-private-key-raises path at 932-933) don't touch the removed field and stay green.
   - Concat-with-other-sections fixtures (538, 551, 564, 576, 590, 884, 897, 919, 942-943): drop ONLY the `'[admin]\ngithub_token_env = "..."\n'` line from the implicit-string-concat tuple. Preserve every other line untouched. Special case for lines 942-943: this is a two-line `[admin]` block (`"[admin]\n"` then `"github_token_env = \"FOREMAN_ADMIN_TOKEN\"\n"`); drop BOTH lines.

5. **Scrub `packages/foreman/tests/test_cli.py`** (23 sites): at each of lines 548, 566, 593, 625, 664, 714, 766, 846, 873, 906, 1084, 1130, 1161, 1195, 1225, 1315, 1347, 1437, 1473, 1508, 1573, 1593, 1629 — delete the single line `f'[admin]\ngithub_token_env = "X"\n'` from the `config_path.write_text(...)` block. The f-prefix on the surrounding lines stays (it carries `{tmp_path}`-style interpolations). All 23 sites have the same shape; do them in source order to keep the diff readable.

6. **Scrub `packages/foreman/tests/test_daemon.py`**: (a) at line 12 change `from foreman.config import AdminConfig, AppsConfig, Config, DaemonConfig, ProjectConfig` to `from foreman.config import AppsConfig, Config, DaemonConfig, ProjectConfig`. (b) delete the `admin=AdminConfig(),` line at line 48 inside the `_config()` helper's `return Config(...)` call.

7. **Scrub `packages/foreman/tests/test_daemon_e2e.py`**: same shape as #6 — drop `AdminConfig` from import line 17; drop `admin=AdminConfig(),` line at line 83.

8. **Scrub `packages/foreman/tests/test_daemon_runners.py`**: same shape as #6 — drop `AdminConfig` from import line 11; drop `admin=AdminConfig(),` line at line 27.

9. **Scrub `packages/foreman/tests/test_role_dispatch.py`**: same shape as #6 — drop `AdminConfig` from import line 10; drop `admin=AdminConfig(),` line at line 26.

10. **Repo-wide verification grep**: `grep -rn 'AdminConfig\|github_token_env' packages/foreman/ docs/` returns ZERO matches in `packages/foreman/`. Any match in `docs/` is a stale reference — quietly delete the literal string from the doc; do NOT undertake a doc rewrite (per Out of scope).

11. **Run `just check`**: ruff clean, mypy clean, full pytest suite green. Record the test count before and after the diff (issue body claims 1088); the count must be identical.

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/src/foreman/config.py` | Delete `class AdminConfig` (lines 44-47) AND the `admin: AdminConfig = Field(default_factory=AdminConfig)` field on `Config` (line 559). |
| `packages/foreman/src/foreman/cli.py` | Reword the 4-line comment block at lines 417-420 to drop the `AdminConfig` name-drop while preserving the `FOREMAN_ADMIN_TOKEN` / `GH_TOKEN` / `GITHUB_TOKEN` env-var explanation. |
| `packages/foreman/tests/test_config.py` | Strip the docstring example at lines 17-18 + 13 runtime fixture sites. Standalone-`[admin]` fixtures become `write_text("")`; concat fixtures lose only the `[admin]` line. |
| `packages/foreman/tests/test_cli.py` | Delete the 23 `f'[admin]\ngithub_token_env = "X"\n'` lines from the named line numbers; preserve every surrounding f-string concat-line. |
| `packages/foreman/tests/test_daemon.py` | Drop `AdminConfig` from the import at line 12; drop `admin=AdminConfig(),` from the `_config()` helper at line 48. |
| `packages/foreman/tests/test_daemon_e2e.py` | Drop `AdminConfig` from the import at line 17; drop `admin=AdminConfig(),` from the `_config()` helper at line 83. |
| `packages/foreman/tests/test_daemon_runners.py` | Drop `AdminConfig` from the import at line 11; drop `admin=AdminConfig(),` from the `_config()` helper at line 27. |
| `packages/foreman/tests/test_role_dispatch.py` | Drop `AdminConfig` from the import at line 10; drop `admin=AdminConfig(),` from the `_config()` helper at line 26. |

No expected changes to:

- `packages/foreman/src/foreman/identity.py`, `init.py` — their use of the word "admin" refers to the operator's role (admin PAT for label creation), not to the deleted `AdminConfig` class. They stay as-is.
- `packages/foreman/tests/test_init.py` — its many `admin = _FakeAdminClient(repo=fake_repo)` lines are a local variable referring to the github client (PAT-based), not the deleted pydantic model. They stay as-is.
- Any other config models (`OrchestratorConfig`, `DaemonConfig`, `ReconcilerConfig`, `AppsConfig`, `ProjectConfig`). Only `AdminConfig` is vestigial; the rest are wired into production paths.
- `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md` — the catalogue entry stays as historical record; this PR's merge commit + CHANGELOG line carry the propagation status (matches the pattern of #299 / #300 / #302).

## Alternatives considered

- **Leave the `Config.admin` field and only delete the `AdminConfig` class body, replacing it with `class AdminConfig(BaseModel): pass`.** Smaller diff (one line struck instead of two), preserves operator forwards-compat (existing `[admin]` blocks in `~/.foreman/config.toml` files keep parsing into an empty model). Rejected because the issue explicitly says "delete + remove tests + remove TOML fixtures" — the goal is full vestige removal, not minimal vestige removal. And pydantic's default `extra='ignore'` already gives operators the forwards-compat: a leftover `[admin]` block in a config file is silently ignored at the top level once `Config.admin` is gone.

- **Add a deprecation warning emitted by `load_config` when `raw` contains an `admin` key.** Rejected as overengineering for a field nothing reads. Operators don't need a warning to remove a config block whose only purpose was decorative. The catalogue calls it vestigial — i.e., already dead — so a deprecation phase is moot.

- **Bulk-delete `AdminConfig` AND the v2 dead-island candidates (`daemon_host.py:101` `get_issue_labels`, `daemon_runners.py:50`) in the same PR.** Rejected per the issue body's explicit "Out of scope" — those are part of the closed v2 dead island and belong in foreman#301's reachability sweep, not piecemeal. Keeping this PR scoped to one catalogue entry keeps the diff easy to review.

- **Rewrite the 23 test_cli.py fixtures to share a common helper that builds the TOML.** Tempting because the fixtures are clearly cut-and-paste, but rejected — that's a separate refactor (cleanup-of-cleanup), not in scope for the D3 catalogue work. The issue asks for fixture removal, not fixture deduplication. Adding it here muddies the diff and trips reviewers on something unrelated to the vestige.

- **Use `replace_all` in the Edit tool to scrub all 23 test_cli.py instances in one shot.** Looks efficient, but rejected because the surrounding `write_text` blocks vary slightly between sites (different `tmp_path` interpolations) — a global replace risks silently corrupting an adjacent line on a site with unusual whitespace. Per-site deletion is the safe move at 23 sites; the Worker does them in source order.

## Open questions

(None. The issue body specifies the deletion in detail, names the file + line ranges, and the empirical verification (`grep` shows ONE definition, ZERO production reads) was reproduced during the spec build. Pydantic's `extra='ignore'` default for `Config` was verified by inspection — no `model_config = ConfigDict(extra="forbid")` is set, so existing `[admin]` blocks in operator config files will be silently ignored rather than raising at load time. The four test-file gap in the issue body's scope description is closed by explicit ACs above. The PR-title convention matches the sibling D3 deletions shipped today.)

## Out of scope

- **The other D3 catalogue entries (`daemon_host.py:101` + `daemon_runners.py:50` `get_issue_labels`).** Per the issue body's Out-of-scope list; those are part of foreman#301's reachability sweep, not piecemeal removal.
- **Operator-facing migration documentation / CHANGELOG copy beyond what `release-please` autogenerates.** The CHANGELOG entry produced by the `chore(config):` prefix is sufficient; no separate doc-update PR.
- **Tightening `Config.model_config` to `extra="forbid"`** so a leftover `[admin]` block raises a validation error at load time. Sounds righteous, but rejected — it would break every existing operator config file that still has the dead `[admin]` block, and the cost (operator must edit their config) is paid for zero benefit (the block has been doing nothing for months). Stay with pydantic's silent-ignore default.
- **Deduplicating the 23 test_cli.py fixture-builder blocks** into a shared helper. Separate refactor; not in scope here.
- **A repo-wide rewrite of any `docs/` mention of `AdminConfig`** beyond the literal-string scrub in AC #10. The catalogue entry in the architecture stability plan stays as historical record (matches #286 / D4's "merged-PR + CHANGELOG carry propagation" pattern).
- **Removing the `FOREMAN_ADMIN_TOKEN` env-var fallback in `cli.py:421-425`.** The env var stays — it's the documented PAT lookup for `foreman init`. Only the dead `AdminConfig.github_token_env` field is removed.

## References

- foreman#303 — this ticket. Names the vestigial field + verifies it empirically.
- `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md` Decision 3, catalogue line 291 — the source-of-truth catalogue entry.
- foreman#299 / PR #299 — sibling D3 deletion (`worker._summarize_failures`). Same `chore(<scope>): delete vestigial …` shape.
- foreman#300 / PR #300 — sibling D3 wiring (`vulture` + `[tool.vulture]` config). Adds the linter that surfaces future vestiges.
- foreman#302 / PR #302 — sibling D3 deletion (`WorktreeManager.cleanup` + tests). Same shape; reviewer should weight this spec against that PR's diff size for sanity.
- foreman#301 — tracking ticket for the v2 reachability sweep + R3 contract; explicitly NOT touched by this PR.
- foreman#63 — issue close-out gating; rationale for the no-closing-keyword constraint on the impl PR body.
- Source pointers used by this spec:
  - `packages/foreman/src/foreman/config.py:44-47` — `AdminConfig` class to delete.
  - `packages/foreman/src/foreman/config.py:559` — `admin: AdminConfig = Field(...)` field to delete.
  - `packages/foreman/src/foreman/cli.py:417-420` — comment block to reword.
  - `packages/foreman/src/foreman/cli.py:421-425` — admin-PAT env-var lookup; reworded comment must preserve this surface.
  - `packages/foreman/tests/test_config.py:17-18, 525, 538, 551, 564, 576, 590, 874, 884, 897, 909, 919, 930, 942-943` — all fixture sites named individually so the Worker can grep them by line number.
  - `packages/foreman/tests/test_cli.py:548, 566, 593, 625, 664, 714, 766, 846, 873, 906, 1084, 1130, 1161, 1195, 1225, 1315, 1347, 1437, 1473, 1508, 1573, 1593, 1629` — the 23 sites.
  - `packages/foreman/tests/test_daemon.py:12, 48` — import + helper site.
  - `packages/foreman/tests/test_daemon_e2e.py:17, 83` — import + helper site.
  - `packages/foreman/tests/test_daemon_runners.py:11, 27` — import + helper site.
  - `packages/foreman/tests/test_role_dispatch.py:10, 26` — import + helper site.
