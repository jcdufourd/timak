# timak

Turn old Time Machine backups into a compact archive that keeps only **user
files**. Applications, macOS itself, Homebrew, caches and similar rebuildable
data are dropped.

timak is for people who have a drawer of old Time Machine disks and want
their own files back from them: documents, photos, mail, projects, as they
were at each backup date. Not the system they ran on, and without depending
on Time Machine to read them. Years of backups take little more than the
size of the user files plus what changed between snapshots, on one disk you
can keep or copy.

timak was called timac until version 1.1. Archives made under that name keep
working: their snapshots hold a `.timac.json` instead of `.timak.json`, which
timak reads, and replaces with a `.timak.json` the next time it updates that
snapshot (`prune`).

timak is free software, shared under the Creative Commons Attribution-ShareAlike
4.0 licence (see [Licence](#licence)).

The result is a plain folder of dated snapshots, laid out the way Timeshift and
rsnapshot do it. A file that didn't change between snapshots is stored once and
hard-linked:

```
archive/
  MacBook-Pro/
    2014-03-02-143000/
      .timak.json              # source, rules, statistics
      Macintosh HD/Users/alice/Documents/…
    2014-03-09-101500/
      Macintosh HD/Users/alice/…  # unchanged files are hard links to the previous snapshot
```

You can open it in Finder, copy files back with `cp`, or move it to another
disk with `rsync -aH`. It doesn't depend on Time Machine or on timak.

On an APFS disk, a new archive is made inside a disk image: `extract --archive
/Volumes/Archive/tm` creates `/Volumes/Archive/tm.sparsebundle`, a
case-sensitive Journaled HFS+ volume named `tm` with the folders above at its
root. Double-click it to browse it in Finder. See [The archive disk](#the-archive-disk).

## Safety

- The sources are only ever **read**. Disk images are attached read-only,
  APFS snapshots are mounted read-only, and timak never deletes anything from
  a backup.
- **`prune` removes data from the archive**, permanently. It runs as a dry run
  unless you add `--apply`; read the dry run first.
- `dedupe` replaces identical copies in the archive with hard links. The
  content stays; with `--loose`, the copies then share one set of metadata.
- Keep your old backup disks until you have run `verify` and looked at the
  archive yourself.

## Status and limitations

- **macOS only.** timak relies on `diskutil`, `mount_apfs` and macOS
  extended attributes. Disk images are attached with `diskutil image`, which
  replaces the deprecated `hdiutil attach`, and ejected with `diskutil eject`.
  On a macOS too old to have `diskutil image`, timak uses `hdiutil attach` and
  `hdiutil detach` instead. `hdiutil info` is still used to find an image that
  is already attached.
- Legacy HFS+ backups (`Backups.backupdb`) are the main target, and are
  tested with real Time Machine directory hard links.
- **APFS backups (macOS 11 and later) are experimental.** Detection has been
  tried on a real image, extraction not yet. See [Source formats](#source-formats).
- Version 1.1.0. The command-line options and the archive layout are not
  expected to change much, but there is no promise of stability yet.

## Requirements

- macOS, Python ≥ 3.11 (`brew install python`)
- rsync 3.x (`brew install rsync`). The `/usr/bin/rsync` that ships with macOS is
  openrsync, which loses metadata.
- **Full Disk Access** for the terminal you run it from (System Settings →
  Privacy & Security), so that it can read inside the backups.
- The archive must go on APFS or HFS+, because exFAT has no hard links. On
  APFS, timak keeps it in a case-sensitive HFS+ disk image (see
  [The archive disk](#the-archive-disk)).

timak itself has no dependencies beyond the Python standard library.

## Installation

```bash
brew install python rsync
git clone <this repository's URL> timak
cd timak
```

You can then run it from the project folder as `python3 -m timak …`, which
is what the examples below do. Or install it, which gives you a `timak`
command:

```bash
python3 -m pip install .        # or `pip install -e .` to work on the code
```

Homebrew's Python may refuse to install into its own environment
("externally managed"); use a virtual environment then
(`python3 -m venv .venv && . .venv/bin/activate`), or keep using
`python3 -m timak` from the project folder.

## Quick start

Say your old backup is a disk image, `/Volumes/Big/old-tm-2016.dmg`, and the
archive goes to `/Volumes/Archive/tm` (an APFS volume):

```bash
python3 -m timak list    /Volumes/Big/old-tm-2016.dmg     # what is in it
python3 -m timak scan    /Volumes/Big/old-tm-2016.dmg     # what would be kept, per rule
python3 -m timak extract /Volumes/Big/old-tm-2016.dmg --archive /Volumes/Archive/tm
python3 -m timak verify  /Volumes/Big/old-tm-2016.dmg --archive /Volumes/Archive/tm
```

If `scan` keeps something you don't want, or drops something you do, write
your own rules first (see [Rules](#rules)) and pass `--rules my-rules.toml`.
If you prefer a window to a terminal, use the [web interface](#web-interface)
or the [Mac app](#mac-app).

### The archive disk

When `extract` creates a new archive on an APFS disk, it makes
`<archive>.sparsebundle` beside where the folder would have gone: a
case-sensitive Journaled HFS+ disk image, named after the archive, that grows
as needed up to the size of the disk. On an HFS+ disk, the archive is a plain
folder as before. To get a plain folder on APFS too, add `--folder`. Naming the
archive `….sparsebundle` makes the image on any disk.

You keep passing the same path to every command (`--archive
/Volumes/Archive/tm`, `timak dedupe /Volumes/Archive/tm`, `timak biggest
/Volumes/Archive/tm/MacBook-Pro`): timak finds `tm.sparsebundle`, mounts it,
works inside it and ejects it again. If you have mounted it yourself, timak
uses that mount and leaves it mounted. A folder named `tm` that already
exists always wins, so archives made before stay where they are.

Inside the image the archive is on HFS+, which keeps whole seconds only and
can't store dates before 1904 or after February 2040. macOS writes such a
file (a photo dated 2098, say) with the date 1 January 1970; timak then
treats that date as matching, so the file is still linked and `verify`
accepts it.

To copy an archive to another disk, copy the `.sparsebundle` (it is a folder
of band files, which also suits `rsync`), or mount it and copy its
content with `rsync -aH`.

## Usage

```bash
# Run from the project folder, or use `timak` if you installed it
python3 -m timak list   /Volumes/Big/old-tm-2016.dmg
python3 -m timak scan   /Volumes/Big/old-tm-2016.dmg            # dry run: sizes kept / dropped per rule
python3 -m timak extract /Volumes/Big/old-tm-2016.dmg --archive /Volumes/Archive/tm
python3 -m timak verify  /Volumes/Big/old-tm-2016.dmg --archive /Volumes/Archive/tm
python3 -m timak dedupe  /Volumes/Archive/tm                     # link moved/renamed duplicates
python3 -m timak du      /Volumes/Archive/tm                     # real size, links counted once
python3 -m timak biggest /Volumes/Archive/tm                     # 100 biggest files and 50 biggest folders
python3 -m timak kinds   /Volumes/Archive/tm                     # space by kind: mail, documents, images, videos…
python3 -m timak prune   /Volumes/Archive/tm --rules my-rules.toml  # apply new rules to old snapshots (dry run; --apply)
python3 -m timak compare /Volumes/Archive/tm /Volumes/Other/tm   # what merging two archives would take
```

Sources can be disk images (`.dmg`, `.sparseimage`, `.sparsebundle`), mounted
backup drives, or a `Backups.backupdb` folder. You can pass several at once.
Their snapshots are merged by machine and date, so backups of the same Mac
spread over several old drives end up in one timeline.

Useful options: `--last N`, `--since 2015-01-01`, `--until …`, `--machine NAME`,
`--volume "Macintosh HD"`, `--rules my-rules.toml`.

Use `--rename-volume OLD=NEW` (repeatable) to file a volume under another name,
so that backups of the same Mac line up when the volume was named differently
over time, e.g. `--rename-volume Data=System`. Volume and machine names decide
which earlier snapshot a file is linked to. Pass the same options to `verify`.

Likewise, `--rename-machine OLD=NEW` files the snapshots of machine OLD under
NEW, for a Mac that was renamed. For legacy backups `--machine` only picks one
of the machines recorded in the backup; it does not rename.

Only volumes that contain one of their roots are extracted. A second disk that
was backed up too, without a `Users` folder, is skipped unless you give it
roots in `[volume_roots]` (see Rules).

`verify` prints the first 50 problems (`--max-problems N` to change that) and
always writes the full list at the top of the archive, in a file named after
the source, such as `verify-Backup2021.timakreport`. Verifying the same source
again replaces it. Files that are only in the archive
and that the rules drop, such as the `.DS_Store` files Finder writes into
every folder you open, are counted but not reported as problems.

`extract` can be interrupted and resumed. Finished snapshots are skipped, and
an unfinished `<stamp>.partial` folder is removed and redone.

### Web interface

```bash
sudo /opt/homebrew/bin/python3 -m timak serve
```

This opens a page in your browser that does everything the commands do:

- **Sources and options:** add backups (with a file picker) and set the archive folder, the rules file and the other options. The page remembers them.
- **Snapshots:** what `list` finds, with the volumes that would be extracted and any volume warnings.
- **Rules:** edit the rules file, checked as you type, including the `[volume_roots]` placement.
- **Run:** scan, extract, verify, dedupe and du, with the live log, a progress bar and a Stop button. Stop works like Ctrl-C, so an extract can be resumed later.
- **Archive:** each archived snapshot, with how many files were hard-linked to the previous snapshot and how much new data it added.

Keep the terminal open while you use the page; Ctrl-C there stops the server and any running command.

The page talks to timak on `127.0.0.1` only, so nothing is reachable from other computers. The address printed in the terminal ends with a secret token, and timak refuses requests without it. Use `--port` to pick another port, and `--no-browser` to only print the address. Under sudo, the page opens in your own browser, not as root.

### Mac app

`mac/` holds Timak.app, a small SwiftUI app that shows the same page in its
own window and runs `timak serve` for you, so no terminal has to stay open.

```bash
mac/build.sh --install     # builds and copies Timak.app to /Applications
```

It needs only the Command Line Tools and Homebrew's Python (3.11 or later). The
app carries its own copy of the `timak` package, so rebuild it after changing
the code.

- At launch, Timak asks for your password: timak runs as root (through
  `sudo`) to mount APFS backups and keep file owners. The failure screen
  offers to run without it. Timak starts the server itself rather than
  through the standard macOS administrator prompt, because only then does
  the server get Timak's Full Disk Access.
- Give **Timak** Full Disk Access (System Settings › Privacy & Security), then
  restart it. The chips at the top of the page show whether it worked.
- Quitting stops the server. If a command is running, you can let it finish in
  the background; opening Timak again before it ends reconnects to it.
- You can drop a disk image or a folder onto the window to add it as a source.
- The Server menu restarts the server or shows its log
  (`~/Library/Logs/Timak/server.log`).
- **Help** at the top right of the page, or Help › Timak Help (⌘?), opens a
  step-by-step guide for people new to all this, in a window of its own. The
  same page is at `/help.html` when you use `timak serve` in a browser.
- Timak › About Timak shows the version and the licence.

**Signing.** `build.sh` signs with your "Apple Development" certificate when
you have one. That is the free Personal Team: in Xcode, Settings › Accounts,
add your Apple ID, then Manage Certificates › + › Apple Development. Without
it the app is signed ad hoc and works, but every rebuild looks like a new app
to macOS, which then forgets its Full Disk Access.

### Running as root

When you run as a normal user, files keep their dates, permissions and
extended attributes, but the archive's files are owned by you. Files your
account can't read are reported and skipped. With
`sudo /opt/homebrew/bin/python3 -m timak …`, owners are kept as well (as
numeric ids). Use the full Python path, because `sudo python3` runs macOS's
old 3.9.

## Rules

`python3 -m timak rules > my-rules.toml` prints the built-in rules so you can
start your own file:

```toml
roots = ["/Users"]       # only these trees are considered
rules = [
  "- ~/Library/Caches/", # first match wins; unmatched paths are kept
  "- node_modules/",     # unanchored: matches at any depth
  "+ ~/Library/Mail/",
]
```

Volumes that need other roots are listed by name, as `timak list` shows them
(after any `--rename-volume`). This is typically an external disk that Time
Machine backed up and that holds only user files:

```toml
[volume_roots]
Large = ["/"]            # the whole volume; rules still apply inside it
"Old Disk" = []          # never extract this one
```

If a backup holds a volume that would be skipped although nothing mentions it
(none of the `roots`, not in `[volume_roots]`), `list`, `scan`, `extract` and
`verify` warn about it, and `scan` and `extract` repeat the warning at the end.
Listing it, even as `= []`, silences the warning. So does `--volume`.

Some Macs kept the home folders on a separate disk mounted as `/Users`, so
the backup has a volume holding `alice/Library/…` rather than
`Users/alice/Library/…`. List it in `home_volumes`, before `[volume_roots]`, and
the rules see its paths as if they were under `/Users`: every `~/` rule
applies there too. Its roots stay written from the volume's own root:

```toml
home_volumes = ["User"]

[volume_roots]
User = ["/"]
```

The default rules drop the macOS bookkeeping folders at the top of a disk
(`.Spotlight-V100`, `.fseventsd`, `.Trashes`, …).

How patterns work:

- **Anchoring:** a leading `/` anchors a pattern at the volume root. Without
  it, the pattern can match at any depth.
- **Shortcuts:** `~/` means `/Users/*/`.
- **Directories:** a trailing `/` makes the pattern match directories only.
- **Wildcards:** `*` and `?` stay within one path component, `**` crosses `/`.
- **Case:** matching ignores case unless you set `case_sensitive = true`.
- **Dropped folders are dropped whole.** A `+` rule can't keep something
  inside a folder that a `-` rule already dropped. To keep part of a folder,
  drop its siblings instead.

Run `scan` first. It shows how much each rule drops, which helps you spot a
rule that is too greedy.

### Finding what takes the space

`biggest` lists the 100 biggest files of an archive, a machine folder in it or
one snapshot (`--top N`), then the 50 biggest folders (`--folders N`), like a
disk map such as GrandPerspective but aware of the hard links:

```
     6.4 GB    18  MacBook: Macintosh HD/Users/alice/.android/avd/Phone.avd/userdata-qemu.img  (18 snapshots, 2020-01-04-120000 … 2021-05-16-120000)
```

- Each file is listed once, with its size on disk, its number of hard links,
  and its path inside the snapshot (volume/path) with the snapshots it is in.
  A file moved or renamed between snapshots shows each of its paths.
- A folder's size counts each file once, where it was first found, so a file
  kept by 30 snapshots doesn't count 30 times. A folder whose space is nearly
  all in one subfolder is left out, since the subfolder says more.
- Each folder comes with a rule that would drop it, ready to paste into your
  rules file. Then run `prune` (below): its dry run lists what the rule would
  remove and how much space that would free.

The list also goes to `biggest.timakreport` at the top of the archive
(`biggest-<machine>[-<snapshot>].timakreport` for a machine or a snapshot).
It reads every path, as long as `du` does; a single snapshot is much quicker.

### Space by kind of file

`kinds` splits the space of an archive, a machine folder or one snapshot by
kind of file: mail, messages, Photos library, images, videos, audio, music
library, documents, code, disk images & archives, virtual machines, fonts,
databases, applications, other.

```
  photos library               212.4 GB  41.0%     98412 files  (heic 120.1 GB, jpg 61.0 GB, mov 25.3 GB, …)
  mail                          38.7 GB   7.5%    912334 files  (emlx 30.2 GB, (none) 5.1 GB, …)
```

- Each file counts once, under the first path it was found at, as in `biggest`.
- Where a file is decides first: anything in `~/Library/Mail`, a Photos
  library, a virtual machine or a git repository goes to that kind, whatever
  its extension. Other files go by extension.
- Each kind lists its biggest extensions, and `other` gets a longer list, so
  you can see what is left unclassified. The tables are at the top of
  `timak/kinds.py`.
- With several machines it also shows the split for each one. The report goes
  to `kinds.timakreport`, named like the `biggest` one.

### Applying new rules to an existing archive

`extract` never changes a snapshot already in the archive, so new rules only
affect snapshots extracted afterwards. `prune` applies them to the old ones:

```bash
python3 -m timak prune /Volumes/Archive/tm --rules my-rules.toml           # dry run
sudo /opt/homebrew/bin/python3 -m timak prune /Volumes/Archive/tm --rules my-rules.toml --apply
```

It goes through every finished snapshot (or only `--machine NAME`) and removes
what the rules drop, the way `extract` would have: dropped folders go whole,
and so does anything outside the roots. Without `--apply` it only reports
what it would remove and how much space that would free.

It lists each removed folder or file once, by machine and rule, biggest first,
with the snapshots it is in, e.g.
`1.0 GB  Macintosh HD/Users/alice/Library/Application Support/SomeApp/  (42 snapshots, 2020-01-04-120000 … 2021-05-16-120000)`.
The terminal shows the 10 biggest per rule (`--top N`); the full list goes to
`prune-dry-run.timakreport` at the top of the archive (`prune.timakreport`
with `--apply`).

- **It can only remove.** A folder the old rules dropped does not come back;
  extract that machine again for that.
- **Removing is permanent.** Read the dry run first.
- **Space comes back only when every snapshot drops a file.** Unchanged files
  are hard links shared by many snapshots. The space reported as freed counts
  only files whose last link is removed.
- **Nothing else may use the archive meanwhile.** A machine with a `.partial`
  snapshot (an extract running or interrupted) is skipped; don't run verify,
  dedupe or compare on the same archive at the same time.
- Each pruned snapshot gets the new rules in its `.timak.json`, with a
  `pruned` record of the old ones. So a rerun skips the snapshots already
  done, and the next `extract` keeps its shortcuts.

## How it works

Snapshots are processed oldest first. For each selected volume, timak walks the
source tree, applies the rules, and compares against the previous snapshot in
the archive:

- **Unchanged directory tree.** In legacy (HFS+) backups, Time Machine stores a
  directory that didn't change as a *directory hard link* to the previous
  backup's copy. When timak sees the same directory inode, it rebuilds that
  subtree from the previous archive snapshot using file hard links, without
  reading the source at all. This is what makes runs over hundreds of
  snapshots fast.
- **Unchanged file.** If the file is the same inode as in the previous
  snapshot (same filesystem), or otherwise has the same size, mtime and mode,
  it is hard-linked to the previous archive copy.
- **Everything else.** New or changed files, symlinks, and every directory
  (for its metadata) are copied by one `rsync -aHXN --files-from` call per
  volume.

`dedupe` then hashes files across the whole archive and hard-links identical
copies. That catches renamed or moved files, and overlapping backups from
different drives. By default it only merges files whose metadata (mode, owner,
mtime, xattrs) is also identical, because hard links share metadata.
`--loose` compares content only.

macOS does not let a process change files inside app bundles (`….app/`)
unless it has the App Management permission (System Settings › Privacy &
Security). `dedupe` leaves those files as they are and says how many it
skipped. Give the permission to your terminal or to Timak to link them too.

`dedupe` and `compare` skip unfinished `.partial` snapshots and hidden
folders at the top of the folder you give them (a disk's `.Spotlight-V100`,
`.Trashes`, …), so you can point them at a whole disk.

### Merging two archives

`compare A B` tells you, before copying anything, what merging archive B
into archive A would take. It only reads both, which can be on different
disks. It compares files the way `dedupe` does, regardless of name or
location, and reports:

- the real size of each archive, and what a dedupe of it alone would save;
- how much of B's content is already in A;
- the size of the merged archive, simply copied together and after dedupe;
- the snapshots (machine and date) that are in both archives.

With `--loose`, only contents are compared, as with `dedupe --loose`. In the
default strict mode, a file whose owner or group differs between the two
disks (for example on a disk that ignores ownership) does not count as
shared.

It costs as much as `dedupe --dry-run` on both: a scan of every file, and
reading the large files that may be duplicates. It keeps one path per
distinct file in memory.

## Source formats

| Source | Status |
|---|---|
| Legacy HFS+ backups (`Backups.backupdb`), in a disk image or on a drive | supported, tested with real directory hard links |
| A single snapshot copied out as a folder (e.g. `Backup20241029/Data/Users/…`, `Backup20241029/Large/…`, or a folder that itself contains `Users/`) | supported. The date comes from the folder name, or `--stamp 2024-10-29`. The machine name is read from the backup when possible |
| APFS backups (macOS 11+), in a disk image, sparsebundle or on a drive | **experimental**, needs **sudo**. Snapshots are found with `diskutil apfs listSnapshots` and mounted read-only by timak with `mount_apfs -s`. Detection has been tried on a real image, extraction not yet |

For APFS backups, run timak as root: `sudo /opt/homebrew/bin/python3 -m timak …`.
APFS backup volumes are often **case-sensitive**. The disk image timak makes
for a new archive on APFS is case-sensitive; if you use a plain folder
instead, put it on a case-sensitive volume too, otherwise files whose names
differ only by case would collide. `extract` warns when the two don't match.

If a backup drive is still a Time Machine destination, remove it as a
destination (or turn Time Machine off) before extracting from it. Otherwise
Time Machine may start a backup, or delete old snapshots, while timak is
reading.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The integration test builds a small HFS+ disk image with real Time Machine-style
directory hard links. It needs Homebrew's rsync and **Full Disk
Access**, and is skipped without them. A few seconds after mounting, macOS
starts protecting any `Backups.backupdb` folder, even on a test image, and
then refuses reads inside it without Full Disk Access, even for root. timak
warns about this at startup.

## Contributing

Bug reports and pull requests are welcome, through the project's issues and
pull requests.

- In a bug report, give the macOS version, the kind of source (legacy or
  APFS, disk image or drive), the command you ran and its output. A
  `.timakreport` file from the archive often helps. Remove any paths or file
  names you would rather not publish.
- In a pull request, keep changes focused, add or update tests in `tests/`,
  and run the tests above (with Full Disk Access, so the integration test
  runs too) before sending it.
- Changes to the Mac app go in `mac/`; rebuild it with `mac/build.sh` to
  check that it still compiles.

By contributing, you agree that your contribution is shared under the same
licence as the rest of the project.

## Licence

Copyright © Jean-Claude Dufourd.

timak is licensed under the
[Creative Commons Attribution-ShareAlike 4.0 International licence](https://creativecommons.org/licenses/by-sa/4.0/)
(CC BY-SA 4.0); the full text is in [LICENSE](LICENSE). You are free to use, share and adapt it, including
commercially, as long as you:

- **give credit** to the author, link to the licence and say whether you
  changed anything;
- **share alike**: distribute your changed versions under the same licence,
  or a compatible one (the licence allows GPLv3).

## No warranty

timak is provided "as is", without warranty of any kind, express or implied,
including fitness for a particular purpose. It reads backups that may hold
the only copy of your data, and some of its commands change or remove files
in the archive. Use it at your own risk, keep your original backups, and check
the result. The author is not liable for any loss of data or other damage
arising from its use. See section 5 of the licence for the full terms.
