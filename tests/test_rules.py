import hashlib
import json
import unittest

from timak.rules import Rules, RulesError


class RulesTest(unittest.TestCase):
    def rules(self, *rules, roots=("/Users",), case_sensitive=False):
        return Rules(list(roots), list(rules), case_sensitive)

    def test_unanchored_matches_any_depth(self):
        r = self.rules("- node_modules/")
        self.assertFalse(r.keeps("Users/alice/proj/node_modules", True))
        self.assertFalse(r.keeps("node_modules", True))
        self.assertTrue(r.keeps("Users/alice/proj/node_modules_x", True))

    def test_dir_only(self):
        r = self.rules("- cache/")
        self.assertFalse(r.keeps("Users/alice/cache", True))
        self.assertTrue(r.keeps("Users/alice/cache", False))

    def test_anchored_and_tilde(self):
        r = self.rules("- ~/Library/Caches/", "- /Users/Shared/")
        self.assertFalse(r.keeps("Users/alice/Library/Caches", True))
        self.assertTrue(r.keeps("Users/alice/x/Library/Caches", True))
        self.assertFalse(r.keeps("Users/Shared", True))
        self.assertTrue(r.keeps("Users/alice/Users/Shared", True))

    def test_star_does_not_cross_slash_but_doublestar_does(self):
        r = self.rules("- /Users/*/tmp", "- /Users/**/deep")
        self.assertFalse(r.keeps("Users/alice/tmp", False))
        self.assertTrue(r.keeps("Users/alice/a/tmp", False))
        self.assertFalse(r.keeps("Users/alice/a/b/deep", False))
        self.assertFalse(r.keeps("Users/deep", False))

    def test_first_match_wins(self):
        r = self.rules("+ ~/Library/Mail/", "- ~/Library/*/")
        self.assertTrue(r.keeps("Users/alice/Library/Mail", True))
        self.assertFalse(r.keeps("Users/alice/Library/Caches", True))

    def test_case_insensitive_by_default(self):
        self.assertFalse(self.rules("- .ds_store").keeps("Users/alice/.DS_Store", False))
        self.assertTrue(self.rules("- .ds_store", case_sensitive=True).keeps("Users/alice/.DS_Store", False))

    def test_wildcards_and_classes(self):
        r = self.rules("- *.tm[p]", "- ?.log")
        self.assertFalse(r.keeps("Users/a/x.tmp", False))
        self.assertFalse(r.keeps("Users/a/1.log", False))
        self.assertTrue(r.keeps("Users/a/12.log", False))

    def test_regex_metacharacters_are_literal(self):
        r = self.rules("- a+b (1).txt")
        self.assertFalse(r.keeps("Users/a+b (1).txt", False))
        self.assertTrue(r.keeps("Users/aab (1).txt", False))

    def test_roots(self):
        self.assertEqual(self.rules(roots=["/Users/alice", "/Users", "/opt/data"]).root_rels(), ["Users", "opt/data"])
        self.assertEqual(self.rules(roots=["/", "/Users"]).root_rels(), [""])
        with self.assertRaises(RulesError):
            self.rules(roots=["Users"])
        with self.assertRaises(RulesError):
            self.rules("node_modules")

    def test_volume_roots(self):
        from types import SimpleNamespace as V

        r = Rules(["/Users"], [], volume_roots={"Large": ["/"], "Old": [], "Système": ["/Data"]})
        self.assertEqual(r.root_rels(V(name="Large", original_name="Large")), [""])
        self.assertEqual(r.root_rels(V(name="Old", original_name="Old")), [])
        self.assertEqual(r.root_rels(V(name="System", original_name="Macintosh HD - Data")), ["Users"])
        self.assertEqual(r.root_rels(V(name="Système", original_name="")), ["Data"])  # decomposed accent
        self.assertEqual(r.root_rels(), ["Users"])
        self.assertNotEqual(r.fingerprint, Rules(["/Users"], []).fingerprint)
        with self.assertRaises(RulesError):
            Rules(["/Users"], [], volume_roots={"Large": "/"})

    def test_volume_roots_from_toml(self):
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('roots = ["/Users"]\nrules = []\n[volume_roots]\nLarge = ["/"]\n"Old Disk" = []\n')
        try:
            r = Rules.load(f.name)
        finally:
            Path(f.name).unlink()
        self.assertEqual(r.volume_roots, {"Large": ["/"], "Old Disk": []})

    def test_default_rules_load(self):
        r = Rules.load()
        self.assertEqual(r.roots, ["/Users"])
        self.assertFalse(r.keeps("Users/alice/Library/Caches", True))
        self.assertTrue(r.keeps("Users/alice/Documents", True))
        self.assertTrue(r.keeps("Users/alice/Library/Mail", True))
        self.assertEqual(r.fingerprint, Rules.load().fingerprint)

    def test_keys_after_volume_roots_header(self):
        # TOML puts every key after [volume_roots] inside that table
        for text in ('[volume_roots]\nLarge = ["/"]\nroots = ["/Users"]', '[volume_roots]\nrules = ["- x/"]'):
            with self.assertRaisesRegex(RulesError, "at the end of the file"):
                Rules.from_text(text)
        self.assertEqual(Rules.from_text('rules = []\n[volume_roots]\nLarge = ["/"]').volume_roots, {"Large": ["/"]})


if __name__ == "__main__":
    unittest.main()


class HomeVolumesTest(unittest.TestCase):
    TEXT = """
roots = ["/Users"]
rules = ["+ ~/Library/Mail/", "- ~/Library/*/", "- /Users/Shared/Adobe/"]
home_volumes = ["User"]

[volume_roots]
User = ["/"]
"""

    def test_home_volume_paths_match_as_under_users(self):
        from types import SimpleNamespace

        rules = Rules.from_text(self.TEXT)
        user = rules.for_volume(SimpleNamespace(name="User", original_name=None))
        other = rules.for_volume(SimpleNamespace(name="Data", original_name="User"))  # by original name too
        plain = rules.for_volume(SimpleNamespace(name="Large", original_name=None))
        self.assertEqual(str(user.match("alice/Library/Caches", True)), "- ~/Library/*/")
        self.assertTrue(user.keeps("alice/Library/Mail", True))
        self.assertFalse(user.keeps("Shared/Adobe", True))
        self.assertTrue(user.keeps("", True) and user.keeps("alice", True))
        self.assertFalse(other.keeps("alice/Library/Caches", True))
        self.assertTrue(plain.keeps("alice/Library/Caches", True))
        self.assertIs(plain, rules)
        self.assertEqual(user.root_rels(), rules.root_rels())  # everything else is the rules'

    def test_fingerprint(self):
        without = self.TEXT.replace('home_volumes = ["User"]\n', "")
        self.assertNotEqual(Rules.from_text(self.TEXT).fingerprint, Rules.from_text(without).fingerprint)
        # rules without home_volumes keep the fingerprint they had before it existed
        old = Rules.from_text(without)
        blob = json.dumps([old.roots, old.rule_texts, old.case_sensitive, sorted(old.volume_roots.items())])
        self.assertEqual(old.fingerprint, hashlib.sha256(blob.encode()).hexdigest()[:16])

    def test_misplaced_or_wrong(self):
        with self.assertRaises(RulesError):
            Rules.from_text('rules = []\n[volume_roots]\nhome_volumes = ["User"]\n')
        with self.assertRaises(RulesError):
            Rules.from_text('home_volumes = "User"\n')
