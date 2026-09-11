"""The ``aesapp://`` launch link, and the two things that make it work on a
desktop: registering the scheme (Windows, no installer) and making sure a
second launch hands its link to the instance that is already running.

    aesapp://contacts?token=<t>&server=<https base>
    aesapp://codeplug?token=<t>&server=<https base>

``server`` is honoured only for https://*.aes.app or http://localhost /
127.0.0.1 (a link cannot point the app at a stranger's server); anything else
falls back to the default base URL.
"""
from __future__ import annotations

import os
import re
import secrets
import socket
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

SCHEME = "aesapp"
ACTION_CONTACTS = "contacts"
ACTION_CODEPLUG = "codeplug"
ACTIONS = (ACTION_CONTACTS, ACTION_CODEPLUG)

_SERVER_ALLOW = re.compile(
    r"^(?:https://(?:[a-z0-9-]+\.)*aes\.app(?::\d{1,5})?|http://(?:localhost|127\.0\.0\.1)(?::\d{1,5})?)$",
    re.IGNORECASE)


class LaunchError(Exception):
    """A link this app cannot act on. Operator-facing message."""


@dataclass(frozen=True)
class LaunchRequest:
    action: str
    token: str
    server: Optional[str]      # as given (None when absent)

    def base_url(self, default: str) -> str:
        return resolve_base_url(self.server, default)


def looks_like_launch_url(s: Optional[str]) -> bool:
    return bool(s) and s.strip().lower().startswith(SCHEME + ":")


def parse_launch_url(url: str) -> LaunchRequest:
    """Parse and validate a launch link. Raises LaunchError on anything that is
    not an aesapp:// link with an action this build knows and a token."""
    if not isinstance(url, str) or not url.strip():
        raise LaunchError("empty link")
    u = urlsplit(url.strip())
    if u.scheme.lower() != SCHEME:
        raise LaunchError(f"not an {SCHEME}:// link")
    # aesapp://<action>?token=… — the "host" part is the action.
    action = (u.netloc or u.path.strip("/").split("/")[0]).lower()
    if action not in ACTIONS:
        raise LaunchError(f'this version of the app does not handle "{action}" links — please update it')
    q = parse_qs(u.query, keep_blank_values=False)
    token = (q.get("token") or [""])[0].strip()
    if not token or not re.fullmatch(r"[A-Za-z0-9_\-]{16,512}", token):
        raise LaunchError("the link carries no usable token — open My Contact Lists on cps.aes.app "
                          "again and click the button")
    server = (q.get("server") or [None])[0]
    if server is not None:
        server = server.strip().rstrip("/") or None
    return LaunchRequest(action=action, token=token, server=server)


def resolve_base_url(server: Optional[str], default: str) -> str:
    """The server base to talk to: the link's, if it is one of ours, else the
    default. Never trusts an arbitrary host from a link."""
    if server and _SERVER_ALLOW.match(server.strip().rstrip("/")):
        return server.strip().rstrip("/")
    return default.rstrip("/")


