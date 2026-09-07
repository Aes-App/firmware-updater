"""The Digital Contact tab's UI message pump, driven through a real Tk widget.

Why this file exists: the tab talks to its worker threads through a queue, and
_drain -> _handle destructures each message by shape. Nothing checked that the
shape a worker POSTS still matches the shape the handler DESTRUCTURES, and when
those two drifted the failure was not a wrong label -- the exception escaped
_drain, the trailing root.after() never re-armed, and the whole pump stopped.
The log froze mid-handshake and the tab sat on "Connecting..." for ever, with
the traceback going only to a stderr a windowed app never shows.

These tests need a Tk display; they skip where there is none (headless CI).

Run:  python -m pytest tests/test_contacts_tab_pump.py -q
"""
from __future__ import annotations

import os
import sys
import time
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tk = pytest.importorskip("tkinter")

from radio_contacts import gui_tab as gt  # noqa: E402


@pytest.fixture()
def tab():
    try:
        root = tk.Tk()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no Tk display: {e}")
    root.withdraw()
    frame = tk.Frame(root)
    t = gt.ContactRefreshTab(frame, root)
    yield t
    try:
        root.destroy()
    except Exception:  # noqa: BLE001
        pass


def _pump(tab, times=3):
    """Run the drain callback the way root.after would, without a mainloop."""
    for _ in range(times):
        tab._drain()


class _Ident:
    """What engine.open_session() returns alongside the link."""
    model = "D890UV"
    version = "V100"
    band = 0


def test_ident_ok_carries_ident_and_link_and_updates_the_label(tab):
    """The worker posts (ident, link); the handler must read the IDENT for the
    label, not the tuple. Reading the tuple raised AttributeError and killed the
    pump -- the bug this test exists for."""
    sentinel = object()
    tab._post("ident_ok", (_Ident(), sentinel))
    _pump(tab)

    assert tab.ident.model == "D890UV"
    assert tab._held is sentinel, "the held PC-mode session must be kept for the write"
    assert "Connected" in tab.ident_lbl.cget("text")
    assert "D890UV" in tab.ident_lbl.cget("text")
    assert "Connecting" not in tab.ident_lbl.cget("text")


def test_a_bad_message_does_not_stop_the_pump(tab):
    """A handler blowing up must cost one message, not the tab. Before the fix
    the exception escaped _drain and the re-arm never ran."""
    tab._post("ident_ok", "not-a-tuple")     # wrong shape on purpose
    _pump(tab)

    # the pump survived: a later, well-formed message is still processed
    tab._post("ident_ok", (_Ident(), None))
    _pump(tab)
    assert tab.ident.model == "D890UV"
    assert "D890UV" in tab.ident_lbl.cget("text")


def test_ident_err_clears_any_held_session(tab):
    """A failed connect must not leave a stale link behind for the write to use."""
    tab._post("ident_ok", (_Ident(), object()))
    _pump(tab)
    assert tab._held is not None

    tab._post("ident_err", "no reply to PROGRAM")
    _pump(tab)
    assert tab.ident is None
    assert tab._held is None, "a failed connect must not leave a session to hand to a write"


def test_the_log_names_the_tab_before_the_radio_and_the_radio_after(tab):
    """The bracket is the source of the line: this tab until the radio says what
    it is, the radio afterwards."""
    tab._log("before")
    assert f"[{gt._LOG_SOURCE}]" in tab.log.get("1.0", "end")

    tab._post("ident_ok", (_Ident(), None))
    _pump(tab)
    tab._log("after")
    text = tab.log.get("1.0", "end")
    assert "[D890UV] after" in text
    assert "—" not in text.split("\n")[0], "the em-dash placeholder is gone"


def test_disconnect_is_offered_only_while_a_session_is_held(tab):
    """Connect holds the radio in PC mode so the write can reuse it. The button
    that gives it back must be live exactly then: not before Connect, and not
    after a write, whose own END already released it."""
    assert "disabled" in tab.disconnect_btn.state(), "nothing is held before Connect"

    tab._post("ident_ok", (_Ident(), object()))
    _pump(tab)
    assert "disabled" not in tab.disconnect_btn.state(), "a held session must be releasable"

    # a write takes ownership of the link and ends it itself
    tab._held = None
    tab._refresh_write_state()
    assert "disabled" in tab.disconnect_btn.state(), "nothing left to release after a write"


def test_disconnect_hands_the_session_to_the_worker_and_resets_the_tab(tab, monkeypatch):
    """_on_disconnect must give the link up ONCE -- to the worker that ENDs it --
    and the tab must forget the radio when it reports back. A link ended twice,
    or kept after being ended, is a stale handle a later write would adopt."""
    closed = []
    monkeypatch.setattr(gt.engine, "close_session",
                        lambda link, log: closed.append(link))

    sentinel = object()
    tab._post("ident_ok", (_Ident(), sentinel))
    _pump(tab)
    assert tab._held is sentinel

    tab._on_disconnect()
    assert tab._held is None, "the tab must not keep a reference the worker now owns"
    for _ in range(50):                      # the worker is a real thread
        if closed:
            break
        time.sleep(0.02)
    assert closed == [sentinel], f"close_session got {closed!r}"

    _pump(tab)                               # drain the worker's "disconnected"
    assert tab.ident is None
    assert tab._held is None
    assert "Not connected" in tab.ident_lbl.cget("text")


def test_disconnect_is_a_no_op_with_nothing_held(tab, monkeypatch):
    """Clicking it when there is no session must not spawn a worker or blank the
    label -- the button is disabled then, but a stray keyboard activation is
    cheap to make harmless."""
    called = []
    monkeypatch.setattr(gt.engine, "close_session", lambda link, log: called.append(link))
    tab._on_disconnect()
    assert called == []
    assert "Not connected" in tab.ident_lbl.cget("text")

