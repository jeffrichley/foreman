# Vendored superpowers skills

These files are copied verbatim from the superpowers Claude Code plugin
and bundled into Foreman's role prompts so the role LLMs share the
discipline that superpowers gives interactive Claude Code sessions.

**Source:** `superpowers` plugin, version **5.1.0**
**Upstream path:** `~/.claude/plugins/cache/claude-plugins-official/superpowers/5.1.0/skills/<skill>/SKILL.md`

**Vendored skills:**

| Skill | Consumed by |
|---|---|
| `writing-plans.md` | Planner |
| `requesting-code-review.md` | Reviewer |
| `receiving-code-review.md` | Fixer |
| `test-driven-development.md` | Worker |
| `executing-plans.md` | Worker |
| `verification-before-completion.md` | Worker |
| `finishing-a-development-branch.md` | Worker |

## Refresh protocol

When superpowers ships a new version:

1. Update the version pin above.
2. Re-copy each `SKILL.md` from the new upstream path.
3. Run the full test suite. The tests pin signature lines from each
   vendored file, so any drift in upstream wording surfaces here.
4. Diff-review the changes before commit — superpowers may have added
   patterns Foreman should adopt, or removed ones the role prompts
   depend on.

## Why vendor (not import)?

The `claude-agent-sdk` Foreman uses to drive role LLMs does not (yet)
support loading Claude Code skills as a runtime dependency. Until that
changes, vendoring is the simplest path that gets every role the same
discipline an interactive Claude Code user has by default.
