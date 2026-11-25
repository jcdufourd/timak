"""The biggest files and folders of an archive, to find what new rules could drop.

Like a disk map such as GrandPerspective, but aware of the archive layout:
a file kept unchanged by 30 snapshots is one file on disk with 30 hard links,
so it is listed once, with its size, its number of links and the snapshots
it is in, under its path inside the snapshot (volume/path). Paths are the
same from one snapshot to the next, so a rule written from them catches the
file in every snapshot.

Folders are merged across snapshots the same way: the size of a folder is
the space of the distinct files found in it, each file counted once, at the
first place it was found.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .dedupe import expected_files
from .util import INFO_NAMES, PARTIAL_SUFFIX, Progress, Ticker, human, log


@dataclass
class BigFile:
    size: int  # bytes on disk
    links: int  # hard links, in this archive or elsewhere on the disk
    where: dict = field(default_factory=dict)  # (machine, volume/path) -> [snapshot stamps]


@dataclass
class BiggestStats:
    paths: int = 0  # file paths looked at
    files: int = 0  # distinct files
    size: int = 0  # bytes on disk, each file once
    snapshots: int = 0
    files_top: list = field(default_factory=list)  # [BigFile], biggest first
    folders_top: list = field(default_factory=list)  # [(machine, volume/path, bytes)], biggest first
    errors: list[str] = field(default_factory=list)


class ArchiveWalk:
    """The files of the snapshots under root: an archive, a machine folder or one snapshot.

    Yields (entry, (machine, stamp), folder inside the snapshot as volume/path)
    for each entry that is not a folder, in every finished snapshot. Files
    beside the volumes (timak's own) are left out. Errors go to `errors`;
    `done` says how many files the caller has handled, for the progress line.
    """

    def __init__(self, root: Path, errors: list[str], done=lambda: 0):
        self.root = root
        self.errors = errors
        self.done = done
        self.snapshots = 0

    def __iter__(self):
        if not self.root.is_dir():
            raise FileNotFoundError(f"no archive at {self.root}")
        ticker, progress = Ticker(), Progress(expected_files(self.root))
        # (folder, snapshot or None, path inside the snapshot)
        stack: list[tuple[str, tuple[str, str] | None, str]] = [(str(self.root), None, "")]
        while stack:
            d, snap, rel = stack.pop()
            try:
                with os.scandir(d) as it:
                    items = list(it)
            except OSError as e:
                self.errors.append(f"{d}: {e.strerror}")
                continue
            if snap is None and any(e.name in INFO_NAMES for e in items):
                p = Path(d)  # a snapshot: <machine>/<stamp>
                snap = (p.parent.name, p.name)
                self.snapshots += 1
            for entry in items:
                if entry.is_dir(follow_symlinks=False):
                    if snap is None:
                        if not entry.name.startswith(".") and not entry.name.endswith(PARTIAL_SUFFIX):
                            stack.append((entry.path, None, ""))
                    else:
                        stack.append((entry.path, snap, f"{rel}/{entry.name}" if rel else entry.name))
                elif snap is not None and rel:  # files beside the volumes are timak's own
                    yield entry, snap, rel
            if ticker.due():
                log(f"  looked at {progress.text(self.done(), 'files')} … {d}")


class Biggest:
    def __init__(self, root: Path, top: int = 100, folders: int = 50):
        self.root = root
        self.top = top
        self.n_folders = folders
        self.stats = BiggestStats()
        self._big: dict[tuple[int, int], BigFile] = {}
        self._floor = 0  # files under this size can't make the top any more
        self._seen: set[tuple[int, int]] = set()
        self._folders: dict[tuple[str, str], int] = {}  # (machine, volume/dir) -> bytes

    def run(self) -> BiggestStats:
        walk = ArchiveWalk(self.root, self.stats.errors, lambda: self.stats.paths)
        for entry, snap, rel in walk:
            self._file(entry, snap, rel)
        self.stats.snapshots = walk.snapshots
        self._finish()
        return self.stats

    def _file(self, entry: os.DirEntry, snap: tuple[str, str], rel: str) -> None:
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError as e:
            self.stats.errors.append(f"{entry.path}: {e.strerror}")
            return
        if not stat.S_ISREG(st.st_mode):
            return
        self.stats.paths += 1
        key = (st.st_dev, st.st_ino)
        size = st.st_blocks * 512
        machine, stamp = snap
        if key not in self._seen:
            self._seen.add(key)
            self.stats.files += 1
            self.stats.size += size
            parts = rel.split("/")
            for i in range(1, len(parts) + 1):
                k = (machine, "/".join(parts[:i]))
                self._folders[k] = self._folders.get(k, 0) + size
        if size <= self._floor:
            return  # (empty files never make the list)
        big = self._big.get(key)
        if big is None:
            big = self._big[key] = BigFile(size, st.st_nlink)
            if len(self._big) > 4 * self.top:
                self._trim()
        big.where.setdefault((machine, f"{rel}/{entry.name}"), []).append(stamp)

    def _trim(self) -> None:
        # The floor only rises, so a file of the final top was never under it:
        # every one of its paths was recorded.
        keep = sorted(self._big.items(), key=lambda kv: -kv[1].size)[: self.top]
        self._big = dict(keep)
        self._floor = keep[-1][1].size

    def _finish(self) -> None:
        self.stats.files_top = sorted(self._big.values(), key=lambda b: -b.size)[: self.top]
        for b in self.stats.files_top:
            for stamps in b.where.values():
                stamps.sort()
        # A folder whose space is nearly all in one subfolder says nothing more
        # than that subfolder: show only the subfolder.
        biggest_child: dict[tuple[str, str], int] = {}
        for (machine, path), size in self._folders.items():
            if "/" in path:
                parent = (machine, path.rsplit("/", 1)[0])
                biggest_child[parent] = max(biggest_child.get(parent, 0), size)
        folders = [
            (machine, path, size)
            for (machine, path), size in self._folders.items()
            if biggest_child.get((machine, path), 0) < 0.9 * size
        ]
        folders.sort(key=lambda f: -f[2])
        self.stats.folders_top = folders[: self.n_folders]


def _when(stamps: list[str]) -> str:
    stamps = sorted(stamps)
    if len(stamps) == 1:
        return stamps[0]
    return f"{len(stamps)} snapshots, {stamps[0]} … {stamps[-1]}"


def suggested_rule(path: str, home_volumes=()) -> str:
    """A rule dropping this volume/path, written the way my-rules.toml does.

    home_volumes: volumes holding home folders at their root (see rules.py)."""
    volume, _, inside = path.partition("/")
    if not inside:
        return f'[volume_roots] "{volume}" = []'
    parts = inside.split("/")
    if volume in home_volumes:
        parts = ["Users", *parts]
    if len(parts) > 2 and parts[0] == "Users" and parts[1] not in ("Shared", "Deleted Users"):
        return "- ~/" + "/".join(parts[2:])
    return "- /" + "/".join(parts)


def repr_rule(rule: str) -> str:
    return '"' + rule.replace("\\", "\\\\").replace('"', '\\"') + '",'


def report_lines(stats: BiggestStats, home_volumes=()) -> list[str]:
    lines = [f"{len(stats.files_top)} biggest files (size on disk, hard links, where):"]
    for b in stats.files_top:
        where = sorted(b.where.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        (machine, path), stamps = where[0]
        lines.append(f"  {human(b.size):>9}  {b.links:>4}  {machine}: {path}  ({_when(stamps)})")
        for (machine, path), stamps in where[1:]:
            lines.append(f"  {'':>9}  {'':>4}  {machine}: {path}  ({_when(stamps)})")
    lines += ["", f"{len(stats.folders_top)} biggest folders (each file counted once, where first found), "
              "with a rule that would drop them:"]  # fmt: skip
    for machine, path, size in stats.folders_top:
        lines.append(f"  {human(size):>9}  {machine}: {path}/")
        rule = suggested_rule(path, home_volumes)
        lines.append(f"  {'':>9}  {rule if rule.startswith('[') else repr_rule(rule + '/')}")
    return lines


def biggest(root: Path, top: int = 100, folders: int = 50) -> BiggestStats:
    return Biggest(root, top, folders).run()
