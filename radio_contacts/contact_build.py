from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import segments as seg
from .contact_tables import ALPHA3_TO_ALPHA2, CONTINENT_NAMES, COUNTRY_CODES, continent_of


@dataclass(frozen=True)
class RadioSpec:
    ident: str
    label: str
    fmt: str
    nx_fmt: Optional[str]
    capacity: int
    nx_capacity: Optional[int] = None


RADIOS: Dict[str, RadioSpec] = {
    "D878UV": RadioSpec("D878UV", "AnyTone AT-D878UV", "anytone_878", None, 200000),
    "D878UV2": RadioSpec("D878UV2", "AnyTone AT-D878UVII", "anytone_878", None, 500000),
    "D578UV": RadioSpec("D578UV", "AnyTone AT-D578UV", "anytone_878", None, 500000),
    "D578UV2": RadioSpec("D578UV2", "AnyTone AT-D578UVII", "anytone_878", None, 500000),
    "D168UV": RadioSpec("D168UV", "AnyTone AT-D168UV", "anytone_878", None, 500000),
    "D890UV": RadioSpec("D890UV", "AnyTone AT-D890UV", "anytone_890", "anytone_890_nx", 500000, 80000),
    "DMR-7X2": RadioSpec("DMR-7X2", "BTECH DMR-7X2 (AT-D890UV)", "anytone_890", "anytone_890_nx",
                         500000, 80000),
}

RECOGNISED_UNSUPPORTED: Dict[str, str] = {
    "D868UV": "AnyTone AT-D868UV",
    "D878S": "AnyTone AT-D878S (single band)",
    "D6X2UV2": "BTECH DMR-6X2 Pro",
}


def radio_for_ident(model: str) -> Optional[RadioSpec]:
    return RADIOS.get((model or "").strip())


def unsupported_label(model: str) -> Optional[str]:
    return RECOGNISED_UNSUPPORTED.get((model or "").strip())


BLOCK = seg.BLOCK

PAGE_878 = 0x40000
IDX_878, HDR_878, BODY_878 = 0x04000000, 0x04840000, 0x05500000
IDX_878_PER_PAGE, BODY_878_PER_PAGE = 128000, 100000
BODY_GUARD_BYTES = 48

PAGE_890 = 0x80000
HDR_890, IDX_890, BODY_890 = 0x07000000, 0x07080000, 0x07900000
IDX_890_PER_PAGE, BODY_890_PER_PAGE = 256000, 200000

NX_BITMAP, NX_BODY, NX_INDEX = 0x18280000, 0x18300000, 0x1A400000
NX_REC = 0xC8
NX_BODY_PER_PAGE = 200000
NX_RECS_PER_PAGE = NX_BODY_PER_PAGE // NX_REC
NX_MAX_RECORDS = (NX_INDEX - NX_BODY) // PAGE_890 * NX_RECS_PER_PAGE

FIELD_CAPS = {"name": 16, "city": 15, "callsign": 8, "state": 16, "country": 16, "remarks": 16}

MIN_SUBSCRIBER_ID = 1000000
MAX_RADIO_ID = 2147483647
MIN_EXPECTED_RECORDS = 100000


class ContactBuildError(Exception):
    pass


def _is_contact_addr_878(a: int) -> bool:
    return (IDX_878 <= a < 0x04800000) or a >= HDR_878


def _is_contact_addr_890(a: int) -> bool:
    return (0x07000000 <= a < 0x18000000) or (0x18280000 <= a < 0x1B000000)


_NEVER_WRITE = ((0x02F9FFF0, 0x02FA0100), (0x04F80000, 0x04F80100))


def _never_write(a: int) -> bool:
    return any(lo <= a < hi for lo, hi in _NEVER_WRITE)


def check_plan(plan: Sequence[seg.Segment], fmt: str) -> None:
    guard = _is_contact_addr_890 if fmt in ("anytone_890", "anytone_890_nx") else _is_contact_addr_878
    for s in plan:
        for off in range(0, len(s.data), BLOCK):
            a = s.addr + off
            if not guard(a) or _never_write(a):
                raise ContactBuildError(
                    "refusing to write outside the contact area, at 0x%08X. This is a bug in the "
                    "list builder, not something you did -- nothing has been sent to the radio." % a)


