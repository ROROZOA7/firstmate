#!/usr/bin/env bash
# tests/fm-pack.test.sh - behavior tests for bin/fm-pack.sh (contract fm-pack.v1)
# and the --pack option of bin/fm-brief.sh.
#
# Every case drives the public commands against an isolated home: fixture packs
# are committed to a throwaway git repository and installed through
# `fm-pack.sh install`, bindings are written as the captain would, and project
# clones are real git clones with an origin/HEAD. Expected tree digests are
# computed independently from the contract's definition, not by fm-pack.
set -u

# shellcheck source=tests/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

command -v python3 >/dev/null 2>&1 || { echo "skip: python3 not found (required by fm-pack.sh)"; exit 0; }
python3 -I -c 'import yaml' >/dev/null 2>&1 || { echo "skip: python3 PyYAML not found (required by fm-pack.sh)"; exit 0; }

PACK="$ROOT/bin/fm-pack.sh"
BRIEF_SH="$ROOT/bin/fm-brief.sh"
TMP_ROOT=$(fm_test_tmproot fm-pack)
fm_git_identity

# --- fixtures ------------------------------------------------------------------

# json <expr>: evaluate a Python expression over the JSON document on stdin as `r`.
json() {
  python3 -I -c 'import json,sys; r=json.load(sys.stdin); v=eval(sys.argv[1]); print(json.dumps(v) if not isinstance(v, str) else v)' "$1"
}

# Contract tree_digest, written independently of bin/fm-pack.py.
contract_digest() {  # <dir>
  python3 -I - "$1" <<'PY'
import hashlib, os, stat, sys
root = sys.argv[1]
lines = []
for d, _dirs, files in os.walk(root):
    for f in files:
        p = os.path.join(d, f)
        st = os.lstat(p)
        mode = "755" if st.st_mode & 0o111 else "644"
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        lines.append(f"{mode} {hashlib.sha256(open(p, 'rb').read()).hexdigest()} {rel}\n")
lines.sort()
print("sha256:" + hashlib.sha256("".join(lines).encode()).hexdigest())
PY
}

# write_pack <dir> <name> <version> [extra-yaml]: a small valid pack source.
write_pack() {
  local dir=$1 name=$2 version=$3 extra=${4:-}
  mkdir -p "$dir/.claude-plugin" "$dir/policy/fallbacks" "$dir/skills"
  printf '{"name": "%s", "version": "%s"}\n' "$name" "$version" > "$dir/.claude-plugin/plugin.json"
  printf '# Preamble\n\n## Hard rules\nNever merge.\n' > "$dir/policy/preamble.md"
  printf 'Prefer the simplest solution.\n' > "$dir/policy/fallbacks/simplest.md"
  printf 'MIT notices\n' > "$dir/NOTICES.md"
  local s
  for s in tdd diagnosing-bugs implement build-method code-review review-method vitest docs-check; do
    mkdir -p "$dir/skills/$s"
    printf -- '---\nname: %s\n---\n# %s\n' "$s" "$s" > "$dir/skills/$s/SKILL.md"
  done
  mkdir -p "$dir/skills/diagnosing-bugs/scripts"
  printf '#!/bin/sh\necho loop\n' > "$dir/skills/diagnosing-bugs/scripts/loop.sh"
  chmod 755 "$dir/skills/diagnosing-bugs/scripts/loop.sh"
  {
    cat <<EOF
schema: fm-pack-manifest.v1
requires: {fm_pack: 1}
name: $name
version: $version
description: fixture pack
harnesses: [claude, codex, opencode, pi]
skills:
  vendored:
EOF
    for s in tdd diagnosing-bugs implement build-method code-review review-method vitest docs-check; do
      printf '    - {id: %s, provenance: {repo: example.com/x, commit: abc, license: MIT, path: %s}}\n' "$s" "$s"
    done
    cat <<'EOF'
  allow_global: [ponytail]
  aliases: {"mattpocock-skills:tdd": tdd, "ponytail:ponytail": ponytail}
  by_kind:
    - {labels: [bug], required: [diagnosing-bugs, tdd]}
    - {labels: [feature], required: [implement, "mattpocock-skills:tdd"]}
    - {labels: [docs], required: []}
  default: {required: [diagnosing-bugs, tdd, implement]}
  by_phase:
    build:
      required: [build-method, ponytail, code-review]
      optional: [code-review, docs-check]
      conditional:
        - {id: vitest, when: "file:vitest.config.*"}
    verify:
      required: [tdd, code-review]
    review:
      required: [review-method, code-review]
  fallbacks:
    - {for: ponytail, path: policy/fallbacks/simplest.md}
banned_tools: [AskUserQuestion]
banned_skills: [grilling, clarify]
preamble: policy/preamble.md
verify_commands: []
evidence: {required: [verify_commands, decisions]}
model_hint: null
settings: {}
EOF
    printf '%s' "$extra"
  } > "$dir/pack.yaml"
}

SRC="$TMP_ROOT/src"
git init -q "$SRC"
commit_src() {  # -> prints the commit sha
  git -C "$SRC" add -A >/dev/null
  git -C "$SRC" commit -q --allow-empty -m fixture >/dev/null
  git -C "$SRC" rev-parse HEAD
}

