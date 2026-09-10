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
# folders can stall for hours -- see the note on CLOUD below. Six minutes is
# generous enough for a genuinely large local tree (a 200GB Photos library on a
# slow external, say) while still cutting off anything that is really stuck.
WALK_BUDGET = 360.0


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
    (".Trash", "the Trash -- already deleted, just never emptied"),
    (".cache/puppeteer", "downloaded Chromium builds"),
    (".cache/ms-playwright", "downloaded browser builds"),
    ("Library/Caches/ms-playwright", "downloaded browser builds"),
    ("Library/Logs/DiagnosticReports", "crash logs"),
    ("Library/Caches/JetBrains", "JetBrains IDE caches"),
    ("Library/Developer/Xcode/UserData/Previews", "SwiftUI preview cache"),
    ("Library/Developer/Xcode/Products", "Xcode build products"),
    ("Library/Android/sdk/sources", "Android source jars, re-downloadable"),
    ("Library/Android/sdk/emulator", "Android emulator binary, re-downloadable"),
    (".expo", "Expo build cache"),
    (".vagrant.d/boxes", "Vagrant box images"),
    (".docker/buildx", "Docker build cache"),
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

# Build output that lives INSIDE a project folder. Each is a rebuild away, and
# across years of projects this is normally the single largest recoverable
# chunk on a developer machine. Each entry pairs the folder with a sibling file
# that proves what it is: "build" alone could be source, "build" next to
# build.gradle is Gradle output. An empty guard means the name is unambiguous.
PROJECT_JUNK = [
    ("node_modules", "package.json", "npm/yarn dependencies"),
    ("Pods", "Podfile", "CocoaPods dependencies"),
    (".next", "package.json", "Next.js build output"),
    (".nuxt", "package.json", "Nuxt build output"),
    (".svelte-kit", "package.json", "SvelteKit build output"),
    ("target", "Cargo.toml", "Rust build output"),
    ("target", "pom.xml", "Maven build output"),
    ("build", "build.gradle", "Gradle build output"),
    ("build", "build.gradle.kts", "Gradle build output"),
    (".gradle", "build.gradle", "project-local Gradle cache"),
    (".gradle", "build.gradle.kts", "project-local Gradle cache"),
    ("DerivedData", "", "Xcode build output"),
    ("__pycache__", "", "Python bytecode"),
    (".pytest_cache", "", "pytest cache"),
    (".mypy_cache", "", "mypy cache"),
    (".ruff_cache", "", "ruff cache"),
    (".tox", "tox.ini", "tox environments"),
]

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
SKIP_HOME = {"Library", "Applications", "Public", "Desktop.localized"}

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

    ("projectjunk", "DELETE", "Build output inside your projects", """
Every project you have ever run `npm install` or `cargo build` or `pod install`
in left a folder of downloaded dependencies and compiled output behind. A
single node_modules is a few hundred megabytes; twenty old projects is tens of
gigabytes. None of it is your code -- it is all reconstructed by running the
install or build command again.

macsweep only counts a folder here when a sibling file proves what it is:
node_modules next to package.json, target next to Cargo.toml, build next to
build.gradle. A folder named "build" that is actually source is not touched."""),

    ("trash", "DELETE", "The Trash", """
Files you already deleted. They occupy disk until the Trash is emptied, which
a lot of people never do. Nothing here is recoverable by macsweep afterward,
but you already decided you didn't want it."""),

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
while the drive is connected. Photos and iMovie libraries are handled
separately, further down, because they need a different treatment."""),

    ("projects", "ARCHIVE", "Cold project folders", """
Project directories you have not touched in months. Moved, not deleted. Any
project with uncommitted git changes is skipped automatically, so work in
progress stays where it is.

Be deliberate here: while the drive is unplugged, these paths stop resolving.
Archive things you are genuinely done with, not the project you'll open
tomorrow."""),

    ("documents", "ARCHIVE", "Cold documents and folders", """
Files and folders in Documents and on the Desktop that you have not opened in
months. Tax paperwork from three years ago, finished coursework, scans,
exports, the folder from a job you already left.

These are moved to the drive, not deleted, and a link stays behind so the path
still works whenever the drive is connected. Nothing here is judged by what is
inside it -- only by how long since you last touched it -- so read the list
before you approve it."""),

    ("libraries", "RELOCATE", "Photo, video and music libraries", """
A Photos, iMovie, Final Cut, Logic or Music library is usually the single
largest thing a non-developer owns. Moving one to an external drive is a
supported, normal thing to do -- Apple documents it.

