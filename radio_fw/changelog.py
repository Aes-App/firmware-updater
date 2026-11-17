from __future__ import annotations

import re
import tkinter as tk
from tkinter import font as tkfont, ttk
from typing import Callable

from . import layout

_ITEM = re.compile(r"^\s*(\d+[.)]|[-*•])\s+")

NO_CHANGELOG = "No changelog has been published for this version."
_BORDER = "#c8c8c8"


def changelog_of(bundle) -> str:
    text = bundle.get("changelog") if isinstance(bundle, dict) else None
    return text.strip() if isinstance(text, str) else ""


def render(text_widget: tk.Text, text: str, font: tkfont.Font) -> None:
    text_widget.configure(state="normal")
    text_widget.delete("1.0", "end")
    if not text:
        text_widget.insert("end", NO_CHANGELOG, ("none",))
    for line in text.split("\n") if text else []:
        item = _ITEM.match(line)
        if not line.strip():
            text_widget.insert("end", "\n", ("gap",))
        elif item:
            marker = item.group(1) + " "
            hang = font.measure(marker)
            tag = "item%d" % hang
            text_widget.tag_configure(tag, lmargin1=0, lmargin2=hang, spacing1=3)
            text_widget.insert("end", marker + line[item.end():].strip() + "\n", (tag,))
        else:
            text_widget.insert("end", line.strip() + "\n", ("para",))
    text_widget.configure(state="disabled")


class ChangelogDialog:

    def __init__(self, root: tk.Misc, radio: str, entries: list[tuple[str, str, str]],
                 selected: str, on_choose: Callable[[str], None]):
        self.entries = entries
        self.on_choose = on_choose
        self.win = win = tk.Toplevel(root)
        win.title("Changelog — " + radio)
        win.transient(root.winfo_toplevel())
        win.minsize(520, 340)
        top = root.winfo_toplevel()
        width = min(860, max(620, top.winfo_width() - 80))
        height = min(560, max(400, top.winfo_height() - 120))
        x, y = layout.centred_over((top.winfo_rootx(), top.winfo_rooty(), top.winfo_width(),
                                    top.winfo_height()), (width, height), gap=60)
        win.geometry("%dx%d+%d+%d" % (width, height, x, y))

        outer = ttk.Frame(win, padding=(14, 12, 14, 12))
        outer.pack(fill="both", expand=True)
        foot = ttk.Frame(outer)
        foot.pack(side="bottom", fill="x", pady=(10, 0))
        ttk.Button(foot, text="Close", command=self.close).pack(side="right")
        self.use_btn = ttk.Button(foot, text="Use this version", command=self._use, default="active")
        self.use_btn.pack(side="right", padx=(0, 8))

        head = ttk.Frame(outer)
        head.pack(fill="x", pady=(0, 8))
        ttk.Label(head, text="Changelog", font=("", 15, "bold")).pack(side="left")
        ttk.Label(head, text=radio, foreground="#666").pack(side="left", anchor="s", padx=(10, 0), pady=(0, 2))

        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 12))
        self.versions = tk.Listbox(left, activestyle="none", exportselection=False, width=34,
                                   borderwidth=0, highlightthickness=1, selectmode="browse",
                                   highlightbackground=_BORDER, highlightcolor=_BORDER)
        self.versions.pack(side="left", fill="y")
        vbar = ttk.Scrollbar(left, orient="vertical", command=self.versions.yview)
        vbar.pack(side="left", fill="y")
        self.versions.configure(yscrollcommand=vbar.set)
        for i, (label, text, _parts) in enumerate(entries):
            self.versions.insert("end", label)
            if not text:
                self.versions.itemconfigure(i, foreground="#999")
        self.versions.bind("<<ListboxSelect>>", lambda _e: self._show(self._current()))

        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        self.title = ttk.Label(right, text="", font=("", 13, "bold"), justify="left")
        self.title.pack(anchor="w", fill="x")
        self.parts = ttk.Label(right, text="", foreground="#666", justify="left")
        self.parts.pack(anchor="w", fill="x", pady=(2, 8))
        right.bind("<Configure>", lambda e: (self.title.configure(wraplength=max(200, e.width - 8)),
                                             self.parts.configure(wraplength=max(200, e.width - 8))), add="+")
        text_font = tkfont.nametofont("TkTextFont")
        frame = ttk.Frame(right)
        frame.pack(fill="both", expand=True)
        self.text = tk.Text(frame, wrap="word", font=text_font, borderwidth=0, highlightthickness=1,
                            highlightbackground=_BORDER, highlightcolor=_BORDER, padx=12, pady=10,
                            cursor="arrow")
        tbar = ttk.Scrollbar(frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=tbar.set)
        tbar.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("para", spacing1=3)
        self.text.tag_configure("gap", spacing1=2)
        self.text.tag_configure("none", foreground="#888")
        self.text_font = text_font

        win.bind("<Escape>", lambda _e: self.close())
        win.bind("<Return>", lambda _e: self._use())
        index = next((i for i, (label, _t, _p) in enumerate(entries) if label == selected), 0)
        self.select(index)
        self.versions.focus_set()
        win.after_idle(self._grab)

    def _grab(self):
        try:
            self.win.grab_set()
        except tk.TclError:
            pass

    def _current(self) -> int:
        picked = self.versions.curselection()
        return picked[0] if picked else 0

    def select(self, index: int) -> None:
        self.versions.selection_clear(0, "end")
        self.versions.selection_set(index)
        self.versions.activate(index)
        self.versions.see(index)
        self._show(index)

    def _show(self, index: int) -> None:
        label, text, parts = self.entries[index]
        self.title.configure(text=label)
        self.parts.configure(text=parts)
        render(self.text, text, self.text_font)
        self.text.yview_moveto(0)

    def _use(self) -> None:
        label = self.entries[self._current()][0]
        self.close()
        self.on_choose(label)

    def close(self) -> None:
        try:
            self.win.grab_release()
            self.win.destroy()
        except tk.TclError:
            pass
