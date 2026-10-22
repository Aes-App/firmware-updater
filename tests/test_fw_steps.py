from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tk = pytest.importorskip("tkinter")
pytest.importorskip("serial")

from radio_fw import gui_tab, spec
from test_tabs_fit_window import _catalog_578, _settle, _window

from test_ports import LISTED


@pytest.fixture(autouse=True)
def _ports(monkeypatch):
    monkeypatch.setattr(gui_tab.list_ports, "comports", lambda: list(LISTED))
    monkeypatch.setattr(gui_tab.messagebox, "askyesno", lambda *a, **k: True)


def _compiled(kind):
    return types.SimpleNamespace(kind=kind, source_names=["x." + kind], sha256="ab", frames=10,
                                 payload_bytes=320, manifest={}, artifact=b"")


def _steps(tab):
    return [(key, tab._heads[key].state, str(tab._heads[key].badge.cget("text"))) for key in tab._step_keys()]


def _accented(tab):
    return [key for key, btn in (("identify", tab.identify_btn), ("files", tab.fetch_btn),
                                 ("start", tab.start_btn)) if str(btn.cget("default")) == "active"]


def test_formatting_of_time_left_and_time_taken():
    assert gui_tab._fmt_eta(5) == "about 5 s"
    assert gui_tab._fmt_eta(300) == "about 5 min"
    assert gui_tab._fmt_took(41) == "41 s"
    assert gui_tab._fmt_took(133) == "2 min 13 s"


def test_a_disabled_default_button_gives_up_the_accent():
    try:
        root = tk.Tk()
    except Exception as e:
        pytest.skip(f"no Tk display: {e}")
    root.withdraw()
    try:
        btn = gui_tab._AccentButton(root, text="Start", accent=True)
        assert str(btn.cget("default")) == "active"
        btn.state(["disabled"])
        assert str(btn.cget("default")) == "normal"
        btn.state(["!disabled"])
        assert str(btn.cget("default")) == "active"
        btn.configure(state="disabled")
        assert str(btn.cget("default")) == "normal"
        btn.configure(state="normal")
        btn.accent(False)
        assert str(btn.cget("default")) == "normal"
    finally:
        root.destroy()


def test_the_steps_follow_a_d578_from_identify_to_start():
    root, tab = _window(800, server=True)
    try:
        tab._catalog, tab._catalog_loaded = _catalog_578(), True
        tab.model_var.set("d578")
        tab._on_model_change()
        assert _steps(tab) == [("model", "done", "✓"), ("identify", "current", "2"), ("files", "todo", "3"),
                               ("targets", "todo", "4"), ("start", "todo", "5")]
        assert _accented(tab) == ["identify"]
        assert tab.start_hint.cget("text") == "Identify the radio first (step 2)."

        tab._on_identified("d578", "D578UV2", "V111", "AT32", None)
        assert [s for _, s, _ in _steps(tab)] == ["done", "done", "current", "todo", "todo"]
        assert _accented(tab) == ["files"], "Fetch is the thing to press now"
        assert tab.start_hint.cget("text") == "Fetch a version first (step 3)."

        for row in tab.rows:
            row.apply_compile(_compiled(row.kind), None)
        assert [s for _, s, _ in _steps(tab)] == ["done", "done", "done", "done", "current"]
        assert tab.targets_note.cget("text") == "4 to write"
        assert tab.start_hint.cget("text") == "Tick the agreement above to start."
        assert _accented(tab) == [], "Start is disabled until the agreement, so it has no accent yet"

        tab.confirm.set(True)
        tab._refresh_start_state()
        assert _accented(tab) == ["start"] and tab.start_hint.cget("text") == ""

        for row in tab.rows:
            row.checked.set(False)
        tab._refresh_start_state()
        assert [s for _, s, _ in _steps(tab)][3] == "current"
        assert tab.start_hint.cget("text") == "Tick at least one target (step 4)."
    finally:
        root.destroy()


def test_a_radio_without_a_family_has_four_steps_and_own_files_need_no_fetch():
    root, tab = _window(800, server=False)
    try:
        assert _steps(tab) == [("model", "done", "✓"), ("files", "done", "✓"),
                               ("targets", "current", "3"), ("start", "todo", "4")]
        assert tab.start_hint.cget("text") == "Choose the files for at least one target (step 3)."
        assert _accented(tab) == []
    finally:
        root.destroy()


def test_the_rail_joins_the_steps_and_is_green_under_a_done_one():
    root, tab = _window(800, server=True)
    try:
        tab._catalog, tab._catalog_loaded = _catalog_578(), True
        tab.model_var.set("d578")
        tab._on_model_change()
        _settle(root)
        placed = [bool(r.place_info()) for r in tab._rails]
        assert placed[:2] == [False, True], placed
        assert str(tab._rails[1].cget("background")) == gui_tab._RAIL["todo"]
        tab._on_identified("d578", "D578UV2", "V111", "AT32", None)
        for row in tab.rows:
            row.apply_compile(_compiled(row.kind), None)
        _settle(root)
        under_targets = tab._rails[3]
        assert under_targets.place_info() and int(under_targets.place_info()["height"]) > 40
        assert str(under_targets.cget("background")) == gui_tab._RAIL["done"]
        badge = tab._heads["targets"].badge
        centre = badge.winfo_rootx() + badge.winfo_width() // 2
        assert abs(under_targets.winfo_rootx() + 1 - centre) <= 1, "the rail runs through the badges' centres"
    finally:
        root.destroy()


