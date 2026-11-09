#!/usr/bin/env python3
"""The engine behind bin/fm-pack.sh, whose header owns the command contract.

Everything here is data handling: it never runs project code. Git is used only
for reads of a project's origin/<default> tree (`ls-tree`, `show`) and, for
`install`, for fetching and archiving one pinned commit of a pack source.
"""
from __future__ import annotations

import datetime as _dt
import difflib
import errno
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:  # pragma: no cover - the shell entrypoint checks first
    sys.stderr.write("fm-pack: python3 PyYAML is required (install python3-yaml or `pip install pyyaml`)\n")
    sys.exit(1)

SCHEMA = "fm-pack.v1"
MANIFEST_SCHEMA = "fm-pack-manifest.v1"
CONTRACTS = ["fm-pack.v1", "fm-pack-manifest.v1", "fm-evidence.v1"]
PHASE = "A"
SUPPORTED_REQUIRES = 1
LANES = ["build", "verify", "review"]
DEFAULT_HARNESSES = ["claude", "codex", "opencode", "pi"]
MAX_TREE_ENTRIES = 4096
MAX_TREE_BYTES = 64 * 1024 * 1024
MAX_YAML_BYTES = 1024 * 1024
TEXT_DIFF_LIMIT = 64 * 1024
SECRET_GLOBS = [".env*", "*.pem", "*.key", "credentials*"]
SECRET_SAMPLES = [".env", ".env.local", ".env.production", "server.pem", "id_rsa.key", "credentials", "credentials.json"]

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX_RE = re.compile(r"^[0-9a-f]{64}$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
TASK_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
PACK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
SKILL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?::[A-Za-z0-9][A-Za-z0-9._-]*)?$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


class Refusal(Exception):
    def __init__(self, code, detail="", reason=None):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.reason = reason

    def as_dict(self):
        out = {"code": self.code, "detail": self.detail}
        if self.reason is not None:
            out["reason"] = self.reason
        return out


class UsageError(Exception):
    pass


# --- home layout -------------------------------------------------------------

def _abs(path):
    return os.path.abspath(os.path.expanduser(path))


class Home:
    def __init__(self):
        root = os.environ.get("FM_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.bin = os.path.dirname(os.path.abspath(__file__))
        self.home = _abs(os.environ.get("FM_HOME") or root)
        self.data = _abs(os.environ.get("FM_DATA_OVERRIDE") or os.path.join(self.home, "data"))
        self.projects = _abs(os.environ.get("FM_PROJECTS_OVERRIDE") or os.path.join(self.home, "projects"))
        self.config = _abs(os.environ.get("FM_CONFIG_OVERRIDE") or os.path.join(self.home, "config"))
        self.packs = os.path.join(self.data, ".packs")
        self.bindings = os.path.join(self.data, ".pack-bindings")

    def pack_dir(self, name, version, digest):
        return os.path.join(self.packs, name, version, digest.split(":", 1)[1])

    def install_record(self, name, version, digest):
        return self.pack_dir(name, version, digest) + ".install.json"


def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def envelope(home, **fields):
    out = {"schema": SCHEMA, "generated_at": now_iso(), "fm_home": home.home}
    out.update(fields)
    return out


def emit(obj):
    sys.stdout.write(json.dumps(obj, indent=2, sort_keys=False, ensure_ascii=False) + "\n")


# --- tree digest (contracts section 1) ---------------------------------------

def _safe_rel(rel):
    """Refuse names the digest lines cannot carry: newline, CR, backslash, non-UTF-8.

    os.walk decodes undecodable bytes as surrogate escapes, which a strict
    UTF-8 encode rejects.
    """
    if "\n" in rel or "\r" in rel or "\\" in rel:
        raise Refusal("path-unsafe", f"unsupported character in path: {rel!r}")
    try:
        rel.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise Refusal("path-unsafe", f"path is not valid UTF-8: {rel!r}") from None


def _walk_error(exc):
    raise Refusal("tree-unreadable", f"cannot read {exc.filename}: {exc.strerror}")


def _hash_checked(full, rel, checked):
    """Hash one file through an O_NOFOLLOW descriptor, re-checking what lstat saw.

    The fstat of the open descriptor must still be a regular file with one link
    and the size lstat reported; at most that many bytes are read, and a file
    that changed while it was read is refused rather than half-hashed.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(full, flags)
    except OSError as exc:
        code = "link-unsafe" if exc.errno == errno.ELOOP else "tree-unreadable"
        raise Refusal(code, f"cannot open {rel}: {exc.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise Refusal("link-unsafe", f"changed to a non-plain file while hashing: {rel}")
        if (st.st_dev, st.st_ino, st.st_size) != (checked.st_dev, checked.st_ino, checked.st_size):
            raise Refusal("tree-changed", f"changed while hashing: {rel}")
        h = hashlib.sha256()
        remaining = st.st_size
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise Refusal("tree-changed", f"shrank while hashing: {rel}")
            h.update(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise Refusal("tree-changed", f"grew while hashing: {rel}")
        return h.hexdigest()
    finally:
        os.close(fd)


def tree_digest(root, installed=False):
    """Return (digest, files) for a pack tree, refusing anything the contract forbids.

    files maps each relative path to (mode, sha256hex).

    mode is normalised (755 when any execute bit is set, else 644); lines
    "<mode> <sha256hex> <relpath>\\n" are sorted by code point and hashed.
    installed=True additionally requires the exact modes install writes.
    An unreadable directory or entry refuses (tree-unreadable); nothing is
    skipped.
    """
    try:
        return _tree_digest(root, installed)
    except OSError as exc:
        raise Refusal("tree-unreadable", f"{exc.filename or root}: {exc.strerror}") from None


def _tree_digest(root, installed):
    lines = []
    files = {}
    entries = 0
    total = 0
    # A trailing slash makes lstat follow a symlinked root, so strip it first.
    root = root.rstrip("/") or "/"
    root_st = os.lstat(root)
    if stat.S_ISLNK(root_st.st_mode) or not stat.S_ISDIR(root_st.st_mode):
        raise Refusal("link-unsafe", "pack root is not a plain directory")
    for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_error, followlinks=False):
        dirnames.sort()
        for name in list(dirnames):
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            _safe_rel(rel)
            entries += 1
            if stat.S_ISLNK(st.st_mode):
                raise Refusal("link-unsafe", f"symbolic link: {rel}")
            if installed and stat.S_IMODE(st.st_mode) != 0o755:
                raise Refusal("mode-unsafe", f"installed directory mode {oct(stat.S_IMODE(st.st_mode))}: {rel}")
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            _safe_rel(rel)
            entries += 1
            if entries > MAX_TREE_ENTRIES:
                raise Refusal("pack-oversized", f"more than {MAX_TREE_ENTRIES} entries")
            if stat.S_ISLNK(st.st_mode):
                raise Refusal("link-unsafe", f"symbolic link: {rel}")
            if not stat.S_ISREG(st.st_mode):
                raise Refusal("link-unsafe", f"not a regular file: {rel}")
            if st.st_nlink != 1:
                raise Refusal("link-unsafe", f"file has {st.st_nlink} hard links: {rel}")
            mode = "755" if st.st_mode & 0o111 else "644"
            if installed and stat.S_IMODE(st.st_mode) != int(mode, 8):
                raise Refusal("mode-unsafe", f"installed file mode {oct(stat.S_IMODE(st.st_mode))}: {rel}")
            total += st.st_size
            if total > MAX_TREE_BYTES:
                raise Refusal("pack-oversized", f"more than {MAX_TREE_BYTES} bytes")
            sha = _hash_checked(full, rel, st)
            lines.append(f"{mode} {sha} {rel}\n")
            files[rel] = (mode, sha)
        if entries > MAX_TREE_ENTRIES:
            raise Refusal("pack-oversized", f"more than {MAX_TREE_ENTRIES} entries")
    lines.sort()
    digest = "sha256:" + hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()
    return digest, dict(sorted(files.items()))


# --- YAML and manifest validation ---------------------------------------------

MAX_YAML_NODES = 20000


class _NoAliasLoader(yaml.SafeLoader):
    """SafeLoader that refuses anchors and aliases, so a small file cannot
    expand into a huge structure (the billion-laughs pattern)."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("YAML aliases are not allowed")
        event = self.peek_event()
        if getattr(event, "anchor", None):
            raise yaml.YAMLError("YAML anchors are not allowed")
        return super().compose_node(parent, index)


def _count_nodes(value, limit):
    stack, seen = [value], 0
    while stack:
        item = stack.pop()
        seen += 1
        if seen > limit:
            return seen
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return seen


def safe_yaml(text):
    """Parse YAML with no anchors or aliases and at most MAX_YAML_NODES nodes."""
    value = yaml.load(text, Loader=_NoAliasLoader)  # noqa: S506 - SafeLoader subclass
    if _count_nodes(value, MAX_YAML_NODES) > MAX_YAML_NODES:
        raise yaml.YAMLError(f"more than {MAX_YAML_NODES} YAML nodes")
    return value


def summary(value):
    """Describe a repo-supplied value by type and length only (str(9), list(3)).

    Repo text is never echoed: not in full, not truncated, not repr'd.
    """
    if isinstance(value, (str, bytes, list, dict, tuple)):
        return f"{type(value).__name__}({len(value)})"
    return type(value).__name__


def read_yaml(path, code):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise Refusal(code, f"missing: {path}") from None
    except OSError as exc:
        raise Refusal(code, f"cannot read {path}: {exc.strerror}") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise Refusal(code, f"not a regular file: {path}")
    if st.st_size > MAX_YAML_BYTES:
        raise Refusal(code, f"larger than {MAX_YAML_BYTES} bytes: {path}")
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_YAML_BYTES + 1)
    except OSError as exc:
        raise Refusal(code, f"cannot read {path}: {exc.strerror}") from None
    if len(raw) > MAX_YAML_BYTES:
        raise Refusal(code, f"larger than {MAX_YAML_BYTES} bytes: {path}")
    try:
        value = safe_yaml(raw.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError, RecursionError) as exc:
        raise Refusal(code, f"cannot parse {path}: {str(exc)[:300]}") from None
    return value, raw


