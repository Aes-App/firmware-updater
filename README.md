# AesApp Radio Updater

A desktop app for updating AnyTone radios, with two tabs:

- **Bluetooth Module Update** — updates the **Bluetooth-module firmware** over the
  computer's own Bluetooth adapter, no Android phone required.
- **Radio and Boards Updates** — updates the D890's **main radio firmware**,
  **icon/font flash**, **NR (noise-reduction) daughterboard**, and **SCT3288
  baseband DSP** over a USB serial cable.

Ships as a CLI, a Tkinter GUI, and self-contained desktop apps (macOS `.app`,
Windows `.exe`).

## Bluetooth Module Update

Two radio/module families, auto-selected by the firmware file type:

| Radio | BT module | Firmware | Protocol | Replaces phone app |
|-------|-----------|----------|----------|--------------------|
| **D890UV** | JieLi ET25 | `.ufw` | JieLi RCSP (mutual auth, device-pull) | `OTA V1.x.apk` |
| **D578 / D878** | Cypress/Infineon WICED ET12 | `.bin` | WICED OTA (no auth, host-push) | `OTA _New tool_V3.0.apk` |

Both paths are **hardware-validated**. The protocols were reverse-engineered from
the vendor Android apps; see [`bt_ota/README.md`](bt_ota/README.md) for the full
protocol write-up (GATT UUIDs, framing, the JieLi RcspAuth handshake, the WICED
verify-reboot race, and device quirks).

## Radio and Boards Updates (D890, over serial)

Pick the vendor update files for the targets you want to write; the app compiles
each one into the exact serial wire stream and validates it hard **before**
anything is sent, then walks the targets one at a time — showing how to put the
radio into each update mode (with a photo of the buttons), asking for the COM
port, and streaming the data.

| Target | Vendor files | Protocol |
|--------|--------------|----------|
| **SCT3288 Baseband** | `.hex` (Intel HEX) | SiCOMM `84 A9 61` framing, host-push |
| **NR Board** | `.ufw` (JieLi) | JieLi bootloader, device-pull |
| **Icons & Fonts** | `.CDD` + `.CDI` (+ optional `.spi`) | AnyTone CPS asset flash |
| **Radio Firmware** | `.CDD` + `.CDI` (+ optional `.spi`) | AnyTone CPS main-MCU flash |

Targets are always written **main firmware last**, so a failure part-way through a
batch leaves the radio still bootable; you can skip any target but not reorder
them. **None of these protocols verifies or reads anything back** — a bad or
interrupted write is only discovered when the radio boots, and can leave it
unbootable, so the tab gates the first write behind an explicit acknowledgement.
The four wire engines (`radio_fw/engines.py`) and the precompilers
(`radio_fw/vendor/fwupd_*`) are a direct port of the browser-based flasher and are
covered by `tests/test_engines.py`.

## Layout

```
bt_ota/                 Bluetooth-module tab (CLI + GUI + BLE protocol backends)
  jl_auth.py            JieLi RcspAuth (pure-Python SAFER+ over the native .so's tables)
  rcsp.py  ota.py       JieLi RCSP codec + BLE OTA state machine (D890)
  wiced.py              Cypress WICED OTA (D578/D878)
  client.py  gui.py     backend selector + Tkinter GUI (builds the two-tab window)
  extract_auth_lib.py   one-time: pull libjl_ota_auth.so out of the OTA APK
  assets/               AesApp branding + the radio/boards step photos
radio_fw/               Radio and Boards tab (serial firmware/board updates, D890)
  spec.py               the four targets: labels, file types, WRITE_ORDER, entry combos
  compiler.py           vendor files -> validated wire artifact + manifest
  engines.py            the four serial wire engines (pyserial)
  gui_tab.py            the guided one-target-at-a-time wizard
  vendor/fwupd_*.py     the precompilers (stdlib-only, shared with the server)
bt_ota_gui.py           GUI entry point (used by both PyInstaller specs)
bt_ota_gui.spec         PyInstaller — macOS .app
bt_ota_gui_win.spec     PyInstaller — Windows .exe (onefile)
build_windows.ps1       Windows build driver (venv + deps + PyInstaller)
make_assets.py          render the logo/icons (.icns, .ico, in-app PNGs)
sign_and_notarize.sh    macOS Developer ID sign + notarize + staple
entitlements.plist      hardened-runtime entitlements
tests/test_engines.py   the serial wire engines' regression suite (fake port + device doubles)
```

