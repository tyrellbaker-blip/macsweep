# macsweep

Reclaim disk space on a Mac without losing anything you care about.

It does four things:

- **Delete** caches and build output that regenerate on their own
- **Archive** cold files — including documents — to an external drive,
  leaving a symlink so the original path still works while the drive is
  plugged in
- **Relocate** photo, video and music libraries to the drive, the way Apple
  intends them to be moved
- **Report** everything big it won't touch itself, with the exact command
  that reclaims it

You approve each category before anything happens, and it explains what each
one actually is before it asks.

A narrow whitelist keeps the automatic deletion safe, but narrow must not
mean silent: the biggest wins on a real machine are usually things no script
should delete on its own — Homebrew and Anaconda in `/opt`, an oversized
Spotlight index, old iPhone backups, a Windows game inside a CrossOver bottle,
`/Applications` itself. macsweep measures all of it and hands you the command.
It will never quietly omit a number because it decided not to act on it.

```
git clone https://github.com/tyrellbaker-blip/macsweep.git
cd macsweep
python3 macsweep.py
```

## First time

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

## The three actions it takes itself

### DELETE — gone, not in the Trash bin

Only from a fixed whitelist in the source: Gradle and npm and Maven caches,
Xcode DerivedData and device support, Android emulator images, `Library/Caches`,
`Library/Logs`, downloaded browser binaries, and the Trash itself. Every one of
them is rebuilt or re-downloaded on demand.

It also finds build output **inside your projects** — `node_modules`, `target`,
`Pods`, `.next`, `build`. This is probably the single
largest recoverable chunk on a developer's machine. Each one is only counted
when a sibling file proves what it is: `node_modules` beside `package.json`,
`target` beside `Cargo.toml`, `build` beside `build.gradle`. A folder named
`build` that is actually source is left alone, and macsweep never descends
into build output to find more build output.

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

This covers cold **documents and folders** too — old tax paperwork, finished
coursework, the folder from a job you left. Nothing is judged by what's inside
it, only by how long since you touched it, so read the list before approving.

**The tradeoff:** while the drive is unplugged, archived
paths stop resolving. Symlinks dangle. Archive things you're genuinely done
with, not the project you'll open tomorrow. Time Machine also follows symlinks
as links rather than as their targets, so archived content stops being backed
up — if it matters, back up the drive separately.

### RELOCATE — for photo, video and music libraries

A Photos or iMovie or Logic library is usually the largest single thing a
non-developer owns, and moving one to an external drive is a supported,
documented Apple workflow. But these bundles record their own location, so a
symlink breaks them. They get their own action: copy, verify, remove the
original, and deliberately **no link**. macsweep then prints the one-time
reopen step:

| library | how to reopen it |
| --- | --- |
| Photos | hold Option while opening Photos, choose it on the drive |
| iMovie | File > Open Library > Other |
| Music | hold Option while opening Music |
| Final Cut | File > Open Library > Other |
| Logic | open the project from its new location |

After that the app remembers. With the drive unplugged the app says it can't
find its library — it isn't damaged, it's on the drive.

Two things to know: if it's your System Photo Library and you use iCloud
Photos, keep the drive connected while Photos is open. And macsweep refuses to
move a library while its app is running, because moving one out from under a
running app corrupts it.

`ARCHIVE` refuses library bundles and `RELOCATE` refuses anything that isn't
one, so neither can be used to do the other's job by mistake.

## Undo

```
python3 macsweep.py --undo
```

Copies archived and relocated items back from the drive. Every run writes a log
to `~/.macsweep/runs/`, so you can undo an older one with
`--undo --run 20260910T183000Z.jsonl`.

Deletes can't be undone. That's what the whitelist is for.

## Never touched — but always measured

Keychains, Preferences, Mail, Messages, Contacts, Calendars, Safari data,
`.ssh`, `.gnupg`, `.aws`, `.config`, and anything under iCloud Drive,
OneDrive, Dropbox, or Google Drive.

These are excluded from **actions**, not from the **report**. If your Mail
cache is 7G, that line appears with the setting that shrinks it. An earlier
version skipped measuring protected paths entirely, which meant a 9G folder
could sit there invisible while the tool congratulated itself on finding 6G of
cache. Refusing to act on something is a safety feature; refusing to mention
it is a blind spot.

The only exception is cloud folders, which genuinely cannot be walked — every
dataless placeholder you `stat()` may trigger a download. Those are named and
counted, not sized, with a pointer to the sync app's own eviction control.

Live application state is measured and reported but never acted on: Docker,
UTM, Parallels, Steam, browser profiles, conda environments. Symlinking these
breaks the app; deleting them loses real data.

Projects with uncommitted git changes are skipped automatically, and so are
documents and folders you've touched recently.

## Two things that make the numbers correct

**Size means blocks on disk, not what a file claims.** A sparse file or an
evicted iCloud placeholder reports its full logical size while occupying
nothing. Measuring `st_size` produces reports wrong by tens of gigabytes — a
Desktop folder that reads as 21G when 475M is actually there. macsweep charges
`st_blocks * 512` and never falls back when that's zero.

**Cloud folders are never walked.** Every dataless placeholder you `stat()` can
trigger a network fetch, and a large iCloud library will stall a scan for
hours. They're skipped by name, and any folder that exceeds a time budget
(6 minutes by default, `--budget`) is abandoned and flagged rather than left to hang.

## Check it yourself before you trust it

```
python3 selftest.py
```

Builds a fake home folder and a fake drive in a temp directory, runs the real
code against them, and checks what actually happened — 44 assertions covering
sparse-file accounting, the build-output guard rules and their traps, the
refusals, verify-before-delete, the relocate path, and undo. Your own files are
never involved; `HOME` is redirected for the duration. Add `--keep` to leave
the sandbox behind and poke at it.

It's worth running once: two real bugs in this tool were found by it rather
than by a user's disk.

## Options

| flag | what |
| --- | --- |
| `--scan-only` | report only, change nothing |
| `--undo` | reverse the last run |
| `--drive PATH` | skip the drive prompt |
| `--folder NAME` | skip the folder-name prompt |
| `--budget N` | seconds allowed per folder (default 360, i.e. 6 minutes) |
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

## Outside your home folder

macsweep won't *modify* anything outside your home folder, but it does look:
`/Applications`, `/opt/homebrew`, `/opt/anaconda3`, `/Library/Updates`,
`/Library/Application Support`. Anything over 1G shows up in the review
section with its command — `brew cleanup --prune=all`, `conda clean --all`,
and so on.

It also checks for local Time Machine snapshots. Those pin blocks you've
already freed, so deleting things can appear to accomplish nothing until
they're thinned. If any exist you get:

```
sudo tmutil thinlocalsnapshots / 100000000000 4
```

If the numbers still don't add up to what System Settings reports:

```
sudo du -xhd 1 /System/Volumes/Data 2>/dev/null | sort -rh | head -20
```

One thing macsweep deliberately won't automate: Messages attachments in
`~/Library/Messages/Attachments`. Messages tracks those files in a database,
so deleting them behind its back breaks conversations. It's reported with two
safe routes — the retention setting, or copying the folder to your drive
first if you want to keep them.

## License

MIT
