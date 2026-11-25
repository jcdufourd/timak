import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from timak.rules import Rules
from timak.scan import KEPT, Scanner, file_key
from timak.sources.base import Snapshot, Volume


class FileKeyTest(unittest.TestCase):
    def st(self, dev, ino, size=10, mtime=1):
        return SimpleNamespace(st_dev=dev, st_ino=ino, st_size=size, st_mtime_ns=mtime)

    def test_legacy_uses_device_and_inode(self):
        self.assertEqual(file_key(self.st(1, 5), None), file_key(self.st(1, 5, size=99), None))
        self.assertNotEqual(file_key(self.st(1, 5), None), file_key(self.st(2, 5), None))

    def test_apfs_ignores_mount_device_but_not_content_changes(self):
        a = file_key(self.st(100, 5), "img|disk14s2|Data")
        self.assertEqual(a, file_key(self.st(101, 5), "img|disk14s2|Data"))  # same file, another snapshot mount
        self.assertNotEqual(a, file_key(self.st(101, 5, size=11), "img|disk14s2|Data"))  # rewritten in place
        self.assertNotEqual(a, file_key(self.st(101, 5), "img|disk15s2|Data"))  # another backup volume


class ScannerTest(unittest.TestCase):
    def test_apfs_snapshots_count_unchanged_files_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            vol = Path(tmp) / "Macintosh HD - Data"
            docs = vol / "Users/alice/Documents"
            docs.mkdir(parents=True)
            (docs / "a.txt").write_bytes(b"x" * 10000)
            (vol / "Applications").mkdir()
            (vol / "Applications/app").write_bytes(b"y" * 10000)
            snaps = []
            for i, stamp in enumerate(["2021-01-10-195352", "2021-01-19-115232", "2021-01-30-214902"]):
                # each snapshot: its own directories (not skippable) holding the same file inodes
                v = vol if i == 0 else Path(tmp) / f"snap{i}" / vol.name
                if i:
                    shutil.copytree(vol, v, copy_function=os.link)
                snaps.append(Snapshot(
                    "apfs", "Mac", stamp, v.parent, (Volume("Macintosh HD", v, vol.name),),
                    source="tm.dmg", extra={"device": "disk14s2"},
                ))  # fmt: skip
            scanner = Scanner(Rules(["/Users"], []))
            scanner.scan(snaps)
            self.assertEqual(scanner.buckets[KEPT].files, 1)
            self.assertEqual(scanner.buckets[KEPT].bytes, os.lstat(docs / "a.txt").st_blocks * 512)
            self.assertEqual(scanner.buckets["outside roots: /Applications"].files, 1)


if __name__ == "__main__":
    unittest.main()
