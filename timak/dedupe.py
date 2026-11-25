"""Hard-link identical files across the whole archive.

`extract` only links a file to the same path in the previous snapshot. This
pass also catches files that were moved or renamed, and backups of the same
machine that came from different drives.

By default two files are merged only if their content, mode, owner, mtime and
extended attributes are identical, since hard links share all metadata.
`loose=True` compares content only (the surviving copy's metadata wins).
"""

from __future__ import annotations

import errno
import json
import os
import stat
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .util import INFO_NAMES, PARTIAL_SUFFIX, REPORT_SUFFIXES, Progress, Ticker, file_hash, human, log, xattrs

HEAD = 64 * 1024


@dataclass
class DedupeStats:
    files: int = 0
    inodes: int = 0
    merged: int = 0  # inodes that disappeared
    saved_bytes: int = 0
    errors: int = 0
    in_apps: int = 0  # duplicates inside app bundles, which macOS would not let us replace

    def summary(self) -> str:
        s = (
            f"{self.files} files ({self.inodes} distinct inodes) scanned, "
            f"{self.merged} duplicates merged, {human(self.saved_bytes)} saved"
        )
        if self.in_apps:
            s += (
                f"; {self.in_apps} files inside app bundles left as they were (macOS protects apps: give "
                "App Management permission in System Settings > Privacy & Security to link them too)"
            )
        return s + (f", {self.errors} ERRORS" if self.errors else "")


class _Inode:
    """One file's inode, kept small: an archive holds millions of them.

    Only one of its paths is kept (as its folder, shared by the files in it, and
    its name), with how many there are under the root: in a hard-linked archive
    each file has a path in every snapshot, and keeping them all once took more
    memory than the Mac had. dedupe finds the others again for the few inodes it
    replaces (`_find_paths`).
    """

    __slots__ = ("size", "dev", "ino", "mode", "uid", "gid", "mtime_ns", "nlink", "blocks", "side", "dir", "name", "count")  # fmt: skip

    def __init__(self, st: os.stat_result, folder: str, name: str, side: int = 0):
        self.size, self.dev, self.ino = st.st_size, st.st_dev, st.st_ino
        self.mode, self.uid, self.gid, self.mtime_ns = st.st_mode, st.st_uid, st.st_gid, st.st_mtime_ns
        self.nlink, self.blocks = st.st_nlink, st.st_blocks
        self.side = side  # which archive it comes from, for `compare`
        self.dir, self.name = folder, name
        self.count = 1  # paths of this inode found under the root

    @property
    def path(self) -> str:
        return os.path.join(self.dir, self.name)


def expected_files(root: Path) -> int | None:
    """Files the snapshots under root held when extracted, from their .timak.json.

    root may be an archive, a machine folder or a snapshot. An estimate for
    progress only: prune has removed files since, and folders taken over whole
    from the previous snapshot are not counted. None when nothing says.
    """
    total, found = 0, False
    for pattern in (p for name in INFO_NAMES for p in (name, f"*/{name}", f"*/*/{name}")):
        for info in root.glob(pattern):
            if any(part.startswith(".") or part.endswith(PARTIAL_SUFFIX) for part in info.parent.relative_to(root).parts):
                continue
            try:
                s = json.loads(info.read_text()).get("stats", {})
            except (OSError, ValueError):
                continue
            if "linked" in s or "copied" in s:
                total += s.get("linked", 0) + s.get("copied", 0)
                found = True
    return total if found else None


def _skip_dir(top: str, d: str, name: str) -> bool:
    return name.endswith(".partial") or (d == top and name.startswith("."))


def _skip_file(top: str, d: str, name: str) -> bool:
    return name in INFO_NAMES or (d == top and name.endswith(REPORT_SUFFIXES))


