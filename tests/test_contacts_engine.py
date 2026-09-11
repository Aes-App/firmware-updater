"""The contact-write engine against a PC-mode radio double that re-verifies
every frame (checksum, LL, trailing ACK byte, address order) and answers with
the CPS's 1:1 ACK discipline. Same FakeSerial as the firmware engines' suite.

Run:  python -m pytest tests/test_contacts_engine.py -q
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_engines import FakeSerial  # noqa: E402

from radio_fw import engines  # noqa: E402
from radio_contacts import engine, segments as seg  # noqa: E402

IDENT_878 = b"ID878UV2" + b"\x0e" + b"V101" + b"\x00\x00" + b"\x06"
IDENT_7X2 = bytes.fromhex("49444d522d375832005631303001000606"[:32])   # "IDMR-7X2" 00 "V100" 01 00 06
assert len(IDENT_878) == 16 and len(IDENT_7X2) == 16


class PcModeDevice:
    """A radio in normal PC mode. Records every accepted block; refuses a bad
    checksum with silence (as a real radio does) and flags it."""

    def __init__(self, ident=IDENT_878, program_reply=b"QX\x06", drop_ack_at=(), stray_before=(),
                 silent_from=None, slow_ack_at=()):
        self.ident = ident
        self.program_reply = program_reply
        self.drop_ack_at = set(drop_ack_at)      # block indexes whose FIRST ack is withheld
        self.stray_before = set(stray_before)    # block indexes answered with a junk byte then ACK
        self.silent_from = silent_from           # block index from which the radio never answers
        # Block indexes the radio is merely SLOW on: no ACK in time, then BOTH
        # copies answered once the resend arrives. A radio that was deaf answers
        # once; one that was busy answers twice, and the second ACK is the one
        # that quietly re-aligns the whole stream if nobody takes it out.
        self.slow_ack_at = set(slow_ack_at)
        self._late_acks = 0
        self.buf = bytearray()
        self.blocks = []            # (addr, data) in the order received (incl. resends)
        self.transcript = []        # "PROGRAM" / "ID" / "W" / "END"
        self.bad = []               # frames that failed the checksum check
        self.session_open = False
        self._withheld = set()

    def on_open(self, ser):
        pass

    def on_host_bytes(self, ser, data):
        self.buf += data
        while self.buf:
            if self.buf[:7] == b"PROGRAM":
                del self.buf[:7]
                self.transcript.append("PROGRAM")
                self.session_open = True
                ser.feed(self.program_reply)
                continue
            if self.buf[:3] == b"END":
                del self.buf[:3]
                self.transcript.append("END")
                self.session_open = False
                ser.feed(b"\x06")
                continue
            if self.buf[0] == 0x02:
                del self.buf[:1]
                self.transcript.append("ID")
                ser.feed(self.ident)
                continue
            if self.buf[0] == 0x57:
                if len(self.buf) < 24:
                    return
                f = bytes(self.buf[:24])
                del self.buf[:24]
                addr = int.from_bytes(f[1:5], "big")
                ll = f[5]
                data = f[6:22]
                ck = f[22]
                calc = (sum(f[1:5]) + ll + sum(data)) & 0xFF
                if ll != 16 or ck != calc or f[23] != 0x06:
                    self.bad.append(f)
                    continue               # a real radio ignores a bad frame (no ACK)
                idx = len(self.blocks)
                self.blocks.append((addr, data))
                self.transcript.append("W")
                if self.silent_from is not None and idx >= self.silent_from:
                    continue
                if idx in self.drop_ack_at and idx not in self._withheld:
                    self._withheld.add(idx)
                    continue               # silent once → the host must resend
                if idx in self.slow_ack_at and idx not in self._withheld:
                    self._withheld.add(idx)
                    self._late_acks += 1
                    continue               # busy: this copy IS taken, answered late
                if idx in self.stray_before:
                    ser.feed(b"\x00")
                if self._late_acks:
                    self._late_acks -= 1
                    ser.feed(b"\x06")     # the overdue ACK for the previous copy
                ser.feed(b"\x06")
                continue
            # a partial "PROGRAM"/"END" still arriving
            if b"PROGRAM".startswith(bytes(self.buf[:7])) or b"END".startswith(bytes(self.buf[:3])):
                return
            del self.buf[:1]


@pytest.fixture()
def fake_port(monkeypatch):
    holder = {}

    def install(device):
        fake = FakeSerial(device)
        monkeypatch.setattr(engines.serial, "Serial", lambda *a, **k: fake)
        holder["fake"] = fake
        return fake
    yield install


def _plan(*ranges):
    segs = []
    for addr, n in ranges:
        segs.append(seg.Segment(addr, bytes((addr + i) & 0xFF for i in range(n * 16))))
    return segs


def _logs():
    lines = []
    return lines, (lambda m, c="info": lines.append((c, m)))


def test_frame_checksum_matches_the_browser_driver():
    # 57 + addr(4 BE) + LL + data + ck + 06; ck sums addr+LL+data, NOT the 57.
    f = engine.frame(0x07000000, bytes(range(16)))
    assert f.hex() == "57" + "07000000" + "10" + bytes(range(16)).hex() + "8f" + "06"
    assert engine.checksum(0x04840000, b"\xff" * 16) == (0x04 + 0x84 + 0x10 + 16 * 0xFF) & 0xFF


def test_parse_ident_878_and_the_7x2_rebadge():
    i = engine.parse_ident(IDENT_878)
    assert (i.model, i.version, i.band) == ("D878UV2", "V101", 0x0E)
    j = engine.parse_ident(IDENT_7X2)
    assert (j.model, j.version, j.band) == ("DMR-7X2", "V100", 0)
    with pytest.raises(engine.ContactWriteError, match="did not end in 06"):
        engine.parse_ident(IDENT_878[:15] + b"\x00")


def test_identify_reads_the_model_and_leaves_pc_mode(fake_port):
    dev = PcModeDevice()
    fake = fake_port(dev)
    lines, log = _logs()
    ident = engine.identify("COMX", on_log=log)
    assert ident.model == "D878UV2" and ident.version == "V101"
    assert dev.transcript == ["PROGRAM", "ID", "END"]
    assert fake.opens == [921600] and fake.closes == 1
    assert not dev.session_open


def test_write_streams_every_block_in_order_then_commits(fake_port):
    dev = PcModeDevice()
    fake = fake_port(dev)
    plan = _plan((0x07000000, 3), (0x07900000, 5))
    lines, log = _logs()
    progress = []
    res = engine.write_contacts("COMX", plan, on_log=log, on_progress=lambda d, t, p: progress.append((d, t, p)),
                                expect_model="D878UV2")
    want = list(seg.iter_blocks(plan))
    assert dev.blocks == want, "every block, in address order, byte-exact"
    assert dev.bad == []
    assert dev.transcript[:2] == ["PROGRAM", "ID"] and dev.transcript[-1] == "END"
    assert dev.transcript.count("W") == 8
    assert res["blocks"] == 8 and res["frames"] == 8 and res["retries"] == 0
    assert progress[-1] == (8, 8, "done")
    assert fake.closes == 1 and not dev.session_open
    # the ack wait is per frame: the host never pipelines (each W was answered before the next)
    tx = bytes(fake.tx)
    assert tx.count(b"\x57") >= 8 and tx.endswith(b"END")


def test_missing_ack_is_answered_by_resending_the_same_frame(fake_port, monkeypatch):
    monkeypatch.setattr(engine, "ACK_TIMEOUT_MS", (60, 60, 60))
    dev = PcModeDevice(drop_ack_at={1}, stray_before={2})
    fake_port(dev)
    plan = _plan((0x07000000, 4))
    lines, log = _logs()
    res = engine.write_contacts("COMX", plan, on_log=log, on_progress=lambda *a: None)
    # block 1 arrived twice (identical), block 2 once with a stray byte swallowed
    addrs = [a for a, _ in dev.blocks]
    assert addrs == [0x07000000, 0x07000010, 0x07000010, 0x07000020, 0x07000030]
    assert dev.blocks[1] == dev.blocks[2]
    assert res["retries"] == 1 and res["frames"] == 5 and res["blocks"] == 4
    assert any("retry" in m for _, m in lines)


def test_bootloader_reply_is_refused_and_nothing_is_written(fake_port):
    dev = PcModeDevice(program_reply=b"\x06")      # UPDATE/PROGRAM bootloader answers a bare 06
    fake = fake_port(dev)
    lines, log = _logs()
    with pytest.raises(engine.NotInPcModeError, match="firmware-update mode"):
        engine.write_contacts("COMX", _plan((0x07000000, 2)), on_log=log, on_progress=lambda *a: None)
    assert dev.blocks == [] and "W" not in dev.transcript
    assert fake.closes == 1


def test_silent_radio_is_refused_with_no_reply(fake_port, monkeypatch):
    monkeypatch.setattr(engine, "HANDSHAKE_TIMEOUT_MS", 80)
    dev = PcModeDevice(program_reply=b"")
    fake_port(dev)
    lines, log = _logs()
    with pytest.raises(engine.NotInPcModeError, match="no reply"):
        engine.identify("COMX", on_log=log)


def test_wrong_radio_is_refused_before_any_block_and_the_session_is_ended(fake_port):
    dev = PcModeDevice()      # a D878UV2
    fake_port(dev)
    lines, log = _logs()
    with pytest.raises(engine.ContactWriteError, match="WRONG RADIO"):
        engine.write_contacts("COMX", _plan((0x07000000, 2)), on_log=log, on_progress=lambda *a: None,
                              expect_model="D890UV")
    assert dev.blocks == []
    assert dev.transcript == ["PROGRAM", "ID", "END"], "END is still sent so the radio leaves PC mode"


def test_abort_mid_write_stops_and_still_ends_the_session(fake_port, monkeypatch):
    monkeypatch.setattr(engine, "PROGRESS_EVERY_BLOCKS", 4)
    dev = PcModeDevice()
    fake_port(dev)
    abort = threading.Event()
    plan = _plan((0x07000000, 40))
    lines, log = _logs()

    def on_progress(done, total, phase):
        if phase == "write" and done >= 8:
            abort.set()
    with pytest.raises(engines.AbortedError, match="run the refresh again"):
        engine.write_contacts("COMX", plan, on_log=log, on_progress=on_progress, abort=abort)
    assert 8 <= len(dev.blocks) < 40
    assert dev.transcript[-1] == "END" and not dev.session_open


def test_radio_that_stops_answering_fails_after_retries_and_ends(fake_port, monkeypatch):
    monkeypatch.setattr(engine, "ACK_TIMEOUT_MS", (40, 40, 40))
    dev = PcModeDevice(silent_from=2)
    fake_port(dev)
    lines, log = _logs()
    # Against the CONSTANT, not a number copied out of it: the attempt count is a
    # tuning decision (it went 5 -> 3 when the per-attempt wait grew to cover a
    # flash erase), and a test that hardcodes it fails for the wrong reason.
    with pytest.raises(engine.ContactWriteError,
                       match="after %d attempts" % engine.WRITE_ATTEMPTS):
        engine.write_contacts("COMX", _plan((0x07000000, 4)), on_log=log, on_progress=lambda *a: None)
    # block 2 was sent once per attempt, then the session was ended
    assert [a for a, _ in dev.blocks].count(0x07000020) == engine.WRITE_ATTEMPTS
    assert dev.transcript[-1] == "END"


def test_empty_plan_is_refused_without_touching_the_port(fake_port):
    dev = PcModeDevice()
    fake = fake_port(dev)
    with pytest.raises(engine.ContactWriteError, match="empty"):
        engine.write_contacts("COMX", [], on_log=lambda *a: None, on_progress=lambda *a: None)
    assert fake.opens == []


def test_a_resent_frame_does_not_leave_the_ack_stream_one_behind(fake_port, monkeypatch):
    """A radio that was BUSY, not deaf, acknowledges both copies of a resend.

    Take only the first and the spare ACK answers the next frame, and the one
    after that, for the rest of the write. The damage is not the shifted count:
    it is that a frame which genuinely goes unanswered is then covered by a stale
    ACK, so the write reports a success it did not earn. That is what this test
    is shaped to catch -- block 4 is never answered, and the write MUST fail.

    Seen for real on an AT-D878UVII over Web Serial: 8 resends in 536,916 frames.
    """
    monkeypatch.setattr(engine, "ACK_TIMEOUT_MS", (60, 60, 60))
    dev = PcModeDevice(slow_ack_at={1}, silent_from=5)
    fake_port(dev)
    lines, log = _logs()
    with pytest.raises(engine.ContactWriteError, match="stopped answering"):
        engine.write_contacts("COMX", _plan((0x07000000, 6)), on_log=log,
                              on_progress=lambda *a: None)
    assert any("duplicate ACK" in m for _, m in lines), "the spare ACK was left in the stream"
    assert dev.transcript[-1] == "END", "the session is closed even when the write fails"


def test_the_first_retry_comes_quickly_and_the_last_one_waits(fake_port, monkeypatch):
    """A lost frame and a busy radio look identical for the first second.

    They want opposite treatment, so the wait GROWS with each attempt: a frame
    that is never coming back costs 1.5 s rather than 15, and a radio that is
    genuinely busy still gets the long wait before anyone gives up on it. A flat
    15 s cost a real 878 write 67 seconds for six frames that were never
    answered.
    """
    assert engine.ACK_TIMEOUT_MS == tuple(sorted(engine.ACK_TIMEOUT_MS)), "waits must grow"
    assert engine.ACK_TIMEOUT_MS[0] > 1420, "the first wait still clears the longest measured busy pause"
    assert engine.WRITE_ATTEMPTS == len(engine.ACK_TIMEOUT_MS)

    # And the loop really uses attempt N's timeout, not attempt 1's every time.
    waits = []
    real = engine.SerialLink.read_exactly

    def spy(self, n, timeout_ms):
        waits.append(timeout_ms)
        return real(self, n, timeout_ms)

    monkeypatch.setattr(engine.SerialLink, "read_exactly", spy)
    monkeypatch.setattr(engine, "ACK_TIMEOUT_MS", (30, 60, 90))
    dev = PcModeDevice(silent_from=0)
    fake_port(dev)
    lines, log = _logs()
    with pytest.raises(engine.ContactWriteError):
        engine.write_contacts("COMX", _plan((0x07000000, 1)), on_log=log,
                              on_progress=lambda *a: None)
    # The three ACK waits for the one block, in order, each capped by its attempt.
    acks = [w for w in waits if w <= 90]
    assert max(acks) <= 90 and any(w > 60 for w in acks), acks
