"""Canonical ``foreman:state-<kebab>`` label-name derivation.

Single source of truth for turning a ``STATE_REGISTRY`` state name
("SpecReview", "NeedsHelp", ...) into the GitHub label
:mod:`foreman.init` pre-creates and
:mod:`foreman.v4.observers.label_observability` stamps at runtime.

Before this module existed, ``init.py`` and the label observer each
carried their own copy of this transform and quietly disagreed for
multi-word states: init kebab-cased ("state-spec-review") while the
observer naive-lowercased ("state-specreview"). The daemon stamped the
run-together form init never created — a colorless, undocumented label
sitting next to the pretty one init pre-provisioned. Both call sites
now delegate here so the two can never diverge again.

Labels are write-only observability: nothing in the daemon reads them
back to make routing decisions (the DB's ``current_state`` column is
the source of truth), so this module has no runtime behavior surface
beyond string formatting.
"""

from __future__ import annotations

import re

#: Matches an uppercase letter not at the start of the string — the
#: insertion point for a kebab-case dash. "SpecReview" -> "Spec-Review".
_CAMEL_BOUNDARY_RE = re.compile(r"(?<!^)([A-Z])")


def state_label(state_name: str) -> str:
    """Map a STATE_REGISTRY entry ("Queued", "SpecReview", ...) to a label.

    The label form is ``foreman:state-<kebab>``. "SpecReview" ->
    "spec-review", "NeedsHelp" -> "needs-help", "Queued" -> "queued".
    """
    kebab = _CAMEL_BOUNDARY_RE.sub(r"-\1", state_name).lower()
    return f"foreman:state-{kebab}"
