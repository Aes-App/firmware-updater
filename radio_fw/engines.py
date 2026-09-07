"""AnyTone AT-D890UV firmware / asset flashing over a serial (CDC-ACM) port.

A faithful pyserial port of the AesApp Web Serial flasher — FOUR protocols, four
sub-targets, one CDC port, decoded byte-for-byte from four USB captures of the
factory tools. This module only
*drives* the precompiled wire streams (radio_fw/vendor/fwupd_*): it never
rebuilds, re-checksums, re-orders or pads a frame. The artifact is the validated
truth; if it does not match its manifest we refuse to open the port.

    fw    main MCU code flash    921600 8N1, RTS only    "UPDATE"  -> 06 -> ident
    icon  asset/icon/font flash  921600 8N1, RTS only    "PROGRAM" -> bare 06
    nr    JieLi NR daughterboard 9600->10000->115200      device-PULL, we serve reads
    sct   SiCOMM SCT3288 DSP     38400 8N1, per the vendor  84 A9 61 framing

fw and icon are the SAME protocol with different handshakes and address spaces.
nr and sct share nothing with them or with each other.

THIS CODE CAN BRICK A RADIO. Three rules follow from that:
  1. Verify before transmitting — the artifact sha256 is checked against the
     manifest before the port is opened.
  2. Never resend a CPS frame — the bootloader wants a strictly monotonic
     single-pass address stream; a missing ACK ends the run. That rule is the
     CPS bootloader's, NOT a general one: the SCT3288 asks for a frame again in
     so many words (17 03/06) and the NR board re-requests anything it misses,
     so those two engines retry exactly where their vendor tool does — and
     nowhere else.
  3. Never guess — an unexpected reply, out-of-range read, or unknown opcode
     raises with the bytes in the message.

Usage:  engines.run(kind, port_name, artifact, manifest,
                     on_log=..., on_progress=..., abort=threading.Event())
`kind` is one of "fw"/"icon"/"nr"/"sct". Each engine owns port open/close (nr
renegotiates its line rate mid-session). Callbacks:
  on_log(msg, cls)             cls in {info, ok, tx, rx, er}
  on_progress(done, total, phase)
  abort                        a threading.Event; checked at every wait point.
"""
from __future__ import annotations

import hashlib
import time

import serial

# CRC + frame builders are imported from the precompiler so the engine and the
# artifact provably agree on the NR frame grammar (the JS reimplemented them and
# self-checks; here we reuse the one true implementation).
from .vendor import fwupd_nr as _nr

VERSION = "1.0.0"


class FirmwareUpdateError(Exception):
    """A protocol/verification failure. The message is operator-facing and
    names the exact fault — mirror the JS wording (tests match on it)."""


class AbortedError(FirmwareUpdateError):
    """The operator aborted mid-run."""


class ReplyTimeoutError(FirmwareUpdateError):
    """The device did not answer in time (as distinct from answering wrongly).

    Kept separate because the two mean different things to an operator: no
    reply at all points at the link (baud, cable, wrong mode), a wrong reply
    points at the protocol or the device's state.
    """


# ── tiny helpers ────────────────────────────────────────────────────────────
def _hex(b: bytes) -> str:
    return b.hex()


def _u32le(n: int) -> bytes:
    return bytes([n & 255, (n >> 8) & 255, (n >> 16) & 255, (n >> 24) & 255])


def _read_u32le(b, o: int) -> int:
    return (b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)) & 0xFFFFFFFF


def _ascii(bs: bytes) -> str:
    return "".join(chr(x) if 0x20 <= x < 0x7F else "." for x in bs)


def _sha256_hex(b: bytes) -> str:
    return hashlib.sha256(bytes(b)).hexdigest()


def _verify_artifact(artifact: bytes, manifest: dict, on_log) -> None:
    """The single cheapest brick-preventer: the server compiled and validated
    these exact bytes, so a hash mismatch means we hold something else."""
    if not isinstance(manifest, dict):
        raise FirmwareUpdateError("no manifest supplied")
    want = manifest.get("sha256")
    if not want:
        raise FirmwareUpdateError(
            "the manifest carries no sha256 — refusing to flash an unverifiable artifact")
    got = _sha256_hex(artifact)
    if got != want:
        raise FirmwareUpdateError(
            "artifact sha256 " + got + " does not match the manifest (" + want + "). "
            "The bytes here are NOT the stream the compiler validated. Nothing was sent.")
    on_log(str(len(artifact)) + " artifact bytes, sha256 " + got[:16] + "… matches the manifest", "ok")


# ── serial plumbing ─────────────────────────────────────────────────────────
# Mirrors the JS PortLink: a buffered link with event-timeout reads, explicit
# DTR/RTS, abort support, and an in-place line-rate change for the NR board.
_REOPEN_SETTLE_S = 0.12   # let the last frame clear the UART before changing rate
_REOPEN_GAP_S = 0.06      # ... and let it settle before resuming
# Read-poll granularity. Set ONCE at open() and never touched again: mutating
# ser.timeout on an open port re-runs _reconfigure_port(), which at macOS custom
# baud rates (921600, 10000) fires an IOSSIOSPEED ioctl — wire-visible USB
# control traffic mid-receive that the browser flasher never generates. Small
# enough that an operator abort still lands within one poll.
_READ_POLL_S = 0.05


