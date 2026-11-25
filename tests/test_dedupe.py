import errno
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from timak import dedupe as dedupe_mod
from timak.compare import compare

from fixtures import T0, write


class LinkedSnapshotsTest(unittest.TestCase):
    """Only one path per inode is kept while indexing: the others are found again."""

    def test_every_path_of_a_duplicate_is_relinked(self):
        tmp = Path(tempfile.mkdtemp(prefix="timak-test-"))
        self.addCleanup(shutil.rmtree, tmp)
        snaps = [tmp / "Mac" / f"2020-0{i}-01-000000" for i in (1, 2, 3)]
        first = snaps[0] / "Data/Users/alice"
        write(first / "Documents/report.txt", "same text")
        write(first / "Old/report copy.txt", "same text")
        for p in first.rglob("*.txt"):
            os.utime(p, (T0, T0))
        for snap in snaps[1:]:  # what extract does: unchanged files linked to the last snapshot
            for p in first.rglob("*.txt"):
                q = snap / p.relative_to(snaps[0])
                q.parent.mkdir(parents=True, exist_ok=True)
                os.link(p, q)
        hidden = tmp / ".Trashes/report.txt"  # skipped by dedupe: makes "report copy" the most linked
        hidden.parent.mkdir()
        os.link(first / "Old/report copy.txt", hidden)
        kept = os.lstat(hidden).st_ino

        stats = dedupe_mod.dedupe(tmp)

        paths = [s / "Data/Users/alice" / rel for s in snaps for rel in ("Documents/report.txt", "Old/report copy.txt")]
        self.assertEqual({os.lstat(p).st_ino for p in paths}, {kept})
        self.assertEqual(os.lstat(hidden).st_nlink, 7)
        self.assertEqual((stats.merged, stats.errors), (1, 0))  # all 3 links of "report" were replaced


class AppBundleTest(unittest.TestCase):
    """macOS refuses to modify files inside app bundles (App Management): not an error."""

    def test_protected_app_files_are_counted_not_errors(self):
        tmp = Path(tempfile.mkdtemp(prefix="timak-test-"))
        self.addCleanup(shutil.rmtree, tmp)
        for snap in ("2020-01-01-000000", "2020-02-01-000000"):
            write(tmp / f"Mac/{snap}/Data/Users/alice/Tool.app/Contents/PkgInfo", "APPL????")
            write(tmp / f"Mac/{snap}/Data/Users/alice/notes.txt", "same notes")
        os.utime(tmp / "Mac/2020-01-01-000000/Data/Users/alice/notes.txt", (1, 1))
        os.utime(tmp / "Mac/2020-02-01-000000/Data/Users/alice/notes.txt", (1, 1))
        for snap in ("2020-01-01-000000", "2020-02-01-000000"):
            os.utime(tmp / f"Mac/{snap}/Data/Users/alice/Tool.app/Contents/PkgInfo", (1, 1))

        real = dedupe_mod._replace_with_link

        def refuse_in_apps(target, path):
            if ".app/" in path:
                raise PermissionError(errno.EPERM, "Operation not permitted", path)
            real(target, path)

        with mock.patch.object(dedupe_mod, "_replace_with_link", refuse_in_apps):
            stats = dedupe_mod.dedupe(tmp)
        self.assertEqual(stats.errors, 0)
        self.assertEqual(stats.in_apps, 1)
        self.assertEqual(stats.merged, 1)  # notes.txt
        self.assertIn("App Management", stats.summary())


class ProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="timak-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_expected_files_from_snapshot_info(self):
        for snap, n in (("2020-01-01-000000", 10), ("2020-02-01-000000", 15)):
            write(self.tmp / f"Mac/{snap}/.timak.json", json.dumps({"stats": {"linked": n, "copied": 2}}))
        write(self.tmp / "Mac/2020-03-01-000000.partial/.timak.json", json.dumps({"stats": {"copied": 99}}))
        self.assertEqual(dedupe_mod.expected_files(self.tmp), 29)
        self.assertEqual(dedupe_mod.expected_files(self.tmp / "Mac"), 29)
        self.assertEqual(dedupe_mod.expected_files(self.tmp / "Mac/2020-01-01-000000"), 12)
        self.assertIsNone(dedupe_mod.expected_files(self.tmp / "Mac/2020-03-01-000000.partial/x"))

    def test_compare_progress_adds_up_to_the_plan(self):
        big, other = os.urandom(200_000), bytearray(os.urandom(200_000))
        other[:1000] = big[:1000]  # same head as big for the first KB only
        for i, data in enumerate((big, big, bytes(other), b"s" * 5000, b"s" * 5000, b"t" * 5000)):
            p = self.tmp / f"f{i}"
            p.write_bytes(data)
            os.utime(p, (1, 1))
        stats = dedupe_mod.DedupeStats()
        by_size = dedupe_mod._collect(self.tmp, 1, stats)
        done = []
        plan = 0
        for size, inodes in by_size.items():
            for c in dedupe_mod._candidates(list(inodes.values()), False):
                plan += len(c) * dedupe_mod._read_cost(size)
            dedupe_mod._groups(list(inodes.values()), False, stats, advance=done.append)
        self.assertEqual(plan, 3 * (200_000 + dedupe_mod.HEAD) + 3 * 5000)
        self.assertEqual(sum(done), plan)


