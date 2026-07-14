# Ticket Dependency Graph — Design Spec

> Design for foreman#524. Brainstormed with Jeff 2026-07-14. Scope deliberately
> narrowed from the original epic-hierarchy framing to **execution-ticket
> dependencies only**.

**Goal:** Teach foreman to gate an execution ticket's dispatch on its GitHub-native
`blocked_by` dependencies — reading them from GitHub, keeping them in sync as they
change, and never running a ticket before its prerequisites are truly done.

**Architecture:** A **level-triggered reconciler** runs inside the existing poll loop.
Each poll it reads a tracked ticket's native `blocked_by` relations from GitHub,
converges the ticket's stored `depends_on` list to match (adds *and* removes), and the
existing frontier check (`list_unmet_dependencies`) gates dispatch. No new storage
model, no hierarchy, no prose parsing.

**Tech stack:** Python 3.12, foreman.v4 package, Postgres (`depends_on` JSONB column
already exists), PyGithub + raw `requester`/GraphQL for the dependencies API.

## Global Constraints

- **Graph only, no prose.** Dependencies come *exclusively* from GitHub's native issue
  **dependencies** API (`blocked_by`). No parsing of `Depends on #N` from bodies/comments.
- **Reconcile, don't append.** Ingestion converges `depends_on` to the GitHub truth every
  poll: add missing, **delete stale**. A removed GitHub relation must disappear from
  `depends_on` on a later poll with no manual cleanup.
- **Never plan unsolicited.** Discovering a dependency on an untracked issue must never
  auto-apply `foreman:plan` to it.
- **No new complexity budget.** Reuse the existing `depends_on` JSONB + `list_unmet_dependencies`
  frontier. No phantom/external nodes, no new state columns beyond what's already there.

## Locked decisions

1. **Scope = execution-ticket dependencies only.** Epic/theme *hierarchy* (sub-issues,
   roll-up, parent-driven fan-out) is explicitly **out of scope** — those are human
   planning tickets that never run through foreman. Parked as a separate follow-up.

2. **Source = GitHub native issue-dependencies** (`GET /repos/{o}/{r}/issues/{n}/dependencies/blocked_by`,
   confirmed HTTP 200 / GA / readable by the daemon identity 2026-07-14). Sub-issues API
   is *not* used.

3. **`depends_on` stays the storage — unchanged shape.** The existing `tickets.depends_on`
   `list[int]` (JSONB) holds **same-project** issue numbers. No shape change for v1:
   cross-project keying and a per-entry `source` tag are **deferred** (see Non-goals) —
   there is no manual-dep or cross-repo workflow today, so nothing to key or protect. No
   new `ticket_edges` table.

4. **Dependency "met" = the dep issue is CLOSED-as-COMPLETED.** Not "foreman ran it to
   Done" — GitHub issue state is the source of truth, so deps satisfied by a human, other
   tooling, or a different repo all resolve uniformly. Closed-as-**not-planned** does NOT
   satisfy a dependency (stays blocked-and-surfaced).

