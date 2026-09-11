"""The server side of a contact refresh: claim the launch token, list the
prebuilt contact bundles, download one (sha256-verified, cached by hash).

Mirrors radio_fw.download — stdlib urllib + certifi, the same retry/backoff on
transient 429/5xx, the same by-hash cache discipline — with one addition: every
request carries the launch token as a bearer, because the contact catalog is
for paying users and the server enforces that per request, not just at link
time. See docs on the server for the contract (CONTACT_BUNDLES.md).
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from typing import Callable, Optional
from urllib.parse import urlencode, urljoin

from radio_fw import download as _dl

DEFAULT_BASE_URL = os.environ.get("AESAPP_FW_BASE_URL", "https://cps.aes.app")
SESSION_PATH = "/api/contacts/session"
CATALOG_PATH = "/api/contacts/catalog"

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 4
_BACKOFF_BASE = 3.0
_BACKOFF_CAP = 30.0


class ContactsError(Exception):
    """A fetch/verify failure. `str(self)` is operator-facing and complete."""


class SessionError(ContactsError):
    """The server refused the launch token / session. `code` is the server's
    machine-readable error ("invalid_token", "token_expired",
    "token_superseded", "plan_required"), or "http_<status>"."""

    def __init__(self, code: str, message: str, status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


_FRIENDLY = {
    "invalid_token": "This link is not valid. Open cps.aes.app/tools/contact-lists and click "
                     "\u201cOpen in AesApp Radio Updater\u201d again.",
    "token_expired": "This link has expired. Open cps.aes.app/tools/contact-lists and click "
                     "\u201cOpen in AesApp Radio Updater\u201d again.",
    "token_superseded": "A newer link replaced this one. Use the most recent link from "
                        "My Contact Lists on cps.aes.app.",
    "plan_required": "Your account's WebCPS plan does not include this. Upgrade on aes.app, then "
                     "open My Contact Lists again.",
}


class _Retryable(Exception):
    def __init__(self, reason: str, message: str, retry_after: Optional[float]):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.retry_after = retry_after


def _user_agent() -> str:
    # radio_fw.download.APP_VERSION is set by the app at startup ("dev" from source).
    return f"AesApp-Radio-Updater/{_dl.APP_VERSION}"


def cache_dir() -> str:
    """<app config dir>/contact_cache — sha256-addressed artifacts."""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/AesApp Radio Updater")
    else:
        base = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
                            "aesapp-radio-updater")
    d = os.path.join(base, "contact_cache")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


# ── HTTP ─────────────────────────────────────────────────────────────────────
def _request(method: str, url: str, token: str, timeout: float,
             on_progress: Optional[Callable[[int, int], None]] = None,
             on_status: Optional[Callable[[str], None]] = None,
             payload: Optional[dict] = None,
             retry: bool = True,
             pass_through: tuple = (401, 403, 410)) -> tuple[int, bytes]:
    """One HTTP exchange with the retry policy of radio_fw.download. Returns
    (status, body) for 2xx and for the auth statuses the caller interprets
    (401/403/410/409); raises ContactsError for everything else.

    `payload` is the JSON body of a POST (default: an empty object).
    `retry=False` is for calls whose whole point is timeliness — a heartbeat
    that spends 30 s backing off has already failed at its job."""
    attempts = _MAX_ATTEMPTS if retry else 1
    for attempt in range(1, attempts + 1):
        try:
            return _request_once(method, url, token, timeout, on_progress, payload, pass_through)
        except _Retryable as e:
            if attempt >= attempts:
                raise ContactsError(e.message + (" (still failing after several tries)." if retry else "."))
            wait = e.retry_after if e.retry_after is not None \
                else min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** (attempt - 1)))
            for remaining in range(max(1, int(round(wait))), 0, -1):
                if on_status:
                    on_status(f"{e.reason} — retrying in {remaining}s (attempt {attempt + 1}/{_MAX_ATTEMPTS})")
                time.sleep(1)
    raise ContactsError("The AesApp server could not be reached.")


def _request_once(method: str, url: str, token: str, timeout: float,
                  on_progress: Optional[Callable[[int, int], None]],
                  payload: Optional[dict] = None,
                  pass_through: tuple = (401, 403, 410)) -> tuple[int, bytes]:
    headers = {"User-Agent": _user_agent(), "Accept": "application/json, application/gzip, */*"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if method == "POST":
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload if payload is not None else {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_dl._ssl_context()) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            chunks, got = [], 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                got += len(chunk)
                if on_progress:
                    on_progress(got, total)
            return resp.status, b"".join(chunks)
    except urllib.error.HTTPError as e:
        # Statuses the CALLER interprets (a refused token, a lost lock, a job
        # that is gone): hand them back with their body instead of turning them
        # into a generic transport error.
        if e.code in pass_through:
            try:
                body = e.read()
            except Exception:  # noqa: BLE001
                body = b""
            return e.code, body
        if e.code in _RETRY_STATUSES:
            reason = ("Server busy (rate limited)" if e.code == 429 else f"Server error (HTTP {e.code})")
            raw = e.headers.get("Retry-After") if e.headers else None
            ra = min(_BACKOFF_CAP, float(raw.strip())) if raw and raw.strip().isdigit() else None
            raise _Retryable(reason, f"The AesApp server returned HTTP {e.code}.", ra)
        if e.code == 404:
            raise ContactsError("That contact bundle is no longer on the server (HTTP 404). "
                                "Reload the list and try again.")
        raise ContactsError(f"The server refused the request (HTTP {e.code}).")
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise ContactsError("The AesApp server took too long to respond. Check your connection and try again.")
        if isinstance(reason, ssl.SSLError):
            raise ContactsError("Could not establish a secure connection to the AesApp server (TLS error).")
        raise ContactsError("Couldn't reach the AesApp server. Check your internet connection, then try again.")
    except (socket.timeout, TimeoutError):
        raise ContactsError("The AesApp server took too long to respond. Check your connection and try again.")
    except OSError as e:
        raise ContactsError(f"Network error reaching the AesApp server: {e}")


def _auth_failure(status: int, body: bytes) -> SessionError:
    code, message = f"http_{status}", ""
    try:
        d = json.loads(body.decode("utf-8"))
        if isinstance(d, dict):
            code = str(d.get("error") or code)
            message = str(d.get("message") or "")
    except (ValueError, UnicodeDecodeError):
        pass
    friendly = _FRIENDLY.get(code)
    if not friendly:
        friendly = message or {401: "The server did not accept this link (not signed in).",
                               403: "The server refused this account for the contact refresher.",
                               410: "This link is no longer valid."}.get(status, f"HTTP {status}")
    return SessionError(code, friendly, status)


def _json(body: bytes, what: str) -> dict:
    try:
        d = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ContactsError(f"The server's {what} was not readable. Try again later.")
    if not isinstance(d, dict):
        raise ContactsError(f"The server returned an unexpected {what} format.")
    return d


# ── the three calls ──────────────────────────────────────────────────────────
def claim_session(base_url: str, token: str, timeout: float = 15.0,
                  on_status: Optional[Callable[[str], None]] = None) -> dict:
    """POST /api/contacts/session: claim (or re-confirm) the launch token.
    Returns the server's session dict ({ok, expiresAt, account, tier}).
    Raises SessionError for a refused token, ContactsError otherwise."""
    url = urljoin(base_url.rstrip("/") + "/", SESSION_PATH.lstrip("/"))
    status, body = _request("POST", url, token, timeout, on_status=on_status)
    if status in (401, 403, 410):
        raise _auth_failure(status, body)
    d = _json(body, "session reply")
    if not d.get("ok"):
        raise ContactsError("The server did not open a session for this link. Try My Contact Lists "
                            "on cps.aes.app again.")
    return d


def fetch_catalog(base_url: str, token: str, timeout: float = 20.0,
                  on_status: Optional[Callable[[str], None]] = None) -> dict:
    """GET /api/contacts/catalog → {schema, generatedAt, radios, lists, bundles}."""
    url = (urljoin(base_url.rstrip("/") + "/", CATALOG_PATH.lstrip("/"))
           + "?" + urlencode({"v": _dl.APP_VERSION}))
    status, body = _request("GET", url, token, timeout, on_status=on_status)
    if status in (401, 403, 410):
        raise _auth_failure(status, body)
    d = _json(body, "catalog")
    for key in ("radios", "lists", "bundles"):
        if key not in d:
            raise ContactsError("The server returned an unexpected catalog format.")
    if not isinstance(d["bundles"], list) or not isinstance(d["radios"], dict):
        raise ContactsError("The server returned an unexpected catalog format.")
    return d


def download_artifact(base_url: str, token: str, bundle: dict, timeout: float = 300.0,
                      on_progress: Optional[Callable[[int, int], None]] = None,
                      on_status: Optional[Callable[[str], None]] = None) -> bytes:
    """The bundle's gzipped CBSEG1 artifact, verified against its sha256 and
    served from the by-hash cache when a good copy is already on disk."""
    sha = str(bundle.get("sha256") or "").lower()
    art_url = bundle.get("artifactUrl")
    if not sha or not art_url or len(sha) != 64:
        raise ContactsError("The catalog entry for this list is incomplete — reload the list and try again.")
    cached = _cached_artifact(sha)
    if cached is not None:
        if on_status:
            on_status("using the copy downloaded earlier (checksum verified)")
        return cached
    url = urljoin(base_url.rstrip("/") + "/", str(art_url).lstrip("/"))
    status, body = _request("GET", url, token, timeout, on_progress, on_status)
    if status in (401, 403, 410):
        raise _auth_failure(status, body)
    actual = hashlib.sha256(body).hexdigest()
    if actual != sha:
        raise ContactsError("The downloaded contact list is corrupt (checksum mismatch) — it will NOT be "
                            "written to the radio. Try again.")
    _store_artifact(sha, body)
    return body


# ── catalog helpers (pure) ───────────────────────────────────────────────────
def radio_entry(catalog: dict, model: str) -> Optional[dict]:
    """The catalog's row for a radio's ID-frame model string ("D878UV2")."""
    radios = catalog.get("radios") or {}
    r = radios.get(model)
    return r if isinstance(r, dict) else None


def bundles_for(catalog: dict, radio: dict, kind: str = "dmr") -> list[dict]:
    """The bundles a radio can take: matching format (or nxFormat for
    kind="nxdn") and, for DMR, no more records than the radio holds."""
    fmt = radio.get("format") if kind == "dmr" else radio.get("nxFormat")
    if not fmt:
        return []
    cap = radio.get("capacity")
    out = []
    for b in catalog.get("bundles") or []:
        if not isinstance(b, dict) or b.get("format") != fmt or b.get("kind", "dmr") != kind:
            continue
        if kind == "dmr" and isinstance(cap, int) and cap > 0 and int(b.get("recordCount") or 0) > cap:
            continue
        out.append(b)
    return out


def bundle_label(b: dict) -> str:
    n = int(b.get("recordCount") or 0)
    built = str(b.get("builtAt") or b.get("sourceUpdatedAt") or "")[:10]
    label = str(b.get("listLabel") or b.get("list") or "?")
    return f"{label} — {n:,} contacts" + (f" (built {built})" if built else "")


def label_bundles(bundles: list[dict]) -> list[tuple[str, dict]]:
    """(label, bundle) pairs in the order the server sent them — the shape a
    Combobox wants, since its `values` are the labels and the operator's pick
    comes back as the label, not the row.

    Two rows can honestly produce the same label (a version of the operator's
    own named after the list it was cut from, two built the same day with the
    same count), so a repeat gets its bundle id appended: the pick must never
    resolve to a list the operator did not point at."""
    out: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for b in bundles:
        lab = bundle_label(b)
        if lab in seen:
            lab += f" [{b.get('id')}]"
        seen.add(lab)
        out.append((lab, b))
    return out


#: Seconds per 16-byte frame, by contact format.
#:
#: The 890 figure is MEASURED on hardware over this app's own wire path
#: (2026-09-10): 41,052 DMR contacts and 14,102 NXDN contacts written to an
#: AT-D890UV in one session, 429,036 frames in 123.2 s with no retries, which is
#: 3,481 frames a second or 55.7 kB/s. The old single figure of 0.55 ms a frame
#: was taken from the factory CPS running in a VM and is about twice too slow.
#:
#: The 878 family has NOT been measured over this path -- writing to one of the
#: radios on the bench would have replaced its contact database, which was not
#: what the test was for -- so it keeps the conservative old figure. Over Web
#: Serial the browser writer measures those radios at 1,625-2,060 frames a
#: second, slower than the 890 on the same cable, so erring long is right.
_SECONDS_PER_FRAME = {"anytone_890": 0.000300, "anytone_890_nx": 0.000300}
_SECONDS_PER_FRAME_DEFAULT = 0.00055


def estimate_seconds(blocks: int, fmt: Optional[str] = None) -> float:
    """How long `blocks` frames take, for the format if we know it."""
    return blocks * _SECONDS_PER_FRAME.get(fmt or "", _SECONDS_PER_FRAME_DEFAULT)


# ── sha-addressed cache ──────────────────────────────────────────────────────
def _cache_file(sha: str) -> str:
    return os.path.join(cache_dir(), sha + ".cbseg.gz")


def _cached_artifact(sha: str) -> Optional[bytes]:
    path = _cache_file(sha)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if hashlib.sha256(data).hexdigest() != sha:
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    return data


def _store_artifact(sha: str, data: bytes) -> None:
    path = _cache_file(sha)
    tmp = path + ".part"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
