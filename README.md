# macsweep

Reclaim disk space on a Mac without losing anything you care about.

It does two things:

- **Delete** caches and build output that regenerate on their own
- **Archive** cold files to an external drive, leaving a symlink so the
  original path still works while the drive is plugged in

You approve each category before anything happens, and it explains what each
one actually is before it asks.

```
git clone https://github.com/tyrellbaker-blip/macsweep.git
cd macsweep
python3 macsweep.py
```

That's the whole install. No dependencies, one file, macOS and Python 3.8+.

## First time? Do this

```
python3 macsweep.py --scan-only
```

Nothing is created, deleted, or moved. You get a picture of where your space
went and what it *would* offer to do. Read it, then run it for real.

## What it asks you

1. **Which external drive.** It lists what's mounted with free space for each;
   pick a number or type a path.
2. **What to name the archive folder.** Defaults to your computer's name. Files
   keep their original layout inside it, so `~/Movies/big.mov` archives to
   `<drive>/<folder>/Movies/big.mov`.
3. **Each category, one at a time.** Sizes, a sample of what's in it, and an
   explanation of what those files are. Answer y or n.
4. **A final confirmation** before anything is touched.

## The two kinds of action

### DELETE — gone, not in the Trash

Only from a fixed whitelist in the source: Gradle and npm and Maven caches,
Xcode DerivedData and device support, Android emulator images, `Library/Caches`,
`Library/Logs`, and similar. Every one of them is rebuilt or re-downloaded on
demand.

macsweep will **not** delete a file because it looks old, or large, or has an
extension it doesn't recognize. If a path isn't on the list, it isn't deleted.
The check runs again at execution time, so editing the plan can't sneak
something past it.

### ARCHIVE — moved to the drive, symlink left behind

The sequence is copy, verify, then delete:

1. Copy to the external drive
2. Count files and total bytes on both sides
3. **Only if they match exactly**, remove the original and create a symlink

A mismatch leaves the original exactly where it was. There is no path through
the code that deletes a source before its copy has been verified.

**The tradeoff, and it's a real one:** while the drive is unplugged, archived
paths stop resolving. Symlinks dangle. Archive things you're genuinely done
with, not the project you'll open tomorrow. Time Machine also follows symlinks
as links rather than as their targets, so archived content stops being backed
up — if it matters, back up the drive separately.

## Undo

```
python3 macsweep.py --undo
```

Copies archived items back from the drive. Every run writes a log to
`~/.macsweep/runs/`, so you can undo an older one with
`--undo --run 20260910T183000Z.jsonl`.

Deletes can't be undone. That's what the whitelist is for.

## Never touched

Keychains, Preferences, Mail, Messages, Contacts, Calendars, Safari data,
`.ssh`, `.gnupg`, `.aws`, `.config`, and anything under iCloud Drive,
OneDrive, Dropbox, or Google Drive.

Two more categories are measured and reported but never acted on:

- **Live application state** — Docker, UTM, Parallels, Steam, browser profiles,
  conda environments. Symlinking these breaks the app; deleting them loses real
  data.
- **Document bundles** — `.photoslibrary`, `.imovielibrary`, `.logicx`,
  `.fcpbundle`. These look like folders but are single documents. Move them
  from inside Photos or iMovie, never from a shell.

Projects with uncommitted git changes are skipped automatically.

## Two things that make the numbers correct

**Size means blocks on disk, not what a file claims.** A sparse file or an
evicted iCloud placeholder reports its full logical size while occupying
nothing. Measuring `st_size` produces reports wrong by tens of gigabytes — a
Desktop folder that reads as 21G when 475M is actually there. macsweep charges
`st_blocks * 512` and never falls back when that's zero.

**Cloud folders are never walked.** Every dataless placeholder you `stat()` can
trigger a network fetch, and a large iCloud library will stall a scan for
hours. They're skipped by name, and any folder that exceeds a time budget
(90s by default, `--budget`) is abandoned and flagged rather than left to hang.

## Options

| flag | what |
| --- | --- |
| `--scan-only` | report only, change nothing |
| `--undo` | reverse the last run |
| `--drive PATH` | skip the drive prompt |
| `--folder NAME` | skip the folder-name prompt |
| `--budget N` | seconds allowed per folder (default 90) |
| `--cold-days N` | untouched days before a file counts as cold (120) |
| `--downloads-days N` | same, for Downloads (60) |
| `--project-days N` | same, for project folders (180) |
| `--min-mb N` | ignore anything smaller (100) |
| `--big-file-mb N` | size threshold for individual files (250) |
| `--big-dir-mb N` | size threshold for folders (500) |

## If macOS blocks it

Some folders need Full Disk Access. If you see permission errors, add Terminal
under System Settings → Privacy & Security → Full Disk Access, then quit and
reopen Terminal.

## What it can't reach

Space outside your home folder — `/Applications`, `/Library`, Homebrew in
`/opt` — isn't touched. If the scan doesn't add up to what System Settings
reports, look there:

```
sudo du -xhd 1 /System/Volumes/Data 2>/dev/null | sort -rh | head -20
```

Common finds: an oversized Homebrew or Anaconda install in `/opt`, a runaway
Spotlight index in `~/Library/Metadata/CoreSpotlight`, and Messages attachments
in `~/Library/Messages/Attachments` (which macsweep leaves alone, because
Messages tracks those files in a database and deleting them behind its back
breaks conversations).

## License

MIT
