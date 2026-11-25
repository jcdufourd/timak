import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from timak.sources import apfs

LIST_SNAPSHOTS = {
    "Snapshots": [
        {"SnapshotName": "com.apple.TimeMachine.2021-01-19-115232.backup"},
        {"SnapshotName": "com.apple.TimeMachine.2021-01-10-195352.backup"},
        {"SnapshotName": "com.apple.os.update-ABC"},
    ]
}


class ApfsDiscoveryTest(unittest.TestCase):
    def test_tm_snapshots_filters_and_sorts(self):
        with mock.patch.object(apfs, "_plist", return_value=LIST_SNAPSHOTS):
            self.assertEqual(
                apfs.tm_snapshots("disk14s2"),
                [
                    ("2021-01-10-195352", "com.apple.TimeMachine.2021-01-10-195352.backup"),
                    ("2021-01-19-115232", "com.apple.TimeMachine.2021-01-19-115232.backup"),
                ],
            )

    def test_mounted_snapshots(self):
        out = (
            "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
            "/dev/disk10s2 on /Volumes/Storage (apfs, local, nodev, nosuid, journaled)\n"
            "com.apple.TimeMachine.2026-03-28-184309.backup@/dev/disk10s2 on "
            "/Volumes/.timemachine/D1CE/2026-03-28-184309.backup (apfs, local, nodev, nosuid, read-only, journaled, nobrowse)\n"
        )
        self.assertEqual(
            apfs.mounted_snapshots(out),
            {("disk10s2", "com.apple.TimeMachine.2026-03-28-184309.backup"):
             Path("/Volumes/.timemachine/D1CE/2026-03-28-184309.backup")},
        )  # fmt: skip

    def test_snapshots_reuses_a_mount_made_by_macos(self):
        names = [("2026-03-27-100000", "com.apple.TimeMachine.2026-03-27-100000.backup"),
                 ("2026-03-28-184309", "com.apple.TimeMachine.2026-03-28-184309.backup")]  # fmt: skip
        with tempfile.TemporaryDirectory() as tmp:
            theirs = Path(tmp) / "2026-03-28-184309.backup"
            (theirs / "2026-03-28-184309.backup" / "Data").mkdir(parents=True)
            mounted = []

            def fake_mount(device, name, mountpoint, stack):
                mounted.append(name)
                (mountpoint / "x.backup" / "Data").mkdir(parents=True)
                stack.callback(shutil.rmtree, mountpoint)

            with (
                mock.patch.object(apfs, "tm_snapshots", return_value=names),
                mock.patch.object(apfs, "mounted_snapshots", return_value={("disk10s2", names[1][1]): theirs}),
                mock.patch.object(apfs, "_mount", side_effect=fake_mount),
                mock.patch.object(apfs, "computer_name", return_value=None),
                apfs.ExitStack() as stack,
            ):
                snaps = apfs.snapshots(Path("/Volumes/Storage"), "disk10s2", "/Volumes/Storage", stack)
        self.assertEqual(mounted, [names[0][1]])  # only the snapshot macOS had not mounted
        self.assertEqual(snaps[1].path, theirs / "2026-03-28-184309.backup")

    def test_destination_device(self):
        info = {"FilesystemType": "apfs", "MountPoint": "/Volumes/Backup2021", "DeviceIdentifier": "disk14s2"}
        with mock.patch.object(apfs, "_plist", side_effect=[info, LIST_SNAPSHOTS]):
            self.assertEqual(apfs.destination_device(Path("/Volumes/Backup2021")), "disk14s2")
        with mock.patch.object(apfs, "_plist", side_effect=[dict(info, FilesystemType="hfs")]):
            self.assertIsNone(apfs.destination_device(Path("/Volumes/Backup2021")))
        # a folder inside a volume is not the destination itself
        with mock.patch.object(apfs, "_plist", side_effect=[info]):
            self.assertIsNone(apfs.destination_device(Path("/Volumes/Backup2021/sub")))

    def test_localized_data_volume_names(self):
        from timak.sources.base import normalize_volume_name

        self.assertEqual(normalize_volume_name("Macintosh HD - Data"), "Macintosh HD")
        self.assertEqual(normalize_volume_name("System - Données"), "System")
        self.assertEqual(normalize_volume_name("System - Données"), "System")  # decomposed accent
        self.assertEqual(normalize_volume_name("Photos"), "Photos")

    def test_computer_name(self):
        import plistlib

        from timak.sources.base import PREFERENCES, Snapshot, Volume, computer_name

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "System - Données"
            prefs = data / PREFERENCES
            prefs.parent.mkdir(parents=True)
            prefs.write_bytes(plistlib.dumps({"System": {"System": {"ComputerName": "minim4"}}}, fmt=plistlib.FMT_BINARY))
            snap = Snapshot("apfs", "Backup2021", "2021-05-16-120000", Path(tmp), (Volume("System", data, data.name),))
            self.assertEqual(computer_name(snap), "minim4")
            prefs.unlink()
            self.assertIsNone(computer_name(snap))

    def test_system_half_of_split_volume_is_not_reported(self):
        from timak.rules import Rules
        from timak.sources.base import Snapshot, unconfigured_volumes, volumes_in

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Macintosh HD/System").mkdir(parents=True)
            (root / "Macintosh HD - Data/Users/alice").mkdir(parents=True)
            (root / "Spare/files").mkdir(parents=True)
            snap = Snapshot("legacy", "Mac", "2020-01-01-000000", root, volumes_in(root))
            self.assertEqual(unconfigured_volumes([snap], Rules(["/Users"], [])), {"Spare": ["2020-01-01-000000"]})

    def test_volume_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2021-01-10-195352.backup/Macintosh HD - Data").mkdir(parents=True)
            self.assertEqual(apfs._volume_parent(root).name, "2021-01-10-195352.backup")


if __name__ == "__main__":
    unittest.main()
