from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tk = pytest.importorskip("tkinter")

from radio_fw import compiler, spec, webcps
from radio_fw.vendor import fwupd_cps

ANSWER = {"schema": 1, "radios": [
    {"model": "anytone_d878uv2", "fwupdModel": "d878uv2", "name": "Anytone AT-D878UVII",
     "releases": [{"key": "4.00", "label": "4.00", "firmware": ["4.00", "4.01a"]}]},
    {"model": "anytone_d878uv", "fwupdModel": "d878uv", "name": "Anytone AT-D878UV",
     "releases": [{"key": "4.00", "label": "4.00", "firmware": ["4.00", "4.01a"]}]},
    {"model": "anytone_d890", "fwupdModel": "d890", "name": "Anytone AT-D890UV",
     "releases": [{"key": "1.05", "label": "1.05", "firmware": ["1.05"]},
                  {"key": "1.04", "label": "1.04", "firmware": ["1.04"]}]},
    {"model": "anytone_d578uv2", "fwupdModel": "d578uv2", "name": "Anytone AT-D578UV(III) Plus",
     "releases": [{"key": "2.09", "label": "2.09 (CPS 1.22)", "firmware": ["2.09"]},
                  {"key": "2.08", "label": "2.08 (CPS 1.21)", "firmware": ["2.08"]}]},
    {"model": "anytone_d168uv", "fwupdModel": "d168", "name": "Anytone AT-D168UV",
     "releases": [{"key": "1.07", "label": "1.07", "firmware": ["1.07"]}]},
]}


ANYTONE = bytes(40) + b"D890UV\0A-BORD2" + bytes(40)
BTECH = bytes(29) + b"DMR-7X2\0A-BORD2" + bytes(40)


def _firmware(image, kind=spec.KIND_FW):
    frames = b"".join(fwupd_cps.build_frame(0x0800C000 + at, image[at:at + 32].ljust(32, b"\0"))
                      for at in range(0, len(image), 32))
    return compiler.CompileResult(kind=kind, artifact=frames, manifest={}, source_names=["x"])


def test_what_webcps_supports_for_each_radio():
    assert webcps.coverage(ANSWER, "d890") == webcps.Coverage(("1.04", "1.05"), True)
    assert webcps.coverage(ANSWER, "d578uv2") == webcps.Coverage(("2.08", "2.09"), True)
    assert webcps.coverage(ANSWER, "d878uv2") == webcps.Coverage(("4.00", "4.01a"), False)
    assert webcps.coverage(ANSWER, "d878uv") == webcps.Coverage(("4.00", "4.01a"), False)
    assert webcps.coverage(ANSWER, "d168") == webcps.Coverage(("1.07",), False)
    for model in ("d578uv", "bt01", "no-such-radio"):
        assert webcps.coverage(ANSWER, model) is None, model


@pytest.mark.parametrize("answer", [
    None, {}, [], "radios", {"radios": "d890"}, {"radios": [None, 3, "d890"]},
    {"radios": [{"fwupdModel": "d890"}]},
    {"radios": [{"fwupdModel": "d890", "releases": "1.05"}]},
    {"radios": [{"fwupdModel": "d890", "releases": [None, {"firmware": "1.05"}, {"firmware": [5, " "]}]}]},
])
def test_an_answer_of_another_shape_counts_as_none(answer):
    assert webcps.coverage(answer, "d890") is None


def test_versions_are_put_in_order_by_their_numbers():
    answer = {"radios": [{"fwupdModel": "d890", "releases": [
        {"firmware": ["2.10"]}, {"firmware": ["2.9", "2.10"]}, {"firmware": ["2.09a"]}]}]}
    assert webcps.coverage(answer, "d890").versions == ("2.9", "2.09a", "2.10")


def test_the_version_a_bundle_installs():
    assert webcps.target_version({"cpsVersion": "1.05-NX-FR"}) == "1.05"
    assert webcps.target_version({"cpsVersion": "2.09-GD-M"}) == "2.09"
    assert webcps.target_version({"cpsVersion": "4.01a"}) == "4.01a"
    for bundle in (None, {}, {"cpsVersion": None}, {"cpsVersion": ""}, {"cpsVersion": "-NX"}):
        assert webcps.target_version(bundle) is None, bundle


def test_webcps_is_suggested_only_within_its_versions():
    d890 = webcps.coverage(ANSWER, "d890")
    assert webcps.applies(d890, "1.05") and webcps.applies(d890, "1.04")
    assert webcps.applies(d890, None), "the operator's own files: the version is not known"
    assert not webcps.applies(d890, "1.03"), "WebCPS does not read or write 1.03"
    d878 = webcps.coverage(ANSWER, "d878uv2")
    assert webcps.applies(d878, "4.01a") and webcps.applies(d878, "4.01A")
    assert not webcps.applies(d878, "3.08N")
    assert not webcps.applies(webcps.coverage(ANSWER, "d168"), "1.07"), \
        "one version: no update to make within it"
    assert not webcps.applies(None, None)


