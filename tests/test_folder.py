import os
import plistlib
import shutil
import tempfile
import unittest
from pathlib import Path

from timak.sources.base import PREFERENCES
from timak.sources.folder import parse_stamp

from fixtures import eject, hfs_image, mount_image, write
from test_pipeline import HAVE_RSYNC, chmod_writable, run

RULES = """
roots = ["/Users"]
rules = ["- ~/Library/Caches/", "- /.Spotlight-V100/"]
[volume_roots]
Large = ["/"]
"""


class VanishedBackupTest(unittest.TestCase):
    def test_stops_with_one_message(self):
        from timak.extract import ExtractError, Stats, VolumeCopy
        from timak.rules import Rules

        tmp = Path(tempfile.mkdtemp(prefix="timak-test-"))
        self.addCleanup(shutil.rmtree, tmp)
        src = tmp / "mount/Data"
        write(src / "Users/alice/a.txt", "a")
        copy = VolumeCopy(src, tmp / "out", Rules.from_text(RULES), "rsync", Stats())
        copy.error("Users/alice/b.txt: Permission denied")  # an ordinary error is only recorded
        self.assertEqual(copy.stats.errors, 1)
        shutil.rmtree(tmp / "mount")  # the image was ejected
        with self.assertRaisesRegex(ExtractError, "backup disappeared.*run the same command again"):
            copy.error("Users/alice: No such file or directory")


class ParseStampTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(parse_stamp("Backup20241029"), "2024-10-29-000000")
        self.assertEqual(parse_stamp("backup 2024-10-29-143000"), "2024-10-29-143000")
        self.assertEqual(parse_stamp("2024-10-29"), "2024-10-29-000000")
        self.assertIsNone(parse_stamp("Backup2021"))
        self.assertIsNone(parse_stamp("Data"))
        self.assertIsNone(parse_stamp("20241399"))  # not a date


def build_copy(path: Path, edited: bool) -> None:
    """A snapshot copied out of Time Machine with Finder: plain files, volumes as sub-folders."""
    data = path / "Data"
    write(data / "Users/alice/Documents/a.txt", "alpha")
    write(data / "Users/alice/Documents/b.txt", "beta, edited" if edited else "beta")
    write(data / "Users/alice/Library/Caches/c", "cache")
    write(data / "Applications/App.app/bin", "binary")
    prefs = data / PREFERENCES
    prefs.parent.mkdir(parents=True)
    prefs.write_bytes(plistlib.dumps({"System": {"System": {"ComputerName": "OldMac"}}}))
    write(path / "Large/Documents/doc.txt", "document")
    write(path / "Large/Apps/tool", "tool")
    write(path / "Large/.Spotlight-V100/index", "index")
    write(path / "Large/notes.txt", "at the top of the disk")
    write(data / "Users/alice/Documents/secret.txt", "private")


