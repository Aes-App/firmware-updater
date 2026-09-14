"""Every button the firmware and contact tabs show must be inside the window.

THE BUG. On a laptop-height window, Connect && Write, Stop and Next were not
merely cut in half -- they were not drawn at all. The wizard packs its protocol
log with expand=True when it is built, and those controls are packed later, step
by step; a plain .pack() appends them AFTER the log. Tk's packer serves widgets in
packing order, so the log kept its ten empty lines and the buttons got whatever
was left, which on an 800-pixel window was nothing. Start on the setup view and
the finished view's buttons had the same shape.

WHAT THIS CHECKS is geometry, not code structure: the real tab, in a window of a
given size, walked through each view, with the bottom edge of every button
compared against the bottom edge of the window. A test that inspected pack order
would pass a layout that still does not fit.

A withdrawn window has no geometry, so the window is mapped but fully
transparent. Needs a Tk display; skips without one.

Run:  python -m pytest tests/test_tabs_fit_window.py -q
"""
from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tk = pytest.importorskip("tkinter")
pytest.importorskip("serial")           # the tab imports pyserial's port lister
pytest.importorskip("bleak")            # bt_ota.gui pulls in the BLE client

from tkinter import ttk                                  # noqa: E402

from bt_ota import gui                                   # noqa: E402
from radio_fw import spec                                # noqa: E402
from radio_fw.gui_tab import RadioBoardsTab              # noqa: E402
from radio_contacts.gui_tab import ContactRefreshTab     # noqa: E402

KINDS = [spec.KIND_FW, spec.KIND_ICON, spec.KIND_SCT, spec.KIND_NR]


def _window(height, width=980, server=True):
    """The tab as main() lays it out: brand header, separator, notebook page."""
    try:
        root = tk.Tk()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no Tk display: {e}")
    try:
        root.attributes("-alpha", 0.0)          # mapped, so it has geometry; not seen
    except tk.TclError:
        pass
    root.geometry("%dx%d+20+20" % (width, height))
    header = ttk.Frame(root)
    header.pack(fill="x", padx=8, pady=6)
    gui._brand_header(header, small=True).pack(side="left")
    ttk.Separator(root).pack(fill="x", padx=8)
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)
    page = ttk.Frame(nb)
    nb.add(page, text="Radio and Boards Updates")
    tab = RadioBoardsTab(page, root)
    if server:
        # The configuration the report came from: the server picker and its
        # status line take a row above everything else.
        tab.source_var.set("server")
        tab.server_row.pack(fill="x", pady=(0, 6), after=tab.src_row)
        tab.server_status.configure(
            text="4 target(s) downloaded and checksum-verified. Tick the ones to write, then agree and Start.")
    return root, tab


def _settle(root):
    for _ in range(6):
        root.update_idletasks()
        root.update()


def _visible(root, widget) -> bool:
    _settle(root)
    if not widget.winfo_ismapped():
        return False
    return widget.winfo_rooty() + widget.winfo_height() <= root.winfo_rooty() + root.winfo_height()


def _walk(root, tab):
    """Every view, in the order an operator meets it. Returns {button: visible}."""
    seen = {"Start Upgrades": _visible(root, tab.start_btn)}

    tab.plan = [types.SimpleNamespace(kind=k) for k in KINDS]
    tab.results = [{"kind": k, "label": spec.label(k), "state": "done"} for k in KINDS]
    tab.step = KINDS.index(spec.KIND_SCT)       # the tallest step: it adds a baud row
    tab.setup.pack_forget()
    tab.wizard.pack(fill="both", expand=True)
    tab._show_step()
    seen["Skip this target"] = _visible(root, tab.skip_btn)

    tab._on_ready()
    seen["Connect && Write"] = _visible(root, tab.connect_btn)

    # The writing state, exactly as _on_connect lays it out (without a port).
    tab.port_row.pack_forget()
    tab.skip_btn.pack_forget()
    tab.progress.pack(fill="x", pady=(6, 0), before=tab.log_lbl)
    tab.wstatus.pack(anchor="w", pady=(4, 0), before=tab.log_lbl)
    tab.abort_btn.pack(anchor="w", pady=(4, 0), before=tab.log_lbl)
    tab._schedule_fit()
    seen["Stop"] = _visible(root, tab.abort_btn)

    tab._on_stage_done()
    seen["Next"] = _visible(root, tab.next_btn)

    tab.step = len(KINDS)                       # firmware written, so the MCU photo shows
    tab._finish_radio()
    seen["Update another radio"] = _visible(root, tab._again_frame.winfo_children()[0])
    return seen


@pytest.mark.parametrize("height", [800, 700, 640])
@pytest.mark.parametrize("server", [False, True])
def test_every_button_is_inside_the_window(height, server):
    root, tab = _window(height, server=server)
    try:
        seen = _walk(root, tab)
    finally:
        root.destroy()
    missing = [name for name, ok in seen.items() if not ok]
    assert not missing, "%dpx window hides: %s" % (height, ", ".join(missing))


