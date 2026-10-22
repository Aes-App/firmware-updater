from __future__ import annotations

import os
import sys
import time
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
    if height is not None:
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
    else:
        tab.source_var.set("local")
        tab._on_source_change()
        tab.server_status.configure(
            text="4 target(s) downloaded and checksum-verified. Tick the ones to write, then agree and Start.")
    return root, tab


def _settle(root):
    for _ in range(6):
        root.update_idletasks()
        root.update()


def _pump(root, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.005)


def _press_start(tab, monkeypatch):
    from radio_fw import gui_tab
    monkeypatch.setattr(gui_tab.messagebox, "askyesno", lambda *a, **k: True)
    tab.plan = [types.SimpleNamespace(kind=k) for k in (spec.KIND_FW, spec.KIND_ICON)]
    tab._begin_radio()


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

    tab._enter_writing_state()
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


def test_the_port_picker_follows_the_photo_without_a_blank_line(monkeypatch):
    root, tab = _window(800, server=False)
    try:
        _press_start(tab, monkeypatch)
        _settle(root)
        under = tab._wbody.winfo_rooty() + tab._wbody.winfo_height()
        buttons_at = tab.wctl.winfo_rooty() - under
        tab._on_ready()
        _settle(root)
        assert tab.port_row.winfo_rooty() - under == buttons_at, (tab.port_row.winfo_rooty() - under, buttons_at)
    finally:
        root.destroy()


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
        assert int(float(str(tab.server_status.cget("wraplength")))) > 1000
    finally:
        root.destroy()


def test_start_does_not_resize_the_window(monkeypatch):
    root, tab = _window(None)
    sizes = set()
    try:
        gui._pin_window_size(root)
        _pump(root, 0.3)
        opened = (root.winfo_width(), root.winfo_height())
        root.bind("<Configure>", lambda e: sizes.add((root.winfo_width(), root.winfo_height()))
                  if e.widget is root else None, add="+")
        _press_start(tab, monkeypatch)
        _pump(root, 1.0)
    finally:
        root.destroy()
    assert sizes <= {opened}, "Start resized the window from %s through %s" % (opened, sorted(sizes))


def test_the_step_text_is_never_wrapped_into_the_photo(monkeypatch):
    root, tab = _window(800)
    wraps = []
    try:
        tab._wbody.bind("<Configure>", lambda _e: wraps.append(
            int(float(str(tab.winstr.cget("wraplength"))))), add="+")
        _press_start(tab, monkeypatch)
        _pump(root, 0.5)
        room = tab._wbody.winfo_width() - tab.wimage.winfo_reqwidth() - 20
    finally:
        root.destroy()
    assert wraps, "the step row was never laid out"
    assert max(wraps) <= room + 4, "wrapped at %d px beside a photo that leaves %d" % (max(wraps), room)


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


def test_the_baseband_baud_choices_follow_the_chip():
    root, tab = _window(800)
    try:
        def show(kind):
            tab.plan = [types.SimpleNamespace(kind=spec.KIND_SCT, manifest={"kind": kind})]
            tab.results = [{"kind": spec.KIND_SCT, "label": "", "state": "pending"}]
            tab.step = 0
            tab.setup.pack_forget()
            tab.wizard.pack(fill="both", expand=True)
            tab._show_step()
            tab._on_ready()
            return list(tab._sct_baud_box.cget("values")), tab._sct_baud.get()

        assert show("sct3288_baseband") == (["38400", "115200"], "38400")
        tab._sct_baud.set("115200")
        assert show("sct3288_baseband")[1] == "115200", "a retry keeps the operator's pick"
        assert show("sct3258_baseband") == (["115200", "9600"], "115200")
        assert show("sct3288_baseband") == (["38400", "115200"], "38400"), "another chip starts at its default"
    finally:
        root.destroy()


@pytest.mark.parametrize("width", [760, 980])
def test_the_baud_picker_shares_the_port_line(width):
    root, tab = _window(800, width=width, server=False)
    try:
        tab.model = "d890"
        tab.plan = [types.SimpleNamespace(kind=spec.KIND_SCT, manifest={"kind": "sct3288_baseband"})]
        tab._begin_radio()
        tab._on_ready()
        _settle(root)
        box = tab._sct_baud_box

        def mid(w):
            return w.winfo_rooty() + w.winfo_height() // 2
        assert abs(mid(box) - mid(tab.port_box)) <= 2, "on the port's line"
        assert box.winfo_width() >= box.winfo_reqwidth()
        assert box.winfo_rootx() + box.winfo_width() <= root.winfo_rootx() + root.winfo_width()
    finally:
        root.destroy()


