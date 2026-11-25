"""Estimate what merging two archives into one would take, dedupe included.

Both archives are only read. Files are compared the way `dedupe` compares them
(content, plus mode, owner, mtime and xattrs unless loose), but across the two
archives even when they are on different disks: once copied onto one disk, such
files would become hard links.
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .dedupe import _collect, _groups
from .util import PARTIAL_SUFFIX, human, log


@dataclass
class CompareStats:
    files: list[int] = field(default_factory=lambda: [0, 0])
    inodes_by_side: list[int] = field(default_factory=lambda: [0, 0])
    size: list[int] = field(default_factory=lambda: [0, 0])  # real size, links counted once
    saved_within: list[int] = field(default_factory=lambda: [0, 0])  # a dedupe of that archive alone would free this
    shared: int = 0  # bytes of B whose content is already in A
    shared_files: int = 0  # B inodes whose content is already in A
    common_snapshots: list[str] = field(default_factory=list)  # machine/stamp in both archives
    errors: int = 0

    @property
    def merged(self) -> int:
        """Size of the merged archive after dedupe."""
        return self.size[0] - self.saved_within[0] + self.size[1] - self.shared - self.saved_within[1]

    def summary(self, a: Path, b: Path) -> str:
        lines = []
        for side, path in enumerate((a, b)):
            lines.append(
                f"{'AB'[side]}: {path}\n"
                f"   {human(self.size[side])} on disk, {self.files[side]} file entries, "
                f"{self.inodes_by_side[side]} distinct files; "
                f"a dedupe of it alone would save {human(self.saved_within[side])}"
            )
        total = self.size[0] + self.size[1]
        lines += [
            f"content of B already in A: {human(self.shared)} ({self.shared_files} files)",
            f"merged, simply copied together: {human(total)}",
            f"merged, after dedupe:           {human(self.merged)}"
            + (f"  (saves {human(total - self.merged)}, {100 * (total - self.merged) / total:.0f}%)" if total else ""),
        ]
        if self.common_snapshots:
            shown = ", ".join(self.common_snapshots[:5]) + (" …" if len(self.common_snapshots) > 5 else "")
            lines.append(
                f"{len(self.common_snapshots)} snapshots are in both archives ({shown}): a merge keeps one copy of each. "
                "The figures above count both; after dedupe the second copy costs little if they are the same."
            )
        if self.errors:
            lines.append(f"{self.errors} ERRORS (see above); the figures leave those files out")
        return "\n".join(lines)


def _snapshots(root: Path) -> set[str]:
    """machine/stamp of the finished snapshots of an archive."""
    found = set()
    try:
        machines = [m for m in os.scandir(root) if m.is_dir(follow_symlinks=False) and not m.name.startswith(".")]
    except OSError:
        return found
    for m in machines:
        try:
            for s in os.scandir(m.path):
                if s.is_dir(follow_symlinks=False) and not s.name.endswith(PARTIAL_SUFFIX) and not s.name.startswith("."):
                    found.add(f"{m.name}/{s.name}")
        except OSError:
            continue
    return found


def compare(a: Path, b: Path, loose: bool = False, min_size: int = 1) -> CompareStats:
    stats = CompareStats()
    indexes = []
    for side, root in enumerate((a, b)):
        if not root.is_dir():
            raise FileNotFoundError(f"{root}: no such folder")
        log(f"indexing {'AB'[side]}: {root}")
        counts = _Counts()
        index = _collect(root, min_size, counts, side=side)
        stats.files[side] = counts.files
        stats.inodes_by_side[side] = counts.inodes
        stats.errors += counts.errors
        for inodes in index.values():
            for n in inodes.values():
                stats.size[side] += n.blocks * 512
        indexes.append(index)
    log(f"indexed {sum(stats.files)} files, {sum(stats.inodes_by_side)} inodes; comparing candidates")

    counts = _Counts()
    for size in sorted(set(indexes[0]) | set(indexes[1]), reverse=True):
        nodes = list(indexes[0].get(size, {}).values()) + list(indexes[1].get(size, {}).values())
        if len(nodes) < 2:
            continue
        for group in _groups(nodes, loose, counts, same_disk=False):
            by_side = defaultdict(list)
            for n in group:
                by_side[n.side].append(n)
            in_a, in_b = by_side[0], by_side[1]
            if in_a:
                keep = max(in_a, key=lambda n: n.blocks)
                stats.saved_within[0] += sum(n.blocks * 512 for n in in_a if n is not keep)
                stats.shared += sum(n.blocks * 512 for n in in_b)
                stats.shared_files += len(in_b)
            elif in_b:
                keep = max(in_b, key=lambda n: n.blocks)
                stats.saved_within[1] += sum(n.blocks * 512 for n in in_b if n is not keep)
    stats.errors += counts.errors
    stats.common_snapshots = sorted(_snapshots(a) & _snapshots(b))
    return stats


class _Counts:
    """The counters _collect and _groups update."""

    def __init__(self):
        self.files = 0
        self.inodes = 0
        self.errors = 0
