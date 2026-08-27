"""fwupd_nr.py -- D890UV NR-board (JieLi daughterboard) update precompiler:
.ufw container VALIDATOR + SERVING PLAN.

THIS PROTOCOL IS DEVICE-PULL, so unlike the CPS-driven FW/ICON updates there is
NO linear frame stream to precompile. The board sends UPDATE_READ_REQ(offset,
count) frames and the host must answer with the bytes of the vendor ``.ufw`` at
that FILE OFFSET, verbatim (proved 982/982 responses, 499,360/499,360 bytes,
against the factory-tool D890 NR-board USB capture).

So this module:
  * validates the ``.ufw`` container hard (an admin uploading the wrong file
    must get an error, never a silently wrong artifact -- a wrong artifact
    bricks the board);
  * copies the ``.ufw`` through to ``--out`` VERBATIM (the browser serves read
    requests straight out of this artifact, ``serve()`` below is the rule);
  * emits a JSON manifest carrying the frame grammar constants for the JS
    engine, the parsed 0x7C0 file table, and the regions the board is expected
    to pull (the UI progress denominator).

Container layout (byte-verified on D890_NR_BOARD_V112_20260306.ufw):
  * NO magic at offset 0 -- byte 0 onward is high-entropy (encrypted). The
    magic is at the END: ``[u32][8 zero]["JLUFW" NUL-padded to 16]`` in the
    last 28 bytes. The u32 (0x42733665 in the reference) matched no common
    CRC-32 and is recorded, not verified.
  * A PLAINTEXT file table at 0x7C0, 32 bytes per record:
      [u32 crc][u32 offset][u32 length][u16 flags][u16 index][char[16] name]
    offsets relative to 0x7C0. The record count is self-describing: records
    run until the first record's data offset (file data is packed immediately
    after the table). The per-record u32 crc algorithm is unknown (it is not
    CRC-32 and not CRC-16/XMODEM); it is emitted raw and NOT verified.
  * Two 32-byte "image copy" headers ([4][ASCII version][8]["QX700N"-style
    ASCII product][10 x 0xFF]) locate the NR image: it spans from
    header2 + 0x1000 to EOF - 0x100 and the flash phase pulls it in 512-byte
    blocks STRICTLY DESCENDING (0x11bfe0 -> 0xa7de0 in the reference), so the
    header the loader validates on boot is written LAST.

Frame grammar (CRC verified 1,982/1,982 frames in the reference capture):
    [0:2] AA 55 magic | [2:4] u16 LE length (= opcode + payload bytes; frame
    total = 6 + length) | [4] opcode | [5:] payload |
    [-2:] u16 LE CRC-16/XMODEM (poly 0x1021, init 0, non-reflected) over the
    WHOLE frame INCLUDING the AA 55 magic.
Opcodes (vendor .NET metadata, "English JL Bootloader.exe"): 01 START,
02 READ req/res, 03 STOP, 04 LEN_NOTIFY, 05 ALIVE, 06 ENTER_UPDATE.

Usage:
  python3 -m radio_fw.vendor.fwupd_nr \
      --ufw D890_NR_BOARD_V112_20260306.ufw --out nr.bin --manifest nr.json

On success prints the manifest (one JSON line) to stdout; on ANY validation
failure exits non-zero with the reason on stderr and writes nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from typing import Dict, List, Tuple

KIND = "d890_nr_ufw"

# ---------------------------------------------------------------- frame grammar
FRAME_MAGIC = b"\xAA\x55"
CRC_POLY = 0x1021          # CRC-16/XMODEM, init 0, non-reflected, stored LE
CRC_INIT = 0x0000
FRAME_MIN = 7              # magic(2) + len(2) + opcode(1) + crc(2)
READ_FRAME_OVERHEAD = 15   # magic(2)+len(2)+opcode(1)+offset(4)+count(4)+crc(2)

OP_START = 0x01            # UPDATE_START (device asks; host RES carries u32 LE baud)
OP_READ = 0x02             # UPDATE_READ req (device, 15 B) / res (host, +data)
OP_STOP = 0x03             # UPDATE_STOP (device; status 0x00 = success)
OP_LEN_NOTIFY = 0x04       # UPDATE_LEN_NOTIFY
OP_ALIVE = 0x05            # UPDATE_ALIVE
OP_ENTER_UPDATE = 0x06     # REQ_ENTER_UPDATE_MODE (host handshake, 9600 8N1)

OPCODES = {
    "UPDATE_START": OP_START,
    "UPDATE_READ": OP_READ,
    "UPDATE_STOP": OP_STOP,
    "UPDATE_LEN_NOTIFY": OP_LEN_NOTIFY,
    "UPDATE_ALIVE": OP_ALIVE,
    "REQ_ENTER_UPDATE_MODE": OP_ENTER_UPDATE,
}

# ------------------------------------------------------------ container layout
TABLE_OFF = 0x7C0          # plaintext file table
TABLE_REC = 32
TABLE_MAX_RECORDS = 64     # sanity bound; the reference has 6
TRAILER_MAGIC = b"JLUFW"   # NUL-padded to 16 in the last 16 bytes
BLOCK = 512                # flash-phase read granularity (reference: 975/982 reads)
HEADER_PULL_LEN = 0x400    # the board reads the (encrypted) ufw header 0x0..0x400
TABLE_PULL_LEN = 0x200     # ... and one 512-byte block at 0x7C0 for the table
COPY_HEADER_PULL_LEN = 0x200
IMAGE_GAP_AFTER_HDR2 = 0x1000   # NR image starts 0x1000 after copy header 2
IMAGE_TAIL_RESERVED = 0x100     # ... and ends 0x100 before EOF (trailer zone)
UPDATE_AGENT = "uart_user.bin"  # the file the board pulls before flashing


class UfwError(ValueError):
    """The uploaded file is not a servable JieLi .ufw container."""


# =============================================================================
# CRC-16/XMODEM -- poly 0x1021, init 0, non-reflected, computed over the WHOLE
# frame including the AA 55 magic, stored u16 LE at the end of the frame.
# Verified 1,982/1,982 frames of the reference capture.
# =============================================================================
def _make_crc_table() -> List[int]:
    table = []
    for byte in range(256):
        c = byte << 8
        for _ in range(8):
            c = ((c << 1) ^ CRC_POLY) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
        table.append(c)
    return table


_CRC_TABLE = _make_crc_table()


def crc16_xmodem(data: bytes, crc: int = CRC_INIT) -> int:
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC_TABLE[((crc >> 8) ^ b) & 0xFF]
    return crc


def build_frame(opcode: int, payload: bytes = b"") -> bytes:
    """One wire frame: AA 55 | u16 LE (1+len(payload)) | opcode | payload | CRC.

    build_frame(OP_ENTER_UPDATE) == aa 55 01 00 06 42 f2, the byte-exact
    handshake of the reference capture.
    """
    body = FRAME_MAGIC + struct.pack("<H", 1 + len(payload)) + bytes([opcode]) + payload
    return body + struct.pack("<H", crc16_xmodem(body))


def parse_frame(buf: bytes) -> Tuple[int, bytes]:
    """(opcode, payload) of one complete frame; UfwError on bad magic/len/CRC."""
    if len(buf) < FRAME_MIN or buf[:2] != FRAME_MAGIC:
        raise UfwError("not an AA 55 frame: %s" % buf[:8].hex(" "))
    length = struct.unpack_from("<H", buf, 2)[0]
    total = 6 + length
    if length < 1 or len(buf) != total:
        raise UfwError(f"frame length field {length} does not match "
                       f"{len(buf)} bytes on the wire")
    stored = struct.unpack_from("<H", buf, total - 2)[0]
    calc = crc16_xmodem(buf[:total - 2])
    if stored != calc:
        raise UfwError(f"frame CRC mismatch: stored {stored:#06x}, "
                       f"computed {calc:#06x}")
    return buf[4], buf[5:total - 2]


def serve(ufw: bytes, offset: int, count: int) -> bytes:
    """THE serving rule: UPDATE_READ_REQ(offset, count) is answered with the
    raw .ufw content at that FILE OFFSET, verbatim. Out-of-range is a hard
    error -- the reference host never served past EOF and a short or padded
    answer would corrupt the flash."""
    if offset < 0 or count < 0 or offset + count > len(ufw):
        raise UfwError(f"read request out of range: offset={offset:#x} "
                       f"count={count} but the .ufw is {len(ufw)} bytes")
    return ufw[offset:offset + count]


# =============================================================================
# Container parsing / validation
# =============================================================================
def _parse_trailer(fw: bytes) -> int:
    """Validate the end-of-file magic; return the (unverified) trailer u32."""
    tail16 = fw[-16:]
    if tail16 != TRAILER_MAGIC + b"\x00" * (16 - len(TRAILER_MAGIC)):
        raise UfwError(
            "missing JLUFW trailer magic: the last 16 bytes are "
            f"{tail16.hex(' ')!r}, expected 'JLUFW' NUL-padded to 16. "
            "This is not a JieLi .ufw NR-board update file.")
    if fw[-24:-16] != b"\x00" * 8:
        raise UfwError("bytes -24..-16 before the JLUFW magic are not zero: "
                       + fw[-24:-16].hex(" "))
    return struct.unpack_from("<I", fw, len(fw) - 28)[0]


def parse_file_table(fw: bytes) -> List[Dict]:
    """The plaintext 32-byte-per-record table at 0x7C0.

    The record count is self-describing: file data is packed immediately after
    the table, so records run until the smallest data offset seen so far. (This
    barrier matters: in the reference file the first data file, uart_update.bin,
    itself BEGINS with 32 bytes that parse as a plausible table record.)
    """
    records: List[Dict] = []
    cur = TABLE_OFF
    barrier = None
    while barrier is None or cur + TABLE_REC <= barrier:
        if len(records) >= TABLE_MAX_RECORDS:
            raise UfwError(f"file table at {TABLE_OFF:#x} exceeds "
                           f"{TABLE_MAX_RECORDS} records; refusing")
        if cur + TABLE_REC > len(fw):
            raise UfwError("file table at 0x7c0 runs past EOF")
        crc, off, length = struct.unpack_from("<III", fw, cur)
        flags, index = struct.unpack_from("<HH", fw, cur + 12)
        raw_name = fw[cur + 16:cur + 32]
        name_b = raw_name.rstrip(b"\x00")
        if (not name_b
                or raw_name != name_b + b"\x00" * (16 - len(name_b))
                or not all(0x20 <= c <= 0x7E for c in name_b)):
            raise UfwError(
                f"file-table record {len(records)} at {cur:#x} has a "
                f"non-ASCII name field {raw_name.hex(' ')} -- not a .ufw "
                "file table (wrong file, or an unknown container revision)")
        if off <= 0 or TABLE_OFF + off + length > len(fw):
            raise UfwError(
                f"file-table record {len(records)} ({name_b.decode()}) spans "
                f"[{TABLE_OFF + off:#x}, {TABLE_OFF + off + length:#x}) which "
                f"is outside the {len(fw)}-byte file")
        records.append({
            "name": name_b.decode("ascii"),
            "offset": TABLE_OFF + off,       # ABSOLUTE file offset
            "length": length,
            "flags": flags,
            "index": index,
            "crc_field": crc,                # algorithm unknown; NOT verified
        })
        barrier = TABLE_OFF + off if barrier is None else min(barrier, TABLE_OFF + off)
        cur += TABLE_REC
    if barrier != cur:
        raise UfwError(
            f"file table is not immediately followed by file data (table ends "
            f"{cur:#x}, first data at {barrier:#x}) -- unknown container revision")
    return records


def locate_copy_headers(fw: bytes) -> Tuple[int, int]:
    """The two 32-byte image-copy headers, by structural signature:
    ASCII version ('0.01'-style) at +4, printable product string at +16,
    padded with exactly 10 x 0xFF to +32. In the reference both carry
    'QX700N'; the NR image the board flashes descending starts 0x1000 after
    the SECOND one. Anything but exactly two consistent hits is a hard error.
    """
    hits: List[int] = []
    pad = b"\xff" * 10
    data_start = TABLE_OFF + TABLE_REC  # never inside header/table
    i = data_start
    while True:
        j = fw.find(pad, i)
        if j < 0:
            break
        i = j + 1
        o = j - 22
        if o < data_start:
            continue
        version = fw[o + 4:o + 8]
        product = fw[o + 16:o + 22]
        if (all(c in b"0123456789." for c in version) and b"." in version
                and all(0x21 <= c <= 0x7E for c in product)):
            hits.append(o)
    if len(hits) != 2:
        raise UfwError(
            f"expected exactly 2 image-copy headers, found {len(hits)} "
            f"({', '.join(hex(h) for h in hits) or 'none'}) -- cannot derive "
            "the NR image region; refusing rather than guessing")
    h1, h2 = hits
    if fw[h1 + 16:h1 + 22] != fw[h2 + 16:h2 + 22]:
        raise UfwError(
            "the two image-copy headers carry different product strings "
            f"({fw[h1 + 16:h1 + 22]!r} vs {fw[h2 + 16:h2 + 22]!r})")
    return h1, h2


def parse_ufw(fw: bytes) -> Dict:
    """Validate the container and derive the serving plan. UfwError on any
    inconsistency -- never a best-effort result."""
    if len(fw) < TABLE_OFF + TABLE_REC + 28:
        raise UfwError(f"file is only {len(fw)} bytes -- far too small for a "
                       ".ufw (needs the 0x7c0 table and the JLUFW trailer)")
    trailer_u32 = _parse_trailer(fw)
    table = parse_file_table(fw)
    agent = [r for r in table if r["name"] == UPDATE_AGENT]
    if len(agent) != 1:
        raise UfwError(
            f"file table has {len(agent)} '{UPDATE_AGENT}' records (need "
            "exactly 1) -- the board pulls this update agent before flashing; "
            f"table holds: {', '.join(r['name'] for r in table)}")
    h1, h2 = locate_copy_headers(fw)
    img_start = h2 + IMAGE_GAP_AFTER_HDR2
    img_end = len(fw) - IMAGE_TAIL_RESERVED
    img_len = img_end - img_start
    if img_len <= 0 or img_len % BLOCK != 0:
        raise UfwError(
            f"derived NR image span [{img_start:#x}, {img_end:#x}) is "
            f"{img_len} bytes, not a positive multiple of {BLOCK} -- the "
            "copy-header geometry does not match the known layout")

    regions = [
        {"name": "ufw_header", "offset": 0, "length": HEADER_PULL_LEN},
        {"name": "file_table", "offset": TABLE_OFF, "length": TABLE_PULL_LEN},
        {"name": UPDATE_AGENT, "offset": agent[0]["offset"],
         "length": agent[0]["length"]},
        {"name": "image_copy_header_1", "offset": h1,
         "length": COPY_HEADER_PULL_LEN},
        {"name": "image_copy_header_2", "offset": h2,
         "length": COPY_HEADER_PULL_LEN},
        {"name": "nr_image", "offset": img_start, "length": img_len},
    ]
    prev_end = -1
    for r in regions:
        if r["offset"] <= prev_end or r["offset"] + r["length"] > len(fw):
            raise UfwError(
                "derived served regions overlap or leave the file "
                f"({r['name']} at {r['offset']:#x}+{r['length']}) -- "
                "unknown container revision, refusing")
        prev_end = r["offset"] + r["length"]
    return {
        "trailer_u32": trailer_u32,
        "file_table": table,
        "copy_headers": (h1, h2),
        "regions": regions,
        "image": (img_start, img_len),
    }


# =============================================================================
# Manifest
# =============================================================================
def build_manifest(fw: bytes) -> Dict:
    info = parse_ufw(fw)
    regions = info["regions"]
    img_start, img_len = info["image"]
    payload = sum(r["length"] for r in regions)
    frames = sum((r["length"] + BLOCK - 1) // BLOCK for r in regions)
    return {
        "kind": KIND,
        # Planned MINIMUM at the 512-byte reference granularity. The pull is
        # device-driven: the reference session sent 982 read-requests (11
        # repeats + 3 sub-block probe re-reads on top of these), every one
        # served idempotently from the same offsets.
        "frames": frames,
        "payload_bytes": payload,
        "wire_bytes": payload + frames * READ_FRAME_OVERHEAD,
        "sha256": hashlib.sha256(fw).hexdigest(),
        # addr_* are FILE OFFSETS into the artifact -- that is this protocol's
        # entire address semantics.
        "addr_first": regions[0]["offset"],
        "addr_last": regions[-1]["offset"] + regions[-1]["length"] - 1,
        "notes": (
            "DEVICE-PULL protocol: no precompiled stream. The NR board sends "
            "UPDATE_READ_REQ(offset,count) and the host must reply with "
            "artifact[offset:offset+count] verbatim (artifact = the vendor "
            ".ufw, unmodified; verify sha256 before serving). frames/"
            "payload_bytes/wire_bytes are the planned minimum host->device "
            "read-responses over expected_regions at 512-byte granularity; "
            "the device may repeat or sub-divide reads. The flash phase pulls "
            "nr_image in 512-byte blocks STRICTLY DESCENDING so the boot-"
            "validated header is written last. Termination: the DEVICE sends "
            "UPDATE_STOP with status 0x00 on success."),
        "ufw_bytes": len(fw),
        "trailer_u32": info["trailer_u32"],
        "file_table_offset": TABLE_OFF,
        "file_table": info["file_table"],
        "expected_regions": regions,
        "flash_phase": {
            "offset": img_start,
            "length": img_len,
            "block": BLOCK,
            "order": "descending",
            "first_block": img_start + img_len - BLOCK,
            "last_block": img_start,
            "blocks": img_len // BLOCK,
        },
        "frame_grammar": {
            "magic": FRAME_MAGIC.hex(),
            "length_field": "u16 LE at [2:4] = opcode+payload byte count; "
                            "frame total = 6 + length",
            "opcode_offset": 4,
            "crc": {
                "algo": "CRC-16/XMODEM",
                "poly": CRC_POLY,
                "init": CRC_INIT,
                "reflected": False,
                "stored": "u16 LE, last 2 bytes of the frame",
                "over": "the whole frame INCLUDING the AA 55 magic, "
                        "excluding the CRC itself",
            },
            "opcodes": OPCODES,
            "read_req": "device->host, 15 B: aa55 | 0900 | 02 | u32le offset "
                        "| u32le count | crc16le",
            "read_res": "host->device: aa55 | u16le(9+count) | 02 | u32le "
                        "offset | u32le count | data | crc16le",
            "read_frame_overhead": READ_FRAME_OVERHEAD,
        },
        "link": {
            "handshake_tx": build_frame(OP_ENTER_UPDATE).hex(),
            "initial_baud": 9600,
            "line": "8N1, DTR+RTS",
            "baud_negotiation": "device sends UPDATE_START; host UPDATE_START_"
                                "RES payload = u32 LE baud, then re-open the "
                                "port at it. Reference session: 9600 -> 10000 "
                                "-> 115200 (a second START round).",
        },
    }


# =============================================================================
# CLI
# =============================================================================
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Validate a D890 NR-board .ufw and emit the serving "
                    "artifact + manifest (device-pull protocol; the artifact "
                    "is the .ufw verbatim).")
    ap.add_argument("--ufw", required=True, help="Vendor .ufw update file.")
    ap.add_argument("--out", required=True,
                    help="Output artifact (the .ufw content, verbatim).")
    ap.add_argument("--manifest", required=True, help="Output JSON manifest.")
    args = ap.parse_args(argv)

    try:
        with open(args.ufw, "rb") as fh:
            fw = fh.read()
    except OSError as e:
        print(f"fwupd_nr: cannot read --ufw: {e}", file=sys.stderr)
        return 2
    try:
        manifest = build_manifest(fw)
    except UfwError as e:
        print(f"fwupd_nr: {args.ufw}: {e}", file=sys.stderr)
        return 2

    try:
        with open(args.out, "wb") as fh:
            fh.write(fw)
        with open(args.manifest, "w") as fh:
            json.dump(manifest, fh, indent=1)
            fh.write("\n")
    except OSError as e:
        print(f"fwupd_nr: cannot write output: {e}", file=sys.stderr)
        return 2
    print(json.dumps(manifest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
