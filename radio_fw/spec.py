"""The four D890 radio/board update targets, and how the desktop app treats each.

This mirrors the AesApp web CPS's firmware-bundle model so the desktop tab offers
exactly what the web CPS's admin firmware pages do — one row per target, compiled
from the same vendor files by the same precompilers (radio_fw/vendor/fwupd_*).

WRITE_ORDER is the single source of the sequence: the tab renders the rows and
runs the wizard in this order and offers no in-place reordering (an operator may
skip any target). It is set to radio firmware first, then icons, SCT3288, NR
board. (Note: this is the reverse of the server's "main-MCU-last" ordering — each
target here is an independent power-cycle + entry combo the operator confirms
one at a time, and the FW bootloader entry is always available for a retry.)
"""
from __future__ import annotations

# These board/firmware updates are D890-only by design (the D578/D878 are
# BT-module-only on the other tab). The four targets below are all D890 packages;
# the tab shows this model and offers no model choice.
SUPPORTED_MODEL = "D890UV"

# kind ids (match the server + the JS engines)
KIND_SCT = "sct"
KIND_NR = "nr"
KIND_ICON = "icon"
KIND_FW = "fw"

# The order the tab shows the rows and walks the wizard: firmware, icons, SCT, NR.
WRITE_ORDER = [KIND_FW, KIND_ICON, KIND_SCT, KIND_NR]

# Per kind: the human label, the file extensions the picker accepts (required
# first), which of them the precompiler cannot do without, and the step image
# shown in the wizard. `multi` is True when a target is built from more than one
# vendor file (the CPS packages are .CDD + .CDI + optional .spi).
KINDS = {
    KIND_SCT: {
        "label": "SCT3288 Baseband",
        "required": ["hex"],
        "optional": [],
        "multi": False,
        "image": "SCT3288.png",
    },
    KIND_NR: {
        "label": "NR Board",
        "required": ["ufw"],
        "optional": [],
        "multi": False,
        "image": "NR.png",
    },
    KIND_ICON: {
        "label": "Icons & Fonts",
        "required": ["cdd", "cdi"],
        "optional": ["spi"],
        "multi": True,
        "image": "ICON.png",
    },
    KIND_FW: {
        "label": "Radio Firmware",
        "required": ["cdd", "cdi"],
        "optional": ["spi"],
        "multi": True,
        "image": "FW.png",
    },
}


def label(kind: str) -> str:
    return KINDS.get(kind, {}).get("label", kind)


def accepts(kind: str) -> list[str]:
    """Extensions the picker offers for a kind, required first."""
    spec = KINDS.get(kind, {})
    return list(spec.get("required", [])) + list(spec.get("optional", []))


def requires(kind: str) -> list[str]:
    return list(KINDS.get(kind, {}).get("required", []))


# ---- Entry instructions (out-of-band) --------------------------------------
# How to put THIS target into its bootloader appears in NO protocol capture and
# cannot be derived — every vendor capture begins with the device already in
# update mode. These are the confirmed D890 key combinations; the wizard shows
# the text + the step image immediately before the target is written, and waits
# for the on-screen confirmation named in each step before connecting.
#
# Button glossary for the D890: PF3 = the top key (the alarm key); PF2 = the
# lower side key; PTT = the large side push-to-talk key; "#" = the # key at the
# bottom right of the keypad. Every combo is entered from a powered-OFF radio.
ENTRY_INSTRUCTIONS = {
    KIND_SCT: (
        "Turn the radio OFF.\n"
        "Hold PF3 (top key) and the # key (bottom-right of the keypad) together, then "
        "power the radio ON — and KEEP HOLDING both for several more seconds, until "
        "the screen shows \"WARNING This is Boot Mode for Sct!!!\". Now connect the "
        "USB cable."
    ),
    KIND_NR: (
        "Turn the radio OFF.\n"
        "Hold PF3 (top key) and PF2 (the lower side key) together, then power the "
        "radio ON while holding both.\n"
        "The screen shows \"UPDATE MODE FOR LinkBoard\". Now connect the USB cable."
    ),
    KIND_ICON: (
        "Turn the radio OFF.\n"
        "Hold the PTT key and PF2 (the lower side key) together, then power the "
        "radio ON while holding both.\n"
        "The screen shows \"UPDATE MODE\". Now connect the USB cable."
    ),
    KIND_FW: (
        "First, in the radio menu, turn OFF both GPS and APRS — leave them off for "
        "the whole update.\n"
        "Then turn the radio OFF.\n"
        "Hold the PTT key and PF3 (top alarm key) together, then power the radio ON "
        "while holding both.\n"
        "The red LED starts blinking. Now connect the USB cable."
    ),
}

# All four combos are confirmed. (Kept for the wizard's optional "unconfirmed"
# marker; empty means every step below is trusted.)
UNCONFIRMED: set[str] = set()
