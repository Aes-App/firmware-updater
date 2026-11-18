from __future__ import annotations

import os
import sys
import threading
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tk = pytest.importorskip("tkinter")
pytest.importorskip("serial")

from radio_codeplug import gui_tab as gt

LINK = "aesapp://codeplug?token=" + "A" * 43 + "&server=https://cps.aes.app"
STOPPED = ("The codeplug was committed; then stopped by the operator during the digital contact "
           "list, after 73,230 of 1,559,022 blocks.")


@pytest.fixture()
def tab(monkeypatch):
    try:
        root = tk.Tk()
    except Exception as e:
        pytest.skip(f"no Tk display: {e}")
    root.withdraw()
    t = gt.CodeplugWriteTab(tk.Frame(root), root)
    t.dialogs = []
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(gt.messagebox, name, lambda *a, _n=name, **k: t.dialogs.append(_n))
    monkeypatch.setattr(gt.messagebox, "askyesno", lambda *a, **k: True)
    t.job = types.SimpleNamespace(lease_seconds=600, project_name="Test", model="anytone_d890",
                                  job_id="j", ident_tokens=[])
    yield t
    try:
        root.destroy()
    except Exception:
        pass


def _writing(tab, at=37.0):
    tab._writing = True
    tab._abort = threading.Event()
    tab.progress["value"] = at


def _bar(tab) -> float:
    return float(tab.progress["value"])


def test_a_stop_empties_the_bar_and_asks_nothing_more(tab):
    _writing(tab)
    tab._on_abort()
    assert tab._abort.is_set()
    tab._handle("write_done", (types.SimpleNamespace(outcome="verify_unavailable", message=STOPPED), True))
    assert _bar(tab) == 0.0
    assert tab.dialogs == [], "the operator confirmed the stop already; no dialog on top"
    assert str(tab.wstatus.cget("text")) == STOPPED, "the line still says what the radio holds"
    assert "editable again" in str(tab.session_status.cget("text"))
    assert not tab._writing and tab.plan is None


def test_a_lease_the_server_took_back_still_gets_its_dialog(tab):
    _writing(tab)
    tab._abort.set()
    tab._handle("write_done", (types.SimpleNamespace(outcome="write_failed", message="lock lost"), True))
    assert tab.dialogs == ["showwarning"]
    assert _bar(tab) == 37.0, "a write that was not stopped keeps the bar where it ended"


def test_the_next_link_starts_from_an_empty_bar(tab):
    _writing(tab, 64.0)
    tab._handle("write_done", (types.SimpleNamespace(outcome="write_failed", message="failed at 64%"), True))
    assert _bar(tab) == 64.0
    tab.handle_launch_url(LINK)
    assert _bar(tab) == 0.0, "a new link starts from an empty bar"
    assert str(tab.wstatus.cget("text")) == ""
    assert tab._busy, "and goes on to claim the link"


def test_an_unconfirmed_report_does_not_say_the_project_is_free(tab):
    _writing(tab)
    tab._handle("write_done", (types.SimpleNamespace(outcome="success", message="written"), False))
    text = str(tab.session_status.cget("text"))
    assert "did not confirm" in text and "10 minutes" in text, text
    assert "editable again" not in text
    log = tab.log.get("1.0", "end")
    assert "the server did not confirm how this write ended" in log


def test_the_worker_hands_over_whether_the_report_arrived(tab, monkeypatch):
    res = types.SimpleNamespace(outcome="verify_unavailable", message="stopped",
                                detail=lambda: {"client": "desktop"})
    monkeypatch.setattr(gt.engine, "write_codeplug", lambda *a, **k: res)
    sess = types.SimpleNamespace(report=lambda *a, **k: False, heartbeat=lambda *a, **k: True)
    plan = types.SimpleNamespace(codeplug=[], contacts=[])
    tab._write_worker("COM9", sess, tab.job, plan, threading.Event())
    posted = []
    while not tab._q.empty():
        posted.append(tab._q.get_nowait())
    assert ("write_done", (res, False)) in posted, posted


@pytest.fixture(autouse=True)
def _no_claim(monkeypatch):
    monkeypatch.setattr(gt.CodeplugWriteTab, "_session_worker", lambda self, sess: None)
