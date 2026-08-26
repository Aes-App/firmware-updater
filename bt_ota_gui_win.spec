# PyInstaller spec for the Windows build of the BT Firmware Updater (onefile .exe).
#   Build on Windows (x64 or x86 or arm64 Python):
#       pyinstaller --noconfirm --clean bt_ota_gui_win.spec
#   -> dist/AesApp BT Updater.exe   (single self-contained executable)
#
# The .exe architecture matches the Python you build with. On Windows-on-ARM you
# can build x64 (and x86) via emulation by installing an x64 (x86) Python.
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

APP_NAME = "AesApp BT Updater"

datas, binaries, hiddenimports = [], [], []
for pkg in ("bleak", "unicorn"):
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
for _asset in ("aesapp_logo.png", "aesapp_logo_sm.png"):
    if os.path.exists(f"bt_ota/assets/{_asset}"):
        datas += [(f"bt_ota/assets/{_asset}", "bt_ota/assets")]
hiddenimports += ["bt_ota", "bt_ota.gui", "bt_ota.ota", "bt_ota.rcsp",
                  "bt_ota.jl_auth", "bt_ota.wiced", "bt_ota.client"]

ICON = "bt_ota/assets/AesApp.ico" if os.path.exists("bt_ota/assets/AesApp.ico") else None

a = Analysis(
    ["bt_ota_gui.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# onefile: a single self-contained .exe
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name=APP_NAME,
    console=False,           # windowed GUI, no console
    disable_windowed_traceback=False,
    icon=ICON,
    version=None,
    upx=False,
)
