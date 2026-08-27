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
# NB: unicorn is deliberately NOT bundled on Windows. The JieLi auth now runs in
# pure Python (bt_ota._jl_e1); unicorn's JIT/memory setup access-violates inside a
# frozen app on hardened Windows. It stays an optional dev/mac dep for validate_ufw.
for pkg in ("bleak",):
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
for _asset in ("aesapp_logo.png", "aesapp_logo_sm.png", "AesApp_icon.png"):
    if os.path.exists(f"bt_ota/assets/{_asset}"):
        datas += [(f"bt_ota/assets/{_asset}", "bt_ota/assets")]
hiddenimports += ["bt_ota", "bt_ota.gui", "bt_ota.ota", "bt_ota.rcsp",
                  "bt_ota.jl_auth", "bt_ota.wiced", "bt_ota.client",
                  "bt_ota._jl_e1", "bt_ota._jl_itab"]

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

# onedir (a folder), NOT onefile: unicorn's JIT/memory access-violates inside a
# PyInstaller ONEFILE on Windows (uc_mem_map faults in the frozen process, though
# the identical unicorn works fine in a venv on the same PC) -- a known
# unicorn+onefile issue. onedir keeps the DLLs in a real folder next to the exe,
# like a normal install, which unicorn is happy with.
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,           # windowed GUI, no console
    disable_windowed_traceback=False,
    icon=ICON,
    version=None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=APP_NAME)
