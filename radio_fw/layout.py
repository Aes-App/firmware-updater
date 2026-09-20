from __future__ import annotations

import tkinter as tk


def follow_width(label, container, reserve=0, minimum=200) -> None:
    def refit(_event=None):
        try:
            r = reserve() if callable(reserve) else reserve
            width = max(minimum, container.winfo_width() - int(r))
            current = int(float(str(label.cget("wraplength") or 0)))
        except (tk.TclError, ValueError):
            return
        if abs(current - width) > 4:
            label.configure(wraplength=width)
    container.bind("<Configure>", refit, add="+")
