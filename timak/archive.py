"""Archive layout: <archive>/<machine>/<YYYY-MM-DD-HHMMSS>/<volume>/<path...>

Each snapshot folder holds a `.timak.json` describing how it was made.
Snapshots are written as `<stamp>.partial` and renamed when complete, so a
folder named after a bare timestamp is always a finished snapshot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .sources.base import STAMP_RE, safe_name
from .util import info_file


@dataclass(frozen=True)
class ArchivedSnapshot:
    stamp: str
    path: Path
    info: dict


def machine_dir(archive: Path, machine: str) -> Path:
    return archive / safe_name(machine)


def read_info(snapshot_dir: Path) -> dict:
    try:
        return json.loads(info_file(snapshot_dir).read_text())
    except (OSError, ValueError):
        return {}


def complete_snapshots(mdir: Path) -> list[ArchivedSnapshot]:
    if not mdir.is_dir():
        return []
    result = [
        ArchivedSnapshot(p.name, p, read_info(p))
        for p in mdir.iterdir()
        if p.is_dir() and not p.is_symlink() and STAMP_RE.fullmatch(p.name)
    ]
    return sorted(result, key=lambda s: s.stamp)


def previous_snapshot(mdir: Path, stamp: str) -> ArchivedSnapshot | None:
    older = [s for s in complete_snapshots(mdir) if s.stamp < stamp]
    return older[-1] if older else None