5. **Untracked dependency ⇒ HOLD, shown as `(untracked)`.** If ticket #2 depends on #1 and
   #1 is not a foreman ticket, #2 **holds** (does not dispatch) until #1 is closed-completed.
   `foreman show`/`ps` renders `relies on #1 (untracked)`. "untracked" is computed for free
   (no ticket row exists for #1) — no new stored state. Auto-clears on the next poll once
   #1 closes-completed. Foreman never auto-plans #1.

6. **Dependency cycle ⇒ needs-help, loudly.** If two *tracked* tickets mutually block
   (#1↔#2), foreman refuses to run a deadlocked graph: it flags **both** into needs-help
   with reason `dependency cycle: #1 ↔ #2`, surfaced via the normal needs-help path. It
   does **not** silently drop an edge and does **not** silently deadlock. Self-heals when a
   human removes one relation. (Only detectable when both ends are tracked; an untracked end
   just holds per #5.)

7. **Reconciler owns `depends_on` (full-replace convergence).** Since there is no manual-dep
   workflow, the GitHub reconciler owns the whole `depends_on` list: each poll it replaces
   it with the current `blocked_by` set from GitHub. Add *and* remove are inherent in a
   full replace — no diff/merge logic, no source tag needed. (When a manual-dep workflow is
   introduced later, re-add per-entry `source` scoping so manual edges survive — deferred.)

## Model

Storage stays the existing `tickets.depends_on` `list[int]` (JSONB), unchanged — no new
columns, no entry-shape change. Entries are same-project issue numbers. `depends_on`
remains the enforced frontier input; only the *population* (reconciler) and the *"met"
resolution* (`list_unmet_dependencies`) change.

## Reconciler (level-triggered, full-replace)

Runs in the existing poll cycle (`Poller._enqueue_open_tickets`, `poller.py`), per tracked
non-terminal ticket, before it is enqueued:

1. `desired ← GitProvider.read_blocked_by(project, issue)` — the ticket's native `blocked_by`
   issue numbers from GitHub.
2. `set_ticket_dependencies(ticket.id, desired)` — full replace.

Convergence is automatic: because step 2 overwrites the whole list, an added relation
appears next poll and a removed one disappears next poll. The mislabel→unlabel case
self-heals with zero diff logic.

## Frontier / scheduling (mostly existing)

`list_unmet_dependencies(ticket)` returns the deps that are not yet satisfied:

- For each `depends_on` entry, resolve the target issue's GitHub state.
- **Met** iff `state == closed AND state_reason == completed`.
- **Unmet** otherwise (open, or closed-as-not-planned).

A ticket is dispatchable iff `list_unmet_dependencies` is empty and it is not held. The
existing queue/frontier gate is reused; only the resolution rule changes.

Cycle check runs over the tracked-ticket dep graph during reconcile; on a cycle, both
tickets are moved to needs-help with the cycle reason (decision #6).

## GitProvider additions

- `read_blocked_by(project, issue) -> list[DepRef]` — native issue-dependencies read
  (REST `dependencies/blocked_by`, or GraphQL if cleaner). Added to the Protocol + Fake +
  PyGithub impl + routing provider.
- `get_issue_state(project, issue) -> str` **already exists** (returns `open`/`closed`), but
  does **not** expose `state_reason`. Extend the provider so the closed-**completed** vs
  closed-**not_planned** distinction is readable — either widen `get_issue_state` to return
  the reason too, or add a sibling `get_issue_state_reason(...)`. Decide in the plan
  (minimal blast radius; `get_issue_state` has existing call sites).
- **Write path (verify at plan time):** an `add_blocked_by(project, issue, blocked_by_issue)`
  so dependent tickets can be authored via API when filed. If no native write endpoint is
  readable by the daemon identity, fall back to authoring in the GitHub UI (read path is
  the load-bearing part).

## Display

- `foreman show <id>` and `foreman ps` render each unmet dependency: `relies on #N`
  (tracked) or `relies on #N (untracked)` (no foreman ticket for #N). Cyclic tickets show
  the `dependency cycle: #A ↔ #B` needs-help reason.

## Non-goals (explicit)

- Sub-issue **hierarchy** ingestion, epic **roll-up**, parent-driven **fan-out** — parked
  follow-up.
- Any **prose parsing** (`Depends on #N` in text).
- A **migration** of existing epics' task-list checkboxes — the fleet adopts native
  dependencies going forward; there are effectively zero today.
- A separate `ticket_edges` table — revisit only if the dependency graph gets dense.
- **Cross-project dependencies** (a dep targeting another repo) — deferred; v1 resolves deps
  against the ticket's own project. Re-add `(project, issue)` keying when a cross-repo dep
  is actually needed.
- **Per-entry `source` tag / manual-dep protection** — deferred; the reconciler owns
  `depends_on` (full-replace) because no manual-dep workflow exists. Re-add when one does.

## Acceptance criteria

- A tracked ticket's native `blocked_by` relations are read from GitHub and written to
  `depends_on` each poll (full-replace), idempotently (no-change poll leaves it identical).
- **Convergence:** add a native `blocked_by` → poll → assert it's in `depends_on`; remove it
  → poll → assert it's gone. No manual cleanup. (The headline test.)
- **Met semantics:** a ticket whose dep issue is open, or closed-as-**not-planned**, stays
  held; when the dep closes-**completed**, it dispatches on the next poll.
- **Untracked:** ticket #2 depending on untracked #1 holds and renders `relies on #1
  (untracked)`; dispatches once #1 closes-completed; #1 is never auto-planned.
- **Cycle:** two mutually-blocking tracked tickets both land held/needs-help with the cycle
  reason; neither runs; removing one relation releases both.
- Unit + integration tests cover every bullet above, using the Fake GitProvider for the
  dependency + issue-state reads.

## Open items for the plan

- Confirm the native dependencies **write** endpoint (decision on API vs UI authoring) — read
  path is load-bearing; write is a convenience.
- `get_issue_state` extension shape (widen return vs sibling method) — pick minimal-blast.
- Cycle handling mechanism: `hold_ticket(reason=...)` (reversible pause, self-heals on
  resume) vs a NeedsHelp terminal state — pick in the plan (leaning `hold_ticket`).
