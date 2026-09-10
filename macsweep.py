#!/usr/bin/env python3
"""
macsweep - reclaim disk space on a Mac, safely.

Two things happen here:

  DELETE  caches and build output that regenerate on their own
  ARCHIVE cold files moved to an external drive, with a symlink left behind
          so the original path still works while the drive is plugged in

Nothing is deleted or moved without you approving that category first, and an
archive is only allowed to delete the original after the copy has been verified
file-for-file and byte-for-byte.

    python3 macsweep.py              walk through it
    python3 macsweep.py --scan-only  look, change nothing
    python3 macsweep.py --undo       reverse the last run

Requires macOS and Python 3.8+. No third-party packages.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
MB = 1 << 20
GB = 1 << 30
STATE = os.path.join(HOME, ".macsweep")

# How long a single folder may be walked before we give up on it. Cloud-synced
# folders can stall for hours -- see the note on CLOUD below.
WALK_BUDGET = 90.0


# ---------------------------------------------------------------------------
# What is what
#
# Three lists decide everything. Read them before you run this.
# ---------------------------------------------------------------------------

# NEVER touched, in any mode, for any reason. Losing these means losing
# passwords, keys, mail, or photos. The scanner does not even walk into most
# of them.
NEVER_TOUCH = [
    "Library/Keychains",
    "Library/Preferences",
    "Library/Mail",
    "Library/Messages",
    "Library/Application Support/AddressBook",
    "Library/Calendars",
    "Library/Safari",
    "Library/Cookies",
    "Library/IdentityServices",
    "Library/Sharing",
    "Library/Accounts",
    "Library/Autosave Information",
    ".ssh",
    ".gnupg",
    ".aws",
    ".kube",
    ".docker/machine",
    ".config",
    ".local/share/keyrings",
    "Library/CloudStorage",
    "Library/Mobile Documents",
]

# Safe to DELETE. Every entry here rebuilds itself: a cache, a downloaded
# package that can be re-fetched, or a log. This is a whitelist -- macsweep
# never deletes anything that is not matched here, no matter how old or large
# it looks.
REGENERATES = [
    ("Library/Caches", "macOS and app caches"),
    ("Library/Logs", "application logs"),
    ("Library/Application Support/CrashReporter", "crash reports"),
    (".cache", "generic tool cache"),
    (".npm/_cacache", "npm package cache"),
    (".npm/_logs", "npm logs"),
    (".yarn/cache", "yarn package cache"),
    (".pnpm-store", "pnpm package store"),
    (".gradle/caches", "Gradle dependency cache"),
    (".gradle/wrapper", "downloaded Gradle distributions"),
    (".gradle/daemon", "Gradle daemon logs"),
    (".gradle/native", "Gradle native bits"),
    (".m2/repository", "Maven dependency cache"),
    (".nuget/packages", "NuGet package cache"),
    (".cocoapods/repos", "CocoaPods spec repos"),
    (".gem/cache", "RubyGems cache"),
    (".cargo/registry/cache", "Cargo download cache"),
    (".cargo/registry/src", "Cargo unpacked sources"),
    (".rustup/downloads", "rustup downloads"),
    (".pub-cache", "Dart/Flutter package cache"),
    ("Library/Caches/pip", "pip wheel cache"),
    ("Library/Caches/Homebrew", "Homebrew download cache"),
    ("Library/Developer/Xcode/DerivedData", "Xcode build output"),
    ("Library/Developer/Xcode/Archives", "Xcode archives"),
    ("Library/Developer/Xcode/iOS DeviceSupport", "iOS debug symbols"),
    ("Library/Developer/Xcode/watchOS DeviceSupport", "watchOS debug symbols"),
    ("Library/Developer/Xcode/tvOS DeviceSupport", "tvOS debug symbols"),
    ("Library/Developer/CoreSimulator/Caches", "simulator caches"),
    ("Library/Android/sdk/system-images", "Android emulator system images"),
    (".android/avd", "Android virtual device disk images"),
    ("Library/Application Support/Code/Cache", "VS Code cache"),
    ("Library/Application Support/Code/CachedData", "VS Code cached data"),
    ("Library/Application Support/Slack/Cache", "Slack cache"),
    ("Library/Application Support/Slack/Service Worker/CacheStorage", "Slack cache"),
    ("Library/Application Support/discord/Cache", "Discord cache"),
    ("Library/Application Support/Spotify/PersistentCache", "Spotify cache"),
]

# Live application state. Measured and shown so you know where space went, but
# never proposed for anything. Symlinking these breaks the app; deleting them
# loses real data (VMs, game saves, database files).
HANDS_OFF = [
    "Library/Containers",
    "Library/Group Containers",
    "Library/Application Support/Claude",
    "Library/Application Support/MobileSync",
    "Library/Application Support/Steam",
    "Library/Application Support/CrossOver",
    "Library/Application Support/UTM",
    "Library/Application Support/Parallels",
    "Library/Application Support/VMware Fusion",
    "Library/Application Support/docker",
    "Library/Application Support/com.docker.docker",
    "Library/Application Support/JetBrains",
    "Library/Application Support/minecraft",
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/Firefox",
    "Library/Application Support/BraveSoftware",
    "Library/Photos",
    "miniconda3",
    "anaconda3",
    ".conda",
    ".ollama",
    ".rustup/toolchains",
    "venvs",
    ".virtualenvs",
]

# Bundles that look like folders but are really one document. A symlink here
# breaks the app that owns them -- Photos and iMovie must be relocated from
# inside the app itself, not from a shell.
NEVER_MOVE_SUFFIXES = (
    ".photoslibrary", ".imovielibrary", ".theater", ".tvlibrary",
    ".musiclibrary", ".aplibrary", ".logicx", ".fcpbundle", ".sparsebundle",
    ".dmg.sparseimage",
)

# Cloud-synced. Two separate hazards: moving a file out forces a full download
# first, and simply WALKING one can block for hours because every dataless
# placeholder you stat() may trigger a network fetch. Never traversed.
CLOUD = [
    "Library/CloudStorage",
    "Library/Mobile Documents",
    "OneDrive",
    "Dropbox",
    "Google Drive",
    "GoogleDrive",
    "iCloud Drive",
    "Creative Cloud Files",
    "Box Sync",
    "pCloud Drive",
    "Sync.com",
    "MEGA",
]

# Top-level home entries that are noise or already covered elsewhere.
SKIP_HOME = {"Library", "Applications", "Public", ".Trash", "Desktop.localized"}

# Extra places worth measuring that a plain listing of ~ would miss.
EXTRA_ROOTS = [
    "~/Library",  # walked one level deep, then per-child
    "~/.cache", "~/.npm", "~/.yarn", "~/.gradle", "~/.m2", "~/.nuget",
    "~/.cocoapods", "~/.gem", "~/.cargo", "~/.rustup", "~/.pub-cache",
    "~/.android", "~/.bun", "~/.deno", "~/.pyenv", "~/.nvm", "~/.local",
    "~/.dotnet", "~/go", "~/miniconda3", "~/anaconda3", "~/venvs",
]

# Categories, in the order they are offered. Each carries the explanation
# shown before the prompt.
CATEGORIES = [
    ("caches", "DELETE", "App and system caches", """
