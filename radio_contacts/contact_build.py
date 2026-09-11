"""Build a digital-contact database from the operator's own register download.

WHAT THIS CHANGES ABOUT THIS APP. Until now the app never encoded a contact: it
downloaded the exact block stream the factory CPS sends, already built by the
server, and streamed it to the radio. That is still what the link-driven flow
does, and it is still the flow to prefer -- the server's bundles are the ones
measured against the factory CPS. This module is the second way in: an operator
who has downloaded a register dump themselves (RadioID.net's user.csv, and
optionally nxdn.csv) can build the same database here, pick the countries they
want, and write it without this app ever fetching or republishing anyone's data.

WHERE THE BYTES COME FROM. Every layout below is a port of the encoders behind
the server's own contact lists, which are byte-validated against captures of the
factory Windows CPS writing real radios. `tests/test_contacts_local_build.py`
pins this port against hashes taken from those encoders, on lists sized to cross
every page boundary. If this file drifts, that test fails; it is the only reason
to trust these bytes.

WHAT IS DELIBERATELY LOSSY, because the CPS is: a contact's fields are cut to the
lengths the CPS stores (name 16, city 15, callsign 8, state 16, country 16) and
every byte outside printable ASCII becomes '?', PER BYTE of the UTF-8 text -- so
"Rene" with an acute is stored as "Ren??", five characters, exactly as the CPS
stores it after importing the same CSV. Improving on that would make a locally
built list differ from the server's for the same input.
"""
from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import segments as seg
from .contact_tables import ALPHA3_TO_ALPHA2, CONTINENT_NAMES, COUNTRY_CODES, continent_of

# --- the radios this can write ----------------------------------------------


@dataclass(frozen=True)
class RadioSpec:
    """What a model needs from us. `ident` is what the 0x02 ID frame answers.

    `capacity` and `nx_capacity` are SEPARATE POOLS in separate flash regions:
    filling the DMR database takes nothing away from the NXDN one. A D890UV holds
    500,000 DMR contacts and 80,000 NXDN contacts, not 500,000 between them.
    """
    ident: str
    label: str
    fmt: str
    nx_fmt: Optional[str]
    capacity: int
    nx_capacity: Optional[int] = None


#: Keyed by ID-frame model string EXACTLY AS engine.parse_ident RETURNS IT --
#: the frame's leading 'I' stripped and nothing else, so an AT-D890UV is
#: "D890UV" and the 7X2 rebadge is "DMR-7X2". The server catalog keys the same
#: radios the same way, and the two have to agree because one Ident feeds both.
#:
#: These keys were briefly written without the leading D, to match a table
#: elsewhere that slices the frame from index 2. Nothing matched: every radio
#: came back as unknown, and the tests missed it because their fake Ident
#: carried the same wrong string. A radio is now identified from RAW FRAME BYTES
#: in tests/test_contacts_local_build.py, which is the only shape of test that
#: can catch two modules disagreeing.
#:
#: EXACTLY matters twice over: 'D878UV' is a strict prefix of 'D878UV2', so a
#: substring match would hand a D878UVII the first-generation radio's 200,000
#: capacity and refuse a list that fits it.
#:
#: These capacities live in the app only for a LOCALLY built list. The
#: link-driven flow still takes them from the server catalog, which is where a
#: newly validated model gets enabled -- that gate is deliberate and this table
#: does not replace it.
#: CAPACITY NOTE, worth reading before changing a number here: 500,000 for the
#: D168UV, D890UV, D878UVII, D578UVII and D578UV; 200,000 for the first-
#: generation D878UV. The server catalog and the browser writer carry the same
#: figures and all three must move together. None of them is measured on
#: hardware -- settle the D578UV against a real one before trusting its number.
RADIOS: Dict[str, RadioSpec] = {
    "D878UV": RadioSpec("D878UV", "AnyTone AT-D878UV", "anytone_878", None, 200000),
    "D878UV2": RadioSpec("D878UV2", "AnyTone AT-D878UVII", "anytone_878", None, 500000),
    "D578UV": RadioSpec("D578UV", "AnyTone AT-D578UV", "anytone_878", None, 500000),
    "D578UV2": RadioSpec("D578UV2", "AnyTone AT-D578UVII", "anytone_878", None, 500000),
    "D168UV": RadioSpec("D168UV", "AnyTone AT-D168UV", "anytone_878", None, 500000),
    "D890UV": RadioSpec("D890UV", "AnyTone AT-D890UV", "anytone_890", "anytone_890_nx", 500000, 80000),
    # Baofeng's rebadged D890UV: same silicon, same addresses, verified on the
    # hardware (its ID frame reads "IDMR-7X2", so the model is "DMR-7X2").
    "DMR-7X2": RadioSpec("DMR-7X2", "BTECH DMR-7X2 (AT-D890UV)", "anytone_890", "anytone_890_nx",
                         500000, 80000),
}

