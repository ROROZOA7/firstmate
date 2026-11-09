#!/usr/bin/env bash
# Skill packs ("playbooks"): resolve which content-addressed pack a project is
# bound to, and what a worker should read, run, and avoid under it.
# Contract fm-pack.v1; the shapes are the playbooks contracts, and this header
# owns the commands, flags, files, and exit codes.
#
# Usage:
#   fm-pack.sh version --json
#   fm-pack.sh list --json
#   fm-pack.sh show <name>[@<version>[#<digest>]] --json
#   fm-pack.sh resolve <project> [--task <id>] [--harness <h>] [--kind <k>]
#              [--labels <a,b,...>] [--start-phase build|verify|review]
#              [--ticket <file|->] --json
#   fm-pack.sh diff <project> --to <name>@<version>#<digest> --json
#   fm-pack.sh render-brief <project> --kind <k> [--harness <h>] [--task <id>]
#              [--heading-level 1|2|3]
#   fm-pack.sh render-brief <project> (--task <id> --from-record | --from-file <resolve.json>)
#              [--heading-level 1|2|3] [--require-ready]
#   fm-pack.sh install <repo> <commit-sha> <path> [--expect-digest sha256:<hex>] --json
#   fm-pack.sh record <project> --task <id> [--kind <k>] [--harness <h>]
#              [--labels <a,b,...>] [--start-phase <p>] [--ticket <file|->]
#              [--from-file <resolve.json>] [--replace] --json
#
# Files (under the active FM_HOME; FM_DATA_OVERRIDE, FM_PROJECTS_OVERRIDE and
# FM_CONFIG_OVERRIDE are honoured like every other fm-* script):
#   data/.packs/<name>/<version>/<hex>/            one installed pack, immutable;
#                                                  <hex> is its tree digest
#   data/.packs/<name>/<version>/<hex>.install.json source {repo, commit, path}
#                                                  and installed_at, written by install
#   data/.pack-bindings/<project>.yaml             captain-owned binding: pack,
#                                                  version, digest, optional
#                                                  verify_commands (replaces the
#                                                  pack list), narrow.skills_disable,
#                                                  narrow.verify_add
#   data/<task>/pack.json                          the resolve output frozen at
#                                                  spawn (written by record)
#   projects/<project>                             read only, and only through
#                                                  git at origin/<default>: the
#                                                  file:<glob> conditions,
#                                                  .firstmate/pack.yaml narrowing,
#                                                  and the .worktreeinclude
#                                                  secret check
# The project's delivery posture shown under project_settings is read through
# bin/fm-project-mode.sh, which owns the registry; a pack never sets it.
#
# Commands:
#   version       contract feature detection.
#   list          installed packs with source, contents, risk flags, and the
#                 projects bound to each digest.
#   show          one installed pack's manifest, file list, and NOTICES.md.
#   resolve       the effective playbook for a project: one skill row per
#                 (phase, skill), banned tools and skills, verify commands,
#                 preamble path, per-harness delivery (Phase A: advisory for
#                 skills, banned, preamble and verify; unsupported for hooks,
#                 mcp and settings), and readiness. Without --kind every
#                 task-row skill is listed with its kinds; with --kind (or
#                 --labels) only the first matching by_kind row, else default.
#                 A qa label, or --start-phase verify|review, skips build.
#                 --ticket reads an issue body whose `Skills:` line replaces
#                 the task row. --task adds recorded_pack and drift against
#                 data/<task>/pack.json. An unbound project reports
#                 binding:null and readiness ok with a note.
#   diff          what changes for <project> if it were bound to the target
#                 digest: resolved skills, verify commands and banned items,
#                 plus every file in either tree by per-file sha, with a text
#                 diff (content "binary-or-large" instead for a file over 64 KB,
#                 not UTF-8, or with a line over 4 KB). requires_captain is true
#                 when hooks, mcp, settings, banned items, policy/ files,
#                 .claude-plugin/, any hooks.json, *.mcp.json or hooks/ file, or
#                 any executable change, or the risk flags change.
#   render-brief  the brief's Playbook section as text: preamble, absolute
#                 installed skill paths per phase, banned items, and the verify
#                 commands the definition of done adds. --from-record renders
#                 exactly what data/<task>/pack.json froze, --from-file a saved
#                 resolve. A preamble carrying a "Delivery contract:" line is
#                 refused (exit 3), as is a pack that is not ready under
#                 --require-ready. An unbound project renders nothing.
#   install       supervisor-only: fetch one pinned commit, refuse symlinks,
#                 submodules, unlicensed vendored skills, non-empty settings,
#                 an unsupported `requires`, a plugin.json name other than the
#                 pack name, or a digest other than --expect-digest; then copy
#                 the tree with modes 644/755 into data/.packs atomically.
#   record        supervisor-only: freeze the resolve output to
#                 data/<task>/pack.json, refusing when readiness is not ok, the
#                 preamble is unsafe, or a record already exists (--replace
#                 overwrites it on purpose). --from-file records a saved resolve
#                 instead; it must itself be ready, and a fresh check of the
#                 binding, the pack digest, origin/<default> and
#                 .worktreeinclude must still pass.
#
# A binding file that exists but cannot be used is not "unbound": readiness
# fails. A project clone with no readable origin/<default> (origin/HEAD, else
# origin/main, else origin/master) refuses project-ref-unknown, so the
# .worktreeinclude check is never skipped. YAML anchors and aliases are refused
# in every file read. Verify commands must be single lines, and no rendered
# Playbook line may read as a "Delivery contract:" line.
# Installed packs are found by name and digest; the version is display only.
# Any fault in an installed tree (changed bytes, a link, a mode, an unreadable
# entry) is reported as digest-mismatch with the specific cause in `reason`.
#
# Exit codes: read-only commands report refusals inside their JSON and exit 0;
# install and record exit 3 on a refusal and still print the JSON; 2 is a usage
# error; 1 is an IO or dependency error. Nothing here runs project code, and
# nothing here writes a binding.
#
# Requires python3 with PyYAML (Debian/Ubuntu: python3-yaml) and git.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "${1:-}" in
  -h|--help|'')
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
    [ -n "${1:-}" ] && exit 0
    exit 2
    ;;
esac

command -v python3 >/dev/null 2>&1 || { echo "fm-pack: python3 is required" >&2; exit 1; }
python3 -I -c 'import yaml' >/dev/null 2>&1 || {
  echo "fm-pack: python3 PyYAML is required (Debian/Ubuntu: apt install python3-yaml; otherwise: python3 -m pip install pyyaml)" >&2
  exit 1
}

FM_ROOT="${FM_ROOT_OVERRIDE:-$(cd "$SCRIPT_DIR/.." && pwd)}"
export FM_ROOT
export FM_HOME="${FM_HOME:-${FM_ROOT_OVERRIDE:-$FM_ROOT}}"
exec python3 -I "$SCRIPT_DIR/fm-pack.py" "$@"
