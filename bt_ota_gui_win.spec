# PyInstaller spec for the Windows build of the AesApp Radio Updater (onefile .exe).
#   Build on Windows (x64 or x86 or arm64 Python):
#       pyinstaller --noconfirm --clean bt_ota_gui_win.spec
#   -> dist/AesApp Radio Updater.exe   (single self-contained executable)
#
# The .exe architecture matches the Python you build with. On Windows-on-ARM you
# can build x64 (and x86) via emulation by installing an x64 (x86) Python.
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

APP_NAME = "AesApp Radio Updater"
COMPANY = "AesApp Inc."


def _app_version():
    """The one VERSION in bt_ota/gui.py, so the .exe cannot disagree with the app
    it contains. Parsed rather than imported: importing the GUI package at build
    time would drag in Tk and bleak for a three-digit string."""
    import re
    src = open(os.path.join(os.path.dirname(os.path.abspath(SPEC)), "bt_ota", "gui.py"),
               encoding="utf-8").read()
    m = re.search(r'^VERSION\s*=\s*"([\d.]+)"', src, re.M)
    if not m:
        raise SystemExit("bt_ota/gui.py has no VERSION -- the .exe would ship unversioned")
    return m.group(1)


VERSION = _app_version()
_v = tuple(int(x) for x in (VERSION.split(".") + ["0", "0", "0"])[:4])


def _version_resource():
    """Write the VSVersionInfo resource PyInstaller stamps into the .exe.

    Without it the .exe has a blank Properties -> Details: no version, no
    company. That costs support ("which build are you on?") and gives SmartScreen
    less to go on for a binary that is not Authenticode-signed.

    Emitted as literal text rather than by building the objects, because
    PyInstaller.utils.win32.versioninfo imports win32api and so cannot even be
    imported off Windows -- which would make this spec unreadable on the machine
    that maintains it. PyInstaller reads the file back with eval()
    (load_version_info_from_text_file), so the text IS the supported interface.
    """
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={_v},
    prodvers={_v},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', {COMPANY!r}),
         StringStruct('FileDescription', {APP_NAME!r}),
         StringStruct('FileVersion', {VERSION!r}),
         StringStruct('InternalName', {APP_NAME!r}),
         StringStruct('OriginalFilename', {APP_NAME + ".exe"!r}),
         StringStruct('ProductName', {APP_NAME!r}),
         StringStruct('ProductVersion', {VERSION!r})])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    out = os.path.join(os.path.dirname(os.path.abspath(SPEC)), "build", "version_win.txt")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    return out


VERSION_FILE = _version_resource()

datas, binaries, hiddenimports = [], [], []
# NB: unicorn is deliberately NOT bundled on Windows. The JieLi auth now runs in
# pure Python (bt_ota._jl_e1); unicorn's JIT/memory setup access-violates inside a
# frozen app on hardened Windows. It stays an optional dev/mac dep for validate_ufw.
for pkg in ("bleak", "serial",   # serial = pyserial, for the radio/boards tab
            "certifi"):          # CA bundle for radio_fw.download's HTTPS fetch
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

# bleak's Windows BLE backend rides on the WinRT projection packages
for pkg in ("winrt", "winrt_runtime", "bleak_winrt"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:
        pass
hiddenimports += collect_submodules("bleak.backends.winrt")
hiddenimports += [
    "winrt.windows.devices.bluetooth",
    "winrt.windows.devices.bluetooth.advertisement",
    "winrt.windows.devices.bluetooth.genericattributeprofile",
    "winrt.windows.devices.enumeration",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.storage.streams",
]

# app package + resources
datas += [
    ("bt_ota/libjl_ota_auth.so", "bt_ota"),
    ("bt_ota/THIRD_PARTY_NOTICES.txt", "bt_ota"),
]
for _asset in ("aesapp_logo.png", "aesapp_logo_sm.png", "AesApp_icon.png",
               "SCT3288.png", "NR.png", "ICON.png", "FW.png", "Reset.png"):
    if os.path.exists(f"bt_ota/assets/{_asset}"):
        datas += [(f"bt_ota/assets/{_asset}", "bt_ota/assets")]
hiddenimports += ["bt_ota", "bt_ota.gui", "bt_ota.ota", "bt_ota.rcsp",
                  "bt_ota.jl_auth", "bt_ota.wiced", "bt_ota.client",
                  "bt_ota._jl_e1", "bt_ota._jl_itab"]
# radio/boards firmware tab (lazily imported in bt_ota.gui.main) + its vendored
# stdlib-only precompilers + pyserial's Windows port enumerator.
hiddenimports += ["radio_fw", "radio_fw.gui_tab", "radio_fw.layout", "radio_fw.engines", "radio_fw.compiler",
                  "radio_fw.spec", "radio_fw.download", "radio_fw.vendor",
                  "radio_fw.vendor.fwupd_cps", "radio_fw.vendor.fwupd_nr",
                  "radio_fw.vendor.fwupd_sct", "serial.tools.list_ports",
                  "serial.tools.list_ports_windows",
                  # radio_fw.download's stdlib HTTPS stack + the CA bundle:
                  "ssl", "json", "urllib.request", "urllib.error", "certifi"]
# Digital Contact Refresh tab (lazily imported in bt_ota.gui.main) + the stdlib
# pieces it leans on, incl. winreg for the aesapp:// scheme registration.
hiddenimports += ["radio_contacts", "radio_contacts.segments", "radio_contacts.catalog",
                  "radio_contacts.engine", "radio_contacts.gui_tab", "radio_contacts.launch",
                  # the local list builder + its generated country tables, named for
                  # the same reason as the rest of the package: bt_ota.gui.main imports
                  # radio_contacts lazily, so no static import reaches any of it.
                  "radio_contacts.contact_build", "radio_contacts.contact_tables",
                  "csv", "gzip", "socket", "secrets", "struct", "winreg"]
# Writing a codeplug prepared on the web app: the job client, the write/verify
# engine and its tab. It reuses radio_contacts' transport and wire primitives.
hiddenimports += ["radio_codeplug", "radio_codeplug.client", "radio_codeplug.engine",
                  "radio_codeplug.gui_tab"]

ICON = "bt_ota/assets/AesApp.ico" if os.path.exists("bt_ota/assets/AesApp.ico") else None

a = Analysis(
    ["bt_ota_gui.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "matplotlib", "unicorn", "capstone"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# onefile: a single self-contained .exe (no _internal folder). Safe again now that
# unicorn is gone -- the onedir workaround existed only because unicorn's uc_mem_map
# access-violated inside a frozen app; the pure-Python auth (bt_ota._jl_e1) has no
# such issue. The bundled .so + assets unpack to a temp _MEIPASS dir at launch, which
# jl_auth._default_so_path and gui._asset_path both already resolve. Trade-off vs
# onedir: ~1-3s slower cold start (extraction) and some EDR flags temp-DLL loading.
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name=APP_NAME,
    console=False,           # windowed GUI, no console
    disable_windowed_traceback=False,
    icon=ICON,
    version=VERSION_FILE,
    upx=False,
    runtime_tmpdir=None,
)
