from __future__ import annotations

KIND_FW = "fw"
KIND_ICON = "icon"
KIND_SCT = "sct"
KIND_NR = "nr"
KIND_APRS = "aprs"

CPS_KINDS = {KIND_FW, KIND_ICON, KIND_APRS}

KIND_FILESPEC = {
    KIND_FW:   {"label": "Radio Firmware",   "required": ["cdd", "cdi"], "optional": ["spi"], "multi": True,  "image": "D890_FW.png"},
    KIND_ICON: {"label": "Icons & Fonts",    "required": ["cdd", "cdi"], "optional": ["spi"], "multi": True,  "image": "D890_ICON.png"},
    KIND_SCT:  {"label": "SCT3288 Baseband", "required": ["hex"],        "optional": [],      "multi": False, "image": "D890_SCT.png"},
    KIND_NR:   {"label": "NR Board",         "required": ["ufw"],        "optional": [],      "multi": False, "image": "D890_NR.png"},
    KIND_APRS: {"label": "APRS + BT Board",  "required": ["cdd", "cdi"], "optional": ["spi"], "multi": True,  "image": "D890_NR.png"},
}
KINDS = KIND_FILESPEC


def label(kind: str, model: str | None = None) -> str:
    if model is not None:
        own = MODELS.get(model, {}).get("labels", {}).get(kind)
        if own:
            return own
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
_INSTR_FW_D168 = (
    "First, in the radio menu, turn OFF APRS — leave it off for the whole "
    "update, so the radio cannot transmit while it is connected.\n"
    "Then turn the radio OFF.\n"
    "Hold PF1 and PF2 (the two keys below PTT) together, then power the radio "
    "ON while holding both.\n"
    "The red LED starts blinking. Now connect the USB cable."
)
_INSTR_FW_D578 = (
    "With the radio ON, hold MENU and EXIT together and turn the radio OFF "
    "while still holding them.\n"
    "Keep holding MENU and EXIT after the screen goes dark. Release the power "
    "button and wait until the red LED above it starts blinking, then release "
    "MENU and EXIT.\n"
    "Now connect the USB cable.\n\n"
    "Once in this mode the radio stays in it until a firmware write completes. "
    "If an earlier update lost power and the radio will not start: unplug the "
    "power, hold MENU and EXIT, reconnect the power while holding them, then "
    "release — the LED blinks and the radio is back in update mode."
)
_INSTR_SCT_D168 = (
    "Turn the radio OFF.\n"
    "Hold PTT and the # key together, then power the radio ON while holding "
    "both, until the screen shows \"BOOT MODE FOR SCT\". Now connect the USB "
    "cable."
)
_INSTR_SCT_D578 = (
    "Turn the radio OFF.\n"
    "Press and hold the channel knob (PUSH) and MENU together, then power the "
    "radio ON while holding both, until the screen shows \"WARNING Boot Mode "
    "for Sct!!!\". Now connect the USB cable.\n\n"
    "When the update finishes, unplug the radio's power to restart it, then "
    "reconnect the power and turn it on."
)
_INSTR_LINKBOARD_D578 = (
    "First, in the radio menu, turn Bluetooth OFF.\n"
    "Then turn the radio OFF.\n"
    "Press and hold the channel knob (PUSH) and P6 together, then power the "
    "radio ON while holding both.\n"
    "The screen shows \"UPDATE MODE FOR LinkBoard\". Now connect the USB cable.\n\n"
    "The board comes in two versions, each with its own file: H for the newer "
    "board, M for the older one. If the update does not start, try the other "
    "file."
)
_INSTR_FW_BT01 = (
    "This updates the BT01 Bluetooth microphone itself, through its own USB "
    "port — not the radio.\n"
    "Turn the microphone OFF.\n"
    "Hold PTT and PF3 together, then power the microphone ON while holding "
    "both, until its red LED blinks.\n"
    "Now connect the microphone's USB cable to the computer.\n\n"
    "Once in this mode the microphone stays in it until a firmware write "
    "completes."
)
_INSTR_ICON_BT01 = (
    "This updates the BT01 microphone's icons, through its own USB port.\n"
    "Turn the microphone OFF.\n"
    "Hold PTT and PF2 together, then power the microphone ON while holding "
    "both. Its screen shows \"UPDATE MODE\".\n"
    "Now connect the microphone's USB cable to the computer.\n\n"
    "⚠ Make sure no radio is connected. In this mode the app cannot tell the "
    "microphone from a radio, so a radio in icon mode would receive the "
    "microphone's icons. If that happens, flash the radio's own icons again.\n\n"
    "When the write finishes, turn the microphone off, then on in Bluetooth or "
    "cable mode."
)
_INSTR_ICON_D578 = (
    "Turn the radio OFF.\n"
    "Press and hold the channel knob (DIAL) and P1 together, then power the "
    "radio ON while holding both.\n"
    "The screen shows \"UPDATE MODE\". Now connect the USB cable."
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
    "d878uv": {
        "label": "D878UV",
        "cps_model": "d878uv",
        "order": [KIND_FW, KIND_ICON],
        "instructions": {KIND_FW: _INSTR_FW, KIND_ICON: _INSTR_ICON},
        "images": {KIND_FW: "D878_FW.png", KIND_ICON: "D878_ICON.png"},
        "reset_image": "D878_Reset.png",
    },
    "d878uv2": {
        "label": "D878UVII",
        "cps_model": "d878uv2",
        "order": [KIND_FW, KIND_ICON, KIND_APRS],
        "instructions": {
            KIND_FW: _INSTR_FW, KIND_ICON: _INSTR_ICON, KIND_APRS: _INSTR_LINKBOARD,
        },
        "images": {KIND_FW: "D878_FW.png", KIND_ICON: "D878_ICON.png", KIND_APRS: "D878_APRS.png"},
        "reset_image": "D878_Reset.png",
    },
    "d168": {
        "label": "D168UV",
        "cps_model": "d168",
        "sct_profile": "sct3288",
        "order": [KIND_FW, KIND_ICON, KIND_SCT],
        "instructions": {KIND_FW: _INSTR_FW_D168, KIND_ICON: _INSTR_ICON,
                         KIND_SCT: _INSTR_SCT_D168},
        "images": {KIND_FW: "D168_FW.png", KIND_ICON: "D168_ICON.png", KIND_SCT: "D168_SCT.png"},
        "reset_image": "D168_Reset.png",
    },
    "d578uv2": {
        "label": "D578UV(III) Plus",
        "covers": ("The AT-D578UV PLUS and AT-D578UVIII PLUS run firmware 2.xx (D578UV2); every "
                   "other D578 runs 1.xx. The radio's Device Info screen shows its version."),
        "cps_model": "d578uv2",
        "sct_profile": "sct3258",
        "order": [KIND_FW, KIND_ICON, KIND_SCT, KIND_APRS],
        "instructions": {KIND_FW: _INSTR_FW_D578, KIND_ICON: _INSTR_ICON_D578,
                         KIND_SCT: _INSTR_SCT_D578, KIND_APRS: _INSTR_LINKBOARD_D578},
        "labels": {KIND_SCT: "SCT3258 Baseband"},
        "images": {KIND_FW: "D578_FW.png", KIND_ICON: "D578_ICON.png", KIND_SCT: "D578_SCT.png",
                   KIND_APRS: "D578_APRS.png"},
        "reset_image": "D578_Reset.png",
    },
    "d578uv": {
        "label": "D578UV(III) G/Pro",
        "covers": ("The AT-D578UV, AT-D578UVIII, AT-D578UVG, AT-D578UVIIIG, AT-D578UV PRO and "
                   "AT-D578UVIII PRO run firmware 1.xx (D578UV); the PLUS models run 2.xx. The "
                   "radio's Device Info screen shows its version."),
        "cps_model": "d578uv",
        "sct_profile": "sct3258",
        "order": [KIND_FW, KIND_ICON, KIND_SCT],
        "instructions": {KIND_FW: _INSTR_FW_D578, KIND_ICON: _INSTR_ICON_D578,
                         KIND_SCT: _INSTR_SCT_D578},
        "labels": {KIND_SCT: "SCT3258 Baseband"},
        "images": {KIND_FW: "D578_FW.png", KIND_ICON: "D578_ICON.png", KIND_SCT: "D578_SCT.png"},
        "reset_image": "D578_Reset.png",
        "untested": True,
    },
    "bt01": {
        "label": "BT01 Mic",
        "cps_model": "bt01",
        "order": [KIND_FW, KIND_ICON],
        "instructions": {KIND_FW: _INSTR_FW_BT01, KIND_ICON: _INSTR_ICON_BT01},
        "labels": {KIND_FW: "Microphone Firmware", KIND_ICON: "Microphone Icons"},
        "images": {KIND_FW: "BT01_FW.png", KIND_ICON: "BT01_ICON.png"},
        "reset_image": "",
        "has_codeplug": False,
    },
}
DEFAULT_MODEL = "d890"
MODEL_ORDER = ["d890", "d878", "d168", "d578", "bt01"]

