from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radio_contacts import contact_build as cb
from radio_contacts import engine
from radio_contacts import segments as seg
from radio_contacts.contact_tables import continent_of

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                       "contact-write-expected.json")
with open(FIXTURE, encoding="utf-8") as _f:
    EXPECTED = json.load(_f)

ANYTONE_HEADER = ('"No.","Radio ID","Callsign","Name","City","State","Country",'
                  '"Remarks","Call Type","Call Alert"')

COUNTRIES = [("Canada", "CAN"), ("United States", "USA"), ("Germany", "DEU"),
             ("Türkiye", "TUR"), ("Ruritania", "RUR"), ("", "")]
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
@pytest.mark.parametrize("fmt", ["anytone_878", "anytone_878uv", "anytone_890"])
def test_dmr_matches_the_server_encoder(tmp_path, count, fmt):
    path = _write_csv(str(tmp_path / ("list%s.csv" % count)), int(count))
    store = cb.read_user_csv(path, min_bytes=0)
    assert len(store) == int(count)
    got = _digest(cb.build_dmr_segments(store, fmt))
    want = EXPECTED["dmr"][count][fmt]
    assert got["table"] == want["table"]
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
    with pytest.raises(cb.ContactBuildError):
        cb.contact_index_key(99999999)


def test_a_plan_never_leaves_the_contact_area(tmp_path):
    store = cb.read_user_csv(_write_csv(str(tmp_path / "l.csv"), 3200), min_bytes=0)
    for fmt in ("anytone_878", "anytone_878uv", "anytone_890"):
        cb.check_plan(cb.build_dmr_segments(store, fmt), fmt)
    cb.check_plan(cb.build_nx_segments(_nx_rows(2600)), "anytone_890_nx")

    with pytest.raises(cb.ContactBuildError):
        cb.check_plan([seg.Segment(0x02580000, bytes(16))], "anytone_878")
    with pytest.raises(cb.ContactBuildError):
        cb.check_plan([seg.Segment(0x04F80000, bytes(16))], "anytone_890")
    with pytest.raises(cb.ContactBuildError):
        cb.check_plan([seg.Segment(0x18180000, bytes(16))], "anytone_890_nx")


def test_the_first_generation_878_writes_its_own_addresses(tmp_path):
    store = cb.read_user_csv(_write_csv(str(tmp_path / "l.csv"), 3200), min_bytes=0)
    gen1 = cb.build_dmr_segments(store, "anytone_878uv")
    uvii = cb.build_dmr_segments(store, "anytone_878")

    assert [s.addr for s in gen1] == [0x04000000, 0x044C0000, 0x04500000, 0x04540000]
    assert [s.addr for s in uvii] == [0x04000000, 0x04840000, 0x05500000, 0x05540000]
    assert [len(s.data) for s in gen1] == [len(s.data) for s in uvii]
    index, header, *body = gen1
    assert index.data == uvii[0].data, "the index holds offsets, so it does not move"
    assert [s.data for s in body] == [s.data for s in uvii[2:]]
    end_gen1 = int.from_bytes(header.data[4:8], "little")
    end_uvii = int.from_bytes(uvii[1].data[4:8], "little")
    assert end_gen1 == end_uvii - (cb.BODY_878 - cb.BODY_878UV) == 0x04552C2C
    assert header.data[:4] == uvii[1].data[:4], "the count is the same list"

    for addr in (0x04340000, 0x04400000, 0x044BFFF0, 0x07700000):
        with pytest.raises(cb.ContactBuildError):
            cb.check_plan([seg.Segment(addr, bytes(16))], "anytone_878uv")
    cb.check_plan([seg.Segment(0x04340000, bytes(16))], "anytone_878")
    cb.check_plan([seg.Segment(0x05500000, bytes(16))], "anytone_878uv")
    with pytest.raises(cb.ContactBuildError):
        cb.check_plan([seg.Segment(0x04800000, bytes(16))], "anytone_878")


def test_each_radio_protects_its_own_option_block_and_only_that(tmp_path):
    store = cb.read_user_csv(_write_csv(str(tmp_path / "big.csv"), 80000), min_bytes=0)
    gen1 = cb.build_dmr_segments(store, "anytone_878uv")
    assert any(s.addr == 0x04F80000 for s in gen1), "the list must reach body page 42"
    cb.check_plan(gen1, "anytone_878uv")

    with pytest.raises(cb.ContactBuildError):
        cb.check_plan([seg.Segment(0x04F80000, bytes(16))], "anytone_890")
    for fmt in ("anytone_878", "anytone_878uv"):
        with pytest.raises(cb.ContactBuildError):
            cb.check_plan([seg.Segment(0x02FA0000, bytes(16))], fmt)
    assert not cb._never_write(0x02FA0000, "anytone_890"), "not the 890's block"
    assert not cb._never_write(0x04F80000, "anytone_878uv"), "not the gen-1's block"
    assert cb._never_write(0x02FA0000, "anytone_878") and cb._never_write(0x04F80000, "anytone_890")