def test_btech_firmware_is_told_from_anytone_by_the_radio_its_image_names():
    btech = _firmware(BTECH)
    assert b"DMR-7X2" not in btech.artifact, "a frame's checksum and address split it"
    assert compiler.written_data(btech).startswith(BTECH)
    assert not webcps.supports_firmware(btech)
    assert webcps.supports_firmware(_firmware(ANYTONE))
    nr = compiler.CompileResult(kind=spec.KIND_NR, artifact=BTECH, manifest={}, source_names=["x"])
    assert compiler.written_data(nr) == b"", "only the CPS protocol's frames are read"


def test_the_dialog_names_the_radio_webcps_supports():
    assert webcps.radio_name("d890") == "D890UV", "not the BTECH-7X2 the tab's name also covers"
    assert webcps.radio_name("d578uv2") == spec.model_label("d578uv2")


def test_the_wording_names_the_versions_and_the_step_to_convert():
    d890 = webcps.coverage(ANSWER, "d890")
    known = webcps.wording("D890UV", d890, "1.05")
    assert known["lead"] == ("AesApp WebCPS supports the D890UV on firmware 1.04 and 1.05 and "
                             "converts a codeplug between them for free. If this radio runs one "
                             "of these now:")
    assert len(known["steps"]) == 4
    assert known["steps"][1].startswith("If that is not 1.05, choose 1.05 in the firmware menu")
    assert known["other"].startswith("Radio on another version?")

    unknown = webcps.wording("D890UV", d890, None)
    assert unknown["lead"].endswith("If you are updating from one of these versions to another:")
    assert unknown["steps"][1].startswith("Choose the firmware you are installing")
    assert unknown["other"].startswith("Updating from or to another version?")

    d878 = webcps.wording("D878UVII", webcps.coverage(ANSWER, "d878uv2"), "4.01a")
    assert "on firmware 4.00 and 4.01a, which keep the same codeplug." in d878["lead"]
    assert len(d878["steps"]) == 3, "one codeplug format: nothing to convert"
    assert not any("firmware menu" in step for step in d878["steps"])


def _root():
    try:
        root = tk.Tk()
    except Exception as e:
        pytest.skip(f"no Tk display: {e}")
    try:
        root.attributes("-alpha", 0.0)
    except tk.TclError:
        pass
    root.geometry("980x800+20+20")
    return root


def _settle(root):
    for _ in range(6):
        root.update_idletasks()
        root.update()


def _press(root, text):
    todo = [root]
    while todo:
        w = todo.pop()
        try:
            if w.winfo_class() in ("TButton", "Button") and w.cget("text") == text:
                w.invoke()
                return
        except tk.TclError:
            pass
        todo += w.winfo_children()
    raise AssertionError("no button " + repr(text))


def test_the_dialog_starts_only_when_told_the_codeplug_is_backed_up(monkeypatch):
    root = _root()
    try:
        cov = webcps.coverage(ANSWER, "d890")
        dialog = webcps.BackupDialog(root, "D890UV", cov, "1.05")
        _settle(root)
        assert dialog.win.title() == webcps.TITLE
        assert [s.cget("text") for s in dialog.steps] == webcps.wording("D890UV", cov, "1.05")["steps"]
        opened = []
        monkeypatch.setattr(webcps.webbrowser, "open", opened.append)
        dialog.open_btn.event_generate("<Button-1>", x=4, y=4)
        assert opened == [webcps.URL] and "utm_campaign=firmware-backup" in webcps.URL
        assert dialog.win.winfo_exists(), "opening WebCPS leaves the question open"
        dialog.cancel_btn.invoke()
        assert dialog.result is False and not dialog.win.winfo_exists()

        dialog = webcps.BackupDialog(root, "D890UV", cov, None)
        _settle(root)
        dialog.win.event_generate("<Escape>")
        assert dialog.result is False and not dialog.win.winfo_exists()

        root.after(100, lambda: _press(root, webcps.START))
        assert webcps.ask_backup(root, "D890UV", cov, "1.05") is True
        root.after(100, lambda: _press(root, "Cancel"))
        assert webcps.ask_backup(root, "D890UV", cov, "1.05") is False
    finally:
        root.destroy()


def test_the_dialog_fits_on_a_small_screen():
    root = _root()
    try:
        dialog = webcps.BackupDialog(root, "D578UV(III) Plus", webcps.coverage(ANSWER, "d578uv2"), None)
        _settle(root)
        assert dialog.win.winfo_reqheight() <= 700, "it must fit a 768-pixel-high laptop screen"
        assert dialog.win.winfo_reqwidth() <= 760
        dialog.close()
    finally:
        root.destroy()


def _tab():
    pytest.importorskip("serial")
    from radio_fw import gui_tab
    root = _root()
    page = tk.Frame(root)
    page.pack(fill="both", expand=True)
    return root, gui_tab.RadioBoardsTab(page, root)