#: Radios we can name from their ID frame but will not build for. Naming one is
#: the difference between "this is an AT-D868UV, which is not supported" and
#: "unknown model D868UV", which reads as though the radio were at fault.
#: Same key convention as RADIOS: what parse_ident returns.
RECOGNISED_UNSUPPORTED: Dict[str, str] = {
    "D868UV": "AnyTone AT-D868UV",
    "D878S": "AnyTone AT-D878S (single band)",
    "D6X2UV2": "BTECH DMR-6X2 Pro",
}


def radio_for_ident(model: str) -> Optional[RadioSpec]:
    return RADIOS.get((model or "").strip())


def unsupported_label(model: str) -> Optional[str]:
    return RECOGNISED_UNSUPPORTED.get((model or "").strip())


# --- addresses, paging, and what may be written ------------------------------

BLOCK = seg.BLOCK

PAGE_878 = 0x40000
IDX_878, HDR_878, BODY_878 = 0x04000000, 0x04840000, 0x05500000
IDX_878_PER_PAGE, BODY_878_PER_PAGE = 128000, 100000
#: The three zero blocks the CPS writes after the body. Not padding: they are
#: written, and every capture has them.
BODY_GUARD_BYTES = 48

PAGE_890 = 0x80000
HDR_890, IDX_890, BODY_890 = 0x07000000, 0x07080000, 0x07900000
IDX_890_PER_PAGE, BODY_890_PER_PAGE = 256000, 200000

NX_BITMAP, NX_BODY, NX_INDEX = 0x18280000, 0x18300000, 0x1A400000
NX_REC = 0xC8
NX_BODY_PER_PAGE = 200000
NX_RECS_PER_PAGE = NX_BODY_PER_PAGE // NX_REC              # 1000
#: What this app can lay out, which is NOT the radio's own ceiling. The body
#: grows towards the index and 66 pages of 1,000 records fit between them; the
#: radio holds 80,000 (RadioSpec.nx_capacity), so its layout must extend further
#: -- but no capture shows where the index moves, and a guess would have the body
#: overwrite its own search index. Refuse rather than guess.
NX_MAX_RECORDS = (NX_INDEX - NX_BODY) // PAGE_890 * NX_RECS_PER_PAGE

FIELD_CAPS = {"name": 16, "city": 15, "callsign": 8, "state": 16, "country": 16, "remarks": 16}

#: Below this a RadioID ID is a repeater or a legacy short ID, not a subscriber --
#: the floor the server applies. user.csv holds no ID under 1,000,000 at all.
MIN_SUBSCRIBER_ID = 1000000
#: dmr_digital_contacts.radio_id is a signed INT on the server, so nothing past
#: this is a contact it would ever have stored.
MAX_RADIO_ID = 2147483647
#: The server refuses a user.csv that yields fewer than this: a truncated download.
MIN_EXPECTED_RECORDS = 100000


class ContactBuildError(Exception):
    """A file we cannot use, or a list a radio cannot hold."""


