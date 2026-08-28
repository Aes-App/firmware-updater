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

datas, binaries, hiddenimports = [], [], []
# NB: unicorn is deliberately NOT bundled on Windows. The JieLi auth now runs in
# pure Python (bt_ota._jl_e1); unicorn's JIT/memory setup access-violates inside a
# frozen app on hardened Windows. It stays an optional dev/mac dep for validate_ufw.
for pkg in ("bleak", "serial"):   # serial = pyserial, for the radio/boards tab
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
hiddenimports += ["radio_fw", "radio_fw.gui_tab", "radio_fw.engines", "radio_fw.compiler",
                  "radio_fw.spec", "radio_fw.vendor", "radio_fw.vendor.fwupd_cps",
                  "radio_fw.vendor.fwupd_nr", "radio_fw.vendor.fwupd_sct",
                  "serial.tools.list_ports", "serial.tools.list_ports_windows"]

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
    version=None,
    upx=False,
    runtime_tmpdir=None,
)
