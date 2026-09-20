from __future__ import annotations

import hashlib
import http.server
import json
import os
import socketserver
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radio_fw import compiler, download, spec


_ARTIFACT = b"\x01\x02\x03\x04" * 100
_SHA = hashlib.sha256(_ARTIFACT).hexdigest()
_MANIFEST = {"kind": "fw", "frames": 3, "payload_bytes": 400, "sha256": _SHA,
             "ident_reply_prefix_ascii": "ID878UV"}


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/api/firmware/catalog":
            body = json.dumps({"schema": 1, "bundles": [{
                "id": 1, "model": "anytone_d878uv", "fwupdModel": "d878uv",
                "cpsVersion": "4.00", "components": [{
                    "kind": "fw", "label": "Main firmware", "sha256": _SHA,
                    "bytes": len(_ARTIFACT), "frames": 3,
                    "artifactUrl": "/a", "manifestUrl": "/m"}]}]}).encode()
            self._send(200, body)
        elif self.path == "/a":
            self._send(200, _ARTIFACT, "application/octet-stream")
        elif self.path == "/a-corrupt":
            self._send(200, _ARTIFACT + b"\x00", "application/octet-stream")
        elif self.path == "/m":
            self._send(200, json.dumps(_MANIFEST).encode())
        else:
            self._send(404, b"nope", "text/plain")


@pytest.fixture()
def server(monkeypatch, tmp_path):
    srv = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(download, "cache_dir", lambda: str(tmp_path))
    try:
        yield base
    finally:
        srv.shutdown()


def test_catalog_and_verified_download(server):
    bundles = download.fetch_catalog(server)
    assert len(bundles) == 1 and bundles[0]["fwupdModel"] == "d878uv"
    comp = bundles[0]["components"][0]
    res = download.download_component(comp, server)
    assert isinstance(res, compiler.CompileResult)
    assert res.kind == "fw"
    assert res.sha256 == _SHA
    assert res.artifact == _ARTIFACT
    assert res.manifest["ident_reply_prefix_ascii"] == "ID878UV"


def test_checksum_mismatch_is_refused(server):
    bad = {"kind": "fw", "sha256": _SHA, "artifactUrl": "/a-corrupt", "manifestUrl": "/m"}
    with pytest.raises(download.DownloadError):
        download.download_component(bad, server)


def test_cache_hit_serves_without_network(server, tmp_path):
    comp = {"kind": "fw", "sha256": _SHA, "artifactUrl": "/a", "manifestUrl": "/m"}
    download.download_component(comp, server)
    comp2 = {"kind": "fw", "sha256": _SHA, "artifactUrl": "/missing", "manifestUrl": "/m"}
    res = download.download_component(comp2, server)
    assert res.artifact == _ARTIFACT


def test_incomplete_catalog_entry_is_refused(server):
    with pytest.raises(download.DownloadError):
        download.download_component({"kind": "fw"}, server)


def test_offline_gives_a_clear_error():
    with pytest.raises(download.DownloadError):
        download.fetch_catalog("http://127.0.0.1:59999", timeout=2)


def _serve(handler_cls):
    srv = socketserver.TCPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_retries_transient_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(download.time, "sleep", lambda *a: None)

    class H(http.server.BaseHTTPRequestHandler):
        hits = 0
        def log_message(self, *a): pass
        def do_GET(self):
            H.hits += 1
            if H.hits < 3:
                self.send_response(503); self.end_headers(); self.wfile.write(b"busy"); return
            body = json.dumps({"schema": 1, "bundles": []}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    H.hits = 0
    srv, base = _serve(H)
    try:
        statuses = []
        out = download.fetch_catalog(base, on_status=statuses.append)
        assert H.hits == 3
        assert out == []
        assert any("retrying" in s.lower() for s in statuses)
    finally:
        srv.shutdown()


def test_retries_give_up_after_max_and_raise(monkeypatch):
    monkeypatch.setattr(download.time, "sleep", lambda *a: None)

    class H(http.server.BaseHTTPRequestHandler):
        hits = 0
        def log_message(self, *a): pass
        def do_GET(self):
            H.hits += 1
            self.send_response(500); self.end_headers(); self.wfile.write(b"nope")

    H.hits = 0
    srv, base = _serve(H)
    try:
        with pytest.raises(download.DownloadError):
            download.fetch_catalog(base)
        assert H.hits == download._MAX_ATTEMPTS
    finally:
        srv.shutdown()


def _spi(model_tail: bytes) -> bytes:
    import struct
    return struct.pack("<IHI", 32, 1, 400) + b"\x00\x00\x00\x00" + model_tail


def test_878_gen_detect_from_spi():
    assert compiler._detect_878_gen(_spi(b"D878UV2 V100"), "d878uv2") == "d878uv2"
    assert compiler._detect_878_gen(_spi(b"D878UV  V100"), "d878uv2") == "d878uv"
    assert compiler._detect_878_gen(None, "d878uv2") == "d878uv2"
    assert compiler._detect_878_gen(_spi(b"D878UV2 V100"), "d890") == "d890"


def test_app_version_is_sent_in_query_and_user_agent(monkeypatch):
    monkeypatch.setattr(download, "APP_VERSION", "9.9.9")
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            seen["path"] = self.path
            seen["ua"] = self.headers.get("User-Agent", "")
            body = json.dumps({"schema": 1, "bundles": []}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    srv, base = _serve(H)
    try:
        download.fetch_catalog(base)
        assert "v=9.9.9" in seen["path"]
        assert "9.9.9" in seen["ua"]
    finally:
        srv.shutdown()
