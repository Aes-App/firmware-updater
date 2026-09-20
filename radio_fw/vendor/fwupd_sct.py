from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple, Tuple

MAGIC = b"\x84\xa9\x61"

ACK_PARITY_DISABLE = "84a9610002001706"
ACK_PARITY_ENABLE = "84a9610002001600"
ACK_FLASH_INITIAL = "84a96100040093002fb8"
ACK_SEG_PARITY = "84a96100040016002f3d"
ACK_WRITE = "84a96100040394002fbc"

ERASE_USER_FLASH_WITH_VOCODE = 1
ERASE_SYSTEM_FLASH = 2
ERASE_VOCODE3 = 3
ERASE_VOCODE1 = 5
ERASE_VOCODE2 = 6
ERASE_VOCODE4 = 10
ERASE_VOCODE6 = 12
ERASE_VOCODE7 = 13
ERASE_VOCODE5 = 15


def region_byte(erase_type: int) -> int:
    return ((erase_type << 1) | 1) & 0xFF


SEGMENT_ERASE_TYPE: Dict[int, int] = {
    0x000100: ERASE_USER_FLASH_WITH_VOCODE,
    0x022000: ERASE_VOCODE1,
    0x035000: ERASE_VOCODE4,
    0x039000: ERASE_SYSTEM_FLASH,
    0x040000: ERASE_VOCODE2,
    0x053000: ERASE_VOCODE6,
    0x056000: ERASE_VOCODE7,
    0x063000: ERASE_VOCODE3,
    0x077000: ERASE_VOCODE5,
}

REGION_TABLE: Dict[int, int] = {
    addr: region_byte(t) for addr, t in SEGMENT_ERASE_TYPE.items()
}

VENDOR_LADDER_BASES = frozenset(SEGMENT_ERASE_TYPE) | {0x05C000}

CAPTURE_PROVEN_REGIONS: Dict[int, int] = {
    0x000100: 0x03,
    0x022000: 0x0B,
    0x035000: 0x15,
    0x053000: 0x19,
    0x056000: 0x1B,
    0x063000: 0x07,
    0x077000: 0x1F,
}


class SctHexError(ValueError):
    pass


class HexRecord(NamedTuple):
    bank: int
    addr: int
    data: bytes

    @property
    def linear(self) -> int:
        return (self.bank << 16) | self.addr