FAMILIES = {
    "d878": {
        "label": "D878 Series",
        "members": ("d878uv", "d878uv2"),
        "chips": ("GD32",),
        "variants": {"d878uv": "D878UV (1st Gen)", "d878uv2": "D878UVII (2nd Gen)"},
        "chip_names": {"GD32": "GD32 MCU"},
    },
    "d578": {
        "label": "D578 Series",
        "members": ("d578uv", "d578uv2"),
        "chips": ("AT32", "GD32"),
        "variants": {"d578uv": "D578UV (1st Gen)", "d578uv2": "D578UV2 (2nd Gen)"},
        "chip_names": {"AT32": "AT32 MCU (New)", "GD32": "GD32 MCU (Old)"},
    },
}
PC_IDENT_MODEL = {
    "D890UV": "d890",
    "D878UV": "d878uv",
    "D878UV2": "d878uv2",
    "D168UV": "d168",
    "D578UV2": "d578uv2",
    "D578UV": "d578uv",
}
FW_BUILD_IDENT = {
    ("d578uv2", "AT32"): "I578UV2A",
    ("d578uv2", "GD32"): "ID578UV2",
    ("d578uv", "AT32"): "ID578UVA",
    ("d578uv", "GD32"): "ID578UV",
}


def is_family(choice: str) -> bool:
    return choice in FAMILIES


