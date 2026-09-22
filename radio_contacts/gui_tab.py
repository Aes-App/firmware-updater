from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Optional

from serial.tools import list_ports

from radio_fw.engines import AbortedError
from radio_fw.layout import follow_width

from . import catalog, contact_build, engine, launch
from . import segments as seg

_LOG_SOURCE = "Digital Contact"

_LOG_TAGS = {"tx": "#0b5fff", "rx": "#7a3fb0", "ok": "#127a2e", "er": "#b00020", "error": "#b00020",
             "info": "#444"}
_NO_SESSION_TEXT = ("Start at cps.aes.app/tools/contact-lists — the “Open in AesApp Radio Updater” "
                    "button at the bottom of My Contact Lists opens this app with a one-time link. "
                    "If your browser did not hand the link over, paste it below.")

_SOURCE_SERVER = "server"
_SOURCE_LOCAL = "local"

(_ROW_INTRO, _ROW_SOURCE, _ROW_SECTION, _ROW_RADIO, _ROW_LIST, _ROW_WRITE,
 _ROW_PROGRESS, _ROW_STATUS, _ROW_LOG_LABEL, _ROW_LOG) = range(10)
_LOG_WEIGHT = 3
_SECTION_WEIGHT = 1
_TREE_KEEP_PX = 0

_EST_BLOCKS_PER_CONTACT = {"anytone_878": 4.0, "anytone_878uv": 4.0, "anytone_890": 7.0}

_TICK_ON, _TICK_OFF, _TICK_SOME = "☑", "☐", "▣"

_WHEEL_DIVISOR = {"aqua": 40.0, "win32": 120.0, "x11": 120.0}

_WHEEL_MAX_ROWS = 4

_TOUCHPAD_PER_ROW = 5.0


def _fmt_eta(seconds: float) -> str:
    if seconds < 90:
        return f"about {int(round(seconds))} s"
    return f"about {int(round(seconds / 60.0))} min"


def _fmt_size(size: int) -> str:
    if size >= 1048576:
        return "%.1f MB" % (size / 1048576.0)
    if size >= 1024:
        return "%.0f kB" % (size / 1024.0)
    return "%d bytes" % size


def _file_label(path: str) -> str:
    try:
        return "%s — %s" % (os.path.basename(path), _fmt_size(os.path.getsize(path)))
    except OSError:
        return os.path.basename(path)


def _facet_name(facet) -> str:
    if not facet.code:
        return "(no country / region in the file)"
    return facet.label or facet.code


def _facet_text(facet) -> str:
    return (_facet_name(facet) + " " + facet.code).lower()