class SerialLink:
    def __init__(self, port_name: str, on_log=None, abort=None):
        self.port_name = port_name
        self.on_log = on_log or (lambda m, c="info": None)
        self.abort = abort
        self.ser: serial.Serial | None = None
        self.baudrate = 0
        self._buf = bytearray()

    def log(self, msg, cls="info"):
        self.on_log(msg, cls)

    def check_abort(self):
        if self.abort is not None and self.abort.is_set():
            raise AbortedError("aborted by the operator")

    def open(self, baud: int, dtr: bool, rts: bool):
        ser = serial.Serial()
        ser.port = self.port_name
        ser.baudrate = baud
        ser.bytesize = serial.EIGHTBITS
        ser.parity = serial.PARITY_NONE
        ser.stopbits = serial.STOPBITS_ONE
        ser.timeout = _READ_POLL_S      # fixed poll granularity; never mutated after open
        ser.write_timeout = 5
        ser.rtscts = False
        ser.dsrdtr = False
        # Set the control lines BEFORE open so a bootloader watching DTR as a
        # reset line does not see a spurious pulse (pyserial applies the stored
        # state on open), then re-assert after in case the driver reset them.
        ser.dtr = dtr
        ser.rts = rts
        ser.open()
        try:
            ser.dtr = dtr
            ser.rts = rts
        except Exception:
            pass
        self.ser = ser
        self.baudrate = baud
        self._buf = bytearray()
        self.log("port open @ " + str(baud) + " baud 8N1", "ok")
        self.log("control lines: DTR=" + ("1" if dtr else "0") + " RTS=" + ("1" if rts else "0"), "info")

    def set_baud(self, baud: int):
        """CDC-ACM SET_LINE_CODING in place — faithful to the capture (no USB
        re-enumeration is observed), and safer than a close/reopen which could
        drop the CDC endpoint mid-session."""
        self.ser.baudrate = baud
        self.baudrate = baud

    def reopen(self, baud: int, dtr: bool, rts: bool):
        """The NR board negotiates its own baud with the update already in
        flight. Settle, change the line rate, and discard stale RX (equivalent
        to the JS close/reopen, without re-enumerating)."""
        time.sleep(_REOPEN_SETTLE_S)
        self.set_baud(baud)
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self._buf = bytearray()
        time.sleep(_REOPEN_GAP_S)
        try:
            self.ser.dtr = dtr
            self.ser.rts = rts
        except Exception:
            pass
        self.log("line rate -> " + str(baud) + " baud (SET_LINE_CODING)", "info")

    def close(self):
        try:
            if self.ser is not None and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    @property
    def buffered(self) -> int:
        return len(self._buf)

    @property
    def inbuf(self) -> bytes:
        return bytes(self._buf)

    def _dropped(self, e) -> "FirmwareUpdateError":
        """A read/enumeration failure mid-session, as an operator-facing error.
        Mirrors the JS 'the radio dropped off the USB bus' diagnosis so the CPS
        no-ACK path and callers see a FirmwareUpdateError, not a raw pyserial
        exception."""
        return FirmwareUpdateError(
            "serial read failed: " + str(e) + " — the radio dropped off the USB bus "
            "(reset, cable or power glitch?)")

    def _read(self, size: int) -> bytes:
        try:
            return self.ser.read(size)
        except (serial.SerialException, OSError) as e:
            raise self._dropped(e)

    def _pump(self):
        """Pull whatever is available into the buffer, blocking at most one poll
        (ser.timeout, set once at open) if nothing is waiting yet."""
        ser = self.ser
        try:
            n = ser.in_waiting
        except (serial.SerialException, OSError) as e:
            raise self._dropped(e)
        if n:
            self._buf += self._read(n)
            return
        # nothing waiting — block one poll for the first byte, then drain the rest
        b = self._read(1)
        if b:
            self._buf += b
            try:
                extra = ser.in_waiting
            except (serial.SerialException, OSError):
                extra = 0
            if extra:
                self._buf += self._read(extra)

    def ensure(self, n: int, timeout_ms: int):
        """Wait until the buffer holds >= n bytes, without consuming."""
        deadline = time.monotonic() + timeout_ms / 1000.0
        while len(self._buf) < n:
            self.check_abort()
            if time.monotonic() > deadline:
                have = len(self._buf)
                raise ReplyTimeoutError(
                    "timeout after " + str(timeout_ms) + " ms: wanted " + str(n)
                    + " byte(s), have " + str(have)
                    + (" [" + _hex(bytes(self._buf)) + "]" if have else ""))
            self._pump()

    def consume(self, n: int) -> bytes:
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def read_exactly(self, n: int, timeout_ms: int) -> bytes:
        self.ensure(n, timeout_ms)
        return self.consume(n)

    def flush(self):
        """Drop anything buffered (ours and the OS's). Called before the first
        data frame so a late handshake byte cannot be mistaken for its ACK."""
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        if self._buf:
            self.log("discarding " + str(len(self._buf)) + " stale RX byte(s): " + _hex(bytes(self._buf)), "info")
        self._buf = bytearray()

    def send(self, data: bytes):
        self.check_abort()
        try:
            self.ser.write(data)
        except (serial.SerialException, OSError) as e:
            raise FirmwareUpdateError(
                "serial write failed: " + str(e) + " — the radio dropped off the USB bus "
                "(reset, cable or power glitch?)")


# ═══════════════════════════════════════════════════════════════════════════
# CPS protocol — fw (main MCU) and icon (asset flash)
# ═══════════════════════════════════════════════════════════════════════════
CPS_ACK = 0x06
CPS_FRAME_LEN = 40
CPS_BAUD = 921600
CPS_HANDSHAKE_TIMEOUT_MS = 3000
CPS_ACK_TIMEOUT_MS = 2000
CPS_FIRST_ACK_TIMEOUT_MS = 5000
CPS_FINISH_TIMEOUT_MS = 2000
CODEPLUG_REPLY = bytes.fromhex("515806")   # "QX"+ACK — the codeplug handshake reply
PROGRESS_EVERY_FRAMES = 64

CPS_PROFILE = {
    "fw":   {"handshake": "UPDATE",  "ident_query": True,  "finish_acked": False, "label": "main MCU firmware"},
    "icon": {"handshake": "PROGRAM", "ident_query": False, "finish_acked": True,  "label": "icons & fonts"},
    # D878UVII BT+APRS linked board: same CPS protocol as fw (UPDATE + an ident
    # query), gated on the "IA-BORD" bootloader identity from the manifest.
    "aprs": {"handshake": "UPDATE",  "ident_query": True,  "finish_acked": False, "label": "APRS + BT board"},
}


def validate_cps_package(kind: str, artifact: bytes, manifest: dict) -> tuple[int, int]:
    """Structural gate, run before the port is opened. sha256 proves the bytes
    are intact; this proves they are the RIGHT KIND — an icon artifact under
    /fw would stream asset data into the MCU code flash at 0x0800C000."""
    profile = CPS_PROFILE[kind]
    if manifest.get("kind") != kind:
        raise FirmwareUpdateError(
            'this is the "' + kind + '" engine but the manifest says kind "'
            + str(manifest.get("kind")) + '" — wrong component. Nothing was sent.')
    frame_len = manifest.get("frame_len") or CPS_FRAME_LEN
    if frame_len != CPS_FRAME_LEN:
        raise FirmwareUpdateError(
            "manifest frame_len is " + str(frame_len) + ", this engine only speaks the "
            + str(CPS_FRAME_LEN) + "-byte D890 CPS frame — refusing to guess at a protocol revision.")
    if not len(artifact) or len(artifact) % frame_len:
        raise FirmwareUpdateError(
            "artifact is " + str(len(artifact)) + " bytes, not a whole number of "
            + str(frame_len) + "-byte frames — truncated stream.")
    frames = len(artifact) // frame_len
    if manifest.get("frames") != frames:
        raise FirmwareUpdateError(
            "artifact holds " + str(frames) + " frames but the manifest declares "
            + str(manifest.get("frames")) + " — artifact/manifest mismatch.")
    if manifest.get("handshake_ascii") != profile["handshake"]:
        raise FirmwareUpdateError(
            'manifest handshake "' + str(manifest.get("handshake_ascii")) + '" != the "' + kind
            + '" handshake "' + profile["handshake"] + '" — mismatched manifest.')
    if profile["ident_query"] != bool(manifest.get("ident_query_hex")):
        raise FirmwareUpdateError(
            'the "' + kind + '" profile ' + ("requires" if profile["ident_query"] else "has no")
            + " bootloader identity query but the manifest "
            + ("carries one" if manifest.get("ident_query_hex") else "omits it") + " — mismatched manifest.")
    if profile["ident_query"] and not manifest.get("ident_reply_prefix_ascii"):
        raise FirmwareUpdateError(
            "the manifest carries no ident_reply_prefix_ascii, so the bootloader identity cannot be "
            "gated against the package model. Refusing to write main firmware blind.")
    if artifact[0] != 0x01 or artifact[frame_len - 1] != CPS_ACK:
        raise FirmwareUpdateError(
            "frame 0 is " + _hex(artifact[:8]) + "… — not a CPS write frame "
            "(must start 01 and end 06). Wrong artifact for this engine.")
    first = _read_u32le(artifact, 1)
    addr_first = (manifest.get("addr_first") or 0) & 0xFFFFFFFF   # JS: addr_first >>> 0 (undefined -> 0)
    if first != addr_first:
        raise FirmwareUpdateError(
            "frame 0 addresses 0x" + format(first, "x") + " but the manifest starts at 0x"
            + format(addr_first, "x") + " — artifact/manifest mismatch.")
    return frames, frame_len