It works differently from everything else here. These libraries record their
own location internally, so a symlink breaks them. Instead macsweep copies the
library to the drive, verifies it, removes the original, and leaves NO link.
You then point the app at the new copy once:

  Photos    hold Option while opening Photos, choose the library on the drive
  iMovie    File > Open Library > Other, pick it on the drive
  Music     hold Option while opening Music
  Logic     it will ask on next launch

After that the app remembers, and it opens normally every time the drive is
attached. With the drive unplugged, the app will say it cannot find its
library -- it is not damaged, it is on the drive.

Two things to know before saying yes. If this is your System Photo Library and
you use iCloud Photos, keep the drive connected while Photos is open or syncing
will pause. And the copy is large, so this step can take a while."""),

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
        if is_cloud(p):
            return          # walking these stalls for hours; see CLOUD
        # NOTE: protected paths ARE measured. They are excluded from actions
        # later, not from the report. A safety rule that hides where the space
        # went is not a safety rule, it is a blind spot.
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
    limit = ("%.0f minutes" % (opts.budget / 60) if opts.budget >= 60
             else "%.0f seconds" % opts.budget)
    print("  skipped, and any folder that takes more than %s is" % limit)
    print("  abandoned rather than left to stall.")
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

    # --- Build output sitting inside project folders ----------------------
    print("  checking project folders for build output...")
    candidates += find_project_junk(opts, walker)

    # --- ARCHIVE candidates ----------------------------------------------
    candidates += find_archivable(opts, walker)

    # --- Media libraries, which move rather than symlink ------------------
    candidates += find_libraries(opts, walker)

    candidates = prune_nested(candidates)
    candidates.sort(key=lambda x: x["bytes"], reverse=True)

    # --- Everything big that macsweep will not touch itself ---------------
    print("  looking outside the home folder...")
    review = find_review(measured, opts)

    return {"candidates": candidates, "measured": measured, "walker": walker,
            "review": review}


def children_of(path):
    try:
        with os.scandir(path) as it:
            return sorted(e.path for e in it if e.is_dir(follow_symlinks=False))
    except OSError:
        return []


def category_for_delete(path: str) -> str:
    r = rel_home(path)
    if r == ".Trash" or r.startswith(".Trash/"):
        return "trash"
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

    # Cold documents and folders in Documents / Desktop
    for folder in ("Documents", "Desktop"):
        base = os.path.join(HOME, folder)
        if not os.path.isdir(base) or is_never_touch(base):
            continue
        for entry in list_entries(base):
            if is_bundle(entry) or os.path.basename(entry).startswith("."):
                continue
            if is_never_touch(entry) or is_hands_off(entry) or is_cloud(entry):
                continue
            if any(entry == o["path"] for o in out):
                continue
            info = entry_info(entry, walker, opts)
            if not info:
                continue
            floor = opts.big_dir_mb if os.path.isdir(entry) else opts.big_file_mb
            if info["bytes"] < floor * MB or info["age"] < opts.cold_days:
                continue
            if os.path.isdir(entry) and git_dirty(entry):
                continue
            info.update(action="ARCHIVE", category="documents",
                        note="untouched %d days" % info["age"])
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


def find_libraries(opts, walker):
    """Photos / iMovie / Logic / Music libraries, anywhere obvious.

    These are relocated rather than symlinked: the bundle records its own
    location, so a link breaks the app. See the "libraries" category text.
    """
    out = []
    seen = set()
    places = [os.path.join(HOME, n) for n in
              ("Pictures", "Movies", "Music", "Documents", "Desktop")] + [HOME]
    for base in places:
        if not os.path.isdir(base) or is_never_touch(base):
            continue
        for entry in list_entries(base):
            if entry in seen or not is_bundle(entry):
                continue
            if entry.lower().endswith((".sparsebundle", ".dmg.sparseimage")):
                continue        # disk images, handled as ordinary big files
            seen.add(entry)
            info = entry_info(entry, walker, opts)
            if not info or info["bytes"] < opts.big_dir_mb * MB:
                continue
            kind = os.path.splitext(entry)[1].lstrip(".")
            info.update(action="RELOCATE", category="libraries",
                        note="%s library, %s" % (kind, human(info["bytes"])))
            out.append(info)
    return out


def find_project_junk(opts, walker, max_depth=4):
    """Find node_modules / target / Pods and friends inside project folders.

    Walks a bounded depth from the usual project locations. Only counts a
    folder when the guard file sits beside it, so a directory named "build"
    that is really source code is never proposed.
    """
    starts = [HOME] + [os.path.join(HOME, n) for n in (
        "Projects", "dev", "Developer", "Code", "src", "repos", "work",
        "PycharmProjects", "IdeaProjects", "WebstormProjects", "CLionProjects",
        "RiderProjects", "XcodeProjects", "AndroidStudioProjects",
        "DataGripProjects", "GoLandProjects")]
    by_name = {}
    for name, guard, why in PROJECT_JUNK:
        by_name.setdefault(name, []).append((guard, why))

    found, seen = [], set()

    def descend(folder, depth):
        if depth > max_depth or is_cloud(folder) or is_never_touch(folder):
            return
        try:
            with os.scandir(folder) as it:
                kids = [e for e in it if e.is_dir(follow_symlinks=False)]
        except OSError:
            return
        names = set()
        try:
            with os.scandir(folder) as it:
                names = {e.name for e in it}
        except OSError:
            pass
        for kid in kids:
            if kid.path in seen:
                continue
            rules = by_name.get(kid.name)
            if rules:
                why = None
                for guard, description in rules:
                    if not guard or guard in names or any(
                            n.endswith(guard) for n in names):
                        why = description
                        break
                if why:
                    seen.add(kid.path)
                    m = walker.measure(kid.path, opts.budget / 4)
                    if m["bytes"] >= opts.min_mb * MB and not m["partial"]:
                        found.append({
                            "action": "DELETE", "path": kid.path,
                            "bytes": m["bytes"], "files": m["files"],
                            "age": age_days(m["newest"]),
                            "category": "projectjunk", "note": why,
                        })
                    continue        # never descend into build output
            if kid.name.startswith(".") and kid.name not in (".next", ".nuxt"):
                continue
            descend(kid.path, depth + 1)

    for start in starts:
        if os.path.isdir(start):
            descend(start, 0)
    return found


def is_project_junk(path):
    """Re-validate a project build folder at execution time.

    execute() calls this rather than trusting the plan, so a hand-edited or
    stale manifest cannot get a folder deleted that no longer has its guard
    file beside it.
    """
    name = os.path.basename(path)
    parent = os.path.dirname(path)
    rules = [(guard, why) for n, guard, why in PROJECT_JUNK if n == name]
    if not rules:
        return None
    try:
        names = set(os.listdir(parent))
    except OSError:
        return None
    for guard, why in rules:
        if not guard or guard in names or any(n.endswith(guard) for n in names):
            return why
    return None


def du_bytes(path, timeout=180):
    """Size via du, for places Python cannot walk without sudo. None on fail."""
    try:
        r = subprocess.run(["du", "-skx", path], capture_output=True,
                           text=True, timeout=timeout)
        first = r.stdout.strip().split("\n")[0] if r.stdout.strip() else ""
        if first:
            return int(first.split()[0]) * 1024
    except Exception:
        pass
    return None


# Things macsweep will not touch automatically but must never hide. Each is
# reported with the exact command or menu that reclaims it.
REVIEW_ADVICE = [
    ("Library/Application Support/MobileSync", "Old iPhone and iPad backups.",
     ["Finder > your device > Manage Backups, delete the old ones",
      "ls -la ~/Library/Application\\ Support/MobileSync/Backup"]),
    ("Library/Application Support/CrossOver", "Windows apps and games in CrossOver bottles.",
     ["Delete a bottle from inside CrossOver, not from the shell"]),
    ("Library/Application Support/Steam", "Installed games.",
     ["Uninstall from Steam's own library view"]),
    ("Library/Containers/com.utmapp.UTM", "UTM virtual machine disk images.",
     ["Delete unused VMs from inside UTM"]),
    ("Library/Containers/com.docker.docker", "Docker images, containers and volumes.",
     ["docker system df", "docker system prune -a   # removes unused images"]),
    ("Library/Application Support/JetBrains", "Old IDE versions and their indexes.",
     ["JetBrains Toolbox > gear > uninstall versions you no longer run"]),
    ("Library/Mail", "Offline copy of mail that also lives on the server.",
     ["Mail > Settings > Accounts > Advanced, stop keeping copies offline"]),
    ("Library/Messages", "Message history and every attachment ever sent to you.",
     ["Messages > Settings > General > Keep messages: 1 Year",
      "or copy ~/Library/Messages/Attachments to the drive first if you want to keep them"]),
    ("Library/Metadata/CoreSpotlight", "Spotlight's content index. A cache, but it must be stopped before removal.",
     ["sudo mdutil -a -i off",
      "rm -rf ~/Library/Metadata/CoreSpotlight/*",
      "sudo mdutil -a -i on    # then reboot; it rebuilds smaller"]),
    ("Library/Photos", "Photos app caches and analysis data.",
     ["Managed by Photos; use Photos > Settings > iCloud > Optimize Mac Storage"]),
    ("miniconda3", "Conda environments and its package cache.",
     ["conda clean --all       # cache only, environments untouched"]),
    ("anaconda3", "Conda environments and its package cache.",
     ["conda clean --all"]),
    (".ollama", "Downloaded local language models.",
     ["ollama list", "ollama rm <model>"]),
]


def find_review(measured, opts):
    """Everything big that macsweep will not act on, with how to reclaim it.

    This exists because the whitelist is deliberately narrow. Narrow must not
    mean silent: if something large is on this disk, it appears here even when
    macsweep refuses to touch it itself.
    """
    out = []

    def add(title, size, why, commands):
        out.append({"title": title, "bytes": size, "why": why,
                    "commands": commands})

    # 1a. Probe every path we have specific advice for, directly. Measuring
    #     only top-level roots would bury a 2.5G VM image inside a generic
    #     "Library/Containers" line with no way to act on it.
    sized = {m["path"]: m["bytes"] for m in measured}
    named = []
    for prefix, why, cmds in REVIEW_ADVICE:
        path = os.path.join(HOME, prefix)
        if not os.path.isdir(path):
            continue
        size = sized.get(path)
        if size is None:
            size = du_bytes(path, 240)
        if size and size >= 1 * GB:
            named.append(path)
            add(prefix, size, why, cmds)

    # 1b. Anything else protected or live that is simply big, so a folder with
    #     no tailored advice still gets named rather than silently dropped.
    for m in measured:
        if m["bytes"] < 1 * GB:
            continue
        path = m["path"]
        if not (is_hands_off(path) or is_never_touch(path)):
            continue
        if path in named:
            continue
        why = "Live application data. macsweep will not touch it."
        inner = [p for p in named if p.startswith(path + os.sep)]
        if inner:
            why += " Includes %s listed separately above." % (
                ", ".join(os.path.basename(p) for p in inner))
        add(rel_home(path), m["bytes"], why,
            ["Remove it from inside the app that owns it"])

    # 2. Outside the home folder entirely -- the blind spot that matters most.
    for path, why, cmds in (
        ("/Applications", "Installed applications.",
         ["du -shx /Applications/* | sort -rh | head -20",
          "Drag the ones you don't use to the Trash"]),
        ("/opt/homebrew", "Homebrew packages, old versions and download cache.",
         ["brew cleanup -n        # preview", "brew cleanup --prune=all",
          "rm -rf \"$(brew --cache)\""]),
        ("/usr/local/Homebrew", "Homebrew (Intel location).",
         ["brew cleanup --prune=all"]),
        ("/opt/anaconda3", "System-wide Anaconda install.",
         ["conda clean --all"]),
        ("/Library/Updates", "Downloaded macOS update installers.",
         ["Install the update, or: sudo rm -rf /Library/Updates/*"]),
        ("/Library/Application Support", "System-wide application support files.",
         ["sudo du -shx /Library/Application\\ Support/* | sort -rh | head"]),
    ):
        if not os.path.isdir(path):
            continue
        size = du_bytes(path)
        if size and size >= 1 * GB:
            add(path, size, why, cmds)

    # 3. Local Time Machine snapshots. These pin blocks you already freed, so
    #    deleting things appears to accomplish nothing until they are thinned.
    try:
        r = subprocess.run(["tmutil", "listlocalsnapshots", "/"],
                           capture_output=True, text=True, timeout=30)
        snaps = [l for l in r.stdout.splitlines()
                 if l.strip().startswith("com.apple.TimeMachine")]
        if snaps:
            add("%d local Time Machine snapshots" % len(snaps), 0,
                "Snapshots hold on to blocks you have already deleted, so "
                "freed space may not show up until they are thinned.",
                ["sudo tmutil thinlocalsnapshots / 100000000000 4",
                 "df -h /System/Volumes/Data"])
    except Exception:
        pass

    # 4. Cloud folders: never walked, so ask the sync app instead.
    cloud_dirs = []
    for base in (os.path.join(HOME, "Library", "CloudStorage"),):
        if os.path.isdir(base):
            cloud_dirs += [os.path.join(base, n) for n in os.listdir(base)]
    if os.path.isdir(os.path.join(HOME, "Library", "Mobile Documents")):
        cloud_dirs.append(os.path.join(HOME, "Library", "Mobile Documents"))
    if cloud_dirs:
        add("%d cloud-synced folders" % len(cloud_dirs), 0,
            "Not measured, because reading them forces downloads. Files kept "
            "locally here can be evicted without losing anything.",
            ["iCloud: System Settings > your name > iCloud > Optimize Mac Storage",
             "or right-click an item in Finder > Remove Download",
             "OneDrive / Dropbox / Google Drive: the app's own 'free up space'"])

    # 5. Simulators, which accumulate silently.
    sim = os.path.join(HOME, "Library/Developer/CoreSimulator/Devices")
    if os.path.isdir(sim):
        size = du_bytes(sim, 240)
        if size and size >= 1 * GB:
            add("Library/Developer/CoreSimulator/Devices", size,
                "Simulator devices, including ones for SDKs you no longer have.",
                ["xcrun simctl delete unavailable   # safe: only orphans",
                 "xcrun simctl erase all            # wipes all simulator content"])

    out.sort(key=lambda x: x["bytes"], reverse=True)
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
        tag = {"DELETE": red("DELETE"), "ARCHIVE": green("ARCHIVE"),
               "RELOCATE": yellow("MOVED to the drive")}[action]
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
        elif action == "RELOCATE":
            print(yellow("  These are copied to the drive and verified, then the"))
            print(yellow("  original is removed. No link is left behind, so you"))
            print(yellow("  point the app at the new location once (instructions"))
            print(yellow("  are printed when it finishes)."))
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


def show_review(review):
    """Print what macsweep found but will not touch, with how to reclaim it.

    The whitelist is narrow on purpose. This section is what keeps narrow from
    turning into silent: if it is big and on this disk, it is named here even
    when macsweep refuses to act on it.
    """
    if not review:
        return
    known = sum(f["bytes"] for f in review)
    header("Bigger wins macsweep will not do for you  --  %s located"
           % human(known))
    print("""
  Each of these is either live application data, outside your home folder, or
  managed by another program. Deleting them from a script would be reckless,
  so macsweep measures them and hands you the exact command instead. Several
  are usually larger than everything macsweep can clean on its own.
