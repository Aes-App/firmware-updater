from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

FRAME_LEN = 40
BLOCK = 32
CMD_WRITE = 0x01
FRAME_TERMINATOR = 0x06
FINISH_BYTE = 0x18
PAD_BYTE = 0x00

CDI_STRIDE = 278
CDI_NAME_LEN = 256
CDI_GRANULARITY = 0x00010000

MODELS = {
    "d890": {
        "fw":   dict(base=0x0800C000, span=(0x08000000, 0x08400000), max_entries=1,
                     handshake="UPDATE", ident="ID890UV", finish_acked=False),
        "icon": dict(base=0x00040000, span=(0x00000000, 0x08000000), max_entries=None,
                     handshake="PROGRAM", ident=None, finish_acked=True),
    },
    "d878uv": {
        "fw":   dict(base=0x08004000, span=(0x08000000, 0x08400000), max_entries=1,
                     handshake="UPDATE", ident="ID878UV", finish_acked=False),
        "icon": dict(base=0x00020000, span=(0x00000000, 0x08000000), max_entries=None,
                     handshake="PROGRAM", ident=None, finish_acked=True),
    },
    "d878uv2": {
        "fw":   dict(base=0x08004000, span=(0x08000000, 0x08400000), max_entries=1,
                     handshake="UPDATE", ident="ID878UV2", finish_acked=False),
        "icon": dict(base=0x00020000, span=(0x00000000, 0x08000000), max_entries=None,
                     handshake="PROGRAM", ident=None, finish_acked=True),
        "aprs": dict(base=0x00002000, span=(0x00000000, 0x00100000), max_entries=1,
                     handshake="UPDATE", ident="IA-BORD", finish_acked=False),
    },
}
DEFAULT_MODEL = "d890"

KIND = MODELS[DEFAULT_MODEL]


def _target(model: str, kind: str) -> dict:
    if model not in MODELS:
        raise UpdateFileError(
            f"unknown model {model!r} (have: {', '.join(MODELS)})")
    kinds = MODELS[model]
    if kind not in kinds:
        raise UpdateFileError(
            f"model {model!r} has no {kind!r} target (have: {', '.join(kinds)})")
    return kinds[kind]


class UpdateFileError(ValueError):
    pass


@dataclass
class CdiEntry:
    index: int
    name: str
    addr: int
    length: int
    foff: int
    granularity: int

    @property
    def frames(self) -> int:
        return (self.length + BLOCK - 1) // BLOCK


