#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys

DEFAULT_PORTAL = os.environ.get("PORTAL_CHECKOUT", "")
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "radio_contacts", "contact_tables.py")


def js_object(src: str, name: str) -> dict:
    start = src.index("export const %s = " % name)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(src[brace:i + 1])
    raise SystemExit("unterminated object literal for %s" % name)


def js_string_lists(src: str, name: str) -> dict:
    start = src.index("const %s = " % name)
    brace = src.index("{", start)
    depth = 0
    end = brace
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    body = src[brace + 1:end]
    out = {}
    for m in re.finditer(r"(\w+):\s*\[(.*?)\]", body, re.S):
        out[m.group(1)] = re.findall(r"'([A-Z]{3})'", m.group(2))
    return out


def main() -> None:
    check = "--check" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    portal = args[0] if args else DEFAULT_PORTAL
    base = os.path.join(portal, "assets", "vue", "utils", "digitalContacts")
    tables_js = os.path.join(base, "tables.js")
    continents_js = os.path.join(base, "continents.js")
    for path in (tables_js, continents_js):
        if not os.path.isfile(path):
            raise SystemExit("not found: %s (pass the portal checkout as an argument)" % path)

    with open(tables_js, encoding="utf-8") as f:
        tsrc = f.read()
    with open(continents_js, encoding="utf-8") as f:
        csrc = f.read()

    alpha3 = js_object(tsrc, "ALPHA3_TO_ALPHA2")
    country_codes = js_object(tsrc, "COUNTRY_CODES")
    members = js_string_lists(csrc, "MEMBERS")

    placed = [c for codes in members.values() for c in codes]
    if len(set(placed)) != len(placed):
        raise SystemExit("a country is filed under two continents in continents.js")
    missing = sorted(set(alpha3) - set(placed))
    if missing:
        raise SystemExit("continents.js does not place: %s" % ", ".join(missing))

    def pyd(d: dict, indent: str = "    ") -> str:
        return "\n".join('%s%r: %r,' % (indent, k, v) for k, v in d.items())

    body = '''"""Country and continent tables for a locally built contact list.

GENERATED FILE -- do not edit. Run tools/gen_contact_tables.py to regenerate.

Transcribed from the aes.app portal's browser builder, which took them from the
command that built the server's own lists. The three must agree: the same
user.csv has to put a contact in the same country whichever tool reads it, or a
list built here and a list built in the browser differ for no visible reason.

COUNTRY_CODES maps a lower-cased country name to its ISO 3166-1 alpha-3 code. It
carries every ISO country name plus the spellings the registers actually write
("Bosnia and Hercegovina", "Korea Republic of", "Luxemburg"), so very little
should ever miss. What does miss is handled by contact_build.country_code(), and
NOT by taking the first three letters as a code: those letters are often another
country's ("Martinique" -> MAR is Morocco), so a colliding guess is marked with
'?' instead. Two values here are deliberately not ISO codes: XKX for Kosovo,
which has none, and the empty string for "Unknown", which is not a place.
"""
from __future__ import annotations

#: lower-cased country name -> alpha-3
COUNTRY_CODES: dict[str, str] = {
%s
}

#: alpha-3 -> ISO 3166-1 alpha-2, kept for reference and for the completeness test
ALPHA3_TO_ALPHA2: dict[str, str] = {
%s
}

#: The continent groups, in the portal's own order of definition.
CONTINENT_MEMBERS: dict[str, tuple[str, ...]] = {
%s
}

#: Display names. "OTHER" also holds a country the tables do not know and the
#: rows that carry no country at all.
CONTINENT_NAMES: dict[str, str] = {
    "AF": "Africa",
    "AS": "Asia",
    "EU": "Europe",
    "NA": "North America",
    "SA": "South America",
    "OC": "Oceania",
    "AN": "Antarctica",
    "OTHER": "Elsewhere and unlisted",
}

COUNTRY_CONTINENT: dict[str, str] = {
    code: key for key, codes in CONTINENT_MEMBERS.items() for code in codes
}


def continent_of(code: str) -> str:
    """The continent key for a country code; anything unlisted is "OTHER"."""
    return COUNTRY_CONTINENT.get(code, "OTHER")
''' % (
        pyd(country_codes),
        pyd(alpha3),
        "\n".join('    %r: (%s),' % (key, "".join("%r, " % c for c in codes))
                  for key, codes in members.items()),
    )

    if check:
        current = open(OUT, encoding="utf-8").read() if os.path.isfile(OUT) else ""
        if current != body:
            raise SystemExit("radio_contacts/contact_tables.py is stale -- run tools/gen_contact_tables.py")
        print("contact_tables.py matches the portal tables (%d countries, %d codes)"
              % (len(country_codes), len(alpha3)))
        return

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(body)
    print("wrote %s\n  %d country names, %d alpha-3 codes, %d continents"
          % (OUT, len(country_codes), len(alpha3), len(members)))


if __name__ == "__main__":
    main()
