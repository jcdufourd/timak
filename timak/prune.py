"""Apply the current rules to snapshots already in the archive.

`extract` never touches a finished snapshot, so tightening the rules only
helps snapshots extracted afterwards. `prune` walks every finished snapshot
of the archive and removes what the rules now drop, the way `extract` would
have: a dropped folder goes whole. It can only remove; something the old
rules dropped cannot come back without extracting again.

Snapshots share unchanged files through hard links, so removing a path frees
space only once every link to that file is gone. The space reported as freed
counts exactly that, over all the snapshots pruned in the run.

Each pruned snapshot gets the new rules in its `.timak.json` (with a `pruned`
record of the change), so `extract` takes its shortcuts again and a rerun of
`prune` skips the snapshots already done.
"""

from __future__ import annotations

import json
import os
import stat
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from . import __version__
from .archive import ArchivedSnapshot, complete_snapshots
from .rules import Rules, join
from .util import INFO_NAME, LEGACY_INFO_NAME, PARTIAL_SUFFIX, Ticker, human, log, rmtree

OUTSIDE_ROOTS = "outside the roots"
VOLUME_SKIPPED = "volume skipped by [volume_roots]"


@dataclass
class PathRecord:
    snapshots: int = 0  # snapshots in which this path is removed
    size: int = 0  # bytes of its files not already counted for an earlier snapshot (same rule)
    entries: int = 0  # files and folders, over all snapshots
    first: str = ""  # first and last snapshot it is in
    last: str = ""


@dataclass
class PruneStats:
    snapshots: int = 0  # snapshots looked at
    pruned: int = 0  # snapshots in which something was (or would be) removed
    up_to_date: int = 0  # already made or pruned with these rules
    entries: int = 0  # files, symlinks and folders removed, counted inside removed folders too
    freed: int = 0  # bytes whose last link is removed
    by_rule: Counter = field(default_factory=Counter)  # rule -> bytes of the files it removes, each file once
    paths_by_rule: Counter = field(default_factory=Counter)  # rule -> paths matched (top of what is removed)
    # (machine, rule, volume/path) -> PathRecord: what is removed, listed once over all snapshots
    paths: dict = field(default_factory=dict)
    skipped_machines: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self, apply: bool) -> str:
        verb = "freed" if apply else "would free"
        text = (
            f"{verb} {human(self.freed)}: {self.entries} entries "
            f"{'removed' if apply else 'to remove'} in {self.pruned} of {self.snapshots} snapshots"
        )
        if self.up_to_date:
            text += f", {self.up_to_date} already up to date"
        if self.errors:
            text += f", {len(self.errors)} errors"
        return text