def _begin(tab, kinds):
    tab.model = "d890"
    tab.plan = [_compiled(k) for k in kinds]
    tab._begin_radio()


def test_the_wizard_takes_the_setup_steps_room_and_back_to_setup_returns_it():
    root, tab = _window(800, server=False)
    try:
        _settle(root)
        assert tab.top.winfo_ismapped()
        _begin(tab, [spec.KIND_FW, spec.KIND_ICON])
        _settle(root)
        assert not tab.top.winfo_ismapped() and not any(r.place_info() for r in tab._rails)
        assert tab.wcontext.cget("text") == "D890UV · your own files"
        tab._back_to_setup()
        _settle(root)
        assert tab.top.winfo_ismapped() and tab.setup.winfo_ismapped()
    finally:
        root.destroy()


def _track(tab):
    return [str(badge.cget("text")) for badge, _ in tab._track_items]


def test_the_track_marks_each_target_as_the_batch_goes():
    root, tab = _window(800, server=False)
    try:
        _begin(tab, [spec.KIND_FW, spec.KIND_ICON, spec.KIND_SCT])
        assert _track(tab) == ["1", "2", "3"]
        tab._on_ready()
        tab._enter_writing_state()
        tab._on_stage_done()
        assert _track(tab) == ["✓", "2", "3"]
        tab._on_next()
        tab._on_stage_error("timeout", False)
        assert _track(tab) == ["✓", "✕", "3"]
        tab._on_skip()
        assert _track(tab) == ["✓", "✕", "3"] and tab.step == 2
        tab._on_skip()
        assert [r["state"] for r in tab.results] == ["done", "failed", "skipped"]
    finally:
        root.destroy()


def test_a_narrow_window_keeps_only_the_current_targets_name_on_the_track():
    root, tab = _window(800, width=560, server=False)
    try:
        _begin(tab, [spec.KIND_FW, spec.KIND_ICON, spec.KIND_SCT, spec.KIND_NR])
        _settle(root)
        tab._fit_track()
        shown = [bool(name.winfo_manager()) for _, name in tab._track_items]
        assert shown == [True, False, False, False], shown
    finally:
        root.destroy()


def test_the_banner_shows_progress_time_left_success_and_failure(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(gui_tab.time, "monotonic", lambda: clock[0])
    root, tab = _window(800, server=False)
    try:
        _begin(tab, [spec.KIND_FW, spec.KIND_ICON])
        tab._on_ready()
        tab._enter_writing_state()
        assert tab.wstatus.cget("text") == "Connecting to the radio…"
        assert str(tab.wstatus.cget("background")) == gui_tab._BANNER["info"][0]
        tab._on_progress(10, 100, "write")
        assert "left" not in tab.wstatus.cget("text"), "no pace yet"
        clock[0] = 4.0
        tab._on_progress(50, 100, "write")
        assert tab.wstatus.cget("text") == "Writing Radio Firmware, about 5 s left — do not unplug the radio " \
                                           "or turn it off."
        assert tab.pct_lbl.cget("text") == "50%"

        tab.results[0]["t0"] = 0.0
        clock[0] = 133.0
        tab._on_stage_done()
        assert tab.wstatus.cget("text").startswith("✓ Radio Firmware written in 2 min 13 s.")
        assert str(tab.wstatus.cget("background")) == gui_tab._BANNER["ok"][0]

        tab._on_next()
        tab._on_ready()
        tab._enter_writing_state()
        tab._on_stage_error("no reply. Nothing was written.", False)
        assert tab.wstatus.cget("text").startswith("✕ Failed: no reply")
        assert "partly written" not in tab.wstatus.cget("text")
        assert str(tab.wstatus.cget("background")) == gui_tab._BANNER["error"][0]
        order = tab.wizard.pack_slaves()
        assert order.index(tab.wctl) > order.index(tab.wstatus), "Retry and Skip sit under the failure"
        tab._on_ready()
        assert not tab.wstatus.winfo_manager() and not tab.progress_row.winfo_manager()
    finally:
        root.destroy()


@pytest.mark.parametrize("states, headline", [
    (["done", "failed", "skipped"], "✕ Icons & Fonts not written — 1 written, 1 skipped."),
    (["done", "done", "done"], "✓ All 3 targets written."),
    (["done", "skipped", "done"], "✓ 2 written, 1 skipped."),
    (["skipped", "skipped", "skipped"], "Nothing was written: every target was skipped."),
])
def test_the_finished_view_says_what_happened_to_each_target(states, headline):
    root, tab = _window(800, server=False)
    try:
        kinds = [spec.KIND_FW, spec.KIND_ICON, spec.KIND_SCT]
        _begin(tab, kinds)
        for r, state, secs in zip(tab.results, states, (133.0, 12.0, 41.0)):
            r["state"] = state
            if state != "skipped":
                r["seconds"] = secs
        tab.step = len(kinds)
        tab._finish_radio()
        assert tab.done_title.cget("text") == headline
        texts = [str(w.cget("text")) for w in tab.done_summary.grid_slaves(column=2)][::-1]
        expected = {"done": "written in ", "failed": "failed after ", "skipped": "skipped"}
        assert all(t.startswith(expected[s]) for t, s in zip(texts, states)), texts
    finally:
        root.destroy()


def test_one_target_written_is_named():
    root, tab = _window(800, server=False)
    try:
        _begin(tab, [spec.KIND_FW])
        tab.results[0]["state"] = "done"
        tab.step = 1
        tab._finish_radio()
        assert tab.done_title.cget("text") == "✓ Radio Firmware written."
    finally:
        root.destroy()
