from __future__ import annotations

from typing import Callable, Optional

from bleak import BleakScanner

from .ota import AnytoneBtOta, DEFAULT_NAME_PREFIXES, UUID_SERVICE as JIELI_SERVICE
from .wiced import WicedOta, UUID_SERVICE as WICED_SERVICE


def firmware_kind(path: str) -> str:
    p = path.lower()
    if p.endswith(".bin"):
        return "wiced"
    if p.endswith(".ufw"):
        return "jieli"
    return "unknown"


MODELS = {
    "jieli": "Anytone D890 (JieLi BT)",
    "wiced": "Anytone D578/D878 Series (Cypress WICED BT)",
}


def make_client(firmware_path: str, *, kind: Optional[str] = None, use_auth: bool = True,
                auth_lib: Optional[str] = None,
                log_cb: Callable[[str], None] = lambda _m: None):
    if kind is None:
        kind = firmware_kind(firmware_path)
    if kind == "wiced":
        return WicedOta(log_cb=log_cb), kind
    if kind == "jieli":
        return AnytoneBtOta(auth_lib=auth_lib, use_auth=use_auth, log_cb=log_cb), kind
    raise ValueError(f"unknown firmware type for {firmware_path!r} (expected .ufw or .bin)")


async def scan_devices(timeout: float = 8.0, name_filter: Optional[str] = None):
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out = []
    for dev, adv in found.values():
        local_name = adv.local_name or ""
        name = local_name or dev.name or ""
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        candidate = (
            JIELI_SERVICE in uuids or WICED_SERVICE in uuids
            or any(name.upper().startswith(p.upper()) for p in DEFAULT_NAME_PREFIXES)
        )
        if name_filter:
            if not (name_filter.lower() in name.lower()
                    or name_filter.lower() == dev.address.lower()):
                continue
        elif not (name or candidate):
            continue
        out.append((dev, adv.rssi, local_name))
    out.sort(key=lambda t: t[1], reverse=True)
    return out