def _str(value, label, code, pattern=None):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value:
        raise Refusal(code, f"{label} must be a non-empty string")
    if pattern is not None and not pattern.match(value):
        raise Refusal(code, f"{label} is not valid: {value!r}")
    return value


def _list(value, label, code, default=None):
    if value is None:
        return [] if default is None else default
    if not isinstance(value, list):
        raise Refusal(code, f"{label} must be a list")
    return value


def _dict(value, label, code):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise Refusal(code, f"{label} must be a mapping")
    return value


def _skill_list(value, label):
    out = []
    for item in _list(value, label, "manifest-invalid"):
        out.append(_str(item, label, "manifest-invalid", SKILL_RE))
    return out


def _inside(root, rel, label, code="path-unsafe"):
    rel = _str(rel, label, "manifest-invalid")
    if rel.startswith("/") or "\\" in rel or any(part in ("..", "") for part in rel.split("/")):
        raise Refusal(code, f"{label} must be a relative path inside the pack: {rel}")
    full = os.path.join(root, *rel.split("/"))
    real_root = os.path.realpath(root)
    real = os.path.realpath(full)
    if os.path.commonpath([real_root, real]) != real_root:
        raise Refusal(code, f"{label} escapes the pack: {rel}")
    return full


def load_manifest(root):
    """Parse and validate <root>/pack.yaml. Returns a normalised dict."""
    raw, _ = read_yaml(os.path.join(root, "pack.yaml"), "manifest-invalid")
    m = _dict(raw, "pack.yaml", "manifest-invalid")
    if m.get("schema") != MANIFEST_SCHEMA:
        raise Refusal("manifest-invalid", f"schema must be {MANIFEST_SCHEMA}")
    requires = _dict(m.get("requires"), "requires", "requires-unsupported")
    if requires.get("fm_pack") != SUPPORTED_REQUIRES or set(requires) - {"fm_pack"}:
        raise Refusal("requires-unsupported", f"requires {requires!r}; this fm-pack supports {{fm_pack: {SUPPORTED_REQUIRES}}}")
    name = _str(m.get("name"), "name", "manifest-invalid", PACK_NAME_RE)
    version = _str(m.get("version"), "version", "manifest-invalid", VERSION_RE)
    settings = m.get("settings")
    if settings not in (None, {}):
        raise Refusal("settings-unsupported", "settings is reserved for Phase B and must be empty")
    skills = _dict(m.get("skills"), "skills", "manifest-invalid")
    vendored = []
    for entry in _list(skills.get("vendored"), "skills.vendored", "manifest-invalid"):
        entry = _dict(entry, "skills.vendored[]", "manifest-invalid")
        sid = _str(entry.get("id"), "skills.vendored[].id", "manifest-invalid", SKILL_RE)
        if ":" in sid:
            raise Refusal("manifest-invalid", f"vendored skill ids are bare names: {sid}")
        prov = _dict(entry.get("provenance"), f"provenance of {sid}", "license-missing")
        if not isinstance(prov.get("license"), str) or not prov.get("license").strip():
            raise Refusal("license-missing", f"vendored skill {sid} has no provenance.license")
        vendored.append({"id": sid, "provenance": prov})
    vendored_ids = [v["id"] for v in vendored]
    if len(set(vendored_ids)) != len(vendored_ids):
        raise Refusal("manifest-invalid", "duplicate vendored skill id")
    aliases = {}
    for key, value in _dict(skills.get("aliases"), "skills.aliases", "manifest-invalid").items():
        aliases[_str(key, "alias", "manifest-invalid", SKILL_RE)] = _str(value, "alias target", "manifest-invalid", SKILL_RE)
    by_kind = []
    for row in _list(skills.get("by_kind"), "skills.by_kind", "manifest-invalid"):
        row = _dict(row, "skills.by_kind[]", "manifest-invalid")
        labels = [_str(x, "by_kind labels", "manifest-invalid").lower() for x in _list(row.get("labels"), "labels", "manifest-invalid")]
        if not labels:
            raise Refusal("manifest-invalid", "a by_kind row needs at least one label")
        by_kind.append({"labels": labels, "required": _skill_list(row.get("required"), "by_kind required")})
    default = _skill_list(_dict(skills.get("default"), "skills.default", "manifest-invalid").get("required"), "default required")
    by_phase = {}
    for phase, row in _dict(skills.get("by_phase"), "skills.by_phase", "manifest-invalid").items():
        if phase not in LANES:
            raise Refusal("manifest-invalid", f"unknown phase {phase!r}; phases are {', '.join(LANES)}")
        row = _dict(row, f"by_phase.{phase}", "manifest-invalid")
        conditional = []
        for cond in _list(row.get("conditional"), f"by_phase.{phase}.conditional", "manifest-invalid"):
            cond = _dict(cond, "conditional[]", "manifest-invalid")
            conditional.append({"id": _str(cond.get("id"), "conditional id", "manifest-invalid", SKILL_RE),
                                "when": _str(cond.get("when"), "conditional when", "manifest-invalid")})
        by_phase[phase] = {"required": _skill_list(row.get("required"), f"by_phase.{phase}.required"),
                           "optional": _skill_list(row.get("optional"), f"by_phase.{phase}.optional"),
                           "conditional": conditional}
    fallbacks = {}
    for fb in _list(skills.get("fallbacks"), "skills.fallbacks", "manifest-invalid"):
        fb = _dict(fb, "fallbacks[]", "manifest-invalid")
        target = _str(fb.get("for"), "fallback for", "manifest-invalid", SKILL_RE)
        fallbacks[target] = _inside(root, fb.get("path"), f"fallback for {target}")
    verify = []
    for vc in _list(m.get("verify_commands"), "verify_commands", "manifest-invalid"):
        vc = _dict(vc, "verify_commands[]", "manifest-invalid")
        verify.append({"id": _one_line(_str(vc.get("id"), "verify id", "manifest-invalid"), "verify id", "manifest-invalid"),
                       "cmd": _one_line(_str(vc.get("cmd"), "verify cmd", "manifest-invalid"), "verify cmd", "manifest-invalid")})
    harnesses = [_str(h, "harnesses", "manifest-invalid") for h in _list(m.get("harnesses"), "harnesses", "manifest-invalid")] or list(DEFAULT_HARNESSES)
    preamble = _inside(root, m.get("preamble"), "preamble") if m.get("preamble") is not None else None
    allow_global = set(_skill_list(skills.get("allow_global"), "allow_global"))
    unbacked = sorted(allow_global - set(fallbacks))
    if unbacked:
        raise Refusal("manifest-invalid", f"allow_global names need a fallback: {', '.join(unbacked)}")
    return {
        "raw": m,
        "name": name,
        "version": version,
        "description": m.get("description"),
        "harnesses": harnesses,
        "vendored": vendored,
        "vendored_ids": set(vendored_ids),
        "allow_global": allow_global,
        "aliases": aliases,
        "by_kind": by_kind,
        "default": default,
        "by_phase": by_phase,
        "fallbacks": fallbacks,
        "banned_tools": [_str(t, "banned_tools", "manifest-invalid") for t in _list(m.get("banned_tools"), "banned_tools", "manifest-invalid")],
        "banned_skills": _skill_list(m.get("banned_skills"), "banned_skills"),
        "preamble": preamble,
        "verify_commands": verify,
        "evidence": _dict(m.get("evidence"), "evidence", "manifest-invalid"),
        "model_hint": m.get("model_hint"),
        "hooks": [str(h.get("id", h)) if isinstance(h, dict) else str(h) for h in _list(m.get("hooks"), "hooks", "manifest-invalid")],
        "mcp": [str(s.get("id", s)) if isinstance(s, dict) else str(s) for s in _list(m.get("mcp"), "mcp", "manifest-invalid")],
    }


def check_pack_tree(root, manifest):
    """Install-time structural checks that resolve repeats as readiness."""
    plugin_path = os.path.join(root, ".claude-plugin", "plugin.json")
    try:
        with open(plugin_path, "rb") as fh:
            plugin = json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise Refusal("plugin-name-mismatch", f"cannot read .claude-plugin/plugin.json: {exc}") from None
    if not isinstance(plugin, dict) or plugin.get("name") != manifest["name"]:
        raise Refusal("plugin-name-mismatch", f"plugin.json name {plugin.get('name') if isinstance(plugin, dict) else None!r} != pack name {manifest['name']!r}")
    for sid in sorted(manifest["vendored_ids"]):
        if not os.path.isfile(os.path.join(root, "skills", sid, "SKILL.md")):
            raise Refusal("missing-skill", f"vendored skill {sid} has no skills/{sid}/SKILL.md")
    for label, path in [("preamble", manifest["preamble"])] + [(f"fallback for {k}", v) for k, v in manifest["fallbacks"].items()]:
        if path is not None and not os.path.isfile(path):
            raise Refusal("manifest-invalid", f"{label} file is missing")