def _cps_handshake(kind: str, link: SerialLink, manifest: dict, on_log):
    hs = manifest["handshake_ascii"]
    tx = hs.encode("ascii")
    on_log('TX "' + hs + '" (' + _hex(tx) + ")", "tx")
    link.send(tx)
    first = link.read_exactly(1, CPS_HANDSHAKE_TIMEOUT_MS)
    on_log("RX " + _hex(first), "rx")

    # THE DANGEROUS OVERLAP: "PROGRAM" is also the codeplug handshake, same USB
    # identity. The codeplug protocol answers 51 58 06 ("QX"+ACK), the asset
    # bootloader a BARE 06. Writing 40-byte bootloader frames into a codeplug
    # session would scribble over the radio's configuration memory.
    if first[0] == CODEPLUG_REPLY[0]:
        tail = b""
        try:
            tail = link.read_exactly(len(CODEPLUG_REPLY) - 1, 500)
        except Exception:
            pass
        raise FirmwareUpdateError(
            "the radio replied " + _hex(first + tail) + " — that is the CODEPLUG protocol "
            '("QX" + ACK), not the update bootloader. The radio is in NORMAL PC mode. Put it into '
            "update mode with the vendor key combination and reconnect. Nothing was written.")
    if first[0] != CPS_ACK:
        raise FirmwareUpdateError(
            'handshake "' + hs + '" answered ' + _hex(first) + " instead of 06 — the radio is not "
            "in update mode. Nothing was written.")
    on_log("update mode entered (" + CPS_PROFILE[kind]["label"] + ")", "ok")

    # fw only: read the bootloader identity and gate the model on it. The single
    # check that stops a D878/D168 package being written to a D890.
    if manifest.get("ident_query_hex"):
        q = bytes.fromhex(manifest["ident_query_hex"])
        on_log("TX " + _hex(q) + " (identity query)", "tx")
        link.send(q)
        ident = link.read_exactly(16, CPS_HANDSHAKE_TIMEOUT_MS)
        on_log("RX " + _hex(ident) + '  "' + _ascii(ident) + '"', "rx")
        if ident[15] != CPS_ACK:
            raise FirmwareUpdateError(
                "the identity frame did not end in 06 (" + _hex(ident) + ") — unreadable "
                "bootloader identity, refusing to write.")
        want = manifest.get("ident_reply_prefix_ascii")
        if want and _ascii(ident).find(want) != 0:
            raise FirmwareUpdateError(
                'the bootloader identifies as "' + _ascii(ident)[:12] + '" but this package is for "'
                + want + '" — WRONG RADIO for this firmware. Nothing was written.')
        on_log("bootloader identity accepted (" + (want or "no model gate in the manifest") + ")", "ok")


def _cps_await_ack(link: SerialLink, index: int, frame: bytes, timeout_ms: int):
    """Strict 1:1 ACK. We never resend: the bootloader takes a single monotonic
    pass, and a resend corrupts its state, so a missing/unexpected ACK ends the run."""
    addr = "0x" + format(_read_u32le(frame, 1), "08x")
    try:
        r = link.read_exactly(1, timeout_ms)
    except AbortedError:
        raise
    except FirmwareUpdateError as e:
        if "aborted by the operator" in str(e) or "serial read failed" in str(e):
            raise
        raise FirmwareUpdateError(
            "no ACK for frame " + str(index) + " @ " + addr + ": " + str(e)
            + ". NOT resending — the bootloader takes a single monotonic pass, so this component "
            "has to be re-flashed from frame 0.")
    if r[0] != CPS_ACK:
        raise FirmwareUpdateError(
            "frame " + str(index) + " @ " + addr + " answered " + _hex(r) + " instead of 06"
            + (" (plus " + _hex(link.inbuf) + " buffered)" if link.buffered else "")
            + " — the bootloader rejected it or the link desynchronised. Stopping before the next frame.")


def _run_cps(kind: str, port_name: str, artifact: bytes, manifest: dict,
             on_log, on_progress, abort, pace_ms: int = 0):
    profile = CPS_PROFILE[kind]
    frames, frame_len = validate_cps_package(kind, artifact, manifest)
    _verify_artifact(artifact, manifest, on_log)
    on_progress(0, frames, "handshake")

    serial_cfg = manifest.get("serial") or {}
    link = SerialLink(port_name, on_log=on_log, abort=abort)
    try:
        link.open(serial_cfg.get("baud") or CPS_BAUD, dtr=False, rts=True)  # RTS only
        link.flush()
        _cps_handshake(kind, link, manifest, on_log)

        link.flush()   # nothing in flight when frame 0's ACK is due
        on_log("streaming " + str(frames) + " x " + str(frame_len) + "-byte frames ("
               + str(manifest.get("payload_bytes")) + " payload bytes) from 0x"
               + format((manifest.get("addr_first") or 0) & 0xFFFFFFFF, "x") + "…", "ok")
        t0 = time.monotonic()
        for i in range(frames):
            link.check_abort()
            frame = artifact[i * frame_len:(i + 1) * frame_len]
            link.send(frame)
            _cps_await_ack(link, i, frame, CPS_FIRST_ACK_TIMEOUT_MS if i == 0 else CPS_ACK_TIMEOUT_MS)
            if pace_ms:
                time.sleep(pace_ms / 1000.0)
            if (i % PROGRESS_EVERY_FRAMES) == 0 or i == frames - 1:
                on_progress(i + 1, frames, "write")
        secs = (time.monotonic() - t0)
        on_log("all " + str(frames) + " frames acknowledged in " + format(secs, ".1f") + " s", "ok")

        on_progress(frames, frames, "finish")
        fin = bytes([int(manifest.get("finish_byte_hex") or "18", 16)])   # JS: || '18' (falsy -> default)
        on_log("TX " + _hex(fin) + " (finish)", "tx")
        link.send(fin)
        try:
            r = link.read_exactly(1, CPS_FINISH_TIMEOUT_MS)
            on_log("RX " + _hex(r) + " (finish acknowledged)", "rx")
        except Exception:
            on_log("no ACK for the finish byte" + (
                " — the capture shows this component ACKs it; every data frame was ACKed, so the "
                "write itself completed" if profile["finish_acked"]
                else " — expected, this component never ACKs it"),
                "er" if profile["finish_acked"] else "info")
        on_log("WARNING: this protocol has no read-back or verify step. \"Done\" means every frame "
               "was acknowledged, not that the image was independently checked — confirm the version "
               "on the radio.", "info")
        on_progress(frames, frames, "done")
    finally:
        link.close()


