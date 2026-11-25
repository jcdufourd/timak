"""Small helpers: sizes, hashing, extended attributes, robust removal, logging."""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import os
import shutil
import stat
import sys
import time
from pathlib import Path

INFO_NAME = ".timak.json"
LEGACY_INFO_NAME = ".timac.json"  # written before the rename to timak; still read, replaced on the next write
INFO_NAMES = (INFO_NAME, LEGACY_INFO_NAME)
REPORT_SUFFIX = ".timakreport"  # verify-<source>.timakreport: verify's full result, at the top of the archive
REPORT_SUFFIXES = (REPORT_SUFFIX, ".timacreport")


def info_file(snapshot_dir: Path) -> Path:
    """The snapshot's info file: .timak.json, or the .timac.json of an archive made before the rename."""
    legacy = snapshot_dir / LEGACY_INFO_NAME
    new = snapshot_dir / INFO_NAME
    return legacy if not new.exists() and legacy.exists() else new


def is_snapshot(path: Path) -> bool:
    return any((path / name).exists() for name in INFO_NAMES)
_IMAGE_SUFFIXES = (".dmg", ".sparseimage", ".sparsebundle")


def report_name(command: str, sources: list[str]) -> str:
    """e.g. verify-Backup2021.timakreport, after the source(s) checked."""
    names = []
    for src in sources:
        p = Path(src).expanduser()
        name = p.name or str(p)
        if name.lower() == "backups.backupdb" and p.parent.name:
            name = p.parent.name  # the drive or image it is on
        for suffix in _IMAGE_SUFFIXES:
            if name.lower().endswith(suffix):
                name = name[: -len(suffix)]
        names.append(name.replace("/", "_") or "source")
    label = "+".join(names[:3]) + (f"+{len(names) - 3}-more" if len(names) > 3 else "")
    return f"{command}-{label}{REPORT_SUFFIX}"
PARTIAL_SUFFIX = ".partial"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def log(*args) -> None:
    print(*args, file=sys.stderr, flush=True)


class Ticker:
    """Rate-limited progress messages."""

    def __init__(self, every: float = 10.0):
        self.every = every
        self.last = time.monotonic()

    def due(self) -> bool:
        now = time.monotonic()
        if now - self.last >= self.every:
            self.last = now
            return True
        return False


def duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min" if hours < 24 else f"{hours / 24:.1f} days"


class Progress:
    """How far a long job is, and the time left at the pace so far.

    `total` may be an estimate, or None when unknown. Past the total the
    estimate was wrong, so only the count is shown.
    """

    def __init__(self, total: float | None, unit=str, settle: float = 30.0):
        self.total = total
        self.unit = unit  # formats an amount, e.g. human for bytes
        self.settle = settle  # seconds before the pace is trusted
        self.start = time.monotonic()

    def text(self, done: float, noun: str = "") -> str:
        noun = f" {noun}" if noun else ""
        if not self.total or done > self.total:
            return self.unit(done) + noun
        s = f"{self.unit(done)} of ~{self.unit(self.total)}{noun} ({100 * done / self.total:.0f}%"
        elapsed = time.monotonic() - self.start
        if done and elapsed >= self.settle and done < self.total:
            s += f", about {duration(elapsed * (self.total - done) / done)} left"
        return s + ")"


def has_full_disk_access() -> bool:
    """macOS blocks reads inside Time Machine backups without Full Disk Access (even for root).

    The block can kick in a few seconds after a backup volume is mounted, so a
    probe of the backup itself is unreliable; probe a TCC-protected folder instead.
    """
    try:
        os.listdir("/Library/Application Support/com.apple.TCC")
        return True
    except PermissionError:
        return False
    except OSError:
        return True  # folder missing (not macOS?): nothing to warn about


FDA_HINT = (
    "this process lacks Full Disk Access: macOS will refuse to read inside Time Machine backups "
    "('Operation not permitted'). Grant it to your terminal app (or to Timak.app, when the app runs timak) "
    "in System Settings > Privacy & Security > Full Disk Access, then restart it."
)


_PC_CASE_SENSITIVE = 11  # macOS <unistd.h>; not in os.pathconf_names


def case_sensitive(path: str | Path) -> bool | None:
    try:
        return bool(os.pathconf(path, _PC_CASE_SENSITIVE))
    except (OSError, ValueError):
        return None


def same_mtime(archived_ns: int, source_ns: int) -> bool:
    """Whether an archived file's mtime matches its source's.

    HFS+ keeps whole seconds only, so a file copied there from APFS (nanoseconds)
    loses the fraction; such a time matches any source time within that second.

    HFS+ also can't store a date before 1904 or after 6 February 2040; macOS then
    writes 1 January 1970 (0) instead, e.g. for a photo dated 2098. Such a
    source time matches an archived 0.
    """
    if archived_ns == source_ns:
        return True
    if archived_ns == 0 and not HFS_MIN <= source_ns // 1_000_000_000 <= HFS_MAX:
        return True
    return archived_ns % 1_000_000_000 == 0 and abs(archived_ns - source_ns) < 1_000_000_000


HFS_MIN = -2_082_844_800  # 1904-01-01 00:00:00 UTC, in Unix time
HFS_MAX = 0xFFFF_FFFF + HFS_MIN  # 2040-02-06 06:28:15 UTC


def file_hash(path: str | Path, limit: int | None = None) -> bytes:
    h = hashlib.blake2b(digest_size=32)
    remaining = limit
    with open(path, "rb", buffering=0) as f:
        while True:
            size = 1 << 20 if remaining is None else min(1 << 20, remaining)
            if size == 0:
                break
            chunk = f.read(size)
            if not chunk:
                break
            h.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return h.digest()


# --- extended attributes (macOS libc; Python's os.*xattr is Linux-only) ---

_XATTR_NOFOLLOW = 0x0001
_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]
_libc.listxattr.restype = ctypes.c_ssize_t
_libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
_libc.getxattr.restype = ctypes.c_ssize_t


def _check(n: int, path) -> int:
    if n < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(path))
    return n


def xattrs(path: str | Path) -> dict[str, bytes]:
    p = os.fsencode(path)
    size = _check(_libc.listxattr(p, None, 0, _XATTR_NOFOLLOW), path)
    if size == 0:
        return {}
    buf = ctypes.create_string_buffer(size)
    size = _check(_libc.listxattr(p, buf, size, _XATTR_NOFOLLOW), path)
    result = {}
    for name in buf.raw[:size].split(b"\0"):
        if not name:
            continue
        n = _check(_libc.getxattr(p, name, None, 0, 0, _XATTR_NOFOLLOW), path)
        val = ctypes.create_string_buffer(n)
        n = _check(_libc.getxattr(p, name, val, n, 0, _XATTR_NOFOLLOW), path)
        result[name.decode(errors="surrogateescape")] = val.raw[:n]
    return result


def rmtree(path: Path) -> None:
    """Remove our own output tree, including read-only directories rsync may have created."""

    def onexc(func, p, exc):
        parent = os.path.dirname(p)
        try:
            os.chmod(parent, stat.S_IMODE(os.lstat(parent).st_mode) | stat.S_IRWXU)
            if os.path.isdir(p) and not os.path.islink(p):
                os.chmod(p, stat.S_IMODE(os.lstat(p).st_mode) | stat.S_IRWXU)
        except OSError:
            pass
        func(p)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=onexc)
    else:
        shutil.rmtree(path, onerror=lambda f, p, e: onexc(f, p, e[1]))
