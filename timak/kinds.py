"""How the space of an archive splits between mail, documents, images, videos…

Each file is counted once, however many snapshots hard-link it, in the kind
of the first path it was found under (like `biggest`). The kind comes from
where the file is first (a mail folder, a Photos library, a virtual machine,
a git repository), then from its extension. The extensions found in each
kind are listed too, so that "other" can be read and the tables extended.
"""

from __future__ import annotations

import stat
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .biggest import ArchiveWalk
from .util import human

# Kinds decided by where the file is, tested in this order on the lowercased
# volume/path/name. A tuple of (substrings, folder-name suffixes).
BY_PLACE: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    ("mail", ("/library/mail/", "/library/mail downloads/", "/library/containers/com.apple.mail/",
              "/thunderbird/profiles/", "/library/thunderbird/"), (".mbox",)),
    ("messages", ("/library/messages/",), ()),
    ("photos library", (), (".photoslibrary", ".aplibrary", "iphoto library", ".migratedphotolibrary")),
    ("music & podcasts library", ("/music/itunes/", "/music/music/", "/library/group containers/243lu875e5.groups.com.apple.podcasts/"), ()),
    ("virtual machines", ("/virtualbox vms/", "/.android/avd/", "/library/containers/com.utmapp.utm/"),
     (".vmwarevm", ".pvm", ".utm")),
    ("disk images & archives", (), (".sparsebundle",)),
    ("code", ("/node_modules/", "/.git/"), ()),
    ("applications", (), (".app",)),
]  # fmt: skip

BY_EXTENSION: dict[str, set[str]] = {
    "mail": {"eml", "emlx", "mbox", "mbx", "msg", "pst", "ost", "olm"},
    "images": {"jpg", "jpeg", "jpe", "png", "gif", "heic", "heif", "tif", "tiff", "bmp", "webp", "svg", "ico",
               "icns", "psd", "psb", "xcf", "ai", "eps", "raw", "dng", "cr2", "cr3", "crw", "nef", "nrw", "arw",
               "srf", "sr2", "orf", "rw2", "raf", "pef", "x3f", "3fr", "erf", "kdc", "mrw", "jxl", "avif"},
    "videos": {"mov", "mp4", "m4v", "avi", "mkv", "mts", "m2ts", "3gp", "3g2", "wmv", "mpg", "mpeg",
               "m2v", "vob", "webm", "flv", "dv", "mxf", "insv", "lrv", "thm"},  # not ts: TypeScript
    "audio": {"mp3", "m4a", "m4b", "m4p", "aac", "wav", "aif", "aiff", "aifc", "flac", "alac", "ogg", "oga",
              "opus", "wma", "caf", "mid", "midi", "amr", "band", "logicx"},
    "documents": {"pdf", "doc", "docx", "dot", "dotx", "xls", "xlsx", "xlsm", "ppt", "pptx", "pps", "ppsx",
                  "pages", "numbers", "key", "odt", "ods", "odp", "odg", "rtf", "rtfd", "txt", "text", "md",
                  "markdown", "tex", "bib", "csv", "tsv", "epub", "mobi", "azw", "azw3", "djvu", "ps", "vcf",
                  "ics", "webarchive", "html", "htm", "xps", "oxps", "one", "opml", "ibooks"},
    "code": {"py", "pyc", "ipynb", "js", "mjs", "cjs", "ts", "tsx", "jsx", "c", "h", "cc", "cpp", "cxx", "hpp",
             "m", "mm", "swift", "java", "class", "jar", "kt", "kts", "scala", "go", "rs", "rb", "php", "pl",
             "pm", "sh", "zsh", "bash", "lua", "r", "jl", "hs", "ml", "el", "lisp", "clj", "cs", "fs", "vb",
             "sql", "css", "scss", "less", "json", "xml", "yml", "yaml", "toml", "ini", "cfg", "gradle",
             "cmake", "make", "mk", "o", "a", "so", "dylib", "wasm", "map", "lock", "iml", "pbxproj",
             "xib", "storyboard"},
    "disk images & archives": {"dmg", "iso", "img", "sparseimage", "toast", "cdr", "zip", "tar", "gz", "tgz",
                               "bz2", "tbz", "xz", "txz", "zst", "7z", "rar", "sit", "sitx", "cab", "pkg",
                               "mpkg", "xip", "apk", "ipa", "deb", "rpm", "jar"},
    "virtual machines": {"vmdk", "vdi", "vhd", "vhdx", "qcow2", "hdd", "hds", "vmem", "vmsn", "nvram"},
    "fonts": {"ttf", "otf", "ttc", "dfont", "woff", "woff2", "pfb", "pfm", "suit"},
    "databases": {"db", "sqlite", "sqlite3", "sqlitedb", "db-wal", "db-shm", "sqlite-wal", "sqlite-shm",
                  "realm", "mdb", "accdb", "fmp12", "ldb", "leveldb"},
}  # fmt: skip
_KIND_OF_EXT = {}
for _kind, _exts in BY_EXTENSION.items():
    for _ext in _exts:
        _KIND_OF_EXT.setdefault(_ext, _kind)  # first table wins (jar: code)

