"""The contact-catalog client against a stand-in server: the bearer session
(200/401/403/410), the catalog, the per-radio bundle filter, the dropdown
labelling, and the sha256 gate + by-hash cache on artifacts.

Run:  python -m pytest tests/test_contacts_catalog.py -q
"""
from __future__ import annotations

import gzip
import hashlib
import http.server
import json
import os
import socketserver
import struct
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radio_fw import download  # noqa: E402
from radio_contacts import catalog, segments as seg  # noqa: E402

_RAW = b"CBSEG1\x00\x00" + struct.pack(">I", 1) + struct.pack(">II", 0x07000000, 32) + bytes(range(32))
_GZ = gzip.compress(_RAW)
_SHA = hashlib.sha256(_GZ).hexdigest()

_CATALOG = {
    "schema": 1, "generatedAt": "2026-09-04T03:10:00+00:00",
    "radios": {
        "D878UV":  {"label": "AT-D878UV", "format": "anytone_878", "nxFormat": None, "capacity": 200000,
                    "enabled": True, "note": None},
        "D878UV2": {"label": "AT-D878UVII", "format": "anytone_878", "nxFormat": None, "capacity": 500000,
                    "enabled": True, "note": None},
        "D890UV":  {"label": "AT-D890UV", "format": "anytone_890", "nxFormat": "anytone_890_nx",
                    "capacity": 500000, "enabled": True, "note": None},
        "D168UV":  {"label": "AT-D168UV", "format": "anytone_878", "nxFormat": None, "capacity": 500000,
                    "enabled": False, "note": "pending hardware validation"},
    },
    "lists": {"200k": {"label": "200k — 10 popular countries", "kind": "dmr"},
              "full": {"label": "Full — worldwide", "kind": "dmr"},
              "nxdn": {"label": "NXDN", "kind": "nxdn"}},
    "bundles": [
        {"id": 1, "list": "200k", "listLabel": "200k — 10 popular countries", "kind": "dmr",
         "format": "anytone_878", "recordCount": 196670, "sha256": _SHA, "bytes": len(_GZ),
         "rawBytes": len(_RAW), "blocks": 2, "builtAt": "2026-09-04T03:05:00+00:00",
         "sourceUpdatedAt": "2026-09-03T21:05:00+00:00", "artifactUrl": "/api/contacts/1/artifact"},
        {"id": 2, "list": "full", "listLabel": "Full — worldwide", "kind": "dmr",
         "format": "anytone_878", "recordCount": 308012, "sha256": _SHA, "bytes": len(_GZ),
         "rawBytes": len(_RAW), "blocks": 2, "builtAt": "2026-09-04T03:05:00+00:00",
         "sourceUpdatedAt": None, "artifactUrl": "/api/contacts/2/artifact"},
        {"id": 3, "list": "200k", "listLabel": "200k — 10 popular countries", "kind": "dmr",
         "format": "anytone_890", "recordCount": 196670, "sha256": _SHA, "bytes": len(_GZ),
         "rawBytes": len(_RAW), "blocks": 2, "builtAt": "2026-09-04T03:05:00+00:00",
         "sourceUpdatedAt": None, "artifactUrl": "/api/contacts/3/artifact"},
        {"id": 4, "list": "nxdn", "listLabel": "NXDN", "kind": "nxdn",
         "format": "anytone_890_nx", "recordCount": 16570, "sha256": _SHA, "bytes": len(_GZ),
         "rawBytes": len(_RAW), "blocks": 2, "builtAt": "2026-09-04T03:35:00+00:00",
         "sourceUpdatedAt": None, "artifactUrl": "/api/contacts/4/artifact"},
        {"id": 5, "list": "200k", "listLabel": "corrupt", "kind": "dmr",
         "format": "anytone_878", "recordCount": 10, "sha256": _SHA, "bytes": len(_GZ),
         "rawBytes": len(_RAW), "blocks": 2, "builtAt": "2026-09-04T03:05:00+00:00",
         "sourceUpdatedAt": None, "artifactUrl": "/api/contacts/5/artifact"},
    ],
}