def _collect(root: Path, min_size: int, stats, side: int = 0) -> dict[int, dict[tuple[int, int], _Inode]]:
    """Regular files under root, by size then (device, inode).

    Skips unfinished snapshots and hidden folders at the top of root, such as a
    disk's .Spotlight-V100 or .Trashes. One path is kept per inode.
    """
    by_size: dict[int, dict[tuple[int, int], _Inode]] = defaultdict(dict)
    ticker = Ticker()
    progress = Progress(expected_files(root))
    seen = 0  # regular files of any size, to compare with the estimate
    top = str(root)
    stack = [top]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                items = list(it)
        except OSError as e:
            stats.errors += 1
            log(f"  error: {d}: {e.strerror}")
            continue
        for entry in items:
            if entry.is_dir(follow_symlinks=False):
                if not _skip_dir(top, d, entry.name):
                    stack.append(entry.path)
                continue
            if _skip_file(top, d, entry.name):
                continue
            st = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                continue
            seen += 1
            if st.st_size < min_size:
                continue
            stats.files += 1
            inodes = by_size[st.st_size]
            key = (st.st_dev, st.st_ino)
            node = inodes.get(key)
            if node is None:
                inodes[key] = _Inode(st, d, entry.name, side)
                stats.inodes += 1
            else:
                node.count += 1
        if ticker.due():
            log(f"  indexed {progress.text(seen, 'files')} … {d}")
    return by_size


def _find_paths(root: Path, wanted: set[tuple[int, int]]) -> dict[tuple[int, int], list[str]]:
    """Every path under root of these inodes, skipping what _collect skips.

    Reads folders only: a folder entry gives its inode without a stat, so only
    the files of these inodes are looked at more closely.
    """
    found: dict[tuple[int, int], list[str]] = defaultdict(list)
    inos = {ino for _, ino in wanted}
    ticker = Ticker()
    top = str(root)
    stack = [top]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                items = list(it)
        except OSError:
            continue  # already counted by _collect
        for entry in items:
            if entry.is_dir(follow_symlinks=False):
                if not _skip_dir(top, d, entry.name):
                    stack.append(entry.path)
            elif entry.inode() in inos and not _skip_file(top, d, entry.name):
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if (st.st_dev, st.st_ino) in wanted and stat.S_ISREG(st.st_mode):
                    found[(st.st_dev, st.st_ino)].append(entry.path)
        if ticker.due():
            log(f"  finding the other paths of the duplicates … {d}")
    return found


def _meta_key(node: _Inode, loose: bool, same_disk: bool):
    dev = node.dev if same_disk else None
    if loose:
        return dev
    return (dev, stat.S_IMODE(node.mode), node.uid, node.gid, node.mtime_ns)


def _candidates(nodes: list[_Inode], loose: bool, same_disk: bool = True) -> list[list[_Inode]]:
    """Same-size inodes whose metadata allows merging: the ones to read."""
    by_meta: dict = defaultdict(list)
    for n in nodes:
        by_meta[_meta_key(n, loose, same_disk)].append(n)
    return [c for c in by_meta.values() if len(c) > 1]


def _read_cost(size: int) -> int:
    """Bytes _groups reads at most for one candidate of this size."""
    return size + HEAD if size > HEAD else size


def _groups(
    nodes: list[_Inode], loose: bool, stats, same_disk: bool = True, advance=None
) -> list[list[_Inode]]:
    """Split same-size inodes into groups of identical files.

    With same_disk (dedupe), only files on one disk can be grouped, since a hard
    link can't cross disks; `compare` groups files from two disks too.
    `advance(n)` is told the bytes done, _read_cost per candidate in all.
    """
    advance = advance or (lambda n: None)
    result = []
    for candidates in _candidates(nodes, loose, same_disk):
        size = candidates[0].size
        groups = [candidates]
        # cheap first pass on the head of the file, then the whole content
        passes = (HEAD, None) if size > HEAD else (None,)
        for limit in passes:
            refined = []
            for g in groups:
                split: dict = defaultdict(list)
                for n in g:
                    try:
                        split[file_hash(n.path, limit)].append(n)
                    except OSError as e:
                        stats.errors += 1
                        log(f"  error: {n.path}: {e.strerror}")
                    advance(limit or size)
                refined += [s for s in split.values() if len(s) > 1]
            if limit is not None:  # the ones left out need no full read
                advance(size * (sum(len(g) for g in groups) - sum(len(g) for g in refined)))
            groups = refined
        for g in groups:
            if loose:
                result.append(g)
                continue
            by_xattr: dict = defaultdict(list)
            for n in g:
                try:
                    by_xattr[tuple(sorted(xattrs(n.path).items()))].append(n)
                except OSError as e:
                    stats.errors += 1
                    log(f"  error: {n.path}: {e.strerror}")
            result += [x for x in by_xattr.values() if len(x) > 1]
    return result


