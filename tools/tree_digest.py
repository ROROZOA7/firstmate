#!/usr/bin/env python3
"""tree_digest per playbooks-contracts §1: sha256 over sorted '<mode> <sha256> <relpath>\n'.
mode is normalised the way git stores it: 755 if any x bit is set, else 644 (so umask doesn't matter).
Usage: tree_digest.py <pack-dir>"""
import hashlib, os, stat, sys
root = sys.argv[1]
lines = []
for d, dirs, files in os.walk(root):
    for f in files:
        p = os.path.join(d, f); st = os.lstat(p)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            sys.exit(f"refuse: not a plain regular file: {p}")
        mode = "755" if st.st_mode & 0o111 else "644"
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        lines.append(f"{mode} {hashlib.sha256(open(p, 'rb').read()).hexdigest()} {rel}\n")
lines.sort()   # Python str sort = code-point order
print(f"sha256:{hashlib.sha256(''.join(lines).encode()).hexdigest()}  ({len(lines)} files)")