SEEN = {"agents": [], "auth": []}


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        SEEN["agents"].append(self.headers.get("User-Agent", ""))
        a = self.headers.get("Authorization", "")
        SEEN["auth"].append(a)
        return a[len("Bearer "):] if a.startswith("Bearer ") else ""

    def do_POST(self):
        if self.path != "/api/contacts/session":
            return self._send(404, b"nope", "text/plain")
        tok = self._auth()
        if tok == "goodtokengoodtoken":
            return self._send(200, json.dumps({"ok": True, "expiresAt": "2026-09-04T13:00:00+00:00",
                                               "account": "s***@aes.app", "tier": "basic"}).encode())
        if tok == "oldtokenoldtoken1":
            return self._send(410, json.dumps({"error": "token_superseded", "message": "newer link"}).encode())
        if tok == "expiredtokenexpire":
            return self._send(410, json.dumps({"error": "token_expired"}).encode())
        if tok == "freetokenfreetoken":
            return self._send(403, json.dumps({"error": "plan_required"}).encode())
        return self._send(401, json.dumps({"error": "invalid_token", "message": "unknown"}).encode())

    def do_GET(self):
        tok = self._auth()
        path = self.path.split("?", 1)[0]
        if tok != "goodtokengoodtoken":
            return self._send(401, json.dumps({"error": "invalid_token"}).encode())
        if path == "/api/contacts/catalog":
            return self._send(200, json.dumps(_CATALOG).encode(),
                              extra={"ETag": '"' + _SHA + '"'})
        if path in ("/api/contacts/1/artifact", "/api/contacts/3/artifact", "/api/contacts/4/artifact"):
            return self._send(200, _GZ, "application/gzip", {"X-Artifact-Sha256": _SHA})
        if path == "/api/contacts/5/artifact":
            return self._send(200, _GZ + b"\x00", "application/gzip")
        return self._send(404, b"nope", "text/plain")


@pytest.fixture()
def server(monkeypatch, tmp_path):
    srv = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(catalog, "cache_dir", lambda: str(tmp_path))
    monkeypatch.setattr(download, "APP_VERSION", "9.9.9-test")
    SEEN["agents"].clear()
    SEEN["auth"].clear()
    try:
        yield base
    finally:
        srv.shutdown()


GOOD = "goodtokengoodtoken"


def test_claim_session_ok_and_reports_the_app_version(server):
    s = catalog.claim_session(server, GOOD)
    assert s["ok"] and s["account"] == "s***@aes.app" and s["tier"] == "basic"
    assert SEEN["auth"][-1] == "Bearer " + GOOD
    assert SEEN["agents"][-1] == "AesApp-Radio-Updater/9.9.9-test"


@pytest.mark.parametrize("token, code, status, hint", [
    ("badtokenbadtoken1", "invalid_token", 401, "not valid"),
    ("oldtokenoldtoken1", "token_superseded", 410, "newer link"),
    ("expiredtokenexpire", "token_expired", 410, "expired"),
    ("freetokenfreetoken", "plan_required", 403, "plan"),
])
def test_refused_tokens_are_typed_and_friendly(server, token, code, status, hint):
    with pytest.raises(catalog.SessionError) as ei:
        catalog.claim_session(server, token)
    assert ei.value.code == code and ei.value.status == status
    assert hint in str(ei.value).lower()


def test_catalog_and_per_radio_filtering(server):
    cat = catalog.fetch_catalog(server, GOOD)
    assert set(cat) >= {"radios", "lists", "bundles"}
    r878 = catalog.radio_entry(cat, "D878UV")
    ids = [b["id"] for b in catalog.bundles_for(cat, r878, "dmr")]
    assert ids == [1, 5], "gen-1 878: the 308k full list is over its 200k capacity; 890-format bundles are not offered"
    r878ii = catalog.radio_entry(cat, "D878UV2")
    assert [b["id"] for b in catalog.bundles_for(cat, r878ii, "dmr")] == [1, 2, 5]
    assert catalog.bundles_for(cat, r878ii, "nxdn") == []
    r890 = catalog.radio_entry(cat, "D890UV")
    assert [b["id"] for b in catalog.bundles_for(cat, r890, "dmr")] == [3]
    assert [b["id"] for b in catalog.bundles_for(cat, r890, "nxdn")] == [4]
    assert catalog.radio_entry(cat, "D999") is None
    assert catalog.radio_entry(cat, "D168UV")["enabled"] is False
    assert catalog.bundle_label(cat["bundles"][0]) == "200k — 10 popular countries — 196,670 contacts (built 2026-09-04)"
    # a stale session on the catalog is a SessionError too
    with pytest.raises(catalog.SessionError):
        catalog.fetch_catalog(server, "badtokenbadtoken1")


