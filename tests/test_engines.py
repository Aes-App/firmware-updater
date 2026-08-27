"""Python port of the AesApp Web Serial flasher's JS test suite.

A fake serial port plus device doubles that DRIVE the protocol and re-verify the
host's bytes (never trust the engine): the CPS device recomputes the bootloader
checksum on every frame, the NR device re-checks each read slice against the
.ufw, and the SCT device answers per-frame ACKs by the manifest plan. The vectors
are the same capture literals the JS suite pins.

Run:  python -m pytest tests/test_engines.py -q
      (or plain `python tests/test_engines.py` for a no-pytest smoke run)
"""
from __future__ import annotations

import os
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radio_fw import engines
from radio_fw.vendor import fwupd_nr


# ── fake serial port ─────────────────────────────────────────────────────────
class FakeSerial:
    """A serial.Serial double. Records TX (the oracle), lets a device double
    inject RX, and drives device-pull protocols via on_poll()."""

    def __init__(self, device=None):
        self._baud = 0
        self.is_open = False
        self.device = device
        self._rx = bytearray()
        self.tx = bytearray()
        self.opens = []
        self.baud_changes = []
        self.signals = []
        self.closes = 0
        self.timeout = 0
        self.write_timeout = 0
        self.port = None
        self.bytesize = self.parity = self.stopbits = None
        self.rtscts = False
        self.dsrdtr = False
        self.dtr = None
        self.rts = None
        self.raise_on_read = None   # set to an exception to simulate a USB drop
        if device is not None:
            device.port = self

    @property
    def baudrate(self):
        return self._baud

    @baudrate.setter
    def baudrate(self, v):
        self._baud = v
        if self.is_open:
            self.baud_changes.append(v)

    def open(self):
        self.is_open = True
        self.opens.append(self._baud)
        self.signals.append((self.dtr, self.rts))
        if self.device and hasattr(self.device, "on_open"):
            self.device.on_open(self)

    @property
    def in_waiting(self):
        if self.raise_on_read is not None:
            raise self.raise_on_read
        return len(self._rx)

    def read(self, n):
        if self.raise_on_read is not None:
            raise self.raise_on_read
        if not self._rx and self.device and hasattr(self.device, "on_poll"):
            self.device.on_poll(self)
        if not self._rx and self.timeout:
            time.sleep(min(self.timeout, 0.002))
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    def write(self, b):
        b = bytes(b)
        self.tx += b
        if self.device and hasattr(self.device, "on_host_bytes"):
            self.device.on_host_bytes(self, b)
        return len(b)

    def reset_input_buffer(self):
        self._rx = bytearray()

    def close(self):
        if self.is_open:
            self.closes += 1
        self.is_open = False

    # device -> host injection
    def feed(self, b):
        self._rx += bytes(b)


def _install(monkeypatch_or_none, device):
    """Point engines.serial.Serial at a single fake bound to `device`."""
    fake = FakeSerial(device)
    engines.serial.Serial = lambda *a, **k: fake  # type: ignore[attr-defined]
    return fake


_REAL_SERIAL_CLASS = engines.serial.Serial


def _restore():
    engines.serial.Serial = _REAL_SERIAL_CLASS  # type: ignore[attr-defined]


