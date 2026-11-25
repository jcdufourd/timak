"""Build small fake Time Machine (legacy) backups for tests."""

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

# tests make their archives as plain folders; test_archiveimage asks for sparse bundles by name
os.environ.setdefault("TIMAK_NO_IMAGE", "1")

MACHINE = "Mac"
S1 = "2020-01-01-000000"
S2 = "2020-01-02-000000"
T0 = 1_577_836_800  # fixed mtimes so copies compare equal
RULES = """
roots = ["/Users"]
rules = ["- ~/Library/Caches/", "- node_modules/"]
"""


def hfs_image(image: Path, size: str, volname: str) -> None:
    """A blank Journaled HFS+ disk image (diskutil image create makes only APFS, ExFAT or FAT)."""
    from timak.diskimage import has_diskutil_image

    if not has_diskutil_image():  # an older macOS
        subprocess.run(["hdiutil", "create", "-quiet", "-size", size, "-fs", "Journaled HFS+", "-volname", volname,
                        str(image)], check=True)  # fmt: skip
        return
    subprocess.run(["diskutil", "image", "create", "blank", "--fs", "None", "--size", size, str(image)],
                   check=True, capture_output=True)  # fmt: skip
    out = subprocess.run(["diskutil", "image", "attach", "--noMount", "--plist", str(image)],
                         check=True, capture_output=True).stdout  # fmt: skip
    disk = min((e["dev-entry"] for e in plistlib.loads(out)["system-entities"]), key=len)
    try:
        subprocess.run(["diskutil", "eraseDisk", "JHFS+", volname, "GPT", disk], check=True, capture_output=True)
    finally:
        subprocess.run(["diskutil", "eject", disk], capture_output=True)


def mount_image(image: Path, mountpoint: Path) -> None:
    from timak.diskimage import has_diskutil_image

    if not has_diskutil_image():
        subprocess.run(["hdiutil", "attach", "-quiet", "-nobrowse", "-mountpoint", str(mountpoint), str(image)],
                       check=True, capture_output=True)  # fmt: skip
        return
    subprocess.run(["diskutil", "image", "attach", "--nobrowse", "--mountPoint", str(mountpoint), str(image)],
                   check=True, capture_output=True)  # fmt: skip


def eject(mountpoint: Path) -> None:
    """Detach the image mounted there, forcing it if busy."""
    from timak.diskimage import detach

    out = subprocess.run(["diskutil", "info", "-plist", str(mountpoint)], capture_output=True).stdout
    if out:
        detach(plistlib.loads(out)["ParentWholeDisk"])


def write(path: Path, text: str, mtime: int = T0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (mtime, mtime))
    return path


def set_xattr(path: Path, name: str, value: str) -> None:
    subprocess.run(["xattr", "-w", name, value, str(path)], check=True)


def link_tree(src: Path, dst: Path, dir_links: bool) -> None:
    """What Time Machine does for an unchanged folder."""
    if dir_links:
        os.link(src, dst)
    else:
        shutil.copytree(src, dst, symlinks=True, copy_function=os.link)


def build_backupdb(base: Path, dir_links: bool) -> Path:
    """Two snapshots; returns the Backups.backupdb folder.

    Built under another name and renamed at the end: on HFS+, macOS (TMSafetyNet)
    refuses most changes inside a folder named Backups.backupdb.
    """
    db = base / "Backups.backupdb"
    staging = base / "staging"
    m = staging / MACHINE
    v1 = m / S1 / "Macintosh HD"
    write(v1 / "Applications/App.app/Contents/MacOS/app", "binary")
    write(v1 / "System/Library/kernel", "kernel")
    home = v1 / "Users/alice"
    write(home / "Documents/a.txt", "alpha")
    write(home / "Documents/sub/b.txt", "beta")
    pic = write(home / "Pictures/p.jpg", "picture" * 1000)
    set_xattr(pic, "com.example.tag", "red")
    write(home / "Library/Caches/c.bin", "cache")
    write(home / "Library/Mail/inbox", "mail")
    write(home / "proj/node_modules/m.js", "module")
    write(home / "proj/main.py", "print()")
    os.symlink("Documents/a.txt", home / "link")
    write(home / "ro/f", "read-only dir")
    os.chmod(home / "ro", 0o555)

    v2 = m / S2 / "Macintosh HD"
    v2.mkdir(parents=True)
    for name in ("Applications", "System"):
        link_tree(v1 / name, v2 / name, dir_links)
    home2 = v2 / "Users/alice"
    (home2 / "Documents").mkdir(parents=True)
    write(home2 / "Documents/a.txt", "alpha, edited", T0 + 60)
    link_tree(home / "Documents/sub", home2 / "Documents/sub", dir_links)
    for name in ("Pictures", "Library", "proj", "ro"):
        link_tree(home / name, home2 / name, dir_links)
    os.symlink("Documents/a.txt", home2 / "link")
    # the picture, copied (new inode) to a new place: only `dedupe` can link it
    moved = write(home2 / "Documents/moved.jpg", "picture" * 1000)
    set_xattr(moved, "com.example.tag", "red")
    os.symlink(S2, m / "Latest")
    staging.rename(db)
    return db