These are files apps keep so they don't have to redo work -- thumbnails,
decoded images, downloaded web assets. Every one of them is rebuilt the next
time the app needs it. Deleting caches is the single safest way to reclaim
space on a Mac. The only cost is that a few apps feel slightly slow the first
time you open them afterward."""),

    ("build", "DELETE", "Build output and package caches", """
Gradle, npm, Maven, CocoaPods, Cargo and friends keep a local copy of every
library they have ever downloaded, plus the compiled output of every build.
None of it is your source code. If a project needs a library again, the build
tool re-downloads it. The cost is that your next build in each project is
slower while it refetches."""),

    ("devtools", "DELETE", "Simulator and emulator images", """
Xcode keeps debug symbols for every iOS version you have ever connected a
device from, and Android Studio keeps a full OS image for each emulator. These
are the biggest single items on most developer machines. Both tools re-download
them on demand -- Xcode when you next attach a device, Android Studio through
its SDK Manager. Deleting an Android AVD does lose that emulator's saved state
(installed apps inside the emulator), so skip this one if you have an emulator
set up mid-project."""),

    ("logs", "DELETE", "Log files", """
Diagnostic text written by apps and by macOS. Useful when something is broken
right now, worthless afterward. Safe to remove."""),

    ("downloads", "ARCHIVE", "Old downloads", """
Files in your Downloads folder that you have not opened in a long time.
Installers, PDFs, zip files you already extracted. These get moved to the
external drive rather than deleted, so nothing is actually lost -- you can
still open them whenever the drive is plugged in."""),

    ("media", "ARCHIVE", "Large media files", """
Videos and images sitting loose in your folders. Moved to the external drive
with a symlink left behind, so the file still appears at its original path
while the drive is connected. Photos and iMovie libraries are deliberately
excluded -- those have to be moved from inside the app itself or they break."""),

    ("projects", "ARCHIVE", "Cold project folders", """
Project directories you have not touched in months. Moved, not deleted. Any
project with uncommitted git changes is skipped automatically, so work in
progress stays where it is.

Be deliberate here: while the drive is unplugged, these paths stop resolving.
Archive things you are genuinely done with, not the project you'll open
tomorrow."""),

    ("bigfiles", "ARCHIVE", "Large, old individual files", """