def cps_field(value: str, cap: int) -> bytes:
    raw = (value or "").strip(" \t\r\n").encode("utf-8", "replace")
    out = bytearray(raw[:cap])
    for i, b in enumerate(out):
        if b < 0x20 or b > 0x7E:
            out[i] = 0x3F
    while out and out[-1] == 0x20:
        out.pop()
    return bytes(out)


def contact_index_key(radio_id: int) -> int:
    m = 0
    p = 0
    while True:
        d = radio_id // (10 ** (p + 1))
        if d == 0:
            break
        m += d * (16 ** p)
        p += 1
    key = 2 * radio_id + 12 * m
    if key > 0xFFFFFFFF:
        raise ContactBuildError(
            "radio ID %d cannot be stored in this contact format (its lookup key does not fit). "
            "Check the file: no amateur DMR ID is that large." % radio_id)
    return key


def _bcd4(decimal: str) -> bytes:
    s = decimal.rjust(8, "0")
    return bytes(((ord(s[i]) - 48) << 4) | (ord(s[i + 1]) - 48) for i in range(0, 8, 2))


def _bcd5(rid: int) -> bytes:
    s = "%010d" % (int(rid) % (10 ** 10))
    return bytes(((ord(s[i]) - 48) << 4) | (ord(s[i + 1]) - 48) for i in range(0, 10, 2))


