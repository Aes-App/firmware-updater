"""What the window does with the version check's answer.

The check itself is pinned in tests/test_update_check.py. What is pinned here is
the wiring around it, which has one rule worth a test: the header may only ever
grow a banner when there IS a newer release. A failed check must leave the header
exactly as it was -- an operator who cannot reach GitHub does not need a line
about it on every launch, and a banner that appeared on failure would send people
to a releases page for no reason.

The About dialog is the opposite: it was ASKED, so it answers in every case,
including the failures, and it never shows a failure in the colour of success.

Needs Tk and the app's own dependencies; skips without either. The check is
never actually run -- _check_for_updates is replaced by one that answers at once.

Run:  python -m pytest tests/test_update_banner.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tk = pytest.importorskip("tkinter")
pytest.importorskip("bleak")            # bt_ota.gui pulls in the BLE client

from tkinter import ttk                                          # noqa: E402

from bt_ota import gui                                           # noqa: E402
from bt_ota import update_check as uc                            # noqa: E402


@pytest.fixture()
def root():
    try:
        r = tk.Tk()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no Tk display: {e}")
    r.withdraw()
    yield r
    try:
        r.destroy()
    except Exception:  # noqa: BLE001
        pass


def _answer(monkeypatch, result):
    """Make _check_for_updates hand back `result` immediately, on this thread."""
    monkeypatch.setattr(gui, "_check_for_updates",
                        lambda root, on_done: on_done(result))


def _res(**kw):
    base = dict(ok=True, newer=False, current="0.9.0", latest="0.9.0",
                url=uc.RELEASES_URL, message="You are running the newest release (0.9.0).")
    base.update(kw)
    return uc.Result(**base)


def _links(widget):
    return [w for w in widget.winfo_children()
            if isinstance(w, ttk.Label) and str(w.cget("text")).strip()]


# -- the header banner -------------------------------------------------------
def test_banner_appears_only_when_there_is_a_newer_release(root, monkeypatch):
    monkeypatch.delenv(uc.OPT_OUT_ENV, raising=False)
    _answer(monkeypatch, _res(newer=True, latest="1.0.0",
                              url="https://github.com/Aes-App/firmware-updater/releases/tag/v1.0.0",
                              message="Version 1.0.0 is available — you are running 0.9.0."))
    header = ttk.Frame(root)
    gui._install_update_banner(root, header)
    holder = header.winfo_children()[0]
    labels = _links(holder)
    assert len(labels) == 1
    assert "1.0.0" in labels[0].cget("text")


@pytest.mark.parametrize("result", [
    _res(),                                                    # up to date
    _res(ok=False, message="Could not reach GitHub."),         # offline
])
def test_header_stays_bare_when_there_is_nothing_to_say(root, monkeypatch, result):
    monkeypatch.delenv(uc.OPT_OUT_ENV, raising=False)
    _answer(monkeypatch, result)
    header = ttk.Frame(root)
    gui._install_update_banner(root, header)
    holder = header.winfo_children()[0]
    assert _links(holder) == []


def test_opting_out_does_not_even_build_the_holder(root, monkeypatch):
    monkeypatch.setenv(uc.OPT_OUT_ENV, "1")
    called = []
    monkeypatch.setattr(gui, "_check_for_updates", lambda *a: called.append(a))
    header = ttk.Frame(root)
    gui._install_update_banner(root, header)
    assert header.winfo_children() == []
    assert called == [], "the opt-out has to stop the request, not just the banner"


# -- the About dialog --------------------------------------------------------
def _about_widgets(root):
    gui.show_about(root)
    dlg = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)][-1]
    btn = next(w for w in dlg.winfo_children()[1].winfo_children()[0].winfo_children()
               if isinstance(w, ttk.Button) and w.cget("text") == "Check for updates")
    info = dlg.winfo_children()[1]
    status = [w for w in info.winfo_children() if isinstance(w, ttk.Label)][0]
    holder = [w for w in info.winfo_children() if isinstance(w, ttk.Frame)][-1]
    return dlg, btn, status, holder


def test_about_starts_silent_and_answers_when_asked(root, monkeypatch):
    _answer(monkeypatch, _res())
    dlg, btn, status, holder = _about_widgets(root)
    assert status.cget("text") == "", "About must not report before anyone asks"
    btn.invoke()
    assert "newest release" in status.cget("text")
    assert str(status.cget("foreground")) == "#127a2e"
    assert _links(holder) == [], "no link to chase when there is nothing newer"
    dlg.destroy()


def test_about_offers_the_release_page_when_there_is_one(root, monkeypatch):
    _answer(monkeypatch, _res(newer=True, latest="1.0.0", message="Version 1.0.0 is available."))
    dlg, btn, status, holder = _about_widgets(root)
    btn.invoke()
    assert "1.0.0" in status.cget("text")
    assert len(_links(holder)) == 1
    dlg.destroy()


def test_about_never_paints_a_failure_as_success(root, monkeypatch):
    _answer(monkeypatch, _res(ok=False, message="Could not reach GitHub."))
    dlg, btn, status, holder = _about_widgets(root)
    btn.invoke()
    assert str(status.cget("foreground")) == "#b00020"
    # A failed check still gives somewhere to look, because the operator asked.
    assert len(_links(holder)) == 1
    dlg.destroy()
