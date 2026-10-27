from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

exc = pytest.importorskip("bleak.exc")

from bt_ota.client import bluetooth_hint

Reason = exc.BleakBluetoothNotAvailableReason


def test_a_stopped_bluez_says_how_to_start_it():
    stopped = exc.BleakDBusError("org.freedesktop.systemd1.NoSuchUnit",
                                 ["Unit dbus-org.bluez.service not found."])
    assert "systemctl enable --now bluetooth" in bluetooth_hint(stopped, "linux")
    absent = exc.BleakDBusError("org.freedesktop.DBus.Error.ServiceUnknown",
                                ["The name org.bluez was not provided by any .service files"])
    assert "systemctl enable --now bluetooth" in bluetooth_hint(absent, "linux")


def test_no_adapter_and_a_switched_off_one_are_told_apart():
    none = exc.BleakBluetoothNotAvailableError("No Bluetooth adapters found.", Reason.NO_BLUETOOTH)
    assert "USB Bluetooth adapter" in bluetooth_hint(none, "linux")
    off = exc.BleakBluetoothNotAvailableError("No powered Bluetooth adapters found.", Reason.POWERED_OFF)
    assert "rfkill unblock bluetooth" in bluetooth_hint(off, "linux")


def test_everything_else_is_left_to_its_own_words():
    gatt = exc.BleakDBusError("org.bluez.Error.Failed", ["Operation failed with ATT error: 0x0e"])
    assert bluetooth_hint(gatt, "linux") is None
    assert bluetooth_hint(TimeoutError("no answer"), "linux") is None
    none = exc.BleakBluetoothNotAvailableError("No Bluetooth adapters found.", Reason.NO_BLUETOOTH)
    assert bluetooth_hint(none, "darwin") is None and bluetooth_hint(none, "win32") is None
