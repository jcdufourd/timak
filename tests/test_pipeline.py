import contextlib
import io
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from timak.cli import main
from timak.extract import ExtractError, find_rsync
from timak.util import INFO_NAME, has_full_disk_access, xattrs

from fixtures import MACHINE, RULES, S1, S2, build_backupdb, eject, hfs_image, mount_image

try:
    find_rsync()
    HAVE_RSYNC = True
except ExtractError:
    HAVE_RSYNC = False


def run(*argv) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main([str(a) for a in argv])
    return code, out.getvalue()


def chmod_writable(root: Path) -> None:
    for dirpath, dirnames, _ in os.walk(root):
        for d in dirnames:
            os.chmod(os.path.join(dirpath, d), 0o755)


class PipelineChecks:
    """Assertions shared by the folder test and the disk image test."""

    def check_pipeline(self, source: Path, tmp: Path, expect_shortcuts: bool):
        rules = tmp / "rules.toml"
        rules.write_text(RULES)
        archive = tmp / "archive"

        code, out = run("list", source, "--rules", rules)
        self.assertEqual(code, 0)
        self.assertIn(S1, out)
        self.assertIn(S2, out)

        code, out = run("scan", source, "--rules", rules)
        self.assertEqual(code, 0)
        self.assertIn("outside roots: /Applications", out)
        self.assertIn("- ~/Library/Caches/", out)
        self.assertIn("- node_modules/", out)

        code, _ = run("extract", source, "--archive", archive, "--rules", rules)
        self.assertEqual(code, 0)

        a1 = archive / MACHINE / S1 / "Macintosh HD"
        a2 = archive / MACHINE / S2 / "Macintosh HD"
        h1, h2 = a1 / "Users/alice", a2 / "Users/alice"

        # dropped
        for p in ("Applications", "System"):
            self.assertFalse((a1 / p).exists(), p)
        self.assertFalse((h1 / "Library/Caches").exists())
        self.assertFalse((h1 / "proj/node_modules").exists())
        # kept
        self.assertEqual((h1 / "Documents/a.txt").read_text(), "alpha")
        self.assertEqual((h2 / "Documents/a.txt").read_text(), "alpha, edited")
        self.assertEqual((h2 / "Library/Mail/inbox").read_text(), "mail")
        self.assertEqual(os.readlink(h2 / "link"), "Documents/a.txt")
        self.assertEqual(stat.S_IMODE(os.lstat(h2 / "ro").st_mode), 0o555)
        self.assertEqual(xattrs(h2 / "Pictures/p.jpg").get("com.example.tag"), b"red")
        self.assertTrue((archive / MACHINE / S2 / INFO_NAME).is_file())
        self.assertFalse(any(p.name.endswith(".partial") for p in (archive / MACHINE).iterdir()))

        # unchanged files are shared, changed ones are not
        ino = lambda p: os.lstat(p).st_ino
        self.assertEqual(ino(h1 / "Documents/sub/b.txt"), ino(h2 / "Documents/sub/b.txt"))
        self.assertEqual(ino(h1 / "Pictures/p.jpg"), ino(h2 / "Pictures/p.jpg"))
        self.assertEqual(ino(h1 / "ro/f"), ino(h2 / "ro/f"))
        self.assertNotEqual(ino(h1 / "Documents/a.txt"), ino(h2 / "Documents/a.txt"))
        self.assertNotEqual(ino(h2 / "Documents/moved.jpg"), ino(h2 / "Pictures/p.jpg"))

        import json

        stats = json.loads((archive / MACHINE / S2 / INFO_NAME).read_text())["stats"]
        if expect_shortcuts:
            self.assertGreater(stats["dirs_shortcut"], 0)
        self.assertEqual(stats["copied"], 2)  # edited a.txt and moved.jpg

        code, out = run("verify", source, "--archive", archive, "--rules", rules, "--content")
        self.assertEqual(code, 0, out)

        # resuming does nothing
        code, _ = run("extract", source, "--archive", archive, "--rules", rules)
        self.assertEqual(code, 0)

        # dedupe links the moved copy
        code, out = run("dedupe", archive)
        self.assertEqual(code, 0, out)
        self.assertIn("1 duplicates merged", out)
        self.assertEqual(ino(h2 / "Documents/moved.jpg"), ino(h2 / "Pictures/p.jpg"))
        code, out = run("verify", source, "--archive", archive, "--rules", rules)
        self.assertEqual(code, 0, out)

        code, out = run("du", archive)
        self.assertEqual(code, 0)
        return archive


