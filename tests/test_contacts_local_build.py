"""A locally built contact database is byte-identical to the server's.

The fixture in tests/fixtures/contact-write-expected.json was produced by
tests/fixtures/gen-contact-write-expected.py, which runs the server's own
contact encoders -- the ones whose output matches captures of the
factory Windows CPS writing real radios. The aes.app portal's browser builder is
pinned against the SAME fixture, so the three implementations cannot drift apart
without a test failing.

The lists are sized to cross every page boundary each format has: 3,200 records
pass the 878's 100,000-byte body page, 12,000 pass it several times and pass the
890's 200,000-byte page, and the NXDN cases straddle its 1,000-record page. One
and two records are in there because an odd and an even count end the index
differently.

Run:  python -m pytest tests/test_contacts_local_build.py -q
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radio_contacts import contact_build as cb  # noqa: E402
from radio_contacts import engine  # noqa: E402
from radio_contacts import segments as seg  # noqa: E402
from radio_contacts.contact_tables import continent_of  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                       "contact-write-expected.json")
with open(FIXTURE, encoding="utf-8") as _f:
    EXPECTED = json.load(_f)

ANYTONE_HEADER = ('"No.","Radio ID","Callsign","Name","City","State","Country",'
                  '"Remarks","Call Type","Call Alert"')

# The rows the generator builds, mirrored verbatim.
COUNTRIES = [("Canada", "CAN"), ("United States", "USA"), ("Germany", "DEU"),
             ("Türkiye", "TUR"), ("Ruritania", "RUR"), ("", "")]
# "\u00a0" is a NO-BREAK SPACE: the CPS stores it as "??", while a Unicode-aware
# trim sees an empty city and shortens every record after it by two bytes. Two
# rows in the live register carry one; it is what caught this port.
CITIES = ["Vancouver", "Kelowna", "Prince George", "René City", "New Westminster EMO",
          "", "\u00a0"]
STATES = ["British Columbia", "WA", "", "Baden-Württemberg", "Ontario", "NY"]


def _rows(n):
    out = []
    rid = 1000000
    for i in range(1, n + 1):
        rid += 1 + (i * 7919) % 13
        country, _code = COUNTRIES[i % len(COUNTRIES)]
        out.append({
            "no": i, "radio_id": rid, "callsign": "VA7T%03d" % (i % 1000),
            "name": "Name%d%s" % (i, "x" * (i % 19)),
            "city": CITIES[i % len(CITIES)], "state": STATES[i % len(STATES)],
            "country": country,
        })
    return out


def _write_csv(path, n):
    lines = [ANYTONE_HEADER]
    for r in _rows(n):
        lines.append('"%d","%d","%s","%s","%s","%s","%s","","Private Call","None"'
                     % (r["no"], r["radio_id"], r["callsign"], r["name"],
                        r["city"], r["state"], r["country"]))
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")
    return path


NX_HEADER = "RADIO_ID,CALLSIGN,FIRST_NAME,LAST_NAME,CITY,STATE,COUNTRY,Attr,TxForbid,Ring"


def _nx_rows(n):
    out = []
    for i in range(1, n + 1):
        out.append({
            "RADIO_ID": i * 3, "CALLSIGN": "N%dXX" % i,
            "FIRST_NAME": "First%d" % i, "LAST_NAME": "Last%s" % ("y" * (i % 9)),
            "CITY": ["Pétion Ville", "New York", "Longbeachville City", ""][i % 4],
            "STATE": ["QC", "New South Wales", "", "Baden-Württemberg"][i % 4],
            "COUNTRY": ["CA", "United States of America", "", "Türkiye"][i % 4],
        })
    return out


def _write_nx_csv(path, n):
    lines = [NX_HEADER]
    for r in _nx_rows(n):
        lines.append("%d,%s,%s,%s,%s,%s,%s,,," % (
            r["RADIO_ID"], r["CALLSIGN"], r["FIRST_NAME"], r["LAST_NAME"],
            r["CITY"], r["STATE"], r["COUNTRY"]))
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")
    return path


def _digest(plan):
    h = hashlib.sha256()
    table = []
    blocks = 0
    for s in plan:
        h.update(s.addr.to_bytes(4, "big"))
        h.update(len(s.data).to_bytes(4, "big"))
        h.update(s.data)
        blocks += len(s.data) // 16
        table.append({"addr": "%08x" % s.addr, "len": len(s.data),
                      "sha256": hashlib.sha256(s.data).hexdigest()[:16]})
    return {"segments": len(plan), "blocks": blocks, "sha256": h.hexdigest(), "table": table}


@pytest.mark.parametrize("count", sorted(EXPECTED["dmr"], key=int))
@pytest.mark.parametrize("fmt", ["anytone_878", "anytone_890"])
def test_dmr_matches_the_server_encoder(tmp_path, count, fmt):
    path = _write_csv(str(tmp_path / ("list%s.csv" % count)), int(count))
    # min_bytes=0: the one- and two-record cases are far under the
    # truncated-download floor a real register download has to clear.
    store = cb.read_user_csv(path, min_bytes=0)
    assert len(store) == int(count)
    got = _digest(cb.build_dmr_segments(store, fmt))
    want = EXPECTED["dmr"][count][fmt]
    assert got["table"] == want["table"]          # says WHICH region drifted
    assert got["sha256"] == want["sha256"]
    assert got["blocks"] == want["blocks"]


@pytest.mark.parametrize("count", sorted(EXPECTED["nx"], key=int))
def test_nxdn_matches_the_server_encoder(tmp_path, count):
    path = _write_nx_csv(str(tmp_path / ("nx%s.csv" % count)), int(count))
    rows = cb.read_nxdn_csv(path)
    assert len(rows) == int(count)
    got = _digest(cb.build_nx_segments(rows))
    assert got["table"] == EXPECTED["nx"][count]["table"]
    assert got["sha256"] == EXPECTED["nx"][count]["sha256"]


def test_the_index_key_matches_the_radios_own_map():
    assert cb.contact_index_key(1) == 2
    assert cb.contact_index_key(7) == 14
    assert cb.contact_index_key(1000000) == 33554432
    assert cb.contact_index_key(3101234) == 102769768
    assert cb.contact_index_key(16777215) == 753853482
    # Eight digits overflow the u32 index field: be loud, never wrap.
    with pytest.raises(cb.ContactBuildError):
        cb.contact_index_key(99999999)


def test_a_plan_never_leaves_the_contact_area(tmp_path):
    store = cb.read_user_csv(_write_csv(str(tmp_path / "l.csv"), 3200), min_bytes=0)
    for fmt in ("anytone_878", "anytone_890"):
        cb.check_plan(cb.build_dmr_segments(store, fmt), fmt)      # must not raise
    cb.check_plan(cb.build_nx_segments(_nx_rows(2600)), "anytone_890_nx")

    # The codeplug is not ours to touch, and neither is the factory block.
    with pytest.raises(cb.ContactBuildError):
        cb.check_plan([seg.Segment(0x02580000, bytes(16))], "anytone_878")
    with pytest.raises(cb.ContactBuildError):
        cb.check_plan([seg.Segment(0x04F80000, bytes(16))], "anytone_890")
    # 0x18000000..0x1818007F is the 890's boot-critical NXDN metadata, NOT contacts.
    with pytest.raises(cb.ContactBuildError):
        cb.check_plan([seg.Segment(0x18180000, bytes(16))], "anytone_890_nx")


def test_a_country_selection_writes_exactly_what_it_selects(tmp_path):
    store = cb.read_user_csv(_write_csv(str(tmp_path / "l.csv"), 600), min_bytes=0)
    assert store.count_selected(None) == 600
    assert store.count_selected(["CAN", "USA"]) == 200

    picked = cb.build_dmr_segments(store, "anytone_878", ["CAN", "USA"])
    # The same rows, encoded on their own, must give the same bytes: a filter
    # chooses contacts, it never changes how one is encoded.
    compact = cb.ContactStore()
    for i, code in enumerate(store.codes):
        if code not in ("CAN", "USA"):
            continue
        for name in ("ids", "callsigns", "names", "cities", "states", "countries", "codes"):
            getattr(compact, name).append(getattr(store, name)[i])
    assert _digest(picked)["sha256"] == _digest(cb.build_dmr_segments(compact, "anytone_878"))["sha256"]

    # Nothing selected produces no plan at all, rather than an empty database.
    assert cb.build_dmr_segments(store, "anytone_878", ["ZZZ"]) == []


def test_the_picker_is_grouped_by_continent_and_ordered_by_size(tmp_path):
    store = cb.read_user_csv(_write_csv(str(tmp_path / "l.csv"), 600), min_bytes=0)
    groups = cb.group_facets(store.facets())
    totals = [total for _key, _name, total, _members in groups]
    assert totals == sorted(totals, reverse=True), "continents must be largest first"
    for _key, _name, _total, members in groups:
        counts = [f.count for f in members]
        assert counts == sorted(counts, reverse=True), "countries must be largest first"

    keys = {key for key, _name, _total, _members in groups}
    assert {"NA", "EU", "AS"} <= keys
    # Rows with no country, and a country the tables do not know, both land in the
    # group the picker must therefore always render. "Türkiye" is NOT in it: the
    # accented spelling is one of the aliases now.
    other = next(m for key, _n, _t, m in groups if key == "OTHER")
    assert {f.code for f in other} == {"", "RUR"}
    assert cb.country_code("Türkiye") == "TUR"


def test_a_country_name_resolves_the_way_the_server_resolves_it():
    """The spellings that cost 11,125 rows of the live register their country.

    Four of them were worse than unrecognised: the old fallback took the first
    three letters as a code, and those letters were another country's.
    """
    assert cb.country_code("Martinique") == "MTQ"          # was MAR, Morocco
    assert cb.country_code("Macedonia") == "MKD"           # was MAC, Macao
    assert cb.country_code("Dominica") == "DMA"            # was DOM
    assert cb.country_code("Saint Lucia") == "LCA"         # all three Saints were SAI
    assert cb.country_code("Saint Kitts and Nevis") == "KNA"
    assert cb.country_code("Saint Vincent and the Grenadines") == "VCT"
    assert cb.country_code("Bosnia and Hercegovina") == "BIH"
    assert cb.country_code("Korea Republic of") == "KOR"
    assert cb.country_code("Luxemburg") == "LUX"
    assert cb.country_code("Ivory Coast") == "CIV"
    assert cb.country_code("México") == "MEX"
    assert cb.country_code("Swasiland") == "SWZ"
    assert cb.country_code("Kosovo") == "XKX"
    # Regions written in the country column, filed under the country they are in.
    assert cb.country_code("Corsica") == "FRA"
    assert cb.country_code("Regiao Norte") == "PRT"
    # "Unknown" is not a place: it joins the rows carrying no country at all.
    assert cb.country_code("Unknown") == ""
    assert cb.country_code("") == ""
    # The short forms this table has always carried.
    assert cb.country_code("USA") == "USA"
    assert cb.country_code("uk") == "GBR"
    assert cb.country_code("Macau") == "MAC"


def test_a_name_nobody_knows_is_never_mistaken_for_a_country():
    # A city in the country column, whose first three letters ARE a country code.
    assert cb.country_code("Mar del Plata") == "MA?"
    assert continent_of("MA?") == "OTHER"
    # Junk whose first three letters are not a code keeps them, so it still
    # groups with its own kind in the picker.
    assert cb.country_code("Zzz Nowhere") == "ZZZ"


def test_a_file_that_is_not_a_register_is_refused(tmp_path):
    bad = tmp_path / "notes.csv"
    bad.write_text("some,columns,that,mean,nothing,at,all\n" + ("1,2,3,4,5,6,7\n" * 200), encoding="utf-8")
    with pytest.raises(cb.ContactBuildError):
        cb.read_user_csv(str(bad))

    tiny = tmp_path / "tiny.csv"
    tiny.write_text(ANYTONE_HEADER, encoding="utf-8")
    with pytest.raises(cb.ContactBuildError):
        cb.read_user_csv(str(tiny))          # under the truncated-download floor
    with pytest.raises(cb.ContactBuildError):
        cb.read_user_csv(str(tiny), min_bytes=0)   # and it holds no rows either


def test_the_radio_table_is_exact_and_excludes_the_868(tmp_path):
    assert cb.radio_for_ident("D878UV").capacity == 200000
    assert cb.radio_for_ident("D878UV2").capacity == 500000
    assert cb.radio_for_ident("D578UV").capacity == 500000
    assert cb.radio_for_ident("D168UV").fmt == "anytone_878"
    assert cb.radio_for_ident("D890UV").nx_fmt == "anytone_890_nx"
    assert cb.radio_for_ident("DMR-7X2").fmt == "anytone_890"
    assert cb.radio_for_ident("D868UV") is None
    assert "D868UV" in cb.unsupported_label("D868UV")


#: Identity frames as they come off the wire: 'I' + model + band + version, 16
#: bytes ending in ACK. The 878 and 7X2 frames are the ones tests/test_contacts_
#: engine.py already pins against the real protocol.
IDENT_FRAMES = {
    "AT-D890UV": b"ID890UV" + b"\x00" + b"V100" + b"\x00\x00\x00" + b"\x06",
    "AT-D878UVII": b"ID878UV2" + b"\x0e" + b"V101" + b"\x00\x00" + b"\x06",
    "AT-D578UVII": b"ID578UV2" + b"\x0e" + b"V101" + b"\x00\x00" + b"\x06",
    "AT-D168UV": b"ID168UV" + b"\x00" + b"V100" + b"\x00\x00\x00" + b"\x06",
    "DMR-7X2": bytes.fromhex("49444d522d3758320056313030010006"),
}


@pytest.mark.parametrize("name,raw", sorted(IDENT_FRAMES.items()))
def test_a_radio_is_recognised_from_the_BYTES_it_actually_sends(name, raw):
    """From the wire to a spec, across both modules, with nothing in between.

    THE BUG THIS EXISTS FOR. This table was first keyed "890UV", copied from the
    browser's table -- which is right THERE, because that code slices the ID
    frame from index 2. engine.parse_ident strips only the
    leading 'I', so a real AT-D890UV arrives here as "D890UV" and matched
    nothing: every radio on the local path came back unknown. The tab's tests
    all passed, because their fake Ident carried "890UV" too -- the same wrong
    assumption on both sides of the assertion.

    So this test starts from BYTES. A convention that drifts between the parser
    and the table now fails here, and a test double cannot paper over it.
    """
    ident = engine.parse_ident(raw)
    spec = cb.radio_for_ident(ident.model)
    assert spec is not None, f"{name} identifies as {ident.model!r}, which the table does not hold"
    assert name in spec.label


def test_the_868_is_named_from_its_bytes_rather_than_called_unknown():
    ident = engine.parse_ident(b"ID868UV" + b"\x00" + b"V100" + b"\x00\x00\x00" + b"\x06")
    assert cb.radio_for_ident(ident.model) is None
    assert cb.unsupported_label(ident.model) == "AnyTone AT-D868UV"


def test_the_cps_field_rules_are_the_lossy_ones_the_cps_applies():
    # Per BYTE of UTF-8, not per character: five characters out of four.
    assert cb.cps_field("René", 16) == b"Ren??"
    # Cut to the cap, then a trailing space removed.
    assert cb.cps_field("New Westminster EMO", 16) == b"New Westminster"
    assert cb.cps_field("  padded  ", 16) == b"padded"
    assert cb.cps_field("", 16) == b""
