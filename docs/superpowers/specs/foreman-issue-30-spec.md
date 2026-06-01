# Spec: Include `queue_depth` in poller's "polled project" log line (issue #30)

## Goal

Add a `queue_depth` field to the structured log record emitted by
`foreman.daemon.poller` at the end of every `poll_project` cycle, so a
foreman operator tailing `~/.foreman/daemon.log` can see how much work is
queued without separately invoking `foreman ps`.

Tracks issue [#30](https://github.com/jeffrichley/foreman/issues/30).

## Acceptance criteria

- After every `poll_project` invocation, the `INFO "polled project"` log
  record emitted by the `foreman.daemon.poller` logger has a
  `queue_depth: int` key in its `extra` dict (i.e. `record.queue_depth` is
  an `int` on the resulting `LogRecord`).
- `queue_depth` reflects the size of the daemon's `DaemonQueue` **after**
  the newly-changed tickets from this poll have been merged into it
  (post-enqueue depth, not pre-enqueue).
- The other three existing fields on the same log entry (`project`,
  `issues_seen`, `changed`) are unchanged in name and meaning.
- A new test in `packages/foreman/tests/test_poller.py` uses pytest's
  `caplog` fixture to assert that the `"polled project"` `LogRecord` has
  a `queue_depth` attribute and that its value equals
  `len(queue)` measured immediately after the test's enqueue path runs.
- Existing tests in `test_poller.py`, `test_daemon.py`, `test_daemon_e2e.py`
  continue to pass under `just check`.
- No behavior change to polling, the queue's contents, dispatch, or dequeue
  ordering.

## Approach

Today, `poll_project` in `packages/foreman/src/foreman/poller.py:35-82` is
pure with respect to the queue: it returns a `list[Ticket]` of
label-changed tickets, and the daemon's `_poller_loop` in
`packages/foreman/src/foreman/daemon.py:132-147` is what actually does
`self.queue.enqueue(ticket)` after `poll_project` returns. That split means
the existing `"polled project"` log call at `poller.py:73-80` fires
**before** the daemon has had a chance to enqueue, so it has no honest
view of post-enqueue queue depth at that point.

The cleanest fix — and the one the issue's "Where" section hints at — is
to **move the enqueue inside `poll_project`** and pass the
`DaemonQueue` in as a required keyword argument. Concretely:

1. Add `queue: DaemonQueue` to `poll_project`'s kwargs.
2. Inside the existing per-issue loop, immediately after the
   `changed.append(...)` line, call `queue.enqueue(ticket)` for the same
   ticket. This preserves dedup semantics (DaemonQueue is dedup-by-key,
   so this is idempotent if the daemon's self-notify hook already
   enqueued the same key).
3. Read `queue_depth = len(queue)` just before the `"polled project"`
   log call and add it to the `extra` dict.
4. Update `_poller_loop` in `daemon.py` to pass `queue=self.queue` and
   drop its now-redundant `for ticket in changed: self.queue.enqueue(...)`
   block.

`poll_project` still returns `list[Ticket]` — the return is useful for
unit tests asserting "what changed," and removing it would be churn
unrelated to the issue. The daemon just stops iterating over the return
value for enqueueing.

Why this is the right shape:
- It matches the issue author's "passes it as a kwarg and reads
  `len(queue)` after enqueuing" hint verbatim.
- It puts the queue depth on the **same** log record that already
  describes the poll outcome, so an operator sees one structured event
  per project per cycle rather than two.
- The existing `DaemonQueue.__len__` (`queue.py:32-33`) and idempotent
  `enqueue` (`queue.py:28-30`) mean no new queue API is needed.
- It does not foreclose future logging additions (e.g. a `parked_count`)
  because they'd land in the same `extra` dict the same way.

Test approach mirrors the convention already in
`packages/foreman/tests/test_logging_setup.py:12-28`: lean on the
standard library's `LogRecord.<extra-key>` attribute access rather than
parsing JSON. For the new poller test, pytest's `caplog` is simpler than
configuring a full daemon JSON sink — find the `"polled project"` record
and assert `record.queue_depth == expected`.

## Sub-requests (topologically sorted)

1. In `packages/foreman/src/foreman/poller.py`: import `DaemonQueue` from
   `foreman.queue`; add `queue: DaemonQueue` to `poll_project`'s keyword
   arguments (place it after `storage`); inside the existing
   `for issue in issues:` loop, call `queue.enqueue(...)` on the just-
   constructed `Ticket` immediately after `changed.append(...)`; add
   `"queue_depth": len(queue)` as a new key in the `extra={...}` dict of
   the existing `_log.info("polled project", ...)` call at lines 73-80.
2. In `packages/foreman/src/foreman/daemon.py`: in `_poller_loop`
   (around lines 132-147), pass `queue=self.queue` into the
   `poll_project(...)` call, and remove the trailing
   `for ticket in changed: self.queue.enqueue(ticket)` block since
   `poll_project` now owns that side effect. The returned `changed` list
   may be ignored (or kept as `_changed = poll_project(...)` for
   readability — Worker's call, no requirement either way).
3. In `packages/foreman/tests/test_poller.py`: import `DaemonQueue` from
   `foreman.queue`; in each of the four existing tests
   (`test_poll_project_enqueues_new_issue`,
   `test_poll_project_returns_nothing_when_labels_unchanged`,
   `test_poll_project_detects_label_changes`,
   `test_poll_project_persists_new_labels_seen`), construct a
   `DaemonQueue()` and pass it as `queue=queue` to the `poll_project(...)`
   call. The existing assertions on `changed` remain valid.
4. In `packages/foreman/tests/test_poller.py`: add a new test
   `test_poll_project_logs_queue_depth` that uses `caplog.at_level(...)`
   on the `foreman.daemon.poller` logger, runs `poll_project` against a
   host returning two label-changed issues into a fresh `DaemonQueue`,
   finds the `LogRecord` whose `.message == "polled project"`, and
   asserts both `hasattr(record, "queue_depth")` and
   `record.queue_depth == 2` (matching the post-enqueue length of the
   queue).

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/poller.py` | Add `queue: DaemonQueue` kwarg; enqueue each changed ticket inline; add `queue_depth` to the `"polled project"` log extra. |
| `packages/foreman/src/foreman/daemon.py` | Pass `queue=self.queue` into `poll_project`; remove the now-redundant `_poller_loop` enqueue block. |
| `packages/foreman/tests/test_poller.py` | Thread a `DaemonQueue` through the four existing tests; add one new test asserting `queue_depth` appears on the emitted `LogRecord`. |

No other files (no CLI, no schema, no docs) need to change — this is a
single structured-log field plus a small refactor that relocates an
existing side effect from the daemon loop into `poll_project`.

## Alternatives considered

- **Keep the enqueue in `_poller_loop` and just compute `queue_depth`
  there, then emit a separate `"queue depth"` log line from the daemon.**
  Rejected: the issue explicitly asks for the field on the poller's
  existing `"polled project"` line, and an extra log per cycle is noise
  an operator would have to correlate by project name.
- **Pass `queue` as an `Optional[DaemonQueue] = None` and skip the
  `queue_depth` field when `None`.** Rejected: acceptance criteria
  require the field always present, and the optional path creates two
  code paths that diverge under test (would also mean `poll_project`
  silently stops enqueueing when called without a queue, which would be
  a footgun for future callers).
- **Compute `queue_depth` before enqueueing (pre-poll depth).** Rejected:
  the operator's question is "did this poll leave work in the queue?",
  which only the post-enqueue value answers; pre-enqueue depth is
  available trivially as `queue_depth - changed`.

## Open questions

(none — the issue is unambiguous, the surface is small, and the existing
queue API supports the required `len()` natively.)

## Out of scope

- Adding additional fields to the `"polled project"` log (e.g.
  `parked_count`, `in_flight_count`, per-stage breakdowns). One field,
  one PR.
- Changing dedup, sort, or dispatch behavior of `DaemonQueue`.
- Refactoring `poll_project` to be async or to batch across projects.
- Adding a CLI surface for queue depth (`foreman ps` already exists per
  the issue body).
- Changing the JSON-lines logging format or the
  `foreman.daemon.poller` logger name — downstream log aggregators may
  depend on both.