@unittest.skipUnless(HAVE_RSYNC, "rsync 3.x not installed")
class FolderPipelineTest(PipelineChecks, unittest.TestCase):
    """Backups as plain folders (no directory hard links: they need HFS+)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="timak-test-"))

    def tearDown(self):
        chmod_writable(self.tmp)
        shutil.rmtree(self.tmp)

    def test_pipeline(self):
        db = build_backupdb(self.tmp / "src", dir_links=False)
        self.check_pipeline(db, self.tmp, expect_shortcuts=False)

    def test_rename_machine(self):
        db = build_backupdb(self.tmp / "src", dir_links=False)
        code, out = run("list", db, "--machine", "OldMac")
        self.assertEqual(code, 2)  # legacy --machine only filters
        archive = self.tmp / "archive"
        code, _ = run("extract", db, "--archive", archive, "--rename-machine", f"{MACHINE}=OldMac")
        self.assertEqual(code, 0)
        self.assertEqual([p.name for p in archive.iterdir() if not p.name.startswith(".")], ["OldMac"])

    def test_interrupted_snapshot_is_redone(self):
        db = build_backupdb(self.tmp / "src", dir_links=False)
        rules = self.tmp / "rules.toml"
        rules.write_text(RULES)
        archive = self.tmp / "archive"
        leftover = archive / MACHINE / (S1 + ".partial") / "junk"
        leftover.mkdir(parents=True)
        code, _ = run("extract", db, "--archive", archive, "--rules", rules)
        self.assertEqual(code, 0)
        self.assertFalse(leftover.parent.exists())
        self.assertTrue((archive / MACHINE / S1 / "Macintosh HD/Users/alice/Documents/a.txt").exists())

    def test_verify_detects_damage(self):
        db = build_backupdb(self.tmp / "src", dir_links=False)
        rules = self.tmp / "rules.toml"
        rules.write_text(RULES)
        archive = self.tmp / "archive"
        run("extract", db, "--archive", archive, "--rules", rules)
        (archive / MACHINE / S2 / "Macintosh HD/Users/alice/Documents/a.txt").unlink()
        (archive / MACHINE / S2 / "Macintosh HD/Users/alice/stray").write_text("x")
        (archive / MACHINE / S2 / "Macintosh HD/Users/alice/node_modules").mkdir()  # dropped by the rules: ignored
        code, out = run("verify", db, "--archive", archive, "--rules", rules)
        self.assertEqual(code, 1)
        self.assertIn("a.txt: missing", out)
        self.assertIn("stray: not in source", out)
        self.assertNotIn("node_modules", out.split("entries checked")[0])
        self.assertIn("1 files only in the archive ignored", out)
        # the full list goes to verify-<source>.timakreport, even when fewer are printed
        code, out = run("verify", db, "--archive", archive, "--rules", rules, "--max-problems", "0")
        self.assertNotIn("a.txt: missing", out.split("more problems")[0])
        report = (archive / "verify-src.timakreport").read_text()  # db is src/Backups.backupdb
        self.assertIn("a.txt: missing", report)
        self.assertIn("stray: not in source", report)
        self.assertIn("2 problems", report)
        self.assertIn("full report:", out)


@unittest.skipUnless(HAVE_RSYNC, "rsync 3.x not installed")
class UnchangedTreeShortcutTest(unittest.TestCase):
    """The directory-inode shortcut, without HFS+: a source compared with itself is entirely unchanged."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="timak-test-"))

    def tearDown(self):
        chmod_writable(self.tmp)
        shutil.rmtree(self.tmp)

    def test_mirror(self):
        from timak.extract import Stats, VolumeCopy
        from timak.rules import Rules

        db = build_backupdb(self.tmp / "src", dir_links=False)
        rules = Rules(["/Users"], ["- ~/Library/Caches/", "- node_modules/"])
        src = db / MACHINE / S1 / "Macintosh HD"
        first, second = self.tmp / "out1", self.tmp / "out2"
        VolumeCopy(src, first, rules, find_rsync(), Stats()).run()

        stats = Stats()
        VolumeCopy(src, second, rules, find_rsync(), stats, prev_out=first, prev_src=src).run()
        self.assertEqual(stats.copied, 0)
        self.assertGreater(stats.dirs_shortcut, 0)
        for dirpath, _, files in os.walk(first):
            for name in files:
                a = Path(dirpath) / name
                b = second / a.relative_to(first)
                self.assertEqual(os.lstat(a).st_ino, os.lstat(b).st_ino, str(b))
        self.assertEqual(stat.S_IMODE(os.lstat(second / "Users/alice/ro").st_mode), 0o555)
        self.assertFalse((second / "Users/alice/Library/Caches").exists())


