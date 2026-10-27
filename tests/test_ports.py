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
    assert set(spec.USB_CHIPS) == set(spec.MODELS), "every model names its chips"


def test_the_label_names_the_chip_when_the_description_does_not():
    assert ports.label(AT) == "/dev/cu.usbmodemC  —  AT32 Virtual Com Port"
    assert ports.label(_port("COM5", "USB Serial Device (COM5)", 0x2E3C)) == "COM5  —  USB Serial Device (COM5) [AT32]"
    assert ports.label(BT) == "/dev/cu.Bluetooth-Incoming-Port"


def test_a_refused_port_on_linux_says_how_to_get_access():
    import errno
    import serial
    denied = serial.SerialException(errno.EACCES, "could not open port /dev/ttyACM0: Permission denied")
    hint = ports.open_hint(denied, "linux")
    assert "60-aesapp-radio.rules" in hint and "dialout" in hint
    assert ports.open_hint(denied, "darwin") is None and ports.open_hint(denied, "win32") is None
    busy = serial.SerialException(errno.EBUSY, "could not open port /dev/ttyACM0: busy")
    assert ports.open_hint(busy, "linux") is None


def test_the_udev_rule_covers_exactly_the_radio_chips():
    import re
    path = os.path.join(os.path.dirname(__file__), "..", "60-aesapp-radio.rules")
    rules = [ln for ln in open(path, encoding="utf-8").read().splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    vids = {int(m, 16) for ln in rules for m in re.findall(r'ATTRS\{idVendor\}=="([0-9a-f]{4})"', ln)}
    assert vids == set(ports.CHIP_VIDS)
    for ln in rules:
        assert 'ENV{ID_MM_DEVICE_IGNORE}="1"' in ln and 'TAG+="uaccess"' in ln


def test_the_serial_link_raises_the_hint_with_the_error(monkeypatch):
    import errno
    import pytest
    import serial
    from radio_fw import engines

    def refuse(self):
        raise serial.SerialException(errno.EACCES, "could not open port /dev/ttyACM0: Permission denied")
    monkeypatch.setattr(serial.Serial, "open", refuse)
    monkeypatch.setattr(ports.sys, "platform", "linux")
    with pytest.raises(serial.SerialException, match="dialout") as raised:
        engines.SerialLink("/dev/ttyACM0").open(9600, dtr=True, rts=True)
    assert raised.value.errno == errno.EACCES


def test_identify_does_not_send_a_refused_user_to_check_the_radio(monkeypatch):
    import errno
    import pytest
    import serial
    pytest.importorskip("tkinter")
    from radio_fw import gui_tab
    monkeypatch.setattr(ports.sys, "platform", "linux")
    denied = serial.SerialException(errno.EACCES, "could not open port /dev/ttyACM0: Permission denied")
    said = gui_tab._identify_failure(denied)
    assert said.startswith("Could not identify the radio: ") and "Check it is on" not in said
    silent = gui_tab._identify_failure(TimeoutError("no answer from the radio"))
    assert "Check it is on normally" in silent and "Choose manually" in silent
