#!/usr/bin/env python3
"""tree_digest per playbooks-contracts §1: sha256 over sorted '<mode> <sha256> <relpath>\n'.

- mode is normalised the way git stores it: 755 if any x bit is set, else 644 (umask doesn't matter).
- Only real directories and regular files with nlink == 1 are allowed; any symlink (file or
  directory), device, FIFO, socket or hard link is refused.
- Limits: at most 4096 entries (files + directories) and 64 MiB of file content in total.
- Files are stream-hashed in 1 MiB chunks.

Usage: tree_digest.py <pack-dir>
"""
import hashlib, os, stat, sys

MAX_ENTRIES = 4096
MAX_BYTES = 64 * 1024 * 1024
CHUNK = 1024 * 1024


def refuse(msg):
    sys.exit(f"refuse: {msg}")


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def main(root):
    if not stat.S_ISDIR(os.lstat(root).st_mode):
        refuse(f"not a real directory: {root}")
    lines, entries, total = [], 0, 0
    for d, dirs, files in os.walk(root):
        for name in dirs:
            p = os.path.join(d, name)
            if not stat.S_ISDIR(os.lstat(p).st_mode):   # a symlink to a dir lands in dirs
                refuse(f"not a real directory: {p}")
        entries += len(dirs) + len(files)
        if entries > MAX_ENTRIES:
            refuse(f"more than {MAX_ENTRIES} entries")
        for name in files:
            p = os.path.join(d, name)
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                refuse(f"not a plain regular file: {p}")
            total += st.st_size
            if total > MAX_BYTES:
                refuse(f"more than {MAX_BYTES} bytes of content")
            mode = "755" if st.st_mode & 0o111 else "644"
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            lines.append(f"{mode} {file_sha256(p)} {rel}\n")
    lines.sort()   # Python str sort = code-point order
    digest = hashlib.sha256("".join(lines).encode()).hexdigest()
    print(f"sha256:{digest}  ({len(lines)} files)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: tree_digest.py <pack-dir>")
    main(sys.argv[1])