def test_the_step_photo_gives_way_before_the_buttons_do():
    """The photo is the one large block that can shrink, so on a short window it
    does -- and on a tall one it stays at its natural size rather than being
    shrunk for nothing."""
    heights = {}
    for h in (1000, 640):
        root, tab = _window(h)
        try:
            _walk(root, tab)
            tab.done.pack_forget()
            tab.wizard.pack(fill="both", expand=True)
            tab.step = KINDS.index(spec.KIND_SCT)
            tab._show_step()
            _settle(root)
            heights[h] = tab._step_img.height()
            natural = tab._step_natural.height()
        finally:
            root.destroy()
    assert heights[1000] == natural, "a tall window must show the photo at full size"
    assert heights[640] < natural, "a short window must shrink the photo"


def test_long_labels_wrap_to_the_window_not_to_a_fixed_width():
    """The wraplengths were fixed pixel guesses, so a wider window spent its width
    as extra LINES of text -- height the buttons then did not have."""
    root, tab = _window(800, width=1200)
    try:
        _settle(root)
        banner = next(w for w in tab.setup.master.pack_slaves()
                      if isinstance(w, ttk.Label) and "NOT codeplugs" in str(w.cget("text")))
        assert int(float(str(banner.cget("wraplength")))) > 1000
    finally:
        root.destroy()


# ---- Digital Contact Refresh ------------------------------------------------
#
# Same bug, different shape. Built top to bottom with pack, the local-build
# section (file rows and the country tree) came before the Radio section and the
# Write row, so at laptop heights Write to radio was not drawn at all -- and the
# 800-pixel layout that did show Write had quietly dropped the progress bar, the
# status line and the log instead. The tab is a grid now: controls never shrink,
# the log gives first, then the tree, which scrolls.

def _contact_window(height, width=980):
    try:
        root = tk.Tk()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no Tk display: {e}")
    try:
        root.attributes("-alpha", 0.0)
    except tk.TclError:
        pass
    root.geometry("%dx%d+20+20" % (width, height))
    header = ttk.Frame(root)
    header.pack(fill="x", padx=8, pady=6)
    gui._brand_header(header, small=True).pack(side="left")
    ttk.Separator(root).pack(fill="x", padx=8)
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)
    page = ttk.Frame(nb)
    nb.add(page, text="Digital Contact Refresh")
    return root, ContactRefreshTab(page, root)


def _local(tab):
    tab.source_var.set("local")
    tab._on_source_change()
    # Two-line messages, as a real gate and a running build produce.
    tab.fit_lbl.configure(
        text="Connect the radio (step 3): its model decides the contact format and how many contacts fit.")
    tab.wstatus.configure(text="Building the contact database on this computer…")


@pytest.mark.parametrize("height", [900, 800, 700])
@pytest.mark.parametrize("local", [False, True])
def test_the_contact_tab_keeps_write_stop_and_its_messages_on_screen(height, local):
    root, tab = _contact_window(height)
    try:
        if local:
            _local(tab)
        seen = {"Write to radio": _visible(root, tab.write_btn),
                "Stop": _visible(root, tab.abort_btn),
                "status line": _visible(root, tab.wstatus)}
        if local:
            # The fit message is the line that says WHY Write is disabled; a Write
            # button you can see but not explain is only half the fix.
            seen["fit message"] = _visible(root, tab.fit_lbl)
            seen["running total"] = _visible(root, tab.sel_lbl)
    finally:
        root.destroy()
    missing = [name for name, ok in seen.items() if not ok]
    assert not missing, "%dpx window (%s) hides: %s" % (
        height, "local build" if local else "server list", ", ".join(missing))


def test_the_log_gives_way_faster_than_the_country_tree():
    """While countries are being picked the log is empty and the tree is what is
    in use, so on a window a little too short the log absorbs most of the
    shortfall and the tree keeps nearly all its rows.

    Proportional, not strictly log-first: grid splits a shortfall by row weight.
    That is deliberate -- the same weights let a tall window grow the tree too --
    so this asserts the proportion, not that the log reaches zero first.
    """
    root, tab = _contact_window(1400)
    try:
        _local(tab)
        _settle(root)
        tree_natural = tab.tree.winfo_reqheight()
        log_natural = tab.log.frame.winfo_reqheight()
    finally:
        root.destroy()

    root, tab = _contact_window(850)
    try:
        _local(tab)
        _settle(root)
        tree_lost = tree_natural - tab.tree.winfo_height()
        log_lost = log_natural - tab.log.frame.winfo_height()
    finally:
        root.destroy()
    assert tree_lost <= tree_natural * 0.2, "the tree lost more than a fifth of its height"
    assert log_lost >= 2 * max(tree_lost, 1), "the log should give way well before the tree does"