# --- installed packs -----------------------------------------------------------

def _listdir(path):
    try:
        return sorted(os.listdir(path)), None
    except OSError as exc:
        return [], exc


def installed_packs(home, unreadable=None):
    """Yield (name, version, digest, dir) for every installed digest directory.

    A directory that cannot be listed is skipped here and appended to
    `unreadable` as (name or None, version or None, path, strerror), so callers
    can report it instead of crashing.
    """
    if not os.path.isdir(home.packs):
        return
    names, err = _listdir(home.packs)
    if err is not None and unreadable is not None:
        unreadable.append((None, None, home.packs, err.strerror))
    for name in names:
        ndir = os.path.join(home.packs, name)
        if not PACK_NAME_RE.match(name) or not os.path.isdir(ndir) or os.path.islink(ndir):
            continue
        versions, err = _listdir(ndir)
        if err is not None and unreadable is not None:
            unreadable.append((name, None, ndir, err.strerror))
        for version in versions:
            vdir = os.path.join(ndir, version)
            if not VERSION_RE.match(version) or not os.path.isdir(vdir) or os.path.islink(vdir):
                continue
            entries, err = _listdir(vdir)
            if err is not None and unreadable is not None:
                unreadable.append((name, version, vdir, err.strerror))
            for entry in entries:
                ddir = os.path.join(vdir, entry)
                if HEX_RE.match(entry) and os.path.isdir(ddir) and not os.path.islink(ddir):
                    yield name, version, "sha256:" + entry, ddir


_VERIFIED = {}


def find_pack(home, name, version, digest):
    """Locate an installed pack by name and digest, verify it, and return (path, manifest, files).

    The digest wins: version is display only, so the pack is found in whichever
    version directory holds that digest, and the manifest's own version is
    reported. Anything wrong with an installed tree - a changed byte, a link, a
    mode, an unreadable entry - is reported as digest-mismatch, with the
    specific cause in `reason`. Verified packs are cached per process.
    """
    if not PACK_NAME_RE.match(name or "") or not DIGEST_RE.match(digest or ""):
        raise Refusal("pack-not-installed", f"invalid pack reference {name}@{version}#{digest}")
    key = (home.packs, name, digest)
    if key in _VERIFIED:
        return _VERIFIED[key]
    hexd = digest.split(":", 1)[1]
    ndir = os.path.join(home.packs, name)

    def fault(reason, detail):
        return Refusal("digest-mismatch", f"installed tree {name}#{digest}: {detail}", reason=reason)

    candidates = []
    try:
        for parent in (home.packs, ndir):
            if os.path.islink(parent):
                raise fault("link-unsafe", f"{os.path.relpath(parent, home.data)} is a symbolic link")
        if os.path.lexists(ndir):
            if not os.path.isdir(ndir):
                raise fault("link-unsafe", f"{os.path.relpath(ndir, home.data)} is not a directory")
            for ver in sorted(os.listdir(ndir)):
                vdir = os.path.join(ndir, ver)
                path = os.path.join(vdir, hexd)
                if not VERSION_RE.match(ver):
                    continue
                if os.path.islink(vdir):
                    if os.path.lexists(path):
                        raise fault("link-unsafe", f"version directory {name}/{ver} is a symbolic link")
                    continue
                # lexists() hides EACCES as "absent"; lstat tells the two apart.
                try:
                    os.lstat(path)
                except FileNotFoundError:
                    continue
                candidates.append(path)
    except OSError as exc:
        raise fault("tree-unreadable", f"{exc.filename}: {exc.strerror}") from None
    if not candidates:
        raise Refusal("pack-not-installed", f"{name}#{digest} is not installed under data/.packs")
    if len(candidates) > 1:
        raise Refusal("digest-mismatch", f"{name}#{digest} is installed under more than one version directory",
                      reason="duplicate-install")
    path = candidates[0]
    try:
        actual, files = tree_digest(path, installed=True)
    except Refusal as exc:
        raise Refusal("digest-mismatch", f"installed tree {name}#{digest}: {exc.detail}", reason=exc.code) from None
    if actual != digest:
        raise Refusal("digest-mismatch", f"installed tree hashes to {actual}, not {digest}", reason="content-changed")
    try:
        manifest = load_manifest(path)
    except Refusal as exc:
        raise Refusal("digest-mismatch", f"installed manifest: {exc.detail}", reason=exc.code) from None
    if manifest["name"] != name or os.path.basename(os.path.dirname(path)) != manifest["version"]:
        raise Refusal("digest-mismatch", f"manifest says {manifest['name']}@{manifest['version']}, directory says "
                      f"{name}@{os.path.basename(os.path.dirname(path))}", reason="manifest-mismatch")
    _VERIFIED[key] = (path, manifest, files)
    return _VERIFIED[key]


def read_install_record(home, name, version, digest):
    try:
        with open(home.install_record(name, version, digest), "rb") as fh:
            rec = json.loads(fh.read().decode("utf-8"))
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def pack_risk(path, manifest):
    risk = []
    if manifest["hooks"]:
        risk.append("shell-hooks")
    if manifest["mcp"]:
        risk.append("network-mcp")
    for dirpath, _dirs, files in os.walk(path):
        rel_dir = os.path.relpath(dirpath, path).replace(os.sep, "/")
        for f in files:
            st = os.lstat(os.path.join(dirpath, f))
            if st.st_mode & 0o111 or "/scripts" in "/" + rel_dir:
                risk.append("shell-scripts")
                break
        if "shell-scripts" in risk:
            break
    return risk


# --- bindings ------------------------------------------------------------------

def validate_project(project):
    if not isinstance(project, str) or not PROJECT_RE.match(project) or project in (".", ".."):
        raise UsageError(f"invalid project id {project!r}; expected [A-Za-z0-9._-]{{1,100}}")


def load_binding(home, project):
    path = os.path.join(home.bindings, project + ".yaml")
    if not os.path.lexists(path):
        return None
    value, raw = read_yaml(path, "binding-invalid")
    b = _dict(value, "binding", "binding-invalid")
    unknown = set(b) - {"pack", "version", "digest", "verify_commands", "narrow"}
    if unknown:
        raise Refusal("binding-invalid", f"unknown binding keys (bindings may only narrow): {', '.join(sorted(unknown))}")
    out = {
        "pack": _str(b.get("pack"), "binding pack", "binding-invalid", PACK_NAME_RE),
        "version": _str(b.get("version"), "binding version", "binding-invalid", VERSION_RE),
        "digest": _str(b.get("digest"), "binding digest", "binding-invalid", DIGEST_RE),
        "binding_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "verify_commands": None,
    }
    if "verify_commands" in b:
        out["verify_commands"] = _verify_list(b.get("verify_commands"), "binding verify_commands", "binding-invalid")
    narrow = _dict(b.get("narrow"), "binding narrow", "binding-invalid")
    unknown = set(narrow) - {"skills_disable", "verify_add"}
    if unknown:
        raise Refusal("binding-invalid", f"unknown narrow keys: {', '.join(sorted(unknown))}")
    out["skills_disable"] = [_str(s, "skills_disable", "binding-invalid", SKILL_RE) for s in _list(narrow.get("skills_disable"), "skills_disable", "binding-invalid")]
    out["verify_add"] = _verify_list(narrow.get("verify_add"), "verify_add", "binding-invalid")
    return out


def _one_line(value, label, code):
    if "\n" in value or "\r" in value:
        raise Refusal(code, f"{label} must be a single line")
    return value


def _verify_list(value, label, code):
    out = []
    for vc in _list(value, label, code):
        vc = _dict(vc, f"{label}[]", code)
        out.append({"id": _one_line(_str(vc.get("id"), f"{label} id", code), f"{label} id", code),
                    "cmd": _one_line(_str(vc.get("cmd"), f"{label} cmd", code), f"{label} cmd", code)})
    ids = [v["id"] for v in out]
    if len(set(ids)) != len(ids):
        raise Refusal(code, f"duplicate id in {label}")
    return out


def all_bindings(home):
    out = {}
    if not os.path.isdir(home.bindings):
        return out
    for entry in sorted(os.listdir(home.bindings)):
        if not entry.endswith(".yaml"):
            continue
        project = entry[: -len(".yaml")]
        if not PROJECT_RE.match(project):
            continue
        try:
            out[project] = load_binding(home, project)
        except Refusal:
            out[project] = None
    return out


# --- project repository reads (origin/<default> only) ---------------------------

TOO_LARGE = object()


class Repo:
    """Read-only view of a project's origin/<default> tree."""

    def __init__(self, path):
        self.path = path
        self.ref = None
        self.files = None
        if not os.path.isdir(os.path.join(path, ".git")) and not os.path.isfile(os.path.join(path, ".git")):
            return
        # origin/HEAD names the default branch; without it, origin/main then
        # origin/master. Full refnames only: a short name such as origin/main
        # would resolve a local tag or branch of that name first, and pooled
        # worktrees share those with the clone.
        head = self._git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        candidates = [head] if head and head.startswith("refs/remotes/origin/") else []
        candidates += ["refs/remotes/origin/main", "refs/remotes/origin/master"]
        for ref in candidates:
            if self._git("rev-parse", "--verify", "--quiet", "--end-of-options", ref + "^{commit}"):
                self.ref = ref
                break
        if self.ref:
            listing = self._git("ls-tree", "-r", "-z", "--name-only", self.ref, raw=True)
            self.files = [f for f in (listing or "").split("\0") if f]

    def _git(self, *args, raw=False):
        try:
            res = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", self.path, *args],
                                 capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if res.returncode != 0:
            return None
        text = res.stdout.decode("utf-8", "replace")
        return text if raw else text.strip()

    @property
    def available(self):
        return self.files is not None

    def show(self, rel, limit=MAX_YAML_BYTES):
        """The blob at <fullref>:<rel>, None when absent, or TOO_LARGE when over limit."""
        if not self.available:
            return None
        spec = f"{self.ref}:{rel}"
        size = self._git("cat-file", "-s", spec)
        if size is None:
            return None
        if not size.isdigit() or int(size) > limit:
            return TOO_LARGE
        return self._git("cat-file", "blob", spec, raw=True)


