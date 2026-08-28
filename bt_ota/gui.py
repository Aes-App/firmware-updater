"""Tkinter GUI for the AnyTone Bluetooth-module OTA updater (by AesApp Inc.).

Scan for radios, pick one and a .ufw file, then Connect & Write. The BLE work
runs on a background asyncio loop; UI updates are marshalled back to the Tk
thread through a queue.

Run from source:  python -m bt_ota gui
"""
from __future__ import annotations

import asyncio
import faulthandler
import os
import queue
import sys
import threading
import traceback
import tkinter as tk
import webbrowser
from tkinter import filedialog, font as tkfont, messagebox, scrolledtext, ttk

from .client import MODELS, firmware_kind, make_client, scan_devices

APP_TITLE = "AesApp Radio Updater"
VENDOR = "AesApp Inc."
WEBSITE = "https://aes.app/"
VERSION = "0.6.0"
LOG_PREFIX = "[aesapp]"

DISCLAIMER_VERSION = "3"
DISCLAIMER = (
    "AesApp Radio Updater — Disclaimer & Terms of Use\n\n"
    "This software is provided by AesApp Inc. \"AS IS\" and \"AS AVAILABLE\", without "
    "warranty of any kind, express or implied, including but not limited to the implied "
    "warranties of merchantability, fitness for a particular purpose, and non-infringement.\n\n"
    "Updating the firmware of a radio is inherently risky and may render the device — or "
    "any of its internal components — temporarily or permanently inoperable. This app can "
    "update a radio's Bluetooth module, its main firmware, its baseband DSP, its "
    "noise-reduction board, and its icon/font storage. The board- and firmware-level "
    "updates are NOT verified or read back by the radio: a wrong, incomplete, or interrupted "
    "write is only discovered when the radio is switched on, and can leave it unable to "
    "boot. You use this software entirely at your own risk.\n\n"
    "To the fullest extent permitted by law, AesApp Inc. and its directors, employees, and "
    "contributors shall not be liable for any direct, indirect, incidental, special, "
    "consequential, or exemplary damages — including but not limited to damaged, bricked, "
    "or malfunctioning hardware, loss of data, or loss of use — arising out of or in any "
    "way connected with the use of, or inability to use, this software, even if advised of "
    "the possibility of such damages.\n\n"
    "You alone are responsible for: using update files intended for your exact device "
    "model; keeping the radio powered, connected, and undisturbed throughout an update; and "
    "complying with all applicable laws, regulations, and manufacturer terms.\n\n"
    "TRADEMARKS & NON-AFFILIATION. \"AnyTone\" is a trademark of Qixiang Electron Science "
    "& Technology Co., Ltd. \"JieLi\" (杰理) is a trademark of Zhuhai Jieli Technology Co., "
    "Ltd. \"Cypress\" and \"WICED\" are trademarks of Infineon Technologies AG. \"SiCOMM\" "
    "is a trademark of its respective owner. All trademarks are the property of their "
    "respective owners and are used for identification only. This software is an "
    "independent, unofficial tool by AesApp Inc. and is not affiliated with, authorized, "
    "endorsed, or sponsored by any of these companies. See \"Third-Party Notices\" (in "
    "About) for open-source attributions.\n\n"
    "By choosing \"Agree & Continue\" you acknowledge that you have read and understood "
    "this disclaimer and accept all risk and responsibility for your use of this software."
)


# ---- assets / small helpers -------------------------------------------------
def _asset_path(name: str) -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (here, getattr(sys, "_MEIPASS", None)):
        if not base:
            continue
        for cand in (os.path.join(base, "assets", name),
                     os.path.join(base, "bt_ota", "assets", name)):
            if os.path.exists(cand):
                return cand
    return os.path.join(here, "assets", name)


def _load_logo(small: bool = False) -> tk.PhotoImage | None:
    # Two pre-rendered sizes (LANCZOS); never subsample at runtime (jaggy).
    for name in ((("aesapp_logo_sm.png",) if small else ()) + ("aesapp_logo.png",)):
        try:
            return tk.PhotoImage(file=_asset_path(name))
        except Exception:
            continue
    return None


