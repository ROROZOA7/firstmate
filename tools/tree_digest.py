#!/usr/bin/env python3
"""tree_digest per playbooks-contracts §1: sha256 over sorted '<mode> <sha256> <relpath>\n'.

- mode is normalised the way git stores it: 755 if any x bit is set, else 644 (umask doesn't matter).
- Only real directories and regular files with nlink == 1 are allowed; any symlink (file or
  directory), device, FIFO, socket or hard link is refused. An unreadable directory is refused,
  not skipped.
- Each file is opened with O_NOFOLLOW and re-checked on the fd (regular, nlink == 1, same dev/inode/size as lstat);
  exactly the fstat size is hashed, and a file that is shorter or longer than that is refused.
- Names containing \\n, \\r or a backslash, or that don't encode as UTF-8, are refused.
- Limits: at most 4096 entries (files + directories) and 64 MiB of file content in total.
- Files are stream-hashed in 1 MiB chunks.

Usage: tree_digest.py <pack-dir>
"""
import hashlib, os, stat, sys

MAX_ENTRIES = 4096
MAX_BYTES = 64 * 1024 * 1024
CHUNK = 1024 * 1024


class Refuse(Exception):
    pass


def check_name(path, name):
    if "\n" in name or "\r" in name or "\\" in name:
        raise Refuse(f"name contains a newline or backslash: {path!r}")
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        raise Refuse(f"name is not valid UTF-8: {path!r}")


def file_sha256(path, budget):
    """Hash exactly the bytes fstat reports; return (hexdigest, mode, size).
    Refuses before reading if the file is larger than the remaining byte budget."""
    lst = os.lstat(path)
    if not stat.S_ISREG(lst.st_mode):   # don't open a FIFO/device at all
        raise Refuse(f"not a plain regular file: {path!r}")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        raise Refuse(f"cannot open without following links: {path!r} ({e.strerror})")
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
                or (st.st_dev, st.st_ino, st.st_size) != (lst.st_dev, lst.st_ino, lst.st_size)):
            raise Refuse(f"not a plain regular file: {path!r}")
        if st.st_size > budget:
            raise Refuse(f"more than {MAX_BYTES} bytes of content")
        h, left = hashlib.sha256(), st.st_size
        while left:
            chunk = os.read(fd, min(CHUNK, left))
            if not chunk:
                raise Refuse(f"file shrank while reading: {path!r}")
            h.update(chunk)
            left -= len(chunk)
        if os.read(fd, 1):
            raise Refuse(f"file grew while reading: {path!r}")
        return h.hexdigest(), st.st_mode, st.st_size
    finally:
        os.close(fd)


def on_walk_error(err):
    raise Refuse(f"cannot read directory: {err.filename!r} ({err.strerror})")


def digest(root):
    if not stat.S_ISDIR(os.lstat(root).st_mode):
        raise Refuse(f"not a real directory: {root!r}")
    lines, entries, total = [], 0, 0
    for d, dirs, files in os.walk(root, onerror=on_walk_error):
        for name in dirs:
            p = os.path.join(d, name)
            check_name(p, name)
            if not stat.S_ISDIR(os.lstat(p).st_mode):   # a symlink to a dir lands in dirs
                raise Refuse(f"not a real directory: {p!r}")
        entries += len(dirs) + len(files)
        if entries > MAX_ENTRIES:
            raise Refuse(f"more than {MAX_ENTRIES} entries")
        for name in files:
            p = os.path.join(d, name)
            check_name(p, name)
            sha, st_mode, size = file_sha256(p, MAX_BYTES - total)
            total += size
            mode = "755" if st_mode & 0o111 else "644"
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            lines.append(f"{mode} {sha} {rel}\n")
    lines.sort()   # Python str sort = code-point order
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest(), len(lines)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: tree_digest.py <pack-dir>")
    try:
        hexd, n = digest(sys.argv[1])
    except Refuse as e:
        sys.exit(f"refuse: {e}")
    print(f"sha256:{hexd}  ({n} files)")
