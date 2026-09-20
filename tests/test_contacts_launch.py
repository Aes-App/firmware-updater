from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radio_contacts import launch

TOKEN = "AbCdEf0123456789_-xyz"


def test_parse_contacts_link():
    r = launch.parse_launch_url(f"aesapp://contacts?token={TOKEN}&server=https://cps.aes.app/")
    assert r.action == "contacts" and r.token == TOKEN and r.server == "https://cps.aes.app"
    assert launch.parse_launch_url(f"AESAPP://contacts?token={TOKEN}").server is None
    assert launch.looks_like_launch_url(" aesapp://contacts?token=x")
    assert not launch.looks_like_launch_url("https://cps.aes.app/tools")


@pytest.mark.parametrize("url, hint", [
    ("https://cps.aes.app/tools", "not an aesapp"),
    ("aesapp://firmware?token=" + TOKEN, "does not handle"),
    ("aesapp://contacts", "no usable token"),
    ("aesapp://contacts?token=short", "no usable token"),
    ("aesapp://contacts?token=has%20space%20chars", "no usable token"),
    ("", "empty"),
])
def test_bad_links_are_refused(url, hint):
    with pytest.raises(launch.LaunchError, match=hint):
        launch.parse_launch_url(url)


@pytest.mark.parametrize("server, expect", [
    ("https://cps.aes.app", "https://cps.aes.app"),
    ("https://cps.aes.app/", "https://cps.aes.app"),
    ("https://dev-cps.aes.app:8443", "https://dev-cps.aes.app:8443"),
    ("https://aes.app", "https://aes.app"),
    ("http://localhost:8000", "http://localhost:8000"),
    ("http://127.0.0.1:8000/", "http://127.0.0.1:8000"),
    ("https://evil.example.com", "https://cps.aes.app"),
    ("https://aes.app.evil.example.com", "https://cps.aes.app"),
    ("http://cps.aes.app", "https://cps.aes.app"),
    ("https://cps.aes.app.evil", "https://cps.aes.app"),
    ("ftp://cps.aes.app", "https://cps.aes.app"),
    (None, "https://cps.aes.app"),
])
def test_server_allowlist(server, expect):
    assert launch.resolve_base_url(server, "https://cps.aes.app") == expect
    r = launch.parse_launch_url(f"aesapp://contacts?token={TOKEN}" + (f"&server={server}" if server else ""))
    assert r.base_url("https://cps.aes.app") == expect


class _Key:
    def __init__(self, reg, path):
        self.reg, self.path = reg, path

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1

    def __init__(self):
        self.values = {}
        self.created = []

    def OpenKey(self, hkey, path):
        if not any(p == path for p, _ in self.values):
            raise OSError("missing")
        return _Key(self, path)

    def CreateKey(self, hkey, path):
        self.created.append(path)
        return _Key(self, path)

    def SetValueEx(self, key, name, _res, _type, value):
        self.values[(key.path, name)] = value

    def QueryValueEx(self, key, name):
        return self.values[(key.path, name)], self.REG_SZ


def test_windows_scheme_registration_is_idempotent():
    wr = FakeWinreg()
    assert launch.register_windows_scheme(r"C:\Apps\AesApp Radio Updater.exe", winreg_module=wr) is True
    cmd = wr.values[(r"Software\Classes\aesapp\shell\open\command", "")]
    assert cmd == '"C:\\Apps\\AesApp Radio Updater.exe" "%1"'
    assert wr.values[(r"Software\Classes\aesapp", "URL Protocol")] == ""
    assert wr.values[(r"Software\Classes\aesapp", "")].startswith("URL:")
    n = len(wr.created)
    assert launch.register_windows_scheme(r"C:\Apps\AesApp Radio Updater.exe", winreg_module=wr) is True
    assert len(wr.created) == n
    assert launch.register_windows_scheme(r"D:\New\AesApp Radio Updater.exe", winreg_module=wr) is True
    assert wr.values[(r"Software\Classes\aesapp\shell\open\command", "")].startswith('"D:\\New\\')


def test_windows_registration_never_raises():
    class Broken(FakeWinreg):
        def CreateKey(self, *a):
            raise OSError("denied")
    assert launch.register_windows_scheme(r"C:\x.exe", winreg_module=Broken()) is False
    assert launch.register_windows_scheme(None, winreg_module=FakeWinreg()) is False


def test_second_launch_forwards_its_link_to_the_running_instance(tmp_path):
    primary = launch.SingleInstance(str(tmp_path))
    got = []
    ev = threading.Event()
    assert primary.forward("aesapp://contacts?token=" + TOKEN) is False, "nobody listening yet"
    assert primary.listen(lambda u: (got.append(u), ev.set()))
    assert os.path.exists(primary.lock_path)
    secondary = launch.SingleInstance(str(tmp_path))
    assert secondary.forward("aesapp://contacts?token=" + TOKEN) is True
    assert ev.wait(3) and got == ["aesapp://contacts?token=" + TOKEN]
    import socket
    port, secret = primary._read_lock()
    with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
        s.sendall((secret + " https://not-a-link\n").encode())
        assert s.recv(8).startswith(b"NO")
    with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
        s.sendall(("wrongsecret aesapp://contacts?token=" + TOKEN + "\n").encode())
        assert s.recv(8).startswith(b"NO")
    assert got == ["aesapp://contacts?token=" + TOKEN]
    primary.close()
    assert not os.path.exists(primary.lock_path)
    assert secondary.forward("aesapp://contacts?token=" + TOKEN) is False


def test_stale_lock_file_does_not_block_a_launch(tmp_path):
    inst = launch.SingleInstance(str(tmp_path))
    with open(inst.lock_path, "w") as f:
        f.write("1 stalesecret\n")
    assert inst.forward("aesapp://contacts?token=" + TOKEN) is False