def _in_app_bundle(path: str) -> bool:
    return any(part.lower().endswith(".app") for part in path.split("/")[:-1])


def _replace_with_link(target: str, path: str) -> None:
    parent = os.path.dirname(path)
    tmp = os.path.join(parent, f".timak-link-{os.getpid()}")
    mode = None
    try:
        os.link(target, tmp)
    except PermissionError:
        # read-only directory (copied from the backup): open it up briefly
        mode = stat.S_IMODE(os.lstat(parent).st_mode)
        os.chmod(parent, mode | stat.S_IWUSR)
        os.link(target, tmp)
    try:
        os.replace(tmp, path)
    except OSError:
        os.unlink(tmp)
        raise
    finally:
        if mode is not None:
            os.chmod(parent, mode)


def dedupe(root: Path, loose: bool = False, min_size: int = 1, dry_run: bool = False) -> DedupeStats:
    stats = DedupeStats()
    by_size = _collect(root, min_size, stats)
    for size in [size for size, inodes in by_size.items() if len(inodes) < 2]:
        del by_size[size]  # nothing else has its size: free it before comparing
    to_read = count = 0
    for size, inodes in by_size.items():
        for c in _candidates(list(inodes.values()), loose):
            count += len(c)
            to_read += len(c) * _read_cost(size)
    log(
        f"indexed {stats.files} files, {stats.inodes} inodes; comparing {count} candidates, "
        f"at most {human(to_read)} to read"
    )
    ticker, progress, read = Ticker(), Progress(to_read, human), 0

    def advance(n: int) -> None:
        nonlocal read
        read += n
        if ticker.due():
            log(f"  compared {progress.text(read, 'read')}")

    merges: list[tuple[_Inode, _Inode]] = []  # (copy to keep, duplicate to replace)
    for size in sorted(by_size, reverse=True):
        nodes = list(by_size.pop(size).values())
        for group in _groups(nodes, loose, stats, advance=advance):
            group.sort(key=lambda n: -n.nlink)  # keep the most-linked copy
            merges += [(group[0], dup) for dup in group[1:]]
    del by_size

    if dry_run:
        stats.merged = len(merges)
        stats.saved_bytes = sum(dup.blocks * 512 for _, dup in merges)
        return stats

    # the duplicates' other paths, in the other snapshots, were not kept while indexing
    linked = {(dup.dev, dup.ino) for _, dup in merges if dup.count > 1}
    if linked:
        log(f"finding the other paths of {len(linked)} duplicates")
    paths = _find_paths(root, linked) if linked else {}

    for keep, dup in merges:
        target = keep.path
        dup_paths = paths.get((dup.dev, dup.ino)) or [dup.path]
        replaced = 0
        for path in dup_paths:
            try:
                _replace_with_link(target, path)
                replaced += 1
            except OSError as e:
                if e.errno == errno.EPERM and _in_app_bundle(path):
                    stats.in_apps += 1
                    continue
                stats.errors += 1
                if e.errno == errno.EMLINK:
                    log(f"  link limit reached for {target}")
                    break
                log(f"  error: {path}: {e.strerror}")
        if replaced == len(dup_paths) and dup.nlink == len(dup_paths):
            # every link of that inode was inside the archive, so its space is freed
            stats.merged += 1
            stats.saved_bytes += dup.blocks * 512
    return stats
