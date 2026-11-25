import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from timak import diskimage
from timak.diskimage import attach_command, attached

from fixtures import hfs_image


class CommandTest(unittest.TestCase):
    def test_diskutil_image_when_present(self):
        with mock.patch.object(diskimage, "has_diskutil_image", return_value=True):
            self.assertEqual(attach_command(Path("/x.dmg"))[:3], ["diskutil", "image", "attach"])
            self.assertIn("--readOnly", attach_command(Path("/x.dmg")))

    def test_hdiutil_on_older_macos(self):
        with mock.patch.object(diskimage, "has_diskutil_image", return_value=False):
            cmd = attach_command(Path("/x.dmg"))
            self.assertEqual(cmd[:2], ["hdiutil", "attach"])
            self.assertIn("-readonly", cmd)


@unittest.skipUnless(shutil.which("diskutil") and shutil.which("hdiutil"), "needs macOS")
class AttachTest(unittest.TestCase):
    """A real image, attached read-only and detached again, each way."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="timak-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.image = self.tmp / "img.dmg"
        hfs_image(self.image, "10m", "TimakAttach")

    def check(self, modern: bool):
        if modern and not diskimage.has_diskutil_image():
            self.skipTest("this macOS has no diskutil image")
        with mock.patch.object(diskimage, "has_diskutil_image", return_value=modern):
            with attached(self.image) as mounts:
                self.assertEqual(len(mounts), 1)
                with self.assertRaises(OSError):  # read-only
                    (mounts[0] / "x").write_text("x")
        info = subprocess.run(["hdiutil", "info"], capture_output=True, text=True).stdout
        self.assertNotIn(str(self.image), info)

    def test_diskutil_image(self):
        self.check(True)

    def test_hdiutil_fallback(self):
        self.check(False)


if __name__ == "__main__":
    unittest.main()
