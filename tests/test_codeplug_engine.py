from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_engines import FakeSerial
from test_contacts_engine import IDENT_878, PcModeDevice

from radio_fw import engines
from radio_contacts import engine as ce, segments as seg
from radio_codeplug import engine as cpe


class ReadablePcModeDevice(PcModeDevice):

    def __init__(self, *a, corrupt=None, deaf_reads=False, **kw):
        super().__init__(*a, **kw)
        self.memory: dict = {}
        self.corrupt = dict(corrupt or {})
        self.deaf_reads = deaf_reads
        self.reads = 0

    def on_host_bytes(self, ser, data):
        self.buf += data
        while self.buf and self.buf[0] == 0x52:
            if len(self.buf) < 6:
                return
            f = bytes(self.buf[:6])
            del self.buf[:6]
            addr = int.from_bytes(f[1:5], "big")
            ll = f[5]
            self.reads += 1
            self.transcript.append("R")
            if self.deaf_reads:
                continue
            held = self.corrupt.get(addr, self.memory.get(addr, b"\xff" * ll))[:ll]
            ck = (sum(f[1:5]) + ll + sum(held)) & 0xFF
            ser.feed(b"\x57" + f[1:5] + bytes([ll]) + held + bytes([ck, 0x06]))
        rest, self.buf = bytes(self.buf), bytearray()
        super().on_host_bytes(ser, rest)

    def _remember(self):
        for addr, data in self.blocks:
            self.memory[addr] = bytes(data)


def _install(monkeypatch, device):
    fake = FakeSerial(device)
    monkeypatch.setattr(engines.serial, "Serial", lambda *a, **k: fake)
    return fake


def _plan(*ranges):
    return [seg.Segment(addr, bytes((addr + i) & 0xFF for i in range(n * 16)))
            for addr, n in ranges]


def _sink():
    logs, progress = [], []
    return (logs, progress,
            lambda m, c="info": logs.append((c, m)),
            lambda d, t, p: progress.append((d, t, p)))


class _EchoDevice(ReadablePcModeDevice):

    def on_host_bytes(self, ser, data):
        self._remember()
        super().on_host_bytes(ser, data)


@pytest.fixture(autouse=True)
def _no_reboot_wait(monkeypatch):
    monkeypatch.setattr(cpe, "REBOOT_WAIT_S", 0)
    monkeypatch.setattr(cpe, "_port_names", lambda: {"COM_TEST"})


def test_a_clean_write_commits_then_verifies(monkeypatch):
    dev = _EchoDevice(ident=IDENT_878)
    _install(monkeypatch, dev)
    logs, progress, on_log, on_progress = _sink()
    plan = _plan((0x02500000, 3), (0x00800000, 1))

    res = cpe.write_codeplug("COM_TEST", plan, [], on_log=on_log, on_progress=on_progress,
                             ident_tokens=["D878UV2"])

    assert res.outcome == "success"
    assert res.blocks_written == 4 and res.mismatches == 0 and res.checked == 4
    assert dev.transcript.count("END") == 2, "one END commits the codeplug, one closes the verify"
    first_end = dev.transcript.index("END")
    assert "R" not in dev.transcript[:first_end], "nothing is read back before the commit"
    assert all(k != "W" for k in dev.transcript[first_end:]), "nothing is written after the commit"
    assert not dev.bad


def test_contacts_are_a_separate_commit_after_the_codeplug(monkeypatch):
    dev = _EchoDevice(ident=IDENT_878)
    _install(monkeypatch, dev)
    _, _, on_log, on_progress = _sink()
    code = _plan((0x02500000, 2))
    contacts = _plan((0x04000000, 2), (0x05500000, 1))

    res = cpe.write_codeplug("COM_TEST", code, contacts, on_log=on_log, on_progress=on_progress,
                             verify=False)

    assert res.outcome == "success"
    assert res.blocks_written == 2 and res.contact_blocks == 3
    ends = [i for i, k in enumerate(dev.transcript) if k == "END"]
    writes = [i for i, k in enumerate(dev.transcript) if k == "W"]
    assert len(ends) == 2, "the codeplug and the contact list are committed separately"
    assert writes[1] < ends[0] < writes[2], "the codeplug commits before any contact page is sent"
    addrs = [a for a, _ in dev.blocks]
    assert addrs == [0x02500000, 0x02500010, 0x04000000, 0x04000010, 0x05500000]


def test_a_mismatch_is_verify_failed_not_a_failed_write(monkeypatch):
    dev = _EchoDevice(ident=IDENT_878, corrupt={0x02500010: b"\x00" * 16})
    _install(monkeypatch, dev)
    _, _, on_log, on_progress = _sink()

    res = cpe.write_codeplug("COM_TEST", _plan((0x02500000, 2)), [],
                             on_log=on_log, on_progress=on_progress)

    assert res.outcome == "verify_failed", "the bytes ARE committed — this is not a failed write"
    assert res.mismatches == 1 and res.checked == 2
    assert res.first_mismatches[0]["addr"] == "02500010"
    assert "Write it again" in res.message


