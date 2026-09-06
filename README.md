# AesApp Radio Updater

A desktop app for updating AnyTone radios, with four tabs:

- **Bluetooth Module Update** — updates the **Bluetooth-module firmware** over the
  computer's own Bluetooth adapter, no Android phone required.
- **Radio and Boards Updates** — updates the D890's **main radio firmware**,
  **icon/font flash**, **NR (noise-reduction) daughterboard**, and **SCT3288
  baseband DSP** over a USB serial cable.
- **Digital Contact Refresh** — replaces the radio's **digital-contact database**
  (the caller-ID names shown for DMR IDs) with one of the lists built daily on
  the AesApp server, over the USB cable. Opened from the Tools page on
  cps.aes.app.
- **Write Codeplug** — writes a codeplug prepared on cps.aes.app to the radio
  over USB, at full speed rather than at Web Serial's. Opened from that
  codeplug's own write dialog. The tab is **hidden until such a link opens it**:
  there is nothing to do in it without one.

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

## Digital Contact Refresh (over serial)

Start from the **Tools page on cps.aes.app** (any signed-in account — no paid
plan needed): its “Open in AesApp Radio Updater” button opens this app through an
`aesapp://contacts?token=…` link. The link is one-time and short-lived; the app
claims it for a one-hour server session, identifies the connected radio over
its COM port, lists the contact lists that radio can hold (a D878UV / D578UV
holds 200 000 contacts, the newer models 500 000), and writes the one you pick.

| Radio | Contact format | Notes |
|-------|----------------|-------|
| **D878UV / D878UVII / D578 / D168UV** | 878-family DMR store | 578 and 168 enabled by the server once validated |
| **D890UV** (and the DMR-7X2 rebadge) | 890 DMR store + optional NXDN list | both lists go in one session |

The artifact the app downloads **is the exact block stream the factory CPS
sends** (sha256-verified, cached by hash); the app never encodes a contact. It
is streamed in one PC-mode session — `PROGRAM` → identity → one 16-byte frame
per ACK → `END` — the same protocol the browser-based CPS uses for contacts,
ported to pyserial in `radio_contacts/engine.py`. Contact sectors are separate
from the codeplug: **channels, zones and settings are never touched**, and an
interrupted write is recovered by simply running the refresh again.

How the link reaches the app:

- **macOS** — the `.app` declares the `aesapp` URL scheme; LaunchServices
  registers it the first time the app is launched (or dropped into
  /Applications) and delivers the link whether the app was closed or already
  running.
- **Windows** — the `.exe` registers the scheme for the current user on its
  first launch (no installer, no admin). A link starts the exe with the URL as
  its argument; if the app is already running, the new process hands the link
  to it and exits.
- Either way, the tab has a **Paste link** box for when the hand-off does not
  fire.

## Write Codeplug (over serial)

Start from the codeplug on **cps.aes.app**: “Write Codeplug to Radio” → “Open in
the desktop app” opens this app through an `aesapp://codeplug?token=…` link.
Writing a D168UV/D578UVII/D878UVII from a browser runs at roughly a tenth of the
factory CPS's speed; this app writes the very same prepared bytes over pyserial
without that penalty.

**The app never builds or edits a codeplug.** The web app owns the encoder and
has already produced the exact block stream — identity override, contact
selection and all — so this tab downloads it, writes it, reads it back, and
reports what happened.

While it works, **the project is read-only on the server**. That lock is a
lease: each progress report renews it for ten minutes, so if this app is
force-quit or the machine sleeps, the lease simply runs out and the owner gets
their project back — no stuck project, ever. Finishing, failing or cancelling
releases it immediately.

The session shape, which is not negotiable:

1. **Codeplug**, committed with its own `END`. It goes first because a session
   that holds the codeplug uncommitted while a multi-megabyte contact list
   streams has been observed to corrupt stray codeplug bytes. The factory CPS
   splits it the same way.
2. **Digital contact list**, if the operator asked for one on the web: its own
   session after the restart the first commit causes.