## Run from source

```bash
pip install -r bt_ota/requirements.txt        # bleak + pyserial
# one-time: pull the auth lib out of the OTA APK you already have
python -m bt_ota.extract_auth_lib "OTA V1.7.2.apk"

python -m bt_ota scan                          # find the radio (put BT in pairing mode)
python -m bt_ota upgrade ET25_QXDZ_V1024.ufw          --name D890UV   # D890  (.ufw)
python -m bt_ota upgrade B707_..._ET12_QX-V10046.bin  --name D578UV   # D578/878 (.bin)
python -m bt_ota gui                           # graphical: both tabs (BT module + radio/boards)
```

The auth `.so` is **not** committed (it's third-party, extracted from the APK for
interop). Regenerate it with `extract_auth_lib` as shown; every build step below
expects it present at `bt_ota/libjl_ota_auth.so`.

## Build the desktop apps

The GUI needs Tk, so **build with a Python that has tkinter** (not the minimal
NuGet/embeddable distributions).

### macOS (`.app`)

```bash
pip install pyinstaller pillow
python make_assets.py "/path/to/AesApp-logo.jpg"   # optional icon/logo refresh
pyinstaller --noconfirm --clean bt_ota_gui.spec    # -> dist/AesApp Radio Updater.app
```

Distribute without Gatekeeper warnings by signing with a **Developer ID
Application** cert and notarizing:

```bash
CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
NOTARY_PROFILE=aesapp-notary \
./sign_and_notarize.sh "dist/AesApp Radio Updater.app"
```

### Windows (`.exe`)

On a real Windows machine, install a python.org Python (which includes tkinter)
and run:

```powershell
powershell -ExecutionPolicy Bypass -File build_windows.ps1 -Python "C:\path\to\python.exe"
# -> dist\AesApp Radio Updater.exe   (single self-contained file)
```

The `.exe` architecture matches the Python you build with. **x64 covers all
modern Windows** (Intel/AMD 64-bit natively, and Windows-on-ARM via its built-in
x64 emulation); build with an x86 Python only if you specifically need to run on
32-bit Windows. `build_windows.ps1` normalizes the process arch env before
PyInstaller, so it also cross-builds correctly from a Windows-on-ARM host where an
emulated x64/x86 Python otherwise mis-reports `platform.machine()`.

## Safety

- The **Bluetooth Module Update** tab writes the Bluetooth co-processor firmware
  only — not the main radio firmware or the codeplug.
- The **Radio and Boards Updates** tab writes the main firmware and the on-board
  DSP/NR/asset flash. **None of those protocols verifies or reads anything back**,
  and an interrupted write can leave a radio that will not boot — so the tab
  requires an explicit acknowledgement before the first write, warns before an
  abort, and never writes the main firmware until last.
- Keep the radio powered and connected for the whole of any update.
- Only ever load files built for your exact model.
- The app shows a disclaimer on first run that must be accepted.

## License & attribution

**BSD 3-Clause License** — © 2026 AesApp Inc. See [`LICENSE`](LICENSE).
Website: <https://aes.app/>

Third-party components and trademarks are credited in
[`bt_ota/THIRD_PARTY_NOTICES.txt`](bt_ota/THIRD_PARTY_NOTICES.txt) (JieLi
`jl_bt_ota` under Apache-2.0; AnyTone is a trademark of Qixiang Electron Science &
Technology Co., Ltd; JieLi of Zhuhai Jieli Technology; WICED of Infineon/Cypress).
This project is not affiliated with, endorsed by, or sponsored by any of them.
