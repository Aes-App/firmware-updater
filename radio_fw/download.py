"""Fetch prebuilt firmware bundles from the AesApp server instead of compiling
local vendor files.

The desktop tab can source a target two ways: the operator's own vendor files
(radio_fw.compiler) or the server's already-compiled, already-validated artifact
(here). Both feed the SAME wizard/engines — a downloaded component becomes a
compiler.CompileResult exactly like a locally compiled one, so nothing
downstream has to care where the bytes came from.

The safety that makes flashing server bytes acceptable is end-to-end sha256:
the catalog names each artifact's sha256, this module re-hashes what it actually
downloaded (and what it reads back from cache), and REFUSES on any mismatch. A
truncated download, a cache bitrot, or a wrong-URL swap can never reach the
radio — these writes are not verified on the wire, so the check has to be here.

Only the Python standard library plus certifi (for a CA bundle that exists
inside a frozen build); no third-party HTTP client.
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

from . import compiler

# The production catalog. Overridable for testing against a local server.
DEFAULT_BASE_URL = os.environ.get("AESAPP_FW_BASE_URL", "https://cps.aes.app")
_CATALOG_PATH = "/api/firmware/catalog"
_UA_NAME = "AesApp-Radio-Updater"

# The app's version, reported to the server (User-Agent + a ?v= on the catalog
# query) purely so it shows in the web log — the server does not act on it. The
# app (bt_ota.gui) sets this at startup; "dev" when run from source/tests.
APP_VERSION = "dev"


def _user_agent() -> str:
    return f"{_UA_NAME}/{APP_VERSION}"

# Transient statuses worth retrying with a backoff rather than failing outright:
# 429 = rate limited, 5xx = the server briefly unhappy. Everything else (404,
# TLS, connection refused) is reported immediately — retrying won't help.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 4          # 1 initial try + 3 retries
_BACKOFF_BASE = 3.0        # seconds -> 3, 6, 12 (doubling), capped
_BACKOFF_CAP = 30.0


class _Retryable(Exception):
    """Internal: a transient failure _get should back off and retry."""

    def __init__(self, reason: str, message: str, retry_after: Optional[float]):
        super().__init__(message)
        self.reason = reason            # short, for the countdown line
        self.message = message          # full, for the final give-up error
        self.retry_after = retry_after  # server-requested delay (429), or None


class DownloadError(Exception):
    """A fetch/verify failure. `str(self)` is operator-facing and complete."""


# ---------------------------------------------------------------------------
# TLS + cache location
# ---------------------------------------------------------------------------
def _ssl_context() -> ssl.SSLContext:
    """A verifying TLS context. A frozen PyInstaller app carries no system CA
    store Python can see, so trust certifi's bundle when it is importable and
    fall back to the platform default otherwise."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def cache_dir() -> str:
    """Where downloaded artifacts are cached, by sha256. Mirrors the app's
    config-dir convention but stays self-contained (radio_fw must not import the
    bt_ota app)."""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/AesApp Radio Updater")
    else:
        base = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
                            "aesapp-radio-updater")
    d = os.path.join(base, "fw_cache")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _get(url: str, timeout: float,
         on_progress: Optional[Callable[[int, int], None]] = None,
         on_status: Optional[Callable[[str], None]] = None) -> bytes:
    """GET a URL and return the body, retrying transient 429/5xx responses with a
    visible backoff countdown (on_status), and raising DownloadError with a
    plain, actionable message on a hard failure or once retries are exhausted."""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return _get_once(url, timeout, on_progress)
        except _Retryable as e:
            if attempt >= _MAX_ATTEMPTS:
                raise DownloadError(e.message + " (still failing after several tries).")
            wait = e.retry_after if e.retry_after is not None \
                else min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** (attempt - 1)))
            _wait_countdown(int(round(wait)), attempt + 1, e.reason, on_status)
    # unreachable: the loop either returns or raises
    raise DownloadError("The AesApp server could not be reached.")


def _get_once(url: str, timeout: float,
              on_progress: Optional[Callable[[int, int], None]]) -> bytes:
    """One GET attempt. Streams the body; raises _Retryable for a transient
    status and DownloadError for a permanent one."""
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            chunks = []
            got = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                got += len(chunk)
                if on_progress:
                    on_progress(got, total)
            return b"".join(chunks)
    except urllib.error.HTTPError as e:
        if e.code in _RETRY_STATUSES:
            reason = ("Server busy (rate limited)" if e.code == 429
                      else f"Server error (HTTP {e.code})")
            raise _Retryable(reason, f"The AesApp server returned HTTP {e.code}.",
                             _retry_after(e))
        if e.code == 404:
            raise DownloadError(
                "That firmware is no longer on the server (HTTP 404). Refresh the version list and try again.")
        raise DownloadError(f"The server refused the request (HTTP {e.code}).")
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise DownloadError("The AesApp server took too long to respond. Check your connection and try again.")
        if isinstance(reason, ssl.SSLError):
            raise DownloadError("Could not establish a secure connection to the AesApp server (TLS error).")
        raise DownloadError(
            "Couldn't reach the AesApp server. Check your internet connection, then try again.")
    except (socket.timeout, TimeoutError):
        raise DownloadError("The AesApp server took too long to respond. Check your connection and try again.")
    except OSError as e:
        raise DownloadError(f"Network error reaching the AesApp server: {e}")