# ── device doubles ───────────────────────────────────────────────────────────
class CpsDevice:
    """fw/icon bootloader double. Re-verifies every frame's checksum."""

    def __init__(self, kind, ident_hex=None, codeplug_mode=False, ack_finish=None,
                 nak_frame=None, drop_ack_at=None):
        self.kind = kind
        self.hs = b"UPDATE" if kind == "fw" else b"PROGRAM"
        self.ident = bytes.fromhex(ident_hex) if ident_hex else None
        self.codeplug = codeplug_mode
        self.ack_finish = ack_finish if ack_finish is not None else (kind == "icon")
        self.nak_frame = nak_frame
        self.drop_ack_at = drop_ack_at
        self.state = "hs"
        self.buf = bytearray()
        self.frames = []
        self.finish_byte = None
        self.bad_checksum = False

    def on_open(self, ser):
        pass

    def on_host_bytes(self, ser, data):
        self.buf += data
        while True:
            if self.state == "hs":
                if len(self.buf) < len(self.hs):
                    return
                if bytes(self.buf[:len(self.hs)]) != self.hs:
                    return
                del self.buf[:len(self.hs)]
                if self.codeplug:
                    ser.feed(bytes.fromhex("515806"))
                    self.state = "dead"
                    return
                ser.feed(b"\x06")
                self.state = "ident" if self.kind == "fw" else "frames"
            elif self.state == "ident":
                if len(self.buf) < 1:
                    return
                q = self.buf[0]
                del self.buf[:1]
                if q != 0x02:
                    return
                ser.feed(self.ident)
                self.state = "frames"
            elif self.state == "frames":
                if len(self.buf) < 1:
                    return
                if self.buf[0] == 0x18:
                    del self.buf[:1]
                    self.finish_byte = 0x18
                    if self.ack_finish:
                        ser.feed(b"\x06")
                    self.state = "done"
                    return
                if len(self.buf) < 40:
                    return
                frame = bytes(self.buf[:40])
                del self.buf[:40]
                idx = len(self.frames)
                self.frames.append(frame)
                s = sum(frame[1:37]) & 0xFFFF
                stored = frame[37] | (frame[38] << 8)
                if frame[0] != 0x01 or frame[39] != 0x06 or s != stored:
                    self.bad_checksum = True
                    ser.feed(b"\x15")
                    return
                if self.nak_frame == idx:
                    ser.feed(b"\x15")
                    return
                if self.drop_ack_at == idx:
                    return   # withhold the ACK — silent radio
                ser.feed(b"\x06")
            else:
                return


class SctDevice:
    """SCT3288 double. Parses 84 A9 61 framing (incl. the write-frame extra
    0x00) and answers each frame with its manifest ACK."""

    def __init__(self, manifest, wrong_ack_at=None):
        self.m = manifest
        self.wrong_ack_at = wrong_ack_at
        self.buf = bytearray()
        self.write_ack = bytes.fromhex(manifest["session"]["write_ack"])
        self.plan = engines.plan_sct(self._artifact_placeholder(), manifest) if False else None
        # Flatten control ACKs and the frame_index kinds so we can answer in order.
        self.kinds = [row[2] for row in manifest["frame_index"]]
        self.control_acks = []
        for c in manifest.get("controls", []):
            for a in c.get("acks", []):
                self.control_acks.append(bytes.fromhex(a))
        self.idx = 0
        self.ci = 0
        self.frames_seen = 0

    def _artifact_placeholder(self):
        return b""

    def on_open(self, ser):
        pass

    def on_host_bytes(self, ser, data):
        self.buf += data
        while True:
            if self.idx >= len(self.kinds):
                return
            frame = self._take_frame()
            if frame is None:
                return
            kind = self.kinds[self.idx]
            if kind == "write":
                ack = self.write_ack
            else:
                ack = self.control_acks[self.ci]
                self.ci += 1
            if self.frames_seen == self.wrong_ack_at:
                # flip one byte of the ACK
                bad = bytearray(ack)
                bad[6] ^= 0xFF
                ack = bytes(bad)
            self.frames_seen += 1
            self.idx += 1
            ser.feed(ack)

    def _take_frame(self):
        """Pull one 84 A9 61 frame out of self.buf, honoring the write-frame
        extra 0x00. Returns the frame bytes or None if incomplete."""
        if len(self.buf) < 6:
            return None
        if bytes(self.buf[:3]) != b"\x84\xa9\x61":
            # desync — drop a byte
            del self.buf[:1]
            return None
        length = (self.buf[3] << 8) | self.buf[4]   # big-endian, payload excl MOD
        mod = self.buf[5]
        total = 6 + length
        if mod == 0x03:
            total += 1   # write frames carry one extra 0x00 past the declared LEN
        if len(self.buf) < total:
            return None
        frame = bytes(self.buf[:total])
        del self.buf[:total]
        return frame