def _is_contact_addr_878(a: int) -> bool:
    """The gap at 0x04800000 holds a codeplug table and is NOT in the window."""
    return (IDX_878 <= a < 0x04800000) or a >= HDR_878


def _is_contact_addr_890(a: int) -> bool:
    """The NXDN window starts ABOVE the boot-critical metadata at 0x18000000."""
    return (0x07000000 <= a < 0x18000000) or (0x18280000 <= a < 0x1B000000)


#: The factory blocks: model identity, serial number, band plan. Never written.
_NEVER_WRITE = ((0x02F9FFF0, 0x02FA0100), (0x04F80000, 0x04F80100))


def _never_write(a: int) -> bool:
    return any(lo <= a < hi for lo, hi in _NEVER_WRITE)


def check_plan(plan: Sequence[seg.Segment], fmt: str) -> None:
    """Refuse a plan that strays outside the contact area for its format.

    The downloaded-bundle path has no such check, and does not need one: the
    server built those bytes. A plan built HERE is built by code in this repo, so
    it gets one -- and it refuses the whole write rather than dropping a block,
    because a contact database that is missing its index is worse than one that
    was never written.
    """
    guard = _is_contact_addr_890 if fmt in ("anytone_890", "anytone_890_nx") else _is_contact_addr_878
    for s in plan:
        for off in range(0, len(s.data), BLOCK):
            a = s.addr + off
            if not guard(a) or _never_write(a):
                raise ContactBuildError(
                    "refusing to write outside the contact area, at 0x%08X. This is a bug in the "
                    "list builder, not something you did -- nothing has been sent to the radio." % a)


# --- the CPS's own field handling --------------------------------------------


def cps_field(value: str, cap: int) -> bytes:
    """One field as the CPS stores it: printable ASCII, capped, no trailing space."""
    raw = (value or "").strip(" \t\r\n").encode("utf-8", "replace")   # the CPS's own strip
    out = bytearray(raw[:cap])
    for i, b in enumerate(out):
        if b < 0x20 or b > 0x7E:
            out[i] = 0x3F
    # Cutting to the cap can leave a trailing space, which the CPS strips.
    while out and out[-1] == 0x20:
        out.pop()
    return bytes(out)


def contact_index_key(radio_id: int) -> int:
    """The radio's id -> lookup key map, exact on every capture.

        M   = sum over p >= 0 of (id // 10**(p+1)) * 16**p
        key = 2*id + 12*M

    Every seven-digit ID keys inside a u32 (9,999,999 -> 322,122,546); an
    eight-digit one does not, and a wrapped key is a contact the radio can never
    look up, so that is an error rather than a silent truncation.
    """
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
    """A continuous logical stream, cut into `per_page` data bytes every `page`.

    That is how the radio lays its index and body out, and a record may straddle
    a cut -- which is why this exists instead of one flat buffer.
    """

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


# --- the store ---------------------------------------------------------------


@dataclass(frozen=True)
class Facet:
    """One country in the loaded register: its code, what to call it, how many."""
    code: str
    label: str
    count: int


@dataclass
class ContactStore:
    """The register, as parallel lists -- one object per contact would be a third
    of a million allocations on a worldwide file."""
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
        """Contacts per country, with the spelling to show for each.

        The label is the most common raw Country spelling behind a code, because a
        code is not a name: USA, US and United States all fold to USA, and the one
        an operator recognises is whichever their register wrote most often. Rows
        with no country keep the empty code and are their own bucket -- they exist,
        they are a fifth of some registers, and a picker that could not offer them
        would quietly drop them.
        """
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
    """The same country breakdown for an NXDN list.

    The NXDN register writes full country names in the same column, so it goes
    through the same name table: a country ticked in the picker means the same
    thing in both halves of the write, which is the whole point of filtering them
    together.
    """
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
    """Add several country breakdowns together, for a picker that is choosing
    for both lists at once.

    The label follows the spelling with the most rows behind it, whichever list
    it came from, so a country that appears ONLY in the NXDN half still gets a
    name and still appears in the picker.
    """
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
    """The NXDN rows a country selection keeps, in their original order."""
    if codes is None:
        return list(rows)
    allowed = set(codes)
    return [r for r in rows if country_code(_trim(str(r.get("COUNTRY", "")))) in allowed]