def parse_cdi(cdi: bytes) -> List[CdiEntry]:
    if not cdi:
        raise UpdateFileError(".CDI is empty")
    if len(cdi) % CDI_STRIDE:
        raise UpdateFileError(
            f".CDI size {len(cdi)} is not a multiple of {CDI_STRIDE} "
            f"(not a CDI index, or truncated)")
    entries: List[CdiEntry] = []
    for i in range(len(cdi) // CDI_STRIDE):
        o = i * CDI_STRIDE
        raw_name = cdi[o:o + CDI_NAME_LEN]
        nul = raw_name.find(b"\x00")
        if nul <= 0:
            raise UpdateFileError(
                f".CDI entry {i}: name is not NUL-terminated ASCII/GBK "
                f"(starts {raw_name[:16]!r})")
        try:
            name = raw_name[:nul].decode("gbk")
        except UnicodeDecodeError as e:
            raise UpdateFileError(f".CDI entry {i}: undecodable GBK name: {e}")
        addr, length, foff, gran = struct.unpack_from(
            "<IIII", cdi, o + CDI_NAME_LEN)
        tail = cdi[o + CDI_NAME_LEN + 16:o + CDI_STRIDE]
        if tail != b"\x00" * 6:
            raise UpdateFileError(
                f".CDI entry {i} ({name}): tail bytes {tail.hex()} != zeros "
                f"(unknown CDI revision -- refusing to guess)")
        if gran != CDI_GRANULARITY:
            raise UpdateFileError(
                f".CDI entry {i} ({name}): granularity {gran:#x} != "
                f"{CDI_GRANULARITY:#x} (unknown CDI revision)")
        if length <= 0:
            raise UpdateFileError(f".CDI entry {i} ({name}): zero length")
        if addr % BLOCK:
            raise UpdateFileError(
                f".CDI entry {i} ({name}): address {addr:#x} not 32-byte "
                f"aligned -- the 32-byte frame sweep cannot start there")
        entries.append(CdiEntry(i, name, addr, length, foff, gran))
    return entries


def parse_spi(spi: bytes) -> Tuple[int, int, int]:
    if len(spi) < 10:
        raise UpdateFileError(f".spi is {len(spi)} bytes, need >= 10")
    block = struct.unpack_from("<I", spi, 0)[0]
    count = struct.unpack_from("<H", spi, 4)[0]
    total = struct.unpack_from("<I", spi, 6)[0]
    if block != BLOCK:
        raise UpdateFileError(
            f".spi on-wire block size {block} != {BLOCK} (not a CPS-update "
            f".spi, or a protocol revision this compiler has never seen)")
    return block, count, total


def validate_package(kind: str, entries: List[CdiEntry], cdd_len: int,
                     spi: Optional[bytes], model: str = DEFAULT_MODEL) -> None:
    k = _target(model, kind)

    expect_foff = 0
    for e in entries:
        if e.foff != expect_foff:
            raise UpdateFileError(
                f".CDI entry {e.index} ({e.name}): .CDD offset {e.foff:#x} != "
                f"expected {expect_foff:#x} -- entries do not tile the .CDD "
                f"(wrong .CDD for this .CDI?)")
        expect_foff += e.length
    if expect_foff > cdd_len:
        raise UpdateFileError(
            f".CDI entries need {expect_foff} bytes but the .CDD is only "
            f"{cdd_len} bytes -- the .CDD is truncated or is the wrong file "
            f"for this .CDI")
    trailing = cdd_len - expect_foff
    if trailing:
        if spi is None:
            raise UpdateFileError(
                f".CDI entries sum to {expect_foff} bytes but the .CDD is "
                f"{cdd_len} ({trailing} trailing) -- supply the .spi to confirm "
                f"the payload length, or fix the .CDD/.CDI pair")
        if parse_spi(spi)[2] != expect_foff:
            raise UpdateFileError(
                f".CDD carries {trailing} bytes past the {expect_foff}-byte "
                f"indexed payload and the .spi total ({parse_spi(spi)[2]}) does "
                f"not confirm it -- mismatched package files")

    for prev, e in zip(entries, entries[1:]):
        if e.addr < prev.addr + prev.length:
            raise UpdateFileError(
                f".CDI entry {e.index} ({e.name}) at {e.addr:#x} overlaps or "
                f"precedes entry {prev.index} ({prev.name}, ends "
                f"{prev.addr + prev.length:#x}) -- the bootloader requires a "
                f"strictly ascending single-pass address stream")

    if k["max_entries"] is not None and len(entries) > k["max_entries"]:
        raise UpdateFileError(
            f"--kind {kind} expects at most {k['max_entries']} .CDI "
            f"entry/entries, got {len(entries)} -- is this actually an "
            f"{'ICON' if kind == 'fw' else 'FW'} package?")
    if entries[0].addr != k["base"]:
        others = ", ".join(f"{kk} at {kc['base']:#010x}"
                           for kk, kc in MODELS[model].items())
        raise UpdateFileError(
            f"--kind {kind} ({model}) expects the stream to start at "
            f"{k['base']:#010x}, but the first .CDI entry ({entries[0].name}) "
            f"loads at {entries[0].addr:#010x} -- wrong file for this kind "
            f"({others})")
    lo, hi = k["span"]
    for e in entries:
        if not (lo <= e.addr and e.addr + e.length <= hi):
            raise UpdateFileError(
                f".CDI entry {e.index} ({e.name}) spans {e.addr:#x}.."
                f"{e.addr + e.length:#x}, outside the {kind} address space "
                f"[{lo:#x}, {hi:#x})")

    if spi is not None:
        _, count, total = parse_spi(spi)
        if count != len(entries):
            raise UpdateFileError(
                f".spi declares {count} entries but the .CDI holds "
                f"{len(entries)} -- mismatched package files")
        if total != expect_foff:
            raise UpdateFileError(
                f".spi declares total length {total} but the .CDI payload is "
                f"{expect_foff} bytes -- mismatched package files")


def build_frame(addr: int, data: bytes) -> bytes:
    if len(data) != BLOCK:
        raise ValueError(f"frame data must be {BLOCK} bytes, got {len(data)}")
    body = struct.pack("<I", addr & 0xFFFFFFFF) + data
    ck = sum(body) & 0xFFFF
    return bytes([CMD_WRITE]) + body + struct.pack("<H", ck) \
        + bytes([FRAME_TERMINATOR])


def compile_frames(entries: List[CdiEntry], cdd: bytes) -> bytes:
    out = bytearray()
    for e in entries:
        for k in range(e.frames):
            chunk = cdd[e.foff + k * BLOCK:e.foff + min(e.length, (k + 1) * BLOCK)]
            out += build_frame(e.addr + k * BLOCK,
                               chunk.ljust(BLOCK, bytes([PAD_BYTE])))
    return bytes(out)


def compile_update(kind: str, cdd: bytes, cdi: bytes,
                   spi: Optional[bytes] = None,
                   model: str = DEFAULT_MODEL) -> Tuple[bytes, dict]:
    k = _target(model, kind)
    if not cdd:
        raise UpdateFileError(".CDD is empty")
    entries = parse_cdi(cdi)
    validate_package(kind, entries, len(cdd), spi, model)
    artifact = compile_frames(entries, cdd)

    n_frames = len(artifact) // FRAME_LEN
    ident = k.get("ident")
    manifest = {
        "kind": kind,
        "model": model,
        "frames": n_frames,
        "payload_bytes": sum(e.length for e in entries),
        "wire_bytes": len(artifact),
        "sha256": hashlib.sha256(artifact).hexdigest(),
        "addr_first": entries[0].addr,
        "addr_last": entries[-1].addr + (entries[-1].frames - 1) * BLOCK,
        "notes": (
            f"Concatenated {FRAME_LEN}-byte CPS-bootloader write frames "
            f"in send order (01 | u32LE addr | 32 data | u16LE "
            f"sum(frame[1:37]) | 06). Handshake first "
            f"({k['handshake']!r}; the PROGRAM handshake MUST get a bare 06 "
            f"back -- 51 58 06 means codeplug mode, abort), then one frame per "
            f"0x06 ACK, then a lone 0x18 "
            f"({'ACKed' if k['finish_acked'] else 'not ACKed'}). Short final "
            f"asset frames are zero-padded; flash gaps between assets are "
            f"skipped. Byte-matched against the factory capture."
        ),
        "frame_len": FRAME_LEN,
        "block_size": BLOCK,
        "base_addr": k["base"],
        "pad_byte": PAD_BYTE,
        "handshake_ascii": k["handshake"],
        "handshake_expect_hex": "06",
        "codeplug_collision_hex": "515806" if k["handshake"] == "PROGRAM" else None,
        "ident_query_hex": "02" if ident else None,
        "ident_reply_prefix_ascii": ident,
        "finish_byte_hex": f"{FINISH_BYTE:02x}",
        "finish_acked": k["finish_acked"],
        "serial": {"baud": 921600, "control_line_state": "RTS only (0x0002)"},
        "entries": [
            {"name": e.name, "addr": e.addr, "length": e.length,
             "cdd_offset": e.foff, "frames": e.frames}
            for e in entries
        ],
        "inputs": {
            "cdd_sha256": hashlib.sha256(cdd).hexdigest(),
            "cdi_sha256": hashlib.sha256(cdi).hexdigest(),
            "spi_sha256": hashlib.sha256(spi).hexdigest() if spi else None,
        },
    }
    return artifact, manifest


def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m radio_fw.vendor.fwupd_cps",
        description="Precompile a D890 CPS update package (.CDD/.CDI/.spi) "
                    "into the exact 40-byte serial frame stream the factory "
                    "CPS sends, plus a JSON manifest for the WebSerial writer.")
    ap.add_argument("--kind", required=True, choices=sorted(KIND),
                    help="fw = main MCU firmware (base 0x0800C000); "
                         "icon = asset/icon flash (base 0x00040000)")
    ap.add_argument("--cdd", required=True, help="vendor .CDD (raw payload)")
    ap.add_argument("--cdi", required=True, help="vendor .CDI (asset index)")
    ap.add_argument("--spi", help="vendor .spi (package manifest; validated "
                                  "against the .CDD/.CDI when given)")
    ap.add_argument("--out", required=True,
                    help="output artifact (concatenated 40-byte frames)")
    ap.add_argument("--manifest", required=True, help="output JSON manifest")
    args = ap.parse_args(argv)

    try:
        cdd = Path(args.cdd).read_bytes()
        cdi = Path(args.cdi).read_bytes()
        spi = Path(args.spi).read_bytes() if args.spi else None
    except OSError as e:
        print(f"cannot read input: {e}", file=sys.stderr)
        return 2
    try:
        artifact, manifest = compile_update(args.kind, cdd, cdi, spi)
    except UpdateFileError as e:
        print(f"REFUSING to compile {args.kind} update: {e}", file=sys.stderr)
        return 2

    try:
        Path(args.out).write_bytes(artifact)
        Path(args.manifest).write_text(
            json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    except OSError as e:
        print(f"cannot write output: {e}", file=sys.stderr)
        return 2
    print(json.dumps(manifest))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
