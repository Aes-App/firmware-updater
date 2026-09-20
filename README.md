# AesApp Radio Updater

A desktop app for updating **AnyTone radios** — Bluetooth-module firmware over
the computer's own Bluetooth adapter, and main firmware, boards, contacts and
codeplugs over a USB cable. No Android phone and no vendor CPS required.

Ships as a macOS `.app`, a Windows `.exe` and a CLI. It works on its own, and
pairs with [cps.aes.app](https://cps.aes.app), which can hand a job straight to
it through an `aesapp://` link.

## What it does

| Tab | What it writes | Radios | Files it takes |
|-----|----------------|--------|----------------|
| **Bluetooth Module Update** | Bluetooth-module firmware, over Bluetooth | D878UV, D878UVII, D578UV, D578UVII, D890UV | `.ufw` (D890UV), `.bin` (D578 and D878 families) |
| **Radio and Boards Updates** | radio firmware, icons and fonts, and the boards each radio has | D878UV, D878UVII, D890UV | `.CDD` + `.CDI` (+ optional `.spi`), `.ufw`, `.hex` |
| **Digital Contact Refresh** | the digital-contact database (the caller-ID names shown for DMR IDs) | D168UV, D578UV, D578UVII, D878UV, D878UVII, D890UV | a list from your cps.aes.app account, or your own `user.csv` and `nxdn.csv` |
| **Write Codeplug** | a codeplug prepared on cps.aes.app | D168UV, D578UVII, D878UVII, D890UV | none — it arrives with the link |

Notes worth knowing before you use it:

- **Digital Contact Refresh** takes either a list you uploaded to cps.aes.app or
  a register download of your own, which it turns into the radio's contact
  database on your machine — pick the countries you want, connect, write.
  Nothing is uploaded on that path.
- **Radio and Boards Updates** offers what each radio actually has: a D890UV
  takes firmware, icons and fonts, the SCT3288 baseband and the NR board; a
  D878UV or D878UVII takes firmware, icons and fonts, and the APRS/Bluetooth
  board. Targets are written with the main firmware last, so a failure part-way
  through leaves the radio still bootable.
- **Write Codeplug** is hidden until a link opens it: there is nothing to do in
  it without one.
- Protocol notes for the Bluetooth side are in
  [`bt_ota/README.md`](bt_ota/README.md).
- The app checks GitHub for a newer release on startup and offers a link. It
  never downloads or installs one. `AESAPP_NO_UPDATE_CHECK=1` turns it off.

## Run from source

```bash
pip install -r bt_ota/requirements.txt        # bleak + pyserial
# one-time: pull the auth lib out of an OTA APK you already have
python -m bt_ota.extract_auth_lib "OTA V1.7.2.apk"

python -m bt_ota gui                          # the app, all tabs
python -m bt_ota scan                         # CLI: find a radio in pairing mode
python -m bt_ota upgrade ET25_QXDZ_V1024.ufw --name D890UV
python -m bt_ota gui "aesapp://contacts?token=…"   # open on a link, as the web app does
```

The auth `.so` is third-party and **not committed**. Every build below expects
it at `bt_ota/libjl_ota_auth.so`.

Tests: `python -m pytest tests/ -q` (the GUI ones skip without a display).

## Build

The GUI needs Tk, so build with a Python that has tkinter — not the minimal
NuGet or embeddable distributions.

**macOS**

```bash
pip install pyinstaller pillow
pyinstaller --noconfirm --clean bt_ota_gui.spec     # -> dist/AesApp Radio Updater.app

# to ship it without Gatekeeper warnings:
CODESIGN_IDENTITY="Developer ID Application: … (TEAMID)" NOTARY_PROFILE=aesapp-notary \
  ./sign_and_notarize.sh "dist/AesApp Radio Updater.app"
```

**Windows**

```powershell
powershell -ExecutionPolicy Bypass -File build_windows.ps1 -Python "C:\path\to\python.exe"
# -> dist\AesApp Radio Updater.exe   (single self-contained file)
```

The `.exe` architecture matches the Python you build with; **x64 covers all
modern Windows**, including Windows-on-ARM through its x64 emulation. The script
runs the fast test suites first, then checks the built archive really contains
the lazily-imported tabs (`tools/check_frozen_modules.py`). `-SkipTests` skips
the first of those and says so.

`python make_assets.py "/path/to/AesApp-logo.jpg"` regenerates the icons.

## Layout

```
bt_ota/                 Bluetooth-module tab, CLI and BLE protocol backends
  jl_auth.py            JieLi RcspAuth (pure Python)
  rcsp.py  ota.py       JieLi RCSP + BLE OTA state machine (D890UV)
  wiced.py              Cypress WICED OTA (D578/D878)
  client.py  gui.py     backend selector + the Tkinter window (builds the tabs)
  update_check.py       "is there a newer release?"
  extract_auth_lib.py   one-time: pull libjl_ota_auth.so out of the OTA APK
  assets/               branding and the step photos
radio_fw/               Radio and Boards tab
  spec.py               the four targets: files, write order, entry combos
  compiler.py           vendor files -> validated wire artifact
  engines.py            the four serial wire engines
  vendor/fwupd_*.py     the precompilers (shared with the server)
radio_contacts/         Digital Contact Refresh tab
  catalog.py            the cps.aes.app client (session, lists, downloads)
  contact_build.py      builds a contact database here, from your own CSVs
  contact_tables.py     generated country tables (tools/gen_contact_tables.py)
  engine.py             the USB PC-mode wire engine
  segments.py launch.py the download container; aesapp:// links
radio_codeplug/         Write Codeplug tab (client, write/verify engine)
tests/                  pytest; fixtures/ holds the byte-for-byte contact oracle
tools/                  generators and build checks
bt_ota_gui.spec         PyInstaller — macOS .app
bt_ota_gui_win.spec     PyInstaller — Windows .exe
build_windows.ps1       Windows build driver
sign_and_notarize.sh    macOS sign + notarize + staple
```

## Safety

- **Radio and Boards Updates** writes main firmware and board flash. Those
  protocols read nothing back, so an interrupted write can leave a radio that
  will not boot. The tab asks for an explicit acknowledgement and always writes
  main firmware last.
- **Digital Contact Refresh** replaces the whole contact database and keeps no
  backup of it. Your codeplug — channels, zones, settings — is untouched, and an
  interrupted contact write is safe to redo.
- **Bluetooth Module Update** writes the Bluetooth co-processor only.
- Keep the radio powered and connected throughout, and only load files built for
  your exact model.

## License

**BSD 3-Clause** — © 2026 AesApp Inc. See [`LICENSE`](LICENSE). Website:
<https://aes.app/>

Third-party components and trademarks are credited in
[`bt_ota/THIRD_PARTY_NOTICES.txt`](bt_ota/THIRD_PARTY_NOTICES.txt) (JieLi
`jl_bt_ota` under Apache-2.0; AnyTone is a trademark of Qixiang Electron Science
& Technology Co., Ltd; JieLi of Zhuhai Jieli Technology; WICED of
Infineon/Cypress). This project is not affiliated with, endorsed by, or
sponsored by any of them.