def glob_holds(repo, pattern):
    """file:<glob>: a glob without '/' matches a file name at any depth; with '/', the full path."""
    if not repo.available:
        return None
    pattern = pattern.lstrip("/")
    for f in repo.files:
        target = f if "/" in pattern else f.rsplit("/", 1)[-1]
        if fnmatch.fnmatchcase(target, pattern):
            return True
    return False


def worktreeinclude_secret(repo):
    text = repo.show(".worktreeinclude", limit=64 * 1024)
    if text is None:
        return None
    if text is TOO_LARGE:
        return ".worktreeinclude is larger than 64 KB and cannot be checked"
    for lineno, line in enumerate(text.splitlines(), 1):
        pat = line.strip()
        if not pat or pat.startswith("#") or pat.startswith("!"):
            continue
        pat = pat.rstrip("/")
        while pat.startswith("**/"):
            pat = pat[3:]
        pat = pat.lstrip("/")
        base = pat.rsplit("/", 1)[-1]
        # A literal entry (config/master.key) is itself a name a secret glob
        # matches; a pattern entry (*.env*) is one that would sweep up a
        # typical secret file name.
        for g in SECRET_GLOBS:
            if fnmatch.fnmatchcase(base, g):
                return f"line {lineno} matches the secret pattern {g}"
        for sample in SECRET_SAMPLES:
            if fnmatch.fnmatchcase(sample, base) or fnmatch.fnmatchcase(sample, pat):
                return f"line {lineno} would include a file named {sample}"
    return None


def repo_gate(project, repo):
    """The repository checks every readiness path runs: origin/<default> must
    be readable, and its .worktreeinclude must not list secrets. Returns
    refusals; resolve and record --from-file both use this one helper."""
    if not repo.available:
        return [{"code": "project-ref-unknown",
                 "detail": f"projects/{project} has no readable origin/<default> (refs/remotes/origin/HEAD, main or "
                           "master); conditions, repo narrowing and the .worktreeinclude check cannot run"}]
    secret = worktreeinclude_secret(repo)
    if secret is not None:
        return [{"code": "secret-worktreeinclude", "detail": f".worktreeinclude: {secret}"}]
    return []


def parse_repo_narrowing(repo):
    """Read origin/<default>:.firstmate/pack.yaml. Returns (skills_disable, verify_add, ignored)."""
    ignored = []
    text = repo.show(".firstmate/pack.yaml") if repo.available else None
    if text is None:
        return [], [], ignored
    if text is TOO_LARGE:
        return [], [], [{"item": ".firstmate/pack.yaml", "reason": f"larger than {MAX_YAML_BYTES} bytes"}]
    try:
        value = safe_yaml(text)
    except (yaml.YAMLError, ValueError, TypeError, RecursionError) as exc:
        return [], [], [{"item": ".firstmate/pack.yaml", "reason": f"unparseable: {str(exc)[:300]}"}]
    if value is None:
        return [], [], ignored
    if not isinstance(value, dict):
        return [], [], [{"item": ".firstmate/pack.yaml", "reason": "not a mapping"}]
    for key in sorted(summary(k) for k in value if k != "narrow"):
        ignored.append({"item": f"key {key}", "reason": "repo files may only narrow"})
    narrow = value.get("narrow")
    if narrow is None:
        narrow = {}
    if not isinstance(narrow, dict):
        ignored.append({"item": "narrow", "reason": "must be a mapping"})
        narrow = {}
    for key in sorted(summary(k) for k in narrow if k not in ("skills_disable", "verify_add")):
        ignored.append({"item": f"narrow key {key}", "reason": "repo files may only narrow"})
    disable = []
    raw_disable = narrow.get("skills_disable")
    if raw_disable is not None and not isinstance(raw_disable, list):
        ignored.append({"item": "narrow.skills_disable", "reason": "must be a list of skill names"})
        raw_disable = []
    for item in raw_disable or []:
        if isinstance(item, str) and SKILL_RE.match(item):
            disable.append(item)
        else:
            ignored.append({"item": f"narrow.skills_disable {summary(item)}", "reason": "not a skill name"})
    verify = []
    raw_verify = narrow.get("verify_add")
    if raw_verify is not None and not isinstance(raw_verify, list):
        ignored.append({"item": "narrow.verify_add", "reason": "must be a list of {id, cmd}"})
        raw_verify = []
    for vc in raw_verify or []:
        if isinstance(vc, dict) and isinstance(vc.get("id"), str) and isinstance(vc.get("cmd"), str) and vc["id"] and vc["cmd"]:
            if any(c in vc["id"] + vc["cmd"] for c in "\n\r"):
                ignored.append({"item": f"narrow.verify_add {summary(vc.get('id'))}", "reason": "id and cmd must be single lines"})
                continue
            verify.append({"id": vc["id"], "cmd": vc["cmd"]})
        else:
            ignored.append({"item": f"narrow.verify_add {summary(vc)}", "reason": "needs {id, cmd}"})
    return disable, verify, ignored


# --- project settings (captain-owned, read through fm-project-mode.sh) ---------

def project_settings(home, project):
    script = os.path.join(home.bin, "fm-project-mode.sh")

    def ask(*flags):
        try:
            res = subprocess.run(["bash", script, *flags, project], capture_output=True, timeout=30, check=False,
                                 env=dict(os.environ, FM_HOME=home.home, FM_DATA_OVERRIDE=home.data))
        except (OSError, subprocess.TimeoutExpired):
            return None, ""
        if res.returncode != 0:
            return None, res.stderr.decode("utf-8", "replace")
        return res.stdout.decode("utf-8", "replace").strip(), res.stderr.decode("utf-8", "replace")

    raw, err = ask("--raw")
    mode = yolo = None
    if raw:
        parts = raw.split()
        mode = parts[0] if parts else None
        yolo = (parts[1] == "on") if len(parts) > 1 else None
    prefix, _ = ask("--branch-prefix")
    forge, _ = ask("--forge")
    pr, _ = ask("--pr")
    merge_policy = None
    policy_path = os.path.join(home.config, "merge-policy", project + ".json")
    if os.path.isfile(policy_path):
        try:
            with open(policy_path, "rb") as fh:
                merge_policy = json.loads(fh.read().decode("utf-8"))
        except (OSError, ValueError):
            merge_policy = {"error": "unreadable"}
    return {
        "registered": "not in registry" not in (err or ""),
        "mode": mode,
        "yolo": yolo,
        "branch_prefix": prefix,
        "forge": forge,
        "pr_text_mode": pr,
        "merge_policy": merge_policy,
        "sandbox_floor": None,
        "owner": "captain",
    }


# --- ticket Skills: line ---------------------------------------------------------

def parse_skills_line(body):
    """Return the names on the ticket's Skills: line, or None. Raises Refusal on conflict."""
    fence = None
    found = None
    for line in body.splitlines():
        m = FENCE_RE.match(line)
        if m:
            marker = m.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        m = re.match(r"^Skills:\s*(.*)$", line, re.I)
        if not m:
            continue
        raw = m.group(1).strip()
        names = []
        for part in raw.split(",") if raw else []:
            token = part.strip()
            if token.startswith("`") or token.endswith("`"):
                if not (len(token) > 2 and token.startswith("`") and token.endswith("`") and token.count("`") == 2):
                    raise Refusal("skills-override-conflict", f"unbalanced backticks in Skills: {token!r}")
                token = token[1:-1]
            if not SKILL_RE.match(token):
                raise Refusal("unknown-skill", f"invalid skill name on Skills: line: {token!r}")
            names.append(token)
        if found is not None and names != found:
            raise Refusal("skills-override-conflict", "the ticket has two Skills: lines that disagree")
        found = names
    return found


# --- resolve ---------------------------------------------------------------------

RANK = {"optional": 0, "conditional": 1, "required": 2}