def test_the_bt01_asks_for_no_codeplug_backup(monkeypatch):
    from radio_fw import gui_tab
    root, tab = _window(800)
    try:
        asked = []
        monkeypatch.setattr(gui_tab.messagebox, "askyesno", lambda *a, **k: asked.append(a) or True)
        tab.model = "bt01"
        tab.plan = [types.SimpleNamespace(kind=spec.KIND_FW)]
        tab._begin_radio()
        assert asked == [], "no backup question for the BT01"
        tab.results = [{"kind": spec.KIND_FW, "label": "", "state": "done"}]
        tab.step = 1
        tab._finish_radio()
        _settle(root)
        assert not tab.mcu_box.winfo_ismapped(), "no MCU reset for the BT01"
        tab.model = "d578uv2"
        tab.plan = [types.SimpleNamespace(kind=spec.KIND_FW)]
        tab._begin_radio()
        assert len(asked) == 1, "a radio still asks"
    finally:
        root.destroy()


def test_an_untested_model_warns_while_selected_and_before_a_batch(monkeypatch):
    from radio_fw import gui_tab
    root, tab = _window(800)
    try:
        _settle(root)
        assert not tab.untested_lbl.winfo_ismapped(), "no warning for a tested radio"
        tab.model_var.set("d578uv")
        tab._on_model_change()
        _settle(root)
        assert tab.untested_lbl.winfo_ismapped()
        assert "not been tested on a real radio" in tab.untested_lbl.cget("text")

        asked = []
        answers = iter([False])
        monkeypatch.setattr(gui_tab.messagebox, "askyesno",
                            lambda title, *a, **k: asked.append(title) or next(answers))
        tab.plan = [types.SimpleNamespace(kind=spec.KIND_FW)]
        tab._begin_radio()
        assert asked == ["Not tested on real hardware"], asked
        assert not tab.wizard.winfo_ismapped(), "declining stops before the walk"

        answers = iter([True, True])
        asked.clear()
        tab._begin_radio()
        assert asked == ["Not tested on real hardware", "Back up your codeplug first"], asked

        tab._lock_model(False)
        tab.model_var.set("d578uv2")
        tab._on_model_change()
        _settle(root)
        assert not tab.untested_lbl.winfo_ismapped(), "the D578UV(III) Plus is tested"
    finally:
        root.destroy()


@pytest.mark.parametrize("width", [980, 760, 640])
def test_the_model_buttons_wrap_rather_than_leave_the_window(width):
    root, tab = _window(700, width=width, server=False)
    try:
        _settle(root)
        for rb in tab._model_btns:
            assert rb.winfo_ismapped(), rb.cget("text")
            assert rb.winfo_rootx() + rb.winfo_width() <= root.winfo_rootx() + root.winfo_width(), \
                (width, rb.cget("text"))
            cx = rb.winfo_rootx() + rb.winfo_width() // 2
            cy = rb.winfo_rooty() + rb.winfo_height() // 2
            hit = root.winfo_containing(cx, cy)
            assert hit is not None and str(hit).startswith(str(rb)), (rb.cget("text"), hit)
        lines = {rb.winfo_rooty() for rb in tab._model_btns}
        label = tab._model_row.grid_slaves(row=0, column=0)[0]
        need = label.winfo_reqwidth() + sum(rb.winfo_reqwidth() + 8 for rb in tab._model_btns)
        assert (len(lines) == 1) == (need <= tab._model_row.winfo_width()), (width, len(lines), need)
    finally:
        root.destroy()


def test_the_port_list_puts_the_selected_radios_chip_first(monkeypatch):
    from radio_fw import gui_tab
    from test_ports import LISTED, AT, GD
    monkeypatch.setattr(gui_tab.list_ports, "comports", lambda: list(LISTED))
    root, tab = _window(800)
    try:
        tab.model = "d168"
        tab._refresh_ports()
        values = list(tab.port_box.cget("values"))
        assert values[0].startswith(AT.device), values
        assert tab._port_map[tab.port_var.get()] == AT.device, "the AT32 port is preselected"
        tab.model = "d878uv2"
        tab._last_port = None
        tab._refresh_ports()
        assert tab._port_map[tab.port_var.get()] == GD.device
    finally:
        root.destroy()