new_home() {  # <name> -> prints the home path
  local h="$TMP_ROOT/$1"
  mkdir -p "$h/data" "$h/projects" "$h/config"
  printf '## Projects\n- web [direct-PR +yolo] - fixture project (added 2026-11-08)\n' > "$h/data/projects.md"
  printf '%s\n' "$h"
}

# make_project <home> <name> [file...]: a clone whose origin/<default> has the files.
make_project() {
  local h=$1 name=$2 bare="$TMP_ROOT/remotes/$2.git" work="$TMP_ROOT/remotes/$2-work"
  shift 2
  rm -rf "$bare" "$work" "$h/projects/$name"
  mkdir -p "$TMP_ROOT/remotes"
  git init -q --bare -b main "$bare"
  git init -q -b main "$work"
  printf 'readme\n' > "$work/README.md"
  local f
  for f in "$@"; do
    mkdir -p "$work/$(dirname "$f")"
    printf 'x\n' > "$work/$f"
  done
  git -C "$work" add -A && git -C "$work" commit -q -m init
  git -C "$work" push -q "$bare" main 2>/dev/null
  git clone -q "$bare" "$h/projects/$name" 2>/dev/null
}

bind() {  # <home> <project> <digest> [extra-yaml]
  mkdir -p "$1/data/.pack-bindings"
  printf 'pack: core\nversion: 0.1.0\ndigest: %s\nverify_commands:\n  - {id: unit, cmd: "npm test"}\n%s' "$3" "${4:-}" > "$1/data/.pack-bindings/$2.yaml"
}

run_pack() {  # <home> args...
  local h=$1
  shift
  FM_HOME="$h" "$PACK" "$@"
}

write_pack "$SRC/core" core 0.1.0
CORE_COMMIT=$(commit_src)
CORE_DIGEST=$(contract_digest "$SRC/core")

HOME_A=$(new_home a)
run_pack "$HOME_A" install "$SRC" "$CORE_COMMIT" core --json >/dev/null || fail "fixture: installing the core pack failed"
make_project "$HOME_A" web src/vitest.config.ts
bind "$HOME_A" web "$CORE_DIGEST"
INSTALLED="$HOME_A/data/.packs/core/0.1.0/${CORE_DIGEST#sha256:}"

# --- tests -----------------------------------------------------------------------

test_version_and_usage() {
  local out rc
  out=$(run_pack "$HOME_A" version --json); rc=$?
  expect_code 0 "$rc" "version"
  assert_equals fm-pack.v1 "$(json 'r["schema"]' <<<"$out")" "version schema"
  assert_contains "$(json 'r["contracts"]' <<<"$out")" '"fm-pack.v1"' "version lists fm-pack.v1"
  assert_equals A "$(json 'r["phase"]' <<<"$out")" "phase"
  assert_equals "$HOME_A" "$(json 'r["fm_home"]' <<<"$out")" "fm_home"
  run_pack "$HOME_A" frobnicate >/dev/null 2>&1; rc=$?
  expect_code 2 "$rc" "unknown command is a usage error"
  run_pack "$HOME_A" resolve 'bad/name' --json >/dev/null 2>&1; rc=$?
  expect_code 2 "$rc" "a project id outside [A-Za-z0-9._-] is a usage error"
  run_pack "$HOME_A" --help | grep -q 'fm-pack.sh resolve' || fail "--help prints the usage"
  pass "fm-pack: version reports the contracts and phase; bad commands and ids are usage errors"
}

test_install_digest_and_modes() {
  local out mode
  assert_present "$INSTALLED/pack.yaml" "the pack installs under data/.packs/<name>/<version>/<hex>"
  assert_equals "$CORE_DIGEST" "$(contract_digest "$INSTALLED")" "the installed tree hashes to its directory name"
  mode=$(stat -c %a "$INSTALLED/skills/diagnosing-bugs/scripts/loop.sh" 2>/dev/null || stat -f %Lp "$INSTALLED/skills/diagnosing-bugs/scripts/loop.sh")
  assert_equals 755 "$mode" "executables install as 755"
  mode=$(stat -c %a "$INSTALLED/pack.yaml" 2>/dev/null || stat -f %Lp "$INSTALLED/pack.yaml")
  assert_equals 644 "$mode" "files install as 644"
  # The digest does not depend on the umask of the checkout it came from.
  local loose="$TMP_ROOT/loose" h
  rm -rf "$loose" && mkdir -p "$loose"
  (umask 0002 && git clone -q "$SRC" "$loose/repo")
  assert_equals "$CORE_DIGEST" "$(contract_digest "$loose/repo/core")" "a umask-0002 checkout keeps the contract digest"
  h=$(new_home umask)
  out=$(cd "$loose" && umask 0002 && FM_HOME="$h" "$PACK" install "$loose/repo" "$CORE_COMMIT" core --expect-digest "$CORE_DIGEST" --json) \
    || fail "install under umask 0002 refused: $out"
  assert_equals "$CORE_DIGEST" "$(json 'r["installed"]["digest"]' <<<"$out")" "install reports the contract digest"
  out=$(run_pack "$HOME_A" install "$SRC" "$CORE_COMMIT" core --json)
  assert_equals true "$(json 'r["installed"]["already_installed"]' <<<"$out")" "a second install of the same digest is a no-op"
  pass "fm-pack: install copies the pinned tree with 644/755 modes into its contract digest, umask-independent and idempotent"
}

