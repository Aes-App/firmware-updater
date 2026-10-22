from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tk = pytest.importorskip("tkinter")
pytest.importorskip("serial")

from radio_codeplug import gui_tab as gt


@pytest.fixture()
def tab(monkeypatch):
    try:
        root = tk.Tk()
    except Exception as e:
        pytest.skip(f"no Tk display: {e}")
    root.withdraw()
    t = gt.CodeplugWriteTab(tk.Frame(root), root)
    t.clock = [0.0]
    monkeypatch.setattr(gt.time, "monotonic", lambda: t.clock[0])
    yield t
    try:
        root.destroy()
    except Exception:
        pass


def _report(tab, at, done, total, phase):
    tab.clock[0] = at
    tab._handle("progress", (done, total, phase))
    return str(tab.wstatus.cget("text"))


def test_each_phase_is_timed_from_its_own_start(tab):
    tab._phase_start = None
    assert "left" not in _report(tab, 1.0, 512, 23645, "codeplug"), "no pace yet"
    assert "about 8 s left" in _report(tab, 1.17, 1024, 23645, "codeplug")
    first = _report(tab, 41.0, 512, 1533550, "contacts")
    assert "left" not in first and "digital contact list" in first
    text = _report(tab, 41.153, 1024, 1533550, "contacts")
    assert "about 8 min left" in text, text


def test_a_new_write_starts_its_own_timing(tab):
    tab._phase_start = None
    _report(tab, 0.0, 512, 1533550, "contacts")
    _report(tab, 0.153, 1024, 1533550, "contacts")
    tab._phase_start = None
    assert "left" not in _report(tab, 900.0, 512, 23645, "codeplug")
