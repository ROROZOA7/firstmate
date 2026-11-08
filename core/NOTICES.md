# Notices: core pack

The core pack itself (`pack.yaml`, `policy/`, `skills/build-method`, `skills/review-method`) is
derived from super-board, MIT © Eric Tech (`licenses/super-board.MIT.txt`). The vendored skills
below keep their own licences. Each licence's full text is in `licenses/`.

Each upstream licence's commit is pinned in `pack.yaml` → `skills.vendored[].provenance`.

| Skill | Source (repo @ commit, path) | Licence | Changes |
|---|---|---|---|
| build-method | ROROZOA7/super-board @ `d0b7d5a`, `skills/super-build/` (SKILL.md, references/worker-preamble.md, references/decision-policy.md) | MIT © 2026 Eric Tech | Rewritten: method kept; board glue (cards, labels, dispatcher exit codes, merge gate, `claude -p`) removed; human gate mapped to Firstmate's `needs-decision` |
| review-method | ROROZOA7/super-board @ `d0b7d5a`, `skills/super-review/SKILL.md` | MIT © 2026 Eric Tech | Rewritten: method kept; card moves, thread-prefix routing and merge protocol removed; verdict reported to Firstmate |
| pr-security-review | ROROZOA7/super-board @ `d0b7d5a`, `skills/pr-security-review/`; originally anthropics/claude-code-security-review @ `0c6a49f`, `.claude/commands/security-review.md` | MIT © 2025 Anthropic (`skills/pr-security-review/LICENSE`) | Copied unchanged from super-board (whose port only changed the diff target to the PR's base branch) |
| tdd | mattpocock/skills @ `4ae2317`, `skills/engineering/tdd/` | MIT © 2026 Matt Pocock | Unchanged |
| diagnosing-bugs | mattpocock/skills @ `4ae2317`, `skills/engineering/diagnosing-bugs/` | MIT © 2026 Matt Pocock | Unchanged |
| implement | mattpocock/skills @ `4ae2317`, `skills/engineering/implement/` | MIT © 2026 Matt Pocock | Unchanged |
| codebase-design | mattpocock/skills @ `4ae2317`, `skills/engineering/codebase-design/` | MIT © 2026 Matt Pocock | Unchanged |
| code-review | mattpocock/skills @ `4ae2317`, `skills/engineering/code-review/` | MIT © 2026 Matt Pocock | Unchanged |
| domain-modeling | mattpocock/skills @ `4ae2317`, `skills/engineering/domain-modeling/` | MIT © 2026 Matt Pocock | Unchanged |
| verification-before-completion | obra/superpowers @ `22f0b46`, `skills/verification-before-completion/` | MIT © 2025 Jesse Vincent | Unchanged |
| testing-strategy | anthropics/knowledge-work-plugins @ `e050c2e`, `engineering/skills/testing-strategy/` | Apache-2.0, no named holder in the licence file (`licenses/anthropics-knowledge-work-plugins.Apache-2.0.txt`) | Unchanged (Apache-2.0 §4b: no files modified). The upstream repo has no NOTICE file |
| vitest | antfu/skills @ `f63e9ec`, `skills/vitest/` | MIT © 2025-present Anthony Fu | Unchanged (generated upstream from vitest docs; see its GENERATION.md) |
| playwright-best-practices | currents-dev/playwright-best-practices-skill @ `0b75b58`, `playwright-best-practices/` | MIT © 2026 Currents Software Inc. | Unchanged |

## Allowlisted, not vendored

`ponytail` and `ponytail-review` are resolved from the worker's global or plugin skills when
present. Their licence is not confirmed, so they are not copied. When they're absent, the
pack's own text fallbacks apply (`policy/fallbacks/`), and those are derived from super-board (MIT).

## File modes

All files were normalised to 644 (755 for the one `.sh` template and for directories), as the
pack tree-digest rules require. That is a mode change only, not a content change.