def _retry_after(e: "urllib.error.HTTPError") -> Optional[float]:
    """The server's Retry-After delay in seconds, if it sent a plain-integer one
    (the HTTP-date form is ignored; the backoff schedule covers it)."""
    raw = e.headers.get("Retry-After") if e.headers else None
    if raw and raw.strip().isdigit():
        return min(_BACKOFF_CAP, float(raw.strip()))
    return None


def _wait_countdown(seconds: int, next_attempt: int, reason: str,
                    on_status: Optional[Callable[[str], None]]) -> None:
    """Sleep `seconds`, ticking a countdown through on_status once a second so
    the UI shows why it paused and for how long."""
    for remaining in range(max(1, seconds), 0, -1):
        if on_status:
            on_status(f"{reason} — retrying in {remaining}s (attempt {next_attempt}/{_MAX_ATTEMPTS})")
        time.sleep(1)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
def fetch_catalog(base_url: str = DEFAULT_BASE_URL, timeout: float = 15.0,
                  on_status: Optional[Callable[[str], None]] = None) -> list[dict]:
    """The prebuilt-bundle catalog: a list of bundle dicts (see the server's
    FirmwareCatalogApiController). Raises DownloadError on any failure so the UI
    can show one clear message and offer Retry."""
    # ?v=<app version> rides along for the server's access log only.
    url = urljoin(base_url + "/", _CATALOG_PATH.lstrip("/")) + "?" + urlencode({"v": APP_VERSION})
    raw = _get(url, timeout, on_status=on_status)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise DownloadError("The server's response was not valid catalog data. Try again later.")
    bundles = data.get("bundles") if isinstance(data, dict) else None
    if not isinstance(bundles, list):
        raise DownloadError("The server returned an unexpected catalog format.")
    return bundles


# ---------------------------------------------------------------------------
# One component -> a CompileResult, sha256-verified, cached by hash
# ---------------------------------------------------------------------------
def download_component(comp: dict, base_url: str = DEFAULT_BASE_URL,
                       timeout: float = 60.0,
                       on_progress: Optional[Callable[[int, int], None]] = None,
                       on_status: Optional[Callable[[str], None]] = None
                       ) -> compiler.CompileResult:
    """Turn one catalog component dict into a verified CompileResult.

    `comp` is an entry from a catalog bundle's "components": at least
    {kind, sha256, artifactUrl, manifestUrl}. The artifact is served from cache
    when a previously downloaded file still hashes to the expected sha256;
    otherwise it is fetched and cached. The manifest is fetched (small) and the
    result is assembled with the same shape compiler.compile_files returns.
    """
    kind = str(comp.get("kind") or "")
    sha = str(comp.get("sha256") or "").lower()
    art_url = comp.get("artifactUrl")
    man_url = comp.get("manifestUrl")
    if not kind or not sha or not art_url or not man_url:
        raise DownloadError("The catalog entry for this target is incomplete — refresh and try again.")

    artifact = _cached_artifact(sha)
    if artifact is None:
        artifact = _get(urljoin(base_url + "/", str(art_url).lstrip("/")), timeout, on_progress, on_status)
        actual = hashlib.sha256(artifact).hexdigest()
        if actual != sha:
            raise DownloadError(
                f"The downloaded {compiler.spec.label(kind)} is corrupt (checksum mismatch) — it will NOT be "
                f"flashed. Try fetching again.")
        _store_artifact(sha, artifact)

    raw_manifest = _get(urljoin(base_url + "/", str(man_url).lstrip("/")), timeout, on_status=on_status)
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise DownloadError("The server's manifest for this target was unreadable. Try again.")
    if not isinstance(manifest, dict):
        raise DownloadError("The server's manifest for this target was not in the expected format.")
    # The manifest states the artifact's own sha256; if it disagrees with what
    # we verified, the two halves came from different builds — refuse.
    man_sha = str(manifest.get("sha256") or "").lower()
    if man_sha and man_sha != sha:
        raise DownloadError("The server's artifact and manifest do not match (different builds). Try again.")

    label = str(comp.get("label") or compiler.spec.label(kind))
    return compiler.CompileResult(
        kind=kind, artifact=artifact, manifest=manifest,
        source_names=[f"{label} (from AesApp server)"])


# ---------------------------------------------------------------------------
# sha-addressed cache
# ---------------------------------------------------------------------------
def _cache_file(sha: str) -> str:
    # sha256 is hex; safe as a filename with no sanitising needed.
    return os.path.join(cache_dir(), sha + ".bin")


def _cached_artifact(sha: str) -> Optional[bytes]:
    """Return cached bytes for `sha` only if they still hash to it — a corrupt
    cache entry is ignored (and will be overwritten) rather than trusted."""
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
    """Cache an artifact by hash. Best-effort: a write failure (read-only home,
    full disk) must not fail the flash — the bytes are already in memory."""
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
