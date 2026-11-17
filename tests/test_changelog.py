from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tk = pytest.importorskip("tkinter")
pytest.importorskip("serial")
pytest.importorskip("bleak")

from tkinter import font as tkfont

from radio_fw import changelog, gui_tab
from test_ports import LISTED
from test_tabs_fit_window import _settle, _window

NOTES_209 = ("Released 2026-11-07 with CPS V1.22.\n\n"
             "1. Separate TX and RX color codes for each channel, so a long line wraps under its own text.\n"
             "12. Fixed an analog channel sending a random DMR ID.\n"
             "- in AM too")


def _catalog():
    def bundle(version, label, notes):
        return {"fwupdModel": "d890", "model": "anytone_d890", "cpsVersion": version, "label": label,
                "cpsUrl": "https://example.invalid/cps.zip", "changelog": notes,
                "components": [{"kind": "fw", "version": "V" + version + " DMR"},
                               {"kind": "nr", "version": "V112"}]}
    return [bundle("1.05", "1.05 DMR", NOTES_209),
            bundle("1.05-NX", "1.05 NX & DMR Beta", None),
            bundle("1.04", "1.04 DMR", "   \n  ")]


@pytest.fixture()
def tab(monkeypatch):
    monkeypatch.setattr(gui_tab.list_ports, "comports", lambda: list(LISTED))
    root, tab = _window(800, server=True)
    tab._catalog, tab._catalog_loaded = _catalog(), True
    tab._populate_versions()
    yield tab
    root.destroy()


def test_a_changelog_is_text_or_nothing():
    assert changelog.changelog_of({"changelog": "  1. One.\n"}) == "1. One."
    for bundle in ({"changelog": None}, {"changelog": "  \n "}, {"changelog": 3}, {}, None):
        assert changelog.changelog_of(bundle) == "", bundle


def test_list_items_wrap_under_their_own_text():
    try:
        root = tk.Tk()
    except Exception as e:
        pytest.skip(f"no Tk display: {e}")
    root.withdraw()
    try:
        text = tk.Text(root)
        font = tkfont.nametofont("TkTextFont")
        changelog.render(text, NOTES_209, font)
        lines = text.get("1.0", "end-1c").split("\n")
        assert lines[0] == "Released 2026-11-07 with CPS V1.22." and lines[1] == ""
        tags = {i: text.tag_names("%d.0" % (i + 1)) for i in range(len(lines))}
        assert tags[0] == ("para",) and tags[1] == ("gap",)
        assert tags[2] == ("item%d" % font.measure("1. "),)
        assert tags[3] == ("item%d" % font.measure("12. "),)
        assert int(str(text.tag_cget(tags[3][0], "lmargin2"))) == font.measure("12. ")
        assert lines[4] == "- in AM too"
        changelog.render(text, "", font)
        assert text.get("1.0", "end-1c") == changelog.NO_CHANGELOG
    finally:
        root.destroy()


def test_the_button_is_offered_only_for_a_version_with_a_changelog(tab):
    assert list(tab.version_box.cget("values")) == ["1.05 DMR", "1.05 NX & DMR Beta", "1.04 DMR"]
    assert tab.changelog_btn.instate(["!disabled"])
    for label, offered in (("1.05 NX & DMR Beta", False), ("1.04 DMR", False), ("1.05 DMR", True)):
        tab.version_var.set(label)
        tab._on_version_selected()
        assert tab.changelog_btn.instate(["!disabled"]) is offered, label
    tab._reload_catalog = lambda: None
    tab.model_var.set("d578")
    tab._on_model_change()
    assert tab.changelog_btn.instate(["disabled"])


def test_the_dialog_shows_each_version_and_carries_a_choice_back(tab):
    tab.changelog_btn.invoke()
    dlg = tab._changelog_dialog
    try:
        assert dlg.versions.get(0, "end") == ("1.05 DMR", "1.05 NX & DMR Beta", "1.04 DMR")
        assert dlg.versions.curselection() == (0,), "the version picked in the tab comes first"
        assert str(dlg.title.cget("text")) == "1.05 DMR"
        assert str(dlg.parts.cget("text")) == "Radio Firmware V1.05 DMR · NR Board V112"
        assert dlg.text.get("1.0", "2.0").strip() == "Released 2026-11-07 with CPS V1.22."
        assert str(dlg.versions.itemcget(1, "foreground")) == "#999", "one with none is greyed"

        dlg.select(1)
        assert dlg.text.get("1.0", "end-1c") == changelog.NO_CHANGELOG
        dlg._use()
        assert tab.version_var.get() == "1.05 NX & DMR Beta"
        assert not dlg.win.winfo_exists(), "the dialog closes"
        assert tab.changelog_btn.instate(["disabled"]), "and the tab follows the new version"
    finally:
        dlg.close()


def test_the_server_row_fits_the_narrowest_window(monkeypatch):
    monkeypatch.setattr(gui_tab.list_ports, "comports", lambda: list(LISTED))
    root, tab = _window(700, width=760, server=True)
    try:
        tab._catalog, tab._catalog_loaded = _catalog(), True
        tab._populate_versions()
        _settle(root)
        right = root.winfo_rootx() + root.winfo_width()
        for btn in (tab.fetch_btn, tab.changelog_btn, tab.cps_btn):
            assert btn.winfo_ismapped() and btn.winfo_width() >= btn.winfo_reqwidth(), btn.cget("text")
            assert btn.winfo_rootx() + btn.winfo_width() <= right, btn.cget("text")
    finally:
        root.destroy()