class NrDevice:
    """JieLi NR-board double — the TALKER. Drives baud negotiation and read
    requests from the manifest, re-verifies each served slice, and stops."""

    def __init__(self, ufw, manifest):
        self.ufw = ufw
        self.m = manifest
        self.buf = bytearray()
        self.served = bytearray(len(ufw))
        self.served_bytes = 0
        self.bad_slice = False
        self.stopped = False
        self.len_notify_reply = None
        self.baud_offers = []
        self.errors = []
        self.cursor = 0
        self.plan = self._build_plan()

    # -- plan: alternating (emit, expect) steps -----------------------------
    def _reads_for(self, off, length, block=512, descending=False):
        out = []
        n = (length + block - 1) // block
        for i in range(n):
            if descending:
                start = off + (n - 1 - i) * block
            else:
                start = off + i * block
            cnt = min(block, off + length - start)
            out.append((start, cnt))
        return out

    def _build_plan(self):
        p = []
        # 1. handshake: host sent ENTER already before first poll; we emit START(bare)
        p.append(("emit", fwupd_nr.build_frame(0x01)))
        p.append(("expect_start_res", 10000))          # host offers 10000
        # 2. echo START_RES(10000); host repeats it
        p.append(("emit", fwupd_nr.build_frame(0x01, engines._u32le(10000))))
        p.append(("expect_repeat", 10000))
        # 3. Phase A reads: every expected region except nr_image
        regions = self.m["expected_regions"]
        flash = self.m["flash_phase"]
        for r in regions:
            if r["name"] == "nr_image":
                continue
            for (off, cnt) in self._reads_for(r["offset"], r["length"]):
                p.append(("emit", fwupd_nr.build_frame(0x02, engines._u32le(off) + engines._u32le(cnt))))
                p.append(("expect_read", off, cnt))
        # 4. bump to 115200
        p.append(("emit", fwupd_nr.build_frame(0x01)))
        p.append(("expect_start_res", 115200))
        p.append(("emit", fwupd_nr.build_frame(0x01)))        # self-map: START again at 115200
        p.append(("expect_start_res", 115200))
        # 5. LEN_NOTIFY -> host replies NR_LEN_NOTIFY_REPLY
        p.append(("emit", fwupd_nr.build_frame(0x04, engines._u32le(flash["length"]))))
        p.append(("expect_len_reply",))
        # 6. Phase B: nr_image descending, plus one repeat and one 32-byte subblock
        blocks = self._reads_for(flash["offset"], flash["length"], block=512, descending=True)
        for (off, cnt) in blocks:
            p.append(("emit", fwupd_nr.build_frame(0x02, engines._u32le(off) + engines._u32le(cnt))))
            p.append(("expect_read", off, cnt))
        rep_off, rep_cnt = blocks[0]
        p.append(("emit", fwupd_nr.build_frame(0x02, engines._u32le(rep_off) + engines._u32le(rep_cnt))))
        p.append(("expect_read", rep_off, rep_cnt))
        p.append(("emit", fwupd_nr.build_frame(0x02, engines._u32le(rep_off) + engines._u32le(32))))
        p.append(("expect_read", rep_off, 32))
        # 7. STOP status 0
        p.append(("emit", fwupd_nr.build_frame(0x03, b"\x00")))
        p.append(("done",))
        return p

    def _mark(self, off, cnt):
        for i in range(off, off + cnt):
            if not self.served[i]:
                self.served[i] = 1
                self.served_bytes += 1

    def on_open(self, ser):
        pass

    def on_poll(self, ser):
        # emit the next device-initiated frame when it's our turn
        if self.cursor >= len(self.plan):
            return
        step = self.plan[self.cursor]
        if step[0] == "emit":
            ser.feed(step[1])
            self.cursor += 1
        elif step[0] == "done":
            self.stopped = True

    def on_host_bytes(self, ser, data):
        self.buf += data
        # parse complete host frames
        while True:
            fr = self._take_frame()
            if fr is None:
                return
            self._consume_host_frame(ser, fr)

    def _take_frame(self):
        while len(self.buf) >= 2 and not (self.buf[0] == 0xAA and self.buf[1] == 0x55):
            del self.buf[:1]
        if len(self.buf) < 6:
            return None
        length = self.buf[2] | (self.buf[3] << 8)
        total = 6 + length
        if len(self.buf) < total:
            return None
        fr = bytes(self.buf[:total])
        del self.buf[:total]
        # CRC check (host frames must be valid)
        stored = fr[total - 2] | (fr[total - 1] << 8)
        if fwupd_nr.crc16_xmodem(fr[:total - 2]) != stored:
            self.errors.append("host frame CRC bad: " + fr.hex())
        return fr

    def _consume_host_frame(self, ser, fr):
        if self.cursor >= len(self.plan):
            return
        step = self.plan[self.cursor]
        op = fr[4]
        payload = fr[5:len(fr) - 2]
        tag = step[0]
        if tag == "expect_start_res":
            if op != 0x01 or len(payload) != 4:
                self.errors.append("expected START_RES, got " + fr.hex())
            else:
                self.baud_offers.append(engines._read_u32le(payload, 0))
            self.cursor += 1
        elif tag == "expect_repeat":
            if op != 0x01 or len(payload) != 4:
                self.errors.append("expected repeat START_RES, got " + fr.hex())
            self.cursor += 1
        elif tag == "expect_read":
            _, off, cnt = step
            if op != 0x02:
                self.errors.append("expected READ response, got op " + hex(op))
            else:
                got = payload[8:]
                if got != self.ufw[off:off + cnt]:
                    self.bad_slice = True
                    self.errors.append("bad slice @ " + hex(off))
                self._mark(off, cnt)
            self.cursor += 1
        elif tag == "expect_len_reply":
            self.len_notify_reply = fr
            self.cursor += 1
        else:
            # unexpected host frame while we were about to emit — ignore
            pass