# ═══════════════════════════════════════════════════════════════════════════
# NR board (JieLi daughterboard) — DEVICE-PULL, inverted from the others
# ═══════════════════════════════════════════════════════════════════════════
NR_OP = {"START": 0x01, "READ": 0x02, "STOP": 0x03, "LEN_NOTIFY": 0x04, "ALIVE": 0x05, "ENTER": 0x06}
NR_MAX_READ = 65535 - 1 - 8
NR_BAUD_LADDER = {9600: 10000, 10000: 115200, 115200: 115200}
NR_LEN_NOTIFY_REPLY = _nr.build_frame(NR_OP["STOP"])   # aa55010003e7a2 (opcode 0x03, certain bytes)
NR_IDLE_TIMEOUT_MS = 20000
NR_MAX_RESYNC_DROP = 256
# A corrupt frame is dropped and re-requested, but a link that only ever
# delivers corrupt frames is not noisy, it is broken.
NR_MAX_BAD_CRC = 8
# The board's longest frame is the 15-byte READ request (opcode + two u32); a
# declared body larger than this is a corrupted length field, not a frame. It
# matters because the length is read BEFORE the CRC can vet it: trusting a
# corrupt one makes us wait for — and then swallow — the good frames behind it.
NR_MAX_DEVICE_PAYLOAD = 32
# ENTER is the ONE frame the board cannot ask for again: until it receives one
# the board is passive, so a lost ENTER hangs us where the vendor recovers. The
# vendor re-drives every frame on a 2 s AutoReset timer, 5 attempts
# (BootHelper.cs:81-85, :296-312, cntRetry = 5 at :44); we copy that for ENTER
# alone. Doing it for the rest would inject duplicate read-response payloads the
# capture never shows a host sending — and the vendor's own retry block never
# touches its pass counter (:296-312), so it resends across a baud change at the
# NEW rate while the board is still on the old one. That is a vendor hazard, not
# a behaviour to port.
NR_ENTER_ATTEMPTS = 5
NR_ENTER_RETRY_TIMEOUT_MS = 2000
PROGRESS_EVERY_READS = 16


def _build_nr_frame(opcode: int, payload: bytes = b"") -> bytes:
    return _nr.build_frame(opcode, payload)


