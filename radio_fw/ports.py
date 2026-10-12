from __future__ import annotations

CHIP_VIDS = {0x0483: "STM32", 0x28E9: "GD32", 0x2E3C: "AT32"}
CHIPS = ("STM32", "GD32", "AT32")


def chip_of(port) -> str | None:
    chip = CHIP_VIDS.get(getattr(port, "vid", None))
    if chip:
        return chip
    text = " ".join(str(getattr(port, a, None) or "")
                    for a in ("description", "product", "manufacturer")).upper()
    return next((c for c in CHIPS if c in text), None)


def ordered(ports, prefer=()) -> list:
    def rank(port):
        chip = chip_of(port)
        if chip is None:
            return 2
        return 0 if chip in prefer else 1
    return sorted(ports, key=rank)


def label(port) -> str:
    desc = (getattr(port, "description", None) or "").strip()
    if desc == "n/a":
        desc = ""
    chip = chip_of(port)
    if chip and chip not in desc.upper():
        desc = (desc + " " if desc else "") + "[" + chip + "]"
    return port.device + ("  —  " + desc if desc else "")
