"""Layout helpers shared by the app's Tk tabs.

Both the Radio and Boards tab and the Digital Contact Refresh tab carry long
explanatory labels, and both were laid out for one window size. This is the
piece they share for coping with others.
"""
from __future__ import annotations

import tkinter as tk


def follow_width(label, container, reserve=0, minimum=200) -> None:
    """Wrap `label` at the width `container` actually has.

    Long labels in these tabs used to wrap at a fixed pixel width chosen for one
    window size, so on a wider window the text broke at ~60% of the width and
    spent the difference as extra LINES -- height the buttons below then did not
    have. `reserve` (a number, or a callable for a neighbour whose width
    changes, like a photo) is taken off the container's width first.

    Bound with add="+" so it composes with any other <Configure> handler on the
    same container; a change under a few pixels is left alone so a relayout that
    only moves the height does not rebind the text.
    """
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
