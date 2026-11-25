"""Check archived snapshots against their source: every kept file present and identical."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .archive import machine_dir
from .rules import Rules, join
from .sources import Snapshot, select_volumes
from .util import Ticker, file_hash, log, same_mtime


@dataclass
class VerifyResult:
    checked: int = 0
    unreadable: int = 0
    ignored: int = 0  # entries in the archive only, that the rules drop (e.g. .DS_Store made by Finder)
    missing_snapshots: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and not self.missing_snapshots


def iter_kept(src: Path, rules: Rules, result: VerifyResult, roots: list[str]) -> Iterator[tuple[str, os.stat_result]]:
    """Every entry under the roots that the rules keep, as (relative path, lstat)."""
    for root in roots:
        if root:
            try:
                st = os.lstat(src / root)
            except OSError:
                continue
            if not stat.S_ISDIR(st.st_mode) or not rules.keeps(root, True):
                continue
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(src / d) as it:
                    items = list(it)
            except OSError:
                result.unreadable += 1
                continue
            for entry in items:
                rel = join(d, entry.name)
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    result.unreadable += 1
                    continue
                is_dir = stat.S_ISDIR(st.st_mode)
                if not rules.keeps(rel, is_dir):
                    continue
                if is_dir:
                    stack.append(rel)
                elif not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                    continue  # sockets, fifos, devices are not archived
                yield rel, st


def _compare(rel: str, src: Path, out: Path, st: os.stat_result, content: bool) -> str | None:
    try:
        o = os.lstat(out / rel)
    except FileNotFoundError:
        return "missing"
    if stat.S_IFMT(o.st_mode) != stat.S_IFMT(st.st_mode):
        return "type differs"
    if stat.S_ISLNK(st.st_mode):
        return None if os.readlink(out / rel) == os.readlink(src / rel) else "symlink target differs"
    if o.st_size != st.st_size:
        return f"size {o.st_size} != {st.st_size}"
    if not same_mtime(o.st_mtime_ns, st.st_mtime_ns):
        return "mtime differs"
    if content:
        try:
            if file_hash(out / rel) != file_hash(src / rel):
                return "content differs"
        except OSError as e:
            return f"unreadable: {e.strerror}"
    return None


def _extras(src: Path, out: Path, rules: Rules, result: VerifyResult) -> Iterator[str]:
    """Entries of the archive that are not in the source.

    Those the rules drop were not put there by extract: typically the .DS_Store
    files Finder writes into every folder you open. They are counted, not reported.
    """
    stack = [""]
    while stack:
        d = stack.pop()
        with os.scandir(out / d) as it:
            for entry in it:
                rel = join(d, entry.name)
                is_dir = entry.is_dir(follow_symlinks=False)
                if not os.path.lexists(src / rel):
                    if rules.keeps(rel, is_dir):
                        yield rel
                    else:
                        result.ignored += 1
                elif is_dir:
                    stack.append(rel)


def verify(
    snapshots: list[Snapshot], archive: Path, rules: Rules, volumes: list[str] | None = None, content: bool = False
) -> VerifyResult:
    result = VerifyResult()
    ticker = Ticker()
    for i, snap in enumerate(snapshots, 1):
        final = machine_dir(archive, snap.machine) / snap.stamp
        if not final.is_dir():
            result.missing_snapshots.append(snap.label)
            continue
        log(f"[{i}/{len(snapshots)}] verifying {snap.label}")
        for vol in select_volumes(snap, rules, volumes):
            out = final / vol.name
            if not out.is_dir():
                result.problems.append(f"{snap.label}: volume {vol.name} missing")
                continue
            vol_rules = rules.for_volume(vol)
            for rel, st in iter_kept(vol.path, vol_rules, result, rules.root_rels(vol)):
                result.checked += 1
                if not stat.S_ISDIR(st.st_mode) and (why := _compare(rel, vol.path, out, st, content)):
                    result.problems.append(f"{snap.label} {vol.name}/{rel}: {why}")
                elif stat.S_ISDIR(st.st_mode) and not (out / rel).is_dir():
                    result.problems.append(f"{snap.label} {vol.name}/{rel}: directory missing")
                if ticker.due():
                    log(f"  {result.checked} checked, {len(result.problems)} problems … {rel}")
            for rel in _extras(vol.path, out, vol_rules, result):
                result.problems.append(f"{snap.label} {vol.name}/{rel}: not in source")
    return result
