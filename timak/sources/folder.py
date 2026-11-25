"""A single snapshot copied out of Time Machine as a plain folder.

Either the folder holds one sub-folder per backed-up volume
(Backup20241029/Data/Users/..., Backup20241029/Large/...), or it is itself a volume
(it contains Users/). The date comes from the folder name (2024-10-29,
20241029, 2024-10-29-143000, ...) or from --stamp.

These are ordinary copies: unlike Time Machine, they share no inodes with
other snapshots, so unchanged files are recognized by size, mtime and mode.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from .base import Snapshot, SourceError, Volume, computer_name, normalize_volume_name, volumes_in

DATE_RE = re.compile(r"(?<!\d)(\d{4})-?(\d{2})-?(\d{2})(?:[-_ T]?(\d{2})(\d{2})(\d{2}))?(?!\d)")


def parse_stamp(text: str) -> str | None:
    """'Backup20241029' -> '2024-10-29-000000'; None if there is no plausible date."""
    for m in DATE_RE.finditer(text):
        y, mo, d, hh, mi, ss = m.groups()
        if 1990 <= int(y) <= 2100 and 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            return f"{y}-{mo}-{d}-{hh or '00'}{mi or '00'}{ss or '00'}"
    return None


def _is_volume(path: Path) -> bool:
    return (path / "Users").is_dir()


def looks_like_snapshot(path: Path) -> bool:
    if not path.is_dir():
        return False
    if _is_volume(path):
        return True
    try:
        return any(_is_volume(c) for c in path.iterdir() if c.is_dir() and not c.name.startswith("."))
    except OSError:
        return False


def snapshot(path: Path, source: str, machine: str | None = None, stamp: str | None = None) -> Snapshot:
    stamp = parse_stamp(stamp) if stamp else parse_stamp(path.name)
    if stamp is None:
        raise SourceError(f"cannot tell the date of {path} from its name; pass --stamp YYYY-MM-DD[-HHMMSS]")
    if _is_volume(path):
        volumes = (Volume(normalize_volume_name(path.name), path, path.name),)
    else:
        volumes = volumes_in(path)
    snap = Snapshot(kind="folder", machine=machine or path.name, stamp=stamp, path=path, volumes=volumes, source=source)
    if not machine:
        snap = replace(snap, machine=computer_name(snap) or path.name)
    return snap
