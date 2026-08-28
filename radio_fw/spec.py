"""The radio/board update targets per model, and how the desktop app treats each.

Mirrors the AesApp web CPS's firmware-bundle model: one row per target, compiled
from the same vendor files by the same precompilers (radio_fw/vendor/fwupd_*).

Two radios are supported here, chosen by a model radio button in the tab:
  D890UV    — Radio Firmware, Icons & Fonts, SCT3288 Baseband, NR Board
  D878UVII  — Radio Firmware, Icons & Fonts, APRS + BT Board (LinkBoard)

The firmware/icon/aprs targets are all the CPS 40-byte-frame protocol (through
fwupd_cps, which is model-parameterised: base address, ident, handshake); SCT3288
(.hex) and the D890 NR board (.ufw) have their own precompilers. Each model's
`order` is the single source of the row/wizard sequence (radio firmware first);
an operator may skip any target but not reorder.
"""
from __future__ import annotations

# ---- kind ids (match the manifests + the wire engines) ----------------------
KIND_FW = "fw"
KIND_ICON = "icon"
KIND_SCT = "sct"
KIND_NR = "nr"
KIND_APRS = "aprs"

# Kinds that route through the CPS precompiler (fwupd_cps, model-parameterised).
# sct/nr have their own precompilers and are not model-parameterised.
CPS_KINDS = {KIND_FW, KIND_ICON, KIND_APRS}

# Per kind: label, the file extensions the picker accepts (required first) and
# which the precompiler cannot do without, whether it takes more than one vendor
# file (`multi`: the CPS packages are .CDD + .CDI + optional .spi), and the step
# photo. A kind's file handling is the same across models.
KIND_FILESPEC = {
    KIND_FW:   {"label": "Radio Firmware",   "required": ["cdd", "cdi"], "optional": ["spi"], "multi": True,  "image": "FW.png"},
    KIND_ICON: {"label": "Icons & Fonts",    "required": ["cdd", "cdi"], "optional": ["spi"], "multi": True,  "image": "ICON.png"},
    KIND_SCT:  {"label": "SCT3288 Baseband", "required": ["hex"],        "optional": [],      "multi": False, "image": "SCT3288.png"},
    KIND_NR:   {"label": "NR Board",         "required": ["ufw"],        "optional": [],      "multi": False, "image": "NR.png"},
    KIND_APRS: {"label": "APRS + BT Board",  "required": ["cdd", "cdi"], "optional": ["spi"], "multi": True,  "image": "NR.png"},
}
# Back-compat alias (older code reads spec.KINDS[kind]["multi"]/["image"]).
KINDS = KIND_FILESPEC


def label(kind: str) -> str:
    return KIND_FILESPEC.get(kind, {}).get("label", kind)


def accepts(kind: str) -> list[str]:
    """Extensions the picker offers for a kind, required first."""
    k = KIND_FILESPEC.get(kind, {})
    return list(k.get("required", [])) + list(k.get("optional", []))


def requires(kind: str) -> list[str]:
    return list(KIND_FILESPEC.get(kind, {}).get("required", []))


def is_multi(kind: str) -> bool:
    return bool(KIND_FILESPEC.get(kind, {}).get("multi"))


def image(kind: str) -> str:
    return KIND_FILESPEC.get(kind, {}).get("image", "")


