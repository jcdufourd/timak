import unittest
from unittest import mock

from timak.util import HFS_MAX, HFS_MIN, Progress, duration, human, report_name, same_mtime

S = 1_000_000_000


class ProgressTest(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(duration(10), "under a minute")
        self.assertEqual(duration(25 * 60), "25 min")
        self.assertEqual(duration(11 * 3600 + 5 * 60), "11 h 05 min")
        self.assertEqual(duration(50 * 3600), "2.1 days")

    def test_time_left_from_pace_so_far(self):
        with mock.patch("timak.util.time.monotonic", return_value=0.0):
            p = Progress(22_000_000)
        with mock.patch("timak.util.time.monotonic", return_value=6 * 3600.0):
            self.assertEqual(p.text(8_000_000, "files"), "8000000 of ~22000000 files (36%, about 10 h 30 min left)")
            self.assertEqual(p.text(30_000_000, "files"), "30000000 files")  # the estimate was low
        with mock.patch("timak.util.time.monotonic", return_value=5.0):
            self.assertEqual(p.text(10), "10 of ~22000000 (0%)")  # too early to tell
        self.assertEqual(Progress(None, human).text(2000, "read"), "2.0 KB read")


class SameMtimeTest(unittest.TestCase):
    def test_exact_and_hfs_whole_seconds(self):
        self.assertTrue(same_mtime(1_600_000_000 * S + 5, 1_600_000_000 * S + 5))
        self.assertTrue(same_mtime(1_600_000_000 * S, 1_600_000_000 * S + 123))  # HFS+ dropped the fraction
        self.assertFalse(same_mtime(1_600_000_000 * S, 1_600_000_001 * S + 1))

    def test_dates_hfs_cannot_store_become_1970(self):
        y2098 = 4_039_369_200 * S  # 2098-01-01, as on photos in a 2021 backup
        self.assertTrue(same_mtime(0, y2098))
        self.assertTrue(same_mtime(0, (HFS_MIN - 1) * S))
        self.assertFalse(same_mtime(0, HFS_MAX * S))  # 2040 fits: a 1970 copy is wrong
        self.assertFalse(same_mtime(0, 1_600_000_000 * S))
        self.assertFalse(same_mtime(1_600_000_000 * S, y2098))


class ReportNameTest(unittest.TestCase):
    def test_named_after_the_sources(self):
        self.assertEqual(report_name("verify", ["/Volumes/External/oldbackups/Backup2021.dmg"]), "verify-Backup2021.timakreport")
        self.assertEqual(report_name("verify", ["/Volumes/TMDisk/"]), "verify-TMDisk.timakreport")
        self.assertEqual(report_name("verify", ["/Volumes/Old/Backups.backupdb"]), "verify-Old.timakreport")
        self.assertEqual(report_name("verify", ["/a/Backup2020.dmg", "/b/Backup2021.sparsebundle"]),
                         "verify-Backup2020+Backup2021.timakreport")  # fmt: skip
        self.assertEqual(report_name("verify", [f"/x/T{i}.dmg" for i in range(5)]), "verify-T0+T1+T2+2-more.timakreport")


if __name__ == "__main__":
    unittest.main()
