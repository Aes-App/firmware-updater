# PyInstaller spec for the Linux build of the AesApp Radio Updater (onefile ELF).
#   Build through build_linux.sh, which runs it on Ubuntu 22.04:
#       pyinstaller --noconfirm --clean bt_ota_gui_linux.spec
#   -> dist/aesapp-radio-updater   (single self-contained executable)
#
# The binary's architecture, its oldest runnable glibc and its Tk are those of
# the machine and the Python it is built with; build_linux.sh says why that is
# Ubuntu 22.04 with deadsnakes' Python 3.12 on Ubuntu's Tk.
import glob
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

APP_NAME = "aesapp-radio-updater"


def _app_version():
    """The one VERSION in bt_ota/gui.py, checked here so a tree without one fails
    the build instead of shipping. Parsed rather than imported: importing the GUI
    package at build time would drag in Tk and bleak for a three-digit string."""
    import re
    src = open(os.path.join(os.path.dirname(os.path.abspath(SPEC)), "bt_ota", "gui.py"),
               encoding="utf-8").read()
    m = re.search(r'^VERSION\s*=\s*"([\d.]+)"', src, re.M)
    if not m:
        raise SystemExit("bt_ota/gui.py has no VERSION -- the binary would ship unversioned")
    return m.group(1)


VERSION = _app_version()

datas, binaries, hiddenimports = [], [], []
# unicorn is not bundled, as on Windows: the JieLi auth runs in pure Python
# (bt_ota._jl_e1), and unicorn stays an optional dev dependency for validate_ufw.
for pkg in ("bleak", "serial",   # serial = pyserial, for the radio/boards tab
            "certifi",           # CA bundle for radio_fw.download's HTTPS fetch
            "dbus_fast"):        # bleak's BlueZ backend talks to bluetoothd over D-Bus
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h
hiddenimports += collect_submodules("bleak.backends.bluezdbus")

# app package + resources
datas += [
    ("bt_ota/libjl_ota_auth.so", "bt_ota"),
    ("bt_ota/THIRD_PARTY_NOTICES.txt", "bt_ota"),
]
for _asset in ("aesapp_logo.png", "aesapp_logo_sm.png", "AesApp_icon.png"):
    if os.path.exists(f"bt_ota/assets/{_asset}"):
        datas += [(f"bt_ota/assets/{_asset}", "bt_ota/assets")]
# the Radio and Boards tab's step photos, the whole folder (the frozen
# self-check fails a build that is missing one)
datas += [(_photo, "radio_fw/assets") for _photo in sorted(glob.glob("radio_fw/assets/*.png"))]
hiddenimports += ["bt_ota", "bt_ota.gui", "bt_ota.ota", "bt_ota.rcsp",
                  "bt_ota.jl_auth", "bt_ota.wiced", "bt_ota.client",
                  "bt_ota._jl_e1", "bt_ota._jl_itab"]
# radio/boards firmware tab (lazily imported in bt_ota.gui.main) + its vendored
# stdlib-only precompilers + pyserial's Linux port enumerator.
hiddenimports += ["radio_fw", "radio_fw.gui_tab", "radio_fw.layout", "radio_fw.ports", "radio_fw.engines", "radio_fw.compiler",
                  "radio_fw.spec", "radio_fw.download", "radio_fw.vendor",
                  "radio_fw.vendor.fwupd_cps", "radio_fw.vendor.fwupd_nr",
                  "radio_fw.vendor.fwupd_sct", "serial.tools.list_ports",
                  "serial.tools.list_ports_linux",
                  # radio_fw.download's stdlib HTTPS stack + the CA bundle:
                  "ssl", "json", "urllib.request", "urllib.error", "certifi"]
# Digital Contact Refresh tab (lazily imported in bt_ota.gui.main) + the stdlib
# pieces it leans on, incl. subprocess for the aesapp:// desktop-entry registration.
hiddenimports += ["radio_contacts", "radio_contacts.segments", "radio_contacts.catalog",
                  "radio_contacts.engine", "radio_contacts.gui_tab", "radio_contacts.launch",
                  # the local list builder + its generated country tables, named for
                  # the same reason as the rest of the package: bt_ota.gui.main imports
                  # radio_contacts lazily, so no static import reaches any of it.
                  "radio_contacts.contact_build", "radio_contacts.contact_tables",
                  "csv", "gzip", "socket", "secrets", "struct", "subprocess"]
# Writing a codeplug prepared on the web app: the job client, the write/verify
# engine and its tab. It reuses radio_contacts' transport and wire primitives.
hiddenimports += ["radio_codeplug", "radio_codeplug.client", "radio_codeplug.engine",
                  "radio_codeplug.gui_tab"]

a = Analysis(
    ["bt_ota_gui.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "matplotlib", "unicorn", "capstone"],
    noarchive=False,
)
# Libraries that must be the desktop's own, never the build machine's, so they
# are left out as PyInstaller already leaves out glibc:
#   - the X11 client libraries and the font stack: they talk to the user's X
#     server and read the user's fontconfig configuration, which a fontconfig
#     older than that configuration misreads;
#   - what only those need (expat, png, brotli, zlib, bsd, md): the bootloader
#     puts the bundle first on LD_LIBRARY_PATH, so a bundled copy would shadow
#     the system's for the system's own fontconfig and freetype;
#   - libgcc_s, which glibc itself depends on.
# Every desktop that can show an X11 window has all of them. libXft and libXss
# stay in: Tk needs them, not every desktop installs them, and they use only the
# stable interfaces of the libraries above.
_SYSTEM_LIBS = ("libX11.so", "libXau.so", "libXdmcp.so", "libXext.so", "libXrender.so",
                "libfontconfig.so", "libfreetype.so", "libexpat.so", "libpng16.so",
                "libbrotlicommon.so", "libbrotlidec.so", "libz.so", "libbsd.so", "libmd.so",
                "libgcc_s.so")
a.binaries = [b for b in a.binaries if not os.path.basename(b[0]).startswith(_SYSTEM_LIBS)]
pyz = PYZ(a.pure)

# onefile, like the Windows .exe: one file to download and chmod +x. strip=False
# because stripping a manylinux wheel's .so can break it, and the size it saves
# is small next to Tcl/Tk.
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name=APP_NAME,
    console=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
)