test_install_refusals() {
  local h out rc commit
  h=$(new_home refusals)
  expect_refusal() {  # <code> <label> install-args...
    local code=$1 label=$2
    shift 2
    out=$(run_pack "$h" install "$@" --json); rc=$?
    expect_code 3 "$rc" "$label exits 3"
    assert_contains "$out" "\"code\": \"$code\"" "$label refuses with $code"
  }
  expect_refusal digest-mismatch "a wrong --expect-digest" "$SRC" "$CORE_COMMIT" core --expect-digest "sha256:$(printf '0%.0s' $(seq 64))"

  write_pack "$SRC/linked" core 0.1.0
  ln -s ../pack.yaml "$SRC/linked/skills/link.md"
  write_pack "$SRC/nolicense" core 0.1.0
  sed -i.bak 's/license: MIT, path: tdd/path: tdd/' "$SRC/nolicense/pack.yaml" && rm -f "$SRC/nolicense/pack.yaml.bak"
  write_pack "$SRC/settings" core 0.1.0
  sed -i.bak 's/^settings: {}/settings: {permissions: {allow: [Bash]}}/' "$SRC/settings/pack.yaml" && rm -f "$SRC/settings/pack.yaml.bak"
  write_pack "$SRC/plugin" core 0.1.0
  printf '{"name": "other"}\n' > "$SRC/plugin/.claude-plugin/plugin.json"
  write_pack "$SRC/requires" core 0.1.0
  sed -i.bak 's/requires: {fm_pack: 1}/requires: {fm_pack: 2}/' "$SRC/requires/pack.yaml" && rm -f "$SRC/requires/pack.yaml.bak"
  write_pack "$SRC/missing" core 0.1.0
  rm -rf "$SRC/missing/skills/vitest"
  commit=$(commit_src)
  expect_refusal link-unsafe "a symlink in the tree" "$SRC" "$commit" linked
  expect_refusal license-missing "a vendored skill without a licence" "$SRC" "$commit" nolicense
  expect_refusal settings-unsupported "non-empty settings" "$SRC" "$commit" settings
  expect_refusal plugin-name-mismatch "a plugin.json name other than the pack name" "$SRC" "$commit" plugin
  expect_refusal requires-unsupported "an unsupported requires" "$SRC" "$commit" requires
  expect_refusal missing-skill "a vendored skill without SKILL.md" "$SRC" "$commit" missing
  [ ! -d "$h/data/.packs/core" ] || [ -z "$(ls "$h/data/.packs/core/0.1.0" 2>/dev/null)" ] || fail "a refused install left files under data/.packs"
  run_pack "$h" install "$SRC" HEAD core --json >/dev/null 2>&1; rc=$?
  expect_code 2 "$rc" "install needs a full commit sha"
  pass "fm-pack: install refuses symlinks, a wrong digest, missing licences or skills, settings, plugin-name and requires mismatches, and leaves nothing behind"
}

test_tree_walk_refusals() {
  local h pack out name
  # Names a digest line cannot carry refuse at install: git stores them, so
  # they come from a real commit.
  write_pack "$SRC/newline" core 0.1.0
  printf 'x\n' > "$SRC/newline/skills/tdd/bad"$'\n'"name.md"
  out=$(run_pack "$(new_home nl)" install "$SRC" "$(commit_src)" newline --json)
  assert_contains "$out" '"code": "path-unsafe"' "a newline in a file name refuses"
  git -C "$SRC" rm -rq --cached newline && rm -rf "$SRC/newline"
  if name=$(printf 'bad-\377.md') && (: > "$SRC/core/$name") 2>/dev/null; then
    rm -f "$SRC/core/$name"
    write_pack "$SRC/latin1" core 0.1.0
    : > "$SRC/latin1/skills/tdd/$name"
    out=$(run_pack "$(new_home latin1)" install "$SRC" "$(commit_src)" latin1 --json)
    assert_contains "$out" '"code": "path-unsafe"' "a non-UTF-8 file name refuses"
    git -C "$SRC" rm -rq --cached latin1 && rm -rf "$SRC/latin1"
    commit_src >/dev/null
  fi
  # An installed tree that can no longer be read refuses instead of skipping
  # the unreadable part (which would hash to a different, plausible digest).
  h=$(new_home unreadable)
  run_pack "$h" install "$SRC" "$CORE_COMMIT" core --json >/dev/null
  bind "$h" web "$CORE_DIGEST"
  pack="$h/data/.packs/core/0.1.0/${CORE_DIGEST#sha256:}"
  chmod 000 "$pack/skills/vitest"
  if [ -r "$pack/skills/vitest" ]; then
    chmod 755 "$pack/skills/vitest"
    pass "fm-pack: tree walk refuses newline and non-UTF-8 names (unreadable-dir case skipped: running as a user who bypasses permissions)"
    return
  fi
  out=$(run_pack "$h" resolve web --json); local rc=$?
  chmod 755 "$pack/skills/vitest"
  expect_code 0 "$rc" "resolve reports an unreadable tree as data"
  # The installed-mode check sees the 000 directory before the walk tries to
  # list it, so either refusal proves the tree was refused rather than skipped.
  case "$(json '[x["code"] for x in r["readiness"]["refusals"]]' <<<"$out")" in
    *'"tree-unreadable"'*|*'"mode-unsafe"'*) ;;
    *) fail "an unreadable directory must refuse, got: $(json 'r["readiness"]' <<<"$out")" ;;
  esac
  assert_equals 0 "$(json 'len([s for s in r["skills"] if s["id"]=="vitest" and s.get("path")])' <<<"$out")" "a refused tree lists no skill paths"
  assert_equals false "$(json 'r["readiness"]["ok"]' <<<"$out")" "an unreadable tree is not ready"
  pass "fm-pack: the tree walk refuses newline and non-UTF-8 names and an unreadable directory instead of skipping it"
}

