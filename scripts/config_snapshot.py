#!/usr/bin/env python3
"""Pin a reference instance's config to a snapshot outside any checkout.

A running container must not read a path inside a development clone: a
branch switch, rebase or `git clean -fdx` would otherwise rewrite (or delete)
the config of a live postfix/dovecot, and "mail1 is up" would not identify a
configuration. `task up` therefore copies configs/<name> into an
operator-owned run directory and every daemon mounts the copy:

    <run-root>/<name>/snapshots/<id>/   immutable copy, id = content hash
    <run-root>/<name>/current           the pinned id (replaced atomically)

`snapshot` copies a source config dir into a new snapshot, then pins it.
Same content gives the same id, so re-running `up` on an unchanged config
reuses the snapshot; changed content gives a new directory, and a container
still mounting the old one keeps its bytes. Nothing is overwritten or
deleted. `current` prints the pinned snapshot the service tasks mount.

The run root defaults to $XDG_STATE_HOME/reference-mailserver
(~/.local/state/reference-mailserver). It must be outside every git work
tree, owned by the caller and not group/other-writable. Sources may contain
only regular files and directories: symlinks, .git and special files are
refused before anything is written.
"""

import argparse
import hashlib
import os
import re
import shutil
import stat
import sys
import tempfile

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
ID_RE = re.compile(r"^[0-9a-f]{20}$")

# What the service tasks mount. A source missing any of these would start a
# daemon on a half-config, so it's refused up front.
REQUIRED_FILES = ("env", "dns.env", "resolv.conf")
REQUIRED_DIRS = ("postgres-init", "postfix-overlay", "dovecot-overlay",
                 "rspamd-overlay", "fail2ban-overlay")


class SnapshotError(Exception):
    pass


def default_run_root():
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state")
    return os.path.join(state, "reference-mailserver")


def git_worktree_of(path):
    """The nearest ancestor of path (inclusive) holding a .git entry, or None."""
    cur = path
    while True:
        if os.path.lexists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def check_name(name):
    if not NAME_RE.match(name) or ".." in name:
        raise SnapshotError(f"invalid CONFIG name {name!r}")


def check_owned_dir(path, what):
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise SnapshotError(f"{what} {path} is a symlink; refusing")
    if not stat.S_ISDIR(st.st_mode):
        raise SnapshotError(f"{what} {path} is not a directory")
    if st.st_uid != os.getuid():
        raise SnapshotError(f"{what} {path} is not owned by uid {os.getuid()}")
    if st.st_mode & 0o022:
        raise SnapshotError(f"{what} {path} is group/other-writable; refusing")


def resolve_run_root(run_root, create):
    raw = os.path.expanduser(run_root or default_run_root())
    if not os.path.isabs(raw):
        raise SnapshotError(f"RUN_ROOT must be an absolute path (got {raw!r})")
    root = os.path.realpath(raw)
    if ":" in root or "," in root:
        # docker -v splits on ':' (and --mount on ','): such a path can't be mounted safely.
        raise SnapshotError(f"RUN_ROOT {root} contains ':' or ','; choose another path")
    tree = git_worktree_of(root)
    if tree is not None:
        raise SnapshotError(
            f"RUN_ROOT {root} is inside the git work tree {tree}; a running "
            "stack must not read a development checkout. Set RUN_ROOT elsewhere")
    if not os.path.lexists(root):
        if not create:
            raise SnapshotError(f"RUN_ROOT {root} does not exist")
        os.makedirs(root, mode=0o700)
    check_owned_dir(root, "RUN_ROOT")
    return root


def instance_dir(root, name, create):
    path = os.path.join(root, name)
    snaps = os.path.join(path, "snapshots")
    for p in (path, snaps):
        if not os.path.lexists(p):
            if not create:
                raise SnapshotError(f"no config snapshot for {name} under {root}")
            os.mkdir(p, 0o700)
        check_owned_dir(p, "run directory")
    return path, snaps


def scan_source(source):
    """Validate the source tree; return its entries (relpath, is_dir, mode)."""
    if os.path.islink(source):
        raise SnapshotError(f"config source {source} is a symlink; refusing")
    if not os.path.isdir(source):
        raise SnapshotError(f"config source {source} not found")
    for f in REQUIRED_FILES:
        p = os.path.join(source, f)
        if os.path.islink(p) or not os.path.isfile(p):
            raise SnapshotError(f"config source {source} lacks regular file {f}")
    for d in REQUIRED_DIRS:
        p = os.path.join(source, d)
        if os.path.islink(p) or not os.path.isdir(p):
            raise SnapshotError(f"config source {source} lacks directory {d}/")
    entries = []
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames.sort()
        for n in sorted(dirnames + filenames):
            full = os.path.join(dirpath, n)
            rel = os.path.relpath(full, source)
            if n == ".git":
                raise SnapshotError(f"config source contains {rel}; refusing")
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                raise SnapshotError(f"config source contains symlink {rel}; refusing")
            if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                raise SnapshotError(f"config source contains special file {rel}; refusing")
            entries.append((rel, stat.S_ISDIR(st.st_mode), stat.S_IMODE(st.st_mode)))
    return sorted(entries)


