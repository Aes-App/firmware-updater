from __future__ import annotations

import re
import sys
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from tkinter import font as tkfont, ttk

from . import compiler, layout, spec

UNSUPPORTED_FIRMWARE = (b"DMR-7X2",)

NAMES = {"d890": "D890UV"}

URL = "https://cps.aes.app/?utm_source=radio-updater&utm_medium=app&utm_campaign=firmware-backup"

TITLE = "Back up your codeplug first"
HEADING = "Back up this radio’s codeplug first"
INTRO = ("After the firmware update the radio needs an MCU reset, which clears everything "
         "on it: channels, zones, contacts and settings.")
PANEL_TITLE = "The quick way: AesApp WebCPS"
SAVES = "No CSV export and re-import in the factory CPS."
OPEN = "Open AesApp WebCPS ↗"
START = "It’s backed up — start"
RESTORE_NOTE = ("Backed up in AesApp WebCPS? After the reset, write the project back to the "
                "radio from there.")

_PANEL_BG = "#f1f5ff"
_PANEL_BORDER = "#c5d3f5"
_NAVY, _NAVY_HOVER = "#12307a", "#1b45ad"
_HAND = "pointinghand" if sys.platform == "darwin" else "hand2"


@dataclass(frozen=True)
class Coverage:
    versions: tuple[str, ...]
    converts: bool


def coverage(info, model: str) -> Coverage | None:
    radios = info.get("radios") if isinstance(info, dict) else None
    if not isinstance(radios, list) or model not in spec.MODELS:
        return None
    wanted = spec.MODELS[model].get("cps_model", model)
    for radio in radios:
        if not isinstance(radio, dict) or radio.get("fwupdModel") != wanted:
            continue
        releases = radio.get("releases")
        formats, versions = 0, []
        for release in releases if isinstance(releases, list) else []:
            firmware = release.get("firmware") if isinstance(release, dict) else None
            listed = [v.strip() for v in firmware if isinstance(v, str) and v.strip()] \
                if isinstance(firmware, list) else []
            if listed:
                formats += 1
                versions += [v for v in listed if v not in versions]
        if versions:
            return Coverage(tuple(sorted(versions, key=_version_key)), formats > 1)
    return None


def target_version(bundle) -> str | None:
    cps = bundle.get("cpsVersion") if isinstance(bundle, dict) else None
    if not isinstance(cps, str):
        return None
    return cps.strip().split("-")[0].strip() or None


def applies(cov: Coverage | None, target: str | None) -> bool:
    if cov is None or len(cov.versions) < 2:
        return False
    return target is None or target.casefold() in {v.casefold() for v in cov.versions}


def supports_firmware(firmware: compiler.CompileResult) -> bool:
    data = compiler.written_data(firmware)
    return not any(name in data for name in UNSUPPORTED_FIRMWARE)


def radio_name(model: str) -> str:
    return NAMES.get(model) or spec.model_label(model)


def wording(radio: str, cov: Coverage, target: str | None) -> dict:
    versions = _join(cov.versions)
    if cov.converts:
        lead = (f"AesApp WebCPS supports the {radio} on firmware {versions} and converts a "
                f"codeplug between them for free.")
    else:
        lead = (f"AesApp WebCPS supports the {radio} on firmware {versions}, which keep the "
                f"same codeplug.")
    lead += (" If this radio runs one of these now:" if target
             else " If you are updating from one of these versions to another:")
    steps = ["Read the radio into a new project in WebCPS, choosing the firmware it runs now. "
             "Its Device Info screen shows it."]
    if cov.converts:
        steps.append(f"If that is not {target}, choose {target} in the firmware menu under the "
                     f"project’s name. WebCPS converts the codeplug." if target else
                     "Choose the firmware you are installing in the firmware menu under the "
                     "project’s name. WebCPS converts the codeplug.")
    steps += ["Update the firmware here, then reset the MCU.",
              "Write the project back to the radio from WebCPS."]
    other = ("Radio on another version? Read it with the factory CPS and save the codeplug to "
             "your computer." if target else
             "Updating from or to another version? Read the radio with the factory CPS and "
             "save the codeplug to your computer.")
    return {"lead": lead, "steps": steps, "other": other}


