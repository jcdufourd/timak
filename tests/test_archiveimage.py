import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from timak import archiveimage
from timak.archiveimage import find_bundle, fs_type, open_archive, wants_bundle

from fixtures import RULES, build_backupdb
from test_pipeline import HAVE_RSYNC, chmod_writable, run


class FindBundleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="timak-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        (self.tmp / "tm.sparsebundle").mkdir()

    def test_found_by_archive_name_bundle_name_or_inside(self):
        bundle = self.tmp / "tm.sparsebundle"
        self.assertEqual(find_bundle(self.tmp / "tm"), (bundle, Path()))
        self.assertEqual(find_bundle(bundle), (bundle, Path()))
        self.assertEqual(find_bundle(self.tmp / "tm/Mac/2020-01-01-000000"), (bundle, Path("Mac/2020-01-01-000000")))

    def test_plain_folder_wins(self):
        (self.tmp / "tm").mkdir()
        self.assertIsNone(find_bundle(self.tmp / "tm"))
        self.assertIsNone(find_bundle(self.tmp / "other"))

    def test_fs_type_and_choice(self):
        self.assertEqual(fs_type(Path("/")), "apfs")
        self.assertEqual(fs_type(self.tmp / "not/yet"), fs_type(self.tmp))
        self.assertTrue(wants_bundle(self.tmp / "x.sparsebundle"))
        with mock.patch.dict(os.environ, {archiveimage.NO_IMAGE_ENV: ""}):
            self.assertEqual(wants_bundle(self.tmp / "x"), fs_type(self.tmp) == "apfs")
        with mock.patch.dict(os.environ, {archiveimage.NO_IMAGE_ENV: "1"}):
            self.assertFalse(wants_bundle(self.tmp / "x"))

    def test_plain_path_is_left_alone(self):
        with open_archive(self.tmp / "plain") as root:
            self.assertEqual(root, self.tmp / "plain")


SMALL = mock.Mock(total=200 * 2**20)  # a 200 MB bundle, not one as big as the test disk


@unittest.skipUnless(HAVE_RSYNC and shutil.which("diskutil"), "needs macOS and rsync 3.x")
class BundleArchiveTest(unittest.TestCase):
    """extract makes the bundle; the other commands find it by the archive's name."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="timak-test-"))
        self.src = build_backupdb(self.tmp / "src", dir_links=False)
        self.rules = self.tmp / "rules.toml"
        self.rules.write_text(RULES)
        self.env = mock.patch.dict(os.environ, {archiveimage.NO_IMAGE_ENV: ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def tearDown(self):
        info = subprocess.run(["hdiutil", "info"], capture_output=True, text=True).stdout
        self.assertNotIn(str(self.tmp), info)  # every command ejected it
        chmod_writable(self.tmp)
        shutil.rmtree(self.tmp)

    def test_extract_then_other_commands(self):
        archive = self.tmp / "tm"
        with mock.patch.object(archiveimage.shutil, "disk_usage", return_value=SMALL):
            code, _ = run("extract", self.src, "--archive", archive, "--rules", self.rules)
        self.assertEqual(code, 0)
        self.assertFalse(archive.exists())
        bundle = self.tmp / "tm.sparsebundle"
        self.assertTrue(bundle.is_dir())
        with open_archive(archive, readonly=True) as root:
            self.assertEqual(subprocess.run(["diskutil", "info", "-plist", str(root)], capture_output=True).returncode, 0)
            self.assertTrue((root / "Mac").is_dir())
            # case-sensitive HFS+
            self.assertEqual(fs_type(root), "hfs")
            self.assertTrue(any(p.name == "Mac" for p in root.iterdir()))
        code, out = run("du", archive)
        self.assertEqual(code, 0)
        self.assertIn("distinct files", out)
        code, out = run("kinds", archive / "Mac")
        self.assertEqual(code, 0)
        self.assertIn("documents", out)
        # a second extract goes into the same bundle
        code, out = run("extract", self.src, "--archive", bundle, "--rules", self.rules)
        self.assertEqual(code, 0)

    def test_case_sensitive(self):
        with mock.patch.object(archiveimage.shutil, "disk_usage", return_value=SMALL):
            with open_archive(self.tmp / "cs", create=True) as root:
                (root / "a").write_text("a")
                (root / "A").write_text("A")
                self.assertEqual(sorted(p.name for p in root.iterdir() if not p.name.startswith(".")), ["A", "a"])

    def test_folder_option(self):
        code, _ = run("extract", self.src, "--archive", self.tmp / "plain", "--rules",
                      self.rules, "--folder")  # fmt: skip
        self.assertEqual(code, 0)
        self.assertTrue((self.tmp / "plain/Mac").is_dir())
        self.assertFalse((self.tmp / "plain.sparsebundle").exists())


if __name__ == "__main__":
    unittest.main()
