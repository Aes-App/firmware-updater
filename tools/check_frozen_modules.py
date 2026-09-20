"""Are the modules we think we shipped actually inside the frozen build?

WHY THIS EXISTS. PyInstaller finds what it can follow. Everything this app does
after startup is imported LAZILY -- the tabs, the contact builder, the country
tables, the update check -- so a module can vanish from a build without a single
warning, and the first sign of it is an operator clicking a tab and getting a
traceback dialog. The .spec files name those modules in `hiddenimports` for
exactly that reason, and this checks the naming worked.

It reads the frozen archive rather than running the app: a GUI cannot be smoke
tested on a build box, and "it launched" would not prove the tab a user has not
clicked yet is present.

Both layouts are handled -- the Windows onefile .exe and the macOS .app, whose
PYZ lives inside Contents/MacOS/<name>.

    python tools/check_frozen_modules.py "dist/AesApp Radio Updater.exe"
    python tools/check_frozen_modules.py "dist/AesApp Radio Updater.app"

Exit codes are deliberately three-way, because "a module is missing" and "I
could not look" are different answers and a build script should treat them
differently:

    0  every required module is in the build
    1  the archive was read and something REQUIRED is not in it  -> fail the build
    2  the archive could not be read at all (unknown layout, PyInstaller version
       mismatch, a packer that moved things) -> warn, do not block a build over a
       check that did not run
"""
from __future__ import annotations

import os
import sys
import tempfile

REQUIRED = [
    "bt_ota.gui",
    "bt_ota.update_check",
    "radio_contacts",
    "radio_contacts.gui_tab",
    "radio_contacts.engine",
    "radio_contacts.catalog",
    "radio_contacts.launch",
    "radio_contacts.segments",
    "radio_contacts.contact_build",
    "radio_contacts.contact_tables",
    "radio_codeplug.gui_tab",
    "radio_codeplug.client",
    "radio_codeplug.engine",
    "radio_fw.gui_tab",
    "radio_fw.layout",
    "radio_fw.engines",
    "radio_fw.compiler",
    "radio_fw.download",
]


def _executable(path: str) -> str:
    if path.endswith(".app") or os.path.isdir(path):
        macos = os.path.join(path, "Contents", "MacOS")
        names = [n for n in os.listdir(macos) if not n.startswith(".")]
        if not names:
            raise SystemExit("no executable in " + macos)
        return os.path.join(macos, names[0])
    return path


class Unreadable(Exception):
    pass


def frozen_modules(path: str) -> set:
    try:
        from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    except ImportError as e:
        raise Unreadable("PyInstaller is not importable here (%s)" % e)

    try:
        arch = CArchiveReader(_executable(path))
    except Exception as e:
        raise Unreadable("%s: %s" % (type(e).__name__, e))
    names = set()
    pyz_entries = [n for n in arch.toc if n.startswith("PYZ")]
    if not pyz_entries:
        raise Unreadable("no PYZ archive inside " + os.path.basename(path))
    for entry in pyz_entries:
        with tempfile.NamedTemporaryFile(suffix=".pyz", delete=False) as f:
            f.write(arch.extract(entry))
            tmp = f.name
        try:
            names |= set(ZlibArchiveReader(tmp).toc)
        except Exception as e:
            raise Unreadable("%s: %s" % (type(e).__name__, e))
        finally:
            os.unlink(tmp)
    names |= {n for n in arch.toc}
    return names


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    path = argv[1]
    if not os.path.exists(path):
        print("no such build: " + path, file=sys.stderr)
        return 1

    try:
        have = frozen_modules(path)
    except Unreadable as e:
        print("could not inspect %s -- %s" % (os.path.basename(path), e), file=sys.stderr)
        print("Not failing the build over a check that did not run. Open the tabs by hand.",
              file=sys.stderr)
        return 2
    missing = [m for m in REQUIRED if m not in have]
    for m in REQUIRED:
        print(("  ok   " if m not in missing else "  MISSING ") + m)
    if missing:
        print("\n%d module(s) are NOT in %s.\n" % (len(missing), os.path.basename(path)),
              file=sys.stderr)
        print("Every one of them is imported lazily, so nothing warned at build time and\n"
              "nothing will warn at startup either -- it fails when a user opens the tab.\n"
              "Add them to `hiddenimports` in the .spec for this platform.", file=sys.stderr)
        return 1
    print("\nall %d lazily-imported modules are present in %s"
          % (len(REQUIRED), os.path.basename(path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
