"""APFS Time Machine backups (macOS 11+): each backup is a read-only APFS snapshot.

The backup volume's snapshots are named com.apple.TimeMachine.<stamp>.backup.
We list them with `diskutil apfs listSnapshots` (no Full Disk Access needed)
and mount each one read-only ourselves with `mount_apfs -s` (needs root), so
we do not depend on macOS having mounted them under /Volumes/.timemachine.
APFS mounts a snapshot only once, though: on a disk still in use by Time
Machine, macOS keeps the latest snapshot mounted there, and we use that mount.
Inside a mounted snapshot the backed-up volumes sit in a `<stamp>.backup` folder.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import tempfile
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

from .base import Snapshot, SourceError, computer_name, volumes_in

TM_SNAPSHOT_RE = re.compile(r"com\.apple\.TimeMachine\.(\d{4}-\d{2}-\d{2}-\d{6})\.backup")


def _plist(cmd: list[str]) -> dict | None:
    proc = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        return None
    try:
        return plistlib.loads(proc.stdout)
    except plistlib.InvalidFileException:
        return None


def volume_info(mount: Path) -> dict | None:
    return _plist(["diskutil", "info", "-plist", str(mount)])


def tm_snapshots(device: str) -> list[tuple[str, str]]:
    """(stamp, snapshot name) of the Time Machine snapshots on an APFS volume, oldest first."""
    data = _plist(["diskutil", "apfs", "listSnapshots", "-plist", device]) or {}
    result = []
    for snap in data.get("Snapshots", []):
        name = snap.get("SnapshotName", "")
        if m := TM_SNAPSHOT_RE.fullmatch(name):
            result.append((m.group(1), name))
    return sorted(result)


def destination_device(mount: Path) -> str | None:
    """The device of `mount` if it is an APFS volume holding Time Machine snapshots."""
    info = volume_info(mount)
    if not info or info.get("FilesystemType") != "apfs" or info.get("MountPoint") != str(mount):
        return None
    device = info.get("DeviceIdentifier")
    return device if device and tm_snapshots(device) else None


def _volume_parent(root: Path) -> Path:
    # backed-up volumes live in <stamp>.backup/ at the snapshot root
    try:
        children = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    except PermissionError as e:
        raise SourceError(
            f"cannot read {root}: {e.strerror}. Give your terminal Full Disk Access "
            "(System Settings > Privacy & Security) and restart it."
        ) from e
    backups = [p for p in children if p.name.endswith(".backup")]
    return backups[0] if len(backups) == 1 else root


_MOUNT_LINE_RE = re.compile(r"^(?P<name>\S+)@/dev/(?P<device>\S+) on (?P<path>.+) \([^()]*\)$")


def mounted_snapshots(mount_output: str | None = None) -> dict[tuple[str, str], Path]:
    """(device, snapshot name) -> mount point, for the snapshots already mounted (e.g. by Time Machine)."""
    if mount_output is None:
        proc = subprocess.run(["mount"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        mount_output = proc.stdout if proc.returncode == 0 else ""
    result = {}
    for line in mount_output.splitlines():
        if m := _MOUNT_LINE_RE.match(line):
            result[(m["device"], m["name"])] = Path(m["path"])
    return result


def _mount(device: str, name: str, mountpoint: Path, stack: ExitStack) -> None:
    proc = subprocess.run(
        ["mount_apfs", "-o", "rdonly,nobrowse", "-s", name, f"/dev/{device}", str(mountpoint)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )  # fmt: skip
    if proc.returncode != 0:
        try:
            mountpoint.rmdir()
        except OSError:
            pass
        hint = " Mounting APFS snapshots needs root: rerun with sudo." if os.geteuid() != 0 else ""
        raise SourceError(f"cannot mount snapshot {name} of /dev/{device}: {proc.stderr.strip()}.{hint}")
    stack.callback(_unmount, mountpoint)


def _unmount(mountpoint: Path) -> None:
    if subprocess.run(["umount", str(mountpoint)], capture_output=True).returncode != 0:
        subprocess.run(["diskutil", "unmount", "force", str(mountpoint)], capture_output=True)
    try:
        mountpoint.rmdir()
    except OSError:
        pass


def snapshots(mount: Path, device: str, source: str, stack: ExitStack, machine: str | None = None) -> list[Snapshot]:
    """Mount every Time Machine snapshot of `device` read-only; they are unmounted when `stack` closes.

    Without `machine`, all snapshots are named after the computer name found in the
    most recent one (one name per source, so a renamed Mac stays one timeline).
    """
    base = Path(tempfile.mkdtemp(prefix="timak-snapshots-"))
    stack.callback(lambda: base.rmdir() if base.exists() and not any(base.iterdir()) else None)
    result = []
    already = mounted_snapshots()
    for stamp, name in tm_snapshots(device):
        mountpoint = already.get((device, name))
        if mountpoint is None:  # not mounted by macOS: mount it ourselves, unmount it when done
            mountpoint = base / stamp
            mountpoint.mkdir()
            _mount(device, name, mountpoint, stack)
        parent = _volume_parent(mountpoint)
        result.append(
            Snapshot(
                kind="apfs",
                machine=machine or mount.name,
                stamp=stamp,
                path=parent,
                volumes=volumes_in(parent),
                source=source,
                extra={"device": device},
            )
        )
    if not machine and result:
        name = computer_name(result[-1])
        if name:
            result = [replace(s, machine=name) for s in result]
    return result