def _set_window_icon(root) -> None:
    """Set the taskbar/titlebar icon to the AesApp mark; Tk shows its own blue
    feather otherwise. iconphoto is cross-platform (the .app's .icns drives the
    mac Dock, so this is mainly for Windows/Linux)."""
    try:
        img = tk.PhotoImage(file=_asset_path("AesApp_icon.png"))
        root.iconphoto(True, img)
        root._app_icon = img  # keep a reference so Tk doesn't garbage-collect it
    except Exception:
        pass


def _config_dir() -> str:
    if sys.platform == "darwin":
        d = os.path.expanduser("~/Library/Application Support/AesApp Radio Updater")
    else:
        d = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
                         "aesapp-radio-updater")
    os.makedirs(d, exist_ok=True)
    return d


def _has_accepted() -> bool:
    try:
        with open(os.path.join(_config_dir(), "eula_accepted"), encoding="utf-8") as f:
            return f.read().strip() == DISCLAIMER_VERSION
    except OSError:
        return False


def _mark_accepted() -> None:
    try:
        with open(os.path.join(_config_dir(), "eula_accepted"), "w", encoding="utf-8") as f:
            f.write(DISCLAIMER_VERSION)
    except OSError:
        pass


def _link_label(parent, text=WEBSITE, url=WEBSITE):
    # "pointinghand" is a macOS-only Tk cursor; "hand2" is the portable X11 name.
    hand = "pointinghand" if sys.platform == "darwin" else "hand2"
    # Match the surrounding small UI font. ttk.Label.cget("font") is empty (ttk uses
    # styles), so deriving from it fell back to an oversized generic font.
    f = tkfont.Font(font=tkfont.nametofont("TkDefaultFont"))
    f.configure(underline=True)
    lbl = ttk.Label(parent, text=text, foreground="#0b5fff", cursor=hand, font=f)
    lbl.bind("<Button-1>", lambda _e: webbrowser.open(url))
    return lbl


# ---- disclaimer gate + about ------------------------------------------------
def _brand_header(parent, small=False):
    row = ttk.Frame(parent)
    logo = _load_logo(small=small)
    if logo is not None:
        lab = ttk.Label(row, image=logo)
        lab.image = logo  # keep a reference
        lab.pack(side="left", padx=(0, 12))
    txt = ttk.Frame(row)
    txt.pack(side="left", anchor="w")
    ttk.Label(txt, text=APP_TITLE, font=("", 15, "bold")).pack(anchor="w")
    sub = ttk.Frame(txt)
    sub.pack(anchor="w")
    ttk.Label(sub, text=f"by {VENDOR}   ").pack(side="left")
    _link_label(sub).pack(side="left")
    return row


def run_disclaimer_gate(root) -> bool:
    """Show the disclaimer *inside* the root window; return True if agreed.

    Rendered in the root (not a Toplevel of a withdrawn root, which can fail to
    map on macOS and leave the app with no visible window).
    """
    if _has_accepted():
        return True
    frame = ttk.Frame(root)
    frame.pack(fill="both", expand=True)
    _brand_header(frame).pack(fill="x", padx=16, pady=(16, 8))

    body = scrolledtext.ScrolledText(frame, width=76, height=16, wrap="word")
    body.pack(fill="both", expand=True, padx=16)
    body.insert("1.0", DISCLAIMER)
    body.configure(state="disabled")

    agree_var = tk.BooleanVar(value=False)
    done = tk.BooleanVar(value=False)
    result = {"ok": False}
    foot = ttk.Frame(frame)
    foot.pack(fill="x", padx=16, pady=12)
    agree_btn = ttk.Button(
        foot, text="Agree & Continue", state="disabled",
        command=lambda: (_mark_accepted(), result.update(ok=True), done.set(True)),
    )
    ttk.Checkbutton(
        foot, text="I have read and agree to the disclaimer above",
        variable=agree_var,
        command=lambda: agree_btn.configure(state="normal" if agree_var.get() else "disabled"),
    ).pack(side="left")
    agree_btn.pack(side="right")
    ttk.Button(foot, text="Decline & Quit",
               command=lambda: (result.update(ok=False), done.set(True))).pack(side="right", padx=(0, 8))

    root.update_idletasks()
    _bring_to_front(root)
    root.wait_variable(done)
    frame.destroy()
    return result["ok"]


