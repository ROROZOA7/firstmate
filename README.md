# Firstmate skill packs

This orphan branch of the ROROZOA7/firstmate fork holds **pack sources** only (content, no Firstmate code).
Packs are pinned by `{repo, commit, path, tree_digest}` and installed by `bin/fm-pack.sh` into `data/.packs/<name>/<version>/<digest>/`.

- Contract: wheelhouse `docs/playbooks-contracts.md` (§1–2)
- Design: wheelhouse `docs/playbooks-architecture.md`
- Each pack lives in `<name>/` with `pack.yaml`, `.claude-plugin/plugin.json`, `skills/`, `policy/` and `NOTICES.md`.
- Vendored third-party skills must carry `provenance: {repo, commit, license, path}` and attribution in `NOTICES.md`.

Packs: `core/` (pilot, PR #1).

Compute a pack's tree digest (contracts §1): `python3 -I tools/tree_digest.py core`
