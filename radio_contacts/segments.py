from __future__ import annotations

import gzip
import struct
from dataclasses import dataclass
from typing import Iterable, Iterator

MAGIC = b"CBSEG1\x00\x00"
BLOCK = 16


class SegmentError(Exception):
    pass


@dataclass(frozen=True)
class Segment:
    addr: int
    data: bytes

    @property
    def end(self) -> int:
        return self.addr + len(self.data)

    @property
    def blocks(self) -> int:
        return len(self.data) // BLOCK


def decode_container(raw: bytes) -> list[Segment]:
    raw = bytes(raw)
    if len(raw) < 12 or raw[:8] != MAGIC:
        raise SegmentError("not a contact-bundle container (bad CBSEG1 magic)")
    (count,) = struct.unpack(">I", raw[8:12])
    out: list[Segment] = []
    p = 12
    for i in range(count):
        if p + 8 > len(raw):
            raise SegmentError(f"segment {i}: truncated header (the container ends early)")
        addr, ln = struct.unpack(">II", raw[p:p + 8])
        p += 8
        if ln == 0 or ln % BLOCK != 0:
            raise SegmentError(f"segment {i}: length {ln} is not a whole number of 16-byte blocks")
        if p + ln > len(raw):
            raise SegmentError(f"segment {i}: truncated data (the container ends early)")
        out.append(Segment(addr, raw[p:p + ln]))
        p += ln
    if p != len(raw):
        raise SegmentError("trailing bytes after the last segment")
    _check_layout(out)
    return out


def decode_gzip_container(gz: bytes) -> list[Segment]:
    try:
        raw = gzip.decompress(gz)
    except (OSError, EOFError, ValueError) as e:
        raise SegmentError(f"the contact bundle is not valid gzip data ({e})")
    return decode_container(raw)


def encode_container(segments: Iterable[Segment]) -> bytes:
    segs = list(segments)
    body = bytearray(MAGIC + struct.pack(">I", len(segs)))
    for s in segs:
        if len(s.data) == 0 or len(s.data) % BLOCK != 0:
            raise SegmentError("segment length must be a positive multiple of 16")
        body += struct.pack(">II", s.addr & 0xFFFFFFFF, len(s.data)) + bytes(s.data)
    return bytes(body)


def _check_layout(segments: list[Segment]) -> None:
    prev_end = -1
    for i, s in enumerate(segments):
        if s.addr < prev_end:
            raise SegmentError(f"segment {i} @0x{s.addr:08x} overlaps or precedes the one before it")
        prev_end = s.end


def merge(*segment_lists: Iterable[Segment]) -> list[Segment]:
    segs = sorted((s for lst in segment_lists for s in lst), key=lambda s: s.addr)
    _check_layout(segs)
    return segs


def block_count(segments: Iterable[Segment]) -> int:
    return sum(s.blocks for s in segments)


def iter_blocks(segments: Iterable[Segment]) -> Iterator[tuple[int, bytes]]:
    for s in segments:
        d = s.data
        for off in range(0, len(d), BLOCK):
            yield s.addr + off, d[off:off + BLOCK]


def describe(segments: Iterable[Segment]) -> str:
    segs = list(segments)
    if not segs:
        return "0 segments"
    return (f"{len(segs)} segment(s), {block_count(segs):,} blocks, "
            f"0x{segs[0].addr:08x}..0x{segs[-1].end - 1:08x}")