def _physical_end(logical: int, per_page: int, page: int) -> int:
    return (logical // per_page) * page + (logical % per_page)


class _PagedSink:

    def __init__(self, base: int, per_page: int, page: int) -> None:
        self.base = base
        self.per_page = per_page
        self.page = page
        self._chunks: List[bytearray] = []
        self._logical = 0

    def push(self, data: bytes) -> None:
        off = 0
        while off < len(data):
            if not self._chunks or len(self._chunks[-1]) == self.per_page:
                self._chunks.append(bytearray())
            room = self.per_page - len(self._chunks[-1])
            take = min(room, len(data) - off)
            self._chunks[-1] += data[off:off + take]
            off += take
            self._logical += take

    @property
    def logical(self) -> int:
        return self._logical

    def pad_to_block(self) -> None:
        rem = self._logical % BLOCK
        if rem:
            self.push(b"\x00" * (BLOCK - rem))

    def pad_final_page_to(self, boundary: int) -> None:
        last = self._logical % self.per_page
        if last:
            pad = (boundary - (last % boundary)) % boundary
            if pad:
                self.push(b"\x00" * pad)

    def segments(self, tail_fill: int = 0x00) -> List[seg.Segment]:
        out = []
        for i, chunk in enumerate(self._chunks):
            data = bytes(chunk)
            rem = len(data) % BLOCK
            if rem:
                data += bytes([tail_fill]) * (BLOCK - rem)
            out.append(seg.Segment(self.base + i * self.page, data))
        return out


def _finish(parts: Iterable[seg.Segment]) -> List[seg.Segment]:
    out = sorted((s for s in parts if len(s.data)), key=lambda s: s.addr)
    end = -1
    for s in out:
        if s.addr % BLOCK or len(s.data) % BLOCK:
            raise ContactBuildError("segment at 0x%08X is not block-aligned" % s.addr)
        if s.addr < end:
            raise ContactBuildError("segments overlap at 0x%08X" % s.addr)
        end = s.addr + len(s.data)
    return out


@dataclass(frozen=True)
class Facet:
    code: str
    label: str
    count: int


@dataclass
class ContactStore:
    ids: List[int] = field(default_factory=list)
    callsigns: List[str] = field(default_factory=list)
    names: List[str] = field(default_factory=list)
    cities: List[str] = field(default_factory=list)
    states: List[str] = field(default_factory=list)
    countries: List[str] = field(default_factory=list)
    codes: List[str] = field(default_factory=list)
    skipped: int = 0
    duplicates: int = 0

    def __len__(self) -> int:
        return len(self.ids)

    def facets(self) -> List[Facet]:
        counts: Dict[str, int] = {}
        labels: Dict[str, Dict[str, int]] = {}
        for code, country in zip(self.codes, self.countries):
            counts[code] = counts.get(code, 0) + 1
            if country:
                per = labels.setdefault(code, {})
                per[country] = per.get(country, 0) + 1
        out = []
        for code, count in counts.items():
            per = labels.get(code, {})
            label = max(sorted(per), key=lambda k: per[k]) if per else ""
            out.append(Facet(code, label, count))
        return out

    def count_selected(self, codes: Optional[Iterable[str]]) -> int:
        if codes is None:
            return len(self.ids)
        allowed = set(codes)
        return sum(1 for c in self.codes if c in allowed)


def nx_facets(rows: Sequence[dict]) -> List[Facet]:
    counts: Dict[str, int] = {}
    labels: Dict[str, Dict[str, int]] = {}
    for row in rows:
        country = _trim(str(row.get("COUNTRY", "")))
        code = country_code(country)
        counts[code] = counts.get(code, 0) + 1
        if country:
            per = labels.setdefault(code, {})
            per[country] = per.get(country, 0) + 1
    out = []
    for code, count in counts.items():
        per = labels.get(code, {})
        label = max(sorted(per), key=lambda k: per[k]) if per else ""
        out.append(Facet(code, label, count))
    return out


def merge_facets(*groups: Sequence[Facet]) -> List[Facet]:
    counts: Dict[str, int] = {}
    votes: Dict[str, Dict[str, int]] = {}
    for facets in groups:
        for facet in facets:
            counts[facet.code] = counts.get(facet.code, 0) + facet.count
            if facet.label:
                per = votes.setdefault(facet.code, {})
                per[facet.label] = per.get(facet.label, 0) + facet.count
    out = []
    for code, count in counts.items():
        per = votes.get(code, {})
        label = max(sorted(per), key=lambda k: per[k]) if per else ""
        out.append(Facet(code, label, count))
    return out


def filter_nx_rows(rows: Sequence[dict], codes: Optional[Iterable[str]]) -> List[dict]:
    if codes is None:
        return list(rows)
    allowed = set(codes)
    return [r for r in rows if country_code(_trim(str(r.get("COUNTRY", "")))) in allowed]


def group_facets(facets: Sequence[Facet]) -> List[Tuple[str, str, int, List[Facet]]]:
    groups: Dict[str, List[Facet]] = {}
    for facet in facets:
        key = "OTHER" if facet.code == "" else continent_of(facet.code)
        groups.setdefault(key, []).append(facet)
    out = []
    for key, members in groups.items():
        members.sort(key=lambda f: (-f.count, f.label))
        out.append((key, CONTINENT_NAMES.get(key, key), sum(f.count for f in members), members))
    out.sort(key=lambda g: -g[2])
    return out


_TRIM = " \t\n\r\x00\x0b"


def _trim(value: str) -> str:
    return (value or "").strip(_TRIM)


def _ascii_lower(value: str) -> str:
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in value)


def _ascii_upper(value: str) -> str:
    return "".join(chr(ord(c) - 32) if "a" <= c <= "z" else c for c in value)


def country_code(country: str) -> str:
    key = _trim(country)
    hit = COUNTRY_CODES.get(_ascii_lower(key))
    if hit is not None:
        return hit
    raw = key.encode("utf-8", "replace")
    end = min(3, len(raw))
    while end > 0 and end < len(raw) and (raw[end] & 0xC0) == 0x80:
        end -= 1
    guess = _ascii_upper(raw[:end].decode("utf-8", "replace"))

    return guess[:2] + "?" if guess in ALPHA3_TO_ALPHA2 else guess


_HEADER_ALIASES = {
    "radio_id": ("radioid", "id", "dmrid"),
    "callsign": ("callsign", "call"),
    "name": ("name", "firstname", "fname"),
    "city": ("city",),
    "state": ("state", "province"),
    "country": ("country",),
}

_MIN_ROW_FIELDS = 6


def _norm_header(cell: str) -> str:
    return "".join(ch for ch in (cell or "").strip().lower() if ch.isalnum())