test_resolve_unbound() {
  local out
  out=$(run_pack "$HOME_A" resolve lumen --json) || fail "resolve of an unbound project failed"
  assert_equals null "$(json 'r["binding"]' <<<"$out")" "unbound binding is null"
  assert_equals true "$(json 'r["readiness"]["ok"]' <<<"$out")" "unbound readiness is ok"
  assert_equals "no pack bound" "$(json 'r["readiness"]["note"]' <<<"$out")" "unbound note"
  assert_equals "[]" "$(json 'r["skills"]' <<<"$out")" "unbound has no skills"
  assert_equals "" "$(run_pack "$HOME_A" render-brief lumen --kind feature)" "an unbound project renders no Playbook"
  pass "fm-pack: an unbound project resolves to binding null with readiness ok, and renders nothing"
}

test_resolve_kinds_and_phases() {
  local out
  out=$(run_pack "$HOME_A" resolve web --json)
  assert_equals fm-pack.v1 "$(json 'r["schema"]' <<<"$out")" "resolve schema"
  assert_equals true "$(json 'r["readiness"]["ok"]' <<<"$out")" "bound readiness ok: $(json 'r["readiness"]' <<<"$out")"
  assert_equals "$CORE_DIGEST" "$(json 'r["binding"]["digest"]' <<<"$out")" "binding digest"
  assert_contains "$(json 'r["binding"]["binding_digest"]' <<<"$out")" "sha256:" "binding_digest"
  # Without --kind every task-row skill is listed once, with every kind it serves.
  assert_equals '["diagnosing-bugs", "tdd", "implement"]' "$(json '[s["id"] for s in r["skills"] if s["phase"]=="task"]' <<<"$out")" "all task rows without --kind"
  assert_equals '["bug", "feature", "default"]' "$(json '[s["kinds"] for s in r["skills"] if s["id"]=="tdd" and s["phase"]=="task"][0]' <<<"$out")" "tdd kinds include an aliased row"
  assert_equals 'core:tdd' "$(json '[s["name"] for s in r["skills"] if s["id"]=="tdd"][0]' <<<"$out")" "vendored skills are namespaced by the pack"
  assert_equals "$INSTALLED/skills/tdd/SKILL.md" "$(json '[s["path"] for s in r["skills"] if s["id"]=="tdd"][0]' <<<"$out")" "paths point into the installed digest"
  # Duplicates within a phase merge, and required beats optional.
  assert_equals '["required"]' "$(json '[s["requirement"] for s in r["skills"] if s["phase"]=="build" and s["id"]=="code-review"]' <<<"$out")" "code-review merges to one required build row"
  assert_equals 'global' "$(json '[s["source"] for s in r["skills"] if s["id"]=="ponytail"][0]' <<<"$out")" "allow_global skills are not vendored"
  assert_contains "$(json '[s["fallback"]["path"] for s in r["skills"] if s["id"]=="ponytail"][0]' <<<"$out")" "/policy/fallbacks/simplest.md" "fallback path"
  assert_equals '{"claude": "advisory", "codex": "advisory", "opencode": "advisory", "pi": "advisory"}' "$(json 'r["skills"][0]["delivery"]' <<<"$out")" "per-harness delivery"
  assert_equals '["npm test"]' "$(json '[v["cmd"] for v in r["verify_commands"]]' <<<"$out")" "binding verify commands replace the pack list"
  assert_equals 'binding' "$(json 'r["verify_commands"][0]["origin"]' <<<"$out")" "verify origin"
  assert_equals '["AskUserQuestion", "grilling", "clarify"]' "$(json '[b["id"] for b in r["banned"]]' <<<"$out")" "banned items"
  assert_equals "captain" "$(json 'r["project_settings"]["owner"]' <<<"$out")" "project settings are captain-owned"
  assert_equals "direct-PR" "$(json 'r["project_settings"]["mode"]' <<<"$out")" "project settings read the registry"
  assert_contains "$(json 'r["preamble"]["path"]' <<<"$out")" "$INSTALLED/policy/preamble.md" "preamble path"

  out=$(run_pack "$HOME_A" resolve web --kind feature --harness codex --json)
  assert_equals '["implement", "tdd"]' "$(json '[s["id"] for s in r["skills"] if s["phase"]=="task"]' <<<"$out")" "--kind picks the first matching row and strips the alias"
  assert_equals codex "$(json 'r["harness"]' <<<"$out")" "--harness is echoed"
  assert_equals advisory "$(json 'r["skills"][0]["delivery"]' <<<"$out")" "--harness makes delivery a plain string"
  out=$(run_pack "$HOME_A" resolve web --kind chore --json)
  assert_equals '["diagnosing-bugs", "tdd", "implement"]' "$(json '[s["id"] for s in r["skills"] if s["phase"]=="task"]' <<<"$out")" "an unmatched kind falls back to default"
  out=$(run_pack "$HOME_A" resolve web --kind docs --json)
  assert_equals '[]' "$(json '[s["id"] for s in r["skills"] if s["phase"]=="task"]' <<<"$out")" "docs needs no task skills"
  out=$(run_pack "$HOME_A" resolve web --labels Bug,qa --json)
  assert_equals '[]' "$(json '[s for s in r["skills"] if s["phase"] in ("task","build")]' <<<"$out")" "a qa label skips build and the task row"
  assert_equals '["verify", "review"]' "$(json 'r["phases"]' <<<"$out")" "a qa task runs verify and review"
  pass "fm-pack: resolve lists one row per (phase, skill) with kinds, aliases, merges, fallbacks, delivery and the bound verify commands"
}