def ask_backup(root: tk.Misc, radio: str, cov: Coverage, target: str | None) -> bool:
    dialog = BackupDialog(root, radio, cov, target)
    try:
        root.wait_window(dialog.win)
    except tk.TclError:
        pass
    return dialog.result


class BackupDialog:

    def __init__(self, root: tk.Misc, radio: str, cov: Coverage, target: str | None):
        self.result = False
        text = wording(radio, cov, target)
        top = root.winfo_toplevel()
        self.win = win = tk.Toplevel(root)
        win.title(TITLE)
        win.transient(top)
        win.resizable(False, False)
        wrap = max(420, tkfont.nametofont("TkDefaultFont").measure("x" * 66))

        outer = ttk.Frame(win, padding=(18, 14, 18, 14))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=HEADING, font=("", 15, "bold")).pack(anchor="w")
        ttk.Label(outer, text=INTRO, wraplength=wrap, justify="left").pack(anchor="w", fill="x", pady=(6, 12))

        panel = tk.Frame(outer, background=_PANEL_BG, padx=14, pady=10, bd=0, highlightthickness=1,
                         highlightbackground=_PANEL_BORDER, highlightcolor=_PANEL_BORDER)
        panel.pack(fill="x")

        def plain(parent, words, **options):
            return tk.Label(parent, text=words, background=_PANEL_BG, justify="left", anchor="w",
                            padx=0, pady=0, bd=0, highlightthickness=0, **options)

        plain(panel, PANEL_TITLE, font=("", 12, "bold"), foreground=_NAVY).pack(anchor="w")
        self.lead = plain(panel, text["lead"], wraplength=wrap - 30)
        self.lead.pack(anchor="w", fill="x", pady=(4, 6))
        steps = tk.Frame(panel, background=_PANEL_BG)
        steps.pack(anchor="w", fill="x")
        steps.columnconfigure(1, weight=1)
        self.steps = []
        for number, step in enumerate(text["steps"], 1):
            plain(steps, f"{number}.").grid(row=number, column=0, sticky="ne", padx=(2, 6), pady=2)
            label = plain(steps, step, wraplength=wrap - 60)
            label.grid(row=number, column=1, sticky="w", pady=2)
            self.steps.append(label)
        plain(panel, SAVES, foreground="#444").pack(anchor="w", pady=(8, 10))
        self.open_btn = tk.Label(panel, text=OPEN, background=_NAVY, foreground="white",
                                 font=("", 11, "bold"), padx=12, pady=5, bd=0, highlightthickness=0,
                                 cursor=_HAND)
        self.open_btn.pack(anchor="w")
        self.open_btn.bind("<Button-1>", lambda _e: self.open_webcps())
        self.open_btn.bind("<Enter>", lambda _e: self.open_btn.configure(background=_NAVY_HOVER))
        self.open_btn.bind("<Leave>", lambda _e: self.open_btn.configure(background=_NAVY))

        self.other = ttk.Label(outer, text=text["other"], foreground="#666", wraplength=wrap,
                               justify="left")
        self.other.pack(anchor="w", fill="x", pady=(10, 0))

        foot = ttk.Frame(outer)
        foot.pack(fill="x", pady=(16, 0))
        self.start_btn = ttk.Button(foot, text=START, command=self._start)
        self.start_btn.pack(side="right")
        self.cancel_btn = ttk.Button(foot, text="Cancel", command=self.close)
        self.cancel_btn.pack(side="right", padx=(0, 8))

        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Escape>", lambda _e: self.close())
        layout.place_over(win, top)
        self.cancel_btn.focus_set()
        win.after_idle(self._grab)

    def _grab(self) -> None:
        try:
            self.win.grab_set()
        except tk.TclError:
            pass

    def open_webcps(self) -> None:
        webbrowser.open(URL)

    def _start(self) -> None:
        self.result = True
        self.close()

    def close(self) -> None:
        try:
            self.win.grab_release()
            self.win.destroy()
        except tk.TclError:
            pass


def _version_key(version: str):
    return [(0, int(part), "") if part.isdigit() else (1, 0, part.casefold())
            for part in re.findall(r"\d+|[^\d.]+", version)]


def _join(versions) -> str:
    items = list(versions)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
