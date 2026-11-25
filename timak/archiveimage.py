"""Archives kept in a case-sensitive HFS+ sparse bundle.

On an APFS disk, `extract` makes a new archive as `<name>.sparsebundle`
beside where the folder would have gone, holding a case-sensitive Journaled
HFS+ volume named <name> with the archive at its root. Every command given
the archive path (`<archive>`, `<archive>.sparsebundle`, or a folder inside
the archive such as `<archive>/<machine>`) mounts the bundle, works inside
it, and ejects it again, unless it was already mounted (then it stays).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import plistlib
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .diskimage import DiskImageError, attached, detach, has_diskutil_image
from .util import human, log

BUNDLE = ".sparsebundle"
NO_IMAGE_ENV = "TIMAK_NO_IMAGE"  # set (to anything) to keep new archives as plain folders, as --folder does


class _Statfs(ctypes.Structure):  # struct statfs, <sys/mount.h> (64-bit inodes)
    _fields_ = [
        ("f_bsize", ctypes.c_uint32), ("f_iosize", ctypes.c_int32), ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64), ("f_bavail", ctypes.c_uint64), ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64), ("f_fsid", ctypes.c_int32 * 2), ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32), ("f_flags", ctypes.c_uint32), ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16), ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024), ("f_flags_ext", ctypes.c_uint32), ("f_reserved", ctypes.c_uint32 * 7),
    ]  # fmt: skip


def fs_type(path: Path) -> str | None:
    """'apfs', 'hfs', 'exfat'… of the disk holding path (or its nearest existing parent)."""
    path = Path(path).absolute()
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        st = _Statfs()
        if libc.statfs(os.fsencode(path), ctypes.byref(st)) != 0:
            return None
        return st.f_fstypename.decode(errors="replace")
    except (OSError, AttributeError):
        return None


def find_bundle(path: Path) -> tuple[Path, Path] | None:
    """(bundle, path inside the archive) when path names an archive kept in a sparse bundle.

    A folder that exists wins: an archive made as a plain folder stays one."""
    path = Path(path).absolute()
    for p in (path, *path.parents):
        if p.suffix == BUNDLE and p.is_dir():
            return p, path.relative_to(p)
        if p.exists():
            return None
        bundle = p.with_name(p.name + BUNDLE) if p.name else None
        if bundle and bundle.is_dir():
            return bundle, path.relative_to(p)
    return None


def create_bundle(bundle: Path, name: str) -> None:
    """A case-sensitive Journaled HFS+ sparse bundle, as big as the disk it is on (it grows as needed)."""
    size = shutil.disk_usage(bundle.parent).total
    log(f"creating {bundle}: a case-sensitive HFS+ disk image (grows up to {human(size)}) holding the archive")
    run = lambda cmd: subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL)
    if not has_diskutil_image():  # an older macOS
        proc = run(["hdiutil", "create", "-quiet", "-type", "SPARSEBUNDLE", "-fs", "Case-sensitive Journaled HFS+",
                    "-volname", name, "-size", f"{size // 2**20}m", str(bundle)])  # fmt: skip
        if proc.returncode != 0:
            raise DiskImageError(f"could not create {bundle}: {proc.stderr.decode(errors='replace').strip()}")
        return
    proc = run(["diskutil", "image", "create", "blank", "--format", "UDSB", "--fs", "None", "--size", str(size), str(bundle)])
    if proc.returncode == 0:
        proc = run(["diskutil", "image", "attach", "--noMount", "--plist", str(bundle)])
    if proc.returncode != 0:
        shutil.rmtree(bundle, ignore_errors=True)
        raise DiskImageError(f"could not create {bundle}: {(proc.stderr or proc.stdout).decode(errors='replace').strip()}")
    disk = min((e["dev-entry"] for e in plistlib.loads(proc.stdout)["system-entities"]), key=len)
    try:
        proc = run(["diskutil", "eraseDisk", "JHFSX", name, "GPT", disk])
    finally:
        detach(disk)
    if proc.returncode != 0:
        shutil.rmtree(bundle, ignore_errors=True)
        raise DiskImageError(f"could not format {bundle}: {(proc.stderr or proc.stdout).decode(errors='replace').strip()}")


def wants_bundle(archive: Path) -> bool:  # (--folder: not asked at all)
    """A new archive goes in a sparse bundle when asked by name, or when its disk is APFS."""
    if archive.suffix == BUNDLE:
        return True
    return not os.environ.get(NO_IMAGE_ENV) and fs_type(archive) == "apfs"


@contextmanager
def open_archive(path: Path, create: bool = False, readonly: bool = False) -> Iterator[Path]:
    """The folder to use for the archive at path, mounting its sparse bundle if it has one.

    create: make the bundle when there is no archive yet and the disk is APFS (extract)."""
    path = Path(path).expanduser()
    found = find_bundle(path)
    if found is None and create and not path.exists() and wants_bundle(path):
        bundle = path if path.suffix == BUNDLE else path.with_name(path.name + BUNDLE)
        bundle.parent.mkdir(parents=True, exist_ok=True)
        create_bundle(bundle, bundle.name.removesuffix(BUNDLE))
        found = bundle, Path()
    if found is None:
        yield path
        return
    bundle, inside = found
    with attached(bundle, readonly=readonly, owners=not readonly and os.geteuid() == 0) as mounts:
        if len(mounts) != 1:
            raise DiskImageError(f"{bundle} should hold one volume, it has {len(mounts)}")
        yield mounts[0] / inside