def resolve(home, project, kind=None, harness=None, task=None, labels=None, start=None,
            ticket_body=None, binding_override=None):
    validate_project(project)
    out = envelope(home, project=project)
    if harness is not None:
        out["harness"] = harness
    out.update({"binding": None, "skills": [], "banned": [], "hooks": [], "mcp": [], "settings_keys": [],
                "verify_commands": [], "preamble": None, "model_hint": {"value": None, "owner": "dispatch"},
                "evidence": None, "project_settings": None, "ignored": [],
                "readiness": {"ok": True, "missing": [], "warnings": [], "refusals": []}})
    if kind is not None:
        out["kind"] = kind
    readiness = out["readiness"]

    def refuse(code, detail=""):
        readiness["refusals"].append({"code": code, "detail": detail})

    def warn(code, detail=""):
        readiness["warnings"].append({"code": code, "detail": detail})

    if task is not None:
        out["recorded_pack"] = None
        out["drift"] = False

    try:
        binding = binding_override if binding_override is not None else load_binding(home, project)
    except Refusal as exc:
        # A binding file exists but is unusable: not "unbound", and not ready.
        refuse(exc.code, exc.detail)
        readiness["ok"] = False
        out["binding_file"] = f"data/.pack-bindings/{project}.yaml"
        return out
    if binding is None:
        readiness["note"] = "no pack bound"
        return out
    out["binding"] = {"pack": binding["pack"], "version": binding["version"], "digest": binding["digest"],
                      "binding_digest": binding.get("binding_digest")}
    out["project_settings"] = project_settings(home, project)
    if task is not None:
        rec = read_task_record(home, task)
        if rec and isinstance(rec.get("binding"), dict):
            rb = rec["binding"]
            out["recorded_pack"] = f"{rb.get('pack')}@{rb.get('version')}#{rb.get('digest')}"
            out["drift"] = rb.get("digest") != binding["digest"] or rb.get("binding_digest") != binding.get("binding_digest")
    try:
        root, manifest, _files = find_pack(home, binding["pack"], binding["version"], binding["digest"])
    except Refusal as exc:
        readiness["refusals"].append(exc.as_dict())
        readiness["ok"] = False
        return out
    if manifest["version"] != binding["version"]:
        warn("version-label", f"the binding says {binding['version']}; the pack at that digest is {manifest['version']} (the digest wins)")
        out["binding"]["version"] = manifest["version"]

    harnesses = manifest["harnesses"]
    if harness is not None and harness not in harnesses:
        warn("harness-undeclared", f"the pack declares {', '.join(harnesses)}")

    def delivery(value):
        if harness is not None:
            return value
        return {h: value for h in harnesses}

    repo = Repo(os.path.join(home.projects, project))
    # The .worktreeinclude secret gate is never skipped silently.
    readiness["refusals"].extend(repo_gate(project, repo))
    repo_disable, repo_verify, repo_ignored = parse_repo_narrowing(repo)
    out["ignored"].extend(repo_ignored)

    banned = set(manifest["banned_skills"])

    def is_banned(name):
        bare = name.split(":", 1)[-1]
        return name in banned or bare in banned

    def canon(name):
        """Return the pack-local id for a skill name, or raise Refusal."""
        if is_banned(name):
            raise Refusal("banned-skill", name)
        target = manifest["aliases"].get(name, name)
        if ":" in target:
            bare = target.split(":", 1)[1]
            if bare in manifest["vendored_ids"] or bare in manifest["fallbacks"]:
                target = bare
            else:
                raise Refusal("unknown-skill", name)
        if is_banned(target):
            raise Refusal("banned-skill", name)
        if target in manifest["vendored_ids"] or target in manifest["allow_global"] or target in manifest["fallbacks"]:
            return target
        raise Refusal("unknown-skill", name)

    label_set = [x.lower() for x in (labels or [])]
    if kind is not None and kind.lower() != "default":
        label_set.append(kind.lower())
    phases = list(LANES)
    entry = start or ("verify" if "qa" in label_set else "build")
    if entry not in LANES:
        raise UsageError(f"--start-phase must be one of {', '.join(LANES)}")
    phases = LANES[LANES.index(entry):]
    if start is not None or "qa" in label_set:
        out["phases"] = phases

    ticket = None
    if ticket_body is not None:
        try:
            ticket = parse_skills_line(ticket_body)
        except Refusal as exc:
            refuse(exc.code, exc.detail)

    # kinds each task-row skill belongs to, across every by_kind row and default
    def canon_or_raw(name):
        try:
            return canon(name)
        except Refusal:
            return name

    kinds_of = {}
    for row in manifest["by_kind"]:
        for name in row["required"]:
            bucket = kinds_of.setdefault(canon_or_raw(name), [])
            bucket.extend(l for l in row["labels"] if l not in bucket)
    for name in manifest["default"]:
        bucket = kinds_of.setdefault(canon_or_raw(name), [])
        if "default" not in bucket:
            bucket.append("default")

    raw_rows = []  # (phase, name, requirement, origin, when, kinds)
    if "build" in phases:
        if ticket is not None:
            raw_rows += [("task", n, "required", "ticket", None, []) for n in ticket]
        elif kind is None and not labels:
            seen, ids = [], set()
            for name in [n for row in manifest["by_kind"] for n in row["required"]] + manifest["default"]:
                if canon_or_raw(name) not in ids:
                    ids.add(canon_or_raw(name))
                    seen.append(name)
            raw_rows += [("task", n, "required", "pack", None, None) for n in seen]
        else:
            chosen = next((row["required"] for row in manifest["by_kind"] if set(row["labels"]) & set(label_set)), None)
            if chosen is None:
                chosen = manifest["default"]
            raw_rows += [("task", n, "required", "pack", None, None) for n in chosen]
    elif ticket is not None:
        for phase in phases:
            raw_rows += [(phase, n, "required", "ticket", None, []) for n in ticket]
    for phase in phases:
        spec = manifest["by_phase"].get(phase, {"required": [], "optional": [], "conditional": []})
        raw_rows += [(phase, n, "required", "pack", None, []) for n in spec["required"]]
        raw_rows += [(phase, c["id"], "conditional", "pack", c["when"], []) for c in spec["conditional"]]
        raw_rows += [(phase, n, "optional", "pack", None, []) for n in spec["optional"]]

    merged = {}
    order = []
    for phase, name, requirement, origin, when, kinds in raw_rows:
        try:
            sid = canon(name)
        except Refusal as exc:
            refuse(exc.code, exc.detail if origin != "pack" else f"{exc.detail} (pack.yaml)")
            continue
        key = (phase, sid)
        if key not in merged:
            merged[key] = {"requirement": requirement, "origins": [origin], "when": when,
                           "kinds": kinds if kinds is not None else list(kinds_of.get(sid, []))}
            order.append(key)
            continue
        cur = merged[key]
        if RANK[requirement] > RANK[cur["requirement"]]:
            cur["requirement"] = requirement
            cur["when"] = when
        if origin not in cur["origins"]:
            cur["origins"].append(origin)

    disabled = {}
    for source, names in (("binding", binding.get("skills_disable") or []), ("repo", repo_disable)):
        for name in names:
            target = manifest["aliases"].get(name, name).split(":", 1)[-1]
            if any(k[1] == target for k in order):
                disabled.setdefault(target, source)
            else:
                shown = name if source == "binding" else summary(name)
                out["ignored"].append({"item": f"{source} skills_disable {shown}", "reason": "no such skill in this pack"})

    file_holds_cache = {}
    for phase, sid in order:
        cur = merged[(phase, sid)]
        conditional = None
        if cur["requirement"] == "conditional":
            m = re.match(r"^file:(.+)$", cur["when"] or "")
            if not m:
                refuse("unknown-condition", f"{sid}: {cur['when']!r}")
                holds = None
            else:
                pattern = m.group(1)
                if pattern not in file_holds_cache:
                    file_holds_cache[pattern] = glob_holds(repo, pattern)
                holds = file_holds_cache[pattern]
            conditional = {"when": cur["when"], "holds": holds}
        if sid in manifest["vendored_ids"]:
            source = "vendored"
            path = os.path.join(root, "skills", sid, "SKILL.md")
            name = f"{manifest['name']}:{sid}"
        else:
            source = "global"
            path = None
            name = sid
        state = "disabled" if sid in disabled else "active"
        origin = disabled[sid] if sid in disabled else cur["origins"][0]
        row = {"id": sid, "name": name, "phase": phase, "kinds": cur["kinds"] if phase == "task" else [],
               "requirement": cur["requirement"], "source": source, "path": path,
               "fallback": {"path": manifest["fallbacks"][sid]} if sid in manifest["fallbacks"] else None,
               "origin": origin, "origins": cur["origins"], "state": state, "conditional": conditional,
               "delivery": delivery("advisory")}
        out["skills"].append(row)
        if state != "active":
            continue
        if source == "vendored" and not os.path.isfile(path):
            if sid not in readiness["missing"]:
                readiness["missing"].append(sid)
                refuse("missing-skill", sid)
        if source == "global" and row["fallback"] is None and cur["requirement"] == "required":
            warn("unverifiable", f"{sid} must come from the worker's own skills; Phase A cannot check that it is visible")

    out["banned"] = ([{"id": t, "kind": "tool", "origin": "pack", "delivery": delivery("advisory")} for t in manifest["banned_tools"]]
                     + [{"id": s, "kind": "skill", "origin": "pack", "delivery": delivery("advisory")} for s in manifest["banned_skills"]])
    out["hooks"] = [{"id": h, "origin": "pack", "delivery": delivery("unsupported")} for h in manifest["hooks"]]
    out["mcp"] = [{"id": s, "origin": "pack", "delivery": delivery("unsupported")} for s in manifest["mcp"]]

    if binding.get("verify_commands") is not None:
        verify = [dict(v, origin="binding") for v in binding["verify_commands"]]
    else:
        verify = [dict(v, origin="pack") for v in manifest["verify_commands"]]
    for source, extra in (("binding", binding.get("verify_add") or []), ("repo", repo_verify)):
        for vc in extra:
            if any(v["id"] == vc["id"] for v in verify):
                out["ignored"].append({"item": f"{source} verify_add id {summary(vc['id'])}", "reason": "duplicate verify command id"})
                continue
            verify.append(dict(vc, origin=source))
    for v in verify:
        v["delivery"] = delivery("advisory")
    out["verify_commands"] = verify
    if not verify:
        refuse("no-verify-commands", "the pack and binding supply no verify commands; the gate invents none")

    out["preamble"] = {"path": manifest["preamble"], "delivery": delivery("advisory")} if manifest["preamble"] else None
    out["model_hint"] = {"value": manifest["model_hint"], "owner": "dispatch"}
    out["evidence"] = manifest["evidence"] or None

    readiness["ok"] = not readiness["refusals"]
    return out


