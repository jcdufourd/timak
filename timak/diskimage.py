"""Read-only attach/detach of disk images with `diskutil image` and `diskutil eject`.

hdiutil's attach and detach are deprecated for these. A macOS too old to have
`diskutil image` still uses hdiutil for both. `hdiutil info` has no
replacement yet (`diskutil image info` describes one image, not what is
attached), so it still says whether an image is already attached.
"""

from __future__ import annotations

import functools
import os
import plistlib
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .util import log

IMAGE_SUFFIXES = (".dmg", ".sparseimage", ".sparsebundle", ".img", ".cdr")


class DiskImageError(RuntimeError):
    pass


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_SUFFIXES


@functools.cache
def has_diskutil_image() -> bool:
    """Whether this macOS has `diskutil image` (an unknown diskutil verb fails)."""
    try:
        proc = subprocess.run(["diskutil", "image", "--help"], capture_output=True, stdin=subprocess.DEVNULL)
    except OSError:
        return False
    return proc.returncode == 0


def attach_command(image: Path, readonly: bool = True, owners: bool = False) -> list[str]:
    """owners: honour file owners (as root, to keep them); macOS ignores them on images by default."""
    if has_diskutil_image():
        return ["diskutil", "image", "attach", *(["--readOnly"] if readonly else []), "--nobrowse", "--plist",
                *(["--mountOptions", "owners"] if owners else []), str(image)]  # fmt: skip
    return ["hdiutil", "attach", *(["-readonly"] if readonly else []), "-nobrowse", "-noverify", "-noautofsck",
            "-noautoopen", *(["-owners", "on"] if owners else []), "-plist", str(image)]  # fmt: skip


def _plist(cmd: list[str]) -> dict:
    proc = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        why = (proc.stderr or proc.stdout).decode(errors="replace").strip()  # diskutil says it on stdout
        raise DiskImageError(f"{' '.join(cmd)} failed: {why}")
    try:
        return plistlib.loads(proc.stdout)
    except plistlib.InvalidFileException as e:
        raise DiskImageError(f"unexpected output from {' '.join(cmd)}") from e


def _mounts(entities: list[dict]) -> list[Path]:
    return [Path(e["mount-point"]) for e in entities if e.get("mount-point")]


def _whole_disk(entities: list[dict]) -> str | None:
    devs = [e["dev-entry"] for e in entities if e.get("dev-entry")]
    return min(devs, key=len) if devs else None


def already_attached(image: Path) -> list[Path] | None:
    info = _plist(["hdiutil", "info", "-plist"])
    target = image.resolve()
    for img in info.get("images", []):
        path = img.get("image-path")
        if path and Path(path).resolve() == target:
            return _mounts(img.get("system-entities", []))
    return None


@contextmanager
def attached(image: Path, readonly: bool = True, owners: bool = False) -> Iterator[list[Path]]:
    """Mount every filesystem of `image` (read-only unless asked); detach on exit unless it was already attached."""
    existing = already_attached(image)
    if existing is not None:
        if not existing:
            raise DiskImageError(f"{image} is attached but nothing is mounted")
        if not readonly and not all(os.access(m, os.W_OK) for m in existing):
            raise DiskImageError(f"{image} is mounted read-only at {', '.join(map(str, existing))}: eject it first")
        log(f"{image.name} is already mounted at {', '.join(map(str, existing))}: timak uses that mount, "
            "so don't eject it until timak is done")
        yield existing
        return

    info = _plist(attach_command(image, readonly, owners))
    entities = info.get("system-entities", [])
    disk = _whole_disk(entities)
    try:
        mounts = _mounts(entities)
        if not mounts:
            raise DiskImageError(f"{image}: no mountable filesystem")
        yield mounts
    finally:
        if disk:
            detach(disk)


def detach(disk: str) -> None:
    """Eject an attached image (its whole disk, e.g. disk8), forcing the unmount if it is busy."""
    run = lambda *cmd: subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL).returncode
    if not has_diskutil_image():
        if run("hdiutil", "detach", disk) != 0:
            run("hdiutil", "detach", "-force", disk)
        return
    if run("diskutil", "eject", disk) != 0:
        run("diskutil", "unmountDisk", "force", disk)
        run("diskutil", "eject", disk)
