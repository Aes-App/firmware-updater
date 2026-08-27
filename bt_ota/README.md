# bt_ota — AnyTone Bluetooth-module firmware updater (no phone needed)

Updates the **Bluetooth module** firmware on AnyTone radios directly from a
computer over its own Bluetooth adapter, replacing the official phone-only apps.
Two module families are supported, auto-selected by the firmware file type:

| Radio | BT module | Firmware | Protocol | Phone app |
|-------|-----------|----------|----------|-----------|
| **D890UV** | JieLi ET25 | `.ufw` | JieLi RCSP (+auth, device-pull) | `OTA V1.x.apk` / "JL OTA" |
| **D578 / D878** | Cypress WICED ET12 | `.bin` | WICED OTA (no auth, host-push) | `OTA _New tool_V3.0.apk` |

The two are completely different silicon and protocols — reverse-engineered from
their respective Android apps. **Both are validated on hardware:** D890 (JieLi)
end-to-end incl. the single-backup reboot→reconnect→resume, and D578/D878 (WICED)
confirmed by flashing a version-bumped image and seeing the radio's BT version
change. See "Status" below.

## The D890 path — JieLi RCSP

The radio's BT chip is a **Zhuhai JieLi (杰理)** BLE module. Its device name
advertises as `ET25SE_BLE_xxxxx` (or your renamed BT name). The app speaks
JieLi's **RCSP** protocol over a GATT service:

| Role   | UUID                                   |
|--------|----------------------------------------|
| service| `0000ae00-0000-1000-8000-00805f9b34fb` |
| write  | `0000ae01-…` (host → device)           |
| notify | `0000ae02-…` (device → host)           |

**Frame:** `FE DC BA | flags | opcode | len(2, big-endian) | payload | EF`
(`flags` bit7 = command/response, bit6 = needs-response).

**OTA sequence** (opcodes): `GetTargetInfo 0x03` → `GetUpdateFileOffset 0xE1`
→ `InquireUpdate 0xE2` (device validates the `.ufw`) → `EnterUpdateMode 0xE3`
→ the **device then pulls** blocks with `0xE5 [offset:4][len:2]` and we answer
with the raw `.ufw` bytes, progress via `0xE8`, until it requests `offset=0,
len=0` (done) → `GetUpdateStatus 0xE6` → the module reboots to apply.

**Auth:** before any OTA command the app runs a **mutual challenge/response**
(JieLi "RcspAuth"): host sends `00`+16 random, device replies `01`+`E(random)`,
host replies `02 "pass"`, and vice-versa. `E()` is a fixed SAFER+/Bluetooth-E1
style cipher with a **key baked into `libjl_ota_auth.so`** (the app never sets a
custom key). We run that real native function under the Unicorn CPU emulator, so
the crypto is byte-exact and host-portable — no need to re-derive it.

The `.ufw` itself is a scrambled JieLi container; **we never need to unscramble
it** — the module's own bootloader does that. We stream the raw bytes it asks
for. (`validate` runs the native header check for a sanity CRC only.)

## The D578/D878 path — Cypress WICED OTA

These radios use a **Cypress/Infineon WICED** BLE module (ET12, `B707…`), and a
different app (`OTA _New tool_V3.0.apk`, `com.example.otasample`). Its GATT
"Firmware Upgrade" service is the standard WICED one, with **no authentication**:

| Role | UUID |
|------|------|
| service | `9e5d1e47-5c13-43a0-8635-82ad38a1386f` |
| control point | `e3dd50bf-…` (write cmd, notify status) |
| data | `92e86c7a-…` (firmware bytes) |

The host **pushes** the raw `.bin` (no container): enable notify on the control
point → `PREPARE_DOWNLOAD [01][size:4 LE]` → `DOWNLOAD [02]` → stream the image
in **20-byte** writes to the data char → `VERIFY [03][crc32:4 LE]` (standard
CRC-32) → the module reboots to apply. Each control-point command is answered by
a 1-byte status notification (`0` = OK). No auth, no `.so`, no reconnect — much
simpler than the JieLi path.

Hardware note: this module **verifies the CRC and reboots to apply immediately**,
dropping the BLE link *before* its final verify-status notification is delivered.
Android wins that race; macOS/bleak loses it, so the last step surfaces as a
"disconnect." The tool treats a disconnect right at verify (after a full,
with-response transfer) as **probable success** and tells you to confirm the BT
version on the radio — verified by flashing a version-bumped image and watching
the radio's version change.

`bt_ota` picks the backend from the file extension: `.ufw` → JieLi, `.bin` →
WICED (`bt_ota/client.py`).

## Install

```bash
pip install -r requirements.txt        # bleak + unicorn
# one-time: pull the auth lib from the OTA APK you already have
python -m bt_ota.extract_auth_lib "OTA V1.7.2.apk"
```

## Use

