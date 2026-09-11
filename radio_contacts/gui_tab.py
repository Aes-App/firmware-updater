"""The "Digital Contact Refresh" tab.

TWO SOURCES, ONE WRITE. The operator picks one at the top of the tab:

  * A list from their aes.app account. Opened from My Contact Lists on
    cps.aes.app through an aesapp://contacts link (see radio_contacts.launch):
    that page is where a list is uploaded and where the hand-off button lives,
    since the DMR / NX Contact Refresher it used to sit on was retired. The link's
    token is claimed for a short server session, the operator picks one of the
    prebuilt lists the radio can hold, and the artifact — the exact block stream
    the factory CPS sends — is downloaded, checksum-verified and streamed. This
    is the path to prefer: those bytes are the ones measured against the CPS.
  * A list built here, from a register download the operator fetched themselves
    (radio_contacts.contact_build). No link, no token, no server: choose the
    user.csv, tick the countries, connect, write. This is why the tab is visible
    from startup — there is now something to begin in it without a link.

Both ends meet in _write_worker, which produces a plan and hands it to
engine.write_contacts: one PC-mode session, one END. Same threading shape as the
Radio and Boards tab throughout — workers only ever _post() to a queue the Tk
thread drains, and nothing but the Tk thread touches a widget.
"""
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

from . import catalog, contact_build, engine, launch
from . import segments as seg

# What the log line is about before the radio has identified itself.
_LOG_SOURCE = "Digital Contact"

_LOG_TAGS = {"tx": "#0b5fff", "rx": "#7a3fb0", "ok": "#127a2e", "er": "#b00020", "error": "#b00020",
             "info": "#444"}
_NO_SESSION_TEXT = ("Start at cps.aes.app/tools/contact-lists — the “Open in AesApp Radio Updater” "
                    "button at the bottom of My Contact Lists opens this app with a one-time link. "
                    "If your browser did not hand the link over, paste it below.")

#: Which source the operator chose. The values are the radio buttons' own, so
#: `self.source_var.get()` is the whole state — there is no second copy to drift.
_SOURCE_SERVER = "server"
_SOURCE_LOCAL = "local"

#: Blocks per contact, measured on the fixture lists of
#: tests/test_contacts_local_build.py (3.96 for the 878 family's ASCII records,
#: 7.03 for the 890's UTF-16 ones). ONLY for the estimate in the confirmation
#: dialog, which has to name a duration before the database exists. The log line
#: and the progress bar both count real blocks once it does.
_EST_BLOCKS_PER_CONTACT = {"anytone_878": 4.0, "anytone_890": 7.0}

#: The country picker's tick indicators. A ttk.Treeview has no checkbox, so the
#: state IS the row's leading glyph and must be redrawn on every toggle.
#: _TICK_SOME is a group whose countries are only partly ticked.
_TICK_ON, _TICK_OFF, _TICK_SOME = "☑", "☐", "▣"

#: What one notch of a wheel is worth, per platform, when we normalise it
#: ourselves. See ContactRefreshTab._on_wheel for why we do.
_WHEEL_DIVISOR = {"aqua": 40.0, "win32": 120.0, "x11": 120.0}

#: The most rows any single wheel event may move. macOS applies scroll
#: ACCELERATION, so a few quick notches arrive as one enormous delta; without a
#: ceiling that is a two-page jump in a six-row tree.
_WHEEL_MAX_ROWS = 4

#: TouchpadScroll deltas per row. Tk's own binding handles ONE EVENT IN FIVE and
#: then scrolls by that event's whole delta, which is where the "five gestures do
#: nothing, the sixth jumps" behaviour comes from; taking every event at 1:1
#: instead would scroll five times too fast. Five keeps the pace Tk intended
#: while nothing is dropped.
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
    """"user.csv — 38.4 MB". The size is here because it is the one cheap sign
    that a register download finished: the real file is tens of megabytes, and a
    truncated one is the commonest thing to be holding by mistake."""
    try:
        return "%s — %s" % (os.path.basename(path), _fmt_size(os.path.getsize(path)))
    except OSError:
        return os.path.basename(path)


