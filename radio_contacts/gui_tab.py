"""The "Digital Contact Refresh" tab.

Opened from the Tools page on cps.aes.app through an aesapp://contacts link
(see radio_contacts.launch): the link's token is claimed for a short server
session, the radio is identified over its COM port, the operator picks one of
the prebuilt lists the radio can hold, and the artifact (the exact block
stream the factory CPS sends) is downloaded, checksum-verified and streamed in
one PC-mode session. Same threading shape as the Radio and Boards tab: workers
post to a queue the Tk thread drains.
"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import Optional

from serial.tools import list_ports

from radio_fw.engines import AbortedError

from . import catalog, engine, launch
from . import segments as seg

_LOG_TAGS = {"tx": "#0b5fff", "rx": "#7a3fb0", "ok": "#127a2e", "er": "#b00020", "error": "#b00020",
             "info": "#444"}
_NO_SESSION_TEXT = ("Start from the Tools page on cps.aes.app — the “Open in AesApp Radio Updater” "
                    "button opens this app with a one-time link. If your browser did not hand the link "
                    "over, paste it below.")


def _fmt_eta(seconds: float) -> str:
    if seconds < 90:
        return f"about {int(round(seconds))} s"
    return f"about {int(round(seconds / 60.0))} min"


class ContactRefreshTab:
    """Builds the whole tab inside `parent` (a Notebook page). `root` is the Tk
    root, used for the .after() UI poll and modal dialogs."""

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
        self._busy = False          # a connect / session / catalog worker is running
        self._writing = False
        self._abort: Optional[threading.Event] = None
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
            self.ident = payload
            self.connect_btn.state(["!disabled"])
            self.ident_lbl.configure(
                text=f"Connected: {payload.model or '?'} {payload.version}".strip(), foreground="#127a2e")
            self._refresh_radio_state()
        elif kind == "ident_err":
            self._busy = False
            self.ident = None
            self.radio = None
            self.connect_btn.state(["!disabled"])
            self.ident_lbl.configure(text=str(payload), foreground="#b00020")
            self._log(str(payload), "er")
            self._populate_lists()
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

    # ---- UI -----------------------------------------------------------------
    def _build(self):
        outer = ttk.Frame(self.parent, padding=10)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, justify="left", wraplength=680, foreground="#444",
                  text=("Replaces the digital-contact database on your radio (the caller-ID names shown "
                        "for DMR IDs) with a list built daily on the AesApp server. The codeplug — "
                        "channels, zones, contacts you created, settings — is not touched.")
                  ).pack(fill="x", pady=(0, 8))

        # 1. session
        sess = ttk.LabelFrame(outer, text="1. Link from cps.aes.app")
        sess.pack(fill="x", pady=(0, 6))
        self.session_status = ttk.Label(sess, text=_NO_SESSION_TEXT, justify="left", wraplength=660,
                                        foreground="#8a6d00")
        self.session_status.pack(fill="x", padx=8, pady=(6, 4))
        row = ttk.Frame(sess)
        row.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(row, text="Paste link:").pack(side="left")
        self.link_var = tk.StringVar()
        self.link_entry = ttk.Entry(row, textvariable=self.link_var, width=52)
        self.link_entry.pack(side="left", padx=6, fill="x", expand=True)
        self.link_entry.bind("<Return>", lambda _e: self._on_use_link())
        self.link_btn = ttk.Button(row, text="Use link", command=self._on_use_link)
        self.link_btn.pack(side="left")

        # 2. radio
        rad = ttk.LabelFrame(outer, text="2. Radio (switched on, USB cable in)")
        rad.pack(fill="x", pady=(0, 6))
        prow = ttk.Frame(rad)
        prow.pack(fill="x", padx=8, pady=6)
        ttk.Label(prow, text="COM port:").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(prow, textvariable=self.port_var, state="readonly", width=40)
        self.port_box.pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(prow, text="Refresh", command=self._refresh_ports).pack(side="left")
        self.connect_btn = ttk.Button(prow, text="Connect", command=self._on_connect)
        self.connect_btn.pack(side="left", padx=(6, 0))
        self.ident_lbl = ttk.Label(rad, text="Not connected.", foreground="#666", wraplength=660,
                                   justify="left")
        self.ident_lbl.pack(fill="x", padx=8, pady=(0, 6))

        # 3. list
        lst = ttk.LabelFrame(outer, text="3. Contact list")
        lst.pack(fill="x", pady=(0, 6))
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

        # 4. write
        wr = ttk.Frame(outer)
        wr.pack(fill="x", pady=(2, 0))
        self.write_btn = ttk.Button(wr, text="Write to radio", command=self._on_write)
        self.write_btn.pack(side="left")
        self.write_btn.state(["disabled"])
        self.abort_btn = ttk.Button(wr, text="Stop", command=self._on_abort)
        self.abort_btn.pack(side="left", padx=8)
        self.abort_btn.state(["disabled"])
        self.progress = ttk.Progressbar(outer, mode="determinate", maximum=100.0)
        self.progress.pack(fill="x", pady=(6, 0))
        self.wstatus = ttk.Label(outer, text="", foreground="#444", wraplength=680, justify="left")
        self.wstatus.pack(fill="x", pady=(4, 0))

        ttk.Label(outer, text="Protocol log").pack(anchor="w", pady=(8, 0))
        self.log = scrolledtext.ScrolledText(outer, height=9, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)
        for tag, color in _LOG_TAGS.items():
            self.log.tag_configure(tag, foreground=color)

        self._t0 = None
        self._refresh_ports()

    # ---- session ------------------------------------------------------------
    def handle_launch_url(self, url: str):
        """Entry point for an aesapp:// link (OS handoff, forwarded instance, or
        the paste box). Claims the token on a worker thread."""
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
        self.token = req.token
        self.base_url = req.base_url(catalog.DEFAULT_BASE_URL)
        try:
            import logging
            logging.getLogger("bt_ota").info("contact tab: link accepted, server %s", self.base_url)
        except Exception:  # noqa: BLE001
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
        except Exception as e:  # noqa: BLE001
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
            except Exception as e:  # noqa: BLE001
                self._post("catalog_err", "Unexpected error: " + str(e))
        threading.Thread(target=worker, daemon=True).start()

    # ---- radio --------------------------------------------------------------
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
        threading.Thread(target=self._ident_worker, args=(port,), daemon=True).start()

    def _ident_worker(self, port):
        try:
            ident = engine.identify(port, on_log=lambda m, c="info": self._post("log", (m, c)))
            self._post("ident_ok", ident)
        except Exception as e:  # noqa: BLE001
            self._post("ident_err", str(e))

    def _refresh_radio_state(self):
        """Re-evaluate the radio row against the catalog (either may arrive
        first) and rebuild the list dropdown."""
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

    # ---- list ---------------------------------------------------------------
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
                # More than one NXDN list only happens once the operator has
                # built their own on the web app, so the picker only shows up
                # then; with the one server list the row reads exactly as it
                # always has.
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
        """The NXDN list to write alongside the DMR one — the only one there is
        when there is only one, which is why its picker can stay hidden then."""
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
        parts.append(f"write {_fmt_eta(catalog.estimate_seconds(blocks))}")
        self.list_info.configure(text=" · ".join(parts), foreground="#444")
        self._refresh_write_state()

    def _refresh_write_state(self):
        ok = (not self._busy and not self._writing and self.session is not None and self.radio is not None
              and self._selected_bundle() is not None and self._selected_port() is not None)
        self.write_btn.state(["!disabled"] if ok else ["disabled"])
        self.link_btn.state(["disabled"] if (self._busy or self._writing) else ["!disabled"])
        self.nx_box.state(["disabled"] if self._writing else ["!disabled"])

    # ---- write --------------------------------------------------------------
    def _on_write(self):
        b = self._selected_bundle()
        port = self._selected_port()
        if b is None or port is None or self.radio is None or self.ident is None or self.token is None:
            return
        nx = self._selected_nx_bundle() if self.nx_var.get() else None
        blocks = int(b.get("blocks") or 0) + (int(nx.get("blocks") or 0) if nx else 0)
        # Name the NXDN list only when there was one to choose: with the single
        # server list the confirmation reads exactly as it always has.
        nx_name = f" “{nx.get('listLabel') or nx.get('list')}”" if (nx and len(self._nx_map) > 1) else ""
        what = (f"{b.get('listLabel') or b.get('list')} ({int(b.get('recordCount') or 0):,} contacts)"
                + (f" and the NXDN list{nx_name} ({int(nx.get('recordCount') or 0):,} contacts)" if nx else ""))
        if not messagebox.askyesno(
                "Write the contact list?",
                f"This replaces the digital-contact database on the {self.ident.model} with:\n\n{what}\n\n"
                f"Estimated time: {_fmt_eta(catalog.estimate_seconds(blocks))}. Keep the radio switched on "
                "and the USB cable in the whole time. Your codeplug is not touched.\n\nContinue?"):
            return
        self._writing = True
        self._abort = threading.Event()
        self._t0 = None
        self.write_btn.state(["disabled"])
        self.abort_btn.state(["!disabled"])
        self.connect_btn.state(["disabled"])
        self.progress["value"] = 0
        self.wstatus.configure(text="Preparing…", foreground="#444")
        self._refresh_write_state()
        threading.Thread(target=self._write_worker,
                         args=(port, self.base_url, self.token, b, nx, self.ident.model, self._abort),
                         daemon=True).start()

    def _write_worker(self, port, base, token, bundle, nx_bundle, model, abort):
        log = lambda m, c="info": self._post("log", (m, c))   # noqa: E731
        try:
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
                abort=abort, expect_model=model)
            self._post("write_done", summary)
        except AbortedError as e:
            self._post("write_err", (str(e), True))
        except (catalog.ContactsError, seg.SegmentError) as e:
            self._post("write_err", (str(e), False))
        except Exception as e:  # noqa: BLE001
            self._post("write_err", (str(e), False))

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

    # ---- log ----------------------------------------------------------------
    def _log(self, msg, cls="info"):
        model = self.ident.model if self.ident is not None else "—"
        line = "[" + time.strftime("%H:%M:%S") + "] [" + model + "] " + msg
        self.log.configure(state="normal")
        tag = cls if cls in _LOG_TAGS else ""
        self.log.insert("end", line + "\n", (tag,) if tag else ())
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---- window-close hook (called by main) ---------------------------------
    def is_writing(self) -> bool:
        return self._writing