def _server_batch(tab, version_label, image=ANYTONE):
    tab._webcps = ANSWER
    tab.model = "d890"
    tab.source_var.set("server")
    tab._version_map = {"1.05 DMR": {"fwupdModel": "d890", "cpsVersion": "1.05"},
                        "1.05 NX&DMR Beta - French": {"fwupdModel": "d890", "cpsVersion": "1.05-NX-FR"},
                        "1.03 DMR": {"fwupdModel": "d890", "cpsVersion": "1.03"}}
    tab._fetched_label = version_label
    tab.plan = [_firmware(image)]


def test_a_firmware_batch_within_webcps_versions_asks_with_the_webcps_way(monkeypatch):
    from radio_fw import gui_tab
    root, tab = _tab()
    try:
        asked, plain = [], []
        monkeypatch.setattr(gui_tab._webcps, "ask_backup",
                            lambda _root, radio, cov, target: asked.append((radio, cov.versions, target)) or False)
        monkeypatch.setattr(gui_tab.messagebox, "askyesno", lambda *a, **k: plain.append(a[0]) or False)
        _server_batch(tab, "1.05 NX&DMR Beta - French")
        tab._begin_radio()
        assert asked == [("D890UV", ("1.04", "1.05"), "1.05")] and plain == []
        assert not tab.wizard.winfo_manager(), "Cancel stops before the walk"

        _server_batch(tab, "1.03 DMR")
        tab._begin_radio()
        assert len(asked) == 1 and plain == ["Back up your codeplug first"], \
            "WebCPS does not support 1.03: the plain question"

        _server_batch(tab, "1.05 DMR")
        tab._webcps = None
        tab._begin_radio()
        assert len(asked) == 1 and len(plain) == 2, "no answer from the server: the plain question"

        _server_batch(tab, None)
        tab.source_var.set("local")
        tab._begin_radio()
        assert asked[-1] == ("D890UV", ("1.04", "1.05"), None), "own files: the version is not known"
    finally:
        root.destroy()


def test_btech_firmware_gets_the_plain_question(monkeypatch):
    from radio_fw import gui_tab
    root, tab = _tab()
    try:
        asked, plain = [], []
        monkeypatch.setattr(gui_tab._webcps, "ask_backup", lambda *a: asked.append(a) or True)
        monkeypatch.setattr(gui_tab.messagebox, "askyesno", lambda *a, **k: plain.append(a[0]) or True)
        _server_batch(tab, None, BTECH)
        tab.source_var.set("local")
        tab._begin_radio()
        assert asked == [] and plain == ["Back up your codeplug first"]
        assert tab.wizard.winfo_manager() and not tab._webcps_offered, "and no WebCPS note at the end"

        _server_batch(tab, "1.05 DMR", BTECH)
        tab._begin_radio()
        assert asked == [] and len(plain) == 2, "whichever way the files came"
    finally:
        root.destroy()


def test_a_batch_without_the_radio_firmware_asks_nothing(monkeypatch):
    from radio_fw import gui_tab
    root, tab = _tab()
    try:
        asked, plain = [], []
        monkeypatch.setattr(gui_tab._webcps, "ask_backup", lambda *a: asked.append(a) or True)
        monkeypatch.setattr(gui_tab.messagebox, "askyesno", lambda *a, **k: plain.append(a[0]) or True)
        tab._webcps = ANSWER
        rows = {row.kind: row for row in tab.rows}
        rows[spec.KIND_FW].apply_compile(_firmware(ANYTONE), None)
        rows[spec.KIND_ICON].apply_compile(_firmware(ANYTONE, spec.KIND_ICON), None)
        rows[spec.KIND_FW].checked.set(False)
        tab._start()
        assert [p.kind for p in tab.plan] == [spec.KIND_ICON]
        assert asked == [] and plain == [] and tab.wizard.winfo_manager()
    finally:
        root.destroy()


def test_the_finished_view_says_where_to_restore_after_the_webcps_way(monkeypatch):
    from radio_fw import gui_tab
    root, tab = _tab()
    try:
        monkeypatch.setattr(gui_tab._webcps, "ask_backup", lambda *a: True)
        monkeypatch.setattr(gui_tab.messagebox, "askyesno", lambda *a, **k: True)
        _server_batch(tab, "1.05 DMR")
        tab._begin_radio()
        assert tab.wizard.winfo_manager() and tab._webcps_offered
        tab.results = [{"kind": spec.KIND_FW, "label": "Radio Firmware", "state": "done"}]
        tab._finish_radio()
        _settle(root)
        assert tab.mcu_box.winfo_ismapped() and tab.webcps_note.winfo_ismapped()
        assert tab.webcps_note_lbl.cget("text") == webcps.RESTORE_NOTE
        opened = []
        monkeypatch.setattr(gui_tab.webbrowser, "open", opened.append)
        tab.webcps_link.event_generate("<Button-1>", x=2, y=2)
        assert opened == [webcps.URL]

        _server_batch(tab, "1.03 DMR")
        tab._begin_radio()
        assert not tab._webcps_offered
        tab.results = [{"kind": spec.KIND_FW, "label": "Radio Firmware", "state": "done"}]
        tab._finish_radio()
        _settle(root)
        assert tab.mcu_box.winfo_ismapped() and not tab.webcps_note.winfo_ismapped()
    finally:
        root.destroy()
