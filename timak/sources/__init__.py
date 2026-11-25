"""Discover Time Machine snapshots in disk images, mounted drives or backup folders."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator

from ..diskimage import attached, is_image
from . import apfs, folder, legacy
from .base import Snapshot, SourceError, Volume, select_volumes

__all__ = ["Snapshot", "SourceError", "Volume", "open_sources", "select_volumes"]


def discover(
    mount: Path,
    source: str,
    stack: ExitStack,
    machine: str | None = None,
    include_in_progress: bool = False,
    stamp: str | None = None,
) -> list[Snapshot]:
    # check APFS first: it needs no Full Disk Access, while reading the volume root may
    device = apfs.destination_device(mount)
    if device is not None:
        return apfs.snapshots(mount, device, source, stack, machine)
    db = legacy.find_backupdb(mount)
    if db is not None:
        snaps = legacy.snapshots(db, source, include_in_progress)
        if machine:
            names = sorted({s.machine for s in snaps})
            snaps = [s for s in snaps if s.machine == machine]
            if not snaps and names:
                # legacy backups name their machines; --machine only picks one of them
                raise SourceError(
                    f"{source}: no backups of machine {machine!r}; this backup holds "
                    + ", ".join(repr(n) for n in names)
                    + f". To file one under another name, use --rename-machine {names[0]!r}={machine!r} instead"
                )
        return snaps
    if folder.looks_like_snapshot(mount):
        return [folder.snapshot(mount, source, machine, stamp)]
    return []


def _rename_volumes(snap: Snapshot, renames: dict[str, str]) -> Snapshot:
    volumes = tuple(replace(v, name=renames.get(v.name, renames.get(v.original_name, v.name))) for v in snap.volumes)
    return replace(snap, volumes=volumes)


@contextmanager
def open_sources(
    paths: list[str | Path],
    machine: str | None = None,
    include_in_progress: bool = False,
    stamp: str | None = None,
    rename_volumes: dict[str, str] | None = None,
    rename_machines: dict[str, str] | None = None,
) -> Iterator[list[Snapshot]]:
    """Attach images / inspect folders and yield all their snapshots, sorted by machine then date."""
    with ExitStack() as stack:
        snaps: list[Snapshot] = []
        for p in paths:
            path = Path(p).expanduser()
            if not path.exists():
                raise SourceError(f"{path}: no such file or directory")
            mounts = stack.enter_context(attached(path)) if is_image(path) else [path]
            found = []
            for mount in mounts:
                found += discover(mount, str(path), stack, machine, include_in_progress, stamp)
            if not found:
                raise SourceError(f"{path}: no Time Machine backups found (looked in {', '.join(map(str, mounts))})")
            snaps += found
        if rename_machines:
            snaps = [replace(s, machine=rename_machines.get(s.machine, s.machine)) for s in snaps]
        if rename_volumes:
            snaps = [_rename_volumes(s, rename_volumes) for s in snaps]
        snaps.sort(key=lambda s: (s.machine, s.stamp, s.source))
        yield snaps