def _facet_name(facet) -> str:
    """What to call one country in the picker: the spelling the operator's own
    file used most often (ContactStore.facets picks it), and a plain statement
    for the rows that carry no country at all — they are a fifth of some
    registers and a picker that hid them would quietly drop them."""
    if not facet.code:
        return "(no country / region in the file)"
    return facet.label or facet.code


def _facet_text(facet) -> str:
    """The haystack the filter box searches: name and code both, so "gbr" and
    "united" each find the United Kingdom."""
    return (_facet_name(facet) + " " + facet.code).lower()


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

        # ---- the local build (source = "my own register download") ----------
        # `_source` is the source that is currently APPLIED to the layout, kept
        # so a change refused mid-write can put the radio button back.
        self._source = _SOURCE_SERVER
        self.spec: Optional[contact_build.RadioSpec] = None   # what the connected radio needs
        self.store: Optional[contact_build.ContactStore] = None
        self.user_path: Optional[str] = None
        self.nx_path: Optional[str] = None
        self.nx_rows: Optional[list] = None
        self.nx_facets: list = []   # the NXDN half's country breakdown
        self._groups: list = []     # group_facets() output, rendered in ITS order
        self._sel_codes: set = set()
        # count_selected walks the whole register (a third of a million rows), so
        # the total is computed once per selection change, not once per repaint.
        self._sel_count = 0
        self._reading = False       # a read_user_csv worker is running
        self._nx_reading = False
        self._item_code: dict = {}  # tree row -> country code ("" is a REAL code)
        self._item_group: dict = {}  # tree row -> continent key
        self._item_name: dict = {}  # tree row -> its text without the tick glyph
        self._pad_accum = 0.0       # trackpad scroll left over from the last event
        # A PC-mode session opened by Connect and kept open until the write uses
        # it, so the radio restarts once for a job instead of twice. Owned by the
        # UI thread; handed to the write worker, which then owns and closes it.
        self._held = None
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
                try:
                    self._handle(kind, payload)
                except Exception as e:  # noqa: BLE001
                    # One malformed message must not take the tab down with it.
                    # Before this, any exception here escaped _drain and the
                    # re-arm below never ran, so the whole pump stopped: the log
                    # froze mid-handshake and the UI sat on its last label for
                    # ever, with the traceback going only to a stderr nobody
                    # sees in a windowed app.
                    try:
                        self._log(f"internal error handling {kind!r}: {e}", "er")
                    except Exception:  # noqa: BLE001
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
            # Whichever source is showing: the bundle dropdown, or the capacity
            # the local gate measures the selection against.
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
            # _refresh_radio_state leaves the message above alone (there is no
            # ident to describe) and re-gates whichever source is showing.
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
            # Nothing is ticked to start with: an operator who wanted every
            # country in the world would not be filtering, and a picker that
            # arrived fully ticked would make "Write" mean 300,000 contacts on
            # the first click.
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
            # The NXDN half changes what every country is worth, so the picker is
            # rebuilt rather than merely re-totalled: a country that appears ONLY
            # in the NXDN list has to appear as a row, or it could not be ticked.
            self._regroup()
            self._render_picker()
            # And the two lines BELOW the picker: the running total now has an
            # NXDN half to name, and the gate says "Reading the file…" while
            # _nx_reading is set. Clearing the flag is not enough -- nothing
            # repaints on its own, so without this the write status sat on
            # "Reading the file…" until the operator happened to click something
            # that refreshed it. Every other read completion in this handler ends
            # the same way; this one did not.
            self._refresh_local_summary()
        elif kind == "local_nx_err":
            self._nx_reading = False
            self.nx_rows = None
            self.nx_lbl.configure(text=str(payload), foreground="#b00020")
            self._log(str(payload), "er")
            self._refresh_local_summary()
        elif kind == "build":
            # The encoding happens before the first frame goes out, and on a
            # worldwide list it is seconds of pure Python. Without this the
            # progress bar sits at 0 with no explanation and the app reads as hung.
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

    # ---- UI -----------------------------------------------------------------
    def _build(self):
        outer = ttk.Frame(self.parent, padding=10)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, justify="left", wraplength=680, foreground="#444",
                  text=("Replaces the digital-contact database on your radio (the caller-ID names shown "
                        "for DMR IDs). The list can come from your aes.app account, built and verified "
                        "daily on the server, or you can build one here from a register download of "
                        "your own. The codeplug — channels, zones, contacts you created, settings — is "
                        "not touched either way.")
                  ).pack(fill="x", pady=(0, 8))

        # 1. where the list comes from. First, because it decides which of the
        # two sections below is even relevant: a link and a server session, or a
        # file on this machine and a country picker.
        src = ttk.LabelFrame(outer, text="1. Where the list comes from")
        src.pack(fill="x", pady=(0, 6))
        self.source_var = tk.StringVar(value=_SOURCE_SERVER)
        ttk.Radiobutton(src, variable=self.source_var, value=_SOURCE_SERVER,
                        command=self._on_source_change,
                        text="A list from my cps.aes.app account that I uploaded earlier"
                        ).pack(anchor="w", padx=8, pady=(6, 0))
        ttk.Radiobutton(src, variable=self.source_var, value=_SOURCE_LOCAL,
                        command=self._on_source_change,
                        text="Build a list locally using user.csv"
                        ).pack(anchor="w", padx=8, pady=(0, 6))

        # The two source sections live in containers of their own so switching is
        # one pack_forget()/pack(before=…) pair and cannot leave half a section
        # behind. Only one is ever packed; the radio row below is shared.
        self.server_box = ttk.Frame(outer)
        self.server_box.pack(fill="x")
        self.local_box = ttk.Frame(outer)
        self._build_local(self.local_box)

        # 2. session (the aes.app source)
        sess = ttk.LabelFrame(self.server_box, text="2. Link from My Contact Lists on cps.aes.app")
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

        # 3. radio (shared by both sources)
        rad = self.rad = ttk.LabelFrame(outer, text="3. Radio (switched on, USB cable in)")
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
        # Connect leaves the radio in PC mode so the write can reuse the session
        # without a second restart. Someone who then decides NOT to write needs a
        # way out that is not "quit the app" or "pull the cable": both leave the
        # radio sitting in PC mode until it is power-cycled.
        self.disconnect_btn = ttk.Button(prow, text="Disconnect", command=self._on_disconnect,
                                         state="disabled")
        self.disconnect_btn.pack(side="left", padx=(6, 0))
        self.ident_lbl = ttk.Label(rad, text="Not connected.", foreground="#666", wraplength=660,
                                   justify="left")
        self.ident_lbl.pack(fill="x", padx=8, pady=(0, 6))

        # 4. list — the server catalog's picker, so it goes away in local mode
        # (where the country picker in section 2 IS the list).
        lst = self.lst = ttk.LabelFrame(outer, text="4. Contact list")
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

        # write
        wr = self.wr = ttk.Frame(outer)
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
        self.log = scrolledtext.ScrolledText(outer, height=7, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)
        for tag, color in _LOG_TAGS.items():
            self.log.tag_configure(tag, foreground=color)

        self._t0 = None
        self._refresh_ports()

    def _build_local(self, parent):
        """Section 2 for the local source: the two files, the read, the countries.

        Built at startup even though it starts hidden, so switching sources is a
        pack() and never a "why did the first click do nothing".
        """
        loc = ttk.LabelFrame(parent, text="2. Your own register download")
        loc.pack(fill="both", expand=True, pady=(0, 6))
        ttk.Label(loc, justify="left", wraplength=660, foreground="#444",
                  text=("Your own user.csv from the register (RadioID.net ▸ Database), built into the "
                        "radio's contact database here — nothing uploaded, nothing fetched. nxdn.csv "
                        "is optional; only the D890 family has an NXDN list to put it in.")
                  ).pack(fill="x", padx=8, pady=(6, 4))

        # Choosing a file reads it -- that is what picking a file means. There
        # used to be a "Read the file" button beside Browse for the re-read after
        # a file changed on disk, or after a read that failed; it never did
        # anything Browse did not, because _on_browse_user re-reads whatever the
        # dialog returns, the same path included. One button, one meaning.
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
        # Ticked, the NXDN list is filtered by the SAME countries as the DMR one
        # and its rows count towards the picker's totals. Unticked, the file is
        # loaded and left out of the write -- which is what an operator wants
        # when the radio is an 890 but this particular write is DMR only, and is
        # also the way to drop a file chosen by mistake, so there is no Clear
        # button beside Browse.
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
        # The filter changes only what is DRAWN. The ticks live in a set of
        # country codes, so filtering can never quietly untick a country the
        # operator can no longer see -- which is exactly what a filter that
        # deleted rows from a widget holding the state would do.
        self.filter_var.trace_add("write", lambda *_a: self._render_picker())

        holder = ttk.Frame(loc)
        holder.pack(fill="both", expand=True, padx=8)
        # Six rows: the tab already carries a radio section, a write row and a
        # protocol log, and a taller tree pushes the window past a 768-pixel
        # laptop screen. The filter box and the two group buttons are what make a
        # short list of visible rows workable.
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
        # Our own scroll handling, on the widget, ahead of Tk's class bindings.
        self.tree.bind("<MouseWheel>", self._on_wheel)
        self.tree.bind("<Button-4>", self._on_wheel)      # X11 sends buttons
        self.tree.bind("<Button-5>", self._on_wheel)
        # A Mac trackpad and a Magic Mouse do NOT send MouseWheel: Tk 9 delivers
        # their precise scrolling as TouchpadScroll, an event type Tk 8.6 (which
        # the Windows build still uses) does not know at all. Binding one it does
        # not know raises, so ask rather than assume.
        try:
            self.tree.bind("<TouchpadScroll>", self._on_touchpad)
        except tk.TclError:
            pass

        srow = ttk.Frame(loc)
        srow.pack(fill="x", padx=8, pady=(6, 2))
        self.sel_lbl = ttk.Label(srow, text="No file read yet.", font=("", 13, "bold"))
        self.sel_lbl.pack(side="left")
        # The read's own state -- its progress, what it skipped, why it failed --
        # shares this line with the running total instead of taking a row of its
        # own: the tab also carries a radio section, a write row and a log, and
        # every row here is a row off the bottom of a 768-pixel screen.
        self.read_lbl = ttk.Label(srow, text="", foreground="#666", wraplength=300,
                                  justify="right", anchor="e")
        self.read_lbl.pack(side="right")
        self.fit_lbl = ttk.Label(loc, text="", wraplength=660, justify="left", foreground="#8a6d00")
        self.fit_lbl.pack(fill="x", padx=8, pady=(0, 8))

    # ---- which source ------------------------------------------------------
    def _is_local(self) -> bool:
        return self.source_var.get() == _SOURCE_LOCAL

    def _on_source_change(self):
        """Show only the section the chosen source needs, then re-judge the
        connected radio through that source's eyes.

        The two sources do NOT agree about which radios they serve:
        catalog.radio_entry answers for the server's bundles (a model is enabled
        there once the server has validated it), contact_build.RADIOS answers for
        what this app can encode. A radio can be in one and not the other, so the
        radio row is re-evaluated on every switch rather than left as it was.
        """
        if self._writing:
            # The write owns the plan it was handed; switching now would only
            # change which widgets are on screen, so put the button back.
            self.source_var.set(self._source)
            return
        self._source = self.source_var.get()
        if self._is_local():
            self.server_box.pack_forget()
            self.lst.pack_forget()
            self.local_box.pack(fill="both", expand=True, before=self.rad)
        else:
            self.local_box.pack_forget()
            self.server_box.pack(fill="x", before=self.rad)
            self.lst.pack(fill="x", pady=(0, 6), before=self.wr)
        self._refresh_radio_state()

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
        # A link only means anything to the aes.app source, and clicking it IS
        # the operator asking for that source. Switch back, or the session it
        # opens would report itself into a section nobody can see.
        if self._is_local():
            self.source_var.set(_SOURCE_SERVER)
            self._on_source_change()
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
        held, self._held = self._held, None      # the worker retires it off the UI thread
        threading.Thread(target=self._ident_worker, args=(port, held), daemon=True).start()

    def _ident_worker(self, port, previous=None):
        log = lambda m, c="info": self._post("log", (m, c))   # noqa: E731
        # Reconnecting means the old session is finished with. END it rather than
        # dropping the port, or the radio sits in PC mode until it is power-cycled.
        engine.close_session(previous, log)
        try:
            ident, link = engine.open_session(port, on_log=log)
            self._post("ident_ok", (ident, link))
        except Exception as e:  # noqa: BLE001
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
        log = lambda m, c="info": self._post("log", (m, c))   # noqa: E731
        engine.close_session(held, log)      # END, then close: never just drop the port
        self._post("disconnected", None)

    def _refresh_radio_state(self):
        """Re-evaluate the radio row for whichever source is selected: against the
        server catalog (either it or the identity may arrive first), rebuilding the
        list dropdown, or against contact_build's table for a local build."""
        if self._is_local():
            # A local build has no catalog to consult and no bundle dropdown to
            # fill: contact_build's own table is the authority there.
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

    # ---- the local build ----------------------------------------------------
    def _refresh_local_radio(self):
        """The radio row for a local build: judged by contact_build.RADIOS.

        Matched EXACTLY, by that table -- "D878UV" is a strict prefix of "D878UV2",
        and a prefix match would hand a D878UVII the first-generation radio's
        200,000-contact capacity and refuse a list that fits it.
        """
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

    # -- the two files
    def _on_browse_user(self):
        # Not during a write: the read it starts is CPU-bound for seconds, and the
        # write it would compete with has a 600 ms ACK timeout per frame -- a
        # starved wire thread turns into resends. The worker holds its own
        # reference to the store, so nothing here would corrupt the write; this is
        # about not making it slower or noisier.
        if self._writing:
            return
        # One read at a time, and it is refused OUT LOUD. Letting a second start
        # would leave two workers racing to post a store, and the loser could land
        # last -- putting the countries of a file the operator had already replaced
        # under that file's name.
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
        """Read the chosen register on a worker thread.

        Seconds of work on a worldwide file, so it cannot be done on the Tk
        thread; the row count comes back through the same queue as everything
        else. The store, the facets and every tick are dropped BEFORE the read,
        not after it, so a read that fails cannot leave the previous file's
        countries on screen beside the new file's name.
        """
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
            except Exception as e:  # noqa: BLE001
                self._post("local_err", "Unexpected error reading that file: " + str(e))
        threading.Thread(target=worker, daemon=True).start()

    def _on_browse_nx(self):
        if self._writing:                 # as _on_browse_user: not while the wire is busy
            return
        if self._nx_reading:              # and one read at a time, for the same reason
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
            except Exception as e:  # noqa: BLE001
                self._post("local_nx_err", "Unexpected error reading that file: " + str(e))
        threading.Thread(target=worker, daemon=True).start()

    def _include_nx(self) -> bool:
        """Is the NXDN half part of this write? A file, the switch, and a radio
        that has an NXDN list at all -- the 878 family has none, so an nxdn.csv
        loaded against one counts towards nothing and is written nowhere."""
        return bool(self.nx_rows) and self.nx_local_var.get() and (
            self.spec is None or self.spec.nx_fmt is not None)

    def _nx_selected(self) -> list:
        """The NXDN rows the current selection keeps."""
        if not self._include_nx():
            return []
        return contact_build.filter_nx_rows(self.nx_rows, self._sel_codes or None)

    def _regroup(self):
        """Rebuild the picker's rows from whichever halves are in play.

        Counts are COMBINED when the NXDN list is included, which is also what
        orders the picker: an operator choosing countries for a write that
        carries both lists is choosing for both, so the number beside a country
        has to be what that country costs them.
        """
        if self.store is None:
            self._groups = []
            return
        facets = self.store.facets()
        if self._include_nx():
            facets = contact_build.merge_facets(facets, self.nx_facets)
        self._groups = contact_build.group_facets(facets)

    def _on_nx_include(self):
        """The Also Filter switch: it changes every count in the picker."""
        if self._writing:
            self.nx_local_var.set(not self.nx_local_var.get())
            return
        self._regroup()
        self._render_picker()
        self._refresh_local_summary()

    def _forget_store(self):
        self.store = None
        self._groups = []
        self._select_codes(set())     # clears the count and the gate message too
        self._render_picker()

    # -- the country picker
    def _render_picker(self):
        """(Re)build the tree from group_facets' output, in ITS order.

        That order is the whole point of the widget: continents by the contacts
        they hold, then the countries inside each the same way, both descending,
        because an operator is looking for the handful of countries they actually
        work and those are the big ones. group_facets has already done it --
        re-sorting here (by name, say, because the rows "look unsorted") would
        undo the one thing the picker has to get right.
        """
        for iid in self.tree.get_children(""):
            self.tree.delete(iid)
        self._item_code, self._item_group, self._item_name = {}, {}, {}
        needle = self.filter_var.get().strip().lower()
        for key, name, total, members in self._groups:
            shown = [f for f in members if not needle or needle in _facet_text(f)]
            if not shown:
                continue
            # Folded, unless a filter is on: a hundred and eighty countries open
            # at once is a list nobody can use, and the group rows carry the
            # number that decides whether it is worth opening one. A search has to
            # show what it found, so it opens what it kept.
            gid = self.tree.insert("", "end", values=(format(total, ","),), open=bool(needle))
            self._item_group[gid] = key
            self._item_name[gid] = name
            for facet in shown:
                cid = self.tree.insert(gid, "end", values=(format(facet.count, ","),))
                self._item_code[cid] = facet.code
                self._item_name[cid] = _facet_name(facet)
        self._update_ticks()

    def _update_ticks(self):
        """Redraw every row's tick from the selection set.

        A group's glyph describes the rows ON SCREEN: while a filter is on, a
        group can read ☑ with unticked countries hidden behind the filter. The
        number beside it is still the group's whole total, and the running total
        below the tree is still the truth — that is the line to trust.
        """
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
        """One click anywhere on a row toggles it.

        Two traps, both measured rather than guessed. The column headings and the
        separators between them are Button-1 targets, and a bare "toggle the row
        under the pointer" fires on a click that was meant to sort or to drag a
        column edge. And a click on a continent's expand/collapse triangle must
        expand it, not tick the whole group -- but "Treeitem.indicator" is what
        the widget reports for the INDENT of every row, childless ones included,
        so vetoing that element alone made a country's leading third of a row
        dead. Only a row that HAS children owns a real triangle.
        """
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
        """Scroll the picker by whole rows, on every platform.

        Tk hands the wheel delta over raw and its own Treeview binding divides by
        40 and TRUNCATES. On macOS that is both too little and too much: a single
        notch arrives as a delta well under 40, so it scrolls zero rows and the
        list looks stuck for three or four clicks -- and then the OS's scroll
        acceleration coalesces the next few into one huge delta, which divides
        into a jump of two pages. (Measured on Tk 9.0.4/aqua, whose class binding
        is `tk::MouseWheel %W y %D -40.0`.)

        So: every event moves at least one row, an accelerated one moves in
        proportion, and nothing moves more than _WHEEL_MAX_ROWS at a time. The
        remainder is deliberately dropped rather than queued -- a fling should
        stop when the fingers do.
        """
        delta = float(getattr(event, "delta", 0) or 0)
        num = getattr(event, "num", 0)
        if num == 4:            # X11 has no delta; it sends button 4/5
            delta = 1.0
        elif num == 5:
            delta = -1.0
        if delta == 0.0:
            return "break"

        try:
            system = self.tree.tk.call("tk", "windowingsystem")
        except tk.TclError:     # pragma: no cover -- a torn-down widget
            system = "aqua"
        divisor = _WHEEL_DIVISOR.get(system, 120.0)
        rows = 1 if delta > 0 else -1
        if abs(delta) >= divisor:
            rows = int(delta / divisor)
        rows = max(-_WHEEL_MAX_ROWS, min(_WHEEL_MAX_ROWS, rows))
        self.tree.yview_scroll(-rows, "units")

        return "break"

    def _on_touchpad(self, event):
        """Trackpad and Magic Mouse scrolling, which is not a wheel event at all.

        THIS is what was still jumping. Tk 9 sends precise scrolling as
        <TouchpadScroll>, whose %D packs two 16-bit deltas, and its own Treeview
        binding reads one event in five:

            if {%# %% 5 == 0} { ... %W yview scroll [expr {-$deltaY}] units }

        So four gestures out of five do nothing at all, and the fifth scrolls by
        its whole delta in ROWS -- two pages of a six-row tree on any real flick.
        A <MouseWheel> handler never sees these events, which is why the first fix
        did not help anyone scrolling with a trackpad.

        Here every event counts, the fractions accumulate so a slow drag still
        moves, and a flick is capped like a wheel burst.
        """
        packed = int(getattr(event, "delta", 0) or 0)
        low = packed & 0xFFFF
        delta_y = low if low < 0x8000 else low - 0x10000
        if delta_y == 0:
            return "break"

        self._pad_accum += delta_y / _TOUCHPAD_PER_ROW
        rows = int(self._pad_accum)          # truncates toward zero
        if rows:
            self._pad_accum -= rows
            self.tree.yview_scroll(max(-_WHEEL_MAX_ROWS, min(_WHEEL_MAX_ROWS, -rows)), "units")

        return "break"

    def _on_tree_key(self, _event):
        for iid in self.tree.selection():
            self._toggle_item(iid)
        return "break"

    def _toggle_item(self, iid):
        """Toggle one country, or every country its group has on screen.

        A group whose visible members are all ticked clears them; anything else
        ticks them all -- so the parent row is both "select this continent" and
        "clear it", and with a filter on it is "select these search hits".
        """
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
        """Every country in the file, filter or no filter: "Select all" that
        selected only the search hits would be a trap with a stale filter box."""
        self._select_codes({f.code for _k, _n, _t, members in self._groups for f in members})

    def _select_codes(self, codes):
        """The ONE place the selection changes.

        The ticks are a set of country codes and the tree is only a drawing of
        it. The total is cached here because ContactStore.count_selected walks the
        whole register -- a third of a million rows -- and every repaint would
        otherwise pay for it.
        """
        self._sel_codes = set(codes)
        self._sel_count = (self.store.count_selected(self._sel_codes)
                           if self.store is not None else 0)
        self._update_ticks()
        self._refresh_local_summary()

    # -- what the operator is about to write
    def _estimated_blocks(self) -> int:
        """A block count BEFORE the database exists, for the dialog's ETA only.

        The real figure is logged the moment the plan is built, and the progress
        bar counts real blocks; this is an average per contact and nothing more.
        """
        if self.spec is None:
            return 0
        blocks = int(self._sel_count * _EST_BLOCKS_PER_CONTACT.get(self.spec.fmt, 4.0))
        nx = self._nx_selected()
        if nx:
            blocks += int(len(nx) * contact_build.NX_REC / float(seg.BLOCK))
        return blocks

    def _refresh_local_summary(self):
        """The running total above the gate message, then the gate itself."""
        if self.store is None:
            self.sel_lbl.configure(text="No file read yet.")
        elif not self._sel_codes:
            self.sel_lbl.configure(text=f"Nothing selected, of {len(self.store):,} in the file.")
        else:
            # The two halves are separate databases in separate flash, so the
            # line says what each costs rather than one number that means neither.
            nx = len(self._nx_selected())
            if nx:
                text = (f"{self._sel_count + nx:,} contacts selected "
                        f"({self._sel_count:,} DMR + {nx:,} NXDN)")
            else:
                text = f"{self._sel_count:,} contacts selected"
            self.sel_lbl.configure(text=text)
        self._refresh_write_state()

    def _local_gate(self) -> tuple:
        """(may we write, what to say, what colour) for a locally built list.

        No token and no session anywhere in here, by design: a list built on this
        machine needs a file, a selection, a port and a radio this app can encode
        for. The server has no say in it -- which is the whole point of the source.
        """
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
        # _fmt_eta already says "about", so this must not say it twice.
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
                # A SEPARATE POOL: the NXDN database has its own flash and its own
                # ceiling, so a full DMR list costs it nothing. Two refusals,
                # because they mean different things -- one is the radio's limit,
                # the other is ours.
                if self.spec.nx_capacity and len(nx) > self.spec.nx_capacity:
                    return (False, "%s NXDN contacts is more than the %s this %s holds. The DMR "
                            "list has its own room and is unaffected."
                            % (format(len(nx), ","), format(self.spec.nx_capacity, ","),
                               self.spec.label), "#b00020")
                if len(nx) > contact_build.NX_MAX_RECORDS:
                    # Refused HERE rather than by the encoder mid-write: past this
                    # the records would reach their own search index, and no
                    # capture shows where the radio moves it.
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
            # Index 0 on purpose, and the server decides what lands there: it
            # puts owned DMR rows first but owned NXDN rows LAST, so an
            # untouched checkbox still writes the worldwide list exactly as it
            # did before pickers existed. Choosing your own is one click.
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
        parts.append(f"write {_fmt_eta(catalog.estimate_seconds(blocks, b.get('format')))}")
        self.list_info.configure(text=" · ".join(parts), foreground="#444")
        self._refresh_write_state()

    def _refresh_write_state(self):
        if self._is_local():
            # The local gate names its own reason, and the label carries it: a
            # disabled Write button with nothing said about why is the failure
            # mode this replaces.
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
        # Offered only while there is really a session to end: not before
        # Connect, not during a write, and not after one (the write's own END
        # already released the radio).
        self.disconnect_btn.state(
            ["!disabled"] if (self._held is not None and not self._busy and not self._writing)
            else ["disabled"])

    # ---- write --------------------------------------------------------------
    def _on_write(self):
        if self._is_local():
            self._on_write_local()
        else:
            self._on_write_server()

    def _begin_write(self):
        """Arm the UI for a write and hand the held session over.

        Ownership of the PC-mode link moves to the worker: write_contacts()
        closes whatever it is given, so the tab must not keep a second reference
        to it. Returns the link for the caller to pass on.
        """
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
        """Confirm and start a write of the list built on this machine.

        The confirmation says where the bytes came from because that is the one
        thing that differs from the other source, and it is not a detail: nothing
        checked these bytes against a download, and the database they replace has
        no backup anywhere.
        """
        ok, _msg, _colour = self._local_gate()
        port = self._selected_port()
        if not ok or port is None or self.ident is None or self.spec is None or self.store is None:
            return
        codes = sorted(self._sel_codes)
        count = self._sel_count
        # An nxdn.csv is only written to a radio that HAS an NXDN list; on any
        # other model it is silently irrelevant, and the gate has already said so.
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
        # Name the NXDN list only when there was one to choose: with the single
        # server list the confirmation reads exactly as it always has.
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
        """Encode the operator's selection into a write plan. WORKER THREAD ONLY.

        This is the seam between the two sources: the downloaded path decodes a
        container the server built, this one builds the same thing here. Both
        arrive at one address-ascending plan, and everything downstream --
        capacity gate aside -- cannot tell them apart.
        """
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
        log = lambda m, c="info": self._post("log", (m, c))   # noqa: E731
        try:
            if local is not None:
                # Encoded on THIS thread: a worldwide list is seconds of pure
                # Python and the Tk thread must not spend them.
                plan = self._build_local_plan(local, log)
            else:
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
            held = None                      # write_contacts closed it
            self._post("write_done", summary)
        except AbortedError as e:
            held = None                      # write_contacts closed it on its way out
            self._post("write_err", (str(e), True))
        except (catalog.ContactsError, seg.SegmentError, contact_build.ContactBuildError) as e:
            # ContactBuildError messages are written for an operator; show as-is.
            self._post("write_err", (str(e), False))
        except Exception as e:  # noqa: BLE001
            self._post("write_err", (str(e), False))
        finally:
            # Only reached with a link still open when the download failed before
            # write_contacts was ever called. Give the radio its END back.
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

    # ---- log ----------------------------------------------------------------
    def _log(self, msg, cls="info"):
        # The bracket names whoever the line is about: the radio once it has told
        # us what it is, and this tab before that. It used to fall back to an em
        # dash, which reads as "something is missing" on precisely the lines that
        # run BEFORE the identity comes back -- the whole connect handshake.
        model = self.ident.model if self.ident is not None else _LOG_SOURCE
        line = "[" + time.strftime("%H:%M:%S") + "] [" + model + "] " + msg
        self.log.configure(state="normal")
        tag = cls if cls in _LOG_TAGS else ""
        self.log.insert("end", line + "\n", (tag,) if tag else ())
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---- window-close hook (called by main) ---------------------------------
    def is_writing(self) -> bool:
        return self._writing

    def release_radio(self) -> None:
        """END any session Connect is holding, so quitting never leaves the radio
        stuck in PC mode. Safe to call when nothing is held."""
        held, self._held = self._held, None
        if held is not None:
            engine.close_session(held, lambda m, c="info": None)