# ---- Entry instructions (out-of-band) --------------------------------------
# How to put a target into its bootloader appears in NO protocol capture and
# cannot be derived. These are the confirmed vendor key combinations; the wizard
# shows the text + the step photo immediately before the target is written, and
# waits for the named on-screen confirmation before connecting.
#
# Button glossary (D890UV and D878UVII share the layout): PF3 = the top key (the
# alarm key); PF2 = the lower side key; PTT = the large side push-to-talk key;
# "#" = the # key at the bottom-right of the keypad. Every combo is entered from
# a powered-OFF radio. The D878UVII fw/icon/aprs combos are identical to the
# D890 fw/icon/nr combos (same keys, same on-screen text), so they are shared.
_INSTR_FW = (
    "First, in the radio menu, turn OFF both GPS and APRS — leave them off for "
    "the whole update.\n"
    "Then turn the radio OFF.\n"
    "Hold the PTT key and PF3 (top alarm key) together, then power the radio ON "
    "while holding both.\n"
    "The red LED starts blinking. Now connect the USB cable."
)
_INSTR_ICON = (
    "Turn the radio OFF.\n"
    "Hold the PTT key and PF2 (the lower side key) together, then power the "
    "radio ON while holding both.\n"
    "The screen shows \"UPDATE MODE\". Now connect the USB cable."
)
_INSTR_LINKBOARD = (
    "Turn the radio OFF.\n"
    "Hold PF3 (top key) and PF2 (the lower side key) together, then power the "
    "radio ON while holding both.\n"
    "The screen shows \"UPDATE MODE FOR LinkBoard\". Now connect the USB cable."
)
_INSTR_SCT = (
    "Turn the radio OFF.\n"
    "Hold PF3 (top key) and the # key (bottom-right of the keypad) together, then "
    "power the radio ON — and KEEP HOLDING both for several more seconds, until "
    "the screen shows \"WARNING This is Boot Mode for Sct!!!\". Now connect the "
    "USB cable."
)

# ---- Models -----------------------------------------------------------------
# `cps_model` is the fwupd_cps model id for this radio's CPS targets; `order` is
# the row/wizard sequence; `instructions` is the entry text per target.
MODELS = {
    "d890": {
        "label": "D890UV",
        "cps_model": "d890",
        "order": [KIND_FW, KIND_ICON, KIND_SCT, KIND_NR],
        "instructions": {
            KIND_FW: _INSTR_FW, KIND_ICON: _INSTR_ICON,
            KIND_SCT: _INSTR_SCT, KIND_NR: _INSTR_LINKBOARD,
        },
    },
    "d878uv2": {
        "label": "D878UVII",
        "cps_model": "d878uv2",
        "order": [KIND_FW, KIND_ICON, KIND_APRS],
        "instructions": {
            KIND_FW: _INSTR_FW, KIND_ICON: _INSTR_ICON, KIND_APRS: _INSTR_LINKBOARD,
        },
    },
}
DEFAULT_MODEL = "d890"
MODEL_ORDER = ["d890", "d878uv2"]   # display order of the radio buttons


def model_label(model: str) -> str:
    return MODELS[model]["label"]


def model_order(model: str) -> list[str]:
    """The target kinds for a model, in row/wizard order."""
    return list(MODELS[model]["order"])


def cps_model(model: str) -> str:
    """The fwupd_cps model id for a desktop model."""
    return MODELS[model]["cps_model"]


def entry_instructions(model: str, kind: str) -> str:
    return MODELS[model]["instructions"].get(kind, "")


# ---- post-firmware MCU reset ------------------------------------------------
# After the radio firmware is written the MCU must be reset/initialised. The
# procedure is the standard AnyTone one and is the same for both radios; the
# wizard shows it on the finish screen whenever Radio Firmware was flashed.
_MCU_RESET = (
    "1. Power the radio OFF.\n"
    "2. Hold the PTT key and PF1 together, then power the radio ON — keep holding "
    "until it restarts. Do NOT power the radio off while it is restarting.\n"
    "3. When the screen prompts, press the GREEN menu key to confirm the MCU "
    "reboot / initialisation.\n"
    "4. Set the time zone, date and time when the radio asks."
)


def mcu_reset(model: str) -> str:
    """The post-firmware MCU reset steps for a model."""
    return _MCU_RESET


# All combos are confirmed. (Kept for the wizard's optional "unconfirmed"
# marker; empty means every step is trusted.)
UNCONFIRMED: set[str] = set()