# ── Windows: register the scheme under HKCU (no installer, no admin) ─────────
def register_windows_scheme(exe_path: Optional[str] = None, winreg_module=None) -> bool:
    """Point HKCU\\Software\\Classes\\aesapp at this executable. Idempotent:
    rewrites only when the stored command differs (a moved .exe). Returns True
    when the registry now names this exe, False when not on Windows / not
    frozen / the write failed (never raises — the app must still start)."""
    if winreg_module is None:
        if sys.platform != "win32":
            return False
        try:
            import winreg as winreg_module  # type: ignore[no-redef]
        except ImportError:
            return False
    exe = exe_path or (sys.executable if getattr(sys, "frozen", False) else None)
    if not exe:
        return False
    command = f'"{exe}" "%1"'
    wr = winreg_module
    try:
        key_path = r"Software\Classes\aesapp"
        try:
            with wr.OpenKey(wr.HKEY_CURRENT_USER, key_path + r"\shell\open\command") as k:
                current, _ = wr.QueryValueEx(k, "")
                if current == command:
                    return True
        except OSError:
            pass
        with wr.CreateKey(wr.HKEY_CURRENT_USER, key_path) as k:
            wr.SetValueEx(k, "", 0, wr.REG_SZ, "URL:AesApp Radio Updater")
            wr.SetValueEx(k, "URL Protocol", 0, wr.REG_SZ, "")
        with wr.CreateKey(wr.HKEY_CURRENT_USER, key_path + r"\DefaultIcon") as k:
            wr.SetValueEx(k, "", 0, wr.REG_SZ, f'"{exe}",0')
        with wr.CreateKey(wr.HKEY_CURRENT_USER, key_path + r"\shell\open\command") as k:
            wr.SetValueEx(k, "", 0, wr.REG_SZ, command)
        return True
    except OSError:
        return False


# ── single instance: hand a link to the running app ──────────────────────────
class SingleInstance:
    """A per-user lock file naming a localhost port + secret. The first app to
    start listens; a later launch with a link forwards it and exits. Localhost
    only, and the secret (readable by this user alone) keeps another local
    process from feeding the app links.

    Usage:
        inst = SingleInstance(config_dir)
        if url and inst.forward(url): sys.exit(0)        # someone else has it
        inst.listen(handler)                             # we are the app
    """

    LOCK_NAME = "instance.lock"

    def __init__(self, config_dir: str):
        self.lock_path = os.path.join(config_dir, self.LOCK_NAME)
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._secret = ""

    # -- secondary side
    def forward(self, url: str, timeout: float = 1.5) -> bool:
        info = self._read_lock()
        if info is None:
            return False
        port, secret = info
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
                s.sendall((secret + " " + url + "\n").encode("utf-8"))
                s.settimeout(timeout)
                reply = s.recv(16)
            return reply.startswith(b"OK")
        except OSError:
            return False

    # -- primary side
    def listen(self, handler: Callable[[str], None]) -> bool:
        """Start accepting forwarded links. `handler` runs on the listener
        thread — marshal to the UI thread inside it."""
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.bind(("127.0.0.1", 0))
            srv.listen(4)
        except OSError:
            return False
        self._secret = secrets.token_urlsafe(24)
        try:
            os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
            with open(self.lock_path, "w", encoding="utf-8") as f:
                f.write(f"{srv.getsockname()[1]} {self._secret}\n")
            try:
                os.chmod(self.lock_path, 0o600)
            except OSError:
                pass
        except OSError:
            srv.close()
            return False
        self._sock = srv
        self._thread = threading.Thread(target=self._serve, args=(srv, handler), daemon=True)
        self._thread.start()
        return True

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        try:
            os.remove(self.lock_path)
        except OSError:
            pass

    @property
    def port(self) -> Optional[int]:
        return self._sock.getsockname()[1] if self._sock is not None else None

    def _serve(self, srv: socket.socket, handler: Callable[[str], None]) -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(2.0)
                    buf = b""
                    while b"\n" not in buf and len(buf) < 4096:
                        chunk = conn.recv(1024)
                        if not chunk:
                            break
                        buf += chunk
                    line = buf.split(b"\n", 1)[0].decode("utf-8", "replace")
                    secret, _, url = line.partition(" ")
                    if secret == self._secret and looks_like_launch_url(url):
                        handler(url.strip())
                        conn.sendall(b"OK\n")
                    else:
                        conn.sendall(b"NO\n")
                except OSError:
                    pass

    def _read_lock(self) -> Optional[tuple[int, str]]:
        try:
            with open(self.lock_path, encoding="utf-8") as f:
                port_s, _, secret = f.read().strip().partition(" ")
            port = int(port_s)
            if 0 < port < 65536 and secret:
                return port, secret
        except (OSError, ValueError):
            pass
        return None