test_resolve_conditions_and_narrowing() {
  local out h
  out=$(run_pack "$HOME_A" resolve web --json)
  assert_equals '{"when": "file:vitest.config.*", "holds": true}' "$(json '[s["conditional"] for s in r["skills"] if s["id"]=="vitest"][0]' <<<"$out")" "a nested vitest.config holds"
  h=$(new_home narrow)
  run_pack "$h" install "$SRC" "$CORE_COMMIT" core --json >/dev/null
  make_project "$h" web .firstmate/pack.yaml
  cat > "$TMP_ROOT/remotes/web-work/.firstmate/pack.yaml" <<'EOF'
narrow:
  skills_disable: [docs-check]
  verify_add: [{id: e2e, cmd: "npm run e2e"}]
  skills_enable: [anything]
verify_commands: []
EOF
  git -C "$TMP_ROOT/remotes/web-work" commit -qam narrow && git -C "$TMP_ROOT/remotes/web-work" push -q "$TMP_ROOT/remotes/web.git" main 2>/dev/null
  git -C "$h/projects/web" fetch -q origin
  # A worker's local copy is never read: only origin/<default> counts.
  printf 'narrow: {skills_disable: [tdd]}\n' > "$h/projects/web/.firstmate/pack.yaml"
  bind "$h" web "$CORE_DIGEST" "narrow:
  skills_disable: [implement]
"
  out=$(run_pack "$h" resolve web --json)
  assert_equals false "$(json '[s["conditional"]["holds"] for s in r["skills"] if s["id"]=="vitest"][0]' <<<"$out")" "no vitest config means the condition does not hold"
  assert_equals '["disabled", "repo"]' "$(json '[[s["state"], s["origin"]] for s in r["skills"] if s["id"]=="docs-check"][0]' <<<"$out")" "repo narrowing disables a skill"
  assert_equals '["disabled", "binding"]' "$(json '[[s["state"], s["origin"]] for s in r["skills"] if s["id"]=="implement"][0]' <<<"$out")" "binding narrowing disables a skill"
  assert_equals '["active"]' "$(json 'list({s["state"] for s in r["skills"] if s["id"]=="tdd"})' <<<"$out")" "the local copy's pack.yaml is ignored"
  assert_equals '["npm test", "npm run e2e"]' "$(json '[v["cmd"] for v in r["verify_commands"]]' <<<"$out")" "repo files may add verify commands but never remove them"
  assert_contains "$(json 'r["ignored"]' <<<"$out")" "repo files may only narrow" "widening keys are reported as ignored"
  assert_contains "$(json 'r["ignored"]' <<<"$out")" "verify_commands" "a repo verify_commands key is ignored"
  pass "fm-pack: file:<glob> conditions and narrowing are read from origin/<default> only, and repo files can only narrow"
}