def tree_digest(base, entries, copy_to=None):
    """Hash (and optionally copy) entries; bytes are read once, so the hash
    always describes exactly what was written."""
    h = hashlib.sha256()
    dir_modes = []
    for rel, is_dir, mode in entries:
        # Write bits are not part of the identity: snapshot files are stored
        # read-only, and must hash the same when re-read.
        mode &= 0o755 if is_dir else 0o555
        h.update(f"{'d' if is_dir else 'f'} {mode:o} {rel}\0".encode())
        if is_dir:
            if copy_to is not None:
                os.mkdir(os.path.join(copy_to, rel), 0o700)
                dir_modes.append((rel, mode))
            continue
        # O_NOFOLLOW: a file swapped for a symlink after scan_source is refused.
        fd = os.open(os.path.join(base, rel), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            data = f.read()
        h.update(f"{len(data)}\0".encode())
        h.update(data)
        if copy_to is not None:
            dst = os.path.join(copy_to, rel)
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(data)
            # Read-only: a snapshot is never edited in place.
            os.chmod(dst, mode)
    # Directory modes last (deepest first), once their contents are written.
    for rel, mode in reversed(dir_modes):
        os.chmod(os.path.join(copy_to, rel), mode)
    return h.hexdigest()[:20]


def read_entries(snap):
    entries = []
    for dirpath, dirnames, filenames in os.walk(snap):
        for n in dirnames + filenames:
            full = os.path.join(dirpath, n)
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                raise SnapshotError(f"snapshot {snap} contains a symlink; refusing")
            entries.append((os.path.relpath(full, snap), stat.S_ISDIR(st.st_mode),
                            stat.S_IMODE(st.st_mode)))
    return sorted(entries)


def snapshot(name, source, run_root):
    check_name(name)
    entries = scan_source(source)            # before any write
    root = resolve_run_root(run_root, create=True)
    src_real = os.path.realpath(source)
    if os.path.commonpath([src_real, root]) == root:
        raise SnapshotError(f"config source {src_real} is inside RUN_ROOT {root}")
    inst, snaps = instance_dir(root, name, create=True)

    stage = tempfile.mkdtemp(prefix=".stage-", dir=snaps)
    try:
        os.chmod(stage, stat.S_IMODE(os.stat(source).st_mode) & 0o755)
        sid = tree_digest(source, entries, copy_to=stage)
        final = os.path.join(snaps, sid)
        created = False
        if not os.path.lexists(final):
            try:
                os.rename(stage, final)
                created = True
            except OSError:
                if not os.path.isdir(final):   # lost a race to the same id is fine
                    raise
        if not created:
            check_existing(final, sid)
    finally:
        if os.path.lexists(stage):
            remove_stage(stage)

    tmp = tempfile.NamedTemporaryFile("w", dir=inst, prefix=".current-", delete=False)
    with tmp:
        tmp.write(sid + "\n")
    os.replace(tmp.name, os.path.join(inst, "current"))
    print(f"  config {name}: {'new' if created else 'unchanged'} snapshot {sid} "
          f"(from {src_real}) -> {final}", file=sys.stderr)
    return final


def remove_stage(stage):
    # Only ever our own staging dir; dirs copied read-only need u+w to empty.
    for dirpath, dirnames, _ in os.walk(stage):
        for d in [dirpath] + [os.path.join(dirpath, n) for n in dirnames]:
            os.chmod(d, stat.S_IMODE(os.lstat(d).st_mode) | 0o700)
    shutil.rmtree(stage)


def check_existing(final, sid):
    st = os.lstat(final)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise SnapshotError(f"snapshot {final} is not a plain directory; refusing")
    if tree_digest(final, read_entries(final)) != sid:
        raise SnapshotError(
            f"snapshot {final} no longer matches its id (edited in place?); "
            "refusing to reuse or overwrite it")


def current(name, run_root):
    check_name(name)
    root = resolve_run_root(run_root, create=False)
    inst, snaps = instance_dir(root, name, create=False)
    pin = os.path.join(inst, "current")
    if os.path.islink(pin) or not os.path.isfile(pin):
        raise SnapshotError(f"no config snapshot pinned for {name} under {root}; "
                            f"run `task up CONFIG={name}` or `task config:snapshot CONFIG={name}`")
    with open(pin, encoding="utf-8") as f:
        sid = f.read().strip()
    if not ID_RE.match(sid):
        raise SnapshotError(f"{pin} holds an invalid snapshot id {sid!r}")
    final = os.path.join(snaps, sid)
    st = os.lstat(final) if os.path.lexists(final) else None
    if st is None or stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise SnapshotError(f"pinned snapshot {final} is missing or not a plain directory")
    return final


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="copy a config dir into a new pinned snapshot")
    s.add_argument("--config", required=True)
    s.add_argument("--source", required=True)
    s.add_argument("--run-root", default="")
    c = sub.add_parser("current", help="print the pinned snapshot dir")
    c.add_argument("--config", required=True)
    c.add_argument("--run-root", default="")
    a = p.parse_args(argv)
    try:
        if a.cmd == "snapshot":
            path = snapshot(a.config, a.source, a.run_root)
        else:
            path = current(a.config, a.run_root)
    except (SnapshotError, OSError) as e:
        print(f"  config snapshot: {e}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
