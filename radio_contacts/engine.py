"""Write a contact bundle over the codeplug PC-mode protocol.

A pyserial port of the browser driver's contact path (enterProgram → readId →
one W-frame per 16-byte block with a 1:1 ACK → END), on the same SerialLink
the firmware engines use. Unlike those bootloader protocols this one is
idempotent per frame — a missing ACK is answered by RESENDING the same frame
(up to five times), exactly as the browser and the factory CPS do.

    TX "PROGRAM"                         RX 51 58 06                 ("QX"+ACK)
    TX 02                                RX 16 bytes … [15]=06       identity
    TX 57 AA AA AA AA 10 <16 data> CK 06 RX 06                       one per block
    TX "END"                             RX 06                       commits

CK = (sum of the 4 address bytes + 0x10 + the 16 data bytes) & 0xFF; the 0x57
is not summed. Contact sectors are separate from the codeplug: a contact-only
session never touches the radio's configuration, and an interrupted one is
recoverable by running it again.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

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
ACK_TIMEOUT_MS = 600
END_TIMEOUT_MS = 4000
WRITE_ATTEMPTS = 5
STRAY_BYTES_PER_ATTEMPT = 8
PROGRESS_EVERY_BLOCKS = 256


class ContactWriteError(FirmwareUpdateError):
    """A protocol failure during a contact session. Operator-facing message."""


class NotInPcModeError(ContactWriteError):
    """The radio did not answer "PROGRAM" with "QX"+ACK."""


@dataclass(frozen=True)
class Ident:
    model: str        # "D878UV2" — the ID frame after its leading 'I'
    version: str      # "V101" or "" when not printable
    band: int         # the byte after the model string (band-plan mode)
    raw: bytes


# ── frames ───────────────────────────────────────────────────────────────────
def checksum(addr: int, data: bytes) -> int:
    a = addr & 0xFFFFFFFF
    return (sum(a.to_bytes(4, "big")) + len(data) + sum(data)) & 0xFF


def frame(addr: int, data: bytes) -> bytes:
    if len(data) != WRITE_LL:
        raise ContactWriteError(f"a contact block must be {WRITE_LL} bytes, got {len(data)}")
    a = (addr & 0xFFFFFFFF).to_bytes(4, "big")
    return b"\x57" + a + bytes([WRITE_LL]) + bytes(data) + bytes([checksum(addr, data), ACK])


def parse_ident(raw: bytes) -> Ident:
    """The 16-byte identity frame: 'I' + model + band byte + version + … + 06."""
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


# ── session primitives ───────────────────────────────────────────────────────
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
        # A bare 06 is the UPDATE/PROGRAM bootloader (firmware mode), not the codeplug protocol.
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


def _exit_program(link: SerialLink, on_log, timeout_ms: int = END_TIMEOUT_MS) -> None:
    on_log('TX "END"', "tx")
    link.send(END)
    r = link.read_exactly(1, timeout_ms)
    on_log("RX " + _hex(r), "rx")
    if r[0] != ACK:
        raise ContactWriteError("END was not acknowledged (" + _hex(r) + ")")
    on_log("left PC mode — writes, if any, are now committed", "ok")


def _write_block(link: SerialLink, addr: int, data: bytes, on_log) -> int:
    """Send one W-frame and wait for its ACK; resend on silence (idempotent).
    Returns the number of attempts it took."""
    f = frame(addr, data)
    for attempt in range(1, WRITE_ATTEMPTS + 1):
        if attempt > 1:
            link.flush()
            time.sleep(0.02)
            on_log(f"  write retry {attempt}/{WRITE_ATTEMPTS} @0x{addr:08x}", "info")
        link.send(f)
        strays = 0
        deadline = time.monotonic() + ACK_TIMEOUT_MS / 1000.0
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
                break                       # timeout → resend
            if b[0] == ACK:
                return attempt
            strays += 1                     # drop a stray non-ACK byte, keep looking
            if strays > STRAY_BYTES_PER_ATTEMPT:
                break
    raise ContactWriteError(f"no ACK for the block at 0x{addr:08x} after {WRITE_ATTEMPTS} attempts — "
                            "the radio stopped answering. Check the cable, power-cycle the radio and "
                            "run the refresh again (a partial contact list is safe to overwrite).")


# ── public entry points ──────────────────────────────────────────────────────
def identify(port_name: str, on_log: Callable[[str, str], None],
             abort: Optional[threading.Event] = None) -> Ident:
    """Open the port, enter PC mode, read the identity, leave PC mode."""
    link = SerialLink(port_name, on_log=on_log, abort=abort)
    try:
        link.open(BAUD, dtr=True, rts=True)
        link.flush()
        _enter_program(link, on_log)
        ident = _read_ident(link, on_log)
        _exit_program(link, on_log)
        return ident
    finally:
        link.close()


def write_contacts(port_name: str, plan: Iterable[seg.Segment],
                   on_log: Callable[[str, str], None],
                   on_progress: Callable[[int, int, str], None],
                   abort: Optional[threading.Event] = None,
                   expect_model: Optional[str] = None,
                   pace_ms: int = 0) -> dict:
    """One PROGRAM…END session writing every block of `plan` in address order.

    `expect_model`, when given, must equal the identity the radio reports NOW
    (the write refuses to run against a radio other than the one the operator
    connected and chose the list for). On abort or a wire error the session is
    still ended with END so the radio leaves PC mode; the error says to re-run.
    Returns {blocks, frames, seconds, retries}.
    """
    segs = list(plan)
    total = seg.block_count(segs)
    if total == 0:
        raise ContactWriteError("the contact bundle is empty — nothing to write")
    on_progress(0, total, "handshake")
    link = SerialLink(port_name, on_log=on_log, abort=abort)
    written = 0
    frames = 0
    retries = 0
    t0 = time.monotonic()
    in_session = False
    try:
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
        # Leave the radio in a sane state: END commits whatever was written and
        # drops the radio out of PC mode. A partial contact DB is harmless —
        # the next refresh overwrites it — and the codeplug was never touched.
        if in_session:
            try:
                on_log("ending the session so the radio leaves PC mode "
                       "(the contact list is only partly written — run the refresh again)", "er")
                link.abort = None          # the operator's abort must not block this last exchange
                _exit_program(link, on_log, timeout_ms=2000)
            except BaseException:  # noqa: BLE001
                on_log("the radio did not acknowledge END — power-cycle it, then run the refresh again", "er")
        if isinstance(e, AbortedError):
            raise AbortedError("aborted by the operator after " + f"{written:,} of {total:,} blocks — "
                               "run the refresh again to complete the contact list")
        raise
    finally:
        link.close()