@unittest.skipUnless(HAVE_RSYNC and shutil.which("diskutil"), "needs macOS and rsync 3.x")
@unittest.skipUnless(has_full_disk_access(), "needs Full Disk Access: macOS locks Backups.backupdb on HFS+ volumes")
class DiskImagePipelineTest(PipelineChecks, unittest.TestCase):
    """A real HFS+ disk image with Time Machine-style directory hard links."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="timak-test-"))
        self.image = self.tmp / "tm.dmg"
        hfs_image(self.image, "30m", "TimakTest")
        mount = self.tmp / "mnt"
        mount_image(self.image, mount)
        try:
            build_backupdb(mount, dir_links=True)
        finally:
            eject(mount)

    def tearDown(self):
        chmod_writable(self.tmp)
        shutil.rmtree(self.tmp)

    def test_pipeline(self):
        self.check_pipeline(self.image, self.tmp, expect_shortcuts=True)
        # the image was detached again (hdiutil info: diskutil has no list of attached images)
        info = subprocess.run(["hdiutil", "info"], capture_output=True, text=True).stdout
        self.assertNotIn(str(self.image), info)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_RSYNC, "rsync 3.x not installed")
class SparseFileWarningTest(unittest.TestCase):
    """A big file made of holes is written in full: extract says so."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="timak-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_sparse_file_is_reported(self):
        from unittest import mock

        docs = self.tmp / "Backup20250101/Users/alice/Documents"
        docs.mkdir(parents=True)
        with open(docs / "big.dmg", "wb") as f:
            f.truncate(8 << 20)  # 8 MB of holes
        (docs / "dense.bin").write_bytes(os.urandom(4 << 20))
        if os.stat(docs / "big.dmg").st_blocks * 512 >= 4 << 20:
            self.skipTest("this filesystem does not keep files sparse")
        out = io.StringIO()
        with mock.patch("timak.extract.SPARSE_MIN", 1 << 20), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(["extract", str(self.tmp / "Backup20250101"), "--archive", str(self.tmp / "archive")])
        out = out.getvalue()
        self.assertEqual(code, 0, out)
        warnings = [l for l in out.splitlines() if "sparse file of" in l]
        self.assertEqual(len(warnings), 1, out)
        self.assertIn("Users/alice/Documents/big.dmg: sparse file", warnings[0])
        self.assertIn('add "- /Users/alice/Documents/big.dmg"', warnings[0])
        self.assertIn("1 sparse files written in full", out)