def read_task_record(home, task):
    if not TASK_RE.match(task or ""):
        raise UsageError(f"invalid task id {task!r}")
    try:
        with open(os.path.join(home.data, task, "pack.json"), "rb") as fh:
            rec = json.loads(fh.read().decode("utf-8"))
        return rec if isinstance(rec, dict) else None
    except (OSError, ValueError):
        return None


# --- render-brief ----------------------------------------------------------------

def shift_headings(text, levels):
    out = []
    fence = None
    for line in text.splitlines():
        m = FENCE_RE.match(line)
        if m:
            marker = m.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            out.append(line)
            continue
        if fence is None:
            h = re.match(r"^( {0,3})(#{1,6})(\s|$)(.*)$", line)
            if h:
                depth = min(6, len(h.group(2)) + levels)
                line = "#" * depth + (h.group(3) or "") + h.group(4)
        out.append(line)
    return "\n".join(out)


def render_brief(r, heading_level=2, task_dir=None):
    b = r.get("binding")
    if not b:
        return ""
    lines = [f"{'#' * heading_level} Playbook (pack {b['pack']}@{b['version']} #{b['digest']})", ""]
    pre = r.get("preamble") or {}
    if pre.get("path"):
        with open(pre["path"], "r", encoding="utf-8") as fh:
            text = fh.read()
        lines += [shift_headings(text.rstrip("\n"), heading_level), ""]
    lines.append("Skills - read and follow before the matching step (absolute paths):")
    kind = r.get("kind") or "default"
    for phase in ["build", "task", "verify", "review"]:
        rows = [s for s in r.get("skills", []) if s["phase"] == phase and s["state"] == "active"]
        label = f"for this task (kind={kind})" if phase == "task" else phase

        def ref(s):
            if s["source"] == "vendored":
                return s["path"]
            if s.get("fallback"):
                return f"{s['id']} (your installed skill if present; otherwise follow {s['fallback']['path']})"
            return f"{s['id']} (your installed skill)"

        required = [ref(s) for s in rows if s["requirement"] == "required"]
        cond = [f"{ref(s)} (when {s['conditional']['when'][5:]} exists)" for s in rows
                if s["requirement"] == "conditional" and (s.get("conditional") or {}).get("holds") is not False]
        optional = [ref(s) for s in rows if s["requirement"] == "optional"]
        if required or phase == "task":
            lines.append(f"- {label}: {', '.join(required) if required else 'none'}")
        if cond:
            lines.append(f"- {label}, if it applies: {', '.join(cond)}")
        if optional:
            lines.append(f"- {label}, optional (use when relevant): {', '.join(optional)}")
    tools = [x["id"] for x in r.get("banned", []) if x.get("kind") == "tool"]
    skills = [x["id"] for x in r.get("banned", []) if x.get("kind") == "skill"]
    if tools or skills:
        lines.append(f"Banned tools: {', '.join(tools) or 'none'}. Banned skills: {', '.join(skills) or 'none'} (and any namespaced variant).")
    evidence = os.path.join(task_dir, "evidence.json") if task_dir else "data/<task>/evidence.json"
    lines.append(f"Definition of done adds: run every verify command at your final head and record it in `{evidence}` (schema fm-evidence.v1).")
    lines += [f"- {v['cmd']}" for v in r.get("verify_commands", [])]
    lines.append("If your sandbox cannot write that file, put the same JSON in your final report and say the file could not be written; never record a command as passing that did not run.")
    text = "\n".join(lines) + "\n"
    # Every rendered line is checked, whatever its source (preamble, skill
    # path, verify command): the section must never carry a line the spawn
    # gate could read as the brief's delivery contract.
    for line in text.splitlines():
        if re.match(r"^\s*(?:[-*]\s+)?Delivery contract:", line):
            raise Refusal("playbook-unsafe", f"a rendered Playbook line looks like a delivery contract: {line[:120]!r}")
    if any(c in v.get("cmd", "") + v.get("id", "") for v in r.get("verify_commands", []) for c in "\n\r"):
        raise Refusal("playbook-unsafe", "a verify command spans more than one line")
    return text


# --- diff ------------------------------------------------------------------------

def parse_ref(text):
    m = re.match(r"^([^@#]+)(?:@([^#]+)(?:#(.+))?)?$", text or "")
    if not m:
        raise UsageError(f"invalid pack reference {text!r}; expected <name>[@<version>[#<digest>]]")
    return m.group(1), m.group(2), m.group(3)


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


MAX_DIFF_LINE = 4096


def _text_diff(a_path, b_path, label):
    """A unified diff, or the marker "binary-or-large" for a file that is over
    the 64 KB cap, not UTF-8, or has a line longer than 4 KB."""
    for path in (a_path, b_path):
        if path:
            try:
                if os.lstat(path).st_size > TEXT_DIFF_LIMIT:
                    return "binary-or-large"
            except OSError:
                return "binary-or-large"
    a = _read_text(a_path) if a_path else ""
    b = _read_text(b_path) if b_path else ""
    if a is None or b is None or "\0" in (a + b):
        return "binary-or-large"
    if any(len(line) > MAX_DIFF_LINE for line in (a + "\n" + b).splitlines()):
        return "binary-or-large"
    if a == b:
        return None
    diff = "".join(difflib.unified_diff((a or "").splitlines(True), (b or "").splitlines(True), f"a/{label}", f"b/{label}"))
    if len(diff.encode("utf-8")) > TEXT_DIFF_LIMIT:
        diff = diff.encode("utf-8")[:TEXT_DIFF_LIMIT].decode("utf-8", "ignore") + "\n... (cut at 64 KB)\n"
    return diff


def diff(home, project, to_ref):
    validate_project(project)
    name, version, digest = parse_ref(to_ref)
    if not (version and digest):
        raise UsageError("--to needs <name>@<version>#<digest>")
    out = envelope(home, project=project, **{"from": None, "to": {"pack": name, "version": version, "digest": digest}},
                   changes=[], risk_changes=[], requires_captain=False, refusals=[])
    before = resolve(home, project)
    try:
        current = load_binding(home, project)
    except Refusal as exc:
        out["refusals"].append(exc.as_dict())
        return out
    try:
        to_root, to_manifest, to_files = find_pack(home, name, version, digest)
    except Refusal as exc:
        out["refusals"].append(exc.as_dict())
        return out
    hypothetical = dict(current or {"verify_commands": None, "skills_disable": [], "verify_add": []})
    hypothetical.update({"pack": name, "version": version, "digest": digest, "binding_digest": None})
    after = resolve(home, project, binding_override=hypothetical)
    if current:
        out["from"] = {"pack": current["pack"], "version": current["version"], "digest": current["digest"]}
    changes = out["changes"]

    def keyed(rows, key):
        return {key(r): r for r in rows}

    bs = keyed(before.get("skills", []), lambda s: f"{s['phase']}:{s['id']}")
    as_ = keyed(after.get("skills", []), lambda s: f"{s['phase']}:{s['id']}")
    fields = ("requirement", "state", "source", "conditional", "kinds")
    for key in sorted(set(bs) | set(as_)):
        a, b = bs.get(key), as_.get(key)
        if a and not b:
            changes.append({"kind": "skill", "op": "remove", "id": key, "before": {f: a.get(f) for f in fields}})
        elif b and not a:
            changes.append({"kind": "skill", "op": "add", "id": key, "after": {f: b.get(f) for f in fields}})
        else:
            if any(a.get(f) != b.get(f) for f in fields):
                changes.append({"kind": "skill", "op": "change", "id": key,
                                "before": {f: a.get(f) for f in fields}, "after": {f: b.get(f) for f in fields}})
    for kind, field, key in (("verify", "verify_commands", "id"), ("banned", "banned", "id"),
                             ("hook", "hooks", "id"), ("mcp", "mcp", "id")):
        bmap = {f"{x.get('kind', '')}:{x[key]}" if kind == "banned" else x[key]: x for x in before.get(field, [])}
        amap = {f"{x.get('kind', '')}:{x[key]}" if kind == "banned" else x[key]: x for x in after.get(field, [])}
        for k in sorted(set(bmap) | set(amap)):
            x, y = bmap.get(k), amap.get(k)
            strip = lambda v: {kk: vv for kk, vv in v.items() if kk != "delivery"}
            if x and not y:
                changes.append({"kind": kind, "op": "remove", "id": k, "before": strip(x)})
            elif y and not x:
                changes.append({"kind": kind, "op": "add", "id": k, "after": strip(y)})
            elif strip(x) != strip(y):
                changes.append({"kind": kind, "op": "change", "id": k, "before": strip(x), "after": strip(y)})
    # Every file in either tree, by per-file sha: SKILL.md text, scripts,
    # references, fallbacks, NOTICES.md and plugin.json all count.
    from_root, from_files = None, {}
    before_risk = set()
    if current:
        try:
            from_root, from_manifest, from_files = find_pack(home, current["pack"], current["version"], current["digest"])
            before_risk = set(pack_risk(from_root, from_manifest))
        except Refusal as exc:
            out["refusals"].append(dict(exc.as_dict(), side="from"))
    for rel in sorted(set(from_files) | set(to_files)):
        a, b = from_files.get(rel), to_files.get(rel)
        if a == b:
            continue
        op = "add" if a is None else "remove" if b is None else "change"
        change = {"kind": _file_kind(rel), "op": op, "id": rel,
                  "before": {"mode": a[0], "sha256": a[1]} if a else None,
                  "after": {"mode": b[0], "sha256": b[1]} if b else None}
        td = _text_diff(os.path.join(from_root, *rel.split("/")) if a else None,
                        os.path.join(to_root, *rel.split("/")) if b else None, rel)
        if td == "binary-or-large":
            change["text_diff"] = None
            change["content"] = "binary-or-large"
        elif td:
            change["text_diff"] = td
        if (a and a[0] == "755") or (b and b[0] == "755"):
            change["executable"] = True
        changes.append(change)
    after_risk = set(pack_risk(to_root, to_manifest))
    out["risk_changes"] = ([{"op": "add", "risk": r} for r in sorted(after_risk - before_risk)]
                           + [{"op": "remove", "risk": r} for r in sorted(before_risk - after_risk)])
    out["requires_captain"] = any(
        c["kind"] in ("hook", "mcp", "setting", "banned", "policy", "preamble", "plugin") or c.get("executable")
        for c in changes) or bool(out["risk_changes"])
    return out