def group_facets(facets: Sequence[Facet]) -> List[Tuple[str, str, int, List[Facet]]]:
    """Facets grouped by continent for the picker.

    ORDER IS BY SIZE at both levels, descending: continents by the contacts they
    hold, then the countries inside each the same way. An operator is looking for
    the handful of countries they actually work, and those are the big ones.

        [(key, display name, total, [Facet, …]), …]
    """
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


#: The characters PHP's trim() strips, which is what the server and the browser
#: builder both strip. Python's own str.strip() is Unicode-aware and takes a
#: NO-BREAK SPACE with it -- and a city that holds nothing but one is then empty
#: here and "??" there, which is two bytes of difference in every record after it.
#: Found the hard way, on two rows out of 296,967.
_TRIM = " \t\n\r\x00\x0b"


def _trim(value: str) -> str:
    return (value or "").strip(_TRIM)


def _ascii_lower(value: str) -> str:
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in value)


def _ascii_upper(value: str) -> str:
    return "".join(chr(ord(c) - 32) if "a" <= c <= "z" else c for c in value)


def country_code(country: str) -> str:
    """A country name -> alpha-3, else a marked guess.

    ASCII-only case folding, and the cut is on bytes, because that is what the
    server and the browser builder do: Python's own lower()/upper() would fold
    accented letters and give a different code for the same country name, which
    would put a contact in a different group depending on which tool read the file.

    The table carries every ISO country name plus the spellings the registers
    actually write, so the fallback is for junk. It must never hand back a code
    that means a REAL country: "Martinique" used to become MAR, which is Morocco.
    A guess that collides with a real alpha-3 is marked with '?', which no ISO
    code contains, so it groups with its own kind and matches no filter.

    `hit is not None` rather than a truthiness test: "Unknown" maps to the EMPTY
    code on purpose, and a falsy check would send it to the guess instead.
    """
    key = _trim(country)
    hit = COUNTRY_CODES.get(_ascii_lower(key))
    if hit is not None:
        return hit
    raw = key.encode("utf-8", "replace")
    end = min(3, len(raw))
    # Step back only if the cut landed INSIDE a character: the byte at the cut
    # being a continuation byte is what says so. Testing the last byte we keep
    # instead would throw away a character that fits.
    while end > 0 and end < len(raw) and (raw[end] & 0xC0) == 0x80:
        end -= 1
    guess = _ascii_upper(raw[:end].decode("utf-8", "replace"))

    return guess[:2] + "?" if guess in ALPHA3_TO_ALPHA2 else guess


# --- reading the register ----------------------------------------------------

#: Header spellings accepted per column. Matching by NAME rather than position
#: means a column added or reordered upstream moves the read with it instead of
#: silently shifting every field one to the left.
_HEADER_ALIASES = {
    "radio_id": ("radioid", "id", "dmrid"),
    "callsign": ("callsign", "call"),
    "name": ("name", "firstname", "fname"),
    "city": ("city",),
    "state": ("state", "province"),
    "country": ("country",),
}

#: Country is the sixth field, so a shorter row cannot be a full profile row --
#: which is also what tells a RadioID dmrid.dat (`ID;CALLSIGN;`) apart from a
#: register and stops it being read as one.
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
        # A column this header does not name keeps its usual offset from the radio
        # ID, which is how a CPS export's "Repeater" heading still finds the
        # callsign.
        columns[key] = found if found >= 0 else anchor + positional[key]
    return columns


