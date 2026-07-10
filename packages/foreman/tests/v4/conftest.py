"""v4-scoped pytest fixtures.

Autouse logging cleanup
-----------------------
``foreman.v4.logging_config.configure_logging`` mutates process-global
state: it attaches RichHandler + JsonLinesHandler to three named loggers
(``foreman.v4``, ``foreman.v4.transitions``, ``foreman.v4.event_bus``)
AND sets ``propagate=False`` on each. Without per-test teardown, that
state leaks across tests and produces two distinct failure modes that
pytest-randomly surfaces unpredictably:

1. Stale JsonLinesHandler — configure_logging is idempotent on handler
   type, so the next caller's tmp_path log file never gets written to
   (the handler still points at a deleted previous tmp_path).
2. propagate=False breaks caplog — caplog attaches at root; with
   propagation disabled, records on the v4 loggers never reach root.

Both manifested on CI commit 9a31841 (red build after #383 merged):
``test_one_transition_reaches_all_four_observers`` saw an empty
``events_logged`` set, and ``test_level_is_honored`` (under seed
1208273269) raised FileNotFoundError reading the stale jsonl path.

Putting this cleanup at v4 scope (instead of per-file) means future
tests that transitively reach configure_logging — say, by exercising a
new ``main()``-shaped entry point — get cleanup for free. The cost is
trivial: a 3-key snapshot + a ``reset_logging`` call that's a no-op
when nothing's attached.
"""

from __future__ import annotations

import logging

import pytest

from foreman.v4.logging_config import reset_logging

_V4_LOGGER_NAMES = (
    "foreman.v4",
    "foreman.v4.transitions",
    "foreman.v4.event_bus",
)


@pytest.fixture(autouse=True)
def _reset_v4_logging():
    snapshots = {n: logging.getLogger(n).propagate for n in _V4_LOGGER_NAMES}
    yield
    reset_logging()
    for name, propagate in snapshots.items():
        logging.getLogger(name).propagate = propagate