3. **Read-back verify** of the codeplug. A mismatch is reported as *verified
   badly*, not as a failed write — the bytes are committed either way, and the
   operator is told to write again before using the radio. A radio that will not
   reopen after a commit is reported as *unverified*, which is routine
   post-commit behaviour and not a fault.

The radio is identified before the first block goes out, against the identity
tokens the server sends for that codeplug — matched exactly, because a
generation-1 D578UV/D878UV answers with a strict prefix of its generation-2
sibling's token.

The link reaches the app exactly as the contact-refresh link does (see above),
and the tab has the same **Paste link** box for when the hand-off does not fire.

## Layout

```
bt_ota/                 Bluetooth-module tab (CLI + GUI + BLE protocol backends)
  jl_auth.py            JieLi RcspAuth (pure-Python SAFER+ over the native .so's tables)
  rcsp.py  ota.py       JieLi RCSP codec + BLE OTA state machine (D890)
  wiced.py              Cypress WICED OTA (D578/D878)
  client.py  gui.py     backend selector + Tkinter GUI (builds the tabbed window)
  extract_auth_lib.py   one-time: pull libjl_ota_auth.so out of the OTA APK
  assets/               AesApp branding + the radio/boards step photos
radio_fw/               Radio and Boards tab (serial firmware/board updates, D890)
  spec.py               the four targets: labels, file types, WRITE_ORDER, entry combos
  compiler.py           vendor files -> validated wire artifact + manifest
  engines.py            the four serial wire engines (pyserial)
  gui_tab.py            the guided one-target-at-a-time wizard
  vendor/fwupd_*.py     the precompilers (stdlib-only, shared with the server)
radio_codeplug/         Write Codeplug tab (writes a codeplug prepared on the web app)
  client.py             the job session: claim, download, heartbeat, outcome
  engine.py             write + commit + read-back verify, over radio_contacts' wire primitives
  gui_tab.py            the tab
radio_contacts/         Digital Contact Refresh tab (serial contact-list write)
  segments.py           the "CBSEG1" contact-bundle container decoder
  catalog.py            the server client (launch-token session, catalog, sha256-verified artifacts)
  engine.py             the PC-mode wire engine (PROGRAM / identity / W-frames / END)
  launch.py             aesapp:// links: parsing, server allow-list, Windows scheme registration, single instance
  gui_tab.py            the tab
bt_ota_gui.py           GUI entry point (used by both PyInstaller specs)
bt_ota_gui.spec         PyInstaller — macOS .app
bt_ota_gui_win.spec     PyInstaller — Windows .exe (onefile)
build_windows.ps1       Windows build driver (venv + deps + PyInstaller)
make_assets.py          render the logo/icons (.icns, .ico, in-app PNGs)
sign_and_notarize.sh    macOS Developer ID sign + notarize + staple
entitlements.plist      hardened-runtime entitlements
tests/test_engines.py   the serial wire engines' regression suite (fake port + device doubles)
tests/test_contacts_*.py  the contact refresh: container, engine (PC-mode radio double), server client, links
```

## Run from source

```bash
pip install -r bt_ota/requirements.txt        # bleak + pyserial
# one-time: pull the auth lib out of the OTA APK you already have
python -m bt_ota.extract_auth_lib "OTA V1.7.2.apk"

python -m bt_ota scan                          # find the radio (put BT in pairing mode)
python -m bt_ota upgrade ET25_QXDZ_V1024.ufw          --name D890UV   # D890  (.ufw)
python -m bt_ota upgrade B707_..._ET12_QX-V10046.bin  --name D578UV   # D578/878 (.bin)
python -m bt_ota gui                           # graphical: all tabs (BT module, radio/boards, contact refresh, codeplug)
python -m bt_ota gui "aesapp://contacts?token=…"   # open the Digital Contact Refresh tab with a link
python -m bt_ota gui "aesapp://codeplug?token=…"   # open the Write Codeplug tab with a link
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
