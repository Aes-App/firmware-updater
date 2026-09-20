from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Tuple

from radio_fw.engines import AbortedError, FirmwareUpdateError, SerialLink

from . import segments as seg

ACK = 0x06
BAUD = 921600
HANDSHAKE = b"PROGRAM"
HANDSHAKE_REPLY = b"QX\x06"
IDENT_QUERY = b"\x02"
END = b"END"
WRITE_LL = 16
HANDSHAKE_TIMEOUT_MS = 2500
IDENT_TIMEOUT_MS = 2500
ACK_TIMEOUT_MS = (1500, 5000, 15000)
END_TIMEOUT_MS = 4000
WRITE_ATTEMPTS = len(ACK_TIMEOUT_MS)
DUPLICATE_ACK_MS = 150
STRAY_BYTES_PER_ATTEMPT = 8
PROGRESS_EVERY_BLOCKS = 256


class ContactWriteError(FirmwareUpdateError):
    pass


class NotInPcModeError(ContactWriteError):
    pass


@dataclass(frozen=True)
class Ident:
    model: str
    version: str
    band: int
    raw: bytes


def checksum(addr: int, data: bytes) -> int:
    a = addr & 0xFFFFFFFF
    return (sum(a.to_bytes(4, "big")) + len(data) + sum(data)) & 0xFF


def frame(addr: int, data: bytes) -> bytes:
    if len(data) != WRITE_LL:
        raise ContactWriteError(f"a contact block must be {WRITE_LL} bytes, got {len(data)}")
    a = (addr & 0xFFFFFFFF).to_bytes(4, "big")
    return b"\x57" + a + bytes([WRITE_LL]) + bytes(data) + bytes([checksum(addr, data), ACK])


def parse_ident(raw: bytes) -> Ident:
    if len(raw) != 16:
        raise ContactWriteError(f"identity frame is {len(raw)} bytes, expected 16")
    if raw[15] != ACK:
        raise ContactWriteError("the identity frame did not end in 06 (" + raw.hex() + ") — "
                                "unreadable radio identity, refusing to write.")
    model = ""
    i = 1
    while i < 15:
        b = raw[i]
        if b == 0 or b < 0x20 or b >= 0x7F:
            break
        model += chr(b)
        i += 1
    band = raw[i] if i < 15 else 0
    rest = raw[i + 1:15]
    version = ""
    for b in rest:
        if 0x20 <= b < 0x7F:
            version += chr(b)
        elif version:
            break
    return Ident(model=model, version=version.strip(), band=band, raw=bytes(raw))


def _hex(b: bytes) -> str:
    return bytes(b).hex()


def _enter_program(link: SerialLink, on_log) -> None:
    on_log('TX "PROGRAM"', "tx")
    link.send(HANDSHAKE)
    try:
        first = link.read_exactly(1, HANDSHAKE_TIMEOUT_MS)
    except AbortedError:
        raise
    except FirmwareUpdateError as e:
        raise NotInPcModeError(
            "no reply to \"PROGRAM\" — is the radio switched on, connected by USB, and is this its "
            "COM port? (" + str(e) + ")")
    if first[0] == ACK:
        raise NotInPcModeError(
            "the radio answered a bare 06 — it is in firmware-update mode, not normal PC mode. "
            "Power-cycle it normally and reconnect. Nothing was written.")
    tail = b""
    try:
        tail = link.read_exactly(2, 800)
    except FirmwareUpdateError:
        pass
    reply = first + tail
    on_log("RX " + _hex(reply), "rx")
    if reply != HANDSHAKE_REPLY:
        raise NotInPcModeError(
            "unexpected reply to \"PROGRAM\": " + _hex(reply) + " (expected 51 58 06). The radio is not "
            "in normal PC mode, or this is not the radio's port. Nothing was written.")
    on_log("PC mode entered", "ok")


def _read_ident(link: SerialLink, on_log) -> Ident:
    on_log("TX 02 (identity)", "tx")
    link.send(IDENT_QUERY)
    raw = link.read_exactly(16, IDENT_TIMEOUT_MS)
    on_log("RX " + _hex(raw), "rx")
    ident = parse_ident(raw)
    on_log(f"radio identifies as {ident.model or '?'} {ident.version} (band plan {ident.band})", "ok")
    return ident


def _exit_program(link: SerialLink, on_log, timeout_ms: int = END_TIMEOUT_MS,
                  wrote: bool = True) -> None:
    on_log('TX "END"', "tx")
    link.send(END)
    r = link.read_exactly(1, timeout_ms)
    on_log("RX " + _hex(r), "rx")
    if r[0] != ACK:
        raise ContactWriteError("END was not acknowledged (" + _hex(r) + ")")
    on_log("left PC mode — writes, if any, are now committed" if wrote
           else "left PC mode — nothing was written; the radio restarts on END",
           "ok")


def _swallow_duplicate_ack(link: SerialLink, on_log) -> None:
    try:
        b = link.read_exactly(1, DUPLICATE_ACK_MS)
    except AbortedError:
        raise
    except FirmwareUpdateError:
        return
    if b[0] == ACK:
        on_log("  swallowed the duplicate ACK from a re-sent frame", "info")