def test_a_d578_firmware_for_the_other_mcu_is_refused(monkeypatch):
    from radio_fw import gui_tab
    from test_ports import LISTED, AT, GD
    monkeypatch.setattr(gui_tab.list_ports, "comports", lambda: list(LISTED))
    root, tab = _window(800)
    started = []
    monkeypatch.setattr(gui_tab.threading, "Thread",
                        lambda *a, **k: types.SimpleNamespace(start=lambda: started.append(k.get("args"))))
    try:
        errors = []
        monkeypatch.setattr(gui_tab.messagebox, "showerror", lambda *a, **k: errors.append(a[1]))
        monkeypatch.setattr(gui_tab.messagebox, "askyesno",
                            lambda *a, **k: pytest.fail("a refusal must not be a question"))
        tab.model = "d578uv2"
        tab._refresh_ports()
        label = {tab._port_map[lb]: lb for lb in tab._port_map}

        def connect(ident, device, kind=spec.KIND_FW):
            tab.plan = [types.SimpleNamespace(kind=kind, artifact=b"",
                                              manifest={"ident_reply_prefix_ascii": ident})]
            tab.step = 0
            tab.results = [{"kind": kind, "label": "", "state": "pending"}]
            tab.port_var.set(label[device])
            tab._on_connect()

        connect("I578UV2A", GD.device)
        assert len(errors) == 1 and "needs the GD build" in errors[0] and not started
        assert tab.results[0]["state"] == "pending", "a refused step is not marked as writing"
        connect("ID578UV2", AT.device)
        assert len(errors) == 2 and "needs the AT build" in errors[1] and not started
        connect("I578UV2A", AT.device)
        assert len(errors) == 2 and len(started) == 1, "the matching port connects"
        tab._writing = False
        connect("I578UV2A", GD.device, kind=spec.KIND_ICON)
        assert len(errors) == 2 and len(started) == 2, "only the firmware step is MCU-specific"
    finally:
        root.destroy()


@pytest.mark.parametrize("model", ["d878uv2", "d168", "d578uv2", "bt01"])
@pytest.mark.parametrize("height", [800, 640])
def test_each_radios_own_photos_load_and_leave_the_buttons_on_screen(model, height):
    root, tab = _window(height, server=False)
    try:
        tab.model = model
        kinds = spec.model_order(model)
        tab.plan = [types.SimpleNamespace(kind=k) for k in kinds]
        tab.results = [{"kind": k, "label": spec.label(k, model), "state": "done"} for k in kinds]
        tab.setup.pack_forget()
        tab.wizard.pack(fill="both", expand=True)
        for i, kind in enumerate(kinds):
            tab.step = i
            tab._show_step()
            tab._on_ready()
            assert tab._step_img is not None, (model, kind)
            assert _visible(root, tab.connect_btn), (model, kind, height)
        tab.step = len(kinds)
        tab._finish_radio()
        _settle(root)
        if spec.mcu_reset(model):
            assert tab._mcu_img is not None, model
        assert _visible(root, tab._again_frame.winfo_children()[0]), (model, height)
    finally:
        root.destroy()


def _catalog_578():
    def bundle(model, version, ident, label):
        return {"fwupdModel": model, "cpsVersion": version, "label": label,
                "components": [{"kind": "fw", "ident": ident}, {"kind": "icon"}]}
    return [bundle("d578uv2", "2.08", "I578UV2A", "Plus 2.08 AT"),
            bundle("d578uv2", "2.08-GD", "ID578UV2", "Plus 2.08 GD"),
            bundle("d578uv2", "2.07", None, "Plus 2.07, build not given"),
            bundle("d578uv", "1.21", "ID578UVA", "G/Pro 1.21 AT")]


