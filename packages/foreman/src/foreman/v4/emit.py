"""emit_outcome — role-side counterpart of parse_outcome_from_stdout.

Each role's CLI calls this as its terminal action. The state machine's
verify hook scans stdout in reverse for the FOREMAN_OUTCOME: marker and
parses what we wrote here. Round-trip property: emit then parse → equal
Outcome.
"""

from __future__ import annotations

import sys
from typing import TextIO

from foreman.v4.outcome import OUTCOME_MARKER, Outcome


def emit_outcome(outcome: Outcome, *, stream: TextIO | None = None) -> None:
    r"""Write one terminal line: ``FOREMAN_OUTCOME:<json>\n``.

    Default stream is sys.stdout. Tests pass StringIO. Roles call this
    once, as the very last thing before sys.exit().
    """
    target = stream if stream is not None else sys.stdout
    target.write(f"{OUTCOME_MARKER}{outcome.model_dump_json()}\n")
    target.flush()
