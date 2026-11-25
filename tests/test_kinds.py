import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from timak.cli import main
from timak.kinds import OTHER, extension, kind_of, kinds

S1, S2 = "2020-01-01-000000", "2020-02-01-000000"


def write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size))
    return path


class KindOfTest(unittest.TestCase):
    def test_place_wins_over_extension(self):
        self.assertEqual(kind_of("HD/Users/alice/Library/Mail/V10/INBOX.mbox/Messages", "1.emlx"), "mail")
        self.assertEqual(kind_of("HD/Users/alice/Library/Mail/V10/Attachments", "photo.jpg"), "mail")
        self.assertEqual(kind_of("HD/Users/alice/Pictures/Photos Library.photoslibrary/originals/0", "a.heic"), "photos library")
        self.assertEqual(kind_of("HD/Users/alice/src/x/.git/objects/ab", "cdef"), "code")
        self.assertEqual(kind_of("HD/Users/alice/VMs/Win.vmwarevm", "disk.vmdk"), "virtual machines")
        self.assertEqual(kind_of("HD/Users/alice/Library/Messages/Attachments/0a", "IMG_1.HEIC"), "messages")

    def test_by_extension(self):
        self.assertEqual(kind_of("HD/Users/alice/Pictures", "IMG_1.JPG"), "images")
        self.assertEqual(kind_of("HD/Users/alice/Movies", "a.mov"), "videos")
        self.assertEqual(kind_of("HD/Users/alice/Documents", "a.pdf"), "documents")
        self.assertEqual(kind_of("HD/Users/alice/Music", "a.flac"), "audio")
        self.assertEqual(kind_of("HD/Users/alice/src", "a.ts"), "code")
        self.assertEqual(kind_of("HD/Users/alice", "README"), OTHER)
        self.assertEqual(kind_of("HD/Users/alice/Library/Mailbox", "x.dat"), OTHER)  # not ~/Library/Mail/

    def test_extension(self):
        self.assertEqual(extension("a.TAR.GZ"), "gz")
        self.assertEqual(extension(".bashrc"), "")
        self.assertEqual(extension("photos.sqlite-wal"), "sqlite-wal")
        self.assertEqual(extension("Version 2.0 final"), "")


class KindsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="timak-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        for machine in ("Mac", "Old"):
            for s in (S1, S2):
                (self.tmp / machine / s / "HD").mkdir(parents=True)
                (self.tmp / machine / s / ".timak.json").write_text(json.dumps({"stats": {}}))
        m, home = self.tmp / "Mac", "HD/Users/alice"
        movie = write(m / S1 / home / "Movies/big.mov", 400_000)
        (m / S2 / home / "Movies").mkdir(parents=True)
        os.link(movie, m / S2 / home / "Movies/big.mov")  # one file, two snapshots
        write(m / S2 / home / "Library/Mail/V2/a.emlx", 20_000)
        write(m / S2 / home / "Documents/a.pdf", 10_000)
        write(self.tmp / "Old" / S1 / home / "weird.xyz", 30_000)
        write(self.tmp / "Mac" / f"{S2}.partial/HD/huge.mov", 900_000)  # unfinished: ignored

    def test_counts_each_file_once(self):
        stats = kinds(self.tmp)
        self.assertEqual(stats.snapshots, 4)
        self.assertEqual(stats.paths, 5)
        self.assertEqual(stats.files, 4)
        self.assertEqual(stats.kinds["videos"].files, 1)
        self.assertLess(stats.kinds["videos"].size, 800_000)
        self.assertEqual(set(stats.kinds), {"videos", "mail", "documents", OTHER})
        self.assertEqual(stats.kinds[OTHER].ext_files["xyz"], 1)
        self.assertEqual(set(stats.by_machine), {"Mac", "Old"})
        self.assertEqual(stats.size, sum(k.size for k in stats.kinds.values()))

    def test_command_writes_report(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(["kinds", str(self.tmp)])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("videos", text)
        self.assertIn("xyz", text)  # in the list of what "other" holds
        self.assertIn("by machine:", text)
        self.assertIn("videos", (self.tmp / "kinds.timakreport").read_text())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            main(["kinds", str(self.tmp / "Mac" / S2)])
        self.assertTrue((self.tmp / f"kinds-Mac-{S2}.timakreport").exists())

    def test_missing_archive(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["kinds", str(self.tmp / "nope")]), 2)


if __name__ == "__main__":
    unittest.main()
