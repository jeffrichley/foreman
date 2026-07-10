# Contributing to foreman

Thanks for considering a contribution. Whether you spotted a bug, want to suggest a feature, or are sending a fix — this short guide covers what you need to know.

Foreman is an AI-augmented PR automation tool maintained by [@jeffrichley](https://github.com/jeffrichley). It dogfoods itself: many PRs in this repo's history were opened by foreman's own roles (Planner / Reviewer / Worker). We disclose every one. We expect the same from you.

## Quick start

```bash
git clone https://github.com/jeffrichley/foreman.git
cd foreman
uv sync --all-extras --dev
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
just check    # ruff + mypy + lint-imports + pytest — must be green before PR
```

Need `just`? `cargo install just` (with a Rust toolchain) or `brew install just` (macOS). It is the single source of truth for the quality gate — run it before every push.

## How to contribute

- **Bug?** Open an issue with a minimal repro and the error class you see. Don't apply any `foreman:*` labels yourself — those are daemon-owned.
- **Feature?** Open an issue first if the change touches multiple files or shifts an interface. Small fixes can go straight to PR.
- **Fix?** Open a PR. Be ready for a review — the reviewer might be human, or might be a foreman role with a `[bot]` suffix. The standards are the same.

## Development workflow

### Pre-PR checklist

- [ ] `just check` is green locally
- [ ] PR title follows conventional commits, lowercase subject. Allowed types: `feat | fix | docs | chore | refactor | test | style | build | ci | perf | revert`
- [ ] If touching `foreman.v4.*`, no new imports from v3 substrate modules (`foreman.reconciler`, `foreman.daemon`, etc.) — `lint-imports` enforces this
- [ ] If an AI agent wrote or substantially edited the patch, see the **AI disclosure** section below

### What gates check what

| Tool | What it catches | Run via |
|---|---|---|
| `ruff check` | lint (style, unused imports, etc.) | `just lint` / `just fix` |
| `ruff format --check` | formatting (canonical whitespace / trailing commas) | `just format-check` / `just fix` |
| `mypy` | type errors in `packages/foreman/src` | `just typecheck` |
| `lint-imports` | architecture boundaries (R1: prod can't import tests; R2: v4 can't import v3 substrate) | `just import-lint` |
| `pytest` | tests | `just test` |
| `gitleaks` (pre-commit) | secret leaks | runs at `git commit` |

### CI for external contributors

First-time external PRs require a maintainer to **approve workflow runs** on each push — that's GitHub's standard safety for forks. Expect a short wait after each push for the green light before CI starts. Subsequent pushes from the same PR may also need re-approval.

## AI disclosure policy

Foreman is an AI tool, and using AI agents (Claude Code, Cursor, Copilot, foreman's own roles, etc.) to author PRs is welcome here. The policy uses **three commit trailers**, each with a distinct meaning. Only one (`Signed-off-by:`) is currently CI-enforced.

| Trailer | Who it names | Required? | Meaning |
|---|---|---|---|
| `Co-Authored-By:` | The AI agent + model | Recommended for human-authored AI-assisted commits | Recognition that an LLM materially contributed alongside the human author |
| `Supervised-by:` | The human who orchestrated the AI run | Required for **foreman-driven** commits; optional otherwise | Attribution for the human accountable for dispatching the autonomous run |
| `Signed-off-by:` | The human who reviewed + accepts responsibility | **Required on every commit** (CI-enforced) | DCO attestation per the [Developer Certificate of Origin](https://developercertificate.org/) |

### When you're a contributor using AI assistance

You're the commit author. The LLM helped. Use this shape:

```
feat(parser): support quoted CSV values

Co-Authored-By: Claude <noreply@anthropic.com>
Signed-off-by: Your Name <you@email>
```

The `Co-Authored-By:` form follows GitHub's [commit co-author convention](https://docs.github.com/en/pull-requests/committing-changes-to-your-project/creating-and-editing-commits/creating-a-commit-with-multiple-authors) so the agent shows up in the contributors view. Pick whichever email the agent's vendor publishes (`noreply@anthropic.com`, `copilot@github.com`, etc.) or leave the email as `<noreply@…>` if you're not sure. The point is honest disclosure, not strict provenance.

### When the commit is foreman-driven

Commits opened by `foreman-planner[bot]`, `foreman-reviewer[bot]`, `foreman-fixer[bot]`, or `foreman-worker[bot]` are already authored by the bot (GitHub's commit author is the bot identity), so `Co-Authored-By:` is redundant. Instead, foreman emits:

```
feat(parser): support quoted CSV values

Supervised-by: Wren <wren@…>
Signed-off-by: Jeff Richley <jeffrichley@gmail.com>
```

The `supervisor` is the human (or being) who dispatched the autonomous run; the `signer` is the human accepting DCO responsibility. They can be the same person — when they are, both trailers point to that person and `[operator.supervisor]` and `[operator.signer]` in `~/.foreman/config.toml` carry identical name/email values. Per-project overrides are supported; see [`docs/RUNBOOK.md`](docs/RUNBOOK.md) for the schema.

### The golden rule

Borrowed from [curl](https://curl.se/dev/contribute.html): an AI-assisted PR must be worth *more* to a reviewer than a human-written one of the same scope. If your prompt produced low-signal output, fix the prompt — don't send the slop. Drive-by PRs of any kind, AI-assisted or not, are not welcome.

### Signing a commit

`git commit -s` appends the `Signed-off-by:` trailer using the `user.name` / `user.email` from your git config. If you forgot `-s` on one or more commits already on your branch, the fastest recovery is `foreman contrib sign-commits` (or `foreman contrib check-signoff` to dry-run + see which commits are missing the trailer). For a single-commit amend without rebase: `git commit --amend -s --no-edit`. The DCO CI check fails the PR if any commit (except synthetic merge commits) lacks it.

## Project shape

- **Solo maintainer**: [@jeffrichley](https://github.com/jeffrichley). High-velocity development; responses are best-effort.
- **AI-augmented**: the autonomous loop is the normal mode of development on this repo. Don't be surprised if a `foreman-*[bot]` opens, reviews, or fixes a PR alongside yours.
- **Opinionated**: there's an architecture stability plan under `docs/superpowers/plans/`. New features are expected to justify their design choice in terms of named patterns or explicit "no pattern applies" reasoning.

## License

By contributing, you agree your contribution is licensed under the project's [MIT License](LICENSE.txt).

---

*If your contribution stalls on a question this doc didn't answer, open an issue with the `question` label.*
