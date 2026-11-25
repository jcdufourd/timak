"""Copy the kept part of each snapshot into the archive, hard-linking unchanged files.

For every snapshot (oldest first) and every selected volume, we walk the
source tree ourselves, applying the rules:

  - a directory that is the *same inode* as in the previous snapshot (a legacy
    Time Machine directory hard link) is unchanged as a whole: its previous
    output is mirrored with hard links and the source is not walked at all;
  - a file that is unchanged since the previous snapshot is hard-linked to the
    previous output. "Unchanged" means the same inode when both are legacy
    snapshots on the same filesystem (legacy backups hard-link unchanged
    files), and otherwise same size, mtime and mode (rsync's quick check);
  - everything else (new or changed files, symlinks, and every directory for
    its metadata) is handed to rsync in one `--files-from` batch, so data,
    extended attributes, resource forks and dates are copied faithfully.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import __version__
from .archive import machine_dir, previous_snapshot
from .rules import Rules, join
from .sources import Snapshot, select_volumes
from .util import INFO_NAME, PARTIAL_SUFFIX, Ticker, human, log, rmtree, same_mtime


MAX_PROBLEMS = 1000  # error messages kept per snapshot; the count in stats is complete


class ExtractError(RuntimeError):
    pass


SPARSE_MIN = 1 << 30  # warn about sparse files from this size


def sparse_used(st: os.stat_result) -> int | None:
    """Bytes actually stored, when a big file is mostly holes (a sparse disk image, a VM disk).

    rsync is run without --sparse, and HFS+ can't keep holes anyway, so such a
    file is written in full: an 800 GB image holding 1 GB takes 800 GB.
    """
    used = st.st_blocks * 512
    return used if st.st_size >= SPARSE_MIN and used < st.st_size // 2 else None


@dataclass
class Stats:
    dirs: int = 0
    dirs_shortcut: int = 0
    linked: int = 0
    copied: int = 0
    copied_bytes: int = 0
    symlinks: int = 0
    skipped_special: int = 0
    sparse: int = 0  # big files mostly made of holes, which the copy writes in full
    errors: int = 0

    def add(self, other: "Stats") -> None:
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))

    def summary(self) -> str:
        s = (
            f"{self.linked} linked, {self.copied} copied ({human(self.copied_bytes)}), "
            f"{self.dirs} dirs ({self.dirs_shortcut} unchanged trees)"
        )
        if self.symlinks:
            s += f", {self.symlinks} symlinks"
        if self.sparse:
            s += f", {self.sparse} sparse files written in full (see warnings)"
        if self.errors:
            s += f", {self.errors} ERRORS"
        return s


def find_rsync(explicit: str | None = None) -> str:
    candidates = [explicit] if explicit else ["/opt/homebrew/bin/rsync", "/usr/local/bin/rsync", shutil.which("rsync")]
    for c in candidates:
        if not c or not os.path.exists(c):
            continue
        try:
            out = subprocess.run([c, "--version"], capture_output=True, text=True).stdout
        except OSError:
            continue
        if "openrsync" not in out and re.search(r"rsync\s+version\s+v?3\.", out):
            return c
    raise ExtractError(
        "rsync 3.x not found (macOS ships openrsync, which does not preserve all metadata). "
        "Install it with `brew install rsync` or pass --rsync PATH."
    )


class VolumeCopy:
    def __init__(
        self,
        src: Path,
        out: Path,
        rules: Rules,
        rsync: str,
        stats: Stats,
        prev_out: Path | None = None,
        prev_src: Path | None = None,
        progress: Ticker | None = None,
        label: str = "",
        trust_inodes: bool = True,
        roots: list[str] | None = None,
        problems: list[str] | None = None,
    ):
        self.problems = [] if problems is None else problems  # error messages, kept in the snapshot's info
        self.roots = rules.root_rels() if roots is None else roots
        self.src = src
        self.out = out
        self.rules = rules
        self.rsync = rsync
        self.stats = stats
        self.prev_out = prev_out
        self.prev_src = prev_src
        self.progress = progress or Ticker()
        self.label = label
        # handed to rsync; never "." — with --files-from, rsync copies the contents of "." one level deep
        self.entries: list[str] = []
        # "same inode = unchanged" only holds for legacy Time Machine snapshots on one filesystem;
        # plain copies on one disk never share inodes, so they fall back to the quick check
        self.same_fs = False
        if trust_inodes and prev_src is not None and prev_out is not None:
            try:
                self.same_fs = os.lstat(src).st_dev == os.lstat(prev_src).st_dev
            except OSError:
                pass

    def error(self, msg: str) -> None:
        self._check_source()
        self.stats.errors += 1
        log(f"  error: {msg}")
        self._keep(msg)

    def _check_source(self) -> None:
        """Errors pile up when the backup goes away (image ejected, drive unplugged): stop with one clear message."""
        if not os.path.isdir(self.src):
            raise ExtractError(
                f"the backup disappeared while timak was reading {self.src}: the disk image was ejected "
                "or the drive disconnected. Snapshots already finished are complete; "
                "run the same command again to resume."
            )

    def _keep(self, msg: str) -> None:
        if len(self.problems) < MAX_PROBLEMS:
            # name files as they are in the archive, not by the temporary mount point
            self.problems.append(msg.replace(f"{self.src}/", f"{self.out.name}/"))

    def run(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        ancestors: set[str] = set()
        for root in self.roots:
            if root:
                try:
                    st = os.lstat(self.src / root)
                except FileNotFoundError:
                    continue
                except OSError as e:
                    self.error(f"{root}: {e.strerror}")
                    continue
                if not stat.S_ISDIR(st.st_mode) or not self.rules.keeps(root, True):
                    continue
                parts = root.split("/")
                for i in range(1, len(parts)):
                    anc = "/".join(parts[:i])
                    if anc not in ancestors:
                        ancestors.add(anc)
                        (self.out / anc).mkdir(exist_ok=True)
                        self.entries.append(anc)
            self._walk(root)
        self._rsync()
        try:
            st = os.lstat(self.src)
            os.chmod(self.out, stat.S_IMODE(st.st_mode))
            os.utime(self.out, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError:
            pass

    # --- walking ---

    def _walk(self, start: str) -> None:
        stack = [start]
        while stack:
            d = stack.pop()
            if d:
                (self.out / d).mkdir(exist_ok=True)
                self.entries.append(d)
            self.stats.dirs += 1
            if self.progress.due():
                log(f"  {self.label}: {self.stats.summary()} … {d}")

            if self._dir_unchanged(d):
                self.stats.dirs_shortcut += 1
                self._mirror(d)
                continue

            try:
                with os.scandir(self.src / d) as it:
                    items = list(it)
            except OSError as e:
                self.error(f"{d or '/'}: {e.strerror}")
                continue
            for entry in items:
                rel = join(d, entry.name)
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError as e:
                    self.error(f"{rel}: {e.strerror}")
                    continue
                mode = st.st_mode
                if stat.S_ISDIR(mode):
                    if self.rules.keeps(rel, True):
                        stack.append(rel)
                elif stat.S_ISREG(mode):
                    if self.rules.keeps(rel, False):
                        self._file(rel, st)
                elif stat.S_ISLNK(mode):
                    if self.rules.keeps(rel, False):
                        self.entries.append(rel)
                        self.stats.symlinks += 1
                else:
                    self.stats.skipped_special += 1

    def _dir_unchanged(self, d: str) -> bool:
        if not self.same_fs:
            return False
        try:
            a = os.lstat(self.src / d)
            b = os.lstat(self.prev_src / d)
        except OSError:
            return False
        return a.st_ino == b.st_ino and a.st_dev == b.st_dev and (self.prev_out / d).is_dir()

    def _mirror(self, start: str) -> None:
        """Recreate prev_out/start under out/start with hard links (start already exists)."""
        stack = [start]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(self.prev_out / d) as it:
                    items = list(it)
            except OSError as e:
                self.error(f"previous snapshot {d}: {e.strerror}")
                continue
            for entry in items:
                rel = join(d, entry.name)
                if entry.is_dir(follow_symlinks=False):
                    (self.out / rel).mkdir()
                    self.entries.append(rel)
                    self.stats.dirs += 1
                    stack.append(rel)
                    continue
                try:
                    os.link(entry.path, self.out / rel, follow_symlinks=False)
                    self.stats.linked += 1
                except OSError:
                    # e.g. too many links: copy it from the (identical) source instead
                    self.entries.append(rel)
                    self.stats.copied += 1

    def _file(self, rel: str, st: os.stat_result) -> None:
        if self.prev_out is not None:
            if self._file_unchanged(rel, st):
                try:
                    os.link(self.prev_out / rel, self.out / rel)
                    self.stats.linked += 1
                    return
                except OSError as e:
                    if e.errno not in (errno.EMLINK, errno.ENOENT, errno.EPERM):
                        raise
        self.entries.append(rel)
        self.stats.copied += 1
        self.stats.copied_bytes += st.st_size
        used = sparse_used(st)
        if used is not None:
            self.stats.sparse += 1
            msg = (
                f"warning: {rel}: sparse file of {human(st.st_size)} holding only {human(used)}, "
                f"it will take {human(st.st_size)} in the archive; add \"- /{rel}\" to your rules to leave it out"
            )
            log(f"  {msg}")
            self._keep(msg)

    def _file_unchanged(self, rel: str, st: os.stat_result) -> bool:
        try:
            if self.same_fs:
                p = os.lstat(self.prev_src / rel)
                return p.st_ino == st.st_ino and p.st_dev == st.st_dev
            p = os.lstat(self.prev_out / rel)
        except OSError:
            return False
        return (
            stat.S_ISREG(p.st_mode)
            and p.st_size == st.st_size
            and same_mtime(p.st_mtime_ns, st.st_mtime_ns)
            and stat.S_IMODE(p.st_mode) == stat.S_IMODE(st.st_mode)
        )

    # --- copying ---

    def _rsync(self) -> None:
        with tempfile.NamedTemporaryFile("wb", prefix="timak-", suffix=".list", delete=False) as f:
            f.write(b"\0".join(os.fsencode(e) for e in self.entries) + b"\0")
            listfile = f.name
        try:
            cmd = [
                self.rsync, "-a", "-H", "-X", "-N", "--numeric-ids",
                "--from0", f"--files-from={listfile}",
                f"{self.src}/", f"{self.out}/",
            ]  # fmt: skip
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        finally:
            os.unlink(listfile)
        lines = [l for l in proc.stderr.decode(errors="replace").splitlines() if l.strip()]
        if proc.returncode != 0:
            self._check_source()
        if proc.returncode in (23, 24):  # some files could not be read / vanished
            problems = [l for l in lines if not l.startswith("rsync error:")]
            self.stats.errors += len(problems)
            for l in problems:
                self._keep(_rsync_problem(l))
            for l in problems[:20]:
                log(f"  {l}")
            if len(problems) > 20:
                log(f"  rsync: … {len(problems) - 20} more")
        elif proc.returncode != 0:
            raise ExtractError(f"rsync failed ({proc.returncode}):\n" + "\n".join(lines[-20:]))


_RSYNC_PATH = re.compile(r'"(.+)"')


def _rsync_problem(line: str) -> str:
    """'rsync: [sender] send_files failed to open "x": Invalid argument (22)' -> 'x: Invalid argument (22)'."""
    m = _RSYNC_PATH.search(line)
    if m and ": " in line[m.end():]:
        return f"{m[1]}: {line.rsplit(': ', 1)[1]}"
    return line.removeprefix("rsync: ")


class Extractor:
    def __init__(self, archive: Path, rules: Rules, rsync: str, volumes: list[str] | None = None):
        self.archive = archive
        self.rules = rules
        self.rsync = rsync
        self.volumes = volumes
        self.progress = Ticker()

    def run(self, snapshots: list[Snapshot]) -> Stats:
        total = Stats()
        by_key = {(s.machine, s.stamp): s for s in snapshots}
        for i, snap in enumerate(snapshots, 1):
            stats = self.extract(snap, by_key, f"[{i}/{len(snapshots)}]")
            if stats:
                total.add(stats)
        return total

    def extract(self, snap: Snapshot, by_key: dict | None = None, prefix: str = "") -> Stats | None:
        mdir = machine_dir(self.archive, snap.machine)
        final = mdir / snap.stamp
        if final.exists():
            log(f"{prefix} {snap.label}: already in archive, skipped")
            return None
        volumes = select_volumes(snap, self.rules, self.volumes)
        if not volumes:
            log(f"{prefix} {snap.label}: no volume contains {', '.join(self.rules.roots)}, skipped")
            return None

        partial = mdir / (snap.stamp + PARTIAL_SUFFIX)
        if partial.exists():
            # leftover from an interrupted run: can take many minutes on a hard disk
            log(f"{prefix} {snap.label}: removing what an interrupted run left in {partial.name} …")
            t = time.monotonic()
            rmtree(partial)
            log(f"  removed in {time.monotonic() - t:.0f}s")
        partial.mkdir(parents=True)
        log(f"{prefix} {snap.label}: extracting")  # the app shows this snapshot as in progress

        prev = previous_snapshot(mdir, snap.stamp)
        # whole-tree shortcuts need the previous output made with the same rules from a
        # source we can see now; per-file linking is always safe
        prev_source = None
        if prev and prev.info.get("rules") == self.rules.fingerprint and by_key:
            prev_source = by_key.get((snap.machine, prev.stamp))
        trust_inodes = snap.kind == "legacy" and prev_source is not None and prev_source.kind == "legacy"

        start = time.monotonic()
        stats = Stats()
        problems: list[str] = []
        for vol in volumes:
            prev_out = prev.path / vol.name if prev and (prev.path / vol.name).is_dir() else None
            prev_src = None
            if prev_source:
                prev_src = next(
                    (v.path for v in select_volumes(prev_source, self.rules, self.volumes) if v.name == vol.name), None
                )
            VolumeCopy(
                vol.path, partial / vol.name, self.rules.for_volume(vol), self.rsync, stats,
                prev_out=prev_out, prev_src=prev_src, progress=self.progress,
                label=f"{snap.label} {vol.name}", trust_inodes=trust_inodes, roots=self.rules.root_rels(vol),
                problems=problems,
            ).run()  # fmt: skip

        info = {
            "tool": f"timak {__version__}",
            "machine": snap.machine,
            "stamp": snap.stamp,
            "kind": snap.kind,
            "source": snap.source,
            "snapshot_path": str(snap.path),
            "volumes": {v.name: v.original_name for v in volumes},
            "previous": prev.stamp if prev else None,
            "rules": self.rules.fingerprint,
            "roots": {v.name: self.rules.roots_for(v) for v in volumes},
            "rule_list": self.rules.rule_texts,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "stats": asdict(stats),
            "problems": problems,
        }
        (partial / INFO_NAME).write_text(json.dumps(info, indent=2) + "\n")
        os.rename(partial, final)
        log(f"{prefix} {snap.label}: {stats.summary()} in {time.monotonic() - start:.1f}s")
        return stats