def choice_label(choice: str) -> str:
    return FAMILIES[choice]["label"] if choice in FAMILIES else model_label(choice)


def family_members(choice: str) -> tuple[str, ...]:
    return FAMILIES[choice]["members"] if choice in FAMILIES else (choice,)


def family_chips(choice: str) -> tuple[str, ...]:
    return FAMILIES[choice]["chips"] if choice in FAMILIES else ()


def variant_label(choice: str, model: str) -> str:
    return FAMILIES[choice]["variants"].get(model, model_label(model)) if choice in FAMILIES else model_label(model)


def chip_label(choice: str, chip: str) -> str:
    return FAMILIES[choice]["chip_names"].get(chip, chip + " MCU") if choice in FAMILIES else chip + " MCU"


def family_of(model: str) -> str:
    return next((f for f, v in FAMILIES.items() if model in v["members"]), model)


def identified_model(choice: str, pc_ident_model: str) -> str | None:
    model = PC_IDENT_MODEL.get(pc_ident_model)
    return model if model in family_members(choice) else None

SERVER_MODELS = {
    "d890": ["d890"],
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
    "d168": "D168UV",
    "d578uv2": "D578UV(III) Plus",
    "d578uv": "D578UV(III) G/Pro",
    "bt01": "BT01",
}


def server_model_name(catalog_model: str) -> str:
    return SERVER_MODEL_NAME.get(catalog_model, catalog_model)


def server_models(model: str) -> list[str]:
    return list(SERVER_MODELS.get(model, [model]))


def server_model_tag(catalog_model: str) -> str:
    return SERVER_MODEL_TAG.get(catalog_model, "")


def model_label(model: str) -> str:
    return MODELS[model]["label"]


def covers(model: str) -> str:
    return MODELS[model].get("covers", "")


def model_order(model: str) -> list[str]:
    return list(MODELS[model]["order"])


def cps_model(model: str) -> str:
    return MODELS[model]["cps_model"]


USB_CHIPS = {
    "d890": ("STM32",),
    "d878uv": ("GD32",),
    "d878uv2": ("GD32",),
    "d168": ("AT32",),
    "d578uv2": ("GD32", "AT32"),
    "d578uv": ("GD32", "AT32"),
    "bt01": ("AT32",),
}


def usb_chips(model: str) -> tuple[str, ...]:
    return USB_CHIPS.get(model, ())


FW_IDENT_CHIP = {
    "I578UV2A": "AT32",
    "ID578UV2": "GD32",
    "ID578UVA": "AT32",
    "ID578UV": "GD32",
}