# ── fixtures ─────────────────────────────────────────────────────────────────
def _make_ufw() -> bytes:
    """The 0x3800-byte synthetic .ufw the JS suite rebuilds (glibc LCG seed 7,
    byte=(s>>16)%255 so 0xFF never appears), with two planted copy headers and a
    JLUFW trailer. fwupd_nr.build_manifest must accept it."""
    size = 0x3800
    buf = bytearray(size)
    s = 7
    for i in range(size):
        s = (s * 1103515245 + 12345) & 0x7FFFFFFF
        buf[i] = (s >> 16) % 255
    # file-table records @0x7C0 (32 bytes: crc|off|len|flags|idx|name[16])
    def rec(crc, off, length, flags, idx, name):
        r = bytearray(32)
        struct.pack_into("<IIIHH", r, 0, crc, off, length, flags, idx)
        nm = name.encode("ascii")
        r[16:16 + len(nm)] = nm
        return bytes(r)
    buf[0x7C0:0x7E0] = rec(0x11223344, 0x40, 0x100, 1, 0, "a.bin")
    buf[0x7E0:0x800] = rec(0x55667788, 0x840, 0x200, 1, 1, "uart_user.bin")
    # two identical copy headers at 0x1400 and 0x1700
    hdr = bytearray(32)
    hdr[0:4] = bytes([1, 2, 3, 4])
    hdr[4:8] = b"0.01"
    hdr[16:22] = b"QX700N"
    for i in range(22, 32):
        hdr[i] = 0xFF
    buf[0x1400:0x1420] = hdr
    buf[0x1700:0x1720] = hdr
    # trailer (28 bytes at EOF-28): u32 0x42733665 @0, "JLUFW" @+12
    tr = bytearray(28)
    struct.pack_into("<I", tr, 0, 0x42733665)
    tr[12:17] = b"JLUFW"
    buf[size - 28:size] = tr
    return bytes(buf)


# ── log/progress capture ─────────────────────────────────────────────────────
class Cap:
    def __init__(self):
        self.logs = []
        self.progress = []

    def on_log(self, msg, cls="info"):
        self.logs.append((cls, msg))

    def on_progress(self, done, total, phase):
        self.progress.append((done, total, phase))

    @property
    def last_progress(self):
        return self.progress[-1] if self.progress else None


# ── the checks ───────────────────────────────────────────────────────────────
FAILS = []
PASSES = []


def check(name, fn):
    try:
        fn()
        PASSES.append(name)
        print("  ok  " + name)
    except Exception as e:  # noqa: BLE001
        FAILS.append((name, repr(e)))
        print("FAIL  " + name + "  ->  " + repr(e))
    finally:
        _restore()


def expect_raises(fn, needle):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        assert needle in msg, "expected %r in error, got: %s" % (needle, msg)
        return e
    raise AssertionError("expected an error containing %r, none raised" % needle)


# fixtures reused across CPS tests (real fwupd_cps output, from the JS suite)
FW_ARTIFACT = bytes.fromhex(
    "0100c000080894a8afc4113c189306c7119f4b5fcb63129342bf855f6f93dbc98cf7ecb3c8e610060120c0000859a"
    "c74c504b2ba42718fd23f5229c38f3e63f39bf44439c193d902a0c79488454d11060140c000083763b76b4a2e2edc"
    "08158ba21f82798162750d8d462201b5ab38e62381f9be49270e06")
