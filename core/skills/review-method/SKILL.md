---
name: review-method
description: Reviewer method for one unattended Firstmate review task. Form your own hypotheses before reading the builder's summary; on a re-review, recheck every prior finding with file:line proof first; run an adversarial truth-check on non-trivial diffs; class every finding (Gap, Bug, Verification miss, Scope drift, Over-engineering); file shape problems instead of blocking. Reports merge-ready, bounced, blocked, human-gated or unverified, and never merges. Use for every review-phase task.
---

# Review method

Ported from super-board's `super-review` lane: the method only. Board glue (card moves, PR
thread prefixes, the merge gate and its exit codes) is gone. Firstmate's merge gate owns
merging; this method produces the verdict and the evidence behind it. Read `<pack>/policy/preamble.md`
first.

Be conservative: say **merge-ready** only when the findings are clean and verification passed.
Whatever you could not verify, name.

## 1. Scope

- Base branch, PR/branch, changed files, the task's acceptance criteria.
- Scope is **the diff**, never the whole codebase. A hot spot three modules away is not this PR's problem.
- Read the nearest `AGENTS.md` / `CLAUDE.md` for the repo's conventions.
- If a prior review report exists, run §2 before anything else.

## 2. Review remembers (re-review only)

Mark every prior finding, keeping its id:

- `fixed`: cite the file:line that shows it.
- `not fixed`: still true of the code. A resolved thread or a builder reply saying it was fixed is not evidence; read the code.
- `no longer applies`: the code is gone or the AC changed. Do not re-raise it.

Any `not fixed` (Over-engineering aside) → **bounce again** and skip the fresh pass: it would
review code that is about to change. All clear → do §3 as a fresh pass. New findings take ids
after the highest one used.

## 3. Your own pass first

1. **Before reading the builder's account** (PR summary, report, verify handoff), read the AC and
   the raw diff and write down 2–4 hypotheses: what must be true for this to be right, and where it
   would most likely break. Reading their summary first anchors you on their framing.
2. Then check each builder claim against the code and test output. A claim is a lead, not evidence.
3. Load `codebase-design` and read the diff in its vocabulary (module, interface, depth, seam,
   adapter, leverage, locality). Does the change land behind a small interface, or widen one?
   Apply the deletion test to anything shallow it adds.
4. Run `code-review` with the fixed point `git merge-base HEAD origin/<base>`. A Spec-axis finding
   that contradicts the AC is a Blocker.
5. Re-run the exact verify commands yourself. Red → a Verification miss or Bug, with the output quoted.

## 4. Classify every finding

**Class:**
- **Gap**: an AC or requested behaviour is missing or incomplete.
- **Bug**: the code likely fails or regresses.
- **Verification miss**: the code may be right, but the evidence doesn't prove it.
- **Scope drift**: the diff changes what the task didn't ask for, or skips a stated constraint.
- **Over-engineering**: works, but builds more than the AC needs. Find it with `ponytail-review`
  if available, else `<pack>/policy/fallbacks/over-engineering-ladder.md`. Always **Should fix**, **never
  blocks on its own**. A simplification that would cut validation, security, data-loss
  protection or accessibility is not a finding.
- **No issue**: a hypothesis or claim you checked that holds. List it under Done with its file:line
  or command, so the next round doesn't re-litigate it.

**Severity:** Blocker (correctness, data loss, security, auth, migrations, money, customer-visible
breakage, failing required tests) · Should fix · Nit · **Deepening opportunity** · Human gate.

## 5. Shape problems are filed, not blocking

A correct diff that leaves a shallow module, a seam callers must work around, or behaviour spread
across call sites is a **deepening opportunity**: a future ticket, never a merge blocker. Write it
up in the ticket format (Problem · Context with `Where:` · Fix · Acceptance Criteria) using the
`codebase-design` terms exactly. Put it in the report as a ticket draft for Firstmate intake. Don't
file one for style, naming, anything an ADR already settled, or a shape the AC asked for.

## 6. Adversarial truth-check

Run it when the diff is ≥ 10 changed lines or touches security, migrations, payments or auth.
Spawn two sub-agents in parallel with this brief, verbatim except for the slots (the
unattended line comes from preamble → "Sub-agents"):

```
You are the <Code-grounder | Historian> in an adversarial truth-check of PR #<P>.
Job: <Code-grounder: verify every file:line the PR cites still exists and does what the PR
claims. | Historian: git blame the changed lines; look for ADRs, prior incidents, reverted
attempts.>
Read the Captain's intent, the ACs and the diff first and form your own hypotheses. Only then read the
builder's summary below, as claims to check, not as the frame.
Captain's intent: <paste>
ACs: <paste>
Diff: <merge-base diff or path to it>
Builder's summary (claims): <paste>
Unattended: never call AskUserQuestion or wait for a human; no issue tracker or setup step;
load skills only from <pack>/skills/<name>/SKILL.md (<pack> = the real pack dir from your
brief; substitute it), never by bare name; never merge.
Budget: at most 50 gh calls; prefer local git. Over budget: confidence "insufficient_data".
Class every finding: Gap / Bug / Verification miss / Scope drift / Over-engineering / No issue.
Severity: Blocker / Should fix / Nit.
Over-engineering is ALWAYS Should fix and NEVER Blocker on its own. It does not lower your
confidence score.
Confidence 0-100 = how sure you are the PR's claims are true and the code does what the ACs
ask. Design taste and over-building do not count against it.
Return: findings (class, severity, file:line, one line each) and confidence.
```

Then re-apply the rules yourself: reclass any Over-engineering marked Blocker to Should fix, and
re-score a sub-agent that lowered confidence only for over-engineering. Take the **minimum** of the
two scores. Below 70 → do not report merge-ready; report `blocked` and quote the lowest-confidence
finding you could not confirm.

## 7. Verdict

- No findings (Over-engineering aside), verify green, truth-check ≥ threshold → `merge-ready`.
- Code-side finding → `bounced` to build. Test-side finding → `bounced` to verify.
- A product, design or ops decision you can't safely guess →
  `needs-decision: [key=human-gate-<topic>] <reason>`.
- Evidence you couldn't collect → `unverified`, saying why.

Never merge, and never push fixes unless the brief explicitly grants that authority.

## Report

One report, edited in place on every round so the table carries each finding's history:

```markdown
[reviewer] <✅ merge-ready | ❌ bounced | 🛑 blocked | 🙋 human-gated | ⚠️ unverified> · round <N>
Did: <scope · base · verify command + result>
✅ Done: <prior findings fixed, hypotheses that held — each with file:line or command>
❌ Not done: <open findings by id, or what you could not verify>
Next: <build | verify | captain | none>

| ID | Owner | Class | Where | Finding | Status |
|---|---|---|---|---|---|
| R1 | build | Bug | `src/cart.py:12` | <finding> · blocker | open |
```