def test_the_d578_button_identifies_the_radio_before_anything_else(monkeypatch):
    from radio_fw import gui_tab
    from radio_contacts.engine import Ident
    from test_ports import LISTED
    monkeypatch.setattr(gui_tab.list_ports, "comports", lambda: list(LISTED))
    root, tab = _window(800, server=True)
    try:
        tab._catalog, tab._catalog_loaded = _catalog_578(), True
        tab.model_var.set("d578")
        tab._on_model_change()
        _settle(root)
        assert tab.model is None and tab.rows == [] and tab.identify_box.winfo_ismapped()
        assert tab.start_btn.instate(["disabled"])
        assert list(tab.version_box.cget("values")) == []
        tab._on_identified("d578", "D878UV2", "V100", "GD32", None)
        assert tab.model is None and "D878 Series" in str(tab.identify_status.cget("text"))
        monkeypatch.setattr(gui_tab._pc_engine, "identify",
                            lambda port, on_log: Ident("D578UV2", "V101", 0, b""))
        tab._identify_worker("d578", "/dev/cu.usbmodemC", "AT32")
        tab._drain()
        assert (tab.model, tab.radio_chip) == ("d578uv2", "AT32")
        assert [r.kind for r in tab.rows] == spec.model_order("d578uv2")
        assert list(tab.version_box.cget("values")) == ["Plus 2.08 AT", "Plus 2.07, build not given"]
        _settle(root)
        assert not tab.identify_body.winfo_ismapped()
        assert str(tab.identify_summary.cget("text")).startswith(
            "D578UV2 (2nd Gen), AT32 MCU (New) — identified from the radio")
        tab._toggle_identify()
        _settle(root)
        assert tab.identify_body.winfo_ismapped() and not tab.identify_manual.winfo_ismapped()
        assert not tab.manual_btn.winfo_ismapped(), "not even the button that would open it"
        tab.model_var.set("d890")
        tab._on_model_change()
        tab.model_var.set("d578")
        tab._on_model_change()
        assert (tab.model, tab.radio_chip) == ("d578uv2", "AT32")
    finally:
        root.destroy()


def test_a_d578_that_cannot_name_its_mcu_or_start_normally_is_chosen_by_hand(monkeypatch):
    from radio_fw import gui_tab
    from test_ports import LISTED
    monkeypatch.setattr(gui_tab.list_ports, "comports", lambda: list(LISTED))
    root, tab = _window(800, server=False)
    try:
        tab.model_var.set("d578")
        tab._on_model_change()
        _settle(root)
        assert tab.manual_btn.winfo_ismapped() and not tab.identify_manual.winfo_ismapped(), \
            "the manual choice waits for its button"
        tab._on_identified("d578", "D578UV", "V100", None, None)
        _settle(root)
        assert tab.model is None and tab.identify_manual.winfo_ismapped()
        assert tab.manual_model_var.get() == "D578UV (1st Gen)"
        assert list(tab.manual_chip_box.cget("values")) == ["AT32 MCU (New)", "GD32 MCU (Old)"]
        tab.manual_chip_var.set("GD32 MCU (Old)")
        tab._on_manual_identity()
        assert (tab.model, tab.radio_chip) == ("d578uv", "GD32")
        assert [r.kind for r in tab.rows] == spec.model_order("d578uv")
        assert str(tab.identify_summary.cget("text")) == "D578UV (1st Gen), GD32 MCU (Old) — chosen manually"
        _settle(root)
        assert not tab.identify_body.winfo_ismapped(), "folded once chosen"
    finally:
        root.destroy()


@pytest.mark.parametrize("width", [760, 980])
def test_the_manual_choice_opens_and_closes_from_its_button(monkeypatch, width):
    from radio_fw import gui_tab
    from test_ports import LISTED
    monkeypatch.setattr(gui_tab.list_ports, "comports", lambda: list(LISTED))
    root, tab = _window(800, width=width, server=False)
    try:
        tab.model_var.set("d578")
        tab._on_model_change()
        _settle(root)
        btn = tab.manual_btn
        assert btn.winfo_width() >= btn.winfo_reqwidth(), "the button is not squeezed"
        assert btn.winfo_rootx() + btn.winfo_width() <= root.winfo_rootx() + root.winfo_width()
        assert str(btn.cget("text")) == "Choose manually…" and not tab.identify_manual.winfo_ismapped()
        btn.invoke()
        _settle(root)
        assert tab.identify_manual.winfo_ismapped() and str(btn.cget("text")) == "Hide manual choice"
        btn.invoke()
        _settle(root)
        assert not tab.identify_manual.winfo_ismapped()
        btn.invoke()
        tab.manual_model_var.set("D578UV2 (2nd Gen)")
        tab.manual_chip_var.set("AT32 MCU (New)")
        tab._on_manual_identity()
        tab._toggle_identify()
        _settle(root)
        assert btn.winfo_ismapped() and not tab.identify_manual.winfo_ismapped(), "closed after use"
    finally:
        root.destroy()


