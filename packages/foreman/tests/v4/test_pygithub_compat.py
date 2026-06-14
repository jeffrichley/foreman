"""Forward-compat canary for PyGithub's private-attr GraphQL access pattern.

``PyGithubGitProvider.enqueue_merge_queue`` and ``merge_verdict`` reach into
``Github._Github__requester.requestJsonAndCheck`` because PyGithub doesn't
expose a typed GraphQL surface. That's a fragile coupling — a PyGithub
minor-version bump could rename or remove the mangled attribute, and the
breakage would surface at MergeQueue time in production rather than at test
time.

This test asserts the attribute name + method signature we rely on, so any
upstream change shows up the next time anyone runs ``just check`` instead of
silently waiting for a real merge to fail.

If this test fails, see:
  docs/superpowers/plans/v4-phases/2026-06-13-foreman-v4-phase-8-deletion-cutover.md
  → "Dogfood watch-points" #2 — PyGithub _Github__requester fragility

The fix is to introduce a thin wrapper or switch the two GraphQL methods to
``requests`` directly; REST surface stays on PyGithub.
"""
from __future__ import annotations

from github import Auth, Github  # type: ignore[import-not-found]

# Use a dummy token; this test does not hit the network — we only inspect
# the in-memory object surface.
_DUMMY_AUTH = Auth.Token("ghp_DUMMY_TOKEN_FOR_COMPAT_TEST")


def test_github_exposes_mangled_requester_attribute():
    """Github instance must expose the name-mangled __requester attribute.

    PyGithubGitProvider reaches it via ``gh._Github__requester``. If PyGithub
    renames the class (e.g., to ``GitHub``) or makes ``__requester`` public,
    the mangled name changes and our access pattern silently breaks.
    """
    gh = Github(auth=_DUMMY_AUTH)
    assert hasattr(gh, "_Github__requester"), (
        "Github._Github__requester is gone — PyGithub upstream renamed the "
        "class or unmangled the private attr. See Phase 8 watch-point #2."
    )


def test_requester_exposes_requestJsonAndCheck():
    """The requester must have ``requestJsonAndCheck``; we call it for GraphQL.

    Signature we depend on:
      requestJsonAndCheck("POST", "/graphql", input={"query": ..., "variables": ...})
      → (headers, payload_dict)
    """
    gh = Github(auth=_DUMMY_AUTH)
    requester = gh._Github__requester  # type: ignore[attr-defined]
    assert hasattr(requester, "requestJsonAndCheck"), (
        "requester.requestJsonAndCheck is gone — PyGithub upstream removed "
        "the raw-JSON entry point. See Phase 8 watch-point #2 for the fix "
        "path (thin wrapper or switch to `requests`)."
    )
    assert callable(requester.requestJsonAndCheck), (
        "requester.requestJsonAndCheck exists but isn't callable — "
        "PyGithub upstream changed its shape. See Phase 8 watch-point #2."
    )


def test_pull_request_exposes_node_id_attribute():
    """PullRequest objects must carry ``node_id``; we pass it to GraphQL.

    We don't instantiate a real PR (would need network), but we can verify
    the attribute is declared on the PullRequest class so a static check
    catches its removal.
    """
    from github.PullRequest import PullRequest  # type: ignore[import-not-found]

    # PyGithub uses lazy property descriptors; the attribute appears on the
    # class even when no instance exists. If upstream removes it, this fails.
    assert hasattr(PullRequest, "node_id"), (
        "PullRequest.node_id is gone — PyGithub upstream removed the GraphQL "
        "node identifier accessor. See Phase 8 watch-point #2; without "
        "node_id we cannot enqueue PRs into the merge queue."
    )