def _bring_to_front(root):
    try:
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        root.after(800, lambda: root.attributes("-topmost", False))
        root.focus_force()
    except tk.TclError:
        pass


def show_about(root):
    dlg = tk.Toplevel(root)
    dlg.title(f"About {APP_TITLE}")
    dlg.transient(root)
    dlg.resizable(False, False)
    _brand_header(dlg).pack(fill="x", padx=16, pady=16)
    info = ttk.Frame(dlg)
    info.pack(fill="x", padx=16)
    ttk.Label(info, text=f"Version {VERSION}").pack(anchor="w")
    ttk.Label(info, text=f"© {VENDOR}. All rights reserved.").pack(anchor="w", pady=(2, 0))
    foot = ttk.Frame(dlg)
    foot.pack(fill="x", padx=16, pady=14)
    ttk.Button(foot, text="Disclaimer",
               command=lambda: _view_text(dlg, "Disclaimer", DISCLAIMER)).pack(side="left")
    ttk.Button(foot, text="Third-Party Notices",
               command=lambda: _view_text(dlg, "Third-Party Notices", _notices_text())).pack(side="left", padx=(8, 0))
    ttk.Button(foot, text="Close", command=dlg.destroy).pack(side="right")
    dlg.grab_set()


def _notices_text() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (here, getattr(sys, "_MEIPASS", None)):
        if not base:
            continue
        for cand in (os.path.join(base, "THIRD_PARTY_NOTICES.txt"),
                     os.path.join(base, "bt_ota", "THIRD_PARTY_NOTICES.txt")):
            if os.path.exists(cand):
                try:
                    with open(cand, encoding="utf-8") as f:
                        return f.read()
                except OSError:
                    pass
    return "Third-party notices file not found in this build."


def _view_text(parent, title, text):
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    dlg.transient(parent)
    t = scrolledtext.ScrolledText(dlg, width=74, height=16, wrap="word")
    t.pack(fill="both", expand=True, padx=12, pady=12)
    t.insert("1.0", text)
    t.configure(state="disabled")
    ttk.Button(dlg, text="Close", command=dlg.destroy).pack(pady=(0, 12))
    dlg.grab_set()


# ---- async loop bridge ------------------------------------------------------
class _AsyncLoop:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


