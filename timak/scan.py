"""Dry run: how much of the backups would be kept, and what each rule drops.

Space is counted once per file across all scanned snapshots (that is what the
backup really occupies), and a directory already seen through a Time Machine
directory hard link is not walked again.

Identifying "the same file" depends on the format: legacy snapshots all live on
one mounted volume, so (device, inode) works. Each APFS snapshot is a separate
mount with its own device number, but an unchanged file keeps its inode number
across snapshots of the same volume, so we use (backup volume, inode, size,
mtime); size and mtime catch files rewritten in place.
"""

from __future__ import annotations

import os
import stat
from collections import defaultdict
from dataclasses import dataclass

from .rules import Rules, join
from .sources import Snapshot, select_volumes
from .util import Ticker, human, log

KEPT = "kept"


def file_volume_id(snap: Snapshot, volume_name: str) -> str | None:
    """Stable id of the volume a snapshot's files live on, when the mount's device number is not."""
    if snap.kind == "apfs":
        return f"{snap.source}|{snap.extra.get('device')}|{volume_name}"
    return None


def file_key(st: os.stat_result, volume_id: str | None) -> tuple:
    if volume_id is None:
        return (st.st_dev, st.st_ino)
    return (volume_id, st.st_ino, st.st_size, st.st_mtime_ns)


@dataclass
class Bucket:
    files: int = 0
    bytes: int = 0


class Scanner:
    def __init__(self, rules: Rules, volumes: list[str] | None = None, kept_only: bool = False):
        self.rules = rules
        self.vol_rules = rules  # the rules as they apply in the volume being scanned
        self.volumes = volumes
        self.kept_only = kept_only
        self.buckets: dict[str, Bucket] = defaultdict(Bucket)
        self.seen_files: set[tuple] = set()
        self.seen_dirs: set[tuple[int, int]] = set()
        self.kept_by_volume: dict[str, Bucket] = defaultdict(Bucket)
        self.errors = 0
        self.progress = Ticker()
        self._set_roots(rules.root_rels())

    def _set_roots(self, roots: list[str]) -> None:
        self.roots = set(roots)
        # directories above a root, e.g. "Users" for root "/Users/alice"
        self.above = {"/".join(r.split("/")[:i]) for r in roots for i in range(len(r.split("/")))}

    def scan(self, snapshots: list[Snapshot]) -> None:
        for i, snap in enumerate(snapshots, 1):
            for vol in select_volumes(snap, self.rules, self.volumes):
                log(f"[{i}/{len(snapshots)}] scanning {snap.label} {vol.name}")
                self._set_roots(self.rules.root_rels(vol))
                self.vol_rules = self.rules.for_volume(vol)
                volume_id = file_volume_id(snap, vol.original_name)
                self.kept_here = self.kept_by_volume[vol.name]
                if self.kept_only:
                    for root in self.roots:
                        if not root or self.vol_rules.keeps(root, True):
                            self._walk(vol.path, root, None, volume_id)
                else:
                    self._walk(vol.path, "", None if "" in self.roots else "above", volume_id)

    def _label_for(self, rel: str, is_dir: bool, parent_label: str | None) -> str | None:
        """None = inside a root and still undecided; any other string = bucket for the whole subtree."""
        if parent_label == "above":
            if rel in self.roots:
                return None if self.vol_rules.keeps(rel, True) else f"dropped root /{rel}"
            if is_dir and rel in self.above:
                return "above"
            return f"outside roots: /{rel}"
        if parent_label is not None:
            return parent_label
        rule = self.vol_rules.match(rel, is_dir)
        if rule is not None and not rule.keep:
            return str(rule)
        return None

    def _walk(self, vol_root, start: str, start_label: str | None, volume_id: str | None) -> None:
        stack = [(start, start_label)]
        while stack:
            d, label = stack.pop()
            try:
                st = os.lstat(vol_root / d)
            except OSError:
                self.errors += 1
                continue
            # only skips legacy directory hard links: APFS directories keep their inode when their
            # content changes, and each APFS snapshot has its own st_dev, so they never match here
            key = (st.st_dev, st.st_ino)
            if key in self.seen_dirs:
                continue
            self.seen_dirs.add(key)
            if self.progress.due():
                kept = self.buckets[KEPT]
                log(f"  kept so far: {kept.files} files, {human(kept.bytes)} … {d}")
            try:
                with os.scandir(vol_root / d) as it:
                    items = list(it)
            except OSError:
                self.errors += 1
                continue
            for entry in items:
                rel = join(d, entry.name)
                try:
                    est = entry.stat(follow_symlinks=False)
                except OSError:
                    self.errors += 1
                    continue
                is_dir = stat.S_ISDIR(est.st_mode)
                sub = self._label_for(rel, is_dir, label)
                if is_dir:
                    stack.append((rel, sub))
                    continue
                fkey = file_key(est, volume_id)
                if fkey in self.seen_files:
                    continue
                self.seen_files.add(fkey)
                size = est.st_blocks * 512
                b = self.buckets[KEPT if sub is None else sub]
                b.files += 1
                b.bytes += size
                if sub is None:
                    self.kept_here.files += 1
                    self.kept_here.bytes += size

    def report(self, top: int = 40) -> str:
        kept = self.buckets.get(KEPT, Bucket())
        dropped = {k: v for k, v in self.buckets.items() if k != KEPT}
        total_dropped = sum(b.bytes for b in dropped.values())
        lines = [f"{'kept':<60} {human(kept.bytes):>10} {kept.files:>10} files"]
        if len(self.kept_by_volume) > 1:
            for name, b in sorted(self.kept_by_volume.items(), key=lambda kv: -kv[1].bytes):
                lines.append(f"  {'on ' + name[:55]:<58} {human(b.bytes):>10} {b.files:>10} files")
        lines += [
            f"{'dropped (total)':<60} {human(total_dropped):>10} {sum(b.files for b in dropped.values()):>10} files",
            "",
            "dropped by rule / location:",
        ]
        for name, b in sorted(dropped.items(), key=lambda kv: -kv[1].bytes)[:top]:
            lines.append(f"  {name[:58]:<58} {human(b.bytes):>10} {b.files:>10} files")
        if len(dropped) > top:
            lines.append(f"  … {len(dropped) - top} more")
        if self.errors:
            lines.append(f"\n{self.errors} entries could not be read (permissions? try sudo / Full Disk Access)")
        lines.append(
            "\nSizes count each file once across the scanned snapshots; "
            "'kept' approximates the archive size before `timak dedupe`."
        )
        return "\n".join(lines)