test_resolve_ticket_skills() {
  local out ticket="$TMP_ROOT/ticket.md"
  # shellcheck disable=SC2016 # Literal backticks are ticket text, not command substitution.
  printf 'Problem\n```\nSkills: grilling\n```\nSkills: `implement`, docs-check\n' > "$ticket"
  out=$(run_pack "$HOME_A" resolve web --kind bug --ticket "$ticket" --json)
  assert_equals true "$(json 'r["readiness"]["ok"]' <<<"$out")" "a fenced Skills: line is ignored"
  assert_equals '["implement", "docs-check"]' "$(json '[s["id"] for s in r["skills"] if s["phase"]=="task"]' <<<"$out")" "Skills: replaces the task row"
  assert_equals '["build-method", "ponytail", "code-review", "vitest", "docs-check"]' "$(json '[s["id"] for s in r["skills"] if s["phase"]=="build"]' <<<"$out")" "phase skills still apply"
  out=$(run_pack "$HOME_A" resolve web --start-phase review --ticket "$ticket" --json)
  assert_equals '["review-method", "code-review", "implement", "docs-check"]' "$(json 'sorted([s["id"] for s in r["skills"]], key=["review-method","code-review","implement","docs-check"].index)' <<<"$out")" "past build, Skills: names are added to the phases run"
  assert_equals '["required"]' "$(json 'list({s["requirement"] for s in r["skills"] if s["id"]=="implement"})' <<<"$out")" "added Skills: names are required"
  printf 'Skills: tdd\nSkills: implement\n' > "$ticket"
  out=$(run_pack "$HOME_A" resolve web --ticket "$ticket" --json)
  assert_contains "$(json 'r["readiness"]["refusals"]' <<<"$out")" "skills-override-conflict" "conflicting Skills: lines refuse"
  printf 'Skills: mattpocock-skills:grilling\n' > "$ticket"
  out=$(run_pack "$HOME_A" resolve web --ticket "$ticket" --json)
  assert_contains "$(json 'r["readiness"]["refusals"]' <<<"$out")" "banned-skill" "a namespaced banned skill refuses"
  assert_equals false "$(json 'r["readiness"]["ok"]' <<<"$out")" "a refusal makes readiness not ok"
  printf 'Skills: other:nonesuch\n' > "$ticket"
  out=$(run_pack "$HOME_A" resolve web --ticket "$ticket" --json)
  assert_contains "$(json 'r["readiness"]["refusals"]' <<<"$out")" "unknown-skill" "an unknown skill refuses"
  printf 'Skills: other:tdd\n' > "$ticket"
  out=$(run_pack "$HOME_A" resolve web --ticket "$ticket" --json)
  assert_equals '["tdd"]' "$(json '[s["id"] for s in r["skills"] if s["phase"]=="task"]' <<<"$out")" "a foreign namespace is stripped when the bare id is vendored"
  pass "fm-pack: a ticket Skills: line replaces the task row, ignores fenced lines, and refuses conflicts, banned and unknown names"
}

test_readiness_refusals() {
  local h out
  h=$(new_home ready)
  bind "$h" web "$CORE_DIGEST"
  out=$(run_pack "$h" resolve web --json)
  assert_contains "$(json 'r["readiness"]["refusals"]' <<<"$out")" "pack-not-installed" "a binding to an absent pack refuses"
  run_pack "$h" install "$SRC" "$CORE_COMMIT" core --json >/dev/null
  make_project "$h" web .worktreeinclude
  printf '.env*\n' > "$TMP_ROOT/remotes/web-work/.worktreeinclude"
  git -C "$TMP_ROOT/remotes/web-work" commit -qam include && git -C "$TMP_ROOT/remotes/web-work" push -q "$TMP_ROOT/remotes/web.git" main 2>/dev/null
  git -C "$h/projects/web" fetch -q origin
  out=$(run_pack "$h" resolve web --json)
  assert_contains "$(json 'r["readiness"]["refusals"]' <<<"$out")" "secret-worktreeinclude" "a secret .worktreeinclude refuses"
  printf 'pack: core\nversion: 0.1.0\ndigest: %s\nverify_commands: []\n' "$CORE_DIGEST" > "$h/data/.pack-bindings/web.yaml"
  out=$(run_pack "$h" resolve web --json)
  assert_contains "$(json 'r["readiness"]["refusals"]' <<<"$out")" "no-verify-commands" "no verify commands refuses"
  printf 'pack: core\nversion: 0.1.0\ndigest: %s\nmerge_policy: auto\n' "$CORE_DIGEST" > "$h/data/.pack-bindings/web.yaml"
  out=$(run_pack "$h" resolve web --json)
  assert_contains "$(json 'r["readiness"]["refusals"]' <<<"$out")" "binding-invalid" "a widening binding key refuses"
  bind "$h" web "$CORE_DIGEST"
  chmod u+w "$h/data/.packs/core/0.1.0/${CORE_DIGEST#sha256:}/skills/tdd/SKILL.md"
  printf 'tampered\n' >> "$h/data/.packs/core/0.1.0/${CORE_DIGEST#sha256:}/skills/tdd/SKILL.md"
  out=$(run_pack "$h" resolve web --json) || fail "resolve exits 0 on a refusal"
  assert_contains "$(json 'r["readiness"]["refusals"]' <<<"$out")" "digest-mismatch" "a tampered install refuses"
  pass "fm-pack: readiness refuses an absent pack, a tampered digest, a secret .worktreeinclude, no verify commands and an invalid binding, exiting 0"
}