def _file_kind(rel):
    """The change kind for one pack file.

    Anything a harness could load as a hook or an MCP server - plugin.json,
    any hooks.json or .mcp.json at any depth, and anything under a hooks/
    directory - is classified so that a change to it requires the captain.
    """
    base = rel.rsplit("/", 1)[-1]
    parts = rel.split("/")
    if rel == "policy/preamble.md":
        return "preamble"
    if rel.startswith("policy/"):
        return "policy"
    if rel == ".claude-plugin/plugin.json" or rel.startswith(".claude-plugin/"):
        return "plugin"
    if base == ".mcp.json" or base.endswith(".mcp.json"):
        return "mcp"
    if base == "hooks.json" or "hooks" in parts[:-1]:
        return "hook"
    if rel.startswith("skills/"):
        return "skill-file"
    if rel == "pack.yaml":
        return "manifest"
    return "file"


# --- list / show -----------------------------------------------------------------

def list_packs(home):
    bindings = all_bindings(home)
    packs = []
    unreadable = []
    for name, version, digest, path in installed_packs(home, unreadable):
        entry = {"name": name, "version": version, "digest": digest, "source": None, "installed_at": None,
                 "contents": {"skills": 0, "hooks": 0, "mcp": 0}, "risk": [], "update": None,
                 "bound_projects": [{"project": p, "digest": b["digest"]} for p, b in bindings.items()
                                    if b and b["pack"] == name and b["digest"] == digest],
                 "verified": False, "refusal": None}
        rec = read_install_record(home, name, version, digest)
        if isinstance(rec.get("source"), dict):
            entry["source"] = {k: rec["source"].get(k) for k in ("repo", "commit", "path")}
        entry["installed_at"] = rec.get("installed_at")
        try:
            _, manifest, _files = find_pack(home, name, version, digest)
            entry["contents"] = {"skills": len(manifest["vendored"]), "hooks": len(manifest["hooks"]), "mcp": len(manifest["mcp"])}
            entry["risk"] = pack_risk(path, manifest)
            entry["description"] = manifest["description"]
            entry["verified"] = True
        except Refusal as exc:
            entry["refusal"] = exc.as_dict()
        packs.append(entry)
    for name, version, path, why in unreadable:
        packs.append({"name": name, "version": version, "digest": None, "source": None, "installed_at": None,
                      "contents": {"skills": 0, "hooks": 0, "mcp": 0}, "risk": [], "update": None,
                      "bound_projects": [], "verified": False,
                      "refusal": {"code": "digest-mismatch", "reason": "tree-unreadable",
                                  "detail": f"{os.path.relpath(path, home.data)}: {why}"}})
    return envelope(home, packs=packs)


def show_pack(home, ref):
    name, version, digest = parse_ref(ref)
    out = envelope(home, manifest=None, digest=None, files=[], notices=None, refusals=[])
    candidates = [(n, v, d, p) for n, v, d, p in installed_packs(home)
                  if n == name and (version is None or v == version) and (digest is None or d == digest)]
    if not candidates:
        out["refusals"].append({"code": "pack-not-installed", "detail": ref})
        return out
    if len(candidates) > 1:
        out["refusals"].append({"code": "ambiguous", "detail": "matches " + ", ".join(f"{n}@{v}#{d}" for n, v, d, _ in candidates)})
        return out
    n, v, d, path = candidates[0]
    out.update({"name": n, "version": v, "digest": d, "path": path})
    try:
        _, manifest, files = find_pack(home, n, v, d)
    except Refusal as exc:
        out["refusals"].append(exc.as_dict())
        return out
    out["manifest"] = manifest["raw"]
    out["files"] = list(files)
    out["notices"] = _read_text(os.path.join(path, "NOTICES.md"))
    out["source"] = read_install_record(home, n, v, d).get("source")
    return out


# --- install / record (mutating) --------------------------------------------------

def _git(args, cwd=None):
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=600, check=False)
    if res.returncode != 0:
        raise Refusal("source-unavailable", f"git {' '.join(args[:2])} failed: {res.stderr.decode('utf-8', 'replace').strip()[:400]}")
    return res.stdout


def _copy_installed(src, dst):
    os.mkdir(dst, 0o755)
    for dirpath, dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        for d in sorted(dirnames):
            os.mkdir(os.path.join(target_dir, d))
            os.chmod(os.path.join(target_dir, d), 0o755)
        for f in sorted(filenames):
            s = os.path.join(dirpath, f)
            t = os.path.join(target_dir, f)
            mode = 0o755 if os.lstat(s).st_mode & 0o111 else 0o644
            with open(s, "rb") as fin, open(t, "xb") as fout:
                shutil.copyfileobj(fin, fout)
            os.chmod(t, mode)
    os.chmod(dst, 0o755)


