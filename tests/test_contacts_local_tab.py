"""The local-build half of the Digital Contact tab, driven through a real Tk widget.

Why this file exists: everything that decides whether a locally built list may be
written lives in the tab, not in the builder. The builder is pinned byte-for-byte
in tests/test_contacts_local_build.py and is not re-tested here. What is tested
here is the wiring that surrounds it, and specifically the four things that would
be silent if they broke:

  * a local build must need NO server session and NO launch token. The link-driven
    gate required both, and a gate that still asks for them would leave Write
    permanently dead for an operator who never had a link.
  * a store, a ticked country, a connected radio and a port must together be
    enough -- and the button must go live for exactly that.
  * a selection bigger than the radio holds must keep the button DISABLED and say
    which two numbers disagree. Silently writing 218,000 contacts to a radio that
    holds 200,000 is the failure this gate exists for.
  * the picker must render in group_facets' order, counts and all. That order --
    both levels by size, descending -- is the only reason the widget is usable on
    a worldwide register, and re-sorting it anywhere would look like a tidy-up.

Same shape as tests/test_contacts_tab_pump.py: a real tab against a withdrawn
Tk root, with the queue drained by hand. Needs a Tk display; skips where there is
none (headless CI).

Run:  python -m pytest tests/test_contacts_local_tab.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tk = pytest.importorskip("tkinter")

from radio_contacts import gui_tab as gt  # noqa: E402
from radio_contacts import segments as seg  # noqa: E402

cb = gt.contact_build


@pytest.fixture()
def tab():
    try:
        root = tk.Tk()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no Tk display: {e}")
    root.withdraw()
    frame = tk.Frame(root)
    t = gt.ContactRefreshTab(frame, root)
    yield t
    try:
        root.destroy()
    except Exception:  # noqa: BLE001
        pass


def _pump(tab, times=3):
    for _ in range(times):
        tab._drain()


class _Ident:
    """What engine.open_session() returns alongside the link."""

    def __init__(self, model="D890UV", version="V100"):
        self.model = model
        self.version = version
        self.band = 0


#: (country, how many contacts). Deliberately not in size order, and with a
#: bigger continent after a smaller one, so a picker that rendered the input
#: order rather than group_facets' order would look right by accident.
ROWS = [("United Kingdom", 5), ("Germany", 3), ("Japan", 50), ("China", 7),
        ("United States", 2), ("", 1), ("Ruritania", 4)]


def _store(rows=ROWS):
    """A ContactStore built directly, without a CSV.

    read_user_csv is covered by tests/test_contacts_local_build.py; what the tab
    needs is a store, and building one here keeps this file about the UI.
    """
    store = cb.ContactStore()
    rid = 3101234
    for country, count in rows:
        for _ in range(count):
            rid += 3
            store.ids.append(rid)
            store.callsigns.append("VA7TST")
            store.names.append("Name")
            store.cities.append("Vancouver")
            store.states.append("BC")
            store.countries.append(country)
            store.codes.append(cb.country_code(country))
    return store


def _local(tab):
    tab.source_var.set(gt._SOURCE_LOCAL)
    tab._on_source_change()


def _load(tab, store=None):
    store = _store() if store is None else store
    tab.user_path = "/tmp/user.csv"
    tab._post("local_store", store)
    _pump(tab)
    return store


def _port(tab, port="COM9"):
    """Pick a COM port without a radio on the machine: the combobox is filled
    from the real port list, which is empty on a build machine."""
    tab._port_map = {port: port}
    tab.port_var.set(port)


def _connect(tab, model="D890UV"):
    tab._post("ident_ok", (_Ident(model), object()))
    _pump(tab)


def _ready(tab, model="D890UV"):
    """The whole local flow, up to the click on Write."""
    _local(tab)
    _load(tab)
    tab._select_codes({"GBR"})
    _port(tab)
    _connect(tab, model)


# ---- the source switch ------------------------------------------------------
def test_switching_to_a_local_build_asks_for_no_link_or_session(tab):
    """The link box goes away and the local section takes its place. winfo_manager
    is the check that works on a withdrawn root -- winfo_ismapped is 0 for every
    widget in a window that was never shown, so it would pass for the wrong
    reason."""
    _local(tab)
    assert tab.local_box.winfo_manager() == "pack"
    assert tab.server_box.winfo_manager() == "", "the link section must be out of the way"
    assert tab.lst.winfo_manager() == "", "the server's bundle picker has nothing to show"
    assert tab.session is None and tab.token is None
    assert "user.csv" in tab.fit_lbl.cget("text"), \
        "the gate must ask for the file, not for a link"

    # and back: the link section returns and the local one goes away
    tab.source_var.set(gt._SOURCE_SERVER)
    tab._on_source_change()
    assert tab.server_box.winfo_manager() == "pack"
    assert tab.local_box.winfo_manager() == ""
    assert tab.lst.winfo_manager() == "pack"


def test_a_link_arriving_switches_back_to_the_server_source(tab):
    """A link is only useful to the aes.app source, and clicking it IS the
    operator asking for that source -- otherwise the session it opens would
    report itself into a section nobody can see."""
    _local(tab)
    tab.handle_launch_url("aesapp://contacts?token=" + "A" * 43)
    assert not tab._is_local()
    assert tab.server_box.winfo_manager() == "pack"


# ---- the write gate ---------------------------------------------------------
def test_a_store_a_ticked_country_and_a_connected_radio_enable_write(tab):
    _ready(tab)
    assert tab.session is None, "a local build must not need a server session"
    assert tab.token is None, "a local build must not need a launch token"
    assert "disabled" not in tab.write_btn.state()
    assert tab.fit_lbl.cget("text").startswith("Ready:")
    assert "5 DMR contacts" in tab.fit_lbl.cget("text")
    assert "AnyTone AT-D890UV" in tab.ident_lbl.cget("text")


def test_each_missing_piece_keeps_write_disabled_and_names_itself(tab):
    _local(tab)
    assert "disabled" in tab.write_btn.state()
    assert "user.csv" in tab.fit_lbl.cget("text")

    _load(tab)
    assert "disabled" in tab.write_btn.state()
    assert "Nothing is selected" in tab.fit_lbl.cget("text"), \
        "nothing selected is nothing to write, and it must say so"

    tab._select_codes({"GBR"})
    assert "disabled" in tab.write_btn.state()
    assert "Connect the radio" in tab.fit_lbl.cget("text")

    _port(tab)
    _connect(tab)
    assert "disabled" not in tab.write_btn.state()

    # and taking the selection away again puts the button back
    tab._select_codes(set())
    assert "disabled" in tab.write_btn.state()


def test_an_over_capacity_selection_keeps_write_disabled_and_names_the_numbers(tab, monkeypatch):
    """The capacity comes from contact_build.RADIOS, so a radio with a tiny one
    exercises the real lookup rather than a hand-set spec."""
    monkeypatch.setitem(cb.RADIOS, "TINYUV",
                        cb.RadioSpec("TINYUV", "Tiny AT-D1UV", "anytone_878", None, 5))
    _local(tab)
    _load(tab)
    _port(tab)
    _connect(tab, "TINYUV")

    tab._select_codes({"GBR"})                    # 5 contacts, exactly the capacity
    assert "disabled" not in tab.write_btn.state(), "a list that exactly fits must be writable"

    tab._select_codes({"GBR", "DEU"})             # 8 contacts, three too many
    assert "disabled" in tab.write_btn.state()
    msg = tab.fit_lbl.cget("text")
    assert "8 DMR contacts is more than the 5" in msg
    assert "Tiny AT-D1UV" in msg
    assert "untick" in msg


def test_a_radio_this_app_cannot_build_for_is_refused_by_name(tab):
    _local(tab)
    _load(tab)
    tab._select_codes({"GBR"})
    _port(tab)
    _connect(tab, "D868UV")                        # recognised, not buildable
    assert tab.spec is None
    assert "disabled" in tab.write_btn.state()
    assert "AT-D868UV" in tab.fit_lbl.cget("text"), \
        "naming the radio is the difference between a refusal and a fault"


# ---- the country picker -----------------------------------------------------
def test_the_picker_renders_group_facets_order_and_counts(tab):
    """Both levels largest-first, exactly as group_facets returned them, with the
    group's own total beside it."""
    _local(tab)
    store = _load(tab)
    want = cb.group_facets(store.facets())

    groups = tab.tree.get_children("")
    assert len(groups) == len(want)
    for gid, (_key, name, total, members) in zip(groups, want):
        assert name in tab.tree.item(gid, "text")
        assert tab.tree.item(gid, "values")[0] == format(total, ",")
        kids = tab.tree.get_children(gid)
        assert [tab.tree.item(k, "values")[0] for k in kids] == \
            [format(f.count, ",") for f in members]
        assert [tab._item_code[k] for k in kids] == [f.code for f in members]

    # the biggest group first, and the biggest country inside it first
    assert "Asia" in tab.tree.item(groups[0], "text")
    assert "Japan" in tab.tree.item(tab.tree.get_children(groups[0])[0], "text")
    # every row starts unticked: an operator who wanted the whole world would not
    # be filtering, and a full picker would make the first click mean everything
    assert all(gt._TICK_OFF in tab.tree.item(g, "text") for g in groups)
    assert tab._sel_count == 0


