from __future__ import annotations

import base64
import math
import os
import queue
import struct
import sys
import threading
import time
import tkinter as tk
import webbrowser
import zlib
from tkinter import filedialog, messagebox, scrolledtext, ttk
from tkinter import font as tkfont

from serial.tools import list_ports

from . import compiler, download, engines, spec
from radio_contacts import engine as _pc_engine
from .layout import follow_width as _follow_width
from . import ports as _ports

_LOG_TAGS = {"er": "#b00020", "error": "#b00020", "done": "#127a2e"}

_BADGE_RGB = {"done": (30, 142, 62), "current": (26, 115, 232), "todo": (154, 160, 166),
              "failed": (200, 32, 40)}
_STEP_FONT = ("", 12, "bold")
_BADGE_FONT = ("", 11, "bold")
_TODO_FG = "#8a8a8a"
_RAIL = {"done": "#1e8e3e", "todo": "#c6c6c6"}
_BANNER = {"info": ("#e8f0fe", "#174ea6"), "ok": ("#e6f4ea", "#127a2e"), "error": ("#fce8e6", "#b00020")}


def _badge_png(size: int, rgb: tuple[int, int, int]) -> bytes:
    sub, r = 4, size / 2.0
    rows = []
    for y in range(size):
        row = bytearray(b"\0")
        for x in range(size):
            hit = sum((x + (i + 0.5) / sub - r) ** 2 + (y + (j + 0.5) / sub - r) ** 2 <= (r - 0.5) ** 2
                      for i in range(sub) for j in range(sub))
            row += bytes(rgb) + bytes([round(255 * hit / (sub * sub))])
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))
    return base64.b64encode(png)


def _fmt_eta(seconds: float) -> str:
    if seconds < 90:
        return "about " + str(int(round(seconds))) + " s"
    return "about " + str(int(round(seconds / 60.0))) + " min"


