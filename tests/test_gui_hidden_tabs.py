"""The Write Codeplug tab stays out of the tab strip until its link arrives.

It is useless without one: the codeplug to write comes WITH the link, and an
always-visible tab reads as a feature you can start from the app, which you
cannot. So it is built at startup and then hidden, and the link that gives it
something to do is what puts it back.

The Digital Contact Refresh tab used to be hidden for the same reason and is NOT
any more: it can build a contact list on this machine from a register download
the operator already has, which starts in the app and needs no link at all. A
contacts link still selects that tab; it is no longer what reveals it.

This pins the Tk mechanics the routing depends on: hide() takes a tab out of the
strip while leaving its page built, and add() puts it back with the label and
position hide() left behind. It also pins the routing itself -- a contacts link
must not reveal the codeplug tab, and a codeplug link must land on the codeplug
tab rather than on the contact refresher.

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

# In main()'s order. The contact refresher is in this list because a local build
# starts in the app: there is something to do in it before any link arrives.
VISIBLE_AT_STARTUP = ["Bluetooth Module Update", "Radio and Boards Updates",
                      "Digital Contact Refresh"]
# A real launch token: 32 random bytes base64url, 43 chars. parse_launch_url
# refuses anything shorter, so a placeholder here would make every link look
# unparseable and every test pass for the wrong reason.
TOKEN = "A" * 43
LINK_DRIVEN = ["Write Codeplug"]


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


def test_only_the_codeplug_tab_is_out_of_the_strip_at_startup(strip):
    """The contact refresher is in the strip from the start (a local build begins
    in the app); the codeplug tab is not, because nothing begins in it."""
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
    """The narrow claim now: the link must land on the codeplug tab, not on the
    contact refresher, which would answer it with a contacts error. The refresher
    being in the strip is no longer evidence of anything."""
    strip.deliver(f"aesapp://codeplug?token={TOKEN}&server=https://cps.aes.app")
    assert strip.selected() == "Write Codeplug"
    assert "Write Codeplug" in strip.visible()


def test_an_unrecognised_link_falls_back_to_contacts_and_selects_it(strip):
    """The contacts tab is where an unparseable link gets its error message, so
    the routing has to bring the operator to it."""
    strip.deliver("aesapp://nonsense")
    assert strip.selected() == "Digital Contact Refresh"
    assert "Digital Contact Refresh" in strip.visible()


def test_a_revealed_tab_keeps_its_label_and_its_position(strip):
    """add() after hide() restores both -- the whole reason the pages are built
    up front and hidden rather than created on demand."""
    strip.deliver(f"aesapp://codeplug?token={TOKEN}")
    strip.deliver(f"aesapp://contacts?token={TOKEN}")
    assert strip.visible() == VISIBLE_AT_STARTUP + LINK_DRIVEN