def install(home, repo_url, commit, subpath, expect_digest=None):
    if not COMMIT_RE.match(commit or ""):
        raise UsageError("install needs the full 40-character commit sha")
    if subpath.startswith("/") or any(p in ("", "..", ".") for p in subpath.strip("/").split("/")):
        raise UsageError(f"invalid pack path {subpath!r}")
    subpath = subpath.strip("/")
    os.makedirs(home.packs, exist_ok=True)
    work = tempfile.mkdtemp(prefix=".install-", dir=home.packs)
    try:
        gitdir = os.path.join(work, "git")
        os.mkdir(gitdir)
        _git(["init", "-q", "--bare", gitdir])
        _git(["--git-dir", gitdir, "fetch", "-q", "--depth", "1", "--no-tags", repo_url, commit])
        tree = _git(["--git-dir", gitdir, "ls-tree", "-r", "-z", commit, "--", subpath]).decode("utf-8", "replace")
        if not tree:
            raise Refusal("source-unavailable", f"{subpath} is empty or absent at {commit}")
        for item in tree.split("\0"):
            if not item:
                continue
            mode = item.split(" ", 1)[0]
            if mode not in ("100644", "100755"):
                raise Refusal("link-unsafe", f"git mode {mode} (symlink or submodule): {item.split(chr(9), 1)[-1]}")
        tarball = _git(["--git-dir", gitdir, "archive", "--format=tar", commit, "--", subpath])
        extract = os.path.join(work, "extract")
        os.mkdir(extract)
        subprocess.run(["tar", "-x", "-f", "-", "-C", extract], input=tarball, check=True, capture_output=True, timeout=600)
        source = os.path.join(extract, *subpath.split("/"))
        digest, _ = tree_digest(source)
        if expect_digest and digest != expect_digest:
            raise Refusal("digest-mismatch", f"source tree hashes to {digest}, expected {expect_digest}")
        manifest = load_manifest(source)
        check_pack_tree(source, manifest)
        final = home.pack_dir(manifest["name"], manifest["version"], digest)
        result = {"name": manifest["name"], "version": manifest["version"], "digest": digest, "path": final}
        if os.path.isdir(final):
            existing, _ = tree_digest(final, installed=True)
            if existing != digest:
                raise Refusal("digest-mismatch", f"{final} exists with different content")
            result["already_installed"] = True
            return result
        staged = os.path.join(work, "staged")
        _copy_installed(source, staged)
        staged_digest, _ = tree_digest(staged, installed=True)
        if staged_digest != digest:
            raise Refusal("digest-mismatch", "the installed copy does not hash like its source")
        os.makedirs(os.path.dirname(final), exist_ok=True)
        record = {"schema": "fm-pack-install.v1", "source": {"repo": repo_url, "commit": commit, "path": subpath},
                  "tree_digest": digest, "installed_at": now_iso()}
        rec_tmp = os.path.join(work, "install.json")
        with open(rec_tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
            fh.write("\n")
        try:
            os.rename(staged, final)
        except OSError:
            # A concurrent install of the same digest won the rename.
            if not os.path.isdir(final) or tree_digest(final, installed=True)[0] != digest:
                raise
            result["already_installed"] = True
            return result
        os.replace(rec_tmp, home.install_record(manifest["name"], manifest["version"], digest))
        result["already_installed"] = False
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _fresh_readiness(home, project, frozen):
    """Cheap re-check of a saved resolve: the binding is unchanged, the pack
    still verifies at its digest, and origin/<default> is readable with no
    secret .worktreeinclude. Returns refusals (empty when ready)."""
    refusals = []
    try:
        binding = load_binding(home, project)
    except Refusal as exc:
        return [exc.as_dict()]
    fb = frozen.get("binding")
    if binding is None:
        return [{"code": "binding-changed", "detail": "the project is no longer bound"}] if fb else []
    if not isinstance(fb, dict) or fb.get("digest") != binding["digest"] or fb.get("binding_digest") != binding["binding_digest"]:
        return [{"code": "binding-changed", "detail": "the binding changed between render and record"}]
    try:
        find_pack(home, binding["pack"], binding["version"], binding["digest"])
    except Refusal as exc:
        refusals.append(exc.as_dict())
    refusals.extend(repo_gate(project, Repo(os.path.join(home.projects, project))))
    return refusals


def record(home, project, task, replace=False, from_file=None, **kwargs):
    """Freeze a resolve to data/<task>/pack.json.

    from_file records an already-rendered resolve (fm-brief.sh renders first,
    then records last): the saved resolve must itself be ready, and a fresh
    readiness re-check (binding, pack digest, origin ref, .worktreeinclude) must
    pass. Otherwise resolve runs here.
    """
    if not TASK_RE.match(task or ""):
        raise UsageError(f"invalid task id {task!r}")
    if from_file is not None:
        try:
            with open(from_file, "rb") as fh:
                r = json.loads(fh.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise UsageError(f"--from-file: {exc}") from None
        if not isinstance(r, dict) or r.get("schema") != SCHEMA or r.get("project") != project \
                or not isinstance(r.get("readiness"), dict):
            raise UsageError("--from-file is not a resolve of this project")
        fresh = _fresh_readiness(home, project, r)
        if fresh:
            raise Refusal("not-ready", "; ".join(f"{x['code']}: {x.get('detail', '')}" for x in fresh))
    else:
        r = resolve(home, project, **kwargs)
    if not r["readiness"].get("ok"):
        raise Refusal("not-ready", "; ".join(f"{x['code']}: {x.get('detail', '')}" for x in r["readiness"].get("refusals", [])))
    if r.get("binding") is None:
        return {"recorded": False, "note": "no pack bound", "path": None}, r
    render_brief(r)  # refuses an unsafe preamble before anything is written
    tdir = os.path.join(home.data, task)
    os.makedirs(tdir, exist_ok=True)
    target = os.path.join(tdir, "pack.json")
    if os.path.lexists(target) and not replace:
        raise Refusal("record-exists", f"{target} already exists; the record is frozen at spawn")
    fd, tmp = tempfile.mkstemp(prefix=".pack.json.", dir=tdir)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(r, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, target)
    return {"recorded": True, "path": target}, r


# --- CLI -------------------------------------------------------------------------

USAGE = """usage: fm-pack.sh version --json
       fm-pack.sh list --json
       fm-pack.sh show <name>[@<version>[#<digest>]] --json
       fm-pack.sh resolve <project> [--task <id>] [--harness <h>] [--kind <k>] [--labels <a,b>]
                  [--start-phase <build|verify|review>] [--ticket <file|->] --json
       fm-pack.sh diff <project> --to <name>@<version>#<digest> --json
       fm-pack.sh render-brief <project> --kind <k> [--harness <h>] [--task <id>] [--heading-level <1-3>]
       fm-pack.sh render-brief <project> (--task <id> --from-record | --from-file <resolve.json>) [--heading-level <1-3>]
       fm-pack.sh install <repo> <commit> <path> [--expect-digest <sha256:...>] --json
       fm-pack.sh record <project> --task <id> [--kind <k>] [--harness <h>] [--labels <a,b>] [--start-phase <p>]
                  [--ticket <file|->] [--from-file <resolve.json>] [--replace] --json"""

VALUE_FLAGS = {"--task", "--harness", "--kind", "--labels", "--start-phase", "--ticket", "--to",
               "--heading-level", "--expect-digest", "--from-file"}
BOOL_FLAGS = {"--json", "--from-record", "--replace", "--require-ready"}


def parse_args(argv):
    pos, opts = [], {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            raise UsageError(None)
        if a.startswith("--"):
            key, eq, val = a.partition("=")
            if key in VALUE_FLAGS:
                if not eq:
                    i += 1
                    if i >= len(argv):
                        raise UsageError(f"{key} needs a value")
                    val = argv[i]
                if key in opts:
                    raise UsageError(f"{key} given twice")
                opts[key] = val
            elif key in BOOL_FLAGS and not eq:
                opts[key] = True
            else:
                raise UsageError(f"unknown option {a}")
        else:
            pos.append(a)
        i += 1
    return pos, opts


def need(pos, count, verb):
    if len(pos) != count:
        raise UsageError(f"{verb}: expected {count} argument(s), got {len(pos)}")


def resolve_kwargs(opts):
    labels = [x.strip() for x in opts["--labels"].split(",") if x.strip()] if "--labels" in opts else None
    ticket = None
    if "--ticket" in opts:
        src = opts["--ticket"]
        ticket = sys.stdin.read() if src == "-" else open(src, "r", encoding="utf-8").read()
    return {"kind": opts.get("--kind"), "harness": opts.get("--harness"), "labels": labels,
            "start": opts.get("--start-phase"), "ticket_body": ticket}


def main(argv):
    home = Home()
    if not argv:
        raise UsageError("missing command")
    verb, rest = argv[0], argv[1:]
    pos, opts = parse_args(rest)
    if verb == "version":
        need(pos, 0, verb)
        emit(envelope(home, contracts=CONTRACTS, phase=PHASE))
        return 0
    if verb == "list":
        need(pos, 0, verb)
        emit(list_packs(home))
        return 0
    if verb == "show":
        need(pos, 1, verb)
        emit(show_pack(home, pos[0]))
        return 0
    if verb == "resolve":
        need(pos, 1, verb)
        emit(resolve(home, pos[0], task=opts.get("--task"), **resolve_kwargs(opts)))
        return 0
    if verb == "diff":
        need(pos, 1, verb)
        if "--to" not in opts:
            raise UsageError("diff needs --to <name>@<version>#<digest>")
        emit(diff(home, pos[0], opts["--to"]))
        return 0
    if verb == "render-brief":
        need(pos, 1, verb)
        validate_project(pos[0])
        level_text = opts.get("--heading-level", "2")
        if level_text not in ("1", "2", "3"):
            raise UsageError("--heading-level must be 1, 2 or 3")
        level = int(level_text)
        task = opts.get("--task")
        if opts.get("--from-record") or "--from-file" in opts:
            if opts.get("--from-record"):
                if not task:
                    raise UsageError("--from-record needs --task <id>")
                r = read_task_record(home, task)
                source = f"data/{task}/pack.json"
            else:
                try:
                    with open(opts["--from-file"], "rb") as fh:
                        r = json.loads(fh.read().decode("utf-8"))
                except (OSError, ValueError):
                    r = None
                source = opts["--from-file"]
            if not isinstance(r, dict):
                raise UsageError(f"no readable resolve record at {source}")
            if r.get("project") != pos[0]:
                raise UsageError(f"{source} records project {r.get('project')!r}, not {pos[0]!r}")
        else:
            if "--kind" not in opts:
                raise UsageError("render-brief needs --kind <k>")
            r = resolve(home, pos[0], **resolve_kwargs(opts))
        task_dir = os.path.join(home.data, task) if task else None
        readiness = r.get("readiness") if isinstance(r.get("readiness"), dict) else {}
        if opts.get("--require-ready") and readiness.get("ok") is not True:
            refusals = readiness.get("refusals") if isinstance(readiness.get("refusals"), list) else []
            why = "; ".join(f"{x.get('code')}: {x.get('detail')}" for x in refusals if isinstance(x, dict)) or "no readiness result"
            sys.stderr.write(f"fm-pack: render-brief refused: the pack is not ready: {why}\n")
            return 3
        try:
            text = render_brief(r, level, task_dir)
        except Refusal as exc:
            sys.stderr.write(f"fm-pack: render-brief refused: {exc.code}: {exc.detail}\n")
            return 3
        sys.stdout.write(text)
        return 0
    if verb == "install":
        need(pos, 3, verb)
        try:
            result = install(home, pos[0], pos[1], pos[2], opts.get("--expect-digest"))
        except Refusal as exc:
            emit(envelope(home, installed=None, refusals=[exc.as_dict()]))
            return 3
        emit(envelope(home, installed=result, refusals=[]))
        return 0
    if verb == "record":
        need(pos, 1, verb)
        if "--task" not in opts:
            raise UsageError("record needs --task <id>")
        kwargs = resolve_kwargs(opts)
        try:
            result, _r = record(home, pos[0], opts["--task"], replace=bool(opts.get("--replace")),
                                from_file=opts.get("--from-file"), **kwargs)
        except Refusal as exc:
            emit(envelope(home, recorded=False, refusals=[exc.as_dict()]))
            return 3
        emit(envelope(home, refusals=[], **result))
        return 0
    raise UsageError(f"unknown command {verb!r}")


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except UsageError as exc:
        if exc.args and exc.args[0]:
            sys.stderr.write(f"fm-pack: {exc.args[0]}\n")
        sys.stderr.write(USAGE + "\n")
        sys.exit(2 if exc.args and exc.args[0] else 0)
    except OSError as exc:
        sys.stderr.write(f"fm-pack: {exc}\n")
        sys.exit(1)