def test_a_radio_that_will_not_answer_reads_is_unverifiable_not_broken(monkeypatch):
    dev = _EchoDevice(ident=IDENT_878, deaf_reads=True)
    _install(monkeypatch, dev)
    _, _, on_log, on_progress = _sink()

    res = cpe.write_codeplug("COM_TEST", _plan((0x02500000, 1)), [],
                             on_log=on_log, on_progress=on_progress)

    assert res.outcome == "verify_unavailable"
    assert res.blocks_written == 1
    assert "committed" in res.message


def test_the_wrong_radio_is_refused_before_anything_is_written(monkeypatch):
    dev = _EchoDevice(ident=b"ID890UV" + b"\x00" + b"\x0e" + b"V105" + b"\x00\x00" + b"\x06")
    _install(monkeypatch, dev)
    _, _, on_log, on_progress = _sink()

    res = cpe.write_codeplug("COM_TEST", _plan((0x02500000, 1)), [],
                             on_log=on_log, on_progress=on_progress,
                             ident_tokens=["D878UV2"])

    assert res.outcome == "write_failed"
    assert dev.blocks == [], "not one block reached the wrong radio"
    assert "WRONG RADIO" in res.message


def test_an_empty_plan_is_refused_rather_than_committing_nothing(monkeypatch):
    dev = _EchoDevice(ident=IDENT_878)
    _install(monkeypatch, dev)
    _, _, on_log, on_progress = _sink()
    with pytest.raises(cpe.CodeplugWriteError, match="empty"):
        cpe.write_codeplug("COM_TEST", [], [], on_log=on_log, on_progress=on_progress)
    assert dev.transcript == []


def test_the_heartbeat_is_rate_limited_so_the_network_cannot_pace_the_wire(monkeypatch):
    beats = []
    beat = cpe._Beat(lambda phase, done, total: beats.append((phase, done)))
    beat.maybe("codeplug", 1, 100)
    beat.maybe("codeplug", 2, 100)
    assert beats == [("codeplug", 1)]
    beat.force("commit", 100, 100)
    assert beats[-1] == ("commit", 100)


def test_the_read_reply_opcode_is_the_radios_not_the_requests():
    assert cpe.READ_REPLY_OPCODE == 0x57


def test_the_model_gate_is_exact_and_covers_rebadges():
    assert cpe._model_matches(["D878UV2"], "D878UV2")
    assert cpe._model_matches(["D890UV", "DMR-7X2"], "DMR-7X2"), "a rebadge is the same radio"
    assert cpe._model_matches(["D890UV"], "d890uv"), "case is not identity"
    assert cpe._model_matches([], "anything"), "no token list means no expectation, not no radios"
    assert not cpe._model_matches(["D878UV2"], "D890UV")
    assert not cpe._model_matches(["D578UV2"], "D578UV")
    assert not cpe._model_matches(["D878UV2"], "D878UV")


def test_a_garbled_read_reply_is_retried_not_called_a_mismatch(monkeypatch):

    class GarbleFirstRead(_EchoDevice):

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.garbled = 0

        def on_host_bytes(self, ser, data):
            self._remember()
            self.buf += data
            while self.buf and self.buf[0] == 0x52:
                if len(self.buf) < 6:
                    return
                f = bytes(self.buf[:6])
                del self.buf[:6]
                addr = int.from_bytes(f[1:5], "big")
                ll = f[5]
                self.reads += 1
                self.transcript.append("R")
                held = self.memory.get(addr, b"\xff" * ll)[:ll]
                ck = (sum(f[1:5]) + ll + sum(held)) & 0xFF
                if self.garbled == 0:
                    self.garbled = 1
                    ck ^= 0xFF
                ser.feed(b"\x57" + f[1:5] + bytes([ll]) + held + bytes([ck, 0x06]))
            rest, self.buf = bytes(self.buf), bytearray()
            PcModeDevice.on_host_bytes(self, ser, rest)

    dev = GarbleFirstRead(ident=IDENT_878)
    _install(monkeypatch, dev)
    _, _, on_log, on_progress = _sink()
    res = cpe.write_codeplug("COM_TEST", _plan((0x02500000, 2)), [],
                             on_log=on_log, on_progress=on_progress)
    assert res.outcome == "success", "a garbled reply is a retry, not a mismatch"
    assert res.mismatches == 0 and res.checked == 2
    assert dev.reads == 3, "the garbled read was asked again"
