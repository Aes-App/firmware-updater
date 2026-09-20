from __future__ import annotations

import gzip
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radio_contacts import segments as seg


def _container(segs):
    body = b"CBSEG1\x00\x00" + struct.pack(">I", len(segs))
    for addr, data in segs:
        body += struct.pack(">II", addr, len(data)) + data
    return body


def test_round_trip_and_block_order():
    a = (0x07000000, bytes(range(32)))
    b = (0x07900000, b"\xab" * 16)
    segs = seg.decode_container(_container([a, b]))
    assert [(s.addr, s.data) for s in segs] == [a, b]
    assert seg.block_count(segs) == 3
    blocks = list(seg.iter_blocks(segs))
    assert [x[0] for x in blocks] == [0x07000000, 0x07000010, 0x07900000]
    assert blocks[1][1] == bytes(range(16, 32))
    assert all(len(x[1]) == 16 for x in blocks)
    assert seg.encode_container(segs) == _container([a, b])
    assert "3 blocks" in seg.describe(segs)


def test_empty_container_is_valid():
    assert seg.decode_container(_container([])) == []
    assert seg.block_count([]) == 0


def test_gzip_wrapper():
    raw = _container([(0x10, bytes(16))])
    assert seg.decode_gzip_container(gzip.compress(raw))[0].addr == 0x10
    with pytest.raises(seg.SegmentError, match="gzip"):
        seg.decode_gzip_container(b"not gzip at all")


@pytest.mark.parametrize("mutate, message", [
    (lambda raw: b"XBSEG1\x00\x00" + raw[8:], "magic"),
    (lambda raw: raw[:-1], "truncated"),
    (lambda raw: raw[:12] + raw[12:16], "truncated header"),
    (lambda raw: raw + b"\x00", "trailing"),
    (lambda raw: raw[:8] + struct.pack(">I", 2) + raw[12:], "truncated"),
])
def test_malformed_is_refused(mutate, message):
    raw = _container([(0x07000000, bytes(32))])
    with pytest.raises(seg.SegmentError, match=message):
        seg.decode_container(mutate(raw))


def test_non_16_length_is_refused():
    with pytest.raises(seg.SegmentError, match="16-byte"):
        seg.decode_container(_container([(0x07000000, bytes(24))]))
    with pytest.raises(seg.SegmentError, match="16-byte"):
        seg.decode_container(_container([(0x07000000, b"")]))


def test_overlap_and_order_are_refused():
    with pytest.raises(seg.SegmentError, match="overlaps"):
        seg.decode_container(_container([(0x100, bytes(32)), (0x110, bytes(16))]))
    with pytest.raises(seg.SegmentError, match="overlaps or precedes"):
        seg.decode_container(_container([(0x200, bytes(16)), (0x100, bytes(16))]))


def test_merge_sorts_two_lists_into_one_plan_and_refuses_overlap():
    dmr = seg.decode_container(_container([(0x07000000, bytes(16)), (0x07900000, bytes(16))]))
    nx = seg.decode_container(_container([(0x18280000, bytes(16)), (0x1A400000, bytes(16))]))
    plan = seg.merge(nx, dmr)
    assert [s.addr for s in plan] == [0x07000000, 0x07900000, 0x18280000, 0x1A400000]
    with pytest.raises(seg.SegmentError):
        seg.merge(dmr, dmr)