class Pruner:
    def __init__(self, archive: Path, rules: Rules, apply: bool = False, machines: list[str] | None = None):
        self.archive = archive
        self.rules = rules
        self.apply = apply
        self.machines = machines
        self.stats = PruneStats()
        self.ticker = Ticker()
        self._first_nlink: dict[tuple[int, int], int] = {}  # inode -> link count when first seen
        self._removed: Counter = Counter()  # inode -> links removed so far
        self._counted: set[tuple[int, int, str]] = set()  # (inode, rule) already counted in by_rule
        self._machine = ""
        self._stamp = ""
        self._vol = ""
        self._vol_rules = rules

    # --- what to look at ---

    def machine_dirs(self) -> list[Path]:
        if not self.archive.is_dir():
            raise FileNotFoundError(f"no archive at {self.archive}")
        found = []
        for p in sorted(self.archive.iterdir()):
            if p.name.startswith(".") or not p.is_dir() or p.is_symlink():
                continue  # a disk's .Spotlight-V100, .DocumentRevisions-V100, …
            try:
                if complete_snapshots(p):
                    found.append(p)
            except OSError as e:
                log(f"{p.name}: skipped, {e.strerror}")
        if self.machines:
            wanted = set(self.machines)
            missing = wanted - {p.name for p in found}
            if missing:
                raise FileNotFoundError(f"no machine {', '.join(sorted(missing))} in {self.archive}")
            found = [p for p in found if p.name in wanted]
        return found

    def run(self) -> PruneStats:
        for mdir in self.machine_dirs():
            partial = sorted(p.name for p in mdir.iterdir() if p.name.endswith(PARTIAL_SUFFIX))
            if partial:
                msg = (
                    f"{mdir.name}: skipped, {partial[0]} is an extraction in progress or interrupted; "
                    "let extract finish (or rerun it) first"
                )
                log(msg)
                self.stats.skipped_machines.append(mdir.name)
                continue
            snaps = complete_snapshots(mdir)
            self._machine = mdir.name
            for i, snap in enumerate(snaps, 1):
                self.prune_snapshot(snap, f"[{i}/{len(snaps)}] {mdir.name}/{snap.stamp}")
        return self.stats

    # --- one snapshot ---

    def prune_snapshot(self, snap: ArchivedSnapshot, label: str) -> None:
        self.stats.snapshots += 1
        self._stamp = snap.stamp
        if snap.info.get("rules") == self.rules.fingerprint:
            self.stats.up_to_date += 1
            log(f"{label}: already pruned with these rules, skipped")
            return
        log(f"{label}: pruning")  # the app shows this snapshot as in progress
        before = self.stats.entries
        volumes = sorted(p for p in snap.path.iterdir() if p.is_dir() and not p.is_symlink())
        roots = {}
        for voldir in volumes:
            vol = SimpleNamespace(name=voldir.name, original_name=snap.info.get("volumes", {}).get(voldir.name))
            vol_roots = self.rules.roots_for(vol)
            self._vol = voldir.name
            if not vol_roots:
                self._drop(voldir, "", True, VOLUME_SKIPPED)
                continue
            roots[voldir.name] = vol_roots
            self._vol_rules = self.rules.for_volume(vol)
            self._prune_volume(voldir, self.rules.root_rels(vol), label)
        removed = self.stats.entries - before
        if removed:
            self.stats.pruned += 1
        if self.apply:
            self._update_info(snap, roots, removed)
        log(f"{label}: {removed} entries {'removed' if self.apply else 'to remove'}")

    def _prune_volume(self, voldir: Path, root_rels: list[str], label: str) -> None:
        stack = [""]
        while stack:
            d = stack.pop()
            if self.ticker.due():
                log(f"  {label}: {self.stats.entries} entries so far … {voldir.name}/{d}")
            try:
                with os.scandir(voldir / d) as it:
                    items = list(it)
            except OSError as e:
                self.stats.errors.append(f"{voldir / d}: {e.strerror}")
                continue
            for entry in items:
                rel = join(d, entry.name)
                is_dir = entry.is_dir(follow_symlinks=False)
                reason = self._drop_reason(rel, is_dir, root_rels)
                if reason:
                    self._drop(Path(entry.path), rel, is_dir, reason)
                elif is_dir:
                    stack.append(rel)

    def _drop_reason(self, rel: str, is_dir: bool, root_rels: list[str]) -> str | None:
        """Why `rel` must go, or None to keep it (and look inside if it is a folder)."""
        if root_rels != [""] and not any(rel == r or rel.startswith(r + "/") for r in root_rels):
            if is_dir and any(r.startswith(rel + "/") for r in root_rels):
                return None  # a folder on the way to a root
            return OUTSIDE_ROOTS
        rule = self._vol_rules.match(rel, is_dir)
        return str(rule) if rule and not rule.keep else None

    # --- removing ---

    def _drop(self, path: Path, rel: str, is_dir: bool, reason: str) -> None:
        self.stats.paths_by_rule[reason] += 1
        size0, entries0 = self.stats.by_rule[reason], self.stats.entries
        self._account(path, reason, is_dir)
        shown = (join(self._vol, rel) if rel else self._vol) + ("/" if is_dir else "")
        rec = self.stats.paths.setdefault((self._machine, reason, shown), PathRecord(first=self._stamp))
        rec.snapshots += 1
        rec.size += self.stats.by_rule[reason] - size0
        rec.entries += self.stats.entries - entries0
        rec.last = self._stamp
        if not self.apply:
            return
        try:
            if is_dir:
                rmtree(path)
            else:
                _unlink(path)
        except OSError as e:
            self.stats.errors.append(f"{path}: {e.strerror or e}")

    def _account(self, path: Path, reason: str, is_dir: bool) -> None:
        if not is_dir:
            self._count(path, reason)
            return
        self.stats.entries += 1
        for dirpath, dirnames, filenames in os.walk(path, onerror=lambda e: self.stats.errors.append(f"{e.filename}: {e.strerror}")):
            self.stats.entries += len(dirnames)
            for name in filenames:
                self._count(Path(dirpath) / name, reason)
            # os.walk lists symlinks to folders in dirnames; they hold no data
            dirnames[:] = [n for n in dirnames if not os.path.islink(os.path.join(dirpath, n))]

    def _count(self, path: Path, reason: str) -> None:
        self.stats.entries += 1
        try:
            st = os.lstat(path)
        except OSError as e:
            self.stats.errors.append(f"{path}: {e.strerror}")
            return
        key = (st.st_dev, st.st_ino)
        size = st.st_blocks * 512
        if (*key, reason) not in self._counted:
            self._counted.add((*key, reason))
            self.stats.by_rule[reason] += size
        first = self._first_nlink.setdefault(key, st.st_nlink)
        self._removed[key] += 1
        if self._removed[key] == first:
            self.stats.freed += size

    def _update_info(self, snap: ArchivedSnapshot, roots: dict, removed: int) -> None:
        info = dict(snap.info)
        info.setdefault("pruned", []).append({
            "tool": f"timak {__version__}",
            "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "previous_rules": snap.info.get("rules"),
            "previous_rule_list": snap.info.get("rule_list"),
            "entries_removed": removed,
        })  # fmt: skip
        info["rules"] = self.rules.fingerprint
        info["rule_list"] = self.rules.rule_texts
        info["roots"] = roots
        path = snap.path / INFO_NAME
        tmp = path.with_name(INFO_NAME + ".tmp")
        try:
            tmp.write_text(json.dumps(info, indent=2) + "\n")
            os.replace(tmp, path)
            (snap.path / LEGACY_INFO_NAME).unlink(missing_ok=True)  # now replaced by .timak.json
        except OSError as e:
            self.stats.errors.append(f"{path}: {e.strerror}")


