"""Both link-driven tabs stay out of the tab strip until their link arrives.

The Digital Contact Refresh and Write Codeplug tabs are useless without a link
from cps.aes.app -- the list to write, or the codeplug, comes WITH the link. An
always-visible tab reads as a feature you can start from the app, which you
cannot. So both are built at startup and then hidden, and the link that gives
them something to do is what puts them back.

This pins the Tk mechanics the routing depends on: hide() takes a tab out of the
strip while leaving its page built, and add() puts it back with the label and
position hide() left behind. It also pins the routing itself -- a contacts link
must not reveal the codeplug tab, and vice versa.

Needs a Tk display; skips where there is none.

Run:  python -m pytest tests/test_gui_hidden_tabs.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tk = pytest.importorskip("tkinter")
from tkinter import ttk                                              # noqa: E402

from radio_contacts import launch as _lu                             # noqa: E402

VISIBLE_AT_STARTUP = ["Bluetooth Module Update", "Radio and Boards Updates"]
# A real launch token: 32 random bytes base64url, 43 chars. parse_launch_url
# refuses anything shorter, so a placeholder here would make every link look
# unparseable and every test pass for the wrong reason.
TOKEN = "A" * 43
LINK_DRIVEN = ["Digital Contact Refresh", "Write Codeplug"]


class Strip:
    """The tab strip as main() builds it, with the same add/hide calls."""

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
        """main()._deliver, minus the tab objects: route, unhide, select."""
        want_codeplug = False
        try:
            want_codeplug = _lu.parse_launch_url(url).action == _lu.ACTION_CODEPLUG
        except Exception:  # noqa: BLE001
            pass
        page = self.pages["Write Codeplug" if want_codeplug else "Digital Contact Refresh"]
        self.nb.add(page)
        self.nb.select(page)


@pytest.fixture()
def strip():
    try:
        root = tk.Tk()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no Tk display: {e}")
    root.withdraw()
    yield Strip(root)
    try:
        root.destroy()
    except Exception:  # noqa: BLE001
        pass


def test_neither_link_driven_tab_is_in_the_strip_at_startup(strip):
    assert strip.visible() == VISIBLE_AT_STARTUP
    assert strip.selected() == VISIBLE_AT_STARTUP[0], \
        "hiding the later tabs must not leave the notebook on a hidden one"


def test_a_contacts_link_reveals_only_the_contacts_tab(strip):
    strip.deliver(f"aesapp://contacts?token={TOKEN}&server=https://cps.aes.app")
    assert strip.selected() == "Digital Contact Refresh"
    assert "Digital Contact Refresh" in strip.visible()
    assert "Write Codeplug" not in strip.visible(), "a contacts link is not a codeplug link"


def test_a_codeplug_link_reveals_only_the_codeplug_tab(strip):
    strip.deliver(f"aesapp://codeplug?token={TOKEN}&server=https://cps.aes.app")
    assert strip.selected() == "Write Codeplug"
    assert "Write Codeplug" in strip.visible()
    assert "Digital Contact Refresh" not in strip.visible()


def test_an_unrecognised_link_falls_back_to_contacts_and_reveals_it(strip):
    """The contacts tab is where an unparseable link gets its error message, so
    it has to be visible to show it."""
    strip.deliver("aesapp://nonsense")
    assert strip.selected() == "Digital Contact Refresh"
    assert "Digital Contact Refresh" in strip.visible()


def test_a_revealed_tab_keeps_its_label_and_its_position(strip):
    """add() after hide() restores both -- the whole reason the pages are built
    up front and hidden rather than created on demand."""
    strip.deliver(f"aesapp://codeplug?token={TOKEN}")
    strip.deliver(f"aesapp://contacts?token={TOKEN}")
    assert strip.visible() == VISIBLE_AT_STARTUP + LINK_DRIVEN