def read_user_csv(path: str, on_progress: Optional[Callable[[int], None]] = None,
                  min_bytes: int = 1000) -> ContactStore:
    """Read a register dump (RadioID user.csv, or any CSV whose header names a
    radio ID) into a store, sorted by radio ID the way the server's own list is.

    One file, no join: user.csv carries the callsign AND the profile on the row
    that owns them. The pair of files this used to need (dmrid.dat + users.json)
    was measured against the live dumps and bought nothing -- it agreed on every
    ID and every callsign, while the join put 6,450 contacts under a profile
    belonging to another operator who happened to share a callsign.
    """
    if not os.path.isfile(path):
        raise ContactBuildError("no such file: %s" % path)
    if os.path.getsize(path) < min_bytes:
        raise ContactBuildError(
            "%s is too small to be the real file (%d bytes). Check that the download finished."
            % (os.path.basename(path), os.path.getsize(path)))

    store = ContactStore()
    seen = set()
    rows: List[Tuple[int, str, str, str, str, str]] = []

    # Decoded as UTF-8 with replacement, exactly as a browser reads the same file:
    # a byte that is not UTF-8 becomes U+FFFD and then '?' in the record, rather
    # than failing the whole build.
    with io.open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        sample = f.readline()
        f.seek(0)
        delimiter = ";" if sample.count(";") >= max(1, sample.count(",")) and sample.count(";") >= 5 else ","
        reader = csv.reader(f, delimiter=delimiter)
        header = next(reader, None)
        if header is None:
            raise ContactBuildError("%s is empty" % os.path.basename(path))
        columns = _map_columns(header)
        # A file whose first line IS data (no header) would have been refused by
        # _map_columns unless a numeric first cell happens to spell a column name,
        # which no number does.
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

    # Sorted by radio ID, which is the order the server's ORDER BY leaves its own
    # lists in -- so a list built here numbers its rows the same way.
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
    """Read an NXDN register dump into the rows the encoder takes."""
    if not os.path.isfile(path):
        raise ContactBuildError("no such file: %s" % path)
    with io.open(path, "rb") as f:
        data = f.read()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # A CPS export is latin-1; guessing UTF-8 for it would put a replacement
        # character where an operator's name should be.
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


# --- the encoders ------------------------------------------------------------


def _selected(store: ContactStore, codes: Optional[Iterable[str]]):
    """Indices of the contacts a country selection keeps, in store order."""
    if codes is None:
        return range(len(store.ids))
    allowed = set(codes)
    return [i for i, code in enumerate(store.codes) if code in allowed]


def _record_878(store: ContactStore, i: int) -> bytes:
    """[00 call type][4-byte BCD id][00] then six NUL-terminated ASCII strings, in
    the ON-DISK order -- name, city, callsign, state, country, remarks -- which is
    NOT the CSV's column order (the CSV puts the callsign third for readability)."""
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
    """[u16 call type][4-byte BCD id][6 UTF-16LE NUL-terminated strings]."""
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
    """The DMR contact write for one radio format, as a segment plan."""
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

    # The header's second u32 is the body END ADDRESS, built from the body's DATA
    # length -- before the padding and the guard blocks below.
    data_len = body.logical
    header = (count.to_bytes(4, "little")
              + (body_base + _physical_end(data_len, body_per, page)).to_bytes(4, "little")
              + b"\x00" * 8)

    if fmt == "anytone_878":
        body.pad_to_block()
        body.push(b"\x00" * BODY_GUARD_BYTES)
    else:
        # The CPS pads the body's FINAL page chunk to a 0x80 boundary; full pages
        # stay exactly 200000. Measured on two factory writes; "pad to 0x100" fitted
        # only the first of them.
        body.pad_final_page_to(0x80)

    index = _PagedSink(idx_base, idx_per, page)
    for key, off in sorted(zip(keys, offsets)):
        index.push(key.to_bytes(4, "little") + off.to_bytes(4, "little"))
    # The index tail is 0xFF PADDING of the final partial block, never a
    # terminator entry: an odd count ends [entry][ff*8], an even one ends on its
    # last entry with no further block.
    parts = index.segments(tail_fill=0xFF)
    parts.append(seg.Segment(header_addr, header))
    parts += body.segments()
    plan = _finish(parts)
    check_plan(plan, fmt)
    return plan


