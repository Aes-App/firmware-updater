"""fwupd_sct.py -- precompile a SiCOMM SCT3288 baseband-DSP updater hex into the
exact serial wire stream the vendor SCT_PORT tool sends to an AT-D890UV.

Protocol (byte-verified against the factory-tool D890 SCT3288 USB capture, bus 2
device 15):

    84 A9 61 | LEN (u16 BIG-endian) | MOD | PAYLOAD [| trailing 0x00]
    PAYLOAD  = CMD | args [| 0x2F | XOR]
    LEN      = len(PAYLOAD)  (CMD..XOR inclusive, EXCLUDING the MOD byte)
    XOR      = 1-byte XOR of LEN_hi .. 0x2F inclusive (LEN, MOD, CMD, args, 2F)

The trailing 0x00 is a PAD TO AN EVEN FRAME LENGTH, not a property of write
frames: the vendor appends it, uncounted by LEN, to any finished frame whose
total length is odd (`if (cmd.Length % 2 != 0) Array.Resize(ref cmd,
cmd.Length + 1);` -- SCT3252.cs:3936-3942). A write frame is 13 + N bytes, so
every EVEN payload N gets the pad and every odd N does not; all four control
frames are 8 or 10 bytes and never do. That reproduces the capture exactly
(1,343/1,343 write frames padded -- every record in the vendor hex has an even
length) while staying correct for an odd-length record.

    94 | bank | addr_hi | addr_lo | count | <count data bytes> | 2F | XOR [| 00]

(The vendor's own .prog scripts are NOT evidence either way: PWritFlash reaches
PAddParity, never SendCmd (SCT3252.cs:7716-7737, :7546-7557), so the .prog lines
are unpadded regardless of length.)

Source file: Intel HEX with a NON-STANDARD type-04. The Extended Linear Address
records carry ZERO data bytes and put the BANK NUMBER in the ADDRESS field
(":00000004FC" = bank 0, ":00000104FB" = bank 1). A stock Intel-HEX loader reads
ULBA=0 for all of them and silently collapses the image into garbage. The
mapping to the wire is one-to-one and ORDER-PRESERVING -- transfer order equals
file order, including the non-monotonic address jumps; records are never sorted,
merged or re-chunked.

Session framing (all byte strings straight from the capture):

    OUT 84 a9 61 00 04 00 16 00 2f 3d   PARITY_DISABLE (sent WITH parity)
    IN  84 a9 61 00 02 00 17 06
    OUT 84 a9 61 00 02 00 16 01         PARITY_ENABLE (sent WITHOUT parity)
    IN  84 a9 61 00 02 00 16 00
    OUT 84 a9 61 00 04 00 93 RR 2f xx   FLASH_INITIAL(region) -- real erase,
    IN  84 a9 61 00 04 00 93 00 2f b8     ~162 ms on the first one
    ... WR_FLASH_DATA frames, each ACKed 84 a9 61 00 04 03 94 00 2f bc ...

The capture shows MORE than the single opening FLASH_INITIAL: every linearly
DIS-contiguous jump in the record stream (7 segments in the V3_01_01A6 file,
marked in the hex by type-04 records -- including a redundant same-bank one) is
preceded by a `16 01` (with parity) + FLASH_INITIAL(region) pair, and the
session closes with `16 01` + FLASH_INITIAL(0x00) (= FLASH_END) + `16 00`.
The region byte is `1 | (InitFlashTypeEnum << 1)` over a hard-coded segment-start
ladder -- see REGION_TABLE below. An unknown segment start is a HARD ERROR:
erasing the wrong region can brick the baseband, so a segment the vendor ladder
does not name is refused rather than guessed.

Output: --out gets the full ordered OUT wire stream (control + write frames,
exactly what the JS WebSerial engine must send); --manifest gets a JSON
manifest with the counts, sha256, session frames, per-segment controls with
expected ACKs, and a frame index (offset/length/kind per frame).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple, Tuple

MAGIC = b"\x84\xa9\x61"

# Expected device replies (from the capture; the JS engine gates on these).
ACK_PARITY_DISABLE = "84a9610002001706"
ACK_PARITY_ENABLE = "84a9610002001600"
ACK_FLASH_INITIAL = "84a96100040093002fb8"   # also ACKs FLASH_END
ACK_SEG_PARITY = "84a96100040016002f3d"      # reply to in-session 16 01 / 16 00
ACK_WRITE = "84a96100040394002fbc"

# InitFlashTypeEnum members the SCT3288 (VersionsEnum.DMR_T2) ladder uses.
# The enum is OUT OF NUMERIC ORDER in the vendor source -- Vocode5 = 15 while
# Vocode6/7/8 = 12/13/14 (InitFlashTypeEnum.cs) -- which is exactly why an
# address-only pattern search over the region bytes finds nothing: the byte
# tracks the ENUM MEMBER, not the address.
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
    """The FLASH_INITIAL region byte for an InitFlashTypeEnum member.

    Vendor (SCT_Device.dll, SICOMM.Wangjunhua.SCT3252.InitFlash, decompiled at
    SCT3252.cs:2224-2226)::

        cmd[1] = (byte)(isStart ? 1u : 0u);
        cmd[1] |= (byte)((int)initType << 1);

    so bit 0 is the isStart flag and the enum sits in bits 1..7. FLASH_END is
    the same command with isStart false and no type, i.e. region byte 0x00.
    """
    return ((erase_type << 1) | 1) & 0xFF


#: Which erase region each dis-contiguous segment start belongs to, from the
#: vendor's hard-coded ladder for VersionsEnum.DMR_T2 -- the SCT3288/D890
#: profile (SCT3252.cs:3248-3420). Keyed by linear (bank<<16 | addr16).
#:
#: Seven of these nine are independently confirmed byte-for-byte by the
#: D890_SCT3288.pcapng capture; test_sct_region_table_matches_vendor_formula
#: is the gate that keeps table and formula in step. 0x039000 and 0x040000 come
#: from the same ladder but the V3_01_01A6 hex never exercises them.
#:
#: DELIBERATELY ABSENT -- 0x05C000 ("Vocoder8"): the vendor's two code paths
#: disagree. The live flasher passes EraseVocode7Flash (SCT3252.cs:3406-3414,
#: under a message string reading "Vocoder8" -- the branch is a verbatim clone
#: of the 0x056000 block), while the .prog generator passes EraseVocode8Flash
#: (:8045-8048). Erasing the wrong region bricks the DSP and no capture settles
#: it, so compile_stream() refuses that segment rather than pick a side.
#:
#: SCOPE: DMR_T2 / SCT3288 only. The same ladder maps 0x000100 to
#: EraseUserFlashNoVocode when _scttitletype == "SCT3917" (SCT3252.cs:3267-3269),
#: and the DMR_T1 / DPMR profiles use entirely different addresses.
SEGMENT_ERASE_TYPE: Dict[int, int] = {
    0x000100: ERASE_USER_FLASH_WITH_VOCODE,   # "DownLoad UserFlash Code"
    0x022000: ERASE_VOCODE1,                  # "DownLoad Vocoder1 Code"
    0x035000: ERASE_VOCODE4,                  # "DownLoad Vocoder4 Code"
    0x039000: ERASE_SYSTEM_FLASH,             # "DownLoad SystemFlash Code"
    0x040000: ERASE_VOCODE2,                  # "DownLoad Vocoder2 Code"
    0x053000: ERASE_VOCODE6,                  # "Vocoder6 Code or User3 Flash"
    0x056000: ERASE_VOCODE7,                  # "DownLoad Vocoder7 Code"
    0x063000: ERASE_VOCODE3,                  # "DownLoad Vocoder3 Code"
    0x077000: ERASE_VOCODE5,                  # "DownLoad Vocoder5 Code"
}

#: FLASH_INITIAL erase-region IDs, keyed by segment start address.
REGION_TABLE: Dict[int, int] = {
    addr: region_byte(t) for addr, t in SEGMENT_ERASE_TYPE.items()
}

#: EVERY address the vendor's DMR_T2 ladder erases on, including the one we
#: refuse to compile. The two sets must not be conflated: REGION_TABLE answers
#: "what erase byte do we send here", this answers "would the vendor erase
#: here at all" -- and that second question is the one the guards ask, so that
#: 0x05C000 is refused in BOTH positions (as a segment start AND as an address
#: a contiguous run walks into) rather than silently written un-erased.
VENDOR_LADDER_BASES = frozenset(SEGMENT_ERASE_TYPE) | {0x05C000}

#: The seven values the capture proves, as a standing regression gate. If the
#: formula above and these ever disagree, the CAPTURE wins -- it is the wire.
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
    """The vendor .hex is malformed / not an SCT3288 updater file."""


class HexRecord(NamedTuple):
    bank: int      # from the preceding non-standard type-04
    addr: int      # 16-bit record address
    data: bytes

    @property
    def linear(self) -> int:
        return (self.bank << 16) | self.addr


# ---------------------------------------------------------------------------
# Intel HEX parsing (strict; hard-fail on anything malformed)
# ---------------------------------------------------------------------------
def parse_sct_hex(raw: bytes) -> List[HexRecord]:
    """Parse the SCT3288 updater Intel HEX into data records, file order.

    Validates every record's structure and checksum and raises SctHexError on
    the first defect: a wrong artifact bricks a radio, so an admin uploading
    the wrong file must get a hard error, never a silently wrong image.
    """
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
        if len(line) % 2 != 1:            # ':' + even number of hex digits
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
            # THE TRAP: this family's type-04 carries zero data bytes and puts
            # the bank number in the ADDRESS field. A standard 2-byte-ULBA
            # type-04 is also accepted (ULBA == bank there); anything else is
            # malformed.
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


# ---------------------------------------------------------------------------
# Frame construction
# ---------------------------------------------------------------------------
def build_frame(mod: int, body: bytes, *, parity: bool = True) -> bytes:
    """One logical frame: MAGIC | LEN(u16be) | MOD | body [| 2F | XOR] [| 00].

    LEN counts body (CMD..args) plus, when parity is on, the 2F marker and the
    XOR byte -- but never the MOD byte. XOR covers LEN_hi..0x2F inclusive.

    The final 0x00 is the vendor's PAD TO EVEN and is not counted by LEN:
    `if (cmd.Length % 2 != 0) Array.Resize(ref cmd, cmd.Length + 1);`
    (SCT3252.cs:3936-3942), applied to every frame after the parity bytes.
    """
    length = len(body) + (2 if parity else 0)
    frame = bytearray(MAGIC)
    frame += length.to_bytes(2, "big")
    frame.append(mod)
    frame += body
    if parity:
        frame.append(0x2F)
        x = 0
        for b in frame[3:]:          # LEN_hi .. 0x2F inclusive
            x ^= b
        frame.append(x)
    if len(frame) % 2:
        frame.append(0x00)
    return bytes(frame)


def write_frame(rec: HexRecord) -> bytes:
    """WR_FLASH_DATA: 94 | bank | addr_hi | addr_lo | count | data | 2F | XOR [| 00].

    13 + len(data) bytes before the pad, so an even-length record (every record
    in every vendor hex on file) gets the pad and an odd-length one does not.
    """
    body = bytes([0x94, rec.bank, rec.addr >> 8, rec.addr & 0xFF,
                  len(rec.data)]) + rec.data
    return build_frame(0x03, body, parity=True)


def parity_disable_frame() -> bytes:
    return build_frame(0x00, b"\x16\x00", parity=True)


def parity_enable_frame() -> bytes:
    return build_frame(0x00, b"\x16\x01", parity=False)


def seg_parity_frame() -> bytes:
    """The in-session `16 01` (WITH parity) sent before each FLASH_INITIAL."""
    return build_frame(0x00, b"\x16\x01", parity=True)


def flash_initial_frame(region: int) -> bytes:
    return build_frame(0x00, bytes([0x93, region]), parity=True)


def flash_end_frame() -> bytes:
    return flash_initial_frame(0x00)


# ---------------------------------------------------------------------------
# Stream compilation
# ---------------------------------------------------------------------------
def segment_starts(records: List[HexRecord]) -> List[int]:
    """Indices where a new linearly dis-contiguous segment begins.

    In the V3_01_01A6 file these coincide exactly with the type-04 records
    whose following data record does not continue the previous byte run (the
    four contiguous bank rollovers 0->1, 2->3, 7->8, 8->9 get NO erase pair in
    the capture; the seven dis-contiguous jumps all do).
    """
    starts = [0]
    for i in range(1, len(records)):
        if records[i].linear != records[i - 1].linear + len(records[i - 1].data):
            starts.append(i)
    return starts


def compile_stream(records: List[HexRecord]) -> Tuple[bytes, dict]:
    """The full ordered OUT wire stream + its manifest."""
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

    # The vendor triggers an erase on EXACT EQUALITY with a region base, tested
    # against every record and never against contiguity (SCT3252.cs:3181 +
    # ladder). We trigger on discontinuity instead, which agrees on every
    # vendor file on disk. Where they could diverge -- a section that runs
    # byte-exactly into the next region base -- the vendor erases and we would
    # not, writing into un-erased NOR. Refuse that layout instead of silently
    # differing; no shipping file reaches this.
    for i, rec in enumerate(records):
        if i not in start_set and rec.linear in VENDOR_LADDER_BASES:
            raise SctHexError(
                f"record {i} writes to {rec.linear:#08x}, which is an erase-region "
                f"base, but it continues the previous record contiguously so no "
                f"FLASH_INITIAL would be emitted for it. The vendor keys its erase "
                f"on the address alone and WOULD erase here, so this file needs a "
                f"capture before it can be compiled safely.")

    stream = bytearray()
    frame_index: List[Tuple[int, int, str]] = []   # (offset, length, kind)
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

    # Opening handshake. PARITY_DISABLE is sent (and ACKed) while the link
    # still has parity; PARITY_ENABLE then goes out bare.
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

    # Close: 16 01 (parity) | FLASH_INITIAL(0x00) = FLASH_END | 16 00 (parity).
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
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