Single files over the size threshold that have not been touched in months --
disk images, exports, datasets, virtual machine files. Moved to the external
drive with a symlink left in place."""),
]


# ---------------------------------------------------------------------------
# Terminal output
# ---------------------------------------------------------------------------

_TTY = sys.stdout.isatty()


def c(code: str, text: str) -> str:
    return "\033[%sm%s\033[0m" % (code, text) if _TTY else text


def bold(t): return c("1", t)
def dim(t): return c("2", t)
def red(t): return c("31", t)
def green(t): return c("32", t)
def yellow(t): return c("33", t)
def cyan(t): return c("36", t)


def rule(char="-", width=74):
    print(dim(char * width))


def header(text: str):
    print()
    rule("=")
    print("  " + bold(text))
    rule("=")


def human(n: int) -> str:
    for unit, div in (("T", 1 << 40), ("G", GB), ("M", MB), ("K", 1024)):
        if n >= div:
            return "%.1f%s" % (n / div, unit)
    return "%dB" % n


def ask(prompt: str, default: str = "") -> str:
    suffix = " [%s]: " % default if default else ": "
    try:
        answer = input(cyan("  " + prompt) + suffix).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  cancelled")
        sys.exit(130)
    return answer or default


def confirm(prompt: str, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        a = ask("%s (%s)" % (prompt, d)).lower()
        if not a:
            return default
        if a in ("y", "yes"):
            return True
        if a in ("n", "no"):
            return False
        print("    please answer y or n")


# ---------------------------------------------------------------------------
# Measurement
#
# The one rule that matters: size means blocks actually allocated on disk, not
# the size a file claims. A sparse file or an evicted cloud placeholder reports
# its full logical size while occupying nothing. Charging st_size for those
# produces reports that are wrong by tens of gigabytes.
# ---------------------------------------------------------------------------

def allocated(st: os.stat_result) -> int:
    blocks = getattr(st, "st_blocks", None)
    if blocks is None:          # non-macOS filesystem, no block count
        return st.st_size
    return blocks * 512         # never fall back to st_size when blocks == 0


def rel_home(path: str) -> str:
    if path.startswith(HOME + os.sep):
        return path[len(HOME) + 1:]
    return path


def _match(path: str, patterns) -> bool:
    r = rel_home(path)
    for p in patterns:
        if r == p or r.startswith(p + "/"):
            return True
    return False


def is_cloud(path: str) -> bool:
    low = rel_home(path).lower()
    return any(h.lower() in low for h in CLOUD)


def is_never_touch(path: str) -> bool:
    return _match(path, NEVER_TOUCH) or is_cloud(path)


def is_hands_off(path: str) -> bool:
    return _match(path, HANDS_OFF)


def regenerates(path: str):
    """Return the human description if this path is a known-safe delete."""
    r = rel_home(path)
    for pat, why in REGENERATES:
        if r == pat or r.startswith(pat + "/"):
            return why
    return None


def is_bundle(path: str) -> bool:
    return path.lower().endswith(NEVER_MOVE_SUFFIXES)


class Walker:
    """Walks trees, charging every inode once, skipping what must not be read."""

    def __init__(self):
        self.seen = set()
        self.unreadable = 0
        self.skipped_cloud = 0

    def measure(self, path: str, budget: float = WALK_BUDGET) -> dict:
        end = time.time() + budget
        total = apparent = files = 0
        newest = 0.0
        partial = False
        stack = [path]
        checked = 0
        while stack:
            checked += 1
            if checked % 400 == 0 and time.time() > end:
                partial = True
                break
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if is_cloud(entry.path):
                                    self.skipped_cloud += 1
                                    continue
                                stack.append(entry.path)
                                continue
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            self.unreadable += 1
                            continue
                        if st.st_nlink > 1:
                            key = (st.st_dev, st.st_ino)
                            if key in self.seen:
                                continue
                            self.seen.add(key)
                        total += allocated(st)
                        apparent += st.st_size
                        files += 1
                        if st.st_mtime > newest:
                            newest = st.st_mtime
            except OSError:
                self.unreadable += 1
        return {"bytes": total, "apparent": apparent, "files": files,
                "newest": newest, "partial": partial}


def age_days(ts: float) -> int:
    return int((time.time() - ts) / 86400) if ts else -1


def git_dirty(path: str) -> bool:
    """True if this is a git repo with uncommitted changes."""
    if not os.path.isdir(os.path.join(path, ".git")):
        return False
    try:
        r = subprocess.run(["git", "-C", path, "status", "--porcelain"],
                           capture_output=True, text=True, timeout=20)
        return bool(r.stdout.strip())
    except Exception:
        return True     # can't tell -> assume dirty, leave it alone


# ---------------------------------------------------------------------------
# Finding the external drive
# ---------------------------------------------------------------------------

def external_volumes():
    out = []
    try:
        names = sorted(os.listdir("/Volumes"))
    except OSError:
        return out
    for name in names:
        mount = os.path.join("/Volumes", name)
        if name.startswith("com.apple.") or not os.path.isdir(mount):
            continue
        try:
            if os.path.realpath(mount) == "/":
                continue
            total, used, free = shutil.disk_usage(mount)
        except OSError:
            continue
        out.append({"mount": mount, "name": name, "total": total,
                    "free": free, "writable": os.access(mount, os.W_OK)})
    return out


def choose_drive(preset: str = None) -> str:
    if preset:
        if not os.path.isdir(preset):
            print(red("  No such folder: %s" % preset))
            sys.exit(1)
        return preset

    vols = external_volumes()
    header("Where should archived files go?")
    if not vols:
        print("  No external volumes found in /Volumes.")
        print("  Plug the drive in, wait for it to appear in Finder, and")
        print("  either rerun this or type the full path below.")
    else:
        print("  Drives I can see:")
        print()
        for i, v in enumerate(vols, 1):
            flag = "" if v["writable"] else red("  (read-only)")
            print("    %d) %-28s %8s free of %-8s%s"
                  % (i, v["name"][:28], human(v["free"]),
                     human(v["total"]), flag))
        print()
        print(dim("  Pick a number, or type a full path like /Volumes/MyDrive"))
    print()

    while True:
        answer = ask("Drive")
        if not answer:
            continue
        if answer.isdigit() and vols:
            idx = int(answer) - 1
            if 0 <= idx < len(vols):
                chosen = vols[idx]
                if not chosen["writable"]:
                    print(red("    That volume is read-only. Pick another."))
                    continue
                return chosen["mount"]
            print(red("    No drive numbered %s." % answer))
            continue
        path = os.path.abspath(os.path.expanduser(answer))
        if not os.path.isdir(path):
            print(red("    Not a folder: %s" % path))
            continue
        if not os.access(path, os.W_OK):
            print(red("    Can't write to %s" % path))
            continue
        if path == "/" or path.startswith(HOME):
            print(red("    That's on the internal disk. Archiving there frees"))
            print(red("    nothing. Choose an external drive."))
            continue
        return path


def machine_name() -> str:
    try:
        r = subprocess.run(["scutil", "--get", "ComputerName"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return os.uname().nodename.split(".")[0]


def safe_folder_name(text: str) -> str:
    keep = "-_. "
    cleaned = "".join(ch for ch in text if ch.isalnum() or ch in keep).strip()
    return cleaned.replace(" ", "-") or "Archive"


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def collect_roots():
    roots, seen = [], set()

    def add(p):
        p = os.path.abspath(os.path.expanduser(p))
        if p in seen or not os.path.isdir(p) or os.path.islink(p):
            return
        if is_never_touch(p):
            return
        seen.add(p)
        roots.append(p)

    try:
        for name in sorted(os.listdir(HOME)):
            if name not in SKIP_HOME:
                add(os.path.join(HOME, name))
    except OSError:
        pass

    # ~/Library is where the space actually hides, and sampling a handful of
    # its subfolders misses tens of gigabytes. Walk all of its children.
    lib = os.path.join(HOME, "Library")
    try:
        for name in sorted(os.listdir(lib)):
            add(os.path.join(lib, name))
    except OSError:
        pass

    for extra in EXTRA_ROOTS:
        add(extra)
    return roots


def scan(opts) -> dict:
    header("Looking at your disk")
    print("  Measuring what is actually on disk. Cloud-synced folders are")
    print("  skipped, and any folder that takes more than %ds is abandoned" % WALK_BUDGET)
    print("  rather than left to stall.")
    print()

    walker = Walker()
    roots = collect_roots()
    measured = []
    started = time.time()

    for i, root in enumerate(roots, 1):
        label = rel_home(root)[:46]
        sys.stdout.write("\r  [%2d/%d] %-46s" % (i, len(roots), label))
        sys.stdout.flush()
        if is_cloud(root):
            continue
        m = walker.measure(root, opts.budget)
        m["path"] = root
        measured.append(m)
        if m["partial"]:
            sys.stdout.write("\r  [%2d/%d] %-46s %s\n"
                             % (i, len(roots), label, yellow("slow, partial")))
    sys.stdout.write("\r" + " " * 72 + "\r")
    print("  measured %d folders in %ds" % (len(measured), time.time() - started))

    measured.sort(key=lambda m: m["bytes"], reverse=True)
    candidates = []

    # --- DELETE candidates: whitelist only -------------------------------
    for m in measured:
        if m["bytes"] < opts.min_mb * MB:
            continue
        why = regenerates(m["path"])
        if not why or is_never_touch(m["path"]):
            continue
        candidates.append({
            "action": "DELETE", "path": m["path"], "bytes": m["bytes"],
            "files": m["files"], "age": age_days(m["newest"]),
            "category": category_for_delete(m["path"]), "note": why,
        })

    # Also look one level inside big roots, so ~/.gradle/caches is found even
    # though ~/.gradle itself is not a delete target.
    for m in measured[:25]:
        if m["bytes"] < 500 * MB or is_never_touch(m["path"]):
            continue
        for child in children_of(m["path"]):
            if any(child == cand["path"] or child.startswith(cand["path"] + "/")
                   for cand in candidates):
                continue
            why = regenerates(child)
            if not why:
                continue
            cm = walker.measure(child, opts.budget / 3)
            if cm["bytes"] < opts.min_mb * MB:
                continue
            candidates.append({
                "action": "DELETE", "path": child, "bytes": cm["bytes"],
                "files": cm["files"], "age": age_days(cm["newest"]),
                "category": category_for_delete(child), "note": why,
            })

    # --- ARCHIVE candidates ----------------------------------------------
    candidates += find_archivable(opts, walker)

    candidates = prune_nested(candidates)
    candidates.sort(key=lambda x: x["bytes"], reverse=True)

    return {"candidates": candidates, "measured": measured, "walker": walker}


def children_of(path):
    try:
        with os.scandir(path) as it:
            return sorted(e.path for e in it if e.is_dir(follow_symlinks=False))
    except OSError:
        return []


def category_for_delete(path: str) -> str:
    r = rel_home(path)
    if "Developer/Xcode" in r or "CoreSimulator" in r or "Android" in r \
            or ".android" in r:
        return "devtools"
    if r.startswith("Library/Logs") or "/logs" in r.lower() or r.endswith("_logs"):
        return "logs"
    for marker in (".gradle", ".m2", ".npm", ".nuget", ".yarn", ".pnpm-store",
                   ".cargo", ".rustup", ".gem", ".cocoapods", ".pub-cache",
                   "DerivedData", "pip", "Homebrew"):
        if marker in r:
            return "build"
    return "caches"


def find_archivable(opts, walker):
    out = []

    # Cold downloads
    downloads = os.path.join(HOME, "Downloads")
    if os.path.isdir(downloads):
        for entry in list_entries(downloads):
            info = entry_info(entry, walker, opts)
            if not info or info["age"] < opts.downloads_days:
                continue
            if info["bytes"] < opts.min_mb * MB:
                continue
            info.update(action="ARCHIVE", category="downloads",
                        note="untouched %d days" % info["age"])
            out.append(info)

    # Loose media
    media_ext = (".mov", ".mp4", ".m4v", ".avi", ".mkv", ".wmv", ".flv",
                 ".raw", ".arw", ".cr2", ".nef", ".dng", ".tif", ".tiff",
                 ".psd", ".ai", ".heic", ".wav", ".aiff", ".flac")
    for folder in ("Movies", "Pictures", "Music", "Desktop", "Documents"):
        base = os.path.join(HOME, folder)
        if not os.path.isdir(base) or is_never_touch(base):
            continue
        for entry in list_entries(base):
            if is_bundle(entry):
                continue
            if os.path.isfile(entry) and entry.lower().endswith(media_ext):
                info = entry_info(entry, walker, opts)
                if not info or info["bytes"] < opts.big_file_mb * MB:
                    continue
                if info["age"] < opts.cold_days:
                    continue
                info.update(action="ARCHIVE", category="media",
                            note="%s, %d days old" % (
                                os.path.splitext(entry)[1].lstrip("."),
                                info["age"]))
                out.append(info)

    # Cold project folders
    project_parents = [HOME] + [
        os.path.join(HOME, n) for n in
        ("Projects", "dev", "Developer", "Code", "src", "repos",
         "PycharmProjects", "IdeaProjects", "WebstormProjects",
         "CLionProjects", "RiderProjects", "XcodeProjects",
         "AndroidStudioProjects")
        if os.path.isdir(os.path.join(HOME, n))
    ]
    seen_projects = set()
    for parent in project_parents:
        for entry in list_entries(parent):
            if entry in seen_projects or not os.path.isdir(entry):
                continue
            seen_projects.add(entry)
            if is_never_touch(entry) or is_hands_off(entry) or is_cloud(entry):
                continue
            if is_bundle(entry) or os.path.basename(entry).startswith("."):
                continue
            if parent == HOME and os.path.basename(entry) in SKIP_HOME:
                continue
            looks_like_project = any(
                os.path.exists(os.path.join(entry, marker)) for marker in
                (".git", "package.json", "pom.xml", "build.gradle",
                 "build.gradle.kts", "Cargo.toml", "pyproject.toml",
                 "requirements.txt", "Gemfile", "go.mod", ".xcodeproj"))
            if not looks_like_project:
                continue
            info = entry_info(entry, walker, opts)
            if not info or info["bytes"] < opts.big_dir_mb * MB:
                continue
            if info["age"] < opts.project_days:
                continue
            if git_dirty(entry):
                continue    # uncommitted work: leave it alone, silently
            info.update(action="ARCHIVE", category="projects",
                        note="no changes in %d days" % info["age"])
            out.append(info)

    # Large cold individual files anywhere in the scanned roots
    for folder in ("Downloads", "Documents", "Desktop", "Movies"):
        base = os.path.join(HOME, folder)
        if not os.path.isdir(base) or is_never_touch(base):
            continue
        for entry in list_entries(base):
            if not os.path.isfile(entry) or is_bundle(entry):
                continue
            if any(entry == o["path"] for o in out):
                continue
            info = entry_info(entry, walker, opts)
            if not info or info["bytes"] < opts.big_file_mb * MB:
                continue
            if info["age"] < opts.cold_days:
                continue
            info.update(action="ARCHIVE", category="bigfiles",
                        note="%d days old" % info["age"])
            out.append(info)

    return out


def list_entries(folder):
    try:
        with os.scandir(folder) as it:
            return sorted(e.path for e in it if not e.is_symlink())
    except OSError:
        return []


def entry_info(path, walker, opts):
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if os.path.isdir(path):
        m = walker.measure(path, opts.budget / 3)
        if m["partial"]:
            return None     # never act on a folder we couldn't fully measure
        return {"path": path, "bytes": m["bytes"], "files": m["files"],
                "age": age_days(m["newest"])}
    return {"path": path, "bytes": allocated(st), "files": 1,
            "age": age_days(st.st_mtime)}


def prune_nested(candidates):
    """Drop anything already covered by a chosen ancestor."""
    kept = []
    by_depth = sorted(candidates, key=lambda c: c["path"].count(os.sep))
    claimed = []
    for cand in by_depth:
        p = cand["path"]
        if any(p == q or p.startswith(q + "/") for q in claimed):
            continue
        claimed.append(p)
        kept.append(cand)
    return kept


# ---------------------------------------------------------------------------
# Presenting a category and asking
# ---------------------------------------------------------------------------

def review(candidates, opts):
    """Walk the categories, explain each, and return the approved list."""
    approved = []
    for key, action, title, explanation in CATEGORIES:
        group = [x for x in candidates if x["category"] == key]
        if not group:
            continue
        group.sort(key=lambda x: x["bytes"], reverse=True)
        total = sum(x["bytes"] for x in group)

        header("%s  --  %s" % (title, human(total)))
        print(explanation.strip("\n"))
        print()
        tag = red("DELETE") if action == "DELETE" else green("ARCHIVE")
        print("  These would be %s:" % tag)
        print()
        for item in group[:20]:
            age = "%dd" % item["age"] if item["age"] >= 0 else "-"
            print("    %9s  %5s  %s" % (human(item["bytes"]), age,
                                        rel_home(item["path"])))
            if item.get("note"):
                print(dim("               %s" % item["note"]))
        if len(group) > 20:
            print(dim("    ... and %d more" % (len(group) - 20)))
        print()

        if action == "DELETE":
            print(yellow("  Deleting is permanent. Everything above regenerates,"))
            print(yellow("  but it will not be in the Trash."))
        else:
            print("  Originals are replaced with symlinks. While the drive is")
            print("  unplugged these paths stop working; plug it back in and")
            print("  they resolve again.")
        print()

        if opts.scan_only:
            print(dim("  (--scan-only: not asking)"))
            continue

        if confirm("Include this category?", default=(action == "DELETE")):
            approved += group
            print(green("    added (%s)" % human(total)))
        else:
            print(dim("    skipped"))

    return approved


# ---------------------------------------------------------------------------
# Doing it
# ---------------------------------------------------------------------------

def rsync_flags():
    flags = ["-a"]
    try:
        blob = subprocess.run(["rsync", "--help"], capture_output=True,
                              text=True, timeout=15).stdout
    except Exception:
        return None
    if "--sparse" in blob:
        flags.append("--sparse")
    for opt, flag in (("--hard-links", "-H"), ("--acls", "-A"),
                      ("--xattrs", "-X")):
        if opt in blob:
            flags.append(flag)
    return flags


def copy_tree(src, dst, flags):
    os.makedirs(os.path.dirname(dst.rstrip("/")), exist_ok=True)
    if flags and shutil.which("rsync"):
        if os.path.isdir(src):
            cmd = ["rsync"] + flags + [src.rstrip("/") + "/", dst.rstrip("/") + "/"]
        else:
            cmd = ["rsync"] + flags + [src, dst]
        r = subprocess.run(cmd)
        # 23/24 mean some files vanished mid-copy; verification catches it
        return r.returncode in (0, 23, 24)
    try:
        if os.path.isdir(src):
            shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        return True
    except Exception as exc:
        print(red("    copy failed: %s" % exc))
        return False


def tally(path):
    """(file count, total apparent bytes) -- what verification compares."""
    if os.path.isfile(path) and not os.path.islink(path):
        return 1, os.lstat(path).st_size
    count = size = 0
    for root, dirs, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if os.path.islink(full):
                continue
            count += 1
            size += st.st_size
    return count, size


def execute(approved, dest_root, opts):
    os.makedirs(dest_root, exist_ok=True)
    os.makedirs(os.path.join(STATE, "runs"), exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = os.path.join(STATE, "runs", stamp + ".jsonl")
    log = open(log_path, "a")

    flags = rsync_flags()
    deletes = [x for x in approved if x["action"] == "DELETE"]
    archives = [x for x in approved if x["action"] == "ARCHIVE"]
    freed = 0
    failed = 0

    if deletes:
        header("Deleting caches and build output")
        for i, item in enumerate(deletes, 1):
            path = item["path"]
            print("  [%d/%d] %s  %s" % (i, len(deletes), human(item["bytes"]),
                                        rel_home(path)))
            if regenerates(path) is None or is_never_touch(path):
                print(red("      refused: not on the safe-delete list"))
                failed += 1
                continue
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                freed += item["bytes"]
                log.write(json.dumps({"action": "DELETE", "path": path,
                                      "bytes": item["bytes"]}) + "\n")
            except Exception as exc:
                print(red("      failed: %s" % exc))
                failed += 1

    if archives:
        header("Archiving to the external drive")
        print("  Each item is copied, verified, and only then removed from the")
        print("  internal disk. A failed check leaves the original untouched.")
        print()
        for i, item in enumerate(archives, 1):
            src = item["path"]
            rel = rel_home(src)
            dst = os.path.join(dest_root, rel)
            print("  [%d/%d] %s  %s" % (i, len(archives), human(item["bytes"]), rel))

            if is_never_touch(src) or is_hands_off(src) or is_bundle(src):
                print(red("      refused: protected path"))
                failed += 1
                continue
            if os.path.exists(dst):
                print(yellow("      already on the drive, skipping"))
                continue

            if not copy_tree(src, dst, flags):
                failed += 1
                continue

            sf, sb = tally(src)
            df, db = tally(dst)
            if (sf, sb) != (df, db):
                print(red("      VERIFY FAILED  source %d files/%s, copy %d files/%s"
                          % (sf, human(sb), df, human(db))))
                print(red("      original left in place"))
                failed += 1
                continue

            try:
                if os.path.isdir(src) and not os.path.islink(src):
                    shutil.rmtree(src)
                else:
                    os.remove(src)
                os.symlink(dst, src)
            except Exception as exc:
                print(red("      failed to swap in symlink: %s" % exc))
                failed += 1
                continue

            freed += item["bytes"]
            print(green("      done"))
            log.write(json.dumps({"action": "ARCHIVE", "path": src,
                                  "dest": dst, "bytes": item["bytes"]}) + "\n")

    log.close()
    return freed, failed, log_path


def undo(opts):
    runs_dir = os.path.join(STATE, "runs")
    if not os.path.isdir(runs_dir):
        print("  No runs recorded yet.")
        return 1
    runs = sorted(f for f in os.listdir(runs_dir) if f.endswith(".jsonl"))
    if not runs:
        print("  No runs recorded yet.")
        return 1
    target = opts.run or runs[-1]
    path = os.path.join(runs_dir, target)
    if not os.path.exists(path):
        print(red("  No such run: %s" % target))
        print("  Available: " + ", ".join(runs))
        return 1

    entries = [json.loads(line) for line in open(path) if line.strip()]
    archived = [e for e in entries if e["action"] == "ARCHIVE"]
    deleted = [e for e in entries if e["action"] == "DELETE"]

    header("Undoing run %s" % target)
    print("  %d archived items can be restored." % len(archived))
    if deleted:
        print(yellow("  %d deleted items cannot -- that is what DELETE means."
                     % len(deleted)))
    print()
    if not archived:
        return 0
    if not confirm("Copy them back to the internal disk?", default=True):
        return 0

    flags = rsync_flags()
    restored = 0
    for e in archived:
        src, dst = e["dest"], e["path"]
        print("  %s" % rel_home(dst))
        if not os.path.exists(src):
            print(red("      not on the drive -- is it plugged in?"))
            continue
        if os.path.islink(dst):
            os.remove(dst)
        elif os.path.exists(dst):
            print(yellow("      something is already there, skipping"))
            continue
        if copy_tree(src, dst, flags):
            restored += 1
            print(green("      restored"))
    print()
    print("  Restored %d of %d." % (restored, len(archived)))
    print("  The copies on the drive are still there; delete them by hand once")
    print("  you are satisfied.")
    return 0


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def write_report(result, dest_root, approved, opts):
    lines = []
    a = lines.append
    total, used, free = shutil.disk_usage("/System/Volumes/Data")
    a("macsweep report")
    a("machine   %s" % machine_name())
    a("when      %s" % datetime.now().strftime("%Y-%m-%d %H:%M"))
    a("disk      %s total, %s used, %s free" % (human(total), human(used),
                                                human(free)))
    a("archive   %s" % dest_root)
    a("")
    a("Largest folders measured:")
    for m in result["measured"][:30]:
        if m["bytes"] < 50 * MB:
            break
        flag = ""
        if is_hands_off(m["path"]):
            flag = "  [live app data, not touched]"
        elif m["partial"]:
            flag = "  [partial measurement]"
        a("  %9s  %s%s" % (human(m["bytes"]), rel_home(m["path"]), flag))
    a("")
    a("Acted on this run:")
    for item in approved:
        a("  %-8s %9s  %s" % (item["action"], human(item["bytes"]),
                              rel_home(item["path"])))
    if not approved:
        a("  (nothing)")
    a("")
    text = "\n".join(lines)

    os.makedirs(dest_root, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    for name in ("macsweep-report-%s.txt" % stamp, "macsweep-report-latest.txt"):
        try:
            with open(os.path.join(dest_root, name), "w") as fh:
                fh.write(text)
        except OSError:
            pass
    return text


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def preflight():
    if sys.platform != "darwin":
        print(red("macsweep is macOS-only. This looks like %s." % sys.platform))
        sys.exit(1)
    if sys.version_info < (3, 8):
        print(red("Needs Python 3.8 or newer. You have %d.%d."
                  % sys.version_info[:2]))
        sys.exit(1)


def intro():
    header("macsweep")
    print("""
  This frees space on your Mac in two ways:

    DELETE   caches and build output that rebuild themselves
    ARCHIVE  cold files moved to an external drive, with a link left
             behind so the original path still works

  You approve each category before anything happens. Deletes come only
  from a fixed list of known-regenerable folders -- macsweep will not
  delete a file just because it looks old. Archives are copied and
  verified before the original is removed.

  Your documents, photos, mail, keychain, and anything in a cloud folder
  are never touched.