test_list_and_show() {
  local out
  out=$(run_pack "$HOME_A" list --json)
  assert_equals '["core"]' "$(json '[p["name"] for p in r["packs"]]' <<<"$out")" "list names"
  assert_equals "$CORE_COMMIT" "$(json 'r["packs"][0]["source"]["commit"]' <<<"$out")" "list source commit"
  assert_equals 8 "$(json 'r["packs"][0]["contents"]["skills"]' <<<"$out")" "list skill count"
  assert_equals '["shell-scripts"]' "$(json 'r["packs"][0]["risk"]' <<<"$out")" "list risk flags"
  assert_equals '[{"project": "web", "digest": "'"$CORE_DIGEST"'"}]' "$(json 'r["packs"][0]["bound_projects"]' <<<"$out")" "list bound projects"
  out=$(run_pack "$HOME_A" show "core@0.1.0#$CORE_DIGEST" --json)
  assert_equals core "$(json 'r["manifest"]["name"]' <<<"$out")" "show manifest"
  assert_contains "$(json 'r["files"]' <<<"$out")" '"skills/tdd/SKILL.md"' "show files"
  assert_contains "$(json 'r["notices"]' <<<"$out")" "MIT notices" "show notices"
  out=$(run_pack "$HOME_A" show nope --json) || fail "show of an absent pack exits 0"
  assert_contains "$out" "pack-not-installed" "show of an absent pack refuses in JSON"
  pass "fm-pack: list reports source, contents, risk and bindings; show returns the manifest, files and notices"
}

test_diff() {
  local commit v2 out
  write_pack "$SRC/core2" core 0.2.0
  printf 'Never merge, ever.\n' >> "$SRC/core2/policy/preamble.md"
  sed -i.bak 's/banned_skills: \[grilling, clarify\]/banned_skills: [grilling]/; s/      required: \[review-method, code-review\]/      required: [review-method, code-review, tdd]/' "$SRC/core2/pack.yaml" && rm -f "$SRC/core2/pack.yaml.bak"
  commit=$(commit_src)
  out=$(run_pack "$HOME_A" install "$SRC" "$commit" core2 --json) || fail "installing core 0.2.0 failed: $out"
  v2=$(json 'r["installed"]["digest"]' <<<"$out")
  out=$(run_pack "$HOME_A" diff web --to "core@0.2.0#$v2" --json)
  assert_equals "$CORE_DIGEST" "$(json 'r["from"]["digest"]' <<<"$out")" "diff from"
  assert_contains "$(json '[(c["kind"], c["op"], c["id"]) for c in r["changes"]]' <<<"$out")" '["skill", "add", "review:tdd"]' "diff shows the added review skill"
  assert_contains "$(json '[(c["kind"], c["op"], c["id"]) for c in r["changes"]]' <<<"$out")" '["banned", "remove", "skill:clarify"]' "diff shows the removed ban"
  assert_contains "$(json '[c.get("text_diff","") for c in r["changes"] if c["kind"]=="preamble"][0]' <<<"$out")" "+Never merge, ever." "diff carries the preamble text diff"
  assert_equals true "$(json 'r["requires_captain"]' <<<"$out")" "a banned change requires the captain"
  out=$(run_pack "$HOME_A" diff web --to "core@0.1.0#$CORE_DIGEST" --json)
  assert_equals "[]" "$(json 'r["changes"]' <<<"$out")" "diff to the bound digest is empty"
  assert_equals false "$(json 'r["requires_captain"]' <<<"$out")" "no change needs no captain"
  pass "fm-pack: diff lists skill, banned and preamble changes against a target digest and flags captain-only changes"
}

test_render_and_record() {
  local out rc h="$HOME_A"
  out=$(run_pack "$h" render-brief web --kind feature --task t1)
  assert_contains "$out" "## Playbook (pack core@0.1.0 #$CORE_DIGEST)" "Playbook heading"
  assert_contains "$out" "### Preamble" "preamble headings nest under the section"
  assert_contains "$out" "- for this task (kind=feature): $INSTALLED/skills/implement/SKILL.md, $INSTALLED/skills/tdd/SKILL.md" "task skills by installed path"
  assert_contains "$out" "ponytail (your installed skill if present; otherwise follow $INSTALLED/policy/fallbacks/simplest.md)" "fallback wording"
  assert_contains "$out" "Banned tools: AskUserQuestion. Banned skills: grilling, clarify" "banned line"
  assert_contains "$out" "$h/data/t1/evidence.json" "evidence path"
  assert_contains "$out" "- npm test" "verify command"
  assert_not_contains "$out" "$SRC" "never points at the source checkout"

  out=$(run_pack "$h" record web --task t1 --kind feature --json); rc=$?
  expect_code 0 "$rc" "record"
  assert_equals true "$(json 'r["recorded"]' <<<"$out")" "record wrote"
  assert_equals fm-pack.v1 "$(json 'r["schema"]' < "$h/data/t1/pack.json")" "pack.json is resolve output"
  assert_equals "$CORE_DIGEST" "$(json 'r["binding"]["digest"]' < "$h/data/t1/pack.json")" "pack.json binding"
  run_pack "$h" record web --task t1 --kind feature --json >/dev/null; rc=$?
  expect_code 3 "$rc" "a second record refuses"
  out=$(run_pack "$h" resolve web --task t1 --json)
  assert_equals false "$(json 'r["drift"]' <<<"$out")" "no drift"
  assert_equals "core@0.1.0#$CORE_DIGEST" "$(json 'r["recorded_pack"]' <<<"$out")" "recorded_pack"
  cp "$h/data/.pack-bindings/web.yaml" "$TMP_ROOT/web.yaml.saved"
  printf '  - {id: lint, cmd: "npm run lint"}\n' >> "$h/data/.pack-bindings/web.yaml"
  out=$(run_pack "$h" resolve web --task t1 --json)
  assert_equals true "$(json 'r["drift"]' <<<"$out")" "a changed binding drifts"
  assert_equals "$(run_pack "$h" render-brief web --kind feature --task t1 | grep -v 'npm run lint')" \
    "$(cp "$TMP_ROOT/web.yaml.saved" "$h/data/.pack-bindings/web.yaml"; run_pack "$h" render-brief web --task t1 --from-record)" \
    "render-brief --from-record reproduces the frozen record"
  out=$(run_pack "$h" record lumen --task t2 --json)
  assert_equals false "$(json 'r["recorded"]' <<<"$out")" "an unbound project records nothing"
  assert_absent "$h/data/t2/pack.json" "no record for an unbound project"
  printf 'pack: core\nversion: 0.1.0\ndigest: %s\nverify_commands: []\n' "$CORE_DIGEST" > "$h/data/.pack-bindings/web.yaml"
  run_pack "$h" record web --task t3 --json >/dev/null; rc=$?
  expect_code 3 "$rc" "record refuses a pack that is not ready"
  assert_absent "$h/data/t3/pack.json" "no record when not ready"
  cp "$TMP_ROOT/web.yaml.saved" "$h/data/.pack-bindings/web.yaml"
  pass "fm-pack: render-brief gives installed paths and verify commands; record freezes resolve once, refuses when not ready, and drift is reported"
}