def _map_columns(header: Sequence[str]) -> Dict[str, int]:
    cells = [_norm_header(c) for c in header]
    try:
        anchor = next(i for i, c in enumerate(cells) if c in _HEADER_ALIASES["radio_id"])
    except StopIteration:
        raise ContactBuildError(
            "that file's first line does not name a radio ID column, so there is no way to tell "
            "which field is which. Check that it is RadioID's user.csv and that the download finished.")
    positional = {"radio_id": 0, "callsign": 1, "name": 2, "city": 3, "state": 4, "country": 5}
    columns = {}
    for key, aliases in _HEADER_ALIASES.items():
        found = next((i for i, c in enumerate(cells) if c in aliases), -1)
        columns[key] = found if found >= 0 else anchor + positional[key]
    return columns


def read_user_csv(path: str, on_progress: Optional[Callable[[int], None]] = None,
                  min_bytes: int = 1000) -> ContactStore:
    if not os.path.isfile(path):
        raise ContactBuildError("no such file: %s" % path)
    if os.path.getsize(path) < min_bytes:
        raise ContactBuildError(
            "%s is too small to be the real file (%d bytes). Check that the download finished."
            % (os.path.basename(path), os.path.getsize(path)))

    store = ContactStore()
    seen = set()
    rows: List[Tuple[int, str, str, str, str, str]] = []

    with io.open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        sample = f.readline()
        f.seek(0)
        delimiter = ";" if sample.count(";") >= max(1, sample.count(",")) and sample.count(";") >= 5 else ","
        reader = csv.reader(f, delimiter=delimiter)
        header = next(reader, None)
        if header is None:
            raise ContactBuildError("%s is empty" % os.path.basename(path))
        columns = _map_columns(header)
        for n, fields in enumerate(reader, 1):
            if len(fields) < _MIN_ROW_FIELDS:
                store.skipped += 1
                continue
            try:
                radio_id = int(_trim(fields[columns["radio_id"]]))
            except (ValueError, IndexError):
                store.skipped += 1
                continue
            callsign = _ascii_upper(_trim(
                fields[columns["callsign"]] if columns["callsign"] < len(fields) else ""))
            if radio_id <= 0 or radio_id > MAX_RADIO_ID or not callsign:
                store.skipped += 1
                continue
            if radio_id < MIN_SUBSCRIBER_ID:
                store.skipped += 1
                continue
            if radio_id in seen:
                store.duplicates += 1
                continue
            seen.add(radio_id)

            def cell(key: str) -> str:
                i = columns[key]
                return _trim(fields[i] if 0 <= i < len(fields) else "")

            rows.append((radio_id, callsign, cell("name"), cell("city"), cell("state"), cell("country")))
            if on_progress is not None and (n & 0x3FFF) == 0:
                on_progress(len(rows))

    if not rows:
        raise ContactBuildError(
            "no usable contacts came out of %s. Check that it is the register's user database and "
            "not another file." % os.path.basename(path))

    rows.sort(key=lambda r: r[0])
    for radio_id, callsign, name, city, state, country in rows:
        store.ids.append(radio_id)
        store.callsigns.append(callsign)
        store.names.append(name)
        store.cities.append(city)
        store.states.append(state)
        store.countries.append(country)
        store.codes.append(country_code(country))

    return store


_NXDN_COLUMNS = {
    "RADIO_ID": ("radioid", "id"),
    "CALLSIGN": ("callsign", "call"),
    "FIRST_NAME": ("firstname", "fname"),
    "LAST_NAME": ("lastname", "lname"),
    "CITY": ("city",),
    "STATE": ("state", "province"),
    "COUNTRY": ("country",),
}


def read_nxdn_csv(path: str) -> List[dict]:
    if not os.path.isfile(path):
        raise ContactBuildError("no such file: %s" % path)
    with io.open(path, "rb") as f:
        data = f.read()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("windows-1252", "replace")

    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if header is None:
        raise ContactBuildError("%s is empty" % os.path.basename(path))
    cells = [_norm_header(c) for c in header]
    columns = {}
    for key, aliases in _NXDN_COLUMNS.items():
        columns[key] = next((i for i, c in enumerate(cells) if c in aliases), -1)
    if columns["RADIO_ID"] < 0:
        raise ContactBuildError(
            "that does not look like an NXDN export: its first line should name a RADIO_ID column.")

    rows = []
    for fields in reader:
        try:
            radio_id = int(_trim(fields[columns["RADIO_ID"]]))
        except (ValueError, IndexError):
            continue
        if radio_id <= 0:
            continue
        row = {"RADIO_ID": radio_id}
        for key in _NXDN_COLUMNS:
            if key == "RADIO_ID":
                continue
            i = columns[key]
            row[key] = _trim(fields[i] if 0 <= i < len(fields) else "")
        rows.append(row)

    if not rows:
        raise ContactBuildError("no usable contacts came out of %s." % os.path.basename(path))
    return rows


