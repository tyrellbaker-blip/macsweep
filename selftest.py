#!/usr/bin/env python3
"""
selftest.py - prove macsweep does what it claims, without touching your Mac.

Builds a fake home folder and a fake external drive in a temporary directory,
runs the real macsweep code against them, and checks what actually happened.
Your own files are never involved: HOME is redirected for the duration and
everything is deleted at the end.

    python3 selftest.py           run the checks
    python3 selftest.py --keep    leave the sandbox behind so you can poke at it

Exit code is 0 if every check passed, 1 otherwise.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import macsweep as ms                                          # noqa: E402

PASS = FAIL = 0
KEEP = "--keep" in sys.argv


def check(name, got, want=True):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  \033[32mpass\033[0m  %s" % name)
    else:
        FAIL += 1
        print("  \033[31mFAIL\033[0m  %s   (got %r, wanted %r)" % (name, got, want))


def section(title):
    print()
    print("\033[1m%s\033[0m" % title)


def write(path, size=0, text=None, days_old=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(text.encode() if text is not None else os.urandom(size))
    if days_old:
        when = time.time() - days_old * 86400
        os.utime(path, (when, when))
    return path


def fill(folder, count=3, size=120 * 1024, days_old=None):
    for i in range(count):
        write(os.path.join(folder, "f%d.bin" % i), size, days_old=days_old)


def build_home(home):
    """A fake home with one of everything macsweep reasons about."""
    # --- safe to delete -------------------------------------------------
    fill(os.path.join(home, "Library/Caches/com.example.app"))
    fill(os.path.join(home, "Library/Logs/SomeApp"))
    fill(os.path.join(home, ".Trash/old-stuff"))
    fill(os.path.join(home, ".gradle/caches/modules-2"))

    # --- build output inside real projects --------------------------------
    write(os.path.join(home, "Projects/webapp/package.json"), text="{}")
    write(os.path.join(home, "Projects/webapp/index.js"), text="console.log(1)")
    fill(os.path.join(home, "Projects/webapp/node_modules/left-pad"))
    write(os.path.join(home, "Projects/api/Cargo.toml"), text="[package]")
    fill(os.path.join(home, "Projects/api/target/debug"))

    # --- traps: these LOOK like build output but are not ------------------
    # a static site whose source folder happens to be called "build"
    write(os.path.join(home, "Projects/portfolio/index.html"), text="<html>")
    fill(os.path.join(home, "Projects/portfolio/build"))
    # a "target" folder with no Cargo.toml / pom.xml beside it
    write(os.path.join(home, "Projects/darts/notes.md"), text="scores")
    fill(os.path.join(home, "Projects/darts/target"))
    # node_modules with no package.json anywhere near it
    fill(os.path.join(home, "Projects/orphan/node_modules"))

    # --- must never be touched -------------------------------------------
    write(os.path.join(home, "Library/Keychains/login.keychain-db"),
          text="PASSWORDS")
    write(os.path.join(home, "Documents/thesis.docx"), text="four years of work")
    write(os.path.join(home, ".ssh/id_ed25519"), text="PRIVATE KEY")
    write(os.path.join(home, "Library/Mail/big.mbox"), 400 * 1024)
    os.makedirs(os.path.join(home, "Pictures/My Photos.photoslibrary/originals"))
    fill(os.path.join(home, "Pictures/My Photos.photoslibrary/originals"), 4)
    write(os.path.join(home, "Pictures/My Photos.photoslibrary/database.db"),
          text="PHOTO DB")

    # --- cold documents ---------------------------------------------------
    write(os.path.join(home, "Documents/Taxes 2019/return.pdf"),
          300 * 1024, days_old=800)
    write(os.path.join(home, "Documents/Taxes 2019/w2.pdf"),
          200 * 1024, days_old=800)
    # ...and something recent that must NOT be swept
    write(os.path.join(home, "Documents/Active Project/draft.md"),
          300 * 1024, text=None, days_old=2)

    # --- archive candidates ----------------------------------------------
    write(os.path.join(home, "Downloads/old-installer.dmg"),
          300 * 1024, days_old=400)
    write(os.path.join(home, "Movies/vacation-2019.mov"),
          400 * 1024, days_old=900)

    # --- a sparse file: claims 1GB, occupies almost nothing ---------------
    sparse = os.path.join(home, "Downloads", "disk.sparse")
    with open(sparse, "wb") as fh:
        fh.seek(1 << 30)
        fh.write(b"\0")
    return sparse


def main():
    sandbox = tempfile.mkdtemp(prefix="macsweep-selftest-")
    home = os.path.join(sandbox, "home")
    drive = os.path.join(sandbox, "FakeDrive")
    os.makedirs(home)
    os.makedirs(drive)

    print("Sandbox: %s" % sandbox)
    print("Your real home folder is not touched at any point.")
    sparse = build_home(home)

    real_home, real_state = ms.HOME, ms.STATE
    ms.HOME = home
    ms.STATE = os.path.join(home, ".macsweep")
    ms._TTY = False
    opts = types.SimpleNamespace(
        budget=30, min_mb=0, cold_days=120, downloads_days=60,
        project_days=180, big_file_mb=0, big_dir_mb=0, scan_only=False,
        run=None)

    try:
        section("1. Measurement is honest about sparse files")
        st = os.lstat(sparse)
        check("file claims about 1GB", st.st_size >= 1 << 30)
        check("macsweep counts what is on disk, not the claim",
              ms.allocated(st) < 10 * 1024 * 1024)

        section("2. Build output inside projects, and the traps")
        junk = {ms.rel_home(x["path"]) for x in ms.find_project_junk(opts, ms.Walker())}
        check("finds node_modules beside package.json",
              "Projects/webapp/node_modules" in junk)
        check("finds target beside Cargo.toml", "Projects/api/target" in junk)
        check("leaves a source folder named 'build' alone",
              "Projects/portfolio/build" not in junk)
        check("leaves 'target' with no Cargo.toml alone",
              "Projects/darts/target" not in junk)
        check("leaves orphan node_modules alone",
              "Projects/orphan/node_modules" not in junk)

        section("3. Nothing important is ever a candidate")
        result = ms.scan(opts)
        paths = {c["path"] for c in result["candidates"]}
        for label, rel in (("keychain", "Library/Keychains"),
                           ("ssh key", ".ssh"),
                           ("Mail", "Library/Mail")):
            full = os.path.join(home, rel)
            check("%s is not proposed for anything" % label,
                  not any(p == full or p.startswith(full + os.sep) for p in paths))
        by_cat = {}
        for cand in result["candidates"]:
            by_cat.setdefault(cand["category"], set()).add(ms.rel_home(cand["path"]))
        check("every DELETE is whitelisted or verified build output",
              all(ms.regenerates(c["path"]) is not None
                  or ms.is_project_junk(c["path"]) is not None
                  for c in result["candidates"] if c["action"] == "DELETE"))

        section("3b. Cold documents are swept, recent ones are not")
        docs = by_cat.get("documents", set())
        check("old tax folder is offered", "Documents/Taxes 2019" in docs)
        check("this week's work is left alone",
              "Documents/Active Project" not in docs)

        section("3c. Photo library is offered as a RELOCATE, not a symlink")
        libs = by_cat.get("libraries", set())
        check("photo library is found",
              "Pictures/My Photos.photoslibrary" in libs)
        lib_rows = [c for c in result["candidates"] if c["category"] == "libraries"]
        check("its action is RELOCATE",
              all(c["action"] == "RELOCATE" for c in lib_rows))

        section("4. Protected things are still MEASURED and reported")
        measured = {ms.rel_home(m["path"]) for m in result["measured"]}
        check("Mail appears in the measurements", "Library/Mail" in measured)
        # the review floor is 1GB; drop it so the tiny sandbox trips it
        real_gb = ms.GB
        ms.GB = 200 * 1024
        try:
            rev = ms.find_review(result["measured"], opts)
        finally:
            ms.GB = real_gb
        titles = {f["title"] for f in rev}
        check("Mail is named in the review section, not hidden",
              any("Mail" in t for t in titles))
        check("every review item carries a command",
              all(f["commands"] for f in rev))

        section("5. Deleting, with the refusals still in force")
        approved = [c for c in result["candidates"] if c["action"] == "DELETE"]
        check("found something to delete", len(approved) > 0)
        # smuggle two forbidden rows into the approved list on purpose
        approved += [
            {"action": "DELETE", "path": os.path.join(home, "Documents"),
             "bytes": 1, "category": "caches"},
            {"action": "ARCHIVE", "path": os.path.join(home, "Library/Keychains"),
             "bytes": 1, "category": "media"},
        ]
        dest = os.path.join(drive, "Test-Archive")
        freed, failed, log = ms.execute(approved, dest, opts)
        check("both smuggled rows refused", failed, 2)
        check("thesis survived",
              open(os.path.join(home, "Documents/thesis.docx")).read(),
              "four years of work")
        check("keychain survived",
              open(os.path.join(home, "Library/Keychains/login.keychain-db")).read(),
              "PASSWORDS")
        check("caches actually gone",
              not os.path.exists(os.path.join(home, "Library/Caches/com.example.app")))
        check("Trash actually emptied",
              not os.path.exists(os.path.join(home, ".Trash/old-stuff")))
        check("node_modules gone",
              not os.path.exists(os.path.join(home, "Projects/webapp/node_modules")))
        check("but the project's source is untouched",
              open(os.path.join(home, "Projects/webapp/index.js")).read(),
              "console.log(1)")
        check("the 'build' trap is untouched",
              os.path.isdir(os.path.join(home, "Projects/portfolio/build")))

        section("6. Archiving: copy, verify, then symlink")
        movie = os.path.join(home, "Movies/vacation-2019.mov")
        original = open(movie, "rb").read()
        freed, failed, log = ms.execute(
            [{"action": "ARCHIVE", "path": movie, "bytes": len(original),
              "category": "media"}], dest, opts)
        check("archived without error", failed, 0)
        check("a symlink replaced the original", os.path.islink(movie))
        check("the file still reads through the link",
              open(movie, "rb").read(), original)
        check("the bytes really are on the drive",
              os.path.isfile(os.path.join(dest, "Movies/vacation-2019.mov")))

        section("7. A bad copy must never delete the original")
        target = os.path.join(home, "Downloads/old-installer.dmg")
        before = open(target, "rb").read()
        good_copy = ms.copy_tree

        def lossy(src, dst, flags):          # pretend the copy truncates
            ok = good_copy(src, dst, flags)
            if os.path.isfile(dst):
                with open(dst, "wb") as fh:
                    fh.write(b"corrupted")
            return ok

        ms.copy_tree = lossy
        freed, failed, _ = ms.execute(
            [{"action": "ARCHIVE", "path": target, "bytes": len(before),
              "category": "downloads"}],
            os.path.join(drive, "Bad-Archive"), opts)
        ms.copy_tree = good_copy
        check("verification caught it", failed, 1)
        check("original is still a real file, not a link",
              os.path.isfile(target) and not os.path.islink(target))
        check("original is byte-for-byte intact", open(target, "rb").read(), before)

        section("7b. Relocating a photo library")
        lib = os.path.join(home, "Pictures/My Photos.photoslibrary")
        db_before = open(os.path.join(lib, "database.db")).read()
        freed, failed, _ = ms.execute(
            [{"action": "RELOCATE", "path": lib, "bytes": 1,
              "category": "libraries"}], dest, opts)
        moved = os.path.join(dest, "Pictures/My Photos.photoslibrary")
        check("relocated without error", failed, 0)
        check("original is gone from the Mac", not os.path.exists(lib))
        check("library is intact on the drive",
              open(os.path.join(moved, "database.db")).read(), db_before)
        check("deliberately NO symlink left behind", os.path.islink(lib), False)

        section("7c. A library cannot be relocated by the wrong route")
        os.makedirs(os.path.join(home, "Pictures/Other.photoslibrary"))
        write(os.path.join(home, "Pictures/Other.photoslibrary/db"), text="x")
        _, failed, _ = ms.execute(
            [{"action": "ARCHIVE", "path": os.path.join(home, "Pictures/Other.photoslibrary"),
              "bytes": 1, "category": "media"}], dest, opts)
        check("ARCHIVE refuses a library bundle (would break the app)", failed, 1)
        _, failed, _ = ms.execute(
            [{"action": "RELOCATE", "path": os.path.join(home, "Documents/thesis.docx"),
              "bytes": 1, "category": "libraries"}], dest, opts)
        check("RELOCATE refuses anything that is not a library", failed, 1)
        check("thesis still intact after that attempt",
              open(os.path.join(home, "Documents/thesis.docx")).read(),
              "four years of work")

        section("8. Undo brings archived files back")
        ms.confirm = lambda *a, **k: True
        ms.undo(types.SimpleNamespace(run=None))
        check("movie is a real file again",
              os.path.isfile(movie) and not os.path.islink(movie))
        check("contents restored exactly", open(movie, "rb").read(), original)
        check("relocated photo library also came back",
              os.path.isfile(os.path.join(lib, "database.db")))

    finally:
        ms.HOME, ms.STATE = real_home, real_state

    print()
    print("=" * 60)
    if FAIL:
        print("\033[31m%d passed, %d FAILED\033[0m" % (PASS, FAIL))
    else:
        print("\033[32mAll %d checks passed.\033[0m" % PASS)
    print("=" * 60)

    if KEEP:
        print("Sandbox left at %s" % sandbox)
    else:
        shutil.rmtree(sandbox, ignore_errors=True)
        print("Sandbox removed.")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
