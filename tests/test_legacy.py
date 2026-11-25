"""Archives made before the rename to timak keep a .timac.json in each snapshot: they still work."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from timak.kinds import kinds
from timak.util import INFO_NAME, LEGACY_INFO_NAME

from fixtures import MACHINE, RULES, S1, S2, build_backupdb
from test_pipeline import HAVE_RSYNC, chmod_writable, run


@unittest.skipUnless(HAVE_RSYNC, "rsync 3.x not installed")
class LegacyArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="timak-test-"))
        self.src = build_backupdb(self.tmp / "src", dir_links=False)
        self.rules = self.tmp / "rules.toml"
        self.rules.write_text(RULES)
        self.archive = self.tmp / "archive"
        # an archive of the first snapshot, as timac wrote it
        code, out = run("extract", self.src, "--archive", self.archive, "--rules", self.rules, "--until", S1)
        self.assertEqual(code, 0, out)
        s1 = self.archive / MACHINE / S1
        (s1 / INFO_NAME).rename(s1 / LEGACY_INFO_NAME)

    def tearDown(self):
        chmod_writable(self.tmp)
        shutil.rmtree(self.tmp)

    def test_next_extract_links_to_the_old_snapshot(self):
        code, out = run("extract", self.src, "--archive", self.archive, "--rules", self.rules)
        self.assertEqual(code, 0, out)
        info = json.loads((self.archive / MACHINE / S2 / INFO_NAME).read_text())
        self.assertEqual(info["previous"], S1)
        self.assertGreater(info["stats"]["linked"], 0)
        self.assertTrue((self.archive / MACHINE / S1 / LEGACY_INFO_NAME).exists())  # left as it was

    def test_snapshot_found_by_walks_and_migrated_by_prune(self):
        self.assertEqual(kinds(self.archive).snapshots, 1)
        code, out = run("biggest", self.archive / MACHINE / S1)
        self.assertEqual(code, 0, out)
        self.assertTrue((self.archive / f"biggest-{MACHINE}-{S1}.timakreport").exists())
        code, out = run("dedupe", self.archive, "--dry-run")
        self.assertEqual(code, 0, out)
        stricter = self.tmp / "stricter.toml"
        stricter.write_text(RULES.replace('rules = [', 'rules = ["- ~/Pictures/", '))
        code, out = run("prune", self.archive, "--rules", stricter, "--apply")
        self.assertEqual(code, 0, out)
        s1 = self.archive / MACHINE / S1
        self.assertTrue((s1 / INFO_NAME).exists())
        self.assertFalse((s1 / LEGACY_INFO_NAME).exists())
        self.assertIn("pruned", json.loads((s1 / INFO_NAME).read_text()))


if __name__ == "__main__":
    unittest.main()
