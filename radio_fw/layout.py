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


def centred_over(parent, size, gap=90) -> tuple[int, int]:
    px, py, pw, ph = parent
    width, height = size
    return px + (pw - width) // 2, py + max(0, min(gap, (ph - height) // 2))


def place_over(win, parent, gap=90) -> None:
    win.withdraw()
    win.update_idletasks()
    top = parent.winfo_toplevel()
    x, y = centred_over((top.winfo_rootx(), top.winfo_rooty(), top.winfo_width(), top.winfo_height()),
                        (win.winfo_reqwidth(), win.winfo_reqheight()), gap)
    win.geometry("+%d+%d" % (x, y))
    win.deiconify()