def _write_block(link: SerialLink, addr: int, data: bytes, on_log) -> int:
    f = frame(addr, data)
    for attempt in range(1, WRITE_ATTEMPTS + 1):
        if attempt > 1:
            link.flush()
            time.sleep(0.02)
            on_log(f"  write retry {attempt}/{WRITE_ATTEMPTS} @0x{addr:08x}", "info")
        link.send(f)
        strays = 0
        deadline = time.monotonic() + ACK_TIMEOUT_MS[attempt - 1] / 1000.0
        while True:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            try:
                b = link.read_exactly(1, remaining_ms)
            except AbortedError:
                raise
            except FirmwareUpdateError as e:
                if "serial read failed" in str(e):
                    raise
                break
            if b[0] == ACK:
                if attempt > 1:
                    _swallow_duplicate_ack(link, on_log)
                return attempt
            strays += 1
            if strays > STRAY_BYTES_PER_ATTEMPT:
                break
    raise ContactWriteError(f"no ACK for the block at 0x{addr:08x} after {WRITE_ATTEMPTS} attempts — "
                            "the radio stopped answering. Check the cable, power-cycle the radio and "
                            "run the refresh again (a partial contact list is safe to overwrite).")


def open_session(port_name: str, on_log: Callable[[str, str], None],
                 abort: Optional[threading.Event] = None) -> Tuple[Ident, SerialLink]:
    link = SerialLink(port_name, on_log=on_log, abort=abort)
    try:
        link.open(BAUD, dtr=True, rts=True)
        link.flush()
        _enter_program(link, on_log)
        ident = _read_ident(link, on_log)
        return ident, link
    except BaseException:
        try:
            link.close()
        except BaseException:
            pass
        raise


def close_session(link: Optional[SerialLink], on_log: Callable[[str, str], None]) -> None:
    if link is None:
        return
    try:
        link.abort = None
        _exit_program(link, on_log, timeout_ms=2000, wrote=False)
    except BaseException:
        on_log("the radio did not acknowledge END — if its display still says PC mode, "
               "power-cycle it", "er")
    finally:
        try:
            link.close()
        except BaseException:
            pass


def identify(port_name: str, on_log: Callable[[str, str], None],
             abort: Optional[threading.Event] = None) -> Ident:
    ident, link = open_session(port_name, on_log, abort)
    try:
        _exit_program(link, on_log, wrote=False)
        return ident
    finally:
        link.close()


def write_contacts(port_name: str, plan: Iterable[seg.Segment],
                   on_log: Callable[[str, str], None],
                   on_progress: Callable[[int, int, str], None],
                   abort: Optional[threading.Event] = None,
                   expect_model: Optional[str] = None,
                   pace_ms: int = 0,
                   link: Optional[SerialLink] = None) -> dict:
    segs = list(plan)
    total = seg.block_count(segs)
    if total == 0:
        raise ContactWriteError("the contact bundle is empty — nothing to write")
    on_progress(0, total, "handshake")
    written = 0
    frames = 0
    retries = 0
    t0 = time.monotonic()
    in_session = False

    ident = None
    if link is not None:
        link.on_log = on_log
        link.abort = abort
        try:
            link.flush()
            ident = _read_ident(link, on_log)
            in_session = True
        except BaseException:
            on_log("the held session did not answer — starting a fresh one", "info")
            try:
                link.close()
            except BaseException:
                pass
            link = None
            ident = None

    if link is None:
        link = SerialLink(port_name, on_log=on_log, abort=abort)
    try:
        if not in_session:
            link.open(BAUD, dtr=True, rts=True)
            link.flush()
            _enter_program(link, on_log)
            in_session = True
            ident = _read_ident(link, on_log)
        if expect_model and ident.model != expect_model:
            raise ContactWriteError(
                f'this radio identifies as "{ident.model}" but the list was chosen for "{expect_model}" — '
                "WRONG RADIO. Nothing was written. Connect again and pick the list for this radio.")
        link.flush()
        on_log(f"writing {total:,} blocks ({seg.describe(segs)}) — one 16-byte frame per ACK", "ok")
        for addr, data in seg.iter_blocks(segs):
            link.check_abort()
            attempts = _write_block(link, addr, data, on_log)
            retries += attempts - 1
            frames += attempts
            written += 1
            if pace_ms:
                time.sleep(pace_ms / 1000.0)
            if (written % PROGRESS_EVERY_BLOCKS) == 0 or written == total:
                on_progress(written, total, "write")
        secs = time.monotonic() - t0
        on_log(f"all {total:,} blocks acknowledged in {secs:.1f} s"
               + (f" ({retries} resend(s))" if retries else ""), "ok")
        on_progress(total, total, "finish")
        _exit_program(link, on_log)
        in_session = False
        on_progress(total, total, "done")
        return {"blocks": written, "frames": frames, "seconds": secs, "retries": retries}
    except BaseException as e:
        if in_session:
            try:
                on_log("ending the session so the radio leaves PC mode"
                       + (" (the contact list is only partly written — run the refresh again)"
                          if written else " (nothing was written)"), "er")
                link.abort = None
                _exit_program(link, on_log, timeout_ms=2000, wrote=bool(written))
            except BaseException:
                on_log("the radio did not acknowledge END — power-cycle it, then run the refresh again", "er")
        if isinstance(e, AbortedError):
            raise AbortedError("aborted by the operator after " + f"{written:,} of {total:,} blocks — "
                               "run the refresh again to complete the contact list")
        raise
    finally:
        link.close()
