from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tk = pytest.importorskip("tkinter")
pytest.importorskip("serial")

from radio_codeplug import gui_tab as codeplug_tab
from radio_contacts import gui_tab as contacts_tab
from radio_fw import gui_tab as fw_tab
from radio_fw import spec

RED, GREEN = "#b00020", "#127a2e"


@pytest.fixture()
def root():
    try:
        r = tk.Tk()
    except Exception as e:
        pytest.skip(f"no Tk display: {e}")
    r.withdraw()
    yield r
    try:
        r.destroy()
    except Exception:
        pass


def _coloured(log, tag):
    ranges = log.tag_ranges(tag)
    return [log.get(ranges[i], ranges[i + 1]) for i in range(0, len(ranges), 2)]


def test_every_log_has_three_colours_only():
    for mod in (fw_tab, contacts_tab, codeplug_tab):
        assert mod._LOG_TAGS == {"er": RED, "error": RED, "done": GREEN}, mod.__name__


def test_a_firmware_failure_is_red_in_the_log_and_a_written_stage_green(root):
    tab = fw_tab.RadioBoardsTab(tk.Frame(root), root)
    tab.plan = [types.SimpleNamespace(kind=spec.KIND_APRS), types.SimpleNamespace(kind=spec.KIND_FW)]
    tab.results = [{"kind": p.kind, "label": "", "state": "pending"} for p in tab.plan]
    tab.model, tab.step = "d878uv2", 0
    tab._log('TX "UPDATE" (555044415445)', "tx")
    tab._on_stage_error("timeout after 3000 ms: wanted 1 byte(s), have 0", False)
    red = _coloured(tab.log, "er")
    assert len(red) == 1 and "FAILED: timeout after 3000 ms" in red[0], red
    assert "TX" not in red[0], "a TX line is black"
    assert not _coloured(tab.log, "done")
    tab.step = 1
    tab._on_stage_done()
    green = _coloured(tab.log, "done")
    assert len(green) == 1 and "Radio Firmware written" in green[0], green


def test_a_written_contact_list_ends_on_one_green_line(root):
    tab = contacts_tab.ContactRefreshTab(tk.Frame(root), root)
    tab._log("PC mode entered", "ok")
    tab._on_write_done({"blocks": 1533550, "seconds": 459.0})
    assert not _coloured(tab.log, "er")
    green = _coloured(tab.log, "done")
    assert len(green) == 1 and "1,533,550 blocks" in green[0], green


@pytest.mark.parametrize("outcome, colour", [("success", "done"), ("verify_unavailable", None),
                                             ("verify_failed", "er")])
def test_a_codeplug_outcome_is_green_black_or_red(root, monkeypatch, outcome, colour):
    monkeypatch.setattr(codeplug_tab.messagebox, "showinfo", lambda *a, **k: None)
    monkeypatch.setattr(codeplug_tab.messagebox, "showwarning", lambda *a, **k: None)
    tab = codeplug_tab.CodeplugWriteTab(tk.Frame(root), root)
    res = types.SimpleNamespace(outcome=outcome, message="the outcome of the write")
    tab._on_write_done(res)
    for tag in ("done", "er"):
        lines = _coloured(tab.log, tag)
        if tag == colour:
            assert len(lines) == 1 and "the outcome of the write" in lines[0], (outcome, tag, lines)
        else:
            assert not lines, (outcome, tag, lines)