def test_owned_rows_ride_the_same_filter_and_lead_the_dropdown():
    """The Digital Contact Builder returns a user's own versions as ordinary
    rows of the same `bundles` array. Nothing in this module knows that: format
    + kind + capacity is the whole filter and the `lists` map is never read,
    which is why an operator's versions reach BOTH dropdowns — DMR and NXDN —
    with no change to the shipped client.

    ORDER is the server's decision and it is not the same for the two kinds.
    Owned DMR rows lead, because every version of this client has had a DMR
    dropdown, so preselecting what the operator built surprises nobody. Owned
    NXDN rows come LAST, because versions before the picker bound the NXDN
    checkbox to the first NXDN bundle with no way to choose: leading with an
    owned row there would silently change what an un-updated app writes to a
    radio. The fixture below is ordered the way the server really sends it."""
    owned_nx = {"id": 91, "list": "my12", "listLabel": "My list \u00b7 Ontario + BC", "kind": "nxdn",
                "format": "anytone_890_nx", "recordCount": 341, "sha256": _SHA, "bytes": len(_GZ),
                "rawBytes": len(_RAW), "blocks": 1, "builtAt": "2026-09-05T09:00:00+00:00",
                "sourceUpdatedAt": None, "artifactUrl": "/api/contacts/u/91/artifact",
                "owned": True, "listName": "Ontario + BC"}
    owned_dmr = dict(owned_nx, id=90, kind="dmr", format="anytone_890", recordCount=3449,
                     artifactUrl="/api/contacts/u/90/artifact")
    cat = dict(_CATALOG, bundles=[owned_dmr] + list(_CATALOG["bundles"]) + [owned_nx])
    r890 = catalog.radio_entry(cat, "D890UV")
    assert [b["id"] for b in catalog.bundles_for(cat, r890, "dmr")] == [90, 3], \
        "the operator's own DMR version leads — the tab preselects index 0"
    assert [b["id"] for b in catalog.bundles_for(cat, r890, "nxdn")] == [4, 91], \
        "but the prebuilt NXDN list leads, so an untouched checkbox writes what it always did"
    r878 = catalog.radio_entry(cat, "D878UV")
    assert [b["id"] for b in catalog.bundles_for(cat, r878, "dmr")] == [1, 5], \
        "an 890-format version is never offered to an 878"
    assert catalog.bundles_for(cat, r878, "nxdn") == []


def test_label_bundles_keeps_server_order_and_never_shadows_a_row():
    rows = [
        {"id": 91, "listLabel": "My list \u00b7 Ontario", "recordCount": 341,
         "builtAt": "2026-09-05T09:00:00+00:00"},
        {"id": 92, "listLabel": "My list \u00b7 Ontario", "recordCount": 341,
         "builtAt": "2026-09-05T09:00:00+00:00"},
        {"id": 4, "listLabel": "NXDN", "recordCount": 16570, "builtAt": "2026-09-04T03:35:00+00:00"},
    ]
    pairs = catalog.label_bundles(rows)
    labels = [lab for lab, _b in pairs]
    assert [b["id"] for _lab, b in pairs] == [91, 92, 4], "server order is the dropdown order"
    assert labels[0] == "My list \u00b7 Ontario \u2014 341 contacts (built 2026-09-05)"
    assert labels[1] == labels[0] + " [92]", "two versions that label the same are told apart by id"
    assert len(set(labels)) == 3
    # the tab looks the pick back up by its label: every row must be reachable
    m = dict(pairs)
    assert [m[lab]["id"] for lab in labels] == [91, 92, 4]
    assert catalog.label_bundles([]) == []


def test_artifact_is_verified_cached_and_decodable(server, tmp_path):
    cat = catalog.fetch_catalog(server, GOOD)
    b = cat["bundles"][0]
    got = catalog.download_artifact(server, GOOD, b)
    assert got == _GZ
    assert seg.block_count(seg.decode_gzip_container(got)) == 2
    assert os.path.exists(os.path.join(str(tmp_path), _SHA + ".cbseg.gz"))
    # served from cache: point the URL at a 404 and it still comes back
    statuses = []
    b2 = dict(b, artifactUrl="/missing")
    assert catalog.download_artifact(server, GOOD, b2, on_status=statuses.append) == _GZ
    assert any("earlier" in s for s in statuses)


def test_corrupt_artifact_is_refused_and_not_cached(server, tmp_path):
    cat = catalog.fetch_catalog(server, GOOD)
    bad = cat["bundles"][4]
    with pytest.raises(catalog.ContactsError, match="checksum"):
        catalog.download_artifact(server, GOOD, bad)
    assert not os.path.exists(os.path.join(str(tmp_path), _SHA + ".cbseg.gz"))
    with pytest.raises(catalog.ContactsError, match="incomplete"):
        catalog.download_artifact(server, GOOD, {"list": "x"})


def test_offline_is_a_clear_error():
    with pytest.raises(catalog.ContactsError):
        catalog.claim_session("http://127.0.0.1:59999", GOOD, timeout=2)


def test_estimate_scales_with_blocks():
    assert 700 < catalog.estimate_seconds(1_500_000) < 900
