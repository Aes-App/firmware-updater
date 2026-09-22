import hashlib
import json
import os
import sys
import tempfile

SERVER = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CONTACT_ENCODERS", "")
if not SERVER or not os.path.isdir(os.path.join(SERVER, "python", "rdt_builder")):
    raise SystemExit(
        "pass the checkout holding the contact encoders as the first argument, or set "
        "$CONTACT_ENCODERS. This script only regenerates the fixture; the tests need "
        "nothing but the JSON it writes.")
sys.path.insert(0, os.path.join(SERVER, "python"))

from rdt_builder import contact_bundle as cb

ANYTONE_HEADER = ('"No.","Radio ID","Callsign","Name","City","State","Country",'
                  '"Remarks","Call Type","Call Alert"')

COUNTRIES = [
    ("Canada", "CAN"),
    ("United States", "USA"),
    ("Germany", "DEU"),
    ("Türkiye", "TUR"),
    ("Ruritania", "RUR"),
    ("", ""),
]
CITIES = ["Vancouver", "Kelowna", "Prince George", "René City",
          "New Westminster EMO", "", "\u00a0"]
STATES = ["British Columbia", "WA", "", "Baden-Württemberg", "Ontario", "NY"]


def rows(n):
    out = []
    rid = 1000000
    for i in range(1, n + 1):
        rid += 1 + (i * 7919) % 13
        country, code = COUNTRIES[i % len(COUNTRIES)]
        out.append({
            "no": i,
            "radio_id": rid,
            "callsign": "VA7T%03d" % (i % 1000),
            "name": "Name%d%s" % (i, "x" * (i % 19)),
            "city": CITIES[i % len(CITIES)],
            "state": STATES[i % len(STATES)],
            "country": country,
            "code": code,
        })
    return out


def csv_text(rs):
    lines = [ANYTONE_HEADER]
    for r in rs:
        lines.append('"%d","%d","%s","%s","%s","%s","%s","","Private Call","None"'
                     % (r["no"], r["radio_id"], r["callsign"], r["name"],
                        r["city"], r["state"], r["country"]))
    return "\r\n".join(lines) + "\r\n"


NX_HEADER = "RADIO_ID,CALLSIGN,FIRST_NAME,LAST_NAME,CITY,STATE,COUNTRY,Attr,TxForbid,Ring"


def nx_rows(n):
    out = []
    for i in range(1, n + 1):
        out.append({
            "RADIO_ID": i * 3,
            "CALLSIGN": "N%dXX" % i,
            "FIRST_NAME": "First%d" % i,
            "LAST_NAME": "Last%s" % ("y" * (i % 9)),
            "CITY": ["Pétion Ville", "New York", "Longbeachville City", ""][i % 4],
            "STATE": ["QC", "New South Wales", "", "Baden-Württemberg"][i % 4],
            "COUNTRY": ["CA", "United States of America", "", "Türkiye"][i % 4],
        })
    return out


def nx_csv_text(rs):
    lines = [NX_HEADER]
    for r in rs:
        lines.append("%d,%s,%s,%s,%s,%s,%s,,," % (
            r["RADIO_ID"], r["CALLSIGN"], r["FIRST_NAME"], r["LAST_NAME"],
            r["CITY"], r["STATE"], r["COUNTRY"]))
    return "\r\n".join(lines) + "\r\n"


def digest(segments):
    h = hashlib.sha256()
    table = []
    blocks = 0
    for addr, data in segments:
        h.update(addr.to_bytes(4, "big"))
        h.update(len(data).to_bytes(4, "big"))
        h.update(data)
        blocks += len(data) // 16
        table.append({"addr": "%08x" % addr, "len": len(data),
                      "sha256": hashlib.sha256(data).hexdigest()[:16]})
    return {"segments": len(segments), "blocks": blocks,
            "sha256": h.hexdigest(), "table": table}


def main():
    out = {"dmr": {}, "nx": {}}
    tmp = tempfile.mkdtemp()
    for n in (1, 2, 37, 3200, 12000):
        path = os.path.join(tmp, "list%d.csv" % n)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(csv_text(rows(n)))
        entry = {}
        for fmt in ("anytone_878", "anytone_878uv", "anytone_890"):
            segs, count = cb.build_segments(path, fmt)
            assert count == n, (fmt, count, n)
            entry[fmt] = digest(segs)
        out["dmr"][str(n)] = entry

    for n in (1, 7, 1000, 2600):
        path = os.path.join(tmp, "nx%d.csv" % n)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(nx_csv_text(nx_rows(n)))
        segs, count = cb.build_segments(path, "anytone_890_nx")
        assert count == n
        out["nx"][str(n)] = digest(segs)

    json.dump(out, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()
