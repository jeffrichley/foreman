---
paths:
  - "packages/foreman/tests/**/*.py"
---

# Test conventions for foreman

## Reach for hypothesis when an invariant exists

`hypothesis` is installed (foreman#336). Prefer property-based tests over
example-based tests when the function under test has a clear invariant —
hypothesis finds counterexamples that hand-written examples miss.

**Good fits:**
- Round-trips: `parse(emit(x)) == x`, `decode(encode(x)) == x`
- Idempotence: `sort(sort(xs)) == sort(xs)`, `f(f(x)) == f(x)`
- Algebraic identities: `union(a, b) == union(b, a)`
- Range invariants: parser output always within documented bounds

**Skip hypothesis when:**
- Behavior is example-driven (specific input → specific output)
- A single integration test exercises the path end-to-end
- The function has no meaningful invariant beyond "doesn't crash"

Don't add hypothesis tests purely for the property-based aesthetic — the
example test is fine when there's nothing structural to assert.
