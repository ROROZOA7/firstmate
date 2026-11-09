# Over-engineering ladder

Fallback for `ponytail-review` when it isn't visible to the reviewer. Run it on the merge-base
diff (`git merge-base HEAD origin/<base>`).

For each dependency, abstraction, config, file or helper the diff adds, ask in order:

1. Is it needed at all to meet the acceptance criteria?
2. Does this codebase already have it?
3. Does the stdlib cover it?
4. Does a native platform feature cover it?
5. Does an already-installed dependency cover it?
6. Would one line do?

The first "yes" is a finding: class **Over-engineering**, severity **Should fix**, routed to
build, written as `<what it builds> → <smaller thing>`.

- It **never blocks a merge on its own**, and it never lowers truth-check confidence.
- When the task bounces for another finding anyway, include it so the rebuild trims it.
- A simplification that would cut validation, security, data-loss protection or
  accessibility is not a finding.