FW_MANIFEST = {
    "kind": "fw", "frames": 3, "payload_bytes": 96, "wire_bytes": 120,
    "sha256": "6ce0377ef4fae0282cea197c3542e52fe068f5564b105958e361c12ab210824a",
    "addr_first": 134266880, "addr_last": 134266944,
    "frame_len": 40, "block_size": 32, "base_addr": 134266880, "pad_byte": 0,
    "handshake_ascii": "UPDATE", "handshake_expect_hex": "06",
    "codeplug_collision_hex": None, "ident_query_hex": "02",
    "ident_reply_prefix_ascii": "ID890UV",
    "finish_byte_hex": "18", "finish_acked": False,
    "serial": {"baud": 921600, "control_line_state": "RTS only (0x0002)"},
}
ICON_ARTIFACT = bytes.fromhex(
    "01000004008f7a27a452418d469d92fa8d0d538c20d845fec19eeb4c55c8f05611db25e1020810060120000400e694"
    "d50ad453fd03cef0cfc1843ee5076ed73eb8750f5f539434727f49628c3b3c10060140000400fdb49c7dbc1a38464c"
    "30d4f696cf0f826c3711af0802cc8cecd403527aedd7c77d100601001004004a816f77cd174dbb9fc2f0e78c81f731"
    "d37eaa06089bc3752d7ae9ec675791cd92110601201004005c8ec1bd2875881ff087839e8e7a4c434338b8e683ddad"
    "0e49b886fa665e4202cc0f06")
ICON_MANIFEST = {
    "kind": "icon", "frames": 5, "payload_bytes": 160, "wire_bytes": 200,
    "sha256": "5a3790bfdaf347e0d8b62e1733f63b9712f52f0651a333d3cfb59ed285680f20",
    "addr_first": 262144, "addr_last": 266272,
    "frame_len": 40, "block_size": 32, "base_addr": 262144, "pad_byte": 0,
    "handshake_ascii": "PROGRAM", "handshake_expect_hex": "06",
    "codeplug_collision_hex": "515806", "ident_query_hex": None,
    "ident_reply_prefix_ascii": None,
    "finish_byte_hex": "18", "finish_acked": True,
    "serial": {"baud": 921600, "control_line_state": "RTS only (0x0002)"},
}
IDENT_D890 = "494438393055560000563130300000" + "06"   # "ID890UV\0\0V100\0\0"+06
IDENT_D878 = "494438373855560000563130300000" + "06"   # "ID878UV..."

# Reassembled from the frame_index-ordered frames (control frames are exact
# session/controls literals; the four 30-byte write frames are the capture bytes).
SCT_ARTIFACT = bytes.fromhex("".join([
    "84a96100040016002f3d",                                          # parity_disable [0:10]
    "84a9610002001601",                                              # parity_enable  [10:18]
    "84a96100040093032fbb",                                          # flash_initial  [18:28]
    "84a9610017039400010010d0a419c839c211346e6e91e7fb0d78bf2f8200",  # write#0 [28:58]
    "84a9610017039400011010d80997bcc5f261e1787944e2691426942f8b00",  # write#1 [58:88]
    "84a961001703940001201060ee163252a3328f8284775fd71bd3e72fae00",  # write#2 [88:118]
    "84a961001703940001301069539326df5382bd8c902a5a4522013c2f8400",  # write#3 [118:148]
    "84a96100040016012f3c",                                          # seg_parity     [148:158]
    "84a96100040093002fb8",                                          # flash_end      [158:168]
    "84a96100040016002f3d",                                          # parity_restore [168:178]
]))
SCT_MANIFEST = {
    "kind": "sct3288_baseband", "frames": 4, "control_frames": 6, "payload_bytes": 64, "wire_bytes": 178,
    "sha256": "fd0b664dfa63465620b685e0608ab608bc6f0ccf7fb15d99692758339fed1231",
    "addr_first": 256, "addr_last": 304, "addr_end": 320, "banks": [0],
    "segments": [{"region": 3, "first_frame": 0, "frames": 4, "addr_start": 256, "addr_end": 320}],
    "session": {
        "parity_disable": "84a96100040016002f3d", "parity_disable_ack": "84a9610002001706",
        "parity_enable": "84a9610002001601", "parity_enable_ack": "84a9610002001600",
        "flash_initial": "84a96100040093032fbb", "flash_initial_region": 3,
        "flash_initial_ack": "84a96100040093002fb8",
        "flash_end": "84a96100040093002fb8", "flash_end_ack": "84a96100040093002fb8",
        "write_ack": "84a96100040394002fbc"},
    "controls": [
        {"before_frame": 0, "frames": ["84a96100040016002f3d", "84a9610002001601"],
         "acks": ["84a9610002001706", "84a9610002001600"]},
        {"before_frame": 0, "frames": ["84a96100040093032fbb"], "acks": ["84a96100040093002fb8"]},
        {"before_frame": 4, "frames": ["84a96100040016012f3c", "84a96100040093002fb8", "84a96100040016002f3d"],
         "acks": ["84a96100040016002f3d", "84a96100040093002fb8", "84a96100040016002f3d"]}],
    "frame_index": [
        [0, 10, "parity_disable"], [10, 8, "parity_enable"], [18, 10, "flash_initial"],
        [28, 30, "write"], [58, 30, "write"], [88, 30, "write"], [118, 30, "write"],
        [148, 10, "seg_parity"], [158, 10, "flash_end"], [168, 10, "parity_restore"]],
}