def fw_chip_of_ident(ident: str | None) -> str | None:
    return FW_IDENT_CHIP.get(ident or "")


def fw_model_mismatch(choice: str, model: str, manifest: dict | None) -> str:
    built_for = (manifest or {}).get("model")
    if choice not in FAMILIES or not built_for or built_for == cps_model(model):
        return ""
    other = next((m for m in family_members(choice) if cps_model(m) == built_for), None)
    return ("This is for the " + (variant_label(choice, other) if other else built_for) + ", but the radio "
            "identified here is the " + variant_label(choice, model) + ". Choose its own version.")


def fw_build_mismatch(model: str, chip: str | None, manifest: dict | None) -> str:
    have = fw_chip(manifest)
    if not chip or not have or have == chip:
        return ""
    return ("This is the " + have[:2] + " build of the radio firmware, but the " + model_label(model)
            + " identified here has " + _a(chip) + " MCU and needs the " + chip[:2] + " build: a version "
            "marked " + chip[:2] + " on the AesApp server, or the .spi from the vendor's " + chip[:2]
            + " folder.")


def fw_chip(manifest: dict | None) -> str | None:
    return FW_IDENT_CHIP.get((manifest or {}).get("ident_reply_prefix_ascii") or "")


def _a(word: str) -> str:
    return ("an " if word[:1] in "AEIOU" else "a ") + word


def fw_port_mismatch(model: str, manifest: dict | None, port_chip: str | None, port: str) -> str:
    want = fw_chip(manifest)
    if not want or port_chip == want or port_chip not in usb_chips(model):
        return ""
    build, need = want[:2], port_chip[:2]
    return ("The selected Radio Firmware is the " + build + " build, for radios with " + _a(want) + " MCU. "
            + port + " is " + _a(port_chip) + " USB port, so the radio on it has " + _a(port_chip)
            + " MCU and needs the " + need + " build. It will NOT be written.\n\n"
            "Choose the " + need + " build: a version marked " + need + " in the AesApp server list, or "
            "the .spi from the vendor's " + need + " folder. If " + port + " is not the radio, pick the "
            "radio's port.")


def sct_profile(model: str) -> str:
    return MODELS[model].get("sct_profile", "sct3288")


def entry_instructions(model: str, kind: str) -> str:
    return MODELS[model]["instructions"].get(kind, "")


def step_image(model: str, kind: str) -> str:
    return MODELS[model].get("images", {}).get(kind, image(kind))


def reset_image(model: str) -> str:
    return MODELS[model].get("reset_image", "D890_Reset.png")


def step_photos() -> set[str]:
    photos = {step_image(m, k) for m in MODELS for k in model_order(m)}
    photos |= {reset_image(m) for m in MODELS if mcu_reset(m)}
    photos.discard("")
    return photos


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


_MCU_RESET_D578 = (
    "Save your codeplug to the PC first if you have not already — the reset "
    "erases it.\n\n"
    "1. Power the radio OFF.\n"
    "2. Hold P2 and press in the channel knob (the one above the microphone "
    "connector), then power the radio ON while holding both. It starts in "
    "Reset Mode.\n"
    "3. Press MENU to confirm the reset. It takes a minute or more.\n"
    "4. Set the GMT offset, date and time: push the channel knob to move between "
    "fields and turn it to change them, then press MENU to confirm. The radio "
    "restarts.\n"
    "5. Write your saved codeplug back to the radio with the CPS."
)


def mcu_reset(model: str) -> str:
    if not has_codeplug(model):
        return ""
    return _MCU_RESET_D578 if model in ("d578uv2", "d578uv") else _MCU_RESET


def has_codeplug(model: str) -> bool:
    return MODELS[model].get("has_codeplug", True)


UNCONFIRMED: set[tuple[str, str]] = {("d578uv2", KIND_ICON), ("d578uv", KIND_ICON)}


def is_unconfirmed(model: str, kind: str) -> bool:
    return (model, kind) in UNCONFIRMED


def is_untested(model: str) -> bool:
    return bool(MODELS[model].get("untested"))


def untested_warning(model: str) -> str:
    return ("The " + MODELS[model]["label"] + " updates have not been tested on a real radio "
            "yet. They are built from the vendor's files with the same protocol and checks as "
            "the radios that have been tested, but nothing has confirmed them on this model.")
