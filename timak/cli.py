"""Command line interface."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .archiveimage import open_archive
from .biggest import biggest
from .biggest import report_lines as biggest_lines
from .compare import compare
from .dedupe import dedupe
from .diskimage import DiskImageError
from .extract import ExtractError, Extractor, find_rsync
from .kinds import kinds
from .kinds import report_lines as kinds_lines
from .prune import prune, report_lines
from .rules import Rules, RulesError, default_rules_text
from .scan import Scanner
from .sources import SourceError, open_sources, select_volumes
from .sources.base import unconfigured_volumes
from .util import FDA_HINT, REPORT_SUFFIX, case_sensitive, has_full_disk_access, human, is_snapshot, log, report_name
from .verify import verify

READS_BACKUPS = {"list", "scan", "extract", "verify", "serve"}
ONLY_READ_ARCHIVE = {"compare", "du"}  # the others write a report in it, or more


def _select(snaps, args):
    if args.since:
        snaps = [s for s in snaps if s.stamp >= args.since]
    if args.until:
        snaps = [s for s in snaps if s.stamp <= args.until]
    if args.last:
        keep = []
        for machine in sorted({s.machine for s in snaps}):
            keep += [s for s in snaps if s.machine == machine][-args.last :]
        snaps = keep
    return snaps


def _source_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("sources", nargs="+", metavar="SOURCE",
                   help="disk image (.dmg, .sparseimage, .sparsebundle), mounted backup drive, Backups.backupdb "
                        "folder, or a single snapshot copied out as a folder")
    p.add_argument("--machine",
                   help="only this machine (legacy backups); name to use for APFS backups and copied folders")
    p.add_argument("--volume", action="append", dest="volumes", metavar="NAME",
                   help="only this backed-up volume (repeatable); default: every volume containing a root")
    p.add_argument("--since", metavar="STAMP", help="only snapshots at or after this time, e.g. 2015-01-01")
    p.add_argument("--until", metavar="STAMP", help="only snapshots at or before this time")
    p.add_argument("--last", type=int, metavar="N", help="only the N most recent snapshots per machine")
    p.add_argument("--rules", type=Path, help="rules file (default: built-in, see `timak rules`)")
    p.add_argument("--include-in-progress", action="store_true", help="also use unfinished (.inProgress) backups")  # fmt: skip
    p.add_argument("--stamp", metavar="DATE",
                   help="date of a copied snapshot folder whose name has none, e.g. 2024-10-29 or 2024-10-29-143000")
    p.add_argument("--rename-volume", action="append", default=[], metavar="OLD=NEW",
                   help="archive volume OLD under the name NEW (repeatable), e.g. Data=System to line up with other backups")  # fmt: skip
    p.add_argument("--rename-machine", action="append", default=[], metavar="OLD=NEW",
                   help="archive machine OLD under the name NEW (repeatable), to join the timeline of a Mac that was renamed")  # fmt: skip


def _renames(items: list[str], option: str) -> dict[str, str]:
    renames = {}
    for item in items:
        old, sep, new = item.partition("=")
        if not sep or not old.strip() or not new.strip():
            raise SourceError(f"{option} expects OLD=NEW, got {item!r}")
        renames[old.strip()] = new.strip()
    return renames


def _open(args):
    return open_sources(
        args.sources, args.machine, args.include_in_progress, args.stamp,
        _renames(args.rename_volume, "--rename-volume"), _renames(args.rename_machine, "--rename-machine"),
    )  # fmt: skip


def _volume_warnings(snaps, rules: Rules, args) -> list[str]:
    """Warn about backed-up volumes that would be skipped without the rules saying so."""
    if args.volumes:  # volumes chosen explicitly on the command line
        return []
    messages = []
    for name, stamps in sorted(unconfigured_volumes(snaps, rules).items()):
        when = stamps[0] if len(stamps) == 1 else f"{stamps[0]} … {stamps[-1]}"
        messages.append(
            f'warning: volume "{name}" is in {len(stamps)} snapshot(s) ({when}) but contains none of '
            f"{', '.join(rules.roots)} and is not in [volume_roots], so it is SKIPPED. "
            f'To keep it, add `"{name}" = ["/"]` under [volume_roots] in your rules file; '
            f'to skip it without this warning, add `"{name}" = []`.'
        )
    for m in messages:
        log(m)
    return messages


def cmd_list(args) -> int:
    rules = Rules.load(args.rules)
    with _open(args) as snaps:
        snaps = _select(snaps, args)
        warnings = _volume_warnings(snaps, rules, args)
        if args.json:
            print(json.dumps({"snapshots": [_snapshot_json(s, rules, args) for s in snaps], "warnings": warnings}))
            return 0
        for s in snaps:
            chosen = {v.original_name for v in select_volumes(s, rules, args.volumes)}
            vols = ", ".join(v.original_name + ("*" if v.original_name in chosen else "") for v in s.volumes)
            flag = " (in progress)" if s.in_progress else ""
            print(f"{s.kind:6} {s.machine:24} {s.stamp}{flag}  {vols}")
        print(f"\n{len(snaps)} snapshots; * = volume that would be extracted")
    return 0


def _snapshot_json(snap, rules: Rules, args) -> dict:
    chosen = {v.original_name for v in select_volumes(snap, rules, args.volumes)}
    return {
        "kind": snap.kind, "machine": snap.machine, "stamp": snap.stamp, "source": snap.source,
        "in_progress": snap.in_progress,
        "volumes": [{"name": v.name, "original_name": v.original_name, "selected": v.original_name in chosen}
                    for v in snap.volumes],
    }  # fmt: skip


def cmd_scan(args) -> int:
    rules = Rules.load(args.rules)
    with _open(args) as snaps:
        snaps = _select(snaps, args)
        warnings = _volume_warnings(snaps, rules, args)
        scanner = Scanner(rules, args.volumes, kept_only=args.kept_only)
        scanner.scan(snaps)
    print(scanner.report())
    for w in warnings:  # again, after a long scan
        print(w)
    return 0


def cmd_extract(args) -> int:
    rules = Rules.load(args.rules)
    rsync = find_rsync(args.rsync)
    archive = args.archive.expanduser()
    if os.geteuid() != 0:
        log("note: not running as root; file owners are not preserved and unreadable files are skipped")
    archive.mkdir(parents=True, exist_ok=True)
    with _open(args) as snaps:
        snaps = _select(snaps, args)
        if case_sensitive(archive) is False and any(case_sensitive(s.path) for s in snaps[:1]):
            log(
                "warning: the backup is case-sensitive but the archive disk is not: files whose names differ "
                "only by case (e.g. README and readme in one folder) would collide. Prefer a case-sensitive "
                "volume, or let timak make a new archive on an APFS disk (without --folder): "
                "it is then a case-sensitive disk image."
            )
        warnings = _volume_warnings(snaps, rules, args)
        total = Extractor(archive, rules, rsync, args.volumes).run(snaps)
    print(f"done: {total.summary()}")
    for w in warnings:  # again, after a long run
        print(w)
    return 1 if total.errors else 0


def cmd_dedupe(args) -> int:
    stats = dedupe(args.archive.expanduser(), loose=args.loose, min_size=args.min_size, dry_run=args.dry_run)
    print(("would merge: " if args.dry_run else "done: ") + stats.summary())
    return 1 if stats.errors else 0


def cmd_prune(args) -> int:
    rules = Rules.load(args.rules)
    archive = args.archive.expanduser()
    try:
        stats = prune(archive, rules, apply=args.apply, machines=args.machine)
    except FileNotFoundError as e:
        print(f"timak: {e}", file=sys.stderr)
        return 2
    if stats.paths:
        print("removed:" if args.apply else "would remove (sizes count each file once, in the first snapshot that has it):")
        for line in report_lines(stats, args.top):
            print(line)
    for e in stats.errors[: args.max_problems]:
        print(e)
    if len(stats.errors) > args.max_problems:
        print(f"… {len(stats.errors) - args.max_problems} more errors")
    summary = ("done: " if args.apply else "dry run, nothing removed: ") + stats.summary(args.apply)
    print(summary)
    _write_prune_report(archive, args, stats, summary)
    if not args.apply and stats.entries:
        print("rerun with --apply to remove them; this cannot be undone")
    if stats.skipped_machines:
        print(f"skipped (extraction in progress or interrupted): {', '.join(stats.skipped_machines)}")
    return 1 if stats.errors or stats.skipped_machines else 0


def _write_prune_report(archive: Path, args, stats, summary: str) -> None:
    """Every path removed (or to remove), in <archive>/prune[-dry-run].timakreport."""
    path = archive / (("prune" if args.apply else "prune-dry-run") + REPORT_SUFFIX)
    lines = [
        f"timak {__version__} prune{'' if args.apply else ' (dry run)'}, {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z}",
        "command: " + " ".join(shlex.quote(a) for a in sys.argv),
        "",
        *report_lines(stats),
    ]
    if stats.errors:
        lines += ["", f"{len(stats.errors)} errors:", *stats.errors]
    lines += ["", summary]
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        log(f"warning: could not write the report {path}: {e.strerror}")
        return
    print(f"full list: {path}")


def cmd_compare(args) -> int:
    a, b = args.archive.expanduser(), args.other.expanduser()
    try:
        stats = compare(a, b, loose=args.loose, min_size=args.min_size)
    except FileNotFoundError as e:
        print(f"timak: {e}", file=sys.stderr)
        return 2
    print(stats.summary(a, b))
    return 1 if stats.errors else 0


def cmd_verify(args) -> int:
    rules = Rules.load(args.rules)
    with _open(args) as snaps:
        snaps = _select(snaps, args)
        _volume_warnings(snaps, rules, args)
        result = verify(snaps, args.archive.expanduser(), rules, args.volumes, args.content)
    for p in result.problems[: args.max_problems]:
        print(p)
    if len(result.problems) > args.max_problems:
        print(f"… {len(result.problems) - args.max_problems} more problems")
    if result.missing_snapshots:
        print(f"{len(result.missing_snapshots)} snapshots not in the archive: {', '.join(result.missing_snapshots[:5])}"
              + (" …" if len(result.missing_snapshots) > 5 else ""))  # fmt: skip
    summary = f"{result.checked} entries checked, {len(result.problems)} problems, {result.unreadable} unreadable in source"
    if result.ignored:
        summary += f"; {result.ignored} files only in the archive ignored, because the rules drop them (e.g. .DS_Store made by Finder)"
    print(summary)
    _write_report(args, result, summary)
    return 0 if result.ok else 1


def _write_report(args, result, summary: str) -> None:
    """The full verify result, whatever --max-problems says, in <archive>/verify-<source>.timakreport."""
    path = args.archive.expanduser() / report_name("verify", args.sources)
    lines = [
        f"timak {__version__} verify, {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z}",
        "command: " + " ".join(shlex.quote(a) for a in sys.argv),
        "",
        *result.problems,
    ]
    if result.missing_snapshots:
        lines += ["", f"{len(result.missing_snapshots)} snapshots not in the archive:", *result.missing_snapshots]
    lines += ["", summary]
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        log(f"warning: could not write the report {path}: {e.strerror}")
        return
    print(f"full report: {path}")


def cmd_serve(args) -> int:
    from .server import serve

    return serve(args.port, open_browser=not args.no_browser, token_file=args.token_file, owner_pid=args.owner_pid)


def cmd_rules(args) -> int:
    print(default_rules_text(), end="")
    return 0


def cmd_biggest(args) -> int:
    root = args.archive.expanduser()
    homes = Rules.load(args.rules).home_volumes if args.rules else ()  # before the long walk
    try:
        stats = biggest(root, args.top, args.folders)
    except FileNotFoundError as e:
        print(f"timak: {e}", file=sys.stderr)
        return 2
    summary = (
        f"{stats.paths} file paths in {stats.snapshots} snapshots, "
        f"{stats.files} distinct files, {human(stats.size)} on disk"
    )
    if stats.errors:
        summary += f", {len(stats.errors)} errors"
    lines = biggest_lines(stats, homes)
    print("\n".join(lines))
    for e in stats.errors[: args.max_problems]:
        print(e)
    print(summary)
    path = _biggest_report_path(root)
    try:
        path.write_text("\n".join([
            f"timak {__version__} biggest, {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z}",
            "command: " + " ".join(shlex.quote(a) for a in sys.argv),
            "", *lines, *([""] + stats.errors if stats.errors else []), "", summary,
        ]) + "\n", encoding="utf-8")  # fmt: skip
        print(f"saved in {path}")
    except OSError as e:
        log(f"warning: could not write the report {path}: {e.strerror}")
    print("to drop something: add its rule to your rules file, then "
          "timak prune ARCHIVE --rules FILE (a dry run, listing what would go)")
    return 1 if stats.errors else 0


def cmd_kinds(args) -> int:
    root = args.archive.expanduser()
    try:
        stats = kinds(root)
    except FileNotFoundError as e:
        print(f"timak: {e}", file=sys.stderr)
        return 2
    summary = (
        f"{stats.paths} file paths in {stats.snapshots} snapshots, "
        f"{stats.files} distinct files, {human(stats.size)} on disk"
    )
    if stats.errors:
        summary += f", {len(stats.errors)} errors"
    lines = kinds_lines(stats, args.extensions)
    print("\n".join(lines))
    for e in stats.errors[: args.max_problems]:
        print(e)
    print(summary)
    path = _biggest_report_path(root, "kinds")
    try:
        path.write_text("\n".join([
            f"timak {__version__} kinds, {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z}",
            "command: " + " ".join(shlex.quote(a) for a in sys.argv),
            "", *lines, *([""] + stats.errors if stats.errors else []), "", summary,
        ]) + "\n", encoding="utf-8")  # fmt: skip
        print(f"saved in {path}")
    except OSError as e:
        log(f"warning: could not write the report {path}: {e.strerror}")
    return 1 if stats.errors else 0


def _biggest_report_path(root: Path, name: str = "biggest") -> Path:
    """At the top of the archive, never inside a snapshot or a machine folder."""
    if is_snapshot(root):  # one snapshot
        return root.parent.parent / f"{name}-{root.parent.name}-{root.name}{REPORT_SUFFIX}"
    try:
        machine = any(is_snapshot(d) for d in root.iterdir() if d.is_dir())
    except OSError:
        machine = False
    if machine:
        return root.parent / f"{name}-{root.name}{REPORT_SUFFIX}"
    return root / f"{name}{REPORT_SUFFIX}"


def cmd_du(args) -> int:
    """Real disk usage of an archive, counting hard-linked files once."""
    seen = set()
    total = files = 0
    for dirpath, dirnames, filenames in os.walk(args.archive.expanduser()):
        for name in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            files += 1
            if (st.st_dev, st.st_ino) not in seen:
                seen.add((st.st_dev, st.st_ino))
                total += st.st_blocks * 512
    print(f"{human(total)} on disk, {files} file entries, {len(seen)} distinct files")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="timak", description="Turn old Time Machine backups into a compact archive of user files."
    )
    parser.add_argument("--version", action="version", version=f"timak {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list snapshots found in the sources")
    _source_args(p)
    p.add_argument("--json", action="store_true", help="print the snapshots as JSON")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("scan", help="dry run: report what would be kept and dropped")
    _source_args(p)
    p.add_argument("--kept-only", action="store_true", help="only walk the roots (faster, no dropped sizes)")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("extract", help="copy kept files into a hard-linked snapshot archive")
    _source_args(p)
    p.add_argument("--archive", type=Path, required=True, help="archive folder (on APFS or HFS+)")
    p.add_argument("--rsync", help="rsync 3.x binary (default: Homebrew's)")
    p.add_argument("--folder", action="store_true",
                   help="make a new archive as a plain folder even on an APFS disk "
                        "(default there: a case-sensitive HFS+ ARCHIVE.sparsebundle)")  # fmt: skip
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("dedupe", help="hard-link identical files across the archive")
    p.add_argument("archive", type=Path)
    p.add_argument("--loose", action="store_true", help="compare content only, ignore mtime/mode/owner/xattrs")
    p.add_argument("--min-size", type=int, default=1, metavar="BYTES", help="skip smaller files (default 1)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_dedupe)

    p = sub.add_parser("prune", help="remove from archived snapshots what the rules now drop (dry run by default)")
    p.add_argument("archive", type=Path)
    p.add_argument("--rules", type=Path, required=True, help="rules file to apply")
    p.add_argument("--machine", action="append", metavar="NAME",
                   help="only this machine folder of the archive (repeatable); default: all")  # fmt: skip
    p.add_argument("--apply", action="store_true", help="really remove; without it, only report")
    p.add_argument("--top", type=int, default=10, metavar="N",
                   help="biggest paths shown per rule (default 10); the report file lists them all")  # fmt: skip
    p.add_argument("--max-problems", type=int, default=50, metavar="N")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("compare", help="what merging two archives would take, dedupe included (read-only)")
    p.add_argument("archive", type=Path, metavar="A")
    p.add_argument("other", type=Path, metavar="B")
    p.add_argument("--loose", action="store_true", help="compare content only, ignore mtime/mode/owner/xattrs")
    p.add_argument("--min-size", type=int, default=1, metavar="BYTES", help="skip smaller files (default 1)")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("verify", help="check the archive against the sources")
    _source_args(p)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--content", action="store_true", help="also compare file contents (slow)")
    p.add_argument("--max-problems", type=int, default=50, metavar="N")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("biggest", help="the biggest files and folders of an archive, each file once (read-only)")
    p.add_argument("archive", type=Path, help="an archive, a machine folder in it or one snapshot")
    p.add_argument("--top", type=int, default=100, metavar="N", help="files listed (default 100)")
    p.add_argument("--folders", type=int, default=50, metavar="N", help="folders listed (default 50)")
    p.add_argument("--rules", type=Path, help="your rules file: suggested rules then use its home_volumes")
    p.add_argument("--max-problems", type=int, default=50, metavar="N")
    p.set_defaults(func=cmd_biggest)

    p = sub.add_parser("kinds", help="space used by mail, documents, images, videos… each file once (read-only)")
    p.add_argument("archive", type=Path, help="an archive, a machine folder in it or one snapshot")
    p.add_argument("--extensions", type=int, default=5, metavar="N", help="extensions shown per kind (default 5)")
    p.add_argument("--max-problems", type=int, default=50, metavar="N")
    p.set_defaults(func=cmd_kinds)

    p = sub.add_parser("du", help="disk usage of an archive (hard links counted once)")
    p.add_argument("archive", type=Path)
    p.set_defaults(func=cmd_du)

    p = sub.add_parser("serve", help="local web interface in your browser")
    p.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 (default 8765)")
    p.add_argument("--no-browser", action="store_true", help="only print the address, do not open a browser")
    p.add_argument("--token-file", type=Path, help=argparse.SUPPRESS)  # used by the Mac app
    p.add_argument("--owner-pid", type=int, help=argparse.SUPPRESS)  # stop once this process is gone and idle
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("rules", help="print the default rules (to start your own rules file)")
    p.set_defaults(func=cmd_rules)
    return parser


def _open_archives(args, stack: contextlib.ExitStack) -> None:
    """Mount an archive kept in a sparse bundle (making one for a new archive on APFS) for the command."""
    readonly = args.command in ONLY_READ_ARCHIVE
    for attr in ("archive", "other"):
        path = getattr(args, attr, None)
        if path is not None:
            create = args.command == "extract" and not args.folder
            setattr(args, attr, stack.enter_context(open_archive(path, create=create, readonly=readonly)))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in READS_BACKUPS and not has_full_disk_access():
        log(f"warning: {FDA_HINT}")
    try:
        with contextlib.ExitStack() as stack:
            _open_archives(args, stack)
            return args.func(args)
    except (SourceError, DiskImageError, ExtractError, RulesError) as e:
        print(f"timak: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted; rerun the same command to resume", file=sys.stderr)
        return 130
