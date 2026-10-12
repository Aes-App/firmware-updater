from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from radio_fw import ports, spec


def _port(device, description, vid=None):
    return types.SimpleNamespace(device=device, description=description, vid=vid,
                                 product=None, manufacturer=None)


BT = _port("/dev/cu.Bluetooth-Incoming-Port", "n/a")
STM = _port("/dev/cu.usbmodemA", "STM32 Virtual ComPort", 0x0483)
GD = _port("/dev/cu.usbmodemB", "GD32 Virtual Com Port", 0x28E9)
DBG = _port("/dev/cu.debug-console", "n/a")
AT = _port("/dev/cu.usbmodemC", "AT32 Virtual Com Port  ", 0x2E3C)
LISTED = [BT, STM, GD, DBG, AT]


def _order(prefer=()):
    return [p.device for p in ports.ordered(LISTED, prefer)]


def test_the_chip_comes_from_the_vendor_id_or_the_name():
    assert [ports.chip_of(p) for p in LISTED] == [None, "STM32", "GD32", None, "AT32"]
    assert ports.chip_of(_port("COM5", "USB Serial Device (COM5)", 0x2E3C)) == "AT32"
    assert ports.chip_of(_port("COM6", "GD32 Virtual Com Port")) == "GD32"
    assert ports.chip_of(_port("COM7", "USB Serial Device (COM7)", 0x1A86)) is None


def test_each_device_puts_its_own_chips_on_top():
    radios_first = [STM.device, GD.device, AT.device, BT.device, DBG.device]
    assert _order() == radios_first
    assert _order(spec.usb_chips("d890")) == [STM.device, GD.device, AT.device, BT.device, DBG.device]
    assert _order(spec.usb_chips("d878uv2")) == [GD.device, STM.device, AT.device, BT.device, DBG.device]
    for model in ("d578uv2", "d578uv"):
        assert _order(spec.usb_chips(model)) == [GD.device, AT.device, STM.device, BT.device, DBG.device]
    for model in ("d168", "bt01"):
        assert _order(spec.usb_chips(model)) == [AT.device, STM.device, GD.device, BT.device, DBG.device]
    assert set(spec.USB_CHIPS) == set(spec.MODEL_ORDER), "every model names its chips"


def test_the_label_names_the_chip_when_the_description_does_not():
    assert ports.label(AT) == "/dev/cu.usbmodemC  —  AT32 Virtual Com Port"
    assert ports.label(_port("COM5", "USB Serial Device (COM5)", 0x2E3C)) == "COM5  —  USB Serial Device (COM5) [AT32]"
    assert ports.label(BT) == "/dev/cu.Bluetooth-Incoming-Port"
