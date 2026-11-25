"""Common model for backup sources: a source yields dated snapshots of volumes."""

from __future__ import annotations

import os
import plistlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from ..rules import Rules

STAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2}-\d{6})")


class SourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Volume:
    name: str  # normalized name used in the archive ("Macintosh HD - Data" -> "Macintosh HD")
    path: Path  # root of the backed-up volume inside the snapshot
    original_name: str = ""


@dataclass(frozen=True)
class Snapshot:
    kind: str  # "legacy" or "apfs"
    machine: str
    stamp: str  # YYYY-MM-DD-HHMMSS
    path: Path
    volumes: tuple[Volume, ...]
    source: str = ""  # the image or mount it came from
    in_progress: bool = False
    extra: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def label(self) -> str:
        return f"{self.machine} {self.stamp}"


# the suffix macOS gives the data volume, in a few system languages
DATA_SUFFIXES = (" - Data", " - Données", " - Daten", " - Datos", " - Dati", " - Dados")


def normalize_volume_name(name: str) -> str:
    """Catalina+ split volumes back up as "<name> - Data"; keep one archive name over time."""
    name = unicodedata.normalize("NFC", name)  # names on disk may be decomposed ("e" + accent)
    for suffix in DATA_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def is_data_volume(name: str) -> bool:
    return unicodedata.normalize("NFC", name).endswith(DATA_SUFFIXES)


def volumes_in(snapshot_dir: Path) -> tuple[Volume, ...]:
    try:
        entries = sorted(os.scandir(snapshot_dir), key=lambda e: e.name)
    except PermissionError as e:
        raise SourceError(
            f"cannot read {snapshot_dir}: {e.strerror}. "
            "Give your terminal Full Disk Access (System Settings > Privacy & Security), or run with sudo."
        ) from e
    return tuple(
        Volume(normalize_volume_name(e.name), Path(e.path), e.name)
        for e in entries
        if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")
    )


def select_volumes(snapshot: Snapshot, rules: Rules, names: list[str] | None = None) -> list[Volume]:
    """Volumes worth extracting: those containing a root (or the ones named), one per archive name."""
    chosen: dict[str, Volume] = {}
    for vol in snapshot.volumes:
        if names:
            if vol.name not in names and vol.original_name not in names:
                continue
        elif not any((vol.path / rel).is_dir() for rel in rules.root_rels(vol)):
            continue
        prev = chosen.get(vol.name)
        # if both "X" and "X - Data" are present, user files live on the Data volume
        if prev is None or is_data_volume(vol.original_name):
            chosen[vol.name] = vol
    return list(chosen.values())


def unconfigured_volumes(snapshots: list[Snapshot], rules: Rules) -> dict[str, list[str]]:
    """Volumes skipped only because nothing mentions them: name -> stamps of the snapshots holding them.

    A volume listed in [volume_roots] (even as []) was decided on, and the
    system half of an "X" / "X - Data" pair is expected to be skipped.
    """
    found: dict[str, list[str]] = {}
    for snap in snapshots:
        chosen = {v.name for v in select_volumes(snap, rules)}
        for vol in snap.volumes:
            if vol.name in chosen or rules.configures(vol):
                continue
            found.setdefault(vol.original_name, []).append(snap.stamp)
    return found


PREFERENCES ="Library/Preferences/SystemConfiguration/preferences.plist"


def computer_name(snapshot: Snapshot) -> str | None:
    """The Mac's name as stored in the backup; legacy backups name their machine folder after it."""
    for vol in sorted(snapshot.volumes, key=lambda v: not is_data_volume(v.original_name)):
        try:
            with open(vol.path / PREFERENCES, "rb") as f:
                name = plistlib.load(f)["System"]["System"]["ComputerName"]
        except (OSError, KeyError, TypeError, plistlib.InvalidFileException):
            continue
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def safe_name(name: str) -> str:
    return name.replace("/", "_").strip() or "_"