def test_a_group_row_ticks_and_clears_its_whole_group(tab):
    _local(tab)
    _load(tab)
    europe = next(g for g in tab.tree.get_children("")
                  if "Europe" in tab.tree.item(g, "text"))

    tab._toggle_item(europe)
    assert tab._sel_codes == {"GBR", "DEU"}
    assert tab._sel_count == 8
    assert gt._TICK_ON in tab.tree.item(europe, "text")

    tab._toggle_item(tab.tree.get_children(europe)[0])          # untick one country
    assert tab._sel_codes == {"DEU"}
    assert gt._TICK_SOME in tab.tree.item(europe, "text"), \
        "a partly ticked group must not read as a full one"

    tab._toggle_item(europe)                                    # part-ticked -> all
    assert tab._sel_codes == {"GBR", "DEU"}
    tab._toggle_item(europe)                                    # all -> none
    assert tab._sel_codes == set()


def test_a_real_click_on_a_country_row_ticks_it(tab):
    """The click handler, through a real Button-1 event on a laid-out widget.

    This is not ceremony: the widget reports "Treeitem.indicator" for the INDENT
    of every row, childless ones included, so the first version of this handler --
    which vetoed that element to protect the expand/collapse triangle -- left the
    leading third of every country row dead. Only a row with children owns a
    triangle, and only a mapped widget can be clicked to prove it.
    """
    # A click needs real coordinates, and coordinates need a laid-out widget: the
    # tab's page frame is not packed by the fixture (the other tests do not need
    # it) and an unmapped Treeview answers bbox with nothing at all.
    tab.parent.pack(fill="both", expand=True)
    tab.root.geometry("900x900+40+40")
    tab.root.deiconify()
    _local(tab)
    _load(tab)
    tab.root.update()
    group = tab.tree.get_children("")[0]
    child = tab.tree.get_children(group)[0]
    box = tab.tree.bbox(child)
    if not box:
        pytest.skip("the tree was never laid out (no usable display)")

    # x inside the indent, where the widget claims an indicator that is not there
    tab.tree.event_generate("<Button-1>", x=box[0] + 20, y=box[1] + box[3] // 2)
    tab.root.update()
    assert tab._sel_codes == {tab._item_code[child]}
    assert gt._TICK_ON in tab.tree.item(child, "text")

    # and the group's real triangle collapses it instead of ticking everything
    gbox = tab.tree.bbox(group)
    tab.tree.event_generate("<Button-1>", x=gbox[0] + 6, y=gbox[1] + gbox[3] // 2)
    tab.root.update()
    assert tab._sel_codes == {tab._item_code[child]}, "the expander must not tick the group"


def test_the_filter_changes_what_is_drawn_and_not_what_is_ticked(tab):
    """The ticks live in a set of country codes; the tree is a drawing of it. A
    filter that held the state would untick whatever it hid."""
    _local(tab)
    _load(tab)
    tab._on_select_all()
    picked = set(tab._sel_codes)
    count = tab._sel_count

    tab.filter_var.set("germ")
    shown = [tab._item_code[k] for g in tab.tree.get_children("")
             for k in tab.tree.get_children(g)]
    assert shown == ["DEU"]
    assert tab._sel_codes == picked, "filtering must not change the selection"
    assert tab._sel_count == count

    tab.filter_var.set("")
    assert len(tab._sel_codes) == len(picked)


# ---- the write seam ---------------------------------------------------------
def test_the_write_worker_builds_the_plan_here_instead_of_downloading(tab, monkeypatch):
    """The one seam this feature adds: in local mode _write_worker encodes the
    plan itself and then calls engine.write_contacts exactly as the downloaded
    path does -- same expect_model, same held link, nothing fetched."""
    captured = {}

    def fake_write(port, plan, on_log, on_progress, abort=None, expect_model=None,
                   pace_ms=0, link=None):
        captured.update(plan=list(plan), port=port, model=expect_model, link=link)
        return {"blocks": seg.block_count(plan), "frames": 1, "seconds": 1.0, "retries": 0}

    def no_download(*_a, **_k):
        raise AssertionError("a locally built list must never fetch anything")

    monkeypatch.setattr(gt.engine, "write_contacts", fake_write)
    monkeypatch.setattr(gt.catalog, "download_artifact", no_download)

    _local(tab)
    store = _load(tab)
    held = object()
    tab._write_worker("COM9", "https://cps.aes.app", None, None, None, "D878UV", None, held,
                      local={"store": store, "spec": cb.radio_for_ident("D878UV"),
                             "codes": ["GBR", "DEU"], "count": 8, "nx": None,
                             "name": "user.csv"})
    _pump(tab)

    assert captured["model"] == "D878UV"
    assert captured["link"] is held, "the held PC-mode session must still be reused"
    # the same plan the builder produces for that selection, block for block
    want = cb.build_dmr_segments(store, "anytone_878", ["GBR", "DEU"])
    assert [(s.addr, s.data) for s in captured["plan"]] == [(s.addr, s.data) for s in want]
    assert "built on this computer" in tab.log.get("1.0", "end")
    assert "Done" in tab.wstatus.cget("text")


def test_a_builder_refusal_reaches_the_operator_word_for_word(tab, monkeypatch):
    """ContactBuildError messages are written for an operator, so the tab must
    show them as they are rather than wrap them in one of its own."""
    def boom(*_a, **_k):
        raise cb.ContactBuildError("radio ID 99999999 cannot be stored in this contact format")

    monkeypatch.setattr(gt.contact_build, "build_dmr_segments", boom)
    monkeypatch.setattr(gt.engine, "write_contacts",
                        lambda *a, **k: pytest.fail("nothing may be written after a refusal"))
    _local(tab)
    store = _load(tab)
    tab._write_worker("COM9", "https://cps.aes.app", None, None, None, "D878UV", None, None,
                      local={"store": store, "spec": cb.radio_for_ident("D878UV"),
                             "codes": ["GBR"], "count": 5, "nx": None, "name": "user.csv"})
    _pump(tab)
    assert "radio ID 99999999 cannot be stored" in tab.wstatus.cget("text")
    assert tab._writing is False


# ---------------------------------------------------------------------------
# The NXDN half, and the wheel
# ---------------------------------------------------------------------------

#: An NXDN list whose countries deliberately DISAGREE with the DMR one: Japan is
#: in both, Norway is NXDN-only (so it has to appear as a new row), and the
#: United Kingdom is DMR-only.
NX_ROWS = [{"RADIO_ID": 1, "COUNTRY": "Japan"}, {"RADIO_ID": 2, "COUNTRY": "Japan"},
           {"RADIO_ID": 3, "COUNTRY": "Norway"}, {"RADIO_ID": 4, "COUNTRY": "United States"}]


def _load_nx(tab, rows=None):
    tab.nx_path = "/tmp/nxdn.csv"
    tab._post("local_nx", NX_ROWS if rows is None else rows)
    _pump(tab)


def _row_counts(tab):
    """{country name: the number drawn beside it} for every leaf row."""
    out = {}
    for gid in tab.tree.get_children(""):
        for cid in tab.tree.get_children(gid):
            out[tab._item_name[cid]] = tab.tree.item(cid, "values")[0]
    return out


def test_the_picker_counts_both_lists_when_the_nxdn_half_is_included(tab):
    _local(tab)
    _load(tab)
    tab.spec = cb.radio_for_ident("D890UV")
    assert _row_counts(tab)["Japan"] == "50"

    _load_nx(tab)
    # Japan is 50 DMR + 2 NXDN, and Norway exists only in the NXDN list, so it
    # has to be tickable now.
    counts = _row_counts(tab)
    assert counts["Japan"] == "52"
    assert counts["Norway"] == "1"
    assert counts["United Kingdom"] == "5", "a DMR-only country is unchanged"

    # And the ORDER follows the combined number: Japan (52) still leads Asia.
    asia = next(m for key, _n, _t, m in tab._groups if key == "AS")
    assert [f.count for f in asia] == sorted([f.count for f in asia], reverse=True)


def test_unticking_include_puts_the_counts_back(tab):
    _local(tab)
    _load(tab)
    tab.spec = cb.radio_for_ident("D890UV")
    _load_nx(tab)
    assert _row_counts(tab)["Japan"] == "52"

    tab.nx_local_var.set(False)
    tab._on_nx_include()
    counts = _row_counts(tab)
    assert counts["Japan"] == "50"
    assert "Norway" not in counts, "an NXDN-only country has nothing to offer now"


def test_the_nxdn_half_is_filtered_by_the_same_countries(tab):
    _local(tab)
    _load(tab)
    _port(tab)          # explicit: this machine's real port list must not decide
    _connect(tab)                                  # an 890, which has an NXDN list
    _load_nx(tab)

    tab._select_codes({"JPN"})
    assert [r["RADIO_ID"] for r in tab._nx_selected()] == [1, 2], "only Japan's NXDN rows"

    tab._select_codes({"GBR"})
    assert tab._nx_selected() == [], "the DMR-only country keeps no NXDN row"
    assert "only the DMR list" in tab._local_gate()[1]

    # A radio with no NXDN list at all takes none of it, whatever is ticked.
    tab._select_codes({"JPN"})
    _connect(tab, "D878UV2")
    assert tab._nx_selected() == []
    assert not tab._include_nx()


def test_a_wheel_notch_moves_the_picker_by_rows_not_pages(tab, monkeypatch):
    _local(tab)
    _load(tab)
    moved = []
    monkeypatch.setattr(tab.tree, "yview_scroll", lambda n, what: moved.append((n, what)))

    class _Wheel:
        def __init__(self, delta):
            self.delta = delta
            self.num = 0

    # A single macOS notch arrives well under Tk's own divisor of 40. Tk's class
    # binding divides and truncates, so it scrolls NOTHING; this must move a row.
    tab._on_wheel(_Wheel(10))
    tab._on_wheel(_Wheel(-10))
    assert moved == [(-1, "units"), (1, "units")]

    # An accelerated burst is proportional, but capped: without the ceiling this
    # is the two-page jump.
    moved.clear()
    tab._on_wheel(_Wheel(400))
    assert moved == [(-gt._WHEEL_MAX_ROWS, "units")]

    # X11 has no delta at all; it sends button 4 and 5.
    moved.clear()
    ev = _Wheel(0)
    ev.num = 5
    tab._on_wheel(ev)
    assert moved == [(1, "units")]

    # And the handler always swallows the event, so Tk's own binding cannot
    # scroll a second time on top of ours.
    assert tab._on_wheel(_Wheel(10)) == "break"


def _pack(dx, dy):
    """A TouchpadScroll %D: two 16-bit deltas in one 32-bit value."""
    return (dx << 16) | (dy & 0xFFFF)


def test_a_trackpad_scrolls_the_picker_smoothly_and_never_by_pages(tab, monkeypatch):
    """The event a Mac actually sends. Tk 9 delivers trackpad and Magic Mouse
    scrolling as TouchpadScroll, not MouseWheel, and its own binding reads one
    event in five -- four gestures do nothing, the fifth jumps by its whole delta
    in rows. A MouseWheel handler never sees any of it."""
    _local(tab)
    _load(tab)
    moved = []
    monkeypatch.setattr(tab.tree, "yview_scroll", lambda n, what: moved.append(n))

    class _Pad:
        def __init__(self, packed):
            self.delta = packed

    # A slow drag: small deltas that Tk's every-fifth rule would mostly discard.
    # Here they accumulate, so five of them are worth a row rather than nothing.
    for _ in range(5):
        tab._on_touchpad(_Pad(_pack(0, 1)))
    assert moved == [-1], f"five small events should be one row, got {moved}"

    # A flick is capped, not a two-page jump.
    moved.clear()
    tab._on_touchpad(_Pad(_pack(0, 200)))
    assert moved == [-gt._WHEEL_MAX_ROWS]

    # Upwards is the other direction, and the leftover does not leak across it.
    moved.clear()
    tab._pad_accum = 0.0
    for _ in range(5):
        tab._on_touchpad(_Pad(_pack(0, -1)))
    assert moved == [1]

    # A purely horizontal gesture scrolls nothing vertically.
    moved.clear()
    tab._on_touchpad(_Pad(_pack(7, 0)))
    assert moved == []
    assert tab._on_touchpad(_Pad(_pack(0, 5))) == "break"


def test_the_scroll_bindings_are_actually_on_the_widget(tab):
    """Binding the wrong event is invisible: the list simply keeps its old
    behaviour, which is how the first attempt at this shipped. Generate the real
    events and see that OUR handlers run."""
    _local(tab)
    _load(tab)
    seen = []
    tab._on_wheel = lambda e: seen.append("wheel") or "break"
    tab._on_touchpad = lambda e: seen.append("pad") or "break"
    # Rebind so the lambdas above are what Tk calls.
    tab.tree.bind("<MouseWheel>", tab._on_wheel)
    try:
        tab.tree.bind("<TouchpadScroll>", tab._on_touchpad)
    except tk.TclError:
        pytest.skip("this Tk has no TouchpadScroll event")

    tab.tree.update_idletasks()
    tab.tree.event_generate("<MouseWheel>", delta=-40, when="now")
    tab.tree.event_generate("<TouchpadScroll>", delta=_pack(0, 3), when="now")
    tab.tree.update()
    assert seen == ["wheel", "pad"]


def test_the_continents_start_folded(tab):
    """A hundred and eighty countries open at once is not a list anyone can use.

    A filter is the exception: it has to show what it found.
    """
    _local(tab)
    _load(tab)
    groups = tab.tree.get_children("")
    assert groups, "there should be continents to fold"
    assert all(not tab.tree.item(g, "open") for g in groups)

    tab.filter_var.set("germ")
    assert all(tab.tree.item(g, "open") for g in tab.tree.get_children("")), \
        "a search must open what it kept"

    tab.filter_var.set("")
    assert all(not tab.tree.item(g, "open") for g in tab.tree.get_children(""))


def test_the_preview_line_breaks_the_two_databases_out(tab):
    _local(tab)
    _load(tab)
    _connect(tab)                                  # an 890: it has an NXDN list
    tab._select_codes({"JPN"})
    assert tab.sel_lbl.cget("text") == "50 contacts selected"

    _load_nx(tab)
    tab._select_codes({"JPN"})
    # 50 DMR + the two Japanese NXDN rows, said as two pools rather than one sum.
    assert tab.sel_lbl.cget("text") == "52 contacts selected (50 DMR + 2 NXDN)"

    # A country with no NXDN row of its own goes back to the plain form.
    tab._select_codes({"GBR"})
    assert tab.sel_lbl.cget("text") == "5 contacts selected"


def test_the_nxdn_ceiling_is_its_own_pool(tab, monkeypatch):
    """80,000 NXDN contacts and 500,000 DMR ones do not come out of one budget.

    And our own layout limit is lower than the radio's, which is a different
    refusal with a different reason.
    """
    _local(tab)
    _load(tab)
    _port(tab)
    _connect(tab)
    _load_nx(tab)
    tab._select_codes({"JPN"})
    assert tab.spec.capacity == 500000 and tab.spec.nx_capacity == 80000

    # More NXDN rows than the radio holds: the DMR half is untouched by it.
    monkeypatch.setattr(tab, "_nx_selected", lambda: [{"RADIO_ID": 1}] * 90000)
    ok, msg, _colour = tab._local_gate()
    assert not ok
    assert "90,000 NXDN contacts is more than the 80,000" in msg
    assert "DMR list has its own room" in msg

    # Between our layout ceiling and the radio's: refused, and it says whose
    # limit is whose.
    monkeypatch.setattr(tab, "_nx_selected", lambda: [{"RADIO_ID": 1}] * 70000)
    ok, msg, _colour = tab._local_gate()
    assert not ok
    assert "this app can lay out safely" in msg and "80,000" in msg


def test_the_write_status_recovers_when_the_nxdn_read_finishes(tab):
    """The gate says "Reading the file…" while a read is running. Something has
    to take it back down when the read lands.

    The bug this pins: choosing an nxdn.csv left the write status stuck on
    "Reading the file…" long after the file was read and the picker had redrawn,
    until the operator happened to click something that refreshed it. The handler
    cleared _nx_reading and repainted the picker, but nothing repaints the two
    lines below it on its own -- and every OTHER read completion (the DMR store,
    both errors) ends by refreshing them. Only the NXDN success path did not.

    A short file makes it worse, not better: the read is over before the operator
    moves the mouse, so the stale line is all they see.
    """
    _local(tab)
    _load(tab)
    _port(tab)
    _connect(tab)                                  # an 890: it has an NXDN list
    tab._select_codes({"JPN"})
    assert "Ready" in tab.fit_lbl.cget("text")

    # Exactly what _on_browse_nx does before it starts its worker.
    tab.nx_path = "/tmp/nxdn.csv"
    tab._nx_reading = True
    tab._refresh_local_summary()
    assert "Reading the file" in tab.fit_lbl.cget("text")

    tab._post("local_nx", NX_ROWS)
    _pump(tab)

    assert "Reading the file" not in tab.fit_lbl.cget("text"), \
        "the gate is still reporting a read that finished"
    assert "Ready" in tab.fit_lbl.cget("text")
    # And the running total counts the half that just arrived, without waiting
    # for a click either.
    assert tab.sel_lbl.cget("text") == "52 contacts selected (50 DMR + 2 NXDN)"
