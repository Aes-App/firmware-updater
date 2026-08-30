# PyInstaller spec for the AesApp Radio Updater GUI.
#   Build from the repo root:  pyinstaller bt_ota_gui.spec
# Produces dist/AesApp Radio Updater.app (macOS) with the Bluetooth usage keys.
#
# Branding assets are optional at build time; generate them with:
#   ./.venv/bin/python make_assets.py "/path/to/AesApp-logo.jpg"
import os
from PyInstaller.utils.hooks import collect_all

APP_NAME = "AesApp Radio Updater"

datas, binaries, hiddenimports = [], [], []
# bleak (BLE) + unicorn (auth emulator) + the pyobjc frameworks bleak uses on macOS
# + pyserial (the radio/boards firmware tab talks to the CDC-ACM port)
for pkg in ("bleak", "unicorn", "CoreBluetooth", "Foundation", "libdispatch", "objc", "serial",
            "certifi"):   # certifi ships the CA bundle radio_fw.download needs in a frozen app
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# ship the extracted JieLi auth library alongside the package
datas += [("bt_ota/libjl_ota_auth.so", "bt_ota")]
# third-party notices (Apache-2.0 attribution for the JieLi lib + trademarks)
datas += [("bt_ota/THIRD_PARTY_NOTICES.txt", "bt_ota")]
# ship branding assets + the radio/boards step photos if present
for _asset in ("aesapp_logo.png", "aesapp_logo_sm.png", "AesApp_icon.png",
               "SCT3288.png", "NR.png", "ICON.png", "FW.png", "Reset.png"):
    if os.path.exists(f"bt_ota/assets/{_asset}"):
        datas += [(f"bt_ota/assets/{_asset}", "bt_ota/assets")]
APP_ICON = "bt_ota/assets/AesApp.icns" if os.path.exists("bt_ota/assets/AesApp.icns") else None

hiddenimports += ["bt_ota", "bt_ota.gui", "bt_ota.ota", "bt_ota.rcsp", "bt_ota.jl_auth",
                  "bt_ota.wiced", "bt_ota.client", "bt_ota._jl_e1", "bt_ota._jl_itab"]
# radio/boards firmware tab (lazily imported in bt_ota.gui.main, so name it here)
# + its vendored, stdlib-only precompilers, + pyserial's port enumerator.
hiddenimports += ["radio_fw", "radio_fw.gui_tab", "radio_fw.engines", "radio_fw.compiler",
                  "radio_fw.spec", "radio_fw.download", "radio_fw.vendor",
                  "radio_fw.vendor.fwupd_cps", "radio_fw.vendor.fwupd_nr",
                  "radio_fw.vendor.fwupd_sct", "serial.tools.list_ports",
                  # radio_fw.download's stdlib HTTPS stack (named so a minimal
                  # build can't drop them):
                  "ssl", "json", "urllib.request", "urllib.error", "certifi"]

a = Analysis(
    ["bt_ota_gui.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name=APP_NAME, console=False, disable_windowed_traceback=False,
    argv_emulation=False, target_arch=None, codesign_identity=None, entitlements_file=None,
    icon=APP_ICON,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=APP_NAME)

app = BUNDLE(
    coll,
    name=f"{APP_NAME}.app",
    icon=APP_ICON,
    bundle_identifier="app.aes.radio-updater",
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": "0.7.1",
        "CFBundleVersion": "0.7.1",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "© AesApp Inc.",
        # macOS refuses Bluetooth to an app without these keys:
        "NSBluetoothAlwaysUsageDescription":
            "Updates your AnyTone radio's Bluetooth module firmware over Bluetooth.",
        "NSBluetoothPeripheralUsageDescription":
            "Communicates with your AnyTone radio over Bluetooth.",
    },
)