@unittest.skipUnless(HAVE_RSYNC, "rsync 3.x not installed")
class FolderSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="timak-test-"))
        self.rules = self.tmp / "rules.toml"
        self.rules.write_text(RULES)
        self.first, self.second = self.tmp / "Backup20241029", self.tmp / "Backup20241108"
        build_copy(self.first, edited=False)
        build_copy(self.second, edited=True)

    def tearDown(self):
        chmod_writable(self.tmp)
        shutil.rmtree(self.tmp)

    def test_two_copies_become_one_timeline(self):
        code, out = run("list", self.first, self.second, "--rules", self.rules)
        self.assertEqual(code, 0, out)
        self.assertIn("OldMac", out)  # name read from the backup
        self.assertIn("2024-10-29-000000", out)
        self.assertIn("2024-11-08-000000", out)
        self.assertIn("Data*", out)
        self.assertIn("Large*", out)

        archive = self.tmp / "archive"
        args = ("--rules", self.rules, "--rename-volume", "Data=System")
        code, _ = run("extract", self.first, self.second, "--archive", archive, *args)
        self.assertEqual(code, 0)

        s1 = archive / "OldMac/2024-10-29-000000"
        s2 = archive / "OldMac/2024-11-08-000000"
        self.assertEqual((s1 / "System/Users/alice/Documents/a.txt").read_text(), "alpha")
        self.assertEqual((s2 / "Large/Documents/doc.txt").read_text(), "document")
        self.assertFalse((s1 / "Data").exists())  # renamed
        self.assertFalse((s1 / "System/Applications").exists())
        self.assertFalse((s1 / "System/Users/alice/Library/Caches").exists())
        # Large is kept whole, except macOS bookkeeping
        self.assertEqual((s1 / "Large/Apps/tool").read_text(), "tool")
        self.assertEqual((s1 / "Large/notes.txt").read_text(), "at the top of the disk")
        self.assertFalse((s1 / "Large/.Spotlight-V100").exists())

        code, out = run("scan", self.first, "--rules", self.rules)
        self.assertEqual(code, 0)
        self.assertIn("on Large", out)  # kept space per volume
        self.assertIn("on Data", out)

        # the copies share no inodes on their disk, yet unchanged files are linked in the archive
        ino = lambda p: os.lstat(p).st_ino
        self.assertNotEqual(ino(self.first / "Data/Users/alice/Documents/a.txt"), ino(self.second / "Data/Users/alice/Documents/a.txt"))
        self.assertEqual(ino(s1 / "System/Users/alice/Documents/a.txt"), ino(s2 / "System/Users/alice/Documents/a.txt"))
        self.assertEqual(ino(s1 / "Large/Documents/doc.txt"), ino(s2 / "Large/Documents/doc.txt"))
        self.assertNotEqual(ino(s1 / "System/Users/alice/Documents/b.txt"), ino(s2 / "System/Users/alice/Documents/b.txt"))
        self.assertEqual((s2 / "System/Users/alice/Documents/b.txt").read_text(), "beta, edited")

        code, out = run("verify", self.first, self.second, "--archive", archive, *args, "--content")
        self.assertEqual(code, 0, out)

    @unittest.skipIf(os.geteuid() == 0, "root reads everything")
    def test_unreadable_file_is_recorded(self):
        import json

        secret = self.first / "Data/Users/alice/Documents/secret.txt"
        secret.chmod(0)
        archive = self.tmp / "archive"
        code, _ = run("extract", self.first, "--archive", archive, "--rules", self.rules)
        self.assertEqual(code, 1)  # finished, with problems
        info = json.loads((archive / "OldMac/2024-10-29-000000/.timak.json").read_text())
        self.assertEqual(info["stats"]["errors"], 1)
        (problem,) = info["problems"]
        self.assertTrue(problem.startswith("Data/Users/alice/Documents/secret.txt: Permission denied"), problem)

    def test_stamp_option(self):
        undated = self.tmp / "copy"
        self.first.rename(undated)
        code, _ = run("list", undated, "--rules", self.rules)
        self.assertEqual(code, 2)  # cannot tell the date
        code, out = run("list", undated, "--rules", self.rules, "--stamp", "2024-10-29")
        self.assertEqual(code, 0)
        self.assertIn("2024-10-29-000000", out)

    def test_warns_about_unconfigured_volume(self):
        import contextlib
        import io

        from timak.cli import main

        write(self.first / "Spare/stuff.txt", "on a disk nobody mentioned")
        archive = self.tmp / "archive"

        def run_both(*argv):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main([str(a) for a in argv])
            return code, out.getvalue(), err.getvalue()

        code, out, err = run_both("list", self.first, "--rules", self.rules)
        self.assertEqual(code, 0)
        self.assertIn('volume "Spare"', err)
        self.assertIn("SKIPPED", err)
        self.assertNotIn('volume "Data"', err)
        self.assertNotIn('volume "Large"', err)

        code, out, _ = run_both("extract", self.first, "--archive", archive, "--rules", self.rules)
        self.assertEqual(code, 0)
        self.assertIn('volume "Spare"', out)  # repeated at the end of the run
        self.assertFalse((archive / "OldMac/2024-10-29-000000/Spare").exists())

        # deciding on it silences the warning
        quiet = self.tmp / "quiet.toml"
        quiet.write_text(RULES + '"Spare" = []\n')
        _, _, err = run_both("list", self.first, "--rules", quiet)
        self.assertNotIn("Spare", err)
        # so does choosing volumes on the command line
        _, _, err = run_both("list", self.first, "--rules", self.rules, "--volume", "Data")
        self.assertNotIn("Spare", err)

    def test_rename_volume_syntax(self):
        code, _ = run("list", self.first, "--rename-volume", "Data")
        self.assertEqual(code, 2)


@unittest.skipUnless(HAVE_RSYNC and shutil.which("diskutil"), "needs macOS and rsync 3.x")
class SecondPrecisionArchiveTest(unittest.TestCase):
    """APFS sources keep nanoseconds, an HFS+ archive whole seconds: unchanged files must still be linked."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="timak-test-"))
        self.mount = self.tmp / "hfs"
        self.mount.mkdir()
        image = self.tmp / "archive.dmg"
        hfs_image(image, "20m", "TimakHFS")
        mount_image(image, self.mount)

    def tearDown(self):
        eject(self.mount)
        chmod_writable(self.tmp)
        shutil.rmtree(self.tmp)

    def test_unchanged_files_are_linked(self):
        first, second = self.tmp / "Backup20241029", self.tmp / "Backup20241108"
        build_copy(first, edited=False)
        build_copy(second, edited=True)
        fraction = 1_700_000_000_123_456_789
        for copy in (first, second):
            for f in copy.rglob("*"):
                if f.is_file():
                    os.utime(f, ns=(fraction, fraction))
        archive = self.mount / "archive"
        code, out = run("extract", first, second, "--archive", archive, "--rules", self.rules())
        self.assertEqual(code, 0, out)

        s1, s2 = archive / "OldMac/2024-10-29-000000", archive / "OldMac/2024-11-08-000000"
        self.assertEqual(os.lstat(s1 / "Data/Users/alice/Documents/a.txt").st_mtime_ns % 1_000_000_000, 0)  # really HFS+
        ino = lambda p: os.lstat(p).st_ino
        self.assertEqual(ino(s1 / "Data/Users/alice/Documents/a.txt"), ino(s2 / "Data/Users/alice/Documents/a.txt"))
        self.assertNotEqual(ino(s1 / "Data/Users/alice/Documents/b.txt"), ino(s2 / "Data/Users/alice/Documents/b.txt"))

        code, out = run("verify", first, second, "--archive", archive, "--rules", self.rules())
        self.assertEqual(code, 0, out)

    def rules(self) -> Path:
        path = self.tmp / "rules.toml"
        path.write_text(RULES)
        return path


if __name__ == "__main__":
    unittest.main()