def _selected(store: ContactStore, codes: Optional[Iterable[str]]):
    if codes is None:
        return range(len(store.ids))
    allowed = set(codes)
    return [i for i, code in enumerate(store.codes) if code in allowed]


def _record_878(store: ContactStore, i: int) -> bytes:
    out = bytearray(b"\x00")
    out += _bcd4(str(store.ids[i]))
    out += b"\x00"
    for value, cap in ((store.names[i], FIELD_CAPS["name"]),
                       (store.cities[i], FIELD_CAPS["city"]),
                       (store.callsigns[i], FIELD_CAPS["callsign"]),
                       (store.states[i], FIELD_CAPS["state"]),
                       (store.countries[i], FIELD_CAPS["country"]),
                       ("", FIELD_CAPS["remarks"])):
        out += cps_field(value, cap) + b"\x00"
    return bytes(out)


def _record_890(store: ContactStore, i: int) -> bytes:
    out = bytearray(b"\x00\x00")
    out += _bcd4(str(store.ids[i] % 100000000))
    for value, cap in ((store.names[i], FIELD_CAPS["name"]),
                       (store.cities[i], FIELD_CAPS["city"]),
                       (store.callsigns[i], FIELD_CAPS["callsign"]),
                       (store.states[i], FIELD_CAPS["state"]),
                       (store.countries[i], FIELD_CAPS["country"]),
                       ("", FIELD_CAPS["remarks"])):
        ascii_bytes = cps_field(value, cap)
        out += b"".join(bytes((b, 0)) for b in ascii_bytes) + b"\x00\x00"
    return bytes(out)


def build_dmr_segments(store: ContactStore, fmt: str, codes: Optional[Iterable[str]] = None,
                       on_progress: Optional[Callable[[int], None]] = None) -> List[seg.Segment]:
    if fmt == "anytone_878":
        page, idx_base, body_base = PAGE_878, IDX_878, BODY_878
        idx_per, body_per = IDX_878_PER_PAGE, BODY_878_PER_PAGE
        header_addr, record = HDR_878, _record_878
    elif fmt == "anytone_890":
        page, idx_base, body_base = PAGE_890, IDX_890, BODY_890
        idx_per, body_per = IDX_890_PER_PAGE, BODY_890_PER_PAGE
        header_addr, record = HDR_890, _record_890
    else:
        raise ContactBuildError("unknown contact format %r" % fmt)

    picked = _selected(store, codes)
    body = _PagedSink(body_base, body_per, page)
    keys: List[int] = []
    offsets: List[int] = []
    for n, i in enumerate(picked, 1):
        offsets.append(body.logical)
        keys.append(contact_index_key(store.ids[i]))
        body.push(record(store, i))
        if on_progress is not None and (n & 0x3FFF) == 0:
            on_progress(n)

    count = len(keys)
    if count == 0:
        return []

    data_len = body.logical
    header = (count.to_bytes(4, "little")
              + (body_base + _physical_end(data_len, body_per, page)).to_bytes(4, "little")
              + b"\x00" * 8)

    if fmt == "anytone_878":
        body.pad_to_block()
        body.push(b"\x00" * BODY_GUARD_BYTES)
    else:
        body.pad_final_page_to(0x80)

    index = _PagedSink(idx_base, idx_per, page)
    for key, off in sorted(zip(keys, offsets)):
        index.push(key.to_bytes(4, "little") + off.to_bytes(4, "little"))
    parts = index.segments(tail_fill=0xFF)
    parts.append(seg.Segment(header_addr, header))
    parts += body.segments()
    plan = _finish(parts)
    check_plan(plan, fmt)
    return plan


