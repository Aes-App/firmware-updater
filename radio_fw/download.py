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

DEFAULT_BASE_URL = os.environ.get("AESAPP_FW_BASE_URL", "https://cps.aes.app")
_CATALOG_PATH = "/api/firmware/catalog"
_UA_NAME = "AesApp-Radio-Updater"

APP_VERSION = "dev"


def _user_agent() -> str:
    return f"{_UA_NAME}/{APP_VERSION}"

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 4
_BACKOFF_BASE = 3.0
_BACKOFF_CAP = 30.0


class _Retryable(Exception):

    def __init__(self, reason: str, message: str, retry_after: Optional[float]):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.retry_after = retry_after


class DownloadError(Exception):
    pass


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def cache_dir() -> str:
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


def _get(url: str, timeout: float,
         on_progress: Optional[Callable[[int, int], None]] = None,
         on_status: Optional[Callable[[str], None]] = None) -> bytes:
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return _get_once(url, timeout, on_progress)
        except _Retryable as e:
            if attempt >= _MAX_ATTEMPTS:
                raise DownloadError(e.message + " (still failing after several tries).")
            wait = e.retry_after if e.retry_after is not None \
                else min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** (attempt - 1)))
            _wait_countdown(int(round(wait)), attempt + 1, e.reason, on_status)
    raise DownloadError("The AesApp server could not be reached.")


def _get_once(url: str, timeout: float,
              on_progress: Optional[Callable[[int, int], None]]) -> bytes:
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
    raw = e.headers.get("Retry-After") if e.headers else None
    if raw and raw.strip().isdigit():
        return min(_BACKOFF_CAP, float(raw.strip()))
    return None


def _wait_countdown(seconds: int, next_attempt: int, reason: str,
                    on_status: Optional[Callable[[str], None]]) -> None:
    for remaining in range(max(1, seconds), 0, -1):
        if on_status:
            on_status(f"{reason} — retrying in {remaining}s (attempt {next_attempt}/{_MAX_ATTEMPTS})")
        time.sleep(1)


def fetch_catalog(base_url: str = DEFAULT_BASE_URL, timeout: float = 15.0,
                  on_status: Optional[Callable[[str], None]] = None) -> list[dict]:
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


def download_component(comp: dict, base_url: str = DEFAULT_BASE_URL,
                       timeout: float = 60.0,
                       on_progress: Optional[Callable[[int, int], None]] = None,
                       on_status: Optional[Callable[[str], None]] = None
                       ) -> compiler.CompileResult:
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
    man_sha = str(manifest.get("sha256") or "").lower()
    if man_sha and man_sha != sha:
        raise DownloadError("The server's artifact and manifest do not match (different builds). Try again.")

    label = str(comp.get("label") or compiler.spec.label(kind))
    return compiler.CompileResult(
        kind=kind, artifact=artifact, manifest=manifest,
        source_names=[f"{label} (from AesApp server)"])


def _cache_file(sha: str) -> str:
    return os.path.join(cache_dir(), sha + ".bin")


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
