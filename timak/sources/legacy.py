"""Legacy (HFS+) Time Machine backups: Backups.backupdb/<machine>/<stamp>/<volume>/..."""

from __future__ import annotations

import os
from pathlib import Path

from .base import STAMP_RE, Snapshot, SourceError, volumes_in


def find_backupdb(root: Path) -> Path | None:
    if root.name == "Backups.backupdb" and root.is_dir():
        return root
    candidate = root / "Backups.backupdb"
    if candidate.is_dir():
        return candidate
    # a machine directory given directly
    if root.parent.name == "Backups.backupdb":
        return root.parent
    return None


def snapshots(backupdb: Path, source: str, include_in_progress: bool = False) -> list[Snapshot]:
    try:
        machines = sorted(os.scandir(backupdb), key=lambda e: e.name)
    except PermissionError as e:
        raise SourceError(
            f"cannot read {backupdb}: {e.strerror}. "
            "Give your terminal Full Disk Access (System Settings > Privacy & Security), or run with sudo."
        ) from e

    result = []
    for machine in machines:
        if not machine.is_dir(follow_symlinks=False) or machine.name.startswith("."):
            continue
        for entry in sorted(os.scandir(machine.path), key=lambda e: e.name):
            if entry.is_symlink() or not entry.is_dir():  # "Latest" is a symlink
                continue
            in_progress = entry.name.endswith(".inProgress")
            if in_progress and not include_in_progress:
                continue
            m = STAMP_RE.fullmatch(entry.name.removesuffix(".inProgress"))
            if not m:
                continue
            path = Path(entry.path)
            result.append(
                Snapshot(
                    kind="legacy",
                    machine=machine.name,
                    stamp=m.group(1),
                    path=path,
                    volumes=volumes_in(path),
                    source=source,
                    in_progress=in_progress,
                )
            )
    return result
