from __future__ import annotations

import os
import re
import secrets
import shutil
import socket
import subprocess
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
    pass


@dataclass(frozen=True)
class LaunchRequest:
    action: str
    token: str
    server: Optional[str]

    def base_url(self, default: str) -> str:
        return resolve_base_url(self.server, default)


def looks_like_launch_url(s: Optional[str]) -> bool:
    return bool(s) and s.strip().lower().startswith(SCHEME + ":")


def parse_launch_url(url: str) -> LaunchRequest:
    if not isinstance(url, str) or not url.strip():
        raise LaunchError("empty link")
    u = urlsplit(url.strip())
    if u.scheme.lower() != SCHEME:
        raise LaunchError(f"not an {SCHEME}:// link")
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
    if server and _SERVER_ALLOW.match(server.strip().rstrip("/")):
        return server.strip().rstrip("/")
    return default.rstrip("/")


def register_windows_scheme(exe_path: Optional[str] = None, winreg_module=None) -> bool:
    if winreg_module is None:
        if sys.platform != "win32":
            return False
        try:
            import winreg as winreg_module
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


DESKTOP_ID = "aesapp-radio-updater.desktop"


def _desktop_exec_arg(path: str) -> str:
    quoted = "".join("\\" + c if c in '\\"`$' else c for c in path).replace("%", "%%")
    return '"' + quoted.replace("\\", "\\\\") + '"'


def _run_quietly(argv) -> None:
    try:
        subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def register_linux_scheme(exe_path: Optional[str] = None, icon_src: Optional[str] = None,
                          data_home: Optional[str] = None, run=_run_quietly) -> bool:
    exe = exe_path or (sys.executable if getattr(sys, "frozen", False) else None)
    if not exe:
        return False
    try:
        base = data_home or os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        apps = os.path.join(base, "applications")
        entry_path = os.path.join(apps, DESKTOP_ID)
        lines = [
            "[Desktop Entry]",
            "Type=Application",
            "Name=AesApp Radio Updater",
            "Comment=Firmware, Bluetooth module, contacts and codeplugs for AnyTone radios",
            "Exec=" + _desktop_exec_arg(exe) + " %u",
            "Terminal=false",
            "Categories=Network;HamRadio;",
            "MimeType=x-scheme-handler/" + SCHEME + ";",
        ]
        if icon_src and os.path.isfile(icon_src):
            icon = os.path.join(base, "aesapp-radio-updater", "icon.png")
            if not os.path.isfile(icon) or os.path.getsize(icon) != os.path.getsize(icon_src):
                os.makedirs(os.path.dirname(icon), exist_ok=True)
                shutil.copyfile(icon_src, icon)
            lines.insert(5, "Icon=" + icon)
        text = "\n".join(lines) + "\n"
        try:
            with open(entry_path, encoding="utf-8") as f:
                if f.read() == text:
                    return True
        except OSError:
            pass
        os.makedirs(apps, exist_ok=True)
        tmp = entry_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, entry_path)
    except OSError:
        return False
    run(["xdg-mime", "default", DESKTOP_ID, "x-scheme-handler/" + SCHEME])
    run(["update-desktop-database", apps])
    return True


class SingleInstance:

    LOCK_NAME = "instance.lock"

    def __init__(self, config_dir: str):
        self.lock_path = os.path.join(config_dir, self.LOCK_NAME)
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._secret = ""

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

    def listen(self, handler: Callable[[str], None]) -> bool:
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