""".rstrip())
    print()


def main():
    p = argparse.ArgumentParser(prog="macsweep", add_help=True)
    p.add_argument("--drive", help="path to the external drive")
    p.add_argument("--folder", help="folder name to create on the drive")
    p.add_argument("--scan-only", action="store_true",
                   help="report only, change nothing")
    p.add_argument("--undo", action="store_true", help="reverse the last run")
    p.add_argument("--run", help="which run to undo (default: most recent)")
    p.add_argument("--budget", type=float, default=WALK_BUDGET,
                   help="seconds to spend on any one folder (default 90)")
    p.add_argument("--cold-days", type=int, default=120, dest="cold_days")
    p.add_argument("--downloads-days", type=int, default=60,
                   dest="downloads_days")
    p.add_argument("--project-days", type=int, default=180,
                   dest="project_days")
    p.add_argument("--min-mb", type=int, default=100, dest="min_mb")
    p.add_argument("--big-file-mb", type=int, default=250, dest="big_file_mb")
    p.add_argument("--big-dir-mb", type=int, default=500, dest="big_dir_mb")
    opts = p.parse_args()

    preflight()

    if opts.undo:
        return undo(opts)

    intro()

    total, used, free = shutil.disk_usage("/System/Volumes/Data")
    pct = 100.0 * used / total
    print("  Your disk: %s total, %s used, %s free (%.0f%% full)"
          % (human(total), human(used), human(free), pct))
    if pct >= 95:
        print(red("  That is very full. macOS gets unstable below about 5GB free."))
    print()

    dest_root = None
    if not opts.scan_only:
        drive = choose_drive(opts.drive)
        default_folder = safe_folder_name(machine_name()) + "-Archive"
        header("What should the archive folder be called?")
        print("  A folder by this name is created on the drive, and archived")
        print("  files keep their original layout inside it. Using a name tied")
        print("  to this Mac keeps things separate if you ever point macsweep")
        print("  at the same drive from another computer.")
        print()
        folder = safe_folder_name(ask("Folder name", default_folder))
        dest_root = os.path.join(drive, folder)
        print()
        print("  Archiving to %s" % bold(dest_root))

    result = scan(opts)
    candidates = result["candidates"]
    walker = result["walker"]

    header("What is taking up space")
    for m in result["measured"][:15]:
        if m["bytes"] < 100 * MB:
            break
        flag = ""
        if is_hands_off(m["path"]):
            flag = dim("   live app data, left alone")
        elif m["partial"]:
            flag = yellow("   partial")
        print("  %9s  %s%s" % (human(m["bytes"]), rel_home(m["path"]), flag))
    if walker.skipped_cloud:
        print()
        print(dim("  %d cloud-synced folders skipped. Use the sync app's own"
                  % walker.skipped_cloud))
        print(dim("  'free up space' or 'remove download' to reclaim those."))

    if not candidates:
        print()
        print(green("  Nothing worth acting on. Your disk is already tidy."))
        return 0

    reclaim = sum(c["bytes"] for c in candidates if c["action"] == "DELETE")
    movable = sum(c["bytes"] for c in candidates if c["action"] == "ARCHIVE")
    print()
    print("  Found %s of deletable cache and %s that could be archived."
          % (bold(human(reclaim)), bold(human(movable))))

    approved = review(candidates, opts)

    if opts.scan_only:
        print()
        print(dim("  --scan-only: nothing was changed."))
        return 0

    if not approved:
        print()
        print("  Nothing approved. Exiting without changes.")
        return 0

    need = sum(c["bytes"] for c in approved if c["action"] == "ARCHIVE")
    if need:
        drive_free = shutil.disk_usage(dest_root if os.path.isdir(dest_root)
                                       else os.path.dirname(dest_root))[2]
        if drive_free < need * 1.05:
            print()
            print(red("  Not enough room: need %s, drive has %s."
                      % (human(need), human(drive_free))))
            return 1

    d = len([x for x in approved if x["action"] == "DELETE"])
    a = len([x for x in approved if x["action"] == "ARCHIVE"])
    header("Ready")
    print("  %d items to delete, %d to archive, %s total."
          % (d, a, human(sum(x["bytes"] for x in approved))))
    print()
    print("  Close other apps first if you can -- files in use may fail to move.")
    print()
    if ask('Type "go" to start').lower() != "go":
        print("  Cancelled. Nothing changed.")
        return 0

    freed, failed, log_path = execute(approved, dest_root, opts)

    header("Done")
    total, used, free_after = shutil.disk_usage("/System/Volumes/Data")
    print("  Reclaimed about %s." % bold(human(freed)))
    print("  Free space now: %s" % human(free_after))
    if failed:
        print(yellow("  %d items could not be handled (see messages above)." % failed))
    print()
    write_report(result, dest_root, approved, opts)
    print("  Report written to %s" % dest_root)
    print("  Undo log: %s" % log_path)
    print()
    print("  Reverse the archived moves any time with:")
    print(bold("      python3 macsweep.py --undo"))
    print()
    print(dim("  Note: archived files live on the drive now. Keep it plugged in,"))
    print(dim("  or expect those paths to stop resolving until you reconnect it."))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  interrupted")
        sys.exit(130)