OTHER = "other"


def extension(name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or len(ext) > 12 or " " in ext:
        return ""
    ext = ext.lower()
    if ext in ("wal", "shm") and "." in stem:  # foo.sqlite-wal is one, foo.db.wal another
        ext = stem.rpartition(".")[2].lower() + "-" + ext
    return ext


def kind_of(rel: str, name: str) -> str:
    """The kind of the file `name` in the folder `rel` (volume/path inside a snapshot)."""
    folder = "/" + rel.lower() + "/"
    for kind, inside, suffixes in BY_PLACE:
        if any(s in folder for s in inside):
            return kind
        if suffixes and any(part.endswith(suffixes) for part in folder.split("/")):
            return kind
    return _KIND_OF_EXT.get(extension(name), OTHER)


@dataclass
class Kind:
    size: int = 0  # bytes on disk, each file once
    files: int = 0  # distinct files
    extensions: Counter = field(default_factory=Counter)  # extension -> bytes
    ext_files: Counter = field(default_factory=Counter)  # extension -> distinct files


@dataclass
class KindsStats:
    paths: int = 0  # file paths looked at
    files: int = 0  # distinct files
    size: int = 0  # bytes on disk, each file once
    snapshots: int = 0
    kinds: dict = field(default_factory=dict)  # kind -> Kind
    by_machine: dict = field(default_factory=dict)  # machine -> {kind -> bytes}
    errors: list[str] = field(default_factory=list)


def kinds(root: Path) -> KindsStats:
    stats = KindsStats()
    seen: set[tuple[int, int]] = set()
    walk = ArchiveWalk(root, stats.errors, lambda: stats.paths)
    for entry, (machine, _), rel in walk:
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError as e:
            stats.errors.append(f"{entry.path}: {e.strerror}")
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        stats.paths += 1
        key = (st.st_dev, st.st_ino)
        if key in seen:
            continue
        seen.add(key)
        size = st.st_blocks * 512
        kind = kind_of(rel, entry.name)
        k = stats.kinds.setdefault(kind, Kind())
        k.size += size
        k.files += 1
        ext = extension(entry.name) or "(none)"
        k.extensions[ext] += size
        k.ext_files[ext] += 1
        m = stats.by_machine.setdefault(machine, Counter())
        m[kind] += size
        stats.files += 1
        stats.size += size
    stats.snapshots = walk.snapshots
    return stats


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:5.1f}%" if whole else "    –"


def report_lines(stats: KindsStats, extensions: int = 5, other_extensions: int = 25) -> list[str]:
    order = sorted(stats.kinds.items(), key=lambda kv: -kv[1].size)
    lines = ["space by kind (size on disk, each file once):"]
    for name, k in order:
        top = ", ".join(f"{e} {human(s)}" for e, s in k.extensions.most_common(extensions) if s)
        lines.append(f"  {name:<26} {human(k.size):>9} {_pct(k.size, stats.size)}  {k.files:>9} files  ({top})")
    lines.append(f"  {'total':<26} {human(stats.size):>9} {_pct(stats.size, stats.size)}  {stats.files:>9} files")
    other = stats.kinds.get(OTHER)
    if other and other.extensions:
        lines += ["", f"biggest extensions in {OTHER} (to extend the tables in timak/kinds.py):"]
        for ext, size in other.extensions.most_common(other_extensions):
            lines.append(f"  {ext:<16} {human(size):>9} {_pct(size, other.size)}  {other.ext_files[ext]:>9} files")
    if len(stats.by_machine) > 1:
        lines += ["", "by machine:"]
        names = [name for name, _ in order]
        for machine, sizes in sorted(stats.by_machine.items()):
            total = sum(sizes.values())
            parts = ", ".join(f"{n} {human(sizes[n])}" for n in names if sizes.get(n))
            lines.append(f"  {machine}: {human(total)}  ({parts})")
    return lines
