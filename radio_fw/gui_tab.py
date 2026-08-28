"""The "Radio and Boards Updates" tab: a guided, one-target-at-a-time wizard for
flashing a radio's update targets over a serial cable. A model radio button
chooses the radio; the target rows follow that model (D890UV: radio firmware,
icons, SCT3288, NR board; D878UVII: radio firmware, icons, APRS+BT board).

Mirrors the operator flow of the AesApp web CPS's admin firmware pages in the
desktop app: the operator picks the vendor files for the targets to update
(compiled + hard-validated locally by radio_fw.compiler for the chosen model),
then the wizard walks them in the model's order (radio firmware first). Each step
shows how to put the radio into that update mode plus a photo of the buttons,
waits for the operator, asks for the COM port, and streams the precompiled wire
data (radio_fw.engines).

THESE WRITES ARE NOT VERIFIED and an interrupted write can brick a radio — the
same warnings the web page carries are enforced here (a confirm gate before the
first write, hard-stop error messages, an abort warning mid-write).
"""
from __future__ import annotations

import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from serial.tools import list_ports

from . import compiler, engines, spec

_LOG_TAGS = {"tx": "#0b5fff", "rx": "#7a3fb0", "ok": "#127a2e", "er": "#b00020", "error": "#b00020"}


def _asset_path(name: str) -> str:
    """Resolve a bundled asset (the step photos live in bt_ota/assets). Probes
    the source tree and the frozen _MEIPASS root, so it works from source and
    inside a PyInstaller build."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # repo root (or _MEIPASS parent)
    for base in (here, getattr(sys, "_MEIPASS", None)):
        if not base:
            continue
        for cand in (os.path.join(base, "bt_ota", "assets", name),
                     os.path.join(base, "radio_fw", "assets", name),
                     os.path.join(base, "assets", name)):
            if os.path.exists(cand):
                return cand
    return os.path.join(here, "bt_ota", "assets", name)


_STEP_BASE_W = 380      # the historical fit-to-width for a step photo
_STEP_SCALE = 1.15      # show it 15% larger than that fit


def _scaled_photo(img: tk.PhotoImage, target_w: int) -> tk.PhotoImage:
    """Scale a PhotoImage to ~target_w with Tk's integer zoom/subsample (no
    Pillow at runtime). Searching a few zoom factors reaches a between-factors
    target (e.g. 340 px from an 888 px source) that plain integer subsample —
    which only steps 888→444→296 — cannot hit."""
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


def _load_step_image(name: str) -> tk.PhotoImage | None:
    """Load a step photo scaled to ~15% larger than the historical fit width."""
    try:
        img = tk.PhotoImage(file=_asset_path(name))
    except Exception:
        return None
    try:
        w = img.width()
        base = w / math.ceil(w / _STEP_BASE_W)     # the old fit-to-380 display size
        target = round(base * _STEP_SCALE)
        if 0 < target < w:                          # never upscale past the source
            img = _scaled_photo(img, target)
    except Exception:
        pass
    return img


def _resolve_cps_siblings(spi_path: str) -> dict[str, str]:
    """The .CDD / .CDI that share a .spi's base filename in the same folder —
    how the factory CPS finds them from the selected .spi. Case-insensitive on
    the extension; returns {"cdd": path, "cdi": path} for whatever is present."""
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


class _Row:
    """One target's row in the setup view: checkbox + file picker + status."""

    def __init__(self, tab, parent, kind, index):
        self.tab = tab
        self.kind = kind
        self.result: compiler.CompileResult | None = None
        self.error: str | None = None
        self.paths: list[str] = []
        self.picked: dict[str, str] = {}   # files gathered by extension (multi-file kinds)
        self._gen = 0   # compile generation; see _pick (stale-compile guard)

        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=3)
        frame.columnconfigure(2, weight=1)

        self.checked = tk.BooleanVar(value=False)
        self.cb = ttk.Checkbutton(frame, variable=self.checked, command=tab._refresh_start_state)
        self.cb.grid(row=0, column=0, sticky="w")
        self.cb.state(["disabled"])   # enabled once a package compiles

        ttk.Label(frame, text=str(index) + ". " + spec.label(kind), width=20).grid(row=0, column=1, sticky="w")

        # CPS kinds (fw/icon/aprs) are a .spi + sibling .CDD/.CDI in one folder;
        # like the factory CPS, the operator picks just the .spi and the .CDD/.CDI
        # load automatically. Single-file kinds (sct/nr) name their one extension.
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

    def _pick(self):
        multi = spec.is_multi(self.kind)
        exts = spec.accepts(self.kind)         # cps: [cdd, cdi, spi]
        req_exts = spec.requires(self.kind)    # cps: [cdd, cdi]
        patterns = " ".join("*." + e for e in exts)
        if multi:
            types = [("Update package (.spi)", "*.spi"),
                     (spec.label(self.kind) + " files (." + " .".join(e.upper() for e in exts) + ")", patterns),
                     ("All files", "*.*")]
            title = ("Select the .spi file — the .CDD and .CDI load automatically from the same "
                     "folder (or Shift-select the .CDD and .CDI yourself)")
            picked = list(filedialog.askopenfilenames(title=title, filetypes=types))
        else:
            types = [(spec.label(self.kind) + " files", patterns), ("All files", "*.*")]
            p = filedialog.askopenfilename(title="Choose the " + spec.label(self.kind) + " file",
                                           filetypes=types)
            picked = [p] if p else []
        if not picked:
            return

        if multi:
            new = {os.path.splitext(p)[1].lower().lstrip("."): p for p in picked
                   if os.path.splitext(p)[1].lower().lstrip(".") in exts}
            if "spi" in new:
                # The .spi is the authoritative entry point (like the factory CPS):
                # reset to it and auto-load the sibling .CDD/.CDI from its folder.
                self.picked = {"spi": new["spi"], **_resolve_cps_siblings(new["spi"])}
            else:
                # No .spi — manual fallback: accumulate the .CDD/.CDI as they come.
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
        # compile off the UI thread (the ICON package is ~126k frames). Bump a
        # generation so a slow earlier compile that finishes after a re-pick is
        # dropped rather than overwriting the newer selection.
        self._gen += 1
        gen = self._gen
        model = self.tab.model   # capture: the model in effect when this pick was made
        threading.Thread(target=self._compile_worker, args=(self.kind, paths, gen, model), daemon=True).start()

    def _compile_worker(self, kind, paths, gen, model):
        try:
            res = compiler.compile_files(kind, paths, model)
            self.tab._post("compiled", (self, res, None, gen))
        except compiler.CompileError as e:
            self.tab._post("compiled", (self, None, str(e), gen))
        except Exception as e:  # noqa: BLE001
            self.tab._post("compiled", (self, None, str(e), gen))

    def apply_compile(self, res, error):
        self.result = res
        self.error = error
        if res is not None:
            names = ", ".join(res.source_names)
            self.status.configure(
                text="✓ " + names + " — " + format(res.frames, ",") + " frames, "
                     + format(res.payload_bytes, ",") + " payload bytes",
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


class RadioBoardsTab:
    """Builds the whole tab inside `parent` (a Notebook page frame). `root` is
    the Tk root, used for the .after() UI poll and modal dialogs."""

    def __init__(self, parent: tk.Widget, root: tk.Tk):
        self.parent = parent
        self.root = root
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self.model = spec.DEFAULT_MODEL
        self.model_var = tk.StringVar(value=self.model)
        self.rows: list[_Row] = []
        self.plan: list[compiler.CompileResult] = []
        self.step = 0
        self.results: list[dict] = []
        self._writing = False
        self._abort: threading.Event | None = None
        self._step_img = None       # keep a ref so Tk doesn't GC the photo
        self._last_port = None
        self._sct_baud = tk.StringVar(value="115200")

        self._build()
        self.root.after(80, self._drain)

    # ---- thread-safe UI plumbing -------------------------------------------
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
            # drop a stale compile: a re-pick superseded it, or a model switch
            # destroyed the row.
            if gen == row._gen and row in self.rows:
                row.apply_compile(res, err)
        elif kind == "log":
            msg, cls = payload
            self._log(msg, cls)
        elif kind == "progress":
            done, total, phase = payload
            pct = (done * 100.0 / total) if total else 0.0
            self.progress["value"] = pct
            self.wstatus.configure(text="Writing (" + phase + ") — do not unplug the radio.  "
                                        + format(pct, ".0f") + "%", foreground="#444")
        elif kind == "stage_done":
            self._on_stage_done()
        elif kind == "stage_error":
            msg, aborted = payload
            self._on_stage_error(msg, aborted)

    # ---- model + rows -------------------------------------------------------
    def _build_rows(self):
        """(Re)build the target rows for the current model, in its write order."""
        for w in self.rows_box.winfo_children():
            w.destroy()
        self.rows = []
        for i, kind in enumerate(spec.model_order(self.model), start=1):
            self.rows.append(_Row(self, self.rows_box, kind, i))

    def _on_model_change(self):
        if self._writing:
            return
        self.model = self.model_var.get()
        self._build_rows()
        self.confirm.set(False)
        self._refresh_start_state()

    # ---- build the views ----------------------------------------------------
    def _build(self):
        outer = ttk.Frame(self.parent)
        outer.pack(fill="both", expand=True, padx=10, pady=8)

        # Radio model chooser — the targets below are per model, and each model's
        # CPS packages are compiled for that radio's own addresses/identity, so
        # picking the wrong model is refused at compile time.
        model_row = ttk.Frame(outer)
        model_row.pack(fill="x", pady=(0, 6))
        ttk.Label(model_row, text="Radio model:", font=("", 11, "bold")).pack(side="left")
        self._model_btns = []
        for m in spec.MODEL_ORDER:
            rb = ttk.Radiobutton(model_row, text=spec.model_label(m), value=m,
                                 variable=self.model_var, command=self._on_model_change)
            rb.pack(side="left", padx=(8, 0))
            self._model_btns.append(rb)

        banner = ttk.Label(
            outer, justify="left", foreground="#7a1020", wraplength=640,
            text=("These are NOT codeplugs. They replace the radio's main firmware, its baseband DSP, "
                  "its NR/APRS daughterboard, or its icon/font flash. Nothing here is verified or read "
                  "back — an interrupted write can leave a radio that will not boot. Keep the radio "
                  "powered and the cable in for the whole update, and only load files built for your "
                  "exact model."))
        banner.pack(fill="x", pady=(0, 8))

        # --- setup view ---
        self.setup = ttk.Frame(outer)
        self.setup.pack(fill="both", expand=True)
        ttk.Label(self.setup, text="Choose what to update", font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(self.setup, justify="left", foreground="#444", wraplength=640,
                  text=("Pick the vendor files for each target you want to write. Each package is compiled "
                        "and checked here before anything is sent. Targets are written in the order shown "
                        "(radio firmware first) — you can skip any, but not reorder them.")
                  ).pack(anchor="w", pady=(0, 6))

        self.rows_box = ttk.Frame(self.setup)
        self.rows_box.pack(fill="x")
        self._build_rows()

        ttk.Separator(self.setup).pack(fill="x", pady=8)
        self.confirm = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self.setup, variable=self.confirm, command=self._refresh_start_state,
            text="I understand these writes are not verified and that an interrupted write can brick a radio."
        ).pack(anchor="w")
        self.start_btn = ttk.Button(self.setup, text="Start Upgrades", command=self._start)
        self.start_btn.pack(anchor="w", pady=(8, 0))
        self.start_btn.state(["disabled"])

        # --- wizard view ---
        self.wizard = ttk.Frame(outer)
        self.wtitle = ttk.Label(self.wizard, text="—", font=("", 13, "bold"))
        self.wtitle.pack(anchor="w")
        self.wcount = ttk.Label(self.wizard, text="", foreground="#666")
        self.wcount.pack(anchor="w")

        body = ttk.Frame(self.wizard)
        body.pack(fill="x", pady=6)
        self.wimage = ttk.Label(body)
        self.wimage.pack(side="left", anchor="n", padx=(0, 12))
        self.winstr = ttk.Label(body, justify="left", wraplength=380,
                                foreground="#7a1020", font=("", 11))
        self.winstr.pack(side="left", anchor="n", fill="x", expand=True)

        # controls: [Ready] -> [port picker + Connect] -> [progress] -> [Next]
        self.wctl = ttk.Frame(self.wizard)
        self.wctl.pack(fill="x", pady=(4, 0))

        self.ready_btn = ttk.Button(self.wctl, text="The radio is in this mode →", command=self._on_ready)
        self.skip_btn = ttk.Button(self.wctl, text="Skip this target", command=self._on_skip)

        self.port_row = ttk.Frame(self.wizard)
        ttk.Label(self.port_row, text="COM port:").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(self.port_row, textvariable=self.port_var, state="readonly", width=42)
        self.port_box.grid(row=0, column=1, sticky="ew", padx=6)
        self.port_row.columnconfigure(1, weight=1)
        ttk.Button(self.port_row, text="Refresh", command=self._refresh_ports).grid(row=0, column=2)
        self.sct_baud_row = ttk.Frame(self.port_row)
        ttk.Label(self.sct_baud_row, text="baud:").pack(side="left")
        ttk.Combobox(self.sct_baud_row, textvariable=self._sct_baud, state="readonly", width=8,
                     values=["115200", "38400"]).pack(side="left", padx=(4, 0))
        self.connect_btn = ttk.Button(self.port_row, text="Connect && Write", command=self._on_connect)
        self.connect_btn.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        self.progress = ttk.Progressbar(self.wizard, mode="determinate", maximum=100.0)
        self.wstatus = ttk.Label(self.wizard, text="", foreground="#444")
        self.abort_btn = ttk.Button(self.wizard, text="Stop", command=self._on_abort)
        self.next_btn = ttk.Button(self.wizard, text="Next", command=self._on_next)

        ttk.Label(self.wizard, text="Protocol log").pack(anchor="w", pady=(8, 0))
        self.log = scrolledtext.ScrolledText(self.wizard, height=10, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)
        for tag, color in _LOG_TAGS.items():
            self.log.tag_configure(tag, foreground=color)

        # --- done view ---
        self.done = ttk.Frame(outer)
        ttk.Label(self.done, text="Radio finished", font=("", 13, "bold")).pack(anchor="w")
        self.done_summary = ttk.Frame(self.done)
        self.done_summary.pack(fill="x", pady=6)
        # Shown only when Radio Firmware was written this batch (see _finish_radio).
        self.mcu_box = ttk.LabelFrame(self.done, text="Reset the MCU now (Radio Firmware was written)")
        _mcu_body = ttk.Frame(self.mcu_box)
        _mcu_body.pack(fill="x", padx=8, pady=6)
        self._mcu_img = _load_step_image("Reset.png")   # keep a ref so Tk doesn't GC it
        ttk.Label(_mcu_body, image=self._mcu_img if self._mcu_img else "").pack(
            side="left", anchor="n", padx=(0, 12))
        self.mcu_reset_lbl = ttk.Label(_mcu_body, justify="left", wraplength=420,
                                       foreground="#7a1020", font=("", 11))
        self.mcu_reset_lbl.pack(side="left", anchor="n", fill="x", expand=True)
        again = self._again_frame = ttk.Frame(self.done)
        again.pack(fill="x")
        ttk.Button(again, text="Update another radio (same selection)", command=self._again).pack(side="left")
        ttk.Button(again, text="Back to setup", command=self._back_to_setup).pack(side="left", padx=8)

    # ---- setup -> start -----------------------------------------------------
    def _refresh_start_state(self):
        any_ready = any(r.ready for r in self.rows)
        ok = self.confirm.get() and any_ready
        self.start_btn.state(["!disabled"] if ok else ["disabled"])

    def _lock_model(self, locked: bool):
        """Lock/unlock the model radio buttons — locked while a batch is in
        progress so the model can't change under an in-flight plan."""
        for rb in getattr(self, "_model_btns", []):
            rb.state(["disabled"] if locked else ["!disabled"])

    def _plan_has_fw(self) -> bool:
        return any(p.kind == spec.KIND_FW for p in self.plan)

    def _start(self):
        self.plan = [r.result for r in self.rows if r.ready]
        if not self.plan:
            messagebox.showwarning("Nothing selected", "Choose and tick at least one target first.")
            return
        self._begin_radio()

    def _begin_radio(self):
        # Radio Firmware can reset the radio's settings, so require a codeplug
        # backup first — every radio, including "flash another". The board/asset
        # updates (icon/BT/NR/SCT) don't touch the codeplug, so this only fires
        # when Radio Firmware is in the batch.
        if self._plan_has_fw():
            if not messagebox.askyesno(
                    "Back up your codeplug first",
                    "This batch writes the Radio Firmware, which can reset the radio's settings.\n\n"
                    "Read the codeplug from THIS radio and save it to your PC before continuing.\n\n"
                    "Is this radio's codeplug backed up?"):
                return
        self._lock_model(True)   # no model change once a batch is under way
        self.step = 0
        self.results = [{"kind": p.kind, "label": spec.label(p.kind), "state": "pending"} for p in self.plan]
        self._clear_log()
        self.setup.pack_forget()
        self.done.pack_forget()
        self.wizard.pack(fill="both", expand=True)
        self._show_step()

    # ---- the walk -----------------------------------------------------------
    def _show_step(self):
        if self.step >= len(self.plan):
            self._finish_radio()
            return
        comp = self.plan[self.step]
        kind = comp.kind
        self.wtitle.configure(text=spec.label(kind))
        self.wcount.configure(text="step " + str(self.step + 1) + " of " + str(len(self.plan)))
        instr = spec.entry_instructions(self.model, kind)
        if kind in spec.UNCONFIRMED:
            instr = "⚠ Entry combo not yet confirmed — verify before continuing.\n\n" + instr
        self.winstr.configure(text=instr)
        self._step_img = _load_step_image(spec.image(kind))
        self.wimage.configure(image=self._step_img if self._step_img else "")

        # reset controls to the "instructions" state
        self.port_row.pack_forget()
        self.progress.pack_forget()
        self.wstatus.configure(text="", foreground="#444")   # clear a prior stage's green/red
        self.wstatus.pack_forget()
        self.abort_btn.pack_forget()
        self.next_btn.pack_forget()
        for w in (self.ready_btn, self.skip_btn):
            w.pack_forget()
        self.ready_btn.configure(text="The radio is in this mode →", state="normal")
        self.ready_btn.pack(in_=self.wctl, side="left")
        self.skip_btn.configure(text="Skip this target", state="normal")
        self.skip_btn.pack(in_=self.wctl, side="left", padx=8)
        self.connect_btn.configure(text="Connect && Write")

    def _on_ready(self):
        # reveal the COM-port picker for this stage (Skip stays available)
        self.ready_btn.pack_forget()
        self._refresh_ports()
        # show the SCT baud selector only for the SCT stage
        if self.plan[self.step].kind == spec.KIND_SCT:
            self.sct_baud_row.grid(row=1, column=1, sticky="w", pady=(4, 0))
        else:
            self.sct_baud_row.grid_forget()
        self.port_row.pack(fill="x", pady=(4, 0))

    def _refresh_ports(self):
        ports = list(list_ports.comports())
        labels = []
        self._port_map = {}
        for p in ports:
            desc = (p.description or "").strip()
            label = p.device + ("  —  " + desc if desc and desc != "n/a" else "")
            labels.append(label)
            self._port_map[label] = p.device
        self.port_box["values"] = labels
        # preselect the last-used port if still present, else the first
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
        self._last_port = port
        comp = self.plan[self.step]
        self.results[self.step]["state"] = "writing"

        # Read the SCT baud HERE, on the Tk thread — a tk.StringVar.get() from the
        # worker thread is a Tcl call off the main loop and can raise, wedging the
        # stage at "Connecting…".
        sct_baud = None
        if comp.kind == spec.KIND_SCT:
            try:
                sct_baud = int(self._sct_baud.get())
            except (ValueError, tk.TclError):
                sct_baud = None

        # switch to the writing state
        self.port_row.pack_forget()
        self.skip_btn.pack_forget()
        self.progress["value"] = 0
        self.progress.pack(fill="x", pady=(6, 0))
        self.wstatus.configure(text="Connecting…", foreground="#444")
        self.wstatus.pack(anchor="w", pady=(4, 0))
        self.abort_btn.pack(anchor="w", pady=(4, 0))

        self._writing = True
        self._abort = threading.Event()
        threading.Thread(target=self._stage_worker, args=(comp, port, self._abort, sct_baud),
                         daemon=True).start()

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
        except Exception as e:  # noqa: BLE001
            self._post("stage_error", (str(e), False))

    def _on_stage_done(self):
        self._writing = False
        self.results[self.step]["state"] = "done"
        self.progress["value"] = 100
        self.wstatus.configure(
            text=spec.label(self.plan[self.step].kind) + " written. Nothing is read back — the result is "
                 "only visible when the radio boots.", foreground="#127a2e")
        self.abort_btn.pack_forget()
        nxt = "Next: " + spec.label(self.plan[self.step + 1].kind) if self.step + 1 < len(self.plan) \
            else "Finish this radio"
        self.next_btn.configure(text=nxt)
        self.next_btn.pack(anchor="w", pady=(6, 0))

    def _on_stage_error(self, msg, aborted):
        self._writing = False
        self.results[self.step]["state"] = "aborted" if aborted else "failed"
        self.wstatus.configure(
            text=("Aborted. " if aborted else "Failed: " + msg + "  ")
                 + "This radio may be partly written — do not power-cycle it before deciding.",
            foreground="#b00020")
        self.abort_btn.pack_forget()
        # offer Retry + Skip again
        self.ready_btn.configure(text="Retry — the radio is in this mode →", state="normal")
        self.ready_btn.pack(in_=self.wctl, side="left")
        self.skip_btn.configure(text="Skip and continue", state="normal")
        self.skip_btn.pack(in_=self.wctl, side="left", padx=8)

    def _on_skip(self):
        if self._writing:
            return
        # A target that already failed/aborted keeps that state in the summary —
        # "Skip and continue" only moves past it. Downgrading it to "skipped"
        # would hide that a write actually started (and may have partly run).
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

    # ---- done ---------------------------------------------------------------
    def _finish_radio(self):
        self.wizard.pack_forget()
        for w in self.done_summary.winfo_children():
            w.destroy()
        for r in self.results:
            color = {"done": "#127a2e", "failed": "#b00020", "aborted": "#b00020",
                     "skipped": "#666"}.get(r["state"], "#444")
            ttk.Label(self.done_summary, text=r["label"] + " — " + r["state"], foreground=color).pack(anchor="w")
        # Whenever Radio Firmware was written, the MCU must be reset — show how.
        fw_done = any(r["kind"] == spec.KIND_FW and r["state"] == "done" for r in self.results)
        if fw_done:
            self.mcu_reset_lbl.configure(text=spec.mcu_reset(self.model))
            self.mcu_box.pack(fill="x", pady=(4, 8), before=self._again_frame)
        else:
            self.mcu_box.pack_forget()
        self.done.pack(fill="both", expand=True)

    def _again(self):
        # _begin_radio does the codeplug prompt + view switch; don't hide the done
        # view first, or a declined prompt would leave a blank tab.
        self._begin_radio()

    def _back_to_setup(self):
        self._lock_model(False)   # back on setup — model can change again
        self.confirm.set(False)
        self.start_btn.state(["disabled"])
        self.done.pack_forget()
        self.wizard.pack_forget()
        self.setup.pack(fill="both", expand=True)

    # ---- log helpers --------------------------------------------------------
    def _log(self, msg, cls="info"):
        # Prefix every line with [time] [model] [step] so a saved/scrolled log is
        # self-describing: [10:23:45] [D890UV] [Icons & Fonts] TX ...
        step = (spec.label(self.plan[self.step].kind)
                if self.plan and 0 <= self.step < len(self.plan) else "—")
        line = "[" + time.strftime("%H:%M:%S") + "] [" + spec.model_label(self.model) + "] [" + step + "] " + msg
        self.log.configure(state="normal")
        tag = cls if cls in _LOG_TAGS else ""
        self.log.insert("end", line + "\n", (tag,) if tag else ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ---- window-close hook (called by main) ---------------------------------
    def is_writing(self) -> bool:
        return self._writing