def run_all():
    # -- frame/CRC vectors ---------------------------------------------------
    def t_crc():
        assert fwupd_nr.crc16_xmodem(bytes.fromhex("aa55010006")) == 0xF242
        assert fwupd_nr.build_frame(0x06).hex() == "aa5501000642f2"
        assert fwupd_nr.build_frame(0x01).hex() == "aa55010001a582"
        assert fwupd_nr.build_frame(0x01, engines._u32le(10000)).hex() == "aa55050001102700000bbf"
        assert fwupd_nr.build_frame(0x01, engines._u32le(115200)).hex() == "aa5505000100c201005cdc"
        assert engines.NR_LEN_NOTIFY_REPLY.hex() == "aa55010003e7a2"
        assert fwupd_nr.build_frame(0x03, b"\x00").hex() == "aa550200030074e9"
        assert fwupd_nr.build_frame(0x02, engines._u32le(0) + engines._u32le(512)).hex() \
            == "aa550900020000000000020000252f"
        assert engines.NR_BAUD_LADDER == {9600: 10000, 10000: 115200, 115200: 115200}
    check("nr crc + frame vectors", t_crc)

    # -- CPS structural gates (no port) -------------------------------------
    def t_kind_gate():
        expect_raises(lambda: engines.validate_cps_package("fw", ICON_ARTIFACT, ICON_MANIFEST),
                      'manifest says kind "icon"')
    check("cps kind gate", t_kind_gate)

    def t_addr_gate():
        # icon bytes smuggled under a doctored fw manifest (frames patched to 5):
        # the kind tag matches, but frame 0 addresses the asset flash, not the MCU.
        doctored = {**FW_MANIFEST, "frames": 5}
        expect_raises(lambda: engines.validate_cps_package("fw", ICON_ARTIFACT, doctored),
                      "addresses 0x40000")
    check("cps address gate (icon bytes under fw manifest)", t_addr_gate)

    def t_sha_gate():
        bad = bytearray(FW_ARTIFACT)
        bad[10] ^= 0x01
        cap = Cap()
        dev = CpsDevice("fw", ident_hex=IDENT_D890)
        _install(None, dev)
        expect_raises(lambda: engines.run("fw", "COM_FAKE", bytes(bad), FW_MANIFEST,
                                          on_log=cap.on_log, on_progress=cap.on_progress),
                      "does not match the manifest")
        assert dev.port.opens == [], "port must not open on a sha mismatch"
    check("cps sha256 gate before open", t_sha_gate)

    # -- fw happy path -------------------------------------------------------
    def t_fw_happy():
        cap = Cap()
        dev = CpsDevice("fw", ident_hex=IDENT_D890)
        fake = _install(None, dev)
        engines.run("fw", "COM_FAKE", FW_ARTIFACT, FW_MANIFEST,
                    on_log=cap.on_log, on_progress=cap.on_progress)
        assert len(dev.frames) == 3, dev.frames
        assert b"".join(dev.frames) == FW_ARTIFACT, "frames must be the artifact bytes VERBATIM"
        assert not dev.bad_checksum
        assert dev.finish_byte == 0x18
        assert fake.tx.endswith(b"\x18"), "finish byte must be the last write"
        assert fake.signals[0] == (False, True), "fw = RTS only (DTR false)"
        assert fake.opens == [921600]
        assert cap.last_progress == (3, 3, "done")
        assert fake.closes == 1
    check("fw happy path", t_fw_happy)

    def t_fw_wrong_radio():
        cap = Cap()
        dev = CpsDevice("fw", ident_hex=IDENT_D878)
        fake = _install(None, dev)
        expect_raises(lambda: engines.run("fw", "COM_FAKE", FW_ARTIFACT, FW_MANIFEST,
                                          on_log=cap.on_log, on_progress=cap.on_progress),
                      "WRONG RADIO")
        assert len(dev.frames) == 0, "no frames before the identity gate"
    check("fw wrong-radio abort", t_fw_wrong_radio)

    def t_fw_nak():
        cap = Cap()
        dev = CpsDevice("fw", ident_hex=IDENT_D890, nak_frame=1)
        fake = _install(None, dev)
        e = expect_raises(lambda: engines.run("fw", "COM_FAKE", FW_ARTIFACT, FW_MANIFEST,
                                              on_log=cap.on_log, on_progress=cap.on_progress),
                          "answered 15 instead of 06")
        assert "Stopping before the next frame" in str(e)
        assert len(dev.frames) == 2, "delivered frame 1 exactly once, no resend"
    check("fw NAK stops, no resend", t_fw_nak)

    def t_fw_silent():
        saved = engines.CPS_ACK_TIMEOUT_MS
        engines.CPS_ACK_TIMEOUT_MS = 150
        try:
            cap = Cap()
            dev = CpsDevice("fw", ident_hex=IDENT_D890, drop_ack_at=1)
            fake = _install(None, dev)
            e = expect_raises(lambda: engines.run("fw", "COM_FAKE", FW_ARTIFACT, FW_MANIFEST,
                                                  on_log=cap.on_log, on_progress=cap.on_progress),
                              "no ACK for frame 1")
            assert "NOT resending" in str(e)
            assert len(dev.frames) == 2, "frame 1 delivered exactly once"
        finally:
            engines.CPS_ACK_TIMEOUT_MS = saved
    check("fw silent radio: no-ACK, no resend", t_fw_silent)

    # -- icon happy path + codeplug abort -----------------------------------
    def t_icon_happy():
        cap = Cap()
        dev = CpsDevice("icon")
        fake = _install(None, dev)
        engines.run("icon", "COM_FAKE", ICON_ARTIFACT, ICON_MANIFEST,
                    on_log=cap.on_log, on_progress=cap.on_progress)
        assert len(dev.frames) == 5
        assert b"".join(dev.frames) == ICON_ARTIFACT
        assert dev.finish_byte == 0x18
        assert fake.signals[0] == (False, True)
        assert cap.last_progress == (5, 5, "done")
    check("icon happy path (finish acked)", t_icon_happy)

    def t_icon_codeplug():
        cap = Cap()
        dev = CpsDevice("icon", codeplug_mode=True)
        fake = _install(None, dev)
        e = expect_raises(lambda: engines.run("icon", "COM_FAKE", ICON_ARTIFACT, ICON_MANIFEST,
                                              on_log=cap.on_log, on_progress=cap.on_progress),
                          "CODEPLUG")
        assert "515806" in str(e)
        assert len(dev.frames) == 0, "no frames into a codeplug session"
    check("icon codeplug-mode abort", t_icon_codeplug)

    # -- SCT happy path + wrong ACK -----------------------------------------
    def t_sct_happy():
        cap = Cap()
        dev = SctDevice(SCT_MANIFEST)
        fake = _install(None, dev)
        engines.run("sct", "COM_FAKE", SCT_ARTIFACT, SCT_MANIFEST,
                    on_log=cap.on_log, on_progress=cap.on_progress)
        assert bytes(fake.tx) == SCT_ARTIFACT, "whole OUT stream must equal the artifact, in order"
        assert cap.last_progress == (10, 10, "done")
        assert fake.closes == 1
    check("sct happy path", t_sct_happy)

    def t_sct_wrong_ack():
        cap = Cap()
        dev = SctDevice(SCT_MANIFEST, wrong_ack_at=4)   # plan index 4 = the second write frame
        fake = _install(None, dev)
        e = expect_raises(lambda: engines.run("sct", "COM_FAKE", SCT_ARTIFACT, SCT_MANIFEST,
                                              on_log=cap.on_log, on_progress=cap.on_progress),
                          "NOT retrying")
        assert "frame 4 (write)" in str(e), str(e)
    check("sct wrong-ACK stops", t_sct_wrong_ack)

    def t_sct_plan_gate():
        bad = {**SCT_MANIFEST, "frame_index": SCT_MANIFEST["frame_index"][:-1]}
        expect_raises(lambda: engines.plan_sct(SCT_ARTIFACT, bad), "covers 168 of 178")
    check("sct plan tiling gate", t_sct_plan_gate)

    # -- NR happy path -------------------------------------------------------
    def t_nr_happy():
        ufw = _make_ufw()
        manifest = fwupd_nr.build_manifest(ufw)
        cap = Cap()
        dev = NrDevice(ufw, manifest)
        fake = _install(None, dev)
        engines.run("nr", "COM_FAKE", ufw, manifest,
                    on_log=cap.on_log, on_progress=cap.on_progress)
        assert not dev.errors, dev.errors
        assert not dev.bad_slice
        assert dev.len_notify_reply is not None and dev.len_notify_reply.hex() == "aa55010003e7a2"
        assert dev.baud_offers == [10000, 115200, 115200], dev.baud_offers
        assert fake.signals[0] == (True, True), "NR = DTR+RTS"
        # in-place SET_LINE_CODING: one open at 9600, baud changes 10000 then 115200
        assert fake.opens == [9600], fake.opens
        assert fake.baud_changes == [10000, 115200], fake.baud_changes
        assert cap.last_progress[2] == "done"
        assert cap.last_progress[0] == manifest["payload_bytes"], (cap.last_progress, manifest["payload_bytes"])
    check("nr device-pull happy path", t_nr_happy)

    def t_nr_size_gate():
        ufw = _make_ufw()
        manifest = fwupd_nr.build_manifest(ufw)
        dev = NrDevice(ufw, manifest)
        fake = _install(None, dev)
        expect_raises(lambda: engines.run("nr", "COM_FAKE", ufw[:1024], manifest,
                                          on_log=(lambda m, c="info": None),
                                          on_progress=(lambda d, t, p: None)),
                      "artifact/manifest mismatch")
        assert fake.opens == [], "no open on a size mismatch"
    check("nr size gate before open", t_nr_size_gate)

    # -- USB drop mid-read is wrapped as a FirmwareUpdateError ---------------
    def t_usb_drop_wrap():
        import serial as pyserial
        fake = FakeSerial(None)
        engines.serial.Serial = lambda *a, **k: fake
        link = engines.SerialLink("COM_FAKE")
        link.open(9600, dtr=True, rts=True)
        fake.raise_on_read = pyserial.SerialException("[Errno 6] Device not configured")
        e = expect_raises(lambda: link.read_exactly(1, 500), "dropped off the USB bus")
        assert isinstance(e, engines.FirmwareUpdateError)
        assert "serial read failed" in str(e), "CPS no-ACK path keys on this substring"
    check("USB drop wraps as FirmwareUpdateError", t_usb_drop_wrap)

    # -- falsy finish_byte_hex still sends 0x18 (JS `|| '18'` semantics) ------
    def t_finish_byte_falsy():
        m = {**FW_MANIFEST, "finish_byte_hex": None}   # JSON null, not absent
        dev = CpsDevice("fw", ident_hex=IDENT_D890)
        _install(None, dev)
        engines.run("fw", "COM_FAKE", FW_ARTIFACT, m,
                    on_log=lambda *a: None, on_progress=lambda *a: None)
        assert dev.finish_byte == 0x18, "a null finish_byte_hex must still send the 0x18 terminator"
    check("falsy finish_byte_hex defaults to 0x18", t_finish_byte_falsy)

    # -- abort mid-write -----------------------------------------------------
    def t_abort():
        cap = Cap()
        abort = threading.Event()
        # abort fires from the progress callback on the first write tick
        def on_prog(done, total, phase):
            cap.on_progress(done, total, phase)
            if phase == "write":
                abort.set()
        dev = CpsDevice("fw", ident_hex=IDENT_D890)
        fake = _install(None, dev)
        e = expect_raises(lambda: engines.run("fw", "COM_FAKE", FW_ARTIFACT, FW_MANIFEST,
                                              on_log=cap.on_log, on_progress=on_prog, abort=abort),
                          "aborted by the operator")
        assert not fake.tx.endswith(b"\x18"), "no finish byte after an abort"
        assert fake.closes == 1
    check("abort mid-write sends no finish byte", t_abort)


if __name__ == "__main__":
    run_all()
    print("\n%d passed, %d failed" % (len(PASSES), len(FAILS)))
    if FAILS:
        for name, err in FAILS:
            print("  FAIL " + name + ": " + err)
        sys.exit(1)


# pytest entry points (one test per behavior, sharing run_all's checks)
def test_all_engine_behaviors():
    run_all()
    assert not FAILS, "engine failures:\n" + "\n".join(n + ": " + e for n, e in FAILS)