def test_a_country_selection_writes_exactly_what_it_selects(tmp_path):
    store = cb.read_user_csv(_write_csv(str(tmp_path / "l.csv"), 600), min_bytes=0)
    assert store.count_selected(None) == 600
    assert store.count_selected(["CAN", "USA"]) == 200

    picked = cb.build_dmr_segments(store, "anytone_878", ["CAN", "USA"])
    compact = cb.ContactStore()
    for i, code in enumerate(store.codes):
        if code not in ("CAN", "USA"):
            continue
        for name in ("ids", "callsigns", "names", "cities", "states", "countries", "codes"):
            getattr(compact, name).append(getattr(store, name)[i])
    assert _digest(picked)["sha256"] == _digest(cb.build_dmr_segments(compact, "anytone_878"))["sha256"]

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
    other = next(m for key, _n, _t, m in groups if key == "OTHER")
    assert {f.code for f in other} == {"", "RUR"}
    assert cb.country_code("Türkiye") == "TUR"


def test_a_country_name_resolves_the_way_the_server_resolves_it():
    assert cb.country_code("Martinique") == "MTQ"
    assert cb.country_code("Macedonia") == "MKD"
    assert cb.country_code("Dominica") == "DMA"
    assert cb.country_code("Saint Lucia") == "LCA"
    assert cb.country_code("Saint Kitts and Nevis") == "KNA"
    assert cb.country_code("Saint Vincent and the Grenadines") == "VCT"
    assert cb.country_code("Bosnia and Hercegovina") == "BIH"
    assert cb.country_code("Korea Republic of") == "KOR"
    assert cb.country_code("Luxemburg") == "LUX"
    assert cb.country_code("Ivory Coast") == "CIV"
    assert cb.country_code("México") == "MEX"
    assert cb.country_code("Swasiland") == "SWZ"
    assert cb.country_code("Kosovo") == "XKX"
    assert cb.country_code("Corsica") == "FRA"
    assert cb.country_code("Regiao Norte") == "PRT"
    assert cb.country_code("Unknown") == ""
    assert cb.country_code("") == ""
    assert cb.country_code("USA") == "USA"
    assert cb.country_code("uk") == "GBR"
    assert cb.country_code("Macau") == "MAC"


def test_a_name_nobody_knows_is_never_mistaken_for_a_country():
    assert cb.country_code("Mar del Plata") == "MA?"
    assert continent_of("MA?") == "OTHER"
    assert cb.country_code("Zzz Nowhere") == "ZZZ"


def test_a_file_that_is_not_a_register_is_refused(tmp_path):
    bad = tmp_path / "notes.csv"
    bad.write_text("some,columns,that,mean,nothing,at,all\n" + ("1,2,3,4,5,6,7\n" * 200), encoding="utf-8")
    with pytest.raises(cb.ContactBuildError):
        cb.read_user_csv(str(bad))

    tiny = tmp_path / "tiny.csv"
    tiny.write_text(ANYTONE_HEADER, encoding="utf-8")
    with pytest.raises(cb.ContactBuildError):
        cb.read_user_csv(str(tiny))
    with pytest.raises(cb.ContactBuildError):
        cb.read_user_csv(str(tiny), min_bytes=0)


def test_the_radio_table_is_exact_and_excludes_the_868(tmp_path):
    assert cb.radio_for_ident("D878UV").capacity == 200000
    assert cb.radio_for_ident("D878UV2").capacity == 500000
    assert cb.radio_for_ident("D878UV").fmt == "anytone_878uv"
    assert cb.radio_for_ident("D878UV2").fmt == "anytone_878"
    assert cb.radio_for_ident("D578UV").capacity == 500000
    assert cb.radio_for_ident("D168UV").fmt == "anytone_878"
    assert cb.radio_for_ident("D890UV").nx_fmt == "anytone_890_nx"
    assert cb.radio_for_ident("DMR-7X2").fmt == "anytone_890"
    assert cb.radio_for_ident("D868UV") is None
    assert "D868UV" in cb.unsupported_label("D868UV")


IDENT_FRAMES = {
    "AT-D890UV": b"ID890UV" + b"\x00" + b"V100" + b"\x00\x00\x00" + b"\x06",
    "AT-D878UVII": b"ID878UV2" + b"\x0e" + b"V101" + b"\x00\x00" + b"\x06",
    "AT-D578UVII": b"ID578UV2" + b"\x0e" + b"V101" + b"\x00\x00" + b"\x06",
    "AT-D168UV": b"ID168UV" + b"\x00" + b"V100" + b"\x00\x00\x00" + b"\x06",
    "DMR-7X2": bytes.fromhex("49444d522d3758320056313030010006"),
}


@pytest.mark.parametrize("name,raw", sorted(IDENT_FRAMES.items()))
def test_a_radio_is_recognised_from_the_BYTES_it_actually_sends(name, raw):
    ident = engine.parse_ident(raw)
    spec = cb.radio_for_ident(ident.model)
    assert spec is not None, f"{name} identifies as {ident.model!r}, which the table does not hold"
    assert name in spec.label


def test_the_868_is_named_from_its_bytes_rather_than_called_unknown():
    ident = engine.parse_ident(b"ID868UV" + b"\x00" + b"V100" + b"\x00\x00\x00" + b"\x06")
    assert cb.radio_for_ident(ident.model) is None
    assert cb.unsupported_label(ident.model) == "AnyTone AT-D868UV"


def test_the_cps_field_rules_are_the_lossy_ones_the_cps_applies():
    assert cb.cps_field("René", 16) == b"Ren??"
    assert cb.cps_field("New Westminster EMO", 16) == b"New Westminster"
    assert cb.cps_field("  padded  ", 16) == b"padded"
    assert cb.cps_field("", 16) == b""