_CP1252_HIGH = {
    0x80: 0x20AC, 0x82: 0x201A, 0x83: 0x0192, 0x84: 0x201E, 0x85: 0x2026, 0x86: 0x2020,
    0x87: 0x2021, 0x88: 0x02C6, 0x89: 0x2030, 0x8A: 0x0160, 0x8B: 0x2039, 0x8C: 0x0152,
    0x8E: 0x017D, 0x91: 0x2018, 0x92: 0x2019, 0x93: 0x201C, 0x94: 0x201D, 0x95: 0x2022,
    0x96: 0x2013, 0x97: 0x2014, 0x98: 0x02DC, 0x99: 0x2122, 0x9A: 0x0161, 0x9B: 0x203A,
    0x9C: 0x0153, 0x9E: 0x017E, 0x9F: 0x0178,
}

_NX_FIELDS = (("FIRST_NAME", 0x06, 0x26), ("LAST_NAME", 0x2C, 0x20), ("CITY", 0x4C, 0x20),
              ("CALLSIGN", 0x6C, 0x18), ("STATE", 0x84, 0x22), ("COUNTRY", 0xA6, 0x22))
_NX_CAPS = {"FIRST_NAME": 16, "LAST_NAME": 16, "CITY": 16, "CALLSIGN": 16, "STATE": 17, "COUNTRY": 17}


def _nx_field(value: str, cap: int, width: int) -> bytes:
    text = (value or "").split("\x00")[0]
    out = bytearray(width)
    o = 0
    for ch in text[:cap]:
        if o + 1 >= width:
            break
        code = ord(ch)
        byte = code if code < 0x100 else 0x3F
        point = _CP1252_HIGH.get(byte, byte)
        out[o] = point & 0xFF
        out[o + 1] = (point >> 8) & 0xFF
        o += 2
    return bytes(out)


def build_nx_segments(rows: Sequence[dict]) -> List[seg.Segment]:
    n = len(rows)
    if n == 0:
        return []
    if n > NX_MAX_RECORDS:
        raise ContactBuildError(
            "%d NXDN contacts is past the %d this app can lay out safely: beyond that the records "
            "would reach the search index, and no capture shows where the radio moves it."
            % (n, NX_MAX_RECORDS))

    parts: List[seg.Segment] = []
    for start in range(0, n, NX_RECS_PER_PAGE):
        chunk = bytearray()
        for row in rows[start:start + NX_RECS_PER_PAGE]:
            rec = bytearray(NX_REC)
            rec[0:5] = _bcd5(row.get("RADIO_ID", 0))
            for key, off, width in _NX_FIELDS:
                rec[off:off + width] = _nx_field(str(row.get(key, "")), _NX_CAPS[key], width)
            chunk += rec
        rem = len(chunk) % BLOCK
        if rem:
            chunk += bytes(BLOCK - rem)
        parts.append(seg.Segment(NX_BODY + (start // NX_RECS_PER_PAGE) * PAGE_890, bytes(chunk)))

    entries = sorted((2 * int(str(int(row.get("RADIO_ID", 0))), 16), slot)
                     for slot, row in enumerate(rows))
    index = bytearray()
    for key, slot in entries:
        index += key.to_bytes(4, "little") + slot.to_bytes(4, "little")
    rem = len(index) % BLOCK
    if rem:
        index += b"\x00" * (BLOCK - rem)
    parts.append(seg.Segment(NX_INDEX, bytes(index)))

    bitmap_len = -(-(-(-n // 8)) // BLOCK) * BLOCK
    bitmap = bytearray(bitmap_len)
    for k in range(bitmap_len):
        val = 0
        for bit in range(8):
            if 8 * k + bit >= n:
                val |= 1 << bit
        bitmap[k] = val
    parts.append(seg.Segment(NX_BITMAP, bytes(bitmap)))

    plan = _finish(parts)
    check_plan(plan, "anytone_890_nx")
    return plan


def describe_plan(plan: Sequence[seg.Segment], count: int) -> str:
    blocks = seg.block_count(plan)
    return "%s contacts, %s blocks in %d segments (%.1f MB)" % (
        format(count, ","), format(blocks, ","), len(plan), blocks * BLOCK / 1048576.0)