test_brief_pack_option() {
  local h="$HOME_A" out rc brief playbook_line dod_line task_line
  out=$(FM_HOME="$h" "$BRIEF_SH" bp1 web --mode direct-PR --pack web --pack-kind bug 2>&1); rc=$?
  expect_code 0 "$rc" "fm-brief --pack ($out)"
  brief="$h/data/bp1/brief.md"
  assert_present "$h/data/bp1/pack.json" "--pack freezes data/<task>/pack.json"
  playbook_line=$(grep -n "^# Playbook (pack core@0.1.0 #$CORE_DIGEST)" "$brief" | cut -d: -f1)
  dod_line=$(grep -n '^# Definition of done' "$brief" | cut -d: -f1)
  task_line=$(grep -n '^# Task' "$brief" | cut -d: -f1)
  [ -n "$playbook_line" ] || fail "the brief has no Playbook section"
  [ "$playbook_line" -gt "$dod_line" ] && [ "$playbook_line" -gt "$task_line" ] || fail "the Playbook section must follow the Definition of done, outside # Task"
  assert_grep "for this task (kind=bug): $INSTALLED/skills/diagnosing-bugs/SKILL.md" "$brief" "the brief carries the bug task skills"
  # The Playbook never reaches the --intent subsections.
  # shellcheck source=bin/fm-brief-heading-lib.sh
  . "$ROOT/bin/fm-brief-heading-lib.sh"
  assert_not_contains "$(fm_brief_heading_body "$brief" "# Task")" "Playbook" "the Task section stays free of the Playbook"
  out=$(FM_HOME="$h" "$BRIEF_SH" bp2 lumen --scout --pack lumen 2>&1) || fail "an unbound --pack must still scaffold: $out"
  assert_contains "$out" "no pack is bound" "unbound --pack says so"
  assert_no_grep "# Playbook" "$h/data/bp2/brief.md" "unbound --pack adds no section"
  printf 'pack: core\nversion: 0.1.0\ndigest: %s\nverify_commands: []\n' "$CORE_DIGEST" > "$h/data/.pack-bindings/web.yaml"
  FM_HOME="$h" "$BRIEF_SH" bp3 web --mode direct-PR --pack web >/dev/null 2>&1; rc=$?
  expect_code 1 "$rc" "a pack that is not ready stops the scaffold"
  assert_absent "$h/data/bp3/brief.md" "no brief after a pack refusal"
  cp "$TMP_ROOT/web.yaml.saved" "$h/data/.pack-bindings/web.yaml"
  FM_HOME="$h" "$BRIEF_SH" bp4 --secondmate web --pack web >/dev/null 2>&1; rc=$?
  expect_code 1 "$rc" "--pack is refused on a secondmate charter"
  FM_HOME="$h" "$BRIEF_SH" bp5 web --mode direct-PR --pack-kind bug >/dev/null 2>&1; rc=$?
  expect_code 1 "$rc" "--pack-kind needs --pack"
  FM_HOME="$h" "$BRIEF_SH" bp6 web --mode direct-PR --pack ../web >/dev/null 2>&1; rc=$?
  expect_code 1 "$rc" "--pack refuses a path-like project id"
  pass "fm-brief: --pack records the pack and appends its Playbook after the Definition of done, outside # Task, and refuses before writing"
}

fm_test_run \
  test_version_and_usage \
  test_install_digest_and_modes \
  test_install_refusals \
  test_tree_walk_refusals \
  test_resolve_unbound \
  test_resolve_kinds_and_phases \
  test_resolve_conditions_and_narrowing \
  test_resolve_ticket_skills \
  test_readiness_refusals \
  test_list_and_show \
  test_diff \
  test_render_and_record \
  test_brief_pack_option