def report_lines(stats: PruneStats, limit: int | None = None) -> list[str]:
    """The removed paths by machine and rule, biggest first; `limit` per rule."""
    lines = []
    groups: dict = {}
    for (machine, reason, shown), rec in stats.paths.items():
        groups.setdefault((machine, reason), []).append((shown, rec))
    order = sorted(groups, key=lambda k: (k[0], -stats.by_rule[k[1]]))
    for machine, reason in order:
        items = sorted(groups[(machine, reason)], key=lambda x: (-x[1].size, x[0]))
        total = sum(r.size for _, r in items)
        lines.append(f"{machine}: {reason}  ({human(total)}, {len(items)} distinct paths)")
        for shown, rec in items[:limit]:
            when = rec.first if rec.snapshots == 1 else f"{rec.snapshots} snapshots, {rec.first} … {rec.last}"
            lines.append(f"  {human(rec.size):>9}  {shown}  ({when})")
        if limit is not None and len(items) > limit:
            lines.append(f"  … {len(items) - limit} more")
    return lines


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except PermissionError:
        parent = path.parent  # rsync may have made the folder read-only
        os.chmod(parent, stat.S_IMODE(os.lstat(parent).st_mode) | stat.S_IRWXU)
        os.unlink(path)


def prune(archive: Path, rules: Rules, apply: bool = False, machines: list[str] | None = None) -> PruneStats:
    return Pruner(archive, rules, apply, machines).run()
