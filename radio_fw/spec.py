from __future__ import annotations

KIND_FW = "fw"
KIND_ICON = "icon"
KIND_SCT = "sct"
KIND_NR = "nr"
KIND_APRS = "aprs"

CPS_KINDS = {KIND_FW, KIND_ICON, KIND_APRS}

KIND_FILESPEC = {
    KIND_FW:   {"label": "Radio Firmware",   "required": ["cdd", "cdi"], "optional": ["spi"], "multi": True,  "image": "FW.png"},
    KIND_ICON: {"label": "Icons & Fonts",    "required": ["cdd", "cdi"], "optional": ["spi"], "multi": True,  "image": "ICON.png"},
    KIND_SCT:  {"label": "SCT3288 Baseband", "required": ["hex"],        "optional": [],      "multi": False, "image": "SCT3288.png"},
    KIND_NR:   {"label": "NR Board",         "required": ["ufw"],        "optional": [],      "multi": False, "image": "NR.png"},
    KIND_APRS: {"label": "APRS + BT Board",  "required": ["cdd", "cdi"], "optional": ["spi"], "multi": True,  "image": "NR.png"},
}
KINDS = KIND_FILESPEC


def label(kind: str) -> str:
    return KIND_FILESPEC.get(kind, {}).get("label", kind)


def accepts(kind: str) -> list[str]:
    k = KIND_FILESPEC.get(kind, {})
    return list(k.get("required", [])) + list(k.get("optional", []))


def requires(kind: str) -> list[str]:
    return list(KIND_FILESPEC.get(kind, {}).get("required", []))


def is_multi(kind: str) -> bool:
    return bool(KIND_FILESPEC.get(kind, {}).get("multi"))


def image(kind: str) -> str:
    return KIND_FILESPEC.get(kind, {}).get("image", "")


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
_INSTR_NR_D890 = _INSTR_LINKBOARD + (
    "\n\n"
    "Not entering this mode? If the Bluetooth menu has also disappeared and the "
    "NR version reads null, a previous update lost power part-way. On radio "
    "firmware V1.05 or later, hold PF3 and PF1 (the upper side key) instead and "
    "power ON — that forces NR update mode. Then continue as normal."
)
_INSTR_SCT = (
    "Turn the radio OFF.\n"
    "Hold PF3 (top key) and the # key (bottom-right of the keypad) together, then "
    "power the radio ON — and KEEP HOLDING both for several more seconds, until "
    "the screen shows \"WARNING This is Boot Mode for Sct!!!\". Now connect the "
    "USB cable."
)

MODELS = {
    "d890": {
        "label": "D890UV",
        "cps_model": "d890",
        "order": [KIND_FW, KIND_ICON, KIND_SCT, KIND_NR],
        "instructions": {
            KIND_FW: _INSTR_FW, KIND_ICON: _INSTR_ICON,
            KIND_SCT: _INSTR_SCT, KIND_NR: _INSTR_NR_D890,
        },
    },
    "d878uv2": {
        "label": "D878 Series (Gen 1 & 2)",
        "cps_model": "d878uv2",
        "order": [KIND_FW, KIND_ICON, KIND_APRS],
        "instructions": {
            KIND_FW: _INSTR_FW, KIND_ICON: _INSTR_ICON, KIND_APRS: _INSTR_LINKBOARD,
        },
    },
}
DEFAULT_MODEL = "d890"
MODEL_ORDER = ["d890", "d878uv2"]

SERVER_MODELS = {
    "d890": ["d890"],
    "d878uv2": ["d878uv", "d878uv2"],
}
SERVER_MODEL_TAG = {
    "d890": "",
    "d878uv": "Gen 1",
    "d878uv2": "Gen 2",
}
SERVER_MODEL_NAME = {
    "d890": "D890UV",
    "d878uv": "D878UV",
    "d878uv2": "D878UVII",
}


def server_model_name(catalog_model: str) -> str:
    return SERVER_MODEL_NAME.get(catalog_model, catalog_model)


def server_models(model: str) -> list[str]:
    return list(SERVER_MODELS.get(model, [model]))


def server_model_tag(catalog_model: str) -> str:
    return SERVER_MODEL_TAG.get(catalog_model, "")


def model_label(model: str) -> str:
    return MODELS[model]["label"]


def model_order(model: str) -> list[str]:
    return list(MODELS[model]["order"])


def cps_model(model: str) -> str:
    return MODELS[model]["cps_model"]


def entry_instructions(model: str, kind: str) -> str:
    return MODELS[model]["instructions"].get(kind, "")


_MCU_RESET = (
    "Save your codeplug to the PC first if you have not already — this step "
    "initialises the radio.\n\n"
    "1. Power the radio OFF.\n"
    "2. Hold the PTT key and PF1 together, then power the radio ON — keep holding "
    "until it restarts. Do NOT power the radio off while it is restarting.\n"
    "3. When the screen prompts, press the GREEN menu key to confirm the MCU "
    "reboot / initialisation.\n"
    "4. Set the time zone, date and time when the radio asks."
)


def mcu_reset(model: str) -> str:
    return _MCU_RESET


UNCONFIRMED: set[str] = set()
