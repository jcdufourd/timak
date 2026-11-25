import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from timak.biggest import Biggest, suggested_rule
from timak.cli import main
from timak.util import INFO_NAME

S1, S2, S3 = "2020-01-01-000000", "2020-02-01-000000", "2020-03-01-000000"


def write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size))
    return path


class BiggestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="timak-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        m = self.tmp / "Mac"
        for s in (S1, S2, S3):
            (m / s / "HD").mkdir(parents=True)
            (m / s / INFO_NAME).write_text(json.dumps({"stats": {"linked": 2, "copied": 2}}))
        home = "HD/Users/alice"
        movie = write(m / S1 / home / "Movies/big.mov", 400_000)
        for s in (S2, S3):  # kept by every snapshot, as extract links it
            (m / s / home / "Movies").mkdir(parents=True)
            os.link(movie, m / s / home / "Movies/big.mov")
        # renamed in S3: same file, a second path
        (m / S3 / home / "Old").mkdir(parents=True)
        os.link(movie, m / S3 / home / "Old/renamed.mov")
        write(m / S2 / home / "Library/Teams/blob", 200_000)
        write(m / S2 / home / "Documents/a.txt", 10_000)
        write(self.tmp / "Mac" / f"{S3}.partial/HD/huge", 900_000)  # unfinished: ignored
        write(self.tmp / "prune.timakreport", 50_000)  # not in a snapshot: ignored
        self.home = home

    def test_files_once_with_links_and_snapshots(self):
        stats = Biggest(self.tmp, top=2).run()
        self.assertEqual(stats.snapshots, 3)
        self.assertEqual(stats.paths, 6)
        self.assertEqual(stats.files, 3)
        movie, blob = stats.files_top
        self.assertGreaterEqual(movie.size, 400_000)
        self.assertEqual(movie.links, 4)
        self.assertEqual(movie.where[("Mac", f"{self.home}/Movies/big.mov")], [S1, S2, S3])
        self.assertEqual(movie.where[("Mac", f"{self.home}/Old/renamed.mov")], [S3])
        self.assertEqual(blob.where, {("Mac", f"{self.home}/Library/Teams/blob"): [S2]})

    def test_trim_keeps_every_path_of_the_top(self):
        b = Biggest(self.tmp, top=1)
        b.run()
        self.assertEqual(len(b.stats.files_top[0].where), 2)
        # many small files seen first must not push the movie out
        for i in range(10):
            write(self.tmp / "Mac" / S1 / "HD" / f"small{i}", 1000 + i)
        self.assertEqual(Biggest(self.tmp, top=1).run().files_top[0].links, 4)

    def test_folders_counted_once_and_narrowed(self):
        stats = Biggest(self.tmp).run()
        folders = {(m, p): s for m, p, s in stats.folders_top}
        movies = folders[("Mac", f"{self.home}/Movies")]
        self.assertLess(movies, 500_000)  # three links, one file
        # HD/Users/alice holds the movie and the blob: no single subfolder has 90% of it
        self.assertIn(("Mac", self.home), folders)
        self.assertNotIn(("Mac", "HD/Users"), folders)  # all in alice: alice says it

    def test_suggested_rule(self):
        self.assertEqual(suggested_rule("HD/Users/alice/Library/Teams"), "- ~/Library/Teams")
        self.assertEqual(suggested_rule("HD/Users/Shared/Adobe"), "- /Users/Shared/Adobe")
        self.assertEqual(suggested_rule("External/VMs"), "- /VMs")
        self.assertEqual(suggested_rule("External"), '[volume_roots] "External" = []')
        self.assertEqual(suggested_rule("User/alice/Library/Developer", ["User"]), "- ~/Library/Developer")
        self.assertEqual(suggested_rule("User/alice/Library/Developer"), "- /alice/Library/Developer")

    def test_cli(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(["biggest", str(self.tmp), "--top", "5"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn(f"Mac: {self.home}/Movies/big.mov  (3 snapshots, {S1} … {S3})", text)
        self.assertIn('"- ~/Library/Teams/",', text)
        self.assertIn("6 file paths in 3 snapshots, 3 distinct files", text)
        self.assertIn("big.mov", (self.tmp / "biggest.timakreport").read_text())
        for root, report in ((self.tmp / "Mac", "biggest-Mac.timakreport"),
                             (self.tmp / "Mac" / S2, f"biggest-Mac-{S2}.timakreport")):  # fmt: skip
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                main(["biggest", str(root)])
            self.assertTrue((self.tmp / report).exists(), report)


if __name__ == "__main__":
    unittest.main()