class CompareTest(unittest.TestCase):
    """What merging two archives would take: content found in both, counted once."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="timak-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.a, self.b = self.tmp / "A", self.tmp / "B"
        photo = os.urandom(200_000)  # over 64 KB: compared by head, then whole
        for root, rel in ((self.a, "Mac/2020-01-01-000000/Data/Users/alice/Pictures/photo.jpg"),
                          (self.b, "Mac/2021-05-01-000000/Data/Users/alice/Old/renamed.jpg")):  # fmt: skip
            p = root / rel
            p.parent.mkdir(parents=True)
            p.write_bytes(photo)
            os.utime(p, (T0, T0))
        # a duplicate inside A that a dedupe of A alone would merge
        write(self.a / "Mac/2020-01-01-000000/Data/Users/alice/a.txt", "x" * 5000)
        write(self.a / "Mac/2020-01-01-000000/Data/Users/alice/copy-of-a.txt", "x" * 5000)
        # already a hard link in A: counted once
        os.link(self.a / "Mac/2020-01-01-000000/Data/Users/alice/a.txt", self.a / "Mac/2020-01-01-000000/Data/Users/alice/link.txt")
        # only in B, and same content as A but another mtime
        write(self.b / "Mac/2021-05-01-000000/Data/Users/alice/only-b.txt", "unique to B")
        write(self.b / "Mac/2021-05-01-000000/Data/Users/alice/a.txt", "x" * 5000, mtime=T0 + 3600)
        # a snapshot in both, an unfinished one, and a disk's hidden folder that is not part of the archive
        write(self.b / "Mac/2020-01-01-000000/Data/Users/alice/z.txt", "z")
        write(self.b / "Mac/2022-01-01-000000.partial/Data/x", "partial")
        write(self.b / ".Spotlight-V100/store", "y" * 9000)

    def blocks(self, *rels):
        return sum(os.lstat(p).st_blocks * 512 for p in rels)

    def test_shared_content_and_merged_size(self):
        stats = compare(self.a, self.b)
        self.assertEqual(stats.errors, 0)
        self.assertEqual(stats.files, [4, 4])  # photo, a, copy, link / renamed, only-b, a, z
        self.assertEqual(stats.inodes_by_side, [3, 4])
        photo_b = self.blocks(self.b / "Mac/2021-05-01-000000/Data/Users/alice/Old/renamed.jpg")
        self.assertEqual(stats.shared, photo_b)
        self.assertEqual(stats.shared_files, 1)  # a.txt in B has another mtime
        self.assertEqual(stats.saved_within[0], self.blocks(self.a / "Mac/2020-01-01-000000/Data/Users/alice/copy-of-a.txt"))
        self.assertEqual(stats.saved_within[1], 0)
        self.assertEqual(stats.merged, sum(stats.size) - stats.shared - stats.saved_within[0])
        self.assertEqual(stats.common_snapshots, ["Mac/2020-01-01-000000"])
        text = stats.summary(self.a, self.b)
        self.assertIn("after dedupe", text)
        self.assertIn("1 snapshots are in both", text)

    def test_loose_ignores_metadata(self):
        stats = compare(self.a, self.b, loose=True)
        self.assertEqual(stats.shared_files, 2)  # the photo and a.txt

    def test_nothing_is_changed(self):
        before = sorted((str(p), os.lstat(p).st_ino) for p in self.tmp.rglob("*"))
        compare(self.a, self.b)
        self.assertEqual(before, sorted((str(p), os.lstat(p).st_ino) for p in self.tmp.rglob("*")))

    def test_missing_archive(self):
        with self.assertRaises(FileNotFoundError):
            compare(self.a, self.tmp / "nope")
