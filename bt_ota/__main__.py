"""CLI for the AnyTone Bluetooth-module OTA updater.

Supports two module families, chosen by firmware type:
  * D890         -> .ufw  (JieLi ET25, RCSP + auth)
  * D578 / D878  -> .bin  (Cypress WICED OTA)

    python -m bt_ota scan
    python -m bt_ota info      [--name D890UV | --address <addr>]     # D890 only
    python -m bt_ota validate  ET25_QXDZ_V1024.ufw
    python -m bt_ota upgrade   ET25_QXDZ_V1024.ufw            [--name D890UV] [--yes]
    python -m bt_ota upgrade   B707_..._ET12_QX-V10046.bin    [--name D578UV] [--yes]

Requires: pip install bleak unicorn   (unicorn only for the D890 .ufw auth;
extract libjl_ota_auth.so with: python -m bt_ota.extract_auth_lib "OTA V1.7.2.apk")
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time


def _log(msg: str) -> None:
    print(f"[aesapp_bt_ota] {msg}", flush=True)


async def _pick_device(name: str | None, address: str | None, timeout: float):
    from .client import scan_devices
    if address:
        return address
    _log(f"scanning {timeout:.0f}s for AnyTone radios...")
    cands = await scan_devices(timeout=timeout, name_filter=name)
    if not cands:
        _log("no candidate devices found. Put the radio's Bluetooth in pairing mode "
             "and make sure it is not connected to a phone.")
        return None
    for dev, rssi, nm in cands:
        _log(f"  {nm or dev.name or '(no name)':24} {dev.address}  rssi={rssi}")
    if len(cands) > 1 and not name:
        _log("multiple devices; re-run with --name or --address to choose one.")
        return None
    return cands[0][0]


async def cmd_scan(args) -> int:
    from .client import scan_devices
    cands = await scan_devices(timeout=args.timeout, name_filter=args.filter)
    if not cands:
        print("no candidate radios found.")
        return 1
    print(f"{'NAME':26} {'ADDRESS':38} RSSI")
    for dev, rssi, nm in cands:
        print(f"{(nm or dev.name or '(no name)'):26} {dev.address:38} {rssi}")
    return 0


async def cmd_info(args) -> int:
    from .ota import AnytoneBtOta, OtaError
    target = await _pick_device(args.name, args.address, args.timeout)
    if target is None:
        return 1
    ota = AnytoneBtOta(auth_lib=args.auth_lib, use_auth=not args.no_auth, log_cb=_log)
    try:
        await ota.connect(target)
        info = await ota.get_target_info()
    except OtaError as e:
        _log(f"error: {e}")
        return 1
    finally:
        await ota.disconnect()
    print("\nDevice info:")
    for k, v in vars(info).items():
        if k == "raw":
            continue
        if v not in ("", 0, False):
            print(f"  {k:24}: {v}")
    mode = "double-backup (safe: survives a failed flash)" if info.support_double_backup \
        else "SINGLE-backup (flash needs reboot+reconnect+resume)"
    print(f"  {'backup_mode':24}: {mode}")
    if args.debug:
        print("\n[debug] raw GetTargetInfo response:")
        print(f"  bytes ({len(ota.last_target_info_raw)}): {ota.last_target_info_raw.hex()}")
        print("  decoded TLV records (type -> data):")
        for name, hexs in info.raw.items():
            print(f"    {name:16}: {hexs}")
    return 0


def cmd_validate(args) -> int:
    import zlib
    from .client import firmware_kind
    fw = open(args.ufw, "rb").read()
    kind = firmware_kind(args.ufw)
    print(f"file: {args.ufw}  ({len(fw)} bytes, {kind})")
    if kind == "wiced":
        print(f"WICED raw firmware .bin — crc32=0x{zlib.crc32(fw) & 0xFFFFFFFF:08X}")
        print("Note: WICED OTA has no container to validate; the module checks the CRC at verify time.")
        return 0
    if kind == "jieli":
        from .jl_auth import AuthEmulator
        code = AuthEmulator(args.auth_lib).validate_ufw(fw)
        if code == -1:
            print("INVALID: header CRC mismatch (corrupt file)")
            return 1
        if code == -2:
            print("INVALID: declared firmware size exceeds file (truncated)")
            return 1
        print(f"VALID: JLUFW header CRC + size checks pass (native code {code}).")
        print("Note: final acceptance is decided by the radio's E2 inquire at upgrade time.")
        return 0
    print("unknown firmware type (expected .ufw for D890 or .bin for D578/D878).")
    return 1


async def cmd_upgrade(args) -> int:
    from .client import make_client, firmware_kind
    kind = firmware_kind(args.ufw)
    if kind == "unknown":
        _log("firmware must be a .ufw (D890) or .bin (D578/D878) file.")
        return 1
    fw = open(args.ufw, "rb").read()

    # JieLi .ufw: sanity-validate before touching the radio
    if kind == "jieli":
        try:
            from .jl_auth import AuthEmulator
            code = AuthEmulator(args.auth_lib).validate_ufw(fw)
            if code in (-1, -2):
                _log(f"refusing to flash: ufw failed validation (code {code}).")
                return 1
        except Exception as e:
            _log(f"(could not pre-validate ufw: {e})")

    target = await _pick_device(args.name, args.address, args.timeout)
    if target is None:
        return 1

    if not args.yes:
        print(f"\nAbout to flash '{args.ufw}' ({len(fw)} bytes, {kind}) to the radio's BT module.")
        print("Keep the radio still and powered during the update. Continue? [y/N] ", end="")
        if input().strip().lower() not in ("y", "yes"):
            print("aborted.")
            return 1

    state = {"t": time.time()}

    def progress(sent: int, total: int) -> None:
        pct = (sent * 100.0 / total) if total else 0.0
        now = time.time()
        if now - state["t"] >= 0.3 or sent >= total:
            bar = "#" * int(pct / 2.5)
            print(f"\r  [{bar:<40}] {pct:5.1f}%  {sent}/{total}", end="", flush=True)
            state["t"] = now

    client, _ = make_client(args.ufw, use_auth=not args.no_auth, auth_lib=args.auth_lib, log_cb=_log)
    try:
        await client.connect(target)
        await client.upgrade(fw, progress_cb=progress)
        print()
        _log("SUCCESS")
        return 0
    except Exception as e:  # OtaError / WicedOtaError
        print()
        _log(f"FAILED: {e}")
        return 1
    finally:
        await client.disconnect()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bt_ota", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--auth-lib", help="path to libjl_ota_auth.so (default: bundled)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="list nearby AnyTone radios (D890 + D578/D878)")
    s.add_argument("--timeout", type=float, default=8.0)
    s.add_argument("--filter", help="only show names/addresses containing this")

    i = sub.add_parser("info", help="connect and print device info")
    i.add_argument("--name")
    i.add_argument("--address")
    i.add_argument("--timeout", type=float, default=8.0)
    i.add_argument("--no-auth", action="store_true")
    i.add_argument("--debug", action="store_true", help="dump raw GetTargetInfo TLV")

    v = sub.add_parser("validate", help="check a firmware file offline (.ufw or .bin)")
    v.add_argument("ufw", metavar="firmware", help=".ufw (D890) or .bin (D578/D878)")

    sub.add_parser("gui", help="launch the graphical updater")

    u = sub.add_parser("upgrade", help="flash firmware to the radio's BT module")
    u.add_argument("ufw", metavar="firmware", help=".ufw (D890) or .bin (D578/D878)")
    u.add_argument("--name")
    u.add_argument("--address")
    u.add_argument("--timeout", type=float, default=8.0)
    u.add_argument("--no-auth", action="store_true")
    u.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "gui":
        from .gui import main as gui_main
        gui_main()
        return 0
    if args.cmd == "validate":
        return cmd_validate(args)
    coro = {"scan": cmd_scan, "info": cmd_info, "upgrade": cmd_upgrade}[args.cmd](args)
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        print("\ninterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