def test_a_firmware_for_the_other_mcu_is_refused_once_the_radio_is_known(monkeypatch):
    from radio_fw import gui_tab
    from test_ports import LISTED
    monkeypatch.setattr(gui_tab.list_ports, "comports", lambda: list(LISTED))
    root, tab = _window(800, server=False)
    try:
        tab.model_var.set("d578")
        tab._on_model_change()
        tab._on_identified("d578", "D578UV2", "V101", "AT32", None)
        fw = next(r for r in tab.rows if r.kind == spec.KIND_FW)
        res = types.SimpleNamespace(source_names=["x.spi"], sha256="ab", frames=3, payload_bytes=96,
                                    manifest={"ident_reply_prefix_ascii": "ID578UV2"})
        fw.apply_compile(res, None)
        assert fw.result is None and "needs the AT build" in fw.error
        res.manifest = {"ident_reply_prefix_ascii": "I578UV2A"}
        fw.apply_compile(res, None)
        assert fw.result is res
    finally:
        root.destroy()


@pytest.mark.parametrize("height", [800, 640])
def test_an_identified_d578_keeps_its_targets_on_screen(monkeypatch, height):
    from radio_fw import gui_tab
    from test_ports import LISTED
    monkeypatch.setattr(gui_tab.list_ports, "comports", lambda: list(LISTED))
    root, tab = _window(height, server=True)
    try:
        tab._catalog, tab._catalog_loaded = _catalog_578(), True
        tab.model_var.set("d578")
        tab._on_model_change()
        tab._on_identified("d578", "D578UV2", "V111", "AT32", None)
        _settle(root)
        assert len(tab.rows) == 4
        hidden = [r.kind for r in tab.rows if not _visible(root, r.cb)]
        assert not hidden and _visible(root, tab.start_btn), (height, hidden)
    finally:
        root.destroy()


def test_the_prebuilt_versions_are_the_default_source():
    root, tab = _window(800, server=True)
    try:
        assert tab.source_var.get() == "server"
        assert [str(rb.cget("text")) for rb in tab._src_btns] == ["Prebuilt from AesApp", "My own files"]
    finally:
        root.destroy()


def test_the_d878_button_identifies_the_generation_and_lists_only_its_versions(monkeypatch):
    from radio_fw import gui_tab
    from test_ports import LISTED
    monkeypatch.setattr(gui_tab.list_ports, "comports", lambda: list(LISTED))
    root, tab = _window(800, server=True)
    try:
        tab._catalog = [{"fwupdModel": m, "cpsVersion": "4.00", "label": label,
                         "components": [{"kind": "fw"}, {"kind": "icon"}]}
                        for m, label in (("d878uv2", "D878UVII 4.00"), ("d878uv", "D878UV 4.00"))]
        tab._catalog_loaded = True
        tab.model_var.set("d878")
        tab._on_model_change()
        _settle(root)
        assert tab.model is None and list(tab.version_box.cget("values")) == []
        tab.manual_btn.invoke()
        _settle(root)
        assert tab.identify_manual.winfo_ismapped()
        assert not tab.manual_chip_box.winfo_ismapped(), "one MCU: nothing to choose"
        tab._on_identified("d878", "D878UV2", "V100", None, None)
        assert (tab.model, tab.radio_chip) == ("d878uv2", "GD32")
        assert [r.kind for r in tab.rows] == spec.model_order("d878uv2")
        assert list(tab.version_box.cget("values")) == ["D878UVII 4.00"]
        assert str(tab.identify_summary.cget("text")) == \
            "D878UVII (2nd Gen) — identified from the radio (D878UV2 V100)"
        fw = next(r for r in tab.rows if r.kind == spec.KIND_FW)
        res = types.SimpleNamespace(source_names=["x.spi"], sha256="ab", frames=3, payload_bytes=96,
                                    manifest={"model": "d878uv", "ident_reply_prefix_ascii": "ID878UV"})
        fw.apply_compile(res, None)
        assert fw.result is None and "D878UV (1st Gen)" in fw.error
    finally:
        root.destroy()
