import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from timak.archive import ArchivedSnapshot
from timak.cli import main
from timak.prune import OUTSIDE_ROOTS, Pruner, prune
from timak.rules import Rules
from timak.util import INFO_NAME

OLD = """
roots = ["/Users"]
rules = ["- ~/Library/Caches/"]
"""

NEW = """
roots = ["/Users"]
rules = [
    "+ ~/Library/Mail/",
    "- ~/Library/*/",
    "- .DS_Store",
]
"""

S1, S2 = "2020-01-01-000000", "2020-02-01-000000"


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def run(*argv) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main([str(a) for a in argv])
    return code, out.getvalue()


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.archive = self.tmp / "archive"
        self.old = Rules.from_text(OLD)
        self.new = Rules.from_text(NEW)
        self.new_file = self.tmp / "new.toml"
        self.new_file.write_text(NEW)
        m = self.archive / "Mac"
        for stamp in (S1, S2):
            home = m / stamp / "Macintosh HD" / "Users" / "alice"
            home.mkdir(parents=True)
            (m / stamp / INFO_NAME).write_text(json.dumps({
                "rules": self.old.fingerprint, "rule_list": self.old.rule_texts,
                "volumes": {"Macintosh HD": "Macintosh HD"}, "stats": {},
            }))  # fmt: skip
        h1 = m / S1 / "Macintosh HD" / "Users" / "alice"
        h2 = m / S2 / "Macintosh HD" / "Users" / "alice"
        write(h1 / "Documents/a.txt", b"a" * 5000)
        write(h1 / "Library/Mail/V8/m.emlx", b"m" * 5000)
        write(h1 / "Library/Application Support/Teams/blob", b"t" * 50000)
        write(h1 / "Library/Containers/x/data", b"x" * 5000)  # only in S1
        write(h1 / ".DS_Store", b"d")
        write(m / S1 / "Macintosh HD/Applications/stray", b"s" * 5000)  # outside the roots
        # S2 shares the unchanged files with S1 through hard links, as extract makes it
        for rel in ("Documents/a.txt", "Library/Mail/V8/m.emlx", "Library/Application Support/Teams/blob"):
            (h2 / rel).parent.mkdir(parents=True, exist_ok=True)
            os.link(h1 / rel, h2 / rel)
        write(h2 / "Library/Application Support/Teams/new", b"n" * 5000)  # only in S2
        self.h1, self.h2 = h1, h2

    def test_dry_run_changes_nothing(self):
        stats = prune(self.archive, self.new)
        self.assertTrue((self.h1 / "Library/Application Support/Teams/blob").exists())
        self.assertEqual(json.loads((self.archive / "Mac" / S1 / INFO_NAME).read_text())["rules"], self.old.fingerprint)
        self.assertEqual(stats.pruned, 2)
        self.assertIn("- ~/Library/*/", stats.by_rule)
        self.assertIn(OUTSIDE_ROOTS, stats.by_rule)
        # blob is shared by both snapshots: counted once, freed because both drop it
        blob = os.lstat(self.h1 / "Library/Application Support/Teams/blob").st_blocks * 512
        self.assertGreaterEqual(stats.freed, blob)

    def test_apply(self):
        stats = prune(self.archive, self.new, apply=True)
        self.assertEqual(stats.errors, [])
        for h in (self.h1, self.h2):
            self.assertTrue((h / "Documents/a.txt").exists())
            self.assertTrue((h / "Library/Mail/V8/m.emlx").exists())
            self.assertFalse((h / "Library/Application Support").exists())
        self.assertFalse((self.h1 / "Library/Containers").exists())
        self.assertFalse((self.h1 / ".DS_Store").exists())
        self.assertFalse((self.archive / "Mac" / S1 / "Macintosh HD/Applications").exists())
        self.assertTrue((self.archive / "Mac" / S1 / "Macintosh HD/Users").is_dir())  # on the way to the root
        info = json.loads((self.archive / "Mac" / S1 / INFO_NAME).read_text())
        self.assertEqual(info["rules"], self.new.fingerprint)
        self.assertEqual(info["rule_list"], self.new.rule_texts)
        self.assertEqual(info["pruned"][0]["previous_rules"], self.old.fingerprint)
        self.assertEqual(info["stats"], {})  # the rest is kept

        again = prune(self.archive, self.new, apply=True)
        self.assertEqual((again.up_to_date, again.entries), (2, 0))

    def test_shared_file_kept_by_one_snapshot_frees_nothing(self):
        # a rule that drops Teams/blob in S1 only: S2 still links it, so no space comes back
        rules = Rules.from_text('roots = ["/"]\nrules = ["- /Users/alice/Library/Application Support/Teams/blob"]')
        p = Pruner(self.archive, rules)
        p.prune_snapshot(ArchivedSnapshot(S1, self.archive / "Mac" / S1, {}), "S1")
        self.assertEqual(p.stats.entries, 1)
        self.assertEqual(p.stats.freed, 0)

    def test_skips_machine_with_partial(self):
        (self.archive / "Mac" / "2020-03-01-000000.partial").mkdir()
        stats = prune(self.archive, self.new, apply=True)
        self.assertEqual(stats.skipped_machines, ["Mac"])
        self.assertTrue((self.h1 / "Library/Application Support").exists())

    def test_cli(self):
        code, out = run("prune", self.archive, "--rules", self.new_file)
        self.assertEqual(code, 0)
        self.assertIn("dry run, nothing removed", out)
        self.assertIn("--apply", out)
        self.assertTrue((self.h1 / "Library/Application Support").exists())
        # the paths, once each with the snapshots they are in, on screen and in the report
        line = "Macintosh HD/Users/alice/Library/Application Support/  (2 snapshots, 2020-01-01-000000 … 2020-02-01-000000)"
        self.assertIn(line, out)
        self.assertIn("Macintosh HD/Users/alice/Library/Containers/  (2020-01-01-000000)", out)
        self.assertIn("Mac: outside the roots", out)
        report = (self.archive / "prune-dry-run.timakreport").read_text()
        self.assertIn(line, report)
        self.assertIn("Macintosh HD/Applications/", report)
        code, out = run("prune", self.archive, "--rules", self.new_file, "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("done: freed", out)
        self.assertFalse((self.h1 / "Library/Application Support").exists())
        code, _ = run("prune", self.archive, "--rules", self.new_file, "--machine", "Nope")
        self.assertEqual(code, 2)

    def test_home_volume(self):
        # a separate disk mounted as /Users: the ~/ rules apply to it
        user = self.archive / "Mac" / S1 / "User" / "alice"
        write(user / "Library/Teams/blob", b"t" * 5000)
        write(user / "Library/Mail/m.emlx", b"m" * 5000)
        info = self.archive / "Mac" / S1 / INFO_NAME
        data = json.loads(info.read_text())
        data["volumes"]["User"] = "User"
        info.write_text(json.dumps(data))
        text = NEW + 'home_volumes = ["User"]\n[volume_roots]\nUser = ["/"]\n'
        prune(self.archive, Rules.from_text(text), apply=True)
        self.assertFalse((user / "Library/Teams").exists())
        self.assertTrue((user / "Library/Mail/m.emlx").exists())


if __name__ == "__main__":
    unittest.main()
