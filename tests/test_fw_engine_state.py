from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_engines import (
    Cap, CpsDevice, FakeSerial, FW_ARTIFACT, FW_MANIFEST, IDENT_D878, IDENT_D890,
)
from radio_fw import engines


def _install(monkeypatch, device):
    fake = FakeSerial(device)
    monkeypatch.setattr(engines.serial, "Serial", lambda *a, **k: fake)
    return fake


class _DoubleAck(CpsDevice):

    def __init__(self, *a, at, **kw):
        super().__init__(*a, **kw)
        self.at = at

    def on_host_bytes(self, ser, data):
        before = len(self.frames)
        super().on_host_bytes(ser, data)
        if before <= self.at < len(self.frames):
            ser.feed(b"\x06")


def test_a_stray_byte_behind_an_ack_stops_the_stream_before_the_next_frame(monkeypatch):
    cap = Cap()
    dev = _DoubleAck("fw", ident_hex=IDENT_D890, at=0)
    _install(monkeypatch, dev)
    with pytest.raises(engines.FirmwareUpdateError, match="out of step") as e:
        engines.run("fw", "COM_FAKE", FW_ARTIFACT, FW_MANIFEST,
                    on_log=cap.on_log, on_progress=cap.on_progress)
    assert len(dev.frames) == 1, "frame 1 must not go out on the strength of a stale byte"
    assert "re-flashed from frame 0" in str(e.value)
    assert e.value.touched is True, "a data frame did reach the bootloader"


def test_a_handshake_failure_says_the_radio_is_untouched(monkeypatch):
    cap = Cap()
    dev = CpsDevice("fw", ident_hex=IDENT_D878)
    _install(monkeypatch, dev)
    with pytest.raises(engines.FirmwareUpdateError, match="WRONG RADIO") as e:
        engines.run("fw", "COM_FAKE", FW_ARTIFACT, FW_MANIFEST,
                    on_log=cap.on_log, on_progress=cap.on_progress)
    assert e.value.touched is False
    assert dev.frames == []


def test_a_silent_radio_at_the_handshake_is_untouched_too(monkeypatch):
    cap = Cap()
    dev = CpsDevice("fw", ident_hex=IDENT_D890, ignore_handshakes=99)
    _install(monkeypatch, dev)
    monkeypatch.setattr(engines, "CPS_HANDSHAKE_TIMEOUT_MS", 30)
    monkeypatch.setattr(engines, "CPS_HANDSHAKE_RETRY_MS", 30)
    with pytest.raises(engines.ReplyTimeoutError) as e:
        engines.run("fw", "COM_FAKE", FW_ARTIFACT, FW_MANIFEST,
                    on_log=cap.on_log, on_progress=cap.on_progress)
    assert e.value.touched is False


def test_a_lost_ack_mid_stream_is_a_touched_radio(monkeypatch):
    cap = Cap()
    dev = CpsDevice("fw", ident_hex=IDENT_D890, drop_ack_at=1)
    _install(monkeypatch, dev)
    monkeypatch.setattr(engines, "CPS_ACK_TIMEOUT_MS", 30)
    with pytest.raises(engines.FirmwareUpdateError, match="no ACK for frame 1") as e:
        engines.run("fw", "COM_FAKE", FW_ARTIFACT, FW_MANIFEST,
                    on_log=cap.on_log, on_progress=cap.on_progress)
    assert e.value.touched is True


def test_a_usb_drop_is_its_own_error_type(monkeypatch):
    cap = Cap()
    dev = CpsDevice("fw", ident_hex=IDENT_D890)
    fake = _install(monkeypatch, dev)
    orig = dev.on_host_bytes

    def on_host_bytes(ser, data):
        orig(ser, data)
        if len(dev.frames) == 1:
            fake.raise_on_read = OSError("device reports readiness to read but returned no data")

    dev.on_host_bytes = on_host_bytes
    with pytest.raises(engines.LinkDroppedError, match="dropped off the USB bus"):
        engines.run("fw", "COM_FAKE", FW_ARTIFACT, FW_MANIFEST,
                    on_log=cap.on_log, on_progress=cap.on_progress)