class ContactRefreshTab:

    def __init__(self, parent: tk.Widget, root: tk.Tk):
        self.parent = parent
        self.root = root
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self.token: Optional[str] = None
        self.base_url: str = catalog.DEFAULT_BASE_URL
        self.session: Optional[dict] = None
        self.catalog: Optional[dict] = None
        self.ident: Optional[engine.Ident] = None
        self.radio: Optional[dict] = None
        self._bundle_map: dict[str, dict] = {}
        self._nx_map: dict[str, dict] = {}
        self._port_map: dict[str, str] = {}
        self._last_port: Optional[str] = None
        self._busy = False
        self._writing = False

        self._source = _SOURCE_SERVER
        self.spec: Optional[contact_build.RadioSpec] = None
        self.store: Optional[contact_build.ContactStore] = None
        self.user_path: Optional[str] = None
        self.nx_path: Optional[str] = None
        self.nx_rows: Optional[list] = None
        self.nx_facets: list = []
        self._groups: list = []
        self._sel_codes: set = set()
        self._sel_count = 0
        self._reading = False
        self._nx_reading = False
        self._item_code: dict = {}
        self._item_group: dict = {}
        self._item_name: dict = {}
        self._pad_accum = 0.0
        self._held = None
        self._abort: Optional[threading.Event] = None
        self._build()
        self.root.after(80, self._drain)

    def _post(self, kind, payload=None):
        self._q.put((kind, payload))

    def _drain(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                try:
                    self._handle(kind, payload)
                except Exception as e:
                    try:
                        self._log(f"internal error handling {kind!r}: {e}", "er")
                    except Exception:
                        pass
        except queue.Empty:
            pass
        finally:
            self.root.after(80, self._drain)

    def _handle(self, kind, payload):
        if kind == "log":
            msg, cls = payload
            self._log(msg, cls)
        elif kind == "status":
            text, color = payload
            self.session_status.configure(text=text, foreground=color)
        elif kind == "session_ok":
            sess, cat = payload
            self._busy = False
            self.session = sess
            self.catalog = cat
            acct = str(sess.get("account") or "your account")
            tier = str(sess.get("tier") or "").capitalize()
            exp = str(sess.get("expiresAt") or "")[:16].replace("T", " ")
            self.session_status.configure(
                text=f"Signed in as {acct}" + (f" ({tier} plan)" if tier else "")
                     + (f" — session valid until {exp}" if exp else ""), foreground="#127a2e")
            self._log("server session opened; catalog lists " + str(len(cat.get("bundles") or []))
                      + " contact bundle(s)", "ok")
            self._refresh_radio_state()
        elif kind == "session_err":
            self._busy = False
            self.session = None
            self.session_status.configure(text=str(payload), foreground="#b00020")
            self._log(str(payload), "er")
            self._refresh_write_state()
        elif kind == "catalog_ok":
            self._busy = False
            self.catalog = payload
            self._log("catalog reloaded", "ok")
            self._refresh_radio_state()
        elif kind == "catalog_err":
            self._busy = False
            self.list_info.configure(text=str(payload), foreground="#b00020")
            self._refresh_write_state()
        elif kind == "ident_ok":
            self._busy = False
            self.ident, self._held = payload
            self.connect_btn.state(["!disabled"])
            self.ident_lbl.configure(
                text=f"Connected: {self.ident.model or '?'} {self.ident.version}".strip(),
                foreground="#127a2e")
            self._refresh_radio_state()
        elif kind == "disconnected":
            self._busy = False
            self.ident = None
            self.radio = None
            self._held = None
            self.connect_btn.state(["!disabled"])
            self.ident_lbl.configure(text="Not connected.", foreground="#666")
            self._refresh_radio_state()
            self._refresh_write_state()
        elif kind == "ident_err":
            self._busy = False
            self.ident = None
            self._held = None
            self.radio = None
            self.connect_btn.state(["!disabled"])
            self.ident_lbl.configure(text=str(payload), foreground="#b00020")
            self._log(str(payload), "er")
            self._refresh_radio_state()
        elif kind == "local_read":
            self.read_lbl.configure(text=f"Reading the file… {int(payload):,} contacts so far",
                                    foreground="#444")
        elif kind == "local_store":
            self._reading = False
            self.store = payload
            self._regroup()
            note = ""
            if payload.skipped or payload.duplicates:
                note = (f" ({payload.skipped:,} row(s) skipped, "
                        f"{payload.duplicates:,} repeated ID(s))")
            self.read_lbl.configure(text=f"{len(payload):,} contacts read" + note,
                                    foreground="#127a2e")
            src = os.path.basename(self.user_path or "")
            self._log(f"{len(payload):,} contacts read" + (" from " + src if src else "") + note, "ok")
            self._select_codes(set())
            self._render_picker()
        elif kind == "local_err":
            self._reading = False
            self._forget_store()
            self.read_lbl.configure(text=str(payload), foreground="#b00020")
            self._log(str(payload), "er")
        elif kind == "local_nx":
            self._nx_reading = False
            self.nx_rows = payload
            self.nx_facets = contact_build.nx_facets(payload)
            self.nx_local_check.state(["!disabled"])
            self.nx_lbl.configure(
                text=_file_label(self.nx_path or "") + f" — {len(payload):,} NXDN contacts",
                foreground="#127a2e")
            self._log(f"{len(payload):,} NXDN contacts read from "
                      f"{os.path.basename(self.nx_path or '')}", "ok")
            self._regroup()
            self._render_picker()
            self._refresh_local_summary()
        elif kind == "local_nx_err":
            self._nx_reading = False
            self.nx_rows = None
            self.nx_lbl.configure(text=str(payload), foreground="#b00020")
            self._log(str(payload), "er")
            self._refresh_local_summary()
        elif kind == "build":
            done = payload
            self.wstatus.configure(
                text="Building the contact database on this computer… "
                     + (f"{int(done):,} contacts encoded" if done else "this takes a few seconds"),
                foreground="#8a6d00")
        elif kind == "dl_status":
            self.wstatus.configure(text=str(payload), foreground="#8a6d00")
        elif kind == "dl_progress":
            got, total = payload
            pct = (got * 100.0 / total) if total else 0.0
            self.wstatus.configure(text="Downloading the list… " + format(pct, ".0f") + "%", foreground="#444")
        elif kind == "progress":
            done, total, phase = payload
            pct = (done * 100.0 / total) if total else 0.0
            self.progress["value"] = pct
            if phase == "write" and self._t0 and done:
                rate = done / max(1e-6, time.monotonic() - self._t0)
                left = (total - done) / max(1e-6, rate)
                self.wstatus.configure(
                    text=f"Writing — do not unplug the radio.  {pct:.0f}%  ({done:,}/{total:,} blocks, "
                         f"{_fmt_eta(left)} left)", foreground="#444")
            else:
                self.wstatus.configure(text=f"Writing ({phase}) — do not unplug the radio.", foreground="#444")
        elif kind == "write_done":
            self._on_write_done(payload)
        elif kind == "write_err":
            msg, aborted = payload
            self._on_write_error(msg, aborted)

    def _build(self):
        outer = self._outer = ttk.Frame(self.parent, padding=10)
        outer.pack(fill="both", expand=True)

        intro = ttk.Label(outer, justify="left", wraplength=680, foreground="#444",
                          text=("Replaces the digital-contact database on your radio (the caller-ID names "
                                "shown for DMR IDs). The list can come from your aes.app account, built "
                                "and verified daily on the server, or you can build one here from a "
                                "register download of your own. The codeplug — channels, zones, contacts "
                                "you created, settings — is not touched either way."))

        src = ttk.LabelFrame(outer, text="1. Where the list comes from")
        self.source_var = tk.StringVar(value=_SOURCE_SERVER)
        ttk.Radiobutton(src, variable=self.source_var, value=_SOURCE_SERVER,
                        command=self._on_source_change,
                        text="A list from my cps.aes.app account that I uploaded earlier"
                        ).pack(anchor="w", padx=8, pady=(6, 0))
        ttk.Radiobutton(src, variable=self.source_var, value=_SOURCE_LOCAL,
                        command=self._on_source_change,
                        text="Build a list locally using user.csv"
                        ).pack(anchor="w", padx=8, pady=(0, 6))

        self.server_box = ttk.Frame(outer)
        self.local_box = ttk.Frame(outer)
        self._build_local(self.local_box)

        sess = ttk.LabelFrame(self.server_box, text="2. Link from My Contact Lists on cps.aes.app")
        sess.pack(fill="x", pady=(0, 6))
        self.session_status = ttk.Label(sess, text=_NO_SESSION_TEXT, justify="left", wraplength=660,
                                        foreground="#8a6d00")
        self.session_status.pack(fill="x", padx=8, pady=(6, 4))
        follow_width(self.session_status, sess, reserve=20)
        row = ttk.Frame(sess)
        row.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(row, text="Paste link:").pack(side="left")
        self.link_var = tk.StringVar()
        self.link_entry = ttk.Entry(row, textvariable=self.link_var, width=52)
        self.link_entry.pack(side="left", padx=6, fill="x", expand=True)
        self.link_entry.bind("<Return>", lambda _e: self._on_use_link())
        self.link_btn = ttk.Button(row, text="Use link", command=self._on_use_link)
        self.link_btn.pack(side="left")

        rad = self.rad = ttk.LabelFrame(outer, text="3. Radio (switched on, USB cable in)")
        prow = ttk.Frame(rad)
        prow.pack(fill="x", padx=8, pady=6)
        ttk.Label(prow, text="COM port:").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(prow, textvariable=self.port_var, state="readonly", width=40)
        self.port_box.pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(prow, text="Refresh", command=self._refresh_ports).pack(side="left")
        self.connect_btn = ttk.Button(prow, text="Connect", command=self._on_connect)
        self.connect_btn.pack(side="left", padx=(6, 0))
        self.disconnect_btn = ttk.Button(prow, text="Disconnect", command=self._on_disconnect,
                                         state="disabled")
        self.disconnect_btn.pack(side="left", padx=(6, 0))
        self.ident_lbl = ttk.Label(rad, text="Not connected.", foreground="#666", wraplength=660,
                                   justify="left")
        self.ident_lbl.pack(fill="x", padx=8, pady=(0, 6))
        follow_width(self.ident_lbl, rad, reserve=20)

        lst = self.lst = ttk.LabelFrame(outer, text="4. Contact list")
        lrow = ttk.Frame(lst)
        lrow.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(lrow, text="DMR list:").pack(side="left")
        self.list_var = tk.StringVar()
        self.list_box = ttk.Combobox(lrow, textvariable=self.list_var, state="readonly", width=48)
        self.list_box.pack(side="left", padx=6, fill="x", expand=True)
        self.list_box.bind("<<ComboboxSelected>>", lambda _e: self._update_info())
        self.reload_btn = ttk.Label(lrow, text="⟳", font=("", 16), cursor="hand2")
        self.reload_btn.pack(side="left", padx=(2, 2))
        self.reload_btn.bind("<Button-1>", lambda _e: self._on_reload_catalog())
        self.nx_var = tk.BooleanVar(value=False)
        self.nx_row = ttk.Frame(lst)
        self.nx_check = ttk.Checkbutton(self.nx_row, variable=self.nx_var, command=self._update_info,
                                        text="Also write the NXDN contact list (D890 family)")
        self.nx_check.pack(side="left")
        self.nx_list_var = tk.StringVar()
        self.nx_box = ttk.Combobox(self.nx_row, textvariable=self.nx_list_var, state="readonly", width=40)
        self.nx_box.bind("<<ComboboxSelected>>", lambda _e: self._update_info())
        self.list_info = ttk.Label(lst, text="Connect the radio and get a link first.", foreground="#666",
                                   wraplength=660, justify="left")
        self.list_info.pack(fill="x", padx=8, pady=(2, 6))
        follow_width(self.list_info, lst, reserve=20)

        wr = self.wr = ttk.Frame(outer)
        self.write_btn = ttk.Button(wr, text="Write to radio", command=self._on_write)
        self.write_btn.pack(side="left")
        self.write_btn.state(["disabled"])
        self.abort_btn = ttk.Button(wr, text="Stop", command=self._on_abort)
        self.abort_btn.pack(side="left", padx=8)
        self.abort_btn.state(["disabled"])
        self.progress = ttk.Progressbar(outer, mode="determinate", maximum=100.0)
        self.wstatus = ttk.Label(outer, text="", foreground="#444", wraplength=680, justify="left")

        log_lbl = ttk.Label(outer, text="Protocol log")
        self.log = scrolledtext.ScrolledText(outer, height=3, wrap="word", state="disabled")

        outer.columnconfigure(0, weight=1)
        follow_width(intro, outer, reserve=8)
        follow_width(self.wstatus, outer, reserve=8)
        intro.grid(row=_ROW_INTRO, column=0, sticky="ew", pady=(0, 8))
        src.grid(row=_ROW_SOURCE, column=0, sticky="ew", pady=(0, 6))
        self.server_box.grid(row=_ROW_SECTION, column=0, sticky="ew")
        rad.grid(row=_ROW_RADIO, column=0, sticky="ew", pady=(0, 6))
        lst.grid(row=_ROW_LIST, column=0, sticky="ew", pady=(0, 6))
        wr.grid(row=_ROW_WRITE, column=0, sticky="ew", pady=(2, 0))
        self.progress.grid(row=_ROW_PROGRESS, column=0, sticky="ew", pady=(6, 0))
        self.wstatus.grid(row=_ROW_STATUS, column=0, sticky="ew", pady=(4, 0))
        log_lbl.grid(row=_ROW_LOG_LABEL, column=0, sticky="w", pady=(8, 0))
        self.log.frame.grid(row=_ROW_LOG, column=0, sticky="nsew")
        outer.rowconfigure(_ROW_LOG, weight=_LOG_WEIGHT)
        self._size_section_row()
        for tag, color in _LOG_TAGS.items():
            self.log.tag_configure(tag, foreground=color)

        self._t0 = None
        self._refresh_ports()

    def _build_local(self, parent):
        loc = ttk.LabelFrame(parent, text="2. Your own register download")
        loc.pack(fill="both", expand=True, pady=(0, 6))
        loc_intro = ttk.Label(
            loc, justify="left", wraplength=660, foreground="#444",
            text=("Your own user.csv from the register (RadioID.net ▸ Database), built into the "
                  "radio's contact database here — nothing uploaded, nothing fetched. nxdn.csv "
                  "is optional; only the D890 family has an NXDN list to put it in."))
        loc_intro.pack(fill="x", padx=8, pady=(6, 4))
        follow_width(loc_intro, loc, reserve=20)

        frow = ttk.Frame(loc)
        frow.pack(fill="x", padx=8)
        ttk.Label(frow, text="Contact list (user.csv):", width=22).pack(side="left")
        ttk.Button(frow, text="Browse…", command=self._on_browse_user).pack(side="left")
        self.user_lbl = ttk.Label(frow, text="No file chosen.", foreground="#666")
        self.user_lbl.pack(side="left", padx=8)

        nrow = ttk.Frame(loc)
        nrow.pack(fill="x", padx=8, pady=(4, 0))
        ttk.Label(nrow, text="NXDN list (nxdn.csv):", width=22).pack(side="left")
        ttk.Button(nrow, text="Browse…", command=self._on_browse_nx).pack(side="left")
        self.nx_local_var = tk.BooleanVar(value=True)
        self.nx_local_check = ttk.Checkbutton(nrow, text="Also Filter", variable=self.nx_local_var,
                                              command=self._on_nx_include)
        self.nx_local_check.pack(side="left", padx=(8, 0))
        self.nx_local_check.state(["disabled"])
        self.nx_lbl = ttk.Label(nrow, text="Optional — the D890 family only.", foreground="#666")
        self.nx_lbl.pack(side="left", padx=8)

        ptop = ttk.Frame(loc)
        ptop.pack(fill="x", padx=8, pady=(10, 2))
        ttk.Label(ptop, text="Countries / regions to write:").pack(side="left")
        self.filter_var = tk.StringVar()
        ttk.Entry(ptop, textvariable=self.filter_var, width=16).pack(side="left", padx=(6, 0))
        ttk.Label(ptop, text="(type to filter)", foreground="#666").pack(side="left", padx=(4, 0))
        ttk.Button(ptop, text="Clear all", command=lambda: self._select_codes(set())
                   ).pack(side="right")
        ttk.Button(ptop, text="Select all", command=self._on_select_all).pack(side="right", padx=(0, 6))
        self.filter_var.trace_add("write", lambda *_a: self._render_picker())

        holder = ttk.Frame(loc)
        holder.pack(fill="both", expand=True, padx=8)
        self.tree = ttk.Treeview(holder, columns=("n",), show="tree headings", height=6,
                                 selectmode="browse")
        self.tree.heading("#0", text="Continent / country / region", anchor="w")
        self.tree.heading("n", text="Contacts", anchor="e")
        self.tree.column("#0", width=380, minwidth=200, stretch=True)
        self.tree.column("n", width=110, minwidth=70, anchor="e", stretch=False)
        vsb = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<space>", self._on_tree_key)
        self.tree.bind("<Return>", self._on_tree_key)
        self.tree.bind("<MouseWheel>", self._on_wheel)
        self.tree.bind("<Button-4>", self._on_wheel)
        self.tree.bind("<Button-5>", self._on_wheel)
        try:
            self.tree.bind("<TouchpadScroll>", self._on_touchpad)
        except tk.TclError:
            pass

        srow = ttk.Frame(loc)
        srow.pack(side="bottom", fill="x", padx=8, pady=(6, 2), before=frow)
        self.sel_lbl = ttk.Label(srow, text="No file read yet.", font=("", 13, "bold"))
        self.sel_lbl.pack(side="left")
        self.read_lbl = ttk.Label(srow, text="", foreground="#666", wraplength=300,
                                  justify="right", anchor="e")
        self.read_lbl.pack(side="right")
        self.fit_lbl = ttk.Label(loc, text="", wraplength=660, justify="left", foreground="#8a6d00")
        self.fit_lbl.pack(side="bottom", fill="x", padx=8, pady=(0, 8), before=srow)
        follow_width(self.fit_lbl, loc, reserve=20)

    def _is_local(self) -> bool:
        return self.source_var.get() == _SOURCE_LOCAL

    def _on_source_change(self):
        if self._writing:
            self.source_var.set(self._source)
            return
        self._source = self.source_var.get()
        if self._is_local():
            self.server_box.grid_remove()
            self.lst.grid_remove()
            self.local_box.grid(row=_ROW_SECTION, column=0, sticky="nsew")
        else:
            self.local_box.grid_remove()
            self.server_box.grid(row=_ROW_SECTION, column=0, sticky="ew")
            self.lst.grid(row=_ROW_LIST, column=0, sticky="ew", pady=(0, 6))
        self._size_section_row()
        self._refresh_radio_state()

    def _size_section_row(self):
        if not self._is_local():
            self._outer.rowconfigure(_ROW_SECTION, weight=0, minsize=0)
            return
        try:
            self._outer.update_idletasks()
            floor = self.local_box.winfo_reqheight() - self.tree.winfo_reqheight() + _TREE_KEEP_PX
        except tk.TclError:
            return
        self._outer.rowconfigure(_ROW_SECTION, weight=_SECTION_WEIGHT, minsize=max(0, floor))

    def handle_launch_url(self, url: str):
        try:
            req = launch.parse_launch_url(url)
        except launch.LaunchError as e:
            self.session_status.configure(text="That link cannot be used: " + str(e), foreground="#b00020")
            self._log("link refused: " + str(e), "er")
            return
        if self._writing:
            self.session_status.configure(
                text="A write is in progress — the new link will be ignored until it finishes.",
                foreground="#8a6d00")
            return
        if self._is_local():
            self.source_var.set(_SOURCE_SERVER)
            self._on_source_change()
        self.token = req.token
        self.base_url = req.base_url(catalog.DEFAULT_BASE_URL)
        try:
            import logging
            logging.getLogger("bt_ota").info("contact tab: link accepted, server %s", self.base_url)
        except Exception:
            pass
        self.session = None
        self.catalog = None
        self.link_var.set("")
        self._busy = True
        self._refresh_write_state()
        self.session_status.configure(text="Checking the link with the AesApp server…", foreground="#444")
        self._log("link received for " + self.base_url + "; claiming the session", "info")
        threading.Thread(target=self._session_worker, args=(self.base_url, self.token), daemon=True).start()

    def _session_worker(self, base, token):
        try:
            sess = catalog.claim_session(base, token, on_status=lambda m: self._post("status", (m, "#8a6d00")))
            cat = catalog.fetch_catalog(base, token, on_status=lambda m: self._post("status", (m, "#8a6d00")))
            self._post("session_ok", (sess, cat))
        except catalog.ContactsError as e:
            self._post("session_err", str(e))
        except Exception as e:
            self._post("session_err", "Unexpected error: " + str(e))

    def _on_use_link(self):
        url = self.link_var.get().strip()
        if not url:
            return
        self.handle_launch_url(url)

    def _on_reload_catalog(self):
        if self._busy or self._writing or not self.token:
            return
        self._busy = True
        self.list_info.configure(text="Reloading the list from the server…", foreground="#666")
        base, token = self.base_url, self.token

        def worker():
            try:
                self._post("catalog_ok", catalog.fetch_catalog(base, token))
            except catalog.ContactsError as e:
                self._post("catalog_err", str(e))
            except Exception as e:
                self._post("catalog_err", "Unexpected error: " + str(e))
        threading.Thread(target=worker, daemon=True).start()

    def _refresh_ports(self):
        labels = []
        self._port_map = {}
        for p in list_ports.comports():
            desc = (p.description or "").strip()
            label = p.device + ("  —  " + desc if desc and desc != "n/a" else "")
            labels.append(label)
            self._port_map[label] = p.device
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

    def _selected_port(self) -> Optional[str]:
        return self._port_map.get(self.port_var.get())

    def _on_connect(self):
        if self._busy or self._writing:
            return
        port = self._selected_port()
        if not port:
            messagebox.showwarning("No COM port", "Pick the radio's COM port. Click Refresh if it isn't listed.")
            return
        self._last_port = port
        self._busy = True
        self.connect_btn.state(["disabled"])
        self.ident_lbl.configure(text="Connecting…", foreground="#444")
        self._refresh_write_state()
        held, self._held = self._held, None
        threading.Thread(target=self._ident_worker, args=(port, held), daemon=True).start()

    def _ident_worker(self, port, previous=None):
        log = lambda m, c="info": self._post("log", (m, c))
        engine.close_session(previous, log)
        try:
            ident, link = engine.open_session(port, on_log=log)
            self._post("ident_ok", (ident, link))
        except Exception as e:
            self._post("ident_err", str(e))

    def _on_disconnect(self):
        if self._writing or self._held is None:
            return
        held, self._held = self._held, None
        self._busy = True
        self.disconnect_btn.state(["disabled"])
        self.connect_btn.state(["disabled"])
        self.ident_lbl.configure(text="Releasing the radio…", foreground="#444")
        threading.Thread(target=self._disconnect_worker, args=(held,), daemon=True).start()

    def _disconnect_worker(self, held):
        log = lambda m, c="info": self._post("log", (m, c))
        engine.close_session(held, log)
        self._post("disconnected", None)

    def _refresh_radio_state(self):
        if self._is_local():
            self._refresh_local_radio()
            return
        self.radio = None
        if self.ident is not None and self.catalog is not None:
            r = catalog.radio_entry(self.catalog, self.ident.model)
            if r is None:
                self.ident_lbl.configure(
                    text=f"Connected: {self.ident.model} {self.ident.version} — this radio is not in the "
                         "server's list of supported models.", foreground="#b00020")
            elif not r.get("enabled", False):
                self.ident_lbl.configure(
                    text=f"Connected: {self.ident.model} {self.ident.version} ({r.get('label', '')}) — "
                         + str(r.get("note") or "the contact refresh is not yet enabled for this model")
                         + ".", foreground="#8a6d00")
            else:
                self.radio = r
                cap = int(r.get("capacity") or 0)
                self.ident_lbl.configure(
                    text=f"Connected: {self.ident.model} {self.ident.version} — {r.get('label', '')}, "
                         f"holds up to {cap:,} contacts.", foreground="#127a2e")
        self._populate_lists()

    def _refresh_local_radio(self):
        self.spec = None
        if self.ident is not None:
            self.spec = contact_build.radio_for_ident(self.ident.model)
            if self.spec is not None:
                self.ident_lbl.configure(
                    text=f"Connected: {self.ident.model} {self.ident.version} — {self.spec.label}, "
                         f"holds up to {self.spec.capacity:,} contacts.", foreground="#127a2e")
            else:
                known = contact_build.unsupported_label(self.ident.model)
                self.ident_lbl.configure(
                    text=f"Connected: {self.ident.model} {self.ident.version} — "
                         + (known + ": this app cannot build a contact list for it."
                            if known else
                            "this app does not know this model, so it will not guess at its "
                            "contact format."), foreground="#b00020")
        self._refresh_local_summary()

    def _on_browse_user(self):
        if self._writing:
            return
        if self._reading:
            self.read_lbl.configure(text="Still reading the file you chose — wait for it to finish.",
                                    foreground="#8a6d00")
            return
        path = filedialog.askopenfilename(
            title="Choose the register's user.csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        self.user_path = path
        self.user_lbl.configure(text=_file_label(path), foreground="#444")
        self._on_read_user()

    def _on_read_user(self):
        if self._reading or self._writing or not self.user_path:
            return
        self._reading = True
        self._forget_store()
        self.read_lbl.configure(text="Reading the file…", foreground="#444")
        path = self.user_path
        self._log("reading " + os.path.basename(path), "info")

        def worker():
            try:
                store = contact_build.read_user_csv(
                    path, on_progress=lambda rows: self._post("local_read", rows))
                self._post("local_store", store)
            except contact_build.ContactBuildError as e:
                self._post("local_err", str(e))
            except OSError as e:
                self._post("local_err", "Could not read that file: " + str(e))
            except Exception as e:
                self._post("local_err", "Unexpected error reading that file: " + str(e))
        threading.Thread(target=worker, daemon=True).start()

    def _on_browse_nx(self):
        if self._writing:
            return
        if self._nx_reading:
            self.nx_lbl.configure(text="Still reading the file you chose — wait for it to finish.",
                                  foreground="#8a6d00")
            return
        path = filedialog.askopenfilename(
            title="Choose the register's nxdn.csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        self.nx_path = path
        self.nx_rows = None
        self.nx_lbl.configure(text=_file_label(path) + " — reading…", foreground="#444")
        self._nx_reading = True
        self._refresh_local_summary()

        def worker():
            try:
                self._post("local_nx", contact_build.read_nxdn_csv(path))
            except contact_build.ContactBuildError as e:
                self._post("local_nx_err", str(e))
            except OSError as e:
                self._post("local_nx_err", "Could not read that file: " + str(e))
            except Exception as e:
                self._post("local_nx_err", "Unexpected error reading that file: " + str(e))
        threading.Thread(target=worker, daemon=True).start()

    def _include_nx(self) -> bool:
        return bool(self.nx_rows) and self.nx_local_var.get() and (
            self.spec is None or self.spec.nx_fmt is not None)

    def _nx_selected(self) -> list:
        if not self._include_nx():
            return []
        return contact_build.filter_nx_rows(self.nx_rows, self._sel_codes or None)

    def _regroup(self):
        if self.store is None:
            self._groups = []
            return
        facets = self.store.facets()
        if self._include_nx():
            facets = contact_build.merge_facets(facets, self.nx_facets)
        self._groups = contact_build.group_facets(facets)

    def _on_nx_include(self):
        if self._writing:
            self.nx_local_var.set(not self.nx_local_var.get())
            return
        self._regroup()
        self._render_picker()
        self._refresh_local_summary()

    def _forget_store(self):
        self.store = None
        self._groups = []
        self._select_codes(set())
        self._render_picker()

    def _render_picker(self):
        for iid in self.tree.get_children(""):
            self.tree.delete(iid)
        self._item_code, self._item_group, self._item_name = {}, {}, {}
        needle = self.filter_var.get().strip().lower()
        for key, name, total, members in self._groups:
            shown = [f for f in members if not needle or needle in _facet_text(f)]
            if not shown:
                continue
            gid = self.tree.insert("", "end", values=(format(total, ","),), open=bool(needle))
            self._item_group[gid] = key
            self._item_name[gid] = name
            for facet in shown:
                cid = self.tree.insert(gid, "end", values=(format(facet.count, ","),))
                self._item_code[cid] = facet.code
                self._item_name[cid] = _facet_name(facet)
        self._update_ticks()

    def _update_ticks(self):
        for gid in self.tree.get_children(""):
            kids = self.tree.get_children(gid)
            on = sum(1 for k in kids if self._item_code.get(k) in self._sel_codes)
            if kids and on == len(kids):
                glyph = _TICK_ON
            elif on:
                glyph = _TICK_SOME
            else:
                glyph = _TICK_OFF
            self.tree.item(gid, text=glyph + "  " + self._item_name[gid])
            for k in kids:
                self.tree.item(k, text=(_TICK_ON if self._item_code.get(k) in self._sel_codes
                                        else _TICK_OFF) + "  " + self._item_name[k])

    def _on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) in ("heading", "separator"):
            return None
        iid = self.tree.identify_row(event.y)
        if not iid:
            return None
        if (self.tree.get_children(iid)
                and "indicator" in str(self.tree.identify_element(event.x, event.y))):
            return None
        self._toggle_item(iid)
        return None

    def _on_wheel(self, event):
        delta = float(getattr(event, "delta", 0) or 0)
        num = getattr(event, "num", 0)
        if num == 4:
            delta = 1.0
        elif num == 5:
            delta = -1.0
        if delta == 0.0:
            return "break"

        try:
            system = self.tree.tk.call("tk", "windowingsystem")
        except tk.TclError:
            system = "aqua"
        divisor = _WHEEL_DIVISOR.get(system, 120.0)
        rows = 1 if delta > 0 else -1
        if abs(delta) >= divisor:
            rows = int(delta / divisor)
        rows = max(-_WHEEL_MAX_ROWS, min(_WHEEL_MAX_ROWS, rows))
        self.tree.yview_scroll(-rows, "units")

        return "break"

    def _on_touchpad(self, event):
        packed = int(getattr(event, "delta", 0) or 0)
        low = packed & 0xFFFF
        delta_y = low if low < 0x8000 else low - 0x10000
        if delta_y == 0:
            return "break"

        self._pad_accum += delta_y / _TOUCHPAD_PER_ROW
        rows = int(self._pad_accum)
        if rows:
            self._pad_accum -= rows
            self.tree.yview_scroll(max(-_WHEEL_MAX_ROWS, min(_WHEEL_MAX_ROWS, -rows)), "units")

        return "break"

    def _on_tree_key(self, _event):
        for iid in self.tree.selection():
            self._toggle_item(iid)
        return "break"

    def _toggle_item(self, iid):
        codes = set(self._sel_codes)
        if iid in self._item_group:
            members = {self._item_code[k] for k in self.tree.get_children(iid)
                       if k in self._item_code}
            if members and members <= codes:
                codes -= members
            else:
                codes |= members
        elif iid in self._item_code:
            code = self._item_code[iid]
            if code in codes:
                codes.discard(code)
            else:
                codes.add(code)
        else:
            return
        self._select_codes(codes)

    def _on_select_all(self):
        self._select_codes({f.code for _k, _n, _t, members in self._groups for f in members})

    def _select_codes(self, codes):
        self._sel_codes = set(codes)
        self._sel_count = (self.store.count_selected(self._sel_codes)
                           if self.store is not None else 0)
        self._update_ticks()
        self._refresh_local_summary()

    def _estimated_blocks(self) -> int:
        if self.spec is None:
            return 0
        blocks = int(self._sel_count * _EST_BLOCKS_PER_CONTACT.get(self.spec.fmt, 4.0))
        nx = self._nx_selected()
        if nx:
            blocks += int(len(nx) * contact_build.NX_REC / float(seg.BLOCK))
        return blocks

    def _refresh_local_summary(self):
        if self.store is None:
            self.sel_lbl.configure(text="No file read yet.")
        elif not self._sel_codes:
            self.sel_lbl.configure(text=f"Nothing selected, of {len(self.store):,} in the file.")
        else:
            nx = len(self._nx_selected())
            if nx:
                text = (f"{self._sel_count + nx:,} contacts selected "
                        f"({self._sel_count:,} DMR + {nx:,} NXDN)")
            else:
                text = f"{self._sel_count:,} contacts selected"
            self.sel_lbl.configure(text=text)
        self._refresh_write_state()

    def _local_gate(self) -> tuple:
        if self._reading or self._nx_reading:
            return False, "Reading the file…", "#444"
        if self.store is None:
            return False, "Choose your register's user.csv above.", "#8a6d00"
        if not self._sel_codes or not self._sel_count:
            return (False, "Nothing is selected, so there is nothing to write — tick at least one "
                           "country.", "#8a6d00")
        if self.ident is None:
            return (False, "Connect the radio (step 3): its model decides the contact format and how "
                           "many contacts fit.", "#8a6d00")
        if self.spec is None:
            known = contact_build.unsupported_label(self.ident.model)
            return (False, (known + " cannot be written by this app." if known else
                            'This app does not know the radio model "%s".' % self.ident.model),
                    "#b00020")
        if self._selected_port() is None:
            return False, "Pick the radio's COM port in step 3.", "#8a6d00"
        if self._sel_count > self.spec.capacity:
            return (False, "%s DMR contacts is more than the %s this %s holds — untick some of them."
                    % (format(self._sel_count, ","), format(self.spec.capacity, ","), self.spec.label),
                    "#b00020")
        msg = "Ready: %s DMR contacts, %s to write." % (
            format(self._sel_count, ","),
            _fmt_eta(catalog.estimate_seconds(self._estimated_blocks(), self.spec.fmt)))
        if self.nx_rows:
            if not self.spec.nx_fmt:
                msg += (" This radio has no NXDN list, so %s will not be written."
                        % os.path.basename(self.nx_path or "nxdn.csv"))
            elif not self.nx_local_var.get():
                msg += (" %s is loaded but left out — tick Also Filter to write it too."
                        % os.path.basename(self.nx_path or "nxdn.csv"))
            else:
                nx = self._nx_selected()
                if self.spec.nx_capacity and len(nx) > self.spec.nx_capacity:
                    return (False, "%s NXDN contacts is more than the %s this %s holds. The DMR "
                            "list has its own room and is unaffected."
                            % (format(len(nx), ","), format(self.spec.nx_capacity, ","),
                               self.spec.label), "#b00020")
                if len(nx) > contact_build.NX_MAX_RECORDS:
                    return (False, "%s NXDN contacts is more than the %s this app can lay out safely. "
                            "The radio holds %s, but no capture shows where its search index moves "
                            "past this, and guessing would overwrite it — untick some of them."
                            % (format(len(nx), ","), format(contact_build.NX_MAX_RECORDS, ","),
                               format(self.spec.nx_capacity or 80000, ",")), "#b00020")
                if not nx:
                    msg += (" No NXDN contact is in what you ticked, so only the DMR list "
                            "will be written.")
                else:
                    msg += (" The %s NXDN contacts in the same places go in the same session."
                            % format(len(nx), ","))
        return True, msg, "#127a2e"

    def _populate_lists(self):
        self._bundle_map = {}
        self._nx_map = {}
        self.nx_row.pack_forget()
        self.nx_box.pack_forget()
        self.nx_var.set(False)
        self.nx_list_var.set("")
        if self.radio is None or self.catalog is None:
            self.list_box["values"] = []
            self.list_var.set("")
            self.list_info.configure(
                text="Connect the radio and get a link first." if (self.ident is None or self.catalog is None)
                else "No list can be written to this radio.", foreground="#666")
            self._refresh_write_state()
            return
        pairs = catalog.label_bundles(catalog.bundles_for(self.catalog, self.radio, "dmr"))
        self._bundle_map = dict(pairs)
        labels = [lab for lab, _b in pairs]
        self.list_box["values"] = labels
        if labels:
            self.list_box.current(0)
        else:
            self.list_var.set("")
        nx = catalog.label_bundles(catalog.bundles_for(self.catalog, self.radio, "nxdn"))
        if nx:
            self._nx_map = dict(nx)
            self.nx_box["values"] = [lab for lab, _b in nx]
            self.nx_box.current(0)
            if len(nx) > 1:
                self.nx_check.configure(text="Also write the NXDN contact list:")
                self.nx_box.pack(side="left", padx=6, fill="x", expand=True)
            else:
                self.nx_check.configure(text="Also write the NXDN contact list ("
                                        + f"{int(nx[0][1].get('recordCount') or 0):,} contacts)")
            self.nx_row.pack(anchor="w", fill="x", padx=8, before=self.list_info)
        self._update_info()

    def _selected_bundle(self) -> Optional[dict]:
        return self._bundle_map.get(self.list_var.get())

    def _selected_nx_bundle(self) -> Optional[dict]:
        return self._nx_map.get(self.nx_list_var.get())

    def _update_info(self):
        b = self._selected_bundle()
        if b is None:
            self.list_info.configure(text="No list is small enough for this radio." if self.radio else "",
                                     foreground="#8a6d00")
            self._refresh_write_state()
            return
        blocks = int(b.get("blocks") or 0)
        mb = int(b.get("bytes") or 0) / 1e6
        parts = [f"{int(b.get('recordCount') or 0):,} contacts, {mb:.1f} MB download"]
        nx = self._selected_nx_bundle() if self.nx_var.get() else None
        if nx:
            blocks += int(nx.get("blocks") or 0)
            parts.append(f"+ NXDN {int(nx.get('bytes') or 0) / 1e6:.1f} MB")
        parts.append(f"write {_fmt_eta(catalog.estimate_seconds(blocks, b.get('format')))}")
        self.list_info.configure(text=" · ".join(parts), foreground="#444")
        self._refresh_write_state()

    def _refresh_write_state(self):
        if self._is_local():
            ok, msg, colour = self._local_gate()
            self.fit_lbl.configure(text=msg, foreground=colour)
            ok = ok and not self._busy and not self._writing
        else:
            ok = (not self._busy and not self._writing and self.session is not None
                  and self.radio is not None and self._selected_bundle() is not None
                  and self._selected_port() is not None)
        self.write_btn.state(["!disabled"] if ok else ["disabled"])
        self.link_btn.state(["disabled"] if (self._busy or self._writing) else ["!disabled"])
        self.nx_box.state(["disabled"] if self._writing else ["!disabled"])
        self.disconnect_btn.state(
            ["!disabled"] if (self._held is not None and not self._busy and not self._writing)
            else ["disabled"])

    def _on_write(self):
        if self._is_local():
            self._on_write_local()
        else:
            self._on_write_server()

    def _begin_write(self):
        self._writing = True
        self._abort = threading.Event()
        self._t0 = None
        self.write_btn.state(["disabled"])
        self.abort_btn.state(["!disabled"])
        self.connect_btn.state(["disabled"])
        self.progress["value"] = 0
        self.wstatus.configure(text="Preparing…", foreground="#444")
        self._refresh_write_state()
        held, self._held = self._held, None
        return held

    def _on_write_local(self):
        ok, _msg, _colour = self._local_gate()
        port = self._selected_port()
        if not ok or port is None or self.ident is None or self.spec is None or self.store is None:
            return
        codes = sorted(self._sel_codes)
        count = self._sel_count
        nx_rows = self._nx_selected() or None
        name = os.path.basename(self.user_path or "user.csv")
        countries = ("1 country / region" if len(codes) == 1
                     else "%d countries / regions" % len(codes))
        lines = ["This replaces the digital-contact database on the %s (%s) with a list this app "
                 "builds HERE, on this computer:" % (self.ident.model, self.spec.label), "",
                 "    %s — %s contacts from %s" % (name, format(count, ","), countries)]
        if nx_rows:
            lines.append("    %s — %s NXDN contacts from the same places"
                         % (os.path.basename(self.nx_path or "nxdn.csv"), format(len(nx_rows), ",")))
        lines += ["",
                  "These bytes are encoded on this machine from the file you chose. They are NOT one "
                  "of the server's verified bundles — there is no download and no checksum to compare "
                  "against, so what your file says is what the radio gets.",
                  "",
                  "The radio's current contact database is REPLACED, and there is no backup of it.",
                  "",
                  "Estimated time: %s, after a few seconds spent building the database. Keep the radio "
                  "switched on and the USB cable in the whole time — do not unplug it. You can stop the "
                  "write, which leaves the list partly written (running it again completes it). Your "
                  "codeplug is not touched." % _fmt_eta(
                      catalog.estimate_seconds(self._estimated_blocks(),
                                           self.spec.fmt if self.spec else None)),
                  "", "Continue?"]
        if not messagebox.askyesno("Write the contact list you built?", "\n".join(lines)):
            return
        held = self._begin_write()
        local = {"store": self.store, "spec": self.spec, "codes": codes, "count": count,
                 "nx": nx_rows, "name": name}
        threading.Thread(target=self._write_worker,
                         args=(port, self.base_url, None, None, None, self.ident.model,
                               self._abort, held),
                         kwargs={"local": local}, daemon=True).start()

    def _on_write_server(self):
        b = self._selected_bundle()
        port = self._selected_port()
        if b is None or port is None or self.radio is None or self.ident is None or self.token is None:
            return
        nx = self._selected_nx_bundle() if self.nx_var.get() else None
        blocks = int(b.get("blocks") or 0) + (int(nx.get("blocks") or 0) if nx else 0)
        nx_name = f" “{nx.get('listLabel') or nx.get('list')}”" if (nx and len(self._nx_map) > 1) else ""
        what = (f"{b.get('listLabel') or b.get('list')} ({int(b.get('recordCount') or 0):,} contacts)"
                + (f" and the NXDN list{nx_name} ({int(nx.get('recordCount') or 0):,} contacts)" if nx else ""))
        if not messagebox.askyesno(
                "Write the contact list?",
                f"This replaces the digital-contact database on the {self.ident.model} with:\n\n{what}\n\n"
                f"Estimated time: {_fmt_eta(catalog.estimate_seconds(blocks, b.get('format')))}. "
                "Keep the radio switched on "
                "and the USB cable in the whole time. Your codeplug is not touched.\n\nContinue?"):
            return
        held = self._begin_write()
        threading.Thread(target=self._write_worker,
                         args=(port, self.base_url, self.token, b, nx, self.ident.model,
                               self._abort, held),
                         daemon=True).start()

    def _build_local_plan(self, local, log):
        store, spec, codes = local["store"], local["spec"], local["codes"]
        count, nx_rows, name = local["count"], local["nx"], local["name"]
        self._post("build", None)
        log("building the contact database here from %s (%s contacts, %d countries / regions)"
            % (name, format(count, ","), len(codes)), "info")
        plans = [contact_build.build_dmr_segments(
            store, spec.fmt, codes, on_progress=lambda n: self._post("build", n))]
        if nx_rows and spec.nx_fmt:
            plans.append(contact_build.build_nx_segments(nx_rows))
        plan = seg.merge(*plans)
        log("built on this computer: " + contact_build.describe_plan(plan, count), "ok")
        return plan

    def _write_worker(self, port, base, token, bundle, nx_bundle, model, abort, held=None,
                      local=None):
        log = lambda m, c="info": self._post("log", (m, c))
        try:
            if local is not None:
                plan = self._build_local_plan(local, log)
            else:
                spec = contact_build.radio_for_ident(model)
                for bd, want, kind in ((bundle, spec.fmt if spec else None, "digital contact"),
                                       (nx_bundle, spec.nx_fmt if spec else None, "NXDN")):
                    got = (bd or {}).get("format")
                    if bd is not None and want and got and got != want:
                        raise catalog.ContactsError(
                            "This %s list is in %s format and the %s uses %s. The app and the "
                            "server disagree about this radio, so nothing has been written -- "
                            "update the app, and report this if it persists."
                            % (kind, got, spec.label, want))
                plans = []
                for bd in ([bundle] + ([nx_bundle] if nx_bundle else [])):
                    log("fetching " + catalog.bundle_label(bd), "info")
                    gz = catalog.download_artifact(
                        base, token, bd,
                        on_progress=lambda g, t: self._post("dl_progress", (g, t)),
                        on_status=lambda m: self._post("dl_status", m))
                    segs = seg.decode_gzip_container(gz)
                    log(f"bundle {bd.get('list')} verified (sha256 {str(bd.get('sha256'))[:12]}…): "
                        + seg.describe(segs), "ok")
                    plans.append(segs)
                plan = seg.merge(*plans)
            self._t0 = time.monotonic()
            summary = engine.write_contacts(
                port, plan,
                on_log=log,
                on_progress=lambda d, t, p: self._post("progress", (d, t, p)),
                abort=abort, expect_model=model, link=held)
            held = None
            self._post("write_done", summary)
        except AbortedError as e:
            held = None
            self._post("write_err", (str(e), True))
        except (catalog.ContactsError, seg.SegmentError, contact_build.ContactBuildError) as e:
            self._post("write_err", (str(e), False))
        except Exception as e:
            self._post("write_err", (str(e), False))
        finally:
            if held is not None:
                engine.close_session(held, log)

    def _on_write_done(self, summary: dict):
        self._writing = False
        self._abort = None
        self.abort_btn.state(["disabled"])
        self.connect_btn.state(["!disabled"])
        self.progress["value"] = 100
        self.wstatus.configure(
            text=f"Done — {int(summary.get('blocks', 0)):,} blocks written in "
                 f"{summary.get('seconds', 0):.0f} s. The radio may restart to load the new list.",
            foreground="#127a2e")
        self._refresh_write_state()

    def _on_write_error(self, msg: str, aborted: bool):
        self._writing = False
        self._abort = None
        self.abort_btn.state(["disabled"])
        self.connect_btn.state(["!disabled"])
        self.wstatus.configure(text=("Stopped: " if aborted else "Failed: ") + msg, foreground="#b00020")
        self._log(msg, "er")
        self._refresh_write_state()

    def _on_abort(self):
        if not self._writing or self._abort is None:
            return
        if not messagebox.askyesno(
                "Stop writing?",
                "The contact list will be left partly written (the radio still works; run the refresh "
                "again to complete it).\n\nStop now?"):
            return
        self._abort.set()

    def _log(self, msg, cls="info"):
        model = self.ident.model if self.ident is not None else _LOG_SOURCE
        line = "[" + time.strftime("%H:%M:%S") + "] [" + model + "] " + msg
        self.log.configure(state="normal")
        tag = cls if cls in _LOG_TAGS else ""
        self.log.insert("end", line + "\n", (tag,) if tag else ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def is_writing(self) -> bool:
        return self._writing

    def release_radio(self) -> None:
        held, self._held = self._held, None
        if held is not None:
            engine.close_session(held, lambda m, c="info": None)