#: cp1252 for the bytes that differ from latin-1; everything else is identity.
_CP1252_HIGH = {
    0x80: 0x20AC, 0x82: 0x201A, 0x83: 0x0192, 0x84: 0x201E, 0x85: 0x2026, 0x86: 0x2020,
    0x87: 0x2021, 0x88: 0x02C6, 0x89: 0x2030, 0x8A: 0x0160, 0x8B: 0x2039, 0x8C: 0x0152,
    0x8E: 0x017D, 0x91: 0x2018, 0x92: 0x2019, 0x93: 0x201C, 0x94: 0x201D, 0x95: 0x2022,
    0x96: 0x2013, 0x97: 0x2014, 0x98: 0x02DC, 0x99: 0x2122, 0x9A: 0x0161, 0x9B: 0x203A,
    0x9C: 0x0153, 0x9E: 0x017E, 0x9F: 0x0178,
}

#: (key, offset, byte width) inside the 0xC8 record. Country runs to the end.
_NX_FIELDS = (("FIRST_NAME", 0x06, 0x26), ("LAST_NAME", 0x2C, 0x20), ("CITY", 0x4C, 0x20),
              ("CALLSIGN", 0x6C, 0x18), ("STATE", 0x84, 0x22), ("COUNTRY", 0xA6, 0x22))
#: The .rdt's own caps, which the CSV passes through on its way to the radio.
_NX_CAPS = {"FIRST_NAME": 16, "LAST_NAME": 16, "CITY": 16, "CALLSIGN": 16, "STATE": 17, "COUNTRY": 17}


def _nx_field(value: str, cap: int, width: int) -> bytes:
    """One NXDN string as the radio stores it.

    Faithful to the server's chain, which is CSV -> .rdt block -> radio: the text
    is reduced to latin-1 first (anything else becomes '?') and capped there, then
    re-read through cp1252 -- so a byte in 0x80..0x9F becomes its cp1252 character
    rather than the C1 control latin-1 calls it. That last step is what makes a
    .rdt-sourced write and a CSV-sourced write of the same list identical.

    A CPS export pads FIRST_NAME to sixteen characters with a NUL and then spaces,
    so the name is what precedes the NUL.
    """
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
    """The D890UV NXDN contact write, as a segment plan."""
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
        # A 0xC8 record is not a whole number of 16-byte blocks, so an odd count
        # leaves the last block half full: zero-fill it, as the CPS does.
        rem = len(chunk) % BLOCK
        if rem:
            chunk += bytes(BLOCK - rem)
        parts.append(seg.Segment(NX_BODY + (start // NX_RECS_PER_PAGE) * PAGE_890, bytes(chunk)))

    # Index: [u32 key][u32 slot], sorted by key. The key is the id's decimal
    # digits read as hexadecimal, doubled.
    entries = sorted((2 * int(str(int(row.get("RADIO_ID", 0))), 16), slot)
                     for slot, row in enumerate(rows))
    index = bytearray()
    for key, slot in entries:
        index += key.to_bytes(4, "little") + slot.to_bytes(4, "little")
    rem = len(index) % BLOCK
    if rem:
        index += b"\x00" * (BLOCK - rem)
    parts.append(seg.Segment(NX_INDEX, bytes(index)))

    # Allocation bitmap: bit clear = slot occupied, LSB first, sized to the list
    # and padded to the write block.
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
    """A one-line summary for the log."""
    blocks = seg.block_count(plan)
    return "%s contacts, %s blocks in %d segments (%.1f MB)" % (
        format(count, ","), format(blocks, ","), len(plan), blocks * BLOCK / 1048576.0)
