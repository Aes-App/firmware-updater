from __future__ import annotations

import os
import sys
import time
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tk = pytest.importorskip("tkinter")

from radio_contacts import gui_tab as gt


@pytest.fixture()
def tab():
    try:
        root = tk.Tk()
    except Exception as e:
        pytest.skip(f"no Tk display: {e}")
    root.withdraw()
    frame = tk.Frame(root)
    t = gt.ContactRefreshTab(frame, root)
    yield t
    try:
        root.destroy()
    except Exception:
        pass


def _pump(tab, times=3):
    for _ in range(times):
        tab._drain()


class _Ident:
    model = "D890UV"
    version = "V100"
    band = 0


def test_ident_ok_carries_ident_and_link_and_updates_the_label(tab):
    sentinel = object()
    tab._post("ident_ok", (_Ident(), sentinel))
    _pump(tab)

    assert tab.ident.model == "D890UV"
    assert tab._held is sentinel, "the held PC-mode session must be kept for the write"
    assert "Connected" in tab.ident_lbl.cget("text")
    assert "D890UV" in tab.ident_lbl.cget("text")
    assert "Connecting" not in tab.ident_lbl.cget("text")


def test_a_bad_message_does_not_stop_the_pump(tab):
    tab._post("ident_ok", "not-a-tuple")
    _pump(tab)

    tab._post("ident_ok", (_Ident(), None))
    _pump(tab)
    assert tab.ident.model == "D890UV"
    assert "D890UV" in tab.ident_lbl.cget("text")


def test_ident_err_clears_any_held_session(tab):
    tab._post("ident_ok", (_Ident(), object()))
    _pump(tab)
    assert tab._held is not None

    tab._post("ident_err", "no reply to PROGRAM")
    _pump(tab)
    assert tab.ident is None
    assert tab._held is None, "a failed connect must not leave a session to hand to a write"


def test_the_log_names_the_tab_before_the_radio_and_the_radio_after(tab):
    tab._log("before")
    assert f"[{gt._LOG_SOURCE}]" in tab.log.get("1.0", "end")

    tab._post("ident_ok", (_Ident(), None))
    _pump(tab)
    tab._log("after")
    text = tab.log.get("1.0", "end")
    assert "[D890UV] after" in text
    assert "—" not in text.split("\n")[0], "the em-dash placeholder is gone"


def test_disconnect_is_offered_only_while_a_session_is_held(tab):
    assert "disabled" in tab.disconnect_btn.state(), "nothing is held before Connect"

    tab._post("ident_ok", (_Ident(), object()))
    _pump(tab)
    assert "disabled" not in tab.disconnect_btn.state(), "a held session must be releasable"

    tab._held = None
    tab._refresh_write_state()
    assert "disabled" in tab.disconnect_btn.state(), "nothing left to release after a write"


def test_disconnect_hands_the_session_to_the_worker_and_resets_the_tab(tab, monkeypatch):
    closed = []
    monkeypatch.setattr(gt.engine, "close_session",
                        lambda link, log: closed.append(link))

    sentinel = object()
    tab._post("ident_ok", (_Ident(), sentinel))
    _pump(tab)
    assert tab._held is sentinel

    tab._on_disconnect()
    assert tab._held is None, "the tab must not keep a reference the worker now owns"
    for _ in range(50):
        if closed:
            break
        time.sleep(0.02)
    assert closed == [sentinel], f"close_session got {closed!r}"

    _pump(tab)
    assert tab.ident is None
    assert tab._held is None
    assert "Not connected" in tab.ident_lbl.cget("text")


def test_disconnect_is_a_no_op_with_nothing_held(tab, monkeypatch):
    called = []
    monkeypatch.setattr(gt.engine, "close_session", lambda link, log: called.append(link))
    tab._on_disconnect()
    assert called == []
    assert "Not connected" in tab.ident_lbl.cget("text")