def _nr_read_frame(link: SerialLink, timeout_ms: int) -> dict:
    """One complete, CRC-valid frame off the byte stream.

    A frame that fails its CRC is DROPPED, not fatal. The vendor does the same:
    `if (!packageHelper.Verify) break;` (BootHelper.cs:236-239) leaves the state
    machine in WaitResponse1 without stopping its 2 s retry timer, so the
    exchange is simply re-driven. Raising here instead would end a 982-read
    session mid-image — the one outcome that needs the emergency PF3+PF1 entry
    to recover from. The board re-requests anything it does not get, so a
    dropped frame costs a round trip and nothing else.

    Leading garbage still means a baud mismatch: the board is the only talker,
    so resync loudly and give up after NR_MAX_RESYNC_DROP bytes.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0

    def left() -> int:
        """The budget still owed to the CALLER's timeout.

        Every wait below must be measured against this one deadline. Passing a
        cached value to each `ensure()` would restart its clock three times over
        and let one call block ~3x the timeout the caller asked for.
        """
        ms = int((deadline - time.monotonic()) * 1000)
        if ms <= 0:
            raise ReplyTimeoutError(
                "timeout after " + str(timeout_ms) + " ms waiting for a complete NR frame")
        return ms

    bad_crc = 0

    def drop_frame(why: str):
        """Discard a frame we cannot trust and resync past it.

        Only the AA 55 is consumed, never the declared length: a corrupted
        length field is exactly the case where trusting it would swallow the
        good frames queued behind it.
        """
        nonlocal bad_crc
        link.consume(2)
        bad_crc += 1
        link.log(why + " — dropping it and resyncing; the board re-requests anything it does not "
                 "get (#" + str(bad_crc) + ")", "er")
        if bad_crc > NR_MAX_BAD_CRC:
            raise FirmwareUpdateError(
                str(bad_crc) + " NR frames in a row were unusable — the link is corrupt, not merely "
                "noisy. Stopping.")

    while True:
        dropped = 0
        while True:
            link.ensure(2, left())
            if link._buf[0] == 0xAA and link._buf[1] == 0x55:
                break
            link.consume(1)
            dropped += 1
            if dropped > NR_MAX_RESYNC_DROP:
                raise FirmwareUpdateError(
                    "no AA 55 frame in " + str(dropped) + " bytes of RX — the link is at the wrong baud "
                    "or the board is not in update mode.")
        if dropped:
            link.log("resynchronised after dropping " + str(dropped) + " stray byte(s)", "er")
        link.ensure(4, left())
        length = link._buf[2] | (link._buf[3] << 8)
        if length < 1 or length > NR_MAX_DEVICE_PAYLOAD:
            drop_frame("NR frame declares a " + str(length) + "-byte body, outside the 1.."
                       + str(NR_MAX_DEVICE_PAYLOAD) + " the board ever sends")
            continue
        total = 6 + length
        link.ensure(total, left())
        frame = bytes(link._buf[:total])          # peek: only consume once it verifies
        stored = frame[total - 2] | (frame[total - 1] << 8)
        calc = _nr.crc16_xmodem(frame[:total - 2])
        if stored == calc:
            link.consume(total)
            return {"opcode": frame[4], "payload": frame[5:total - 2], "raw": frame}
        drop_frame("NR frame CRC mismatch: stored 0x" + format(stored, "x") + ", computed 0x"
                   + format(calc, "x") + " over " + _hex(frame[:min(total, 24)]))


def _run_nr(port_name: str, artifact: bytes, manifest: dict, on_log, on_progress, abort):
    if manifest.get("kind") != "d890_nr_ufw":
        raise FirmwareUpdateError(
            'this is the "nr" engine but the manifest says kind "' + str(manifest.get("kind"))
            + '" — wrong component. Nothing was sent.')
    if manifest.get("ufw_bytes") != len(artifact):
        raise FirmwareUpdateError(
            "artifact is " + str(len(artifact)) + " bytes but the manifest describes a "
            + str(manifest.get("ufw_bytes")) + "-byte .ufw — artifact/manifest mismatch.")
    link_meta = manifest.get("link") or {}
    enter_frame = bytes.fromhex(link_meta.get("handshake_tx", "") or "")
    built = _build_nr_frame(NR_OP["ENTER"])
    if enter_frame != built:
        raise FirmwareUpdateError(
            "manifest handshake " + _hex(enter_frame) + " != locally built " + _hex(built)
            + " — this engine and the precompiler disagree about the NR frame grammar. Refusing to "
            "talk to the board.")
    _verify_artifact(artifact, manifest, on_log)

    total = manifest.get("payload_bytes") or len(artifact)
    served_map = bytearray(len(artifact))
    served_bytes = 0
    reads = 0
    announced_len = None        # the image length the board reported via LEN_NOTIFY
    len_zero_warned = False      # so the "0 bytes" note is logged once, not per repeat
    on_progress(0, total, "handshake")

    link = SerialLink(port_name, on_log=on_log, abort=abort)
    try:
        link.open(link_meta.get("initial_baud") or 9600, dtr=True, rts=True)   # NR: DTR+RTS
        link.flush()

        # ENTER is the one frame the board cannot re-request, so it is the one
        # frame we retry (NR_ENTER_ATTEMPTS). Nothing is written until the board
        # answers, so retrying here is free.
        pending = None
        for attempt in range(1, NR_ENTER_ATTEMPTS + 1):
            on_log("TX " + _hex(enter_frame) + " REQ_ENTER_UPDATE_MODE"
                   + ("" if attempt == 1 else " (retry " + str(attempt - 1)
                      + " of " + str(NR_ENTER_ATTEMPTS - 1) + ")"), "tx")
            link.send(enter_frame)
            try:
                pending = _nr_read_frame(link, NR_ENTER_RETRY_TIMEOUT_MS)
                break
            except AbortedError:
                raise
            except ReplyTimeoutError:
                if attempt == NR_ENTER_ATTEMPTS:
                    raise FirmwareUpdateError(
                        "the NR board did not answer REQ_ENTER_UPDATE_MODE in "
                        + str(NR_ENTER_ATTEMPTS) + " attempts over "
                        + str(NR_ENTER_ATTEMPTS * NR_ENTER_RETRY_TIMEOUT_MS // 1000)
                        + " s. Nothing has been written. Check the screen reads \"UPDATE MODE "
                        "FOR LinkBoard\" and that this is the radio's COM port.")
                link.flush()

        last_start_res = None
        finished = False
        while not finished:
            link.check_abort()
            if pending is not None:
                f, pending = pending, None
            else:
                f = _nr_read_frame(link, NR_IDLE_TIMEOUT_MS)
            op = f["opcode"]
            payload = f["payload"]
            raw = f["raw"]

            if op == NR_OP["START"]:
                if len(payload) == 0:
                    nxt = NR_BAUD_LADDER.get(link.baudrate)
                    if not nxt:
                        raise FirmwareUpdateError(
                            "the board asked for a baud while the port is at " + str(link.baudrate)
                            + ", which is not on the capture-derived ladder (9600 -> 10000 -> 115200). "
                            "Refusing to guess a rate.")
                    res = _build_nr_frame(NR_OP["START"], _u32le(nxt))
                    on_log("RX " + _hex(raw) + " UPDATE_START", "rx")
                    on_log("TX " + _hex(res) + " UPDATE_START_RES baud=" + str(nxt), "tx")
                    link.send(res)
                    last_start_res = res
                    if nxt != link.baudrate:
                        link.reopen(nxt, dtr=True, rts=True)
                    else:
                        on_log("already at " + str(nxt) + " baud — port left open", "info")
                elif len(payload) == 4:
                    if not last_start_res:
                        raise FirmwareUpdateError(
                            "the board echoed a baud (" + _hex(raw) + ") before we offered one — "
                            "unexpected session order, stopping.")
                    echoed = _read_u32le(payload, 0)
                    offered = _read_u32le(last_start_res, 5)
                    if echoed != offered:
                        raise FirmwareUpdateError(
                            "the board echoed baud " + str(echoed) + " but we offered " + str(offered)
                            + " — refusing to guess what it wants.")
                    on_log("RX " + _hex(raw) + " (baud echo) — repeating START_RES", "tx")
                    link.send(last_start_res)
                else:
                    raise FirmwareUpdateError(
                        "UPDATE_START with a " + str(len(payload)) + "-byte payload (" + _hex(raw)
                        + ") — the capture only ever shows 0 (ask) or 4 (echo).")

            elif op == NR_OP["READ"]:
                if len(payload) != 8:
                    raise FirmwareUpdateError(
                        "UPDATE_READ_REQ payload is " + str(len(payload)) + " bytes (" + _hex(raw)
                        + "), expected 8 = u32 LE offset + u32 LE count.")
                off = _read_u32le(payload, 0)
                count = _read_u32le(payload, 4)
                # A zero-length request is legal and self-describing: the vendor
                # answers it with a bare 9-byte payload and no data
                # (BootHelper.cs:172-184), and our own fwupd_nr.serve() already
                # does the same. Only a request that runs off the end of the
                # .ufw is refused — answering that short or zero-padded really
                # would write garbage.
                if count and off + count > len(artifact):
                    raise FirmwareUpdateError(
                        "the board requested " + str(count) + " byte(s) at 0x" + format(off, "x")
                        + " but the .ufw is " + str(len(artifact)) + " bytes. Refusing to answer short "
                        "or padded — that writes garbage to the board. Wrong .ufw for this NR board?")
                if off > len(artifact):
                    raise FirmwareUpdateError(
                        "the board asked to read at 0x" + format(off, "x") + ", past the end of the "
                        + str(len(artifact)) + "-byte .ufw. Wrong .ufw for this NR board?")
                if count > NR_MAX_READ:
                    raise FirmwareUpdateError(
                        "the board requested " + str(count) + " bytes at 0x" + format(off, "x")
                        + ", more than the " + str(NR_MAX_READ) + " a single AA55 frame can carry "
                        "(u16 length field). Refusing rather than sending a wrapped length. Wrong "
                        ".ufw for this board?")
                link.send(_build_nr_frame(NR_OP["READ"], payload + artifact[off:off + count]))
                for i in range(off, off + count):
                    if not served_map[i]:
                        served_map[i] = 1
                        served_bytes += 1
                reads += 1
                if (reads % PROGRESS_EVERY_READS) == 1:
                    on_log("served " + str(count) + " B @ 0x" + format(off, "x") + " (read #" + str(reads) + ")", "rx")
                on_progress(served_bytes, max(total, served_bytes), "serve")

            elif op == NR_OP["STOP"]:
                if len(payload) == 0:
                    on_log("RX " + _hex(raw) + " — the board echoed our LEN_NOTIFY reply, ignoring", "info")
                else:
                    status = payload[0]
                    on_log("RX " + _hex(raw) + " UPDATE_STOP status=0x" + format(status, "x"),
                           "ok" if status == 0 else "er")
                    if status != 0:
                        raise FirmwareUpdateError(
                            "the NR board ended the update with status 0x" + format(status, "x")
                            + " (0x00 = success). The image is NOT complete.")
                    finished = True

            elif op == NR_OP["LEN_NOTIFY"]:
                notified = _read_u32le(payload, 0) if len(payload) >= 4 else None
                announced_len = notified
                flash_phase = manifest.get("flash_phase") or {}
                expect = flash_phase.get("length")
                on_log("RX " + _hex(raw) + " UPDATE_LEN_NOTIFY len=" + str(notified), "rx")
                # The board decides the image length itself and only flashes when it
                # is non-zero. len=0 means the agent found nothing to write — almost
                # always because the board is already on this version (the factory
                # tool announces the full image size here when an update is due).
                if notified == 0 and not len_zero_warned:
                    len_zero_warned = True
                    on_log("the board reports 0 bytes to flash — the update agent found nothing to write. "
                           "This normally means the NR board is ALREADY on this firmware version; the "
                           "session will end cleanly WITHOUT re-flashing. Verify the version on the radio.",
                           "er")
                elif notified and expect and notified != expect:
                    on_log("the board announced " + str(notified) + " image bytes but this .ufw derives "
                           + str(expect) + " — it drives the pull so the flash continues, but this may "
                           "not be the package it expects", "er")
                on_log("TX " + _hex(NR_LEN_NOTIFY_REPLY) + " (LEN_NOTIFY reply)", "tx")
                link.send(NR_LEN_NOTIFY_REPLY)

            elif op == NR_OP["ALIVE"]:
                on_log("RX " + _hex(raw) + " UPDATE_ALIVE — no reply is known for this frame "
                       "(never seen in the reference capture); continuing", "er")

            else:
                raise FirmwareUpdateError(
                    "unknown NR opcode 0x" + format(op, "x") + " (" + _hex(raw)
                    + ") — this engine only knows 01/02/03/04/05/06. Stopping rather than guessing.")

        # Honest outcome: the board flashes only when it announced a non-zero
        # image length AND actually pulled the image region. If it announced 0
        # (or pulled far less than the image), nothing was written — say so
        # rather than implying a successful flash.
        flash_len = (manifest.get("flash_phase") or {}).get("length") or 0
        if announced_len == 0 or (flash_len and served_bytes < flash_len // 2):
            on_log("NR session ended successfully, but the NR image was NOT re-flashed: the board "
                   "pulled only " + str(served_bytes) + " byte(s)"
                   + (" and reported 0 bytes to flash" if announced_len == 0 else "")
                   + " (a full flash pulls ~" + str(flash_len) + "). This is the expected result when "
                   "the board is already on this version — verify the NR version on the radio. If it is "
                   "on an OLDER version, re-check the .ufw and that the board is in NR update mode.", "er")
        else:
            on_log("NR update complete: " + str(served_bytes) + " unique bytes served over " + str(reads)
                   + " read requests (planned " + str(total) + ")", "ok")
        on_progress(served_bytes, max(total, served_bytes), "done")
    finally:
        link.close()


# ═══════════════════════════════════════════════════════════════════════════
# SCT3288 baseband DSP — host push, its own framing
# ═══════════════════════════════════════════════════════════════════════════
# The host baud is NOT in the capture (its only control transfers are USBPcap's
# synthetic already-attached descriptor replay, so the port was open before the
# trace began). It is settled instead by two independent pieces of vendor
# evidence, both naming 38400 for THIS radio:
#   * "3288 base band upgrade.pdf", AnyTone's own D890 procedure: the Config
#     dialog is shown at "Port Rate: 38400", the Flash Update window header
#     reads "COM8  BaudRate: 38400", and the run log beneath it is a SUCCESSFUL
#     flash ("DownLoad UserFlash Code Successful!", vocoders 1-3, ...);
#   * the shipped SCT.ini persists "Baud Rate=38400" on all four [Port Setup]
#     profiles.
# 115200 is only the generic SiCOMM tool's own default (HPISerialPort.cs:11
# `_baudRate = 115200`, FormSelectPort.cs:211 combo default; the picker offers
# 115200/38400/19200/9600), i.e. what the dialog shows before an operator
# chooses. The tab still lets the operator pick either (gui_tab.py).
SCT_BAUD = 38400
SCT_ALT_BAUD = 115200
SCT_ACK_TIMEOUT_MS = 3000
SCT_ERASE_TIMEOUT_MS = 15000
# Once a reply's header is in hand the rest of it is already on the wire.
SCT_REPLY_BODY_TIMEOUT_MS = 1000
SCT_MAGIC = b"\x84\xa9\x61"
SCT_MAX_REPLY_LEN = 64          # every known reply body is 2 or 4 bytes
# Device reply "receive do command error, resend command again": body opcode
# 0x17 with sub-code 3 or 6. The vendor resends the frame ONCE and then gives
# up (SCT3252.cs:4479-4520; maxCount = 2 at WritFlash :2175 / InitFlash :2235).
SCT_NAK_OPCODE = 0x17
SCT_NAK_SUBCODES = (0x03, 0x06)
SCT_RESENDABLE = ("write", "flash_initial", "flash_end")


def plan_sct(artifact: bytes, manifest: dict) -> list[dict]:
    """Turn the manifest into an ordered [frame -> expected ACK] plan and prove
    it tiles the artifact exactly. Control ACKs are flattened in stream order."""
    if manifest.get("kind") != "sct3288_baseband":
        raise FirmwareUpdateError(
            'this is the "sct" engine but the manifest says kind "' + str(manifest.get("kind"))
            + '" — wrong component. Nothing was sent.')
    index = manifest.get("frame_index")
    if not isinstance(index, list) or not index:
        raise FirmwareUpdateError(
            "the manifest carries no frame_index — this engine cannot tell control frames from write "
            "frames, and their ACKs differ. Refusing.")
    session = manifest.get("session") or {}
    write_ack = session.get("write_ack")
    if not write_ack:
        raise FirmwareUpdateError("the manifest carries no session.write_ack — refusing to send unacked writes.")
    control_acks = []
    for c in (manifest.get("controls") or []):
        for a in (c.get("acks") or []):
            control_acks.append(a)

    plan = []
    cursor = 0
    ci = 0
    writes = 0
    for row in index:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            raise FirmwareUpdateError(
                "frame_index row " + repr(row) + " is not [offset, length, kind] — corrupt manifest.")
        off, length, kind = row[0], row[1], row[2]
        if off != cursor:
            raise FirmwareUpdateError(
                "frame_index is not contiguous: expected the next frame at " + str(cursor)
                + ", manifest says " + str(off) + " — corrupt manifest.")
        if length <= 0 or off + length > len(artifact):
            raise FirmwareUpdateError(
                "frame_index row [" + str(off) + ", " + str(length) + ", " + str(kind) + "] leaves the "
                + str(len(artifact)) + "-byte artifact — artifact/manifest mismatch.")
        if kind == "write":
            ack = write_ack
            writes += 1
        else:
            if ci >= len(control_acks):
                raise FirmwareUpdateError(
                    "frame_index holds more control frames than manifest.controls has ACKs ("
                    + str(len(control_acks)) + ") — corrupt manifest.")
            ack = control_acks[ci]
            ci += 1
        plan.append({"off": off, "len": length, "kind": kind, "ack": bytes.fromhex(ack)})
        cursor += length
    if cursor != len(artifact):
        raise FirmwareUpdateError(
            "frame_index covers " + str(cursor) + " of " + str(len(artifact)) + " artifact bytes — "
            "truncated stream or mismatched manifest.")
    if ci != len(control_acks):
        raise FirmwareUpdateError(
            "manifest.controls declares " + str(len(control_acks)) + " ACKs but frame_index has only "
            + str(ci) + " control frames — corrupt manifest.")
    if writes != manifest.get("frames"):
        raise FirmwareUpdateError(
            "frame_index holds " + str(writes) + " write frames but the manifest declares "
            + str(manifest.get("frames")) + " — corrupt manifest.")
    return plan


def _sct_read_frame(link: SerialLink, timeout_ms: int, what: str) -> bytes:
    """One complete 84 A9 61 reply, framed by its OWN length field.

    The SCT3288's replies are not all the same length, so a fixed-length read
    is unsafe: the opening `16 00` is answered 8 bytes long by a parity-OFF DSP
    and 10 bytes long by a parity-ON one (see _run_sct), and the mid-flash redo
    request has never been captured at all. Reading len(expected) bytes would
    strand the tail of a longer reply in the buffer and desync every ACK after
    it.

    The vendor's pad-to-even never applies here: every reply body is 2 or 4
    bytes, so every reply is 8 or 10 bytes long — even either way.
    """
    head = link.read_exactly(5, timeout_ms)
    if head[:3] != SCT_MAGIC:
        raise FirmwareUpdateError(
            what + ": reply does not begin with the 84 a9 61 magic (" + _hex(head)
            + ") — the link is out of frame. Stopping rather than guessing where "
            "the next reply starts.")
    length = (head[3] << 8) | head[4]
    if length < 1 or length > SCT_MAX_REPLY_LEN:
        raise FirmwareUpdateError(
            what + ": reply declares a " + str(length) + "-byte body (" + _hex(head)
            + "), which is outside anything this protocol sends. Stopping.")
    return head + link.read_exactly(1 + length, SCT_REPLY_BODY_TIMEOUT_MS)


def _sct_is_redo_request(frame: bytes) -> bool:
    """Is this the device asking us to send the last frame again?"""
    return (len(frame) >= 8 and frame[:3] == SCT_MAGIC
            and frame[6] == SCT_NAK_OPCODE and frame[7] in SCT_NAK_SUBCODES)


def _run_sct(port_name: str, artifact: bytes, manifest: dict, on_log, on_progress, abort,
             baud: int = SCT_BAUD, pace_ms: int = 0):
    plan = plan_sct(artifact, manifest)
    _verify_artifact(artifact, manifest, on_log)
    on_progress(0, len(plan), "handshake")

    # Frame 0 is the opening `16 00`, and the DSP's answer to it depends on the
    # parity state it is ALREADY in, which we do not control:
    #   * parity OFF (a fresh session)  -> 17 06, the vendor's "command error"
    #   * parity ON  (a previous run of ours aborted mid-session and left it on)
    #                                   -> the 10-byte echo, our ACK_SEG_PARITY
    # Our own compiler knows this: it emits this byte-identical frame twice with
    # two different expected ACKs (fwupd_sct compile_stream). Accepting only the
    # first made every in-tool Retry fail at frame 0 — and blame the baud. The
    # vendor gates on neither: SetupParityCheck reads the reply and discards it
    # (SCT3252.cs:5779-5789).
    #
    # The closing `16 00` is the same frame and carries the parity-ON reply, so
    # the plan's own last step supplies both the alternative ACK and the frame
    # to send on the way out. On a failure the vendor sends it too — every one
    # of its abort branches restores the entering parity state
    # (SCT3252.cs:3436/:3452/:3459/:3468) — so a later run does not meet a DSP
    # left in the other mode.
    alt_first_ack = None
    parity_restore_frame = None
    for step in plan:
        if step["kind"] == "parity_restore":
            alt_first_ack = step["ack"]
            parity_restore_frame = artifact[step["off"]:step["off"] + step["len"]]

    link = SerialLink(port_name, on_log=on_log, abort=abort)
    committed = False        # has an erase been issued? (nothing is recoverable after)
    try:
        # DTR/RTS: carried over from the browser flasher this engine ports
        # (channelBuddy assets/js/firmware-update-serial.js), which could not
        # deassert DTR even in principle. The vendor tool sets neither — it
        # assigns only PortName, BaudRate, StopBits and ReadBufferSize before
        # Open (HPISerialPort.cs:88-95), leaving both lines LOW — and it opens
        # 8N2, where we open 8N1 (SerialLink.open). Both differences are
        # untested on hardware and neither has ever been implicated in a
        # failure, so they are documented here rather than changed blind; the
        # stop-bit one is the one that actually rides in SET_LINE_CODING.
        link.open(baud, dtr=True, rts=True)
        link.flush()
        on_log("SCT3288: " + str(manifest.get("frames")) + " write frames + "
               + str(len(plan) - manifest.get("frames")) + " control frames, "
               + str(manifest.get("payload_bytes")) + " payload bytes, "
               + str(len(manifest.get("segments") or [])) + " erase segment(s)", "ok")

        writes_sent = 0
        for i, step in enumerate(plan):
            link.check_abort()
            kind = step["kind"]
            frame = artifact[step["off"]:step["off"] + step["len"]]
            is_erase = kind in ("flash_initial", "flash_end")
            is_last = i == len(plan) - 1
            timeout = SCT_ERASE_TIMEOUT_MS if is_erase else SCT_ACK_TIMEOUT_MS
            accepted = [step["ack"]]
            if i == 0 and alt_first_ack and alt_first_ack != step["ack"]:
                accepted.append(alt_first_ack)
            if kind != "write":
                on_log("TX " + kind + " " + _hex(frame), "tx")
            link.send(frame)
            if is_erase:
                # The moment the erase command LEAVES THE HOST, not when it is
                # acknowledged. The DSP answers a FLASH_INITIAL only after the
                # physical region erase (the vendor allows 15 s for it —
                # SCT3252.cs:2205), so a timeout or a bad reply here means the
                # erase is in flight or already done. Setting this after the ACK
                # would make a failed erase report "nothing on the baseband has
                # been changed", which is the one thing we must never tell an
                # operator about a half-erased DSP.
                committed = True

            attempt = 0
            while True:
                attempt += 1
                lenient = is_last and kind == "parity_restore"
                try:
                    r = _sct_read_frame(link, timeout, "frame " + str(i) + " (" + kind + ")")
                except AbortedError:
                    raise
                except FirmwareUpdateError as e:
                    # Two shapes reach here: no reply at all (ReplyTimeoutError)
                    # and a reply we cannot frame. Both are link-layer symptoms
                    # at frame 0, and both are harmless on the closing frame.
                    if lenient:
                        on_log(str(e) + " — but the flash itself is complete and acknowledged; "
                               "only the closing parity restore went unanswered. Continuing.", "er")
                        r = None
                        break
                    if i == 0:
                        raise FirmwareUpdateError(
                            str(e) + " The SCT3288 did not answer the opening frame in a form this "
                            "engine recognises. The most likely cause is the host baud: this run used "
                            + str(baud) + " — try "
                            + str(SCT_ALT_BAUD if baud == SCT_BAUD else SCT_BAUD) + " instead."
                            + _sct_state_note(committed))
                    raise FirmwareUpdateError(
                        str(e) + " — the SCT3288 stopped answering as expected."
                        + _sct_state_note(committed))
                if r in accepted:
                    break
                # The device explicitly asking for the frame again — the one
                # reply the vendor recovers from, and only for write/erase
                # frames. Checked AFTER the expected-ACK compare, because
                # frame 0's success ACK is itself a 17 06. Bounded to ONE
                # resend, exactly as the vendor's maxCount = 2.
                if (_sct_is_redo_request(r) and kind in SCT_RESENDABLE
                        and attempt == 1):
                    on_log("RX " + _hex(r) + " — the SCT3288 asked for frame " + str(i)
                           + " (" + kind + ") again; resending once", "er")
                    link.send(frame)
                    continue
                if lenient:
                    # Everything is already committed and ACKed; the vendor's
                    # counterpart only flips a host-side flag. Failing here
                    # would report a completed flash as a failure.
                    on_log("RX " + _hex(r) + " — unexpected reply to the closing parity restore. "
                           "The flash itself is complete and acknowledged; continuing.", "er")
                    break
                raise FirmwareUpdateError(
                    "frame " + str(i) + " (" + kind + "): expected "
                    + " or ".join(_hex(a) for a in accepted) + ", got " + _hex(r)
                    + "." + _sct_state_note(committed))

            if kind != "write" and r is not None:
                on_log("RX " + _hex(r), "rx")
            if kind == "write":
                writes_sent += 1
            if pace_ms:
                time.sleep(pace_ms / 1000.0)
            on_progress(i + 1, len(plan), "write" if kind == "write" else ("finish" if writes_sent else "handshake"))
        on_log("SCT3288 update complete: " + str(writes_sent) + " write frames acknowledged. This "
               "protocol has no read-back or verify step.", "ok")
        on_progress(len(plan), len(plan), "done")
    except FirmwareUpdateError:
        # Leave the DSP's parity where we found it so the operator's retry —
        # and any later vendor-tool session — opens against a known state.
        # This runs on an operator abort too (AbortedError is a
        # FirmwareUpdateError), and it has to bypass the abort gate to do it:
        # SerialLink.send() checks the abort flag first, so with the flag set
        # the restore would be swallowed by the except below and never reach
        # the wire — leaving parity ON in exactly the case that motivated it.
        # One 10-byte control frame after an abort is what the vendor's own
        # failure exits send (SCT3252.cs:3436/:3452/:3459/:3468). It does NOT
        # send the vendor's preceding FLASH_END: issuing an erase-family
        # command on the way out of a failure is not something any capture
        # justifies.
        if parity_restore_frame is not None and link.ser is not None:
            on_log("TX parity_restore " + _hex(parity_restore_frame)
                   + " (restoring the DSP's parity state on the way out)", "tx")
            saved_abort, link.abort = link.abort, None
            try:
                link.send(parity_restore_frame)
            except Exception:
                on_log("could not send the parity restore — the DSP may be left with parity ON. "
                       "The next run's opening frame accepts either state, so this is recoverable.", "er")
            finally:
                link.abort = saved_abort
        raise
    finally:
        link.close()


def _sct_state_note(committed: bool) -> str:
    """What the operator can conclude about the baseband's state."""
    if committed:
        return (" Stopping. NOT retrying from here: an erase is already committed, so this "
                "component must be re-flashed from the start.")
    return (" Stopping. No erase has been issued yet, so nothing on the baseband has been "
            "changed — it is safe to try again.")


# ── public surface ──────────────────────────────────────────────────────────
def run(kind: str, port_name: str, artifact, manifest: dict,
        on_log=None, on_progress=None, abort=None, **opts):
    """Drive one update. `kind` in {"fw","icon","nr","sct"}. Raises
    FirmwareUpdateError (or AbortedError) on any fault; the message is
    operator-facing. Blocking — call on a worker thread."""
    on_log = on_log or (lambda m, c="info": None)
    on_progress = on_progress or (lambda d, t, p: None)
    artifact = bytes(artifact)
    if kind in ("fw", "icon", "aprs"):
        _run_cps(kind, port_name, artifact, manifest, on_log, on_progress, abort,
                 pace_ms=opts.get("pace_ms", 0))
    elif kind == "nr":
        _run_nr(port_name, artifact, manifest, on_log, on_progress, abort)
    elif kind == "sct":
        _run_sct(port_name, artifact, manifest, on_log, on_progress, abort,
                 baud=opts.get("baud") or SCT_BAUD, pace_ms=opts.get("pace_ms", 0))
    else:
        raise FirmwareUpdateError('unknown update kind "' + str(kind) + '"')
