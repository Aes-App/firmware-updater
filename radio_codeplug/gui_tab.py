from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import Optional

from serial.tools import list_ports

from radio_fw.engines import AbortedError
from radio_contacts import catalog as _cat
from radio_contacts import engine as ce
from radio_contacts import launch
from radio_contacts import segments as seg

from . import client, engine

_LOG_SOURCE = "Codeplug"

_LOG_TAGS = {"tx": "#0b5fff", "rx": "#7a3fb0", "ok": "#127a2e", "er": "#b00020", "error": "#b00020",
             "info": "#444"}
_NO_SESSION_TEXT = ("Start from the codeplug you want to write on cps.aes.app — “Write Codeplug to "
                    "Radio”, then “Open in the desktop app”. If your browser did not hand the link "
                    "over, paste it below.")


def _fmt_eta(seconds: float) -> str:
    if seconds < 90:
        return f"about {int(round(seconds))} s"
    return f"about {int(round(seconds / 60.0))} min"


class CodeplugWriteTab:

    def __init__(self, parent: tk.Widget, root: tk.Tk):
        self.parent = parent
        self.root = root
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self.session: Optional[client.CodeplugSession] = None
        self.job: Optional[client.CodeplugJob] = None
        self.plan: Optional[client.WritePlan] = None
        self.ident = None
        self._port_map: dict[str, str] = {}
        self._last_port: Optional[str] = None
        self._busy = False
        self._writing = False
        self._abort: Optional[threading.Event] = None
        self._t0: Optional[float] = None
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
        elif kind == "job_ok":
            self._busy = False
            self.job, self.plan = payload
            self.ident = None
            self._describe_job()
            self._refresh_write_state()
        elif kind == "job_err":
            self._busy = False
            self.session = self.job = self.plan = None
            self.ident = None
            self.session_status.configure(text=str(payload), foreground="#b00020")
            self._log(str(payload), "er")
            self._refresh_write_state()
        elif kind == "progress":
            done, total, phase = payload
            pct = (done * 100.0 / total) if total else 0.0
            self.progress["value"] = pct
            if phase in ("codeplug", "contacts") and self._t0 and done:
                rate = done / max(1e-6, time.monotonic() - self._t0)
                left = (total - done) / max(1e-6, rate)
                what = "codeplug" if phase == "codeplug" else "digital contact list"
                self.wstatus.configure(
                    text=f"Writing the {what} — do not unplug the radio.  {pct:.0f}%  "
                         f"({done:,}/{total:,} blocks, {_fmt_eta(left)} left)", foreground="#444")
            elif phase == "verify":
                self.wstatus.configure(text=f"Reading the codeplug back to verify it… {pct:.0f}%",
                                       foreground="#444")
            else:
                self.wstatus.configure(text=f"Writing ({phase}) — do not unplug the radio.",
                                       foreground="#444")
        elif kind == "ident":
            self.ident = payload
        elif kind == "write_done":
            self._on_write_done(payload)
        elif kind == "write_err":
            msg, aborted = payload
            self._on_write_error(msg, aborted)

    def _build(self):
        outer = ttk.Frame(self.parent, padding=10)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, justify="left", wraplength=680, foreground="#444",
                  text=("Writes a codeplug prepared on cps.aes.app to your radio over USB — the same "
                        "bytes the web app would write, but at full speed. Your project stays "
                        "read-only on the server until this finishes, so nothing can change "
                        "underneath the write.")
                  ).pack(fill="x", pady=(0, 8))

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

        rad = ttk.LabelFrame(outer, text="2. Radio (switched on, USB cable in)")
        rad.pack(fill="x", pady=(0, 6))
        prow = ttk.Frame(rad)
        prow.pack(fill="x", padx=8, pady=6)
        ttk.Label(prow, text="COM port:").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(prow, textvariable=self.port_var, state="readonly", width=40)
        self.port_box.pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(prow, text="Refresh", command=self._refresh_ports).pack(side="left")

        job = ttk.LabelFrame(outer, text="3. Codeplug to write")
        job.pack(fill="x", pady=(0, 6))
        self.job_info = ttk.Label(job, text="Open a link first.", foreground="#666",
                                  wraplength=660, justify="left")
        self.job_info.pack(fill="x", padx=8, pady=6)

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

        self._refresh_ports()

    def _log(self, msg: str, cls: str = "info"):
        who = self.ident.model if self.ident is not None else _LOG_SOURCE
        line = "[" + time.strftime("%H:%M:%S") + "] [" + who + "] " + msg
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n", cls if cls in _LOG_TAGS else "info")
        self.log.see("end")
        self.log.configure(state="disabled")

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

    def _refresh_write_state(self):
        ready = bool(self.plan) and not self._busy and not self._writing
        self.write_btn.state(["!disabled"] if ready else ["disabled"])
        self.abort_btn.state(["!disabled"] if self._writing else ["disabled"])
        for w in (self.link_btn, self.link_entry, self.port_box):
            try:
                w.state(["disabled"] if self._writing else ["!disabled"])
            except Exception:
                pass

    def _describe_job(self):
        if not self.job or not self.plan:
            return
        s = self.plan.stats or {}
        lines = [f"“{self.job.project_name}”  ·  {self.job.model}"]
        counts = [f"{s.get(k, 0):,} {label}" for k, label in
                  (("channels", "channels"), ("zones", "zones"), ("scanLists", "scan lists"),
                   ("talkGroups", "talk groups"))
                  if s.get(k)]
        if counts:
            lines.append("  ·  ".join(counts))
        cb = seg.block_count(self.plan.codeplug)
        lines.append(f"{cb:,} blocks of codeplug"
                     + (f" + {seg.block_count(self.plan.contacts):,} blocks of digital contacts"
                        if self.plan.contacts else " (digital contacts not written)"))
        lines.append("This REPLACES the channels, zones, scan lists, talk groups, radio IDs, RX groups, "
                     "roaming, APRS and tones on the radio. The band plan is not changed.")
        self.job_info.configure(text="\n".join(lines), foreground="#444")

    def handle_launch_url(self, url: str):
        try:
            req = launch.parse_launch_url(url)
        except launch.LaunchError as e:
            self.session_status.configure(text="That link cannot be used: " + str(e), foreground="#b00020")
            self._log("link refused: " + str(e), "er")
            return
        if req.action != launch.ACTION_CODEPLUG:
            self.session_status.configure(text="That link is not a codeplug write.", foreground="#b00020")
            return
        if self._writing:
            self.session_status.configure(
                text="A write is in progress — the new link will be ignored until it finishes.",
                foreground="#8a6d00")
            return
        base = req.base_url(_cat.DEFAULT_BASE_URL)
        self.session = client.CodeplugSession(
            base, req.token, on_status=lambda m: self._post("status", (m, "#8a6d00")))
        self.job = self.plan = None
        self.link_var.set("")
        self._busy = True
        self._refresh_write_state()
        self.session_status.configure(text="Checking the link with the AesApp server…", foreground="#444")
        self._log("link received for " + base + "; claiming the write session", "info")
        threading.Thread(target=self._session_worker, args=(self.session,), daemon=True).start()

    def _session_worker(self, sess: client.CodeplugSession):
        try:
            job = sess.claim()
            self._post("log", (f"session opened for “{job.project_name}” ({job.model})", "ok"))
            plan = sess.fetch_plan()
            self._post("log", ("write data downloaded: " + seg.describe(plan.codeplug)
                               + (f"; contacts: {seg.describe(plan.contacts)}" if plan.contacts else ""), "ok"))
            self._post("job_ok", (job, plan))
            self._post("status", (f"Ready to write “{job.project_name}”. "
                                  f"Your project is locked on the server while this runs.", "#127a2e"))
        except _cat.ContactsError as e:
            self._post("job_err", str(e))
        except Exception as e:
            self._post("job_err", "Unexpected error: " + str(e))

    def _on_use_link(self):
        url = self.link_var.get().strip()
        if url:
            self.handle_launch_url(url)

    def _on_write(self):
        if self._writing or not self.plan or not self.job or not self.session:
            return
        port = self._selected_port()
        if not port:
            messagebox.showwarning("No COM port", "Pick the radio's COM port. Click Refresh if it isn't listed.")
            return
        contacts_line = ("\n• The digital contact list WILL be written too — a separate, slower phase "
                         "with a second restart."
                         if self.plan.contacts else "\n• Digital contacts are kept (not written).")
        if not messagebox.askyesno(
                "Write this codeplug?",
                f"Write “{self.job.project_name}” to the radio on {port}?\n\n"
                "• Channels, zones, scan lists, talk groups, radio IDs, RX groups, roaming, APRS and "
                "tones are REPLACED."
                + contacts_line
                + "\n• The band plan is kept.\n"
                "• The radio restarts after each phase; leave it plugged in.\n\nContinue?"):
            return
        self._last_port = port
        self._writing = True
        self._t0 = time.monotonic()
        self._abort = threading.Event()
        self.progress["value"] = 0
        self._refresh_write_state()
        self._log(f"writing “{self.job.project_name}” to {port}", "ok")
        threading.Thread(target=self._write_worker,
                         args=(port, self.session, self.job, self.plan, self._abort),
                         daemon=True).start()

    def _write_worker(self, port, sess: client.CodeplugSession, job: client.CodeplugJob,
                      plan: client.WritePlan, abort: threading.Event):
        def on_log(m, c="info"):
            self._post("log", (m, c))

        def on_progress(done, total, phase):
            self._post("progress", (done, total, phase))

        def on_phase(phase, done, total):
            try:
                sess.heartbeat(phase, done, total)
            except client.LockLostError as e:
                abort.set()
                self._post("log", (str(e), "er"))

        try:
            res = engine.write_codeplug(
                port, plan.codeplug, plan.contacts,
                on_log=on_log, on_progress=on_progress, on_phase=on_phase,
                abort=abort, ident_tokens=job.ident_tokens,
                on_ident=lambda i: self._post("ident", i),
            )
            sess.report(res.outcome, res.detail(), res.message)
            self._post("write_done", res)
        except AbortedError as e:
            sess.report(client.OUTCOME_WRITE_FAILED, {"client": "desktop", "aborted": True}, str(e))
            self._post("write_err", (str(e), True))
        except (ce.ContactWriteError, OSError) as e:
            sess.report(client.OUTCOME_WRITE_FAILED, {"client": "desktop"}, str(e))
            self._post("write_err", (str(e), False))
        except Exception as e:
            sess.report(client.OUTCOME_WRITE_FAILED, {"client": "desktop"}, str(e))
            self._post("write_err", ("Unexpected error: " + str(e), False))

    def _on_abort(self):
        if self._abort is not None:
            self._abort.set()
            self._log("stopping after the current block…", "er")

    def _on_write_done(self, res: engine.WriteResult):
        self._writing = False
        self._abort = None
        self.progress["value"] = 100 if res.outcome == "success" else self.progress["value"]
        colour = {"success": "#127a2e", "verify_unavailable": "#8a6d00"}.get(res.outcome, "#b00020")
        self.wstatus.configure(text=res.message, foreground=colour)
        self._log(res.message, "ok" if res.outcome == "success" else "er")
        self.plan = None
        self.session_status.configure(
            text="Done — your project is editable again on cps.aes.app. Start another write there if "
                 "you need one.", foreground="#444")
        self._refresh_write_state()
        if res.outcome == "success":
            messagebox.showinfo("Codeplug written", res.message)
        else:
            messagebox.showwarning("Codeplug write finished with a warning", res.message)

    def _on_write_error(self, msg: str, aborted: bool):
        self._writing = False
        self._abort = None
        self.wstatus.configure(text=msg, foreground="#b00020")
        self._log(msg, "er")
        self.plan = None
        self._refresh_write_state()
        if not aborted:
            messagebox.showerror("Codeplug write failed", msg)

    def is_writing(self) -> bool:
        return self._writing