""".strip("\n"))
    print()
    for f in review:
        size = human(f["bytes"]) if f["bytes"] else "     ?"
        print("  %9s  %s" % (size, bold(f["title"])))
        print("             %s" % f["why"])
        for cmd in f["commands"]:
            print(dim("             $ ") + cyan(cmd) if cmd.startswith(
                ("brew", "conda", "docker", "sudo", "rm ", "du ", "xcrun",
                 "ollama", "df ", "ls ")) else "             " + dim(cmd))
        print()


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


# Apps that hold a library open. Moving a library out from under a running
# app corrupts it, so the move is refused while one of these is running.
LIBRARY_APPS = {
    ".photoslibrary": "Photos",
    ".imovielibrary": "iMovie",
    ".theater": "iMovie",
    ".tvlibrary": "TV",
    ".musiclibrary": "Music",
    ".aplibrary": "Aperture",
    ".logicx": "Logic Pro",
    ".fcpbundle": "Final Cut Pro",
}


def running_app_for(path):
    """Name of the app that owns this library, if it is running right now."""
    for suffix, app in LIBRARY_APPS.items():
        if path.lower().endswith(suffix):
            try:
                r = subprocess.run(["pgrep", "-x", app], capture_output=True,
                                   text=True, timeout=10)
                return app if r.returncode == 0 else None
            except Exception:
                return None
    return None


def reopen_steps(rel):
    low = rel.lower()
    if low.endswith(".photoslibrary"):
        return ["Hold Option and open Photos, then choose this library.",
                "If it is your System Photo Library, open Photos > Settings >",
                "General and click 'Use as System Photo Library' once."]
    if low.endswith((".imovielibrary", ".theater")):
        return ["Open iMovie, then File > Open Library > Other, and pick it."]
    if low.endswith(".musiclibrary"):
        return ["Hold Option and open Music, then choose this library."]
    if low.endswith(".tvlibrary"):
        return ["Hold Option and open TV, then choose this library."]
    if low.endswith(".logicx"):
        return ["Open the project from its new location; Logic will remember."]
    if low.endswith(".fcpbundle"):
        return ["Open Final Cut Pro, then File > Open Library > Other."]
    return ["Open the owning app and point it at the new location."]


def execute(approved, dest_root, opts):
    os.makedirs(dest_root, exist_ok=True)
    os.makedirs(os.path.join(STATE, "runs"), exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = os.path.join(STATE, "runs", stamp + ".jsonl")
    log = open(log_path, "a")

    flags = rsync_flags()
    deletes = [x for x in approved if x["action"] == "DELETE"]
    archives = [x for x in approved if x["action"] == "ARCHIVE"]
    relocates = [x for x in approved if x["action"] == "RELOCATE"]
    freed = 0
    failed = 0
    relocated_ok = []

    if deletes:
        header("Deleting caches and build output")
        for i, item in enumerate(deletes, 1):
            path = item["path"]
            print("  [%d/%d] %s  %s" % (i, len(deletes), human(item["bytes"]),
                                        rel_home(path)))
            if is_never_touch(path) or (regenerates(path) is None
                                        and is_project_junk(path) is None):
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

    if relocates:
        header("Moving libraries to the drive")
        print("  Copied and verified first, then the original is removed. No")
        print("  symlink is left: these record their own location, so a link")
        print("  would break them. You will get the reopen steps at the end.")
        print()
        for i, item in enumerate(relocates, 1):
            src = item["path"]
            rel = rel_home(src)
            dst = os.path.join(dest_root, rel)
            print("  [%d/%d] %s  %s" % (i, len(relocates), human(item["bytes"]), rel))

            if is_never_touch(src) or is_hands_off(src):
                print(red("      refused: protected path"))
                failed += 1
                continue
            if not is_bundle(src):
                print(red("      refused: not a recognised library bundle"))
                failed += 1
                continue
            if os.path.exists(dst):
                print(yellow("      already on the drive, skipping"))
                continue
            if running_app_for(src):
                print(red("      refused: %s appears to be running. Quit it first."
                          % running_app_for(src)))
                failed += 1
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
                shutil.rmtree(src)
            except Exception as exc:
                print(red("      copy is on the drive but the original could not"))
                print(red("      be removed: %s" % exc))
                failed += 1
                continue

            freed += item["bytes"]
            relocated_ok.append((rel, dst))
            print(green("      moved"))
            log.write(json.dumps({"action": "RELOCATE", "path": src,
                                  "dest": dst, "bytes": item["bytes"]}) + "\n")

        if relocated_ok:
            print()
            rule("=")
            print("  " + bold("One more step for each library you just moved"))
            rule("=")
            for rel, dst in relocated_ok:
                print()
                print("  %s" % bold(rel))
                print("     now at %s" % dst)
                for line in reopen_steps(rel):
                    print("     %s" % line)
            print()

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
    archived = [e for e in entries
                if e["action"] in ("ARCHIVE", "RELOCATE")]
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
    a("Worth doing by hand (macsweep will not touch these):")
    for f in result.get("review", []):
        a("  %9s  %s" % (human(f["bytes"]) if f["bytes"] else "?", f["title"]))
        a("             %s" % f["why"])
        for cmd in f["commands"]:
            a("             %s" % cmd)
    if not result.get("review"):
        a("  (nothing)")
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
                   help="seconds to spend on any one folder (default 360)")
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

    show_review(result.get("review", []))

    if not candidates:
        print()
        print(green("  Nothing macsweep can act on itself."))
        if result.get("review"):
            print("  The items above are still worth working through by hand.")
        return 0

    reclaim = sum(c["bytes"] for c in candidates if c["action"] == "DELETE")
    movable = sum(c["bytes"] for c in candidates if c["action"] != "DELETE")
    print()
    print("  Found %s of deletable cache and %s that could be moved to the drive."
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

    need = sum(c["bytes"] for c in approved if c["action"] != "DELETE")
    if need:
        drive_free = shutil.disk_usage(dest_root if os.path.isdir(dest_root)
                                       else os.path.dirname(dest_root))[2]
        if drive_free < need * 1.05:
            print()
            print(red("  Not enough room: need %s, drive has %s."
                      % (human(need), human(drive_free))))
            return 1

    d = len([x for x in approved if x["action"] == "DELETE"])
    a = len([x for x in approved if x["action"] != "DELETE"])
    header("Ready")
    print("  %d items to delete, %d to move to the drive, %s total."
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