def _fmt_took(seconds: float) -> str:
    s = int(round(seconds))
    return str(s) + " s" if s < 60 else str(s // 60) + " min " + str(s % 60) + " s"


def _asset_path(name: str) -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for base in (here, getattr(sys, "_MEIPASS", None)):
        if not base:
            continue
        for cand in (os.path.join(base, "radio_fw", "assets", name),
                     os.path.join(base, "assets", name)):
            if os.path.exists(cand):
                return cand
    return os.path.join(here, "radio_fw", "assets", name)


_STEP_BASE_W = 380
_STEP_SCALE = 1.15


def _scaled_photo(img: tk.PhotoImage, target_w: int) -> tk.PhotoImage:
    w = img.width()
    if w <= 0 or target_w <= 0:
        return img
    best = None
    for z in range(1, 7):
        s = max(1, round(z * w / target_w))
        err = abs((w * z) / s - target_w)
        if best is None or err < best[0]:
            best = (err, z, s)
    _, z, s = best
    out = img.zoom(z, z) if z > 1 else img
    if s > 1:
        out = out.subsample(s, s)
    return out


_STEP_MIN_H = 120
_LOG_RESERVE = 64


def _load_step_source(name: str) -> tk.PhotoImage | None:
    try:
        return tk.PhotoImage(file=_asset_path(name))
    except Exception:
        return None


def _load_step_image(name: str) -> tk.PhotoImage | None:
    try:
        img = tk.PhotoImage(file=_asset_path(name))
    except Exception:
        return None
    try:
        w = img.width()
        base = w / math.ceil(w / _STEP_BASE_W)
        target = round(base * _STEP_SCALE)
        if 0 < target < w:
            img = _scaled_photo(img, target)
    except Exception:
        pass
    return img


def _fw_ident(bundle: dict) -> str | None:
    for c in bundle.get("components") or []:
        if c.get("kind") == spec.KIND_FW:
            return c.get("ident")
    return None


def _resolve_cps_siblings(spi_path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    folder = os.path.dirname(spi_path)
    base = os.path.splitext(os.path.basename(spi_path))[0]
    try:
        names = os.listdir(folder or ".")
    except OSError:
        return out
    for fn in names:
        stem, ext = os.path.splitext(fn)
        e = ext.lower().lstrip(".")
        if e in ("cdd", "cdi") and stem == base:
            out[e] = os.path.join(folder, fn)
    return out


class _AccentButton(ttk.Button):

    def __init__(self, *args, accent=False, **kwargs):
        super().__init__(*args, **kwargs)
        self._accent = accent
        self._sync()

    def accent(self, on: bool):
        self._accent = on
        self._sync()

    def state(self, statespec=None):
        if statespec is None:
            return super().state()
        out = super().state(statespec)
        self._sync()
        return out

    def configure(self, cnf=None, **kwargs):
        out = super().configure(cnf, **kwargs)
        if "state" in kwargs or (isinstance(cnf, dict) and "state" in cnf):
            self._sync()
        return out

    config = configure

    def _sync(self):
        super().configure(default="active" if self._accent and self.instate(["!disabled"]) else "normal")


class _StepHead:

    def __init__(self, tab, parent, title):
        self.tab = tab
        self.frame = ttk.Frame(parent)
        self.badge = ttk.Label(self.frame, compound="center", foreground="white", font=_BADGE_FONT)
        self.badge.pack(side="left")
        self.title = ttk.Label(self.frame, text=title, font=_STEP_FONT)
        self.title.pack(side="left", padx=(8, 0))
        self.state = None

    def show(self, number, state):
        self.state = state
        self.badge.configure(image=self.tab._badge(state), text="✓" if state == "done" else str(number))
        self.title.configure(foreground=_TODO_FG if state == "todo" else "")


class _Row:

    def __init__(self, tab, parent, kind, index):
        self.tab = tab
        self.kind = kind
        self.result: compiler.CompileResult | None = None
        self.error: str | None = None
        self.paths: list[str] = []
        self.picked: dict[str, str] = {}
        self._gen = 0

        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=2)
        frame.columnconfigure(2, weight=1)

        self.checked = tk.BooleanVar(value=False)
        self.cb = ttk.Checkbutton(frame, variable=self.checked, command=tab._refresh_start_state)
        self.cb.grid(row=0, column=0, sticky="w")
        self.cb.state(["disabled"])

        ttk.Label(frame, text=str(index) + ". " + spec.label(kind, tab.model), width=20).grid(row=0, column=1, sticky="w")

        if spec.is_multi(kind):
            btn_text = "Choose .spi file…"
            init_status = ("Pick the .spi update file — the .CDD and .CDI load automatically from "
                           "the same folder. (No .spi? Shift-select the .CDD and .CDI instead.)")
            init_color = "#8a6d00"
        else:
            btn_text = "Choose file… (." + (spec.requires(kind)[0].upper() if spec.requires(kind) else "?") + ")"
            init_status, init_color = "not selected", "#666"
        self.btn = ttk.Button(frame, text=btn_text, command=self._pick)
        self.btn.grid(row=0, column=2, sticky="w", padx=6)

        self.status = ttk.Label(frame, text=init_status, foreground=init_color,
                                wraplength=520, justify="left")
        self.status.grid(row=1, column=1, columnspan=2, sticky="w")

        self.version_lbl = ttk.Label(frame, text="", foreground="#0b5f99")
        self.version_lbl.grid(row=0, column=3, sticky="e", padx=(6, 0))
        _follow_width(self.status, frame,
                      reserve=lambda: self.cb.winfo_reqwidth() + self.version_lbl.winfo_reqwidth() + 18)

    def _pick(self):
        multi = spec.is_multi(self.kind)
        exts = spec.accepts(self.kind)
        req_exts = spec.requires(self.kind)
        patterns = " ".join("*." + e for e in exts)
        if multi:
            types = [("Update package (.spi)", "*.spi"),
                     (spec.label(self.kind, self.tab.model) + " files (." + " .".join(e.upper() for e in exts) + ")", patterns),
                     ("All files", "*.*")]
            title = ("Select the .spi file — the .CDD and .CDI load automatically from the same "
                     "folder (or Shift-select the .CDD and .CDI yourself)")
            picked = list(filedialog.askopenfilenames(title=title, filetypes=types))
        else:
            types = [(spec.label(self.kind, self.tab.model) + " files", patterns), ("All files", "*.*")]
            p = filedialog.askopenfilename(title="Choose the " + spec.label(self.kind, self.tab.model) + " file",
                                           filetypes=types)
            picked = [p] if p else []
        if not picked:
            return

        if multi:
            new = {os.path.splitext(p)[1].lower().lstrip("."): p for p in picked
                   if os.path.splitext(p)[1].lower().lstrip(".") in exts}
            if "spi" in new:
                self.picked = {"spi": new["spi"], **_resolve_cps_siblings(new["spi"])}
            else:
                self.picked.update(new)
            missing = [e for e in req_exts if e not in self.picked]
            if missing:
                have = ", ".join(os.path.basename(v) for v in self.picked.values()) or "nothing yet"
                miss = ", ".join("." + e.upper() for e in missing)
                if "spi" in self.picked:
                    tip = (" — but the " + miss + " isn't next to it in that folder. Shift-select the "
                           + miss + " yourself.")
                else:
                    tip = ". Click “Choose .spi file…” again to add it (or Shift-select both)."
                self.status.configure(text="Have " + have + " — still need " + miss + tip,
                                      foreground="#8a6d00")
                self.checked.set(False)
                self.cb.state(["disabled"])
                self.result = None
                self.error = None
                self.tab._refresh_start_state()
                return
            paths = list(self.picked.values())
        else:
            paths = picked

        self.paths = paths
        self.status.configure(text="compiling…", foreground="#666")
        self.checked.set(False)
        self.cb.state(["disabled"])
        self.result = None
        self.error = None
        self.tab._refresh_start_state()
        self._gen += 1
        gen = self._gen
        model = self.tab.model
        threading.Thread(target=self._compile_worker, args=(self.kind, paths, gen, model), daemon=True).start()

    def _compile_worker(self, kind, paths, gen, model):
        try:
            res = compiler.compile_files(kind, paths, model)
            self.tab._post("compiled", (self, res, None, gen))
        except compiler.CompileError as e:
            self.tab._post("compiled", (self, None, str(e), gen))
        except Exception as e:
            self.tab._post("compiled", (self, None, str(e), gen))

    def apply_compile(self, res, error):
        if res is not None and self.tab.model:
            mismatch = spec.fw_model_mismatch(self.tab.family or "", self.tab.model, res.manifest)
            if not mismatch and self.kind == spec.KIND_FW:
                mismatch = spec.fw_build_mismatch(self.tab.model, self.tab.radio_chip, res.manifest)
            if mismatch:
                res, error = None, mismatch
        self.result = res
        self.error = error
        if res is not None:
            names = ", ".join(res.source_names)
            sha = (res.sha256 or "")[:10]
            skipped = res.manifest.get("skipped_entries") or []
            chip = spec.fw_chip(res.manifest) if self.kind == spec.KIND_FW else None
            self.status.configure(
                text="✓ " + names + " — " + format(res.frames, ",") + " frames, "
                     + format(res.payload_bytes, ",") + " payload bytes"
                     + (" · sha " + sha if sha else "")
                     + (" · for the " + chip + " MCU" if chip else "")
                     + (" · leaves out " + ", ".join(str(e.get("name", "?")) for e in skipped)
                        + " (not counted in the .spi, as the factory CPS does)" if skipped else ""),
                foreground="#127a2e")
            self.checked.set(True)
            self.cb.state(["!disabled"])
        else:
            self.status.configure(text="✗ " + (error or "compile failed"), foreground="#b00020")
            self.checked.set(False)
            self.cb.state(["disabled"])
        self.tab._refresh_start_state()

    @property
    def ready(self) -> bool:
        return self.result is not None and self.checked.get()

    def enable_local_pick(self, enabled: bool):
        try:
            self.btn.state(["!disabled"] if enabled else ["disabled"])
        except tk.TclError:
            pass

    def note(self, text: str, color: str = "#666"):
        self.status.configure(text=text, foreground=color)

    def set_server_version(self, version):
        try:
            self.version_lbl.configure(text=("ver " + version) if version else "")
        except tk.TclError:
            pass

    def reset_selection(self):
        self.result = None
        self.error = None
        self.paths = []
        self.picked = {}
        self._gen += 1
        self.checked.set(False)
        self.cb.state(["disabled"])

    def begin_download(self):
        self.note("downloading… 0%", "#666")
        self.checked.set(False)
        self.cb.state(["disabled"])
        self.result = None
        self.error = None


class RadioBoardsTab:

    def __init__(self, parent: tk.Widget, root: tk.Tk):
        self.parent = parent
        self.root = root
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self.model = spec.DEFAULT_MODEL
        self.model_var = tk.StringVar(value=self.model)
        self.family: str | None = None
        self.radio_chip: str | None = None
        self._identified: dict[str, tuple] = {}
        self._identifying = False
        self.source_var = tk.StringVar(value="server")
        self._catalog: list[dict] = []
        self._catalog_loaded = False
        self._catalog_loading = False
        self._version_map: dict[str, dict] = {}
        self._fetch_token = 0
        self._dl_pending = 0
        self._fetched_label = None
        self.rows: list[_Row] = []
        self.plan: list[compiler.CompileResult] = []
        self.step = 0
        self.results: list[dict] = []
        self._writing = False
        self._abort: threading.Event | None = None
        self._step_img = None
        self._last_port = None
        self._eta_start = None
        self._sct_baud = tk.StringVar(value=str(engines.SCT_BAUD))
        self._badge_size = max(16, round(root.winfo_fpixels("12p") * 1.35))
        self._badges: dict[str, tk.PhotoImage] = {}

        self._build()
        self._on_source_change()
        self.root.after(80, self._drain)

    def _badge(self, state: str) -> tk.PhotoImage:
        if state not in self._badges:
            self._badges[state] = tk.PhotoImage(master=self.root,
                                                data=_badge_png(self._badge_size, _BADGE_RGB[state]))
        return self._badges[state]

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
        if kind == "compiled":
            row, res, err, gen = payload
            if gen == row._gen and row in self.rows:
                row.apply_compile(res, err)
        elif kind == "identified":
            self._on_identified(*payload)
        elif kind == "log":
            msg, cls = payload
            self._log(msg, cls)
        elif kind == "progress":
            self._on_progress(*payload)
        elif kind == "stage_done":
            self._on_stage_done()
        elif kind == "stage_error":
            msg, aborted = payload
            self._on_stage_error(msg, aborted)
        elif kind == "catalog_ok":
            self._catalog = payload or []
            self._catalog_loaded = True
            self._catalog_loading = False
            self._populate_versions()
        elif kind == "catalog_err":
            self._catalog_loading = False
            self.fetch_btn.state(["disabled"])
            self.server_status.configure(
                text=str(payload) + "  (Click “Refresh list” to try again.)", foreground="#b00020")
        elif kind == "catalog_status":
            self.server_status.configure(text=str(payload), foreground="#8a6d00")
        elif kind == "dl_status":
            row, gen, msg = payload
            if gen == row._gen and row in self.rows and row.result is None:
                row.note(str(msg), "#8a6d00")
        elif kind == "dl_progress":
            row, gen, got, total = payload
            if gen == row._gen and row in self.rows and row.result is None:
                pct = (got * 100.0 / total) if total else 0.0
                row.note("downloading… " + format(pct, ".0f") + "%", "#666")
        elif kind == "dl_settle":
            if payload == self._fetch_token:
                self._dl_pending = max(0, self._dl_pending - 1)
                if self._dl_pending == 0:
                    self._on_fetch_done()

    def _build_rows(self):
        for w in self.rows_box.winfo_children():
            w.destroy()
        self.rows = []
        self._fetched_label = None
        if self.model is None:
            ttk.Label(self.rows_box, foreground="#8a6d00", justify="left",
                      text="Identify the radio above, or choose it by hand, to see what can be "
                           "updated on it.").pack(anchor="w", pady=3)
            return
        for i, kind in enumerate(spec.model_order(self.model), start=1):
            self.rows.append(_Row(self, self.rows_box, kind, i))
        local = self.source_var.get() == "local"
        for r in self.rows:
            r.enable_local_pick(local)
            if not local:
                r.note("Pick a version above and click Fetch.", "#8a6d00")

    def _on_model_change(self):
        if self._writing:
            return
        choice = self.model_var.get()
        self.family = choice if spec.is_family(choice) else None
        if self.family:
            self.model, self.radio_chip = self._identified.get(self.family, (None, None))[:2]
        else:
            self.model, self.radio_chip = choice, None
        self._show_identify()
        self._show_model_notes()
        self._build_rows()
        self.confirm.set(False)
        if self.source_var.get() == "server":
            self._populate_versions()
        self._refresh_start_state()

    def _on_source_change(self):
        if self._writing:
            return
        server = self.source_var.get() == "server"
        self._build_rows()
        if server:
            self.version_box.state(["!disabled"])
            self.reload_btn.state(["!disabled"])
            self.server_row.pack(fill="x", padx=(self._indent, 0), pady=(0, 6), after=self.src_row)
            self._ensure_catalog()
        else:
            self.server_row.pack_forget()
        self.setup_desc.configure(text=(
            "Tick the targets to write. They are written in this order, radio firmware first; any can "
            "be skipped, none reordered." if server else
            "Pick the vendor files for each target; each is compiled and checked here before anything "
            "is sent. They are written in this order, radio firmware first."))
        self._refresh_start_state()

    def _ensure_catalog(self):
        if self._catalog_loaded or self._catalog_loading:
            if self._catalog_loaded:
                self._populate_versions()
            return
        self._reload_catalog()

    def _on_reload_click(self, _event=None):
        if not self.reload_btn.instate(["disabled"]):
            self._reload_catalog()

    def _reload_catalog(self):
        if self._catalog_loading:
            return
        self._catalog_loading = True
        self.version_box["values"] = []
        self.version_var.set("")
        self.fetch_btn.state(["disabled"])
        self.server_status.configure(text="Loading versions from the AesApp server…", foreground="#666")
        threading.Thread(target=self._catalog_worker, daemon=True).start()

    def _catalog_worker(self):
        try:
            bundles = download.fetch_catalog(
                on_status=lambda m: self._post("catalog_status", m))
            self._post("catalog_ok", bundles)
        except download.DownloadError as e:
            self._post("catalog_err", str(e))
        except Exception as e:
            self._post("catalog_err", str(e))

    def _populate_versions(self):
        if self.model is None:
            self._version_map = {}
            self.version_box["values"] = []
            self.version_var.set("")
            self.fetch_btn.state(["disabled"])
            self.cps_btn.state(["disabled"])
            self.server_status.configure(text="Identify the radio above first.", foreground="#8a6d00")
            return
        wanted = spec.server_models(self.model)
        self._version_map = {}
        labels = []
        for b in self._catalog:
            if b.get("fwupdModel") not in wanted:
                continue
            built_for = spec.fw_chip_of_ident(_fw_ident(b))
            if self.radio_chip and built_for and built_for != self.radio_chip:
                continue
            label = b.get("label") or (
                spec.server_model_name(b.get("fwupdModel", "")) + " " + str(b.get("cpsVersion", "?")))
            n, base = 1, label
            while label in self._version_map:
                n += 1
                label = base + " #" + str(n)
            self._version_map[label] = b
            labels.append(label)
        self.version_box["values"] = labels
        if labels:
            self.version_box.current(0)
            self.fetch_btn.state(["!disabled"])
            self._on_version_selected()
            self.server_status.configure(
                text="Choose a version and click Fetch. Files are checksum-verified before flashing.",
                foreground="#127a2e")
        else:
            self.version_var.set("")
            self.fetch_btn.state(["disabled"])
            self.cps_btn.state(["disabled"])
            self.server_status.configure(
                text="No prebuilt versions are available for this radio yet. Use your own files instead.",
                foreground="#8a6d00")

    def _selected_bundle(self) -> "dict | None":
        return self._version_map.get(self.version_var.get())

    def _on_version_selected(self):
        b = self._selected_bundle()
        self.cps_btn.state(["!disabled"] if (b and b.get("cpsUrl")) else ["disabled"])
        if not self._writing and self.version_var.get() != self._fetched_label \
                and any(r.result or r.error for r in self.rows):
            self._clear_fetched()
        self._show_component_versions(b)

    def _show_component_versions(self, bundle):
        comps = {c.get("kind"): c for c in (bundle.get("components", []) if bundle else [])}
        for r in self.rows:
            c = comps.get(r.kind)
            r.set_server_version(c.get("version") if c else None)

    def _clear_fetched(self):
        self._fetched_label = None
        for r in self.rows:
            r.reset_selection()
            r.note("Pick a version above and click Fetch.", "#8a6d00")
        self.server_status.configure(
            text="Choose a version and click Fetch. Files are checksum-verified before flashing.",
            foreground="#127a2e")
        self._refresh_start_state()

    def _on_download_cps(self):
        b = self._selected_bundle()
        url = b.get("cpsUrl") if b else None
        if not url:
            return
        try:
            webbrowser.open(url)
            self.server_status.configure(text="Opening the CPS download in your browser…",
                                         foreground="#127a2e")
        except Exception:
            self.server_status.configure(text="Couldn't open a browser. CPS link: " + url,
                                         foreground="#b00020")

    def _on_fetch(self):
        if self._writing:
            return
        label = self.version_var.get()
        bundle = self._version_map.get(label)
        if not bundle:
            return
        self._fetched_label = label
        comps = {c.get("kind"): c for c in bundle.get("components", [])}
        self._fetch_token += 1
        token = self._fetch_token
        self.fetch_btn.state(["disabled"])
        self.reload_btn.state(["disabled"])
        self.server_status.configure(
            text="Downloading " + label + " …", foreground="#666")
        started = 0
        for r in self.rows:
            comp = comps.get(r.kind)
            if comp is None:
                r.reset_selection()
                r.note("not part of this version", "#999")
                continue
            r._gen += 1
            r.begin_download()
            threading.Thread(target=self._fetch_worker,
                             args=(r, comp, r._gen, token, self.model), daemon=True).start()
            started += 1
        self._dl_pending = started
        if started == 0:
            self.server_status.configure(
                text="This version has no flashable targets for the selected radio.", foreground="#8a6d00")
            self.fetch_btn.state(["!disabled"])
            self.reload_btn.state(["!disabled"])
        self._refresh_start_state()

    def _on_fetch_done(self):
        self.fetch_btn.state(["!disabled"])
        self.reload_btn.state(["!disabled"])
        ok = sum(1 for r in self.rows if r.result is not None)
        failed = [r for r in self.rows if r.error]
        if failed:
            self.server_status.configure(
                text=str(ok) + " target(s) ready; " + str(len(failed))
                     + " could not be downloaded — see the rows below. Try Fetch again.",
                foreground="#b00020")
        elif ok:
            self.server_status.configure(
                text=str(ok) + " target(s) downloaded and checksum-verified. Tick the ones to write, "
                     "then agree and Start.", foreground="#127a2e")
        self._refresh_start_state()

    def _fetch_worker(self, row, comp, gen, token, model):
        try:
            res = download.download_component(
                comp,
                on_progress=lambda got, total: self._post("dl_progress", (row, gen, got, total)),
                on_status=lambda m: self._post("dl_status", (row, gen, m)))
            res = compiler.check_downloaded(res, model)
            self._post("compiled", (row, res, None, gen))
        except (download.DownloadError, compiler.CompileError) as e:
            self._post("compiled", (row, None, str(e), gen))
        except Exception as e:
            self._post("compiled", (row, None, str(e), gen))
        finally:
            self._post("dl_settle", token)

    def _build(self):
        outer = self._outer = ttk.Frame(self.parent)
        outer.pack(fill="both", expand=True, padx=10, pady=8)
        indent = self._indent = self._badge_size + 8
        self._heads: dict[str, _StepHead] = {}

        top = self.top = ttk.Frame(outer)
        top.pack(fill="x")

        model_row = ttk.Frame(top)
        model_row.pack(fill="x", pady=(0, 6))
        head = self._heads["model"] = _StepHead(self, model_row, "Radio model")
        head.frame.grid(row=0, column=0, sticky="nw")
        self._model_btns = []
        for m in spec.MODEL_ORDER:
            rb = ttk.Radiobutton(model_row, text=spec.choice_label(m), value=m,
                                 variable=self.model_var, command=self._on_model_change)
            self._model_btns.append(rb)
        self._model_row = model_row
        self._model_layout = None
        self._flow_model_buttons()
        model_row.bind("<Configure>", self._flow_model_buttons, add="+")

        self.covers_lbl = ttk.Label(top, text="", foreground="#444", justify="left")
        _follow_width(self.covers_lbl, top, reserve=indent)
        self.untested_lbl = ttk.Label(top, text="", foreground="#b00020", justify="left")
        _follow_width(self.untested_lbl, top, reserve=indent)
        self._build_identify(top)
        self._show_model_notes()

        src_row = self.src_row = ttk.Frame(top)
        src_row.pack(fill="x", pady=(0, 4))
        head = self._heads["files"] = _StepHead(self, src_row, "Update files")
        head.frame.pack(side="left")
        self._src_btns = []
        for val, txt in (("server", "Prebuilt from AesApp"),
                         ("local", "My own files")):
            rb = ttk.Radiobutton(src_row, text=txt, value=val, variable=self.source_var,
                                 command=self._on_source_change)
            rb.pack(side="left", padx=(12, 0))
            self._src_btns.append(rb)

        self.server_row = ttk.Frame(top)
        ttk.Label(self.server_row, text="Version:").grid(row=0, column=0, sticky="w")
        self.version_var = tk.StringVar()
        self.version_box = ttk.Combobox(self.server_row, textvariable=self.version_var,
                                        state="readonly", width=34)
        self.version_box.grid(row=0, column=1, sticky="w", padx=(6, 4))
        self.version_box.bind("<<ComboboxSelected>>", lambda _e: self._on_version_selected())
        self.reload_btn = ttk.Label(self.server_row, text="⟳", font=("", 16),
                                    cursor="hand2")
        self.reload_btn.grid(row=0, column=2, padx=(2, 2))
        self.reload_btn.bind("<Button-1>", self._on_reload_click)
        self.fetch_btn = _AccentButton(self.server_row, text="Fetch", command=self._on_fetch)
        self.fetch_btn.grid(row=0, column=3, padx=(8, 0))
        self.cps_btn = ttk.Button(self.server_row, text="Download CPS",
                                  command=self._on_download_cps)
        self.cps_btn.grid(row=0, column=4, padx=(8, 0))
        self.cps_btn.state(["disabled"])
        self.server_status = ttk.Label(self.server_row, text="", foreground="#666",
                                       wraplength=620, justify="left")
        self.server_status.grid(row=1, column=0, columnspan=5, sticky="w", pady=(2, 0))
        _follow_width(self.server_status, self.server_row, reserve=8)

        self.setup = ttk.Frame(outer)
        self.setup.pack(fill="x")
        start_box = ttk.Frame(self.setup)
        start_box.pack(side="bottom", fill="x")
        head = self._heads["targets"] = _StepHead(self, self.setup, "Choose what to update")
        head.frame.pack(fill="x", pady=(2, 0))
        self.targets_note = ttk.Label(head.frame, text="", foreground="#555")
        self.targets_note.pack(side="left", padx=(10, 0))
        self.setup_desc = ttk.Label(self.setup, justify="left", foreground="#444")
        self.setup_desc.pack(anchor="w", padx=(indent, 0), pady=(0, 2))
        _follow_width(self.setup_desc, self.setup, reserve=indent + 8)

        self.rows_box = ttk.Frame(self.setup)
        self.rows_box.pack(fill="x", padx=(indent, 0))
        self._build_rows()

        head = self._heads["start"] = _StepHead(self, start_box, "Confirm and start")
        head.frame.pack(fill="x", pady=(6, 0))
        self.confirm = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            start_box, variable=self.confirm, command=self._refresh_start_state,
            text="I understand these writes are not verified and that an interrupted write can brick a radio."
        ).pack(anchor="w", padx=(indent, 0))
        start_row = ttk.Frame(start_box)
        start_row.pack(fill="x", padx=(indent, 0), pady=(6, 0))
        self.start_btn = _AccentButton(start_row, text="Start Upgrades", command=self._start)
        self.start_btn.pack(side="left")
        self.start_btn.state(["disabled"])
        self.start_hint = ttk.Label(start_row, text="", foreground="#666")
        self.start_hint.pack(side="left", padx=(10, 0))

        self.wizard = ttk.Frame(outer)
        self.track = ttk.Frame(self.wizard)
        self.track.pack(fill="x", pady=(0, 8))
        self._track_items: list[tuple] = []
        title_row = ttk.Frame(self.wizard)
        title_row.pack(fill="x")
        self.wtitle = ttk.Label(title_row, text="—", font=("", 15, "bold"))
        self.wtitle.pack(side="left")
        self.wcontext = ttk.Label(title_row, text="", foreground="#666")
        self.wcontext.pack(side="left", anchor="s", padx=(12, 0), pady=(0, 2))

        body = ttk.Frame(self.wizard)
        body.pack(fill="x", pady=6)
        self._wbody = body
        self._step_natural = None
        self._step_src = None
        self._fit_pending = False
        self.wizard.bind("<Configure>", lambda _e: self._schedule_fit())
        self.wizard.bind("<Configure>", lambda _e: self._fit_track(), add="+")
        self.wimage = ttk.Label(body)
        self.wimage.pack(side="left", anchor="n", padx=(0, 12))
        self.winstr = ttk.Label(body, justify="left", wraplength=380,
                                foreground="#7a1020", font=("", 11))
        self.winstr.pack(side="left", anchor="n", fill="x", expand=True)
        _follow_width(self.winstr, body, reserve=lambda: self.wimage.winfo_reqwidth() + 20)

        self.wctl = ttk.Frame(self.wizard)
        self.wctl.pack(fill="x", pady=(4, 0))

        self.ready_btn = _AccentButton(self.wctl, text="The radio is in this mode →", command=self._on_ready,
                                       accent=True)

        self.port_row = ttk.Frame(self.wizard)
        ttk.Label(self.port_row, text="COM port:").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(self.port_row, textvariable=self.port_var, state="readonly", width=42)
        self.port_box.grid(row=0, column=1, sticky="ew", padx=6)
        self.port_row.columnconfigure(1, weight=1)
        ttk.Button(self.port_row, text="Refresh", command=self._refresh_ports).grid(row=0, column=2)
        self.sct_baud_row = ttk.Frame(self.port_row)
        ttk.Label(self.sct_baud_row, text="Baud:").pack(side="left")
        self._sct_baud_box = ttk.Combobox(self.sct_baud_row, textvariable=self._sct_baud, state="readonly",
                                          width=8, values=[str(engines.SCT_BAUD), str(engines.SCT_ALT_BAUD)])
        self._sct_baud_box.pack(side="left", padx=(4, 0))
        port_actions = self._port_actions = ttk.Frame(self.port_row)
        port_actions.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.connect_btn = _AccentButton(port_actions, text="Connect && Write", command=self._on_connect,
                                         accent=True)
        self.connect_btn.pack(side="left")
        self.skip_btn = ttk.Button(self.wizard, text="Skip this target", command=self._on_skip)

        self.progress_row = ttk.Frame(self.wizard)
        self.progress = ttk.Progressbar(self.progress_row, mode="determinate", maximum=100.0)
        self.progress.pack(side="left", fill="x", expand=True)
        self.pct_lbl = ttk.Label(self.progress_row, text="", font=("", 20, "bold"), width=5, anchor="e")
        self.pct_lbl.pack(side="left", padx=(10, 0))
        self.wstatus = tk.Label(self.wizard, text="", anchor="w", justify="left", padx=10, pady=6, bd=0)
        _follow_width(self.wstatus, self.wizard, reserve=24)
        self.abort_btn = ttk.Button(self.wizard, text="Stop", command=self._on_abort)
        self.next_btn = _AccentButton(self.wizard, text="Next", command=self._on_next, accent=True)

        self.log_lbl = ttk.Label(self.wizard, text="Protocol log")
        self.log_lbl.pack(anchor="w", pady=(8, 0))
        self.log = scrolledtext.ScrolledText(self.wizard, height=10, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)
        for tag, color in _LOG_TAGS.items():
            self.log.tag_configure(tag, foreground=color)

        self.done = ttk.Frame(outer)
        again = self._again_frame = ttk.Frame(self.done)
        again.pack(side="bottom", fill="x")
        self.done_title = tk.Label(self.done, text="", anchor="w", justify="left", padx=10, pady=8, bd=0,
                                   font=("", 13, "bold"))
        self.done_title.pack(fill="x")
        _follow_width(self.done_title, self.done, reserve=24)
        self.done_summary = ttk.Frame(self.done)
        self.done_summary.pack(fill="x", pady=6)
        self.mcu_box = ttk.LabelFrame(self.done, text="Reset the MCU now (Radio Firmware was written)")
        _mcu_body = self._mcu_body = ttk.Frame(self.mcu_box)
        _mcu_body.pack(fill="x", padx=8, pady=6)
        self._mcu_natural = _load_step_image(spec.reset_image(spec.DEFAULT_MODEL))
        self._mcu_src = _load_step_source(spec.reset_image(spec.DEFAULT_MODEL))
        self._mcu_img = self._mcu_natural
        self._mcu_label = ttk.Label(_mcu_body, image=self._mcu_img if self._mcu_img else "")
        self._mcu_label.pack(side="left", anchor="n", padx=(0, 12))
        self.done.bind("<Configure>", lambda _e: self._schedule_fit())
        self.mcu_reset_lbl = ttk.Label(_mcu_body, justify="left", wraplength=420,
                                       foreground="#7a1020", font=("", 11))
        self.mcu_reset_lbl.pack(side="left", anchor="n", fill="x", expand=True)
        _follow_width(self.mcu_reset_lbl, _mcu_body, reserve=lambda: self._mcu_label.winfo_reqwidth() + 20)
        ttk.Button(again, text="Update another radio (same selection)", command=self._again).pack(side="left")
        ttk.Button(again, text="Back to setup", command=self._back_to_setup).pack(side="left", padx=8)

        self._rails = [tk.Frame(outer, width=2, bd=0, highlightthickness=0) for _ in range(len(self._heads) - 1)]
        self._rails_pending = False
        bound = set()
        for head in self._heads.values():
            w = head.frame
            while w is not outer and str(w) not in bound:
                bound.add(str(w))
                for event in ("<Configure>", "<Map>"):
                    w.bind(event, lambda _e: self._schedule_rails(), add="+")
                w = w.master

    def _refresh_start_state(self):
        any_ready = any(r.ready for r in self.rows)
        ok = self.confirm.get() and any_ready
        self.start_btn.state(["!disabled"] if ok else ["disabled"])
        self._refresh_steps()

    def _step_keys(self) -> list[str]:
        return ["model"] + (["identify"] if self.family else []) + ["files", "targets", "start"]

    def _step_done(self, key) -> bool:
        if key == "model":
            return True
        if key == "identify":
            return self.model is not None
        if key == "files":
            return self.source_var.get() == "local" or any(r.result is not None for r in self.rows)
        if key == "targets":
            return any(r.ready for r in self.rows)
        return False

    def _refresh_steps(self):
        current = None
        for n, key in enumerate(self._step_keys(), start=1):
            if current is None and not self._step_done(key):
                current = key
            self._heads[key].show(n, "done" if current is None else "current" if current == key else "todo")
        server = self.source_var.get() == "server"
        for key, btn in (("identify", self.identify_btn), ("files", self.fetch_btn), ("start", self.start_btn)):
            btn.accent(key == current and (key != "files" or server))
        ticked = sum(1 for r in self.rows if r.ready)
        self.targets_note.configure(text=(str(ticked) + " to write") if ticked else "")
        self.start_hint.configure(text=self._start_blocker())
        self._schedule_rails()

    def _schedule_rails(self):
        if self._rails_pending:
            return
        self._rails_pending = True
        try:
            self._outer.after_idle(self._place_rails)
        except tk.TclError:
            self._rails_pending = False

    def _place_rails(self):
        self._rails_pending = False
        heads = [self._heads[key] for key in self._step_keys()]
        try:
            showing = bool(self.setup.winfo_manager())
            ox, oy = self._outer.winfo_rootx(), self._outer.winfo_rooty()
            for i, rail in enumerate(self._rails):
                if not showing or i + 1 >= len(heads):
                    rail.place_forget()
                    continue
                upper, lower = heads[i].badge, heads[i + 1].badge
                top = upper.winfo_rooty() - oy + upper.winfo_reqheight() + 3
                bottom = lower.winfo_rooty() - oy - 3
                if bottom - top < 4:
                    rail.place_forget()
                    continue
                rail.configure(background=_RAIL["done" if heads[i].state == "done" else "todo"])
                rail.place(x=upper.winfo_rootx() - ox + upper.winfo_reqwidth() // 2 - 1, y=top,
                           width=2, height=bottom - top)
        except tk.TclError:
            pass

    def _start_blocker(self) -> str:
        step = {key: n for n, key in enumerate(self._step_keys(), start=1)}
        if self.model is None:
            return "Identify the radio first (step " + str(step["identify"]) + ")."
        if self._dl_pending:
            return "Downloading…"
        if any(r.paths and r.result is None and r.error is None for r in self.rows):
            return "Checking the files…"
        if not any(r.result is not None for r in self.rows):
            if self.source_var.get() == "server":
                return "Fetch a version first (step " + str(step["files"]) + ")."
            return "Choose the files for at least one target (step " + str(step["targets"]) + ")."
        if not any(r.ready for r in self.rows):
            return "Tick at least one target (step " + str(step["targets"]) + ")."
        if not self.confirm.get():
            return "Tick the agreement above to start."
        return ""

    def _lock_model(self, locked: bool):
        for rb in (getattr(self, "_model_btns", []) + getattr(self, "_src_btns", [])
                   + getattr(self, "_identify_widgets", [])):
            rb.state(["disabled"] if locked else ["!disabled"])
        server = self.source_var.get() == "server"
        for w in (getattr(self, "version_box", None), getattr(self, "fetch_btn", None),
                  getattr(self, "reload_btn", None)):
            if w is not None:
                w.state(["disabled"] if (locked or not server) else ["!disabled"])
        if getattr(self, "cps_btn", None) is not None:
            if locked or not server:
                self.cps_btn.state(["disabled"])
            else:
                self._on_version_selected()

    def _plan_has_fw(self) -> bool:
        return any(p.kind == spec.KIND_FW for p in self.plan)

    def _start(self):
        self.plan = [r.result for r in self.rows if r.ready]
        if not self.plan:
            messagebox.showwarning("Nothing selected", "Choose and tick at least one target first.")
            return
        self._begin_radio()

    def _flow_model_buttons(self, _event=None):
        try:
            width = self._model_row.winfo_width()
        except tk.TclError:
            return
        if width <= 1:
            width = 10 ** 6
        label = self._model_row.grid_slaves(row=0, column=0)
        avail = width - (label[0].winfo_reqwidth() if label else 0)
        layout, line, x = [], 0, 0
        for rb in self._model_btns:
            w = rb.winfo_reqwidth() + 8
            if x and x + w > avail:
                line, x = line + 1, 0
            layout.append(line)
            x += w
        if layout == self._model_layout:
            return
        self._model_layout = layout
        lines = getattr(self, "_model_lines", [])
        while len(lines) <= max(layout):
            frame = ttk.Frame(self._model_row)
            frame.grid(row=len(lines), column=1, sticky="w")
            lines.append(frame)
        self._model_lines = lines
        for rb in self._model_btns:
            rb.pack_forget()
        for rb, n in zip(self._model_btns, layout):
            rb.pack(in_=lines[n], side="left", padx=(8, 0))
            rb.lift(lines[n])

    def _build_identify(self, outer):
        box = self.identify_box = ttk.Frame(outer)
        head = self._heads["identify"] = _StepHead(self, box, "Identify the radio")
        head.frame.pack(fill="x")
        self.identify_toggle = ttk.Label(head.frame, text="", font=("", 12))
        self.identify_toggle.pack(side="left", padx=(6, 0))
        self.identify_summary = ttk.Label(head.frame, text="", foreground="#127a2e")
        self.identify_summary.pack(side="left", padx=(8, 0))
        for w in (head.title, self.identify_toggle, self.identify_summary):
            w.configure(cursor="hand2")
            w.bind("<Button-1>", lambda _e: self._toggle_identify())
        body = self.identify_body = ttk.Frame(box)
        help_lbl = ttk.Label(body, justify="left", foreground="#444",
                             text="Turn on your radio normally and plug in the USB cable; we will read "
                                  "and identify the variant.")
        help_lbl.pack(fill="x", pady=(4, 0))
        _follow_width(help_lbl, body, reserve=8)
        row = self._identify_row = ttk.Frame(body)
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text="COM port:").pack(side="left")
        self.id_port_var = tk.StringVar()
        self.id_port_box = ttk.Combobox(row, textvariable=self.id_port_var, state="readonly", width=20)
        self.id_port_box.pack(side="left", padx=6, fill="x", expand=True)
        id_refresh = ttk.Button(row, text="Refresh", command=self._refresh_id_ports)
        id_refresh.pack(side="left")
        self.identify_btn = _AccentButton(row, text="Identify", command=self._on_identify)
        self.identify_btn.pack(side="left", padx=(8, 0))
        self.manual_btn = ttk.Button(row, text="Choose manually…", command=self._toggle_manual)
        self.manual_btn.pack(side="left", padx=(8, 0))
        self.identify_status = ttk.Label(body, text="", justify="left", foreground="#444")
        _follow_width(self.identify_status, body, reserve=8)
        manual = self.identify_manual = ttk.Frame(body)
        ttk.Label(manual, text="Variant:").pack(side="left")
        self.manual_model_var = tk.StringVar()
        self.manual_model_box = ttk.Combobox(manual, textvariable=self.manual_model_var,
                                             state="readonly", width=18)
        self.manual_model_box.pack(side="left", padx=6)
        self.manual_chip_lbl = ttk.Label(manual, text="MCU:")
        self.manual_chip_lbl.pack(side="left", padx=(6, 0))
        self.manual_chip_var = tk.StringVar()
        self.manual_chip_box = ttk.Combobox(manual, textvariable=self.manual_chip_var,
                                            state="readonly", width=15)
        self.manual_chip_box.pack(side="left", padx=6)
        manual_use = ttk.Button(manual, text="Use this", command=self._on_manual_identity)
        manual_use.pack(side="left", padx=(6, 0))
        self._identify_widgets = [self.id_port_box, id_refresh, self.identify_btn, self.manual_btn,
                                  self.manual_model_box, self.manual_chip_box, manual_use]
        self._id_port_map: dict[str, str] = {}
        self._id_port_chips: dict[str, str | None] = {}
        self._identify_open = True
        self._manual_open = False

    def _show_identify(self):
        if not self.family:
            self.identify_box.pack_forget()
            return
        self._manual_models = {spec.variant_label(self.family, m): m for m in spec.family_members(self.family)}
        self._manual_chips = {spec.chip_label(self.family, c): c for c in spec.family_chips(self.family)}
        self.manual_model_box["values"] = list(self._manual_models)
        self.manual_chip_box["values"] = list(self._manual_chips)
        if len(self._manual_chips) > 1:
            self.manual_chip_lbl.pack(side="left", padx=(6, 0), after=self.manual_model_box)
            self.manual_chip_box.pack(side="left", padx=6, after=self.manual_chip_lbl)
        else:
            self.manual_chip_lbl.pack_forget()
            self.manual_chip_box.pack_forget()
        self.identify_box.pack(fill="x", pady=(0, 6), after=self._model_row)
        self._refresh_id_ports()
        self._identify_note("", "#444")
        self._identify_open = self.family not in self._identified
        self._manual_open = False
        self._layout_identify()

    def _toggle_identify(self):
        self._identify_open = not self._identify_open
        self._layout_identify()

    def _toggle_manual(self):
        self._manual_open = not self._manual_open
        self._layout_identify()

    def _layout_identify(self):
        known = self._identified.get(self.family)
        self.identify_toggle.configure(text="▾" if self._identify_open else "▸")
        self.identify_summary.configure(text=self._identity_summary(known) if known else "")
        if not self._identify_open:
            self.identify_body.pack_forget()
            return
        self.identify_body.pack(fill="x", padx=(self._indent, 0))
        if known and known[2]:
            self.manual_btn.pack_forget()
            self.identify_manual.pack_forget()
            return
        self.manual_btn.pack(side="left", padx=(8, 0), after=self.identify_btn)
        self.manual_btn.configure(text="Hide manual choice" if self._manual_open else "Choose manually…")
        if self._manual_open:
            self.identify_manual.pack(fill="x", pady=(6, 0))
        else:
            self.identify_manual.pack_forget()

    def _identity_summary(self, known):
        model, chip, auto, detail = known
        return (spec.variant_label(self.family, model) + self._chip_part(chip)
                + (" — identified from the radio" + (" (" + detail + ")" if detail else "") if auto
                   else " — chosen manually"))

    def _radio_description(self) -> str:
        known = self._identified.get(self.family) if self.family else None
        if not known:
            return spec.model_label(self.model)
        model, chip, auto, detail = known
        return (spec.variant_label(self.family, model) + self._chip_part(chip)
                + (" (identified: " + detail + ")" if auto else " (chosen manually)"))

    def _chip_part(self, chip) -> str:
        return ", " + spec.chip_label(self.family, chip) if len(spec.family_chips(self.family or "")) > 1 else ""

    def _identify_note(self, text, color):
        self.identify_status.configure(text=text, foreground=color)
        if text:
            self.identify_status.pack(fill="x", pady=(4, 0), after=self._identify_row)
        else:
            self.identify_status.pack_forget()

    def _refresh_id_ports(self):
        chips = tuple(c for m in spec.family_members(self.family or "") for c in spec.usb_chips(m))
        ports = _ports.ordered(list_ports.comports(), chips)
        self._id_port_map = {_ports.label(p): p.device for p in ports}
        self._id_port_chips = {_ports.label(p): _ports.chip_of(p) for p in ports}
        labels = list(self._id_port_map)
        self.id_port_box["values"] = labels
        if labels:
            self.id_port_box.current(0)
        else:
            self.id_port_var.set("")

    def _on_identify(self):
        if self._writing or self._identifying:
            return
        label = self.id_port_var.get()
        port = self._id_port_map.get(label)
        if not port:
            messagebox.showwarning("No COM port", "Pick the radio's COM port. Click Refresh if it isn't listed.")
            return
        self._identifying = True
        self.identify_btn.state(["disabled"])
        self._identify_note("Reading the radio…", "#444")
        threading.Thread(target=self._identify_worker,
                         args=(self.family, port, self._id_port_chips.get(label)), daemon=True).start()

    def _identify_worker(self, family, port, chip):
        try:
            ident = _pc_engine.identify(port, on_log=lambda *_a: None)
            self._post("identified", (family, ident.model, ident.version, chip, None))
        except Exception as e:
            self._post("identified", (family, None, None, chip, str(e)))

    def _on_identified(self, family, pc_model, version, chip, error):
        self._identifying = False
        self.identify_btn.state(["!disabled"])
        if family != self.family:
            return
        self._identify_open = True
        if error:
            self._identify_note("Could not identify the radio: " + error + "\nCheck it is on normally "
                                "and that this is its port, or press “Choose manually…”.", "#b00020")
            self._layout_identify()
            return
        model = spec.identified_model(family, pc_model)
        if model is None:
            other = spec.PC_IDENT_MODEL.get(pc_model)
            self._identify_note("This radio identifies as " + (pc_model or "?")
                                + (", which is updated on the " + spec.choice_label(spec.family_of(other))
                                   + " button" if other else ", which this app does not update")
                                + " — not a " + spec.choice_label(family) + ".", "#b00020")
            self._layout_identify()
            return
        if len(spec.family_chips(family)) == 1:
            chip = spec.family_chips(family)[0]
        if chip not in spec.family_chips(family):
            for text, m in self._manual_models.items():
                if m == model:
                    self.manual_model_var.set(text)
            self._identify_note("This is a " + spec.variant_label(family, model) + ", but its port does not "
                                "say which MCU it has: choose the MCU below.", "#8a6d00")
            self._manual_open = True
            self._layout_identify()
            return
        self._apply_identity(model, chip, True, pc_model + (" " + version if version else ""))

    def _on_manual_identity(self):
        model = self._manual_models.get(self.manual_model_var.get())
        chips = spec.family_chips(self.family or "")
        chip = chips[0] if len(chips) == 1 else self._manual_chips.get(self.manual_chip_var.get())
        if not model or not chip:
            messagebox.showwarning("Choose the radio", "Choose the variant and the MCU first.")
            return
        self._apply_identity(model, chip, False, "")

    def _apply_identity(self, model, chip, auto, detail):
        self._identified[self.family] = (model, chip, auto, detail)
        self.model, self.radio_chip = model, chip
        self._identify_note("", "#444")
        self._identify_open = False
        self._manual_open = False
        self._layout_identify()
        self._show_model_notes()
        self._build_rows()
        self.confirm.set(False)
        if self.source_var.get() == "server":
            self._populate_versions()
        self._refresh_start_state()

    def _show_model_notes(self):
        above = self.identify_box if self.family else self._model_row
        if self.model is None:
            self.covers_lbl.pack_forget()
            self.untested_lbl.pack_forget()
            return
        covers = "" if self.family else spec.covers(self.model)
        if covers:
            self.covers_lbl.configure(text=covers)
            self.covers_lbl.pack(fill="x", padx=(self._indent, 0), pady=(0, 6), after=above)
            above = self.covers_lbl
        else:
            self.covers_lbl.pack_forget()
        if spec.is_untested(self.model):
            self.untested_lbl.configure(text="⚠ " + spec.untested_warning(self.model))
            self.untested_lbl.pack(fill="x", padx=(self._indent, 0), pady=(0, 6), after=above)
        else:
            self.untested_lbl.pack_forget()

    def _begin_radio(self):
        if spec.is_untested(self.model) and not messagebox.askyesno(
                "Not tested on real hardware",
                spec.untested_warning(self.model) + "\n\nContinue anyway?",
                icon=messagebox.WARNING):
            return
        if self._plan_has_fw() and spec.has_codeplug(self.model):
            if not messagebox.askyesno(
                    "Back up your codeplug first",
                    "This batch writes the Radio Firmware, which can reset the radio's settings.\n\n"
                    "Read the codeplug from THIS radio and save it to your PC before continuing.\n\n"
                    "Is this radio's codeplug backed up?"):
                return
        self._lock_model(True)
        self.step = 0
        self.results = [{"kind": p.kind, "label": spec.label(p.kind, self.model), "state": "pending"} for p in self.plan]
        self._clear_log()
        self._log(download._user_agent() + " · " + self._radio_description() + " · targets: "
                  + ", ".join(r["label"] for r in self.results))
        self.wcontext.configure(text=self._batch_context())
        self._build_track()
        self.top.pack_forget()
        self.setup.pack_forget()
        self.done.pack_forget()
        for rail in self._rails:
            rail.place_forget()
        self.wizard.pack(fill="both", expand=True)
        self._show_step()

    def _batch_context(self) -> str:
        radio = (spec.variant_label(self.family, self.model) + self._chip_part(self.radio_chip)
                 if self.family else spec.model_label(self.model))
        files = (self._fetched_label if self.source_var.get() == "server" and self._fetched_label
                 else "your own files")
        return radio + " · " + files

    def _build_track(self):
        for w in self.track.winfo_children():
            w.destroy()
        self._track_items = []
        for i, r in enumerate(self.results):
            if i:
                line = ttk.Frame(self.track, width=24, height=self._badge_size)
                line.pack_propagate(False)
                ttk.Separator(line).pack(fill="x", expand=True)
                line.pack(side="left", padx=6)
            badge = ttk.Label(self.track, compound="center", foreground="white", font=_BADGE_FONT)
            badge.pack(side="left")
            name = ttk.Label(self.track, text=r["label"])
            name.pack(side="left", padx=(6, 0))
            self._track_items.append((badge, name))

    def _update_track(self):
        bold = self._bold_font()
        for i, ((badge, name), r) in enumerate(zip(self._track_items, self.results)):
            state = r["state"]
            if state == "done":
                look, mark, fg = "done", "✓", ""
            elif state in ("failed", "aborted"):
                look, mark, fg = "failed", "✕", "#b00020"
            elif state == "skipped":
                look, mark, fg = "todo", "–", _TODO_FG
            elif i == self.step:
                look, mark, fg = "current", str(i + 1), ""
            else:
                look, mark, fg = "todo", str(i + 1), _TODO_FG
            badge.configure(image=self._badge(look), text=mark)
            name.configure(foreground=fg, font=bold if i == self.step else "")
        self._fit_track()

    def _fit_track(self):
        try:
            avail = self.wizard.winfo_width()
        except tk.TclError:
            return
        if avail <= 1 or not self._track_items:
            return
        full = sum(b.winfo_reqwidth() + n.winfo_reqwidth() + 6 for b, n in self._track_items) \
            + (len(self._track_items) - 1) * 36
        for i, (badge, name) in enumerate(self._track_items):
            show = full <= avail or i == self.step
            if show and not name.winfo_manager():
                name.pack(side="left", padx=(6, 0), after=badge)
            elif not show and name.winfo_manager():
                name.pack_forget()

    def _bold_font(self):
        if not hasattr(self, "_bold"):
            self._bold = tkfont.nametofont("TkDefaultFont").copy()
            self._bold.configure(weight="bold")
        return self._bold

    def _banner(self, kind, text, widget=None):
        bg, fg = _BANNER[kind]
        (widget or self.wstatus).configure(text=text, background=bg, foreground=fg)

    def _show_step(self):
        if self.step >= len(self.plan):
            self._finish_radio()
            return
        comp = self.plan[self.step]
        kind = comp.kind
        self.wtitle.configure(text=spec.label(kind, self.model))
        self._update_track()
        instr = spec.entry_instructions(self.model, kind)
        if spec.is_unconfirmed(self.model, kind):
            instr = "⚠ Entry combo not yet confirmed — verify before continuing.\n\n" + instr
        self.winstr.configure(text=instr)
        photo = spec.step_image(self.model, kind)
        self._step_natural = _load_step_image(photo) if photo else None
        self._step_src = _load_step_source(photo) if photo else None
        self._step_img = self._step_natural
        self.wimage.configure(image=self._step_img if self._step_img else "")

        self.port_row.pack_forget()
        self.progress_row.pack_forget()
        self.wstatus.pack_forget()
        self.abort_btn.pack_forget()
        self.next_btn.pack_forget()
        self.wctl.pack(fill="x", pady=(4, 0), after=self._wbody)
        for w in (self.ready_btn, self.skip_btn):
            w.pack_forget()
        self.ready_btn.configure(text="The radio is in this mode →", state="normal")
        self.ready_btn.pack(in_=self.wctl, side="left")
        self.skip_btn.configure(text="Skip this target", state="normal")
        self.skip_btn.pack(in_=self.wctl, side="left", padx=(8, 0))
        self.skip_btn.lift(self.wctl)
        self.connect_btn.configure(text="Connect && Write")
        self._schedule_fit()

    def _schedule_fit(self):
        if self._fit_pending:
            return
        self._fit_pending = True
        try:
            self.wizard.after_idle(self._fit_step_image)
        except tk.TclError:
            self._fit_pending = False

    def _fit_step_image(self):
        self._fit_pending = False
        try:
            if self.wizard.winfo_ismapped():
                self._step_img = self._fit_photo(
                    self.wimage, self._step_img, self._step_natural, self._step_src,
                    container=self.wizard, block=self._wbody,
                    give=self.log.frame, reserve=_LOG_RESERVE)
            elif self.done.winfo_ismapped() and self.mcu_box.winfo_ismapped():
                self._mcu_img = self._fit_photo(
                    self._mcu_label, self._mcu_img, self._mcu_natural, self._mcu_src,
                    container=self.done, block=self._mcu_body)
        except tk.TclError:
            pass

    @staticmethod
    def _fit_photo(label, shown, natural, src, container, block, give=None, reserve=0):
        if natural is None or src is None:
            return shown
        total = container.winfo_height()
        if total <= 1:
            return shown
        fixed = container.winfo_reqheight() - block.winfo_reqheight()
        if give is not None:
            fixed -= give.winfo_reqheight()
        room = total - fixed - reserve
        target_h = max(_STEP_MIN_H, min(natural.height(), room))
        if shown is not None and abs(shown.height() - target_h) < 8:
            return shown
        if target_h >= natural.height():
            img = natural
        else:
            img = _scaled_photo(src, max(1, round(src.width() * target_h / src.height())))
        label.configure(image=img)
        return img

    def _on_ready(self):
        self.ready_btn.pack_forget()
        self.skip_btn.pack(in_=self._port_actions, side="left", padx=(8, 0))
        self.skip_btn.lift(self._port_actions)
        self.progress_row.pack_forget()
        self.wstatus.pack_forget()
        self.wctl.pack_forget()
        self._refresh_ports()
        if self.plan[self.step].kind == spec.KIND_SCT:
            manifest = getattr(self.plan[self.step], "manifest", None) or {}
            chip, default_baud, alt_baud = engines.SCT_CHIPS.get(
                manifest.get("kind"), ("", engines.SCT_BAUD, engines.SCT_ALT_BAUD))
            self._sct_baud_box.configure(values=[str(default_baud), str(alt_baud)])
            if chip != getattr(self, "_sct_baud_chip", None):
                self._sct_baud.set(str(default_baud))
                self._sct_baud_chip = chip
            self.sct_baud_row.grid(row=0, column=3, sticky="w", padx=(12, 0))
        else:
            self.sct_baud_row.grid_forget()
        self.port_row.pack(fill="x", pady=(4, 0), before=self.log_lbl)
        self._schedule_fit()

    def _refresh_ports(self):
        ports = _ports.ordered(list_ports.comports(), spec.usb_chips(self.model))
        labels = []
        self._port_map = {}
        self._port_chips = {}
        for p in ports:
            label = _ports.label(p)
            labels.append(label)
            self._port_map[label] = p.device
            self._port_chips[label] = _ports.chip_of(p)
        self.port_box["values"] = labels
        if labels:
            pick = 0
            if self._last_port:
                for i, lb in enumerate(labels):
                    if self._port_map[lb] == self._last_port:
                        pick = i
                        break
            self.port_box.current(pick)
        else:
            self.port_var.set("")

    def _on_connect(self):
        label = self.port_var.get()
        port = getattr(self, "_port_map", {}).get(label)
        if not port:
            messagebox.showwarning("No COM port", "Pick the radio's COM port. Click Refresh if it isn't listed.")
            return
        comp = self.plan[self.step]
        if comp.kind == spec.KIND_FW:
            port_chip = getattr(self, "_port_chips", {}).get(label)
            refusal = spec.fw_port_mismatch(self.model, getattr(comp, "manifest", None), port_chip, port)
            if refusal:
                self._log("Refused: " + refusal.split("\n\n")[0], "er")
                messagebox.showerror("Firmware for the other MCU", refusal)
                return
        self._last_port = port
        self.results[self.step]["state"] = "writing"
        self.results[self.step]["t0"] = time.monotonic()
        names = ", ".join(getattr(comp, "source_names", None) or []) or "?"
        sha = (getattr(comp, "sha256", None) or "")[:10]
        frames = getattr(comp, "frames", None)
        details = ([format(frames, ",") + " frames"] if frames else []) + (["sha " + sha] if sha else [])
        if self.source_var.get() == "server" and self._fetched_label:
            details.append("version " + self._fetched_label)
        self._log("writing through " + port + ": " + names + (" (" + ", ".join(details) + ")" if details else ""))

        sct_baud = None
        if comp.kind == spec.KIND_SCT:
            try:
                sct_baud = int(self._sct_baud.get())
            except (ValueError, tk.TclError):
                sct_baud = None

        self._enter_writing_state()
        self._update_track()

        self._writing = True
        self._abort = threading.Event()
        threading.Thread(target=self._stage_worker, args=(comp, port, self._abort, sct_baud),
                         daemon=True).start()

    def _enter_writing_state(self):
        self.port_row.pack_forget()
        self.skip_btn.pack_forget()
        self.progress["value"] = 0
        self.pct_lbl.configure(text="0%")
        self._eta_start = None
        self.progress_row.pack(fill="x", pady=(6, 0), before=self.log_lbl)
        self._banner("info", "Connecting to the radio…")
        self.wstatus.pack(fill="x", pady=(6, 0), before=self.log_lbl)
        self.abort_btn.pack(anchor="w", pady=(6, 0), before=self.log_lbl)
        self._schedule_fit()

    def _on_progress(self, done, total, phase):
        pct = (done * 100.0 / total) if total else 0.0
        self.progress["value"] = pct
        self.pct_lbl.configure(text=format(pct, ".0f") + "%")
        label = spec.label(self.plan[self.step].kind, self.model) if 0 <= self.step < len(self.plan) else ""
        left = self._stage_eta(phase, done, total)
        if phase == "handshake":
            text = "Connecting to the radio…"
        elif phase in ("finish", "done"):
            text = "Finishing " + label + " — do not unplug the radio."
        else:
            text = ("Writing " + label + (", " + _fmt_eta(left) + " left" if left is not None else "")
                    + " — do not unplug the radio or turn it off.")
        self._banner("info", text)

    def _stage_eta(self, phase, done, total):
        if phase not in ("write", "serve") or not total:
            return None
        now = time.monotonic()
        start = self._eta_start
        if start is None or start[0] != phase or done < start[2]:
            self._eta_start = (phase, now, done)
            return None
        _, t0, done0 = start
        if done <= done0 or now - t0 < 2.0:
            return None
        return (total - done) * (now - t0) / (done - done0)

    def _stage_worker(self, comp, port, abort, sct_baud):
        opts = {}
        if comp.kind == spec.KIND_SCT and sct_baud:
            opts["baud"] = sct_baud
        try:
            engines.run(comp.kind, port, comp.artifact, comp.manifest,
                        on_log=lambda m, c="info": self._post("log", (m, c)),
                        on_progress=lambda d, t, p: self._post("progress", (d, t, p)),
                        abort=abort, **opts)
            self._post("stage_done", None)
        except engines.AbortedError as e:
            self._post("stage_error", (str(e), True))
        except Exception as e:
            self._post("stage_error", (str(e), False))

    def _on_stage_done(self):
        self._writing = False
        result = self.results[self.step]
        result["state"] = "done"
        took = self._took(result)
        label = spec.label(self.plan[self.step].kind, self.model)
        self._log(label + " written: every frame was acknowledged. Nothing is read back; check the version "
                  "on the radio.", "done")
        self.progress["value"] = 100
        self.pct_lbl.configure(text="100%")
        self._banner("ok", "✓ " + label + " written" + (" in " + _fmt_took(took) if took is not None else "")
                     + ". Nothing is read back — the result is only visible when the radio boots.")
        self.abort_btn.pack_forget()
        nxt = "Next: " + spec.label(self.plan[self.step + 1].kind, self.model) if self.step + 1 < len(self.plan) \
            else "Finish this radio"
        self.next_btn.configure(text=nxt)
        self.next_btn.pack(anchor="w", pady=(6, 0), before=self.log_lbl)
        self._update_track()
        self._schedule_fit()

    @staticmethod
    def _took(result):
        if "t0" not in result:
            return None
        result["seconds"] = time.monotonic() - result["t0"]
        return result["seconds"]

    def _on_stage_error(self, msg, aborted):
        self._writing = False
        result = self.results[self.step]
        result["state"] = "aborted" if aborted else "failed"
        self._took(result)
        self._log(("Stopped: " if aborted else "FAILED: ") + msg, "er")
        untouched = not aborted and "Nothing was written" in msg
        self._banner("error", ("✕ Stopped. " if aborted else "✕ Failed: " + msg + "  ")
                     + ("" if untouched else "This radio may be partly written — do not power-cycle it "
                                             "before deciding."))
        self.abort_btn.pack_forget()
        self.wctl.pack(fill="x", pady=(6, 0), before=self.log_lbl)
        self.ready_btn.configure(text="Retry — the radio is in this mode →", state="normal")
        self.ready_btn.pack(in_=self.wctl, side="left")
        self.skip_btn.configure(text="Skip and continue", state="normal")
        self.skip_btn.pack(in_=self.wctl, side="left", padx=(8, 0))
        self.skip_btn.lift(self.wctl)
        self._update_track()
        self._schedule_fit()

    def _on_skip(self):
        if self._writing:
            return
        if self.results[self.step]["state"] not in ("failed", "aborted"):
            self.results[self.step]["state"] = "skipped"
        self.step += 1
        self._show_step()

    def _on_next(self):
        self.next_btn.pack_forget()
        self.step += 1
        self._show_step()

    def _on_abort(self):
        if not self._writing or self._abort is None:
            return
        if not messagebox.askyesno(
                "Stop writing NOW?",
                "An interrupted write can leave this radio unbootable.\n\nStop anyway?"):
            return
        self._abort.set()

    def _finish_radio(self):
        self.wizard.pack_forget()
        for w in self.done_summary.winfo_children():
            w.destroy()
        written = [r for r in self.results if r["state"] == "done"]
        failed = [r for r in self.results if r["state"] in ("failed", "aborted")]
        skipped = [r for r in self.results if r["state"] == "skipped"]
        others = ([str(len(written)) + " written"] if written else []) \
            + ([str(len(skipped)) + " skipped"] if skipped else [])
        if failed:
            look = "error"
            title = ("✕ " + (failed[0]["label"] if len(failed) == 1 else str(len(failed)) + " targets")
                     + " not written" + (" — " + ", ".join(others) if others else "") + ".")
        elif written:
            look = "ok"
            title = ("✓ " + (written[0]["label"] + " written." if len(self.results) == 1
                             else "All " + str(len(written)) + " targets written." if not skipped
                             else ", ".join(others) + "."))
        else:
            look, title = "info", "Nothing was written: every target was skipped."
        self._banner(look, title, widget=self.done_title)
        for i, r in enumerate(self.results):
            look, mark, colour, what = {
                "done": ("done", "✓", "#127a2e", "written"),
                "failed": ("failed", "✕", "#b00020", "failed"),
                "aborted": ("failed", "✕", "#b00020", "stopped"),
                "skipped": ("todo", "–", "#666", "skipped"),
            }.get(r["state"], ("todo", str(i + 1), "#666", r["state"]))
            if r.get("seconds") is not None and r["state"] != "skipped":
                what += (" in " if r["state"] == "done" else " after ") + _fmt_took(r["seconds"])
            ttk.Label(self.done_summary, image=self._badge(look), text=mark, compound="center",
                      foreground="white", font=_BADGE_FONT).grid(row=i, column=0, sticky="w", pady=2)
            ttk.Label(self.done_summary, text=r["label"]).grid(row=i, column=1, sticky="w", padx=(8, 18))
            ttk.Label(self.done_summary, text=what, foreground=colour).grid(row=i, column=2, sticky="w")
        fw_done = any(r["kind"] == spec.KIND_FW and r["state"] == "done" for r in self.results)
        if fw_done and spec.mcu_reset(self.model):
            self.mcu_reset_lbl.configure(text=spec.mcu_reset(self.model))
            photo = spec.reset_image(self.model)
            self._mcu_natural = _load_step_image(photo) if photo else None
            self._mcu_src = _load_step_source(photo) if photo else None
            self._mcu_img = self._mcu_natural
            self._mcu_label.configure(image=self._mcu_img if self._mcu_img else "")
            self.mcu_box.pack(fill="x", pady=(4, 8), after=self.done_summary)
        else:
            self.mcu_box.pack_forget()
        self.done.pack(fill="x")
        self._schedule_fit()

    def _again(self):
        self._begin_radio()

    def _back_to_setup(self):
        self._lock_model(False)
        self.confirm.set(False)
        self.start_btn.state(["disabled"])
        self.done.pack_forget()
        self.wizard.pack_forget()
        self.setup.pack(fill="x")
        self.top.pack(fill="x", before=self.setup)
        self._refresh_start_state()

    def _log(self, msg, cls="info"):
        step = (spec.label(self.plan[self.step].kind, self.model)
                if self.plan and 0 <= self.step < len(self.plan) else "—")
        radio = (spec.variant_label(self.family, self.model) if self.family and self.model
                 else spec.model_label(self.model) if self.model else "—")
        line = "[" + time.strftime("%H:%M:%S") + "] [" + radio + "] [" + step + "] " + msg
        self.log.configure(state="normal")
        tag = cls if cls in _LOG_TAGS else ""
        self.log.insert("end", line + "\n", (tag,) if tag else ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def is_writing(self) -> bool:
        return self._writing
