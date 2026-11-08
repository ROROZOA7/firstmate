# Simplest solution first

Fallback for `ponytail` when it isn't visible to the worker. Apply it once the problem is
understood and before the first line of code.

1. Climb the ladder and stop at the first rung that holds: does it need to exist at all? →
   already in this codebase? → stdlib? → native platform feature? → an already-installed
   dependency? → one line? → only then the minimum code.
2. Add no new dependency, abstraction, config or file the acceptance criteria don't need.
3. A bug fix goes at the root cause: one guard in the shared function beats one per caller.
4. Never cut validation, security, data-loss protection or accessibility to shrink a diff.
5. Mark a deliberate corner with a `ponytail:` comment that names its ceiling.

This decides *how little* to write. It never overrides an acceptance criterion, the `tdd` red
test, or the decision ladder in `policy/preamble.md`.
