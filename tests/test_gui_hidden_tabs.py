from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tk = pytest.importorskip("tkinter")
from tkinter import ttk

from radio_contacts import launch as _lu

VISIBLE_AT_STARTUP = ["Bluetooth Module Update", "Radio and Boards Updates",
                      "Digital Contact Refresh"]
TOKEN = "A" * 43
LINK_DRIVEN = ["Write Codeplug"]


class Strip:

    def __init__(self, root):
        self.nb = ttk.Notebook(root)
        self.pages = {}
        for name in VISIBLE_AT_STARTUP + LINK_DRIVEN:
            f = ttk.Frame(self.nb)
            self.nb.add(f, text=name)
            self.pages[name] = f
        for name in LINK_DRIVEN:
            self.nb.hide(self.pages[name])

    def visible(self):
        return [self.nb.tab(t, "text") for t in self.nb.tabs()
                if self.nb.tab(t, "state") != "hidden"]

    def selected(self):
        return self.nb.tab(self.nb.select(), "text")

    def deliver(self, url):
        want_codeplug = False
        try:
            want_codeplug = _lu.parse_launch_url(url).action == _lu.ACTION_CODEPLUG
        except Exception:
            pass
        page = self.pages["Write Codeplug" if want_codeplug else "Digital Contact Refresh"]
        self.nb.add(page)
        self.nb.select(page)


@pytest.fixture()
def strip():
    try:
        root = tk.Tk()
    except Exception as e:
        pytest.skip(f"no Tk display: {e}")
    root.withdraw()
    yield Strip(root)
    try:
        root.destroy()
    except Exception:
        pass


def test_only_the_codeplug_tab_is_out_of_the_strip_at_startup(strip):
    assert strip.visible() == VISIBLE_AT_STARTUP
    assert "Write Codeplug" not in strip.visible()
    assert strip.selected() == VISIBLE_AT_STARTUP[0], \
        "hiding the later tab must not leave the notebook on a hidden one"


def test_a_contacts_link_selects_the_contacts_tab_and_reveals_nothing_else(strip):
    strip.deliver(f"aesapp://contacts?token={TOKEN}&server=https://cps.aes.app")
    assert strip.selected() == "Digital Contact Refresh"
    assert "Digital Contact Refresh" in strip.visible()
    assert "Write Codeplug" not in strip.visible(), "a contacts link is not a codeplug link"


def test_a_codeplug_link_reveals_the_codeplug_tab_and_goes_to_it(strip):
    strip.deliver(f"aesapp://codeplug?token={TOKEN}&server=https://cps.aes.app")
    assert strip.selected() == "Write Codeplug"
    assert "Write Codeplug" in strip.visible()


def test_an_unrecognised_link_falls_back_to_contacts_and_selects_it(strip):
    strip.deliver("aesapp://nonsense")
    assert strip.selected() == "Digital Contact Refresh"
    assert "Digital Contact Refresh" in strip.visible()


def test_a_revealed_tab_keeps_its_label_and_its_position(strip):
    strip.deliver(f"aesapp://codeplug?token={TOKEN}")
    strip.deliver(f"aesapp://contacts?token={TOKEN}")
    assert strip.visible() == VISIBLE_AT_STARTUP + LINK_DRIVEN
