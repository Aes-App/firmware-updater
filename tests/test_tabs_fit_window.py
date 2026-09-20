from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tk = pytest.importorskip("tkinter")
pytest.importorskip("serial")
pytest.importorskip("bleak")

from tkinter import ttk

from bt_ota import gui
from radio_fw import spec
from radio_fw.gui_tab import RadioBoardsTab
from radio_contacts.gui_tab import ContactRefreshTab

KINDS = [spec.KIND_FW, spec.KIND_ICON, spec.KIND_SCT, spec.KIND_NR]


def _window(height, width=980, server=True):
    try:
        root = tk.Tk()
    except Exception as e:
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
    nb.add(page, text="Radio and Boards Updates")
    tab = RadioBoardsTab(page, root)
    if server:
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
    seen = {"Start Upgrades": _visible(root, tab.start_btn)}

    tab.plan = [types.SimpleNamespace(kind=k) for k in KINDS]
    tab.results = [{"kind": k, "label": spec.label(k), "state": "done"} for k in KINDS]
    tab.step = KINDS.index(spec.KIND_SCT)
    tab.setup.pack_forget()
    tab.wizard.pack(fill="both", expand=True)
    tab._show_step()
    seen["Skip this target"] = _visible(root, tab.skip_btn)

    tab._on_ready()
    seen["Connect && Write"] = _visible(root, tab.connect_btn)

    tab.port_row.pack_forget()
    tab.skip_btn.pack_forget()
    tab.progress.pack(fill="x", pady=(6, 0), before=tab.log_lbl)
    tab.wstatus.pack(anchor="w", pady=(4, 0), before=tab.log_lbl)
    tab.abort_btn.pack(anchor="w", pady=(4, 0), before=tab.log_lbl)
    tab._schedule_fit()
    seen["Stop"] = _visible(root, tab.abort_btn)

    tab._on_stage_done()
    seen["Next"] = _visible(root, tab.next_btn)

    tab.step = len(KINDS)
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
    root, tab = _window(800, width=1200)
    try:
        _settle(root)
        banner = next(w for w in tab.setup.master.pack_slaves()
                      if isinstance(w, ttk.Label) and "NOT codeplugs" in str(w.cget("text")))
        assert int(float(str(banner.cget("wraplength")))) > 1000
    finally:
        root.destroy()


def _contact_window(height, width=980):
    try:
        root = tk.Tk()
    except Exception as e:
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
            seen["fit message"] = _visible(root, tab.fit_lbl)
            seen["running total"] = _visible(root, tab.sel_lbl)
    finally:
        root.destroy()
    missing = [name for name, ok in seen.items() if not ok]
    assert not missing, "%dpx window (%s) hides: %s" % (
        height, "local build" if local else "server list", ", ".join(missing))


def test_the_log_gives_way_faster_than_the_country_tree():
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
