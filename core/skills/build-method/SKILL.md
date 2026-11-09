---
name: build-method
description: Builder method for one unattended Firstmate task. Make the smallest safe change that meets the brief's acceptance criteria, test-first, with the simplest solution first and official docs first for third-party, upgrade, auth and billing code. Verify, review your own diff, open a draft PR and stop; never merge. Use at the start of every build-phase task and again on a rebuild pass after verify or review sends findings back.
---

# Build method

Ported from super-board's `super-build` lane: the method only. The board glue (cards, labels,
dispatcher exit codes, merge gate) is gone; Firstmate owns task state and merging. Read
`policy/preamble.md` first: it holds the unattended rules and the decision ladder this
method relies on.

## 1. Load the skills for this task

The brief lists the resolved skills. Load them in that order before writing code.

- **Task row** comes from the ticket's `Skills:` line when there is one (it replaces the kind
  row and does not extend it); otherwise from the task kind:

  | Kind | Load, in order |
  |---|---|
  | `bug` | `diagnosing-bugs` → `tdd` |
  | `feature` | `implement` → `tdd` → `codebase-design` |
  | `ux` | `implement` → `tdd` |
  | `refactor` / `tech-debt` | `codebase-design` → `tdd` |
  | `tests` | `tdd` |
  | `docs` | none: skip `tdd`, there is no behaviour to pin |
  | unclassified | `diagnosing-bugs`, `tdd`, `implement`, `codebase-design` until you can tell bug from feature |

  Diagnose before you write the red test. Implement against the AC before you reach for
  design vocabulary.
- **Always, on top of the row:** simplest solution first (`ponytail` if available, otherwise
  `policy/fallbacks/simplest-solution-first.md`) before the first line of code;
  `verification-before-completion` before the final commit; `code-review` once on your own diff.
- **When the situation calls for it:** `testing-strategy` when coverage scope is open; the
  repo's test library (vitest, playwright…) picked by the localisation ladder below, never by kind.
- **Never inside a worker:** `grilling`, `shape`, `clarify`, `improve-codebase-architecture`,
  or anything else that waits on a human. Wanting one means the ticket needed a human first.
  That is a human gate (preamble).

- **Vendored skills run unattended:** in vendored skills, every "ask/confirm with the user"
  step maps to the decision ladder, and anything still needing a human becomes
  `needs-decision: [key=human-gate-<topic>]`. The spec is the brief's Captain's intent and AC;
  there is no issue-tracker setup. Load pack skills by the absolute paths in the brief, never
  by bare name (preamble → "Vendored skills run unattended", "Loading pack skills").

## 2. Testing is three layers

- **Discipline**: `tdd`. Seams, red-green order, assertion anti-patterns.
- **Placement**: the localisation ladder. Walk down and stop at the first rung that still
  reproduces: (1) call the module directly → unit; (2) wire real collaborators → integration;
  (3) drive a browser → e2e; (4) needs a live third party → not a test; record a mock or
  contract gap. Write the test at the rung you stopped on.
- **Mechanics**: the repo decides the library. Read `package.json` / the build file.

A test counts as done only when it goes **red for the right reason** against the unfixed code
(a timeout or a missing selector pins the harness, not the bug) and **survives a refactor**
that leaves behaviour unchanged.

## 3. Docs before outside-tool code

Before writing code, decide whether the task touches a third-party API, SDK, library, CLI or
service; a dependency or framework **upgrade**; or **auth or billing**. If it does, read the
current official docs for the installed version first (context7 when available, else the
vendor's own docs, migration guide or changelog). Not memory, not blog posts. Repo docs and
existing call sites settle how *this* repo uses the tool; vendor docs settle what the tool does.

Cite each doc as a `Docs:` bullet in the PR body (URL + the one fact it settled). If the docs
were unreachable, say so, mark the code `unverified against current docs`, and keep the change
minimal. If no outside tool was touched, write `Docs: none needed`.

## 4. Keep the PR small

Stay under the project's size cap (default 400 changed lines; lockfiles, generated files,
snapshots and migration SQL don't count). Check `git diff --shortstat origin/<base>...HEAD`
before each push. If the change grows past the cap, **stop**: commit and push what you have,
leave the PR a draft, and propose the split in the PR (thin end-to-end slices, each under the
cap with its own AC, and which slice this PR keeps). Then report
`needs-decision: [key=human-gate-split] too big — split proposed in the PR`. Never pad past the
cap to finish.

## 5. Finish

1. All verify commands from the brief are green. Run them; don't infer.
2. `verification-before-completion`: every claim in your report has the command output behind it.
3. `code-review` on your own diff, fixed point `git merge-base HEAD origin/<base>`, so it never
   asks for one. Fix Standards findings. A Spec finding you can't close in scope is a human gate.
4. Commit and push. Fill in `evidence.json` (verify command results, `decisions[]`).
5. Open a **draft** PR and stop. Never merge; never run `gh pr merge`.

Acceptance criteria are the completion contract. Never edit the ticket's checkboxes to make it
look done. A partial result that is type-checked, tested and safe to merge on its own may ship
as a draft PR that says plainly which AC are not met. Report it as `needs-decision` and never
as done.

## 6. On failure

Stop, keep salvageable work in a commit, and write a handoff line:

```
root-cause-hash: <first 12 hex of sha256("build|<error class>|<first 3 unique normalized file:line frames>")>
```

The same hash on two runs means the same failure is repeating; don't retry blindly.

## 7. Rebuild pass

When the task comes back from verify or review: read **every** prior finding first (review
report, PR threads addressed to the builder, the last failing verify output). Fix each one
before anything new, and say in the report which file:line closed each finding. Then run §5 again.