# ---- main window ------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk, container: tk.Widget | None = None):
        self.root = root
        # `container` is the widget the UI is parented on: the root when this is
        # the whole window, or a Notebook page frame when it is one tab. Window-
        # level setup (title/minsize/close protocol) is left to main() in the
        # tabbed case so the two tabs share one close handler.
        self.ui = container if container is not None else root
        self._standalone = container is None
        if self._standalone:
            self.root.title(APP_TITLE)
            self.root.minsize(640, 500)

        self._aio = _AsyncLoop()
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._devices: list = []
        self._busy = False

        self._build_ui()
        if os.environ.get("BT_OTA_DEBUG"):
            self._append_log(f"{LOG_PREFIX} debug logging on — logs at {_config_dir()}")
        if self._standalone:
            self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._drain)

    def _build_ui(self):
        pad = dict(padx=8, pady=6)

        # The brand header + About live here only when this App is the whole
        # window; in the tabbed layout main() puts one shared header above the
        # Notebook so both tabs carry it.
        if self._standalone:
            header = ttk.Frame(self.ui)
            header.pack(fill="x", **pad)
            _brand_header(header, small=True).pack(side="left")
            ttk.Button(header, text="About", command=lambda: show_about(self.root)).pack(side="right")
            ttk.Separator(self.ui).pack(fill="x", padx=8)

        # Step 1: model
        model = ttk.Frame(self.ui)
        model.pack(fill="x", **pad)
        ttk.Label(model, text="1. Model:").pack(side="left")
        self.model_var = tk.StringVar(value="jieli")
        for kind, label in MODELS.items():
            ttk.Radiobutton(model, text=label, value=kind, variable=self.model_var,
                            command=self._on_model_change).pack(side="left", padx=(8, 0))

        top = ttk.Frame(self.ui)
        top.pack(fill="x", **pad)
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="2. Radio:").grid(row=0, column=0, sticky="w")
        self.device_var = tk.StringVar()
        self.device_box = ttk.Combobox(top, textvariable=self.device_var, state="readonly")
        self.device_box.grid(row=0, column=1, sticky="ew", padx=6)
        self.scan_btn = ttk.Button(top, text="Scan", command=self.on_scan)
        self.scan_btn.grid(row=0, column=2, sticky="e")

        ttk.Label(top, text="3. Firmware:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.file_var = tk.StringVar()
        self.file_entry = ttk.Entry(top, textvariable=self.file_var)
        self.file_entry.grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        self.browse_btn = ttk.Button(top, text="Browse…", command=self.on_browse)
        self.browse_btn.grid(row=1, column=2, sticky="e", pady=(6, 0))

        action = ttk.Frame(self.ui)
        action.pack(fill="x", **pad)
        action.columnconfigure(1, weight=1)
        self.write_btn = ttk.Button(action, text="4. Connect & Write", command=self.on_write)
        self.write_btn.grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(action, mode="determinate", maximum=100.0)
        self.progress.grid(row=0, column=1, sticky="ew", padx=(10, 0))

        ttk.Label(self.ui, text="Log").pack(anchor="w", padx=8)
        self.log = scrolledtext.ScrolledText(self.ui, height=15, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        self.status_var = tk.StringVar(value="1. Select your model  2. Scan and pick the radio  3. Choose firmware  4. Connect & Write")
        ttk.Label(self.ui, textvariable=self.status_var, relief="sunken", anchor="w").pack(
            fill="x", side="bottom", ipady=2)

    # -- thread-safe UI plumbing --------------------------------------------
    def _post(self, kind, payload=None):
        self._q.put((kind, payload))

    def _drain(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def _handle(self, kind, payload):
        if kind == "log":
            self._append_log(payload)
        elif kind == "status":
            self.status_var.set(payload)
        elif kind == "progress":
            sent, total = payload
            pct = (sent * 100.0 / total) if total else 0.0
            self.progress["value"] = pct
            self.status_var.set(f"Writing… {pct:5.1f}%  ({sent}/{total} bytes)")
        elif kind == "scan_result":
            self._on_scan_result(payload)
        elif kind == "done":
            self._set_busy(False)
            self.progress["value"] = 100.0
            self.status_var.set("Done — the module is rebooting to apply the new firmware.")
            messagebox.showinfo("Success", "Bluetooth firmware written successfully.\n\n"
                                "Restart the radio, then check the new version under "
                                "Device Info ▸ BT Soft Ver.")
        elif kind == "error":
            self._set_busy(False)
            self.status_var.set(f"Failed: {payload}")
            messagebox.showerror("Update failed", str(payload))
        elif kind == "scan_done":
            self._set_busy(False)

    def _append_log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _log_cb(self, msg):
        self._post("log", f"{LOG_PREFIX} {msg}")
        if os.environ.get("BT_OTA_DEBUG"):
            import logging
            logging.getLogger("bt_ota").debug(msg)

    def _progress_cb(self, sent, total):
        self._post("progress", (sent, total))

    def _set_busy(self, busy: bool):
        self._busy = busy
        state = "disabled" if busy else "normal"
        for w in (self.scan_btn, self.browse_btn, self.write_btn):
            w.configure(state=state)
        self.device_box.configure(state="disabled" if busy else "readonly")

    # -- button handlers -----------------------------------------------------
    def _model_ext(self) -> str:
        return ".ufw" if self.model_var.get() == "jieli" else ".bin"

    def _on_model_change(self):
        kind = self.model_var.get()
        self.status_var.set(f"Model: {MODELS[kind]} — pick a {self._model_ext()} firmware, then Scan and Write.")
        # drop a previously-picked file that doesn't match the new model
        path = self.file_var.get().strip()
        if path and firmware_kind(path) not in ("unknown", kind):
            self.file_var.set("")
        # re-order any already-scanned radios so the one for this model floats up
        if self._devices:
            self._refresh_device_labels(select_top=True)

    def on_browse(self):
        kind = self.model_var.get()
        desc = "D890 firmware (.ufw)" if kind == "jieli" else "D578/D878 firmware (.bin)"
        path = filedialog.askopenfilename(
            title=f"Select {desc}",
            filetypes=[(desc, f"*{self._model_ext()}"), ("All files", "*.*")],
        )
        if path:
            self.file_var.set(path)

    def on_scan(self):
        if self._busy:
            return
        self._set_busy(True)
        self.status_var.set("Scanning for radios…")
        self._append_log(f"{LOG_PREFIX} scanning (put the radio in pairing mode, not connected to a phone)…")
        self._aio.submit(self._do_scan())

    async def _do_scan(self):
        try:
            cands = await scan_devices(timeout=6.0)
            self._post("scan_result", cands)
        except Exception as e:  # noqa: BLE001
            self._post("log", f"{LOG_PREFIX} scan error: {e}")
            self._post("error", f"scan failed: {e}")
        finally:
            self._post("scan_done")

    def _on_scan_result(self, cands):
        self._devices = cands
        labels = self._refresh_device_labels(select_top=True)
        if labels:
            self.status_var.set(f"Found {len(labels)} device(s). Select one, choose a {self._model_ext()} file, then Connect & Write.")
            self._append_log(f"{LOG_PREFIX} found {len(labels)} candidate(s).")
        else:
            self.status_var.set("No radios found. Is Bluetooth on and in pairing mode?")
            self._append_log(f"{LOG_PREFIX} no candidates found.")

    def _refresh_device_labels(self, select_top: bool = False):
        """(Re)build the device dropdown from self._devices, floating the radio
        that matches the selected model to the top. Run after a scan and whenever
        the model selection changes."""
        self._devices = self._sort_candidates(self._devices)
        labels = [self._device_label(dev, rssi, local_name)
                  for dev, rssi, local_name in self._devices]
        self.device_box["values"] = labels
        if labels and select_top:
            self.device_box.current(0)
        return labels

    def _sort_candidates(self, cands):
        """Sort the scanned radios: the one matching the selected model first,
        then by signal strength (works regardless of the incoming order, so
        switching models re-sorts cleanly). The D890 (JieLi) is identified by its
        advertised local name ("D890UV"); the D578/D878 (WICED) module by its
        dev.name ("ELET_…" prefix)."""
        jieli = self.model_var.get() == "jieli"
        def rank(item):
            dev, rssi, local_name = item
            ln = (local_name or "").upper()
            dn = (getattr(dev, "name", None) or "").upper()
            match = ("D890" in ln or "890UV" in ln) if jieli \
                else (dn.startswith("ELET_") or ln.startswith("ELET_"))
            r = rssi if isinstance(rssi, (int, float)) else -999
            return (0 if match else 1, -r)
        return sorted(cands, key=rank)

    @staticmethod
    def _device_label(dev, rssi, local_name) -> str:
        """`"Local Name" (dev.name) [-45 dBm]` when the radio advertises a local
        name — the radio's own surrounding quotes are stripped (the D890 literally
        advertises `"D890UV"`, quotes and all) and dev.name is appended only when
        it adds something (e.g. the D890's `ET25SE_BLE_…`). When dev.name just
        repeats the local name it's dropped rather than padded with the address.
        Radios with no local name show their dev.name / address plainly."""
        ln = (local_name or "").strip().strip('"').strip()
        dn = (getattr(dev, "name", None) or "").strip().strip('"').strip()
        if ln:
            tail = f" ({dn})" if (dn and dn.lower() != ln.lower()) else ""
            return f'"{ln}"{tail} [{rssi} dBm]'
        return f'{dn or (dev.address or "").strip() or "(no name)"} [{rssi} dBm]'

    def on_write(self):
        if self._busy:
            return
        idx = self.device_box.current()
        if idx < 0 or idx >= len(self._devices):
            messagebox.showwarning("No radio", "Scan and select a radio first.")
            return
        path = self.file_var.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showwarning("No firmware", f"Choose a {self._model_ext()} firmware file first.")
            return
        kind = self.model_var.get()
        fk = firmware_kind(path)
        if fk != "unknown" and fk != kind:
            if not messagebox.askyesno(
                "File / model mismatch",
                f"Selected model is {MODELS[kind]}, but the file looks like a "
                f"{MODELS.get(fk, fk)} file:\n  {os.path.basename(path)}\n\nFlash anyway?",
            ):
                return
        device, _rssi, local_name = self._devices[idx]
        name = ((local_name or "").strip().strip('"').strip()
                or device.name or device.address)
        if not messagebox.askyesno(
            "Confirm firmware write",
            f"Write\n  {os.path.basename(path)}\nto the Bluetooth module of\n  {name}\n"
            f"as {MODELS[kind]}?\n\nKeep the radio powered and still during the update.",
        ):
            return
        try:
            fw = open(path, "rb").read()
        except OSError as e:
            messagebox.showerror("Cannot read file", str(e))
            return
        self._set_busy(True)
        self.progress["value"] = 0.0
        self.status_var.set("Connecting…")
        self._aio.submit(self._do_write(device, path, kind, fw))

    async def _do_write(self, device, path, kind, fw):
        try:
            client, kind = make_client(path, kind=kind, use_auth=True, log_cb=self._log_cb)
        except ValueError as e:
            self._post("error", str(e))
            return
        try:
            self._post("status", f"Connecting… ({kind})")
            await client.connect(device)
            self._post("status", "Connected — writing firmware…")
            await client.upgrade(fw, progress_cb=self._progress_cb)
            self._post("done")
        except Exception as e:  # noqa: BLE001 - OtaError / WicedOtaError / BLE
            self._post("log", f"{LOG_PREFIX} ERROR: {e}\n{traceback.format_exc()}")
            self._post("error", str(e))
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    def is_writing(self) -> bool:
        """True while a BLE scan/connect/write is in flight (for the shared close
        handler — closing mid-write can brick the BT module)."""
        return self._busy

    def stop(self):
        """Stop the background asyncio loop (called by the shared close handler
        when this App is one tab of a Notebook)."""
        try:
            self._aio.stop()
        except Exception:
            pass

    def _on_close(self):
        self.stop()
        self.root.destroy()


def _diag() -> int:
    """Frozen-bundle import self-test for BOTH tabs: no window, no BLE permission
    needed. Catches a missing hiddenimport or an unbundled asset in a frozen
    build without launching the UI."""
    import bleak  # noqa: F401
    if sys.platform == "darwin":
        import bleak.backends.corebluetooth.scanner  # noqa: F401
        import bleak.backends.corebluetooth.client   # noqa: F401
    elif sys.platform == "win32":
        import bleak.backends.winrt.scanner  # noqa: F401
        import bleak.backends.winrt.client   # noqa: F401
    from .jl_auth import AuthEmulator
    emu = AuthEmulator()
    out = emu.get_encrypted_auth_data(bytes([0] + list(range(1, 17))))

    # Radio and Boards tab: its serial stack, precompilers, and step photos.
    import serial  # noqa: F401
    from serial.tools import list_ports  # noqa: F401
    from radio_fw import engines, compiler, spec  # noqa: F401
    from radio_fw.gui_tab import _asset_path as _rf_asset
    images = sorted({spec.image(k) for k in spec.KIND_FILESPEC} | {"Reset.png"})
    missing = [img for img in images if not os.path.exists(_rf_asset(img))]
    if missing:
        print(f"DIAG FAIL: step images not found in the bundle: {missing}")
        return 1
    # prove the NR frame grammar agrees end to end (engine <-> vendored precompiler)
    assert engines.NR_LEN_NOTIFY_REPLY.hex() == "aa55010003e7a2"
    # prove the 878 CPS model + aprs target are wired (byte-match is in the tests)
    from radio_fw.vendor import fwupd_cps
    assert fwupd_cps.MODELS["d878uv2"]["aprs"]["ident"] == "IA-BORD"
    assert "aprs" in engines.CPS_PROFILE
    print(f"DIAG OK: BT tab (bleak+so, auth sample={out.hex()}); "
          f"Radio tab (pyserial {serial.VERSION}, precompilers, {len(images)} step photos, "
          f"models {'/'.join(spec.model_label(m) for m in spec.MODEL_ORDER)})")
    return 0


def _install_diagnostics():
    """Always write native-crash stacks to crash.log; with BT_OTA_DEBUG=1 also write
    full bleak/WinRT + app debug to debug.log. Both go to the config dir and work in
    a windowed build (no console/redirection needed)."""
    logdir = _config_dir()
    try:
        faulthandler.enable(open(os.path.join(logdir, "crash.log"), "a", buffering=1))
    except Exception:
        pass
    if os.environ.get("BT_OTA_DEBUG"):
        import logging
        try:
            logging.basicConfig(
                level=logging.DEBUG,
                format="%(asctime)s %(name)s %(levelname)s %(message)s",
                filename=os.path.join(logdir, "debug.log"), filemode="w",
            )
            logging.getLogger("bt_ota").info(
                "frozen=%s LIBUNICORN_PATH=%s",
                bool(getattr(sys, "_MEIPASS", None)), os.environ.get("LIBUNICORN_PATH"))
        except Exception:
            pass


def main():
    if os.environ.get("BT_OTA_DIAG"):
        raise SystemExit(_diag())
    _install_diagnostics()
    root = tk.Tk()
    root.title(APP_TITLE)
    root.minsize(760, 640)
    _set_window_icon(root)
    try:
        ttk.Style().theme_use("aqua")  # native look on macOS; ignored elsewhere
    except tk.TclError:
        pass
    _bring_to_front(root)
    if not run_disclaimer_gate(root):
        root.destroy()
        return

    # One shared brand header (logo + title + "by AesApp Inc." + website + About)
    # above the tabs, so both tabs carry the identity — not just the Bluetooth one.
    header = ttk.Frame(root)
    header.pack(fill="x", padx=8, pady=6)
    _brand_header(header, small=True).pack(side="left")
    ttk.Button(header, text="About", command=lambda: show_about(root)).pack(side="right")
    ttk.Separator(root).pack(fill="x", padx=8)

    # Two tabs: the original Bluetooth-module updater, and the radio/boards
    # firmware wizard. The BT App becomes tab 1 (parented on its page frame);
    # the radio/boards tab is imported lazily so a missing serial stack degrades
    # to a message in that tab instead of taking down the whole window.
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)

    bt_page = ttk.Frame(nb)
    nb.add(bt_page, text="Bluetooth Module Update")
    app = App(root, container=bt_page)

    radio_page = ttk.Frame(nb)
    nb.add(radio_page, text="Radio and Boards Updates")
    boards = None
    try:
        from radio_fw.gui_tab import RadioBoardsTab
        boards = RadioBoardsTab(radio_page, root)
    except Exception as e:  # noqa: BLE001
        ttk.Label(radio_page, foreground="#b00020", justify="left", wraplength=600,
                  text="Radio and Boards updates are unavailable in this build: " + str(e)
                       + "\n\nThe Bluetooth Module Update tab is unaffected.").pack(padx=16, pady=16)

    def _on_close():
        busy = False
        try:
            busy = app.is_writing()   # BLE scan/connect/write on the asyncio loop
        except Exception:
            pass
        if not busy and boards is not None:
            try:
                busy = boards.is_writing()   # a serial firmware/board write
            except Exception:
                pass
        if busy and not messagebox.askyesno(
                "Quit during a write?",
                "An update is in progress. Quitting now can leave a radio unbootable.\n\nQuit anyway?"):
            return
        app.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)
    _bring_to_front(root)
    root.mainloop()


if __name__ == "__main__":
    main()