def parse_sct_hex(raw: bytes) -> List[HexRecord]:
    records: List[HexRecord] = []
    bank = 0
    saw_eof = False
    for lineno, line in enumerate(raw.split(b"\n"), 1):
        line = line.strip()
        if not line:
            continue
        if saw_eof:
            raise SctHexError(f"line {lineno}: record after EOF (type-01) record")
        if not line.startswith(b":"):
            raise SctHexError(f"line {lineno}: does not start with ':'")
        if len(line) % 2 != 1:
            raise SctHexError(f"line {lineno}: odd number of hex digits")
        try:
            rec = bytes.fromhex(line[1:].decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            raise SctHexError(f"line {lineno}: non-hex characters") from None
        if len(rec) < 5:
            raise SctHexError(f"line {lineno}: record shorter than 5 bytes")
        cnt, typ = rec[0], rec[3]
        addr16 = (rec[1] << 8) | rec[2]
        if len(rec) != 5 + cnt:
            raise SctHexError(
                f"line {lineno}: length byte says {cnt} data bytes but the "
                f"record carries {len(rec) - 5}")
        if sum(rec) & 0xFF != 0:
            raise SctHexError(f"line {lineno}: record checksum invalid")
        data = rec[4:4 + cnt]

        if typ == 0x00:
            if cnt == 0:
                raise SctHexError(f"line {lineno}: zero-length data record")
            if addr16 + cnt > 0x10000:
                raise SctHexError(
                    f"line {lineno}: record wraps the 16-bit address space "
                    f"(addr {addr16:#06x} + {cnt} bytes)")
            records.append(HexRecord(bank, addr16, data))
        elif typ == 0x04:
            if cnt == 0:
                bank = addr16
            elif cnt == 2:
                bank = (data[0] << 8) | data[1]
            else:
                raise SctHexError(
                    f"line {lineno}: type-04 with {cnt} data bytes "
                    f"(expected 0 [bank-in-address form] or 2 [standard ULBA])")
            if bank > 0xFF:
                raise SctHexError(
                    f"line {lineno}: bank {bank:#x} does not fit the 1-byte "
                    f"wire field")
        elif typ == 0x01:
            if cnt != 0 or addr16 != 0:
                raise SctHexError(f"line {lineno}: malformed EOF record")
            saw_eof = True
        else:
            raise SctHexError(
                f"line {lineno}: unsupported record type {typ:#04x} -- the "
                f"SCT3288 wire mapping is defined for types 00/01/04 only")
    if not saw_eof:
        raise SctHexError("no EOF (type-01) record -- file truncated?")
    if not records:
        raise SctHexError("no data records")
    return records


def build_frame(mod: int, body: bytes, *, parity: bool = True) -> bytes:
    length = len(body) + (2 if parity else 0)
    frame = bytearray(MAGIC)
    frame += length.to_bytes(2, "big")
    frame.append(mod)
    frame += body
    if parity:
        frame.append(0x2F)
        x = 0
        for b in frame[3:]:
            x ^= b
        frame.append(x)
    if len(frame) % 2:
        frame.append(0x00)
    return bytes(frame)


def write_frame(rec: HexRecord) -> bytes:
    body = bytes([0x94, rec.bank, rec.addr >> 8, rec.addr & 0xFF,
                  len(rec.data)]) + rec.data
    return build_frame(0x03, body, parity=True)


def parity_disable_frame() -> bytes:
    return build_frame(0x00, b"\x16\x00", parity=True)


def parity_enable_frame() -> bytes:
    return build_frame(0x00, b"\x16\x01", parity=False)


def seg_parity_frame() -> bytes:
    return build_frame(0x00, b"\x16\x01", parity=True)


def flash_initial_frame(region: int) -> bytes:
    return build_frame(0x00, bytes([0x93, region]), parity=True)


def flash_end_frame() -> bytes:
    return flash_initial_frame(0x00)


def segment_starts(records: List[HexRecord]) -> List[int]:
    starts = [0]
    for i in range(1, len(records)):
        if records[i].linear != records[i - 1].linear + len(records[i - 1].data):
            starts.append(i)
    return starts


def compile_stream(records: List[HexRecord]) -> Tuple[bytes, dict]:
    starts = segment_starts(records)
    start_set = set(starts)
    for i in starts:
        if records[i].linear not in REGION_TABLE:
            raise SctHexError(
                f"segment starting at linear address {records[i].linear:#08x} "
                f"is not in the vendor's SCT3288 erase ladder "
                f"(SCT3252.cs:3248-3420), so no FLASH_INITIAL erase region is "
                f"known for it. Erasing the wrong region can brick the "
                f"baseband, so this is refused rather than guessed. If the "
                f"vendor tool really does flash this layout, add the address "
                f"and its InitFlashTypeEnum to "
                f"radio_fw.vendor.fwupd_sct.SEGMENT_ERASE_TYPE.")

    for i, rec in enumerate(records):
        if i not in start_set and rec.linear in VENDOR_LADDER_BASES:
            raise SctHexError(
                f"record {i} writes to {rec.linear:#08x}, which is an erase-region "
                f"base, but it continues the previous record contiguously so no "
                f"FLASH_INITIAL would be emitted for it. The vendor keys its erase "
                f"on the address alone and WOULD erase here, so this file needs a "
                f"capture before it can be compiled safely.")

    stream = bytearray()
    frame_index: List[Tuple[int, int, str]] = []
    controls: List[dict] = []
    segments: List[dict] = []

    def emit(frame: bytes, kind: str) -> None:
        frame_index.append((len(stream), len(frame), kind))
        stream.extend(frame)

    def emit_controls(before_frame: int, pairs: List[Tuple[bytes, str, str]]) -> None:
        controls.append({
            "before_frame": before_frame,
            "frames": [f.hex() for f, _, _ in pairs],
            "acks": [ack for _, _, ack in pairs],
        })
        for frame, kind, _ in pairs:
            emit(frame, kind)

    emit_controls(0, [
        (parity_disable_frame(), "parity_disable", ACK_PARITY_DISABLE),
        (parity_enable_frame(), "parity_enable", ACK_PARITY_ENABLE),
    ])

    next_start = {idx: pos for pos, idx in enumerate(starts)}
    seg_end = starts[1:] + [len(records)]
    written = 0
    for k, rec in enumerate(records):
        if k in next_start:
            pos = next_start[k]
            region = REGION_TABLE[rec.linear]
            pre: List[Tuple[bytes, str, str]] = []
            if k != 0:
                pre.append((seg_parity_frame(), "seg_parity", ACK_SEG_PARITY))
            pre.append((flash_initial_frame(region), "flash_initial",
                        ACK_FLASH_INITIAL))
            emit_controls(k, pre)
            last = records[seg_end[pos] - 1]
            segments.append({
                "region": region,
                "first_frame": k,
                "frames": seg_end[pos] - k,
                "addr_start": rec.linear,
                "addr_end": last.linear + len(last.data),
            })
        emit(write_frame(rec), "write")
        written += len(rec.data)

    emit_controls(len(records), [
        (seg_parity_frame(), "seg_parity", ACK_SEG_PARITY),
        (flash_end_frame(), "flash_end", ACK_FLASH_INITIAL),
        (parity_disable_frame(), "parity_restore", ACK_SEG_PARITY),
    ])

    artifact = bytes(stream)
    manifest = {
        "kind": "sct3288_baseband",
        "frames": len(records),
        "control_frames": sum(len(c["frames"]) for c in controls),
        "payload_bytes": written,
        "wire_bytes": len(artifact),
        "sha256": hashlib.sha256(artifact).hexdigest(),
        "addr_first": records[0].linear,
        "addr_last": records[-1].linear,
        "addr_end": records[-1].linear + len(records[-1].data),
        "banks": list(dict.fromkeys(r.bank for r in records)),
        "segments": segments,
        "session": {
            "parity_disable": parity_disable_frame().hex(),
            "parity_disable_ack": ACK_PARITY_DISABLE,
            "parity_enable": parity_enable_frame().hex(),
            "parity_enable_ack": ACK_PARITY_ENABLE,
            "flash_initial": flash_initial_frame(
                REGION_TABLE[records[0].linear]).hex(),
            "flash_initial_region": REGION_TABLE[records[0].linear],
            "flash_initial_ack": ACK_FLASH_INITIAL,
            "flash_end": flash_end_frame().hex(),
            "flash_end_ack": ACK_FLASH_INITIAL,
            "write_ack": ACK_WRITE,
        },
        "controls": controls,
        "frame_index": [list(t) for t in frame_index],
        "notes": (
            "Full ordered OUT stream for the SCT3288 baseband updater "
            "(84A961-framed, u16be LEN excl. MOD, XOR parity over LEN..2F; "
            "any frame whose total length is odd carries one extra 0x00 that "
            "LEN does not count -- write frames are 13+N, so every even N is "
            "padded). Artifact = controls + write frames exactly as the vendor "
            "SCT_PORT tool sends them; strict 1:1 ACK discipline, expected "
            "ACK bytes in session/controls. frame_index rows are "
            "[offset, length, kind]; 'frames' counts write frames only. "
            "Erase-region IDs are derived as (InitFlashTypeEnum << 1) | 1 over "
            "the vendor's DMR_T2 segment ladder (see REGION_TABLE)."
        ),
    }
    return artifact, manifest


def compile_hex_file(hex_path: str) -> Tuple[bytes, dict]:
    raw = Path(hex_path).read_bytes()
    if not raw.strip():
        raise SctHexError(f"{hex_path}: empty file")
    return compile_stream(parse_sct_hex(raw))


def _main() -> None:
    ap = argparse.ArgumentParser(
        description="Precompile an SCT3288 baseband updater .hex into the "
                    "exact serial wire stream (D890UV baseband DSP update).")
    ap.add_argument("--hex", required=True, help="vendor SCT3288 updater .hex")
    ap.add_argument("--out", required=True, help="output wire-stream artifact")
    ap.add_argument("--manifest", required=True, help="output JSON manifest")
    args = ap.parse_args()

    try:
        artifact, manifest = compile_hex_file(args.hex)
    except (SctHexError, OSError) as e:
        print(f"fwupd_sct: {e}", file=sys.stderr)
        sys.exit(2)
    Path(args.out).write_bytes(artifact)
    Path(args.manifest).write_text(json.dumps(manifest, indent=1) + "\n")
    print(json.dumps({k: manifest[k] for k in (
        "kind", "frames", "control_frames", "payload_bytes", "wire_bytes",
        "sha256", "addr_first", "addr_last")}))


if __name__ == "__main__":
    _main()