```bash
python -m bt_ota scan                       # find the radio (put BT in pairing mode)
python -m bt_ota info      --name D890UV     # connect + auth + print device info (D890 only)
python -m bt_ota validate  ET25_QXDZ_V1024.ufw
python -m bt_ota upgrade   ET25_QXDZ_V1024.ufw           --name D890UV   # D890  (.ufw)
python -m bt_ota upgrade   B707_..._ET12_QX-V10046.bin   --name D578UV   # D578/878 (.bin)
```

Renaming the radio's BT name to e.g. `D890UV` makes it easy to match with
`--name`. On macOS `--address` is a CoreBluetooth UUID; on Linux it's a MAC.

## GUI / standalone app

A Tkinter GUI (Scan → pick radio → pick `.ufw` → Connect & Write, with a log +
progress + status bar):

```bash
python -m bt_ota gui
```

Build a self-contained macOS app (bundles Python, bleak, unicorn, and the auth
`.so`; sets the Bluetooth-usage plist key macOS requires) with PyInstaller:

```bash
# from the repo root
pip install pyinstaller pillow     # + `brew install python-tk@3.14` for Tk
python make_assets.py "/path/to/AesApp-logo.jpg"   # optional: app icon + in-app logo
pyinstaller --noconfirm --clean bt_ota_gui.spec
# -> dist/AesApp Radio Updater.app
```

`make_assets.py` renders the logo into `bt_ota/assets/` (an `aesapp_logo.png`
for the window/disclaimer and an `AesApp.icns` app icon); the spec picks them up
if present and works without them too. On first run the app shows a disclaimer
that must be accepted (stored per-user under Application Support).

The app is ad-hoc signed, so first launch: right-click ▸ Open (or
`xattr -dr com.apple.quarantine "AesApp Radio Updater.app"`). On the first Scan
macOS asks for Bluetooth permission — Allow it. (For Windows, build with
`build_windows.ps1` + `bt_ota_gui_win.spec`; the plist keys here are macOS-only.)

### Signing & notarizing (Developer ID)

To distribute without Gatekeeper warnings, sign with a **Developer ID
Application** certificate and notarize:

```bash
# one-time: create the cert (Xcode ▸ Settings ▸ Accounts ▸ Manage Certificates
#   ▸ + ▸ Developer ID Application), then store notary credentials:
xcrun notarytool store-credentials aesapp-notary \
    --apple-id "you@example.com" --team-id "TEAMID" --password "app-specific-password"

# each release:
CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
NOTARY_PROFILE=aesapp-notary \
./sign_and_notarize.sh "dist/AesApp Radio Updater.app"
```

`sign_and_notarize.sh` signs every embedded binary with the hardened runtime +
`entitlements.plist` (JIT + executable memory for unicorn, library-validation
off for the bundled dylibs), verifies, submits to Apple's notary service,
staples the ticket, and repackages the distributable zip.

## Status

**Hardware-validated.** A D890UV BT module was flashed end-to-end from macOS:
auth → transfer → status 128 → automatic reboot/reconnect → bootloader pass →
`result 0`. Two device quirks the code handles (learned on hardware):

- The running firmware sends **no reply to enter-update-mode** and just starts
  pulling blocks; the bootloader pass *does* reply. Both are tolerated.
- When `GetUpdateFileOffset` returns `(0,0)` the inquire must carry a single
  **priority byte** (`0x00`), not an empty payload.

Offline suite (no hardware) — run `python -m bt_ota.selftest ET25_QXDZ_V1024.ufw`:
RCSP framing (against the app's own known auth vector), block-transfer parsing,
the **full mutual auth handshake** (host ↔ a modelled device, real emulated
crypto), and native `.ufw` validation.

Not yet exercised: double-backup devices (single connection, no reconnect — a
simpler subset of what's tested) and single-backup devices whose bootloader
re-advertises on a *different* address (there's a scan-by-name reconnect
fallback, but it's untested).

## Safety / caveats

- **BT-module OTA only.** This updates the Bluetooth co-processor firmware, not
  the main radio firmware or the codeplug.
- Keep the radio powered and near the computer for the whole update.
- JieLi OTA keeps the running firmware until the new image is verified, so a
  failed attempt normally just reverts. **Single-backup** devices reboot into a
  bootloader after the transfer; the tool drives that automatically
  (`status 128` → `changeCommunicationWay 0x0B` → reconnect + resume). Keep the
  radio powered so the bootloader can finish once it starts.
- Only ever feed a `.ufw` built for your exact model.

## Files

| file | purpose |
|------|---------|
| `jl_auth.py` | Unicorn oracle for `libjl_ota_auth.so` + the auth handshake |
| `rcsp.py`    | RCSP frame codec, opcodes, TargetInfo parser |
| `ota.py`     | `bleak` BLE client + OTA state machine |
| `__main__.py`| CLI (`scan` / `info` / `validate` / `upgrade`) |
| `selftest.py`| offline tests |
| `extract_auth_lib.py` | pull `libjl_ota_auth.so` from the OTA APK |
