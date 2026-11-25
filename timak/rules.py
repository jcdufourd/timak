"""Keep/drop rules: which paths of a backed-up volume end up in the archive.

A rules file is TOML:

    roots = ["/Users"]          # only these trees are considered at all
    case_sensitive = false      # HFS+/APFS volumes are usually case-insensitive
    rules = [
        "- ~/Library/Caches/",  # first matching rule wins
        "+ ~/Library/Mail/",
        "- node_modules/",
    ]

    home_volumes = ["User"]     # optional: volumes holding home folders at their root

    [volume_roots]              # optional: other roots for some backed-up volumes
    Large = ["/"]               # keep that whole volume (rules still apply)
    "Old Disk" = []             # never extract that volume

A volume is extracted only if one of its roots exists in it.

A volume listed in `home_volumes` was mounted as /Users (a separate disk for
the home folders, e.g. "User/alice/Library"): rules see its paths as if they were
under /Users ("Users/alice/Library"), so "~/" rules apply to it too. Its roots
are still written from the volume's own root.

Anything under a root that matches no rule is kept. A dropped directory is
pruned entirely: a "+" rule cannot rescue something below a dropped folder.

Patterns are matched against the path relative to the volume root
(e.g. "Users/alice/Library/Caches"):
  - a leading "/" anchors the pattern at the volume root,
    otherwise it may match at any depth (like rsync);
  - "~/" is shorthand for "/Users/*/";
  - a trailing "/" restricts the pattern to directories;
  - "*" and "?" do not cross "/", "**" does, "[abc]" is a character class.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
import unicodedata
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


class RulesError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    keep: bool
    pattern: str
    regex: re.Pattern
    dir_only: bool

    def __str__(self) -> str:
        return ("+ " if self.keep else "- ") + self.pattern


def _translate(pattern: str, case_sensitive: bool) -> tuple[re.Pattern, bool]:
    dir_only = pattern.endswith("/")
    p = pattern.rstrip("/")
    if p == "~":
        p = "/Users/*"
    elif p.startswith("~/"):
        p = "/Users/*/" + p[2:]
    anchored = p.startswith("/")
    p = p.lstrip("/")
    if not p:
        raise RulesError(f"empty pattern: {pattern!r}")

    out: list[str] = []
    i = 0
    while i < len(p):
        if p.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif p.startswith("**", i):
            out.append(".*")
            i += 2
        elif p[i] == "*":
            out.append("[^/]*")
            i += 1
        elif p[i] == "?":
            out.append("[^/]")
            i += 1
        elif p[i] == "[" and (end := p.find("]", i + 2)) != -1:
            body = p[i + 1 : end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = end + 1
        else:
            out.append(re.escape(p[i]))
            i += 1

    prefix = "" if anchored else "(?:.*/)?"
    flags = re.S if case_sensitive else re.S | re.I
    return re.compile(prefix + "".join(out) + r"\Z", flags), dir_only


def parse_rule(text: str, case_sensitive: bool = False) -> Rule:
    text = text.strip()
    if len(text) < 3 or text[0] not in "+-" or text[1] != " ":
        raise RulesError(f"rule must look like '+ pattern' or '- pattern': {text!r}")
    pattern = text[2:].strip()
    regex, dir_only = _translate(pattern, case_sensitive)
    return Rule(text[0] == "+", pattern, regex, dir_only)


def _check_roots(roots: list[str], where: str) -> list[str]:
    if not isinstance(roots, list):
        raise RulesError(f"{where} must be a list of paths")
    checked = []
    for root in roots:
        if isinstance(root, str) and root[:2] in ("+ ", "- ") and where.startswith("volume_roots."):
            raise RulesError(
                f"{where} holds rules: keys written after the [volume_roots] header belong to that table, "
                "so put [volume_roots] at the end of the file, after `rules = [...]`"
            )
        if not isinstance(root, str) or not root.startswith("/") or any(c in root for c in "*?["):
            raise RulesError(f"{where}: roots must be literal absolute paths: {root!r}")
        checked.append("/" + root.strip("/"))
    return checked


def _nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name)


class Rules:
    def __init__(
        self,
        roots: list[str],
        rules: list[str],
        case_sensitive: bool = False,
        volume_roots: dict[str, list[str]] | None = None,
        home_volumes: list[str] | None = None,
    ):
        if not roots:
            raise RulesError("at least one root is required")
        self.roots = _check_roots(roots, "roots")
        # per backed-up volume, replacing `roots`; [] means "skip this volume"
        self.volume_roots = {
            _nfc(name): _check_roots(r, f"volume_roots.{name}") for name, r in (volume_roots or {}).items()
        }
        if not isinstance(home_volumes or [], list) or not all(isinstance(n, str) for n in home_volumes or []):
            raise RulesError("home_volumes must be a list of volume names")
        self.home_volumes = sorted({_nfc(n) for n in home_volumes or []})
        self.rule_texts = list(rules)
        self.case_sensitive = case_sensitive
        self.rules = [parse_rule(r, case_sensitive) for r in rules]

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Rules":
        if path is None:
            return cls.from_text(default_rules_text(), "default rules")
        return cls.from_text(Path(path).read_text(), str(path))

    @classmethod
    def from_text(cls, text: str, where: str = "rules") -> "Rules":
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise RulesError(f"{where}: {e}") from e
        if not isinstance(data.get("rules", []), list):
            raise RulesError(f"{where}: rules must be a list of strings")
        volume_roots = data.get("volume_roots", {})
        if misplaced := sorted({"roots", "rules", "case_sensitive", "home_volumes"} & set(volume_roots if isinstance(volume_roots, dict) else ())):
            raise RulesError(
                f"{where}: {', '.join(misplaced)} is inside [volume_roots]: keys written after the [volume_roots] "
                "header belong to that table, so put [volume_roots] at the end of the file"
            )
        return cls(
            roots=data.get("roots", ["/Users"]),
            rules=data.get("rules", []),
            case_sensitive=data.get("case_sensitive", False),
            volume_roots=data.get("volume_roots", {}),
            home_volumes=data.get("home_volumes", []),
        )

    @property
    def fingerprint(self) -> str:
        """Identifies the rule set; archives made with other rules are not reused."""
        parts = [self.roots, self.rule_texts, self.case_sensitive, sorted(self.volume_roots.items())]
        if self.home_volumes:  # absent, archives made before home_volumes existed keep their fingerprint
            parts.append(self.home_volumes)
        blob = json.dumps(parts)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def configures(self, volume) -> bool:
        """Whether [volume_roots] says something about this volume."""
        return any(name and _nfc(name) in self.volume_roots for name in (volume.name, volume.original_name))

    def roots_for(self, volume=None) -> list[str]:
        """Roots that apply to a backed-up volume (looked up by archive name, then original name)."""
        if volume is not None:
            for name in (volume.name, volume.original_name):
                if name and (roots := self.volume_roots.get(_nfc(name))) is not None:
                    return roots
        return self.roots

    def is_home_volume(self, volume) -> bool:
        return any(name and _nfc(name) in self.home_volumes for name in (volume.name, volume.original_name))

    def for_volume(self, volume) -> "Rules":
        """The rules as they apply inside `volume`: match() and keeps() take
        paths relative to that volume's root."""
        return _HomeVolumeRules(self) if volume is not None and self.is_home_volume(volume) else self

    def match(self, rel: str, is_dir: bool) -> Rule | None:
        """First rule matching `rel` (relative to the volume root), if any."""
        for rule in self.rules:
            if rule.dir_only and not is_dir:
                continue
            if rule.regex.match(rel):
                return rule
        return None

    def keeps(self, rel: str, is_dir: bool) -> bool:
        rule = self.match(rel, is_dir)
        return rule is None or rule.keep

    def root_rels(self, volume=None) -> list[str]:
        """Roots of `volume` relative to its root, outermost first, no duplicates."""
        rels = sorted({r.strip("/") for r in self.roots_for(volume)})
        if "" in rels:  # "/" covers everything
            return [""]
        # a root inside another root is already covered
        return [r for r in rels if not any(r.startswith(o + "/") for o in rels if o != r)]


class _HomeVolumeRules:
    """Rules for a volume mounted as /Users: its paths are matched under "Users/"."""

    def __init__(self, rules: Rules):
        self._rules = rules

    def match(self, rel: str, is_dir: bool) -> Rule | None:
        return self._rules.match(f"Users/{rel}" if rel else "Users", is_dir)

    def keeps(self, rel: str, is_dir: bool) -> bool:
        rule = self.match(rel, is_dir)
        return rule is None or rule.keep

    def __getattr__(self, name):
        return getattr(self._rules, name)


def join(parent: str, name: str) -> str:
    """Join relative paths where "" is the volume root."""
    return f"{parent}/{name}" if parent else name


def default_rules_text() -> str:
    return resources.files("timak").joinpath("default_rules.toml").read_text()
